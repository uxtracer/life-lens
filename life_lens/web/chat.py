"""Web Chat — 用户问相册问题,backend 跑两轮 LLM 调用:

  Round 1: 用户问题 → JSON {"action": ..., "args": ...}(LLM 选工具)
  Round 2: 把 Round 1 工具的查询结果喂回 LLM → 流式中文回答

LLM provider 走 web/llm.py(claude-p / openai-compat 双 backend,配置在 ~/.life_lens/config.json)。

SSE 协议:event 名 = phase / planned / result / chunk / done / error。

每次 chat 写一条 jsonl 到 ~/.life_lens/chat_log/YYYY-MM-DD.jsonl,
方便后续 grep 排查 "我刚才问 X 它返了 Y,怎么回事"。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..store import db, repo
from ..query import search as qsearch, aggregate as qagg
from . import llm as llm_mod

log = logging.getLogger(__name__)


def _log_chat(
    root: Path,
    question: str,
    provider_id: Optional[str],
    plan: Optional[dict],
    tool_result: Optional[dict],
    answer: str,
    photo_ids: list,
    error: Optional[str] = None,
) -> None:
    """写一行 jsonl 到 ~/.life_lens/chat_log/YYYY-MM-DD.jsonl。

    字段:
      ts        — UTC iso timestamp
      question  — 用户原话
      provider  — LLM provider id
      plan      — Round 1 LLM 出的 {action, args, rationale}
      result    — { summary, total/count, sampled_photo_ids[:10] }(精简,不存全部 raw)
      answer    — Round 2 LLM 完整输出文本(含 [photo:xxx] 标记)
      refs      — Round 2 引用的 photo_id 列表(已 dedup)
      error     — 异常文本(若有)
    """
    try:
        log_dir = root / "chat_log"
        log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.utcnow().strftime("%Y-%m-%d")
        fp = log_dir / f"{day}.jsonl"

        # 精简 tool_result,只留 summary 信息 + 前 10 个 photo_id 样本
        result_compact = None
        if tool_result:
            items = tool_result.get("items") or []
            places = tool_result.get("places") or []
            sample_ids = (
                [it.get("photo_id") for it in items[:10]]
                if items else
                [pid for pl in places[:10] for pid in (pl.get("sample_photo_ids") or [])[:1]]
            )
            result_compact = {
                "total":         tool_result.get("total"),
                "items_count":   len(items),
                "places_count":  len(places),
                "by_year_count": len(tool_result.get("by_year") or []),
                "sample_photo_ids": [s for s in sample_ids if s],
            }

        entry = {
            "ts":       datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "question": question,
            "provider": provider_id,
            "plan":     plan,
            "result":   result_compact,
            "answer":   answer,
            "refs":     photo_ids,
            "error":    error,
        }
        with fp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("chat log write failed: %s", e)

router = APIRouter()

ROUND1_SYSTEM_TEMPLATE = """你是一个相册查询调度器。用户会问一个关于他相册的问题。
你的工作是输出一个 JSON,告诉系统该调哪个工具拿数据。**只输出 JSON,不要其他任何文字。**

**当前事实**(基于这些判断时间窗,不要用训练知识猜"最近/今年"是哪一年):
- 今天日期:{today}
- 相册照片实际时间范围:{photo_time_range}

**模糊时间词解析约定**(用户没明示具体时长时遵守):
- "最近" / "这段时间" / "近期" → **time_from = today 往前推 6 个月**(宽松,确保有照片可选)
- "最近几天" / "这两天" → today 往前推 7 天
- "最近一个月" / "上个月" → today 往前推 30 天
- "最近一年" / "去年至今" → today 往前推 365 天
- "今年" → time_from = 今年 1-1,time_to = today
- "去年" → time_from = 去年 1-1,time_to = 去年 12-31
- 不写死具体年份,基于今天动态算

**搜不到时不要硬给 1-2 张**(time_from 卡太严容易命中很少)→ 优先放宽时间窗,
比如"最近"返 0-1 张时,LLM 可在 rationale 里说明并扩大到 1 年。

可用工具:

