"""统一把任意格式(HEIC/RAW/JPEG/PNG/...)解到 PIL.Image RGB。"""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    _HEIF_OK = True
except Exception:
    _HEIF_OK = False


def decode_to_rgb(path: Path) -> Image.Image:
    """打开图片并:① 处理 EXIF orientation 自动转向 ② 转 RGB(去掉 alpha)。"""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.load()
        return im


def heif_supported() -> bool:
    return _HEIF_OK
