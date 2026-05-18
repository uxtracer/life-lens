"""统一 ~/.life_lens/config.json 读写 — 给 GUI Settings 页和老 _load_config 调用者复用。

设计:
- **原子写**:tempfile → os.replace,避免半写状态(掉电 / Ctrl+C 时不会留半个 json 文件)
- **chmod 600**:含 amap_key + api_key,设私有
- **保留未知字段**:用户可能写 `_comment` 之类教学注释,`update_*` 只改单字段不覆盖
- **utf-8 显式 encoding**:Windows 上 Python 默认 cp1252/gbk 遇到中文 label 会静默吞掉
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def get_config_path() -> Path:
    """config.json 实际路径。
    优先 env `LIFE_LENS_ROOT`(CLI / web server 启动时 export),否则用默认 ~/.life_lens。
    支持单进程并行多 root(开发同时跑私有 + 公开两份)。
    """
    root_env = os.environ.get("LIFE_LENS_ROOT")
    if root_env:
        return Path(root_env).expanduser() / "config.json"
    return Path.home() / ".life_lens" / "config.json"


def load_config() -> dict:
    """读 config.json,失败返回 {}。"""
    path = get_config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning("config.json 解析失败: %s,fallback 到 {}", e)
        return {}


def save_config(d: dict) -> None:
    """原子写 config.json,chmod 600。"""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".config_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError as e:
            log.warning("chmod 600 config.json 失败: %s(继续)", e)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def update_amap_key(key: str) -> None:
    """只改 amap_key 一项,保留其他所有字段(包括 _comment)。"""
    d = load_config()
    d["amap_key"] = (key or "").strip()
    save_config(d)


def update_llm_provider(provider_id: str, cfg: dict) -> None:
    """加 / 覆盖一个 provider。

    cfg 必须含:`kind="openai-compat"` + `model` + `api_key` + `base_url`,可选 `label`。
    其他 provider 不变;default 若为空且这是第一个,自动设为这个。
    """
    d = load_config()
    llm = d.setdefault("llm", {})
    providers = llm.setdefault("providers", {})
    providers[provider_id] = cfg
    if not llm.get("default"):
        llm["default"] = provider_id
    save_config(d)


def remove_llm_provider(provider_id: str) -> None:
    """删一个 provider;若它是 default,把 default 改成剩下的第一个或 None。"""
    d = load_config()
    llm = d.get("llm") or {}
    providers = llm.get("providers") or {}
    if provider_id not in providers:
        return
    del providers[provider_id]
    if llm.get("default") == provider_id:
        llm["default"] = next(iter(providers), None)
    save_config(d)


def set_llm_default(provider_id: str) -> None:
    """切默认 provider。如果 provider_id 不在 providers 里,raise。"""
    d = load_config()
    llm = d.setdefault("llm", {})
    providers = llm.get("providers") or {}
    if provider_id not in providers:
        raise ValueError(f"provider {provider_id!r} 不存在(已有: {list(providers)})")
    llm["default"] = provider_id
    save_config(d)


def update_vision_config(endpoint: str, model: str) -> None:
    """更新本地视觉模型配置(Ollama endpoint + 模型名)。

    只一组配置(不像 LLM 那样多 provider)。endpoint 是 host 根(如 http://localhost:11434),
    code 端拼 /api/generate 或 /api/tags。
    """
    d = load_config()
    d["vision"] = {
        "endpoint": (endpoint or "").strip(),
        "model": (model or "").strip(),
    }
    save_config(d)
