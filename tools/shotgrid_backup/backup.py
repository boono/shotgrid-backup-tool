#!/usr/bin/env python3
"""Back up readable ShotGrid data to an atomic local snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable


TOOL_VERSION = "1.0.1"
DEFAULT_ENTITIES = [
    "Project", "Asset", "Episode", "Sequence", "Shot", "Task",
    "Version", "Note", "Playlist", "Attachment",
]
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
SENSITIVE_TEXT = re.compile(
    r"(?i)(authorization|api[_-]?key|script[_-]?key|client_secret|access_token|"
    r"refresh_token|cookie|password|token)(\s*[:=]\s*)([^\s&,;]+)"
)
# Error strings sometimes contain unescaped spaces in file/NAS URLs. Match to a
# hard punctuation boundary so the remainder of such paths cannot leak.
URL_TEXT = re.compile(
    r"(?i)\b(?:https?|file|smb|afp|nfs)://[^,;，；\r\n<>\"']+"
)
BARE_PROXY_CREDENTIALS = re.compile(
    r"(?i)(?<![A-Za-z0-9._-])[^:@/\s]+:[^@/\s]+@"
    r"(?=(?:\[[0-9A-F:]+\]|[A-Za-z0-9.-]+)(?::\d{1,5})?(?:\s|$|[,;]))"
)
QUOTED_TEXT = re.compile(r"(?P<quote>['\"])(?P<content>.*?)(?P=quote)")
WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:[\\/]|\\\\)[^,;，；\r\n<>\"']+"
)
POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_:])/(?:[^,;，；\r\n<>\"']+)"
)


def is_ignorable_platform_metadata(relative: Any) -> bool:
    """Return whether a path is Finder metadata, never ShotGrid backup data."""
    name = str(relative).replace("\\", "/").rsplit("/", 1)[-1]
    return name == ".DS_Store"


def json_value(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def _safe_url(value: str) -> str:
    """Keep only a URL origin; never persist credentials or signed path material."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        scheme = value.split(":", 1)[0].lower()
        return f"{scheme}://[URL_REDACTED]"
    if parsed.scheme.lower() == "file":
        return "file://[PATH_REDACTED]"
    try:
        host = parsed.hostname or "[HOST_REDACTED]"
    except ValueError:
        host = "[HOST_REDACTED]"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        port = ""
    return f"{parsed.scheme.lower()}://{host}{port}/[URL_REDACTED]"


def _redact_urls_and_paths(value: str) -> str:
    """Remove URL secrets and local, Windows or NAS absolute path details."""
    safe_urls: list[str] = []

    def replace_url(match: re.Match[str]) -> str:
        marker = f"SAFEURLTOKEN{len(safe_urls)}ENDTOKEN"
        safe_urls.append(_safe_url(match.group(0)))
        return marker

    value = URL_TEXT.sub(replace_url, value)
    value = BARE_PROXY_CREDENTIALS.sub("[CREDENTIALS_REDACTED]@", value)

    def replace_quoted(match: re.Match[str]) -> str:
        content = match.group("content")
        if (
            content.startswith(("/", "\\\\"))
            or re.match(r"(?i)^[A-Z]:[\\/]", content)
        ):
            return f"{match.group('quote')}[PATH_REDACTED]{match.group('quote')}"
        return match.group(0)

    value = QUOTED_TEXT.sub(replace_quoted, value)
    value = WINDOWS_ABSOLUTE_PATH.sub("[PATH_REDACTED]", value)
    value = POSIX_ABSOLUTE_PATH.sub("[PATH_REDACTED]", value)
    for index, safe_url in enumerate(safe_urls):
        value = value.replace(f"SAFEURLTOKEN{index}ENDTOKEN", safe_url)
    return value


