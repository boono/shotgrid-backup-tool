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
from typing import Any, Optional
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
from media_sync import inspect_latest_snapshot, materialize_latest_media


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UI_ROOT = Path(__file__).resolve().parent / "ui"
SESSION_TOKEN = secrets.token_urlsafe(32)
PROXY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+:\d{1,5}$")
AUTO_WORKERS = min(8, max(4, os.cpu_count() or 4))
MEDIA_MAX_WORKERS = 32
CREDENTIAL_LOCK = threading.Lock()
CREDENTIALS: dict[str, dict[str, Any]] = {}


def safe_log_text(value: Any) -> str:
    """Remove source locations that should never be echoed into the browser log."""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://\S+", "[URL]", text)
    text = re.sub(r"(?<![\w.])(?:/[^\s,;:]+)+", "[PATH]", text)
    text = re.sub(r"(?i)(?<!\w)[A-Z]:\\[^\s,;]+", "[PATH]", text)
    text = re.sub(r"\\\\[^\\\s]+\\[^\s,;]+", "[PATH]", text)
    return text[:400] or "未提供错误详情"


def safe_media_ref(event: dict[str, Any]) -> str:
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    entity = source.get("type") or event.get("entity") or "Media"
    source_id = source.get("id") or event.get("source_id") or "?"
    field = event.get("field") or "file"
    entity = re.sub(r"[^A-Za-z0-9_]", "_", str(entity))[:80] or "Media"
    source_id = str(source_id) if str(source_id).isdigit() else "?"
    field = re.sub(r"[^A-Za-z0-9_]", "_", str(field))[:80] or "file"
    return f"{entity}:{source_id}.{field}"


def safe_error_code(event: dict[str, Any]) -> str:
    error = event.get("error") if isinstance(event.get("error"), dict) else {}
    value = event.get("error_code") or error.get("code") or error.get("type") or "ERROR"
    code = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value).upper())[:80]
    return code or "ERROR"


class JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.status = "idle"
        self.phase = "等待检查"
        self.started = 0.0
        self.completed_at: Optional[float] = None
        self.action = "full_backup"
        self.copy_external = False
        self.base_complete = False
        self.entities_total = 0
        self.entities_done = 0
        self.records_total = 0
        self.records_done = 0
        self.attachments_total = 0
        self.attachments_done = 0
        self.bytes_done = 0
        self.media_plan_seen = False
        self.transfer_items_total = 0
        self.transfer_items_done = 0
        self.transfer_bytes_total = 0
        self.transfer_bytes_done = 0
        self.transfer_reused = 0
        self.reused_interrupted = 0
        self.transfer_workers = {"download": 0, "copy": 0}
        self.transfer_rates = {"download": 0.0, "copy": 0.0}
        self.transfer_etas = {"download": None, "copy": None}
        self.transfer_eta_samples = {"download": 0, "copy": 0}
        self.transfer_eta_calibrating = {"download": False, "copy": False}
        self.transfer_eta_waiting = False
        self.transfer_retrying = 0
        self.transfer_retried = 0
        self.transfer_final_failed = 0
        self.transfer_failed_refs: set[str] = set()
        self.transfer_manifest_status = "idle"
        self.errors = 0
        self.result_path: Optional[str] = None
        self.message = ""
        self.logs: list[str] = []

    def begin(
        self,
        expected_counts: dict[str, Any],
        action: str,
        base_path: Optional[str] = None,
        reused_interrupted: int = 0,
        copy_external: bool = False,
    ) -> None:
        with self.lock:
            if self.status == "running":
                raise RuntimeError("已有备份正在运行")
            self.reset()
            self.status = "running"
            self.action = action
            self.copy_external = bool(copy_external)
            self.phase = (
                "恢复中断备份" if action == "resume_media"
                else "准备媒体补充" if action == "media_supplement"
                else "准备完整快照"
            )
            self.started = time.monotonic()
            self.result_path = base_path
            self.reused_interrupted = max(0, reused_interrupted)
            self.entities_total = len(expected_counts)
            self.records_total = sum(
                int(value.get("active", 0)) + int(value.get("retired", 0))
                for value in expected_counts.values()
            )
            if action == "resume_media":
                self.logs.append(
                    "恢复已完成的实体导出；"
                    f"将复用中断任务中的 {self.reused_interrupted} 个媒体文件"
                )
            elif action == "media_supplement":
                self.logs.append("开始补全已有实体快照的外部媒体")
            else:
                self.logs.append("开始完整实体快照与外部媒体补充任务")

    @staticmethod
    def _event_int(event: dict[str, Any], *keys: str) -> Optional[int]:
        for key in keys:
            value = event.get(key)
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _event_float(event: dict[str, Any], *keys: str) -> Optional[float]:
        for key in keys:
            value = event.get(key)
            if value is not None:
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    return None
        return None

    def _update_transfer_totals(self, event: dict[str, Any]) -> None:
        items_total = self._event_int(event, "items_total", "total")
        bytes_total = self._event_int(event, "bytes_total")
        if items_total is not None:
            self.transfer_items_total = max(self.transfer_items_total, items_total)
        if bytes_total is not None:
            self.transfer_bytes_total = max(self.transfer_bytes_total, bytes_total)

    def _update_transfer_position(self, event: dict[str, Any], include_size: bool) -> None:
        self._update_transfer_totals(event)
        items_done = self._event_int(event, "items_done", "done")
        bytes_done = self._event_int(event, "bytes_done")
        if items_done is None:
            self.transfer_items_done += 1
        else:
            self.transfer_items_done = max(self.transfer_items_done, items_done)
        if bytes_done is not None:
            self.transfer_bytes_done = max(self.transfer_bytes_done, bytes_done)
        elif include_size:
            self.transfer_bytes_done += self._event_int(event, "size", "bytes") or 0

    def _update_transfer_eta(self, event: dict[str, Any], kind: str) -> None:
        if not any(
            key in event
            for key in ("eta_seconds", "eta", "eta_sample_count", "sample_count", "calibrating")
        ):
            return
        eta = self._event_float(event, "eta_seconds", "eta")
        samples = self._event_int(event, "eta_sample_count", "sample_count")
        calibrating_value = event.get("calibrating")
        calibrating = (
            bool(calibrating_value)
            if calibrating_value is not None
            else samples is not None and samples < 10
        )
        self.transfer_eta_waiting = False
        if eta is not None:
            self.transfer_etas[kind] = eta
        if samples is not None:
            self.transfer_eta_samples[kind] = samples
        self.transfer_eta_calibrating[kind] = calibrating

    def _update_retry_stats(self, event: dict[str, Any]) -> None:
        nested = event.get("retry") or event.get("retry_counts") or event.get("counters") or {}
        if not isinstance(nested, dict):
            nested = {}
        for fields, attribute in (
            (("retrying", "retry_scheduled"), "transfer_retrying"),
            (("retried", "retry_complete"), "transfer_retried"),
            (("final_failed",), "transfer_final_failed"),
        ):
            value = self._event_int(event, *fields)
            if value is None:
                value = self._event_int(nested, *fields)
            if value is not None:
                setattr(self, attribute, max(getattr(self, attribute), value))
        manifest_status = event.get("manifest_status")
        if manifest_status is not None:
            self.transfer_manifest_status = safe_log_text(manifest_status)[:40]

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
                    f"{safe_log_text(error.get('message', 'unknown'))}"
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
                self.base_complete = True
                self.result_path = event.get("path")
                self.phase = "准备媒体补充"
                self.logs.append("实体快照已发布，继续补全所选媒体范围")
            elif name == "backup_error":
                self.errors += int(event.get("errors", 1))
            elif name == "media_supplement_plan":
                self.media_plan_seen = True
                self.transfer_eta_waiting = True
                self.transfer_manifest_status = "partial"
                self._update_retry_stats(event)
                self.phase = "规划媒体"
                self._update_transfer_totals(event)
                reused = self._event_int(
                    event, "reused_items", "reused", "items_reused", "skipped_items", "skipped"
                )
                if reused is not None:
                    self.transfer_reused = max(self.transfer_reused, reused)
                    self.transfer_items_done = max(self.transfer_items_done, reused)
                reused_interrupted = self._event_int(event, "reused_interrupted")
                if reused_interrupted is not None:
                    self.reused_interrupted = max(
                        self.reused_interrupted, reused_interrupted
                    )
                    self.transfer_reused = max(
                        self.transfer_reused, self.reused_interrupted
                    )
                    self.transfer_items_done = max(
                        self.transfer_items_done, self.reused_interrupted
                    )
                bytes_done = self._event_int(event, "bytes_done", "reused_bytes")
                if bytes_done is not None:
                    self.transfer_bytes_done = max(self.transfer_bytes_done, bytes_done)
                self.logs.append(
                    "媒体补充计划："
                    f"{self.transfer_items_total} 项，复用/跳过 {self.transfer_reused} 项，"
                    f"其中中断复用 {self.reused_interrupted} 项"
                )
            elif name == "media_transfer_tuning":
                self.media_plan_seen = True
                kind = str(event.get("kind", "download")).lower()
                kind = kind if kind in self.transfer_workers else "download"
                workers = self._event_int(event, "workers", f"{kind}_workers")
                rate = self._event_float(
                    event, "bytes_per_second", "rate_bps", "throughput_bps"
                )
                if workers is not None:
                    self.transfer_workers[kind] = workers
                if rate is not None:
                    self.transfer_rates[kind] = rate
                self._update_transfer_eta(event, kind)
                eta = self.transfer_etas[kind]
                calibrating = self.transfer_eta_calibrating[kind]
                self.phase = "下载媒体" if kind == "download" else "复制外部媒体"
                kind_rate = self.transfer_rates[kind] / (1024 * 1024)
                eta_text = (
                    "ETA 校准中"
                    if calibrating
                    else f"ETA {eta:.0f}s" if eta is not None
                    else "ETA 待事件"
                )
                self.logs.append(
                    "自适应并发："
                    f"下载 {self.transfer_workers['download']} / 复制 {self.transfer_workers['copy']}，"
                    f"{self.phase} {kind_rate:.1f} MB/s，{eta_text}"
                )
            elif name == "media_transfer_complete":
                self.media_plan_seen = True
                self._update_transfer_position(event, include_size=True)
                kind = str(event.get("kind", "download")).lower()
                kind = kind if kind in self.transfer_workers else "download"
                self._update_transfer_eta(event, kind)
                self.phase = "复制外部媒体" if kind == "copy" else "下载媒体"
            elif name == "media_transfer_reused":
                self.media_plan_seen = True
                self._update_transfer_position(event, include_size=True)
                kind = str(event.get("kind", "download")).lower()
                kind = kind if kind in self.transfer_workers else "download"
                self._update_transfer_eta(event, kind)
                reused = self._event_int(
                    event, "reused_items", "reused", "items_reused", "skipped_items", "skipped"
                )
                if reused is None:
                    reused_count = self._event_int(event, "items") or 1
                    if str(event.get("kind")) == "base":
                        self.transfer_reused = max(self.transfer_reused, reused_count)
                    else:
                        self.transfer_reused += reused_count
                else:
                    self.transfer_reused = max(self.transfer_reused, reused)
                reused_interrupted = self._event_int(event, "reused_interrupted")
                if reused_interrupted is not None:
                    self.reused_interrupted = max(
                        self.reused_interrupted, reused_interrupted
                    )
                self.phase = "复用已有媒体"
                self.logs.append(
                    f"复用/跳过：{self.transfer_reused} / {self.transfer_items_total} 项"
                )
            elif name == "media_transfer_retry":
                self.media_plan_seen = True
                self._update_transfer_totals(event)
                self._update_retry_stats(event)
                kind = str(event.get("kind", "download")).lower()
                kind = kind if kind in self.transfer_workers else "download"
                self._update_transfer_eta(event, kind)
                retry_status = str(
                    event.get("status")
                    or event.get("retry_status")
                    or event.get("retry_state")
                    or "retrying"
                )
                retry_status = {
                    "retry_scheduled": "retrying",
                    "retry_complete": "retried",
                }.get(retry_status, retry_status)
                if retry_status not in {"retrying", "retried", "final_failed"}:
                    retry_status = "retrying"
                if (
                    retry_status == "retrying"
                    and self._event_int(event, "retrying", "retry_scheduled") is None
                ):
                    self.transfer_retrying += 1
                elif (
                    retry_status == "retried"
                    and self._event_int(event, "retried", "retry_complete") is None
                ):
                    self.transfer_retried += 1
                elif retry_status == "final_failed" and self._event_int(event, "final_failed") is None:
                    self.transfer_final_failed += 1
                if retry_status == "final_failed":
                    self.transfer_failed_refs.add(safe_media_ref(event))
                self.phase = "重试媒体传输"
                self.logs.append(
                    f"媒体重试 {safe_log_text(retry_status)} "
                    f"{safe_media_ref(event)} code={safe_error_code(event)}"
                )
            elif name == "media_transfer_error":
                self.media_plan_seen = True
                self._update_transfer_position(event, include_size=False)
                self.errors += 1
                kind = str(event.get("kind", "download")).lower()
                kind = kind if kind in self.transfer_workers else "download"
                self._update_transfer_eta(event, kind)
                self._update_retry_stats(event)
                media_ref = safe_media_ref(event)
                if media_ref not in self.transfer_failed_refs:
                    self.transfer_failed_refs.add(media_ref)
                    self.transfer_final_failed += 1
                self.transfer_manifest_status = "partial"
                self.logs.append(
                    f"媒体传输最终失败 {media_ref} "
                    f"code={safe_error_code(event)}"
                )
            elif name == "media_supplement_complete":
                self.media_plan_seen = True
                self.phase = "媒体补充完成"
                self.transfer_workers = {"download": 0, "copy": 0}
                self.transfer_rates = {"download": 0.0, "copy": 0.0}
                self._update_retry_stats(event)
                self.transfer_manifest_status = str(
                    event.get("manifest_status") or "complete"
                )[:40]
                self._update_transfer_totals(event)
                items_done = self._event_int(event, "items_done")
                bytes_done = self._event_int(event, "bytes_done")
                if items_done is not None:
                    self.transfer_items_done = max(self.transfer_items_done, items_done)
                if bytes_done is not None:
                    self.transfer_bytes_done = max(self.transfer_bytes_done, bytes_done)
                if event.get("path"):
                    self.result_path = str(event["path"])
                self.logs.append(
                    "媒体补充完成："
                    f"{self.transfer_items_done} 项，复用/跳过 {self.transfer_reused} 项"
                )
            self.logs = self.logs[-200:]

    def finish(self, result_path: str) -> None:
        with self.lock:
            if self.transfer_final_failed:
                self.status = "failed"
                self.phase = "媒体补充失败"
                self.completed_at = time.monotonic()
                self.message = (
                    f"仍有 {self.transfer_final_failed} 个媒体项最终失败；"
                    "已完成文件保留，下次只补失败/缺失项"
                )
                self.logs.append(self.message)
                return
            self.status = "complete"
            self.phase = "媒体补充完成"
            self.completed_at = time.monotonic()
            self.result_path = result_path
            if self.action == "resume_media":
                self.message = "中断任务的数据基线已封存，所选范围内的缺失媒体已补全"
            elif self.action == "media_supplement":
                self.message = (
                    "已有实体快照的 ShotGrid 托管媒体和外部媒体已补全"
                    if self.copy_external
                    else "已有实体快照的 ShotGrid 托管媒体已补全；外部文件未请求复制"
                )
            else:
                self.message = (
                    "实体快照已发布，ShotGrid 托管媒体和外部媒体补充包已完成"
                    if self.copy_external
                    else "实体快照已发布，ShotGrid 托管媒体补充包已完成；外部文件未请求复制"
                )
            self.logs.append(self.message)

    def preserve_result(self, result_path: Optional[str]) -> None:
        with self.lock:
            if result_path:
                self.result_path = str(result_path)

    def fail(self, error: dict[str, str]) -> None:
        with self.lock:
            self.status = "failed"
            media_failure = (
                self.media_plan_seen or self.base_complete
                or self.action in {"media_supplement", "resume_media"}
            )
            self.phase = "媒体补充失败" if media_failure else "备份失败"
            self.completed_at = time.monotonic()
            detail = f"{safe_log_text(error['type'])}: {safe_log_text(error['message'])}"
            if media_failure and self.base_complete:
                self.message = (
                    "媒体补充失败；刚发布的完整实体快照与已完成文件均已保留，"
                    f"下次只补失败/缺失项。{detail}"
                )
            elif media_failure and self.result_path:
                self.message = (
                    "媒体补充失败；原完整实体快照与已完成文件均已保留，"
                    f"下次只补失败/缺失项。{detail}"
                )
            elif media_failure:
                self.message = (
                    "媒体补充失败；已完成文件保留，下次只补失败/缺失项。"
                    f"{detail}"
                )
            else:
                self.message = f"备份失败：{detail}"
            self.logs.append(self.message)

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
                base_progress = metadata_ratio * 0.72 + attachment_ratio * 0.28
            else:
                base_progress = metadata_ratio * (1.0 if self.base_complete else 0.92)
            if self.base_complete:
                base_progress = 1.0
            item_ratio = (
                min(1.0, self.transfer_items_done / self.transfer_items_total)
                if self.transfer_items_total else 0.0
            )
            byte_ratio = (
                min(1.0, self.transfer_bytes_done / self.transfer_bytes_total)
                if self.transfer_bytes_total else 0.0
            )
            transfer_progress = max(item_ratio, byte_ratio)
            if self.action in {"media_supplement", "resume_media"}:
                progress = transfer_progress
            elif self.media_plan_seen:
                progress = 0.72 + transfer_progress * 0.28
            else:
                progress = base_progress * 0.72
            if self.status == "complete":
                progress = 1.0
            eta = None
            if self.status == "running" and self.media_plan_seen:
                known_etas = [
                    value for kind, value in self.transfer_etas.items()
                    if value is not None and not self.transfer_eta_calibrating[kind]
                ]
                eta = max(known_etas) if known_etas else None
            elif self.status == "running" and progress > 0.02:
                eta = max(0.0, elapsed * (1.0 - progress) / progress)
            return {
                "status": self.status,
                "action": self.action,
                "phase": self.phase,
                "elapsed_seconds": round(elapsed, 1),
                "eta_seconds": round(eta, 1) if eta is not None else None,
                "progress": round(progress, 4),
                "entities": {"done": self.entities_done, "total": self.entities_total},
                "records": {"done": self.records_done, "total": self.records_total},
                "attachments": {"done": self.attachments_done, "total": self.attachments_total},
                "bytes_done": self.bytes_done,
                "media_transfer": {
                    "items": {
                        "done": self.transfer_items_done,
                        "total": self.transfer_items_total,
                    },
                    "bytes": {
                        "done": self.transfer_bytes_done,
                        "total": self.transfer_bytes_total,
                    },
                    "reused": self.transfer_reused,
                    "reused_interrupted": self.reused_interrupted,
                    "workers": dict(self.transfer_workers),
                    "download_workers": self.transfer_workers["download"],
                    "copy_workers": self.transfer_workers["copy"],
                    "download_bytes_per_second": round(
                        self.transfer_rates["download"], 1
                    ),
                    "copy_bytes_per_second": round(
                        self.transfer_rates["copy"], 1
                    ),
                    "retrying": self.transfer_retrying,
                    "retried": self.transfer_retried,
                    "final_failed": self.transfer_final_failed,
                    "manifest_status": self.transfer_manifest_status,
                    "active": self.status == "running" and self.media_plan_seen,
                    "eta_waiting": self.transfer_eta_waiting,
                    "eta": {
                        kind: {
                            "seconds": self.transfer_etas[kind],
                            "sample_count": self.transfer_eta_samples[kind],
                            "calibrating": self.transfer_eta_calibrating[kind],
                        }
                        for kind in ("download", "copy")
                    },
                },
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
    copy_external = payload.get("copy_external", False)
    if not script_name or (require_key and not script_key):
        raise ValueError("Script Name 和 API Key 不能为空")
    if proxy and not PROXY_PATTERN.match(proxy):
        raise ValueError("代理格式应为 host:port，不能包含协议或账号密码")
    if proxy and not 1 <= int(proxy.rsplit(":", 1)[1]) <= 65535:
        raise ValueError("代理端口必须在 1-65535 范围内")
    if not isinstance(copy_external, bool):
        raise ValueError("外部媒体复制选项必须是 boolean")
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
        "copy_external": copy_external,
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
            "copy_external": settings["copy_external"],
        }
    return handle


