"""相册名 → 城市 + 事件关键词(本地 LLM 解析 + 去重缓存)。

`meta.source_signals.albums`(用户当年的相册/文件夹名)往往带强信号:
- 地点:"2015.2.14-三亚" / "2015.4.19-颐和园"(景点→北京)→ 城市
- 事件:"2015.11.22-北京大雪" / "十岁生日" → 关键词

无 GPS 的老照片靠它补 `derived.location_bucket` 城市(只到城市颗粒度),
事件关键词合并进 `vision.tags` 让全文/语义搜索能命中。

铁律:相册名可能含真名/亲属称谓,**绝不外发** —— 只走本地 Ollama 文本解析(无 images),
不碰 DeepSeek/高德。相册名是高度重复的小集合,按 album_name 缓存到 album_parse_cache 表,
unique 名字只解析一次。LLM 不可用/解析失败 → graceful 返回空,不做规则兜底。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

log = logging.getLogger(__name__)

# 通用/自动相册名(不含用户信息):解析时直接判空,省一次 LLM 调用
_GENERIC_ALBUMS = {
    "favorites", "recents", "recently added", "最近添加", "最近项目", "个人收藏",
    "收藏", "截屏", "screenshots", "hidden", "已隐藏", "videos", "视频",
    "all photos", "所有照片", "library", "图库", "live photos", "selfies", "自拍",
    "panoramas", "全景", "bursts", "连拍", "slo-mo", "慢动作", "imports", "导入",
}

# 剥日期前缀:"2015.2.14-" / "2015-" / "2015年3月-" / "2015年3月14日" / "2015.11.22 " 等
# 月/日 可带中文后缀(年X月、X日)也可只用分隔符;尾部连带剥掉悬空的分隔符与 年月日
_DATE_PREFIX = re.compile(
    r"^\s*\d{4}"                       # 年份
    r"(?:\s*[.\-/年]\s*\d{1,2})?"      # 月(前导分隔符或「年」)
    r"\s*月?"                          # 可选「月」字
    r"(?:\s*[.\-/]?\s*\d{1,2})?"       # 日(分隔符可选,可能紧跟在「月」后)
    r"\s*日?"                          # 可选「日」字
    r"\s*[\-_．.、:：~年月日]*\s*"       # 尾部分隔符 + 悬空 年月日
)

ALBUM_PROMPT = """你在解析一个照片相册/文件夹的名字,从里面提取地点和事件关键词。

相册名:「{name}」

输出严格 JSON:{{"country": 国家, "city": 城市, "province": 省, "place": 具体地点, "event_tags": [关键词]}}

规则:
- country:仅当相册名指向**中国以外的国家**时给国家名(如"日本""美国""法国");国内或无法判断 → null。
- city:如果相册名明确指向某个城市(直接写了城市名,或写了该城市的**著名且唯一**景点/地标),给出规范中文城市名(如"北京""上海""三亚");著名景点映射到所在城市(如"颐和园"→"北京","外滩"→"上海","西湖"→"杭州")。**地名模糊、可能对应多个城市无法确定的(如只写"奥体""体育馆""公园"),宁可 null 也不要猜。只写到国家(如"日本""美国")无法确定具体城市的,city → null(国家写进 country)。** 无法判断就 null。
- province:city 对应的省/直辖市(如"北京""海南""浙江");不确定就 null。
- place:相册名里点明的**真实地理地标/景点/建筑物**的规范名(如"颐和园""外滩""西湖""莽山森林公园")。**以下都不是 place,一律 null:城市或省份名(三亚/北京)、植物或物品(银杏/花)、天气或自然现象(大雪/晚霞)、事件或场合(过年/生日/满月)。** 没有明确地标就 null。
- event_tags:1-4 个描述事件/场合/活动/人物关系的中文关键词(如"过年""生日""旅行""毕业""婚礼""艺术照""聚会""野餐")。不要包含日期,不要把城市名/景点名重复放进来。没有有意义的事件就空数组 []。
- 名字像通用相册(收藏/截屏/最近添加之类)→ city/province/place 为 null、event_tags 为空。

