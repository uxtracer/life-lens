"""A 层测试:相册名解析 + 缓存 + merge + 已扫库回填(全程 mock LLM,无 Ollama)。

覆盖 scanner/album.py 与 scanner/reprocess.py::reprocess_albums。
固件相册名一律用占位名/公共景点(三亚/颐和园),不含真名。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from life_lens.scanner import album as album_mod
from life_lens.store import repo


# ---- 假 LLM:返回 raw Ollama 格式 {city, province, event_tags},并记录调用次数 ----

class FakeLLM:
    def __init__(self, table: dict):
        self.table = table          # album_name -> {city, province, event_tags}
        self.calls: list[str] = []

    def __call__(self, name: str):
        self.calls.append(name)
        return self.table.get(name)  # None 模拟解析失败


# ---- parse_album / 缓存 ----

def test_parse_album_caches_one_llm_call(empty_db: sqlite3.Connection):
    llm = FakeLLM({"2015.4.19-颐和园": {"city": "北京", "province": "北京",
                                        "place": "颐和园", "event_tags": ["旅行"]}})
    r1 = album_mod.parse_album("2015.4.19-颐和园", empty_db, llm=llm)
    r2 = album_mod.parse_album("2015.4.19-颐和园", empty_db, llm=llm)
    assert r1 == {"country": None, "city": "北京", "province": "北京",
                  "place": "颐和园", "tags": ["旅行"]}
    assert r2 == r1
    assert llm.calls == ["2015.4.19-颐和园"]  # 第二次命中缓存,不再调 LLM


def test_parse_album_generic_skips_llm(empty_db: sqlite3.Connection):
    llm = FakeLLM({})
    for name in ("Favorites", "截屏", "最近添加"):
        r = album_mod.parse_album(name, empty_db, llm=llm)
        assert r == {"country": None, "city": None, "province": None, "place": None, "tags": []}
    assert llm.calls == []  # 通用相册名不调 LLM


def test_parse_album_date_prefix_stripped():
    assert album_mod._strip_date_prefix("2015.2.14-三亚") == "三亚"
    assert album_mod._strip_date_prefix("2015年3月-颐和园") == "颐和园"
    assert album_mod._strip_date_prefix("2013.7.6 夏青岛") == "夏青岛"
    assert album_mod._strip_date_prefix("颐和园") == "颐和园"


def test_parse_album_llm_fail_not_cached(empty_db: sqlite3.Connection):
    llm = FakeLLM({})  # 表里没有 → 返回 None(解析失败)
    r1 = album_mod.parse_album("某园区秋游", empty_db, llm=llm)
    assert r1 == {"country": None, "city": None, "province": None, "place": None, "tags": []}
    # 失败不写缓存 → 再调一次仍会触发 LLM
    album_mod.parse_album("某园区秋游", empty_db, llm=llm)
    assert llm.calls == ["某园区秋游", "某园区秋游"]


# ---- signals_for_albums 聚合 ----

def test_signals_aggregate_city_first_tags_dedup(empty_db: sqlite3.Connection):
    llm = FakeLLM({
        "2015.2.14-三亚":   {"city": "三亚", "province": "海南", "place": None,
                            "event_tags": ["旅行", "海边"]},
        "全家福":           {"city": None,  "province": None,   "place": None,
                            "event_tags": ["旅行", "聚会"]},
    })
    sig = album_mod.signals_for_albums(["全家福", "2015.2.14-三亚"], empty_db, llm=llm)
    assert sig["city"] == "三亚"        # 取首个非空 city
    assert sig["province"] == "海南"
    assert sig["place"] is None        # 只是城市,无具体景点
    assert sig["tags"] == ["旅行", "聚会", "海边"]  # 顺序保留 + 去重


# ---- merge_album_tags 幂等 ----

def test_merge_album_tags_idempotent(empty_db: sqlite3.Connection):
    llm = FakeLLM({"生日相册": {"city": None, "province": None, "place": None,
                              "event_tags": ["生日", "聚会"]}})
    vision = {"tags": ["公园", "生日"]}
    album_mod.merge_album_tags(vision, ["生日相册"], empty_db, llm=llm)
    assert vision["tags"] == ["公园", "生日", "聚会"]  # 已有的 生日 不重复
    # 再 merge 一次幂等
    album_mod.merge_album_tags(vision, ["生日相册"], empty_db, llm=llm)
    assert vision["tags"] == ["公园", "生日", "聚会"]


def test_merge_album_tags_none_vision(empty_db: sqlite3.Connection):
    assert album_mod.merge_album_tags(None, ["x"], empty_db, llm=FakeLLM({})) is None


# ---- reprocess_albums:已扫库回填 ----

def _photo_with_albums(pid: str, albums: list, *, desc="某活动现场", tags=None) -> dict:
    return {
        "schema_version": "0.1",
        "identity": {
            "photo_id": pid, "source": "photos_library", "source_ref": pid,
            "original_path": f"/fake/{pid}.heic", "content_hash": pid,
            "file_size_bytes": 1000, "original_format": "heic",
            "sidecar_path": None, "preprocessed_path": f"/fake/.cache/{pid}.jpg",
        },
        "exif": {"captured_at_local": "2015-02-14T12:00:00", "captured_at_utc": "",
                 "tz_offset_minutes": None, "gps": None},          # 无 GPS
        "vision": {"description": desc, "media_type": "photo", "subject": "single",
                   "scene": "户外", "objects": [], "tags": list(tags or ["户外"]),
                   "ocr_text": "", "mood": "平静"},
        "people": {"persons": [], "names": [], "face_count": 0, "source_apple_persons": []},
        "derived": {"time_bucket": None,
                    "location_bucket": {"country": None, "city": None, "place_name": None,
                                        "is_home": False, "is_travel": False,
                                        "location_source": None}},
        "meta": {"processed_at": "2015-02-14T05:00:00Z",
                 "group_versions": {"vision": "test-v1"},
                 "source_signals": {"albums": albums}, "errors": []},
    }


def _seed_cache(conn, name, city, province, place, tags, country=None):
    album_mod._write_cache(conn, name,
                           {"country": country, "city": city, "province": province,
                            "place": place, "tags": tags})


def test_reprocess_albums_dry_run_no_write(tmp_path: Path, monkeypatch):
    from life_lens.store import db
    from life_lens.scanner import reprocess
    root = tmp_path
    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    repo.upsert_photo(conn, _photo_with_albums("a1", ["2015.4.19-颐和园"]))
    _seed_cache(conn, "2015.4.19-颐和园", "北京", "北京", "颐和园", ["旅行"])
    conn.commit()
    conn.close()

    res = reprocess.reprocess_albums(root, ["a1"], dry_run=True)
    assert res["ok"] and res["dry_run"] and res["done"] == 1
    d = res["details"][0]
    assert d["city_before"] is None and d["city_after"] == "北京"
    assert "旅行" in d["tags_after"] and "旅行" not in d["tags_before"]
    # 景点 POI 进 place_name / formatted_address(跟 GPS 路径同格式)
    assert d["place_name_after"] == "颐和园"
    assert d["formatted_address_after"] == "北京 · 颐和园"

    # dry_run 不写库:重新查 vision/derived 仍是旧值
    conn2 = db.connect(db.get_db_path(root))
    row = conn2.execute("SELECT vision, derived FROM photos WHERE photo_id='a1'").fetchone()
    assert "旅行" not in (json.loads(row[0])["tags"])
    assert (json.loads(row[1])["location_bucket"] or {}).get("city") is None
    conn2.close()


def test_reprocess_albums_writes_city_place_tags_fts(tmp_path: Path, monkeypatch):
    from life_lens.store import db
    from life_lens.scanner import reprocess, runner
    monkeypatch.setattr(runner, "_get_embedder_for_scan", lambda: None)  # 不加载 fastembed

    root = tmp_path
    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    rec = _photo_with_albums("b1", ["2015.4.19-颐和园"], tags=["晴天"])
    repo.upsert_photo(conn, rec)
    repo.update_fts(conn, "b1", rec["vision"], rec["people"])
    _seed_cache(conn, "2015.4.19-颐和园", "北京", "北京", "颐和园", ["旅行"])
    conn.commit()
    conn.close()

    res = reprocess.reprocess_albums(root, ["b1"], dry_run=False)
    assert res["ok"] and res["done"] == 1 and res["city_filled"] == 1 and res["tags_added"] == 1

    conn2 = db.connect(db.get_db_path(root))
    row = conn2.execute("SELECT vision, derived FROM photos WHERE photo_id='b1'").fetchone()
    lb = json.loads(row[1])["location_bucket"]
    assert lb["city"] == "北京"
    assert lb["poi_name"] == "颐和园" and lb["place_name"] == "颐和园"
    assert lb["formatted_address"] == "北京 · 颐和园"
    assert "location_source" not in lb            # 字段已移除
    assert "旅行" in json.loads(row[0])["tags"]
    # album 事件词进了 FTS tags 列(POI 颐和园 走 location,不进全文,跟 GPS 路径一致)
    fts = conn2.execute("SELECT tags FROM photos_fts WHERE photo_id='b1'").fetchone()
    assert fts and "旅行" in fts[0]
    conn2.close()


def test_reprocess_albums_no_albums_skips(tmp_path: Path):
    from life_lens.store import db
    from life_lens.scanner import reprocess
    root = tmp_path
    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    repo.upsert_photo(conn, _photo_with_albums("c1", []))  # 无 albums
    conn.commit()
    conn.close()

    res = reprocess.reprocess_albums(root, ["c1"], dry_run=True)
    assert res["no_albums"] == 1 and res["done"] == 0


def test_album_country_only_fills_country_not_city(tmp_path: Path):
    """国外相册只有 country(日本):填 location_bucket.country,city/place_name 留空。"""
    from life_lens.store import db
    from life_lens.scanner import derived as derive_mod
    root = tmp_path
    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    _seed_cache(conn, "2015.9.16-日本", None, None, None, [], country="日本")
    conn.commit()

    rec = _photo_with_albums("d1", ["2015.9.16-日本"])
    lb = derive_mod.compute(rec, conn=conn)["location_bucket"]
    assert lb["country"] == "日本"
    assert lb["city"] is None and lb["place_name"] is None
    conn.close()