def consume_credential(payload: dict[str, Any], settings: dict[str, Any]) -> str:
    handle = str(payload.get("credential_handle", ""))
    with CREDENTIAL_LOCK:
        credential = CREDENTIALS.pop(handle, None)
    if not credential or credential["expires"] <= time.monotonic():
        raise RuntimeError("检查凭据已过期，请重新运行完整检查")
    for key in ("site_url", "script_name", "http_proxy", "copy_external"):
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


def snapshot_summary(inspection: dict[str, Any]) -> dict[str, Any]:
    """Return only bounded count-like snapshot facts for the preflight UI."""
    legacy = inspection.get("legacy_media")
    if not isinstance(legacy, dict):
        legacy = inspection.get("legacy_media_counts")
    safe_legacy: dict[str, Any] = {}
    if isinstance(legacy, dict):
        for key, value in legacy.items():
            normalized = str(key).lower()
            if any(marker in normalized for marker in ("path", "url", "source", "token")):
                continue
            if isinstance(value, (bool, int, float)) or value is None:
                safe_legacy[str(key)[:80]] = value
    return {
        "snapshot_id": str(inspection.get("snapshot_id") or "unknown")[:160],
        "needs_media": bool(inspection.get("needs_media")),
        "existing_complete_supplement": bool(
            inspection.get("existing_complete_supplement")
        ),
        "recoverable_interrupted": bool(inspection.get("recoverable_interrupted")),
        "reused_interrupted": max(
            0,
            int(
                inspection.get("reused_interrupted")
                or inspection.get("reusable_media_count")
                or 0
            ),
        ),
        "legacy_media": safe_legacy,
    }


