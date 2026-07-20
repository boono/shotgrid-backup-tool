#!/usr/bin/env python3
"""Materialize media omitted from an immutable ShotGrid entity snapshot.

The module deliberately keeps entity snapshots immutable.  It can also seal a
new entity-only base from a provably complete, interrupted legacy export; the
interrupted directory itself is never modified.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import ssl
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urljoin, urlsplit

try:  # Package import (tests/application) and direct script-directory import.
    from .backup import (
        atomic_json,
        atomic_text,
        ensure_private_directory,
        fsync_directory,
        json_value,
        reject_unexpected_html,
        acquire_output_lock,
        release_output_lock,
        safe_error,
        sha256_file,
    )
except ImportError:  # pragma: no cover - direct execution compatibility
    from backup import (  # type: ignore
        atomic_json,
        atomic_text,
        ensure_private_directory,
        fsync_directory,
        json_value,
        reject_unexpected_html,
        acquire_output_lock,
        release_output_lock,
        safe_error,
        sha256_file,
    )


def _verify_base_snapshot(snapshot: Path, require_full: bool = False) -> Dict[str, Any]:
    """Lazy-load the legacy verifier without creating a package import cycle."""
    tool_directory = str(Path(__file__).resolve().parent)
    inserted = False
    if tool_directory not in sys.path:
        sys.path.insert(0, tool_directory)
        inserted = True
    try:
        try:
            from .snapshot_verify import verify_snapshot as verifier
        except (ImportError, ValueError):  # pragma: no cover - direct import
            from snapshot_verify import verify_snapshot as verifier  # type: ignore
        return verifier(snapshot, require_full=require_full)
    finally:
        if inserted:
            try:
                sys.path.remove(tool_directory)
            except ValueError:
                pass


def verify_snapshot(snapshot: Path, require_full: bool = False) -> Dict[str, Any]:
    """Compatibility proxy for callers that previously imported this symbol."""
    return _verify_base_snapshot(snapshot, require_full=require_full)


SUPPLEMENT_FORMAT = "shotgrid_media_supplement"
SUPPLEMENT_SCHEMA_VERSION = 1
SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
URL_IN_TEXT = re.compile(r"https?://[^\s]+", re.IGNORECASE)
SEQUENCE_TOKEN = re.compile(r"(%0?(\d*)d|([#@]+)|\$F(\d*))")
MEDIA_FIELDS = {"image", "filmstrip_image"}
TRANSFER_COUNTERS = (
    "reused_base",
    "reused_supplement",
    "reused_interrupted",
    "resumed",
    "downloaded",
    "copied",
)
MIN_FREE_RESERVE_BYTES = 512 * 1024 * 1024
MAX_SINGLE_MEDIA_BYTES = 2 * 1024**4
MAX_SUPPLEMENT_BYTES = 100 * 1024**4
MAX_SEQUENCE_SCAN_ENTRIES = 1_000_000
MAX_SEQUENCE_FILES = 200_000
MAX_SEQUENCE_BYTES = 100 * 1024**4


class MediaSyncError(RuntimeError):
    """A bounded, user-facing media synchronization failure."""


class TransientMediaPending(MediaSyncError):
    """ShotGrid is still returning its transient processing placeholder."""


def _require_disk_capacity(directory: Path, requested_bytes: int = 0) -> None:
    requested = max(0, int(requested_bytes))
    if requested > MAX_SINGLE_MEDIA_BYTES:
        raise MediaSyncError("单个媒体超过安全大小上限")
    usage = shutil.disk_usage(directory)
    if usage.free - requested < MIN_FREE_RESERVE_BYTES:
        raise MediaSyncError("输出磁盘剩余空间不足，已在写入前停止")


def _require_supplement_capacity(directory: Path, requested_bytes: int) -> None:
    requested = max(0, int(requested_bytes))
    if requested > MAX_SUPPLEMENT_BYTES:
        raise MediaSyncError("媒体补充包已知总量超过安全大小上限")
    usage = shutil.disk_usage(directory)
    if usage.free - requested < MIN_FREE_RESERVE_BYTES:
        raise MediaSyncError("输出磁盘不足以容纳已知的待传输媒体")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _timestamp_id(suffix: str = "") -> str:
    value = _utc_now().strftime("%Y%m%dT%H%M%S_%fZ")
    return value + suffix


def _safe_component(value: Any, fallback: str) -> str:
    cleaned = SAFE_COMPONENT.sub("_", str(value)).strip("._")
    return (cleaned or fallback)[:96]


def _id_bucket(source_id: int) -> str:
    start = (int(source_id) // 1000) * 1000
    return f"{start:06d}_{start + 999:06d}"


def _frame_bucket(frame: int) -> str:
    return "frames_" + _id_bucket(frame)


def _canonical_json_hash(value: Any) -> str:
    encoded = json.dumps(
        json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_origin(value: str) -> str:
    raw = str(value).strip()
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise MediaSyncError("ShotGrid server 不是有效的 HTTP(S) origin")
    if parsed.username is not None or parsed.password is not None:
        raise MediaSyncError("ShotGrid server origin 不得包含 userinfo")
    host = parsed.hostname.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise MediaSyncError("ShotGrid server hostname 无法规范化") from error
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{parsed.scheme.lower()}://{authority}"


def _site_fingerprint(value: str) -> str:
    return hashlib.sha256(_normalized_origin(value).encode("utf-8")).hexdigest()


def _sg_server(sg: Any) -> str:
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
        value = getattr(sg, "server", None)
    if not value:
        raise MediaSyncError("当前 ShotGrid client 未暴露 server origin，无法执行站点门禁")
    return str(value)


def _safe_relative(root: Path, relative: str) -> Path:
    if (
        not relative
        or "\\" in relative
        or "\x00" in relative
        or re.match(r"^[A-Za-z]:", relative)
        or Path(relative).is_absolute()
        or any(part in {"", ".", ".."} for part in Path(relative).parts)
    ):
        raise MediaSyncError("索引包含无效相对路径")
    if root.is_symlink():
        raise MediaSyncError("索引 root 是符号链接")
    current = root
    for component in Path(relative).parts:
        current = current / component
        if current.is_symlink():
            raise MediaSyncError("索引路径组件是符号链接")
    target = (root / relative).resolve()
    resolved = root.resolve()
    if target != resolved and resolved not in target.parents:
        raise MediaSyncError("索引路径越界")
    return target


def _iter_jsonl(path: Path) -> Iterator[Tuple[int, Dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise MediaSyncError(f"JSONL 第 {line_number} 行不是 object")
            yield line_number, value


def _parse_checksums(path: Path) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise MediaSyncError(f"checksums.sha256 第 {line_number} 行格式错误")
        rows.append((parts[0], parts[1]))
    return rows


def _hash_triplet(snapshot: Path) -> Dict[str, str]:
    required = {
        "base_manifest_sha256": snapshot / "manifest.json",
        "base_completed_sha256": snapshot / "COMPLETED.json",
        "base_checksums_sha256": snapshot / "checksums.sha256",
    }
    result: Dict[str, str] = {}
    for key, path in required.items():
        if not path.is_file():
            raise MediaSyncError(f"base 缺少 {path.name}")
        result[key] = sha256_file(path)
    return result


def _assert_hash_triplet(snapshot: Path, expected: Dict[str, str]) -> None:
    actual = _hash_triplet(snapshot)
    if actual != expected:
        raise MediaSyncError("base manifest/COMPLETED/checksums 在媒体补全过程中发生变化")


def _published_snapshot_candidates(output_root: Path) -> List[Path]:
    candidates: List[Path] = []
    if not output_root.is_dir():
        return candidates
    for path in output_root.iterdir():
        if not path.is_dir() or path.name.endswith(".incomplete"):
            continue
        if path.name == "media_supplements":
            continue
        if (path / "manifest.json").is_file() and (path / "COMPLETED.json").is_file():
            candidates.append(path.resolve())
    return sorted(candidates, key=lambda item: item.name)


def find_latest_snapshot(output_root: Path) -> Optional[Path]:
    """Return the latest sealed entity snapshot without mutating the output root."""
    root = Path(output_root).expanduser().resolve()
    pointer = root / "latest.txt"
    if pointer.is_file():
        try:
            name = pointer.read_text(encoding="utf-8").strip()
            if name and Path(name).name == name and not name.endswith(".incomplete"):
                target = root / name
                if (
                    target.is_dir()
                    and (target / "manifest.json").is_file()
                    and (target / "COMPLETED.json").is_file()
                ):
                    return target.resolve()
        except (OSError, UnicodeError):
            pass
    candidates = _published_snapshot_candidates(root)
    return candidates[-1] if candidates else None


def _interrupted_candidates(output_root: Path) -> List[Path]:
    if not output_root.is_dir():
        return []
    return sorted(
        [
            path
            for path in output_root.iterdir()
            if path.is_dir() and path.name.endswith(".incomplete")
        ],
        key=lambda item: item.name,
    )


def _entity_inventory(
    entity_path: Path, entity: str, collect_records: bool = False
) -> Tuple[Dict[str, int], set, List[Dict[str, Any]]]:
    counts = {"active": 0, "retired": 0}
    previous = {False: 0, True: 0}
    ids: set = set()
    records: List[Dict[str, Any]] = []
    for line_number, envelope in _iter_jsonl(entity_path):
        source = envelope.get("source") or {}
        record = envelope.get("record") or {}
        state_name = envelope.get("state")
        if state_name not in {"active", "retired"}:
            raise MediaSyncError(f"{entity} entity JSONL 第 {line_number} 行 state 无效")
        retired = state_name == "retired"
        source_id = source.get("id")
        if (
            not isinstance(source_id, int)
            or source_id <= 0
            or source.get("type") != entity
            or record.get("id") != source_id
        ):
            raise MediaSyncError(f"{entity} entity JSONL 第 {line_number} 行 envelope 无效")
        if source_id <= previous[retired] or source_id in ids:
            raise MediaSyncError(f"{entity} entity JSONL 第 {line_number} 行 ID 顺序无效")
        previous[retired] = source_id
        ids.add(source_id)
        counts[state_name] += 1
        if collect_records:
            records.append(envelope)
    return counts, ids, records


def _link_inventory(link_path: Path, entity: str, source_ids: set) -> int:
    count = 0
    for line_number, link in _iter_jsonl(link_path):
        source = link.get("source") or {}
        target = link.get("target") or {}
        if (
            source.get("type") != entity
            or source.get("id") not in source_ids
            or not target.get("type")
            or not isinstance(target.get("id"), int)
            or not link.get("field")
            or not isinstance(link.get("ordinal"), int)
        ):
            raise MediaSyncError(f"{entity} links JSONL 第 {line_number} 行无效")
        count += 1
    return count


def _estimate_interrupted_media(path: Path) -> int:
    total = 0
    for root_name in ("attachments", "media"):
        root = path / root_name
        if not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            try:
                if (
                    candidate.is_file()
                    and not candidate.is_symlink()
                    and not candidate.name.endswith(".part")
                    and candidate.name != "index.json"
                    and candidate.stat().st_size > 0
                ):
                    total += 1
            except OSError:
                continue
    return total


def _assess_interrupted(path: Path) -> Dict[str, Any]:
    """Prove that all entity/schema/link exports finished before interruption."""
    result: Dict[str, Any] = {
        "recoverable": False,
        "path": str(path),
        "snapshot_id": path.name[: -len(".incomplete")],
        "errors": [],
        "entities": {},
        "reusable_media_count": _estimate_interrupted_media(path),
    }
    try:
        schema_path = path / "schema/entities.json"
        if not schema_path.is_file():
            raise MediaSyncError("中断快照缺少 schema/entities.json")
        entity_schema = _load_json(schema_path)
        if not isinstance(entity_schema, dict) or not entity_schema:
            raise MediaSyncError("中断快照实体 schema 为空或无效")
        interrupted_manifest = {}
        if (path / "manifest.json").is_file():
            value = _load_json(path / "manifest.json")
            if isinstance(value, dict):
                interrupted_manifest = value
        recovery_header = {}
        if (path / "recovery_header.json").is_file():
            value = _load_json(path / "recovery_header.json")
            if isinstance(value, dict):
                recovery_header = value
        event_path = path / "logs/events.jsonl"
        if not event_path.is_file():
            # Schema-v1 exports predate entity envelopes, relationship indexes
            # and durable completion events.  They can never be promoted to a
            # v3 entity base.  A later, freshly generated v3 base may still
            # reuse payloads whose old index contains a trustworthy SHA-256,
            # but only when the v1 manifest proves the same source site.
            legacy_schema = int(interrupted_manifest.get("schema_version", 0) or 0)
            has_raw_entities = False
            for legacy_entity_path in (path / "entities").glob("*.jsonl"):
                try:
                    first_row = next(_iter_jsonl(legacy_entity_path), None)
                    envelope = first_row[1] if first_row else {}
                    has_raw_entities = not (
                        isinstance(envelope.get("source"), dict)
                        and isinstance(envelope.get("record"), dict)
                    )
                except Exception:
                    has_raw_entities = False
                break
            if legacy_schema == 1 or has_raw_entities:
                result["legacy_v1_media_only"] = True
                result["legacy_source_site"] = str(
                    interrupted_manifest.get("site") or ""
                )
                result["interrupted_manifest"] = interrupted_manifest
                result["recovery_header"] = recovery_header
                return result
        recovery_source = (
            recovery_header.get("source") or interrupted_manifest.get("source") or {}
        )
        if not recovery_source.get("site"):
            raise MediaSyncError("中断快照缺少 source.site 恢复证据")
        planned = recovery_header.get("entity_types_planned")
        if not isinstance(planned, list) or not planned:
            planned = interrupted_manifest.get("entity_types_planned")
        if not isinstance(planned, list) or not planned:
            planned = sorted(entity_schema)
        planned_set = {str(item) for item in planned}
        if planned_set != set(entity_schema):
            raise MediaSyncError("中断快照不是全 schema 实体导出，不能封存为完整 API base")

        completed: Dict[str, Dict[str, int]] = {}
        forbidden: List[str] = []
        for _, event in _iter_jsonl(event_path):
            name = event.get("event")
            if name == "entity_complete" and event.get("entity"):
                entity = str(event["entity"])
                if entity in completed:
                    raise MediaSyncError(f"中断快照含重复 entity_complete：{entity}")
                try:
                    completed[entity] = {
                        "active": int(event["active"]),
                        "retired": int(event["retired"]),
                    }
                except (KeyError, TypeError, ValueError) as error:
                    raise MediaSyncError(
                        f"中断快照的 {entity} entity_complete 缺少有效计数"
                    ) from error
            elif name == "entity_error":
                forbidden.append(str(name))
        if forbidden:
            raise MediaSyncError("中断快照包含 entity_error，拒绝恢复")
        missing_events = sorted(planned_set - set(completed))
        if missing_events:
            raise MediaSyncError(
                "中断快照缺少 entity_complete 证据：" + ", ".join(missing_events[:12])
            )

        metadata: Dict[str, Any] = {}
        for entity in sorted(planned_set):
            field_schema_path = path / "schema/fields" / f"{entity}.json"
            entity_path = path / "entities" / f"{entity}.jsonl"
            link_path = path / "links" / f"{entity}.jsonl"
            if not field_schema_path.is_file() or not entity_path.is_file() or not link_path.is_file():
                raise MediaSyncError(f"中断快照缺少 {entity} 的 schema/entity/link 文件")
            field_schema = _load_json(field_schema_path)
            if not isinstance(field_schema, dict):
                raise MediaSyncError(f"{entity} 字段 schema 无效")
            counts, source_ids, records = _entity_inventory(
                entity_path, entity, collect_records=True
            )
            if counts != completed[entity]:
                raise MediaSyncError(
                    f"{entity} entity_complete 计数与实体文件不一致："
                    f"event={completed[entity]} actual={counts}"
                )
            link_count = _link_inventory(link_path, entity, source_ids)
            link_fields = {
                str(field): _schema_data_type(field_meta)
                for field, field_meta in field_schema.items()
                if _schema_data_type(field_meta) in {"entity", "multi_entity"}
            }
            expected_links = 0
            for envelope in records:
                record = envelope.get("record") or {}
                for field, data_type in link_fields.items():
                    value = record.get(field)
                    if data_type == "entity":
                        expected_links += int(
                            isinstance(value, dict)
                            and isinstance(value.get("id"), int)
                            and bool(value.get("type"))
                        )
                    elif isinstance(value, list):
                        expected_links += sum(
                            isinstance(target, dict)
                            and isinstance(target.get("id"), int)
                            and bool(target.get("type"))
                            for target in value
                        )
            if link_count != expected_links:
                raise MediaSyncError(
                    f"{entity} link 计数与实体记录不一致："
                    f"expected={expected_links} actual={link_count}"
                )
            declared = (interrupted_manifest.get("entities") or {}).get(entity) or {}
            entity_hash = sha256_file(entity_path)
            link_hash = sha256_file(link_path)
            for key, actual in (
                ("active", counts["active"]),
                ("retired", counts["retired"]),
                ("link_count", link_count),
                ("sha256", entity_hash),
                ("link_sha256", link_hash),
            ):
                if key in declared and declared.get(key) != actual:
                    raise MediaSyncError(
                        f"{entity} 的原 manifest {key} 与实际文件不一致"
                    )
            metadata[entity] = {
                **counts,
                "fields": len(field_schema),
                "file": f"entities/{entity}.jsonl",
                "sha256": entity_hash,
                "schema_file": f"schema/fields/{entity}.json",
                "link_file": f"links/{entity}.jsonl",
                "link_count": link_count,
                "link_sha256": link_hash,
                "retirement_supported": bool(
                    declared.get("retirement_supported", counts["retired"] > 0)
                ),
            }
        result["entities"] = metadata
        result["entity_schema"] = entity_schema
        result["interrupted_manifest"] = interrupted_manifest
        result["recovery_header"] = recovery_header
        result["recoverable"] = True
    except Exception as error:
        bounded = safe_error(error)
        bounded["message"] = URL_IN_TEXT.sub("[REDACTED_URL]", bounded["message"])
        result["errors"].append(bounded)
    return result


def _copy_file_atomic(source: Path, target: Path) -> int:
    """Copy a regular non-symlink file through a private, fsynced .part file."""
    source_stat = source.lstat()
    if source.is_symlink() or not stat.S_ISREG(source_stat.st_mode):
        raise MediaSyncError("本地媒体源不是普通文件或是符号链接")
    ensure_private_directory(target.parent)
    _require_disk_capacity(target.parent, source_stat.st_size)
    part = target.with_name(target.name + ".part")
    if part.exists() or part.is_symlink():
        if part.is_symlink() or not part.is_file():
            raise MediaSyncError("目标 .part 不是安全的普通文件")
        part.unlink()
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(str(source), source_flags)
    target_fd: Optional[int] = None
    copied_digest = hashlib.sha256()
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise MediaSyncError("本地媒体源在打开时不再是普通文件")
        target_fd = os.open(str(part), target_flags, 0o600)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            copied_digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        os.fsync(target_fd)
        after = os.fstat(source_fd)
        if (
            opened.st_ino,
            opened.st_dev,
            opened.st_size,
            opened.st_mode,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_ino,
            after.st_dev,
            after.st_size,
            after.st_mode,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise MediaSyncError("本地媒体源在复制期间发生变化")
    except Exception:
        part.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)
    if sha256_file(part) != copied_digest.hexdigest():
        part.unlink(missing_ok=True)
        raise MediaSyncError("本地媒体复制内容与读取流哈希不一致")
    os.chmod(part, 0o600)
    os.replace(part, target)
    os.chmod(target, 0o600)
    fsync_directory(target.parent)
    return target.stat().st_size


def _clone_or_copy(source: Path, target: Path) -> Tuple[int, bool]:
    source_stat = source.lstat()
    if source.is_symlink() or not stat.S_ISREG(source_stat.st_mode):
        raise MediaSyncError("复用源不是普通文件或是符号链接")
    ensure_private_directory(target.parent)
    if target.exists() or target.is_symlink():
        raise MediaSyncError("复用目标已存在")
    if os.name != "nt" and source_stat.st_mode & 0o077:
        return _copy_file_atomic(source, target), False
    if sys.platform == "darwin":
        part = target.with_name(target.name + ".part")
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            clonefile = libc.clonefile
            clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
            clonefile.restype = ctypes.c_int
            if clonefile(os.fsencode(source), os.fsencode(part), 0) == 0:
                after = source.lstat()
                if (
                    source_stat.st_ino,
                    source_stat.st_dev,
                    source_stat.st_size,
                    source_stat.st_mode,
                    source_stat.st_mtime_ns,
                    source_stat.st_ctime_ns,
                ) != (
                    after.st_ino,
                    after.st_dev,
                    after.st_size,
                    after.st_mode,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise MediaSyncError("复用源在 clone 期间发生变化")
                os.chmod(part, 0o600)
                os.replace(part, target)
                fsync_directory(target.parent)
                return source_stat.st_size, True
        except (AttributeError, OSError):
            pass
        finally:
            part.unlink(missing_ok=True)
    return _copy_file_atomic(source, target), False


def _remove_replaceable_target(target: Path) -> None:
    if target.is_symlink():
        raise MediaSyncError("拒绝覆盖符号链接目标")
    if target.exists():
        if not target.is_file():
            raise MediaSyncError("拒绝覆盖非普通文件目标")
        target.unlink()


def _seal_recovered_base(
    output_root: Path, assessment: Dict[str, Any], sg: Any
) -> Path:
    source = Path(assessment["path"])
    snapshot_id = _timestamp_id("_recovered")
    incomplete = output_root / f"{snapshot_id}.incomplete"
    final = output_root / snapshot_id
    if incomplete.exists() or final.exists():
        raise MediaSyncError("recovered base 目标目录已存在")
    ensure_private_directory(incomplete)
    copy_roots = ["schema/entities.json", "logs/events.jsonl"]
    if (source / "recovery_header.json").is_file():
        copy_roots.append("recovery_header.json")
    for entity in sorted(assessment["entities"]):
        copy_roots.extend(
            [
                f"schema/fields/{entity}.json",
                f"entities/{entity}.jsonl",
                f"links/{entity}.jsonl",
            ]
        )
    try:
        for relative in copy_roots:
            original = source / relative
            target = incomplete / relative
            _clone_or_copy(original, target)
        atomic_json(incomplete / "logs/errors.json", [])
        server = _sg_server(sg)
        interrupted_manifest = assessment.get("interrupted_manifest") or {}
        recovery_header = assessment.get("recovery_header") or {}
        evidence = recovery_header or interrupted_manifest
        evidence_source = evidence.get("source") or {}
        declared_site = str(evidence_source.get("site") or "")
        if not declared_site:
            raise MediaSyncError("中断快照缺少可验证的 source.site，拒绝自动封存")
        source_origin = _normalized_origin(declared_site)
        canonical_fingerprint = _site_fingerprint(source_origin)
        evidence_completeness = interrupted_manifest.get("completeness") or {}
        declared_profile = str(evidence_completeness.get("profile") or "")
        strict_profile = declared_profile in {"site_full", "site_api_full"}
        mode = str(evidence.get("mode") or "full")
        include_retired = bool(evidence.get("include_retired", False))
        all_readable = bool(
            recovery_header.get("all_readable_entities")
            or evidence_completeness.get("all_readable_entities")
            or evidence.get("scope") == "all_readable_entities"
        )
        full_history = bool(
            evidence_completeness.get("full_history")
            if interrupted_manifest
            else mode == "full" and include_retired
        )
        recovered_profile = declared_profile if strict_profile else "recovered_entity_export"
        started_at = _utc_now().isoformat()
        manifest: Dict[str, Any] = {
            "format": "shotgrid_portable_snapshot",
            "schema_version": 3,
            "snapshot_id": snapshot_id,
            "started_at": started_at,
            "completed_at": _utc_now().isoformat(),
            "status": "complete",
            "source": {
                "site": source_origin,
                "site_fingerprint": canonical_fingerprint,
            },
            "tool": {"name": "ews_sg_media_sync_recovery", "version": "1.0.0"},
            "mode": mode,
            "updated_since": evidence.get("updated_since"),
            "snapshot_upper_bound": evidence.get("snapshot_upper_bound"),
            "include_archived_projects": bool(
                evidence.get("include_archived_projects", False)
            ),
            "include_retired": include_retired,
            "consistency": "recovered_completed_entity_exports",
            "scope": (
                "all_readable_entities" if all_readable else "recovered_completed_entities"
            ),
            "entity_types_planned": sorted(assessment["entities"]),
            "entities": assessment["entities"],
            "completeness": {
                "profile": recovered_profile,
                "all_readable_entities": all_readable,
                "full_history": full_history,
                "attachment_payloads": False,
                "downloadable_media_payloads": False,
                "external_published_files": "deferred",
            },
            "payload_scope": "deferred/recovered",
            "attachments": {"downloaded": 0, "failed": 0, "skipped": 0, "total": 0},
            "media": {"downloaded": 0, "failed": 0, "metadata_only": 0, "total": 0},
            "lineage": {
                "recovered_from_interrupted_snapshot_id": assessment["snapshot_id"],
                "recovered_from_directory": source.name,
                "recovery_evidence": (
                    "recovery_header+schema+entity_complete_counts+entity_jsonl+links_jsonl"
                    if recovery_header
                    else "schema+entity_complete_counts+entity_jsonl+links_jsonl"
                ),
                "source_identity_evidence": (
                    "recovery_header"
                    if recovery_header
                    else "interrupted_manifest"
                    if declared_site
                    else "missing"
                ),
                "history_scope_evidence": (
                    "interrupted_manifest"
                    if interrupted_manifest
                    else "recovery_header"
                    if recovery_header
                    else "unproven"
                ),
            },
            "errors": [],
        }
        atomic_json(incomplete / "manifest.json", manifest)
        payloads = sorted(
            path
            for path in incomplete.rglob("*")
            if path.is_file() and path.name not in {"manifest.json", "checksums.sha256", "COMPLETED.json"}
        )
        lines = [
            f"{sha256_file(path)}  {path.relative_to(incomplete).as_posix()}" for path in payloads
        ]
        atomic_text(incomplete / "checksums.sha256", "\n".join(lines) + "\n")
        manifest["integrity"] = {
            "algorithm": "sha256",
            "checksums_file": "checksums.sha256",
            "files_hashed": len(lines),
            "manifest_excluded_to_avoid_self_reference": True,
        }
        atomic_json(incomplete / "manifest.json", manifest)
        atomic_json(
            incomplete / "COMPLETED.json",
            {
                "format": "shotgrid_snapshot_completion_receipt",
                "snapshot_id": snapshot_id,
                "completed_at": manifest["completed_at"],
                "manifest_sha256": sha256_file(incomplete / "manifest.json"),
                "checksums_sha256": sha256_file(incomplete / "checksums.sha256"),
            },
        )
        os.replace(incomplete, final)
        fsync_directory(output_root)
        atomic_text(output_root / "latest.txt", snapshot_id + "\n")
        return final
    except Exception:
        # A failed recovery remains clearly unpublished and resumability is not
        # claimed.  Do not remove it: its contents can aid diagnosis.
        raise


def _latest_complete_supplement(base: Path) -> Optional[Path]:
    root = base.parent / "media_supplements" / base.name
    pointer = root / "latest.txt"
    candidates: List[Path] = []
    if pointer.is_file():
        try:
            name = pointer.read_text(encoding="utf-8").strip()
            if name and Path(name).name == name and not name.endswith(".incomplete"):
                candidates.append(root / name)
        except OSError:
            pass
    if root.is_dir():
        candidates.extend(
            path
            for path in root.iterdir()
            if path.is_dir() and not path.name.endswith(".incomplete")
        )
    seen: set = set()
    for candidate in sorted(candidates, key=lambda item: item.name, reverse=True):
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result = verify_media_supplement(resolved)
        if result.get("ok"):
            return resolved
    return None


def inspect_latest_snapshot(output_root: Path) -> Dict[str, Any]:
    """Inspect the latest base and any salvageable interrupted export."""
    root = Path(output_root).expanduser()
    result: Dict[str, Any] = {
        "found": False,
        "snapshot_path": None,
        "snapshot_id": None,
        "existing_complete_supplement": None,
        "needs_media": True,
        "legacy_media": False,
        "legacy_media_counts": {"attachments": 0, "media": 0},
        "recoverable_interrupted": False,
        "interrupted_path": None,
        "reusable_media_count": 0,
    }
    base = find_latest_snapshot(root)
    if base:
        verification = _verify_base_snapshot(base, require_full=False)
        manifest = _load_json(base / "manifest.json") if (base / "manifest.json").is_file() else {}
        attachment_count = int((manifest.get("attachments") or {}).get("downloaded", 0))
        media_count = int((manifest.get("media") or {}).get("downloaded", 0))
        existing = _latest_complete_supplement(base) if verification.get("ok") else None
        result.update(
            {
                "found": True,
                "snapshot_path": str(base),
                "snapshot_id": manifest.get("snapshot_id") or base.name,
                "verification": verification,
                "existing_complete_supplement": str(existing) if existing else None,
                "needs_media": existing is None,
                "legacy_media": bool(attachment_count or media_count),
                "legacy_media_counts": {
                    "attachments": attachment_count,
                    "media": media_count,
                },
            }
        )
    interrupted = _interrupted_candidates(root)
    if interrupted:
        candidate = interrupted[-1]
        if base is None or candidate.name[: -len(".incomplete")] > base.name:
            assessment = _assess_interrupted(candidate)
            result.update(
                {
                    "recoverable_interrupted": bool(assessment.get("recoverable")),
                    "interrupted_path": str(candidate.resolve()),
                    "reusable_media_count": int(assessment.get("reusable_media_count", 0)),
                    "interrupted_errors": assessment.get("errors", []),
                }
            )
    return result


def _schema_data_type(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return "unknown"
    value = metadata.get("data_type", "unknown")
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    return str(value or "unknown")


def _locator_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value if value.startswith(("http://", "https://")) else None
    if isinstance(value, dict):
        url = value.get("url")
        return str(url) if isinstance(url, str) and url.startswith(("http://", "https://")) else None
    return None


def _is_transient_media_locator(value: Any) -> bool:
    url = _locator_url(value)
    if not url:
        return False
    try:
        return "/images/status/transient/" in urlsplit(url).path.lower()
    except ValueError:
        return False


def _locator_expected_size(record: Dict[str, Any], value: Any) -> Optional[int]:
    candidates: List[Any] = []
    if isinstance(value, dict):
        candidates.extend([value.get("size"), value.get("file_size")])
    candidates.extend([record.get("file_size"), record.get("sg_file_size")])
    for candidate in candidates:
        try:
            parsed = int(candidate)
            if parsed >= 0:
                return parsed
        except (TypeError, ValueError):
            continue
    return None


def _source_key(entity: str, source_id: int, field: str) -> str:
    return f"{entity}:{int(source_id)}:{field}"


def _media_relative_root(entity: str, source_id: int, field: str) -> Path:
    return (
        Path("media")
        / _safe_component(entity, "Entity")
        / _id_bucket(source_id)
        / str(int(source_id))
        / _safe_component(field, "field")
    )


def _safe_media_name(value: Any, field: str, source_id: int) -> str:
    original: Any = None
    if isinstance(value, dict):
        original = value.get("name") or value.get("display_name")
    url = _locator_url(value)
    if not original and url:
        original = Path(unquote(urlsplit(url).path)).name
    if not original and isinstance(value, dict):
        for key in ("local_path_mac", "local_path", "local_path_linux", "local_path_windows"):
            if value.get(key):
                original = Path(str(value[key])).name
                break
    cleaned = _safe_component(original or f"{field}_{source_id}", "media")
    return cleaned[:160]


def _classify_locator(
    entity: str,
    source_id: int,
    field: str,
    state_name: str,
    data_type: str,
    value: Any,
    record: Dict[str, Any],
    copy_external: bool,
) -> Dict[str, Any]:
    link_type = value.get("link_type") if isinstance(value, dict) else None
    is_image = data_type == "image" or field in MEDIA_FIELDS
    is_published_path = entity == "PublishedFile" and field == "path"
    expected_size = _locator_expected_size(record, value)
    locator_hash = _canonical_json_hash(value)
    item: Dict[str, Any] = {
        "source": {"type": entity, "id": source_id},
        "source_key": _source_key(entity, source_id, field),
        "field": field,
        "state": state_name,
        "status": "pending",
        "owner": "none",
        "acquisition": "pending",
        "files": [],
        "locator_sha256": locator_hash,
        "temporal_fidelity": "snapshot_exact",
        "_locator": value,
        "_expected_size": expected_size,
        "_record": record,
        "_required": True,
    }
    if link_type == "upload":
        item["kind"] = "hosted_upload"
        item["_transfer"] = "download"
        return item
    if link_type == "local":
        item["kind"] = "external_local"
        item["temporal_fidelity"] = "current_copy"
        item["_transfer"] = "copy"
        if not copy_external:
            item.update(
                {
                    "status": "excluded_by_policy",
                    "owner": "none",
                    "acquisition": "excluded_by_policy",
                    "_required": False,
                    "_transfer": None,
                }
            )
        return item
    if is_published_path and link_type == "web":
        item.update(
            {
                "kind": "ordinary_web",
                "status": "skipped",
                "owner": "none",
                "acquisition": "ordinary",
                "_required": False,
                "_transfer": None,
            }
        )
        return item
    if is_image:
        url = _locator_url(value)
        if url:
            item["kind"] = "hosted_image"
            item["_transfer"] = "download"
        else:
            item["kind"] = "ambiguous_hosted"
            item["_transfer"] = None
            item["status"] = "failed"
            item["acquisition"] = "failed"
            item["_planning_error"] = "托管 image/filmstrip locator 无法安全解释"
            item["_error_code"] = "UNSAFE_LOCATOR"
        return item
    if is_published_path:
        if value is None or value == "":
            item.update(
                {
                    "kind": "no_payload",
                    "status": "skipped",
                    "owner": "none",
                    "acquisition": "no_payload",
                    "_required": False,
                    "_transfer": None,
                }
            )
            return item
        url = _locator_url(value)
        if url and urlsplit(url).scheme.lower() == "https":
            item["kind"] = "external_https"
            item["temporal_fidelity"] = "current_fetch"
            item["_transfer"] = "download"
        else:
            item["kind"] = "ambiguous_external"
            item["_transfer"] = None
            item["status"] = "failed"
            item["acquisition"] = "failed"
            item["_planning_error"] = "PublishedFile.path locator 无法安全解释"
            item["_error_code"] = "UNSAFE_LOCATOR"
        if not copy_external:
            item.update(
                {
                    "status": "excluded_by_policy",
                    "owner": "none",
                    "acquisition": "excluded_by_policy",
                    "_required": False,
                    "_transfer": None,
                }
            )
            item.pop("_planning_error", None)
        return item
    if _locator_url(value):
        item.update(
            {
                "kind": "ordinary_web",
                "status": "skipped",
                "owner": "none",
                "acquisition": "ordinary",
                "_required": False,
                "_transfer": None,
            }
        )
        return item
    item.update(
        {
            "kind": "ambiguous",
            "status": "skipped",
            "owner": "none",
            "acquisition": "ordinary",
            "_required": False,
            "_transfer": None,
        }
    )
    return item


def _build_media_plan(base: Path, copy_external: bool) -> List[Dict[str, Any]]:
    manifest = _load_json(base / "manifest.json")
    entities = manifest.get("entity_types_planned") or sorted((manifest.get("entities") or {}))
    plan: List[Dict[str, Any]] = []
    seen: set = set()
    for entity_value in entities:
        entity = str(entity_value)
        schema_path = base / "schema/fields" / f"{entity}.json"
        entity_path = base / "entities" / f"{entity}.jsonl"
        if not schema_path.is_file() or not entity_path.is_file():
            raise MediaSyncError(f"base 缺少 {entity} schema/entity 文件")
        field_schema = _load_json(schema_path)
        if not isinstance(field_schema, dict):
            raise MediaSyncError(f"base 的 {entity} field schema 无效")
        candidate_fields: Dict[str, str] = {}
        for field, metadata in field_schema.items():
            data_type = _schema_data_type(metadata)
            if (
                data_type in {"image", "url"}
                or field in MEDIA_FIELDS
                or (entity == "Attachment" and field == "this_file")
                or (entity == "PublishedFile" and field == "path")
            ):
                candidate_fields[str(field)] = data_type
        for _, envelope in _iter_jsonl(entity_path):
            source = envelope.get("source") or {}
            record = envelope.get("record") or {}
            source_id = int(source["id"])
            state_name = str(envelope.get("state"))
            for field, data_type in candidate_fields.items():
                value = record.get(field)
                is_published_path = entity == "PublishedFile" and field == "path"
                if (value is None or value == "") and not is_published_path:
                    continue
                key = _source_key(entity, source_id, field)
                if key in seen:
                    raise MediaSyncError(f"base 媒体 source key 重复：{key}")
                seen.add(key)
                plan.append(
                    _classify_locator(
                        entity,
                        source_id,
                        field,
                        state_name,
                        data_type,
                        value,
                        record,
                        copy_external,
                    )
                )
    plan.sort(key=lambda item: item["source_key"])
    return plan


def _validated_file(path: Path, size: Any, digest: Any) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        if path.stat().st_size != int(size):
            return False
        return bool(digest) and sha256_file(path) == str(digest)
    except (OSError, TypeError, ValueError):
        return False


def _base_media_map(base: Path) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    attachment_index = base / "attachments/index.json"
    if attachment_index.is_file():
        rows = _load_json(attachment_index)
        if isinstance(rows, list):
            for row in rows:
                try:
                    source_id = int(row["attachment_id"])
                    relative = "attachments/" + str(row["file"])
                    target = _safe_relative(base, relative)
                    if not _validated_file(target, row.get("size"), row.get("sha256")):
                        continue
                    key = _source_key("Attachment", source_id, "this_file")
                    result[key] = {
                        "path": relative,
                        "size": int(row["size"]),
                        "sha256": str(row["sha256"]),
                    }
                except (KeyError, TypeError, ValueError, MediaSyncError):
                    continue
    media_index = base / "media/index.json"
    if media_index.is_file():
        rows = _load_json(media_index)
        if isinstance(rows, list):
            for row in rows:
                try:
                    source = row.get("source") or {}
                    key = _source_key(str(source["type"]), int(source["id"]), str(row["field"]))
                    relative = str(row["file"])
                    target = _safe_relative(base, relative)
                    if not _validated_file(target, row.get("size"), row.get("sha256")):
                        continue
                    result[key] = {
                        "path": relative,
                        "size": int(row["size"]),
                        "sha256": str(row["sha256"]),
                    }
                except (KeyError, TypeError, ValueError, MediaSyncError):
                    continue
    return result


def _apply_base_reuse(
    plan: List[Dict[str, Any]], base: Path, counters: Dict[str, int]
) -> None:
    mapping = _base_media_map(base)
    for item in plan:
        if not item.get("_required") or item.get("status") != "pending":
            continue
        if _is_transient_media_locator(item.get("_locator")):
            continue
        file_info = mapping.get(item["source_key"])
        if not file_info:
            continue
        item.update(
            {
                "status": "complete",
                "owner": "base",
                "acquisition": "reused_base",
                "files": [file_info],
                "_transfer": None,
            }
        )
        counters["reused_base"] += 1
        counters["reused_base_bytes"] += int(file_info["size"])


def _local_locator_path(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    if sys.platform == "darwin":
        keys = ("local_path_mac", "local_path")
    elif os.name == "nt":
        keys = ("local_path_windows", "local_path")
    else:
        keys = ("local_path_linux", "local_path")
    for key in keys:
        candidate = value.get(key)
        if candidate:
            return str(candidate)
    return None


def _sequence_pattern(name: str) -> Optional[Tuple[re.Pattern, int]]:
    matches = list(SEQUENCE_TOKEN.finditer(name))
    if not matches:
        return None
    if len(matches) != 1:
        raise MediaSyncError("本地序列 basename 包含多个 frame token")
    match = matches[0]
    width_text = match.group(2) or match.group(4)
    if match.group(3):
        width_text = str(len(match.group(3)))
    width = int(width_text) if width_text else 0
    digits = rf"(?P<frame>\d{{{width}}})" if width else r"(?P<frame>\d+)"
    expression = "^" + re.escape(name[: match.start()]) + digits + re.escape(name[match.end() :]) + "$"
    return re.compile(expression), width


def _expand_local_files(value: Any) -> List[Dict[str, Any]]:
    raw = _local_locator_path(value)
    if not raw:
        raise MediaSyncError("local locator 缺少当前平台可用路径")
    if raw.startswith("file://"):
        parsed = urlsplit(raw)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise MediaSyncError("file URL 不得包含 userinfo、query 或 fragment")
        if parsed.netloc not in {"", "localhost"}:
            raise MediaSyncError("拒绝非 localhost 的 file URL")
        raw = unquote(parsed.path)
    if any(ord(character) < 32 for character in raw):
        raise MediaSyncError("本地 locator 含控制字符")
    source = Path(raw)
    if not source.is_absolute():
        raise MediaSyncError("拒绝相对 local locator")
    if sys.platform == "darwin" and len(source.parts) > 1:
        # macOS exposes these fixed system aliases as root-level symlinks.
        # Canonicalize only the OS-owned aliases; symlinks anywhere below the
        # canonical root remain forbidden.
        alias = source.parts[1]
        if alias in {"var", "tmp", "etc"}:
            source = Path("/private") / alias / Path(*source.parts[2:])
    current = Path(source.anchor)
    for component in source.parts[1:-1]:
        current = current / component
        try:
            if current.is_symlink():
                raise MediaSyncError("本地 locator 的父目录包含符号链接")
        except OSError as error:
            raise MediaSyncError("无法安全检查本地 locator 父目录") from error
    pattern = _sequence_pattern(source.name)
    if pattern:
        parent = source.parent
        if parent.is_symlink() or not parent.is_dir():
            raise MediaSyncError("本地序列父目录不存在或是符号链接")
        regex, _ = pattern
        members: List[Dict[str, Any]] = []
        scanned = 0
        sequence_bytes = 0
        with os.scandir(parent) as entries:
            for directory_entry in entries:
                scanned += 1
                if scanned > MAX_SEQUENCE_SCAN_ENTRIES:
                    raise MediaSyncError("本地序列目录扫描条目超过安全上限")
                match = regex.fullmatch(directory_entry.name)
                if not match:
                    continue
                candidate = parent / directory_entry.name
                candidate_stat = directory_entry.stat(follow_symlinks=False)
                if directory_entry.is_symlink() or not stat.S_ISREG(candidate_stat.st_mode):
                    raise MediaSyncError("本地序列匹配到符号链接或非普通文件")
                sequence_bytes += int(candidate_stat.st_size)
                if len(members) >= MAX_SEQUENCE_FILES:
                    raise MediaSyncError("本地序列帧数超过安全上限")
                if sequence_bytes > MAX_SEQUENCE_BYTES:
                    raise MediaSyncError("本地序列总大小超过安全上限")
                members.append(
                    {
                        "source": candidate,
                        "name": _safe_component(candidate.name, "frame"),
                        "frame": int(match.group("frame")),
                        "size": int(candidate_stat.st_size),
                        "source_identity": (
                            candidate_stat.st_dev,
                            candidate_stat.st_ino,
                            candidate_stat.st_size,
                            candidate_stat.st_mode,
                            candidate_stat.st_mtime_ns,
                            candidate_stat.st_ctime_ns,
                        ),
                    }
                )
        if not members:
            raise MediaSyncError("本地序列没有匹配到任何帧")
        members.sort(key=lambda item: (item["frame"], item["name"]))
        return members
    try:
        source_stat = source.lstat()
    except FileNotFoundError as error:
        raise MediaSyncError("本地媒体源不存在") from error
    if source.is_symlink() or not stat.S_ISREG(source_stat.st_mode):
        raise MediaSyncError("本地 locator 不是普通文件或是符号链接/目录")
    return [
        {
            "source": source,
            "name": _safe_component(source.name, "media"),
            "frame": None,
            "size": int(source_stat.st_size),
            "source_identity": (
                source_stat.st_dev,
                source_stat.st_ino,
                source_stat.st_size,
                source_stat.st_mode,
                source_stat.st_mtime_ns,
                source_stat.st_ctime_ns,
            ),
        }
    ]


def _item_destination(item: Dict[str, Any], name: str, frame: Optional[int] = None) -> str:
    source = item["source"]
    root = _media_relative_root(str(source["type"]), int(source["id"]), str(item["field"]))
    if frame is not None:
        root = root / _frame_bucket(frame)
    return (root / _safe_component(name, "media")).as_posix()


def _prepare_local_items(plan: List[Dict[str, Any]]) -> None:
    for item in plan:
        if item.get("status") != "pending" or item.get("kind") != "external_local":
            continue
        try:
            members = _expand_local_files(item["_locator"])
            for member in members:
                member["relative"] = _item_destination(
                    item, member["name"], member.get("frame")
                )
            item["_local_files"] = members
            item["_expected_size"] = sum(int(member["size"]) for member in members)
            item["source_fingerprint"] = _canonical_json_hash(
                [
                    {
                        "relative": member["relative"],
                        "frame": member.get("frame"),
                        "size": member["size"],
                        "identity": list(member["source_identity"]),
                    }
                    for member in members
                ]
            )
        except Exception as error:
            item.update(
                {
                    "status": "failed",
                    "owner": "none",
                    "acquisition": "failed",
                    "_transfer": None,
                    "_planning_error": safe_error(error, [_local_locator_path(item["_locator"]) or ""])[
                        "message"
                    ],
                    "_error_code": _error_code(error),
                }
            )


def _public_item(item: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "source",
        "source_key",
        "field",
        "kind",
        "state",
        "status",
        "owner",
        "acquisition",
        "files",
        "locator_sha256",
        "materialized_locator_sha256",
        "temporal_fidelity",
        "source_fingerprint",
    }
    return {key: json_value(value) for key, value in item.items() if key in allowed}


def _new_counters() -> Dict[str, int]:
    counters: Dict[str, int] = {}
    for name in TRANSFER_COUNTERS:
        counters[name] = 0
        counters[name + "_bytes"] = 0
    return counters


def _validate_public_https_url(url: str) -> List[str]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise MediaSyncError("隔离下载仅允许 HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise MediaSyncError("HTTPS URL 不得包含 userinfo")
    if parsed.fragment:
        raise MediaSyncError("HTTPS URL 不得包含 fragment")
    host = parsed.hostname.rstrip(".")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise MediaSyncError("HTTPS hostname DNS 解析失败") from error
    if not addresses:
        raise MediaSyncError("HTTPS hostname 没有可用地址")
    public_ips: List[str] = []
    for address in addresses:
        raw_ip = address[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as error:
            raise MediaSyncError("HTTPS DNS 返回了无效 IP") from error
        if not ip.is_global:
            raise MediaSyncError("HTTPS hostname 解析到非公网 IP，隔离下载拒绝访问")
        normalized = str(ip)
        if normalized not in public_ips:
            public_ips.append(normalized)
    return public_ips


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection whose socket uses the IP validated immediately before."""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self.pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self.pinned_ip, self.port), self.timeout, self.source_address
        )
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _open_pinned_https(url: str) -> Tuple[http.client.HTTPSConnection, Any, str]:
    current = url
    for redirect_count in range(6):
        parsed = urlsplit(current)
        public_ips = _validate_public_https_url(current)
        request_target = parsed.path or "/"
        if parsed.query:
            request_target += "?" + parsed.query
        last_error: Optional[BaseException] = None
        for pinned_ip in public_ips:
            connection = _PinnedHTTPSConnection(
                str(parsed.hostname), parsed.port or 443, pinned_ip, 60.0
            )
            try:
                connection.request(
                    "GET",
                    request_target,
                    headers={"User-Agent": "ews-sg-media-sync/1.0", "Accept": "*/*"},
                )
                response = connection.getresponse()
                break
            except Exception as error:
                last_error = error
                connection.close()
        else:
            if last_error is not None:
                raise last_error
            raise MediaSyncError("HTTPS hostname 没有可连接的公网地址")
        if response.status in {301, 302, 303, 307, 308}:
            location = response.getheader("Location")
            response.close()
            connection.close()
            if not location:
                raise MediaSyncError("HTTPS redirect 缺少 Location")
            if redirect_count >= 5:
                raise MediaSyncError("HTTPS redirect 次数超过上限")
            current = urljoin(current, location)
            continue
        if response.status < 200 or response.status >= 300:
            error = urllib.error.HTTPError(
                current,
                int(response.status),
                str(response.reason),
                response.headers,
                None,
            )
            response.close()
            connection.close()
            raise error
        return connection, response, current
    raise MediaSyncError("HTTPS redirect 次数超过上限")


