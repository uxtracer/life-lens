"""life_lens CLI 入口。

默认行为(无参数)= 启动 web GUI + 自动开浏览器。
子命令是 REST 的本地 wrapper,用于脚本 / cron。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="lens",
        description="life_lens — 本地相册结构化记忆库",
    )
    parser.add_argument("--root", type=Path, default=None,
                        help="数据根目录(默认 ~/.life_lens)")
    sub = parser.add_subparsers(dest="cmd")

    # 默认子命令:serve(也是无 cmd 时的默认行为)
    serve_p = sub.add_parser("serve", help="启动 web GUI(默认行为)")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=7878)
    serve_p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    scan_p = sub.add_parser("scan", help="扫描一个本地目录(filesystem source);默认续传")
    scan_p.add_argument("path", type=Path, help="目录绝对路径")
    scan_p.add_argument("--workers", type=int, default=1)
    scan_p.add_argument("--limit", type=int, default=None, help="只处理前 N 张(开发/烟测用)")
    scan_p.add_argument("--no-vision", action="store_true", help="跳过视觉模型(只跑 exif + 预处理 + derived)")
    scan_p.add_argument("--model", default=None, help="Ollama 模型名,默认 qwen3-vl:8b-instruct")
    scan_p.add_argument("--retry-failed", action="store_true",
                        help="启动前把所有 status=failed 的 jobs 重置 pending(retry_count=0)")
    scan_p.add_argument("--enqueue-only", action="store_true",
                        help="只跑 Phase A 入队,不处理")

    status_p = sub.add_parser("status", help="打印 jobs 统计 + photo 数")
    status_p.add_argument("--jobs", action="store_true",
                          help="详细模式:列最近 5 个 run + 最近失败")
    sub.add_parser("init",   help="初始化数据库(在 --root 下创建 lens.db)")

    rep_p = sub.add_parser("reprocess", help="仅重跑某个 group(不重跑昂贵的 vision)")
    rep_p.add_argument("--group", required=True, choices=["faces"], help="目前只支持 faces")
    rep_p.add_argument("--mode", default="quick", choices=["quick", "full"],
                       help="quick=只重新分配 cluster(秒级);full=重跑 InsightFace detect(慢)。默认 quick")

    bak_p = sub.add_parser("backup", help="给 lens.db 拍一个 WAL-safe 快照(原子)")
    bak_p.add_argument("--out", type=Path, default=None,
                       help="输出路径,默认 <root>/backups/lens-YYYYMMDD-HHMM.db")

    args = parser.parse_args(argv)
    root = (args.root or Path.home() / ".life_lens").expanduser().resolve()
    # 让 store.config 跟 --root 走(scan/reprocess 等子命令也会读 config 拿 amap_key / vision endpoint)
    import os as _os
    _os.environ["LIFE_LENS_ROOT"] = str(root)

    cmd = args.cmd or "serve"

    if cmd == "serve":
        from ..web.server import run
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 7878)
        open_browser = not getattr(args, "no_browser", False)
        run(host=host, port=port, root=root, open_browser=open_browser)
    elif cmd == "scan":
        _cmd_scan(
            root, args.path, args.workers, args.limit, args.no_vision, args.model,
            retry_failed=args.retry_failed, enqueue_only=args.enqueue_only,
        )
    elif cmd == "status":
        _cmd_status(root, jobs=getattr(args, "jobs", False))
    elif cmd == "init":
        _cmd_init(root)
    elif cmd == "reprocess":
        _cmd_reprocess(root, args.group, args.mode)
    elif cmd == "backup":
        _cmd_backup(root, args.out)
    else:
        parser.print_help()
        sys.exit(2)


def _cmd_reprocess(root: Path, group: str, mode: str):
    if group == "faces":
        if mode == "quick":
            from ..scanner.reprocess import rematch_faces
            print("quick rematch:复用已有 embedding 重新归类...")
            result = rematch_faces(root)
        else:
            from ..scanner.reprocess import reprocess_faces
            print("full reprocess:重跑 InsightFace detect + assign...")
            result = reprocess_faces(root)
        print(json.dumps(result, ensure_ascii=False, indent=2))


def _cmd_init(root: Path):
    from ..store import db
    root.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    conn.close()
    print(f"已初始化: {db.get_db_path(root)}")


def _cmd_scan(
    root: Path, path: Path, workers: int, limit, no_vision: bool, model,
    *, retry_failed: bool = False, enqueue_only: bool = False,
):
    import signal
    import threading
    import time
    from ..sources.filesystem import FilesystemSource
    from ..sources.photos_library import ApplePhotosSource
    from ..scanner.runner import scan_source, Progress, generate_run_id, _enqueue_phase
    from ..store import db, repo

    path = path.expanduser().resolve()
    is_apple = path.name.endswith(".photoslibrary")
    if is_apple:
        if not path.exists():
            print(f"Apple Photos 库不存在: {path}", file=sys.stderr)
            sys.exit(1)
    elif not path.is_dir():
        print(f"目录不存在: {path}", file=sys.stderr)
        sys.exit(1)

    # 启动兜底:reset 残留 + 注册 source
    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    repo.reset_stuck_jobs(conn)
    repo.mark_running_as_stopped(conn)
    if is_apple:
        src = ApplePhotosSource(path)
        repo.upsert_source(conn, src.source_id, "photos_library", {"path": str(path)})
    else:
        src = FilesystemSource(path)
        repo.upsert_source(conn, src.source_id, "filesystem", {"path": str(path)})

    if retry_failed:
        n = repo.reset_failed_jobs(conn)
        print(f"--retry-failed: 重置 {n} 个 failed → pending")

    conn.close()

    if enqueue_only:
        # 只跑 Phase A
        run_id = generate_run_id()
        conn = db.connect(db.get_db_path(root))
        repo.create_run(conn, run_id, kind="scan", triggered_by="cli",
                        source_ids=[src.source_id])
        try:
            ref_cache, new_count, skip = _enqueue_phase(conn, src, run_id, limit=limit)
            stats = repo.job_stats(conn, run_id=run_id)
            total = sum(stats.values())
            repo.update_run_counts(conn, run_id, total=total,
                                   done=stats.get("done", 0), failed=stats.get("failed", 0))
            repo.finish_run(conn, run_id, status="stopped")
        finally:
            conn.close()
        print(f"--enqueue-only 完成:入队 {new_count} / 跳过已 done {skip}  run_id={run_id}")
        return

    vision = None
    if not no_vision:
        from ..vision.ollama import OllamaVision, DEFAULT_MODEL, health_check
        vision = OllamaVision(model=model or DEFAULT_MODEL)
        ok, msg = health_check(expect_model=vision.model)
        if ok:
            print(f"视觉模型: {vision.name}  ({msg})")
        else:
            print(f"⚠️  视觉模型不可用: {msg}", file=sys.stderr)
            print("    继续跑(vision 阶段会失败,只产 exif + face + derived)")
    else:
        print("跳过视觉模型(--no-vision)")

    progress = Progress()

    def on_sigint(signum, frame):
        if not progress.stop_flag.is_set():
            print("\n收到 Ctrl+C,等当前一张跑完后退出(再按一次强制退出)...", file=sys.stderr)
            progress.stop_flag.set()
        else:
            print("\n强制退出。", file=sys.stderr)
            sys.exit(130)

    signal.signal(signal.SIGINT, on_sigint)

    suffix = f" --limit {limit}" if limit else ""
    print(f"扫描: {path}{suffix}")

    # 后台线程跑 scan,主线程打印进度条
    result_box: dict = {}

    def runner():
        try:
            scan_source(
                root, src, run_id=None, kind="scan", triggered_by="cli",
                progress=progress, vision=vision, limit=limit,
            )
        except Exception as e:
            result_box["error"] = str(e)

    th = threading.Thread(target=runner, daemon=True)
    th.start()

    _print_progress_loop(root, progress, refresh_sec=2.0)
    th.join()

    conn = db.connect(db.get_db_path(root))
    try:
        stats = repo.job_stats(conn, run_id=progress.run_id)
        run = repo.get_run(conn, progress.run_id)
        scanned_up_to = repo.get_run_scanned_up_to(conn, progress.run_id)
    finally:
        conn.close()

    done = stats.get("done", 0)
    failed = stats.get("failed", 0)
    pending = stats.get("pending", 0)
    total = sum(stats.values())
    final = (run or {}).get("status", "?")
    print(f"\n本次 run={progress.run_id}  状态={final}")
    print(f"  done={done}  failed={failed}  pending={pending}  total={total}")
    if scanned_up_to:
        print(f"  已扫到拍照时间: {scanned_up_to}")
    if pending:
        print("  💡 还有 pending,再跑一次 `lens scan ...` 自然续传")


def _print_progress_loop(root: Path, progress, refresh_sec: float = 2.0):
    """主线程打 ASCII 进度条,每 N 秒覆盖一行 \\r。"""
    import time
    from ..store import db, repo
    while not progress.finished:
        try:
            conn = db.connect(db.get_db_path(root))
            try:
                stats = repo.job_stats(conn, run_id=progress.run_id) if progress.run_id else {}
            finally:
                conn.close()
            done    = stats.get("done", 0)
            failed  = stats.get("failed", 0)
            pending = stats.get("pending", 0)
            processing = stats.get("processing", 0)
            total = done + failed + pending + processing
            snap = progress.snapshot()
            rate = snap.get("rate")
            cap = snap.get("current_captured_at") or ""
            cur_path = snap.get("current_path") or ""
            cur_short = (cur_path.rsplit("/", 1)[-1] if cur_path else "")
            pct = (done + failed) / total * 100 if total else 0
            eta = ""
            if rate and rate > 0 and pending:
                secs = int(pending / rate)
                h, m = divmod(secs, 3600); m //= 60
                eta = f"ETA {h}h{m:02d}m" if h else f"ETA {m}m"
            bar_w = 28
            filled = int(bar_w * pct / 100)
            bar = "█" * filled + "░" * (bar_w - filled)
            line = (
                f"[{bar}] {done + failed}/{total} {pct:.1f}%  "
                f"fail={failed}  rate={rate or 0:.2f}/s  {eta}  → {cap}  {cur_short}"
            )
            sys.stdout.write("\r" + line[:200] + "\033[K")
            sys.stdout.flush()
        except Exception:
            pass
        time.sleep(refresh_sec)
    sys.stdout.write("\n")


def _cmd_backup(root: Path, out: Path = None):
    """用 sqlite3 .backup API 做 WAL-safe 快照。

    直接 cp lens.db 在 WAL 模式下可能拷到事务中间态。.backup() 走 SQLite Online Backup API,
    保证拷的是一致性快照,即使扫描 / chat 同时在写也安全。
    """
    import sqlite3
    from datetime import datetime
    from ..store import db

    src_path = db.get_db_path(root)
    if not src_path.exists():
        print(f"❌ lens.db 不存在: {src_path}", file=sys.stderr)
        sys.exit(1)

    if out is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M")
        out = root / "backups" / f"lens-{ts}.db"
    out = out.expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"备份 {src_path} → {out}")
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(out))
    try:
        with dst:
            src.backup(dst)
        size_mb = out.stat().st_size / (1024 * 1024)
        print(f"✅ 完成,{size_mb:.1f} MB")
    finally:
        src.close()
        dst.close()


def _cmd_status(root: Path, jobs: bool = False):
    from ..store import db, repo
    conn = db.connect(db.get_db_path(root))
    db.init_schema(conn)
    try:
        stats = repo.job_stats(conn)
        total = repo.count_photos(conn)
        out = {
            "root": str(root),
            "total_photos": total,
            "job_stats": stats,
        }
        if jobs:
            out["recent_runs"] = repo.list_runs(conn, limit=5)
            out["recent_failures"] = repo.recent_failures(conn, limit=5)
            resumable = repo.get_resumable_run(conn)
            if resumable:
                out["resumable_run"] = {
                    "run_id": resumable["run_id"],
                    "pending": resumable.get("pending"),
                    "scanned_up_to": resumable.get("scanned_up_to"),
                }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
