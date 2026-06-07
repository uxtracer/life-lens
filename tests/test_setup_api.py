"""配置类 API endpoint 契约测试 — /setup/status / /ollama/ping / /config/*"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """临时 root + 临时 config.json,启 TestClient。"""
    # store.config 通过 LIFE_LENS_ROOT 自动指到 tmp_path/config.json
    monkeypatch.setenv("LIFE_LENS_ROOT", str(tmp_path))

    from life_lens.web.server import create_app
    app = create_app(tmp_path)
    return TestClient(app)


def test_setup_status_first_run(client: TestClient):
    """全新空环境:is_first_run=True, next_step='configure'。"""
    r = client.get("/api/setup/status")
    assert r.status_code == 200
    body = r.json()
    assert body["is_first_run"] is True
    assert body["next_step"] == "configure"
    assert body["sources"]["count"] == 0
    assert body["photos"]["count"] == 0
    assert body["llm"]["configured"] is False
    assert body["llm"]["count"] == 0
    # amap / ollama 内容跟环境相关,只验证字段存在
    assert "configured" in body["amap"]
    assert "ok" in body["ollama"]


def test_config_amap_key_set(client: TestClient, tmp_path):
    r = client.post("/api/config/amap-key", json={"key": "test_amap_key_xyz"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    # 验证 config.json 写了
    cfg_path = tmp_path / "config.json"
    assert cfg_path.exists()
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["amap_key"] == "test_amap_key_xyz"


def test_config_amap_key_empty_rejected(client: TestClient):
    r = client.post("/api/config/amap-key", json={"key": ""})
    assert r.status_code == 400


def test_config_llm_provider_upsert(client: TestClient, tmp_path):
    r = client.post("/api/config/llm-provider", json={
        "op": "upsert",
        "provider_id": "deepseek",
        "config": {
            "kind": "openai-compat",
            "model": "deepseek-v4-flash",
            "api_key": "sk-test",
            "base_url": "https://api.deepseek.com/v1",
            "label": "DeepSeek",
        },
    })
    assert r.status_code == 200
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["llm"]["providers"]["deepseek"]["model"] == "deepseek-v4-flash"
    assert cfg["llm"]["default"] == "deepseek"


def test_config_llm_provider_rejects_claude_p(client: TestClient):
    """v0.4 起 kind=claude-p 应被拒绝。"""
    r = client.post("/api/config/llm-provider", json={
        "op": "upsert", "provider_id": "x",
        "config": {"kind": "claude-p", "model": "claude-opus-4-7"},
    })
    assert r.status_code == 400
    assert "openai-compat" in r.json()["detail"]


def test_config_llm_provider_missing_field(client: TestClient):
    r = client.post("/api/config/llm-provider", json={
        "op": "upsert", "provider_id": "x",
        "config": {"kind": "openai-compat", "model": "m"},   # 缺 api_key / base_url
    })
    assert r.status_code == 400


def test_config_llm_provider_delete(client: TestClient):
    # 先 upsert 两个
    for pid in ["a", "b"]:
        client.post("/api/config/llm-provider", json={
            "op": "upsert", "provider_id": pid,
            "config": {"kind": "openai-compat", "model": "m", "api_key": "k", "base_url": "u"},
        })
    r = client.post("/api/config/llm-provider", json={"op": "delete", "provider_id": "a"})
    assert r.status_code == 200
    # 验 status 里只剩 1 个
    s = client.get("/api/setup/status").json()
    assert s["llm"]["count"] == 1


def test_config_llm_default_unknown_404(client: TestClient):
    r = client.post("/api/config/llm-default", json={"provider_id": "nonexistent"})
    assert r.status_code == 404


def test_ollama_ping_returns_shape(client: TestClient):
    """ping 应至少返回 {ok, endpoint} — ok 真假取决于本机是否跑 Ollama。"""
    r = client.get("/api/ollama/ping")
    assert r.status_code == 200
    body = r.json()
    assert "ok" in body
    assert "endpoint" in body


def test_setup_next_step_progression(client: TestClient, tmp_path):
    """配满后 next_step 应该跳出 'configure'。
    我们 mock-style 写 config 加 amap + llm + 一个 source,但 photos 仍空 → next='scan'。
    """
    # 配 amap
    client.post("/api/config/amap-key", json={"key": "key_x"})
    # 配 LLM
    client.post("/api/config/llm-provider", json={
        "op": "upsert", "provider_id": "deepseek",
        "config": {"kind": "openai-compat", "model": "m", "api_key": "k",
                   "base_url": "https://api.deepseek.com/v1"},
    })
    # 加 source(用 filesystem 指到 tmp_path 当 placeholder)
    src_dir = tmp_path / "fake_album"
    src_dir.mkdir()
    client.post("/api/sources", json={"kind": "filesystem", "path": str(src_dir)})
    # 此时 next_step 应该是 'scan'(配齐 + 有 source + 无 photo) — 但取决于 Ollama 在不在
    s = client.get("/api/setup/status").json()
    if s["ollama"]["ok"] and s["ollama"].get("has_vision_model"):
        assert s["next_step"] == "scan", f"got {s}"
        assert s["is_first_run"] is False
    # 如果 Ollama 不通,next_step 仍是 'configure'(因为依赖未就绪)— 我们不强求 assert


def test_config_chat_notes_roundtrip(client: TestClient):
    """GET 默认空 → POST 写入 → GET 读回(strip 后)。"""
    r = client.get("/api/config/chat-notes")
    assert r.status_code == 200 and r.json()["notes"] == ""
    r = client.post("/api/config/chat-notes", json={"notes": "  豆豆是张三的小名\n李丽是张三的妈妈  "})
    assert r.status_code == 200
    assert r.json()["notes"] == "豆豆是张三的小名\n李丽是张三的妈妈"
    assert client.get("/api/config/chat-notes").json()["notes"] == "豆豆是张三的小名\n李丽是张三的妈妈"


def test_config_chat_notes_clear(client: TestClient):
    """POST 空串 = 清空(回到未填写状态)。"""
    client.post("/api/config/chat-notes", json={"notes": "豆豆是张三的小名"})
    r = client.post("/api/config/chat-notes", json={"notes": ""})
    assert r.status_code == 200 and r.json()["notes"] == ""


def test_config_chat_notes_too_long_rejected(client: TestClient):
    """>4000 字拒绝(这段每次提问都进 prompt,防误贴长文)。"""
    r = client.post("/api/config/chat-notes", json={"notes": "长" * 4001})
    assert r.status_code == 400
