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
- **Phase 6** 🟢:语义索引 inline 化 + Web 端补建/全量重建兜底 + `lens update` 子命令 + install.sh 端口占用先 kill 再启 + **album 信号**(本地 LLM 解析相册名 → 无 GPS 老照片补城市 + 事件关键词进 vision.tags)+ **LAN 分权**(配置页开关控制内网「问相册」移动页,热生效,见「隐私边界」)+ **问相册背景知识**(config `chat.user_notes` 注入两轮 prompt,别名→真名)+ **persons 模糊匹配**(≥2 字子串兜底)

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

**有意例外:album 事件关键词写进 `vision.tags`**(`scanner/album.py::merge_album_tags`)。理由:`vision.tags` 本就同时喂 FTS `tags` 列 + embedding source_text,写进去**自动可搜、索引侧零改动**;放 derived 还得给 FTS/embedding 再开一路。代价是 vision 不再是纯 LLM 死字符串 —— 用**幂等重注**化解:扫描产 vision 后、每次 `reprocess_vision_for` 后都重新 merge(dedup),所以重跑 vision 不会把 album tag 冲掉。**别当 bug 改回 derived。**

### LLM 不写 SQL,不操作连接

Web Chat 两轮 LLM:Round 1 出 `{action, args}`,Python `_dispatch` 调 `query/`;Round 2 流式回答 + `[photo:xxx]`。LLM 只填白名单字段(`query / time_from / time_to / persons / location / persons_mode / query_expansions`)。任何"让 LLM 直接出 SQL"或"工具开放任意字段"的诱惑都先拒掉。

### 拆调用 > 长 prompt

8B 小模型 instruction following 弱。把 6 条铁律 + JSON schema + set-of-mark + 人名约束塞 1500 字单 prompt → LLM 会忽略人名约束。**拆调用**(`describe_description` + `describe_struct` 各 300-500 字)实测只多 10-20% 时间(image token KV cache 命中),质量大幅提升。新加字段先想清楚是放进现有 prompt 还是再拆一次。

### 依赖方向严格单向

- 查询:`web (api / chat) → query → store/repo` + `geocode` 模块独立
- 写入:`web / cli → scanner → {sources, preprocess, vision, faces, exif, geocode, store}`
- 所有 adapter 只依赖 `schema` 和 `store`

## 技术栈硬约束

- **代码保持 Python 3.9 兼容**(公开仓 / 兄弟部署可能仍是 3.9;系统 `/usr/bin/python3` 仍是 3.9.6)。所有新文件必须 `from __future__ import annotations`,不要在运行时表达式里写 `X | Y`(类型注解可以)。**但本机 `.venv_lens` 已用 `~/.local/bin/python3.13` 重建**(2026-06):macOS Tahoe 的 Photos 库新 schema 只有 osxphotos 0.69+ 认得,而 0.65+ 需 Py 3.10+。代码 3.9 语法在 3.13 上向前兼容,照常跑。详见「踩过的坑 / macOS Tahoe Photos 库」
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
| 相册名(可能含真名/亲属称谓) | ❌ 永远本地 | 本地 Ollama 文本解析(**不走 DeepSeek/高德**),见 `album.py` |
| GPS 数字(裸 lat/lng) | ✅ | 高德 reverse geocoding |
| 用户问题文本 | ✅ | LLM(用户自己同意) |
| 查询返回的精简结果 | ✅ | Round 2 prompt(发描述文本 + photo_id,**不发图本身**) |

判断标准:**外发数据能否让接收方还原"这是哪张照片"**?GPS 单独发不行,但 GPS + 时间 + 缩略图组合就行 → 所以缩略图字节不能外发。

### Apple Photos「隐藏」相册照片:入口完全排除 ⚠️

用户在 Photos 里隐藏 = 明确不想被看到(常是敏感内容)。**`sources/photos_library.py::iter_photos` 在最顶端 `if p.hidden: continue`**,隐藏照片连 Phase A 队列条目 / preprocess / vision / 入库都不产生 —— 不是"扫了再藏",是**根本不扫**。取消隐藏后需重新扫描才纳入。

**历史教训(2026-06)**:老代码读了 `p.hidden` 存进 `source_signals.hidden` 却**从没用它过滤任何东西** —— 隐藏照片照样跑 vision、照样在浏览/问相册里显示、搜得到(实测 290 张被扫、242 张已生成 AI 描述)。已批量清除(photos/jobs/faces/embeddings/fts 五表 + 预处理缓存 jpg 全删)。**`source_signals.hidden` 字段保留但现在恒为 false**(入口已排除),别再以为它在做事;derived **只镜像 `favorite`,不镜像 `hidden`**。回归测试 `tests/test_photos_source_hidden.py`。

### LAN 分权(局域网 ≠ 外发,但也不是本机)

