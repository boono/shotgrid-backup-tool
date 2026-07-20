#!/usr/bin/env python3
"""Local-only web application for ShotGrid backup."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit

from audit import count_active_records, count_records
from backup import (
    create_client,
    discover_entities,
    ensure_private_directory,
    entity_supports_retirement,
    run_backup,
    safe_error,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UI_ROOT = Path(__file__).resolve().parent / "ui"
SESSION_TOKEN = secrets.token_urlsafe(32)
PROXY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+:\d{1,5}$")
AUTO_WORKERS = min(8, max(4, os.cpu_count() or 4))
CREDENTIAL_LOCK = threading.Lock()
CREDENTIALS: dict[str, dict[str, Any]] = {}


class JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.status = "idle"
        self.phase = "等待检查"
        self.started = 0.0
        self.completed_at: float | None = None
        self.entities_total = 0
        self.entities_done = 0
        self.records_total = 0
        self.records_done = 0
        self.attachments_total = 0
        self.attachments_done = 0
        self.bytes_done = 0
        self.errors = 0
        self.result_path: str | None = None
        self.message = ""
        self.logs: list[str] = []

    def begin(self, expected_counts: dict[str, Any]) -> None:
        with self.lock:
            if self.status == "running":
                raise RuntimeError("已有备份正在运行")
            self.reset()
            self.status = "running"
            self.phase = "准备快照"
            self.started = time.monotonic()
            self.entities_total = len(expected_counts)
            self.records_total = sum(
                int(value.get("active", 0)) + int(value.get("retired", 0))
                for value in expected_counts.values()
            )
            self.logs.append("开始备份任务")

    def event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        with self.lock:
            if name == "backup_started":
                self.phase = "导出实体"
                self.entities_total = int(event.get("entities_total", self.entities_total))
                self.logs.append(
                    f"实体导出启动：{self.entities_total} 类，{event.get('workers', '?')} workers"
                )
            elif name == "entity_page":
                count = int(event.get("records", 0))
                self.records_done += count
                self.phase = f"导出 {event.get('entity')}"
            elif name == "entity_complete":
                self.entities_done += 1
                self.logs.append(
                    f"{event.get('entity')} 完成：active={event.get('active', 0)} "
                    f"retired={event.get('retired', 0)}"
                )
            elif name == "entity_error":
                self.errors += 1
                error = event.get("error") or {}
                self.logs.append(
                    f"{event.get('entity')} 失败：{error.get('type', 'Error')}: "
                    f"{error.get('message', 'unknown')}"
                )
            elif name == "attachment_plan":
                self.phase = "下载附件"
                self.attachments_total += int(event.get("total", 0))
                self.logs.append(f"附件下载队列：{self.attachments_total}")
            elif name == "attachment_complete":
                self.attachments_done += 1
                self.bytes_done += int(event.get("size", 0))
            elif name == "attachment_error":
                self.attachments_done += 1
                self.errors += 1
                self.logs.append(f"附件 {event.get('attachment_id')} 下载失败")
            elif name == "media_plan":
                total = int(event.get("total", 0))
                self.phase = "下载 ShotGrid 媒体"
                self.attachments_total += total
                self.logs.append(f"图片 / filmstrip / 上传媒体队列：{total}")
            elif name == "media_complete":
                self.attachments_done += 1
                self.bytes_done += int(event.get("size", 0))
            elif name == "media_error":
                self.attachments_done += 1
                self.errors += 1
                self.logs.append(
                    f"媒体 {event.get('entity')}:{event.get('source_id')}.{event.get('field')} 下载失败"
                )
            elif name == "backup_complete":
                self.result_path = event.get("path")
            elif name == "backup_error":
                self.errors += int(event.get("errors", 1))
            self.logs = self.logs[-200:]

    def finish(self, result_path: str) -> None:
        with self.lock:
            self.status = "complete"
            self.phase = "备份完成"
            self.completed_at = time.monotonic()
            self.result_path = result_path
            self.message = "快照已原子发布"
            self.logs.append(f"备份完成：{result_path}")

    def fail(self, error: dict[str, str]) -> None:
        with self.lock:
            self.status = "failed"
            self.phase = "备份失败"
            self.completed_at = time.monotonic()
            self.message = f"{error['type']}: {error['message']}"
            self.logs.append(f"备份失败：{self.message}")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            elapsed = 0.0
            if self.started:
                elapsed = (self.completed_at or time.monotonic()) - self.started
            record_ratio = min(1.0, self.records_done / self.records_total) if self.records_total else 0.0
            entity_ratio = min(1.0, self.entities_done / self.entities_total) if self.entities_total else 0.0
            metadata_ratio = max(record_ratio, entity_ratio)
            if self.attachments_total:
                attachment_ratio = min(1.0, self.attachments_done / self.attachments_total)
                progress = metadata_ratio * 0.72 + attachment_ratio * 0.28
            else:
                progress = metadata_ratio * (1.0 if self.status == "complete" else 0.92)
            if self.status == "complete":
                progress = 1.0
            eta = None
            if self.status == "running" and progress > 0.02:
                eta = max(0.0, elapsed * (1.0 - progress) / progress)
            return {
                "status": self.status,
                "phase": self.phase,
                "elapsed_seconds": round(elapsed, 1),
                "eta_seconds": round(eta, 1) if eta is not None else None,
                "progress": round(progress, 4),
                "entities": {"done": self.entities_done, "total": self.entities_total},
                "records": {"done": self.records_done, "total": self.records_total},
                "attachments": {"done": self.attachments_done, "total": self.attachments_total},
                "bytes_done": self.bytes_done,
                "errors": self.errors,
                "result_path": self.result_path,
                "message": self.message,
                "logs": list(self.logs),
            }


STATE = JobState()


def normalize_site_url(value: Any) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("站点地址必须是 https:// 开头的 ShotGrid origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("站点地址不能包含账号、密码、query 或 fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("站点地址只能填写 origin，不能包含额外路径")
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{parsed.hostname}{port}"


def validate_settings(payload: dict[str, Any], require_key: bool = True) -> dict[str, Any]:
    site_url = normalize_site_url(payload.get("site_url"))
    script_name = str(payload.get("script_name", "")).strip()
    script_key = str(payload.get("script_key", "")).strip()
    proxy = str(payload.get("http_proxy", "")).strip() or None
    output_text = str(payload.get("output", ".local/backups")).strip()
    if not script_name or (require_key and not script_key):
        raise ValueError("Script Name 和 API Key 不能为空")
    if proxy and not PROXY_PATTERN.match(proxy):
        raise ValueError("代理格式应为 host:port，不能包含协议或账号密码")
    if proxy and not 1 <= int(proxy.rsplit(":", 1)[1]) <= 65535:
        raise ValueError("代理端口必须在 1-65535 范围内")
    output = Path(output_text).expanduser()
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    if output.is_symlink():
        raise ValueError("输出目录不能是符号链接")
    return {
        "site_url": site_url,
        "script_name": script_name,
        "script_key": script_key,
        "http_proxy": proxy,
        "output": Path(os.path.abspath(output)),
        "workers": AUTO_WORKERS,
        "download_attachments": True,
        "updated_since": None,
    }


def store_credential(settings: dict[str, Any]) -> str:
    handle = secrets.token_urlsafe(32)
    with CREDENTIAL_LOCK:
        now = time.monotonic()
        expired = [key for key, value in CREDENTIALS.items() if value["expires"] <= now]
        for key in expired:
            CREDENTIALS.pop(key, None)
        CREDENTIALS[handle] = {
            "expires": now + 600,
            "site_url": settings["site_url"],
            "script_name": settings["script_name"],
            "script_key": settings["script_key"],
            "http_proxy": settings["http_proxy"],
        }
    return handle


def consume_credential(payload: dict[str, Any], settings: dict[str, Any]) -> str:
    handle = str(payload.get("credential_handle", ""))
    with CREDENTIAL_LOCK:
        credential = CREDENTIALS.pop(handle, None)
    if not credential or credential["expires"] <= time.monotonic():
        raise RuntimeError("检查凭据已过期，请重新运行完整检查")
    for key in ("site_url", "script_name", "http_proxy"):
        if credential[key] != settings[key]:
            raise RuntimeError("连接设置已改变，请重新运行完整检查")
    return str(credential["script_key"])


def check_output(output: Path) -> dict[str, Any]:
    if output.exists() and output.is_symlink():
        raise RuntimeError("输出目录不能是符号链接")
    ensure_private_directory(output)
    with tempfile.NamedTemporaryFile(prefix=".write_check_", dir=output, delete=True):
        pass
    usage = shutil.disk_usage(output)
    return {"path": str(output), "free_bytes": usage.free, "writable": True}


def preflight(payload: dict[str, Any]) -> dict[str, Any]:
    settings = validate_settings(payload)
    try:
        output = check_output(settings["output"])
        client = create_client(
            settings["site_url"], settings["script_name"], settings["script_key"], settings["http_proxy"]
        )
        server = client.info()
        schema = client.schema_entity_read()
        entities = discover_entities(schema)
        client.find_one("Project", [], ["id"], include_archived_projects=True)

        def count_entity(entity: str) -> tuple[str, dict[str, int], bool]:
            worker = create_client(
                settings["site_url"], settings["script_name"], settings["script_key"], settings["http_proxy"]
            )
            fields = list(worker.schema_field_read(entity))
            worker.find_one(
                entity, [], fields or ["id"],
                retired_only=False, include_archived_projects=True,
            )
            supports_retirement = entity_supports_retirement(worker, entity)
            return entity, {
                "active": count_active_records(worker, entity, 2),
                "retired": (
                    count_records(worker, entity, 2, retired_only=True)
                    if supports_retirement else 0
                ),
            }, supports_retirement

        counts: dict[str, dict[str, int]] = {}
        retirement_support: dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=min(settings["workers"], len(entities))) as executor:
            futures = [executor.submit(count_entity, entity) for entity in entities]
            for future in as_completed(futures):
                entity, value, supported = future.result()
                counts[entity] = value
                retirement_support[entity] = supported
        handle = store_credential(settings)
        return {
            "ok": True,
            "credential_handle": handle,
            "checks": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "authenticated": True,
                "schema_entities": len(schema),
                "server_version": server.get("full_version") or server.get("version"),
                "output": output,
                "workers": settings["workers"],
            },
            "entities": entities,
            "retirement_support": dict(sorted(retirement_support.items())),
            "counts": dict(sorted(counts.items())),
        }
    finally:
        settings["script_key"] = ""


def start_job(payload: dict[str, Any]) -> None:
    settings = validate_settings(payload, require_key=False)
    settings["script_key"] = consume_credential(payload, settings)
    expected_counts = payload.get("expected_counts") or {}
    STATE.begin(expected_counts)

    def worker() -> None:
        try:
            sg = create_client(
                settings["site_url"], settings["script_name"], settings["script_key"], settings["http_proxy"]
            )
            args = Namespace(
                entities=None,
                output=settings["output"],
                updated_since=settings["updated_since"],
                no_attachments=not settings["download_attachments"],
                workers=settings["workers"],
            )
            config = {
                "workers": settings["workers"],
                "all_readable": True,
                "http_proxy": settings["http_proxy"],
                "include_retired": True,
                "download_attachments": settings["download_attachments"],
                "page_size": 500,
                "max_retries": 4,
            }
            factory = lambda: create_client(
                settings["site_url"], settings["script_name"], settings["script_key"], settings["http_proxy"]
            )
            result = run_backup(sg, args, config, client_factory=factory, progress=STATE.event)
            STATE.finish(str(result))
        except Exception as error:
            STATE.fail(safe_error(error, [settings.get("script_key", "")]))
        finally:
            settings["script_key"] = ""

    threading.Thread(target=worker, name="shotgrid-backup-job", daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "ShotGridBackup/1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def json_response(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Backup-Token", ""), SESSION_TOKEN)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 128 * 1024:
            raise ValueError("请求过大")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON object")
        return value

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            if not self.authorized():
                self.json_response({"error": "unauthorized"}, HTTPStatus.FORBIDDEN)
                return
            self.json_response(STATE.snapshot())
            return
        name = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        target = (UI_ROOT / name).resolve()
        if UI_ROOT not in target.parents and target != UI_ROOT:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = "text/html; charset=utf-8"
        if target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif target.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self.authorized():
            self.json_response({"error": "unauthorized"}, HTTPStatus.FORBIDDEN)
            return
        try:
            payload = self.read_json()
            if self.path == "/api/preflight":
                self.json_response(preflight(payload))
            elif self.path == "/api/start":
                start_job(payload)
                self.json_response({"ok": True})
            elif self.path == "/api/open-output":
                result = STATE.snapshot().get("result_path")
                if not result or not Path(result).exists():
                    raise RuntimeError("没有可打开的完成快照")
                if platform.system() == "Darwin":
                    subprocess.Popen(["open", result])
                elif platform.system() == "Windows":
                    os.startfile(result)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", result])
                self.json_response({"ok": True})
            else:
                self.json_response({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            safe = safe_error(error, [str(locals().get("payload", {}).get("script_key", ""))])
            self.json_response(
                {"error": f"{safe['type']}: {safe['message']}"}, HTTPStatus.BAD_REQUEST
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="ShotGrid Backup 本地 UI")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--print-session-url", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}/"
    url = f"{base_url}#token={SESSION_TOKEN}"
    print(f"ShotGrid Backup App: {base_url}", flush=True)
    if args.print_session_url:
        print(f"Session URL: {url}", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
