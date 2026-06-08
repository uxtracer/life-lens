"""A 层测试:国外 GPS 地点解析(高德短路 + place_apple 兜底)。

覆盖 geocode/amap.py 的 parse_place_apple / reverse_geocode 短路 / _build_formatted_address,
以及 scanner/derived.py 的 elif place_apple 透传分支与安全回归。
全程无 Ollama / 无高德 key / 无 HTTP。
"""
from __future__ import annotations

import sqlite3

import pytest

from life_lens.geocode import amap
from life_lens.scanner import derived as derive_mod


# ---- parse_place_apple:结构化映射 ----

def test_parse_place_apple_four_segments():
    """4 段:首段=poi,末段=country,倒二=province,倒三=city。"""
    r = amap.parse_place_apple("环球影城, Universal City, 加利福尼亚, 美国")
    assert r == {
        "country": "美国", "province": "加利福尼亚", "city": "Universal City",
        "district": None, "township": None, "aoi_name": None,
        "poi_name": "环球影城", "place_name": "环球影城",
    }


def test_parse_place_apple_three_segments_no_poi():
    """3 段:无 poi(首段就是城市),place_name 退到城市。"""
    r = amap.parse_place_apple("巴黎, 法兰西岛, 法国")
    assert r["country"] == "法国" and r["province"] == "法兰西岛" and r["city"] == "巴黎"
    assert r["poi_name"] is None and r["place_name"] == "巴黎"

    r2 = amap.parse_place_apple("美瑛町, 北海道, 日本")
    assert r2["country"] == "日本" and r2["province"] == "北海道" and r2["city"] == "美瑛町"
    assert r2["poi_name"] is None


def test_parse_place_apple_single_segment():
    """1 段:只有国家。"""
    r = amap.parse_place_apple("日本")
    assert r["country"] == "日本"
    assert r["province"] is None and r["city"] is None and r["poi_name"] is None
    assert r["place_name"] is None


def test_parse_place_apple_robust():
    """None / 非 str / 空串 / 全空段 → None;脏空格 + 全角逗号兼容。"""
    assert amap.parse_place_apple(None) is None
    assert amap.parse_place_apple(123) is None
    assert amap.parse_place_apple("") is None
    assert amap.parse_place_apple("  ,  , ") is None
    r = amap.parse_place_apple(" 巴黎 ，  法兰西岛 ， 法国 ")  # 全角逗号 + 多余空格
    assert r["country"] == "法国" and r["city"] == "巴黎"


# ---- reverse_geocode:国外短路,不触缓存 ----

def test_reverse_geocode_out_of_china_short_circuits(monkeypatch):
    """巴黎坐标 → None,且短路发生在缓存读取之前(_cache_get 不应被调用)。"""
    def _boom(*a, **k):
        raise AssertionError("_cache_get 不应被调用:国外应在缓存前短路")
    monkeypatch.setattr(amap, "_cache_get", _boom)
    assert amap.reverse_geocode(48.8566, 2.3522, conn=None) is None  # 巴黎


def test_reverse_geocode_empty_shell_cache_is_miss(monkeypatch):
    """泰国(10.11,99.81)落在粗 bbox 内 → 不短路 → 命中只有 country 的空壳缓存 → 当 miss 返 None。"""
    shell = {"country": "中国", "province": None, "city": None, "district": None,
             "township": None, "aoi_name": None, "poi_name": None, "provider": "amap-gcj02"}
    monkeypatch.setattr(amap, "_cache_get", lambda *a, **k: shell)
    assert amap.reverse_geocode(10.116956, 99.813919, conn=None) is None


def test_reverse_geocode_poi_only_china_border_kept(monkeypatch):
    """中国边境 POI(德天瀑布:有 poi 无 province)仍判有效,原样返回。"""
    rec = {"country": "中国", "province": None, "city": None, "district": None,
           "township": None, "aoi_name": None, "poi_name": "德天跨国瀑布景区",
           "provider": "amap-gcj02"}
    monkeypatch.setattr(amap, "_cache_get", lambda *a, **k: rec)
    out = amap.reverse_geocode(22.855567, 106.723708, conn=None)
    assert out is not None and out["poi_name"] == "德天跨国瀑布景区"


