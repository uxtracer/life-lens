"""FastAPI app + 启动时自动打开浏览器。

CLI 入口 `lens` 调用本模块的 run() 启动。
"""
from __future__ import annotations

import logging
import os
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from . import api, chat as chat_mod
from ..store import db, repo

log = logging.getLogger(__name__)

# 默认数据根目录:用户家目录下的 ~/.life_lens
DEFAULT_ROOT = Path.home() / ".life_lens"


def create_app(root: Path) -> FastAPI:
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="life_lens", version="0.1.0")
    app.state.root = root

    # 启动时跑迁移 + 兜底:残留 processing 回 pending,running 状态的 run 标 stopped
    _bootstrap_db(root)

    # API 路由
    app.include_router(api.router, prefix="/api")
    app.include_router(chat_mod.router, prefix="/api")

    # 静态前端
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/health")
    def health():
        return {"ok": True, "root": str(root)}

    return app


def _bootstrap_db(root: Path) -> None:
    """server 启动时:
      1. init_schema(创建 scan_runs / ALTER ADD COLUMN 等幂等迁移)
      2. reset_stuck_jobs:残留 processing → pending(进程上次没正常退出)
      3. mark_running_as_stopped:把所有 scan_runs.status='running' 标 stopped
         (进程没了 run 不可能还 running,Sources 页据此显示"上次未完成,点继续")
      4. **不**自动启动 Phase B,等用户点
    """
    try:
        conn = db.connect(db.get_db_path(root))
        db.init_schema(conn)
        stuck = repo.reset_stuck_jobs(conn)
        runs = repo.mark_running_as_stopped(conn)
        if stuck or runs:
            log.info("启动兜底:reset %d 个 stuck jobs,标 %d 个 run 为 stopped", stuck, runs)
        conn.close()
    except Exception:
        log.exception("server 启动 _bootstrap_db 失败")


def run(host: str = "127.0.0.1", port: int = 7878, root: Path = DEFAULT_ROOT, open_browser: bool = True):
    import uvicorn
    # 让 store.config 跟 --root 走,而不是固定 ~/.life_lens
    os.environ["LIFE_LENS_ROOT"] = str(root)
    app = create_app(root)

    if open_browser:
        url = f"http://{host}:{port}"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="info")