def _open_private_part(target: Path) -> Tuple[Path, int]:
    ensure_private_directory(target.parent)
    part = target.with_name(target.name + ".part")
    if part.exists() or part.is_symlink():
        if part.is_symlink() or not part.is_file():
            raise MediaSyncError("目标 .part 不是安全普通文件")
        part.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return part, os.open(str(part), flags, 0o600)


def _isolated_https_download(url: str, target: Path, expected_size: Optional[int]) -> int:
    """Download without auth, cookies, proxies, environment netrc or URL userinfo."""
    ensure_private_directory(target.parent)
    _require_disk_capacity(target.parent, int(expected_size or 0))
    part, descriptor = _open_private_part(target)
    written = 0
    next_space_check = 64 * 1024 * 1024
    connection: Optional[http.client.HTTPSConnection] = None
    response: Any = None
    try:
        connection, response, _ = _open_pinned_https(url)
        try:
            content_length = response.headers.get("Content-Length")
            if expected_size is not None and content_length:
                try:
                    if int(content_length) != int(expected_size):
                        raise MediaSyncError("HTTPS Content-Length 与快照 locator size 不匹配")
                except ValueError as error:
                    raise MediaSyncError("HTTPS Content-Length 无效") from error
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    count = os.write(descriptor, view)
                    written += count
                    if written > MAX_SINGLE_MEDIA_BYTES:
                        raise MediaSyncError("单个媒体超过安全大小上限")
                    if written >= next_space_check:
                        _require_disk_capacity(target.parent, 0)
                        next_space_check += 64 * 1024 * 1024
                    view = view[count:]
        finally:
            response.close()
            connection.close()
        os.fsync(descriptor)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    if written <= 0:
        part.unlink(missing_ok=True)
        raise MediaSyncError("HTTPS 下载结果为空")
    if expected_size is not None and written != int(expected_size):
        part.unlink(missing_ok=True)
        raise MediaSyncError("HTTPS 下载大小与快照 locator 不匹配")
    reject_unexpected_html(part)
    os.chmod(part, 0o600)
    os.replace(part, target)
    os.chmod(target, 0o600)
    fsync_directory(target.parent)
    return written


