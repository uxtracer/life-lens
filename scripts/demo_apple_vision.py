"""端到端 demo:Apple Photos 的人脸 + 真名 → set-of-mark → qwen3-vl description。

验证目标:把 Apple bbox + 真名注入 vision prompt,看输出 description 是否用真名而不是
"左边的男士"这类方位代号。

不动数据库、不动 scanner pipeline,只在内存里跑通流程。
"""
from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

# 把项目根加到 sys.path,这样能 import life_lens.*
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import osxphotos
from PIL import Image
from pillow_heif import register_heif_opener

from life_lens.preprocess.resize import resize_long_edge
from life_lens.vision.annotate import annotate_faces
from life_lens.vision.ollama import OllamaVision

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("demo")

LIB = str(Path.home() / "Pictures" / "Photos Library.photoslibrary")


def transform_bbox(x: float, y: float, w: float, h: float, orient: int):
    """Apple sensor-space normalized bbox → display-space normalized bbox。

    orient 来自 PhotoInfo.original_orientation (EXIF orientation 1/3/6/8)。
    """
    if orient == 1:
        return (x,     y,     w, h)
    if orient == 3:
        return (1 - x, 1 - y, w, h)
    if orient == 6:
        return (1 - y, x,     h, w)
    if orient == 8:
        return (y,     1 - x, h, w)
    log.warning(f"未支持的 orientation={orient},按 1 处理")
    return (x, y, w, h)


def pick_sample(db: osxphotos.PhotosDB) -> osxphotos.PhotoInfo:
    """挑一张真脸 ≥ 2 + 至少一张已命名的合照。

    优先固定 uuid(orient=3 那张),fallback 自动找。
    """
    FIXED = "0009079B-CF68-44B6-B4E6-22785CA970E0"
    for p in db.photos():
        if p.uuid == FIXED:
            return p
    log.warning("固定 uuid 没找到,自动挑")
    for p in db.photos():
        if not (p.face_info and p.path):
            continue
        real = [fi for fi in p.face_info if (fi.quality or -1) > 0 and fi.size > 0]
        named = [fi for fi in real if fi.name]
        if len(real) >= 2 and named:
            return p
    raise RuntimeError("找不到合适样本")


def main() -> None:
    register_heif_opener()

    log.info(f"loading {LIB} ...")
    db = osxphotos.PhotosDB(dbfile=LIB)

    p = pick_sample(db)
    log.info(f"sample uuid={p.uuid}  orient={p.original_orientation}  "
             f"size={p.original_width}x{p.original_height}  date={p.date}")
    log.info(f"path={p.path}")

    real_faces = [fi for fi in p.face_info if (fi.quality or -1) > 0 and fi.size > 0]
    log.info(f"真脸 {len(real_faces)} / 总 {len(p.face_info)}")

    # 解码 + resize 长边 1024(跟主 pipeline 一致)
    im_raw = Image.open(p.path).convert("RGB")
    raw_W, raw_H = im_raw.size
    im = resize_long_edge(im_raw, max_long=1024)
    W, H = im.size
    log.info(f"raw {raw_W}x{raw_H} → preprocessed {W}x{H}")

    # 把每张真脸的 mwg bbox 按 orientation 转到 display 坐标,
    # 然后换算到 resize 后的像素 (x, y, w, h) top-left 格式给 annotate_faces
    bboxes_px: list[tuple[float, float, float, float]] = []
    face_items: list[tuple[int, str | None]] = []
    orient = p.original_orientation or 1
    for idx, fi in enumerate(real_faces, start=1):
        a = fi.mwg_rs_area
        cx, cy, ww, hh = transform_bbox(a.x, a.y, a.w, a.h, orient)
        # display 坐标系下:cx, cy 是 center;ww, hh 是 normalized 宽高
        px = (cx - ww / 2) * W
        py = (cy - hh / 2) * H
        pw = ww * W
        ph = hh * H
        bboxes_px.append((px, py, pw, ph))
        name = fi.name or None
        face_items.append((idx, name))
        log.info(f"  face [{idx}] name={name!r}  pixel bbox=({px:.0f},{py:.0f},{pw:.0f},{ph:.0f})")

    # 编码成 JPEG(annotate_faces 接 bytes)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    jpeg_bytes = buf.getvalue()

    # set-of-mark 画框
    annotated = annotate_faces(jpeg_bytes, bboxes_px)
    out_jpg = Path("/tmp/apple_photos_probe/demo_annotated.jpg")
    out_jpg.parent.mkdir(exist_ok=True)
    out_jpg.write_bytes(annotated)
    log.info(f"annotated → {out_jpg}")

    # 跑 vision describe_description
    log.info("calling qwen3-vl describe_description ...")
    vm = OllamaVision()
    res = vm.describe_description(annotated, face_items=face_items)
    log.info(f"latency={res.latency_ms}ms  error={res.error}")
    log.info(f"raw_text:\n{res.raw_text}")
    if res.parsed:
        log.info(f"parsed:\n{json.dumps(res.parsed, ensure_ascii=False, indent=2)}")

    # 也跑 struct 看 actions[] 顺序对不对
    log.info("calling qwen3-vl describe_struct ...")
    res2 = vm.describe_struct(annotated, face_items=face_items)
    log.info(f"latency={res2.latency_ms}ms  error={res2.error}")
    if res2.parsed:
        log.info(f"parsed:\n{json.dumps(res2.parsed, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
