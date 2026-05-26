# life_lens

## 一句话定位

把整本相册(几万到几十万张)用本地视觉模型转成结构化 JSON 文本,让 LLM 后续能查询、能基于它写"用户的生活"。**照片字节完全本地**(Ollama + qwen3-vl:8b),GPS 数字 / 用户问题文本可外发(地图 API / claude -p)。

兄弟项目都是 markdown wiki + `claude -p`,本项目**不走 markdown** — 几十万行只有 SQLite + sidecar JSON 才扛得住。

## 当前状态

**Phase 5 已完成 + Phase 6 起步**(2026-05,公开仓 https://github.com/uxtracer/life-lens)

主线 scan 持续跑(用户 27888 张 Apple Photos,~24s/张,合盖待机几天扫完)。Phase 2-4 全部稳定;Phase 5 完成开源发布(5-tab UI / RESTful 统一 / install.sh / config 跟 `--root`);Phase 6 语义索引 inline 化(主扫描 + reprocess vision 每张顺手 embed),消除"新照片只能 FTS 命中"的隐式坑。

## Phase 路线图

- **Phase 0-1** ✅:骨架 + Ollama vision + InsightFace + 种子人物 + FTS5 + 高德 reverse + Web Chat
- **Phase 2** ✅:**生产化** — jobs db 驱动 + Run 历史 + graceful stop / WGS-84→GCJ-02 50m 网格 + 配额 / vision prompt v9.2 + self-check / 评测集 11 张 / FTS5 trigram / Web UI Scan+Runs+Reprocess
- **Phase 3** ✅:Apple Photos source + iCloud 备份脚本 + cities 地图;**未做**:sidecar JSON 导出、MCP server(Py ≥ 3.10)、视频
- **Phase 4** ✅:语义向量(bge-small-zh + fastembed) + hybrid + RRF + LLM query expansion + chat jsonl 日志
- **Phase 5** ✅:开源发布(脱敏 + db schema_version 自动备份 + A 层 pytest + publish.sh + 5-tab UI + RESTful 统一砍 claude-p + install.sh 一行 + config 跟 `--root`)
- **Phase 6** 🟢:语义索引 inline 化 + Web 端补建/全量重建兜底 + `lens update` 子命令 + install.sh 端口占用先 kill 再启

## 推荐工作流(重要)

**先建种子人物再扫描** — 不要反过来。pipeline 顺序是 face → vision,vision 两次调用都注入 set-of-mark,description 一开始就用真名。先扫再补种子需要手动 reprocess vision(~22s/张)。

GUI 流:配置 → 面孔(添加种子) → 扫描 → 浏览。

## 架构铁律

### LLM 产物 vs 派生产物 分层

| group | 谁产 | 改起来 |
|---|---|---|
| `vision` | LLM(5-15s/张) | **贵** — 改 prompt 全库重跑要几天 |
| `derived` | Python 规则 | **便宜** — 几分钟跑完几十万张 |

**任何新分类 / 枚举 / 桶化字段一律放 `derived/`。不要让 LLM 多产字段**。LLM 只产原始素材(description / scene / objects / tags / ocr_text / mood + actions)。

### LLM 不写 SQL,不操作连接

Web Chat 两轮 LLM:Round 1 出 `{action, args}`,Python `_dispatch` 调 `query/`;Round 2 流式回答 + `[photo:xxx]`。LLM 只填白名单字段(`query / time_from / time_to / persons / location / persons_mode / query_expansions`)。任何"让 LLM 直接出 SQL"或"工具开放任意字段"的诱惑都先拒掉。

### 拆调用 > 长 prompt

8B 小模型 instruction following 弱。把 6 条铁律 + JSON schema + set-of-mark + 人名约束塞 1500 字单 prompt → LLM 会忽略人名约束。**拆调用**(`describe_description` + `describe_struct` 各 300-500 字)实测只多 10-20% 时间(image token KV cache 命中),质量大幅提升。新加字段先想清楚是放进现有 prompt 还是再拆一次。

### 依赖方向严格单向

- 查询:`web (api / chat) → query → store/repo` + `geocode` 模块独立
- 写入:`web / cli → scanner → {sources, preprocess, vision, faces, exif, geocode, store}`
- 所有 adapter 只依赖 `schema` 和 `store`

## 技术栈硬约束

- **Python 3.9 兼容**(用户机器系统 `/usr/bin/python3 = 3.9.6`)。所有新文件必须 `from __future__ import annotations`,不要在运行时表达式里写 `X | Y`(类型注解可以)
- **venv 叫 `.venv_lens`**(不和兄弟项目冲突)
- **不要装新框架**。前端原生 HTML + vanilla JS,**不引 React / Vue / Tailwind**
- **LLM 文本调用**:用户只有 Claude Max plan,**没有 anthropic API key**。统一走 `web/llm.py` 的 `openai-compat`(DeepSeek 默认)。Phase 5 已删 `claude-p` kind,**绝不 `import anthropic`**
- **LLM 视觉调用**:走本地 Ollama HTTP,目标 `qwen3-vl:8b-instruct`。**入参必须是预处理后的 1024px JPEG**,原 HEIC / 大图喂模型会失败
- **中文文档不加空格**:中文紧挨英文/数字,不加盘古之白(全局规则)
- **所有 `read_text()` 显式 `encoding="utf-8"`**(Windows 默认 cp1252/gbk 会炸)

## 隐私边界

| 数据 | 能否外发 | 谁接 |
|---|---|---|
| 原图字节 | ❌ 永远本地 | — |
| 照片描述 / vision JSON | ❌ 永远本地 | — |
| GPS 数字(裸 lat/lng) | ✅ | 高德 reverse geocoding |
| 用户问题文本 | ✅ | LLM(用户自己同意) |
| 查询返回的精简结果 | ✅ | Round 2 prompt(发描述文本 + photo_id,**不发图本身**) |

判断标准:**外发数据能否让接收方还原"这是哪张照片"**?GPS 单独发不行,但 GPS + 时间 + 缩略图组合就行 → 所以缩略图字节不能外发。

## 不要做的事

- **不要自动 commit**(全局 `~/.claude/CLAUDE.md` 已规定)。用户说"备份" / "提交" / "commit" 再做
- **不要为不存在的需求加抽象**。新 vision adapter 要加 MLX-VLM 时再写,不要预先 stub
- **不要把视频纳入主流程**。`sources/filesystem.py` 扩展名白名单只含图片
- **不要把 description 当严格事实源**。改名后 description 残留旧名 — 设计意图,结构化查询走 `people.persons[]`(查询时 join `persons` 表拿最新名)
- **不要绕过 `query/` 直接写 SQL**。否则 Web Chat / REST / CLI 三个入口行为会漂移
- **不要发原图字节到外部**(见上表)
- **不要在 ai_invest 风格上加 mcp 依赖**。MCP SDK 要 Python ≥ 3.10,本机 3.9
- **不要在代码 / prompt 例子 / 文档教训里用真名 / 真实小景点 / 真实生活轨迹**。所有 person/place 例子用占位符(张三 / 小明 / 王女士 / 李四 / 赵阿姨 / 小华 / 李丽;某园区 / 某商场)。公共景区(阿那亚 / 敦煌 / 雅丹 / 青海)和知名地标(外滩 / 长城)可保留。**理由:定期 sync 到公开 GitHub 仓库 (uxtracer/life-lens),真名是定时炸弹**。`scripts/publish.sh` 有 grep 兜底,但**那只是 last line of defense**

## 踩过的坑(仍有警告价值)

### Ollama num_ctx 默认 2048 会静默截断 ⚠️

图片 ~800 image tokens + prompt ~500-1000 tokens + `num_predict=1024` → 总计 4-5k,远超默认。后果:prompt 开头的 set-of-mark mapping 会被切掉。`vision/ollama.py` 显式 `num_ctx=16384`。

### set-of-mark prompting 注意事项

- LLM 能识别红框 + `[N]` 编号(实测 qwen3-vl 8b)
- prompt 必须说"ocr_text 不要包含 [N] 编号",否则会污染
- 不要在 prompt 末尾说"不要描述方框/编号本身",会让 LLM 过度执行直接忽略红框
- struct prompt 不需要"必须用真名"约束(name 字段后端用 cluster_id 组装),只需要 actions 按编号顺序;**actions 是 dict 不是 array**(`{"1": "蹲坐", "2": "举手机"}`),v9.1 起锁死红框编号防错位

### 跨年龄人脸用 max-pooling 而非 mean centroid ⚠️

同一 person 跨年龄/光照,mean centroid 会拉偏("四不像"); max-pooling 保留每张种子作为独立判据,新脸总能匹到最相近的那张。**不要改回 centroid**。

### InsightFace age/gender 对儿童不准

正脸成人挺准,儿童(< 14 岁)经常错(11 岁男孩可能估成 23 岁女)。对策:prompt 标"参考估计(可能不准,以视觉为准)"作弱 hint,**不依赖**它做关键决策。真正锁定 [N]→人 的是 **bbox 位置 hint(9 宫格,100% 准)**。

### iPhone EXIF 没 OffsetTime → captured_at_utc 是空字符串 ⚠️

iPhone(尤其早期 iOS)经常没 OffsetTimeOriginal,`exif/extract.py` 没 OffsetTime 时 `captured_at_utc` 留空。**症状**:`WHERE captured_at_utc >= ?` 把所有照片筛掉。**当前修复**:`COALESCE(NULLIF(captured_at_utc,''), captured_at_local)`。**根治 TODO**:有 GPS 时按经度估时区。

### 高德 reverse geocoding 设计要点

- key 放 `~/.life_lens/config.json`(`chmod 600`)或 env `AMAP_KEY`,**不进 git**
- **必须 WGS-84 → GCJ-02 转换**(`amap.py::wgs84_to_gcj02`):iPhone EXIF 是 WGS-84,高德用 GCJ-02,中国境内偏 **300-700m**,小景点(几十米直径的音乐厅 / 寺庙)被偏到几百米外丢 POI。国境外 `_out_of_china` 自动跳过
- **50m 网格缓存**(`GRID_STEP_DEG=0.0005`),`provider='amap-gcj02'` 做版本隔离
- **`place_name` / `formatted_address` 不入缓存,出口 `_derive_place_and_formatted` 实时拼**(改地点策略不必失效缓存)
- **POI 取距离最近**(`min(pois, key=distance)`),高德默认按权重(主景区名)排,不是距离
- **直辖市坑**:北京/上海/天津/重庆的 `addressComponent.city` 是空数组,fallback 到 province
- **配额管理**:`amap_quota` 表按 Asia/Shanghai 日期记 count,免费 5000/天上限设 4800(buffer 200)。耗尽时 `is_quota_exhausted` 阻止 HTTP,runner 自动暂停 run

### FTS5 `unicode61` 不索引中文 ⚠️

SQLite FTS5 默认 `unicode61 remove_diacritics 2` **完全不索引中文**(`MATCH '观光车'` 返 0 张)。改 `tokenize='trigram'`(SQLite 3.34+ 内置),db.py 检测旧 schema 自动 DROP + CREATE + 回填。**限制:搜索词 ≥ 3 字符**(2 字"长城"匹配不到 trigram),`search.py` query 短词 fallback LIKE。

### scan_runs snapshot 列防 reprocess 偷 jobs

reprocess 把 jobs 行的 `run_id` 改成新 run_id 让进度可查;副作用是原 run 的 jobs 被偷走后实时查 `time_range / scanned_up_to` 全 null,历史 run 进度卡空白。对策:`scan_runs` 加 `snapshot_captured_min / max / scanned_up_to` 三快照列,enqueue 后一次性冻结 + 每张完成时增量推进。

### self-check 字面匹配的固有局限

`vision/role_check.py` bigram + char 字面匹配 description 邻域。**对同义表达无能为力**(struct="举手机自拍" vs description="面带笑容" 误报)。前端 banner **降级浅蓝**(不是橙黄)+ "已核对忽略" 按钮 → `meta.errors.acknowledged=true`,重跑 vision 时清除。要彻底消除得换 LLM-based self-check,贵,**当前不做**。

### LLM 偏好截短 photo_id 到 8 位

部分模型(非顶级)截 git-short-hash 风格 → `<img src="/api/thumb/19D5A112">` 404。**两层防御**:
1. ROUND2_SYSTEM 写"photo_id 必须完整复制" + 示例 — 减少但不消除
2. **前端 candidates 前缀补全**(`app.js::_fixPhotoId`):取 result 帧的所有 candidate id 做集合,LLM 写的 id 不在 → 前缀唯一匹配自动补全

**铁律:只靠 prompt 教不稳,关键不变量必须程序层守住**。

### LLM 还会**伪造**完整格式合法的 UUID(脑补) ⚠️⚠️

比截短更隐蔽:LLM 看到几十条 36 字符 Apple uuid → 脑补出新的、格式合法、db 里不存在的 uuid。`_fixPhotoId` 救不了(没法把假 id 改回真 id)。

**对策**:`_fixPhotoId` 返三态(真 id / 原 id 兜底 / `null` 视作幻觉),`renderAssistantBody` 用 `.filter(Boolean)` 把 null 丢掉(不画红框感叹号,静默消失)。**连带 bug**:`renderAssistantBody(div, buf)` 在 chunk 流式时漏传 `candidateIds` → 前缀补全 short-circuit 没机会跑。**任何"程序层兜底"必须验证调用现场真的传了参数**。

### `time_to` day-only 字典序坑 ⚠️

LLM 传 `time_to="2026-05-04"` 期望含整天,但 SQL 字符串字典序:`'2026-05-04T12:42:24' <= '2026-05-04'` → **False**(T ASCII 0x54 > '\0'),5/4 当天全被排除。

**修法**:`_build_extra_filters` 检测 `time_to` 长度=10 时自动补 `T23:59:59`。`time_from` 同样情况不用补(`>=` 字典序行为正确)。

### dense embedding short-query vs long-doc 弱点 ⚠️

bge-small-zh 把整段 200 字 description 压成 512 维。短 query("裙子" 2 字)跟长 doc 余弦只 ~0.40,关键词在 doc 占 3 字会被稀释。

**两层修复**:
1. `build_source_text` 加 **objects 字段**(LLM 写的精炼物品清单,关键词浓缩)
2. **LLM query expansion**: Round 1 LLM 主动扩词(`"裙子" → ["连衣裙","长裙","短裙"]`),FTS 路径 `LIKE %连衣裙%` 100% 字面命中

教训:**dense + sparse 互补是标准做法**(dense 解语义,sparse 解字面)。

### LLM 优秀行为靠运气,要 prompt 显式化

某 case 用户问"小华穿裙子" 召回 117 张(都含小华但 0 张穿裙子),LLM 诚实说"没找到" — 但**靠运气**。修法:ROUND2_SYSTEM 加"引用前轻量校验 description+tags+objects+scene,不符合不引用,全部不符合按结果空处理" — 把涌现行为变成必选项。

### Apple Photos `face_info=[]` 偶有漏识别

Apple Photos.app 后台脸识别有时漏检。老实现是 `face_info=[]` 时跳过 InsightFace(信任 Apple),**改成返 `None` → fallback InsightFace 兜底**。代价每张多 0.3-1s,值得。已扫数据用 SQL 反查 `face_count=0 AND vision.subject IN (single,portrait,group)` 批量 reprocess(memory `project_life_lens_apple_face_misses`)。

### 选 `.photoslibrary` 包时 AppleScript 返回尾斜杠 → 误走 filesystem ⚠️⚠️

Web「选 Photos 库」按钮走 `pick_folder(mode=photos_library)` → osascript `choose file` 选 `.photoslibrary`。**`.photoslibrary` 是 macOS package(目录),`POSIX path of` 它会带尾斜杠**(`/Users/.../X.photoslibrary/`)。前端 `app.js` 原来用 `path.endsWith('.photoslibrary')` 判 kind → 带斜杠时为 **false** → 错走 `filesystem` source。后果极隐蔽:filesystem walker **钻进包内部**,把 `originals/` + `resources/derivatives/`(缩略图/裁剪衍生 `_1_105_c`)全当独立照片扫,同张照片重复多份、几乎 0 脸、且 filesystem source **永远拿不到 Apple 命名的脸**(那是 photos_library source 专属)。后端 `add_source` 的 `resolve()` 又把斜杠抹了,存进库的路径看着没问题,kind 却已经错了 → debug 时极易被误导。

**修复(双保险)**:前端判 kind 前 `path.replace(/\/+$/, '')`;后端 `pick_folder` 出口 `path.rstrip("/") or "/"`。**教训**:凡按后缀判类型的地方,先归一化尾斜杠;package 路径和普通目录路径行为不一致。

### fastembed 默认缓存在临时目录,会被 macOS 定期清 ⚠️

fastembed 0.7.4 不传 `cache_dir` 时,模型(bge-small-zh ~95MB)落在 `tempfile.gettempdir()/fastembed_cache`,macOS 上是 `/var/folders/.../T/` —— **系统会定期 purge**(重启 / 闲置几天)。症状:隔几天再扫,第一张照片卡 3-4 分钟"下载模型",其实是临时缓存被清后重下。`embedder.py` 显式传 `cache_dir=<root>/.cache/fastembed/`(和 preprocessed 缓存同级,持久)。**注意代码老注释曾写"下到 ~/.cache/fastembed/",是错的**(那目录从不存在)。

### MCP Python ≥ 3.10,本机 3.9

`pip install mcp` 报 `Requires-Python >=3.10`。**当前**:web chat 走手写两轮 `claude -p` tool loop(`web/chat.py`),不依赖 MCP。等 Python 升级再补。

## 数据布局

- `~/.life_lens/lens.db` — SQLite WAL 主库(`LIFE_LENS_ROOT` env 可改根)
- `~/.life_lens/.cache/preprocessed/{photo_id}.jpg` — 1024px JPEG 缓存,**永远复用**(重扫不重转、换 prompt 不重转、换模型不重转)
- `~/.life_lens/config.json` — `chmod 600`,**不进 git**;Phase 5 `store/config.py` 统一读写
- `~/.life_lens/chat_log/YYYY-MM-DD.jsonl` — 每次 chat 一条 jsonl(question/plan/result/answer/refs/error)
- 原图保留在用户自己目录,db 只存绝对路径
- `sample/` 是私人测试照片(已 gitignore),**所有处理用 `sample/` 跑烟测**

## JSON 记录分组(schema_version 0.1)

顶层 6 group:`identity / exif / vision / people / derived / meta`。详细字段以 `store/schema.sql` 为准。要点:

- `identity.photo_id` = fs `content_hash[:16]` 或 apple `uuid`
- `vision.*` 是 LLM 死字符串,改名后**残留旧名,接受**
- `people.persons[].cluster_id` 是锚,name 查询时 join `persons` 表实时 resolve
- `people.names[]` 是派生去重列表,SQL 查询方便
- `derived.location_bucket` 走 amap reverse,失败 graceful 全 null
- `meta.errors[]` 装 self-check / vision_role_mismatch,带 `acknowledged` 字段

## Vision Prompt v9.2 要点

**为什么拆**:8B 小模型单次长 prompt 注意力分散。两次调用 image-tokens KV cache 命中,只多 10-20% 时间。

**v9.2 增量**:每个 `[N]` 编号注入位置 hint(face bbox → 9 宫格,100% 准)+ age/gender 弱辅助(标"参考估计")。评测 baseline v9.1 8/11 → v9.2 11/11 PASS。

**Call 1 DESCRIPTION_PROMPT**(`v9.2-desc-2026-05-position-hint`):set-of-mark 图 + `[1]=张三(位置:下中;参考估计:约 11 岁、男(可能不准))` + 末尾"先逐一对照红框真实位置和给的标签,确认 [N]→人映射,再开始写" → `{"description": "..."}` 80-200 字。

**Call 2 STRUCT_PROMPT**(`v9.2-struct-2026-05-position-hint`):同样图 + 位置 hint → `{ media_type, subject, scene, objects, tags, ocr_text, mood, actions{} }`,**actions 是 dict** `{"1": "...", "2": "..."}`。

**Ollama 参数**:`num_ctx=16384`、`format=json`、`temperature=0.2`、视觉并发上限=1。

**描述风格**(给未来 prompt 调整者):无套话("这张照片展示了...")、无推测("似乎/可能/像是")、无 markdown(`**场景**:...`)、不用方位代号(已识别就用真名)。

## LLM 访问层

**Web Chat**(主入口,`web/chat.py`):`POST /api/chat` SSE,两轮 LLM(`web/llm.py` 抽象,Phase 5 只剩 `openai-compat` 一个 kind)。Round 1 出 JSON `{action, args, rationale}`,action ∈ `search_photos / places_visited / counts_by_year`;Python `_dispatch` 调 `query/`;Round 2 流式中文回答 + `[photo:xxx]`。SSE 帧:`phase / planned / result / chunk / done / error`。

**LLM provider 配置**(`~/.life_lens/config.json`):

```jsonc
{
  "amap_key": "...",
  "llm": {
    "default": "deepseek",
    "providers": {
      "deepseek": { "kind": "openai-compat", "model": "deepseek-v4-flash",
                    "api_key": "sk-...", "base_url": "https://api.deepseek.com/v1",
                    "label": "DeepSeek (快+便宜)" }
    }
  }
}
```

热读取(每次调用读 JSON),改 config 不需要重启。旧 `kind="claude-p"` 启动时忽略 + WARN。Env `LIFE_LENS_LLM_PROVIDER/MODEL/API_KEY/BASE_URL` 可临时覆盖。

**query/ 共享层**(三轨共用):`search.search_photos(query, time_from, time_to, persons[], persons_mode, location, query_expansions, limit)` hybrid FTS5+sem RRF / `aggregate.places_visited(year?)` / `aggregate.counts_by_year()`。

**MCP server**:推迟(Py ≥ 3.10)。

## CLI

```
lens                          # 默认 = 启动 web + 开浏览器
lens scan <path>              # 默认续传,db 驱动断点恢复
   --retry-failed              # failed → pending 再跑
   --enqueue-only              # 只 Phase A 入队不处理
   --no-vision                 # 跳过 vision(只 exif+face+derived)
lens status [--jobs]
lens init / backup
lens reprocess --group faces   # CLI 只接 faces,Web /api/reprocess 支持 faces/vision/derived
lens update                    # git pull + pip install + kill 旧 server + 起新(端口被占先 kill)
```

argparse,**不要换 click/typer**。**主操作入口是 Web 端**,CLI 兜底。

## 增量扫描机制(Phase 2 db 驱动两阶段)

- Photo identity = `sha1(size || mtime_ns || first_64KB || last_64KB)`(避免整文件 SHA1);Apple Photos 用 `PhotoInfo.uuid`
- **Phase A 入队**:`source.iter_photos()` 流式 → `identity.content_hash` 判重(已 done 同 hash 跳过) → `exif.peek_captured_at` 拿拍照时间 → `enqueue_job(run_id, captured_at_local)`
- **Phase B 处理**:**单线程**主循环(vision/face 有 lock,workers>1 增益 ~5%,换 graceful 控制 + db 一致性更划算)→ `get_pending ORDER BY captured_at_local ASC` → `pipeline.process_one` → 状态机推进
- jobs 状态机:`pending → processing → done | failed`,失败 `retry_count<3` 重置 pending,>=3 永久 failed
- **断点续传**:server 启动 `reset_stuck_jobs`(processing → pending)+ `mark_running_as_stopped`(scan_runs.status='running' 但进程没了的标 stopped)
- **scan_runs.done/failed 增量** `inc_run_done` / `inc_run_failed`,resume 不重置
- **graceful stop**:`progress.stop_flag.set()` 当前张跑完后退出。Web `/api/scan/stop`,CLI SIGINT
- **高德 quota 自动暂停**:每张完成后 `is_quota_exhausted(conn)` → set stop_flag,note 写"次日 UTC+8 0:00 重置"

## 人脸 / 种子人物

- **检测 + embedding**:InsightFace buffalo_l(ONNX, 512 维, L2 归一化)+ age/gender(genderage.onnx)
- **聚类**:`faces/cluster.py` max-pooling 新脸 vs cluster 内**每个 face embedding** 取 max(非 centroid 平均),阈值默认 0.5
- **种子人物**:GUI 上传(`/api/seed-persons`),写 `photos.source='seed'` + faces + persons 命名。**种子图不入主库**(所有 list/browse 接口过滤 `source != 'seed'`)
- **改名透明**:`people.persons[].cluster_id` 锚定,查询时 join `persons` 表 resolve 真名,改名不重跑 vision。`vision.description` 死字符串接受不一致
- **Apple 命名映射**:source 解 `face_info.name` → `cluster_id='apple:<name>'`,Apple 检测未命名 → `apple_face:<uuid>`(在 life_lens 命名无意义,引导回 Photos.app)

### 两种重跑

| 命令 | 干什么 | 耗时(10000 张) |
|---|---|---|
| `rematch_faces`(quick) | 不跑 detect,用已有 embedding 重新算 max-pooling,把主库脸吸到已命名 anchor | 秒级到几十秒 |
| `reprocess_faces`(full) | 重跑 InsightFace detect + assign | 几小时 |
| `reprocess_vision_for(photo_ids)` | 指定 photo 重跑 vision(两次调用),prompt 注入当前最新人名 → description 用真名 | 每张 ~40s |

## 测试节奏

**两层**:A 层(管道,改任何代码必跑,无 LLM)/ B 层(AI 质量,改 prompt 或 vision 模型必跑,需 Ollama)。

```bash
source .venv_lens/bin/activate

# A 层 pytest (~3s, CI 友好)
pytest tests/ -v
# 检查项:db migration / query (FTS+persons) / API contract / 隐私 grep

# B 层评测集回归(改 prompt / vision 模型必跑) — 11 张人工 ground truth
python tests/eval/run_eval.py                  # 全部
python tests/eval/run_eval.py --case IMG_0685  # 单张
# 目标:11/11 PASS(baseline v9.1 是 8/11,v9.2 是 11/11)

# 烟测:小样本目录
lens scan ~/Pictures/test_album
lens status --jobs

# 看库
sqlite3 ~/.life_lens/lens.db "SELECT photo_id, json_extract(exif, '$.captured_at_local') FROM photos LIMIT 5"
```

**压测目标**:单张端到端 < 25s(M 系列 Mac)。

## 开源发布流程

公开仓 https://github.com/uxtracer/life-lens,双目录:

| 目录 | 用途 | git remote |
|---|---|---|
| `~/claude/life_lens/` | 私有开发主战场,monorepo 子目录 | uxtracer/claude(私有) |
| `~/claude/life_lens-public/` | 公开镜像 working copy(独立 git) | uxtracer/life-lens(公开) |

发版:`bash scripts/publish.sh`(rsync `--delete` + 12 exclude + 隐私 grep + pytest A 层),**不自动 push**,提示人工 `cd` 镜像目录 commit + push。

**已知边界**:私有 history 有真名,公开 repo `git init` 从干净 HEAD 重新开始,**不保留 history**;两边各自演进,**不双向同步**;公开 PR 进来手动 cherry-pick 回私有。

**关键陷阱**:`pip install -e` venv 里登记的是源码绝对路径,`cd` 换 CWD 不换 Python 包加载路径 — 测开源版必须在镜像目录建独立 `.venv_lens`。

## 关键路径速查

```
life_lens/sources/             数据源 adapter(filesystem / photos_library)
life_lens/preprocess/cache.py  缓存键 = photo_id;改了所有缓存失效
life_lens/exif/extract.py      只 4 个字段,EXIF 别加机型/参数
life_lens/scanner/derived.py   派生规则,改了 reprocess --group derived
life_lens/scanner/runner.py    Phase A/B + graceful stop + 拍照时间正序 + amap quota 监控
life_lens/scanner/reprocess.py rematch/reprocess_faces + reprocess_vision_for + select_photos
life_lens/vision/prompts.py    v9.2 + _bbox_position(9 宫格)+ _desc/_struct_set_of_mark
life_lens/vision/role_check.py description ↔ persons.actions 自检
life_lens/geocode/amap.py      WGS-84→GCJ-02 + 50m 网格 + 配额 + POI 距离最近
life_lens/embed/               source_text 拼接(含 objects) + Embedder singleton
life_lens/query/search.py      hybrid FTS+sem RRF + persons_mode + query_expansions
life_lens/query/semantic.py    内存 brute-force 索引 + rrf_merge_multi
life_lens/store/schema.sql     改要 bump db.py::SCHEMA_VERSION,启动自动备份 + 迁移
life_lens/store/config.py      config.json 原子写 + chmod 600 + 单字段 update
life_lens/web/llm.py           openai-compat(claude-p 已删,旧 config 启动忽略 + WARN)
life_lens/web/chat.py          SSE 两轮 LLM tool loop + jsonl 日志
life_lens/web/static/          vanilla HTML/JS:5-tab + 配置卡片 + photo_id 前缀补全
life_lens/cli/main.py          argparse,不要换 click/typer
scripts/publish.sh             私有 → 公开镜像 rsync + 隐私 grep + pytest + 提示人工 push
scripts/build_embeddings.py    回填 photo_embeddings(Phase 6 inline 化后只兜底用)
tests/eval/                    B 层 11 张人工 ground truth(私有,publish 排除)
```

## CLAUDE.md 更新时机

**working memory,不是文档归档**。立刻更新的时机:
1. Phase 完成 — 改"当前状态" + Phase 路线图
2. 新陷阱/坑被识别 — 加进"踩过的坑"(第一时间,否则细节会忘)
3. 架构铁律变化 — 写进"铁律"或"不要做的事"
4. 核心字段/API 改名 — 全局替换,**不要漏**
5. 设计选择从 A 改成 B — 记录为什么,防止后续 Claude 又改回 A

不该写:临时调试日志、一次性实验过程、"今天发现 LLM 输出有点奇怪" 没结论的观察。
