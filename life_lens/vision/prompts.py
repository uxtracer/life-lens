"""Vision prompt 模板(拆调用版 v9)。

设计:**两次 LLM 调用**。
1. DESCRIPTION_PROMPT — 只要 `description`(60-180字客观记录),带 set-of-mark + 强人名约束
2. STRUCT_PROMPT — 只要结构化字段(media_type / subject / scene / objects / tags / ocr_text / mood / actions),
                   带 set-of-mark 但**不要求 LLM 给真名**(actions 按编号顺序输出即可,后端用 cluster_id 组装 people.persons)

拆开原因:单次 prompt 太长 → 8B 模型注意力分散,description 用代号忽略人名 hint。
拆开后每个 prompt ~300-500 字,LLM 能专注一个任务,实测 description 能稳定用真名。
"""
from __future__ import annotations

from typing import Optional

DESCRIPTION_PROMPT_VERSION = "v9.7-desc-2026-06-strict-factual"
STRUCT_PROMPT_VERSION       = "v9.2-struct-2026-05-position-hint"

# 严格枚举(struct prompt 输出需要 normalize 兜底)
ALLOWED_MEDIA_TYPE = {"photo", "screenshot", "other"}
ALLOWED_SUBJECT    = {"single", "portrait", "group", "landscape", "object", "food", "pet", "mixed"}


# ============================================================
# Description prompt(Call 1)
# ============================================================

_DESC_RULES = """写作要求:
1. 只写照片中直接看得见的事实。严禁使用推测词:似乎、可能、像是、看起来、应为、应该是、暗示、估计、推测、或许、仿佛、大概。不确定就省略,不要换一种措辞继续猜。
2. 这是静态照片。严禁添加照片无法提供的声音、气味、体感、气温、风力或照片发生前后的事情;严禁把静止物体写成正在晃动、摇曳、飘动、流动。可以写直接可见的状态,如"衣角扬起""树枝倾斜",不能写"微风吹动衣角""风穿过树林发出声响"。
3. 按画面重心决定详略,但不改变事实标准:
   - 景区、风景或环境信息丰富时:具体写主要景物、前后景、光线、天气、色彩及主体人物与环境的位置关系。
   - 人物近景或简单肖像时:直接写主体人物、清晰可见的神情/动作和必要背景,不要为凑字数添加氛围、故事或不可见细节。
4. 人物只聚焦已命名主体、明显占据画面主体的人,以及与主体有直接可见互动的人。景区/街道背景中远处、边缘、被遮挡或与主体无互动的旁人默认完全省略,不得逐人描述。出现多个未识别人物时,除非某人与已命名主体有明确直接互动,否则最多用一处集合短语概括,如"身旁有几位同行者"或"远处游客较多";严禁按左/右/前/后逐个列举,严禁使用"一名…另一名…"展开无名者。
5. 多人照片中,动作、表情、视线和身体接触必须严格对应本人。不能确认动作属于谁就省略,严禁把无名者的动作写到已命名人物身上。
6. 表情清晰可辨时才写,如大笑、皱眉、闭眼或注视某物;表情普通或看不清就略过。避免"对着镜头微笑""面带微笑""摆姿势""合影留念"等套话。
7. 主体人物有辨识度的服饰应保留具体可见信息,如颜色、衣物类型、图案或配饰;不要泛化成"衣着素净""穿着时尚",也不要罗列背景路人的服饰。
8. 可见文字通常不写进description(另一次调用会提取ocr_text);只有店招、景点名或活动横幅等能帮助定位场景的文字才提及。
9. 跳过装饰性次要物件。信息较少时宁可简短结束,不要靠姿势、穿搭、情绪或无关物件凑字数。
10. 不要使用"这张照片展示了""画面中可以看到"等套话,不要使用Markdown。
"""

