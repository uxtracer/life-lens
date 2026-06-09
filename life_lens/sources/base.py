"""PhotoSource 接口 + 数据类型。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Tuple


@dataclass(frozen=True)
class PhotoRef:
    """指向源中一张照片的轻量句柄,不携带图片字节。"""
    source_id: str            # 'photos_library' | 'fs:<root>'
    source_ref: str           # Apple uuid 或绝对路径
    original_path: Path       # 原图绝对路径(用于读 bytes / 计算 hash)
    original_format: str      # 'heic' | 'jpeg' | 'png' | ...


@dataclass
class SourceMetadata:
    """source 能免费提供的额外信号(不需要 LLM 也能拿到)。"""
    apple_uuid: Optional[str] = None
    apple_persons: list[str] = field(default_factory=list)
    apple_albums: list[str] = field(default_factory=list)
    apple_keywords: list[str] = field(default_factory=list)
    apple_favorite: bool = False
    apple_hidden: bool = False
    apple_place: Optional[str] = None
    # Apple 自己的拍摄时间(osxphotos p.date,权威、带时区)。文件 EXIF 常缺 DateTimeOriginal
    # (PNG/截图/老照片/iCloud 合并导入),此时用它兜底。格式与 exif/extract.py 一致。
    apple_captured_local: Optional[str] = None        # 'YYYY-MM-DDTHH:MM:SS'(本地,无 tz)
    apple_captured_utc: Optional[str] = None          # 'YYYY-MM-DDTHH:MM:SSZ'
    apple_tz_offset_minutes: Optional[int] = None


@dataclass(frozen=True)
class FaceFromSource:
    """source 自带的人脸数据(Apple Photos 等)。返回这个 = 跳过 InsightFace detect。

    cluster_id 由 source 自己决定稳定方案,比如:
      - 已命名: 'apple:<name>'(用真名做稳定 ID,后续即使改名也能 join 到正确 person)
      - 未命名: 'apple_face:<face_uuid>'(用 Apple 内部 face uuid)
    bbox 是 preprocessed image 的像素坐标 (x, y, w, h) top-left + 宽高。
    """
    cluster_id: str
    name: Optional[str]
    bbox: Tuple[float, float, float, float]


class PhotoSource(ABC):
    """所有数据源 adapter 的统一接口。"""

    source_id: str

    @abstractmethod
    def iter_photos(self) -> Iterator[PhotoRef]:
        """枚举源中所有照片。"""
        ...

    @abstractmethod
    def get_metadata(self, ref: PhotoRef) -> SourceMetadata:
        """拿到该照片的免费元信息。Filesystem source 返回空 metadata。"""
        ...

    def iter_faces(
        self, ref: PhotoRef, image_size: Tuple[int, int]
    ) -> Optional[list[FaceFromSource]]:
        """source 自带的人脸数据。返回 None = 走 InsightFace 路径;返回 list = 跳过 InsightFace。

        image_size 是 **preprocessed image**(长边 ≤ 1024)的 (W, H);source 负责把 bbox
        换算成对应的像素坐标。返回的 list 顺序就是 set-of-mark 编号顺序([1], [2], ...)。
        默认返回 None(filesystem 走原来的 detect + cluster 路径)。
        """
        return None

    def kind(self) -> str:
        """'filesystem' | 'photos_library'"""
        ...
