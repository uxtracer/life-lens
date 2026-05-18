"""高德地图 reverse geocoding (GPS → 国家/省/市/区/街道/AOI/POI)。

设计:
- key 从 ~/.life_lens/config.json 或 env AMAP_KEY 拿(不入 git)
- 网格缓存:lat/lng 各取小数 4 位(~11m),同一栋楼/同一景区只调一次
- 缓存表 geocode_cache(SQLite,life_lens.db 内)
- 失败/无 key 时返回 None,**graceful degradation** — 不阻塞 derived 其他字段

调用对照:
  https://restapi.amap.com/v3/geocode/regeo?key=&location=lng,lat&poi=1&extensions=all

注意:用户原始 GPS 是 WGS-84,高德用 GCJ-02(火星坐标)。
直接传 WGS-84 给高德有 50-500m 偏移,但 reverse geocoding 找 AOI/POI
受影响很小(景区/小区 AOI 范围远大于偏移)。**不做坐标系转换**,保持简单。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Optional

import requests

log = logging.getLogger(__name__)

AMAP_REGEO_URL = "https://restapi.amap.com/v3/geocode/regeo"
DEFAULT_RADIUS = 500          # 米
GRID_STEP_DEG = 0.0005         # ~55m 步长,坐标量化为该步长整数倍 → 同一商场/景区合并到一条缓存
PROVIDER = "amap-gcj02"        # WGS-84 → GCJ-02 转换后调用高德;隔离旧 'amap'/'amap-50m'(未转换 ~600m 偏移)的错误缓存

# 高德个人免费每日 5000 次;留 200 buffer 防重试 / 误差,设上限 4800
AMAP_DAILY_LIMIT = 4800
SHANGHAI_TZ_OFFSET_HOURS = 8


def get_amap_key() -> Optional[str]:
    """读 key 顺序:env AMAP_KEY → ~/.life_lens/config.json → None。"""
    env = os.environ.get("AMAP_KEY")
    if env:
        return env.strip()
    from ..store import config as cfg_store
    key = cfg_store.load_config().get("amap_key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def _grid_key(lat: float, lng: float) -> tuple[str, str]:
    """50m 网格量化:坐标 round 到 GRID_STEP_DEG 整数倍,末位强制为 0 或 5。

    用 WGS-84(用户 EXIF 输入侧)做 key,语义清晰;查高德前再转 GCJ-02。
    """
    lat_q = round(lat / GRID_STEP_DEG) * GRID_STEP_DEG
    lng_q = round(lng / GRID_STEP_DEG) * GRID_STEP_DEG
    return (f"{lat_q:.4f}", f"{lng_q:.4f}")


# ---------- WGS-84 → GCJ-02(中国"火星坐标"加密)----------
# iPhone EXIF 是 WGS-84,高德用 GCJ-02。中国大陆两者偏移 300-700m,
# 直接传 WGS-84 给高德会把偏远小景点(几十米直径)定位到旁边,丢失 POI。
# 国境外不做转换。
import math as _math

_GCJ_A = 6378245.0
_GCJ_EE = 0.00669342162296594323


def _out_of_china(lat: float, lng: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _gcj_transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0*x + 3.0*y + 0.2*y*y + 0.1*x*y + 0.2*_math.sqrt(abs(x))
    ret += (20.0*_math.sin(6.0*x*_math.pi) + 20.0*_math.sin(2.0*x*_math.pi))*2.0/3.0
    ret += (20.0*_math.sin(y*_math.pi)     + 40.0*_math.sin(y/3.0*_math.pi))*2.0/3.0
    ret += (160.0*_math.sin(y/12.0*_math.pi) + 320*_math.sin(y*_math.pi/30.0))*2.0/3.0
    return ret


def _gcj_transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0*y + 0.1*x*x + 0.1*x*y + 0.1*_math.sqrt(abs(x))
    ret += (20.0*_math.sin(6.0*x*_math.pi) + 20.0*_math.sin(2.0*x*_math.pi))*2.0/3.0
    ret += (20.0*_math.sin(x*_math.pi)     + 40.0*_math.sin(x/3.0*_math.pi))*2.0/3.0
    ret += (150.0*_math.sin(x/12.0*_math.pi) + 300.0*_math.sin(x/30.0*_math.pi))*2.0/3.0
    return ret


def wgs84_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02。国境外原样返回。"""
    if _out_of_china(lat, lng):
        return lat, lng
    dlat = _gcj_transform_lat(lng - 105.0, lat - 35.0)
    dlng = _gcj_transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _math.pi
    magic = _math.sin(radlat); magic = 1 - _GCJ_EE * magic * magic
    sqrtmagic = _math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_GCJ_A * (1 - _GCJ_EE)) / (magic * sqrtmagic) * _math.pi)
    dlng = (dlng * 180.0) / (_GCJ_A / sqrtmagic * _math.cos(radlat) * _math.pi)
    return lat + dlat, lng + dlng