_HEDGE_WORDS = (
    "应为", "应当", "似乎", "可能", "仿佛", "像是", "看起来", "应该是",
    "暗示", "估计", "推测", "或许", "大概",
)
_INVISIBLE_SENSORY_PHRASES = (
    "发出声", "传来", "听见", "听到", "闻到", "气味", "香气", "凉意", "暖意",
)
_INFERRED_MOTION_PHRASES = (
    "微风中", "随风", "风吹动", "风穿过", "轻轻晃动", "正在晃动", "摇曳", "飘动",
)
_PERSON_CLAIM_PHRASES = (
    "一人", "一个人", "男子", "女子", "男士", "女士", "游客", "行人", "人影", "身影",
)


def _bbox_position(bbox: tuple, image_size: tuple) -> str:
    """从 bbox 算 9 宫格位置标签:'上左'/'中右'/'正中' 等。

    bbox: (x, y, w, h) 像素;image_size: (W, H)
    """
    if not bbox or not image_size:
        return ""
    x, y, w, h = bbox
    W, H = image_size
    if W <= 0 or H <= 0:
        return ""
    cx = x + w / 2
    cy = y + h / 2
    horiz = "左" if cx < W / 3 else ("右" if cx > 2 * W / 3 else "中")
    vert = "上" if cy < H / 3 else ("下" if cy > 2 * H / 3 else "中")
    if horiz == "中" and vert == "中":
        return "正中"
    return vert + horiz   # "上左" / "下中" / "中右" 等


def _demographics_text(age, gender) -> str:
    """格式化 InsightFace 估计值(可能不准,只作弱参考)。"""
    parts = []
    if age is not None:
        parts.append(f"约 {age} 岁")
    if gender is not None:
        parts.append("男" if gender == 1 else "女")
    return "、".join(parts)


def _face_line(item) -> str:
    """item 可能是:
      - tuple(idx, name)                                     ← 旧格式(只 name)
      - dict {idx, name, position?, age?, gender?, bbox?, image_size?}  ← 新格式
    返回一行 prompt 文字。
    """
    if isinstance(item, dict):
        idx = item.get("idx")
        name = item.get("name") or "未识别"
        position = item.get("position")
        if not position and item.get("bbox") and item.get("image_size"):
            position = _bbox_position(item["bbox"], item["image_size"])
        demo = _demographics_text(item.get("age"), item.get("gender"))
        bits = [f"位置:{position}"] if position else []
        if demo:
            bits.append(f"参考估计:{demo}(可能不准,以你看到的为准)")
        suffix = " (" + "; ".join(bits) + ")" if bits else ""
        return f"  [{idx}] = {name}{suffix}"
    else:
        # 旧格式 tuple(idx, name)
        idx, name = item
        return f"  [{idx}] = {name if name else '未识别'}"


def _desc_set_of_mark(face_items: Optional[list]) -> str:
    """description prompt 的 set-of-mark mapping 段。
    支持新旧两种 face_items 格式,新格式带位置 + 年龄/性别 hint。"""
    if not face_items:
        return ""
    total = len(face_items)
    lines = [
        f"检测到 {total} 张脸。图上每张脸有红框 + 编号 [1][2]...,下面给出编号对应的真名和位置。"
        f"这些编号只用于身份核对,不代表每个人都要写进description;背景路人按写作要求省略:"
    ]
    for item in face_items:
        lines.append(_face_line(item))
    return "\n".join(lines) + "\n"


def _desc_people_guard(face_items: Optional[list]) -> str:
    """按人脸检测结果追加无人/有人场景的硬约束。"""
    if not face_items:
        return (
            "程序没有检测到人脸。若照片确实没有清晰可见、占据主体的人物,必须按无人场景描述,"
            "严禁凭空加入一人、男子、女子、游客、行人、人影或身影,也不得臆造人物动作。"
            "只有人物身体在照片中直接清晰可见且明显是主体时才可客观提及。\n"
        )
    return (
        "程序检测到的人脸只用于身份核对。不得在红框之外凭空增加人物;"
        "不得把一个编号人物的动作、表情或身体接触转移给另一个编号。\n"
    )