监听**始终 0.0.0.0**,按**来源 IP 分权**(`web/server.py` 的 `lan_gate` 中间件),两层控制:

1. **总开关** config `serve.lan_chat`(默认**关**)— 关闭时内网一律 403。配置页 Card 6「📱 内网访问」可切;**gate 每次远程请求热读 config,翻开关即时生效不用重启**(这正是"监听始终 0.0.0.0"的原因:bind 地址改不了,gate 行为可以热改)。开关端点 `GET/POST /api/config/lan-chat` 本身不在白名单 — 内网设备不能给自己开门
2. **白名单**(`_LAN_ALLOW`):开启后局域网设备访问 `/` 拿到移动端问相册页(`chat.html`),只放行 `POST /api/chat`、`GET /api/thumb|original|photo/{id}`(仅单段路径)、`GET /api/llm-providers|llm-info`、`/static/*`、`/health`,其余 403。配置/扫描/面孔/浏览及一切写操作内网不可见

本机(loopback)永远全功能,不受开关影响。设计决定(2026-06,用户确认):内网可看缩略图 + 原图下载(自己的设备);**不加口令**(信任家庭局域网);开关默认关 = 安全 opt-in。host 仍可用 CLI `--host` 或 config `serve.host` 硬 override(如锁回 127.0.0.1 彻底不监听内网,代价是改了要重启)。

**智能相框是独立的第三层(2026-06,用户确认"新开接口、安全起见")**:`web/frame.py` 一组只读出图端点(`/api/frame/next|photo|playlist|info`),**独立开关** `frame.lan_enabled`(默认关),和 `serve.lan_chat` **互不影响** —— gate 里两个功能各自 `if 开关: 查各自白名单`(`_LAN_FRAME_ALLOW` vs `_LAN_ALLOW`)。相框吐缩放后的 JPEG(默认 1024、`mode=contain|cover`、`quality=90`),不直传原图字节。**出图源:`_pick_source` 优先全分辨率原图(`decode_to_rgb`,单次压缩),原图不在本机才回退 1024/q85 预处理缓存** —— 走缓存是"原图→1024/q85→再压 q82"双重 JPEG + 竖图缓存宽仅 768 横屏 cover 还放大,实测发糊;从原图直接缩到屏幕尺寸是一次干净降采样,质量明显更好(2026-06 用户反馈"模糊"后改)。照片池优先级 `?theme=` > config `frame.theme` > 收藏;**主题三态**(`_resolve_pool`,非 LLM 兜底):空→收藏 / 命中已命名人→**人脸过滤出这个人全部照片**(`persons=`,含模糊兜底)/ 否则→内容搜索(`query=`)。**为什么人名优先走人脸**:名字当内容搜索既漏又混(实测某人 query 前 400 张里 243 张不是他、漏掉 1843 张人脸照,因为 description 不一定写名字)。`/next` 做"洗牌轮播"(进程内 `_Rotator`,一轮内每张恰好一次,池签名变了重洗),元信息走响应头 `X-Photo-Id/Date/Caption`(中文 URL 编码)。配置端点 `GET/POST /api/config/frame` **不在任何 LAN 白名单** —— 相框不能给自己开门/改主题。

