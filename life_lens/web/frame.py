"""智能相框(ESP32 等)LAN 接口 — 轮播照片。

设计目标:给"带屏幕的 dumb 设备"最省事的固件 —— 一次 GET 直接拿到一张
已缩放好、可直接喂给 JPEG 解码器的图;服务端负责"该放哪张"(洗牌轮播)。

隐私 / 安全(见 CLAUDE.md「LAN 分权」):
  - 这组端点是**独立的新接口**,和「问相册」(serve.lan_chat)完全隔离,
    单独开关 `frame.lan_enabled`(默认关)。两者互不影响。
  - LAN gate(web/server.py)按 _LAN_FRAME_ALLOW 白名单 + frame_lan_enabled() 放行。
    配置/扫描/写操作 + 相框自身的开关端点都不在白名单 → 相框不能给自己开门或改配置。
  - 只吐缩放后的 baseline JPEG(默认 1024、优先从原图单次压缩),不发原图字节。

端点:
  GET /api/frame/next                  → image/jpeg(主接口;轮播下一张,元信息在响应头)
  GET /api/frame/photo/{photo_id}      → image/jpeg(取指定 id,给自管播放列表的智能固件)
  GET /api/frame/playlist              → JSON id 列表 + 描述(智能固件本地轮播用)
  GET /api/frame/info                  → JSON {theme, pool_size}(本机自测 / 调试)

主题(照片池)优先级:?theme= 查询参数 > config frame.theme > 收藏照片。
"""
from __future__ import annotations

import io
import json
import logging
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response

from ..store import db
from ..query import search as search_mod
from ..preprocess.cache import cache_path
from ..preprocess.decode import decode_to_rgb
from . import chat as chat_mod
from . import llm as llm_mod

log = logging.getLogger(__name__)

router = APIRouter()

# 缩放参数边界 —— 防止恶意/手滑传超大尺寸把内存撑爆
_MIN_SIDE = 16
_MAX_SIDE = 2048
# 默认 = 预处理缓存原生尺寸(1024px 长边)。不传 w/h 时直接给满分辨率,不缩小;
# 固件传了自己屏幕尺寸就按尺寸裁剪缩放。源图是 1024px JPEG,请求超过 1024 不会更清晰。
_DEFAULT_W = 1024
_DEFAULT_H = 1024
_POOL_LIMIT = 1000       # 轮播池上限(够大保证多样,又不至于每次请求拖太久)
_CAPTION_MAX = 80        # 响应头 caption 截断(设备头缓冲有限)
_DEFAULT_QUALITY = 90    # 默认 JPEG 质量(从原图单次压缩,90 接近无损又不至太大)


# ============================================================
# 轮播器:按主题维护一份洗牌顺序 + 游标,逐张走完再重洗。
#
# 为什么不用纯随机:纯随机会短期内重复、漏掉部分照片;洗牌+游标保证"轮播"
# 语义(一轮内每张恰好出现一次)。池签名(ids 内容)变了(如新增收藏)自动重洗。
# 状态是进程内内存,重启丢失(无所谓,重洗即可),线程安全靠一把锁。
# ============================================================

class _Rotator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {}     # key -> {"order": [...], "pos": int, "sig": int, "ids": [...]}
        import random
        self._rng = random.Random()

    def next_index(self, key: str, ids: list[str]) -> Optional[int]:
        """返回 ids 中下一张的下标;ids 为空返 None。"""
        if not ids:
            return None
        sig = hash(tuple(ids))
        with self._lock:
            st = self._state.get(key)
            if st is None or st["sig"] != sig or st["pos"] >= len(st["order"]):
                order = list(range(len(ids)))
                self._rng.shuffle(order)
                st = {"order": order, "pos": 0, "sig": sig}
                self._state[key] = st
            idx = st["order"][st["pos"]]
            st["pos"] += 1
            return idx


_rotator = _Rotator()


