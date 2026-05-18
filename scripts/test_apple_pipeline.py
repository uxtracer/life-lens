"""smoke test:Apple Photos source → 完整 process_one() → 把结果 dump 出来。

不动主库(用 /tmp 临时 db),不动 sources 表,直接调用 process_one() 跑 1-2 张样本,
验证:
  1. source.iter_faces() 走通,跳过 InsightFace
  2. faces / persons 表正确写入
  3. vision 拿到真名 face_items,description 里有真名
  4. people.persons[] 用 'apple:<name>' cluster_id 组装
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pillow_heif import register_heif_opener

from life_lens.scanner.pipeline import process_one
from life_lens.sources.photos_library import ApplePhotosSource
from life_lens.store.db import connect, init_schema
from life_lens.vision.ollama import OllamaVision

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("test")

LIB = str(Path.home() / "Pictures" / "Photos Library.photoslibrary")
# 之前探查确认过的 4 张 sample(每种 orientation 一张),都已知 face/bbox 正确
SAMPLE_UUIDS = [
    "0009079B-CF68-44B6-B4E6-22785CA970E0",  # orient=3,横向,2 张脸
    "00256EFD-451C-417D-B5B6-6AA7777B844B",  # orient=1,横向,4 张脸
    "0030B69D-3CA6-43BA-9BF2-2AD2BE4BD650",  # orient=6,竖向 CW,2 张脸
    "06B68C5A-DE91-4B4B-8DCB-1C87D7716FF6",  # orient=8,竖向 CCW,2 张脸
]


def main() -> None:
    register_heif_opener()

    tmp_root = Path(tempfile.mkdtemp(prefix="life_lens_apple_test_"))
    log.info(f"临时 root: {tmp_root}")
    try:
        db_path = tmp_root / "lens.db"
        conn = connect(db_path)
        init_schema(conn)

        source = ApplePhotosSource(Path(LIB))
        vision = OllamaVision()

        # 把 SAMPLE_UUIDS 转成 PhotoRef
        refs = []
        for ref in source.iter_photos():
            if ref.source_ref in SAMPLE_UUIDS:
                refs.append(ref)
            if len(refs) == len(SAMPLE_UUIDS):
                break
        log.info(f"挑出 {len(refs)} 张 sample")

        for ref in refs:
            log.info(f"\n=== 处理 {ref.source_ref} ===")
            rec = process_one(
                root=tmp_root,
                source=source,
                ref=ref,
                vision=vision,
                vision_lock=None,
                enable_faces=True,
                faces_lock=None,
                db_conn=conn,
            )
            conn.commit()

            # 关键字段简报
            log.info(f"photo_id: {rec['identity']['photo_id']}")
            log.info(f"faces version: {rec['meta']['group_versions'].get('faces')}")
            people = rec.get("people", {})
            log.info(f"people.persons[]:")
            for pp in people.get("persons", []):
                log.info(f"  {pp}")
            log.info(f"people.names: {people.get('names')}")
            vision_g = rec.get("vision", {})
            desc = vision_g.get("description", "")
            log.info(f"vision.description: {desc[:200]}")
            log.info(f"vision.scene: {vision_g.get('scene')}")
            errors = rec.get("meta", {}).get("errors", [])
            if errors:
                log.warning(f"errors: {errors}")

        # 看 persons 表
        log.info("\n=== persons 表 ===")
        for row in conn.execute("SELECT cluster_id, name FROM persons ORDER BY cluster_id"):
            log.info(f"  {dict(row) if hasattr(row, 'keys') else row}")
        # 看 faces 表 face 数
        n = conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
        log.info(f"faces total: {n}")

        conn.close()
    finally:
        keep = "--keep" in sys.argv
        if keep:
            log.info(f"保留临时目录: {tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    main()
