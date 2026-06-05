"""共享查询层 — search_photos。MCP / REST / CLI 都调它,确保行为一致。

Hybrid 检索(2026-05 起):FTS5/LIKE + 语义向量两路并跑,Reciprocal Rank Fusion 合并。
LLM 不感知,query 字段填啥 hybrid 自己处理。语义路径失败 / embedding 表空 → 静默降级 FTS-only。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from ..store import repo

log = logging.getLogger(__name__)


def _split_location_keywords(q: str) -> list[str]:
    """把 location 查询拆成多个候选关键词,任一命中即匹配。

    策略:
      1. 整串本身(若 ≥2 字)
      2. 按空格/标点切的 token(≥2 字)
      3. 若整串是连续 ≥4 字中文(如 "金山岭阿那亚"),按地名常用切割点滑窗
         (如"金山岭" / "阿那亚")— 用 3-4 字滑窗
    去重保序。
    """
    if not q:
        return []
    q = q.strip()
    out: list[str] = []
    seen: set[str] = set()
    def _add(s: str):
        s = s.strip()
        if len(s) >= 2 and s not in seen:
            out.append(s); seen.add(s)
    _add(q)
    # 按常见分隔切
    import re as _re
    for tok in _re.split(r'[\s,、|,;;/]+', q):
        _add(tok)
    # 中文连续 ≥4 字 → 3-4 字滑窗(常见地名长度)
    cjk = "".join(c for c in q if "一" <= c <= "鿿")
    if len(cjk) >= 4:
        for w in (3, 4):
            for i in range(len(cjk) - w + 1):
                _add(cjk[i:i + w])
    return out


def _quote_fts(q: str) -> str:
    """FTS5 MATCH 查询字符串转义 — 把 ASCII 标点替换成空格,避免 FTS 语法解析失败。

    用户原话 "猫 dog" 会被拆成 token 各自匹配(OR 语义),想要短语匹配请加引号。
    """
    safe = []
    for ch in q:
        if ch.isalnum() or ch in (' ', '"') or ord(ch) > 127:
            safe.append(ch)
        else:
            safe.append(' ')
    return ' '.join(''.join(safe).split())


def _resolve_person_filter(conn: sqlite3.Connection, persons: list[str]) -> list[str]:
    """把用户传的 persons(可能是人名 or cluster_id)解析成 cluster_id 列表。

    **重要**:同一个 name 可能对应多个 cluster_id(InsightFace `c_xxx` + 种子 `seed_xxx` +
    Apple `apple:<name>` 同名多条),全部要返回。OR 语义查询里 SQL 用 IN(...) 任一命中。
    """
    if not persons:
        return []
    name_to_cids: dict[str, list[str]] = {}
    rows = conn.execute("SELECT cluster_id, name FROM persons WHERE name IS NOT NULL").fetchall()
    for r in rows:
        name_to_cids.setdefault(r["name"], []).append(r["cluster_id"])
    out: list[str] = []
    for p in persons:
        if p in name_to_cids:
            out.extend(name_to_cids[p])
        elif p.startswith("seed_") or p.startswith("c_") or p.startswith("apple"):
            out.append(p)
        # 既不是已知 name 也不是 cluster_id 格式 — 跳过(返回空匹配)
    return out


def _row_to_item(row: sqlite3.Row, name_map: dict) -> dict:
    """photos 表 row → 给 LLM 看的 item dict。两路路径(FTS / 语义)共用。

    captured_at 选 utc 优先,空(iPhone 没 OffsetTimeOriginal 时)fallback 到 local。
    要求 SELECT 字段包含 captured_at_utc 和 exif(后者用来取 captured_at_local)。
    """
    vision = json.loads(row["vision"]) if row["vision"] else {}
    people = json.loads(row["people"]) if row["people"] else {}
    derived = json.loads(row["derived"]) if row["derived"] else {}
    exif_raw = row["exif"] if "exif" in row.keys() else None
    exif = json.loads(exif_raw) if exif_raw else {}
    resolved_persons = []
    for per in (people.get("persons") or []):
        cid = per.get("cluster_id")
        nm = name_map.get(cid) if cid else None
        if nm:
            resolved_persons.append(nm)
    loc = derived.get("location_bucket") or {}
    captured_at = row["captured_at_utc"] or exif.get("captured_at_local") or ""
    return {
        "photo_id":    row["photo_id"],
        "captured_at": captured_at,
        "description": vision.get("description", ""),
        "scene":       vision.get("scene"),
        "tags":        vision.get("tags") or [],
        "objects":     vision.get("objects") or [],   # 物品清单,Round 2 LLM 判定时跟 description/tags 互补
        "persons":     resolved_persons,
        "formatted_address": loc.get("formatted_address") or loc.get("place_name") or loc.get("city"),
        "photo_type":  derived.get("photo_type"),
        "favorite":    bool(derived.get("favorite")),
    }


def _resolve_persons_grouped(
    conn: sqlite3.Connection, persons: list[str]
) -> tuple[list[list[str]], list[str]]:
    """persons=['李丽','张三'] → ([[wq_cids], [xk_cids]], unrecognized=[])。

    一个人名可能对应多个 cluster_id(InsightFace c_xxx + Apple apple:李丽 + 种子 seed_xxx 各一条)。
    AND 语义下,同名 cluster 折叠成 OR 一组,组之间 AND。

    返回 (groups, unrecognized):
      - groups:           已识别 person 的 cluster_id 组(组内 OR,组间 AND)
      - unrecognized:     persons 里无法在 db 解析到的人名(用于上层决定"严格失败"还是"忽略")
    """
    name_to_cids: dict[str, list[str]] = {}
    for r in conn.execute("SELECT cluster_id, name FROM persons WHERE name IS NOT NULL").fetchall():
        name_to_cids.setdefault(r["name"], []).append(r["cluster_id"])
    groups: list[list[str]] = []
    unrecognized: list[str] = []
    for p in persons:
        if p in name_to_cids:
            groups.append(name_to_cids[p])
        elif p.startswith("seed_") or p.startswith("c_") or p.startswith("apple"):
            groups.append([p])
        else:
            unrecognized.append(p)
    return groups, unrecognized


def _build_extra_filters(
    conn: sqlite3.Connection,
    time_from: Optional[str],
    time_to: Optional[str],
    persons: Optional[list[str]],
    location: Optional[str],
    persons_mode: str = "OR",
    favorite_only: bool = False,
) -> tuple[list[str], list]:
    """time/persons/location filter 拼 SQL 片段(两路共用)。

    persons_mode:
      - 'OR'(默认):任一人物命中(查"X 出现过的照片")
      - 'AND':所有人物都在同一张(查"X 和 Y 的合影")
    favorite_only: True → 只返 Apple 收藏(derived.favorite=1 的生成列)
    """
    where: list[str] = []
    params: list = []
    if favorite_only:
        where.append("p.favorite = 1")
    if persons:
        mode = (persons_mode or "OR").upper()
        if mode == "AND":
            groups, unrecognized = _resolve_persons_grouped(conn, persons)
            # AND 严格:任一 person 未识别 → 0 结果(不能假装那人不存在让 AND 退化成 OR)
            if unrecognized or not groups:
                return ["1=0"], []
            for cid_group in groups:
                ph = ",".join("?" * len(cid_group))
                where.append(
                    f"EXISTS(SELECT 1 FROM faces WHERE photo_id=p.photo_id AND cluster_id IN ({ph}))"
                )
                params.extend(cid_group)
        else:
            cids = _resolve_person_filter(conn, persons)
            if not cids:
                return ["1=0"], []
            placeholders = ",".join("?" * len(cids))
            where.append(f"p.photo_id IN (SELECT photo_id FROM faces WHERE cluster_id IN ({placeholders}))")
            params.extend(cids)
    _ts = "COALESCE(NULLIF(p.captured_at_utc, ''), json_extract(p.exif, '$.captured_at_local'))"
    if time_from:
        where.append(f"{_ts} >= ?")
        params.append(time_from)
    if time_to:
        # day-only(YYYY-MM-DD)的 inclusive end 自动补到当天 T23:59:59,
        # 否则字符串字典序 '2026-05-04T12:42:24' <= '2026-05-04' = False,会漏掉当天的照片
        tt = time_to
        if len(tt) == 10 and tt.count("-") == 2:
            tt = tt + "T23:59:59"
        where.append(f"{_ts} <= ?")
        params.append(tt)
    if location:
        candidates = _split_location_keywords(location)
        or_clauses = []
        for kw in candidates:
            or_clauses.append("json_extract(p.derived, '$.location_bucket.formatted_address') LIKE ?")
            params.append(f"%{kw}%")
        where.append("(" + " OR ".join(or_clauses) + ")")
    return where, params


def _search_fts(
    conn: sqlite3.Connection,
    query: Optional[str],
    time_from: Optional[str],
    time_to: Optional[str],
    persons: Optional[list[str]],
    location: Optional[str],
    limit: int,
    offset: int,
    persons_mode: str = "OR",
    favorite_only: bool = False,
) -> dict:
    """原 FTS5/LIKE + 过滤路径。trigram 要求 token ≥ 3 字,短词 fallback LIKE。"""
    sql_parts = ["SELECT DISTINCT p.photo_id, p.captured_at_utc, p.vision, p.people, p.derived, p.exif"]
    where: list[str] = ["p.source != 'seed'", "p.vision IS NOT NULL"]
    params: list = []

    if query:
        q = _quote_fts(query)
        tokens = q.split() if q else []
        has_short = any(len(t) < 3 for t in tokens) if tokens else False
        if not tokens:
            from_clause = "FROM photos p"
            order = "ORDER BY p.captured_at_utc DESC"
        elif has_short:
            from_clause = "FROM photos p"
            for t in tokens:
                where.append(
                    "(json_extract(p.vision,'$.description') LIKE ? "
                    "OR json_extract(p.vision,'$.scene') LIKE ? "
                    "OR json_extract(p.vision,'$.ocr_text') LIKE ? "
                    "OR json_extract(p.vision,'$.tags') LIKE ? "
                    "OR json_extract(p.vision,'$.objects') LIKE ? "
                    "OR json_extract(p.people,'$.persons') LIKE ?)"
                )
                pattern = f"%{t}%"
                params.extend([pattern] * 6)
            order = "ORDER BY p.captured_at_utc DESC"
        else:
            sql_parts.append(", bm25(photos_fts) AS score")
            from_clause = "FROM photos_fts JOIN photos p ON p.photo_id = photos_fts.photo_id"
            where.append("photos_fts MATCH ?")
            params.append(q)
            order = "ORDER BY score ASC, p.captured_at_utc DESC"
    else:
        from_clause = "FROM photos p"
        order = "ORDER BY p.captured_at_utc DESC"

    extra_where, extra_params = _build_extra_filters(
        conn, time_from, time_to, persons, location, persons_mode, favorite_only
    )
    where.extend(extra_where)
    params.extend(extra_params)

    sql = (
        " ".join(sql_parts) + " " + from_clause + " WHERE " + " AND ".join(where)
        + " " + order + " LIMIT ? OFFSET ?"
    )
    rows = conn.execute(sql, params + [limit, offset]).fetchall()

    count_sql = "SELECT COUNT(DISTINCT p.photo_id) " + from_clause + " WHERE " + " AND ".join(where)
    total = conn.execute(count_sql, params).fetchone()[0]

    name_map = repo.cluster_name_map(conn)
    items = [_row_to_item(r, name_map) for r in rows]
    return {"total": total, "items": items}


def _search_semantic(
    conn: sqlite3.Connection,
    query: str,
    time_from: Optional[str],
    time_to: Optional[str],
    persons: Optional[list[str]],
    location: Optional[str],
    limit: int,
    persons_mode: str = "OR",
    favorite_only: bool = False,
) -> list[dict]:
    """语义召回 + 应用其他 filter。失败时返 [],调用方 fallback 到 FTS-only。"""
    from .semantic import get_semantic_index, has_embeddings
    if not has_embeddings(conn):
        return []
    try:
        from ..embed import get_embedder
        embedder = get_embedder()
        index = get_semantic_index(conn)
        # 召回多一点,过 filter 后还有余量给 RRF
        pairs = index.search(embedder.embed_one(query), k=limit * 3)
    except Exception as e:
        log.warning("semantic search failed: %s (will use FTS only)", e)
        return []
    if not pairs:
        return []

    ids = [pid for pid, _ in pairs]
    placeholders = ",".join("?" * len(ids))
    where: list[str] = [
        f"p.photo_id IN ({placeholders})",
        "p.source != 'seed'",
        "p.vision IS NOT NULL",
    ]
    params: list = list(ids)
    extra_where, extra_params = _build_extra_filters(
        conn, time_from, time_to, persons, location, persons_mode, favorite_only
    )
    where.extend(extra_where)
    params.extend(extra_params)

    sql = (
        "SELECT p.photo_id, p.captured_at_utc, p.vision, p.people, p.derived, p.exif "
        "FROM photos p WHERE " + " AND ".join(where)
    )
    rows = conn.execute(sql, params).fetchall()
    rows_by_id = {r["photo_id"]: r for r in rows}

    name_map = repo.cluster_name_map(conn)
    items = []
    for pid in ids:   # 保持相似度顺序
        if pid not in rows_by_id:
            continue   # 被 filter 过滤掉了
        items.append(_row_to_item(rows_by_id[pid], name_map))
        if len(items) >= limit:
            break
    return items


_FAVORITE_RANK_BONUS = 3   # 收藏在最终结果里上浮约 3 位(轻加权,相关性仍为主,不置顶)


def _apply_favorite_boost(items: list[dict], bonus: int = _FAVORITE_RANK_BONUS) -> list[dict]:
    """对已按相关性/时间排好的结果做"收藏轻加权":收藏照片在原排名上上浮 bonus 位。

    用 (原下标 - bonus*favorite) 作排序键、原下标兜底 → 收藏小幅靠前,
    但不会把明显更相关的非收藏照片挤到后面(差距 ≤ bonus)。
    """
    if not items:
        return items
    keyed = [
        ((i - bonus) if it.get("favorite") else i, i, it)
        for i, it in enumerate(items)
    ]
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [it for _, _, it in keyed]


def search_photos(
    conn: sqlite3.Connection,
    query: Optional[str] = None,
    query_expansions: Optional[list[str]] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    persons: Optional[list[str]] = None,
    persons_mode: str = "OR",
    location: Optional[str] = None,
    favorite_only: bool = False,
    limit: int = 80,
    offset: int = 0,
) -> dict:
    """Hybrid 检索:FTS5/LIKE + 语义两路并跑,Reciprocal Rank Fusion 合并。

    Args:
        query:            主关键词(字面 / 语义都行)
        query_expansions: 可选同义/近义/上下位扩词列表(LLM 决定),每个走完整 hybrid 后合并
                          用于:具体物品/装饰(裙子→连衣裙)、宽泛场景(美食→吃的)等 dense 短板
        time_from:        ISO8601(含,>=)
        time_to:          ISO8601(含,<=)
        persons:          人名或 cluster_id 列表
        persons_mode:     'OR'(默认,任一在)/ 'AND'(都在同一张)
        location:         formatted_address 子串(LIKE,内部自动拆滑窗)
        favorite_only:    True → 只返 Apple 收藏照片
        limit/offset:     分页(语义路径忽略 offset)

    Returns:
        { "total": int, "items": [...] }
    """
    fts_result = _search_fts(
        conn, query, time_from, time_to, persons, location, limit, offset,
        persons_mode, favorite_only,
    )

    # 没 query 或 offset>0(翻页)→ 不跑语义,行为同纯 FTS 不变
    if not query or not query.strip() or offset > 0:
        fts_result["items"] = _apply_favorite_boost(fts_result["items"])
        return fts_result

    # 收集所有要跑的 query(主 + 扩词)
    queries = [query] + [q for q in (query_expansions or []) if q and q.strip() and q != query]

    from .semantic import rrf_merge_multi
    all_items: list[dict] = []
    seen_ids: set[str] = set()

    # 主 query 的 fts 结果先入
    for it in fts_result["items"]:
        if it["photo_id"] not in seen_ids:
            all_items.append(it)
            seen_ids.add(it["photo_id"])

    # 多 query 跑 sem(可 batch 化),每个 query 单独 RRF
    sem_results_per_query: list[list[dict]] = []
    for q in queries:
        sem_items = _search_semantic(
            conn, q, time_from, time_to, persons, location, limit * 3,
            persons_mode, favorite_only,
        )
        sem_results_per_query.append(sem_items)
        for it in sem_items:
            if it["photo_id"] not in seen_ids:
                all_items.append(it)
                seen_ids.add(it["photo_id"])

    # 扩词 fts 也跑(短词 LIKE 在扩词里通常命中具体名 — "连衣裙" 字面命中)
    fts_results_per_exp: list[list[dict]] = []
    for q in queries[1:]:   # 跳过主 query(上面已跑)
        r = _search_fts(conn, q, time_from, time_to, persons, location, limit, 0,
                        persons_mode, favorite_only)
        fts_results_per_exp.append(r["items"])
        for it in r["items"]:
            if it["photo_id"] not in seen_ids:
                all_items.append(it)
                seen_ids.add(it["photo_id"])

    # RRF:每个路径(主 fts / 主 sem / 扩词 sem N / 扩词 fts N)的 rank 都贡献分数
    paths: list[list[dict]] = [fts_result["items"]] + sem_results_per_query + fts_results_per_exp
    merged = rrf_merge_multi(paths, k=60, limit=limit * 3 if (time_from or time_to) else limit)

    # 用户明示了时间窗 → 时间信号是强意图(比如"最近 X"),合并后按 captured_at 倒序重排
    # 没时间窗 → 保留 RRF 相关性排序(用户问"海边"时不应该把最新一张排最前)
    if time_from or time_to:
        merged.sort(key=lambda it: it.get("captured_at") or "", reverse=True)
        merged = merged[:limit]
    merged = _apply_favorite_boost(merged)
    return {"total": max(fts_result["total"], len(seen_ids)), "items": merged}