1. search_photos — 关键词 + 时间窗 + 人物 + 地点 过滤(最常用)
   args: {{ query?: 关键词中文(支持自然语言场景如"海边""美食""雪景",后端会同时跑 FTS + 语义向量检索),
           query_expansions?: [同义/近义/上下位扩词数组,0-5 个],
           time_from?: ISO日期如 "2024-01-01" (含),
           time_to?: ISO日期如 "2024-12-31" (含),
           persons?: [人名数组,如 ["小明"]],
           persons_mode?: "OR"(默认,任一在) | "AND"(都在同一张),
           location?: 城市/地点/国家关键词,
           favorite_only?: true | false(默认 false)。用户明说"我收藏的""收藏过的""标星/喜欢的"照片时传 true;可和其他条件组合(如收藏的某人照片),
           limit?: 默认 80,召回宽好挑;合影/聚会/某人某主题等"宽搜"场景可放到 150-200(系统硬上限 200) }}
   **query_expansions 重要**:dense embedding 对"短 query + 长 description"有弱点,
   query 是具体物品/装饰(裙子/墨镜/草帽)时关键词埋在长 description 里召回不准 — 你扩词补足字面命中。
   - 具体名词(人名/地名/品牌)→ **不扩**,留空数组 [](小明 / 阿那亚 已唯一,扩反引噪音)
   - 描述性物品 / 装饰 → 扩 3-5 个语义近义词:`"裙子" → ["连衣裙","长裙","短裙","裙摆"]`
   - 宽泛场景词 → 扩 2-4 个:`"美食" → ["食物","餐厅","吃的","菜肴"]` / `"雪景" → ["雪山","雪地","积雪"]`
   - 抽象情绪 → 扩 1-2 个:`"开心" → ["欢笑","微笑"]`
   - 不要扩成完全无关词(`美食` 不要扩 `餐厅装修` 这种远义)
   **persons_mode 重要**:
   - 默认 "OR"(任一在)适合"X 出现过的照片""X 去哪了"这类"X 是主角"问题
   - 看到"X 和 Y 的合影""X 跟 Y 一起""X 和 Y、Z 三人"这类**多人同框**问法 → 传 `persons_mode:"AND"`
   - AND 时同名多 cluster 自动折叠(小明 = 种子+InsightFace+Apple 等多条 cluster 在 db 里,AND 不会因此漏)
   **location 字段重要提示**:
   - 后端按 ≥2 字滑窗拆 + 多字段(country/province/city/district/township/aoi/poi/place_name)LIKE 任一命中
   - **传单一短地名比拼长串好**:用户问"金山岭阿那亚",传 `"阿那亚"` 或 `"金山岭"`(任选其一即可命中)
   - 别拼"金山岭阿那亚"这种长串,会因顺序问题降低命中

2. places_visited — 列出去过的地点(按频次降序,每地点附 3 张代表照片 id)
   args: {{ year?: 整数(可选,过滤某年),
           persons?: [人名数组,可选],
           persons_mode?: "OR"(默认,任一人出现的地方都算)| "AND"(三人都在同一张照片的地方) }}
   **用这个工具的场景**:用户问"X 去过哪些地方"/"X 最近去哪玩了" — 这类**地点聚合**
   问题用 places_visited(persons=[X]) 比 search_photos 好(后者会被 limit 截断,
   某地点照片多会霸占采样,看不到其他地点)。
   **persons_mode 用 AND 的场景**:用户问"X 和 Y 一起去过哪"/"X、Y、Z 共同去过哪"
   等明确"一起"语义 — 必传 persons_mode:"AND",否则会把任一人单独去过的地方算进来。

3. counts_by_year — 按年统计照片数
   args: {{}}

输出格式(必须是合法 JSON,不要 markdown 代码块):
{{ "action": "search_photos" 或其他, "args": {{...}}, "rationale": "为啥这么查(一句话)" }}

