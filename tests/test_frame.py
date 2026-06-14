"""智能相框(ESP32 等)LAN 接口契约测试。

设计(web/frame.py + web/server.py):
  - 独立新接口,独立开关 frame.lan_enabled(默认关),和「问相册」(lan_chat)隔离。
  - LAN gate 按 _LAN_FRAME_ALLOW 白名单放行;配置/写操作/相框自身开关端点都 403。
  - 本机永远全功能;远程靠 frame.lan_enabled 热读放行。

模拟远程:TestClient client=(ip, port);默认 'testclient' 被视作本机。
config 跟 env LIFE_LENS_ROOT 走,fixture 指 tmp_path 隔离真实配置。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LIFE_LENS_ROOT", str(tmp_path))
    from life_lens.web.server import create_app
    return create_app(tmp_path)


@pytest.fixture
def local(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def remote(app) -> TestClient:
    return TestClient(app, client=("192.168.1.50", 40000))


@pytest.fixture
def frame_on(app):
    from life_lens.store import config
    config.update_frame_lan(True)


@pytest.fixture
def chat_on(app):
    from life_lens.store import config
    config.update_lan_chat(True)


# ---------- 开关默认关:相框端点对内网一律 403 ----------

@pytest.mark.parametrize("path", [
    "/api/frame/next",
    "/api/frame/photo/abc",
    "/api/frame/playlist",
    "/api/frame/info",
])
def test_frame_disabled_by_default(remote: TestClient, path: str):
    r = remote.get(path)
    assert r.status_code == 403, f"{path} 开关默认关应 403,实际 {r.status_code}"
    assert "未开启" in r.json()["detail"]


# ---------- 相框开关 ≠ 问相册开关:互相隔离 ----------

def test_frame_on_does_not_open_chat(remote: TestClient, frame_on):
    """只开相框,问相册端点仍 403。"""
    assert remote.post("/api/chat").status_code == 403
    assert remote.get("/api/llm-providers").status_code == 403


def test_chat_on_does_not_open_frame(remote: TestClient, chat_on):
    """只开问相册,相框端点仍 403。"""
    assert remote.get("/api/frame/info").status_code == 403
    assert remote.get("/api/frame/next").status_code == 403


# ---------- 相框开启后:白名单放行,但配置/写仍 403 ----------

def test_frame_info_passes_gate(remote: TestClient, frame_on):
    # 过 gate → 业务层 200(空库 pool_size=0)
    r = remote.get("/api/frame/info")
    assert r.status_code == 200
    assert r.json()["pool_size"] == 0


def test_frame_next_empty_pool_404(remote: TestClient, frame_on):
    # 过 gate,但空库没照片 → 业务层 404(不是 403)
    r = remote.get("/api/frame/next")
    assert r.status_code == 404


def test_frame_photo_missing_passes_gate(remote: TestClient, frame_on):
    assert remote.get("/api/frame/photo/no-such-id").status_code == 404


def test_frame_playlist_passes_gate(remote: TestClient, frame_on):
    r = remote.get("/api/frame/playlist")
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/config/frame"),                # 配置端点不对内网开放
    ("POST", "/api/config/frame"),
    ("POST", "/api/frame/playlist/rebuild"),     # LLM 构建是本机管理操作
    ("GET", "/api/frame/playlist/status"),       # 构建状态也只对本机
    ("GET", "/api/status"),
    ("GET", "/api/photos"),
    ("POST", "/api/scan"),
])
def test_frame_remote_still_blocked(remote: TestClient, frame_on, method: str, path: str):
    """相框开关开着,也只放行 /api/frame/{next,photo,playlist,info} 读图;
    配置/管理/LLM 构建一律 403(/playlist/rebuild、/playlist/status 是子路径,不匹配白名单)。"""
    r = remote.request(method, path)
    assert r.status_code == 403, f"{method} {path} 应被拒,实际 {r.status_code}"


def test_remote_cannot_toggle_frame_itself(remote: TestClient, frame_on):
    """相框设备不能给自己开/关门或改主题。"""
    assert remote.post("/api/config/frame", json={"enabled": True}).status_code == 403


# ---------- 配置端点(本机) ----------

def test_frame_config_defaults(local: TestClient):
    r = local.get("/api/config/frame")
    assert r.status_code == 200
    body = r.json()
    assert body["lan_enabled"] is False       # 默认关
    assert body["theme"] == ""                 # 默认空 = 收藏
    assert body["using_favorites"] is True
    assert "next_url" in body                  # 可能 None(无网络),字段必须在


def test_frame_config_toggle_hot_applies(local: TestClient, remote: TestClient):
    assert remote.get("/api/frame/info").status_code == 403
    r = local.post("/api/config/frame", json={"enabled": True})
    assert r.status_code == 200 and r.json()["lan_enabled"] is True
    assert remote.get("/api/frame/info").status_code == 200
    local.post("/api/config/frame", json={"enabled": False})
    assert remote.get("/api/frame/info").status_code == 403


def test_frame_config_set_theme(local: TestClient):
    # auto_build=false:测试环境没配 LLM,不触发后台构建线程
    r = local.post("/api/config/frame", json={"theme": "海边", "auto_build": False})
    assert r.status_code == 200
    assert r.json()["theme"] == "海边"
    assert r.json()["build_started"] is False
    # 只改 theme 不动 enabled
    assert local.get("/api/config/frame").json()["lan_enabled"] is False
    info = local.get("/api/config/frame").json()
    assert info["theme"] == "海边"
    assert info["using_favorites"] is False


def test_frame_theme_too_long_rejected(local: TestClient):
    r = local.post("/api/config/frame", json={"theme": "x" * 201})
    assert r.status_code == 400


# ---------- LLM 审核播放列表:本机端点 ----------

def test_playlist_status_shape(local: TestClient):
    r = local.get("/api/frame/playlist/status")
    assert r.status_code == 200
    body = r.json()
    assert "build" in body and "saved" in body
    assert body["saved"] is None          # 还没构建过


def test_playlist_rebuild_empty_theme_rejected(local: TestClient):
    # 空主题(收藏模式)不需要 AI 挑选
    r = local.post("/api/frame/playlist/rebuild", json={})
    assert r.status_code == 400
