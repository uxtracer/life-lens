"""fastembed 薄包装。bge-small-zh-v1.5 中文检索 SOTA,512 维 float32。

模型权重首次 init 自动下到 ~/.cache/fastembed/,后续 offline。
fastembed 内部输出已 L2 normalized,所以 SemanticIndex 不需要再 normalize。
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_DIM = 512


class Embedder:
    """单 process 复用一个实例(模型加载 ~3s,常驻内存 ~200MB)。"""

    def __init__(self, model_id: str = DEFAULT_MODEL):
        from fastembed import TextEmbedding
        log.info("loading embedder: %s", model_id)
        self._m = TextEmbedding(model_id)
        self.model_id = model_id
        self.model = model_id.split("/")[-1]   # 'bge-small-zh-v1.5'
        self.dim = DEFAULT_DIM

    def embed(self, texts: list[str]) -> np.ndarray:
        """返回 (N, dim) float32,L2 normalized。"""
        return np.array(list(self._m.embed(texts)), dtype=np.float32)

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]


_singleton: Optional[Embedder] = None


def get_embedder() -> Embedder:
    """进程内懒加载 singleton。Web server 首次 chat 时 init,之后常驻。"""
    global _singleton
    if _singleton is None:
        _singleton = Embedder()
    return _singleton