例子(都基于上面的"今天日期"算时间):
- 用户问 "去年和小明的合影" → time_from / time_to 取**今天日期的前一年** + 因为"和小明的合影"是多人同框 → 还要带"我"或具体那个"我"?如果用户没指"和谁",小明一个人 OR 即可;如果用户问"X 和 Y 的合影"必传 `persons_mode:"AND"`
- 用户问 "李丽跟张三的合影" → {{"action":"search_photos","args":{{"persons":["李丽","张三"],"persons_mode":"AND","limit":150}},"rationale":"两人同框,AND 语义,合影类放大 limit 保召回"}}
- 用户问 "李丽穿裙子的照片" → {{"action":"search_photos","args":{{"persons":["李丽"],"query":"裙子","query_expansions":["连衣裙","长裙","短裙","裙摆"]}},"rationale":"裙子是具体装饰物,扩词补足字面命中(LLM description 写'连衣裙'不写'裙子')"}}
- 用户问 "小明去过阿那亚吗" → {{"action":"search_photos","args":{{"persons":["小明"],"location":"阿那亚"}},"rationale":"按人物+地点查,不限时间"}}
- 用户问 "最近张三去哪玩了" → {{"action":"places_visited","args":{{"persons":["张三"]}},"rationale":"地点聚合,按人物筛选"}}(如果用户明确指了时间还可加 year 参数)
- 用户问 "张三和李四、赵阿姨一起去过哪" → {{"action":"places_visited","args":{{"persons":["张三","李四","赵阿姨"],"persons_mode":"AND"}},"rationale":"三人共同到过的地点(AND),不能用 OR 否则任一人单独去的地方会混进来"}}
- 用户问 "我去过哪些地方" → {{"action":"places_visited","args":{{}},"rationale":"按地点聚合"}}
- 用户问 "今年拍了多少照片" → 用今天日期的当前年算 time_from / time_to
- 用户问 "海边的照片" → {{"action":"search_photos","args":{{"query":"海边"}},"rationale":"场景类查询,hybrid 检索"}}(语义路径自动召回沙滩/海岸/海面等近义场景,即使 description 没出现"海边"二字)
- 用户问 "我收藏的照片" → {{"action":"search_photos","args":{{"favorite_only":true,"limit":150}},"rationale":"用户要看收藏,favorite_only 过滤;不限主题放大 limit"}}
- 用户问 "小明收藏的合影" → {{"action":"search_photos","args":{{"persons":["小明"],"favorite_only":true}},"rationale":"收藏+人物组合过滤"}}
"""


def _build_round1_system(conn) -> str:
    """动态拼 Round 1 prompt:注入今天日期 + db 照片时间范围。
    让 LLM 别基于训练 cutoff 猜"最近/今年/去年"。"""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        row = conn.execute(
            "SELECT MIN(json_extract(exif, '$.captured_at_local')) AS lo, "
            "       MAX(json_extract(exif, '$.captured_at_local')) AS hi "
            "FROM photos WHERE source != 'seed' AND vision IS NOT NULL"
        ).fetchone()
        lo = (row["lo"] or "")[:10] if row and row["lo"] else None
        hi = (row["hi"] or "")[:10] if row and row["hi"] else None
        time_range = f"{lo} ~ {hi}" if lo and hi else "(空库,没有 vision 完成的照片)"
    except Exception:
        time_range = "(查询失败)"
    return ROUND1_SYSTEM_TEMPLATE.format(today=today, photo_time_range=time_range)

ROUND2_SYSTEM = """你是用户的相册助手。系统已经根据用户的问题查到了一批照片(或聚合结果)。
请用简洁中文回答用户的问题,引用具体照片时**用 `[photo:photo_id]` 标记**(系统会渲染成缩略图)。

**关键铁律 — photo_id 必须原样复制**:
- 查询结果里 photo_id 是短编码(形如 `19D5A112-6` 约 10 字符,或 `d1303b8069`)
- **必须逐字符 copy 结果里给的 id,绝对不可自己编造、改写、补长、再截短**
- 只能引用查询结果里真实存在的 id — 凭"印象"写一个看起来合法的 id = 幻觉,前端会丢弃

风格:
- 直接给答案,不要"好的,根据查询结果..."这种开头
- 不要列原始 JSON,要把信息组织成可读叙述
- 引用照片时插入 [photo:完整id] 标记,前端会换成图
- 如果查询结果为空,直接说"没找到符合条件的照片"+ 简短原因(可能是时间窗太窄/人名拼错等)

