"""fastembed 薄包装。bge-small-zh-v1.5 中文检索 SOTA,512 维 float32。

模型权重首次 init 下到 `<root>/.cache/fastembed/`(默认 ~/.life_lens/.cache/fastembed/),
后续 offline。**不要用 fastembed 默认 cache_dir** —— 0.7.4 默认落在 tempfile.gettempdir(),
macOS 是 /var/folders/.../T/,会被系统定期清,导致每隔几天重新下 ~95MB(踩过)。
fastembed 内部输出已 L2 normalized,所以 SemanticIndex 不需要再 normalize。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_DIM = 512


def _cache_dir() -> str:
    """持久模型缓存:<root>/.cache/fastembed/(root 同 store.config,env LIFE_LENS_ROOT 可改)。"""
    root_env = os.environ.get("LIFE_LENS_ROOT")
    root = Path(root_env).expanduser() if root_env else Path.home() / ".life_lens"
    d = root / ".cache" / "fastembed"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


class Embedder:
    """单 process 复用一个实例(模型加载 ~3s,常驻内存 ~200MB)。"""

    def __init__(self, model_id: str = DEFAULT_MODEL):
        from fastembed import TextEmbedding
        log.info("loading embedder: %s", model_id)
        self._m = TextEmbedding(model_id, cache_dir=_cache_dir())
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