def supplement_satisfies_policy(
    inspection: dict[str, Any], copy_external: bool
) -> bool:
    """Check that an existing supplement covers the currently requested layer."""
    existing = inspection.get("existing_complete_supplement")
    if not existing:
        return False
    if existing is True:
        return True
    if not copy_external:
        return True
    try:
        manifest_path = Path(str(existing)) / "manifest.json"
        if not manifest_path.is_file() or manifest_path.stat().st_size > 8 * 1024 * 1024:
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        policy = manifest.get("policy") if isinstance(manifest, dict) else {}
        coverage = manifest.get("payload_coverage") if isinstance(manifest, dict) else {}
        return bool((policy or {}).get("copy_external")) and (
            (coverage or {}).get("external") == "complete"
        )
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return False


def preflight(payload: dict[str, Any]) -> dict[str, Any]:
    settings = validate_settings(payload)
    try:
        output = check_output(settings["output"])
        inspection = inspect_latest_snapshot(settings["output"])
        client = create_client(
            settings["site_url"], settings["script_name"], settings["script_key"], settings["http_proxy"]
        )
        server = client.info()
        schema = client.schema_entity_read()
        entities = discover_entities(schema)
        client.schema_field_read("Project")
        client.find_one("Project", [], ["id"], include_archived_projects=True)

        base_found = bool(inspection.get("found"))
        recoverable_interrupted = bool(inspection.get("recoverable_interrupted"))
        supplement_complete = base_found and supplement_satisfies_policy(
            inspection, settings["copy_external"]
        )
        action = (
            "resume_media" if recoverable_interrupted
            else "media_complete" if supplement_complete
            else "media_supplement" if base_found
            else "full_backup"
        )

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
        if action == "full_backup":
            with ThreadPoolExecutor(max_workers=min(settings["workers"], len(entities))) as executor:
                futures = [executor.submit(count_entity, entity) for entity in entities]
                for future in as_completed(futures):
                    entity, value, supported = future.result()
                    counts[entity] = value
                    retirement_support[entity] = supported
        handle = store_credential(settings) if action != "media_complete" else None
        return {
            "ok": True,
            "credential_handle": handle,
            "action": action,
            "base": (
                snapshot_summary(inspection)
                if base_found or recoverable_interrupted else None
            ),
            "checks": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "authenticated": True,
                "schema_entities": len(schema),
                "server_version": server.get("full_version") or server.get("version"),
                "output": output,
                "workers": settings["workers"],
                "media_max_workers": MEDIA_MAX_WORKERS,
            },
            "entities": entities if action == "full_backup" else [],
            "retirement_support": dict(sorted(retirement_support.items())),
            "counts": dict(sorted(counts.items())),
        }
    finally:
        settings["script_key"] = ""