**引用前必做的轻量校验**(防止 hybrid 召回宽误塞):
- 系统检索是宽召回(可能返几十张候选,不一定每张都真符合用户问题)
- 候选可能带 `match` 字段:`literal` = 关键词字面命中(强证据);`semantic` = 仅语义
  向量相近(**弱候选,大概率是凑数的**,可快速略读、不符合直接跳过)
- 引用每张照片**前**,内心快速读一遍它的 `description` + `tags` + `objects` + `scene`,
  判断**是否真符合用户问题的核心意图**
- 不符合 → 不引用(哪怕它进了候选列表)
- **全部不符合** → 当作"找不到",照"结果为空"那条处理(明说没找到 + 简短原因)
- 例:用户问"穿裙子",candidate description="穿外套" tags=["户外","旅行"] objects=["外套","帽子"] → 都不含裙装 → 不引用
- 反例:不要因为"被召回"就硬挑一张引用 — 召回宽是为了不漏,**精确率靠你把关**

**地点聚合的关键(places_visited 返回结果时)**:
返回的 places 是**高德 POI 细粒度**,每条带:
- `place`: POI 名(如"阿那亚金山岭上院")
- `formatted_address`: 完整地址,形如"河北省承德市滦平县涝洼镇 · 阿那亚金山岭 · 阿那亚金山岭上院"
- `aoi_name`: 景区/小区 AOI(可能 null,因为高德 AOI 多边形不一定覆盖所有点)
- `city`: 城市
- `count`, `sample_photo_ids`

**用 formatted_address 判断地理归属**(POI 名字相似不够保险):
- 看 formatted_address 中间段(AOI 段)相同 → 归到同一目的地
- 例:几个 POI 的 formatted 都是 "河北...涝洼镇 · 阿那亚金山岭 · XXX" → 都属"阿那亚金山岭"目的地
- 例:某 POI 的 formatted 是 "北京...怀柔区 · 故宫北门" → 这是另一个目的地,不要混

**回答示例**(对"张三去过哪"问题):
- 正确:"张三最近去了**阿那亚金山岭**(河北承德金山岭一带),在景区里的上院、景观泳池、音乐厅、停车场、老道洞等多个点位都拍了照片 [photo:xxx] [photo:yyy]"
- 错误:"张三去了上院(7 张)、景观泳池(1 张)、音乐厅(2 张)..." — 太碎,没传达"目的地"这个核心
- 引用照片时,每个聚合后的目的地引用 1-3 张代表 [photo:xxx]
"""


def _llm_run(system: str, user_text: str, stream: bool = False,
             provider_id: Optional[str] = None) -> Iterator[str]:
    """调统一 LLM 抽象层。provider_id=None 走 config 里的 default。"""
    yield from llm_mod.call_llm(system, user_text, stream=stream, provider_id=provider_id)


def _extract_json(text: str) -> Optional[dict]:
    """从 LLM 输出抠 JSON。容忍 ```json fence 和文本环绕。"""
    # 1) 直接尝试
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2) ```json fence
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 3) 第一个 { 到最后一个 }
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(text[s:e+1])
        except Exception:
            pass
    return None


_LIMIT_HARD_CAP = 200   # Round 2 LLM context 保护:每条 item 约 200-400 token,200 张 ≈ 40-80k 安全在主流 64k+ 模型内

_SHORT_ID_BASE_LEN = 10   # Round 2 prompt 里 photo_id 的短前缀长度(碰撞自动加长)


def _short_id_map(full_ids: list) -> dict:
    """full_id → 短前缀映射(基础 10 位,结果集内碰撞则整体加长)。

    给 Round 2 prompt 瘦身用:36 字符 Apple uuid ≈ 15 token/个,候选 150 条 + LLM 回答
    复述 ≈ 数千 token 纯浪费。前端 _fixPhotoId 本来就按 candidates 做前缀唯一补全,
    短 id 直接复用该机制;服务端 done 帧另行扩回完整 id(_expand_photo_ids)。
    """
    uniq = [fid for fid in dict.fromkeys(full_ids) if fid]
    n = _SHORT_ID_BASE_LEN
    while True:
        shorts = {fid: fid[:n] for fid in uniq}
        if len(set(shorts.values())) == len(shorts):
            return shorts
        n += 4
        if n >= max((len(f) for f in uniq), default=0):
            return {fid: fid for fid in uniq}