# ============================================================
# 辅助
# ============================================================

def _root(request: Request) -> Path:
    return request.app.state.root


def _conn(request: Request):
    conn = db.connect(db.get_db_path(_root(request)))
    db.init_schema(conn)
    return conn


def _clamp_side(v: Optional[int], default: int) -> int:
    if v is None:
        return default
    return max(_MIN_SIDE, min(_MAX_SIDE, int(v)))


def _resolve_theme(request: Request, theme: Optional[str]) -> str:
    """主题优先级:查询参数 > config frame.theme > ''(收藏)。"""
    if theme is not None and theme.strip():
        return theme.strip()
    from ..store import config as cfg
    return cfg.frame_theme()


def _resolve_pool(conn, theme: str, limit: int = _POOL_LIMIT) -> tuple[list[str], str]:
    """构建照片池(走共享 query 层,语义和 chat/REST 一致)。返回 (photo_ids, kind)。

    主题三态(kind):
      - 空 → 'favorites':只取收藏照片
      - 命中已命名的人(人脸过滤,含模糊兜底)→ 'person':**出这个人全部照片**。
        关键:名字当内容搜索会既漏又混(描述不一定写名字 / 语义飘到别人),
        所以人名优先走 `persons=` 人脸过滤,锚定"是谁"。
      - 否则 → 'theme':当场景/物品内容搜索(query=theme)
    三路 base where 都含 vision IS NOT NULL(只选已处理、能显示的照片)。
    """
    if not theme:
        res = search_mod.search_photos(conn, favorite_only=True, limit=limit)
        return [it["photo_id"] for it in res.get("items", [])], "favorites"
    # 先当人名:人脸过滤(search_photos 的 persons 解析内置精确 + cluster_id + 模糊兜底;
    # 不是人名时 _resolve_person_filter 返空 → 结果空,自然 fall through 到内容搜索)
    pres = search_mod.search_photos(conn, persons=[theme], limit=limit)
    pitems = pres.get("items", [])
    if pitems:
        return [it["photo_id"] for it in pitems], "person"
    # 否则当内容主题
    res = search_mod.search_photos(conn, query=theme, limit=limit)
    return [it["photo_id"] for it in res.get("items", [])], "theme"


def _pool_ids(conn, theme: str, limit: int = _POOL_LIMIT) -> list[str]:
    ids, _kind = _resolve_pool(conn, theme, limit)
    return ids


# ============================================================
# LLM 审核播放列表(可控性 + 第二道校验 + 灵活检索)
#
# 问题:纯 search 召回宽、不可控(语义飘、描述没写名字漏召)。复用「问相册」两轮 LLM:
#   Round 1  主题 → 检索条件(人名/时间/地点/扩词,比字符串搜灵活)
#   校验轮    把候选的 description/tags/objects/scene 喂 LLM,只留真正符合主题的
# 产出一份已审核的 photo_id 列表落盘缓存。
#
# **关键:LLM 只在"设主题 / 点重建"时跑一次,/next 只读缓存洗牌 —— 绝不每帧调 LLM。**
# 构建未完成 / 失败 / 无 LLM provider 时,/next 优雅回退到非 LLM 池(相框始终有图)。
# ============================================================

_PLAYLIST_FILE = "frame_playlist.json"
_VERIFY_CAND_CAP = 200       # 进校验轮的候选上限(token 保护,和 chat Round 2 同量级)

_build_state: dict = {
    "running": False, "theme": None, "phase": None,
    "candidate_count": 0, "verified_count": 0,
    "built_at": None, "error": None, "provider": None,
}
_build_lock = threading.Lock()


def _playlist_path(root: Path) -> Path:
    return root / _PLAYLIST_FILE


