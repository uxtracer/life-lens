"""REST API 路由。所有路由都从 app.state.root 拿数据根目录。"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Body, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse

from ..store import db, repo
from ..sources.filesystem import FilesystemSource
from ..sources.photos_library import ApplePhotosSource
from ..scanner.runner import scan_source, process_pending_for_run, Progress, generate_run_id
from ..geocode import amap as amap_geo

router = APIRouter()

# 单全局扫描状态(同时间只跑一个 run)
_progress_lock = threading.Lock()
_current_progress: Optional[Progress] = None
_current_thread: Optional[threading.Thread] = None


def _set_current(progress: Progress, thread: threading.Thread):
    global _current_progress, _current_thread
    with _progress_lock:
        _current_progress = progress
        _current_thread = thread


def _build_source(target: dict):
    kind = target["kind"]
    if kind == "filesystem":
        return FilesystemSource(Path(target["config"]["path"]))
    if kind == "photos_library":
        return ApplePhotosSource(Path(target["config"]["path"]))
    raise HTTPException(501, f"暂未支持 source kind: {kind}")


def _get_root(request: Request) -> Path:
    return request.app.state.root


def _conn(request: Request):
    conn = db.connect(db.get_db_path(_get_root(request)))
    db.init_schema(conn)
    return conn


# ---------- sources ----------

@router.get("/sources")
def list_sources(request: Request):
    conn = _conn(request)
    try:
        return {"sources": repo.list_sources(conn)}
    finally:
        conn.close()


@router.post("/sources")
def add_source(request: Request, body: dict = Body(...)):
    """body: { kind: 'filesystem' | 'photos_library', path?: '/abs/path' }"""
    conn = _conn(request)
    try:
        kind = body.get("kind")
        if kind == "filesystem":
            path = body.get("path")
            if not path:
                raise HTTPException(400, "filesystem 类型必须提供 path")
            p = Path(path).expanduser().resolve()
            if not p.is_dir():
                raise HTTPException(400, f"目录不存在: {p}")
            source_id = f"fs:{p}"
            repo.upsert_source(conn, source_id, "filesystem", {"path": str(p)})
            return {"source_id": source_id}
        elif kind == "photos_library":
            path = body.get("path") or str(Path.home() / "Pictures" / "Photos Library.photoslibrary")
            p = Path(path).expanduser().resolve()
            if not p.exists() or not p.name.endswith(".photoslibrary"):
                raise HTTPException(400, f"Apple Photos 库不存在或路径不对: {p}")
            # TCC 探测:数据库可读才能接入
            dbfile = p / "database" / "Photos.sqlite"
            if not dbfile.exists():
                raise HTTPException(400, f"找不到 Photos 数据库: {dbfile}")
            try:
                with open(dbfile, "rb") as _f:
                    _f.read(16)
            except PermissionError:
                raise HTTPException(
                    403,
                    "无法读取 Apple Photos 数据库。请到 系统设置 → 隐私与安全性 → "
                    "完全磁盘访问权限,把 Terminal/iTerm 勾上,然后重启终端再试。"
                )
            source_id = f"apple:{p.name}"
            repo.upsert_source(conn, source_id, "photos_library", {"path": str(p)})
            return {"source_id": source_id}
        else:
            raise HTTPException(400, f"未知 kind: {kind}")
    finally:
        conn.close()


@router.post("/sources/pick-folder")
def pick_folder(mode: str = "folder"):
    """打开本地原生文件夹选择对话框,返回绝对路径。

    mode:
      - "folder"(默认): 选普通文件夹。注意 macOS `choose folder` 不让选 .photoslibrary
        这类 package(系统行为,显示为灰色)
      - "photos_library": 选 Apple Photos 图库 (.photoslibrary)。走 `choose file` + UTI
        过滤(`com.apple.photos.library`),把 package 当文件来选

    Linux:有 zenity 就用,没有就返 501,让用户手填。
    Windows:暂不支持(未测试)。
    """
    import platform
    import subprocess
    sys = platform.system()
    if sys == "Darwin":
        if mode == "photos_library":
            # .photoslibrary 是 macOS package(包),`choose folder` 不让选(灰色)。
            # `choose file` 默认把 package 当文件处理(showing package contents=false),
            # 不加 of type 过滤(新版 macOS 对 UTI 字符串支持不一致),让用户在
            # Pictures 目录里直接选 .photoslibrary。后端验路径后缀兜底。
            script = (
                'POSIX path of (choose file with prompt '
                '"选择 Apple Photos 图库 (.photoslibrary):" '
                'default location (path to pictures folder))'
            )
        else:
            script = 'POSIX path of (choose folder with prompt "选择要扫描的照片目录:")'
        try:
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=300,
            )
            if r.returncode != 0:
                err = (r.stderr or "").strip()
                # 用户取消按钮 → 返回 cancelled,不算错
                if "User canceled" in err or "-128" in err or not err:
                    return {"path": None, "cancelled": True}
                raise HTTPException(500, f"folder picker failed: {err}")
            # 剥尾斜杠:choose file 选 .photoslibrary 包返回带 / 的 POSIX 路径,
            # 前端按后缀判 kind 会误走 filesystem,这里统一在出口剥掉
            path = (r.stdout.strip().rstrip("/")) or "/"
            if mode == "photos_library" and not path.endswith(".photoslibrary"):
                raise HTTPException(400, f"必须选 .photoslibrary 结尾的 Apple Photos 图库,你选的是:{path}")
            return {"path": path, "cancelled": False}
        except subprocess.TimeoutExpired:
            return {"path": None, "cancelled": True}
    elif sys == "Linux":
        # 尝试 zenity
        import shutil
        if shutil.which("zenity"):
            try:
                r = subprocess.run(
                    ["zenity", "--file-selection", "--directory", "--title=选择要扫描的照片目录"],
                    capture_output=True, text=True, timeout=300,
                )
                if r.returncode != 0:
                    return {"path": None, "cancelled": True}
                return {"path": r.stdout.strip(), "cancelled": False}
            except subprocess.TimeoutExpired:
                return {"path": None, "cancelled": True}
        raise HTTPException(501, "Linux 上找不到 zenity,请手动填路径")
    else:
        raise HTTPException(501, f"{sys} 暂不支持本地文件夹选择,请手动填路径")


@router.delete("/sources/{source_id:path}")
def delete_source(request: Request, source_id: str):
    conn = _conn(request)
    try:
        repo.delete_source(conn, source_id)
        return {"ok": True}
    finally:
        conn.close()


# ---------- scan ----------

def _current_snapshot() -> Optional[dict]:
    if _current_progress is None:
        return None
    snap = _current_progress.snapshot()
    if snap.get("finished") and _current_thread is not None and not _current_thread.is_alive():
        return None
    return snap


def _running() -> bool:
    snap = _current_snapshot()
    return bool(snap and not snap.get("finished"))


@router.post("/scan")
def scan(request: Request, body: dict = Body(default={})):
    """触发新扫描。body:
      - { source_ids?: [...] }  不传 = 所有 enabled source(此处简单实现:第一个)
    返回 { run_id }
    """
    if _running():
        raise HTTPException(409, "已经有扫描任务在跑")

    root = _get_root(request)
    body = body or {}
    source_ids = body.get("source_ids")

    conn = _conn(request)
    try:
        sources = repo.list_sources(conn)
        if source_ids:
            targets = [s for s in sources if s["source_id"] in source_ids]
        else:
            targets = [s for s in sources if s.get("enabled", 1)]
        if not targets:
            raise HTTPException(400, "没有可扫描的 source")
        # 简化:目前一次只跑一个 source(同时间一个 run)。多 source 由用户分次触发
        target = targets[0]
    finally:
        conn.close()

    src = _build_source(target)
    run_id = generate_run_id()
    progress = Progress(run_id=run_id)

    def worker():
        try:
            from ..vision.ollama import OllamaVision
            vision = OllamaVision()
            scan_source(
                root, src,
                run_id=run_id, kind="scan", triggered_by="web",
                progress=progress, vision=vision,
            )
        except Exception as e:
            with progress.lock:
                progress.error = str(e)
                progress.finished = True

    th = threading.Thread(target=worker, daemon=True)
    _set_current(progress, th)
    th.start()
    return {"run_id": run_id}


@router.post("/scan/stop")
def scan_stop(request: Request):
    """Graceful stop:当前正在跑的 photo 跑完后退出,run 标 stopped。"""
    if not _running():
        raise HTTPException(409, "当前没有运行中的扫描")
    _current_progress.stop_flag.set()
    return {"ok": True, "run_id": _current_progress.run_id}


@router.post("/scan/resume")
def scan_resume(request: Request):
    """恢复最近一个 stopped/failed 且有 pending 的 run。"""
    if _running():
        raise HTTPException(409, "已经有扫描任务在跑")
    root = _get_root(request)
    conn = _conn(request)
    try:
        if amap_geo.is_quota_exhausted(conn):
            q = amap_geo.quota_status(conn)
            raise HTTPException(
                409,
                f"今日高德配额已用完({q['used']}/{q['limit']}),次日 UTC+8 0:00 重置 ({q['next_reset_at']})",
            )
        resumable = repo.get_resumable_run(conn)
        if not resumable:
            raise HTTPException(404, "没有可恢复的 run(都已完成或没有 pending)")
        run_id = resumable["run_id"]
        source_ids = resumable.get("source_ids") or []
        sources = repo.list_sources(conn)
        target = next((s for s in sources if s["source_id"] in source_ids), None)
        if not target:
            # fallback:用第一个 enabled source(原 source 可能被删了)
            enabled = [s for s in sources if s.get("enabled", 1)]
            if not enabled:
                raise HTTPException(400, "找不到 run 对应的 source,也没有其他可用 source")
            target = enabled[0]
    finally:
        conn.close()

    src = _build_source(target)
    progress = Progress(run_id=run_id)

    def worker():
        try:
            from ..vision.ollama import OllamaVision
            vision = OllamaVision()
            process_pending_for_run(
                root, src, run_id,
                progress=progress, vision=vision,
            )
        except Exception as e:
            with progress.lock:
                progress.error = str(e)
                progress.finished = True

    th = threading.Thread(target=worker, daemon=True)
    _set_current(progress, th)
    th.start()
    return {"run_id": run_id}


@router.get("/status")
def status(request: Request):
    """统一状态:current_run(若在跑)+ resumable_run(若有未完成)+ 全局 counts。"""
    root = _get_root(request)
    conn = _conn(request)
    try:
        stats = repo.job_stats(conn)
        total_photos = repo.count_photos(conn)

        current_run = None
        snap = _current_snapshot()
        if snap and not snap.get("finished"):
            run_id = snap["run_id"]
            run_row = repo.get_run(conn, run_id) or {}
            run_stats = repo.job_stats(conn, run_id=run_id)
            # 优先用 finish_run 时存的 snapshot(resume 后能保留时间游标),实时查兜底
            snap_lo = run_row.get("snapshot_captured_min")
            snap_hi = run_row.get("snapshot_captured_max")
            snap_scanned = run_row.get("snapshot_scanned_up_to")
            live_lo, live_hi = repo.get_run_time_range(conn, run_id)
            live_scanned = repo.get_run_scanned_up_to(conn, run_id)
            time_lo = snap_lo or live_lo
            time_hi = snap_hi or live_hi
            # scanned_up_to 实时跟着新 done 走:max(snapshot, live)
            scanned_up_to = max(filter(None, [snap_scanned, live_scanned]), default=None)
            # 累计 done/failed 用 scan_runs 表(增量维护),processing 实时
            total = run_row.get("total", 0)
            done = run_row.get("done", 0)
            failed = run_row.get("failed", 0)
            processing = run_stats.get("processing", 0)
            pending = max(0, total - done - failed - processing)
            elapsed_seconds, rate, eta_seconds = _compute_rate_eta(
                run_row, is_finished=False, done=done, pending=pending
            )
            current_run = {
                "run_id": run_id,
                "kind": run_row.get("kind"),
                "source_ids": run_row.get("source_ids") or [],
                "status": "running",
                "phase": snap.get("phase") or "processing",
                "total": total, "done": done, "failed": failed,
                "pending": pending, "processing": processing,
                "rate": rate, "eta_seconds": eta_seconds, "elapsed_seconds": elapsed_seconds,
                "time_range": {"min": time_lo, "max": time_hi},
                "scanned_up_to": scanned_up_to,
                "current_path": snap.get("current_path"),
                "current_photo_id": snap.get("current_photo_id"),
                "current_captured_at": snap.get("current_captured_at"),
                "current_thumb_url": (
                    f"/api/thumb/{snap['current_photo_id']}" if snap.get("current_photo_id") else None
                ),
                "started_at": run_row.get("started_at"),
                "stop_requested": snap.get("stop_requested", False),
            }

        resumable_runs = []
        if current_run is None:
            for r in repo.list_resumable_runs(conn, limit=20):
                total = r.get("total", 0) or 0
                done = r.get("done", 0) or 0
                failed = r.get("failed", 0) or 0
                pending = r.get("pending", 0) or 0
                resumable_runs.append({
                    "run_id": r["run_id"],
                    "kind": r.get("kind"),
                    "source_ids": r.get("source_ids") or [],
                    "status": r.get("status"),
                    "total": total,
                    "done": done,
                    "failed": failed,
                    "pending": pending,
                    "started_at": r.get("started_at"),
                    "stopped_at": r.get("finished_at"),
                    "time_range": {
                        "min": r.get("snapshot_captured_min"),
                        "max": r.get("snapshot_captured_max"),
                    },
                    "scanned_up_to": r.get("snapshot_scanned_up_to") or r.get("scanned_up_to"),
                    "note": r.get("note"),
                })
        # 单数字段保留兼容(取最近的)
        resumable = resumable_runs[0] if resumable_runs else None
    finally:
        conn.close()

    # 高德配额状态(每次都查,毫秒级)
    conn_q = _conn(request)
    try:
        amap_quota = amap_geo.quota_status(conn_q)
    finally:
        conn_q.close()

    return {
        "current_run": current_run,
        "resumable_run": resumable,
        "resumable_runs": resumable_runs,
        "amap_quota": amap_quota,
        "global": {
            "photos_total": total_photos,
            "jobs_done":    stats.get("done", 0),
            "jobs_failed":  stats.get("failed", 0),
            "jobs_pending": stats.get("pending", 0),
        },
        # 兼容旧字段(老前端可能在用)
        "progress": _current_snapshot(),
        "job_stats": stats,
        "total_photos": total_photos,
    }


# ---------- runs ----------

@router.get("/runs")
def list_runs_api(request: Request, limit: int = 50):
    conn = _conn(request)
    try:
        return {"runs": repo.list_runs(conn, limit=limit)}
    finally:
        conn.close()


@router.get("/runs/{run_id}")
def get_run_api(request: Request, run_id: str):
    conn = _conn(request)
    try:
        run = repo.get_run(conn, run_id)
        if not run:
            raise HTTPException(404, "run 不存在")

        is_finished = run["status"] in ("stopped", "completed", "failed")

        # time_range / scanned_up_to:优先用 finish_run 时存的快照(避免 reprocess 偷 jobs 后丢失),
        # fallback 用实时 jobs 查询(running 状态用)
        snap_lo = run.get("snapshot_captured_min")
        snap_hi = run.get("snapshot_captured_max")
        snap_scanned = run.get("snapshot_scanned_up_to")
        if snap_lo or snap_hi or snap_scanned:
            time_lo, time_hi, scanned_up_to = snap_lo, snap_hi, snap_scanned
        else:
            time_lo, time_hi = repo.get_run_time_range(conn, run_id)
            scanned_up_to = repo.get_run_scanned_up_to(conn, run_id)

        # stats:finished 用 scan_runs 表的快照(暂停那一刻就冻结),running 用 jobs 实时查
        if is_finished:
            total = run.get("total", 0)
            done = run.get("done", 0)
            failed = run.get("failed", 0)
            pending = max(0, total - done - failed)
            processing = 0
        else:
            run_stats = repo.job_stats(conn, run_id=run_id)
            done = run_stats.get("done", 0)
            failed = run_stats.get("failed", 0)
            pending = run_stats.get("pending", 0)
            processing = run_stats.get("processing", 0)
            total = done + failed + pending + processing

        # elapsed / rate / eta:running 用滑窗实时,finished 用 done/elapsed 平均
        elapsed_seconds, rate, eta_seconds = _compute_rate_eta(run, is_finished, done, pending)

        return {
            **run,
            "stats": {"total": total, "done": done, "failed": failed,
                      "pending": pending, "processing": processing},
            "time_range": {"min": time_lo, "max": time_hi},
            "scanned_up_to": scanned_up_to,
            "elapsed_seconds": elapsed_seconds,
            "rate": rate,
            "eta_seconds": eta_seconds,
        }
    finally:
        conn.close()


def _compute_rate_eta(run: dict, is_finished: bool, done: int, pending: int):
    """统一计算 elapsed/rate/eta。
    - finished:rate = done / 总用时,eta=None
    - running:elapsed=now - started_at;rate 若 _current_progress 有滑窗用滑窗,否则用 done/elapsed 兜底
    """
    from datetime import datetime, timezone
    started = run.get("started_at")
    finished = run.get("finished_at")
    def _parse(ts):
        if not ts: return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None
    start_dt = _parse(started)
    if not start_dt:
        return None, None, None
    end_dt = _parse(finished) if is_finished else datetime.now(timezone.utc)
    if not end_dt:
        return None, None, None
    elapsed = max(0.0, (end_dt - start_dt).total_seconds())

    rate = None
    if is_finished:
        if elapsed > 0 and done > 0:
            rate = round(done / elapsed, 3)
        return int(elapsed), rate, None

    # running:优先用 _current_progress 滑窗(更反映"当下"速率)
    if _current_progress is not None and _current_progress.run_id == run["run_id"]:
        snap = _current_progress.snapshot()
        rate = snap.get("rate")
    # 滑窗没数据(刚启动 < 2 张)时,用 done/elapsed 兜底
    if rate is None and elapsed > 0 and done > 0:
        rate = round(done / elapsed, 3)
    eta_seconds = None
    if rate and rate > 0 and pending > 0:
        eta_seconds = int(pending / rate)
    return int(elapsed), rate, eta_seconds


@router.get("/runs/{run_id}/failures")
def run_failures(request: Request, run_id: str, limit: int = 200):
    conn = _conn(request)
    try:
        rows = repo.recent_failures(conn, run_id=run_id, limit=limit)
    finally:
        conn.close()
    return {"failures": rows}


@router.post("/runs/{run_id}/retry")
def run_retry(request: Request, run_id: str):
    """重置该 run 的 failed → pending(retry_count=0),并启动 Phase B 继续跑。"""
    if _running():
        raise HTTPException(409, "已经有扫描任务在跑,先停止")
    root = _get_root(request)
    conn = _conn(request)
    try:
        run = repo.get_run(conn, run_id)
        if not run:
            raise HTTPException(404, "run 不存在")
        reset_count = repo.reset_failed_jobs(conn, run_id=run_id)
        source_ids = run.get("source_ids") or []
        sources = repo.list_sources(conn)
        target = next((s for s in sources if s["source_id"] in source_ids), None)
        if not target:
            enabled = [s for s in sources if s.get("enabled", 1)]
            if not enabled:
                raise HTTPException(400, "没有可用 source")
            target = enabled[0]
    finally:
        conn.close()

    if reset_count == 0:
        return {"ok": True, "reset_count": 0, "note": "没有 failed 行可重置"}

    src = _build_source(target)
    progress = Progress(run_id=run_id)

    def worker():
        try:
            from ..vision.ollama import OllamaVision
            vision = OllamaVision()
            process_pending_for_run(
                root, src, run_id,
                progress=progress, vision=vision,
            )
        except Exception as e:
            with progress.lock:
                progress.error = str(e)
                progress.finished = True

    th = threading.Thread(target=worker, daemon=True)
    _set_current(progress, th)
    th.start()
    return {"run_id": run_id, "reset_count": reset_count}


@router.post("/runs/{run_id}/resume")
def run_resume_api(request: Request, run_id: str):
    """对特定 run 显式恢复(即使不是最近的)。"""
    if _running():
        raise HTTPException(409, "已经有扫描任务在跑")
    root = _get_root(request)
    conn = _conn(request)
    try:
        if amap_geo.is_quota_exhausted(conn):
            q = amap_geo.quota_status(conn)
            raise HTTPException(
                409,
                f"今日高德配额已用完({q['used']}/{q['limit']}),次日 UTC+8 0:00 重置 ({q['next_reset_at']})",
            )
        run = repo.get_run(conn, run_id)
        if not run:
            raise HTTPException(404, "run 不存在")
        source_ids = run.get("source_ids") or []
        sources = repo.list_sources(conn)
        target = next((s for s in sources if s["source_id"] in source_ids), None)
        if not target:
            enabled = [s for s in sources if s.get("enabled", 1)]
            if not enabled:
                raise HTTPException(400, "没有可用 source")
            target = enabled[0]
    finally:
        conn.close()

    src = _build_source(target)
    progress = Progress(run_id=run_id)

    def worker():
        try:
            from ..vision.ollama import OllamaVision
            vision = OllamaVision()
            process_pending_for_run(
                root, src, run_id,
                progress=progress, vision=vision,
            )
        except Exception as e:
            with progress.lock:
                progress.error = str(e)
                progress.finished = True

    th = threading.Thread(target=worker, daemon=True)
    _set_current(progress, th)
    th.start()
    return {"run_id": run_id}


# ---------- reprocess(批量重跑) ----------

def _normalize_selector(body: dict) -> dict:
    sel = body.get("selector") or {}
    out: dict = {}
    if sel.get("source_ids"):
        out["source_ids"] = list(sel["source_ids"])
    if sel.get("person_ids"):
        out["person_ids"] = list(sel["person_ids"])
    for k in ("time_from", "time_to", "missing_field", "person_count", "fts_query"):
        if sel.get(k):
            out[k] = sel[k]
    return out


@router.post("/reprocess/preview")
def reprocess_preview(request: Request, body: dict = Body(...)):
    """预览匹配数 + 20 张抽样(photo_id/path/captured_at + thumb)。"""
    sel = _normalize_selector(body)
    from ..scanner.reprocess import select_photos
    conn = _conn(request)
    try:
        ids = select_photos(conn, **sel)
        sample_ids = ids[:20]
        rows = []
        if sample_ids:
            placeholders = ",".join("?" * len(sample_ids))
            cursor = conn.execute(
                f"""
                SELECT photo_id, original_path,
                       json_extract(exif,'$.captured_at_local') AS captured_at
                FROM photos WHERE photo_id IN ({placeholders})
                """,
                sample_ids,
            )
            rows = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()
    return {
        "count": len(ids),
        "sample": [
            {**r, "thumb_url": f"/api/thumb/{r['photo_id']}"}
            for r in rows
        ],
    }


@router.post("/reprocess")
def reprocess_run_api(request: Request, body: dict = Body(...)):
    """根据选择器 + stage 创建 reprocess run 并启动 Phase B。

    body: { selector: {...}, stage: 'vision'|'derived'|'faces', note?: str }
    """
    if _running():
        raise HTTPException(409, "已经有任务在跑")

    stage = body.get("stage")
    if stage not in ("vision", "derived", "faces"):
        raise HTTPException(400, "stage 必须是 vision / derived / faces")
    note = body.get("note")
    sel = _normalize_selector(body)

    root = _get_root(request)
    from ..scanner.reprocess import select_photos

    conn = _conn(request)
    try:
        ids = select_photos(conn, **sel)
        if not ids:
            raise HTTPException(400, "选择器没匹配到任何 photo")

        run_id = generate_run_id()
        repo.create_run(
            conn, run_id, kind=f"reprocess-{stage}", triggered_by="web",
            source_ids=sel.get("source_ids"), selector=sel, note=note,
        )
    finally:
        conn.close()

    # 三种 stage 跑法不同
    if stage == "vision":
        # 重跑 vision:走 reprocess_vision_for(直接对 photo_ids 跑两次 LLM,不经 jobs 表)
        # 把 jobs 也挂上 run_id 便于看进度
        conn2 = _conn(request)
        try:
            repo.reset_jobs_for_reprocess(conn2, ids, run_id)
            repo.update_run_counts(conn2, run_id, total=len(ids), done=0, failed=0)
        finally:
            conn2.close()

        progress = Progress(run_id=run_id)

        def worker_vision():
            from ..scanner.reprocess import reprocess_vision_for
            try:
                # 分批跑,每张完成后单独更 jobs
                done_count = 0
                failed_count = 0
                for pid in ids:
                    if progress.stop_flag.is_set():
                        break
                    with progress.lock:
                        progress.current_photo_id = pid
                    try:
                        result = reprocess_vision_for(root, [pid])
                        conn3 = db.connect(db.get_db_path(root))
                        db.init_schema(conn3)
                        try:
                            if result.get("done", 0) > 0:
                                repo.finish_job(conn3, pid)
                                done_count += 1
                                with progress.lock:
                                    import time as _t
                                    progress.recent_done_timestamps.append(_t.monotonic())
                            else:
                                repo.fail_job(conn3, pid, "reprocess_vision_for: done=0")
                                failed_count += 1
                            repo.update_run_counts(conn3, run_id, total=len(ids),
                                                   done=done_count, failed=failed_count)
                        finally:
                            conn3.close()
                    except Exception as e:
                        conn3 = db.connect(db.get_db_path(root))
                        db.init_schema(conn3)
                        try:
                            repo.fail_job(conn3, pid, f"{type(e).__name__}: {e}")
                            failed_count += 1
                        finally:
                            conn3.close()
                conn4 = db.connect(db.get_db_path(root))
                db.init_schema(conn4)
                try:
                    final = "stopped" if progress.stop_flag.is_set() else "completed"
                    repo.finish_run(conn4, run_id, status=final)
                finally:
                    conn4.close()
            finally:
                with progress.lock:
                    progress.finished = True

        th = threading.Thread(target=worker_vision, daemon=True)
        _set_current(progress, th)
        th.start()
        return {"run_id": run_id, "count": len(ids)}

    elif stage == "derived":
        # 重跑 derived(全库),不走 jobs 状态机(秒级到分钟级)
        progress = Progress(run_id=run_id)

        def worker_derived():
            from ..scanner.reprocess import reprocess_derived
            try:
                reprocess_derived(root)
            finally:
                conn5 = db.connect(db.get_db_path(root))
                db.init_schema(conn5)
                try:
                    repo.finish_run(conn5, run_id, status="completed")
                    repo.update_run_counts(conn5, run_id, total=len(ids), done=len(ids), failed=0)
                finally:
                    conn5.close()
                with progress.lock:
                    progress.finished = True

        th = threading.Thread(target=worker_derived, daemon=True)
        _set_current(progress, th)
        th.start()
        return {"run_id": run_id, "count": len(ids)}

    elif stage == "faces":
        # faces 重跑用 rematch_faces(quick),不走 jobs
        progress = Progress(run_id=run_id)

        def worker_faces():
            from ..scanner.reprocess import rematch_faces
            try:
                rematch_faces(root)
            finally:
                conn6 = db.connect(db.get_db_path(root))
                db.init_schema(conn6)
                try:
                    repo.finish_run(conn6, run_id, status="completed")
                    repo.update_run_counts(conn6, run_id, total=len(ids), done=len(ids), failed=0)
                finally:
                    conn6.close()
                with progress.lock:
                    progress.finished = True

        th = threading.Thread(target=worker_faces, daemon=True)
        _set_current(progress, th)
        th.start()
        return {"run_id": run_id, "count": len(ids)}


# ---------- photos ----------

@router.get("/photos")
def photos(
    request: Request,
    page: int = 0,
    page_size: int = 60,
    order_by: str = "captured",   # 'captured'(默认,拍照时间倒序) | 'imported'(写入 db 时间倒序)
    favorite_only: bool = False,  # True → 只列 Apple 收藏
):
    conn = _conn(request)
    try:
        items = repo.list_photos(
            conn, limit=page_size, offset=page * page_size, order_by=order_by,
            favorite_only=favorite_only,
        )
        name_map = repo.cluster_name_map(conn)
        total = repo.count_photos(conn, favorite_only=favorite_only)
    finally:
        conn.close()
    items = [repo.resolve_persons(r, name_map) for r in items]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/photo/{photo_id}")
def photo(request: Request, photo_id: str):
    conn = _conn(request)
    try:
        rec = repo.get_photo(conn, photo_id)
        name_map = repo.cluster_name_map(conn)
    finally:
        conn.close()
    if not rec:
        raise HTTPException(404, "未找到该 photo")
    rec = repo.resolve_persons(rec, name_map)
    # 把 meta.errors 里未 acknowledge 的 vision_role_mismatch 抽到顶层方便前端展示
    errors = (rec.get("meta") or {}).get("errors") or []
    mismatches = [e.get("error") for e in errors
                  if isinstance(e, dict)
                  and e.get("group") == "vision_role_mismatch"
                  and not e.get("acknowledged")]
    rec["role_mismatches"] = mismatches
    return rec


@router.post("/photo/{photo_id}/mismatches/acknowledge")
def acknowledge_mismatches(request: Request, photo_id: str):
    """用户人肉核对后,标记本张照片的所有 vision_role_mismatch 为已确认。
    重跑 vision 时这些标记会清除(因为 LLM 输出变了需重新评估)。"""
    import json as _json
    conn = _conn(request)
    try:
        row = conn.execute("SELECT meta FROM photos WHERE photo_id=?", (photo_id,)).fetchone()
        if not row:
            raise HTTPException(404, "未找到该 photo")
        meta = _json.loads(row["meta"]) if row["meta"] else {}
        errors = meta.get("errors") or []
        n = 0
        for e in errors:
            if isinstance(e, dict) and e.get("group") == "vision_role_mismatch" and not e.get("acknowledged"):
                e["acknowledged"] = True
                n += 1
        meta["errors"] = errors
        conn.execute(
            "UPDATE photos SET meta = ?, updated_at = ? WHERE photo_id = ?",
            (_json.dumps(meta, ensure_ascii=False), repo.now_iso(), photo_id),
        )
        return {"acknowledged_count": n}
    finally:
        conn.close()


@router.get("/thumb/{photo_id}")
def thumb(request: Request, photo_id: str):
    from ..preprocess.cache import cache_path
    p = cache_path(_get_root(request), photo_id)
    if not p.exists():
        raise HTTPException(404, "缩略图缓存不存在")
    return FileResponse(p, media_type="image/jpeg")


# ---------- seed persons(种子人物) ----------

@router.get("/persons")
def list_persons(request: Request):
    """所有已命名人物(种子人物 + 已命名的扫描 cluster)。"""
    from ..faces.seeds import list_named_persons
    conn = _conn(request)
    try:
        return {"persons": list_named_persons(conn)}
    finally:
        conn.close()


# 旧路径保留兼容
@router.get("/seed-persons")
def list_seeds_compat(request: Request):
    return list_persons(request)


@router.post("/seed-persons")
async def create_or_extend_seed(
    request: Request,
    name: str = Form(...),
    files: list[UploadFile] = File(...),
):
    """新建种子人物 或 给已有种子人物追加照片。
    重复 name 的会自动追加到同一个 cluster_id。
    """
    from ..faces.seeds import find_or_create_person_by_name, add_seeds
    import tempfile

    name = name.strip()
    if not name:
        raise HTTPException(400, "name 不能为空")
    if not files:
        raise HTTPException(400, "至少要上传 1 张种子照片")

    root = _get_root(request)
    # 保存上传的 tmp 文件
    tmp_paths: list[Path] = []
    try:
        for f in files:
            suffix = Path(f.filename or "seed.jpg").suffix or ".jpg"
            tmp = Path(tempfile.NamedTemporaryFile(delete=False, suffix=suffix).name)
            tmp.write_bytes(await f.read())
            tmp_paths.append(tmp)

        conn = _conn(request)
        try:
            cluster_id = find_or_create_person_by_name(conn, name)
            success, warnings = add_seeds(conn, root, cluster_id, tmp_paths)
        finally:
            conn.close()
    finally:
        for t in tmp_paths:
            try: t.unlink()
            except: pass

    # 自动触发 quick rematch — 让已扫照片的 cluster 立刻重新归类
    rematch_result = None
    if success > 0:
        try:
            from ..scanner.reprocess import rematch_faces
            rematch_result = rematch_faces(root)
        except Exception as e:
            warnings.append(f"自动 rematch 失败: {type(e).__name__}: {e}")

    return {
        "cluster_id": cluster_id,
        "name": name,
        "added": success,
        "warnings": warnings,
        "rematch": rematch_result,
    }


@router.delete("/seed-persons/{cluster_id}")
def delete_seed_person_api(request: Request, cluster_id: str):
    from ..faces.seeds import delete_seed_person
    conn = _conn(request)
    try:
        delete_seed_person(conn, _get_root(request), cluster_id)
        return {"ok": True}
    finally:
        conn.close()


# ---------- faces ----------

@router.get("/face-clusters")
def face_clusters(request: Request):
    """匿名 cluster(扫描时自动产生的,还没命名的)。已命名的种子人物走 /api/seed-persons。"""
    conn = _conn(request)
    try:
        all_clusters = repo.list_face_clusters(conn)
        # 过滤掉已命名的(它们在种子人物视图里)
        unnamed = [c for c in all_clusters if not c.get("person_name")]
        return {"clusters": unnamed}
    finally:
        conn.close()


@router.get("/persons/{cluster_id}/photos")
def person_photos(request: Request, cluster_id: str):
    """返回这个 person 出现的所有主库 photo_id(排除种子图)。"""
    conn = _conn(request)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT f.photo_id FROM faces f
            JOIN photos p ON p.photo_id = f.photo_id
            WHERE f.cluster_id = ? AND p.source != 'seed'
            """,
            (cluster_id,),
        ).fetchall()
        return {"photo_ids": [r[0] for r in rows]}
    finally:
        conn.close()


