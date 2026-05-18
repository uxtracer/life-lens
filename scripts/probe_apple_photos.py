"""探查 Apple Photos 给的人脸数据 — 跑一遍看字段长啥样,验证 bbox 坐标系。"""
from __future__ import annotations
import sys
from collections import Counter
from pathlib import Path

import osxphotos


LIB = str(Path.home() / "Pictures" / "Photos Library.photoslibrary")


def main() -> None:
    print(f"[loading] {LIB}")
    db = osxphotos.PhotosDB(dbfile=LIB)
    photos = db.photos()
    print(f"[stats] 总照片数: {len(photos)}")

    # 1. 整库统计 — 多少照片有脸 / 有命名
    with_faces = 0
    with_named = 0
    total_faces = 0
    person_counter: Counter[str] = Counter()
    for p in photos:
        if p.face_info:
            with_faces += 1
            total_faces += len(p.face_info)
        if p.persons:
            real = [n for n in p.persons if n and n != "_UNKNOWN_"]
            if real:
                with_named += 1
                for n in real:
                    person_counter[n] += 1

    print(f"[stats] 含脸照片: {with_faces} ({with_faces*100//max(len(photos),1)}%)")
    print(f"[stats] 含命名人物照片: {with_named}")
    print(f"[stats] 总脸数: {total_faces}")
    print(f"[stats] 不同已命名人物数: {len(person_counter)}")
    print(f"[stats] Top 10 命名人物:")
    for name, cnt in person_counter.most_common(10):
        print(f"        {name}: {cnt}")

    # 多 sample 验证 bbox 变换规律:跨 original_orientation / 横向 / 竖向
    print("\n[search] 收集多 sample(不同 orientation)...")
    by_orient: dict[int, list] = {}
    for p in photos:
        if not (p.face_info and p.path):
            continue
        real = [fi for fi in p.face_info if (fi.quality or -1) > 0 and fi.size > 0]
        named = [fi for fi in real if fi.name]
        if len(real) < 2 or not named:
            continue
        o = p.orientation or 1
        by_orient.setdefault(o, []).append(p)
    # 每种 orientation 挑 1 张,按 uuid 排序保证稳定
    samples = []
    for o in sorted(by_orient.keys()):
        plist = sorted(by_orient[o], key=lambda x: x.uuid)
        samples.append(plist[0])
    # 把已知 sample 排第一
    samples.sort(key=lambda x: 0 if x.uuid == "0009079B-CF68-44B6-B4E6-22785CA970E0" else 1)
    print(f"[search] 选了 {len(samples)} 张:")
    for s in samples:
        print(f"        {s.uuid}  orient={s.orientation}  orig_orient={s.original_orientation}  "
              f"{s.original_width}x{s.original_height}")
    sample = samples[0] if samples else None

    if sample is None:
        print("[search] 没找到符合条件的照片")
        return

    # 先 dump 第一张的字段,后面 render 循环跑所有 samples
    p = sample
    print(f"\n[sample] uuid={p.uuid}")
    print(f"[sample] path={p.path}")
    print(f"[sample] original_filename={p.original_filename}")
    print(f"[sample] date={p.date}")
    print(f"[sample] dimensions(orig)={p.original_width}x{p.original_height}")
    print(f"[sample] persons={p.persons}")
    print(f"[sample] person_info (count={len(p.person_info)}):")
    for pi in p.person_info:
        attrs = {}
        for a in dir(pi):
            if a.startswith("_"):
                continue
            try:
                v = getattr(pi, a)
                if callable(v):
                    continue
                attrs[a] = v
            except Exception as e:
                attrs[a] = f"<err: {e}>"
        print(f"  - person {pi.uuid}:")
        for k, v in sorted(attrs.items()):
            if k in ("photo", "photos", "face_info"):
                continue
            print(f"      {k}: {v!r}")
    print(f"[sample] face_info (count={len(p.face_info)}):")
    for fi in p.face_info:
        # 把 FaceInfo 的所有属性都 dump 一下,看坐标系
        attrs = {}
        for a in dir(fi):
            if a.startswith("_"):
                continue
            try:
                v = getattr(fi, a)
                if callable(v):
                    continue
                attrs[a] = v
            except Exception as e:
                attrs[a] = f"<err: {e}>"
        print(f"  - face {fi.uuid}:")
        for k, v in sorted(attrs.items()):
            if k in ("photo",):
                continue
            print(f"      {k}: {v!r}")
        print()

    # 3. 对所有 samples 各画 4 种 bbox 变换
    print("\n[render] 准备画红框验证 bbox(对每张 sample 画 4 种)...")
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[render] PIL 没装,跳过")
        return
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        print("[render] pillow-heif 没装,HEIC 会失败")

    out_dir = Path("/tmp/apple_photos_probe")
    out_dir.mkdir(exist_ok=True)

    # 4 种 normalized bbox 变换 (x_in, y_in, w_in, h_in) → (x_out, y_out, w_out, h_out)
    bbox_transforms = {
        "rot0":   lambda x, y, w, h: (x,     y,     w, h),
        "rot90":  lambda x, y, w, h: (1 - y, x,     h, w),
        "rot180": lambda x, y, w, h: (1 - x, 1 - y, w, h),
        "rot270": lambda x, y, w, h: (y,     1 - x, h, w),
    }

    for s in samples:
        src = Path(s.path) if s.path else None
        if not src or not src.exists():
            print(f"[render] {s.uuid} 文件不存在,跳过")
            continue
        real_faces = [fi for fi in s.face_info if (fi.quality or -1) > 0 and fi.size > 0]
        if not real_faces:
            print(f"[render] {s.uuid} 无真脸,跳过")
            continue
        raw = Image.open(src).convert("RGB")
        W, H = raw.size
        src_W = real_faces[0].source_width
        src_H = real_faces[0].source_height
        print(f"\n[render] {s.uuid}  orig_orient={s.original_orientation}  "
              f"raw={W}x{H}  src(sensor)={src_W}x{src_H}  真脸={len(real_faces)}")

        font = None
        for fp in [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/Library/Fonts/Arial.ttf",
        ]:
            try:
                font = ImageFont.truetype(fp, size=max(28, min(W, H) // 30))
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        for label, transform in bbox_transforms.items():
            canvas = raw.copy()
            draw = ImageDraw.Draw(canvas)
            for i, fi in enumerate(real_faces, 1):
                a = fi.mwg_rs_area
                nx, ny, nw, nh = transform(a.x, a.y, a.w, a.h)
                x, y = nx * W, ny * H
                w, h = nw * W, nh * H
                box = [x - w/2, y - h/2, x + w/2, y + h/2]
                draw.rectangle(box, outline=(255, 0, 0), width=max(4, min(W, H) // 300))
                text = f"[{i}] {fi.name}" if fi.name else f"[{i}]"
                tx, ty = box[0], max(0, box[1] - font.size - 4)
                draw.text((tx, ty), text, fill=(255, 0, 0), font=font)
            out_path = out_dir / f"orient{s.original_orientation}_{s.uuid[:8]}_{label}.jpg"
            canvas.thumbnail((1600, 1600))
            canvas.save(out_path, quality=88)
            print(f"  → {out_path.name}")

    print("\n[done] 多 sample 完成,对照 orientation 看哪个变换对得上")


if __name__ == "__main__":
    main()
