"""共享查询层 — 聚合(places_visited / counts_by_year / 等)。"""
from __future__ import annotations

import sqlite3
from typing import Optional


def places_visited(
    conn: sqlite3.Connection,
    year: Optional[int] = None,
    persons: Optional[list[str]] = None,
    persons_mode: str = "OR",
) -> dict:
    """返回去过的地点列表(按出现频次降序),每地点附 3 张代表照片 id。

    Args:
        year:         过滤某年(可选)
        persons:      人名或 cluster_id 列表
        persons_mode: 'OR'(默认,任一在)— "张三去过哪些地方"
                      'AND'(全在同一张)— "张三和李四、赵阿姨一起去过哪些地方"

    Returns:
        { "places": [{ formatted_address, count, sample_photo_ids[:3] }] }
    """
    where = ["p.source != 'seed'", "p.vision IS NOT NULL"]
    params: list = []
    if year is not None:
        where.append("CAST(json_extract(p.derived, '$.time_bucket.year') AS INTEGER) = ?")
        params.append(year)

    join = ""
    if persons:
        mode = (persons_mode or "OR").upper()
        if mode == "AND":
            # 跟 search.py 同样做法:同名 cluster 折叠为 OR 一组,组之间 AND
            # 严格模式:任一 person 未识别 → 0 结果(避免 AND 退化成 OR 误导)
            from .search import _resolve_persons_grouped
            groups, unrecognized = _resolve_persons_grouped(conn, persons)
            if unrecognized or not groups:
                return {"places": [], "unrecognized_persons": unrecognized}
            for cid_group in groups:
                ph = ",".join(["?"] * len(cid_group))
                where.append(
                    f"EXISTS(SELECT 1 FROM faces WHERE photo_id=p.photo_id AND cluster_id IN ({ph}))"
                )
                params.extend(cid_group)
        else:
            from .search import _resolve_person_filter
            cids = _resolve_person_filter(conn, persons)
            if not cids:
                return {"places": []}
            placeholders = ",".join(["?"] * len(cids))
            join = "JOIN faces f ON f.photo_id = p.photo_id"
            where.append(f"f.cluster_id IN ({placeholders})")
            params.extend(cids)

    # 至少有一个地点字段非空(用 place_name 或 city 兜底)
    where.append(
        "(json_extract(p.derived, '$.location_bucket.place_name') IS NOT NULL "
        "OR json_extract(p.derived, '$.location_bucket.city') IS NOT NULL)"
    )

    # MIN(formatted_address) 取每组任一代表(同一 place 通常 formatted 都一样,MIN 就是那个值)。
    # 给 Round 2 LLM 看完整地址(行政区划+AOI+POI),能正确把"阿那亚金山岭上院/景观泳池/音乐厅"
    # 等同景区不同 POI 聚合到"阿那亚金山岭"目的地维度。
    sql = f"""
    SELECT
      COALESCE(
        json_extract(p.derived, '$.location_bucket.place_name'),
        json_extract(p.derived, '$.location_bucket.city')
      ) AS place,
      json_extract(p.derived, '$.location_bucket.country') AS country,
      MIN(json_extract(p.derived, '$.location_bucket.formatted_address')) AS formatted_address,
      MIN(json_extract(p.derived, '$.location_bucket.aoi_name'))          AS aoi_name,
      MIN(json_extract(p.derived, '$.location_bucket.city'))              AS city,
      COUNT(DISTINCT p.photo_id) AS cnt,
      GROUP_CONCAT(DISTINCT p.photo_id) AS photo_ids
    FROM photos p {join}
    WHERE {' AND '.join(where)}
    GROUP BY place, country
    ORDER BY cnt DESC
    """
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        if not r["place"]:
            continue
        all_ids = (r["photo_ids"] or "").split(",") if r["photo_ids"] else []
        # 只给 formatted_address(完整地址,包含国家/省/市/区/镇/AOI/POI 全粒度,
        # 让 Round 2 LLM 看着完整上下文做地理聚合)
        out.append({
            "formatted_address": r["formatted_address"] or r["place"],
            "count": int(r["cnt"]),
            "sample_photo_ids": all_ids[:3],
        })
    return {"places": out}


def counts_by_year(conn: sqlite3.Connection) -> dict:
    """按年统计照片数量。"""
    sql = """
    SELECT
      json_extract(derived, '$.time_bucket.year') AS year,
      COUNT(*) AS cnt
    FROM photos
    WHERE source != 'seed' AND vision IS NOT NULL
    GROUP BY year
    ORDER BY year DESC
    """
    rows = conn.execute(sql).fetchall()
    return {"by_year": [{"year": r["year"], "count": r["cnt"]} for r in rows if r["year"]]}