def _desc_subject_guard(face_items: Optional[list], subject_hint: Optional[str]) -> str:
    """struct已确认无人风景时,给description追加更强的程序事实。"""
    if subject_hint == "landscape" and not face_items:
        return (
            "程序已先行确认subject=landscape且未检测到人脸:这是无人风景。description中绝对不能出现"
            "一人、一个人、男子、女子、游客、行人、人影、身影或任何人物动作。\n"
        )
    return ""


def description_person_claims(description: str) -> list[str]:
    """返回description中的明确人物词,供无人风景程序层兜底。"""
    return [phrase for phrase in _PERSON_CLAIM_PHRASES if phrase in (description or "")]


def _extract_name(item) -> Optional[str]:
    if isinstance(item, dict):
        return item.get("name")
    return item[1]


def _desc_final_reminder(face_items: Optional[list]) -> str:
    """末尾硬约束 — 利用 last-instruction-wins,8B 模型对 prompt 末尾指令最敏感。"""
    if not face_items:
        return ""
    named = [_extract_name(it) for it in face_items if _extract_name(it)]
    if not named:
        return (
            "提示:只描述作为照片主体的未识别人物;远处、边缘或与主体无互动的背景路人完全省略。"
            "需要指代主体时可用方位/明显外貌,但description中不要出现'方框'/'编号'/'[N]'。"
            "严禁推测、不可见声音和静态照片中的臆造运动。\n"
        )
    names_str = "、".join(named)
    return (
        f"⚠️ 关键约束(必须严格遵守,任何一条违反都会让记录失效):\n"
        f"1. **先逐一对照红框**:看每个红框真实在画面什么位置,核对上面给的'位置:XX'标签(标签是\n"
        f"   程序从坐标算出来的,100% 准)。如果你看到红框 [1] 实际在'下中',就和我们给的标签一致 → \n"
        f"   确认 [1] 那个人就是 {named[0] if named else '...'} 这一类。**这一步必须先做,再开始写**。\n"
        f"2. description 里**必须**用真名({names_str})指代已识别的人,严禁'左侧男子'/'戴眼镜女子'\n"
        f"   /'中间的人'等方位/外貌代号。\n"
        f"3. **严禁错位**:若写某个真名的动作、穿着或表情,必须确实是那个红框里的人,不能写到别人名下。\n"
        f"4. 未识别的人才用方位/外貌描述。description 中不要出现'方框'/'编号'/'[N]'。\n"
        f"5. 严禁推测词、不可见的声音/气味/体感,以及从静态照片臆造持续运动;不确定就省略。\n"
        f"6. 多个未识别人物最多用一处集合短语概括;严禁按左右位置逐个列举,严禁用'一名…另一名…'展开。背景路人完全省略。\n"
        f"7. 主体人物有辨识度的服饰保留具体颜色、类型或图案,不要泛化成'衣着素净'。\n"
        f"8. 对每个已命名主体,若衣物颜色/类型/图案清晰且有辨识度,至少保留一项具体信息;不写抽象穿搭评价。\n"
        f"9. 普通站姿和普通表情不写;严禁使用'对着镜头微笑'/'面带微笑'/'摆姿势'/'合影留念'套话。\n"
    )