# ---------- 每日配额(Asia/Shanghai)----------

def _today_local() -> str:
    """返回 UTC+8 的 YYYY-MM-DD。"""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=SHANGHAI_TZ_OFFSET_HOURS))).strftime("%Y-%m-%d")


def _next_reset_at() -> str:
    """次日 00:00 UTC+8 的 ISO 字符串(给前端展示用)。"""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=SHANGHAI_TZ_OFFSET_HOURS))
    now = datetime.now(tz)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat(timespec="seconds")


def _quota_today_count(conn: sqlite3.Connection) -> int:
    if conn is None:
        return 0
    today = _today_local()
    row = conn.execute("SELECT count FROM amap_quota WHERE date_local=?", (today,)).fetchone()
    return int(row[0]) if row else 0


def _quota_inc(conn: sqlite3.Connection) -> int:
    """配额计数 +1(原子 UPSERT)。返回更新后的值。"""
    if conn is None:
        return 0
    from datetime import datetime, timezone
    today = _today_local()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn.execute(
        """
        INSERT INTO amap_quota(date_local, count, last_at)
        VALUES (?, 1, ?)
        ON CONFLICT(date_local) DO UPDATE SET count = count + 1, last_at = excluded.last_at
        """,
        (today, now),
    )
    return _quota_today_count(conn)


def is_quota_exhausted(conn: Optional[sqlite3.Connection]) -> bool:
    """今日(UTC+8)配额是否已耗尽。"""
    if conn is None:
        return False
    return _quota_today_count(conn) >= AMAP_DAILY_LIMIT


def quota_status(conn: Optional[sqlite3.Connection]) -> dict:
    """供 /api/status 用。"""
    used = _quota_today_count(conn) if conn is not None else 0
    return {
        "used": used,
        "limit": AMAP_DAILY_LIMIT,
        "exhausted": used >= AMAP_DAILY_LIMIT,
        "date_local": _today_local(),
        "next_reset_at": _next_reset_at(),
    }


