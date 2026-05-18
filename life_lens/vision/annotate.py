"""set-of-mark 标注:在 JPEG 上每张脸画红色方框 + [N] 编号标签。

VLM 看到带标注的图,能直接 grounding 到"红框 [1] 里的脸"对应 prompt 里的"[1]=张三",
比"画面左上, 大"这种文字方位描述精确得多。
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """跨平台找一个能画数字/中文的字体。失败 fallback 到 PIL 默认。"""
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",                       # mac 中文
        "/System/Library/Fonts/Helvetica.ttc",                      # mac 英文
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",     # linux
        "C:/Windows/Fonts/msyh.ttc",                                # windows 中文
        "C:/Windows/Fonts/Arial.ttf",                               # windows
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    log.warning("找不到 truetype 字体,fallback 到 PIL default(字会很小)")
    return ImageFont.load_default()


def annotate_faces(
    jpeg_bytes: bytes,
    bboxes: list[tuple[float, float, float, float]],
) -> bytes:
    """每个 bbox 画一个红框 + 1-based 编号 [N]。返回新 JPEG bytes。

    bboxes 的顺序就是编号顺序(调用方负责把顺序和 face_items 对齐)。
    """
    if not bboxes:
        return jpeg_bytes
    im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    draw = ImageDraw.Draw(im)
    w, h = im.size

    box_thickness = max(2, w // 300)
    font_size     = max(22, h // 25)
    font          = _load_font(font_size)
    pad           = max(2, font_size // 5)

    red = (255, 30, 30)
    white = (255, 255, 255)

    for i, bbox in enumerate(bboxes, start=1):
        x, y, bw, bh = bbox
        x2, y2 = x + bw, y + bh
        # 红色脸框
        draw.rectangle([x, y, x2, y2], outline=red, width=box_thickness)
        # 编号标签
        label = f"[{i}]"
        try:
            tb = draw.textbbox((0, 0), label, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
        except AttributeError:
            tw, th = font.getsize(label)
        # 优先放框上方;上方空间不够就放进框内顶部
        bg_x0 = x
        bg_y0 = y - th - 2 * pad
        bg_x1 = x + tw + 2 * pad
        bg_y1 = y
        if bg_y0 < 0:
            bg_y0 = y
            bg_y1 = y + th + 2 * pad
        draw.rectangle([bg_x0, bg_y0, bg_x1, bg_y1], fill=red)
        draw.text((bg_x0 + pad, bg_y0 + pad), label, fill=white, font=font)

    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()
