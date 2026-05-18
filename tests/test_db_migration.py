"""db schema 版本号 + 升级 + 幂等性测试。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from life_lens.store import db


def test_init_schema_on_empty_db(tmp_path: Path):
    """全新空 db init 后:user_version = SCHEMA_VERSION,所有期望表存在。"""
    db_path = tmp_path / "fresh.db"
    conn = db.connect(db_path)
    assert db._get_user_version(conn) == 0   # 新 db 默认 0

    db.init_schema(conn)
    assert db._get_user_version(conn) == db.SCHEMA_VERSION

    # 所有核心表存在
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    expected = {"photos", "jobs", "scan_runs", "amap_quota", "sources",
                "faces", "persons", "photo_embeddings"}
    assert expected.issubset(tables), f"缺表: {expected - tables}"

    # FTS 虚表
    fts_tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'photos_fts%'"
    ).fetchall()}
    assert "photos_fts" in fts_tables


def test_init_schema_idempotent(tmp_path: Path):
    """连续 init 多次不报错,user_version 保持 SCHEMA_VERSION。"""
    db_path = tmp_path / "idem.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    db.init_schema(conn)
    db.init_schema(conn)
    assert db._get_user_version(conn) == db.SCHEMA_VERSION


def test_init_schema_no_auto_backup_for_fresh_db(tmp_path: Path):
    """新 db(< 4KB)init 不应触发 auto-backup。"""
    db_path = tmp_path / "tiny.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    backups = list((db_path.parent / "backups").glob("auto-*.db")) if (db_path.parent / "backups").exists() else []
    assert backups == [], f"新 db 不应该备份,实际: {backups}"


def test_init_schema_upgrades_old_db_to_v1(tmp_path: Path):
    """模拟老 db(user_version=0,只有部分表)→ init → 升到 v1 + 自动备份。"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    # 模拟老 schema:photos 表存在(含所有生成列,这部分跟当前一致),
    # 但缺新表(photo_embeddings / scan_runs / amap_quota / persons.age_estimate 等)
    # 也不写 PRAGMA user_version(0 = 未追踪)
    conn.executescript("""
        CREATE TABLE photos (
            photo_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            original_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT '0.1',
            identity TEXT NOT NULL,
            exif TEXT,
            vision TEXT,
            people TEXT,
            derived TEXT,
            meta TEXT NOT NULL,
            captured_at_utc TEXT GENERATED ALWAYS AS (json_extract(exif, '$.captured_at_utc')) STORED,
            media_type      TEXT GENERATED ALWAYS AS (json_extract(vision, '$.media_type')) STORED,
            is_keeper       INTEGER GENERATED ALWAYS AS (json_extract(derived, '$.is_keeper')) STORED,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        -- 老的 jobs 表(没有 run_id / captured_at_local,模拟 v0 → v1 ALTER 路径)
        CREATE TABLE jobs (
            photo_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            stage TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            enqueued_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );
    """)
    # 塞够数据 > 4KB 触发备份
    import json
    for i in range(50):
        conn.execute(
            "INSERT INTO photos(photo_id, source, source_ref, original_path, content_hash, "
            "schema_version, identity, meta, created_at, updated_at) "
            "VALUES (?, 'filesystem', ?, ?, ?, '0.1', '{}', '{}', '2026-01-01', '2026-01-01')",
            (f"p_{i:03d}", f"/x/{i}.jpg", f"/x/{i}.jpg", f"hash_{i}_pad_padding_padding"),
        )
    conn.commit()
    conn.close()
    assert db_path.stat().st_size > 4096

    # 重新打开 + init
    conn = db.connect(db_path)
    assert db._get_user_version(conn) == 0
    db.init_schema(conn)
    assert db._get_user_version(conn) == db.SCHEMA_VERSION

    # 新表应该被创建
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "photo_embeddings" in tables   # v1 新加的表
    assert "scan_runs" in tables

    # 自动备份应该生成
    backups = sorted((db_path.parent / "backups").glob("auto-pre-*.db"))
    assert len(backups) == 1, f"期望 1 个 auto-backup,实际 {len(backups)}"
    assert "from-v0" in backups[0].name