只输出 JSON,不要解释。"""


def _strip_date_prefix(name: str) -> str:
    return _DATE_PREFIX.sub("", name).strip()


def _ollama_parse(name: str, *, timeout: int = 60) -> Optional[dict]:
    """调本地 Ollama 文本补全解析单个相册名。失败返回 None(不抛)。"""
    import requests

    from ..vision.ollama import get_configured_host, get_configured_model, _extract_json

    url = get_configured_host().rstrip("/") + "/api/generate"
    payload = {
        "model": get_configured_model(),
        "prompt": ALBUM_PROMPT.format(name=name),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 256, "num_ctx": 2048},
    }
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        raw = r.json().get("response", "")
    except Exception as e:
        log.warning("album 解析 Ollama 调用失败 name=%r: %s", name, e)
        return None
    parsed, err = _extract_json(raw)
    if parsed is None:
        log.warning("album 解析 JSON 失败 name=%r: %s", name, err)
        return None
    return parsed


def _write_cache(conn, name: str, result: dict) -> None:
    from ..store.repo import now_iso

    conn.execute(
        """
        INSERT INTO album_parse_cache(album_name, country, city, province, place, tags_json, parsed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(album_name) DO UPDATE SET
            country=excluded.country, city=excluded.city, province=excluded.province,
            place=excluded.place, tags_json=excluded.tags_json, parsed_at=excluded.parsed_at
        """,
        (name, result["country"], result["city"], result["province"], result["place"],
         json.dumps(result["tags"], ensure_ascii=False), now_iso()),
    )
    conn.commit()


def parse_album(
    name: str,
    conn,
    *,
    llm: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """解析单个相册名 → {city, province, tags}。按 album_name 缓存(命中不调 LLM)。

    llm: callable(name) -> parsed dict | None;默认本地 Ollama。测试注入假的。
    LLM 调用失败(Ollama 不通)→ 返回空且**不写缓存**(下次可重试)。
    """
    empty = {"country": None, "city": None, "province": None, "place": None, "tags": []}
    name = (name or "").strip()
    if not name:
        return empty

    row = conn.execute(
        "SELECT country, city, province, place, tags_json FROM album_parse_cache WHERE album_name = ?",
        (name,),
    ).fetchone()
    if row is not None:
        return {"country": row[0], "city": row[1], "province": row[2], "place": row[3],
                "tags": json.loads(row[4]) if row[4] else []}

    stripped = _strip_date_prefix(name)
    if name.lower() in _GENERIC_ALBUMS or stripped.lower() in _GENERIC_ALBUMS:
        result = empty
    else:
        parsed = (llm or _ollama_parse)(name)
        if parsed is None:
            return empty  # 不写缓存,留待下次重试
        result = {
            "country": parsed.get("country") or None,
            "city": parsed.get("city") or None,
            "province": parsed.get("province") or None,
            "place": parsed.get("place") or None,
            "tags": [t for t in (parsed.get("event_tags") or [])
                     if t and isinstance(t, str)],
        }

    _write_cache(conn, name, result)
    return result


def signals_for_albums(
    albums,
    conn,
    *,
    llm: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """聚合一张照片的多个相册名:country/city/place 各取首个非空;tags 合并去重。

    country 独立聚合(国外相册可能只有 country 没 city);city/province/place 同源(随 city)。
    """
    out = {"country": None, "city": None, "province": None, "place": None, "tags": []}
    seen: set = set()
    for name in (albums or []):
        sig = parse_album(name, conn, llm=llm)
        if sig.get("country") and not out["country"]:
            out["country"] = sig["country"]
        if sig["city"] and not out["city"]:
            out["city"] = sig["city"]
            out["province"] = sig["province"]
            out["place"] = sig["place"]
        for t in sig["tags"]:
            if t not in seen:
                seen.add(t)
                out["tags"].append(t)
    return out


def merge_album_tags(
    vision: Optional[dict],
    albums,
    conn,
    *,
    llm: Optional[Callable[[str], Optional[dict]]] = None,
) -> Optional[dict]:
    """把 album 事件关键词合并进 vision['tags'](dedup,幂等)。原地改并返回 vision。

    ⚠️ 有意把派生数据写进 vision.tags(铁律例外,见 CLAUDE.md):vision.tags 本就喂
    FTS + embedding,写进去自动可搜;靠这个 merge 是 vision 写入路径的固定一步,
    每次 reprocess vision 后重注,所以重跑不会丢。
    """
    if not vision:
        return vision
    sig = signals_for_albums(albums, conn, llm=llm)
    if not sig["tags"]:
        return vision
    existing = list(vision.get("tags") or [])
    seen = set(existing)
    for t in sig["tags"]:
        if t not in seen:
            seen.add(t)
            existing.append(t)
    vision["tags"] = existing
    return vision
