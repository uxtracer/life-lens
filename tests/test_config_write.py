"""store/config.py 原子写 + chmod + load/update 测试。

不依赖 LLM / Ollama。通过 LIFE_LENS_ROOT env 指向 tmp_path,跑完不污染用户真实 config。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest


@pytest.fixture
def tmp_config(tmp_path: Path, monkeypatch):
    """通过 LIFE_LENS_ROOT 把 config 重定向到 tmp_path/config.json。"""
    monkeypatch.setenv("LIFE_LENS_ROOT", str(tmp_path))
    return tmp_path / "config.json"


def test_load_empty_returns_empty_dict(tmp_config):
    from life_lens.store import config as cfg
    assert cfg.load_config() == {}


def test_save_and_load_roundtrip(tmp_config):
    from life_lens.store import config as cfg
    d = {"amap_key": "test_key", "llm": {"default": "x", "providers": {"x": {"kind": "openai-compat"}}}}
    cfg.save_config(d)
    assert tmp_config.exists()
    assert cfg.load_config() == d


def test_save_chmod_600(tmp_config):
    """保存后文件权限 0600(只有 owner 可读写)。"""
    from life_lens.store import config as cfg
    cfg.save_config({"amap_key": "x"})
    mode = stat.S_IMODE(tmp_config.stat().st_mode)
    assert mode == 0o600, f"期望 0o600,实际 {oct(mode)}"


def test_save_is_utf8(tmp_config):
    """中文 label 用 utf-8 存,不会双重转义。"""
    from life_lens.store import config as cfg
    cfg.save_config({"llm": {"providers": {"a": {"label": "中文标签"}}}})
    raw = tmp_config.read_text(encoding="utf-8")
    assert "中文标签" in raw   # ensure_ascii=False 起作用


def test_save_atomic_no_tmp_leak(tmp_config):
    """save 之后不应留 .tmp 临时文件(原子 replace 应该清掉)。"""
    from life_lens.store import config as cfg
    cfg.save_config({"a": 1})
    tmps = list(tmp_config.parent.glob(".config_*.tmp"))
    assert tmps == [], f"期望无 tmp 文件残留,实际 {tmps}"


def test_update_amap_key_preserves_other_fields(tmp_config):
    """update_amap_key 只改单字段,_comment 等其他字段不能丢。"""
    from life_lens.store import config as cfg
    cfg.save_config({"_comment": "教学注释", "amap_key": "old", "llm": {"default": "x"}})
    cfg.update_amap_key("new_key")
    d = cfg.load_config()
    assert d["amap_key"] == "new_key"
    assert d["_comment"] == "教学注释"
    assert d["llm"] == {"default": "x"}


def test_update_llm_provider_first_sets_default(tmp_config):
    """第一个 provider 自动成为 default。"""
    from life_lens.store import config as cfg
    cfg.update_llm_provider("deepseek", {
        "kind": "openai-compat", "model": "deepseek-v4-flash",
        "api_key": "sk-x", "base_url": "https://api.deepseek.com/v1",
    })
    d = cfg.load_config()
    assert d["llm"]["default"] == "deepseek"
    assert "deepseek" in d["llm"]["providers"]


def test_update_llm_provider_second_keeps_default(tmp_config):
    """第二个 provider 不改 default。"""
    from life_lens.store import config as cfg
    cfg.update_llm_provider("deepseek", {"kind": "openai-compat", "model": "m1"})
    cfg.update_llm_provider("openai", {"kind": "openai-compat", "model": "m2"})
    d = cfg.load_config()
    assert d["llm"]["default"] == "deepseek"


def test_remove_llm_provider_reassigns_default(tmp_config):
    """删 default 那个 → 自动 reassign 到剩下的第一个。"""
    from life_lens.store import config as cfg
    cfg.update_llm_provider("a", {"kind": "openai-compat", "model": "x"})
    cfg.update_llm_provider("b", {"kind": "openai-compat", "model": "y"})
    cfg.remove_llm_provider("a")
    d = cfg.load_config()
    assert "a" not in d["llm"]["providers"]
    assert d["llm"]["default"] == "b"


def test_set_llm_default_unknown_raises(tmp_config):
    from life_lens.store import config as cfg
    cfg.update_llm_provider("a", {"kind": "openai-compat", "model": "x"})
    with pytest.raises(ValueError):
        cfg.set_llm_default("nonexistent")


def test_chat_user_notes_default_empty(tmp_config):
    from life_lens.store import config as cfg
    assert cfg.chat_user_notes() == ""


def test_update_chat_user_notes_roundtrip_preserves_others(tmp_config):
    """写背景知识只动 chat.user_notes,amap_key 等其他字段不丢。"""
    from life_lens.store import config as cfg
    cfg.update_amap_key("k1")
    cfg.update_chat_user_notes("  豆豆是张三的小名  ")
    assert cfg.chat_user_notes() == "豆豆是张三的小名"   # strip
    d = cfg.load_config()
    assert d["amap_key"] == "k1"


def test_chat_prompts_inject_notes(tmp_config):
    """背景知识注入 Round 1/2 prompt;空时两边都不出现"背景知识"段。"""
    import sqlite3
    from life_lens.store import config as cfg
    from life_lens.web import chat as chat_mod

    assert chat_mod._round1_notes_section() == ""
    assert "背景知识" not in chat_mod._build_round2_system()

    cfg.update_chat_user_notes("豆豆是张三的小名")
    r1 = chat_mod._build_round1_system(sqlite3.connect(":memory:"))  # 空 conn 走"查询失败"分支,验 format 占位符
    assert "豆豆是张三的小名" in r1
    assert "豆豆是张三的小名" in chat_mod._build_round2_system()