@router.post("/persons/{cluster_id}/name")
def name_person(request: Request, cluster_id: str, body: dict = Body(...)):
    """body: { name: str | null } — null 取消命名"""
    name = body.get("name")
    if name is not None:
        name = str(name).strip() or None
    conn = _conn(request)
    try:
        repo.set_person_name(conn, cluster_id, name)
        return {"ok": True, "cluster_id": cluster_id, "name": name}
    finally:
        conn.close()


@router.get("/photo/{photo_id}/faces")
def photo_faces(request: Request, photo_id: str):
    conn = _conn(request)
    try:
        return {"faces": repo.list_faces_for_photo(conn, photo_id)}
    finally:
        conn.close()


@router.get("/face/{face_id}/crop")
def face_crop(request: Request, face_id: str):
    """根据 face_id 找到 photo + bbox,从预处理 JPEG 裁出脸 + 上下文。

    返回 200x200 的 JPEG;扩展 bbox 30% 给点上下文。
    """
    import io
    import json as _json
    from PIL import Image
    from ..preprocess.cache import cache_path

    conn = _conn(request)
    try:
        row = conn.execute(
            "SELECT photo_id, bbox FROM faces WHERE face_id = ?",
            (face_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "face 不存在")
    photo_id = row["photo_id"]
    bbox = _json.loads(row["bbox"])  # [x, y, w, h]
    src = cache_path(_get_root(request), photo_id)
    if not src.exists():
        raise HTTPException(404, "预处理图不存在")

    im = Image.open(src).convert("RGB")
    x, y, w, h = bbox
    pad = 0.3
    cx, cy = x + w / 2, y + h / 2
    size = max(w, h) * (1 + pad)
    left   = max(0, int(cx - size / 2))
    top    = max(0, int(cy - size / 2))
    right  = min(im.width,  int(cx + size / 2))
    bottom = min(im.height, int(cy + size / 2))
    crop = im.crop((left, top, right, bottom))
    # 输出固定尺寸,GUI 网格整齐
    crop.thumbnail((240, 240), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=85)
    buf.seek(0)
    from fastapi.responses import Response
    return Response(content=buf.read(), media_type="image/jpeg")


@router.post("/reprocess")
def reprocess_endpoint(request: Request, body: dict = Body(...)):
    """body 形式:
       - { group: 'faces', mode?: 'quick'|'full' }:重跑 face(quick=秒级 rematch,full=重跑 detect)
       - { group: 'vision', photo_ids: [...] }:对指定照片重跑 vision(每张 ~20s)
    """
    group = body.get("group", "faces")
    root = _get_root(request)
    if group == "faces":
        mode = body.get("mode", "quick")
        if mode == "quick":
            from ..scanner.reprocess import rematch_faces
            return rematch_faces(root)
        elif mode == "full":
            from ..scanner.reprocess import reprocess_faces
            return reprocess_faces(root)
        else:
            raise HTTPException(400, f"未知 mode: {mode}")
    elif group == "vision":
        photo_ids = body.get("photo_ids") or []
        if not isinstance(photo_ids, list) or not photo_ids:
            raise HTTPException(400, "vision reprocess 需要非空 photo_ids 数组")
        from ..scanner.reprocess import reprocess_vision_for
        return reprocess_vision_for(root, photo_ids)
    elif group == "derived":
        from ..scanner.reprocess import reprocess_derived
        return reprocess_derived(root)
    else:
        raise HTTPException(400, f"未知 group: {group}")


_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".heic": "image/heic", ".heif": "image/heif",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}