def _structured_for_sg(item: Dict[str, Any], site_origin: str) -> bool:
    value = item.get("_locator")
    if item["source"]["type"] == "Attachment" and item["field"] == "this_file":
        return isinstance(value, dict) and value.get("link_type") == "upload"
    if not isinstance(value, dict):
        return False
    url = _locator_url(value)
    if value.get("link_type") == "upload" and not url:
        return True
    if not url:
        return False
    try:
        return _normalized_origin(url) == site_origin
    except Exception:
        return False


def _sg_download_once(client: Any, item: Dict[str, Any], target: Path) -> int:
    source = item["source"]
    expected_size = item.get("_expected_size")
    ensure_private_directory(target.parent)
    _require_disk_capacity(target.parent, int(expected_size or 0))
    part, descriptor = _open_private_part(target)
    os.close(descriptor)
    try:
        if source["type"] == "Attachment" and item["field"] == "this_file":
            client.download_attachment(int(source["id"]), str(part))
        else:
            client.download_attachment(item["_locator"], file_path=str(part))
        if part.is_symlink() or not part.is_file() or part.stat().st_size <= 0:
            raise MediaSyncError("ShotGrid 下载结果为空或无效")
        size = part.stat().st_size
        if size > MAX_SINGLE_MEDIA_BYTES:
            raise MediaSyncError("单个媒体超过安全大小上限")
        if expected_size is not None and size != int(expected_size):
            raise MediaSyncError("ShotGrid 下载大小与快照 locator 不匹配")
        reject_unexpected_html(part)
        with part.open("rb") as stream:
            os.fsync(stream.fileno())
        os.chmod(part, 0o600)
        os.replace(part, target)
        os.chmod(target, 0o600)
        fsync_directory(target.parent)
        return size
    except Exception:
        part.unlink(missing_ok=True)
        raise


