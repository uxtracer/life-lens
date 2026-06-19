"""未命名面孔归并到已有人物。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from life_lens.scanner.reprocess import refill_people_from_faces
from life_lens.store import db, repo


def test_merge_apple_unknown_face_into_named_person(populated_db):
    conn = populated_db
    source_cid = "apple_face:unknown-example"
    target_cid = "seed_张三"
    conn.execute(
        "INSERT INTO faces(face_id, photo_id, cluster_id, embedding, bbox, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("f_unknown", "p_005", source_cid, b"", "[0,0,10,10]", datetime.now().isoformat()),
    )

    affected = repo.merge_face_clusters(conn, source_cid, target_cid)
    refill_people_from_faces(conn, affected)

    assert affected == ["p_005"]
    assert conn.execute(
        "SELECT cluster_id FROM faces WHERE face_id = 'f_unknown'"
    ).fetchone()[0] == target_cid
    people = json.loads(conn.execute(
        "SELECT people FROM photos WHERE photo_id = 'p_005'"
    ).fetchone()[0])
    assert people["persons"] == [
        {"cluster_id": target_cid, "name": "张三", "action": ""}
    ]
    assert people["names"] == ["张三"]


def test_merge_requires_named_target(populated_db):
    conn = populated_db
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO faces(face_id, photo_id, cluster_id, embedding, bbox, created_at) "
        "VALUES ('f_source', 'p_005', 'c_source', X'', NULL, ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO faces(face_id, photo_id, cluster_id, embedding, bbox, created_at) "
        "VALUES ('f_target', 'p_004', 'c_target', X'', NULL, ?)",
        (now,),
    )

    with pytest.raises(ValueError, match="目标必须是已命名面孔"):
        repo.merge_face_clusters(conn, "c_source", "c_target")


def test_naming_with_existing_name_automatically_merges(tmp_path: Path):
    """即使用户在“新建人物”里输入已有姓名,也不能产生同名孤岛。"""
    from life_lens.web.server import create_app

    app = create_app(tmp_path)
    conn = db.connect(db.get_db_path(tmp_path))
    identity = {
        "photo_id": "p_unknown",
        "source": "photos_library",
        "source_ref": "p_unknown",
        "original_path": "/fake/p_unknown.jpg",
        "content_hash": "p_unknown",
    }
    repo.ensure_photo_row(conn, identity)
    people = {
        "persons": [{"cluster_id": "apple_face:one", "name": None, "action": "望向远处"}],
        "names": [],
        "face_count": 1,
        "source_apple_persons": [],
    }
    conn.execute(
        "UPDATE photos SET people = ? WHERE photo_id = ?",
        (json.dumps(people, ensure_ascii=False), "p_unknown"),
    )
    conn.execute(
        "INSERT INTO faces(face_id, photo_id, cluster_id, embedding, bbox, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("f_unknown_api", "p_unknown", "apple_face:one", b"", "[0,0,10,10]", repo.now_iso()),
    )
    repo.set_person_name(conn, "seed_existing", "张三")
    conn.close()

    with TestClient(app) as client:
        response = client.post(
            "/api/persons/apple_face%3Aone/name",
            json={"name": "张三"},
        )
    assert response.status_code == 200
    assert response.json()["merged"] is True
    assert response.json()["cluster_id"] == "seed_existing"

    conn = db.connect(db.get_db_path(tmp_path))
    assert conn.execute(
        "SELECT cluster_id FROM faces WHERE face_id = 'f_unknown_api'"
    ).fetchone()[0] == "seed_existing"
    updated = json.loads(conn.execute(
        "SELECT people FROM photos WHERE photo_id = 'p_unknown'"
    ).fetchone()[0])
    assert updated["persons"] == [
        {"cluster_id": "seed_existing", "name": "张三", "action": "望向远处"}
    ]
    conn.close()
