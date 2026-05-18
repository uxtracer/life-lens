"""长边 ≤ N 像素的等比缩放。"""
from __future__ import annotations

from PIL import Image

MAX_LONG_EDGE = 1024


def resize_long_edge(im: Image.Image, max_long: int = MAX_LONG_EDGE) -> Image.Image:
    w, h = im.size
    long = max(w, h)
    if long <= max_long:
        return im
    scale = max_long / float(long)
    new_w = int(round(w * scale))
    new_h = int(round(h * scale))
    return im.resize((new_w, new_h), Image.LANCZOS)
