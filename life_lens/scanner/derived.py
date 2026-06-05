"""derived group:纯规则派生 + reverse geocoding。

vision 接入后补 photo_type / is_keeper。
location_bucket 调高德 geocode/amap.py 拿到 country/province/city/district/aoi_name(无 key 或失败 graceful 降级 — 字段全 null)。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)


SEASONS_N = {12: "winter", 1: "winter", 2: "winter",
             3: "spring", 4: "spring", 5: "spring",
             6: "summer", 7: "summer", 8: "summer",
             9: "autumn", 10: "autumn", 11: "autumn"}


def compute(record: dict, conn: Optional[sqlite3.Connection] = None) -> dict:
    """计算 derived group。conn 用于 geocode 缓存(可选,但强烈推荐传)。"""
    exif = record.get("exif") or {}
    vision = record.get("vision") or {}
    ss = (record.get("meta") or {}).get("source_signals") or {}
    return {
        "time_bucket":     _time_bucket(exif),
        "location_bucket": _location_bucket(exif, record.get("meta", {}), conn),
        "photo_type":      _photo_type(vision),
        "is_keeper":       _is_keeper(vision),
        # Apple 收藏镜像到 derived,供 favorite 生成列/查询用(铁律:可查字段进 derived)
        "favorite":        1 if ss.get("favorite") else 0,
    }


def _time_bucket(exif: dict) -> Optional[dict]:
    local_str = exif.get("captured_at_local")
    if not local_str:
        return None
    try:
        dt = datetime.fromisoformat(local_str)
    except Exception:
        return None
    hour = dt.hour
    if   5  <= hour < 11: tod = "morning"
    elif 11 <= hour < 14: tod = "noon"
    elif 14 <= hour < 18: tod = "afternoon"
    elif 18 <= hour < 22: tod = "evening"
    elif 22 <= hour or hour < 2: tod = "night"
    else: tod = "late_night"
    iso_year, iso_week, _ = dt.isocalendar()
    return {
        "year":         dt.year,
        "month":        dt.strftime("%Y-%m"),
        "iso_week":     f"{iso_year}-W{iso_week:02d}",
        "season":       SEASONS_N.get(dt.month, "unknown"),
        "time_of_day":  tod,
        "day_of_week":  dt.strftime("%A").lower(),
        "is_weekend":   dt.weekday() >= 5,
    }


def _location_bucket(exif: dict, meta: dict, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """优先级:GPS reverse(高德)> Apple place 透传 > album 名推断(无 GPS 老照片兜底)。

    album 兜底只在 GPS/Apple 都拿不到城市时填,city/place 走相册名解析,
    place_name / formatted_address 跟 GPS 路径同格式(复用 amap._build_formatted_address)。
    相册名 ≠ 精确拍摄地,只到城市/景点颗粒度;绝不覆盖真实地点。见 scanner/album.py。
    """
    ss = meta.get("source_signals") or {}
    place_apple = ss.get("place_apple")
    albums = ss.get("albums") or []
    gps = exif.get("gps")

    # album 国家/城市/景点兜底(仅本地缓存/LLM,需要 conn)
    album_country = None
    album_city = None
    album_province = None
    album_place = None
    if conn is not None and albums:
        try:
            from . import album as album_mod
            sig = album_mod.signals_for_albums(albums, conn)
            album_country = sig.get("country")
            album_city = sig.get("city")
            album_province = sig.get("province")
            album_place = sig.get("place")
        except Exception as e:
            log.warning("album signals failed: %s", e)

    if not gps and not place_apple and not album_city and not album_country:
        return None

    bucket = {
        "country":     None,
        "province":    None,
        "city":        None,
        "district":    None,
        "township":    None,
        "aoi_name":    None,
        "poi_name":    None,
        "place_name":  place_apple,            # fallback
        "formatted_address": None,
        "is_home":     False,
        "is_travel":   False,
    }

    if gps and isinstance(gps, dict) and gps.get("lat") is not None and gps.get("lng") is not None:
        try:
            from ..geocode.amap import reverse_geocode
            parsed = reverse_geocode(float(gps["lat"]), float(gps["lng"]), conn=conn)
        except Exception as e:
            log.warning("reverse_geocode failed: %s", e)
            parsed = None
        if parsed:
            for k in ("country", "province", "city", "district", "township",
                      "aoi_name", "poi_name", "formatted_address"):
                bucket[k] = parsed.get(k)
            # place_name 首选 AOI > POI > Apple 透传
            bucket["place_name"] = parsed.get("place_name") or place_apple

    # album 兜底:GPS reverse 没拿到地点时,用相册名推断的国家/城市/景点填。
    if not bucket["country"] and album_country:
        bucket["country"] = album_country
    if not bucket["city"] and album_city:
        bucket["city"] = album_city
        if album_province and not bucket["province"]:
            bucket["province"] = album_province
        if album_place:
            bucket["poi_name"] = album_place
        # place_name 只放真 POI/AOI(跟 GPS 路径一致),只有城市时留空,不拿城市名顶替
        bucket["place_name"] = bucket["poi_name"] or bucket["aoi_name"]
        from ..geocode.amap import _build_formatted_address
        bucket["formatted_address"] = _build_formatted_address(bucket)

    return bucket


def _photo_type(vision: dict) -> Optional[str]:
    """vision 未接入(Phase 0)则为 null。"""
    if not vision:
        return None
    mt = vision.get("media_type")
    if mt in ("screenshot", "other"):
        return mt
    if mt != "photo":
        return None
    # photo 子分类规则
    subject = vision.get("subject")
    tags = vision.get("tags") or []
    scene = vision.get("scene") or ""
    if subject == "food" or any(t in tags for t in ("美食", "食物")):
        return "food"
    if subject == "pet":
        return "pet"
    if any(t in tags for t in ("婚礼", "演唱会", "会议", "聚会", "毕业")):
        return "event"
    if subject == "single":
        return "selfie"
    if subject in ("landscape", "object"):
        return "travel"
    return "daily"


def _is_keeper(vision: dict) -> Optional[bool]:
    if not vision:
        return None
    return vision.get("media_type") == "photo"
