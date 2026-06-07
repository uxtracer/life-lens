"""LAN gate(局域网分权)契约测试。

设计(web/server.py):监听始终 0.0.0.0,两层控制:
  1. 总开关 config `serve.lan_chat`(默认关)— 关闭时内网一律 403,
     每次远程请求热读,翻开关即时生效不用重启
  2. 开启后只放行「问相册」白名单(POST /api/chat、GET thumb/original/photo、
     llm-providers/llm-info、/static/*),其余 403;远程 / 拿到移动端 chat 页

模拟远程:TestClient 的 client 参数指定来源 (ip, port);默认 'testclient'
被 _is_local_client 视作本机(否则其他 API 测试全 403)。
config 读写跟 env LIFE_LENS_ROOT 走,fixture 指到 tmp_path 隔离用户真实配置。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    # config.json 跟 tmp root 走 — 不读/不写用户真实 ~/.life_lens/config.json
    monkeypatch.setenv("LIFE_LENS_ROOT", str(tmp_path))
    from life_lens.web.server import create_app
    return create_app(tmp_path)


@pytest.fixture
def lan_on(app):
    """打开内网「问相册」总开关(写 tmp config)。"""
    from life_lens.store import config
    config.update_lan_chat(True)


@pytest.fixture
def local(app) -> TestClient:
    """默认 client=('testclient', …) → 视作本机。"""
    return TestClient(app)


@pytest.fixture
def remote(app) -> TestClient:
    """模拟局域网设备来源 IP。"""
    return TestClient(app, client=("192.168.1.9", 40000))


# ---------- 总开关默认关:内网一律 403(包括 chat 页本身) ----------

@pytest.mark.parametrize("method,path", [
    ("GET", "/"),
    ("GET", "/chat"),
    ("GET", "/static/chat.js"),
    ("POST", "/api/chat"),
    ("GET", "/api/llm-providers"),
    ("GET", "/api/thumb/abc"),
])
def test_lan_disabled_by_default(remote: TestClient, method: str, path: str):
    r = remote.request(method, path)
    assert r.status_code == 403, f"{method} {path} 开关默认关应 403,实际 {r.status_code}"
    assert "未开启" in r.json()["detail"]


def test_lan_disabled_local_unaffected(local: TestClient):
    """开关关着,本机完全不受影响。"""
    assert local.get("/").status_code == 200
    assert local.get("/api/status").status_code == 200


# ---------- 热切换:本机 POST 开关,远程即时生效(不重启) ----------

def test_toggle_hot_applies(local: TestClient, remote: TestClient):
    assert remote.get("/api/llm-providers").status_code == 403
    # 开
    r = local.post("/api/config/lan-chat", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert remote.get("/api/llm-providers").status_code == 200
    # 关
    local.post("/api/config/lan-chat", json={"enabled": False})
    assert remote.get("/api/llm-providers").status_code == 403


def test_remote_cannot_toggle_itself(remote: TestClient, lan_on):
    """开关端点不在白名单 — 内网设备不能给自己开/关门。"""
    r = remote.post("/api/config/lan-chat", json={"enabled": True})
    assert r.status_code == 403


# ---------- 开关开启后:管理/数据端点仍一律 403 ----------

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/status"),
    ("GET", "/api/sources"),
    ("GET", "/api/photos"),
    ("GET", "/api/persons"),
    ("GET", "/api/runs"),
    ("GET", "/api/setup/status"),
    ("GET", "/api/face/abc/crop"),
    ("GET", "/api/photo/abc/faces"),            # /api/photo 只放行单段路径
    ("GET", "/api/config/lan-chat"),
    ("GET", "/api/config/chat-notes"),
    ("POST", "/api/config/chat-notes"),
    ("POST", "/api/scan"),
    ("POST", "/api/scan/stop"),
    ("POST", "/api/reprocess"),
    ("POST", "/api/seed-persons"),
    ("POST", "/api/config/amap-key"),
    ("POST", "/api/photo/abc/mismatches/acknowledge"),
    ("DELETE", "/api/sources/whatever"),
])
def test_remote_blocked(remote: TestClient, lan_on, method: str, path: str):
    r = remote.request(method, path)
    assert r.status_code == 403, f"{method} {path} 应被 LAN gate 拒绝,实际 {r.status_code}"
    assert "仅本机" in r.json()["detail"]


# ---------- 开关开启后:问相册所需最小集放行 ----------

def test_remote_llm_providers_ok(remote: TestClient, lan_on):
    r = remote.get("/api/llm-providers")
    assert r.status_code == 200
    assert "providers" in r.json()


def test_remote_thumb_passes_gate(remote: TestClient, lan_on):
    # 过 gate 后由业务层返 404(id 不存在),而不是 403
    assert remote.get("/api/thumb/no-such-id").status_code == 404


def test_remote_photo_detail_passes_gate(remote: TestClient, lan_on):
    assert remote.get("/api/photo/no-such-id").status_code == 404


def test_remote_original_passes_gate(remote: TestClient, lan_on):
    assert remote.get("/api/original/no-such-id").status_code == 404


def test_remote_static_ok(remote: TestClient, lan_on):
    assert remote.get("/static/chat.js").status_code == 200


# ---------- / 按来源分流 ----------

def test_root_local_gets_full_app(local: TestClient):
    r = local.get("/")
    assert r.status_code == 200
    assert 'data-page="settings"' in r.text          # 5-tab 完整页特征

def test_root_remote_gets_chat_page(remote: TestClient, lan_on):
    r = remote.get("/")
    assert r.status_code == 200
    assert "<title>Life Lens 人生透镜</title>" in r.text
    assert 'data-page="settings"' not in r.text      # 不是 5-tab 页

def test_chat_page_explicit_path(local: TestClient, remote: TestClient, lan_on):
    for c in (local, remote):
        r = c.get("/chat")
        assert r.status_code == 200
        assert "<title>Life Lens 人生透镜</title>" in r.text


# ---------- 本机不受影响 ----------

def test_local_full_access(local: TestClient):
    assert local.get("/api/status").status_code == 200
    assert local.get("/api/setup/status").status_code == 200


# ---------- 开关 API 本身 ----------

def test_lan_chat_config_api(local: TestClient):
    r = local.get("/api/config/lan-chat")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False          # 默认关
    assert "lan_url" in body                 # 可能为 None(无网络),但字段必须在
