#!/usr/bin/env python3
"""Verify a portable ShotGrid snapshot and assess future restore readiness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from backup import atomic_json, sha256_file


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_snapshot_path(snapshot: Path, relative: str) -> Path:
    target = (snapshot / relative).resolve()
    root = snapshot.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"快照路径越界：{relative}")
    return target


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


def verify_snapshot(snapshot: Path, require_full: bool = False) -> dict[str, Any]:
    snapshot = snapshot.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checked_files = 0
    checked_records = 0
    checked_links = 0
    checked_attachments = 0
    if snapshot.name.endswith(".incomplete"):
        errors.append("目录以 .incomplete 结尾，不是已发布快照")
    manifest_path = snapshot / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "errors": ["缺少 manifest.json"], "warnings": []}
    try:
        manifest = load_json(manifest_path)
    except Exception as error:
        return {"ok": False, "errors": [f"manifest.json 无法解析：{error}"], "warnings": []}
    if manifest.get("format") != "shotgrid_portable_snapshot":
        errors.append("未知或旧版快照格式")
    if int(manifest.get("schema_version", 0)) < 3:
        errors.append("schema_version 低于 3，不满足当前恢复合同")
    if manifest.get("status") != "complete":
        errors.append(f"快照状态不是 complete：{manifest.get('status')}")
    if manifest.get("errors"):
        errors.append("manifest 包含备份错误")
    completeness = manifest.get("completeness") or {}
    if require_full and completeness.get("profile") != "site_full":
        errors.append("该快照不是 site_full profile")

    entity_schema_path = snapshot / "schema/entities.json"
    readable_entities: set[str] = set()
    if not entity_schema_path.is_file():
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
    if completeness.get("profile") == "site_full" and readable_entities != planned_entities:
        errors.append(
            "site_full 的 readable/planned 实体集合不一致："
            f"missing={sorted(readable_entities - planned_entities)} "
            f"unexpected={sorted(planned_entities - readable_entities)}"
        )

    errors_log = snapshot / "logs/errors.json"
    if not errors_log.is_file():
        errors.append("缺少 logs/errors.json")
    else:
        try:
            logged_errors = load_json(errors_log)
            if logged_errors != []:
                errors.append("logs/errors.json 不是空数组")
        except Exception as error:
            errors.append(f"logs/errors.json 无法解析：{error}")

    checksum_path = snapshot / "checksums.sha256"
    declared_paths: set[str] = set()
    if not checksum_path.is_file():
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

    receipt_path = snapshot / "COMPLETED.json"
    if not receipt_path.is_file():
        errors.append("缺少 COMPLETED.json 发布回执")
    else:
        try:
            receipt = load_json(receipt_path)
            if receipt.get("snapshot_id") != manifest.get("snapshot_id"):
                errors.append("COMPLETED.json snapshot_id 不匹配")
            if receipt.get("manifest_sha256") != sha256_file(manifest_path):
                errors.append("manifest.json 哈希与发布回执不匹配")
            if not checksum_path.is_file() or receipt.get("checksums_sha256") != sha256_file(checksum_path):
                errors.append("checksums.sha256 哈希与发布回执不匹配")
        except Exception as error:
            errors.append(f"发布回执校验失败：{error}")

    actual_payloads = {
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", "checksums.sha256", "COMPLETED.json"}
    }
    for relative in sorted(actual_payloads - declared_paths):
        errors.append(f"存在未登记文件：{relative}")
    for relative in sorted(declared_paths - actual_payloads):
        errors.append(f"校验清单引用不存在文件：{relative}")

    attachment_source_ids: set[int] = set()
    for entity, metadata in sorted(entities.items()):
        relative = str(metadata.get("file") or f"entities/{entity}.jsonl")
        target = safe_snapshot_path(snapshot, relative)
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
        link_path = safe_snapshot_path(snapshot, link_relative)
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

    attachment_index = snapshot / "attachments/index.json"
    expected_downloaded = int((manifest.get("attachments") or {}).get("downloaded", 0))
    if expected_downloaded and not attachment_index.is_file():
        errors.append("manifest 声明有附件，但缺少 attachments/index.json")
    elif attachment_index.is_file():
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
    media_index_path = snapshot / "media/index.json"
    expected_media = int(media_metadata.get("downloaded", 0))
    if expected_media and not media_index_path.is_file():
        errors.append("manifest 声明有媒体，但缺少 media/index.json")
    elif media_index_path.is_file():
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
                target = safe_snapshot_path(snapshot, relative)
                if not target.is_file():
                    raise ValueError(f"缺少媒体文件：{relative}")
                if target.stat().st_size != int(item["size"]):
                    raise ValueError(f"媒体大小不匹配：{relative}")
                if sha256_file(target) != item["sha256"]:
                    raise ValueError(f"媒体哈希不匹配：{relative}")
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
    return {
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
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="离线校验 ShotGrid 本地快照")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--verify", action="store_true", help="逐文件、逐记录校验完整性")
    parser.add_argument("--require-full", action="store_true", help="要求 site_full 完整站点 profile")
    parser.add_argument("--output", type=Path, help="JSON 报告输出；默认打印到 stdout")
    args = parser.parse_args()
    try:
        result = verify_snapshot(args.snapshot, require_full=args.require_full)
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
