"""内存 brute-force 余弦相似度索引 + Reciprocal Rank Fusion 合并工具。

设计:
- 全加载 photo_embeddings 到 numpy matrix(15w 张 × 512 维 × 4B = 300MB,Mac 完全 OK)
- query 时 matrix @ qvec ~50-100ms(15w 张),用户感知淹没在 LLM 那几秒里
- 简单 invalidation:按 photo_embeddings 行数变化时重建(够用,扫描中 chat 偶尔 race 不致命)
- 失败降级:fastembed 没装 / 表空 → 函数静默返 []
"""
from __future__ import annotations

import logging
import sqlite3
import threading

import numpy as np

log = logging.getLogger(__name__)


class SemanticIndex:
    def __init__(self, ids: list[str], matrix: np.ndarray | None):
        self.ids = ids
        self.matrix = matrix

    def search(self, query_vec: np.ndarray, k: int = 50) -> list[tuple[str, float]]:
        """返回 [(photo_id, similarity)],按余弦相似度降序。空索引返回 []。"""
        if self.matrix is None or len(self.ids) == 0:
            return []
        scores = self.matrix @ query_vec   # (N,)
        idx = np.argsort(-scores)[:k]
        return [(self.ids[i], float(scores[i])) for i in idx]


# 进程内单例(web server 启动后常驻)+ 简单 invalidation
_lock = threading.Lock()
_cache: dict = {"count": -1, "ids": None, "matrix": None}


def has_embeddings(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT EXISTS(SELECT 1 FROM photo_embeddings LIMIT 1)").fetchone()
    return bool(row[0])


def get_semantic_index(conn: sqlite3.Connection) -> SemanticIndex:
    """懒加载 + 简单 invalidation(按行数变化)。

    并发说明:同 process 多线程 chat 都用同一个矩阵(只读)是安全的。
    扫描中 chat 偶尔拿到旧版本(漏几张)不致命,下次 query 行数变化会重建。
    """
    cur_count = conn.execute("SELECT COUNT(*) FROM photo_embeddings").fetchone()[0]
    with _lock:
        if _cache["count"] != cur_count or _cache["ids"] is None:
            log.info("semantic index (re)building: %d vectors", cur_count)
            rows = conn.execute("SELECT photo_id, vec FROM photo_embeddings").fetchall()
            ids = [r["photo_id"] for r in rows]
            matrix = (
                np.stack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
                if rows else None
            )
            _cache["ids"] = ids
            _cache["matrix"] = matrix
            _cache["count"] = cur_count
        return SemanticIndex(_cache["ids"], _cache["matrix"])


def rrf_merge(
    list_a: list[dict],
    list_b: list[dict],
    k: int = 60,
    limit: int | None = None,
) -> list[dict]:
    """两路 RRF 合并,见 rrf_merge_multi。"""
    return rrf_merge_multi([list_a, list_b], k=k, limit=limit)


def rrf_merge_multi(
    lists: list[list[dict]],
    k: int = 60,
    limit: int | None = None,
) -> list[dict]:
    """N 路 Reciprocal Rank Fusion。

    item dict 必须含 'photo_id'。每个 list 是按相关性 ranked 的 items。
    同一 photo_id 出现 N 次时累加每个 list 的倒数排名权重(rank 0 起算)。
    k=60 是 Cormack et al. 论文经典值;无需 score 标定(rank-based)。

    用于多 query 扩词检索:主 query FTS / 主 query sem / 扩词1 FTS / 扩词1 sem / ...
    所有路径合并,某照片只要在多路径都靠前,合并分高。
    """
    scores: dict[str, float] = {}
    by_id: dict[str, dict] = {}
    for one_list in lists:
        for rank, item in enumerate(one_list):
            pid = item["photo_id"]
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (k + rank + 1)
            by_id.setdefault(pid, item)
    ordered = sorted(scores.items(), key=lambda x: -x[1])
    out = [by_id[pid] for pid, _ in ordered]
    return out[:limit] if limit else out
