"""增量人脸聚类(max-pooling)。

新脸 embedding → 与 db 中所有现有 face embedding 一一算余弦 → 按 cluster_id 取 max
→ 最高分 cluster 如果 max >= 阈值则归入该 cluster,否则新建 cluster。

为什么是 max-pooling 而不是 mean centroid:
- 跨年龄/跨光照:孩子小时候 + 长大后的种子混在一个 cluster,平均会拉成"四不像";
  保留每张作为独立判据,新脸总能匹到最像的那张种子。
- 种子机制天然支持:种子 = 提前往 faces 表填好的 embedding 行,无需新表/新逻辑。

L2 归一化的 embedding 之间余弦相似度 = 点积。
buffalo_l 同人余弦相似度通常 > 0.5,不同人 < 0.3。阈值 0.5 是经验起点。
"""
from __future__ import annotations

import logging
import sqlite3
import uuid

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.5


def assign(
    conn: sqlite3.Connection,
    embedding: np.ndarray,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[str, bool]:
    """把一张脸 assign 到一个 cluster。返回 (cluster_id, is_new)。

    embedding 必须是 L2 归一化的 (512,) float32。
    """
    cluster_ids, mat = _load_all_face_embeddings(conn)
    if mat is not None and len(cluster_ids) > 0:
        sims = mat @ embedding                            # (N_faces,) 余弦
        # 按 cluster 取 max
        best_per_cluster: dict[str, float] = {}
        for cid, s in zip(cluster_ids, sims):
            cur = best_per_cluster.get(cid)
            if cur is None or s > cur:
                best_per_cluster[cid] = float(s)
        if best_per_cluster:
            best_cid, best_sim = max(best_per_cluster.items(), key=lambda kv: kv[1])
            if best_sim >= threshold:
                return best_cid, False
    # 新建
    new_id = f"c_{uuid.uuid4().hex[:10]}"
    return new_id, True


def _load_all_face_embeddings(conn: sqlite3.Connection):
    """从 faces 表加载所有已分配 cluster 的脸 embedding,返回 (cluster_ids[], (N,512) matrix | None)。"""
    rows = conn.execute(
        "SELECT cluster_id, embedding FROM faces WHERE cluster_id IS NOT NULL"
    ).fetchall()
    if not rows:
        return [], None
    cluster_ids: list[str] = []
    embs: list[np.ndarray] = []
    for r in rows:
        cid = r["cluster_id"] if isinstance(r, sqlite3.Row) else r[0]
        blob = r["embedding"] if isinstance(r, sqlite3.Row) else r[1]
        cluster_ids.append(cid)
        embs.append(np.frombuffer(blob, dtype=np.float32))
    return cluster_ids, np.stack(embs)


def display_name(conn: sqlite3.Connection, cluster_id: str) -> str:
    """cluster_id → 已命名的名字(persons 表);未命名则返回 cluster_id 本身作 fallback。"""
    row = conn.execute("SELECT name FROM persons WHERE cluster_id = ?", (cluster_id,)).fetchone()
    if row and row[0]:
        return row[0]
    return cluster_id
