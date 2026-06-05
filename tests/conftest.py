"""Pytest fixtures — 临时 db / 测试数据。

设计原则:**所有测试必须可在无 Ollama / 无真实照片 / 无 API key 的环境跑**。
GitHub Actions CI 跑这些,5 分钟内出结果。

跟 tests/eval/(B 层 AI 质量评测,依赖真实 db + Ollama)对应,这里是 A 层管道测试。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from life_lens.store import db, repo


# ---- 临时 db fixtures ----

@pytest.fixture
def empty_db(tmp_path: Path) -> sqlite3.Connection:
    """全新临时 db,已 init_schema。每个测试独立(tmp_path 自动清理)。"""
    db_path = tmp_path / "test_lens.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def populated_db(empty_db: sqlite3.Connection) -> sqlite3.Connection:
    """预填充 5 张 fake 照片用于 query 测试。

    fixture 设计:
      - 2 张含 "张三"(单人 + 合影)
      - 1 张含 "张三" + "李丽"(合影)
      - 1 张含 "李丽"(单人)
      - 1 张无人(海边风景)
    场景词: "海边", "公园", "城市"
    """
    conn = empty_db
    photos = [
        _fake_photo("p_001", names=["张三"], scene="公园", desc="张三在公园散步"),
        _fake_photo("p_002", names=["张三"], scene="城市", desc="张三在城市街头"),
        _fake_photo("p_003", names=["张三", "李丽"], scene="海边", desc="张三和李丽在海边合影"),
        _fake_photo("p_004", names=["李丽"], scene="公园", desc="李丽在公园里"),
        _fake_photo("p_005", names=[], scene="海边", desc="夕阳下的海边景色,无人"),
    ]
    for rec in photos:
        repo.upsert_photo(conn, rec)
        # 写 FTS5 + faces
        vision = rec["vision"]
        people = rec["people"]
        actions = " ".join(p.get("action", "") for p in (people.get("persons") or []))
        objects = " ".join(vision.get("objects") or [])
        tags = " ".join(vision.get("tags") or [])
        conn.execute(
            "INSERT INTO photos_fts(photo_id, description, scene, tags, ocr_text, actions, objects) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rec["identity"]["photo_id"], vision["description"], vision["scene"],
             tags, "", actions, objects),
        )
        # faces 表 + persons cluster 命名
        for p in people["persons"]:
            cid = p["cluster_id"]
            conn.execute(
                "INSERT INTO faces(face_id, photo_id, cluster_id, embedding, bbox, created_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?)",
                (f"f_{rec['identity']['photo_id']}_{cid}", rec["identity"]["photo_id"], cid,
                 datetime.now().isoformat()),
            )
        for p in people["persons"]:
            if p.get("name"):
                conn.execute(
                    "INSERT OR REPLACE INTO persons(cluster_id, name, updated_at) VALUES (?, ?, ?)",
                    (p["cluster_id"], p["name"], datetime.now().isoformat()),
                )
    return conn


@pytest.fixture
def favorite_db(empty_db: sqlite3.Connection) -> sqlite3.Connection:
    """4 张照片,其中 2 张收藏(derived.favorite=1)→ 测 favorite 生成列过滤。

    favorite_only 走 `photos.favorite` 生成列,不依赖 FTS,故只 upsert_photo 即可。
    """
    conn = empty_db
    photos = [
        _fake_photo("p_f1", names=["张三"], scene="公园", desc="张三在公园", favorite=1),
        _fake_photo("p_f2", names=["张三"], scene="城市", desc="张三在城市", favorite=0),
        _fake_photo("p_f3", names=["李丽"], scene="海边", desc="李丽在海边", favorite=1),
        _fake_photo("p_f4", names=[],       scene="公园", desc="无人风景",   favorite=0),
    ]
    for rec in photos:
        repo.upsert_photo(conn, rec)
        for p in rec["people"]["persons"]:
            cid = p["cluster_id"]
            conn.execute(
                "INSERT INTO faces(face_id, photo_id, cluster_id, embedding, bbox, created_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?)",
                (f"f_{rec['identity']['photo_id']}_{cid}", rec["identity"]["photo_id"], cid,
                 datetime.now().isoformat()),
            )
            if p.get("name"):
                conn.execute(
                    "INSERT OR REPLACE INTO persons(cluster_id, name, updated_at) VALUES (?, ?, ?)",
                    (cid, p["name"], datetime.now().isoformat()),
                )
    return conn


def _fake_photo(pid: str, *, names: list, scene: str, desc: str, favorite: int = 0) -> dict:
    """构造一个完整 6-group photo record(无 vision 真值,仅测试用)。favorite=1 标收藏。"""
    persons = [
        {"cluster_id": f"seed_{n.replace(' ', '_')}", "name": n, "action": "站立"}
        for n in names
    ]
    return {
        "schema_version": "0.1",
        "identity": {
            "photo_id": pid,
            "source": "filesystem",
            "source_ref": f"/fake/{pid}.jpg",
            "original_path": f"/fake/{pid}.jpg",
            "content_hash": pid,
            "file_size_bytes": 1000,
            "original_format": "jpeg",
            "sidecar_path": None,
            "preprocessed_path": f"/fake/.cache/{pid}.jpg",
        },
        "exif": {
            "captured_at_local": "2026-01-15T12:00:00",
            "captured_at_utc": "2026-01-15T04:00:00Z",
            "tz_offset_minutes": 480,
            "gps": None,
        },
        "vision": {
            "description": desc,
            "media_type": "photo",
            "subject": "single" if len(names) == 1 else ("group" if len(names) > 1 else "landscape"),
            "scene": scene,
            "objects": [],
            "tags": [scene],
            "ocr_text": "",
            "mood": "平静",
        },
        "people": {
            "persons": persons,
            "names": names,
            "face_count": len(names),
            "source_apple_persons": [],
        },
        "derived": {
            "time_bucket": {"year": 2026, "month": "2026-01", "season": "winter",
                             "time_of_day": "noon", "day_of_week": "thursday",
                             "is_weekend": False, "iso_week": "2026-W03"},
            "location_bucket": {"country": None, "city": None,
                                "place_name": scene, "is_home": False, "is_travel": False},
            "photo_type": "daily",
            "is_keeper": True,
            "favorite": favorite,
        },
        "meta": {
            "processed_at": "2026-01-15T05:00:00Z",
            "group_versions": {"vision": "test-v1"},
            "source_signals": {},
            "errors": [],
        },
    }