@router.get("/original/{photo_id}")
def original(request: Request, photo_id: str, download: int = 0):
    """返回原图字节(`identity.original_path` 指向的真原图)。

    download=0(默认):无 attachment 头,浏览器自决 — JPEG 内联显示;
                       HEIC 浏览器不识别会触发下载或交给系统 app(Safari 可能转 JPEG 渲染)。
    download=1:加 Content-Disposition: attachment + 文件名,强制下载对话框。

    两条路径文件源相同(`identity.original_path`),区别只在 HTTP 头。

    显式 media_type:Python mimetypes 默认不认 HEIC(返 text/plain),浏览器会按文本处理。
    用扩展名查表补一份(JPEG/PNG/HEIC/WEBP/GIF/TIFF),不识别的回退默认 application/octet-stream。
    """
    conn = _conn(request)
    try:
        rec = repo.get_photo(conn, photo_id)
    finally:
        conn.close()
    if not rec:
        raise HTTPException(404, "未找到该 photo")
    op = Path(rec["identity"]["original_path"])
    if not op.exists():
        raise HTTPException(404, "原图文件已不存在(可能 Apple 照片仅在 iCloud 未下载到本地)")
    media_type = _EXT_MIME.get(op.suffix.lower(), "application/octet-stream")
    if download:
        return FileResponse(
            op,
            media_type=media_type,
            filename=op.name,
            headers={"Content-Disposition": f'attachment; filename="{op.name}"'},
        )
    return FileResponse(op, media_type=media_type)