def build_description_prompt(
    face_items: Optional[list[tuple[int, Optional[str]]]] = None,
    subject_hint: Optional[str] = None,
) -> str:
    """Call 1 — 只生成 description 字段。"""
    parts = [
        "你是一名严格、客观的生活影像记录员。这张照片将用来'还原一个人的生活' — 一年后回看时,记录必须仍然可信。",
        "",
        "请写一段60-180字的连贯中文description。根据画面重心决定环境与人物的详略,但所有内容必须直接可见。画面信息少时可以更短。",
        "",
        _DESC_RULES,
    ]
    parts.append(_desc_people_guard(face_items))
    subject_guard = _desc_subject_guard(face_items, subject_hint)
    if subject_guard:
        parts.append(subject_guard)
    sm = _desc_set_of_mark(face_items)
    if sm:
        parts.append(sm)
    parts.append("")
    parts.append("按 JSON 输出,只有一个字段:")
    parts.append('{ "description": "..." }')
    parts.append("")
    fr = _desc_final_reminder(face_items)
    if fr:
        parts.append(fr)
    parts.append("只输出 JSON,不要 ```代码标记,不要任何解释。")
    return "\n".join(parts)


# ============================================================
# Struct prompt(Call 2)
# ============================================================

def _struct_set_of_mark(face_items: Optional[list]) -> str:
    if not face_items:
        return ""
    total = len(face_items)
    # 输出每个编号的位置 hint(从 bbox 算,100% 准)
    lines = [
        f"画面共 {total} 个人。图上每张脸有红框 + 编号,下面给出每个编号在画面里的位置(从坐标算,100% 准):"
    ]
    for item in face_items:
        if isinstance(item, dict):
            idx = item.get("idx")
            pos = item.get("position")
            if not pos and item.get("bbox") and item.get("image_size"):
                pos = _bbox_position(item["bbox"], item["image_size"])
            lines.append(f"  [{idx}] 位置:{pos or '?'}")
        else:
            idx, _ = item
            lines.append(f"  [{idx}](无位置信息)")
    map_section = "\n".join(lines) + "\n"
    return (
        map_section +
        f"\n**actions 字段是 JSON object(字典),key 是红框编号字符串 \"1\" \"2\" ... \"{total}\","
        f"value 是该编号那个人的 ≤20 字动作描述(如 '戴帽微笑'/'举手机自拍')。**\n"
        f"⚠️ 关键:key 严格锁定红框编号 — 看 [1] 红框那个人(实际在'{_get_pos(face_items, 1)}'位置)→ 写到 actions[\"1\"]。\n"
        f"**先逐个对照红框编号和上面给的位置 hint 确认是哪个人,再开始写 actions**。\n"
        f"未识别的人也要给 action,不要漏 key。\n"
        f"ocr_text 字段不要包含我画的 [N] 编号(这是我加的辅助标注,不是图里原本的文字)。\n"
    )


def _get_pos(face_items, idx: int) -> str:
    """从 face_items 取指定 idx 的 position 文字(只用于 prompt 内嵌示例)"""
    for item in face_items or []:
        if isinstance(item, dict) and item.get("idx") == idx:
            pos = item.get("position")
            if not pos and item.get("bbox") and item.get("image_size"):
                pos = _bbox_position(item["bbox"], item["image_size"])
            return pos or "?"
    return "?"


def build_struct_prompt(face_items: Optional[list[tuple[int, Optional[str]]]] = None) -> str:
    """Call 2 — 只生成其他结构化字段。"""
    parts = [
        "请提取这张照片的结构化信息,按 JSON 输出。",
        "",
        "{",
        '  "media_type": "photo|screenshot|other",',
        '  "subject": "single|portrait|group|landscape|object|food|pet|mixed|null",',
        '  "scene": "单个简短中文短语,如 \'外滩夜景\'/\'山林小径\'/\'咖啡馆室内\'",',
        '  "objects": ["3-8 个主要可见物体的中文名,跳过装饰性物件"],',
        '  "tags": ["3-10 个氛围/活动/季节/光线标签"],',
        '  "ocr_text": "图中可见文字原文(中英文按原文,空格分隔),没有则空字符串",',
        '  "mood": "单个中文词:宁静/热闹/温暖/孤寂/欢快/凝重",',
        '  "actions": {"1": "≤20 字动作描述", "2": "...", "...": "..."}  // 字典 key 是红框编号字符串',
        "}",
        "",
        "字段规则:",
        "- media_type 严格三选一,不要造新词。",
        "- subject 仅 media_type=photo 时填实际枚举,否则填 null。",
        "- objects/tags 跳过空调出风口/扶手/警示牌等装饰性物件。",
        "",
    ]
    sm = _struct_set_of_mark(face_items)
    if sm:
        parts.append(sm)
    parts.append("只输出 JSON,不要 ```代码标记,不要任何解释。")
    return "\n".join(parts)