def _refetch_locator(client: Any, item: Dict[str, Any]) -> Optional[Any]:
    source = item["source"]
    if item.get("state") == "retired":
        try:
            rows = client.find(
                str(source["type"]),
                [["id", "is", int(source["id"])]],
                [str(item["field"])],
                limit=1,
                page=1,
                retired_only=True,
                include_archived_projects=True,
            )
        except TypeError:
            try:
                rows = client.find(
                    str(source["type"]),
                    [["id", "is", int(source["id"])]],
                    [str(item["field"])],
                    limit=1,
                    retired_only=True,
                )
            except Exception:
                return None
        except Exception:
            return None
        row = rows[0] if isinstance(rows, list) and rows else None
        return row.get(str(item["field"])) if isinstance(row, dict) else None
    try:
        row = client.find_one(
            str(source["type"]),
            [["id", "is", int(source["id"])]],
            [str(item["field"])],
            include_archived_projects=True,
        )
    except TypeError:
        try:
            row = client.find_one(
                str(source["type"]),
                [["id", "is", int(source["id"])]],
                [str(item["field"])],
            )
        except Exception:
            return None
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    return row.get(str(item["field"]))


def _download_item(
    item: Dict[str, Any],
    supplement: Path,
    client_getter: Callable[[], Any],
    site_origin: str,
    retry_event: Optional[Callable[[str, Dict[str, Any], str], None]] = None,
) -> Dict[str, Any]:
    if _is_transient_media_locator(item.get("_locator")):
        client = client_getter()
        refreshed = _refetch_locator(client, item)
        if not refreshed or _is_transient_media_locator(refreshed):
            error = TransientMediaPending("ShotGrid 媒体仍处于 transient processing 状态")
            if retry_event:
                retry_event("final_failed", item, "TRANSIENT_MEDIA_PENDING")
            raise error
        item["_locator"] = refreshed
        item["_expected_size"] = _locator_expected_size(item.get("_record") or {}, refreshed)
        item["temporal_fidelity"] = "current_refetch"
        item["materialized_locator_sha256"] = _canonical_json_hash(refreshed)
    name = _safe_media_name(item["_locator"], item["field"], int(item["source"]["id"]))
    relative = _item_destination(item, name)
    target = _safe_relative(supplement, relative)
    _remove_replaceable_target(target)
    value = item["_locator"]
    last_error: Optional[BaseException] = None
    retries_used = 0
    for attempt in range(4):
        try:
            if _structured_for_sg(item, site_origin):
                size = _sg_download_once(client_getter(), item, target)
            else:
                url = _locator_url(item["_locator"])
                if not url:
                    raise MediaSyncError("required 媒体没有可用 HTTPS locator")
                size = _isolated_https_download(url, target, item.get("_expected_size"))
            digest = sha256_file(target)
            if retries_used and retry_event:
                retry_event("retry_complete", item, "OK")
            return {
                "files": [{"path": relative, "size": size, "sha256": digest}],
                "size": size,
                "retry_count": retries_used,
            }
        except Exception as error:
            last_error = error
            target.unlink(missing_ok=True)
            if attempt >= 3 or not _retryable_transfer_error(error):
                break
            retries_used += 1
            if retry_event:
                retry_event("retry_scheduled", item, _error_code(error))
            delay = min(8.0, 0.25 * (2**attempt))
            headers = getattr(error, "headers", None)
            retry_after = headers.get("Retry-After") if headers is not None else None
            if retry_after is not None:
                try:
                    delay = min(60.0, max(delay, float(str(retry_after).strip())))
                except ValueError:
                    pass
            time.sleep(delay + random.uniform(0.0, delay * 0.25))

    # Signed media URLs can expire.  Query exactly once; changed locators are
    # allowed as current media but must never be represented as snapshot-exact.
    client = client_getter()
    refreshed = _refetch_locator(client, item)
    if refreshed:
        changed = _canonical_json_hash(refreshed) != item["locator_sha256"]
        original = item["_locator"]
        original_size = item.get("_expected_size")
        item["_locator"] = refreshed
        item["_expected_size"] = _locator_expected_size(item.get("_record") or {}, refreshed)
        try:
            if _structured_for_sg(item, site_origin):
                size = _sg_download_once(client, item, target)
            else:
                url = _locator_url(refreshed)
                if not url:
                    raise MediaSyncError("重新查询后的媒体 locator 不可下载")
                size = _isolated_https_download(url, target, item.get("_expected_size"))
            digest = sha256_file(target)
            if changed:
                item["temporal_fidelity"] = "current_refetch"
                item["materialized_locator_sha256"] = _canonical_json_hash(refreshed)
            if retries_used and retry_event:
                retry_event("retry_complete", item, "OK")
            return {
                "files": [{"path": relative, "size": size, "sha256": digest}],
                "size": size,
                "retry_count": retries_used,
            }
        except Exception as error:
            last_error = error
            target.unlink(missing_ok=True)
        finally:
            if not changed:
                item["_locator"] = original
                item["_expected_size"] = original_size
    if last_error is None:
        raise MediaSyncError("媒体下载失败")
    if retry_event:
        retry_event("final_failed", item, _error_code(last_error))
    raise last_error


def _retryable_transfer_error(error: BaseException) -> bool:
    code = getattr(error, "code", None) or getattr(error, "errcode", None)
    if code in {408, 425, 429, 500, 502, 503, 504}:
        return True
    name = type(error).__name__.lower()
    message = str(error).lower()
    return (
        name in {"timeout", "timeouterror", "connectionerror", "protocolerror", "urlerror"}
        or any(
            marker in message
            for marker in (
                "timed out",
                "timeout",
                "connection reset",
                "connection aborted",
                "remote end closed",
                "temporarily unavailable",
                "too many requests",
                "http 408",
                "http 429",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
            )
        )
    )


def _safe_existing_payload(path: Path, expected_size: Optional[int]) -> Optional[Dict[str, Any]]:
    try:
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            return None
        if expected_size is not None and info.st_size != int(expected_size):
            return None
        reject_unexpected_html(path)
        return {"size": int(info.st_size), "sha256": sha256_file(path)}
    except Exception:
        return None