**LLM 审核播放列表(2026-06,可控性 + 二道校验 + 灵活检索)**:纯 search 召回宽不可控(语义飘、描述漏名)。复用「问相册」两轮 LLM —— Round 1(`chat._build_round1_system`)把主题翻译成检索条件(人名/时间/地点/**扩词**,比字符串搜灵活),`_dispatch` 召回,再一道 `_llm_verify` 把候选的 description/tags/objects/scene 喂 LLM **只留真正符合主题的**(复用 `chat._short_id_map/_slim_for_llm/_expand_photo_ids` 瘦身还原),产出已审核 `photo_id` 列表落盘 `frame_playlist.json`。**铁律:LLM 只在"设主题 / 点重建"时跑一次(后台线程 `start_build`),`/next` 只读缓存洗牌——绝不每帧调 LLM**;构建未完成/失败/无 provider 时 `/next` 优雅回退非 LLM 池(相框不黑屏)。无 `query` 的纯人名/收藏类**不校验**(人脸已精确,LLM 看不到脸,校验只会误删)。实测「海边」80 候选→审定 71,「美食」Round 1 自动扩词`[食物,餐厅,菜肴...]`→留 72,各 ~50s。构建/状态端点 `POST /api/frame/playlist/rebuild`、`GET /api/frame/playlist/status` **仅本机**(子路径不匹配白名单的 `^/api/frame/playlist$`)。⚠️**隐私**:构建时候选的 description + 短 photo_id 会发给所选 LLM(和「问相册」同级外发,不发图字节)。测试 `tests/test_frame.py`。

⚠️ **`@app.middleware("http")` 不覆盖 WebSocket** — 将来若加 ws 端点,它会完全绕过 lan_gate(静默旁路)。加 ws 前必须给 gate 补 websocket 维度(或 ASGI 层拦截)。已知接受的暴露面:开关关闭时内网请求仍会走到 uvicorn/h11 的 HTTP 解析(连接能建立,403 在中间件层)— 比绑 127.0.0.1 多出"HTTP 解析层 pre-auth 漏洞"这一层理论风险,家庭场景接受。测试在 `tests/test_lan_gate.py`(TestClient 默认 client host 是 `'testclient'`,`_is_local_client` 视作本机 — 否则全部 API 测试 403;模拟远程显式传 `client=(ip, port)`;config 跟 env `LIFE_LENS_ROOT` 走,fixture 指 tmp_path 隔离真实配置)。

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

### 文件常没 EXIF 拍摄日期 → Apple `p.date` 兜底 ⚠️⚠️

`exif/extract.py` 只从**文件 EXIF** 读 DateTimeOriginal。但 **PNG / 截图 / 老照片 / iCloud 合并导入的图,文件里根本没这个字段** —— 实测一个库 **21,332 张(~19%)`captured_at_local` 为空**。空日期照片在浏览/Photos 里按"添加日期"显示,全挤在导入那几天,看着像"某天突然几万张"。**Apple 自己有权威日期**(`osxphotos p.date`,带时区,Photos 列表就靠它排序),我们以前没读。**修复(2026-06)**:`SourceMetadata` 加 `apple_captured_local/utc/tz_offset_minutes`(`photos_library.get_metadata` 从 `p.date`/`p.tzoffset` 算);`pipeline.process_one` 在文件 EXIF 没日期时用它兜底;Phase A 入队排序同样兜底(`runner.py`,否则空日期全排队首 + 时间维度失真)。**只在文件 EXIF 缺时兜底,不动已有正确日期的照片**。存量用脚本读 `p.date` 回填(改 exif + jobs.captured_at_local + derived.time_bucket,不重跑 vision)。

### Apple 源会把视频也吞进来 ⚠️

`db.photos()` 同时返回照片**和视频**,`iter_photos` 以前没过滤 → 实测 3,966 个 MOV/MP4 进了库(vision 解不了 MOV,纯失败/噪音 + 全是空日期)。filesystem 源靠扩展名白名单挡视频,Apple 源没扩展名概念,**必须 `if p.ismovie: continue`**(和 hidden 同处)。已批量清除存量。

### 高德 reverse geocoding 设计要点

- key 放 `~/.life_lens/config.json`(`chmod 600`)或 env `AMAP_KEY`,**不进 git**
- **必须 WGS-84 → GCJ-02 转换**(`amap.py::wgs84_to_gcj02`):iPhone EXIF 是 WGS-84,高德用 GCJ-02,中国境内偏 **300-700m**,小景点(几十米直径的音乐厅 / 寺庙)被偏到几百米外丢 POI。国境外 `_out_of_china` 自动跳过
- **50m 网格缓存**(`GRID_STEP_DEG=0.0005`),`provider='amap-gcj02'` 做版本隔离
- **`place_name` / `formatted_address` 不入缓存,出口 `_derive_place_and_formatted` 实时拼**(改地点策略不必失效缓存)
- **POI 取距离最近**(`min(pois, key=distance)`),高德默认按权重(主景区名)排,不是距离
- **直辖市坑**:北京/上海/天津/重庆的 `addressComponent.city` 是空数组,fallback 到 province
- **配额管理**:`amap_quota` 表按 Asia/Shanghai 日期记 count,免费 5000/天上限设 4800(buffer 200)。耗尽时 `is_quota_exhausted` 阻止 HTTP,runner 自动暂停 run

### 国外 GPS:高德只覆盖中国 → 靠 Apple `place_apple` 兜底 ⚠️⚠️

高德 reverse 只有中国境内数据,境外坐标返回 HTTP200/status=1 但 `addressComponent` 全空。老代码 `_extract` 写 `country = ac.get("country") or "中国"`,把**所有国外照片硬标成"中国"且 formatted_address=null**(实测东京/巴黎/洛杉矶共 ~1.2 万张中招)。**国外地点根本不该问高德** —— 数据源是 Apple Photos 透传的 `place_apple`(osxphotos `p.place.name`,Apple 全球地图早就反查好,如 `环球影城, Universal City, 加利福尼亚, 美国`)。

**三处修复(2026-06,均在 `geocode/amap.py` + `scanner/derived.py`)**:
1. `reverse_geocode` 顶部 `_out_of_china` 短路 return None(cache 之前)→ 国外不调高德、不耗配额、自动绕过历史污染缓存;`_extract` 的 `or "中国"` 去掉。
2. 新 `parse_place_apple(s)`:Apple 串**细→粗、末段恒国家**,按位置映射 country=末段/province=倒二/city=倒三/poi=首段(仅 ≥4 段),产出与高德**同构**的 bucket。derived `_location_bucket` 的 `if parsed: ... elif place_apple:` —— **安全不变量:国内 reverse 成功必走 `if parsed`,apple 永不覆盖高德中国结果**。
3. `_build_formatted_address` 按 country 分支:国外(country!="中国")country 打头、各级 ` · ` 连接(`美国 · 加利福尼亚 · 环球影城`);中国行**字节不变**(admin 拼接)。

**`_out_of_china` 的 bbox 很粗**(lng 73–135/lat 3.86–53.55)**会把泰国/越南/印度等圈进"境内"** → 不短路、高德对这些境外点只回 country。对策:`reverse_geocode` 出口 `_has_location()`(province/city/aoi/poi 任一非空才算查到),**只有 country 的空壳当 miss 返 None** → 走 apple 兜底;真有 POI 的中国边境点(德天瀑布:有 poi 无 province)仍保留。**改了 GPS/地点逻辑后跑 `reprocess_derived` 回填**(全库 11 万张 ~1min,国内走缓存、国外短路,不耗配额)。已知小瑕疵:单段 apple 是水域/地标名(北太平洋/塞纳河)会落进 country 字段,无 gazetteer 难判,接受。

### FTS5 `unicode61` 不索引中文 ⚠️

SQLite FTS5 默认 `unicode61 remove_diacritics 2` **完全不索引中文**(`MATCH '观光车'` 返 0 张)。改 `tokenize='trigram'`(SQLite 3.34+ 内置),db.py 检测旧 schema 自动 DROP + CREATE + 回填。**限制:搜索词 ≥ 3 字符**(2 字"长城"匹配不到 trigram),`search.py` query 短词 fallback LIKE。

### scan_runs snapshot 列防 reprocess 偷 jobs

reprocess 把 jobs 行的 `run_id` 改成新 run_id 让进度可查;副作用是原 run 的 jobs 被偷走后实时查 `time_range / scanned_up_to` 全 null,历史 run 进度卡空白。对策:`scan_runs` 加 `snapshot_captured_min / max / scanned_up_to` 三快照列,enqueue 后一次性冻结 + 每张完成时增量推进。

### self-check 字面匹配的固有局限

`vision/role_check.py` bigram + char 字面匹配 description 邻域。**对同义表达无能为力**(struct="举手机自拍" vs description="面带笑容" 误报)。前端 banner **降级浅蓝**(不是橙黄)+ "已核对忽略" 按钮 → `meta.errors.acknowledged=true`,重跑 vision 时清除。要彻底消除得换 LLM-based self-check,贵,**当前不做**。

### LLM 偏好截短 photo_id 到 8 位

部分模型(非顶级)截 git-short-hash 风格 → `<img src="/api/thumb/19D5A112">` 404。**两层防御**:
1. ROUND2_SYSTEM 写"id 必须原样复制" + 示例 — 减少但不消除
2. **前端 candidates 前缀补全**(`app.js::_fixPhotoId`):取 result 帧的所有 candidate id 做集合,LLM 写的 id 不在 → 前缀唯一匹配自动补全

**铁律:只靠 prompt 教不稳,关键不变量必须程序层守住**。

**2026-06 演进**:Round 2 瘦身后服务端**主动**发 10 位短前缀给 LLM(`_short_id_map` 结果集内构造唯一,碰撞自动加长),前端靠同一套 `_fixPhotoId` 还原 — "id 不能改"的不变量没破,只是从"求 LLM 抄对 36 字符"换成"程序层保证唯一前缀可还原"。**前提**:SSE result 帧必须始终携带完整 raw id(candidates 是还原的依据,瘦身只瘦 LLM prompt 这一份);LLM 把 10 位再截短到 8 位仍能唯一还原,幻觉 id 照旧 null 丢弃。

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

### dense brute-force top-K 永远返 K 条 → 稀疏 query 全是凑数噪音 ⚠️

用户问"张三哭的样子",persons 是硬过滤、query 只影响排序 → 返回 150 张全有张三但只有 3 张字面含"哭"(库里就这么多),其余 147 张是语义路径按余弦"最近的 K 条"硬凑的(分数 0.32-0.38 纯噪音水平)——再喂给 Round 2 烧 35k tokens 逐条校验。**对策(2026-06)**:`_search_semantic` 加余弦下限 `SEMANTIC_MIN_SCORE=0.42`。标定数据(bge-small-zh):稀疏 query 真命中 ≥0.48、正常场景词 top150 ≥0.45、噪音底 ≤0.38,两侧余量 ~0.05。只砍语义路径,FTS 字面/扩词不受影响,最坏退化纯 FTS。实测"哭"case 候选 150→26 条、prompt -81%;海边/雪景等正常 query 满额不受影响。**换 embedding 模型必须重新标定**(方法:取稀疏 query 的字面真命中分数 vs top-K 噪音底分布,阈值取两者中间;标定注释在 `search.py::_search_semantic`)。

### LLM 优秀行为靠运气,要 prompt 显式化

某 case 用户问"小华穿裙子" 召回 117 张(都含小华但 0 张穿裙子),LLM 诚实说"没找到" — 但**靠运气**。修法:ROUND2_SYSTEM 加"引用前轻量校验 description+tags+objects+scene,不符合不引用,全部不符合按结果空处理" — 把涌现行为变成必选项。

### Apple Photos `face_info=[]` 偶有漏识别

Apple Photos.app 后台脸识别有时漏检。老实现是 `face_info=[]` 时跳过 InsightFace(信任 Apple),**改成返 `None` → fallback InsightFace 兜底**。代价每张多 0.3-1s,值得。已扫数据用 SQL 反查 `face_count=0 AND vision.subject IN (single,portrait,group)` 批量 reprocess(memory `project_life_lens_apple_face_misses`)。

### 选 `.photoslibrary` 包时 AppleScript 返回尾斜杠 → 误走 filesystem ⚠️⚠️

Web「选 Photos 库」按钮走 `pick_folder(mode=photos_library)` → osascript `choose file` 选 `.photoslibrary`。**`.photoslibrary` 是 macOS package(目录),`POSIX path of` 它会带尾斜杠**(`/Users/.../X.photoslibrary/`)。前端 `app.js` 原来用 `path.endsWith('.photoslibrary')` 判 kind → 带斜杠时为 **false** → 错走 `filesystem` source。后果极隐蔽:filesystem walker **钻进包内部**,把 `originals/` + `resources/derivatives/`(缩略图/裁剪衍生 `_1_105_c`)全当独立照片扫,同张照片重复多份、几乎 0 脸、且 filesystem source **永远拿不到 Apple 命名的脸**(那是 photos_library source 专属)。后端 `add_source` 的 `resolve()` 又把斜杠抹了,存进库的路径看着没问题,kind 却已经错了 → debug 时极易被误导。

**修复(双保险)**:前端判 kind 前 `path.replace(/\/+$/, '')`;后端 `pick_folder` 出口 `path.rstrip("/") or "/"`。**教训**:凡按后缀判类型的地方,先归一化尾斜杠;package 路径和普通目录路径行为不一致。

### fastembed 默认缓存在临时目录,会被 macOS 定期清 ⚠️

fastembed 0.7.4 不传 `cache_dir` 时,模型(bge-small-zh ~95MB)落在 `tempfile.gettempdir()/fastembed_cache`,macOS 上是 `/var/folders/.../T/` —— **系统会定期 purge**(重启 / 闲置几天)。症状:隔几天再扫,第一张照片卡 3-4 分钟"下载模型",其实是临时缓存被清后重下。`embedder.py` 显式传 `cache_dir=<root>/.cache/fastembed/`(和 preprocessed 缓存同级,持久)。**注意代码老注释曾写"下到 ~/.cache/fastembed/",是错的**(那目录从不存在)。

### 加生成列(GENERATED ... STORED/VIRTUAL)迁移三连坑 ⚠️⚠️

给 photos 加 favorite 生成列(`json_extract(derived,'$.favorite')`,Phase 6 收藏功能)时连踩三个:

1. **`ALTER TABLE ADD COLUMN` 只能加 VIRTUAL 生成列,STORED 会报错**。所以 `schema.sql` 的 `CREATE TABLE` 里用 STORED(fresh 安装),`db.py::_migrate_columns` 的 ALTER 用 VIRTUAL(老库)。两者对查询/索引等价。
2. **`PRAGMA table_info` 不列生成列** → 用它判"列是否已存在"会误判 fresh 库(schema.sql 已建 STORED favorite)缺列 → 再 ALTER → `duplicate column name`。**判存在性必须用 `PRAGMA table_xinfo`**(它列生成列)。
3. **生成列的 `CREATE INDEX` 不能放 `schema.sql`**:`init_schema` 先 `executescript(schema.sql)` 再 `_migrate_columns`,老库 executescript 阶段 favorite 列还没 ALTER 出来 → `no such column: favorite`。索引必须在 `_migrate_columns` 里、加列之后建。

模板就是已有的 `is_keeper`。新加任何 `derived/` 生成列照此办理。**已处理照片需 `reprocess --group derived` 回填新字段**(老 derived JSON 没这个 key → 生成列 NULL)。

### macOS Tahoe Photos 库需 osxphotos 0.69+ → 本机 venv 升到 Py 3.13 ⚠️⚠️

macOS Tahoe(Darwin 25.x)的 Photos 库是新 schema(asset 表名后缀变了,实测 `Z_33ASSETS`)。**osxphotos 0.64.3(原先为兼容 Py 3.9 钉死的 `<0.65`)读不了**,Phase A 入队直接抛 `sqlite3.OperationalError: no such table: Z_28ASSETS`(它还在找老后缀),scan run 标 failed、total=0。**死结**:支持新库要 osxphotos 0.69+,而 0.65+ 用了运行时 `X | None`(Py 3.10+)。

**解法(2026-06)**:本机其实已装 `~/.local/bin/python3.{10,11,12,13}`,用 **python3.13 重建 `.venv_lens`** + osxphotos 升到 0.75.9,读到 11 万张。`pyproject.toml` 的 apple extra 改成 **python_version marker**:`<3.10` 仍 `osxphotos>=0.60,<0.65`、`>=3.10` 用 `>=0.69`,这样 `lens update` 不会把它打回旧版,也不破公开仓的 3.9 故事。代码本身 3.9 语法在 3.13 上照跑。

### qwen3-vl:8b 的默认 tag 是 thinking 变体,本项目必须 `-instruct` ⚠️⚠️

Ollama 上 `qwen3-vl:8b`(不带后缀)拉到的是 **thinking 变体**(`/api/show` 的 capabilities 含 `thinking`,renderer/parser=`qwen3-vl-thinking`)。本项目 vision 调用是 `format=json` + `num_predict=1024` —— thinking 模型先生成一大段推理链,把 token 预算吃光,`response` 吐**空/截断 JSON**,且慢 3-10 倍。**症状:扫描卡在第一张**(后台 worker 线程把错误吞了,前端只看到不动)。**必须 `ollama pull qwen3-vl:8b-instruct`**(capabilities 无 thinking),代码 `DEFAULT_MODEL` 就是它,零配置命中。

**连带:探活的前缀匹配会给假绿灯**(已修)。`ollama_probe.py::ping` 和 `ollama.py::health_check` 原来用 `m.startswith("qwen3-vl")` 容错匹配 —— 基于"`:8b` 和 `:8b-instruct` 是同一个模型"的**错误假设**(其实前者 thinking),导致只装 thinking 版时配置页/CLI 也判"已就位",把问题遮住直到扫描才暴露。**已改成精确 tag 匹配 + `/api/show` 读 capabilities,命中 thinking 时明确警告**。教训:**凡"模型是否就位"的探活,别只比名字前缀,要精确 tag + 能力(最好再试跑一次)**。

### MCP Python ≥ 3.10(本机 venv 已是 3.13,不再受阻)

`pip install mcp` 要 `Requires-Python >=3.10`。原先本机 venv 是 3.9 装不了;**2026-06 venv 升 3.13 后这条解除**(见上「macOS Tahoe」)。**当前**:web chat 仍走手写两轮 `web/llm.py` openai-compat tool loop,不依赖 MCP;要补 MCP server 现在环境已就绪。

### 本地 openai-compat server(LM Studio)三连坑:latin-1 乱码 / think 块 / 上下文太小 ⚠️

用 LM Studio 跑 chat,Round 1 全对、Round 2 全坏(空回答 / 乱码 / 400),三个独立原因:

1. **SSE 响应头不带 charset → requests 按 latin-1 解** → 中文全花(`ç¸å...`)。DeepSeek 带 `charset=utf-8` 所以从没暴露。修复:`llm.py` 流式路径强制 `r.encoding="utf-8"`(SSE 规范本就只有 UTF-8)。
2. **thinking 变体把推理链 `<think>...</think>` 塞 content**(和 vision 侧 qwen3-vl thinking 同款坑)。修复:`llm.py` `_strip_think` / `_strip_think_stream` 程序层剥掉(tag 跨 chunk 也处理);provider 可配 `extra_body`(如 `chat_template_kwargs.enable_thinking=false`)透传给 server。但剥只是兜底,推理 token 照样耗时,**根治在 server 侧关 think**。
3. **Round 2 prompt 塞 80-200 张候选 JSON ≈ 30-80k token,LM Studio 默认装载 ctx 4096** → 超限被截断(空回答)或直接 400。**代码救不了,用户侧装载模型时 ctx 开到 ≥32k**。"同模型跑别的 agent 很好"不代表这里能用 — 别的 agent prompt 短。

诊断入口:`~/.life_lens/chat_log/*.jsonl` 按 provider 字段过滤,plan/answer/error 一目了然。

### album 名解析:8B 城市猜测不可靠,先 curate 缓存再回填 ⚠️

`album.py` 用本地 Ollama(qwen3-vl:8b)解析相册名补地点。**8B 对模糊地名的城市/国家判断不稳**:实测把"奥体"猜成杭州、"艺术照"脑补成三亚、国家"日本"塞进 city。prompt 加"模糊就 null / 国家→null"也不稳定遵守(老问题,见「拆调用 > 长 prompt」)。

**对策不是硬调 prompt,而是利用相册名是小集合**:一个库 unique 相册名通常只有几十个。流程:dry-run 解析 → 把 unique 名的 country/city/place/tags 打表给用户 → 用户口述修正、直接 `UPDATE album_parse_cache` → 从修正后缓存全量回填(`reprocess_albums` 纯读缓存不再调 LLM,3156 张 ~1min)。

**改 cache 的 city 必须连 province 一起改** ⚠️:踩过坑——只 `UPDATE city='北京'` 没改 province,残留的省(浙江/海南)被 `amap._build_formatted_address` 拼成"海南北京""浙江北京"。改完自查 `WHERE province IS NOT NULL AND city IS NOT NULL AND province!=city`(同省不同名如 海南/三亚、湖南/郴州 是正常的,要排除)。

分层约定:国外只到国家 → `location_bucket.country`,city 留 null;景点(颐和园)→ `poi_name`,只有城市时 `place_name` 留空(跟 GPS 路径一致,不拿城市名顶替)。tag 白名单类的批量清理是**一次性人工 curate**,不进 prompt/代码(否则将来新扫描会被永久约束)。

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
- `derived.location_bucket` 优先级:**国内 GPS → 高德 reverse**;**国外 GPS → Apple `place_apple` 解析**(`parse_place_apple`,高德对境外短路返 None 时);**无 GPS → album 名兜底**。三路都填同构字段(country/province/city/poi_name/place_name/formatted_address),跟 GPS 同 `_build_formatted_address` 格式;全失败 graceful 全 null。见「踩过的坑 / 国外 GPS」
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

**Round 2 prompt 瘦身**(2026-06,input token 优化):进 LLM 的查询结果走 `_slim_for_llm` 副本 — compact dumps(去 indent)+ 去空字段(null/""/[]/false)+ **photo_id 换 10 位短前缀**(`_short_id_map`,结果集内碰撞自动加长)。宽搜 150 条 ≈ 50k → 37k tokens。**SSE result 帧和 chat_log 仍是完整 raw**(前端缩略图/`_fixPhotoId` 前缀补全依赖完整 id);`done` 帧的 photo_ids 服务端 `_expand_photo_ids` 扩回完整 id(否则前端"未引用候选"按完整 id 比对会全判未引用)。短 id 直接复用前端既有的前缀补全机制,不是新协议。

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

**query/ 共享层**(三轨共用):`search.search_photos(query, time_from, time_to, persons[], persons_mode, location, query_expansions, favorite_only, limit)` hybrid FTS5+sem RRF / `aggregate.places_visited(year?)` / `aggregate.counts_by_year()`。带 query 时每条 item 标 `match: literal|semantic`(literal=任一 FTS/LIKE 路径字面命中,semantic=仅向量相近的弱候选)— Round 2 prompt 据此分层校验。语义路径有余弦下限 `SEMANTIC_MIN_SCORE=0.42`(bge-small-zh 实测标定,见踩过的坑;换 embedding 模型要重标)。`favorite_only=True` 走 `photos.favorite` 生成列(源自 `derived.favorite` ← Apple 收藏);收藏在结果里**轻加权**(`_apply_favorite_boost` 上浮 ~3 位,不置顶)。browse `/api/photos?favorite_only=true` + ⭐ 角标 + "只看收藏" 开关同源。**persons 人名模糊兜底**:精确 name 没中时 ≥2 字互为子串匹配(`_fuzzy_name_cids`,解决"3 字人名只说后 2 字搜不到";单字太泛不模糊;命中多人全返 OR 语义,AND 路径折叠成组)— 匹配顺序 精确 → cluster_id 直传 → 模糊,顺序不能换(`apple:张三` 含人名子串,模糊放前面会把"指定具体 cluster"扩成"同名全部")。

**问相册背景知识**(config `chat.user_notes`,配置页 Card 4):用户写小名/别名↔真名、人物关系、"家"在哪等自由文本,`chat.py` 每次提问热读注入 Round 1(指示把别名换真名再填 persons)+ Round 2(回答沿用用户称呼)。别名→真名只能靠它(模糊匹配解决不了无字面重叠的别名)。API `GET/POST /api/config/chat-notes`(4000 字上限;不在 LAN 白名单,内网设备不可改)。注意:这段文字随每次提问外发给 LLM(和用户问题同级,UI 已提示)。

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
- **Apple 命名映射**:source 解 `face_info.name` → `cluster_id='apple:<name>'`,Apple检测未命名 → `apple_face:<uuid>`(跨照片不聚类);Web端可把待命名脸手动并入已有姓名并同步`people.persons[]`

### 两种重跑

| 命令 | 干什么 | 耗时(10000 张) |
|---|---|---|
| `rematch_faces`(quick) | 不跑 detect,用已有 embedding 重新算 max-pooling,把主库脸吸到已命名 anchor | 秒级到几十秒 |
| `reprocess_faces`(full) | 重跑 InsightFace detect + assign | 几小时 |
| `reprocess_vision_for(photo_ids)` | 指定 photo 重跑 vision(两次调用),prompt 注入当前最新人名 → description 用真名 | 每张 ~40s |
| `reprocess_albums(photo_ids, dry_run)` | 不重跑 vision,从 albums 补 location 城市/景点 + 合并事件 tag(读 album_parse_cache)。`dry_run` 返前后对比明细 | 几千张 ~1min |

`reprocess_albums` **未接 CLI / Web**(原计划判定非必需),靠临时脚本 / `python -c` 驱动,配合 curate 后的 `album_parse_cache`(见「album 名解析」坑)。`photo_ids_for_run(conn, run_id)` 取某 run 的全部 photo_id。

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
| `~/claude/life_lens-public/` | 公开镜像 working copy(独立 git,**已加 monorepo .gitignore**) | uxtracer/life-lens(公开) |

`publish.sh` 的 SRC 从脚本自身位置推导,私有仓挪目录不用改脚本(2026-06 已两次搬迁验证);DST 固定 `~/claude/life_lens-public`,丢了重新 `git clone` 即可(镜像不保留私有 history,本就无状态)。**注意**:2026-06 monorepo 搬到 `~/claude/` 后,公开镜像成了 monorepo 根下的兄弟目录,靠根 `.gitignore` 的 `life_lens-public/` 行隔离。

发版:`bash scripts/publish.sh`(rsync `--delete` + 12 exclude + 隐私 grep + pytest A 层),**不自动 push**,提示人工 `cd` 镜像目录 commit + push。

**已知边界**:私有 history 有真名,公开 repo `git init` 从干净 HEAD 重新开始,**不保留 history**;两边各自演进,**不双向同步**;公开 PR 进来手动 cherry-pick 回私有。

**关键陷阱**:`pip install -e` venv 里登记的是源码绝对路径,`cd` 换 CWD 不换 Python 包加载路径 — 测开源版必须在镜像目录建独立 `.venv_lens`。

## 关键路径速查

```
life_lens/sources/             数据源 adapter(filesystem / photos_library)
life_lens/preprocess/cache.py  缓存键 = photo_id;改了所有缓存失效
life_lens/exif/extract.py      只 4 个字段,EXIF 别加机型/参数
life_lens/scanner/derived.py   派生规则,改了 reprocess --group derived(location_bucket album 国家/城市/景点兜底 + favorite 镜像 source_signals;hidden 不镜像 —— 隐藏照片在 source 入口已排除,见隐私边界)
life_lens/scanner/album.py     相册名→国家/城市/景点+事件关键词(本地 Ollama 解析 + album_parse_cache 去重缓存)
life_lens/scanner/runner.py    Phase A/B + graceful stop + 拍照时间正序 + amap quota 监控
life_lens/scanner/reprocess.py rematch/reprocess_faces + reprocess_vision_for + reprocess_albums(run 范围+dry_run)+ select_photos
life_lens/vision/prompts.py    v9.2 + _bbox_position(9 宫格)+ _desc/_struct_set_of_mark
life_lens/vision/role_check.py description ↔ persons.actions 自检
life_lens/geocode/amap.py      WGS-84→GCJ-02 + 50m 网格 + 配额 + POI 距离最近 + 国外短路(_out_of_china/_has_location)+ parse_place_apple(国外靠 Apple 透传)
life_lens/embed/               source_text 拼接(含 objects) + Embedder singleton
life_lens/query/search.py      hybrid FTS+sem RRF + persons_mode + query_expansions
life_lens/query/semantic.py    内存 brute-force 索引 + rrf_merge_multi
life_lens/store/schema.sql     改要 bump db.py::SCHEMA_VERSION,启动自动备份 + 迁移
life_lens/store/config.py      config.json 原子写 + chmod 600 + 单字段 update
life_lens/web/llm.py           openai-compat(claude-p 已删,旧 config 启动忽略 + WARN)
life_lens/web/chat.py          SSE 两轮 LLM tool loop + jsonl 日志
life_lens/web/frame.py         智能相框 LAN 接口(/api/frame/next|photo|playlist|info)— 洗牌轮播 + 缩放出图 + LLM 审核 frame_playlist.json
life_lens/web/server.py        FastAPI app + LAN gate(_LAN_ALLOW 问相册 + _LAN_FRAME_ALLOW 相框,两个独立开关)
life_lens/web/static/          vanilla HTML/JS。index.html(桌面 5-tab)+ chat.html(移动端问相册)
                               共用 chat.js(api/viewer/SSE/前缀补全)— index.html 必须先 chat.js 后 app.js
                               (app.js 依赖 chat.js 的全局,两边不要重复声明)
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
