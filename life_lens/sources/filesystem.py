"""任意目录递归扫描的 PhotoSource 实现。"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .base import PhotoRef, PhotoSource, SourceMetadata

# 支持的图片扩展名(小写)
IMAGE_EXTS = {
    "jpg", "jpeg", "png", "heic", "heif", "tiff", "tif", "webp", "bmp", "gif",
}


class FilesystemSource(PhotoSource):
    """递归扫描一个目录。"""

    kind_name = "filesystem"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.source_id = f"fs:{self.root}"

    def iter_photos(self) -> Iterator[PhotoRef]:
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            ext = p.suffix.lower().lstrip(".")
            if ext not in IMAGE_EXTS:
                continue
            yield PhotoRef(
                source_id=self.source_id,
                source_ref=str(p),
                original_path=p,
                original_format=ext,
            )

    def get_metadata(self, ref: PhotoRef) -> SourceMetadata:
        return SourceMetadata()

    def kind(self) -> str:
        return self.kind_name
