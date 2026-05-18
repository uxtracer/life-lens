"""回填 photo_embeddings 表。可重复跑(text_hash 命中 → 跳过)。

用法:
    python scripts/build_embeddings.py            # 增量(默认,跳过未变的)
    python scripts/build_embeddings.py --force    # 全量重 embed(换模型/调 source_text 后)
    python scripts/build_embeddings.py --limit 100   # 只跑前 100 张(调试)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from life_lens.embed import Embedder, build_source_text, text_hash
from life_lens.store import db


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("build_embeddings")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path.home() / ".life_lens")
    ap.add_argument("--force", action="store_true", help="忽略 text_hash 全量重 embed")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 张")
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()

    conn = db.connect(db.get_db_path(args.root))
    db.init_schema(conn)

    rows = conn.execute(
        """
        SELECT photo_id, vision, people FROM photos
        WHERE source != 'seed' AND vision IS NOT NULL
        ORDER BY photo_id
        """
    ).fetchall()
    log.info("vision 已完成: %d 张", len(rows))

    existing = dict(conn.execute(
        "SELECT photo_id, text_hash FROM photo_embeddings"
    ).fetchall())

    todo: list[tuple[str, str, str]] = []   # (photo_id, source_text, hash)
    skipped_empty = 0
    skipped_same = 0
    for r in rows:
        try:
            vision = json.loads(r["vision"]) if r["vision"] else None
            people = json.loads(r["people"]) if r["people"] else None
        except Exception:
            skipped_empty += 1
            continue
        text = build_source_text(vision, people)
        if not text:
            skipped_empty += 1
            continue
        h = text_hash(text)
        if not args.force and existing.get(r["photo_id"]) == h:
            skipped_same += 1
            continue
        todo.append((r["photo_id"], text, h))

    if args.limit:
        todo = todo[: args.limit]

    log.info("待 embed: %d 张 (跳过 同 hash %d / 空文本 %d)", len(todo), skipped_same, skipped_empty)

    if not todo:
        log.info("全部已是最新,无需重跑")
        conn.close()
        return

    emb = Embedder()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    t0 = time.time()
    for i in range(0, len(todo), args.batch):
        chunk = todo[i : i + args.batch]
        texts = [t for _, t, _ in chunk]
        vecs = emb.embed(texts)

        rows_to_write = [
            (pid, emb.model, emb.dim, vec.tobytes(), h, now)
            for (pid, _, h), vec in zip(chunk, vecs)
        ]
        conn.executemany(
            """
            INSERT INTO photo_embeddings (photo_id, model, dim, vec, text_hash, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(photo_id) DO UPDATE SET
                model=excluded.model, dim=excluded.dim, vec=excluded.vec,
                text_hash=excluded.text_hash, updated_at=excluded.updated_at
            """,
            rows_to_write,
        )

        done_so_far = min(i + args.batch, len(todo))
        if (i // args.batch) % 5 == 0 or done_so_far == len(todo):
            elapsed = time.time() - t0
            rate = done_so_far / elapsed if elapsed > 0 else 0
            log.info("  进度 %d/%d  %.1f 张/s", done_so_far, len(todo), rate)

    log.info("done in %.1fs", time.time() - t0)
    conn.close()


if __name__ == "__main__":
    main()
