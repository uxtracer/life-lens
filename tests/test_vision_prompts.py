"""Vision description prompt事实边界与人物主次回归测试。"""
from __future__ import annotations

from life_lens.vision.prompts import (
    DESCRIPTION_PROMPT_VERSION,
    build_description_prompt,
    normalize_description,
)


def test_description_prompt_restores_strict_factual_boundary():
    prompt = build_description_prompt()

    assert DESCRIPTION_PROMPT_VERSION.startswith("v9.7-desc-")
    assert "严格、客观的生活影像记录员" in prompt
    assert "只写照片中直接看得见的事实" in prompt
    for word in ("似乎", "可能", "像是", "看起来", "暗示", "估计", "推测", "或许"):
        assert word in prompt
    assert "严禁添加照片无法提供的声音、气味、体感" in prompt
    assert "严禁把静止物体写成正在晃动、摇曳、飘动、流动" in prompt
    assert "轻微故事感" not in prompt
    assert "生活散文" not in prompt
    assert "结尾余味" not in prompt


def test_no_face_prompt_forbids_inventing_people():
    prompt = build_description_prompt()

    assert "程序没有检测到人脸" in prompt
    assert "必须按无人场景描述" in prompt
    assert "严禁凭空加入一人、男子、女子、游客、行人、人影或身影" in prompt
    assert "不得臆造人物动作" in prompt


def test_landscape_subject_hint_adds_program_fact():
    prompt = build_description_prompt(subject_hint="landscape")

    assert "程序已先行确认subject=landscape且未检测到人脸" in prompt
    assert "这是无人风景" in prompt
    assert "绝对不能出现" in prompt


def test_named_face_prompt_keeps_identity_and_action_guard():
    prompt = build_description_prompt([(1, "张三"), (2, None)])

    assert "用真名(张三)" in prompt
    assert "严禁错位" in prompt
    assert "不能确认动作属于谁就省略" in prompt
    assert "不得把一个编号人物的动作、表情或身体接触转移给另一个编号" in prompt


def test_background_bystanders_are_not_forced_by_face_marks():
    prompt = build_description_prompt([(1, "张三"), (2, None), (3, None)])

    assert "编号只用于身份核对,不代表每个人都要写进description" in prompt
    assert "背景路人完全省略" in prompt
    assert "最多用一处集合短语概括" in prompt
    assert "严禁按左/右/前/后逐个列举" in prompt
    assert '严禁使用"一名…另一名…"展开无名者' in prompt
    assert "不得逐人描述" in prompt


def test_distinctive_clothing_is_kept_for_main_subject():
    prompt = build_description_prompt([(1, "张三")])

    assert "主体人物有辨识度的服饰应保留具体可见信息" in prompt
    assert "颜色、衣物类型、图案或配饰" in prompt
    assert "不要泛化成\"衣着素净\"" in prompt
    assert "至少保留一项具体信息" in prompt
    assert "不写抽象穿搭评价" in prompt
    assert "严禁使用'对着镜头微笑'/'面带微笑'/'摆姿势'/'合影留念'套话" in prompt


def test_normalize_description_surfaces_hedges_sensory_claims_and_motion():
    _, warnings = normalize_description({
        "description": "远处似乎有人,风穿过树林发出声响,树枝随风摇曳。"
    })

    assert any("hedge words" in w for w in warnings)
    assert any("invisible sensory claims" in w for w in warnings)
    assert any("inferred motion" in w for w in warnings)