def test_has_location():
    assert amap._has_location({"poi_name": "x"})
    assert amap._has_location({"city": "北京"})
    assert not amap._has_location({"country": "中国"})  # 只有国家 = 空壳
    assert not amap._has_location({})


# ---- _build_formatted_address:国外 / 中国回归 ----

def test_build_formatted_foreign_multi():
    rec = {"country": "美国", "province": "加利福尼亚", "city": "Universal City",
           "poi_name": "环球影城"}
    assert amap._build_formatted_address(rec) == "美国 · 加利福尼亚 · Universal City · 环球影城"


def test_build_formatted_foreign_country_only():
    assert amap._build_formatted_address({"country": "日本"}) == "日本"


def test_build_formatted_china_unchanged():
    """中国行字节不变:行政拼接 · poi。"""
    assert amap._build_formatted_address(
        {"country": "中国", "city": "北京", "poi_name": "颐和园"}) == "北京 · 颐和园"
    assert amap._build_formatted_address(
        {"province": "河北省", "city": "承德市", "district": "滦平县", "township": "涝洼镇",
         "aoi_name": "阿那亚金山岭", "poi_name": "阿那亚山谷音乐厅"}
    ) == "河北省承德市滦平县涝洼镇 · 阿那亚金山岭 · 阿那亚山谷音乐厅"


def test_build_formatted_no_country_no_lead():
    """country=None 不加段(回到中国分支),无任何字段 → None。"""
    assert amap._build_formatted_address({"country": None}) is None
    assert amap._build_formatted_address({}) is None


# ---- derived 透传:elif place_apple + 安全回归 ----

def _photo(gps, place_apple):
    return {
        "exif": {"captured_at_local": "2020-05-01T12:00:00", "gps": gps},
        "vision": {},
        "meta": {"source_signals": ({"place_apple": place_apple} if place_apple else {})},
    }


def test_derived_foreign_gps_apple_fills_china_parity(monkeypatch):
    """国外 GPS + apple:reverse 返 None,bucket 字段集与国内一致且值正确。"""
    monkeypatch.setattr(amap, "reverse_geocode", lambda *a, **k: None)  # 国外短路
    rec = _photo({"lat": 34.14, "lng": -118.35}, "环球影城, Universal City, 加利福尼亚, 美国")
    lb = derive_mod._location_bucket(rec["exif"], rec["meta"], conn=None)
    assert lb["country"] == "美国"
    assert lb["province"] == "加利福尼亚" and lb["city"] == "Universal City"
    assert lb["poi_name"] == "环球影城" and lb["place_name"] == "环球影城"
    assert lb["formatted_address"] == "美国 · 加利福尼亚 · Universal City · 环球影城"


def test_derived_domestic_gps_apple_does_not_override(monkeypatch):
    """安全回归(最重要):国内 GPS reverse 成功 → 全部字段来自高德,apple 绝不覆盖。"""
    china = {"country": "中国", "province": "北京", "city": "北京", "district": None,
             "township": None, "aoi_name": None, "poi_name": "颐和园",
             "place_name": "颐和园", "formatted_address": "北京 · 颐和园"}
    monkeypatch.setattr(amap, "reverse_geocode", lambda *a, **k: china)
    # 故意给一个会"污染"的 apple 串,断言它不生效
    rec = _photo({"lat": 39.99, "lng": 116.27}, "假地点, 假城市, 假省, 美国")
    lb = derive_mod._location_bucket(rec["exif"], rec["meta"], conn=None)
    assert lb["country"] == "中国"
    assert lb["poi_name"] == "颐和园" and lb["formatted_address"] == "北京 · 颐和园"


def test_derived_foreign_gps_no_apple_country_none(monkeypatch):
    """国外 GPS 但无 apple:country 不被硬填中国,留 None(graceful)。"""
    monkeypatch.setattr(amap, "reverse_geocode", lambda *a, **k: None)
    rec = _photo({"lat": 48.85, "lng": 2.35}, None)
    lb = derive_mod._location_bucket(rec["exif"], rec["meta"], conn=None)
    # GPS 存在 → bucket 非 None,但所有字段空(不硬填中国、不造 formatted)
    assert lb["country"] is None and lb["formatted_address"] is None