def _collect_full_ids(tool_result: dict) -> list:
    ids = [it.get("photo_id") for it in (tool_result.get("items") or [])]
    for pl in tool_result.get("places") or []:
        ids.extend(pl.get("sample_photo_ids") or [])
    return [i for i in ids if i]


def _slim_for_llm(tool_result: dict, id_map: dict) -> dict:
    """给 Round 2 prompt 的瘦身副本(SSE result 帧 / chat_log 仍用完整 raw):
    - 去空字段(null / "" / [] / false)— LLM 不需要看 "scene": null
    - photo_id / sample_photo_ids 换短前缀
    其余结构原样,不截 description(它是"引用前轻量校验"的依据)。
    """
    def slim_dict(d: dict) -> dict:
        out = {}
        for k, v in d.items():
            if v is None or v == "" or v == [] or v is False:
                continue
            if k == "photo_id":
                v = id_map.get(v, v)
            elif k == "sample_photo_ids":
                v = [id_map.get(i, i) for i in v]
            out[k] = v
        return out

    slimmed = dict(tool_result)
    if tool_result.get("items"):
        slimmed["items"] = [slim_dict(it) for it in tool_result["items"]]
    if tool_result.get("places"):
        slimmed["places"] = [slim_dict(pl) for pl in tool_result["places"]]
    return slimmed


def _expand_photo_ids(short_ids: list, full_ids: list, id_map: dict) -> list:
    """LLM 回答里抠出的(短)id → 完整 id。顺序:短映射反查 → 完整 id 直接命中 →
    唯一前缀匹配 → 原样保留(前端 _fixPhotoId 仍有最后一道兜底)。"""
    rev = {s: f for f, s in id_map.items()}
    full_set = set(full_ids)
    out = []
    for sid in short_ids:
        if sid in rev:
            out.append(rev[sid])
            continue
        if sid in full_set:
            out.append(sid)
            continue
        hits = [f for f in full_set if f.startswith(sid)]
        out.append(hits[0] if len(hits) == 1 else sid)
    return list(dict.fromkeys(out))

def _dispatch(conn, action: str, args: dict) -> dict:
    """执行工具调用,返回结果 dict(只含与回答相关的精简字段)。"""
    if action == "search_photos":
        clean = {k: v for k, v in args.items() if v is not None}
        if clean.get("limit") and clean["limit"] > _LIMIT_HARD_CAP:
            clean["limit"] = _LIMIT_HARD_CAP
        return qsearch.search_photos(conn, **clean)
    if action == "places_visited":
        return qagg.places_visited(
            conn,
            year=args.get("year"),
            persons=args.get("persons"),
            persons_mode=args.get("persons_mode", "OR"),
        )
    if action == "counts_by_year":
        return qagg.counts_by_year(conn)
    raise ValueError(f"未知工具: {action}")


def _sse(event: str, data) -> str:
    """格式化 SSE 帧:event 名 + JSON data。"""
    payload = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
    return f"event: {event}\ndata: {payload}\n\n"


@router.get("/llm-info")
def llm_info(provider_id: Optional[str] = None):
    """单个 provider 的 public 描述。带 ?provider_id=xxx 查具体某个。"""
    return llm_mod.get_provider_info(provider_id)


@router.get("/llm-providers")
def llm_providers():
    """全部已配置的 LLM provider 列表 + 默认(不含 api_key)。"""
    return llm_mod.list_providers_public()


