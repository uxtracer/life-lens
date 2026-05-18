"""Description vs persons.actions 自检 — 验证 description 没把人和动作的关系搞错。

设计:
  - struct LLM call 输出 actions dict (Python 已锁死红框编号 → action)
  - description LLM call 是自由文本,8B 模型容易把"X 的动作"写到 Y 名下
  - struct 的 (cluster_id, name, action) 是 ground truth,用它来 verify description

算法(和 tests/eval/run_eval.py 里的 _role_check 一致):
  对每个 (name, action) 对:
    1. 在 description 里找 name 所有出现位置
    2. 对每个位置,取后面 window 字符,截到下一个 person 的 name 出现前
    3. 检查该窗口是否含 action 的任一非停用字
    4. 任一位置命中 → ok;全部位置都 miss → mismatch

为什么只看"名字之后":中文叙事天然"名字 + 动作"语序("张三举手机自拍"),
不看前面避免别人的动作误关联(IMG_0685 当前 description "小明举手机自拍" 出现在
张三位置的窗口前面,如果双向扫会误判 hit)。
"""
from __future__ import annotations

from typing import Optional

# action 字符串里这些字不当关键词(语法/语气字)
_STOP_CHARS = set("的了着在和或与是面再看就也都还很")


def _is_cjk(c: str) -> bool:
    """是否是中日韩文字(粗略,够用)。"""
    if not c:
        return False
    code = ord(c)
    return 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF


def _extract_action_keywords(action: str) -> tuple[list[str], list[str]]:
    """从 action 短语提取检索关键词。返回 (bigrams, chars)。

    bigrams 是中文 2 字滑窗(最强命中信号 — "举手机自拍" → ["举手","手机","机自","自拍"])
    chars 是单字(去停用字)(fallback 命中需 ≥2 个不同字命中)
    英文 token 按 word 切,放进 chars(英文一个 word 已经足够特异)

    例:
      '蹲坐望镜头' → bigrams=['蹲坐','坐望','望镜','镜头'], chars=['蹲','坐','望','镜','头']
      '举手机自拍' → bigrams=['举手','手机','机自','自拍'], chars=['举','手','机','自','拍']
      'walking step' → bigrams=[], chars=['walking','step']
    """
    if not action:
        return [], []
    s = action.strip()
    # 中文 2 字滑窗(只保留连续 cjk 字符序列里的)
    bigrams: list[str] = []
    cjk_only = "".join(c for c in s if _is_cjk(c))
    for i in range(len(cjk_only) - 1):
        bigrams.append(cjk_only[i:i + 2])
    # 中文单字(去停用字)
    chars: list[str] = []
    for c in s:
        if _is_cjk(c) and c not in _STOP_CHARS:
            chars.append(c)
    # 英文 token(整词作 char,因为英文单 word 已足够 specific)
    for token in s.replace(",", " ").replace("，", " ").replace("。", " ").split():
        token = token.strip(".,;。,;").lower()
        if token and token.isascii() and token.isalnum() and len(token) >= 2:
            chars.append(token)
    # 去重保序
    bigrams = list(dict.fromkeys(bigrams))
    chars = list(dict.fromkeys(chars))
    return bigrams, chars


def check_description_vs_persons(
    description: str,
    persons: list[dict],
    window_chars: int = 30,
) -> list[str]:
    """对每个有名字 + action 的 person,检查 description 邻域是否命中 action 关键词。

    Args:
        description: vision.description 文本
        persons:     [{cluster_id, name, action}, ...](people.persons[])
        window_chars: 名字后多少字符算"邻域"

    Returns:
        不一致报告列表(空 list 表示一致)。每条形如:
          "小明 (action='蹲坐望镜头'): description 邻域未命中关键词 ['蹲','坐','望','镜','头']"
    """
    if not description or not persons:
        return []

    issues: list[str] = []
    # 取所有"有名字 + 有 action"的 person — 未命名/无动作的不参与
    named = [(p.get("name"), p.get("action") or "") for p in persons
             if p.get("name") and (p.get("action") or "").strip()]
    if not named:
        return []
    all_names = [n for n, _ in named]

    for name, action in named:
        bigrams, chars = _extract_action_keywords(action)
        if not bigrams and not chars:
            continue   # 没法提关键词,跳过

        if name not in description:
            issues.append(f"{name} (action={action!r}): description 中未出现该名字")
            continue

        # 在 description 里找 name 所有出现位置
        positions: list[int] = []
        start = 0
        while True:
            i = description.find(name, start)
            if i < 0:
                break
            positions.append(i)
            start = i + len(name)

        hit = False
        for pos in positions:
            tail_start = pos + len(name)
            tail = description[tail_start: tail_start + window_chars]
            # 截到下一个 person 的 name 出现位置之前
            cut = len(tail)
            for other_name in all_names:
                if other_name == name:
                    continue
                idx = tail.find(other_name)
                if 0 <= idx < cut:
                    cut = idx
            tail = tail[:cut]
            # 命中策略:bigram 任一命中 = 强信号(认 hit);否则单字 ≥2 个不同命中(防泛字单字误判,
            # 比如"手表"的"手"误命中"举手机自拍"的"手")
            if any(bg in tail for bg in bigrams):
                hit = True
                break
            char_matched = sum(1 for kw in chars if kw in tail)
            if char_matched >= 2:
                hit = True
                break

        if not hit:
            issues.append(
                f"{name} (action={action!r}): description 邻域(后 {window_chars} 字"
                f",截到下一个名字)未命中 bigrams={bigrams} chars={chars}"
            )

    return issues
