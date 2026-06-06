"""文本 LLM 调用抽象 — **只支持 OpenAI 兼容 RESTful API**(DeepSeek / OpenAI / Together / 任何 `/v1/chat/completions`)。

v0.4 起 **删除** `kind="claude-p"`(本地 subprocess `claude -p`)— 它依赖本地装 Claude Code,只对作者
方便,开源用户大概率没装;DeepSeek v4-flash 等便宜模型对相册 chat 场景完全够用。

配置 `~/.life_lens/config.json`:

    {
      "llm": {
        "default": "deepseek",
        "providers": {
          "deepseek": {
            "kind": "openai-compat",
            "model": "deepseek-v4-flash",
            "label": "DeepSeek Chat (便宜+中文好)",
            "api_key": "sk-...",
            "base_url": "https://api.deepseek.com/v1"
          },
          "openai": { ... }
        }
      }
    }

旧格式(单 provider)仍兼容:`"llm": { "provider": "openai-compat", "model": "...", "api_key": "...", "base_url": "..." }`。

**本地 server(LM Studio / Ollama / vLLM)注意**:
- provider 可加 `"extra_body": {...}`,原样并进 request body。典型用途:关 thinking —
  `"extra_body": {"chat_template_kwargs": {"enable_thinking": false}}`(vLLM / 部分 server 约定)。
- thinking 模型把推理链以 `<think>...</think>` 塞进 content 的,本层自动剥掉(`_strip_think*`),
  但推理 token 照样耗时间,**强烈建议在 server 侧关掉 think 模式**。
- SSE 响应头不带 charset 时 requests 默认按 latin-1 解,中文全花 — 本层已强制 `r.encoding="utf-8"`。

**迁移**:若旧 config 含 `kind="claude-p"` 的 provider,启动时**自动跳过 + 日志 WARN**,
让用户改填 RESTful API key。CHANGELOG v0.4 有说明。

vision(Ollama qwen3-vl)不进这个抽象。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Iterable, Iterator, Optional

import requests

from ..store import config as cfg_store

log = logging.getLogger(__name__)


_warned_claude_p = False


def _llm_block() -> dict:
    """读 config.json 的 llm 块。"""
    return cfg_store.load_config().get("llm") or {}


def _list_providers() -> tuple[dict[str, dict], str]:
    """归一化:无论新旧格式,都返回 ({id: provider_cfg, ...}, default_id)。

    新格式 {default, providers: {...}} → 直接返回(自动过滤 kind="claude-p" 旧 provider)。
    旧格式 {provider, model, api_key, base_url} → 包装成单 provider id="default"。
    都没有 → 返回 ({}, "")。
    """
    global _warned_claude_p
    blk = _llm_block()
    # 新格式
    if "providers" in blk:
        raw = blk["providers"] or {}
        # 过滤 claude-p 旧 provider(已废弃)
        filtered = {}
        skipped = []
        for pid, p in raw.items():
            if (p or {}).get("kind") == "claude-p":
                skipped.append(pid)
                continue
            filtered[pid] = p
        if skipped and not _warned_claude_p:
            log.warning(
                "config.json 含已废弃的 claude-p provider %s,已自动忽略。v0.4 起 LLM 只支持 "
                "RESTful API(OpenAI 兼容),请改用 DeepSeek/OpenAI key — 详见 CHANGELOG。",
                skipped,
            )
            _warned_claude_p = True
        default_id = blk.get("default")
        if default_id not in filtered:
            default_id = next(iter(filtered), "")
        return filtered, default_id

    # 旧格式(单 provider)
    if "provider" in blk:
        kind = blk["provider"]
        if kind == "claude-p":
            if not _warned_claude_p:
                log.warning(
                    "config.json 用了旧单 provider 格式 + kind=claude-p,已废弃。请改成新格式 "
                    "providers map + openai-compat。"
                )
                _warned_claude_p = True
            return {}, ""
        single = {
            "kind":     kind,
            "model":    blk.get("model"),
            "api_key":  blk.get("api_key"),
            "base_url": blk.get("base_url"),
            "timeout":  blk.get("timeout"),
            "label":    blk.get("model") or kind,
        }
        return {"default": single}, "default"

    # 啥也没配
    return {}, ""


def _resolve(provider_id: Optional[str]) -> tuple[str, dict]:
    """根据 provider_id 拿配置;None 走 default。环境变量可覆盖单值。"""
    providers, default_id = _list_providers()
    if not providers:
        raise RuntimeError(
            "尚未配置 LLM provider。到 Web UI 配置页 → LLM 文本模型 卡片,填 api_key + base_url + model,"
            "推荐 DeepSeek v4-flash(https://platform.deepseek.com/)。"
        )
    pid = provider_id or default_id
    cfg = dict(providers.get(pid) or providers.get(default_id) or {})
    # env 覆盖(仅对当前选中的 provider)
    if os.environ.get("LIFE_LENS_LLM_MODEL"):
        cfg["model"] = os.environ["LIFE_LENS_LLM_MODEL"]
    if os.environ.get("LIFE_LENS_LLM_API_KEY"):
        cfg["api_key"] = os.environ["LIFE_LENS_LLM_API_KEY"]
    if os.environ.get("LIFE_LENS_LLM_BASE_URL"):
        cfg["base_url"] = os.environ["LIFE_LENS_LLM_BASE_URL"]
    return pid, cfg


def list_providers_public() -> dict:
    """给前端的 provider 列表(去掉 api_key 等敏感字段)。"""
    providers, default_id = _list_providers()
    return {
        "providers": [
            {
                "id":    pid,
                "label": p.get("label") or p.get("model") or pid,
                "kind":  p.get("kind"),
                "model": p.get("model"),
                "base_url": p.get("base_url"),
                "has_key": bool(p.get("api_key")),
            }
            for pid, p in providers.items()
        ],
        "default": default_id,
    }


def get_provider_info(provider_id: Optional[str] = None) -> dict:
    """单个 provider 的 public 描述(给 chat 页头部 / debug)。"""
    providers, default_id = _list_providers()
    if not providers:
        return {"id": "", "kind": None, "model": None, "label": "(未配置)", "base_url": None}
    pid = provider_id or default_id
    cfg = providers.get(pid) or providers.get(default_id) or {}
    return {
        "id":       pid,
        "kind":     cfg.get("kind"),
        "model":    cfg.get("model"),
        "label":    cfg.get("label") or cfg.get("model") or pid,
        "base_url": cfg.get("base_url"),
    }


# ============================================================
# thinking 模型兜底:剥 <think>...</think>
# ============================================================
# qwen3 等 thinking 变体经 openai-compat(LM Studio / Ollama / vLLM)会把推理链
# 以 <think>...</think> 塞进 content。不剥的话:Round 1 JSON 被推理文本环绕
# (_extract_json 多半还能救),Round 2 流式会把整段推理直接吐给用户。
# 注意这只是兜底 — 推理 token 照样花时间,根治是 server 侧关 think(见模块 docstring)。

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _strip_think(text: str) -> str:
    """非流式版:去掉 <think>...</think> 块;没闭合(被截断)时丢掉 <think> 之后全部。"""
    out = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    i = out.find(_THINK_OPEN)
    if i >= 0:
        out = out[:i]
    return out.lstrip("\n")


def _strip_think_stream(chunks: Iterable[str]) -> Iterator[str]:
    """流式版:增量过滤 <think> 块,tag 跨 chunk 断开也能识别(尾部留半个 tag 待定)。"""
    pend = ""           # 未决文本(可能以半个 tag 结尾)
    in_think = False
    for chunk in chunks:
        pend += chunk
        out = ""
        while pend:
            if in_think:
                i = pend.find(_THINK_CLOSE)
                if i < 0:
                    # 整段都在思考块里,只留可能是半个闭 tag 的尾巴
                    pend = pend[-(len(_THINK_CLOSE) - 1):]
                    break
                pend = pend[i + len(_THINK_CLOSE):]
                in_think = False
            else:
                i = pend.find(_THINK_OPEN)
                if i < 0:
                    # 尾部若是半个开 tag 则留住待下个 chunk,其余放行
                    keep = 0
                    for k in range(min(len(_THINK_OPEN) - 1, len(pend)), 0, -1):
                        if pend.endswith(_THINK_OPEN[:k]):
                            keep = k
                            break
                    if keep:
                        out += pend[:-keep]
                        pend = pend[-keep:]
                    else:
                        out += pend
                        pend = ""
                    break
                out += pend[:i]
                pend = pend[i + len(_THINK_OPEN):]
                in_think = True
        if out:
            yield out
    if pend and not in_think:
        yield pend


# ============================================================
# Public API
# ============================================================

def call_llm(system: str, user: str, stream: bool = False,
             temperature: Optional[float] = None,
             provider_id: Optional[str] = None) -> Iterator[str]:
    """统一文本 LLM 调用。返回 stdout 文本片段的迭代器。

    Args:
        system:      system prompt
        user:        user message
        stream:      True 时流式 yield 多次,False 时整段 yield 一次
        temperature: 0-1,None 走 provider 默认
        provider_id: 指定 provider(对应 config.llm.providers 的 key)。None 走 default

    Raises:
        RuntimeError: provider 不可用或调用失败
    """
    pid, cfg = _resolve(provider_id)
    kind = cfg.get("kind", "openai-compat")
    if kind != "openai-compat":
        raise RuntimeError(
            f"未知 LLM kind: {kind!r}。v0.4 起只支持 'openai-compat'(RESTful API)。"
            f"请到 Web 配置页改 provider {pid!r}。"
        )
    raw = _openai_compat(system, user, cfg, stream=stream, temperature=temperature)
    if stream:
        yield from _strip_think_stream(raw)
    else:
        yield _strip_think("".join(raw))


# ============================================================
# Provider: OpenAI-compatible HTTP
# ============================================================

def _openai_compat(system: str, user: str, cfg: dict, stream: bool,
                   temperature: Optional[float]) -> Iterator[str]:
    api_key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "openai-compat 缺 api_key(到 Web 配置页 → LLM 文本模型 填,或 env OPENAI_API_KEY)"
        )
    base_url = (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model = cfg.get("model") or "gpt-4o-mini"
    timeout = float(cfg.get("timeout") or 120)

    body: dict = {
        "model":    model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "stream":   stream,
    }
    if temperature is not None:
        body["temperature"] = temperature
    # provider 级透传(本地 server 关 thinking 等):
    # "extra_body": {"chat_template_kwargs": {"enable_thinking": false}}
    extra = cfg.get("extra_body")
    if isinstance(extra, dict):
        body.update(extra)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    url = f"{base_url}/chat/completions"

    if not stream:
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"openai-compat call failed: {type(e).__name__}: {e}")
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"openai-compat empty response: {data}")
        yield (choices[0].get("message") or {}).get("content") or ""
        return

    # stream=True:SSE,每行 `data: {json}` 或 `data: [DONE]`
    try:
        r = requests.post(url, headers=headers, json=body, stream=True, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"openai-compat stream call failed: {type(e).__name__}: {e}")

    # SSE 永远是 UTF-8,但响应头不带 charset 时 requests 按 RFC 默认 latin-1 解,
    # 中文全花(LM Studio 等本地 server 就不带)。强制 utf-8。
    r.encoding = "utf-8"
    for raw_line in r.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        if raw_line.startswith(":"):       # SSE comment / keep-alive
            continue
        if not raw_line.startswith("data:"):
            continue
        payload = raw_line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = (choices[0].get("delta") or {}).get("content")
        if delta:
            yield delta