@router.post("/chat")
def chat(request: Request, body: dict = Body(...)) -> StreamingResponse:
    """SSE 流式 chat。body = { "question": "..." }。

    事件序列:
      phase   { "stage": "planning|executing|answering" }
      planned { "action": "...", "args": {...}, "rationale": "..." }
      result  { "summary": "...简述查到了什么..." }
      chunk   "<增量文本>"
      done    { "photo_ids": [...] }
      error   "<错误信息>"
    """
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "缺少 question")
    provider_id = body.get("provider_id")     # None → 用 default

    root = request.app.state.root

    def gen():
        try:
            # ---- Round 1: 选工具(prompt 注入今天日期 + db 时间范围)----
            yield _sse("phase", {"stage": "planning"})
            conn_for_prompt = db.connect(db.get_db_path(root))
            try:
                round1_system = _build_round1_system(conn_for_prompt)
            finally:
                conn_for_prompt.close()
            r1_text = "".join(_llm_run(round1_system, question, stream=False, provider_id=provider_id))
            plan = _extract_json(r1_text)
            if not plan or "action" not in plan:
                yield _sse("error", f"无法从 LLM 输出抠出工具 JSON: {r1_text[:300]}")
                return
            yield _sse("planned", {
                "action":    plan.get("action"),
                "args":      plan.get("args", {}),
                "rationale": plan.get("rationale", ""),
            })

            # ---- 执行工具 ----
            yield _sse("phase", {"stage": "executing"})
            conn = db.connect(db.get_db_path(root))
            try:
                tool_result = _dispatch(conn, plan["action"], plan.get("args") or {})
            finally:
                conn.close()

            # 给前端一个 summary(精简,不要把全部 raw 倒出去)
            if plan["action"] == "search_photos":
                summary = f"搜到 {tool_result.get('total', 0)} 张,显示前 {len(tool_result.get('items', []))} 张"
            elif plan["action"] == "places_visited":
                summary = f"去过 {len(tool_result.get('places', []))} 个地点"
            elif plan["action"] == "counts_by_year":
                summary = f"覆盖 {len(tool_result.get('by_year', []))} 年"
            else:
                summary = "已查询"
            yield _sse("result", {"summary": summary, "raw": tool_result})

            # ---- Round 2: 出回答 ----
            yield _sse("phase", {"stage": "answering"})
            # 瘦身副本只进 LLM prompt(compact dumps + 去空字段 + 短 id),
            # SSE result 帧 / chat_log 仍是完整 raw — 前端缩略图/候选补全不受影响
            full_ids = _collect_full_ids(tool_result)
            id_map = _short_id_map(full_ids)
            slim_result = _slim_for_llm(tool_result, id_map)
            r2_prompt = (
                f"用户问题: {question}\n\n"
                f"你调用了 {plan['action']}({json.dumps(plan.get('args') or {}, ensure_ascii=False)})\n\n"
                f"查询结果:\n```json\n{json.dumps(slim_result, ensure_ascii=False, separators=(',', ':'))}\n```\n\n"
                "请基于以上结果用中文回答用户。引用具体照片时插入 [photo:photo_id] 标记。"
            )
            answer_buf = []
            for chunk in _llm_run(ROUND2_SYSTEM, r2_prompt, stream=True, provider_id=provider_id):
                answer_buf.append(chunk)
                yield _sse("chunk", chunk)

            # 抠出回答里被 [photo:xxx] 引用的(短)id,扩回完整 id 再发 done 帧 —
            # 前端"未引用候选"折叠区按完整 id 比对,直接发短 id 会全判未引用。
            # 注意:Apple uuid 含短横,正则要带 '-'(filesystem hash 没短横也匹配)
            full = "".join(answer_buf)
            cited_short = list(dict.fromkeys(re.findall(r"\[photo:([A-Za-z0-9_-]+)\]", full)))
            photo_ids = _expand_photo_ids(cited_short, full_ids, id_map)
            yield _sse("done", {"photo_ids": photo_ids})
            _log_chat(root, question, provider_id, plan, tool_result, full, photo_ids, error=None)
        except Exception as e:
            log.exception("chat failed")
            yield _sse("error", f"{type(e).__name__}: {e}")
            try:
                _log_chat(root, question, provider_id, locals().get("plan"),
                          locals().get("tool_result"), "".join(locals().get("answer_buf") or []),
                          [], error=f"{type(e).__name__}: {e}")
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream")
