"""Web API 接口契约 smoke test —— 启 FastAPI TestClient 跑核心 endpoint。

不依赖 LLM / Ollama / 真实照片库:
  - 用 tmp_path 临时 db
  - 不触发 scan,只测 GET 类接口
  - 不调 chat(那个需要 LLM)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """临时 root 目录 → 全新空 db → 启 FastAPI TestClient。"""
    from life_lens.web.server import create_app
    app = create_app(tmp_path)
    return TestClient(app)


def test_api_status(client: TestClient):
    """GET /api/status 返 200 + 含 global + photos_total = 0(空 db)。"""
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "global" in body
    assert body["global"].get("photos_total") == 0


def test_api_photos_empty(client: TestClient):
    """空 db /api/photos 返 200 + items=[] + total=0。"""
    r = client.get("/api/photos")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert "page" in body and "page_size" in body


def test_api_photos_order_by_imported(client: TestClient):
    """新增的 order_by=imported 参数应被接受(空 db 返同样)。"""
    r = client.get("/api/photos?order_by=imported&page_size=10")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_api_sources_empty(client: TestClient):
    """GET /api/sources 返 [] 或 dict 含 sources(具体 shape 由 api 定)。"""
    r = client.get("/api/sources")
    assert r.status_code == 200
    # 不强求 shape,只要不 500


def test_api_llm_providers(client: TestClient):
    """GET /api/llm-providers 返 200,有 providers + default 字段。"""
    r = client.get("/api/llm-providers")
    assert r.status_code == 200
    body = r.json()
    assert "providers" in body
    assert "default" in body


def test_api_photo_not_found(client: TestClient):
    """不存在的 photo_id 返 404。"""
    r = client.get("/api/photo/does_not_exist")
    assert r.status_code == 404


def test_api_thumb_not_found(client: TestClient):
    """不存在的 thumb 返 404。"""
    r = client.get("/api/thumb/does_not_exist")
    assert r.status_code == 404


def test_api_original_not_found(client: TestClient):
    """不存在的 original 返 404,?download=1 同样。"""
    assert client.get("/api/original/does_not_exist").status_code == 404
    assert client.get("/api/original/does_not_exist?download=1").status_code == 404
