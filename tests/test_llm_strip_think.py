"""web/llm.py thinking 兜底:剥 <think>...</think>(非流式 + 流式增量)。

背景:qwen3 等 thinking 变体经 LM Studio / Ollama / vLLM openai-compat 会把推理链
以 <think> 块塞进 content。流式版要处理 tag 跨 chunk 断开的情况。
"""
from __future__ import annotations

from life_lens.web.llm import _strip_think, _strip_think_stream


# ---------- 非流式 ----------

def test_strip_plain_text_untouched():
    assert _strip_think('{"action": "search_photos"}') == '{"action": "search_photos"}'


def test_strip_think_block_before_json():
    raw = "<think>用户想找人物照片,应该用 search_photos</think>\n{\"action\": \"x\"}"
    assert _strip_think(raw) == '{"action": "x"}'


def test_strip_unclosed_think_drops_tail():
    # 被 max_tokens 截断:<think> 没闭合 → 之后全丢,不能把推理链当回答
    raw = "前文<think>推理到一半被截"
    assert _strip_think(raw) == "前文"


def test_strip_multiple_blocks():
    raw = "<think>a</think>答案一<think>b</think>答案二"
    assert _strip_think(raw) == "答案一答案二"


# ---------- 流式 ----------

def _run(chunks):
    return "".join(_strip_think_stream(chunks))


def test_stream_passthrough():
    assert _run(["你好", ",世界"]) == "你好,世界"


def test_stream_think_block_filtered():
    assert _run(["<think>推理", "推理</think>", "答案"]) == "答案"


def test_stream_tag_split_across_chunks():
    # 开/闭 tag 都被 chunk 边界切开
    assert _run(["<th", "ink>x</th", "ink>真答案"]) == "真答案"


def test_stream_unclosed_think_yields_nothing():
    assert _run(["<think>只有推理没答案"]) == ""


def test_stream_partial_open_tag_at_end_not_lost():
    # 尾部像半个 tag 但其实不是 → 收尾要放出来
    assert _run(["答案<th"]) == "答案<th"


def test_stream_text_around_block():
    assert _run(["前", "<think>", "中", "</think>", "后"]) == "前后"
