#!/usr/bin/env python3
"""Inventory readable ShotGrid data without exporting business record values."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from backup import DEFAULT_ENTITIES, atomic_json, connect, entity_supports_retirement, retry


MEDIA_TYPES = {"image", "url"}


def schema_value(metadata: dict[str, Any], key: str, default: Any = None) -> Any:
    value = metadata.get(key, default)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def count_records(
    sg: Any,
    entity: str,
    attempts: int,
    retired_only: bool = False,
    field: str | None = None,
) -> int:
    total = 0
    page_size = 5000
    filters = [[field, "is_not", None]] if field else []
    last_id = 0
    while True:
        rows = retry(
            lambda: sg.find(
                entity,
                [*filters, ["id", "greater_than", last_id]],
                ["id"],
                order=[{"field_name": "id", "direction": "asc"}],
                limit=page_size,
                page=1,
                retired_only=retired_only,
                include_archived_projects=True,
            ),
            attempts,
            f"统计 {entity}{'.' + field if field else ''}",
        )
        total += len(rows)
        if len(rows) < page_size:
            return total
        next_id = max(int(row["id"]) for row in rows)
        if next_id <= last_id:
            raise RuntimeError(f"{entity} 统计游标没有前进")
        last_id = next_id


def count_active_records(sg: Any, entity: str, attempts: int) -> int:
    """Use ShotGrid's aggregate endpoint when possible, then fall back safely."""
    try:
        result = retry(
            lambda: sg.summarize(
                entity,
                [],
                [{"field": "id", "type": "record_count"}],
                include_archived_projects=True,
            ),
            attempts,
            f"汇总 {entity}",
        )
        return int(result["summaries"]["id"])
    except Exception:
        return count_records(sg, entity, attempts)


def audit_entity(sg: Any, entity: str, attempts: int) -> dict[str, Any]:
    fields = retry(lambda: sg.schema_field_read(entity), attempts, f"读取 {entity} schema")
    field_types: dict[str, int] = {}
    link_targets: set[str] = set()
    media_fields: list[dict[str, Any]] = []
    editable_fields = 0
    for field_name, metadata in fields.items():
        data_type = str(schema_value(metadata, "data_type", "unknown"))
        field_types[data_type] = field_types.get(data_type, 0) + 1
        if bool(schema_value(metadata, "editable", False)):
            editable_fields += 1
        properties = metadata.get("properties") or {}
        valid_types = schema_value(properties, "valid_types", []) or []
        if data_type in {"entity", "multi_entity"}:
            link_targets.update(str(value) for value in valid_types)
        if data_type in MEDIA_TYPES or field_name in {"image", "filmstrip_image"}:
            item: dict[str, Any] = {"field": field_name, "data_type": data_type}
            try:
                item["active_nonempty"] = count_records(sg, entity, attempts, field=field_name)
            except Exception as error:
                item["count_error"] = type(error).__name__ + ": " + str(error)[:300]
            media_fields.append(item)

    result: dict[str, Any] = {
        "default_backup": entity in DEFAULT_ENTITIES,
        "field_count": len(fields),
        "editable_field_count": editable_fields,
        "field_types": dict(sorted(field_types.items())),
        "link_target_types": sorted(link_targets),
        "media_fields": media_fields,
    }
    result["active_count"] = count_active_records(sg, entity, attempts)
    try:
        result["retirement_supported"] = entity_supports_retirement(sg, entity)
        result["retired_count"] = (
            count_records(sg, entity, attempts, retired_only=True)
            if result["retirement_supported"] else 0
        )
    except Exception as error:
        result["retired_count_error"] = type(error).__name__ + ": " + str(error)[:300]
    return result


def run_audit(sg: Any, output: Path, attempts: int, selected: list[str] | None) -> dict[str, Any]:
    started = dt.datetime.now(dt.timezone.utc)
    server_info = retry(sg.info, attempts, "读取服务信息")
    entity_schema = retry(sg.schema_entity_read, attempts, "读取实体 schema")
    if selected:
        entities = sorted(set(selected))
    else:
        entities = sorted(entity_schema)
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": started.isoformat(),
        "site": os.environ.get("SHOTGRID_URL", ""),
        "server_version": server_info.get("full_version") or server_info.get("version"),
        "privacy": "counts_and_schema_only",
        "default_entities": DEFAULT_ENTITIES,
        "entities": {},
        "errors": [],
    }
    for index, entity in enumerate(entities, start=1):
        try:
            result = audit_entity(sg, entity, attempts)
            report["entities"][entity] = result
            print(
                f"[{index}/{len(entities)}] {entity}: "
                f"active={result['active_count']} retired={result.get('retired_count', '?')}",
                file=sys.stderr,
            )
        except Exception as error:
            report["errors"].append({
                "entity": entity,
                "error": type(error).__name__ + ": " + str(error)[:300],
            })
            print(f"[{index}/{len(entities)}] {entity}: 无法统计 ({type(error).__name__})", file=sys.stderr)
    report["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    report["summary"] = {
        "schema_entities": len(entity_schema),
        "attempted_entities": len(entities),
        "readable_entities": len(report["entities"]),
        "unreadable_entities": len(report["errors"]),
        "nonempty_entities": sum(
            1
            for value in report["entities"].values()
            if value.get("active_count", 0) or value.get("retired_count", 0)
        ),
        "nondefault_nonempty_entities": sorted(
            entity
            for entity, value in report["entities"].items()
            if not value["default_backup"]
            and (value.get("active_count", 0) or value.get("retired_count", 0))
        ),
    }
    atomic_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="只用计数和 schema 探察 ShotGrid 备份覆盖")
    parser.add_argument("--output", type=Path, default=Path(".local/audits/latest.json"))
    parser.add_argument("--http-proxy", help="HTTP 代理，格式为 host:port")
    parser.add_argument("--entities", help="只探察逗号分隔的实体类型")
    parser.add_argument("--max-retries", type=int, default=2)
    args = parser.parse_args()
    try:
        selected = [item.strip() for item in args.entities.split(",") if item.strip()] if args.entities else None
        report = run_audit(connect(args.http_proxy), args.output.resolve(), args.max_retries, selected)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))
        print(f"审计报告：{args.output.resolve()}")
        return 0
    except Exception as error:
        print(f"审计失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
