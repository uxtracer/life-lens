"""DB 驱动的扫描调度器(v2)。

两阶段:
  Phase A(入队):流式遍历 source.iter_photos(),计算 photo_id + content_hash,
                  判重(photos.content_hash + jobs.status='done' 双重 + peek EXIF 时间),
                  enqueue_job 挂 run_id + captured_at_local。

  Phase B(处理):单线程主循环 pull get_pending(ORDER BY captured_at_local ASC)
                  → pipeline.process_one → upsert_photo + update_fts + finish_job。
                  失败走 fail_job(指数退避内置:retry_count<3 重置 pending,>=3 永久 failed)。
                  stop_flag 触发:跑完当前一张就退出,run 标 stopped。

为什么单线程:vision 在 pipeline 内部已经 vision_lock 串行(Ollama 单实例不能并行),
4 workers 实测增益 ~5%。换单线程拿到 db 写一致性 + graceful shutdown 简单 + 不丢进度。
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from ..exif import extract as exif_extract
from ..geocode import amap as amap_geo
from ..sources.base import PhotoSource, PhotoRef
from ..store import db, repo
from ..vision.base import VisionModel
from . import identity as ident
from . import pipeline

log = logging.getLogger(__name__)


def generate_run_id() -> str:
    return f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"


# 进程级 embedder 缓存。首次扫描 ~3s 加载,后续 run 复用。
# fastembed 没装 / 加载失败 → 标 failed,后续跳过(不重试),用户可在 Web "重建语义索引" 兜底。
_embedder_state: dict = {"embedder": None, "failed": False}


def _get_embedder_for_scan():
    if _embedder_state["embedder"] is not None:
        return _embedder_state["embedder"]
    if _embedder_state["failed"]:
        return None
    try:
        from ..embed import get_embedder
        _embedder_state["embedder"] = get_embedder()
        log.info("语义 embedder 已加载: %s", _embedder_state["embedder"].model)
        return _embedder_state["embedder"]
    except Exception as e:
        log.warning(
            "加载 embedder 失败(扫描将跳过语义索引,可在 Web 端用'重建语义索引'兜底): %s", e
        )
        _embedder_state["failed"] = True
        return None


@dataclass
class Progress:
    """轻量进度对象。耐久状态全在 db(jobs + scan_runs),这里只缓存 in-flight 信息 + 滑窗速率。

    phase: 'pending' → 'enqueueing'(Phase A 遍历图片库)→ 'processing'(Phase B 单张处理)。
    Phase A 数量未知所以没有进度条,前端只显示"正在遍历图片库..."文字。
    """
    run_id: str = ""
    phase: str = "pending"
    stop_flag: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    current_path: str = ""
    current_photo_id: str = ""
    current_captured_at: str = ""
    recent_done_timestamps: deque = field(default_factory=lambda: deque(maxlen=120))
    error: Optional[str] = None
    finished: bool = False

    def snapshot(self) -> dict:
        with self.lock:
            window = list(self.recent_done_timestamps)
            rate = None
            if len(window) >= 2:
                elapsed = window[-1] - window[0]
                if elapsed > 0:
                    rate = round(len(window) / elapsed, 3)
            return {
                "run_id": self.run_id,
                "phase": self.phase,
                "current_path": self.current_path,
                "current_photo_id": self.current_photo_id,
                "current_captured_at": self.current_captured_at,
                "rate": rate,
                "error": self.error,
                "finished": self.finished,
                "stop_requested": self.stop_flag.is_set(),
            }


def scan_source(
    root: Path,
    source: PhotoSource,
    *,
    run_id: Optional[str] = None,
    kind: str = "scan",
    triggered_by: str = "cli",
    progress: Optional[Progress] = None,
    on_each: Optional[Callable[[dict], None]] = None,
    vision: Optional[VisionModel] = None,
    limit: Optional[int] = None,
    note: Optional[str] = None,
    workers: int = 1,   # 兼容旧调用,但实际单线程跑
) -> Progress:
    """两阶段扫描入口。同步阻塞,通过 progress.stop_flag 中断。"""
    progress = progress or Progress()
    if not run_id:
        run_id = generate_run_id()
    progress.run_id = run_id

    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    repo.reset_stuck_jobs(conn)
    repo.create_run(
        conn, run_id, kind=kind, triggered_by=triggered_by,
        source_ids=[getattr(source, "source_id", "")],
        note=note,
    )

    final_status = "completed"
    try:
        with progress.lock:
            progress.phase = "enqueueing"
        ref_cache, new_count, skip_count = _enqueue_phase(conn, source, run_id, limit=limit)
        log.info("Phase A 入队 %d / 跳过已 done %d  run=%s", new_count, skip_count, run_id)
        # scan run 的 total = 本次入队的(skip 的不算,它们属于别的 run);
        # _process_phase 内每张完成时 inc done/failed 增量更新 scan_runs
        repo.set_run_total(conn, run_id, new_count)
        # 一次性冻结 time_range(jobs 完整时),后续 reprocess 偷 jobs 不会影响
        live_lo, live_hi = repo.get_run_time_range(conn, run_id)
        repo.set_run_time_range(conn, run_id, live_lo, live_hi)

        with progress.lock:
            progress.phase = "processing"
        _process_phase(conn, root, source, ref_cache, vision, run_id, progress, on_each)

        if progress.stop_flag.is_set():
            final_status = "stopped"
        else:
            remaining = repo.get_pending(conn, limit=1, run_id=run_id)
            final_status = "stopped" if remaining else "completed"
    except Exception as e:
        log.exception("scan_source 失败")
        progress.error = str(e)
        final_status = "failed"
    finally:
        try:
            repo.finish_run(conn, run_id, status=final_status)
            if hasattr(source, "source_id"):
                try:
                    repo.mark_source_scanned(conn, source.source_id)
                except Exception:
                    pass
        finally:
            conn.close()
        with progress.lock:
            progress.finished = True
    return progress


def process_pending_for_run(
    root: Path,
    source: PhotoSource,
    run_id: str,
    *,
    progress: Optional[Progress] = None,
    on_each: Optional[Callable[[dict], None]] = None,
    vision: Optional[VisionModel] = None,
) -> Progress:
    """只跑 Phase B(对已存在 run 的 pending 处理),用于:
      - /api/scan/resume:恢复 stopped 状态的 run
      - /api/runs/{id}/retry:重试 failed → pending 后继续跑
      - /api/reprocess:已经入队完,只跑处理
    """
    progress = progress or Progress()
    progress.run_id = run_id

    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    repo.reset_stuck_jobs(conn)
    # 把 run 状态置 running
    conn.execute("UPDATE scan_runs SET status='running', finished_at=NULL WHERE run_id=?", (run_id,))

    # 重建 ref_cache:重新遍历 source。
    # 只有 fs source 的 photo_id 依赖 content_hash(读首尾各 64KB);Apple source
    # photo_id = uuid,算 hash 纯属浪费 — 11 万张 × 128KB ≈ 14GB 无效 IO,
    # 是 resume 卡几分钟的主因,跳过后只剩 osxphotos 整库加载(无法避免)。
    with progress.lock:
        progress.phase = "enqueueing"   # 复用前端"正在遍历图片库"提示
    ref_cache: dict[str, PhotoRef] = {}
    for ref in source.iter_photos():
        if ref.source_id.startswith("fs:"):
            try:
                chash = ident.content_hash(ref.original_path)
            except Exception:
                continue
            pid = ident.photo_id_for(ref.source_id, ref.source_ref, chash)
        else:
            pid = ref.source_ref
        ref_cache[pid] = ref

    final_status = "completed"
    try:
        # resume / retry 路径不动 total(scan_runs.total 已经在首次 scan 时写好,
        # 之前 done/failed 累积值保留,本次只在 _process_phase 增量 +1)
        with progress.lock:
            progress.phase = "processing"
        _process_phase(conn, root, source, ref_cache, vision, run_id, progress, on_each)
        if progress.stop_flag.is_set():
            final_status = "stopped"
        else:
            remaining = repo.get_pending(conn, limit=1, run_id=run_id)
            final_status = "stopped" if remaining else "completed"
    except Exception as e:
        log.exception("process_pending_for_run 失败")
        progress.error = str(e)
        final_status = "failed"
    finally:
        try:
            repo.finish_run(conn, run_id, status=final_status)
        finally:
            conn.close()
        with progress.lock:
            progress.finished = True
    return progress


def _enqueue_phase(conn, source: PhotoSource, run_id: str, limit: Optional[int] = None):
    """流式遍历 source,enqueue_job + 建 ref_cache。"""
    ref_cache: dict[str, PhotoRef] = {}
    new_count = 0
    skip_count = 0
    seen = 0
    for ref in source.iter_photos():
        seen += 1
        try:
            chash = ident.content_hash(ref.original_path)
        except Exception as e:
            log.warning("content_hash 失败 %s: %s", ref.original_path, e)
            continue
        photo_id = ident.photo_id_for(ref.source_id, ref.source_ref, chash)
        ref_cache[photo_id] = ref

        existing_hash = repo.get_existing_hash(conn, photo_id)
        status = repo.get_job_status(conn, photo_id)
        if existing_hash == chash and status == "done":
            skip_count += 1
            continue

        # 确保 photos 行存在(jobs.photo_id 有 FK 到 photos)。
        # 这里写一个最小的 stub,pipeline.process_one + upsert_photo 之后会覆盖完整字段。
        identity_stub = {
            "photo_id":      photo_id,
            "source":        source.kind(),
            "source_ref":    ref.source_ref,
            "original_path": str(ref.original_path),
            "content_hash":  chash,
        }
        repo.ensure_photo_row(conn, identity_stub)

        captured = exif_extract.peek_captured_at(ref.original_path)
        repo.enqueue_job(conn, photo_id, run_id=run_id, captured_at_local=captured)
        new_count += 1
        if limit is not None and new_count >= limit:
            break
    return ref_cache, new_count, skip_count


def _process_phase(conn, root, source, ref_cache, vision, run_id, progress, on_each):
    """单线程主循环:pull pending → process_one → 状态机更新。"""
    vision_lock = threading.Lock() if vision is not None else None
    faces_lock = threading.Lock()

    while not progress.stop_flag.is_set():
        pending_ids = repo.get_pending(conn, limit=20, run_id=run_id)
        if not pending_ids:
            break
        for pid in pending_ids:
            if progress.stop_flag.is_set():
                break
            ref = ref_cache.get(pid)
            if ref is None:
                # ref_cache 没缓存 — source 变了,或入队后文件被删
                repo.fail_job(conn, pid, "ref 不在 source 当前枚举里(文件可能被删/移动)")
                continue

            repo.claim_job(conn, pid, stage="processing")
            cap_row = conn.execute(
                "SELECT captured_at_local FROM jobs WHERE photo_id=?", (pid,)
            ).fetchone()
            with progress.lock:
                progress.current_path = str(ref.original_path)
                progress.current_photo_id = pid
                progress.current_captured_at = (cap_row[0] if cap_row else "") or ""

            try:
                rec = pipeline.process_one(
                    root, source, ref,
                    vision=vision, vision_lock=vision_lock,
                    enable_faces=True, faces_lock=faces_lock, db_conn=conn,
                )
                repo.upsert_photo(conn, rec)
                repo.update_fts(
                    conn, rec["identity"]["photo_id"],
                    rec.get("vision"), rec.get("people"),
                )
                emb_result = repo.update_embedding(
                    conn, rec["identity"]["photo_id"],
                    rec.get("vision"), rec.get("people"),
                    _get_embedder_for_scan(),
                )
                if emb_result == "failed":
                    log.warning("embedding 失败 photo=%s(scan 继续,FTS 仍可命中)", pid)
                repo.finish_job(conn, pid)
                repo.inc_run_done(conn, run_id)   # 增量累加 scan_runs.done(resume 时不丢历史)
                # scanned_up_to 滚动向前(取 max)
                cap = cap_row[0] if cap_row else None
                repo.bump_run_scanned_up_to(conn, run_id, cap)
                with progress.lock:
                    progress.recent_done_timestamps.append(time.monotonic())
                if on_each:
                    try:
                        on_each(rec)
                    except Exception:
                        log.exception("on_each callback 异常")
                # 高德每日配额耗尽 → graceful stop,等次日 UTC+8 0:00 配额重置后用户在 Web 点继续
                if amap_geo.is_quota_exhausted(conn):
                    log.info("高德今日配额耗尽,暂停 run=%s,次日 UTC+8 0:00 重置后可继续", run_id)
                    conn.execute(
                        "UPDATE scan_runs SET note=? WHERE run_id=?",
                        ("高德今日配额耗尽,次日 UTC+8 0:00 重置后可继续", run_id),
                    )
                    progress.stop_flag.set()
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                log.exception("process failed: %s", ref.original_path)
                final = repo.fail_job(conn, pid, err_msg)
                if final == "failed":   # 永久失败才计入(retry 留 pending 不算)
                    repo.inc_run_failed(conn, run_id)
                log.info("photo %s → %s", pid, final)
