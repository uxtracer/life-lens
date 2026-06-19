"""Ollama HTTP 视觉模型 adapter(拆调用版 v9)。

POST /api/generate(非 stream),images 字段传 base64 JPEG。`format=json` 让模型严格输出 JSON。
**重要**:options.num_ctx 必须显式设置 — Ollama 默认 num_ctx=2048 会静默截断 prompt
(图片 ~800 tokens + 我们的 prompt ~500-1000 tokens 经常超),所以一律设 16384 给余量。
qwen3-vl:8b 本身支持 256k context,16k 安全。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Optional

import requests

from .base import VisionModel, VisionResult
from .prompts import (
    build_description_prompt, build_struct_prompt,
    description_person_claims,
    normalize_description, normalize_struct,
    DESCRIPTION_PROMPT_VERSION, STRUCT_PROMPT_VERSION,
)

log = logging.getLogger(__name__)

DEFAULT_HOST     = "http://localhost:11434"
DEFAULT_ENDPOINT = DEFAULT_HOST + "/api/generate"
DEFAULT_TAGS_URL = DEFAULT_HOST + "/api/tags"
DEFAULT_MODEL    = "qwen3-vl:8b-instruct"
DEFAULT_TIMEOUT  = 180   # 秒
RETRIES          = 3
DEFAULT_NUM_CTX  = 16384


def get_configured_host() -> str:
    """Ollama host 根(用于拼 /api/generate / /api/tags)。
    优先级:env OLLAMA_HOST > config.json vision.endpoint > DEFAULT_HOST。
    """
    import os
    env = os.environ.get("OLLAMA_HOST")
    if env:
        return env.rstrip("/")
    try:
        from ..store import config as cfg
        v = cfg.load_config().get("vision") or {}
        if v.get("endpoint"):
            return v["endpoint"].rstrip("/")
    except Exception:
        pass
    return DEFAULT_HOST


def get_configured_model() -> str:
    """vision 模型名。config.json vision.model > DEFAULT_MODEL。"""
    try:
        from ..store import config as cfg
        v = cfg.load_config().get("vision") or {}
        if v.get("model"):
            return v["model"]
    except Exception:
        pass
    return DEFAULT_MODEL


def health_check(
    endpoint: str = DEFAULT_TAGS_URL,
    timeout: float = 5.0,
    expect_model: Optional[str] = None,
) -> tuple[bool, str]:
    """Phase B 启动前调一次。返回 (ok, msg)。不抛异常。

    expect_model 给定时,会检查 /api/tags 列表里是否包含(name 前缀匹配)。
    """
    try:
        r = requests.get(endpoint, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        models = [m.get("name", "") for m in (data.get("models") or [])]
        if expect_model:
            # 精确匹配 — 不要前缀容错。qwen3-vl:8b 是 thinking 变体,跟 :8b-instruct 不是
            # 同一个模型(format=json 下吐空 → 扫描卡第一张),前缀匹配会把它误判成"已就位"。
            hit = expect_model in models
            if not hit:
                return False, f"Ollama 在线,但未找到模型 {expect_model}(已有: {models[:5]})。请 `ollama pull {expect_model}`"
        return True, f"Ollama OK,{len(models)} 个本地模型"
    except requests.exceptions.RequestException as e:
        return False, f"Ollama 不通 ({type(e).__name__}: {e})"
    except Exception as e:
        return False, f"health_check 异常: {type(e).__name__}: {e}"


class OllamaVision(VisionModel):
    def __init__(
        self,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        num_ctx: int = DEFAULT_NUM_CTX,
    ):
        # 未传则走 config.json + env(get_configured_*),走默认。
        # endpoint 这里要的是完整 generate URL,host + /api/generate
        self.model = model or get_configured_model()
        if endpoint is None:
            endpoint = get_configured_host() + "/api/generate"
        self.endpoint = endpoint
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.name = f"ollama:{self.model}"
        self.description_version = f"{self.name}@{DESCRIPTION_PROMPT_VERSION}"
        self.struct_version      = f"{self.name}@{STRUCT_PROMPT_VERSION}"

    def describe_description(
        self,
        jpeg_bytes: bytes,
        face_items: Optional[list[tuple[int, Optional[str]]]] = None,
        subject_hint: Optional[str] = None,
    ) -> VisionResult:
        prompt = build_description_prompt(face_items, subject_hint=subject_hint)
        result = self._call(jpeg_bytes, prompt)
        if result.parsed:
            normalized, warns = normalize_description(result.parsed)
            result.parsed = normalized
            result.warnings = (result.warnings or []) + warns
        # 关键不变量不能只靠prompt:struct确认无人风景且未检测到脸时,
        # description出现明确人物词→本地纠错重写一次;仍失败则拒收。
        if (
            subject_hint == "landscape"
            and not face_items
            and result.parsed
            and description_person_claims(result.parsed.get("description", ""))
        ):
            correction = (
                prompt
                + "\n纠错:上一版违反了无人风景约束。重新从原图写description,只写自然景物和可见环境,"
                  "绝对不要出现任何人物、人影或人物动作。只输出JSON。"
            )
            retry = self._call(jpeg_bytes, correction)
            if retry.parsed:
                normalized, warns = normalize_description(retry.parsed)
                retry.parsed = normalized
                retry.warnings = (retry.warnings or []) + warns
            claims = description_person_claims(
                (retry.parsed or {}).get("description", "")
            )
            if claims:
                retry.parsed = None
                retry.error = "landscape_person_hallucination_after_retry"
                retry.warnings = (retry.warnings or []) + [
                    "无人风景description两次出现人物词,已拒收"
                ]
            return retry
        return result

    def describe_struct(
        self,
        jpeg_bytes: bytes,
        face_items: Optional[list[tuple[int, Optional[str]]]] = None,
    ) -> VisionResult:
        prompt = build_struct_prompt(face_items)
        result = self._call(jpeg_bytes, prompt)
        if result.parsed:
            expected = len(face_items) if face_items else 0
            normalized, warns = normalize_struct(result.parsed, expected_actions_count=expected)
            result.parsed = normalized
            result.warnings = (result.warnings or []) + warns
        return result

    # ---------- 共享 HTTP 调用 ----------

    def _call(self, jpeg_bytes: bytes, prompt: str) -> VisionResult:
        b64 = base64.b64encode(jpeg_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.2,
                "num_predict": 1024,
                "num_ctx": self.num_ctx,
            },
        }

        last_err: Optional[str] = None
        for attempt in range(1, RETRIES + 1):
            t0 = time.monotonic()
            try:
                r = requests.post(self.endpoint, json=payload, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                raw = data.get("response", "")
                latency_ms = int((time.monotonic() - t0) * 1000)
                parsed, err = _extract_json(raw)
                if parsed is not None:
                    return VisionResult(raw_text=raw, parsed=parsed, error=None, latency_ms=latency_ms)
                return VisionResult(raw_text=raw, parsed=None, error=f"json_parse_failed: {err}", latency_ms=latency_ms)
            except requests.exceptions.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
                log.warning("ollama call attempt %d/%d failed: %s", attempt, RETRIES, last_err)
                if attempt < RETRIES:
                    time.sleep(2 ** attempt)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                log.exception("ollama unexpected error")
                break

        return VisionResult(raw_text="", parsed=None, error=last_err or "unknown", latency_ms=0)


# ---------- JSON 容错解析 ----------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _extract_json(text: str) -> tuple[Optional[dict], Optional[str]]:
    if not text:
        return None, "empty_response"
    s = text.strip()
    try:
        return json.loads(s), None
    except Exception:
        pass
    s2 = _FENCE.sub("", s).strip()
    try:
        return json.loads(s2), None
    except Exception:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        candidate = s[i : j + 1]
        try:
            return json.loads(candidate), None
        except Exception as e:
            return None, f"brace_extract_failed: {e}"
    return None, "no_json_object_found"
