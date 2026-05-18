"""语义向量 embedding(bge-small-zh-v1.5 via fastembed)。

设计:
- embedder 懒加载(fastembed 首次 init 要下载模型 ~95MB 到 ~/.cache/fastembed/)
- vec 存 photo_embeddings 表,(N, 512) float32 BLOB
- 查询时全加载内存,brute-force 余弦相似度(见 query/semantic.py)
- text_hash 做 idempotent 增量(source_text 没变 → 跳过)
"""
from __future__ import annotations

from .embedder import Embedder, get_embedder
from .source_text import build_source_text, text_hash

__all__ = ["Embedder", "get_embedder", "build_source_text", "text_hash"]