def load_playlist(root: Path) -> Optional[dict]:
    p = _playlist_path(root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_playlist(root: Path, data: dict) -> None:
    p = _playlist_path(root)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


_VERIFY_SYSTEM = """你在为一个"智能相框"挑照片。用户设了一个主题,系统已用检索召回一批候选(宽召回,可能含不符合的)。
你的工作:逐张读候选的 description / tags / objects / scene,只保留**真正符合主题**的照片,输出 JSON。
**只输出 JSON,不要任何其他文字。** 格式:{"photo_ids": ["id1","id2", ...]}

规则:
- 只能从候选里挑,photo_id 必须逐字符原样复制(短编码),绝不可编造、改写、补长
- 候选可能带 match 字段:literal=关键词字面命中(强证据);semantic=仅语义相近(弱候选,从严,不符合就丢)
- 宁缺毋滥:不确定是否符合就不放;但若大量候选确实都符合主题,就都保留(别为了"精简"乱砍)
- 主题是人名/人物时,候选已按人脸召回,默认都符合,除非 description 明显矛盾
"""


def _llm_verify(theme: str, items: list, provider_id: Optional[str]) -> list:
    """把候选喂 LLM,返回它认可的完整 photo_id 列表(复用 chat 的短 id 瘦身 + 还原)。"""
    items = items[:_VERIFY_CAND_CAP]
    full_ids = [it.get("photo_id") for it in items if it.get("photo_id")]
    if not full_ids:
        return []
    id_map = chat_mod._short_id_map(full_ids)
    slim = chat_mod._slim_for_llm({"items": items}, id_map)
    prompt = (
        f"相框主题: {theme}\n\n"
        f"候选照片(只能从这里挑):\n```json\n"
        f"{json.dumps(slim['items'], ensure_ascii=False, separators=(',', ':'))}\n```\n\n"
        f"只保留**真正符合主题「{theme}」**的照片,输出 {{\"photo_ids\":[...]}}。"
    )
    text = "".join(llm_mod.call_llm(_VERIFY_SYSTEM, prompt, stream=False, provider_id=provider_id))
    parsed = chat_mod._extract_json(text) or {}
    short = parsed.get("photo_ids") or []
    if not isinstance(short, list):
        return []
    return chat_mod._expand_photo_ids([str(s) for s in short], full_ids, id_map)


def build_playlist(root: Path, theme: str, provider_id: Optional[str] = None) -> dict:
    """同步构建一份 LLM 审核过的播放列表并落盘。theme 必须非空(空=收藏不需 LLM)。

    流程:Round 1 主题→检索条件 → dispatch 召回 → (有 query 才)LLM 校验过滤 → 存盘。
    人名/收藏类(无 query)不校验(人脸已精确,LLM 看不到脸,校验只会误删)。
    """
    def _phase(name: str):
        with _build_lock:
            _build_state["phase"] = name

    _phase("planning")
    conn = db.connect(db.get_db_path(root))
    try:
        system = chat_mod._build_round1_system(conn)
        r1 = "".join(llm_mod.call_llm(system, theme, stream=False, provider_id=provider_id))
        plan = chat_mod._extract_json(r1) or {}
        action = plan.get("action") or "search_photos"
        args = dict(plan.get("args") or {})
        # 相框只要出图:places_visited / counts_by_year 不适用 → 退回内容搜索
        if action != "search_photos":
            action, args = "search_photos", {"query": theme}
        # 相框要的是"大池子轮播",强制拉满召回(LLM 默认 limit 偏小,只够 chat 展示几张)
        args["limit"] = _VERIFY_CAND_CAP
        _phase("searching")
        result = chat_mod._dispatch(conn, action, args)
    finally:
        conn.close()

    items = result.get("items") or []
    cand_ids = [it["photo_id"] for it in items if it.get("photo_id")]
    needs_verify = bool(args.get("query"))      # 有内容词才需要第二道校验
    verified = cand_ids
    if needs_verify and items:
        _phase("verifying")
        try:
            v = _llm_verify(theme, items, provider_id)
            if v:                               # LLM 全砍空时退回原候选,避免相框黑屏
                verified = v
        except Exception as e:
            log.warning("frame playlist verify 失败,退回原候选: %s", e)

    data = {
        "theme": theme,
        "kind": ("person" if (args.get("persons") and not args.get("query")) else
                 "favorites" if args.get("favorite_only") else "theme"),
        "photo_ids": verified,
        "candidate_count": len(cand_ids),
        "verified_count": len(verified),
        "verified_by_llm": bool(needs_verify),
        "plan": {"action": action, "args": args, "rationale": plan.get("rationale", "")},
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": provider_id,
    }
    _save_playlist(root, data)
    return data


def _build_worker(root: Path, theme: str, provider_id: Optional[str]) -> None:
    try:
        data = build_playlist(root, theme, provider_id)
        with _build_lock:
            _build_state.update({
                "running": False, "phase": "done", "error": None,
                "candidate_count": data["candidate_count"],
                "verified_count": data["verified_count"],
                "built_at": data["built_at"],
            })
    except Exception as e:
        log.exception("frame playlist 构建失败")
        with _build_lock:
            _build_state.update({
                "running": False, "phase": "error",
                "error": f"{type(e).__name__}: {e}",
            })


def start_build(root: Path, theme: str, provider_id: Optional[str] = None) -> bool:
    """后台线程启动一次构建。已在跑 → 返 False(不重复启动)。theme 空 → 不启动。"""
    theme = (theme or "").strip()
    if not theme:
        return False
    with _build_lock:
        if _build_state["running"]:
            return False
        _build_state.update({
            "running": True, "theme": theme, "phase": "planning",
            "candidate_count": 0, "verified_count": 0,
            "built_at": None, "error": None, "provider": provider_id,
        })
    t = threading.Thread(target=_build_worker, args=(root, theme, provider_id), daemon=True)
    t.start()
    return True


def _brief(conn, photo_id: str) -> dict:
    """取单张的轻量元信息(描述 + 拍摄时间),给响应头 / playlist 用。"""
    row = conn.execute(
        """
        SELECT json_extract(vision, '$.description') AS caption,
               COALESCE(NULLIF(captured_at_utc, ''),
                        json_extract(exif, '$.captured_at_local')) AS date
        FROM photos WHERE photo_id = ?
        """,
        (photo_id,),
    ).fetchone()
    if not row:
        return {"caption": None, "date": None}
    return {"caption": row["caption"], "date": row["date"]}


def _pick_source(root: Path, conn, photo_id: str) -> Optional[Path]:
    """选出图源:**优先原图**(全分辨率,单次压缩),原图不在(如 iCloud 未下载)
    才回退预处理 1024px 缓存。两者都没有返 None。

    为什么优先原图:相框从原图直接缩到屏幕尺寸 = 一次干净降采样;走缓存则是
    "原图→1024/q85→再压" 双重 JPEG,且竖图缓存宽仅 768,横屏 cover 还会被放大 → 更糊。
    """
    row = conn.execute(
        "SELECT json_extract(identity,'$.original_path') FROM photos WHERE photo_id=?",
        (photo_id,),
    ).fetchone()
    if row and row[0]:
        op = Path(row[0])
        if op.exists():
            return op
    cp = cache_path(root, photo_id)
    return cp if cp.exists() else None


def _render_jpeg(path: Path, w: int, h: int, mode: str, quality: int) -> bytes:
    """把源图(原图或缓存)缩放出相框尺寸的 JPEG。

    用 decode_to_rgb 统一解码(HEIC/RAW/JPEG 都行 + EXIF 方向校正)。
    mode:
      - 'contain'(默认):等比缩放到 w×h 内(不裁剪,可能留边,不丢人脸)
      - 'cover':等比缩放并居中裁剪铺满 w×h(全屏相框常用)
    """
    from PIL import Image, ImageOps
    im = decode_to_rgb(path)
    if mode == "cover":
        im = ImageOps.fit(im, (w, h), Image.LANCZOS)
    else:
        im.thumbnail((w, h), Image.LANCZOS)
    buf = io.BytesIO()
    # 必须 baseline(progressive=False):嵌入式解码器(ESP32 TJpg_Decoder 等)
    # 只能解基线 JPEG,渐进式会直接解码失败(JDR_FMT3)。文档也承诺的是 baseline。
    im.save(buf, "JPEG", quality=quality, optimize=True, progressive=False)
    return buf.getvalue()


def _image_response(request: Request, conn, photo_id: str, brief: Optional[dict],
                    w: int, h: int, mode: str, quality: int) -> Response:
    root = _root(request)
    src = _pick_source(root, conn, photo_id)
    if src is None:
        raise HTTPException(404, "照片不可用(原图和缓存都没有)")
    try:
        data = _render_jpeg(src, w, h, mode, quality)
    except Exception:
        # 原图解码失败(如缺 HEIC 支持 / 文件损坏)→ 回退缓存再试一次
        cp = cache_path(root, photo_id)
        if src != cp and cp.exists():
            data = _render_jpeg(cp, w, h, mode, quality)
        else:
            raise HTTPException(404, "照片解码失败")
    headers = {
        "X-Photo-Id": photo_id,
        "Cache-Control": "no-store",
    }
    if brief:
        if brief.get("date"):
            headers["X-Photo-Date"] = str(brief["date"])
        cap = (brief.get("caption") or "")[:_CAPTION_MAX]
        if cap:
            # 头部值必须 latin-1 安全 → 中文 URL 编码,固件按需解码
            headers["X-Photo-Caption"] = urllib.parse.quote(cap)
    return Response(content=data, media_type="image/jpeg", headers=headers)


# ============================================================
# 端点
# ============================================================

@router.get("/frame/next")
def frame_next(
    request: Request,
    theme: Optional[str] = None,
    w: Optional[int] = None,
    h: Optional[int] = None,
    mode: str = "contain",
    quality: int = _DEFAULT_QUALITY,
):
    """轮播下一张 —— 直接回 image/jpeg。

    查询参数:
      theme   主题(留空 = 用 config frame.theme,再留空 = 收藏照片)
      w,h     目标尺寸(默认 1024×1024;固件按自己屏幕传更省内存)
      mode    contain(默认,不裁) | cover(裁剪铺满)
      quality JPEG 质量(默认 90;从原图单次压缩,不是双重压缩)
    元信息在响应头:X-Photo-Id / X-Photo-Date / X-Photo-Caption(URL 编码)。
    """
    w = _clamp_side(w, _DEFAULT_W)
    h = _clamp_side(h, _DEFAULT_H)
    mode = "cover" if mode == "cover" else "contain"
    quality = max(40, min(95, quality))

    conn = _conn(request)
    try:
        resolved = _resolve_theme(request, theme)
        # 有主题且已有匹配的 LLM 审核列表 → 用它(可控、已校验);否则回退非 LLM 池
        ids, key = None, None
        if resolved:
            pl = load_playlist(_root(request))
            if pl and pl.get("theme") == resolved and pl.get("photo_ids"):
                ids = pl["photo_ids"]
                key = "pl:" + resolved
        if ids is None:
            ids = _pool_ids(conn, resolved)
            key = resolved or "__favorites__"
        if not ids:
            raise HTTPException(404, "照片池为空(没有收藏照片,或该主题没匹配到)")
        # 逐张走轮播,跳过原图和缓存都缺的(最多试几张)
        last_err = None
        for _ in range(min(8, len(ids))):
            idx = _rotator.next_index(key, ids)
            if idx is None:
                break
            pid = ids[idx]
            if _pick_source(_root(request), conn, pid) is None:
                last_err = pid
                continue
            return _image_response(request, conn, pid, _brief(conn, pid), w, h, mode, quality)
        raise HTTPException(404, f"池里照片都取不到图源(最后试的 {last_err})")
    finally:
        conn.close()


@router.get("/frame/photo/{photo_id}")
def frame_photo(
    request: Request,
    photo_id: str,
    w: Optional[int] = None,
    h: Optional[int] = None,
    mode: str = "contain",
    quality: int = _DEFAULT_QUALITY,
):
    """取指定 photo_id 的相框尺寸 JPEG(给自管播放列表的智能固件)。"""
    w = _clamp_side(w, _DEFAULT_W)
    h = _clamp_side(h, _DEFAULT_H)
    mode = "cover" if mode == "cover" else "contain"
    quality = max(40, min(95, quality))
    conn = _conn(request)
    try:
        return _image_response(request, conn, photo_id, _brief(conn, photo_id), w, h, mode, quality)
    finally:
        conn.close()


@router.get("/frame/playlist")
def frame_playlist(request: Request, theme: Optional[str] = None, limit: int = 200):
    """返回照片池的 id 列表 + 描述 —— 智能固件本地缓存后自己轮播。

    每项:{ photo_id, date, caption }。只含有预处理缓存的(能直接取图)。
    """
    limit = max(1, min(_POOL_LIMIT, limit))
    conn = _conn(request)
    try:
        resolved = _resolve_theme(request, theme)
        ids = _pool_ids(conn, resolved, limit=limit)
        items = []
        root = _root(request)
        for pid in ids:
            if not cache_path(root, pid).exists():
                continue
            b = _brief(conn, pid)
            items.append({"photo_id": pid, "date": b["date"], "caption": b["caption"]})
    finally:
        conn.close()
    return {"theme": resolved, "count": len(items), "items": items}


@router.get("/frame/info")
def frame_info(request: Request, theme: Optional[str] = None):
    """池大小 + 当前主题 + 匹配方式(本机自测 / 配置页显示用)。

    kind:favorites(收藏)/ person(命中人名,出这个人全部照片)/ theme(内容搜索)。
    """
    conn = _conn(request)
    try:
        resolved = _resolve_theme(request, theme)
        ids, kind = _resolve_pool(conn, resolved)
    finally:
        conn.close()
    return {
        "theme": resolved, "pool_size": len(ids), "kind": kind,
        "using_favorites": kind == "favorites",
    }


# ---- LLM 审核播放列表:构建 / 状态(仅本机 —— 不在 LAN 白名单,相框设备访问不到)----

@router.post("/frame/playlist/rebuild")
def frame_playlist_rebuild(request: Request, body: dict = Body(default={})):
    """用 LLM 重建当前主题的审核播放列表(后台线程)。

    body:{ theme?: 覆盖当前 config 主题, provider_id?: 指定 LLM }
    主题为空(收藏)不需要 LLM,返回 skipped。
    """
    body = body or {}
    theme = body.get("theme")
    if theme is None:
        from ..store import config as cfg
        theme = cfg.frame_theme()
    theme = (theme or "").strip()
    if not theme:
        raise HTTPException(400, "收藏模式(空主题)不需要 AI 挑选;请先设一个主题")
    started = start_build(_root(request), theme, body.get("provider_id"))
    if not started:
        raise HTTPException(409, "已有构建任务在跑")
    return {"ok": True, "theme": theme}


@router.get("/frame/playlist/status")
def frame_playlist_status(request: Request):
    """构建进度 + 当前落盘列表概况(配置页卡片轮询用)。"""
    with _build_lock:
        state = dict(_build_state)
    pl = load_playlist(_root(request))
    saved = None
    if pl:
        saved = {
            "theme": pl.get("theme"),
            "kind": pl.get("kind"),
            "count": len(pl.get("photo_ids") or []),
            "candidate_count": pl.get("candidate_count"),
            "verified_by_llm": pl.get("verified_by_llm"),
            "built_at": pl.get("built_at"),
        }
    return {"build": state, "saved": saved}