# ============================================================
# 输出 normalize(struct 字段的 enum 兜底)
# ============================================================

def normalize_struct(parsed: dict, expected_actions_count: int = 0) -> tuple[dict, list[str]]:
    """对 struct call 的输出做兜底校正。返回 (normalized, warnings)。"""
    warnings: list[str] = []
    out = dict(parsed)

    mt = out.get("media_type")
    if mt not in ALLOWED_MEDIA_TYPE:
        warnings.append(f"media_type out-of-enum: {mt!r} → other")
        out["media_type"] = "other"

    sub = out.get("subject")
    if out["media_type"] != "photo":
        if sub is not None:
            warnings.append(f"subject set on non-photo: {sub!r} → null")
            out["subject"] = None
    else:
        if sub not in ALLOWED_SUBJECT:
            warnings.append(f"subject out-of-enum: {sub!r} → mixed")
            out["subject"] = "mixed"

    for k in ("objects", "tags"):
        if out.get(k) is None:
            out[k] = []
        elif not isinstance(out[k], list):
            warnings.append(f"{k} 不是数组 → []")
            out[k] = []

    # actions:v9.1 起优先接受 dict({"1":"...","2":"..."}),按编号 key 取避免顺序错位
    # 旧 list 格式也兼容(按下标顺序),便于历史 prompt 回滚 / LLM 偶尔不按 dict 输出
    acts_raw = out.get("actions")
    if isinstance(acts_raw, dict):
        # 按红框编号 "1" "2" ... 取
        if expected_actions_count > 0:
            actions = []
            for i in range(1, expected_actions_count + 1):
                v = acts_raw.get(str(i), acts_raw.get(i, ""))
                actions.append(str(v) if v is not None else "")
            out["actions"] = actions
        else:
            out["actions"] = []
    elif isinstance(acts_raw, list):
        # 兼容旧格式 — 但记 warn,因为顺序可能错
        if expected_actions_count > 0:
            warnings.append("actions 是 list(旧格式),顺序可能错位 — 建议 prompt 已要求 dict")
            actions = list(acts_raw)
            if len(actions) < expected_actions_count:
                actions += [""] * (expected_actions_count - len(actions))
            out["actions"] = [str(a) if a is not None else "" for a in actions[:expected_actions_count]]
        else:
            out["actions"] = [str(a) if a is not None else "" for a in acts_raw]
    elif acts_raw is None:
        out["actions"] = [""] * expected_actions_count if expected_actions_count > 0 else []
    else:
        warnings.append(f"actions 类型异常 ({type(acts_raw).__name__})  → []")
        out["actions"] = [""] * expected_actions_count if expected_actions_count > 0 else []

    return out, warnings


def normalize_description(parsed: dict) -> tuple[dict, list[str]]:
    """对 description call 的输出做兜底校正。"""
    warnings: list[str] = []
    out = dict(parsed)
    desc = out.get("description") or ""
    leak = [w for w in _HEDGE_WORDS if w in desc]
    if leak:
        warnings.append(f"description contains hedge words: {leak}")
    sensory = [w for w in _INVISIBLE_SENSORY_PHRASES if w in desc]
    if sensory:
        warnings.append(f"description contains invisible sensory claims: {sensory}")
    motion = [w for w in _INFERRED_MOTION_PHRASES if w in desc]
    if motion:
        warnings.append(f"description contains inferred motion: {motion}")
    out["description"] = desc
    return out, warnings
