"""PhotoRecord 数据结构。对应 PLAN.md 中 JSON Schema v1 的 6 个 group。

刻意用 dict 风格而非 dataclass — JSON 字段会持续迭代,松散结构更友好。
此模块只提供:
    - SCHEMA_VERSION 常量
    - 工厂函数 new_record()
    - merge 工具(增量更新某个 group 不丢其他 group)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA_VERSION = "0.1"

# group 列表(顶层 6 个,稳定)
GROUPS = ("identity", "exif", "vision", "people", "derived", "meta")


def new_record(
    photo_id: str,
    source: str,
    source_ref: str,
    original_path: str,
    content_hash: str,
    file_size_bytes: int,
    original_format: str,
    sidecar_path: Optional[str] = None,
    preprocessed_path: Optional[str] = None,
) -> dict:
    """创建一个空的 PhotoRecord 骨架,只填 identity 和 meta。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": {
            "photo_id":          photo_id,
            "source":            source,
            "source_ref":        source_ref,
            "original_path":     original_path,
            "content_hash":      content_hash,
            "file_size_bytes":   file_size_bytes,
            "original_format":   original_format,
            "sidecar_path":      sidecar_path,
            "preprocessed_path": preprocessed_path,
        },
        "exif":    None,
        "vision":  None,
        "people":  None,
        "derived": None,
        "meta": {
            "processed_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "group_versions": {},
            "source_signals": {},
            "errors":         [],
        },
    }


def stamp_group_version(record: dict, group: str, version: str) -> None:
    record.setdefault("meta", {}).setdefault("group_versions", {})[group] = version
    record["meta"]["processed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_error(record: dict, group: str, err: str) -> None:
    record.setdefault("meta", {}).setdefault("errors", []).append(
        {"group": group, "error": err, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    )
