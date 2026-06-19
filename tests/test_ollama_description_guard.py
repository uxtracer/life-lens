"""无人风景description的人物幻觉程序层兜底。"""
from __future__ import annotations

from life_lens.vision.base import VisionResult
from life_lens.vision.ollama import OllamaVision


def _result(description: str) -> VisionResult:
    return VisionResult(parsed={"description": description})


def test_landscape_person_claim_triggers_one_local_retry(monkeypatch):
    vision = object.__new__(OllamaVision)
    responses = iter([
        _result("峡谷里有一人站在溪边。"),
        _result("峡谷两侧岩壁陡峭,溪水穿过乱石。"),
    ])
    calls = []

    def fake_call(image, prompt):
        calls.append(prompt)
        return next(responses)

    monkeypatch.setattr(vision, "_call", fake_call)
    result = vision.describe_description(b"jpeg", subject_hint="landscape")

    assert len(calls) == 2
    assert "纠错:上一版违反了无人风景约束" in calls[1]
    assert result.parsed == {"description": "峡谷两侧岩壁陡峭,溪水穿过乱石。"}


def test_landscape_person_claim_is_rejected_after_failed_retry(monkeypatch):
    vision = object.__new__(OllamaVision)
    responses = iter([
        _result("远处有一名游客。"),
        _result("一人站在山脚下。"),
    ])
    monkeypatch.setattr(vision, "_call", lambda image, prompt: next(responses))

    result = vision.describe_description(b"jpeg", subject_hint="landscape")

    assert result.parsed is None
    assert result.error == "landscape_person_hallucination_after_retry"
    assert "无人风景description两次出现人物词,已拒收" in result.warnings


def test_non_landscape_does_not_apply_people_guard(monkeypatch):
    vision = object.__new__(OllamaVision)
    calls = []

    def fake_call(image, prompt):
        calls.append(prompt)
        return _result("张三站在栏杆旁。")

    monkeypatch.setattr(vision, "_call", fake_call)
    result = vision.describe_description(
        b"jpeg", face_items=[(1, "张三")], subject_hint="portrait"
    )

    assert len(calls) == 1
    assert result.parsed is not None
