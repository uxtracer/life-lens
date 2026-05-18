"""探活 Ollama 服务 — 给 web UI 配置页 / Settings 卡片用。

不引主 vision 模块依赖,只发一个 HTTP GET /api/tags。失败时给"装 Ollama 看官网"友好提示。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

def _default_endpoint() -> str:
    """每次调用都读 config(不缓存),避免改完 UI 还要重启。"""
    from .ollama import get_configured_host
    return get_configured_host()


def _default_vision_model() -> str:
    from .ollama import get_configured_model
    return get_configured_model()


# 静态默认(仅作为 fallback / 测试)— 真实调用走 _default_*()
DEFAULT_ENDPOINT = os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
DEFAULT_VISION_MODEL = "qwen3-vl:8b-instruct"

log = logging.getLogger(__name__)


def ping(endpoint: Optional[str] = None, timeout: float = 2.0) -> dict[str, Any]:
    """探 GET {endpoint}/api/tags。

    endpoint 不传则走 config.json vision.endpoint(再 fallback env / 默认)。

    返回:
      ok=True:  {ok, endpoint, models: [...], has_vision_model: bool, vision_model_name}
      ok=False: {ok, endpoint, error: "..."}
    """
    if endpoint is None:
        endpoint = _default_endpoint()
    vision_model = _default_vision_model()
    url = f"{endpoint.rstrip('/')}/api/tags"
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        models = [m.get("name", "") for m in (r.json().get("models") or [])]
        # 检查目标 vision 模型(精确匹配 + 容错前缀匹配)
        has_vision = any(
            m == vision_model or m.startswith(vision_model.split(":")[0] + ":")
            for m in models
        )
        return {
            "ok": True,
            "endpoint": endpoint,
            "models": models,
            "has_vision_model": has_vision,
            "vision_model_name": vision_model,
        }
    except requests.exceptions.ConnectionError:
        return {
            "ok": False,
            "endpoint": endpoint,
            "error": f"无法连接 Ollama({endpoint})。装 https://ollama.com/ 然后 terminal 跑 `ollama serve`",
        }
    except requests.exceptions.Timeout:
        return {"ok": False, "endpoint": endpoint, "error": f"timeout ({timeout}s)"}
    except Exception as e:
        return {"ok": False, "endpoint": endpoint, "error": f"{type(e).__name__}: {e}"}
