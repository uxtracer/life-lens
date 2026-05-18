"""VisionModel ABC + result 数据类。

入参约定:**预处理后的 1024px JPEG 字节**(可能已被 annotate 画过红框 + 编号)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VisionResult:
    """视觉模型一次调用的返回。"""
    raw_text: str = ""
    parsed: Optional[dict] = None
    error: Optional[str] = None
    warnings: list = field(default_factory=list)
    latency_ms: int = 0


class VisionModel(ABC):
    """所有视觉模型 adapter 的统一接口(拆调用版 v9)。"""

    name: str       # "ollama:qwen3-vl:8b-instruct"
    description_version: str    # "ollama:qwen3-vl:8b@v9-desc-2026-05"
    struct_version: str         # "ollama:qwen3-vl:8b@v9-struct-2026-05"

    @abstractmethod
    def describe_description(
        self,
        jpeg_bytes: bytes,
        face_items: Optional[list[tuple[int, Optional[str]]]] = None,
    ) -> VisionResult:
        """Call 1 — 生成 description(80-200 字叙事段落,带 set-of-mark 强人名约束)。
        输入应该是 annotate 后的图(带红框 + 编号)。
        """
        ...

    @abstractmethod
    def describe_struct(
        self,
        jpeg_bytes: bytes,
        face_items: Optional[list[tuple[int, Optional[str]]]] = None,
    ) -> VisionResult:
        """Call 2 — 生成结构化字段(media_type/subject/scene/objects/tags/ocr_text/mood/actions)。
        actions 按 set-of-mark 编号顺序输出,Python 后端用 cluster_ids 组装成 people.persons[]。
        输入应该是 annotate 后的图(带红框 + 编号),以便 actions 数组能对齐编号。
        """
        ...