# ============================================================
# Setup / Config endpoints(Phase 5 — 给 Web 配置页用)
# ============================================================

def _count_named_persons(conn) -> int:
    """已命名 cluster 数量(persons.name 非空)。"""
    return conn.execute(
        "SELECT COUNT(*) FROM persons WHERE name IS NOT NULL AND name != ''"
    ).fetchone()[0]


def _count_sources(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]


@router.get("/setup/status")
def setup_status(request: Request):
    """聚合就绪检查 — 给配置页 + 顶部 banner 用,一次返所有维度。

    返回:
      {
        "ollama":   { ok, error?, has_vision_model?, models?, vision_model_name },
        "amap":     { configured: bool, quota?: {used, limit, ...} },
        "llm":      { configured: bool, default?: id, count: int },
        "sources":  { count: int },
        "persons":  { named_count: int },
        "photos":   { count: int },
        "is_first_run": bool,        # 啥都没配 + db 空 → 新手默认看到"配置"页
        "next_step":    "configure" | "scan" | "browse",
      }
    """
    from ..vision import ollama_probe
    from ..geocode import amap as amap_geo
    from . import llm as llm_mod

    conn = _conn(request)
    try:
        sources_n = _count_sources(conn)
        persons_n = _count_named_persons(conn)
        photos_n = repo.count_photos(conn)
        amap_quota = amap_geo.quota_status(conn)
    finally:
        conn.close()

    amap_key = bool(amap_geo.get_amap_key())
    llm_pub = llm_mod.list_providers_public()
    llm_count = len(llm_pub.get("providers") or [])

    ollama_status = ollama_probe.ping()

    ready_all = (
        ollama_status.get("ok")
        and ollama_status.get("has_vision_model")
        and amap_key
        and llm_count > 0
    )
    is_first_run = (sources_n == 0) and (photos_n == 0)

    # next_step 给前端启动时自动激活合适 tab 用
    if not ready_all or sources_n == 0:
        next_step = "configure"
    elif photos_n == 0:
        next_step = "scan"
    else:
        next_step = "browse"

    return {
        "ollama": ollama_status,
        "amap": {
            "configured": amap_key,
            "quota": amap_quota,
        },
        "llm": {
            "configured": llm_count > 0,
            "default": llm_pub.get("default"),
            "count": llm_count,
        },
        "sources": {"count": sources_n},
        "persons": {"named_count": persons_n},
        "photos": {"count": photos_n},
        "is_first_run": is_first_run,
        "next_step": next_step,
    }


