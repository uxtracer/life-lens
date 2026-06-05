"""Photo / Job / Source 仓储层。所有 SQL 集中于此。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- sources ----------

def upsert_source(conn: sqlite3.Connection, source_id: str, kind: str, config: dict) -> None:
    conn.execute(
        """
        INSERT INTO sources(source_id, kind, config, enabled, created_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(source_id) DO UPDATE SET config=excluded.config
        """,
        (source_id, kind, json.dumps(config, ensure_ascii=False), now_iso()),
    )


def list_sources(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM sources ORDER BY created_at").fetchall()
    return [
        {**dict(r), "config": json.loads(r["config"])} for r in rows
    ]


def delete_source(conn: sqlite3.Connection, source_id: str) -> None:
    """删 source + 级联清理孤儿 scan_runs / jobs。

    保留 status='done' 的历史 run(审计 / photos 表里仍有那次扫到的照片);
    只清理 source_ids 完全等于 [source_id] 且非 done 状态的 run,以及它们的 jobs。
    跨 source 的 run(source_ids 含多个)不动。
    """
    # 找出所有 source_ids 只含此 source 且非 done 的 run
    orphan_runs = conn.execute(
        """
        SELECT run_id FROM scan_runs
        WHERE source_ids = ? AND status != 'done'
        """,
        (json.dumps([source_id], ensure_ascii=False),),
    ).fetchall()
    orphan_ids = [r["run_id"] for r in orphan_runs]
    for rid in orphan_ids:
        conn.execute("DELETE FROM jobs WHERE run_id = ?", (rid,))
        conn.execute("DELETE FROM scan_runs WHERE run_id = ?", (rid,))
    conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))


def mark_source_scanned(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute("UPDATE sources SET last_scan_at = ? WHERE source_id = ?", (now_iso(), source_id))


# ---------- photos ----------

def upsert_photo(conn: sqlite3.Connection, record: dict) -> None:
    """record 是 PhotoRecord.to_dict() 的输出(6 个 group + identity 顶层字段)。"""
    identity = record["identity"]
    now = now_iso()
    conn.execute(
        """
        INSERT INTO photos(photo_id, source, source_ref, original_path, content_hash,
                           schema_version, identity, exif, vision, people, derived, meta,
                           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(photo_id) DO UPDATE SET
            content_hash   = excluded.content_hash,
            identity       = excluded.identity,
            exif           = excluded.exif,
            vision         = COALESCE(excluded.vision, photos.vision),
            people         = COALESCE(excluded.people, photos.people),
            derived        = excluded.derived,
            meta           = excluded.meta,
            updated_at     = excluded.updated_at
        """,
        (
            identity["photo_id"],
            identity["source"],
            identity["source_ref"],
            identity["original_path"],
            identity["content_hash"],
            record.get("schema_version", "0.1"),
            json.dumps(identity, ensure_ascii=False),
            json.dumps(record.get("exif"), ensure_ascii=False) if record.get("exif") else None,
            json.dumps(record.get("vision"), ensure_ascii=False) if record.get("vision") else None,
            json.dumps(record.get("people"), ensure_ascii=False) if record.get("people") else None,
            json.dumps(record.get("derived"), ensure_ascii=False) if record.get("derived") else None,
            json.dumps(record.get("meta", {}), ensure_ascii=False),
            now,
            now,
        ),
    )


def get_photo(conn: sqlite3.Connection, photo_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM photos WHERE photo_id = ?", (photo_id,)).fetchone()
    if not row:
        return None
    return _row_to_record(row)


def list_photos(
    conn: sqlite3.Connection,
    limit: int = 100,
    offset: int = 0,
    order_by: str = "captured",
    favorite_only: bool = False,
) -> list[dict]:
    """列出已处理完成的生活记录照片(排除种子图 + 排除 vision 未生成的 placeholder 行)。

    favorite_only: True → 只列 Apple 收藏(derived.favorite=1 的生成列)。

    order_by:
      - 'captured'(默认):按拍照时间倒序。fallback:iPhone EXIF 经常没 OffsetTimeOriginal
        → captured_at_utc 是空字符串(生成列从 JSON 抽,空 JSON 字段 → '')。
        COALESCE+NULLIF 把空字符串当 NULL,fallback 到 captured_at_local。
      - 'imported':按"最近处理完"倒序(photos.updated_at,vision 完成时刷新,= meta.processed_at)。
        扫描期间用户想看"刚跑完 vision 的一批",跟拍照时间无关。
        ⚠ 不用 created_at — Phase A 一次性 enqueue,所有照片 created_at 在同一秒,无区分度。
    """
    if order_by == "imported":
        order_sql = "ORDER BY updated_at DESC, photo_id"
    else:
        order_sql = (
            "ORDER BY COALESCE(NULLIF(captured_at_utc, ''), "
            "                  json_extract(exif, '$.captured_at_local')) DESC NULLS LAST, "
            "         photo_id"
        )
    fav_sql = " AND favorite = 1" if favorite_only else ""
    rows = conn.execute(
        f"SELECT * FROM photos "
        f"WHERE source != 'seed' AND vision IS NOT NULL{fav_sql} "
        f"{order_sql} "
        f"LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [_row_to_record(r) for r in rows]


def count_photos(conn: sqlite3.Connection, favorite_only: bool = False) -> int:
    """已处理完成的生活记录照片总数(排除种子图 + placeholder 行)。"""
    fav_sql = " AND favorite = 1" if favorite_only else ""
    return conn.execute(
        f"SELECT COUNT(*) FROM photos WHERE source != 'seed' AND vision IS NOT NULL{fav_sql}"
    ).fetchone()[0]


def get_existing_hash(conn: sqlite3.Connection, photo_id: str) -> Optional[str]:
    row = conn.execute("SELECT content_hash FROM photos WHERE photo_id = ?", (photo_id,)).fetchone()
    return row[0] if row else None


def _row_to_record(row: sqlite3.Row) -> dict:
    return {
        "schema_version": row["schema_version"],
        "identity": json.loads(row["identity"]),
        "exif":     json.loads(row["exif"])     if row["exif"]     else None,
        "vision":   json.loads(row["vision"])   if row["vision"]   else None,
        "people":   json.loads(row["people"])   if row["people"]   else None,
        "derived":  json.loads(row["derived"])  if row["derived"]  else None,
        "meta":     json.loads(row["meta"]),
    }


# ---------- jobs ----------

def enqueue_job(
    conn: sqlite3.Connection,
    photo_id: str,
    run_id: Optional[str] = None,
    captured_at_local: Optional[str] = None,
) -> None:
    """入队一张 photo 到 jobs 表。重复入队会重置 status=pending 但保留 retry_count。"""
    conn.execute(
        """
        INSERT INTO jobs(photo_id, status, retry_count, enqueued_at, run_id, captured_at_local)
        VALUES (?, 'pending', 0, ?, ?, ?)
        ON CONFLICT(photo_id) DO UPDATE SET
            status='pending',
            last_error=NULL,
            enqueued_at=excluded.enqueued_at,
            run_id=excluded.run_id,
            captured_at_local=COALESCE(excluded.captured_at_local, jobs.captured_at_local)
        """,
        (photo_id, now_iso(), run_id, captured_at_local),
    )


def claim_job(conn: sqlite3.Connection, photo_id: str, stage: str = "processing") -> None:
    conn.execute(
        "UPDATE jobs SET status='processing', stage=?, started_at=? WHERE photo_id=?",
        (stage, now_iso(), photo_id),
    )


def finish_job(conn: sqlite3.Connection, photo_id: str) -> None:
    conn.execute(
        "UPDATE jobs SET status='done', finished_at=? WHERE photo_id=?",
        (now_iso(), photo_id),
    )


def fail_job(
    conn: sqlite3.Connection,
    photo_id: str,
    err: str,
    *,
    max_retries: int = 3,
) -> str:
    """失败处理:retry_count < max_retries → 重置 pending(可重试);否则永久 failed。

    返回最终 status('pending' 或 'failed')。
    """
    row = conn.execute("SELECT retry_count FROM jobs WHERE photo_id=?", (photo_id,)).fetchone()
    retry_count = (row[0] if row else 0) + 1
    if retry_count < max_retries:
        conn.execute(
            """
            UPDATE jobs
            SET status='pending', last_error=?, retry_count=?, finished_at=NULL
            WHERE photo_id=?
            """,
            (err, retry_count, photo_id),
        )
        return "pending"
    conn.execute(
        """
        UPDATE jobs
        SET status='failed', last_error=?, retry_count=?, finished_at=?
        WHERE photo_id=?
        """,
        (err, retry_count, now_iso(), photo_id),
    )
    return "failed"


def reset_stuck_jobs(conn: sqlite3.Connection) -> int:
    """启动时调用 — 把 processing 的残留重置回 pending。"""
    cur = conn.execute("UPDATE jobs SET status='pending' WHERE status='processing'")
    return cur.rowcount


def get_pending(
    conn: sqlite3.Connection,
    limit: int = 100,
    run_id: Optional[str] = None,
) -> list[str]:
    """按拍照时间正序拉 pending(旧→新);run_id 可选过滤。"""
    if run_id is None:
        rows = conn.execute(
            """
            SELECT photo_id FROM jobs
            WHERE status='pending'
            ORDER BY captured_at_local ASC NULLS LAST, enqueued_at
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT photo_id FROM jobs
            WHERE status='pending' AND run_id=?
            ORDER BY captured_at_local ASC NULLS LAST, enqueued_at
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
    return [r[0] for r in rows]


def get_job_status(conn: sqlite3.Connection, photo_id: str) -> Optional[str]:
    row = conn.execute("SELECT status FROM jobs WHERE photo_id=?", (photo_id,)).fetchone()
    return row[0] if row else None


def reset_failed_jobs(conn: sqlite3.Connection, run_id: Optional[str] = None) -> int:
    """把 failed 重置 pending(给 --retry-failed / Web 重试按钮用)。"""
    if run_id is None:
        cur = conn.execute(
            "UPDATE jobs SET status='pending', retry_count=0, last_error=NULL WHERE status='failed'"
        )
    else:
        cur = conn.execute(
            "UPDATE jobs SET status='pending', retry_count=0, last_error=NULL WHERE status='failed' AND run_id=?",
            (run_id,),
        )
    return cur.rowcount


def reset_jobs_for_reprocess(
    conn: sqlite3.Connection,
    photo_ids: list[str],
    run_id: str,
) -> int:
    """Reprocess 用:把指定 photo_id 列表重置 pending,挂到新 run_id。"""
    if not photo_ids:
        return 0
    n = 0
    for pid in photo_ids:
        conn.execute(
            """
            INSERT INTO jobs(photo_id, status, retry_count, enqueued_at, run_id)
            VALUES (?, 'pending', 0, ?, ?)
            ON CONFLICT(photo_id) DO UPDATE SET
                status='pending',
                retry_count=0,
                last_error=NULL,
                enqueued_at=excluded.enqueued_at,
                run_id=excluded.run_id
            """,
            (pid, now_iso(), run_id),
        )
        n += 1
    return n


def job_stats(conn: sqlite3.Connection, run_id: Optional[str] = None) -> dict[str, int]:
    if run_id is None:
        rows = conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
    else:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM jobs WHERE run_id=? GROUP BY status", (run_id,)
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def recent_failures(conn: sqlite3.Connection, run_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    """最近 N 条失败(供 CLI status / Web Runs 详情用)。"""
    if run_id is None:
        rows = conn.execute(
            """
            SELECT j.photo_id, j.last_error, j.finished_at, j.retry_count,
                   p.original_path, j.captured_at_local
            FROM jobs j
            LEFT JOIN photos p ON p.photo_id = j.photo_id
            WHERE j.status='failed'
            ORDER BY j.finished_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT j.photo_id, j.last_error, j.finished_at, j.retry_count,
                   p.original_path, j.captured_at_local
            FROM jobs j
            LEFT JOIN photos p ON p.photo_id = j.photo_id
            WHERE j.status='failed' AND j.run_id=?
            ORDER BY j.finished_at DESC
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- scan_runs ----------

def create_run(
    conn: sqlite3.Connection,
    run_id: str,
    kind: str,
    triggered_by: str,
    source_ids: Optional[list[str]] = None,
    selector: Optional[dict] = None,
    note: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO scan_runs(run_id, kind, source_ids, selector, status, triggered_by, started_at, note)
        VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
        """,
        (
            run_id, kind,
            json.dumps(source_ids or [], ensure_ascii=False),
            json.dumps(selector, ensure_ascii=False) if selector else None,
            triggered_by, now_iso(), note,
        ),
    )


def update_run_counts(
    conn: sqlite3.Connection,
    run_id: str,
    total: int,
    done: int,
    failed: int,
) -> None:
    """绝对值写入(reprocess 内部自己维护 counter 的场景)。
    对 scan_source / process_pending_for_run 路径**不要用这个**,改用 inc_run_done / inc_run_failed,
    避免 resume 时把累积 done 覆盖成 0。
    """
    conn.execute(
        "UPDATE scan_runs SET total=?, done=?, failed=? WHERE run_id=?",
        (total, done, failed, run_id),
    )


def set_run_total(conn: sqlite3.Connection, run_id: str, total: int) -> None:
    """单独写 total(scan 阶段入队完成时调一次,resume 不动)。"""
    conn.execute("UPDATE scan_runs SET total=? WHERE run_id=?", (total, run_id))


def inc_run_done(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("UPDATE scan_runs SET done = done + 1 WHERE run_id=?", (run_id,))


def inc_run_failed(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("UPDATE scan_runs SET failed = failed + 1 WHERE run_id=?", (run_id,))


def finish_run(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    """status ∈ {'completed', 'stopped', 'failed'}

    只更新 status + finished_at。time_range / scanned_up_to 不在这里写:
    - time_range:在 enqueue 完成后 set_run_time_range 一次性写入(那时 jobs 完整)
    - scanned_up_to:_process_phase 每张完成时增量 max
    这样后续 reprocess 偷 jobs 行不会影响历史 run 的进度展示。
    """
    conn.execute(
        "UPDATE scan_runs SET status=?, finished_at=? WHERE run_id=?",
        (status, now_iso(), run_id),
    )


def set_run_time_range(conn: sqlite3.Connection, run_id: str, lo: Optional[str], hi: Optional[str]) -> None:
    """Phase A 入队完成后调,一次性把时间范围冻结到快照(jobs 表完整时)。"""
    conn.execute(
        "UPDATE scan_runs SET snapshot_captured_min=?, snapshot_captured_max=? WHERE run_id=?",
        (lo, hi, run_id),
    )


def bump_run_scanned_up_to(conn: sqlite3.Connection, run_id: str, captured_at: Optional[str]) -> None:
    """每张完成后调,把 scanned_up_to 向前推进(取 max)。"""
    if not captured_at:
        return
    conn.execute(
        "UPDATE scan_runs SET snapshot_scanned_up_to = "
        "MAX(COALESCE(snapshot_scanned_up_to, ''), ?) WHERE run_id=?",
        (captured_at, run_id),
    )


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_run_row(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM scan_runs WHERE run_id=?", (run_id,)).fetchone()
    return _run_row(row) if row else None


def _run_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["source_ids"] = json.loads(d["source_ids"]) if d.get("source_ids") else []
    except Exception:
        d["source_ids"] = []
    try:
        d["selector"] = json.loads(d["selector"]) if d.get("selector") else None
    except Exception:
        d["selector"] = None
    return d


def get_resumable_run(conn: sqlite3.Connection) -> Optional[dict]:
    """找最近一个 stopped/failed 且仍有 pending 的 run。保留以备兼容,新代码用 list_resumable_runs。"""
    runs = list_resumable_runs(conn, limit=1)
    return runs[0] if runs else None


def list_resumable_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """所有 stopped/failed 且仍有 pending(scan_runs.total > done + failed)的 run,按开始时间倒序。

    pending 不能再依赖 jobs 表(reprocess 会偷 jobs),用 scan_runs.total - done - failed 算。
    """
    rows = conn.execute(
        """
        SELECT r.*,
               (SELECT MAX(captured_at_local) FROM jobs
                WHERE run_id=r.run_id AND status='done') AS live_scanned_up_to,
               (r.total - r.done - r.failed) AS pending_count
        FROM scan_runs r
        WHERE r.status IN ('stopped','failed')
          AND (r.total - r.done - r.failed) > 0
        ORDER BY r.started_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for row in rows:
        d = _run_row(row)
        d["pending"] = row["pending_count"] or 0
        d["scanned_up_to"] = d.get("snapshot_scanned_up_to") or row["live_scanned_up_to"]
        out.append(d)
    return out


def get_running_run(conn: sqlite3.Connection) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM scan_runs WHERE status='running' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return _run_row(row) if row else None


def mark_running_as_stopped(conn: sqlite3.Connection) -> int:
    """server 启动时调用:把所有 running 的 run 标 stopped(因为进程已挂)。"""
    cur = conn.execute(
        "UPDATE scan_runs SET status='stopped', finished_at=? WHERE status='running'",
        (now_iso(),),
    )
    return cur.rowcount


def get_run_time_range(conn: sqlite3.Connection, run_id: str) -> tuple[Optional[str], Optional[str]]:
    row = conn.execute(
        """
        SELECT MIN(captured_at_local) AS lo, MAX(captured_at_local) AS hi
        FROM jobs WHERE run_id=? AND captured_at_local IS NOT NULL AND captured_at_local != ''
        """,
        (run_id,),
    ).fetchone()
    if not row:
        return None, None
    return row["lo"], row["hi"]


def get_run_scanned_up_to(conn: sqlite3.Connection, run_id: str) -> Optional[str]:
    """该 run 下 done 状态 jobs 的 max(captured_at_local) — 用作时间游标。"""
    row = conn.execute(
        """
        SELECT MAX(captured_at_local) AS scanned_up_to
        FROM jobs
        WHERE run_id=? AND status='done' AND captured_at_local IS NOT NULL AND captured_at_local != ''
        """,
        (run_id,),
    ).fetchone()
    return row["scanned_up_to"] if row else None


# ---------- faces / persons ----------

def ensure_photo_row(conn: sqlite3.Connection, identity: dict) -> None:
    """face 阶段前调:确保 photos 行存在,让 faces FK 通过。后续 upsert_photo 会覆盖完整字段。"""
    now = now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO photos(photo_id, source, source_ref, original_path, content_hash,
                                     identity, meta, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identity["photo_id"],
            identity["source"],
            identity["source_ref"],
            identity["original_path"],
            identity["content_hash"],
            json.dumps(identity, ensure_ascii=False),
            json.dumps({}, ensure_ascii=False),
            now,
            now,
        ),
    )


def insert_face(
    conn: sqlite3.Connection,
    face_id: str,
    photo_id: str,
    cluster_id: Optional[str],
    embedding_bytes: bytes,
    bbox: tuple[float, float, float, float],
) -> None:
    conn.execute(
        """
        INSERT INTO faces(face_id, photo_id, cluster_id, embedding, bbox, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(face_id) DO UPDATE SET cluster_id=excluded.cluster_id
        """,
        (
            face_id,
            photo_id,
            cluster_id,
            embedding_bytes,
            json.dumps(list(bbox), ensure_ascii=False),
            now_iso(),
        ),
    )


def delete_faces_of_photo(conn: sqlite3.Connection, photo_id: str) -> None:
    """重跑该照片人脸前先清掉旧的(face_id 含 photo_id,理论上不会冲突,但稳妥)。"""
    conn.execute("DELETE FROM faces WHERE photo_id = ?", (photo_id,))


def list_face_clusters(conn: sqlite3.Connection, named_only: bool = False) -> list[dict]:
    """按 face_count 排序;每个 cluster 给 3 个代表 face_id 用于 crop 展示。

    named_only=True 时只返回已命名的(用于"种子人物"视图)。
    """
    where_clauses = ["f.cluster_id IS NOT NULL"]
    if named_only:
        where_clauses.append("p.name IS NOT NULL")
    where_sql = " AND ".join(where_clauses)
    rows = conn.execute(
        f"""
        SELECT
            f.cluster_id,
            COUNT(*) AS face_count,
            p.name AS person_name
        FROM faces f
        LEFT JOIN persons p ON p.cluster_id = f.cluster_id
        WHERE {where_sql}
        GROUP BY f.cluster_id
        ORDER BY (p.name IS NOT NULL) DESC, face_count DESC
        """
    ).fetchall()
    result = []
    for r in rows:
        cid = r["cluster_id"]
        reps = conn.execute(
            "SELECT face_id FROM faces WHERE cluster_id = ? LIMIT 3",
            (cid,),
        ).fetchall()
        result.append({
            "cluster_id":  cid,
            "face_count":  r["face_count"],
            "person_name": r["person_name"],
            "sample_face_ids": [x[0] for x in reps],
        })
    return result


def set_person_name(conn: sqlite3.Connection, cluster_id: str, name: Optional[str]) -> None:
    """命名 / 改名 / 取消命名(name=None)。"""
    if name:
        conn.execute(
            """
            INSERT INTO persons(cluster_id, name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cluster_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at
            """,
            (cluster_id, name, now_iso()),
        )
    else:
        conn.execute("DELETE FROM persons WHERE cluster_id = ?", (cluster_id,))


def cluster_name_map(conn: sqlite3.Connection) -> dict[str, str]:
    """全表 cluster_id → name 映射(persons 表)。查询时透明替换用。"""
    rows = conn.execute("SELECT cluster_id, name FROM persons WHERE name IS NOT NULL").fetchall()
    return {r["cluster_id"]: r["name"] for r in rows}


def cluster_demographics_map(conn: sqlite3.Connection) -> dict[str, dict]:
    """cluster_id → {age_estimate, gender_estimate} 映射,供 vision prompt 注入 hint。"""
    rows = conn.execute(
        "SELECT cluster_id, age_estimate, gender_estimate FROM persons WHERE age_estimate IS NOT NULL OR gender_estimate IS NOT NULL"
    ).fetchall()
    return {r["cluster_id"]: {"age": r["age_estimate"], "gender": r["gender_estimate"]} for r in rows}


def set_person_demographics(
    conn: sqlite3.Connection,
    cluster_id: str,
    age_estimate: Optional[int],
    gender_estimate: Optional[int],
) -> None:
    """种子人物上传后调用,把多张种子图算出来的平均 age / 多数 gender 存进去。"""
    conn.execute(
        """
        INSERT INTO persons(cluster_id, age_estimate, gender_estimate, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(cluster_id) DO UPDATE SET
            age_estimate=excluded.age_estimate,
            gender_estimate=excluded.gender_estimate,
            updated_at=excluded.updated_at
        """,
        (cluster_id, age_estimate, gender_estimate, now_iso()),
    )


def resolve_persons(record: dict, name_map: dict[str, str]) -> dict:
    """实时把 people.persons[].cluster_id 映射出最新 name(persons 表改名后立刻生效)。
    返回新 dict(原 record 不变)。
    """
    if not record or not record.get("people"):
        return record
    out = dict(record)
    people = dict(out["people"])
    persons = people.get("persons") or []
    new_persons = []
    resolved_names: list[str] = []
    for p in persons:
        np = dict(p)
        cid = np.get("cluster_id")
        if cid and cid in name_map:
            np["name"] = name_map[cid]
            resolved_names.append(name_map[cid])
        new_persons.append(np)
    people["persons"] = new_persons
    if resolved_names:
        people["names"] = sorted(set((people.get("names") or []) + resolved_names))
    out["people"] = people
    return out


def list_faces_for_photo(conn: sqlite3.Connection, photo_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT f.face_id, f.cluster_id, f.bbox, p.name
        FROM faces f
        LEFT JOIN persons p ON p.cluster_id = f.cluster_id
        WHERE f.photo_id = ?
        """,
        (photo_id,),
    ).fetchall()
    return [{
        "face_id": r["face_id"],
        "cluster_id": r["cluster_id"],
        "name": r["name"],
        "bbox": json.loads(r["bbox"]) if r["bbox"] else None,
    } for r in rows]


# ---------- FTS ----------

def update_fts(conn: sqlite3.Connection, photo_id: str,
               vision: Optional[dict], people: Optional[dict] = None) -> None:
    """重建该照片的 FTS 行。description 来自 vision,actions 来自 people.persons[]。"""
    conn.execute("DELETE FROM photos_fts WHERE photo_id = ?", (photo_id,))
    if not vision:
        return
    actions = " ".join(p.get("action", "") for p in ((people or {}).get("persons") or []))
    objects = " ".join(vision.get("objects") or [])
    tags    = " ".join(vision.get("tags") or [])
    conn.execute(
        """
        INSERT INTO photos_fts(photo_id, description, scene, tags, ocr_text, actions, objects)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            photo_id,
            vision.get("description", "") or "",
            vision.get("scene", "") or "",
            tags,
            vision.get("ocr_text", "") or "",
            actions,
            objects,
        ),
    )


# ---------- embedding(语义向量,与 update_fts 同时机调用)----------

def update_embedding(
    conn: sqlite3.Connection,
    photo_id: str,
    vision: Optional[dict],
    people: Optional[dict],
    embedder,
) -> str:
    """build_source_text + text_hash 增量写 photo_embeddings。

    Returns: 'skipped'(text 空或 hash 未变)/ 'wrote'(新增或更新)/ 'failed'(嵌入异常)。

    embedder: 任何提供 .embed_one(str) → np.ndarray 和 .model/.dim 属性的对象;None 时跳过。
    失败不抛 — scan 不能因 embedding 挂。调用方拿到 'failed' 自行 log。
    """
    if embedder is None:
        return "skipped"
    from ..embed import build_source_text, text_hash
    text = build_source_text(vision, people)
    if not text:
        return "skipped"
    h = text_hash(text)
    row = conn.execute(
        "SELECT text_hash FROM photo_embeddings WHERE photo_id = ?", (photo_id,)
    ).fetchone()
    if row and row[0] == h:
        return "skipped"
    try:
        vec = embedder.embed_one(text)
    except Exception:
        return "failed"
    conn.execute(
        """
        INSERT INTO photo_embeddings (photo_id, model, dim, vec, text_hash, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(photo_id) DO UPDATE SET
            model=excluded.model, dim=excluded.dim, vec=excluded.vec,
            text_hash=excluded.text_hash, updated_at=excluded.updated_at
        """,
        (photo_id, embedder.model, embedder.dim, vec.tobytes(), h, now_iso()),
    )
    return "wrote"