def start_job(payload: dict[str, Any]) -> None:
    settings = validate_settings(payload, require_key=False)
    try:
        settings["script_key"] = consume_credential(payload, settings)
        inspection = inspect_latest_snapshot(settings["output"])
        base_found = bool(inspection.get("found"))
        recoverable_interrupted = bool(inspection.get("recoverable_interrupted"))
        supplement_complete = base_found and supplement_satisfies_policy(
            inspection, settings["copy_external"]
        )
        if supplement_complete and not recoverable_interrupted:
            raise RuntimeError("该完整实体快照的媒体补充包已存在，无需重复传输")
        action = (
            "resume_media" if recoverable_interrupted
            else "media_supplement" if base_found
            else "full_backup"
        )
        expected_counts = payload.get("expected_counts") or {}
        if not isinstance(expected_counts, dict):
            expected_counts = {}
        base_path = str(inspection.get("snapshot_path")) if base_found else None
        STATE.begin(
            expected_counts if action == "full_backup" else {},
            action,
            base_path,
            max(
                0,
                int(
                    inspection.get("reused_interrupted")
                    or inspection.get("reusable_media_count")
                    or 0
                ),
            ),
            settings["copy_external"],
        )
    except Exception:
        settings["script_key"] = ""
        raise

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
                "defer_media": True,
                "page_size": 500,
                "max_retries": 4,
            }
            factory = lambda: create_client(
                settings["site_url"], settings["script_name"], settings["script_key"], settings["http_proxy"]
            )
            if action == "full_backup":
                run_backup(sg, args, config, client_factory=factory, progress=STATE.event)
            result = materialize_latest_media(
                settings["output"],
                sg,
                client_factory=factory,
                max_workers=MEDIA_MAX_WORKERS,
                copy_external=settings["copy_external"],
                progress=STATE.event,
            )
            STATE.finish(str(result))
        except Exception as error:
            if action == "resume_media":
                try:
                    recovered = inspect_latest_snapshot(settings["output"])
                    if recovered.get("found"):
                        STATE.preserve_result(recovered.get("snapshot_path"))
                except Exception:
                    pass
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
