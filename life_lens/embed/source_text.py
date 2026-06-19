"""从 photo record 的 vision/people 字段拼出用于 embedding 的中文文本。

拼接字段:description + scene + tags + objects + mood + actions
- description:主描述(LLM 60-180字),信息量大但关键词被稀释
- scene / tags / objects:精炼关键词(物品/活动名),对短 query "裙子→连衣裙" 这类匹配关键
- mood / actions:辅助

只用 vision 已稳定的字段(都是 LLM 写死的字符串),改名/补种子人物不影响 embedding —
要触发重 embed 需要 vision 重跑导致 description 变化。
"""
from __future__ import annotations

import hashlib


def build_source_text(vision: dict | None, people: dict | None = None) -> str:
    """vision/people JSON dict → 拼接的搜索文本。任一字段为 None/空时跳过。"""
    if not vision:
        return ""
    parts: list[str] = []
    desc = vision.get("description") or ""
    if desc:
        parts.append(desc)
    scene = vision.get("scene") or ""
    if scene:
        parts.append(scene)
    tags = vision.get("tags") or []
    if tags:
        parts.append(" ".join(t for t in tags if t))
    objects = vision.get("objects") or []
    if objects:
        parts.append(" ".join(o for o in objects if o))
    mood = vision.get("mood") or ""
    if mood:
        parts.append(mood)
    if people:
        persons = people.get("persons") or []
        actions = [p.get("action") for p in persons if p.get("action")]
        if actions:
            parts.append(" ".join(actions))
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()


def text_hash(s: str) -> str:
    """sha1[:16] 做 idempotent 增量 — text 没变就跳过重 embed。"""
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]