@router.get("/ollama/ping")
def ollama_ping(endpoint: Optional[str] = None):
    """探活 Ollama 服务。可选传 endpoint 覆盖默认(用于试新的 endpoint 还没存)。"""
    from ..vision import ollama_probe
    return ollama_probe.ping(endpoint)


@router.post("/config/vision")
def config_set_vision(body: dict = Body(...)):
    """写本地视觉模型配置(endpoint + model)到 ~/.life_lens/config.json。"""
    from ..store import config as cfg_store
    endpoint = (body or {}).get("endpoint", "").strip()
    model = (body or {}).get("model", "").strip()
    if not endpoint:
        raise HTTPException(400, "endpoint 不能空")
    if not model:
        raise HTTPException(400, "model 不能空")
    # 接受 host 根 或 完整 /api/generate URL — 都规范化成 host 根
    if endpoint.endswith("/api/generate"):
        endpoint = endpoint[: -len("/api/generate")]
    elif endpoint.endswith("/api/tags"):
        endpoint = endpoint[: -len("/api/tags")]
    cfg_store.update_vision_config(endpoint, model)
    return {"ok": True}


def _guess_lan_ip():
    """探测本机局域网 IP(给配置页显示手机访问 URL)。

    UDP connect 不真发包,只查路由表选源地址;失败(无网络)返 None。
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


@router.get("/config/lan-chat")
def config_get_lan_chat(request: Request):
    """内网「问相册」开关状态 + 手机访问 URL。"""
    from ..store import config as cfg_store
    ip = _guess_lan_ip()
    port = request.url.port or 7878
    return {
        "enabled": cfg_store.lan_chat_enabled(),
        "lan_url": f"http://{ip}:{port}" if ip else None,
    }


@router.post("/config/lan-chat")
def config_set_lan_chat(body: dict = Body(...)):
    """写内网「问相册」开关。gate 每次远程请求热读,即时生效不用重启。

    本端点不在 LAN 白名单 — 内网设备不能给自己开门。
    """
    from ..store import config as cfg_store
    enabled = bool((body or {}).get("enabled"))
    cfg_store.update_lan_chat(enabled)
    return {"ok": True, "enabled": enabled}


@router.get("/config/chat-notes")
def config_get_chat_notes():
    """「问相册」背景知识(用户写给 LLM 的小名/人物关系/常用地点等)。"""
    from ..store import config as cfg_store
    return {"notes": cfg_store.chat_user_notes()}


@router.post("/config/chat-notes")
def config_set_chat_notes(body: dict = Body(...)):
    """写「问相册」背景知识。chat.py 每次提问热读,保存即生效。

    本端点不在 LAN 白名单 — 内网设备改不了(背景知识属于配置,仅本机)。
    长度上限 4000 字:这段文字每次提问都进 prompt,防止误贴长文吃爆 token。
    """
    from ..store import config as cfg_store
    notes = str((body or {}).get("notes") or "")
    if len(notes) > 4000:
        raise HTTPException(400, f"背景知识太长({len(notes)} 字 > 4000 上限),请精简")
    cfg_store.update_chat_user_notes(notes)
    return {"ok": True, "notes": cfg_store.chat_user_notes()}


@router.post("/config/amap-key")
def config_set_amap_key(body: dict = Body(...)):
    """写 amap_key 到 ~/.life_lens/config.json。"""
    from ..store import config as cfg_store
    key = (body or {}).get("key", "").strip()
    if not key:
        raise HTTPException(400, "key 不能为空(传 {key: '...'})")
    cfg_store.update_amap_key(key)
    return {"ok": True}


@router.post("/config/amap-key/validate")
def config_validate_amap_key(body: dict = Body(...)):
    """用给的 key 跑一次测试调用,验证有效。不写 config。

    用一个固定坐标(北京天安门 39.9087, 116.3975)调高德 reverse geocode 端点。
    只验 HTTP 200 + 业务 status=1。会消耗用户 1 次免费配额(无 caching,因为 key 还没写库)。
    """
    key = (body or {}).get("key", "").strip()
    if not key:
        raise HTTPException(400, "key 不能为空")
    try:
        import requests
        from ..geocode.amap import wgs84_to_gcj02
        gcj_lat, gcj_lng = wgs84_to_gcj02(39.9087, 116.3975)
        r = requests.get(
            "https://restapi.amap.com/v3/geocode/regeo",
            params={
                "key": key,
                "location": f"{gcj_lng:.6f},{gcj_lat:.6f}",
                "extensions": "base",
                "output": "json",
            },
            timeout=8.0,
        )
        r.raise_for_status()
        data = r.json()
        # 高德业务返回:status="1" 成功;"0" + info/infocode 错
        if str(data.get("status")) == "1":
            addr = (data.get("regeocode") or {}).get("formatted_address") or "(成功但地址空)"
            return {"ok": True, "sample_address": addr}
        else:
            return {
                "ok": False,
                "error": f'高德 status={data.get("status")} info={data.get("info")} infocode={data.get("infocode")}',
            }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/config/llm-provider")
def config_set_llm_provider(body: dict = Body(...)):
    """加 / 覆盖一个 LLM provider。

    body: {
      "op": "upsert" | "delete",
      "provider_id": "deepseek",
      "config": { kind, model, api_key, base_url, label }   # op=upsert 必填
    }
    """
    from ..store import config as cfg_store
    op = (body or {}).get("op", "upsert")
    pid = (body or {}).get("provider_id", "").strip()
    if not pid:
        raise HTTPException(400, "provider_id 不能为空")
    if op == "delete":
        cfg_store.remove_llm_provider(pid)
        return {"ok": True, "op": "delete"}
    cfg = (body or {}).get("config") or {}
    # 强制 kind=openai-compat(v0.4 起只支持这个)
    if cfg.get("kind") and cfg["kind"] != "openai-compat":
        raise HTTPException(400, f"kind 必须是 'openai-compat'(v0.4 起);拿到 {cfg.get('kind')!r}")
    cfg["kind"] = "openai-compat"
    # 编辑场景:api_key 留空时保留旧 key(用户不愿重输)
    if not cfg.get("api_key"):
        existing = (cfg_store.load_config().get("llm") or {}).get("providers") or {}
        old = existing.get(pid) or {}
        if old.get("api_key"):
            cfg["api_key"] = old["api_key"]
    # 基本必填校验
    for f in ("model", "api_key", "base_url"):
        if not cfg.get(f):
            raise HTTPException(400, f"config.{f} 不能为空")
    cfg_store.update_llm_provider(pid, cfg)
    return {"ok": True, "op": "upsert"}


@router.post("/config/llm-default")
def config_set_llm_default(body: dict = Body(...)):
    """切默认 LLM provider。"""
    from ..store import config as cfg_store
    pid = (body or {}).get("provider_id", "").strip()
    if not pid:
        raise HTTPException(400, "provider_id 不能为空")
    try:
        cfg_store.set_llm_default(pid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


# ---------- 语义索引(embeddings)----------
#
# 主扫描 + reprocess vision 已自动 inline 写入,绝大多数场景不需要手动重建。
# 这两个端点是兜底:
#   - 用户 fastembed 装失败,扫描期跳过了 → 装好后点"重建"补全
#   - 换 embedding 模型 / 改 build_source_text 拼装规则 → force=true 全量重 embed
#   - 历史数据迁移(老 db 没 inline,扫的存量照片没向量)

_embedding_rebuild_state: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "failed": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "force": False,
}
_embedding_rebuild_lock = threading.Lock()


def _embedding_rebuild_worker(root: Path, force: bool):
    """在后台线程里跑全量 / 增量 embedding 回填。"""
    from datetime import datetime, timezone
    from ..embed import get_embedder, build_source_text, text_hash

    state = _embedding_rebuild_state
    try:
        embedder = get_embedder()
    except Exception as e:
        with _embedding_rebuild_lock:
            state["error"] = f"加载 embedder 失败: {type(e).__name__}: {e}"
            state["running"] = False
            state["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return

    conn = db.connect(db.get_db_path(root))
    try:
        rows = conn.execute(
            "SELECT photo_id, vision, people FROM photos "
            "WHERE source != 'seed' AND vision IS NOT NULL"
        ).fetchall()
        existing = dict(conn.execute("SELECT photo_id, text_hash FROM photo_embeddings").fetchall())

        todo: list[tuple[str, str, str]] = []
        for r in rows:
            try:
                vision = json.loads(r["vision"]) if r["vision"] else None
                people = json.loads(r["people"]) if r["people"] else None
            except Exception:
                continue
            text = build_source_text(vision, people)
            if not text:
                continue
            h = text_hash(text)
            if not force and existing.get(r["photo_id"]) == h:
                continue
            todo.append((r["photo_id"], text, h))

        with _embedding_rebuild_lock:
            state["total"] = len(todo)
            state["done"] = 0
            state["failed"] = 0

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        batch = 32
        for i in range(0, len(todo), batch):
            chunk = todo[i : i + batch]
            try:
                vecs = embedder.embed([t for _, t, _ in chunk])
            except Exception:
                with _embedding_rebuild_lock:
                    state["failed"] += len(chunk)
                continue
            rows_to_write = [
                (pid, embedder.model, embedder.dim, vec.tobytes(), h, now)
                for (pid, _, h), vec in zip(chunk, vecs)
            ]
            conn.executemany(
                """
                INSERT INTO photo_embeddings (photo_id, model, dim, vec, text_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(photo_id) DO UPDATE SET
                    model=excluded.model, dim=excluded.dim, vec=excluded.vec,
                    text_hash=excluded.text_hash, updated_at=excluded.updated_at
                """,
                rows_to_write,
            )
            with _embedding_rebuild_lock:
                state["done"] += len(chunk)
    finally:
        conn.close()
        with _embedding_rebuild_lock:
            state["running"] = False
            state["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


@router.get("/embeddings/status")
def embeddings_status(request: Request):
    """返语义索引覆盖率 + 当前重建进度(若有)。"""
    conn = _conn(request)
    try:
        total_vision = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE source != 'seed' AND vision IS NOT NULL"
        ).fetchone()[0]
        total_indexed = conn.execute("SELECT COUNT(*) FROM photo_embeddings").fetchone()[0]
        last_built_at = conn.execute(
            "SELECT MAX(updated_at) FROM photo_embeddings"
        ).fetchone()[0]
        model_row = conn.execute(
            "SELECT model FROM photo_embeddings LIMIT 1"
        ).fetchone()
        model = model_row[0] if model_row else None
    finally:
        conn.close()
    with _embedding_rebuild_lock:
        rebuild = dict(_embedding_rebuild_state)
    return {
        "total_with_vision": total_vision,
        "total_indexed": total_indexed,
        "missing": max(0, total_vision - total_indexed),
        "last_built_at": last_built_at,
        "model": model,
        "rebuild": rebuild,
    }


@router.post("/embeddings/rebuild")
def embeddings_rebuild(request: Request, body: dict = Body(default={})):
    """启动后台线程回填 / 重建 photo_embeddings。

    body:
      - force (bool, 默认 false):true 时忽略 text_hash 全量重 embed(换模型 / 改 source_text 用)
    """
    from datetime import datetime, timezone

    force = bool((body or {}).get("force"))
    with _embedding_rebuild_lock:
        if _embedding_rebuild_state["running"]:
            raise HTTPException(409, "已有 embedding 重建任务在跑")
        _embedding_rebuild_state.update({
            "running": True,
            "total": 0,
            "done": 0,
            "failed": 0,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "finished_at": None,
            "error": None,
            "force": force,
        })

    root = _get_root(request)
    t = threading.Thread(
        target=_embedding_rebuild_worker, args=(root, force), daemon=True
    )
    t.start()
    return {"ok": True, "force": force}
