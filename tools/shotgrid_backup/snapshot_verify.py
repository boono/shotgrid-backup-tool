#!/usr/bin/env python3
"""Verify a portable ShotGrid snapshot and assess future restore readiness."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Optional

from backup import atomic_json, sha256_file


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
SUPPLEMENT_CONTROL_FILES = {"manifest.json", "checksums.sha256", "COMPLETED.json"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _lstat_owned_path(root: Path, relative: Any, label: str) -> tuple[str, Path]:
    """Return a lexical child path after rejecting links in every existing component."""
    normalized = _safe_relative_path(relative, label)
    root = root.absolute()
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        raise ValueError(f"{label} root 不存在")
    except OSError as error:
        raise ValueError(
            f"{label} root 无法 lstat：{type(error).__name__}"
        ) from error
    if stat.S_ISLNK(root_info.st_mode):
        raise ValueError(f"{label} root 不得是 symlink")
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"{label} root 不是目录")

    parts = PurePosixPath(normalized).parts
    target = root.joinpath(*parts)
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            # Callers retain responsibility for reporting a missing target.  Once
            # a component is absent there is no descendant symlink to inspect.
            break
        except OSError as error:
            raise ValueError(
                f"{label} 组件无法 lstat：{normalized} "
                f"({type(error).__name__})"
            ) from error
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"{label} 不得经过 symlink：{normalized}")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{label} 中间组件不是目录：{normalized}")
        if index == len(parts) - 1 and not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{label} 目标不是普通文件：{normalized}")
    return normalized, target


def safe_snapshot_path(snapshot: Path, relative: str) -> Path:
    """Resolve a trusted base-snapshot path without following filesystem links."""
    _, target = _lstat_owned_path(snapshot, relative, "快照路径")
    return target


def _scan_owned_tree(root: Path, label: str) -> tuple[list[str], set[str]]:
    """Inventory regular files without following symlinks or opening special files."""
    errors: list[str] = []
    regular_files: set[str] = set()
    root = root.absolute()
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return [f"{label} root 不存在"], regular_files
    except OSError as error:
        return [f"{label} root 无法 lstat：{type(error).__name__}"], regular_files
    if stat.S_ISLNK(root_info.st_mode):
        return [f"{label} root 不得是 symlink"], regular_files
    if not stat.S_ISDIR(root_info.st_mode):
        return [f"{label} root 不是目录"], regular_files

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    try:
                        relative = path.relative_to(root).as_posix()
                        info = path.lstat()
                    except (OSError, ValueError) as error:
                        errors.append(
                            f"{label} 条目无法 lstat：{entry.name} ({type(error).__name__})"
                        )
                        continue
                    mode = info.st_mode
                    if stat.S_ISLNK(mode):
                        errors.append(f"{label} 不得包含 symlink：{relative}")
                    elif stat.S_ISDIR(mode):
                        pending.append(path)
                    elif stat.S_ISREG(mode):
                        regular_files.add(relative)
                    else:
                        errors.append(f"{label} 不得包含非普通 payload：{relative}")
        except OSError as error:
            relative_directory = (
                "." if directory == root else directory.relative_to(root).as_posix()
            )
            errors.append(
                f"{label} 目录无法扫描：{relative_directory} ({type(error).__name__})"
            )
    return errors, regular_files


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path.name}:{line_number} 不是 JSON object")
            yield line_number, value


def parse_checksums(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError(f"checksums.sha256:{line_number} 格式错误")
        rows.append((parts[0], parts[1]))
    return rows


def _load_media_supplement_verifier() -> Callable[[Path], dict[str, Any]]:
    """Load the sibling module both from the CLI and importlib-based tests."""
    try:
        from media_sync import verify_media_supplement

        return verify_media_supplement
    except ImportError as import_error:
        module_path = Path(__file__).with_name("media_sync.py")
        if not module_path.is_file():
            raise RuntimeError(f"缺少媒体补充包校验模块：{module_path}") from import_error
        module_directory = str(module_path.parent)
        inserted = module_directory not in sys.path
        if inserted:
            sys.path.insert(0, module_directory)
        try:
            from media_sync import verify_media_supplement

            return verify_media_supplement
        except ImportError as retry_error:
            raise RuntimeError(f"无法加载媒体补充包校验模块：{module_path}") from retry_error
        finally:
            if inserted and sys.path and sys.path[0] == module_directory:
                sys.path.pop(0)


def _safe_relative_path(relative: Any, label: str) -> str:
    """Validate a portable, lexical POSIX path without resolving it."""
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} 缺少相对路径")
    if "\x00" in relative or "\\" in relative or WINDOWS_DRIVE_RE.match(relative):
        raise ValueError(f"{label} 不是安全的 POSIX 相对路径：{relative!r}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or relative.startswith("/"):
        raise ValueError(f"{label} 不得使用绝对路径：{relative}")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"{label} 含不安全路径组件：{relative}")
    normalized = pure.as_posix()
    if normalized != relative:
        raise ValueError(f"{label} 不是规范相对路径：{relative}")
    return normalized


def _safe_owned_path(root: Path, relative: Any, label: str) -> tuple[str, Path]:
    """Resolve an indexed path while rejecting symlinks in every component."""
    return _lstat_owned_path(root, relative, label)


def _media_layout_error(relative: str, item: dict[str, Any]) -> Optional[str]:
    """Return why a media payload is not in the portable sharded layout."""
    parts = PurePosixPath(relative).parts
    if not parts or parts[0] != "media":
        return "媒体 payload 不在 media/ 下"
    if len(parts) < 6:
        return "媒体 payload 必须使用 media/<Entity>/<id_bucket>/<source_id>/<field>/... 分层"
    source = item.get("source") or {}
    source_type = str(source.get("type") or "")
    safe_entity = (SAFE_COMPONENT_RE.sub("_", source_type).strip("._") or "Entity")[:96]
    if parts[1] != safe_entity:
        return "媒体 payload 的 Entity 目录与 source.type 不一致"
    source_id = source.get("id")
    if not isinstance(source_id, int) or source_id <= 0:
        return "媒体 payload 的 source.id 无效"
    bucket_start = (source_id // 1000) * 1000
    expected_bucket = f"{bucket_start:06d}_{bucket_start + 999:06d}"
    if parts[2] != expected_bucket:
        return "媒体 payload 的 id_bucket 与 source.id 不一致"
    if parts[3] != str(source_id):
        return "媒体 payload 的 source_id 目录与 source.id 不一致"
    field = str(item.get("field") or "")
    safe_field = (SAFE_COMPONENT_RE.sub("_", field).strip("._") or "field")[:96]
    if parts[4] != safe_field:
        return "媒体 payload 的 field 目录与索引字段不一致"
    return None


def _copy_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "ok": False,
            "errors": ["verify_media_supplement() 未返回 JSON object"],
            "warnings": [],
        }
    copied = dict(result)
    copied["errors"] = list(result.get("errors") or [])
    copied["warnings"] = list(result.get("warnings") or [])
    return copied


def _integer(value: Any, label: str, errors: list[str]) -> Optional[int]:
    if isinstance(value, bool):
        errors.append(f"{label} 不是有效整数")
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        errors.append(f"{label} 不是有效整数")
        return None
    if parsed < 0:
        errors.append(f"{label} 不得为负数")
        return None
    return parsed


def _find_absolute_lineage_values(value: Any, prefix: str = "lineage") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            findings.extend(_find_absolute_lineage_values(nested, f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            findings.extend(_find_absolute_lineage_values(nested, f"{prefix}[{index}]"))
    elif isinstance(value, str):
        if (
            value.startswith(("/", "\\", "~/", "file://"))
            or WINDOWS_DRIVE_RE.match(value)
        ):
            findings.append(f"{prefix} 含源机器绝对路径：{value}")
    return findings


def _coverage_group(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "")
    if (
        kind == "ambiguous"
        and item.get("status") == "skipped"
        and item.get("acquisition") == "ordinary"
    ):
        return "ordinary"
    if kind.startswith("ambiguous") or kind == "ambiguous":
        return "ambiguous"
    if kind.startswith("hosted"):
        return "hosted"
    if kind.startswith("external"):
        return "external"
    if kind.startswith("ordinary"):
        return "ordinary"
    return kind


def _payload_group(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "")
    if kind == "ambiguous_hosted" or kind.startswith("hosted"):
        return "hosted"
    if kind == "ambiguous_external" or kind.startswith("external"):
        return "external"
    return kind


def _summarize_supplement_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for kind in ("hosted", "external"):
        selected = [item for item in items if _payload_group(item) == kind]
        acquisitions: dict[str, int] = {}
        files = 0
        total_bytes = 0
        for item in selected:
            acquisition = str(item.get("acquisition") or "unknown")
            acquisitions[acquisition] = acquisitions.get(acquisition, 0) + 1
            indexed_files = item.get("files") if isinstance(item.get("files"), list) else []
            files += len(indexed_files)
            for indexed_file in indexed_files:
                if isinstance(indexed_file, dict):
                    try:
                        total_bytes += int(indexed_file.get("size", 0))
                    except (TypeError, ValueError):
                        pass
        summary[kind] = {
            "copied": acquisitions.get("copied", 0),
            "downloaded": acquisitions.get("downloaded", 0),
            "resumed": acquisitions.get("resumed", 0),
            "reused": (
                acquisitions.get("reused_base", 0)
                + acquisitions.get("reused_supplement", 0)
                + acquisitions.get("reused_interrupted", 0)
            ),
            "failed": sum(1 for item in selected if item.get("status") == "failed"),
            "files": files,
            "bytes": total_bytes,
            "acquisitions": dict(sorted(acquisitions.items())),
        }
    summary["files"] = sum(int(summary[kind]["files"]) for kind in ("hosted", "external"))
    summary["bytes"] = sum(int(summary[kind]["bytes"]) for kind in ("hosted", "external"))
    return summary


def _verify_supplement_contract(
    supplement: Path,
    *,
    base_snapshot: Optional[Path] = None,
    base_manifest: Optional[dict[str, Any]] = None,
    require_complete: bool = False,
    require_all_media: bool = False,
) -> dict[str, Any]:
    supplement = supplement.absolute()
    try:
        result = _copy_result(_load_media_supplement_verifier()(supplement))
    except Exception as error:
        result = {
            "ok": False,
            "errors": [f"媒体补充包基础校验失败：{error}"],
            "warnings": [],
        }
    errors = result["errors"]
    warnings = result["warnings"]
    if base_snapshot is not None:
        # The standalone verifier discovers its base from the canonical output
        # tree.  This CLI has an explicit base argument, so a portable copied
        # pair may live elsewhere; the checks below re-verify every base-owned
        # file and the complete lineage hash triplet against that explicit base.
        errors[:] = [
            error
            for error in errors
            if error != "supplement 不在 media_supplements/<base_id>/<supplement_id> 层级"
            and not str(error).startswith("index owner root 不可用：")
        ]
    if supplement.name.endswith(".incomplete"):
        errors.append("媒体补充包目录以 .incomplete 结尾，不是已发布补充包")
    manifest_path = supplement / "manifest.json"
    if not manifest_path.is_file():
        errors.append("媒体补充包缺少 manifest.json")
        result["ok"] = False
        return result
    try:
        manifest = load_json(manifest_path)
        if not isinstance(manifest, dict):
            raise ValueError("manifest 不是 JSON object")
    except Exception as error:
        errors.append(f"媒体补充包 manifest.json 无法解析：{error}")
        result["ok"] = False
        return result

    result.update({
        "path": str(supplement),
        "format": manifest.get("format"),
        "schema_version": manifest.get("schema_version"),
        "supplement_id": manifest.get("supplement_id"),
        "base_snapshot_id": (manifest.get("lineage") or {}).get("base_snapshot_id"),
        "status": manifest.get("status"),
        "media_payload_scope": manifest.get("payload_scope") or {},
        "media_payload_coverage": manifest.get("payload_coverage") or {},
        "transfer_stats": manifest.get("transfer") or {},
    })
    if manifest.get("format") != "shotgrid_media_supplement":
        errors.append("输入目录不是 shotgrid_media_supplement")
    supplement_schema_version = _integer(
        manifest.get("schema_version"), "媒体补充包 schema_version", errors
    )
    if supplement_schema_version is not None and supplement_schema_version < 1:
        errors.append("媒体补充包 schema_version 低于 1")
    if manifest.get("status") == "complete" and manifest.get("errors"):
        errors.append("complete 媒体补充包 manifest.errors 不为空")

    lineage = manifest.get("lineage") or {}
    if not isinstance(lineage, dict):
        errors.append("媒体补充包 lineage 不是 JSON object")
        lineage = {}
    errors.extend(_find_absolute_lineage_values(lineage, "媒体补充包 lineage"))
    if base_snapshot is not None and base_manifest is not None:
        base_snapshot = base_snapshot.absolute()
        expected_lineage = {
            "base_snapshot_id": base_manifest.get("snapshot_id"),
            "base_manifest_sha256": sha256_file(
                safe_snapshot_path(base_snapshot, "manifest.json")
            ),
            "base_completed_sha256": sha256_file(
                safe_snapshot_path(base_snapshot, "COMPLETED.json")
            ),
            "base_checksums_sha256": sha256_file(
                safe_snapshot_path(base_snapshot, "checksums.sha256")
            ),
        }
        for key, expected in expected_lineage.items():
            if lineage.get(key) != expected:
                errors.append(
                    f"媒体补充包 lineage.{key} 不匹配："
                    f"expected={expected} actual={lineage.get(key)}"
                )

    checksum_path = supplement / "checksums.sha256"
    declared_paths: set[str] = set()
    if not checksum_path.is_file():
        errors.append("媒体补充包缺少 checksums.sha256")
    else:
        try:
            for expected, raw_relative in parse_checksums(checksum_path):
                relative, target = _safe_owned_path(
                    supplement, raw_relative, "媒体补充包 checksums.sha256"
                )
                if relative in declared_paths:
                    raise ValueError(f"checksums.sha256 重复登记：{relative}")
                declared_paths.add(relative)
                if not target.is_file():
                    errors.append(f"媒体补充包缺少校验文件：{relative}")
                elif sha256_file(target) != expected:
                    errors.append(f"媒体补充包 SHA-256 不匹配：{relative}")
        except Exception as error:
            errors.append(f"媒体补充包校验清单失败：{error}")

    actual_payloads: set[str] = set()
    if supplement.is_dir() and not supplement.is_symlink():
        for path in supplement.rglob("*"):
            relative = path.relative_to(supplement).as_posix()
            if path.is_symlink():
                errors.append(f"媒体补充包不得包含 symlink：{relative}")
            elif path.is_file() and relative not in SUPPLEMENT_CONTROL_FILES:
                actual_payloads.add(relative)
    elif supplement.is_symlink():
        errors.append("媒体补充包根目录不得是 symlink")
    for relative in sorted(actual_payloads - declared_paths):
        errors.append(f"媒体补充包存在未登记 payload：{relative}")
    for relative in sorted(declared_paths - actual_payloads):
        errors.append(f"媒体补充包校验清单引用不存在 payload：{relative}")
    integrity = manifest.get("integrity") or {}
    if not isinstance(integrity, dict):
        errors.append("媒体补充包 integrity 不是 JSON object")
    else:
        if integrity.get("checksums_file") != "checksums.sha256":
            errors.append("媒体补充包 integrity.checksums_file 无效")
        files_hashed = _integer(
            integrity.get("files_hashed"), "integrity.files_hashed", errors
        )
        if files_hashed is not None and files_hashed != len(declared_paths):
            errors.append(
                "integrity.files_hashed 数量不匹配："
                f"manifest={files_hashed} actual={len(declared_paths)}"
            )
    receipt_path = supplement / "COMPLETED.json"
    if not receipt_path.is_file():
        errors.append("媒体补充包缺少 COMPLETED.json 发布回执")
    else:
        try:
            receipt = load_json(receipt_path)
            if not isinstance(receipt, dict):
                raise ValueError("COMPLETED.json 不是 JSON object")
            if receipt.get("supplement_id") != manifest.get("supplement_id"):
                errors.append("媒体补充包 COMPLETED.json supplement_id 不匹配")
            if receipt.get("base_snapshot_id") != lineage.get("base_snapshot_id"):
                errors.append("媒体补充包 COMPLETED.json base_snapshot_id 不匹配")
            if receipt.get("manifest_sha256") != sha256_file(manifest_path):
                errors.append("媒体补充包 manifest.json 哈希与发布回执不匹配")
            if (
                not checksum_path.is_file()
                or receipt.get("checksums_sha256") != sha256_file(checksum_path)
            ):
                errors.append("媒体补充包 checksums.sha256 哈希与发布回执不匹配")
        except Exception as error:
            errors.append(f"媒体补充包发布回执校验失败：{error}")

    items: list[dict[str, Any]] = []
    raw_index_path = manifest.get("media_index") or "media/index.json"
    try:
        index_relative, index_path = _safe_owned_path(
            supplement, raw_index_path, "媒体补充包 media_index"
        )
        if index_relative not in declared_paths:
            errors.append(f"媒体补充包 media_index 未登记到 checksums.sha256：{index_relative}")
        raw_items = load_json(index_path)
        if not isinstance(raw_items, list):
            raise ValueError("媒体补充包 index 不是 JSON array")
        if any(not isinstance(item, dict) for item in raw_items):
            raise ValueError("媒体补充包 index item 不是 JSON object")
        items = raw_items
    except Exception as error:
        errors.append(f"媒体补充包 index 校验失败：{error}")

    seen_source_keys: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    base_owner_unverified = False
    acquisition_counts: dict[str, int] = {}
    for item_number, item in enumerate(items, start=1):
        label = f"媒体补充包 index[{item_number}]"
        source = item.get("source") or {}
        source_valid = (
            isinstance(source, dict)
            and bool(source.get("type"))
            and type(source.get("id")) is int
            and source.get("id") > 0
        )
        if not source_valid:
            errors.append(f"{label} source 无效")
        field = item.get("field")
        if not isinstance(field, str) or not field:
            errors.append(f"{label} field 无效")
        source_key = item.get("source_key")
        if not isinstance(source_key, str) or not source_key:
            errors.append(f"{label} source_key 无效")
        elif source_key in seen_source_keys:
            errors.append(f"媒体补充包 index source_key 重复：{source_key}")
        else:
            seen_source_keys.add(source_key)
            if source_valid and isinstance(field, str) and field:
                expected_source_key = f"{source['type']}:{source['id']}:{field}"
                if source_key != expected_source_key:
                    errors.append(
                        f"{label} source_key 与 source/field 不一致：{source_key}"
                    )
        if item.get("state") not in {"active", "retired"}:
            errors.append(f"{label} state 无效：{item.get('state')}")
        if item.get("status") not in {
            "complete", "failed", "skipped", "excluded_by_policy"
        }:
            errors.append(f"{label} status 无效：{item.get('status')}")
        if not isinstance(item.get("kind"), str) or not item.get("kind"):
            errors.append(f"{label} kind 无效")
        files = item.get("files")
        if not isinstance(files, list):
            errors.append(f"{label} files 不是 JSON array")
            files = []
        owner = item.get("owner")
        if owner not in {"base", "supplement", "none"}:
            errors.append(f"{label} owner 无效：{owner}")
        if owner == "none" and files:
            errors.append(f"{label} owner=none 时不得声明 files")
        acquisition = str(item.get("acquisition") or "unknown")
        acquisition_counts[acquisition] = acquisition_counts.get(acquisition, 0) + 1
        for file_number, indexed_file in enumerate(files, start=1):
            file_label = f"{label}.files[{file_number}]"
            if not isinstance(indexed_file, dict):
                errors.append(f"{file_label} 不是 JSON object")
                continue
            try:
                relative = _safe_relative_path(indexed_file.get("path"), file_label)
            except Exception as error:
                errors.append(str(error))
                continue
            target_key = (str(owner), relative)
            if target_key in seen_targets:
                errors.append(f"媒体补充包 index 目标重复：owner={owner} path={relative}")
            else:
                seen_targets.add(target_key)
            if owner == "supplement":
                try:
                    _, target = _safe_owned_path(supplement, relative, file_label)
                except Exception as error:
                    errors.append(str(error))
                    continue
                layout_error = _media_layout_error(relative, item)
                if layout_error:
                    errors.append(f"{file_label} {layout_error}：{relative}")
            elif owner == "base" and base_snapshot is not None:
                try:
                    _, target = _safe_owned_path(base_snapshot, relative, file_label)
                except Exception as error:
                    errors.append(str(error))
                    continue
                if relative.startswith("media/"):
                    layout_error = _media_layout_error(relative, item)
                    if layout_error:
                        warnings.append(f"legacy base media 路径未分层：{relative}（{layout_error}）")
            elif owner == "base":
                target = None
                base_owner_unverified = True
            else:
                target = None
            expected_size = _integer(indexed_file.get("size"), f"{file_label}.size", errors)
            expected_hash = indexed_file.get("sha256")
            if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
                errors.append(f"{file_label}.sha256 无效")
            if target is not None:
                if not target.is_file():
                    errors.append(f"{file_label} 引用文件不存在：{relative}")
                else:
                    if expected_size is not None and target.stat().st_size != expected_size:
                        errors.append(f"{file_label} 大小不匹配：{relative}")
                    if isinstance(expected_hash, str) and SHA256_RE.fullmatch(expected_hash):
                        if sha256_file(target) != expected_hash:
                            errors.append(f"{file_label} SHA-256 不匹配：{relative}")
    if base_owner_unverified:
        warnings.append("未提供 base snapshot，无法复核 owner=base 的媒体 payload")

    coverage = manifest.get("coverage") or {}
    if not isinstance(coverage, dict):
        errors.append("媒体补充包 coverage 不是 JSON object")
        coverage = {}
    expected_overall = {
        "items_total": len(items),
        "required": sum(
            1 for item in items if item.get("status") in {"complete", "failed"}
        ),
        "covered": sum(1 for item in items if item.get("status") == "complete"),
        "failed": sum(1 for item in items if item.get("status") == "failed"),
        "total_bytes": sum(
            int(indexed_file.get("size", 0))
            for item in items
            for indexed_file in (item.get("files") or [])
            if isinstance(indexed_file, dict) and str(indexed_file.get("size", "")).isdigit()
        ),
    }
    for key, expected in expected_overall.items():
        actual = _integer(coverage.get(key), f"coverage.{key}", errors)
        if actual is not None and actual != expected:
            errors.append(f"coverage.{key} 数量不匹配：manifest={actual} actual={expected}")
    for kind in ("hosted", "external", "ordinary", "ambiguous"):
        metadata = coverage.get(kind) or {}
        if not isinstance(metadata, dict):
            errors.append(f"coverage.{kind} 不是 JSON object")
            continue
        selected = [item for item in items if _coverage_group(item) == kind]
        actual_total = _integer(metadata.get("total"), f"coverage.{kind}.total", errors)
        if actual_total is not None and actual_total != len(selected):
            errors.append(
                f"coverage.{kind}.total 数量不匹配：manifest={actual_total} actual={len(selected)}"
            )
        for status_key in ("complete", "failed", "skipped", "excluded_by_policy"):
            if status_key not in metadata:
                continue
            expected = sum(1 for item in selected if item.get("status") == status_key)
            actual = _integer(
                metadata.get(status_key), f"coverage.{kind}.{status_key}", errors
            )
            if actual is not None and actual != expected:
                errors.append(
                    f"coverage.{kind}.{status_key} 数量不匹配："
                    f"manifest={actual} actual={expected}"
                )

    transfer = manifest.get("transfer") or {}
    counters = transfer.get("counters") if isinstance(transfer, dict) else {}
    if isinstance(counters, dict):
        for acquisition, expected in sorted(acquisition_counts.items()):
            if acquisition in counters:
                actual = _integer(counters.get(acquisition), f"transfer.counters.{acquisition}", errors)
                if actual is not None and actual != expected:
                    errors.append(
                        f"transfer.counters.{acquisition} 数量不匹配："
                        f"manifest={actual} actual={expected}"
                    )

    if require_complete or require_all_media:
        if manifest.get("status") != "complete":
            errors.append(f"媒体补充包状态不是 complete：{manifest.get('status')}")
        required = _integer(coverage.get("required"), "coverage.required", errors)
        covered = _integer(coverage.get("covered"), "coverage.covered", errors)
        if required is not None and covered is not None and required != covered:
            errors.append(f"媒体补充包 required/covered 不一致：{required}/{covered}")
        failed = _integer(coverage.get("failed"), "coverage.failed", errors)
        if failed not in {None, 0}:
            errors.append(f"媒体补充包 failed 不为 0：{failed}")
    payload_scope = manifest.get("payload_scope") or {}
    if require_all_media:
        payload_coverage = manifest.get("payload_coverage") or {}
        hosted_complete = (
            isinstance(payload_coverage, dict)
            and payload_coverage.get("hosted") == "complete"
        ) or (
            isinstance(payload_scope, dict) and payload_scope.get("hosted") == "complete"
        )
        external_complete = (
            isinstance(payload_coverage, dict)
            and payload_coverage.get("external") == "complete"
        ) or (
            isinstance(payload_scope, dict) and payload_scope.get("external") == "complete"
        )
        if isinstance(payload_scope, str):
            hosted_complete = hosted_complete or "shotgrid_hosted_complete" in payload_scope
            external_complete = external_complete or "external_complete" in payload_scope
        if not hosted_complete:
            errors.append("--require-all-media 要求 hosted media complete")
        if not external_complete:
            errors.append("--require-all-media 要求 external media complete")

    result["media_stats"] = _summarize_supplement_items(items)
    result["checked_items"] = len(items)
    result["ok"] = not errors
    return result


def verify_snapshot(
    snapshot: Path,
    require_full: bool = False,
    media_supplement: Optional[Path] = None,
    require_all_media: bool = False,
) -> dict[str, Any]:
    snapshot = snapshot.absolute()
    errors: list[str] = []
    warnings: list[str] = []
    checked_files = 0
    checked_records = 0
    checked_links = 0
    checked_attachments = 0
    checked_media_files = 0
    checked_media_bytes = 0
    if snapshot.name.endswith(".incomplete"):
        errors.append("目录以 .incomplete 结尾，不是已发布快照")

    # Inventory the complete base tree before trusting any control file.  scandir
    # plus lstat deliberately avoids following links and makes special payloads
    # (FIFO/socket/device) a validation error before any parser opens them.
    tree_errors, scanned_regular_files = _scan_owned_tree(snapshot, "base snapshot")
    errors.extend(tree_errors)
    try:
        manifest_path = safe_snapshot_path(snapshot, "manifest.json")
    except ValueError as error:
        errors.append(f"manifest.json 路径不安全：{error}")
        return {
            "ok": False,
            "snapshot": str(snapshot),
            "errors": errors,
            "warnings": warnings,
        }
    if not manifest_path.is_file():
        errors.append("缺少 manifest.json")
        return {
            "ok": False,
            "snapshot": str(snapshot),
            "errors": errors,
            "warnings": warnings,
        }
    try:
        manifest = load_json(manifest_path)
    except Exception as error:
        errors.append(f"manifest.json 无法解析：{error}")
        return {
            "ok": False,
            "snapshot": str(snapshot),
            "errors": errors,
            "warnings": warnings,
        }
    if isinstance(manifest, dict) and manifest.get("format") == "shotgrid_media_supplement":
        if media_supplement is not None:
            errors.append("输入目录已是媒体补充包，不能再指定 --media-supplement")
            return {
                "ok": False,
                "snapshot": str(snapshot),
                "errors": errors,
                "warnings": warnings,
            }
        supplement_result = _verify_supplement_contract(
            snapshot,
            require_complete=require_full,
            require_all_media=require_all_media,
        )
        if errors:
            supplement_result["errors"] = errors + list(
                supplement_result.get("errors") or []
            )
            supplement_result["ok"] = False
        return supplement_result
    if not isinstance(manifest, dict):
        errors.append("manifest.json 不是 JSON object")
        return {
            "ok": False,
            "snapshot": str(snapshot),
            "errors": errors,
            "warnings": warnings,
        }
    if manifest.get("format") != "shotgrid_portable_snapshot":
        errors.append("未知或旧版快照格式")
    if int(manifest.get("schema_version", 0)) < 3:
        errors.append("schema_version 低于 3，不满足当前恢复合同")
    if manifest.get("status") != "complete":
        errors.append(f"快照状态不是 complete：{manifest.get('status')}")
    if manifest.get("errors"):
        errors.append("manifest 包含备份错误")
    completeness = manifest.get("completeness") or {}
    if (require_full or require_all_media) and completeness.get("profile") not in {
        "site_full", "site_api_full"
    }:
        errors.append("该快照不是 site_full profile")
    lineage = manifest.get("lineage")
    if lineage is not None:
        errors.extend(_find_absolute_lineage_values(lineage, "base lineage"))

    try:
        entity_schema_path = safe_snapshot_path(snapshot, "schema/entities.json")
    except ValueError as error:
        errors.append(f"实体 schema 路径不安全：{error}")
        entity_schema_path = None
    readable_entities: set[str] = set()
    if entity_schema_path is None or not entity_schema_path.is_file():
        errors.append("缺少 schema/entities.json")
    else:
        try:
            source_schema = load_json(entity_schema_path)
            readable_entities = set(source_schema)
        except Exception as error:
            errors.append(f"实体 schema 无法解析：{error}")

    entities = manifest.get("entities") or {}
    planned_entities = set(manifest.get("entity_types_planned") or [])
    exported_entities = set(entities)
    if planned_entities != exported_entities:
        errors.append(
            "计划与导出实体集合不一致："
            f"missing={sorted(planned_entities - exported_entities)} "
            f"unexpected={sorted(exported_entities - planned_entities)}"
        )
    if completeness.get("profile") in {"site_full", "site_api_full"} and readable_entities != planned_entities:
        errors.append(
            "site_full 的 readable/planned 实体集合不一致："
            f"missing={sorted(readable_entities - planned_entities)} "
            f"unexpected={sorted(planned_entities - readable_entities)}"
        )

    try:
        errors_log = safe_snapshot_path(snapshot, "logs/errors.json")
    except ValueError as error:
        errors.append(f"logs/errors.json 路径不安全：{error}")
        errors_log = None
    if errors_log is None or not errors_log.is_file():
        errors.append("缺少 logs/errors.json")
    else:
        try:
            logged_errors = load_json(errors_log)
            if logged_errors != []:
                errors.append("logs/errors.json 不是空数组")
        except Exception as error:
            errors.append(f"logs/errors.json 无法解析：{error}")

    try:
        checksum_path = safe_snapshot_path(snapshot, "checksums.sha256")
    except ValueError as error:
        errors.append(f"checksums.sha256 路径不安全：{error}")
        checksum_path = None
    declared_paths: set[str] = set()
    if checksum_path is None or not checksum_path.is_file():
        errors.append("缺少 checksums.sha256")
    else:
        try:
            for expected, relative in parse_checksums(checksum_path):
                declared_paths.add(relative)
                target = safe_snapshot_path(snapshot, relative)
                if not target.is_file():
                    errors.append(f"缺少校验文件：{relative}")
                    continue
                actual = sha256_file(target)
                checked_files += 1
                if actual != expected:
                    errors.append(f"SHA-256 不匹配：{relative}")
        except Exception as error:
            errors.append(f"校验清单失败：{error}")

    try:
        receipt_path = safe_snapshot_path(snapshot, "COMPLETED.json")
    except ValueError as error:
        errors.append(f"COMPLETED.json 路径不安全：{error}")
        receipt_path = None
    if receipt_path is None or not receipt_path.is_file():
        errors.append("缺少 COMPLETED.json 发布回执")
    else:
        try:
            receipt = load_json(receipt_path)
            if receipt.get("snapshot_id") != manifest.get("snapshot_id"):
                errors.append("COMPLETED.json snapshot_id 不匹配")
            if receipt.get("manifest_sha256") != sha256_file(manifest_path):
                errors.append("manifest.json 哈希与发布回执不匹配")
            if checksum_path is None or not checksum_path.is_file() or receipt.get("checksums_sha256") != sha256_file(checksum_path):
                errors.append("checksums.sha256 哈希与发布回执不匹配")
        except Exception as error:
            errors.append(f"发布回执校验失败：{error}")

    actual_payloads = scanned_regular_files - SUPPLEMENT_CONTROL_FILES
    for relative in sorted(actual_payloads - declared_paths):
        errors.append(f"存在未登记文件：{relative}")
    for relative in sorted(declared_paths - actual_payloads):
        errors.append(f"校验清单引用不存在文件：{relative}")

    attachment_source_ids: set[int] = set()
    for entity, metadata in sorted(entities.items()):
        relative = str(metadata.get("file") or f"entities/{entity}.jsonl")
        try:
            target = safe_snapshot_path(snapshot, relative)
        except ValueError as error:
            errors.append(f"实体文件路径不安全：{relative}（{error}）")
            continue
        if not target.is_file():
            errors.append(f"缺少实体文件：{relative}")
            continue
        expected_hash = metadata.get("sha256")
        if expected_hash and sha256_file(target) != expected_hash:
            errors.append(f"manifest 实体哈希不匹配：{relative}")
        counts = {"active": 0, "retired": 0}
        previous = {False: 0, True: 0}
        source_ids: set[int] = set()
        try:
            for line_number, record in iter_jsonl(target):
                source = record.get("source") or {}
                payload = record.get("record") or {}
                state = record.get("state")
                if state not in {"active", "retired"}:
                    raise ValueError(f"{relative}:{line_number} state 无效：{state}")
                retired = state == "retired"
                source_id = source.get("id")
                if not isinstance(source_id, int) or source_id <= 0:
                    raise ValueError(f"{relative}:{line_number} 缺少有效 source id")
                if source.get("type") != entity or payload.get("id") != source_id:
                    raise ValueError(f"{relative}:{line_number} source envelope 与 record 不一致")
                if source_id <= previous[retired]:
                    raise ValueError(f"{relative}:{line_number} source id 未严格递增")
                if source_id in source_ids:
                    raise ValueError(f"{relative}:{line_number} source id 跨状态重复：{source_id}")
                previous[retired] = source_id
                source_ids.add(source_id)
                if entity == "Attachment":
                    attachment_source_ids.add(source_id)
                counts["retired" if retired else "active"] += 1
                checked_records += 1
        except Exception as error:
            errors.append(str(error))
            continue
        for key in ("active", "retired"):
            if counts[key] != int(metadata.get(key, -1)):
                errors.append(
                    f"{entity} {key} 数量不匹配：manifest={metadata.get(key)} actual={counts[key]}"
                )
        link_relative = str(metadata.get("link_file") or f"links/{entity}.jsonl")
        try:
            link_path = safe_snapshot_path(snapshot, link_relative)
        except ValueError as error:
            errors.append(f"关系索引路径不安全：{link_relative}（{error}）")
            continue
        if not link_path.is_file():
            errors.append(f"缺少关系索引：{link_relative}")
        else:
            expected_hash = metadata.get("link_sha256")
            if expected_hash and sha256_file(link_path) != expected_hash:
                errors.append(f"关系索引哈希不匹配：{link_relative}")
            actual_links = 0
            try:
                for line_number, link in iter_jsonl(link_path):
                    source = link.get("source") or {}
                    target_link = link.get("target") or {}
                    if source.get("type") != entity or not isinstance(source.get("id"), int):
                        raise ValueError(f"{link_relative}:{line_number} source 无效")
                    if source.get("id") not in source_ids:
                        raise ValueError(f"{link_relative}:{line_number} source 是孤儿记录")
                    if not target_link.get("type") or not isinstance(target_link.get("id"), int):
                        raise ValueError(f"{link_relative}:{line_number} target 无效")
                    if not link.get("field") or not isinstance(link.get("ordinal"), int):
                        raise ValueError(f"{link_relative}:{line_number} field/ordinal 无效")
                    actual_links += 1
                    checked_links += 1
            except Exception as error:
                errors.append(str(error))
            if actual_links != int(metadata.get("link_count", -1)):
                errors.append(
                    f"{entity} link 数量不匹配：manifest={metadata.get('link_count')} actual={actual_links}"
                )

    try:
        attachment_index = safe_snapshot_path(snapshot, "attachments/index.json")
    except ValueError as error:
        errors.append(f"附件索引路径不安全：{error}")
        attachment_index = None
    expected_downloaded = int((manifest.get("attachments") or {}).get("downloaded", 0))
    if expected_downloaded and (attachment_index is None or not attachment_index.is_file()):
        errors.append("manifest 声明有附件，但缺少 attachments/index.json")
    elif attachment_index is not None and attachment_index.is_file():
        try:
            index = load_json(attachment_index)
            if not isinstance(index, list):
                raise ValueError("附件索引不是 JSON array")
            seen_ids: set[int] = set()
            for item in index:
                attachment_id = int(item["attachment_id"])
                if attachment_id in seen_ids:
                    raise ValueError(f"附件索引重复 id：{attachment_id}")
                seen_ids.add(attachment_id)
                if attachment_id not in attachment_source_ids:
                    raise ValueError(f"附件索引引用不存在的 Attachment：{attachment_id}")
                target = safe_snapshot_path(snapshot, "attachments/" + str(item["file"]))
                if not target.is_file():
                    raise ValueError(f"缺少附件文件：{item['file']}")
                if target.stat().st_size != int(item["size"]):
                    raise ValueError(f"附件大小不匹配：{item['file']}")
                if sha256_file(target) != item["sha256"]:
                    raise ValueError(f"附件哈希不匹配：{item['file']}")
                checked_attachments += 1
            if len(index) != expected_downloaded:
                errors.append(
                    f"附件数量不匹配：manifest={expected_downloaded} actual={len(index)}"
                )
        except Exception as error:
            errors.append(f"附件校验失败：{error}")

    media_metadata = manifest.get("media") or {}
    try:
        media_index_path = safe_snapshot_path(snapshot, "media/index.json")
    except ValueError as error:
        errors.append(f"媒体索引路径不安全：{error}")
        media_index_path = None
    expected_media = int(media_metadata.get("downloaded", 0))
    if expected_media and (media_index_path is None or not media_index_path.is_file()):
        errors.append("manifest 声明有媒体，但缺少 media/index.json")
    elif media_index_path is not None and media_index_path.is_file():
        try:
            media_index = load_json(media_index_path)
            if not isinstance(media_index, list):
                raise ValueError("媒体索引不是 JSON array")
            seen_media: set[tuple[str, int, str]] = set()
            for item in media_index:
                source = item.get("source") or {}
                key = (str(source.get("type")), int(source.get("id")), str(item.get("field")))
                if key in seen_media:
                    raise ValueError(f"媒体索引重复：{key}")
                seen_media.add(key)
                relative = str(item.get("file", ""))
                layout_error = _media_layout_error(relative, item)
                if layout_error:
                    warnings.append(f"legacy base media 路径未分层：{relative}（{layout_error}）")
                target = safe_snapshot_path(snapshot, relative)
                if not target.is_file():
                    raise ValueError(f"缺少媒体文件：{relative}")
                if target.stat().st_size != int(item["size"]):
                    raise ValueError(f"媒体大小不匹配：{relative}")
                if sha256_file(target) != item["sha256"]:
                    raise ValueError(f"媒体哈希不匹配：{relative}")
                checked_media_files += 1
                checked_media_bytes += target.stat().st_size
            if len(media_index) != expected_media:
                errors.append(
                    f"媒体数量不匹配：manifest={expected_media} actual={len(media_index)}"
                )
            if int(media_metadata.get("failed", 0)) != 0:
                errors.append("manifest.media.failed 不为 0")
        except Exception as error:
            errors.append(f"媒体校验失败：{error}")

    if manifest.get("consistency") != "transactional":
        warnings.append("ShotGrid API 不提供跨实体事务快照；恢复前需按事件日志和业务抽样复核")
    result: dict[str, Any] = {
        "ok": not errors,
        "snapshot": str(snapshot),
        "snapshot_id": manifest.get("snapshot_id"),
        "format": manifest.get("format"),
        "schema_version": manifest.get("schema_version"),
        "profile": completeness.get("profile"),
        "checked_files": checked_files,
        "checked_records": checked_records,
        "checked_links": checked_links,
        "checked_attachments": checked_attachments,
        "checked_media_files": checked_media_files,
        "checked_media_bytes": checked_media_bytes,
        "errors": errors,
        "warnings": warnings,
    }
    if media_supplement is None:
        result["media_payload_scope"] = manifest.get("payload_scope") or {
            "hosted": "api_downloadable_only",
            "external": "not_verified",
        }
        result["media_payload_coverage"] = {
            "hosted": "api_downloadable_only",
            "external": "not_verified",
        }
        result["media_stats"] = {
            "hosted": {
                "copied": 0,
                "downloaded": int(media_metadata.get("downloaded", 0)),
                "resumed": 0,
                "reused": 0,
                "failed": int(media_metadata.get("failed", 0)),
                "files": checked_media_files,
                "bytes": checked_media_bytes,
                "acquisitions": {"base_snapshot": checked_media_files},
            },
            "external": {
                "copied": 0,
                "downloaded": 0,
                "resumed": 0,
                "reused": 0,
                "failed": 0,
                "files": 0,
                "bytes": 0,
                "acquisitions": {},
            },
            "files": checked_media_files,
            "bytes": checked_media_bytes,
        }
        result["transfer_stats"] = {}
        warnings.append(
            "未提供已验证的媒体补充包；base 快照即使为 site_full，"
            "也不能证明 PublishedFile/LocalStorage 等 external media payload 完整"
        )
        if require_all_media:
            errors.append("--require-all-media 要求同时提供 --media-supplement")
    elif errors:
        result["media_supplement"] = {
            "ok": False,
            "skipped": True,
            "errors": ["base snapshot 校验失败，未继续校验媒体补充包"],
            "warnings": [],
        }
    else:
        supplement_result = _verify_supplement_contract(
            media_supplement,
            base_snapshot=snapshot,
            base_manifest=manifest,
            require_complete=require_full,
            require_all_media=require_all_media,
        )
        result["media_supplement"] = supplement_result
        result["media_payload_scope"] = supplement_result.get("media_payload_scope") or {}
        result["media_payload_coverage"] = supplement_result.get("media_payload_coverage") or {}
        result["media_stats"] = supplement_result.get("media_stats") or {}
        result["transfer_stats"] = supplement_result.get("transfer_stats") or {}
        errors.extend(
            f"媒体补充包：{error}" for error in (supplement_result.get("errors") or [])
        )
        warnings.extend(
            f"媒体补充包：{warning}" for warning in (supplement_result.get("warnings") or [])
        )
    result["ok"] = not errors
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="离线校验 ShotGrid 本地快照")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--verify", action="store_true", help="逐文件、逐记录校验完整性")
    parser.add_argument("--require-full", action="store_true", help="要求完整站点 profile；提供补充包时也要求补充包完整")
    parser.add_argument("--media-supplement", type=Path, help="可选媒体补充包目录；先校验 base 再校验补充包及 lineage")
    parser.add_argument("--require-all-media", action="store_true", help="硬性要求完整站点 base 及 hosted/external media 全覆盖")
    parser.add_argument("--output", type=Path, help="JSON 报告输出；默认打印到 stdout")
    args = parser.parse_args()
    try:
        result = verify_snapshot(
            args.snapshot,
            require_full=args.require_full,
            media_supplement=args.media_supplement,
            require_all_media=args.require_all_media,
        )
        if args.output:
            atomic_json(args.output.resolve(), result)
            print(f"报告已写入：{args.output.resolve()}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("ok", True) else 1
    except Exception as error:
        print(f"快照校验失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