def _init_cache_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS geocode_cache (
        lat_grid    TEXT NOT NULL,
        lng_grid    TEXT NOT NULL,
        provider    TEXT NOT NULL,
        result      TEXT NOT NULL,
        fetched_at  TEXT NOT NULL,
        PRIMARY KEY (lat_grid, lng_grid, provider)
    )
    """)
    conn.commit()


def _cache_get(conn: sqlite3.Connection, lat: float, lng: float) -> Optional[dict]:
    if conn is None:
        return None
    _init_cache_table(conn)
    lat_g, lng_g = _grid_key(lat, lng)
    row = conn.execute(
        "SELECT result FROM geocode_cache WHERE lat_grid=? AND lng_grid=? AND provider=?",
        (lat_g, lng_g, PROVIDER),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _cache_set(conn: sqlite3.Connection, lat: float, lng: float, result: dict) -> None:
    if conn is None:
        return
    _init_cache_table(conn)
    lat_g, lng_g = _grid_key(lat, lng)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn.execute("""
    INSERT OR REPLACE INTO geocode_cache(lat_grid, lng_grid, provider, result, fetched_at)
    VALUES (?, ?, ?, ?, ?)
    """, (lat_g, lng_g, PROVIDER, json.dumps(result, ensure_ascii=False), now))
    conn.commit()


def _extract(regeo: dict) -> dict:
    """从高德 regeo 响应里挑我们要的字段。"""
    ac = regeo.get("addressComponent") or {}
    aois = regeo.get("aois") or []
    pois = regeo.get("pois") or []

    # 城市:高德对北京/上海/重庆/天津这种直辖市,city 是空数组,要取 province
    city = ac.get("city")
    if isinstance(city, list) and not city:
        city = ac.get("province")
    if isinstance(city, list):
        city = None

    province = ac.get("province")
    if isinstance(province, list):
        province = None

    district = ac.get("district")
    if isinstance(district, list):
        district = None

    township = ac.get("township")
    if isinstance(township, list):
        township = None

    # AOI 首选:dist=0 表示在 AOI 边界内(景区/小区/校园),代表"在这片区域"
    aoi_name = None
    if aois:
        aoi_name = aois[0].get("name")

    # POI:**距离最近的**(高德返回顺序是按权重不是距离 — pois[0] 经常是"主景区名"
    # 而真实最近的具体地标在数组中间,比如阿那亚金山岭场景下 pois[0]='阿那亚金山岭' 但
    # dist=418m,真实最近 'pois[2]=阿那亚山谷音乐厅 dist=24m' 才是用户真实在的位置)
    poi_name = None
    if pois:
        def _dist(p):
            try: return float(p.get("distance") or "999999")
            except Exception: return 999999.0
        nearest = min(pois, key=_dist)
        poi_name = nearest.get("name")

    return {
        "country":    ac.get("country") or "中国",
        "province":   province,
        "city":       city,
        "district":   district,
        "township":   township,
        "aoi_name":   aoi_name,
        "poi_name":   poi_name,
        "provider":   PROVIDER,
        # 注:place_name / formatted_address 不入缓存,reverse_geocode 出口实时派生,
        # 这样改"优先策略"(POI 优先 vs AOI 优先 vs 距离阈值)都不必失效缓存
    }


def _derive_place_and_formatted(rec: dict) -> dict:
    """在 reverse_geocode 出口注入 place_name + formatted_address。"""
    out = dict(rec)
    out["place_name"] = rec.get("poi_name") or rec.get("aoi_name")
    out["formatted_address"] = _build_formatted_address(out)
    return out


def _build_formatted_address(rec: dict) -> Optional[str]:
    """规范的 formatted: '河北省承德市滦平县涝洼镇 · 阿那亚金山岭 · 阿那亚山谷音乐厅'。
    顺序:行政区划 · AOI(大区域) · POI(具体地点)。AOI==POI 时去重只显示一次。
    直辖市(province==city)行政区划部分也只显示一次。
    """
    if not rec:
        return None
    province = rec.get("province")
    city     = rec.get("city")
    district = rec.get("district")
    township = rec.get("township")
    aoi      = rec.get("aoi_name")
    poi      = rec.get("poi_name")

    # 行政区划
    admin_parts = [province] if province else []
    for p in (city, district, township):
        if p and p != province:
            admin_parts.append(p)
    admin = "".join(admin_parts)

    # 地点段:AOI · POI(同名去重)
    place_parts: list[str] = []
    if aoi: place_parts.append(aoi)
    if poi and poi != aoi: place_parts.append(poi)

    out_parts = []
    if admin: out_parts.append(admin)
    out_parts.extend(place_parts)
    return " · ".join(out_parts) if out_parts else None


def reverse_geocode(lat: float, lng: float, conn: Optional[sqlite3.Connection] = None,
                    timeout: float = 8.0) -> Optional[dict]:
    """GPS → 行政区划 + AOI/POI 名。

    Args:
        lat, lng: WGS-84(EXIF 出来的);不做坐标系转换
        conn:     SQLite 连接,用于网格缓存。None 则不缓存
        timeout:  HTTP 超时秒

    Returns:
        dict(见 _extract)或 None(无 key / 调用失败 / 超时)
    """
    # 缓存 key 仍用 WGS-84(用户输入侧),命中后实时派生 place_name / formatted_address
    cached = _cache_get(conn, lat, lng)
    if cached is not None:
        return _derive_place_and_formatted(cached)

    key = get_amap_key()
    if not key:
        return None

    # 配额检查 — 已耗尽则直接返回 None,不发 HTTP(避免触发 USER_DAILY_QUERY_OVER_LIMIT)
    if is_quota_exhausted(conn):
        return None

    # WGS-84 → GCJ-02 后传给高德。否则在中国大陆有 300-700m 偏移,
    # 偏远小景点(几十米直径)会被定位到错误位置。
    gcj_lat, gcj_lng = wgs84_to_gcj02(lat, lng)

    try:
        r = requests.get(AMAP_REGEO_URL, params={
            "key":        key,
            "location":   f"{gcj_lng},{gcj_lat}",   # 高德是 lng,lat;且要 GCJ-02
            "poi":        1,
            "extensions": "all",
            "radius":     DEFAULT_RADIUS,
        }, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("amap regeo failed for (%s,%s): %s", lat, lng, e)
        return None

    if data.get("status") != "1":
        # 高德返回 status != "1" 但 HTTP 200。常见 infocode:10003 (并发) / 10004 (单 IP 限速) /
        # 10044 (每日限额) — 不计入配额(没拿到结果);记日志方便排查
        log.warning("amap regeo error: info=%s infocode=%s", data.get("info"), data.get("infocode"))
        return None
    # 成功 → 配额 +1
    _quota_inc(conn)
    regeo = data.get("regeocode") or {}
    parsed = _extract(regeo)
    _cache_set(conn, lat, lng, parsed)        # 缓存里只存 components,出口实时派生
    return _derive_place_and_formatted(parsed)