def safe_error(error: BaseException, secret_values: Iterable[str] = ()) -> dict[str, str]:
    """Return a bounded error safe for UI, manifests and persistent logs."""
    message = str(error).replace("\r", " ").replace("\n", " ")
    for secret in secret_values:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    message = SENSITIVE_TEXT.sub(r"\1\2[REDACTED]", message)
    message = _redact_urls_and_paths(message)
    return {"type": type(error).__name__, "message": message[:600] or "未提供错误详情"}


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(
        path,
        json.dumps(json_value(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def atomic_text(path: Path, value: str) -> None:
    ensure_private_directory(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_private_directory(path: Path) -> None:
    existed = path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        if path.is_symlink():
            raise RuntimeError(f"拒绝使用符号链接目录：{path}")
        if not existed:
            os.chmod(path, 0o700)


def shotgrid_site_origin(sg: Any) -> str:
    """Return a canonical origin from real or test shotgun_api3 clients."""
    value = getattr(sg, "base_url", None)
    config = getattr(sg, "config", None)
    if not value and config is not None:
        server = getattr(config, "server", None)
        if server:
            raw_server = str(server).strip()
            if "://" in raw_server:
                value = raw_server
            else:
                scheme = str(getattr(config, "scheme", None) or "https").rstrip(":/")
                value = f"{scheme}://{raw_server}"
    if not value:
        value = os.environ.get("SHOTGRID_URL", "")
    text = str(value or "").strip().rstrip("/")
    if text and "://" not in text:
        text = "https://" + text
    return text


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def retire_stale_output_lock(lock_path: Path, minimum_age_seconds: float = 15.0) -> None:
    """Retire only a same-host dead lock; young or foreign locks fail closed."""
    try:
        before = lock_path.lstat()
    except FileNotFoundError:
        return
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError("输出目录锁不是安全的普通文件")
    age = max(0.0, time.time() - before.st_mtime)
    try:
        text = lock_path.read_text(encoding="utf-8")[:4096]
        metadata = json.loads(text) if text.lstrip().startswith("{") else {}
        if not metadata:
            match = re.search(r"\bpid=(\d+)\b", text)
            metadata = {"pid": int(match.group(1)) if match else 0}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        metadata = {}
    lock_host = str(metadata.get("host") or "")
    if lock_host and lock_host != socket.gethostname():
        raise RuntimeError("输出目录锁来自另一台主机，拒绝自动接管")
    pid = int(metadata.get("pid") or 0)
    if _pid_is_alive(pid):
        raise RuntimeError("输出目录已有仍在运行的备份/媒体任务")
    if age < minimum_age_seconds:
        raise RuntimeError("输出目录锁刚创建且所有权尚未确认，请稍后重试")
    after = lock_path.lstat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError("输出目录锁在检查期间发生变化")
    stale = lock_path.with_name(
        f"{lock_path.name}.stale.{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    os.replace(lock_path, stale)


def acquire_output_lock(lock_path: Path) -> tuple[int, tuple[int, int]]:
    retire_stale_output_lock(lock_path)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise RuntimeError("输出目录已有备份或媒体补全任务") from error
    metadata = {
        "version": 1,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "token": secrets.token_hex(16),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    os.write(descriptor, (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8"))
    os.fsync(descriptor)
    current = os.fstat(descriptor)
    return descriptor, (current.st_dev, current.st_ino)


def release_output_lock(lock_path: Path, descriptor: int, identity: tuple[int, int]) -> None:
    try:
        os.close(descriptor)
    finally:
        try:
            current = lock_path.lstat()
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) == identity and not lock_path.is_symlink():
            lock_path.unlink()
            fsync_directory(lock_path.parent)


def retry(call: Callable[[], Any], attempts: int, label: str) -> Any:
    for index in range(attempts):
        try:
            return call()
        except Exception as error:
            if index + 1 >= attempts or not is_retryable(error):
                raise
            delay = min(30, 2**index)
            print(f"{label} 失败，{delay}s 后重试 ({index + 1}/{attempts})", file=sys.stderr)
            time.sleep(delay)
    raise AssertionError("unreachable")


def is_retryable(error: BaseException) -> bool:
    code = getattr(error, "code", None) or getattr(error, "errcode", None)
    if code in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True
    name = type(error).__name__.lower()
    if name in {"timeout", "timeouterror", "connectionerror", "protocolerror", "urlerror"}:
        return True
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "timed out", "timeout", "connection reset", "connection aborted",
            "remote end closed", "temporarily unavailable", "too many requests",
            "http 429", "http 502", "http 503", "http 504",
        )
    )


def parse_since(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def readable_fields(field_schema: dict[str, Any]) -> list[str]:
    # schema_field_read is already permission-filtered. UI visibility is not an
    # API readability flag, so hidden-but-readable fields must also be backed up.
    fields = list(field_schema)
    if "id" not in fields:
        fields.append("id")
    return sorted(set(fields))


def schema_value(metadata: dict[str, Any], key: str, default: Any = None) -> Any:
    value = metadata.get(key, default)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def discover_entities(entity_schema: dict[str, Any]) -> list[str]:
    """Return every entity type exposed by the authenticated schema."""
    return sorted(entity_schema)


def entity_supports_retirement(sg: Any, entity: str) -> bool:
    """Detect whether retired_only produces a distinct record set."""
    query = {
        "order": [{"field_name": "id", "direction": "asc"}],
        "limit": 10,
        "page": 1,
        "include_archived_projects": True,
    }
    active = sg.find(entity, [], ["id"], retired_only=False, **query)
    retired = sg.find(entity, [], ["id"], retired_only=True, **query)
    if not retired:
        return False
    if not active:
        return True
    return [int(row["id"]) for row in active] != [int(row["id"]) for row in retired]


def iter_records(
    sg: Any,
    entity: str,
    fields: list[str],
    page_size: int,
    filters: list[list[Any]],
    retired_only: bool,
    attempts: int,
    on_page: Callable[[int, int], None] | None = None,
) -> Iterable[dict[str, Any]]:
    batch = 1
    last_id = 0
    while True:
        page_filters = [*filters, ["id", "greater_than", last_id]]
        rows = retry(
            lambda: sg.find(
                entity,
                page_filters,
                fields,
                order=[{"field_name": "id", "direction": "asc"}],
                limit=page_size,
                page=1,
                retired_only=retired_only,
                include_archived_projects=True,
            ),
            attempts,
            f"查询 {entity} batch={batch}",
        )
        if not rows:
            return
        yield from rows
        if on_page:
            on_page(batch, len(rows))
        next_id = max(int(row["id"]) for row in rows)
        if next_id <= last_id:
            raise RuntimeError(f"{entity} 分页游标没有前进：{last_id}")
        last_id = next_id
        if len(rows) < page_size:
            return
        batch += 1


def safe_attachment_name(record: dict[str, Any]) -> str:
    uploaded = record.get("this_file") or {}
    original = uploaded.get("name") or uploaded.get("url") or f"attachment_{record['id']}"
    original = Path(str(original).split("?")[0]).name
    cleaned = SAFE_NAME.sub("_", original).strip("._") or "attachment"
    return cleaned[:180]


def safe_media_name(item: dict[str, Any]) -> str:
    value = item.get("value") or {}
    url = value.get("url") if isinstance(value, dict) else value
    original = value.get("name") if isinstance(value, dict) else None
    original = original or Path(urlsplit(str(url)).path).name or item["field"]
    cleaned = SAFE_NAME.sub("_", str(original)).strip("._") or "media"
    return cleaned[:180]


def safe_path_component(value: Any, fallback: str) -> str:
    cleaned = SAFE_NAME.sub("_", str(value)).strip("._")
    return (cleaned or fallback)[:120]


def id_bucket(source_id: int) -> str:
    if source_id < 0:
        raise ValueError("实体 ID 不能小于 0")
    start = (source_id // 1000) * 1000
    return f"{start:06d}_{start + 999:06d}"


def is_structured_upload(value: Any) -> bool:
    """Only ShotGrid upload dictionaries may enter authenticated download APIs."""
    return isinstance(value, dict) and value.get("link_type") == "upload"


def media_download_url(item: dict[str, Any]) -> str | None:
    value = item.get("value")
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return value
    if isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            return url
    return None


def reject_unexpected_html(path: Path) -> None:
    if path.suffix.lower() in {".html", ".htm"}:
        return
    prefix = path.read_bytes()[:1024].lstrip().lower()
    if prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html"):
        raise RuntimeError("下载结果是 HTML 页面而不是媒体文件")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def load_config(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("配置文件顶层必须是 JSON object")
    return value


def create_client(
    site_url: str,
    script_name: str,
    script_key: str,
    http_proxy: str | None = None,
) -> Any:
    try:
        from shotgun_api3 import Shotgun
    except ImportError as error:
        raise RuntimeError("缺少 shotgun_api3；请先运行 pip install -r requirements.txt") from error
    return Shotgun(
        site_url,
        script_name=script_name,
        api_key=script_key,
        http_proxy=http_proxy,
        connect=False,
    )


def connect(http_proxy: str | None = None) -> Any:
    required = ["SHOTGRID_URL", "SHOTGRID_SCRIPT_NAME", "SHOTGRID_SCRIPT_KEY"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("缺少环境变量：" + ", ".join(missing))
    return create_client(
        os.environ["SHOTGRID_URL"],
        os.environ["SHOTGRID_SCRIPT_NAME"],
        os.environ["SHOTGRID_SCRIPT_KEY"],
        http_proxy or os.environ.get("SHOTGRID_HTTP_PROXY"),
    )


def _run_backup_unlocked(
    sg: Any,
    args: argparse.Namespace,
    config: dict[str, Any],
    client_factory: Callable[[], Any] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """Create a parallel snapshot. Every worker receives its own API client."""
    requested_entities = args.entities or config.get("entities")
    if isinstance(requested_entities, str):
        requested_entities = [item.strip() for item in requested_entities.split(",") if item.strip()]
    requested_page_size = int(config.get("page_size", 500))
    attempts = int(config.get("max_retries", 4))
    include_retired = bool(config.get("include_retired", True))
    retirement_support = config.get("retirement_support") or {}
    defer_media = bool(config.get("defer_media", False))
    download_attachments = (
        bool(config.get("download_attachments", True))
        and not args.no_attachments
        and not defer_media
    )
    workers = int(getattr(args, "workers", None) or config.get("workers", 4))
    if requested_page_size < 1 or attempts < 1 or workers < 1:
        raise ValueError("page_size、max_retries 和 workers 必须大于 0")
    if workers > 16:
        raise ValueError("workers 不能超过 16")
    if workers > 1 and client_factory is None:
        workers = 1
    error_secrets = [
        os.environ.get("SHOTGRID_SCRIPT_KEY", ""),
        str(getattr(getattr(sg, "config", None), "api_key", "") or ""),
        str(getattr(getattr(sg, "config", None), "script_key", "") or ""),
    ]

    event_lock = threading.Lock()
    event_log_path: Path | None = None
    event_sequence = 0

    def emit(event: str, **payload: Any) -> None:
        nonlocal event_sequence
        with event_lock:
            event_sequence += 1
            item = {
                "seq": event_sequence,
                "event": event,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                **payload,
            }
            if event_log_path:
                with event_log_path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(json_value(item), ensure_ascii=False, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            if progress:
                progress(item)

    server_info = retry(sg.info, attempts, "读取服务信息")
    server_page_limit = int(server_info.get("api_max_entities_per_page") or requested_page_size)
    client_page_limit = int(getattr(getattr(sg, "config", None), "records_per_page", requested_page_size))
    page_size = min(requested_page_size, server_page_limit, client_page_limit)

    started = dt.datetime.now(dt.timezone.utc)
    snapshot_id = started.strftime("%Y%m%dT%H%M%S_%fZ")
    output_root = args.output.resolve()
    incomplete = output_root / f"{snapshot_id}.incomplete"
    final = output_root / snapshot_id
    if incomplete.exists() or final.exists():
        raise FileExistsError(f"快照目录已存在：{incomplete} 或 {final}")
    ensure_private_directory(incomplete)
    event_log_path = incomplete / "logs/events.jsonl"
    ensure_private_directory(event_log_path.parent)
    event_log_path.touch(mode=0o600)
    os.chmod(event_log_path, 0o600)

    site_url = shotgrid_site_origin(sg)
    site_fingerprint = hashlib.sha256(str(site_url).lower().encode("utf-8")).hexdigest()
    entity_schema = retry(sg.schema_entity_read, attempts, "读取实体 schema")
    all_readable = bool(config.get("all_readable", False))
    if not requested_entities:
        requested_entities = discover_entities(entity_schema) if all_readable else DEFAULT_ENTITIES
    entities = list(dict.fromkeys(requested_entities))
    unknown_entities = sorted(set(entities) - set(entity_schema))
    if unknown_entities:
        raise RuntimeError("当前凭据看不到实体：" + ", ".join(unknown_entities))
    manifest: dict[str, Any] = {
        "format": "shotgrid_portable_snapshot",
        "schema_version": 3,
        "snapshot_id": snapshot_id,
        "started_at": started.isoformat(),
        "source": {"site": site_url, "site_fingerprint": site_fingerprint},
        "tool": {
            "name": "ews_sg_backup",
            "version": TOOL_VERSION,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "shotgun_api3": package_version("shotgun_api3"),
            "server_version": server_info.get("full_version") or server_info.get("version"),
        },
        "mode": "incremental" if args.updated_since else "full",
        "updated_since": args.updated_since,
        "snapshot_upper_bound": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "include_archived_projects": True,
        "include_retired": include_retired,
        "workers": workers,
        "page_size": page_size,
        "requested_page_size": requested_page_size,
        "consistency": (
            "bounded_incremental_best_effort" if args.updated_since else "keyset_full_best_effort"
        ),
        "scope": "all_readable_entities" if all_readable else "explicit_entities",
        "completeness": {
            "profile": (
                "site_api_full" if defer_media and all_readable and not args.updated_since
                else "site_full" if all_readable and not args.updated_since and download_attachments
                else "scoped"
            ),
            "all_readable_entities": all_readable,
            "full_history": not bool(args.updated_since),
            "attachment_payloads": download_attachments,
            "downloadable_media_payloads": not defer_media,
            "external_published_files": (
                "deferred_to_media_supplement" if defer_media else "metadata_only"
            ),
        },
        "entity_types_planned": entities,
        "entities": {},
        "attachments": {
            "downloaded": 0, "failed": 0, "skipped": 0, "total": 0, "deferred": 0,
        },
        "media": {
            "policy": (
                "payloads_deferred_to_media_supplement" if defer_media
                else "structured_shotgrid_uploads_only"
            ),
            "external_published_files": (
                "deferred_to_media_supplement" if defer_media else "metadata_only"
            ),
            "downloaded": 0,
            "failed": 0,
            "metadata_only": 0,
            "total": 0,
            "deferred": 0,
        },
        "restore_contract": {
            "source_identity": "entity_type_and_source_id",
            "record_files": "entities/<EntityType>.jsonl",
            "record_envelope": {"source": "type_and_id", "state": "active_or_retired", "record": "api_fields"},
            "schema_files": "schema/entities.json and schema/fields/<EntityType>.json",
            "attachment_index": "attachments/index.json",
            "event_log": "logs/events.jsonl",
            "integrity_file": "checksums.sha256",
            "verification_command": "python tools/shotgrid_backup/snapshot_verify.py <snapshot> --verify",
        },
        "errors": [],
    }
    if defer_media:
        manifest["payload_scope"] = "deferred_to_media_supplement"
    recovery_header = {
        "format": "shotgrid_snapshot_recovery_header",
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "created_at": started.isoformat(),
        "source": dict(manifest["source"]),
        "mode": manifest["mode"],
        "updated_since": manifest["updated_since"],
        "snapshot_upper_bound": manifest["snapshot_upper_bound"],
        "include_archived_projects": manifest["include_archived_projects"],
        "include_retired": manifest["include_retired"],
        "consistency": manifest["consistency"],
        "scope": manifest["scope"],
        "all_readable_entities": all_readable,
        "entity_types_planned": list(entities),
        "defer_media": defer_media,
        "payload_scope": manifest.get("payload_scope", "included_in_base"),
    }
    recovery_header_path = incomplete / "recovery_header.json"
    atomic_json(recovery_header_path, recovery_header)
    manifest["recovery_header"] = {
        "file": recovery_header_path.name,
        "sha256": sha256_file(recovery_header_path),
    }
    atomic_json(incomplete / "schema/entities.json", entity_schema)
    emit("snapshot_created", snapshot_id=snapshot_id, entities_total=len(entities), mode=manifest["mode"])

    plans: list[dict[str, Any]] = []
    for entity in entities:
        try:
            field_schema = retry(lambda entity=entity: sg.schema_field_read(entity), attempts, f"读取 {entity} 字段 schema")
            atomic_json(incomplete / f"schema/fields/{entity}.json", field_schema)
            fields = readable_fields(field_schema)
            media_fields = {
                field_name: str(schema_value(metadata, "data_type", "unknown"))
                for field_name, metadata in field_schema.items()
                if str(schema_value(metadata, "data_type", "unknown")) in {"image", "url"}
                or field_name in {"image", "filmstrip_image"}
            }
            link_fields = dict(sorted({
                field_name: str(schema_value(metadata, "data_type", "unknown"))
                for field_name, metadata in field_schema.items()
                if str(schema_value(metadata, "data_type", "unknown")) in {"entity", "multi_entity"}
            }.items()))
            filters: list[list[Any]] = []
            if "updated_at" in fields:
                if args.updated_since:
                    filters.append(["updated_at", "greater_than", args.updated_since])
                    filters.append(["updated_at", "less_than", manifest["snapshot_upper_bound"]])
            elif args.updated_since:
                manifest["errors"].append({
                    "entity": entity,
                    "phase": "schema",
                    "error": "实体没有 updated_at，不能安全执行增量备份",
                })
                continue
            supports_retirement = bool(
                retirement_support.get(entity)
                if entity in retirement_support
                else entity_supports_retirement(sg, entity)
            )
            plans.append({
                "entity": entity,
                "fields": fields,
                "filters": filters,
                "media_fields": media_fields,
                "link_fields": link_fields,
                "supports_retirement": supports_retirement,
            })
        except Exception as error:
            safe = safe_error(error, error_secrets)
            manifest["errors"].append({"entity": entity, "phase": "schema", "error": safe})
            emit("entity_error", entity=entity, phase="schema", error=safe)

    emit("backup_started", entities_total=len(plans), workers=workers, page_size=page_size)

    def export_entity(plan: dict[str, Any]) -> dict[str, Any]:
        entity = plan["entity"]
        fields = plan["fields"]
        client = client_factory() if client_factory else sg
        attachment_rows: list[dict[str, Any]] = []
        media_rows: list[dict[str, Any]] = []
        destination = incomplete / f"entities/{entity}.jsonl"
        ensure_private_directory(destination.parent)
        link_destination = incomplete / f"links/{entity}.jsonl"
        ensure_private_directory(link_destination.parent)
        counts = {"active": 0, "retired": 0}
        link_count = 0

        def page_event(retired: bool) -> Callable[[int, int], None]:
            return lambda batch, count: emit(
                "entity_page", entity=entity, retired=retired, batch=batch, records=count
            )

        destination.touch(mode=0o600)
        os.chmod(destination, 0o600)
        link_destination.touch(mode=0o600)
        os.chmod(link_destination, 0o600)
        with (
            destination.open("w", encoding="utf-8", newline="\n") as stream,
            link_destination.open("w", encoding="utf-8", newline="\n") as link_stream,
        ):
            retired_modes = [False, True] if include_retired and plan["supports_retirement"] else [False]
            for retired in retired_modes:
                for record in iter_records(
                    client,
                    entity,
                    fields,
                    page_size,
                    plan["filters"],
                    retired,
                    attempts,
                    page_event(retired),
                ):
                    envelope = {
                        "source": {"type": entity, "id": int(record["id"])},
                        "state": "retired" if retired else "active",
                        "record": record,
                    }
                    stream.write(json.dumps(json_value(envelope), ensure_ascii=False, sort_keys=True) + "\n")
                    counts["retired" if retired else "active"] += 1
                    for field_name, data_type in plan["link_fields"].items():
                        value = record.get(field_name)
                        values = value if data_type == "multi_entity" and isinstance(value, list) else [value]
                        for ordinal, target in enumerate(values):
                            if not isinstance(target, dict) or not target.get("type") or not target.get("id"):
                                continue
                            link = {
                                "source": {"type": entity, "id": int(record["id"])},
                                "state": "retired" if retired else "active",
                                "field": field_name,
                                "ordinal": ordinal,
                                "target": {
                                    "type": str(target["type"]),
                                    "id": int(target["id"]),
                                    "name": target.get("name"),
                                },
                            }
                            link_stream.write(
                                json.dumps(json_value(link), ensure_ascii=False, sort_keys=True) + "\n"
                            )
                            link_count += 1
                    if entity == "Attachment":
                        attachment_record = dict(record)
                        attachment_record["_backup_retired"] = retired
                        attachment_rows.append(attachment_record)
                    for field_name, data_type in plan["media_fields"].items():
                        if entity == "Attachment" and field_name == "this_file":
                            continue
                        value = record.get(field_name)
                        if not value:
                            continue
                        media_rows.append({
                            "entity": entity,
                            "source_id": int(record["id"]),
                            "field": field_name,
                            "state": "retired" if retired else "active",
                            "data_type": data_type,
                            "downloadable": is_structured_upload(value),
                            "value": value,
                        })
            stream.flush()
            os.fsync(stream.fileno())
            link_stream.flush()
            os.fsync(link_stream.fileno())
        return {
            "entity": entity,
            "counts": counts,
            "fields": len(fields),
            "sha256": sha256_file(destination),
            "link_count": link_count,
            "link_sha256": sha256_file(link_destination),
            "attachment_rows": attachment_rows,
            "media_rows": media_rows,
        }

    attachment_rows: list[dict[str, Any]] = []
    media_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(plans)))) as executor:
        futures = {executor.submit(export_entity, plan): plan["entity"] for plan in plans}
        for future in as_completed(futures):
            entity = futures[future]
            try:
                result = future.result()
                counts = result["counts"]
                manifest["entities"][entity] = {
                    **counts,
                    "fields": result["fields"],
                    "file": f"entities/{entity}.jsonl",
                    "sha256": result["sha256"],
                    "schema_file": f"schema/fields/{entity}.json",
                    "link_file": f"links/{entity}.jsonl",
                    "link_count": result["link_count"],
                    "link_sha256": result["link_sha256"],
                    "retirement_supported": next(
                        plan["supports_retirement"] for plan in plans if plan["entity"] == entity
                    ),
                }
                attachment_rows.extend(result["attachment_rows"])
                media_rows.extend(result["media_rows"])
                emit("entity_complete", entity=entity, **counts)
                print(f"{entity}: active={counts['active']} retired={counts['retired']}")
            except Exception as error:
                safe = safe_error(error, error_secrets)
                manifest["errors"].append({"entity": entity, "phase": "records", "error": safe})
                emit("entity_error", entity=entity, phase="records", error=safe)
                print(f"{entity}: 失败：{safe['type']}: {safe['message']}", file=sys.stderr)

    if defer_media and "Attachment" in entities:
        manifest["attachments"]["deferred"] = sum(
            1 for record in attachment_rows
            if is_structured_upload(record.get("this_file"))
        )

    if download_attachments and "Attachment" in entities:
        attachment_dir = incomplete / "attachments"
        ensure_private_directory(attachment_dir)
        upload_rows = [
            record for record in attachment_rows
            if is_structured_upload(record.get("this_file"))
        ]
        manifest["attachments"]["skipped"] = len(attachment_rows) - len(upload_rows)
        manifest["attachments"]["total"] = len(upload_rows)
        emit("attachment_plan", total=len(upload_rows), skipped=manifest["attachments"]["skipped"])
        local_client = threading.local()

        def attachment_client() -> Any:
            if not hasattr(local_client, "client"):
                local_client.client = client_factory() if client_factory else sg
            return local_client.client

        def download_one(record: dict[str, Any]) -> dict[str, Any]:
            attachment_id = int(record["id"])
            bucket_dir = attachment_dir / id_bucket(attachment_id)
            ensure_private_directory(bucket_dir)
            record_dir = bucket_dir / str(attachment_id)
            ensure_private_directory(record_dir)
            target = record_dir / safe_attachment_name(record)
            temporary = target.with_suffix(target.suffix + ".part")
            if temporary.exists() and temporary.is_symlink():
                raise RuntimeError("拒绝覆盖符号链接 .part 文件")
            temporary.touch(mode=0o600, exist_ok=True)
            os.chmod(temporary, 0o600)
            try:
                retry(
                    lambda: attachment_client().download_attachment(record["id"], str(temporary)),
                    attempts,
                    f"下载附件 {record['id']}",
                )
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise RuntimeError("下载结果为空")
                uploaded = record.get("this_file") or {}
                expected_size = record.get("file_size") or uploaded.get("size") or uploaded.get("file_size")
                if expected_size and temporary.stat().st_size != int(expected_size):
                    raise RuntimeError(
                        f"下载大小不匹配：expected={int(expected_size)} actual={temporary.stat().st_size}"
                    )
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
                return {
                    "attachment_id": attachment_id,
                    "retired": bool(record.get("_backup_retired")),
                    "file": target.relative_to(attachment_dir).as_posix(),
                    "size": target.stat().st_size,
                    "sha256": sha256_file(target),
                }
            except Exception:
                temporary.unlink(missing_ok=True)
                raise

        attachment_index: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(workers, max(1, len(upload_rows)))) as executor:
            futures = {executor.submit(download_one, record): record["id"] for record in upload_rows}
            for future in as_completed(futures):
                attachment_id = futures[future]
                try:
                    item = future.result()
                    attachment_index.append(item)
                    manifest["attachments"]["downloaded"] += 1
                    emit("attachment_complete", attachment_id=attachment_id, size=item["size"])
                except Exception as error:
                    safe = safe_error(error, error_secrets)
                    manifest["attachments"]["failed"] += 1
                    manifest["errors"].append({"attachment_id": attachment_id, "error": safe})
                    emit("attachment_error", attachment_id=attachment_id, error=safe)
        attachment_index.sort(key=lambda item: item["attachment_id"])
        atomic_json(attachment_dir / "index.json", attachment_index)

    downloadable_media = [] if defer_media else [
        item for item in media_rows
        if is_structured_upload(item.get("value")) and media_download_url(item)
    ]
    if defer_media:
        manifest["media"]["deferred"] = len(media_rows)
    else:
        manifest["media"]["metadata_only"] = len(media_rows) - len(downloadable_media)
        manifest["media"]["total"] = len(downloadable_media)
    if downloadable_media:
        media_root = incomplete / "media"
        ensure_private_directory(media_root)
        emit(
            "media_plan",
            total=len(downloadable_media),
            metadata_only=manifest["media"]["metadata_only"],
        )
        media_local = threading.local()

        def media_client() -> Any:
            if not hasattr(media_local, "client"):
                media_local.client = client_factory() if client_factory else sg
            return media_local.client

        def download_media_item(item: dict[str, Any]) -> dict[str, Any]:
            entity_dir = media_root / safe_path_component(item["entity"], "Entity")
            ensure_private_directory(entity_dir)
            source_id = int(item["source_id"])
            bucket_dir = entity_dir / id_bucket(source_id)
            ensure_private_directory(bucket_dir)
            source_dir = bucket_dir / str(source_id)
            ensure_private_directory(source_dir)
            field_dir = source_dir / safe_path_component(item["field"], "field")
            ensure_private_directory(field_dir)
            target = field_dir / safe_media_name(item)
            temporary = target.with_suffix(target.suffix + ".part")
            if temporary.exists() and temporary.is_symlink():
                raise RuntimeError("拒绝覆盖符号链接 .part 文件")
            temporary.touch(mode=0o600, exist_ok=True)
            os.chmod(temporary, 0o600)
            try:
                url = media_download_url(item)
                if not url:
                    raise RuntimeError("媒体没有可下载 URL")
                attachment_value = item.get("value")
                if not is_structured_upload(attachment_value):
                    raise RuntimeError("拒绝把非 ShotGrid upload 媒体交给认证下载客户端")
                retry(
                    lambda: media_client().download_attachment(attachment_value, file_path=str(temporary)),
                    attempts,
                    f"下载媒体 {item['entity']} {item['source_id']} {item['field']}",
                )
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise RuntimeError("下载结果为空")
                value = item.get("value") or {}
                expected_size = (
                    value.get("size") or value.get("file_size")
                    if isinstance(value, dict) else None
                )
                if expected_size and temporary.stat().st_size != int(expected_size):
                    raise RuntimeError(
                        f"下载大小不匹配：expected={int(expected_size)} actual={temporary.stat().st_size}"
                    )
                reject_unexpected_html(temporary)
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
                return {
                    "source": {"type": item["entity"], "id": item["source_id"]},
                    "field": item["field"],
                    "state": item["state"],
                    "file": target.relative_to(incomplete).as_posix(),
                    "size": target.stat().st_size,
                    "sha256": sha256_file(target),
                }
            except Exception:
                temporary.unlink(missing_ok=True)
                raise

        media_index: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(workers, len(downloadable_media))) as executor:
            futures = {executor.submit(download_media_item, item): item for item in downloadable_media}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    result = future.result()
                    media_index.append(result)
                    manifest["media"]["downloaded"] += 1
                    emit(
                        "media_complete",
                        entity=item["entity"], source_id=item["source_id"],
                        field=item["field"], size=result["size"],
                    )
                except Exception as error:
                    safe = safe_error(error, error_secrets)
                    manifest["media"]["failed"] += 1
                    manifest["errors"].append({
                        "entity": item["entity"], "source_id": item["source_id"],
                        "field": item["field"], "phase": "media", "error": safe,
                    })
                    emit(
                        "media_error", entity=item["entity"], source_id=item["source_id"],
                        field=item["field"], error=safe,
                    )
        media_index.sort(key=lambda item: (item["source"]["type"], item["source"]["id"], item["field"]))
        atomic_json(media_root / "index.json", media_index)

    manifest["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    manifest["duration_seconds"] = round(
        (dt.datetime.fromisoformat(manifest["completed_at"]) - started).total_seconds(), 3
    )
    manifest["status"] = "complete" if not manifest["errors"] else "partial"
    atomic_json(incomplete / "logs/errors.json", manifest["errors"])
    atomic_json(incomplete / "manifest.json", manifest)
    if manifest["errors"]:
        emit("backup_error", errors=len(manifest["errors"]), snapshot_id=snapshot_id)
        raise RuntimeError(f"备份存在 {len(manifest['errors'])} 个错误；结果保留在 {incomplete}")
    emit("snapshot_sealed", snapshot_id=snapshot_id, errors=0)
    checksum_paths = [
        path for path in incomplete.rglob("*")
        if path.is_file()
        and path.name not in {"checksums.sha256", "manifest.json"}
        and not is_ignorable_platform_metadata(path)
    ]
    checksum_lines = [
        f"{sha256_file(path)}  {path.relative_to(incomplete).as_posix()}"
        for path in sorted(checksum_paths)
    ]
    atomic_text(incomplete / "checksums.sha256", "\n".join(checksum_lines) + "\n")
    manifest["integrity"] = {
        "algorithm": "sha256",
        "checksums_file": "checksums.sha256",
        "files_hashed": len(checksum_lines),
        "manifest_excluded_to_avoid_self_reference": True,
    }
    atomic_json(incomplete / "manifest.json", manifest)
    atomic_json(incomplete / "COMPLETED.json", {
        "format": "shotgrid_snapshot_completion_receipt",
        "snapshot_id": snapshot_id,
        "completed_at": manifest["completed_at"],
        "manifest_sha256": sha256_file(incomplete / "manifest.json"),
        "checksums_sha256": sha256_file(incomplete / "checksums.sha256"),
    })
    os.replace(incomplete, final)
    fsync_directory(output_root)
    atomic_text(output_root / "latest.txt", snapshot_id + "\n")
    if progress:
        progress({
            "event": "backup_complete",
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "path": str(final),
            "files_hashed": len(checksum_lines),
        })
    return final


def run_backup(
    sg: Any,
    args: argparse.Namespace,
    config: dict[str, Any],
    client_factory: Callable[[], Any] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """Serialize publication per output root and always release the process lock."""
    output_root = args.output.resolve()
    ensure_private_directory(output_root)
    lock_path = output_root / ".backup.lock"
    descriptor, lock_identity = acquire_output_lock(lock_path)
    try:
        return _run_backup_unlocked(sg, args, config, client_factory, progress)
    finally:
        release_output_lock(lock_path, descriptor, lock_identity)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 ShotGrid 数据备份为本地快照")
    parser.add_argument("--config", type=Path, help="JSON 配置文件")
    parser.add_argument("--output", type=Path, default=Path(".local/backups"), help="备份根目录")
    parser.add_argument("--entities", help="逗号分隔的实体类型")
    parser.add_argument("--all-readable", action="store_true", help="备份当前凭据可读的全部实体")
    parser.add_argument("--updated-since", type=parse_since, help="ISO 8601 增量起点")
    parser.add_argument("--no-attachments", action="store_true", help="不下载 Attachment 原文件")
    parser.add_argument("--http-proxy", help="HTTP 代理，格式为 host:port")
    parser.add_argument("--workers", type=int, default=4, help="并发 worker 数，1-16")
    parser.add_argument("--progress-json", action="store_true", help="逐行输出结构化进度事件")
    parser.add_argument("--check", action="store_true", help="只验证凭据和 API 连接")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(args.config)
        if args.all_readable:
            config["all_readable"] = True
        if args.http_proxy:
            config["http_proxy"] = args.http_proxy
        sg = connect(args.http_proxy)
        if args.check:
            server = sg.info()
            entities = sg.schema_entity_read()
            sg.find_one("Project", [], ["id"])
            result = {
                "server_version": server.get("full_version") or server.get("version"),
                "authenticated_schema_entities": len(entities),
                "project_read_check": "ok",
            }
            print("连接与鉴权成功：" + json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        progress = None
        if args.progress_json:
            progress = lambda event: print(
                "PROGRESS " + json.dumps(json_value(event), ensure_ascii=False, sort_keys=True),
                flush=True,
            )
        result = run_backup(
            sg,
            args,
            config,
            client_factory=lambda: connect(args.http_proxy),
            progress=progress,
        )
        print(f"备份完成：{result}")
        return 0
    except Exception as error:
        safe = safe_error(error, [os.environ.get("SHOTGRID_SCRIPT_KEY", "")])
        print(f"备份失败：{safe['type']}: {safe['message']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
