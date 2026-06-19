"""仅重跑某个 group 的逻辑(不重跑昂贵的 vision)。

Phase 3 提供 faces 两种模式:
- quick rematch:不跑 InsightFace,只用 faces 表已有 embedding 重新跑 cluster.assign。
                上传新种子后 / 阈值微调时用。秒级(matrix 乘法)。
- full reprocess:重跑 InsightFace detect + assign。换 face 模型或修预处理时用。慢。
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from ..faces import detector as face_det, cluster as face_cluster
from ..preprocess.cache import cache_path
from ..store import db, repo

log = logging.getLogger(__name__)


def reprocess_faces(root: Path, only_failed: bool = False) -> dict:
    """对所有非种子 photos 重新跑人脸检测 + cluster assign。

    用预处理缓存,不重跑 vision/exif。
    """
    if not face_det.available():
        return {"ok": False, "error": "InsightFace 不可用,先装 onnxruntime + insightface"}

    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    try:
        rows = conn.execute(
            "SELECT photo_id FROM photos WHERE source != 'seed'"
        ).fetchall()
        photo_ids = [r[0] for r in rows]

        done = 0
        new_clusters = 0
        matched = 0
        for pid in photo_ids:
            cp = cache_path(root, pid)
            if not cp.exists():
                continue
            try:
                jpeg_bytes = cp.read_bytes()
                detections = face_det.detect(jpeg_bytes)
                # 清旧
                repo.delete_faces_of_photo(conn, pid)
                # 写新
                for i, d in enumerate(detections):
                    cid, is_new = face_cluster.assign(conn, d.embedding)
                    repo.insert_face(conn, f"{pid}-{i}", pid, cid,
                                     d.embedding.tobytes(), d.bbox)
                    if is_new:
                        new_clusters += 1
                    else:
                        matched += 1
                done += 1
            except Exception as e:
                log.exception("reprocess faces failed for %s", pid)

        # 把 vision.people_in_photo 的 name 字段重新对齐(用最新 cluster_id)
        _refill_vision_names(conn)
    finally:
        conn.close()

    return {
        "ok": True,
        "photos_processed": done,
        "faces_matched_existing": matched,
        "faces_new_cluster": new_clusters,
    }


def rematch_faces(root: Path, threshold: float = None) -> dict:
    """Quick rematch:不跑 InsightFace detect,用 faces 表已有 embedding 把主库脸"吸"到已命名人物。

    设计:
    - 锚点(anchors) = 已命名 person 名下的所有 face embedding(种子 + 标注过的)
    - 候选 = 主库脸里,当前 cluster 还没命名的(已被命名的认为是 ground truth,不动)
    - 对每张候选,和锚点矩阵算余弦,按 anchor cluster_id 取 max。
      max >= 阈值就把候选归到该 anchor cluster_id。否则保持原 cluster_id 不变。

    这个设计**不破坏未命名 cluster 之间的现有归属**,只解决"新种子能否吸走主库脸"。
    秒级即可处理几万张照片(纯矩阵乘法,无 InsightFace 推理)。
    """
    if threshold is None:
        threshold = face_cluster.DEFAULT_THRESHOLD

    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    try:
        # 1. 加载所有 anchor(已命名 person 名下的 face)
        anchor_rows = conn.execute(
            """
            SELECT f.cluster_id, f.embedding
            FROM faces f
            WHERE f.cluster_id IN (SELECT cluster_id FROM persons WHERE name IS NOT NULL)
            """
        ).fetchall()

        if not anchor_rows:
            return {
                "ok": True, "mode": "quick",
                "anchors": 0, "candidates": 0, "moved": 0,
                "note": "没有已命名人物作 anchor,什么也没改",
            }

        anchor_cluster_ids: list[str] = []
        anchor_embs: list[np.ndarray] = []
        for r in anchor_rows:
            anchor_cluster_ids.append(r["cluster_id"])
            anchor_embs.append(np.frombuffer(r["embedding"], dtype=np.float32))
        anchor_mat = np.stack(anchor_embs)  # (A, 512)

        # 2. 候选 = 主库脸里,当前 cluster_id 还没被命名的
        candidate_rows = conn.execute(
            """
            SELECT f.face_id, f.cluster_id, f.embedding
            FROM faces f
            JOIN photos p ON p.photo_id = f.photo_id
            WHERE p.source != 'seed'
              AND (f.cluster_id IS NULL OR f.cluster_id NOT IN (SELECT cluster_id FROM persons WHERE name IS NOT NULL))
            """
        ).fetchall()

        moved = 0
        affected_photo_ids: set[str] = set()
        for r in candidate_rows:
            face_id = r["face_id"]
            emb     = np.frombuffer(r["embedding"], dtype=np.float32)
            sims    = anchor_mat @ emb   # (A,)
            # 按 anchor cluster_id 取 max
            best_per: dict[str, float] = {}
            for cid, s in zip(anchor_cluster_ids, sims):
                cur = best_per.get(cid)
                if cur is None or s > cur:
                    best_per[cid] = float(s)
            best_cid, best_sim = max(best_per.items(), key=lambda kv: kv[1])
            if best_sim >= threshold:
                conn.execute(
                    "UPDATE faces SET cluster_id = ? WHERE face_id = ?",
                    (best_cid, face_id),
                )
                # 拿这个 face 所在 photo_id(用于后续 caption 刷新)
                pid_row = conn.execute(
                    "SELECT photo_id FROM faces WHERE face_id = ?", (face_id,)
                ).fetchone()
                if pid_row:
                    affected_photo_ids.add(pid_row[0])
                moved += 1

        # 3. 重新填 vision.people_in_photo 的 cluster_id
        _refill_vision_names(conn)
    finally:
        conn.close()

    return {
        "ok": True,
        "mode": "quick",
        "anchors": len(anchor_rows),
        "candidates": len(candidate_rows),
        "moved": moved,
        "affected_photo_ids": sorted(affected_photo_ids),
    }


def reprocess_vision_for(root: Path, photo_ids: list[str], model: str = None) -> dict:
    """对指定 photo_ids 重跑 vision 阶段(两次 LLM 调用:description + struct)。

    使用预处理 JPEG 缓存 + InsightFace 已有的 face/cluster 数据;不重跑 face detection。
    用当前最新的 cluster→name 映射注入 prompt。
    """
    import json
    from ..vision.ollama import OllamaVision, DEFAULT_MODEL
    from ..vision.annotate import annotate_faces
    from ..preprocess.cache import cache_path

    vision = OllamaVision(model=model or DEFAULT_MODEL)

    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    try:
        name_map = repo.cluster_name_map(conn)

        done = 0
        failed = 0
        for pid in photo_ids:
            cp = cache_path(root, pid)
            if not cp.exists():
                failed += 1
                continue
            try:
                # 1. 拿这张照片所有 face(按 face_id 排序,稳定顺序)
                face_rows = conn.execute(
                    "SELECT face_id, cluster_id, bbox FROM faces WHERE photo_id = ? ORDER BY face_id",
                    (pid,),
                ).fetchall()

                # 2. 构建 face_items + bboxes + cluster_ids
                face_items = []
                bboxes = []
                cluster_ids = []
                for idx, fr in enumerate(face_rows, start=1):
                    cid = fr["cluster_id"]
                    cluster_ids.append(cid)
                    face_items.append((idx, name_map.get(cid)))
                    if fr["bbox"]:
                        bboxes.append(tuple(json.loads(fr["bbox"])))

                # 3. 读图 + annotate
                jpeg_bytes = cp.read_bytes()
                if bboxes:
                    jpeg_bytes = annotate_faces(jpeg_bytes, bboxes)

                # 升级 face_items → 含 bbox + image_size + persons.age/gender(给 v9.2 prompt)
                face_items_for_prompt = face_items
                if face_items and bboxes:
                    try:
                        from PIL import Image as _Image
                        import io as _io
                        with _Image.open(_io.BytesIO(jpeg_bytes)) as _im:
                            image_size = _im.size
                        demo_map = repo.cluster_demographics_map(conn)
                        face_items_for_prompt = []
                        for (idx2, name), bbox, cid in zip(face_items, bboxes, cluster_ids):
                            demo = demo_map.get(cid, {})
                            face_items_for_prompt.append({
                                "idx": idx2,
                                "name": name,
                                "bbox": bbox,
                                "image_size": image_size,
                                "age": demo.get("age"),
                                "gender": demo.get("gender"),
                            })
                    except Exception:
                        face_items_for_prompt = face_items

                # 4. 两次 LLM 调用
                desc_res = vision.describe_description(jpeg_bytes, face_items=face_items_for_prompt or None)
                struct_res = vision.describe_struct(jpeg_bytes, face_items=face_items_for_prompt or None)

                # 5. 合并 vision group(不含 actions)
                new_vision: dict = {}
                actions: list[str] = []
                warns: list[str] = []
                if struct_res.parsed:
                    for k in ("media_type", "subject", "scene", "objects", "tags", "ocr_text", "mood"):
                        new_vision[k] = struct_res.parsed.get(k)
                    actions = list(struct_res.parsed.get("actions") or [])
                    warns.extend(struct_res.warnings or [])
                if desc_res.parsed:
                    new_vision["description"] = desc_res.parsed.get("description", "")
                    warns.extend(desc_res.warnings or [])

                if not new_vision:
                    failed += 1
                    continue

                # 6. 重组 people.persons[](cluster_id + name + 新 action)
                photo_row = conn.execute(
                    "SELECT vision, people, meta FROM photos WHERE photo_id = ?", (pid,)
                ).fetchone()
                old_people = json.loads(photo_row["people"]) if photo_row and photo_row["people"] else {}
                persons = []
                for idx, (i, name) in enumerate(face_items):
                    cid = cluster_ids[idx]
                    action = actions[idx] if idx < len(actions) else ""
                    persons.append({"cluster_id": cid, "name": name, "action": action})
                new_people = {
                    "persons":              persons,
                    "names":                sorted(set(n for _, n in face_items if n)),
                    "face_count":           len(cluster_ids),
                    "source_apple_persons": (old_people or {}).get("source_apple_persons", []),
                }

                # 7. meta + group_versions
                meta = json.loads(photo_row["meta"]) if photo_row and photo_row["meta"] else {}
                meta.setdefault("group_versions", {})
                meta["group_versions"]["vision_description"] = vision.description_version
                meta["group_versions"]["vision_struct"]      = vision.struct_version
                meta["group_versions"]["people"]             = "apple+insightface@v9"
                # 重跑 vision 时清掉旧的 vision_role_mismatch 警告(避免累积)
                old_errors = meta.get("errors") or []
                meta["errors"] = [e for e in old_errors
                                  if (e.get("group") if isinstance(e, dict) else None)
                                     not in ("vision_role_mismatch", "vision_warn", "vision_desc_warn", "vision_struct_warn")]
                if warns:
                    meta.setdefault("errors", []).extend(
                        [{"group": "vision_warn", "error": w, "at": repo.now_iso()} for w in warns]
                    )
                # 跑 self-check:重跑后的 description vs 新 persons
                try:
                    from ..vision.role_check import check_description_vs_persons
                    new_desc = new_vision.get("description") or ""
                    if new_desc and persons:
                        issues = check_description_vs_persons(new_desc, persons)
                        for issue in issues:
                            meta.setdefault("errors", []).append({
                                "group": "vision_role_mismatch",
                                "error": issue,
                                "at": repo.now_iso(),
                            })
                except Exception as e:
                    meta.setdefault("errors", []).append({
                        "group": "vision_role_check_failed",
                        "error": f"{type(e).__name__}: {e}",
                        "at": repo.now_iso(),
                    })

                # album 事件关键词重注进 vision.tags(与扫描路径一致,重跑不丢)
                try:
                    albums = (meta.get("source_signals") or {}).get("albums") or []
                    if albums:
                        from . import album as album_mod
                        album_mod.merge_album_tags(new_vision, albums, conn)
                except Exception as e:
                    log.warning("album tags 合并失败 photo=%s: %s", pid, e)

                conn.execute(
                    """UPDATE photos SET vision = ?, people = ?, meta = ?, updated_at = ?
                       WHERE photo_id = ?""",
                    (
                        json.dumps(new_vision, ensure_ascii=False),
                        json.dumps(new_people, ensure_ascii=False),
                        json.dumps(meta, ensure_ascii=False),
                        repo.now_iso(),
                        pid,
                    ),
                )
                repo.update_fts(conn, pid, new_vision, new_people)
                from .runner import _get_embedder_for_scan
                emb_result = repo.update_embedding(
                    conn, pid, new_vision, new_people, _get_embedder_for_scan()
                )
                if emb_result == "failed":
                    log.warning("reprocess embedding 失败 photo=%s(FTS 已更新)", pid)
                done += 1
            except Exception as e:
                failed += 1
                log.exception("reprocess vision failed for %s", pid)
    finally:
        conn.close()

    return {
        "ok": True,
        "group": "vision",
        "requested": len(photo_ids),
        "done": done,
        "failed": failed,
    }


def reprocess_derived(root: Path) -> dict:
    """对所有非种子 photos 重跑 derived(time_bucket + location_bucket + photo_type + is_keeper)。

    location_bucket 现在会调高德 reverse geocoding(网格化缓存,同一栋楼只调一次)。
    几十万张照片 ~几分钟(GPS 缓存命中后 ~ms 级)。
    """
    import json
    from . import derived as derive_mod
    from ..schema.photo_record import stamp_group_version

    conn = db.connect(db.get_db_path(root))
    try:
        rows = conn.execute(
            "SELECT photo_id, identity, exif, vision, people, meta FROM photos WHERE source != 'seed'"
        ).fetchall()

        done = 0
        failed = 0
        no_gps = 0
        with_location = 0

        for r in rows:
            try:
                rec = {
                    "identity": json.loads(r["identity"]),
                    "exif":     json.loads(r["exif"])   if r["exif"]   else None,
                    "vision":   json.loads(r["vision"]) if r["vision"] else None,
                    "people":   json.loads(r["people"]) if r["people"] else None,
                    "meta":     json.loads(r["meta"])   if r["meta"]   else {},
                }
                new_derived = derive_mod.compute(rec, conn=conn)
                stamp_group_version(rec, "derived", "rules-v2-geocode")
                conn.execute(
                    "UPDATE photos SET derived=?, meta=?, updated_at=datetime('now') WHERE photo_id=?",
                    (
                        json.dumps(new_derived, ensure_ascii=False),
                        json.dumps(rec["meta"], ensure_ascii=False),
                        r["photo_id"],
                    ),
                )
                conn.commit()
                done += 1
                lb = new_derived.get("location_bucket") or {}
                if (rec.get("exif") or {}).get("gps") is None:
                    no_gps += 1
                if lb.get("city") or lb.get("aoi_name"):
                    with_location += 1
            except Exception as e:
                failed += 1
                log.exception("reprocess derived failed for %s", r["photo_id"])

        return {
            "ok": True,
            "group": "derived",
            "total": len(rows),
            "done": done,
            "failed": failed,
            "no_gps": no_gps,
            "with_location": with_location,
        }
    finally:
        conn.close()


def photo_ids_for_run(conn, run_id: str) -> list[str]:
    """某个 scan run 关联的所有(非种子)photo_id,按 jobs.run_id。"""
    rows = conn.execute(
        """SELECT j.photo_id FROM jobs j
           JOIN photos p ON p.photo_id = j.photo_id
           WHERE j.run_id = ? AND p.source != 'seed'""",
        (run_id,),
    ).fetchall()
    return [r[0] for r in rows]


def reprocess_albums(root: Path, photo_ids: list[str], dry_run: bool = False) -> dict:
    """从 source_signals.albums 解析,补 location_bucket 城市 + 合并事件关键词进 vision.tags。

    **不重跑 vision**(只读已存的 albums + 已有 vision)。本地 LLM 解析相册名(去重缓存)。
    dry_run=True 时不写库,只返回每张前后对比明细(供 review)。
    """
    import json
    from . import album as album_mod
    from . import derived as derive_mod
    from ..schema.photo_record import stamp_group_version

    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    embedder = None
    if not dry_run:
        from .runner import _get_embedder_for_scan
        embedder = _get_embedder_for_scan()
    try:
        done = 0
        failed = 0
        no_albums = 0
        city_filled = 0
        tags_added = 0
        details: list[dict] = []

        for pid in photo_ids:
            try:
                row = conn.execute(
                    "SELECT identity, exif, vision, people, derived, meta FROM photos WHERE photo_id = ?",
                    (pid,),
                ).fetchone()
                if row is None:
                    failed += 1
                    continue
                rec = {
                    "identity": json.loads(row["identity"]),
                    "exif":     json.loads(row["exif"])    if row["exif"]    else None,
                    "vision":   json.loads(row["vision"])  if row["vision"]  else None,
                    "people":   json.loads(row["people"])  if row["people"]  else None,
                    "derived":  json.loads(row["derived"]) if row["derived"] else None,
                    "meta":     json.loads(row["meta"])    if row["meta"]    else {},
                }
                albums = ((rec["meta"].get("source_signals") or {}).get("albums")) or []
                if not albums:
                    no_albums += 1
                    continue

                sig = album_mod.signals_for_albums(albums, conn)

                vision = rec.get("vision") or {}
                tags_before = list(vision.get("tags") or [])
                lb_before = (rec.get("derived") or {}).get("location_bucket") or {}
                city_before = lb_before.get("city")

                album_mod.merge_album_tags(vision, albums, conn)  # 原地改 vision["tags"]
                rec["vision"] = vision
                tags_after = list(vision.get("tags") or [])

                new_derived = derive_mod.compute(rec, conn=conn)
                lb_after = new_derived.get("location_bucket") or {}
                city_after = lb_after.get("city")

                if set(tags_after) != set(tags_before):
                    tags_added += 1
                if city_after and not city_before:
                    city_filled += 1

                if dry_run:
                    details.append({
                        "photo_id": pid,
                        "albums": albums,
                        "parsed": sig,
                        "tags_before": tags_before,
                        "tags_after": tags_after,
                        "city_before": city_before,
                        "city_after": city_after,
                        "place_name_after": lb_after.get("place_name"),
                        "formatted_address_after": lb_after.get("formatted_address"),
                    })
                else:
                    stamp_group_version(rec, "derived", "rules-v3-album")
                    conn.execute(
                        "UPDATE photos SET vision=?, derived=?, meta=?, updated_at=? WHERE photo_id=?",
                        (
                            json.dumps(vision, ensure_ascii=False),
                            json.dumps(new_derived, ensure_ascii=False),
                            json.dumps(rec["meta"], ensure_ascii=False),
                            repo.now_iso(),
                            pid,
                        ),
                    )
                    repo.update_fts(conn, pid, vision, rec.get("people"))
                    emb = repo.update_embedding(conn, pid, vision, rec.get("people"), embedder)
                    if emb == "failed":
                        log.warning("album reprocess embedding 失败 photo=%s", pid)
                    conn.commit()
                done += 1
            except Exception:
                failed += 1
                log.exception("reprocess albums failed for %s", pid)

        return {
            "ok": True,
            "group": "albums",
            "dry_run": dry_run,
            "requested": len(photo_ids),
            "done": done,
            "failed": failed,
            "no_albums": no_albums,
            "city_filled": city_filled,
            "tags_added": tags_added,
            "details": details,
        }
    finally:
        conn.close()


def select_photos(
    conn,
    source_ids: list[str] | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
    missing_field: str | None = None,
    person_count: str | None = None,
    person_ids: list[str] | None = None,
    fts_query: str | None = None,
    limit: int | None = None,
) -> list[str]:
    """白名单字段选择器:返回匹配 photo_id 列表。Web Reprocess 页 + /api/reprocess 用。

    missing_field 取值:
      - 'vision' / 'people' / 'derived'            → 对应 group IS NULL
      - 'description_empty' / 'description_short'  → vision.description 异常
      - 'location'                                 → derived.location_bucket 全空
    person_count: 'single'(face_count=1) / 'multi'(face_count>=2) / None
    person_ids: cluster_id 列表(AND 语义需要每个都出现?这里实现 OR — 出现任一即匹配)
    fts_query: 走 photos_fts MATCH
    """
    wheres: list[str] = ["p.source != 'seed'"]
    params: list = []

    if source_ids:
        placeholders = ",".join(["?"] * len(source_ids))
        wheres.append(f"p.source IN ({placeholders})")
        params.extend(source_ids)

    # 时间比较是字符串字典序;'YYYY-MM-DD' day-only 时,time_to 自动补 T23:59:59 覆盖整天
    if time_from:
        wheres.append(
            "COALESCE(NULLIF(json_extract(p.exif,'$.captured_at_utc'),''), "
            "json_extract(p.exif,'$.captured_at_local')) >= ?"
        )
        params.append(time_from)

    if time_to:
        tt = time_to
        if len(tt) == 10 and tt.count("-") == 2:
            tt = tt + "T23:59:59"
        wheres.append(
            "COALESCE(NULLIF(json_extract(p.exif,'$.captured_at_utc'),''), "
            "json_extract(p.exif,'$.captured_at_local')) <= ?"
        )
        params.append(tt)

    if missing_field == "vision":
        wheres.append("p.vision IS NULL")
    elif missing_field == "people":
        wheres.append("p.people IS NULL")
    elif missing_field == "derived":
        wheres.append("p.derived IS NULL")
    elif missing_field == "description_empty":
        wheres.append("(json_extract(p.vision,'$.description') IS NULL OR json_extract(p.vision,'$.description') = '')")
    elif missing_field == "description_short":
        wheres.append("(json_extract(p.vision,'$.description') IS NOT NULL AND length(json_extract(p.vision,'$.description')) < 50)")
    elif missing_field == "location":
        wheres.append(
            "(json_extract(p.derived,'$.location_bucket.city') IS NULL "
            "AND json_extract(p.derived,'$.location_bucket.aoi_name') IS NULL)"
        )
    elif missing_field == "role_mismatch":
        # description 语义对齐差:meta.errors 含 group='vision_role_mismatch' 且**未 acknowledged**
        wheres.append(
            "p.meta IS NOT NULL AND EXISTS ("
            "  SELECT 1 FROM json_each(json_extract(p.meta, '$.errors')) je"
            "  WHERE json_extract(je.value, '$.group') = 'vision_role_mismatch'"
            "    AND COALESCE(json_extract(je.value, '$.acknowledged'), 0) = 0"
            ")"
        )

    # 人数(走 people.face_count)
    if person_count == "none":
        wheres.append("(json_extract(p.people,'$.face_count') = 0 OR p.people IS NULL)")
    elif person_count == "single":
        wheres.append("json_extract(p.people,'$.face_count') = 1")
    elif person_count == "multi":
        wheres.append("json_extract(p.people,'$.face_count') >= 2")

    # 包含特定人物(任一 cluster_id 命中即可) — 走 faces 表 join
    if person_ids:
        ph = ",".join(["?"] * len(person_ids))
        wheres.append(f"p.photo_id IN (SELECT photo_id FROM faces WHERE cluster_id IN ({ph}))")
        params.extend(person_ids)

    join_fts = ""
    if fts_query:
        join_fts = "JOIN photos_fts f ON f.photo_id = p.photo_id"
        wheres.append("f.photos_fts MATCH ?")
        params.append(fts_query)

    where_sql = " AND ".join(wheres) if wheres else "1=1"
    sql = f"SELECT p.photo_id FROM photos p {join_fts} WHERE {where_sql}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [r[0] for r in rows]


def refill_people_from_faces(conn, photo_ids: list[str] | None = None) -> None:
    """对每张 photo,根据当前 faces 表的 cluster_id 同步重建 people.persons[]。

    保留旧 action 描述(LLM 写的不变),只更新 cluster_id 和 name 部分。
    给 vision.description 加个 stale 警告(因为它是 LLM 死字符串,改名/重新归类后可能过时)。
    """
    import json
    name_map = repo.cluster_name_map(conn)
    if photo_ids:
        placeholders = ",".join(["?"] * len(photo_ids))
        rows = conn.execute(
            f"SELECT photo_id, people FROM photos "
            f"WHERE source != 'seed' AND photo_id IN ({placeholders})",
            photo_ids,
        ).fetchall()
    elif photo_ids == []:
        rows = []
    else:
        rows = conn.execute(
            "SELECT photo_id, people FROM photos WHERE source != 'seed'"
        ).fetchall()
    for r in rows:
        pid = r["photo_id"]
        old_people = json.loads(r["people"]) if r["people"] else {}
        old_persons = old_people.get("persons") or []
        face_rows = conn.execute(
            "SELECT cluster_id FROM faces WHERE photo_id = ? ORDER BY face_id",
            (pid,),
        ).fetchall()
        cluster_ids = [fr[0] for fr in face_rows]
        new_persons = []
        for i, cid in enumerate(cluster_ids):
            old_action = old_persons[i].get("action", "") if i < len(old_persons) else ""
            new_persons.append({"cluster_id": cid, "name": name_map.get(cid), "action": old_action})
        new_people = {
            "persons":              new_persons,
            "names":                sorted(set(name_map[c] for c in cluster_ids if c in name_map)),
            "face_count":           len(cluster_ids),
            "source_apple_persons": old_people.get("source_apple_persons", []),
        }
        conn.execute(
            "UPDATE photos SET people = ?, updated_at = ? WHERE photo_id = ?",
            (json.dumps(new_people, ensure_ascii=False), repo.now_iso(), pid),
        )


_refill_vision_names = refill_people_from_faces
