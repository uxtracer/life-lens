"""预处理缓存:.cache/preprocessed/{photo_id}.jpg

第一次预处理:解码 → resize → JPEG q=85 → 写盘
重复调用:文件已在直接返回路径
"""
from __future__ import annotations

from pathlib import Path
from PIL import Image

from .decode import decode_to_rgb
from .resize import resize_long_edge, MAX_LONG_EDGE

JPEG_QUALITY = 85


def cache_dir(root: Path) -> Path:
    d = root / ".cache" / "preprocessed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_path(root: Path, photo_id: str) -> Path:
    return cache_dir(root) / f"{photo_id}.jpg"


def ensure_preprocessed(
    root: Path,
    photo_id: str,
    original_path: Path,
    max_long: int = MAX_LONG_EDGE,
    quality: int = JPEG_QUALITY,
) -> Path:
    """如果缓存里没有,跑解码+缩放+JPEG 编码;返回缓存文件路径。"""
    out = cache_path(root, photo_id)
    if out.exists() and out.stat().st_size > 0:
        return out
    im = decode_to_rgb(original_path)
    im = resize_long_edge(im, max_long)
    tmp = out.with_suffix(".jpg.tmp")
    im.save(tmp, "JPEG", quality=quality, optimize=True, progressive=True)
    tmp.replace(out)
    return out


def prune(root: Path, keep_ids: set[str]) -> int:
    """删除不在 keep_ids 中的缓存文件。返回删除数。"""
    d = cache_dir(root)
    removed = 0
    for p in d.glob("*.jpg"):
        if p.stem not in keep_ids:
            p.unlink()
            removed += 1
    return removed
