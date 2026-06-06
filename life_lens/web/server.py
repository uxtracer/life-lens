"""FastAPI app + 启动时自动打开浏览器。

CLI 入口 `lens` 调用本模块的 run() 启动。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from . import api, chat as chat_mod
from ..store import db, repo

log = logging.getLogger(__name__)

# 默认数据根目录:用户家目录下的 ~/.life_lens
DEFAULT_ROOT = Path.home() / ".life_lens"

# ============================================================
# LAN gate:非本机(局域网)客户端按白名单分权
#
# 设计:监听始终 0.0.0.0,本机(loopback)保留全功能 5-tab;局域网设备
# 受两层控制:
#   1. 总开关 config `serve.lan_chat`(默认关,配置页可切)— 关闭时内网
#      一律 403。**每次远程请求热读**,翻开关即时生效,不用重启
#   2. 开启后也只放行「问相册」所需的最小端点集 — 配置/扫描/面孔/浏览
#      以及一切写操作对内网一律 403。手机/iPad 访问 / 直接拿到移动端 chat 页
# ============================================================

# (method, 路径正则) 白名单 — 内网设备只放行这些
_LAN_ALLOW = [
    ("GET", re.compile(r"^/$")),                       # 移动端 chat 页(index 路由按来源分流)
    ("GET", re.compile(r"^/chat$")),                   # 同上,显式路径
    ("GET", re.compile(r"^/health$")),
    ("GET", re.compile(r"^/static/[^/]+$")),           # 前端静态资源(纯代码,无数据)
    ("POST", re.compile(r"^/api/chat$")),              # 问相册 SSE
    ("GET", re.compile(r"^/api/thumb/[^/]+$")),        # 1024px 缩略图
    ("GET", re.compile(r"^/api/original/[^/]+$")),     # 原图查看/下载(用户决定开放)
    ("GET", re.compile(r"^/api/photo/[^/]+$")),        # viewer 的 caption/描述(只读单段路径,
                                                       #   /api/photo/{id}/faces 等子路径不放行)
    ("GET", re.compile(r"^/api/llm-providers$")),      # provider 下拉(已脱敏,无 api_key)
    ("GET", re.compile(r"^/api/llm-info$")),
]


def _is_local_client(request: Request) -> bool:
    """是否本机请求。'testclient' 是 starlette TestClient 的默认 client host,
    视作本机 — 否则 A 层既有 API 测试全 403;模拟远程的测试显式传 client=(ip, port)。"""
    host = request.client.host if request.client else ""
    return host in ("", "127.0.0.1", "::1", "localhost", "testclient")


def create_app(root: Path) -> FastAPI:
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="life_lens", version="0.1.0")
    app.state.root = root

    # 启动时跑迁移 + 兜底:残留 processing 回 pending,running 状态的 run 标 stopped
    _bootstrap_db(root)

    # LAN gate:必须注册在最外层 — 非本机请求先过白名单再进任何路由
    @app.middleware("http")
    async def lan_gate(request: Request, call_next):
        if _is_local_client(request):
            return await call_next(request)
        # 总开关热读(只对远程请求读,本机零开销;config 是小 json,代价可忽略)
        from ..store import config as config_mod
        if not config_mod.lan_chat_enabled():
            return JSONResponse(
                {"detail": "内网访问未开启(在本机配置页打开「内网访问」开关)"},
                status_code=403,
            )
        method = "GET" if request.method == "HEAD" else request.method
        path = request.url.path
        for m, pat in _LAN_ALLOW:
            if m == method and pat.match(path):
                return await call_next(request)
        return JSONResponse(
            {"detail": "仅本机可访问(局域网设备只开放「问相册」)"},
            status_code=403,
        )

    # API 路由
    app.include_router(api.router, prefix="/api")
    app.include_router(chat_mod.router, prefix="/api")

    # 静态前端
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def index(request: Request):
        # 本机 → 完整 5-tab 页;局域网设备 → 移动端问相册页
        if _is_local_client(request):
            return FileResponse(static_dir / "index.html")
        return FileResponse(static_dir / "chat.html")

    @app.get("/chat")
    def chat_page():
        # 显式移动端 chat 页(本机也可访问,方便桌面调试)
        return FileResponse(static_dir / "chat.html")

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


def run(host: str = "0.0.0.0", port: int = 7878, root: Path = DEFAULT_ROOT, open_browser: bool = True):
    import uvicorn
    # 让 store.config 跟 --root 走,而不是固定 ~/.life_lens
    os.environ["LIFE_LENS_ROOT"] = str(root)
    app = create_app(root)

    if open_browser:
        # 0.0.0.0/:: 是监听地址不是可访问地址,浏览器打开 loopback
        open_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        url = f"http://{open_host}:{port}"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="info")
