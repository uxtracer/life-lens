"""从图片 EXIF 抽 4 个字段:captured_at_local / captured_at_utc / tz_offset_minutes / gps

只读关键字段,忽略 camera / settings / dimensions。
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from PIL import Image, ExifTags

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass


# 反查 EXIF tag 名 → id
_TAG_BY_NAME = {v: k for k, v in ExifTags.TAGS.items()}
_GPS_TAG_BY_NAME = {v: k for k, v in ExifTags.GPSTAGS.items()}


def peek_captured_at(path: Path) -> Optional[str]:
    """轻量版:只读 DateTime tag,返回 ISO 本地时间字符串(无时区),失败返回 None。

    用于扫描入队阶段(几十万张),不解析 GPS、不算时区,毫秒级。
    """
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif:
                return None
            for name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                tag = _TAG_BY_NAME.get(name)
                v = exif.get(tag) if tag else None
                if v:
                    try:
                        local = datetime.strptime(str(v).strip(), "%Y:%m:%d %H:%M:%S")
                        return local.replace(tzinfo=None).isoformat(timespec="seconds")
                    except Exception:
                        continue
    except Exception:
        return None
    return None


def extract(path: Path) -> dict:
    """返回 exif group。失败时返回带 null 的占位结构,而不是抛异常。"""
    result = {
        "captured_at_local": None,
        "captured_at_utc":   None,
        "tz_offset_minutes": None,
        "gps":               None,
    }
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif:
                return result
            # 时间
            local, tz_min = _read_datetime(exif)
            if local:
                result["captured_at_local"] = local.replace(tzinfo=None).isoformat(timespec="seconds")
                if tz_min is not None:
                    utc = (local - timedelta(minutes=tz_min)).replace(tzinfo=timezone.utc)
                    result["captured_at_utc"]   = utc.isoformat(timespec="seconds").replace("+00:00", "Z")
                    result["tz_offset_minutes"] = tz_min
                else:
                    # 没时区信息,直接当本地时间存,utc 留空
                    pass
            # GPS
            gps_ifd = _get_gps_ifd(exif)
            if gps_ifd:
                gps = _parse_gps(gps_ifd)
                if gps:
                    result["gps"] = gps
    except Exception:
        # 安静失败,返回占位结构;errors 由 pipeline 层记录
        pass
    return result


def _read_datetime(exif) -> tuple[Optional[datetime], Optional[int]]:
    """从 EXIF 读拍摄时间。优先 DateTimeOriginal,缺则 DateTimeDigitized / DateTime。"""
    dt_str = None
    for name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        tag = _TAG_BY_NAME.get(name)
        if tag and exif.get(tag):
            dt_str = exif.get(tag)
            break
    if not dt_str:
        return None, None
    try:
        local = datetime.strptime(str(dt_str).strip(), "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None, None

    # 时区:OffsetTimeOriginal / OffsetTime,如 "+08:00"
    tz_str = None
    for name in ("OffsetTimeOriginal", "OffsetTimeDigitized", "OffsetTime"):
        tag = _TAG_BY_NAME.get(name)
        if tag and exif.get(tag):
            tz_str = str(exif.get(tag)).strip()
            break
    tz_min = _parse_offset(tz_str) if tz_str else None
    return local, tz_min


def _parse_offset(s: str) -> Optional[int]:
    """'+08:00' → 480"""
    try:
        sign = 1 if s[0] == "+" else -1
        hh, mm = s[1:].split(":")
        return sign * (int(hh) * 60 + int(mm))
    except Exception:
        return None


def _get_gps_ifd(exif) -> Optional[dict]:
    try:
        ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
        if not ifd:
            return None
        return {ExifTags.GPSTAGS.get(k, k): v for k, v in ifd.items()}
    except Exception:
        return None


def _parse_gps(g: dict) -> Optional[dict]:
    try:
        lat = _dms_to_decimal(g.get("GPSLatitude"))
        lng = _dms_to_decimal(g.get("GPSLongitude"))
        if lat is None or lng is None:
            return None
        if str(g.get("GPSLatitudeRef", "N")).upper() == "S":
            lat = -lat
        if str(g.get("GPSLongitudeRef", "E")).upper() == "W":
            lng = -lng
        return {"lat": round(lat, 6), "lng": round(lng, 6)}
    except Exception:
        return None


def _dms_to_decimal(dms) -> Optional[float]:
    if not dms:
        return None
    try:
        d, m, s = [float(x) for x in dms]
        return d + m / 60 + s / 3600
    except Exception:
        return None