def _interrupted_media_map(
    interrupted: Optional[Path], plan: List[Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    if not interrupted:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    by_key = {
        item["source_key"]: item
        for item in plan
        if item.get("status") == "pending"
        and not _is_transient_media_locator(item.get("_locator"))
    }
    legacy_locator_hashes: Optional[Dict[str, str]] = None
    try:
        interrupted_manifest = _load_json(interrupted / "manifest.json")
        if int(interrupted_manifest.get("schema_version", 0) or 0) == 1:
            legacy_locator_hashes = {}
            wanted_by_entity: Dict[str, Dict[int, List[Dict[str, Any]]]] = {}
            for item in by_key.values():
                source = item["source"]
                wanted_by_entity.setdefault(str(source["type"]), {}).setdefault(
                    int(source["id"]), []
                ).append(item)
            for entity, wanted_ids in wanted_by_entity.items():
                legacy_entity_path = interrupted / "entities" / f"{entity}.jsonl"
                if not legacy_entity_path.is_file():
                    continue
                for _, record in _iter_jsonl(legacy_entity_path):
                    try:
                        source_id = int(record["id"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    for item in wanted_ids.get(source_id, []):
                        field = str(item["field"])
                        if field in record:
                            legacy_locator_hashes[item["source_key"]] = (
                                _canonical_json_hash(record.get(field))
                            )
    except Exception:
        # A schema-v1 source without parseable locator evidence is never reused.
        legacy_locator_hashes = {}

    def locator_matches(item: Dict[str, Any]) -> bool:
        return legacy_locator_hashes is None or legacy_locator_hashes.get(
            item["source_key"]
        ) == item.get("locator_sha256")

    # Prefer legacy indexes when present.  They safely map both the original
    # flat layout and newer bucketed backup layouts without guessing names.
    attachment_index = interrupted / "attachments/index.json"
    if attachment_index.is_file():
        try:
            rows = _load_json(attachment_index)
            for row in rows if isinstance(rows, list) else []:
                key = _source_key("Attachment", int(row["attachment_id"]), "this_file")
                item = by_key.get(key)
                if not item or not locator_matches(item):
                    continue
                declared_size = int(row["size"])
                declared_hash = str(row["sha256"])
                if not re.fullmatch(r"[0-9a-f]{64}", declared_hash):
                    continue
                if item.get("_expected_size") is not None and declared_size != int(
                    item["_expected_size"]
                ):
                    continue
                candidate = _safe_relative(interrupted, "attachments/" + str(row["file"]))
                validated = _safe_existing_payload(candidate, declared_size)
                if validated and validated["sha256"] == declared_hash:
                    result[key] = {"source_path": candidate, **validated}
        except Exception:
            pass
    media_index = interrupted / "media/index.json"
    if media_index.is_file():
        try:
            rows = _load_json(media_index)
            for row in rows if isinstance(rows, list) else []:
                source = row.get("source") or {}
                key = _source_key(str(source["type"]), int(source["id"]), str(row["field"]))
                item = by_key.get(key)
                if not item or not locator_matches(item):
                    continue
                declared_size = int(row["size"])
                declared_hash = str(row["sha256"])
                if not re.fullmatch(r"[0-9a-f]{64}", declared_hash):
                    continue
                if item.get("_expected_size") is not None and declared_size != int(
                    item["_expected_size"]
                ):
                    continue
                candidate = _safe_relative(interrupted, str(row["file"]))
                validated = _safe_existing_payload(candidate, declared_size)
                if validated and validated["sha256"] == declared_hash:
                    result[key] = {"source_path": candidate, **validated}
        except Exception:
            pass
    return result


def _supplement_lineage_matches(
    manifest: Dict[str, Any],
    base: Path,
    base_hashes: Dict[str, str],
    copy_external: bool,
) -> bool:
    lineage = manifest.get("lineage") or {}
    policy = manifest.get("policy") or {}
    return (
        lineage.get("base_snapshot_id") == base.name
        and all(lineage.get(key) == value for key, value in base_hashes.items())
        and bool(policy.get("copy_external", True)) == bool(copy_external)
    )


def _load_index(path: Path) -> List[Dict[str, Any]]:
    value = _load_json(path)
    if not isinstance(value, list):
        raise MediaSyncError("media/index.json 不是 array")
    return [item for item in value if isinstance(item, dict)]


def _entry_files_valid(entry: Dict[str, Any], owner_root: Path) -> bool:
    files = entry.get("files") or []
    if entry.get("status") == "complete" and not files:
        return False
    for file_info in files:
        try:
            target = _safe_relative(owner_root, str(file_info["path"]))
            if not _validated_file(target, file_info.get("size"), file_info.get("sha256")):
                return False
        except (KeyError, MediaSyncError):
            return False
    return True


def _complete_supplement_reusable(
    supplement: Path,
    plan: List[Dict[str, Any]],
    base: Path,
    base_hashes: Dict[str, str],
    copy_external: bool,
) -> bool:
    verification = verify_media_supplement(supplement)
    if not verification.get("ok"):
        return False
    manifest = _load_json(supplement / "manifest.json")
    if not _supplement_lineage_matches(manifest, base, base_hashes, copy_external):
        return False
    index = _load_index(supplement / "media/index.json")
    prior = {str(entry.get("source_key")): entry for entry in index}
    if set(prior) != {str(item["source_key"]) for item in plan}:
        return False
    for item in plan:
        entry = prior.get(item["source_key"])
        if not entry or entry.get("locator_sha256") != item.get("locator_sha256"):
            return False
        if (
            _is_transient_media_locator(item.get("_locator"))
            and entry.get("temporal_fidelity") != "current_refetch"
        ):
            return False
        if item.get("_required") and entry.get("status") != "complete":
            return False
        if not item.get("_required") and entry.get("status") not in {
            "skipped",
            "excluded_by_policy",
            "complete",
        }:
            return False
    return True


def _latest_resumable_incomplete(
    root: Path,
    base: Path,
    base_hashes: Dict[str, str],
    copy_external: bool,
) -> Optional[Path]:
    if not root.is_dir():
        return None
    candidates = sorted(
        [path for path in root.iterdir() if path.is_dir() and path.name.endswith(".incomplete")],
        key=lambda item: item.name,
        reverse=True,
    )
    for path in candidates:
        manifest_path = path / "manifest.json"
        index_path = path / "media/index.json"
        if not manifest_path.is_file() or not index_path.is_file():
            continue
        try:
            manifest = _load_json(manifest_path)
            if _supplement_lineage_matches(manifest, base, base_hashes, copy_external):
                return path
        except Exception:
            continue
    return None


def _apply_resume(
    plan: List[Dict[str, Any]],
    supplement: Path,
    counters: Dict[str, int],
) -> None:
    index_path = supplement / "media/index.json"
    if not index_path.is_file():
        return
    try:
        entries = _load_index(index_path)
    except Exception:
        return
    prior = {str(entry.get("source_key")): entry for entry in entries}
    checkpoint_path = supplement / "logs/checkpoints.jsonl"
    if checkpoint_path.is_file() and not checkpoint_path.is_symlink():
        try:
            for _, checkpoint in _iter_jsonl(checkpoint_path):
                entry = checkpoint.get("entry")
                if not isinstance(entry, dict):
                    continue
                source_key = str(entry.get("source_key") or "")
                if source_key:
                    prior[source_key] = entry
        except (OSError, ValueError, json.JSONDecodeError, MediaSyncError):
            # The full index remains the durable fallback if an interrupted
            # process left an unreadable journal tail.
            pass
    for item in plan:
        if item.get("status") != "pending":
            continue
        entry = prior.get(item["source_key"])
        if not entry or entry.get("locator_sha256") != item.get("locator_sha256"):
            continue
        if (
            item.get("kind") == "external_local"
            and entry.get("source_fingerprint") != item.get("source_fingerprint")
        ):
            continue
        if (
            _is_transient_media_locator(item.get("_locator"))
            and entry.get("temporal_fidelity") != "current_refetch"
        ):
            continue
        if entry.get("owner") != "supplement":
            continue
        valid_files: List[Dict[str, Any]] = []
        for file_info in entry.get("files") or []:
            try:
                target = _safe_relative(supplement, str(file_info["path"]))
                if _validated_file(target, file_info.get("size"), file_info.get("sha256")):
                    valid_files.append(dict(file_info))
            except (KeyError, MediaSyncError):
                continue
        item["_resume_files"] = {str(row["path"]): row for row in valid_files}
        if (
            valid_files
            and len(valid_files) == len(entry.get("files") or [])
            and entry.get("status") == "complete"
        ):
            item.update(
                {
                    "status": "complete",
                    "owner": "supplement",
                    "acquisition": "resumed",
                    "files": valid_files,
                    "_transfer": None,
                    "temporal_fidelity": entry.get("temporal_fidelity", "snapshot_exact"),
                }
            )
            counters["resumed"] += 1
            counters["resumed_bytes"] += sum(int(row["size"]) for row in valid_files)


def _previous_reuse_map(
    previous: Optional[Path], base: Path
) -> Dict[str, Dict[str, Any]]:
    if not previous:
        return {}
    try:
        entries = _load_index(previous / "media/index.json")
    except Exception:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if entry.get("status") != "complete" or entry.get("owner") not in {"supplement", "base"}:
            continue
        root = previous if entry.get("owner") == "supplement" else base
        if not _entry_files_valid(entry, root):
            continue
        result[str(entry.get("source_key"))] = {"entry": entry, "root": root}
    return result


def _copy_reuse_item(
    item: Dict[str, Any],
    source_entry: Dict[str, Any],
    source_root: Path,
    supplement: Path,
    acquisition: str,
) -> Dict[str, Any]:
    output_files: List[Dict[str, Any]] = []
    total = 0
    for ordinal, file_info in enumerate(source_entry.get("files") or []):
        source_path = _safe_relative(source_root, str(file_info["path"]))
        frame_value = file_info.get("frame")
        frame = int(frame_value) if frame_value is not None else None
        relative = _item_destination(item, Path(str(file_info["path"])).name, frame)
        target = _safe_relative(supplement, relative)
        _remove_replaceable_target(target)
        size, _ = _clone_or_copy(source_path, target)
        digest = sha256_file(target)
        if size != int(file_info["size"]) or digest != str(file_info["sha256"]):
            target.unlink(missing_ok=True)
            raise MediaSyncError("复用 supplement 文件校验失败")
        row: Dict[str, Any] = {"path": relative, "size": size, "sha256": digest}
        if frame is not None:
            row["frame"] = frame
        output_files.append(row)
        total += size
    if not output_files:
        raise MediaSyncError("复用 supplement entry 没有文件")
    return {"files": output_files, "size": total, "acquisition": acquisition}


def _copy_interrupted_item(
    item: Dict[str, Any], source_info: Dict[str, Any], supplement: Path
) -> Dict[str, Any]:
    source_path = Path(source_info["source_path"])
    relative = _item_destination(item, source_path.name)
    target = _safe_relative(supplement, relative)
    _remove_replaceable_target(target)
    size, _ = _clone_or_copy(source_path, target)
    digest = sha256_file(target)
    if size != int(source_info["size"]) or digest != str(source_info["sha256"]):
        target.unlink(missing_ok=True)
        raise MediaSyncError("中断媒体复用后的 hash 不匹配")
    return {
        "files": [{"path": relative, "size": size, "sha256": digest}],
        "size": size,
        "acquisition": "reused_interrupted",
    }


def _copy_local_item(item: Dict[str, Any], supplement: Path) -> Dict[str, Any]:
    members = item.get("_local_files") or []
    resume_files = item.get("_resume_files") or {}
    output_files: List[Dict[str, Any]] = []
    copied_bytes = 0
    resumed_bytes = 0
    copied_count = 0
    for member in members:
        relative = str(member["relative"])
        previous = resume_files.get(relative)
        target = _safe_relative(supplement, relative)
        if previous and _validated_file(target, previous.get("size"), previous.get("sha256")):
            row = dict(previous)
            resumed_bytes += int(row["size"])
        else:
            _remove_replaceable_target(target)
            current = Path(member["source"]).lstat()
            current_identity = (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mode,
                current.st_mtime_ns,
                current.st_ctime_ns,
            )
            if current_identity != tuple(member.get("source_identity") or ()):
                raise MediaSyncError("本地媒体源在发现后、复制前发生变化")
            size = _copy_file_atomic(Path(member["source"]), target)
            digest = sha256_file(target)
            row = {"path": relative, "size": size, "sha256": digest}
            copied_bytes += size
            copied_count += 1
        if member.get("frame") is not None:
            row["frame"] = int(member["frame"])
        output_files.append(row)
    if not output_files:
        raise MediaSyncError("本地媒体复制计划为空")
    return {
        "files": output_files,
        "size": copied_bytes + resumed_bytes,
        "copied_bytes": copied_bytes,
        "resumed_bytes": resumed_bytes,
        "copied_count": copied_count,
        "acquisition": "copied" if copied_count else "resumed",
    }


def _coverage(plan: List[Dict[str, Any]]) -> Dict[str, Any]:
    groups: Dict[str, Dict[str, int]] = {
        "hosted": {"total": 0, "complete": 0, "failed": 0},
        "external": {
            "total": 0,
            "complete": 0,
            "failed": 0,
            "excluded_by_policy": 0,
        },
        "ordinary": {"total": 0, "skipped": 0},
        "ambiguous": {
            "total": 0,
            "complete": 0,
            "failed": 0,
            "excluded_by_policy": 0,
        },
    }
    required = 0
    covered = 0
    failed = 0
    total_bytes = 0
    for item in plan:
        kind = str(item.get("kind", "ambiguous"))
        status_name = str(item.get("status", "pending"))
        if kind.startswith("hosted_"):
            group = "hosted"
        elif kind.startswith("external_"):
            group = "external"
        elif kind in {"ordinary_web", "no_payload"} or (
            kind == "ambiguous" and not item.get("_required")
        ):
            group = "ordinary"
        else:
            group = "ambiguous"
        groups[group]["total"] += 1
        if status_name in groups[group]:
            groups[group][status_name] += 1
        if item.get("_required"):
            required += 1
            if status_name == "complete":
                covered += 1
            elif status_name == "failed":
                failed += 1
        for file_info in item.get("files") or []:
            total_bytes += int(file_info.get("size", 0))
    return {
        **groups,
        "items_total": len(plan),
        "required": required,
        "covered": covered,
        "failed": failed,
        "total_bytes": total_bytes,
    }


def _bounded_item_error(item: Dict[str, Any], error: BaseException) -> Dict[str, str]:
    secrets: List[str] = []
    locator = item.get("_locator")
    if isinstance(locator, str):
        secrets.append(locator)
    elif isinstance(locator, dict):
        for value in locator.values():
            if isinstance(value, str):
                secrets.append(value)
    bounded = safe_error(error, secrets)
    bounded["message"] = URL_IN_TEXT.sub("[REDACTED_URL]", bounded["message"])
    return bounded


def _error_code(error: BaseException) -> str:
    if isinstance(error, TransientMediaPending):
        return "TRANSIENT_MEDIA_PENDING"
    code = getattr(error, "code", None) or getattr(error, "errcode", None)
    if isinstance(code, int):
        return f"HTTP_{code}" if 100 <= code <= 599 else f"REMOTE_{code}"
    name = type(error).__name__.upper()
    message = str(error).lower()
    if "timeout" in name or "timed out" in message:
        return "TIMEOUT"
    if "size" in message or "大小" in message:
        return "SIZE_MISMATCH"
    if "html" in message.lower():
        return "UNEXPECTED_HTML"
    if "symlink" in message.lower() or "符号链接" in message:
        return "UNSAFE_SYMLINK"
    if "https" in message.lower() or "locator" in message.lower():
        return "UNSAFE_LOCATOR"
    return "TRANSFER_FAILED"


def _manifest_errors(plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    errors: List[Dict[str, Any]] = []
    for item in plan:
        if item.get("status") != "failed" or not item.get("_required"):
            continue
        errors.append(
            {
                "source": item["source"],
                "field": item["field"],
                "error_code": str(item.get("_error_code") or "TRANSFER_FAILED"),
            }
        )
    return errors


def _build_manifest(
    supplement_id: str,
    started_at: str,
    status_name: str,
    base: Path,
    base_hashes: Dict[str, str],
    site_fingerprint: str,
    copy_external: bool,
    plan: List[Dict[str, Any]],
    counters: Dict[str, int],
    queues: Dict[str, Dict[str, Any]],
    max_workers: int,
) -> Dict[str, Any]:
    coverage = _coverage(plan)
    temporal_modes = sorted(
        {
            str(item.get("temporal_fidelity") or "snapshot_exact")
            for item in plan
            if item.get("status") == "complete"
        }
    )
    current_modes = [mode for mode in temporal_modes if mode != "snapshot_exact"]
    if "current_refetch" in current_modes:
        temporal = "current_refetch"
    elif len(current_modes) == 1:
        temporal = current_modes[0]
    elif current_modes:
        temporal = "mixed_current_sources"
    else:
        temporal = "snapshot_exact"
    hosted_failed = any(
        item.get("_required")
        and item.get("status") == "failed"
        and (
            item.get("kind") == "ambiguous_hosted"
            or str(item.get("kind", "")).startswith("hosted_")
        )
        for item in plan
    )
    external_failed = any(
        item.get("_required")
        and item.get("status") == "failed"
        and (
            item.get("kind") == "ambiguous_external"
            or str(item.get("kind", "")).startswith("external_")
        )
        for item in plan
    )
    hosted_scope = "partial" if hosted_failed else "complete"
    external_scope = (
        "not_requested" if not copy_external else "partial" if external_failed else "complete"
    )
    transfer_stats = {
        "eta_method": "last_100_task_duration_by_transfer_kind",
        "download": queues["download"],
        "copy": queues["copy"],
        "aggregate": {
            "current_bytes_per_second": sum(
                float(queues[name].get("current_bytes_per_second", 0.0))
                for name in ("download", "copy")
            ),
            "peak_bytes_per_second": sum(
                float(queues[name].get("peak_bytes_per_second", 0.0))
                for name in ("download", "copy")
            ),
        },
    }
    return {
        "format": SUPPLEMENT_FORMAT,
        "schema_version": SUPPLEMENT_SCHEMA_VERSION,
        "supplement_id": supplement_id,
        "status": status_name,
        "started_at": started_at,
        "completed_at": _utc_now().isoformat() if status_name in {"complete", "partial"} else None,
        "lineage": {
            "base_snapshot_id": base.name,
            **base_hashes,
            "site_fingerprint": site_fingerprint,
            "snapshot_exact": temporal == "snapshot_exact",
            "temporal_fidelity": temporal,
            "temporal_fidelity_modes": temporal_modes or ["snapshot_exact"],
        },
        "policy": {"copy_external": bool(copy_external)},
        "payload_scope": f"shotgrid_hosted_{hosted_scope}/external_{external_scope}",
        "payload_coverage": {
            "hosted": hosted_scope,
            "external": external_scope,
        },
        "coverage": coverage,
        "transfer": {
            "max_workers": max_workers,
            "eta_method": "last_100_task_duration_by_transfer_kind",
            "counters": dict(counters),
            "queues": queues,
        },
        "transfer_stats": transfer_stats,
        "media_index": "media/index.json",
        "event_log": "logs/events.jsonl",
        "checkpoint_log": "logs/checkpoints.jsonl",
        "errors": _manifest_errors(plan),
    }


def _write_partial_state(
    supplement: Path,
    manifest: Dict[str, Any],
    plan: List[Dict[str, Any]],
) -> None:
    atomic_json(supplement / "media/index.json", [_public_item(item) for item in plan])
    atomic_json(supplement / "logs/errors.json", manifest.get("errors") or [])
    atomic_json(supplement / "manifest.json", manifest)


class _SupplementEvents:
    """Thread-safe, fsynced supplement audit log with monotonic sequence IDs."""

    def __init__(
        self,
        supplement: Path,
        callback: Optional[Callable[[Dict[str, Any]], None]],
    ) -> None:
        self.path = supplement / "logs/events.jsonl"
        ensure_private_directory(self.path.parent)
        if self.path.is_symlink():
            raise MediaSyncError("supplement events.jsonl 不得是符号链接")
        self.callback = callback
        self.lock = threading.Lock()
        self.sequence = 0
        if self.path.is_file():
            for _, row in _iter_jsonl(self.path):
                sequence = row.get("seq")
                if type(sequence) is not int or sequence <= self.sequence:
                    raise MediaSyncError("supplement events.jsonl seq 无效")
                self.sequence = sequence
        else:
            self.path.touch(mode=0o600)
            os.chmod(self.path, 0o600)
            fsync_directory(self.path.parent)

    def emit(self, payload: Dict[str, Any], notify: bool = True) -> Dict[str, Any]:
        with self.lock:
            self.sequence += 1
            event = {
                "seq": self.sequence,
                "at": _utc_now().isoformat(),
                **json_value(payload),
            }
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if notify and self.callback:
                self.callback(dict(event))
        return event


class _SupplementCheckpoints:
    """Append-only, fsynced item checkpoints used to resume large batches."""

    def __init__(self, supplement: Path) -> None:
        self.path = supplement / "logs/checkpoints.jsonl"
        ensure_private_directory(self.path.parent)
        if self.path.is_symlink():
            raise MediaSyncError("supplement checkpoints.jsonl 不得是符号链接")
        self.lock = threading.Lock()
        self.sequence = 0
        if self.path.is_file():
            try:
                for _, row in _iter_jsonl(self.path):
                    sequence = row.get("seq")
                    if type(sequence) is not int or sequence <= self.sequence:
                        raise MediaSyncError("supplement checkpoints.jsonl seq 无效")
                    self.sequence = sequence
            except (OSError, ValueError, json.JSONDecodeError, MediaSyncError):
                # A previous full index is still usable.  Start a fresh journal
                # so a torn final line cannot poison all later checkpoints.
                rotated = self.path.with_name(
                    self.path.name + ".invalid." + _timestamp_id()
                )
                os.replace(self.path, rotated)
                fsync_directory(self.path.parent)
                self.sequence = 0
                self.path.touch(mode=0o600)
                os.chmod(self.path, 0o600)
                fsync_directory(self.path.parent)
        else:
            self.path.touch(mode=0o600)
            os.chmod(self.path, 0o600)
            fsync_directory(self.path.parent)

    def append(self, item: Dict[str, Any]) -> None:
        with self.lock:
            self.sequence += 1
            checkpoint = {
                "seq": self.sequence,
                "at": _utc_now().isoformat(),
                "source_key": str(item.get("source_key") or ""),
                "entry": _public_item(item),
                "error_code": item.get("_error_code"),
            }
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())


def _progress_totals(plan: List[Dict[str, Any]]) -> Tuple[int, int, int, int]:
    required = [item for item in plan if item.get("_required")]
    items_done = sum(item.get("status") in {"complete", "failed"} for item in required)
    bytes_done = sum(
        int(file_info.get("size", 0))
        for item in required
        if item.get("status") == "complete"
        for file_info in item.get("files") or []
    )
    bytes_total = 0
    for item in required:
        actual = sum(int(row.get("size", 0)) for row in item.get("files") or [])
        expected = item.get("_expected_size")
        bytes_total += actual if actual else int(expected or 0)
    return items_done, bytes_done, len(required), max(bytes_total, bytes_done)


def _run_dynamic_queue(
    kind: str,
    tasks: List[Tuple[Dict[str, Any], Callable[[], Dict[str, Any]]]],
    worker_cap: int,
    plan: List[Dict[str, Any]],
    counters: Dict[str, int],
    queues: Dict[str, Dict[str, Any]],
    progress: Optional[Callable[[Dict[str, Any]], None]],
    persist: Callable[[], None],
    checkpoint: Callable[[Dict[str, Any]], None],
    state_lock: threading.RLock,
) -> None:
    queue_stats = queues[kind]
    if not tasks:
        return
    workers = min(worker_cap, 2 if worker_cap > 1 else 1)
    cursor = 0
    prior_rate = 0.0
    processed = 0
    duration_samples = deque(maxlen=100)  # type: ignore[var-annotated]
    rate_windows = deque(maxlen=100)  # type: ignore[var-annotated]

    def timed_call(call: Callable[[], Dict[str, Any]]) -> Tuple[bool, Any, float]:
        task_started = time.monotonic()
        try:
            return True, call(), max(time.monotonic() - task_started, 1e-9)
        except Exception as error:
            return False, error, max(time.monotonic() - task_started, 1e-9)

    def recent_rate(current_bytes: int = 0, current_seconds: float = 0.0) -> float:
        sampled_seconds = sum(float(row[1]) for row in rate_windows) + current_seconds
        if sampled_seconds <= 0:
            return 0.0
        return (
            sum(int(row[0]) for row in rate_windows) + int(current_bytes)
        ) / sampled_seconds

    def aggregate_rate() -> float:
        return sum(
            float(queues[name].get("current_bytes_per_second", 0.0))
            for name in ("download", "copy")
        )

    while cursor < len(tasks):
        window = tasks[cursor : cursor + max(workers * 2, 1)]
        cursor += len(window)
        started = time.monotonic()
        window_bytes = 0
        window_errors = 0
        window_retries = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(timed_call, call): item for item, call in window}
            for future in as_completed(futures):
                item = futures[future]
                succeeded, value, task_duration = future.result()
                processed += 1
                duration_samples.append(task_duration)
                event: Dict[str, Any]
                if succeeded:
                    outcome = value
                    size = int(outcome.get("size", 0))
                    window_retries += int(outcome.get("retry_count", 0))
                    window_bytes += size
                    with state_lock:
                        item.update(
                            {
                                "status": "complete",
                                "owner": "supplement",
                                "acquisition": outcome.get("acquisition")
                                or ("downloaded" if kind == "download" else "copied"),
                                "files": outcome["files"],
                                "_transfer": None,
                            }
                        )
                        acquisition = str(item["acquisition"])
                        if acquisition in counters:
                            counters[acquisition] += 1
                            counters[acquisition + "_bytes"] += size
                        if acquisition == "copied" and outcome.get("resumed_bytes"):
                            resumed_bytes = int(outcome["resumed_bytes"])
                            counters["resumed"] += 1
                            counters["resumed_bytes"] += resumed_bytes
                            counters["copied_bytes"] -= resumed_bytes
                        queue_stats["items_done"] += 1
                        queue_stats["bytes_done"] += size
                        rate = recent_rate(
                            window_bytes, max(time.monotonic() - started, 1e-9)
                        )
                        queue_stats["bytes_per_second"] = rate
                        queue_stats["current_bytes_per_second"] = rate
                        queue_stats["average_recent_bytes_per_second"] = rate
                        queue_stats["peak_bytes_per_second"] = max(
                            float(queue_stats.get("peak_bytes_per_second", 0.0)), rate
                        )
                        queue_stats["workers_final"] = workers
                        queue_stats.update(
                            _estimate_eta(duration_samples, len(tasks) - processed, workers)
                        )
                        items_done, bytes_done, items_total, bytes_total = _progress_totals(plan)
                        eta = _estimate_eta(duration_samples, len(tasks) - processed, workers)
                        event_name = (
                            "media_transfer_reused"
                            if acquisition.startswith("reused_") or acquisition == "resumed"
                            else "media_transfer_complete"
                        )
                        event = {
                            "event": event_name,
                            "kind": kind,
                            "size": size,
                            "items_done": items_done,
                            "bytes_done": bytes_done,
                            "items_total": items_total,
                            "bytes_total": bytes_total,
                            "bytes_per_second": round(rate, 3),
                            "aggregate_bytes_per_second": round(aggregate_rate(), 3),
                            "source": item["source"],
                            "field": item["field"],
                            "acquisition": acquisition,
                            "temporal_fidelity": item.get("temporal_fidelity"),
                            **eta,
                        }
                else:
                    error = value
                    bounded = _bounded_item_error(item, error)
                    window_errors += 1
                    with state_lock:
                        item.update(
                            {
                                "status": "failed",
                                "owner": "none",
                                "acquisition": "failed",
                                "files": [],
                                "_transfer": None,
                                "_error": bounded["message"],
                                "_error_type": bounded["type"],
                                "_error_code": _error_code(error),
                            }
                        )
                        queue_stats["items_done"] += 1
                        queue_stats["errors"] += 1
                        rate = recent_rate(
                            window_bytes, max(time.monotonic() - started, 1e-9)
                        )
                        queue_stats["bytes_per_second"] = rate
                        queue_stats["current_bytes_per_second"] = rate
                        queue_stats["average_recent_bytes_per_second"] = rate
                        queue_stats["peak_bytes_per_second"] = max(
                            float(queue_stats.get("peak_bytes_per_second", 0.0)), rate
                        )
                        queue_stats["workers_final"] = workers
                        queue_stats.update(
                            _estimate_eta(duration_samples, len(tasks) - processed, workers)
                        )
                        items_done, bytes_done, items_total, bytes_total = _progress_totals(plan)
                        eta = _estimate_eta(duration_samples, len(tasks) - processed, workers)
                        event = {
                            "event": "media_transfer_error",
                            "kind": kind,
                            "size": 0,
                            "items_done": items_done,
                            "bytes_done": bytes_done,
                            "items_total": items_total,
                            "bytes_total": bytes_total,
                            "source": item["source"],
                            "field": item["field"],
                            "error_code": _error_code(error),
                            "bytes_per_second": round(rate, 3),
                            "aggregate_bytes_per_second": round(aggregate_rate(), 3),
                            **eta,
                        }
                # Persist this result before announcing completion.  The
                # append-only journal keeps this O(1); the full index is
                # refreshed once per adaptive window below.
                checkpoint(item)
                if progress:
                    progress(event)
        elapsed = max(time.monotonic() - started, 1e-9)
        rate_windows.append((window_bytes, elapsed))
        rate = recent_rate()
        with state_lock:
            queue_stats["windows"].append(
                {
                    "workers": workers,
                    "items": len(window),
                    "bytes": window_bytes,
                    "errors": window_errors,
                    "retries": window_retries,
                    "duration_seconds": round(elapsed, 6),
                    "bytes_per_second": round(rate, 3),
                }
            )
            if window_errors or window_retries:
                workers = max(1, workers // 2)
            elif workers < worker_cap and (prior_rate <= 0 or rate >= prior_rate * 0.8):
                workers = min(worker_cap, workers + max(1, workers // 2))
            elif prior_rate > 0 and rate < prior_rate * 0.6:
                workers = max(1, workers - 1)
            prior_rate = rate
            queue_stats["workers_final"] = workers
            queue_stats.update(_estimate_eta(duration_samples, len(tasks) - processed, workers))
            tuning_event = {
                "event": "media_transfer_tuning",
                "kind": kind,
                "workers": workers,
                "bytes_per_second": round(rate, 3),
                "aggregate_bytes_per_second": round(aggregate_rate(), 3),
                **_estimate_eta(duration_samples, len(tasks) - processed, workers),
            }
        persist()
        if progress:
            progress(tuning_event)
    with state_lock:
        queue_stats["workers_final"] = 0
        queue_stats["current_bytes_per_second"] = 0.0
        queue_stats["bytes_per_second"] = 0.0
        queue_stats["eta_seconds"] = 0.0
        queue_stats["calibrating"] = False
        completed_event = {
            "event": "media_transfer_tuning",
            "kind": kind,
            "workers": 0,
            "bytes_per_second": 0.0,
            "aggregate_bytes_per_second": round(aggregate_rate(), 3),
            "eta_seconds": 0.0,
            "eta_sample_count": len(duration_samples),
            "calibrating": False,
            "queue_complete": True,
        }
    persist()
    if progress:
        progress(completed_event)


def _estimate_eta(
    durations: Sequence[float], remaining_items: int, current_target_workers: int
) -> Dict[str, Any]:
    """Estimate ETA from only the latest 100 completed task durations."""
    samples = [max(float(value), 0.0) for value in list(durations)[-100:]]
    count = len(samples)
    calibrating = count < 10
    eta: Optional[float] = None
    if not calibrating:
        workers = max(1, int(current_target_workers))
        eta = (sum(samples) / count) * max(0, int(remaining_items)) / workers
        eta = round(eta, 3)
    return {
        "eta_seconds": eta,
        "eta_sample_count": count,
        "calibrating": calibrating,
    }


def _deduplicate_supplement_files(supplement: Path, plan: List[Dict[str, Any]]) -> None:
    by_digest: Dict[Tuple[str, int], Path] = {}
    for item in plan:
        if item.get("status") != "complete" or item.get("owner") != "supplement":
            continue
        for file_info in item.get("files") or []:
            target = _safe_relative(supplement, str(file_info["path"]))
            key = (str(file_info["sha256"]), int(file_info["size"]))
            existing = by_digest.get(key)
            if existing is None:
                by_digest[key] = target
                continue
            try:
                if target.samefile(existing):
                    continue
                target.unlink()
                os.link(str(existing), str(target), follow_symlinks=False)
                fsync_directory(target.parent)
            except OSError:
                if not target.exists():
                    _copy_file_atomic(existing, target)


def _queue_template(items_total: int) -> Dict[str, Any]:
    return {
        "items_total": int(items_total),
        "items_done": 0,
        "bytes_done": 0,
        "errors": 0,
        "workers_final": 0,
        "bytes_per_second": 0.0,
        "current_bytes_per_second": 0.0,
        "peak_bytes_per_second": 0.0,
        "average_recent_bytes_per_second": 0.0,
        "eta_seconds": None,
        "eta_sample_count": 0,
        "calibrating": True,
        "windows": [],
    }


def _select_or_recover_base(output_root: Path, sg: Any) -> Tuple[Path, Optional[Path]]:
    base = find_latest_snapshot(output_root)
    interrupted_candidates = _interrupted_candidates(output_root)
    candidate = interrupted_candidates[-1] if interrupted_candidates else None
    candidate_assessment: Optional[Dict[str, Any]] = None
    if base is not None and candidate is not None:
        try:
            base_lineage = (_load_json(base / "manifest.json").get("lineage") or {})
            if base_lineage.get("recovered_from_directory") == candidate.name:
                return base, candidate
        except Exception:
            pass
        candidate_assessment = _assess_interrupted(candidate)
        if candidate_assessment.get("legacy_v1_media_only"):
            legacy_site = str(candidate_assessment.get("legacy_source_site") or "")
            if legacy_site and _normalized_origin(legacy_site) == _normalized_origin(
                _sg_server(sg)
            ):
                return base, candidate
            # Unknown- or wrong-site v1 data is never used as a payload source.
            return base, None
    if candidate and (base is None or candidate.name[: -len(".incomplete")] > base.name):
        assessment = candidate_assessment or _assess_interrupted(candidate)
        if not assessment.get("recoverable"):
            detail = (assessment.get("errors") or [{}])[0].get("message", "证据不完整")
            raise MediaSyncError(
                "最新中断快照无法安全恢复；为避免重拉前误封存，已 fail closed："
                + str(detail)
            )
        recovery_header = assessment.get("recovery_header") or {}
        interrupted_manifest = assessment.get("interrupted_manifest") or {}
        source = (recovery_header.get("source") or interrupted_manifest.get("source") or {})
        if not source.get("site"):
            raise MediaSyncError("中断快照缺少 source.site，拒绝自动恢复")
        if _normalized_origin(str(source["site"])) != _normalized_origin(_sg_server(sg)):
            raise MediaSyncError("中断快照 source.site 与当前 ShotGrid client 不一致")
        base = _seal_recovered_base(output_root, assessment, sg)
        return base, candidate
    if base is None:
        raise MediaSyncError("输出目录没有 sealed snapshot，也没有可恢复的中断实体导出")
    manifest = _load_json(base / "manifest.json")
    lineage = manifest.get("lineage") or {}
    interrupted_name = lineage.get("recovered_from_directory")
    interrupted = None
    if interrupted_name and Path(str(interrupted_name)).name == str(interrupted_name):
        possible = output_root / str(interrupted_name)
        if possible.is_dir() and possible.name.endswith(".incomplete"):
            interrupted = possible
    return base, interrupted


def materialize_latest_media(
    output_root: Path,
    sg: Any,
    client_factory: Optional[Callable[[], Any]] = None,
    max_workers: int = 32,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    *,
    copy_external: bool = False,
) -> Path:
    """Complete all required media for the newest immutable entity snapshot.

    Hosted media is always required.  External local/NAS/PublishedFile payloads
    are required only when ``copy_external`` is true.
    """
    root = Path(output_root).expanduser().resolve()
    if int(max_workers) < 1 or int(max_workers) > 32:
        raise ValueError("max_workers 必须在 1..32")
    ensure_private_directory(root)
    lock_path = root / ".backup.lock"
    try:
        lock_fd, lock_identity = acquire_output_lock(lock_path)
    except RuntimeError as error:
        raise MediaSyncError(str(error)) from error
    try:
        base, interrupted = _select_or_recover_base(root, sg)
        verification = _verify_base_snapshot(base, require_full=False)
        if not verification.get("ok"):
            raise MediaSyncError(
                "base snapshot 校验失败：" + "; ".join(verification.get("errors") or ["unknown"])
            )
        base_hashes = _hash_triplet(base)
        base_manifest = _load_json(base / "manifest.json")
        source = base_manifest.get("source") or {}
        base_site = str(source.get("site") or "")
        current_site = _sg_server(sg)
        if _normalized_origin(base_site) != _normalized_origin(current_site):
            raise MediaSyncError("base source.site 与当前 ShotGrid client server 不一致")
        canonical_fingerprint = _site_fingerprint(base_site)
        declared_fingerprint = str(source.get("site_fingerprint") or "")
        legacy_fingerprint = hashlib.sha256(base_site.lower().encode("utf-8")).hexdigest()
        if declared_fingerprint and declared_fingerprint not in {
            canonical_fingerprint,
            legacy_fingerprint,
        }:
            raise MediaSyncError("base source.site_fingerprint 与 source.site 不一致")
        if _site_fingerprint(current_site) != canonical_fingerprint:
            raise MediaSyncError("当前 ShotGrid client fingerprint 与 base 不一致")

        counters = _new_counters()
        plan = _build_media_plan(base, bool(copy_external))
        _apply_base_reuse(plan, base, counters)
        _prepare_local_items(plan)
        supplements_root = root / "media_supplements" / base.name
        ensure_private_directory(supplements_root)
        previous = _latest_complete_supplement(base)
        if previous:
            previous_manifest = _load_json(previous / "manifest.json")
            if not _supplement_lineage_matches(
                previous_manifest, base, base_hashes, bool(copy_external)
            ):
                previous = None

        if previous and _complete_supplement_reusable(
            previous, plan, base, base_hashes, bool(copy_external)
        ):
            _assert_hash_triplet(base, base_hashes)
            if progress:
                _, _, items_total, bytes_total = _progress_totals(plan)
                progress(
                    {
                        "event": "media_supplement_plan",
                        "items_total": items_total,
                        "bytes_total": bytes_total,
                        "reused_items": counters["reused_base"],
                        "base_snapshot_id": base.name,
                    }
                )
                progress({"event": "media_supplement_complete", "path": str(previous)})
            return previous

        resumable = _latest_resumable_incomplete(
            supplements_root, base, base_hashes, bool(copy_external)
        )
        if resumable:
            incomplete = resumable
            supplement_id = incomplete.name[: -len(".incomplete")]
            prior_manifest = _load_json(incomplete / "manifest.json")
            started_at = str(prior_manifest.get("started_at") or _utc_now().isoformat())
            for stale in ("COMPLETED.json", "checksums.sha256"):
                stale_path = incomplete / stale
                if stale_path.is_symlink():
                    raise MediaSyncError("incomplete supplement 含危险的 seal 符号链接")
                stale_path.unlink(missing_ok=True)
            _apply_resume(plan, incomplete, counters)
        else:
            supplement_id = _timestamp_id()
            incomplete = supplements_root / f"{supplement_id}.incomplete"
            if incomplete.exists():
                raise MediaSyncError("supplement ID 冲突")
            ensure_private_directory(incomplete)
            started_at = _utc_now().isoformat()

        event_sink = _SupplementEvents(incomplete, progress)
        checkpoint_sink = _SupplementCheckpoints(incomplete)
        _, _, items_total, bytes_total = _progress_totals(plan)
        event_sink.emit(
            {
                "event": "media_supplement_plan",
                "items_total": items_total,
                "bytes_total": bytes_total,
                "reused_items": counters["reused_base"] + counters["resumed"],
                "base_snapshot_id": base.name,
                "copy_external": bool(copy_external),
                "hosted_items": sum(
                    str(item.get("kind", "")).startswith("hosted_") for item in plan
                ),
                "external_items": sum(
                    str(item.get("kind", "")).startswith("external_") for item in plan
                ),
            }
        )
        for item in plan:
            if len(item.get("_local_files") or []) > 1:
                event_sink.emit(
                    {
                        "event": "media_sequence_expanded",
                        "source": item["source"],
                        "field": item["field"],
                        "frames": len(item["_local_files"]),
                    }
                )

        # Emit already reused progress after the destination is known.
        for acquisition in ("reused_base", "resumed"):
            count = counters[acquisition]
            if count:
                current_done, current_bytes, total_items, total_bytes = _progress_totals(plan)
                event_sink.emit(
                    {
                        "event": "media_transfer_reused",
                        "kind": "base" if acquisition == "reused_base" else "resume",
                        "size": counters[acquisition + "_bytes"],
                        "items": count,
                        "items_done": current_done,
                        "bytes_done": current_bytes,
                        "items_total": total_items,
                        "bytes_total": total_bytes,
                        "bytes_per_second": 0.0,
                        "aggregate_bytes_per_second": 0.0,
                        "eta_seconds": None,
                        "eta_sample_count": 0,
                        "calibrating": True,
                    }
                )

        interrupted_map = _interrupted_media_map(interrupted, plan)
        previous_map = _previous_reuse_map(previous, base)
        copy_tasks: List[Tuple[Dict[str, Any], Callable[[], Dict[str, Any]]]] = []
        download_tasks: List[Tuple[Dict[str, Any], Callable[[], Dict[str, Any]]]] = []
        thread_clients = threading.local()
        retry_counts = {"retry_scheduled": 0, "retry_complete": 0, "final_failed": 0}
        retry_lock = threading.Lock()

        def retry_notifier(action: str, item: Dict[str, Any], error_code: str) -> None:
            with retry_lock:
                retry_counts[action] += 1
                snapshot = dict(retry_counts)
            event_sink.emit(
                {
                    "event": "media_transfer_retry",
                    "kind": "download",
                    "retry_state": action,
                    **snapshot,
                    "source": item["source"],
                    "field": item["field"],
                    "error_code": error_code,
                }
            )

        def client_getter() -> Any:
            if client_factory is None:
                return sg
            if not hasattr(thread_clients, "client"):
                thread_clients.client = client_factory()
            return thread_clients.client

        for item in plan:
            if item.get("status") != "pending":
                continue
            interrupted_source = interrupted_map.get(item["source_key"])
            if interrupted_source:
                copy_tasks.append(
                    (
                        item,
                        lambda item=item, info=interrupted_source: _copy_interrupted_item(
                            item, info, incomplete
                        ),
                    )
                )
                continue
            previous_source = previous_map.get(item["source_key"])
            if (
                previous_source
                and previous_source["entry"].get("locator_sha256") == item.get("locator_sha256")
                and (
                    not _is_transient_media_locator(item.get("_locator"))
                    or previous_source["entry"].get("temporal_fidelity")
                    == "current_refetch"
                )
            ):
                copy_tasks.append(
                    (
                        item,
                        lambda item=item, source_info=previous_source: _copy_reuse_item(
                            item,
                            source_info["entry"],
                            source_info["root"],
                            incomplete,
                            "reused_supplement",
                        ),
                    )
                )
                continue
            if item.get("kind") == "external_local":
                copy_tasks.append(
                    (item, lambda item=item: _copy_local_item(item, incomplete))
                )
            elif item.get("_transfer") == "download":
                download_tasks.append(
                    (
                        item,
                        lambda item=item: _download_item(
                            item,
                            incomplete,
                            client_getter,
                            _normalized_origin(base_site),
                            retry_notifier,
                        ),
                    )
                )
            else:
                item.update(
                    {
                        "status": "failed",
                        "owner": "none",
                        "acquisition": "failed",
                        "_planning_error": item.get("_planning_error")
                        or "required 媒体没有安全 transfer route",
                        "_error_code": item.get("_error_code") or "UNSAFE_LOCATOR",
                    }
                )

        pending_items = [item for item, _ in copy_tasks + download_tasks]
        known_pending_bytes = sum(
            max(0, int(item.get("_expected_size") or 0)) for item in pending_items
        )
        _require_supplement_capacity(incomplete, known_pending_bytes)

        queues = {
            "download": _queue_template(len(download_tasks)),
            "copy": _queue_template(len(copy_tasks)),
        }
        queues["download"].update(retry_counts)
        state_lock = threading.RLock()

        def persist_running() -> None:
            with state_lock:
                queues["download"].update(retry_counts)
                manifest = _build_manifest(
                    supplement_id,
                    started_at,
                    "running",
                    base,
                    base_hashes,
                    canonical_fingerprint,
                    bool(copy_external),
                    plan,
                    counters,
                    queues,
                    int(max_workers),
                )
                _write_partial_state(incomplete, manifest, plan)

        persist_running()
        download_cap = int(max_workers) if client_factory is not None else 1
        with ThreadPoolExecutor(max_workers=2) as pool_executor:
            pool_futures = [
                pool_executor.submit(
                    _run_dynamic_queue,
                    "copy",
                    copy_tasks,
                    int(max_workers),
                    plan,
                    counters,
                    queues,
                    event_sink.emit,
                    persist_running,
                    checkpoint_sink.append,
                    state_lock,
                ),
                pool_executor.submit(
                    _run_dynamic_queue,
                    "download",
                    download_tasks,
                    download_cap,
                    plan,
                    counters,
                    queues,
                    event_sink.emit,
                    persist_running,
                    checkpoint_sink.append,
                    state_lock,
                ),
            ]
            for pool_future in as_completed(pool_futures):
                pool_future.result()
        _deduplicate_supplement_files(incomplete, plan)
        queues["download"].update(retry_counts)
        _assert_hash_triplet(base, base_hashes)
        coverage = _coverage(plan)
        succeeded = coverage["required"] == coverage["covered"] and coverage["failed"] == 0
        status_name = "complete" if succeeded else "partial"
        manifest = _build_manifest(
            supplement_id,
            started_at,
            status_name,
            base,
            base_hashes,
            canonical_fingerprint,
            bool(copy_external),
            plan,
            counters,
            queues,
            int(max_workers),
        )
        _write_partial_state(incomplete, manifest, plan)
        if not succeeded:
            event_sink.emit(
                {
                    "event": "media_supplement_failed",
                    "supplement_id": supplement_id,
                    "required": coverage["required"],
                    "covered": coverage["covered"],
                    "failed": coverage["failed"],
                }
            )
            _write_partial_state(incomplete, manifest, plan)
            raise MediaSyncError(
                f"媒体补全存在 {coverage['failed']} 个 required 失败；保留 {incomplete} 供续传"
            )

        # A successful supplement must have no untracked .part remnants.
        for part in incomplete.rglob("*.part"):
            if part.is_symlink() or not part.is_file():
                raise MediaSyncError("supplement 含不安全的 .part 残留")
            part.unlink()
        event_sink.emit(
            {
                "event": "media_supplement_complete",
                "supplement_id": supplement_id,
                "required": coverage["required"],
                "covered": coverage["covered"],
                "bytes": coverage["total_bytes"],
            },
            notify=False,
        )
        payloads = sorted(
            path
            for path in incomplete.rglob("*")
            if path.is_file() and path.name not in {"manifest.json", "checksums.sha256", "COMPLETED.json"}
        )
        checksum_lines = [
            f"{sha256_file(path)}  {path.relative_to(incomplete).as_posix()}" for path in payloads
        ]
        atomic_text(incomplete / "checksums.sha256", "\n".join(checksum_lines) + "\n")
        manifest["integrity"] = {
            "algorithm": "sha256",
            "checksums_file": "checksums.sha256",
            "files_hashed": len(checksum_lines),
            "manifest_excluded_to_avoid_self_reference": True,
        }
        atomic_json(incomplete / "manifest.json", manifest)
        atomic_json(
            incomplete / "COMPLETED.json",
            {
                "format": "shotgrid_media_supplement_completion_receipt",
                "supplement_id": supplement_id,
                "base_snapshot_id": base.name,
                "completed_at": manifest["completed_at"],
                "manifest_sha256": sha256_file(incomplete / "manifest.json"),
                "checksums_sha256": sha256_file(incomplete / "checksums.sha256"),
            },
        )
        final = supplements_root / supplement_id
        os.replace(incomplete, final)
        fsync_directory(supplements_root)
        atomic_text(supplements_root / "latest.txt", supplement_id + "\n")
        if progress:
            progress({"event": "media_supplement_complete", "path": str(final)})
        return final
    finally:
        release_output_lock(lock_path, lock_fd, lock_identity)


def _verify_supplement_file_shape(
    relative: str, source: Dict[str, Any], field: str, frame: Optional[int]
) -> Optional[str]:
    parts = Path(relative).parts
    source_id = int(source["id"])
    expected_prefix = (
        "media",
        _safe_component(source["type"], "Entity"),
        _id_bucket(source_id),
        str(source_id),
        _safe_component(field, "field"),
    )
    if len(parts) not in {6, 7} or tuple(parts[:5]) != expected_prefix:
        return "supplement media 路径未按 Entity/ID bucket/source/field 分片"
    if frame is None:
        if len(parts) != 6:
            return "非序列媒体含意外 frame bucket"
    else:
        if len(parts) != 7 or parts[5] != _frame_bucket(int(frame)):
            return "序列媒体 frame bucket 与 index.frame 不一致"
    if parts[-1] != _safe_component(parts[-1], "media"):
        return "supplement media 文件名不安全"
    return None


def verify_media_supplement(path: Path) -> Dict[str, Any]:
    """Verify a sealed media supplement and its immutable base lineage."""
    requested_path = Path(path).expanduser()
    root_is_symlink = requested_path.is_symlink()
    supplement = requested_path.resolve()
    errors: List[str] = []
    warnings: List[str] = []
    checked_files = 0
    checked_items = 0
    result: Dict[str, Any] = {
        "ok": False,
        "path": str(supplement),
        "supplement_id": None,
        "base_snapshot_id": None,
        "checked_files": 0,
        "checked_items": 0,
        "errors": errors,
        "warnings": warnings,
    }
    if supplement.name.endswith(".incomplete"):
        errors.append("目录以 .incomplete 结尾，不是 sealed supplement")
    if root_is_symlink:
        errors.append("supplement 根目录是符号链接")
    manifest_path = supplement / "manifest.json"
    if not manifest_path.is_file():
        errors.append("缺少 manifest.json")
        return result
    try:
        manifest = _load_json(manifest_path)
    except Exception as error:
        errors.append(f"manifest.json 无法解析：{type(error).__name__}")
        return result
    result["supplement_id"] = manifest.get("supplement_id")
    lineage = manifest.get("lineage") or {}
    base_id = str(lineage.get("base_snapshot_id") or "")
    result["base_snapshot_id"] = base_id or None
    if manifest.get("format") != SUPPLEMENT_FORMAT:
        errors.append("supplement format 无效")
    if int(manifest.get("schema_version", 0)) < SUPPLEMENT_SCHEMA_VERSION:
        errors.append("supplement schema_version 过旧")
    if manifest.get("status") != "complete":
        errors.append("supplement status 不是 complete")
    if manifest.get("errors"):
        errors.append("complete supplement manifest 含 errors")
    if not re.fullmatch(r"[0-9a-f]{64}", str(lineage.get("site_fingerprint") or "")):
        errors.append("lineage site_fingerprint 无效")
    if manifest.get("supplement_id") != supplement.name:
        errors.append("supplement_id 与目录名不一致")

    base: Optional[Path] = None
    if (
        base_id
        and supplement.parent.name == base_id
        and supplement.parent.parent.name == "media_supplements"
    ):
        base = supplement.parents[2] / base_id
    else:
        errors.append("supplement 不在 media_supplements/<base_id>/<supplement_id> 层级")
    if base is not None:
        verification = _verify_base_snapshot(base, require_full=False)
        if not verification.get("ok"):
            errors.append("lineage base snapshot 校验失败")
        else:
            try:
                actual_hashes = _hash_triplet(base)
                for key, actual in actual_hashes.items():
                    if lineage.get(key) != actual:
                        errors.append(f"lineage {key} 不匹配")
                base_manifest = _load_json(base / "manifest.json")
                base_site = str((base_manifest.get("source") or {}).get("site") or "")
                if base_site and lineage.get("site_fingerprint") != _site_fingerprint(base_site):
                    errors.append("lineage site_fingerprint 与 base source.site 不匹配")
            except Exception as error:
                errors.append(f"lineage base hash 校验失败：{type(error).__name__}")

    checksum_path = supplement / "checksums.sha256"
    declared_paths: set = set()
    if not checksum_path.is_file():
        errors.append("缺少 checksums.sha256")
    else:
        try:
            for expected, relative in _parse_checksums(checksum_path):
                if relative in declared_paths:
                    errors.append(f"checksums 重复路径：{relative}")
                    continue
                declared_paths.add(relative)
                target = _safe_relative(supplement, relative)
                if target.is_symlink() or not target.is_file():
                    errors.append(f"checksums 文件缺失或是 symlink：{relative}")
                    continue
                checked_files += 1
                if sha256_file(target) != expected:
                    errors.append(f"checksums SHA-256 不匹配：{relative}")
        except Exception as error:
            errors.append(f"checksums 校验失败：{type(error).__name__}")

    receipt_path = supplement / "COMPLETED.json"
    if not receipt_path.is_file():
        errors.append("缺少 COMPLETED.json")
    else:
        try:
            receipt = _load_json(receipt_path)
            if receipt.get("supplement_id") != manifest.get("supplement_id"):
                errors.append("COMPLETED supplement_id 不匹配")
            if receipt.get("base_snapshot_id") != base_id:
                errors.append("COMPLETED base_snapshot_id 不匹配")
            if receipt.get("manifest_sha256") != sha256_file(manifest_path):
                errors.append("COMPLETED manifest_sha256 不匹配")
            if (
                not checksum_path.is_file()
                or receipt.get("checksums_sha256") != sha256_file(checksum_path)
            ):
                errors.append("COMPLETED checksums_sha256 不匹配")
        except Exception as error:
            errors.append(f"COMPLETED 校验失败：{type(error).__name__}")

    actual_payloads: set = set()
    root_control_files = {"manifest.json", "checksums.sha256", "COMPLETED.json"}
    if supplement.is_dir():
        for candidate in supplement.rglob("*"):
            try:
                if candidate.is_symlink():
                    errors.append(
                        "supplement 含符号链接：" + candidate.relative_to(supplement).as_posix()
                    )
                elif candidate.is_file():
                    candidate_relative = candidate.relative_to(supplement).as_posix()
                    if candidate_relative not in root_control_files:
                        actual_payloads.add(candidate_relative)
            except OSError:
                errors.append("supplement 文件树无法安全读取")
    for relative in sorted(actual_payloads - declared_paths):
        errors.append(f"存在未登记 payload：{relative}")
    for relative in sorted(declared_paths - actual_payloads):
        errors.append(f"checksum 引用不存在 payload：{relative}")

    try:
        index_path = _safe_relative(
            supplement, str(manifest.get("media_index") or "media/index.json")
        )
    except Exception:
        index_path = supplement / "__invalid_media_index__"
        errors.append("manifest media_index 路径无效")
    index: List[Dict[str, Any]] = []
    if not index_path.is_file():
        errors.append("缺少 media/index.json")
    else:
        try:
            index = _load_index(index_path)
        except Exception as error:
            errors.append(f"media/index.json 无效：{type(error).__name__}")
    seen_keys: set = set()
    seen_targets: set = set()
    policy = manifest.get("policy") or {}
    copy_external = bool(policy.get("copy_external", True))
    calculated_required = 0
    calculated_covered = 0
    calculated_failed = 0
    allowed_entry_keys = {
        "source",
        "source_key",
        "field",
        "kind",
        "state",
        "status",
        "owner",
        "acquisition",
        "files",
        "locator_sha256",
        "materialized_locator_sha256",
        "temporal_fidelity",
        "source_fingerprint",
    }
    calculated_groups: Dict[str, Dict[str, int]] = {
        "hosted": {"total": 0, "complete": 0, "failed": 0},
        "external": {
            "total": 0,
            "complete": 0,
            "failed": 0,
            "excluded_by_policy": 0,
        },
        "ordinary": {"total": 0, "skipped": 0},
        "ambiguous": {
            "total": 0,
            "complete": 0,
            "failed": 0,
            "excluded_by_policy": 0,
        },
    }
    for entry in index:
        checked_items += 1
        unknown_entry_keys = sorted(set(entry) - allowed_entry_keys)
        if unknown_entry_keys:
            errors.append("index 含未允许字段：" + ",".join(unknown_entry_keys))
        source = entry.get("source") or {}
        source_key = str(entry.get("source_key") or "")
        field = str(entry.get("field") or "")
        kind = str(entry.get("kind") or "")
        status_name = str(entry.get("status") or "")
        owner = str(entry.get("owner") or "")
        try:
            if type(source.get("id")) is not int or int(source["id"]) <= 0:
                raise ValueError
            source_id = int(source["id"])
        except (KeyError, TypeError, ValueError):
            errors.append("index source.id 无效")
            continue
        if not source.get("type") or not field:
            errors.append("index source.type/field 缺失")
            continue
        if set(source) - {"type", "id"}:
            errors.append(f"index source 含未允许字段：{source_key}")
        if entry.get("state") not in {"active", "retired"}:
            errors.append(f"index state 无效：{source_key}")
        if status_name not in {"complete", "failed", "skipped", "excluded_by_policy"}:
            errors.append(f"index status 无效：{source_key}")
        if not kind:
            errors.append(f"index kind 缺失：{source_key}")
        expected_key = _source_key(str(source["type"]), source_id, field)
        if source_key != expected_key:
            errors.append(f"index source_key 无效：{source_key}")
        if source_key in seen_keys:
            errors.append(f"index source_key 重复：{source_key}")
        seen_keys.add(source_key)
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("locator_sha256") or "")):
            errors.append(f"index locator_sha256 无效：{source_key}")
        materialized_hash = entry.get("materialized_locator_sha256")
        if materialized_hash is not None and not re.fullmatch(
            r"[0-9a-f]{64}", str(materialized_hash)
        ):
            errors.append(f"index materialized_locator_sha256 无效：{source_key}")
        source_fingerprint = entry.get("source_fingerprint")
        if source_fingerprint is not None and not re.fullmatch(
            r"[0-9a-f]{64}", str(source_fingerprint)
        ):
            errors.append(f"index source_fingerprint 无效：{source_key}")
        required = (
            kind.startswith("hosted_")
            or kind == "ambiguous_hosted"
            or (copy_external and (kind.startswith("external_") or kind == "ambiguous_external"))
        )
        if kind.startswith("hosted_"):
            group_name = "hosted"
        elif kind.startswith("external_"):
            group_name = "external"
        elif kind in {"ordinary_web", "no_payload"} or (
            kind == "ambiguous" and not required
        ):
            group_name = "ordinary"
        else:
            group_name = "ambiguous"
        calculated_groups[group_name]["total"] += 1
        if status_name in calculated_groups[group_name]:
            calculated_groups[group_name][status_name] += 1
        if required:
            calculated_required += 1
            if status_name == "complete":
                calculated_covered += 1
            elif status_name == "failed":
                calculated_failed += 1
        if status_name == "complete" and owner not in {"base", "supplement"}:
            errors.append(f"complete index owner 无效：{source_key}")
        if owner not in {"base", "supplement", "none"}:
            errors.append(f"index owner 未知：{source_key}")
        if status_name == "excluded_by_policy" and copy_external:
            errors.append(f"copy_external=true 但 index 被 policy 排除：{source_key}")
        files = entry.get("files") or []
        if not isinstance(files, list):
            errors.append(f"index files 不是 array：{source_key}")
            files = []
        if owner == "none" and files:
            errors.append(f"owner=none 却含 files：{source_key}")
        owner_root = base if owner == "base" else supplement
        if owner in {"base", "supplement"} and owner_root is None:
            errors.append(f"index owner root 不可用：{source_key}")
            continue
        if status_name == "complete" and not files:
            errors.append(f"complete index 没有 files：{source_key}")
        for file_info in files:
            try:
                if not isinstance(file_info, dict):
                    raise MediaSyncError("index file 不是 object")
                if set(file_info) - {"path", "size", "sha256", "frame"}:
                    errors.append(f"index file 含未允许字段：{source_key}")
                relative = str(file_info["path"])
                target_key = (owner, relative)
                if target_key in seen_targets:
                    errors.append(f"index target 重复：{owner}:{relative}")
                seen_targets.add(target_key)
                frame_value = file_info.get("frame")
                frame = int(frame_value) if frame_value is not None else None
                if owner == "supplement":
                    shape_error = _verify_supplement_file_shape(relative, source, field, frame)
                    if shape_error:
                        errors.append(f"{shape_error}：{relative}")
                elif owner == "base" and not relative.startswith(("attachments/", "media/")):
                    errors.append(f"owner=base 文件不在 attachments/media：{relative}")
                target = _safe_relative(owner_root, relative)  # type: ignore[arg-type]
                if not _validated_file(target, file_info.get("size"), file_info.get("sha256")):
                    errors.append(f"index 文件大小/哈希(hash)无效：{relative}")
            except Exception as error:
                errors.append(f"index 文件校验失败：{source_key}:{type(error).__name__}")

    coverage = manifest.get("coverage") or {}
    if int(coverage.get("required", -1)) != calculated_required:
        errors.append("coverage.required 与 index 不一致")
    if int(coverage.get("covered", -1)) != calculated_covered:
        errors.append("coverage.covered 与 index 不一致")
    if int(coverage.get("failed", -1)) != calculated_failed:
        errors.append("coverage.failed 与 index 不一致")
    if int(coverage.get("items_total", -1)) != len(index):
        errors.append("coverage.items_total 与 index 不一致")
    for group_name, calculated in calculated_groups.items():
        declared_group = coverage.get(group_name) or {}
        for key, value in calculated.items():
            if int(declared_group.get(key, -1)) != value:
                errors.append(f"coverage.{group_name}.{key} 与 index 不一致")
    indexed_bytes = sum(
        int(file_info.get("size", 0))
        for entry in index
        for file_info in (entry.get("files") or [])
    )
    if int(coverage.get("total_bytes", -1)) != indexed_bytes:
        errors.append("coverage.total_bytes 与 index 不一致")
    integrity = manifest.get("integrity") or {}
    if int(integrity.get("files_hashed", -1)) != len(declared_paths):
        errors.append("integrity.files_hashed 与 checksums 行数不一致")
    if calculated_required != calculated_covered or calculated_failed:
        errors.append("required media coverage 未闭合")
    temporal = str(lineage.get("temporal_fidelity") or "")
    index_temporal_modes = sorted(
        {
            str(entry.get("temporal_fidelity") or "snapshot_exact")
            for entry in index
            if entry.get("status") == "complete"
        }
    )
    current_refetch = "current_refetch" in index_temporal_modes
    if current_refetch and temporal != "current_refetch":
        errors.append("index current_refetch 未反映到 lineage")
    if any(mode != "snapshot_exact" for mode in index_temporal_modes):
        if lineage.get("snapshot_exact") is not False:
            errors.append("含当前时点媒体的 supplement 不得声明 snapshot_exact")
    declared_modes = lineage.get("temporal_fidelity_modes")
    if declared_modes is not None and sorted(declared_modes) != (
        index_temporal_modes or ["snapshot_exact"]
    ):
        errors.append("lineage temporal_fidelity_modes 与 index 不一致")

    result.update(
        {
            "ok": not errors,
            "checked_files": checked_files,
            "checked_items": checked_items,
            "errors": errors,
            "warnings": warnings,
            "payload_scope": manifest.get("payload_scope"),
            "temporal_fidelity": temporal,
        }
    )
    return result
