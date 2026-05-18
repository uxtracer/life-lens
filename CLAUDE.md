# life_lens

## 一句话定位

把整本相册(几万到几十万张)用本地视觉模型转成结构化 JSON 文本,让 LLM 后续能查询、能基于它写"用户的生活"。**照片字节完全本地**(本地推理 Ollama + qwen3-vl:8b),GPS 数字 / 用户问题文本可外发(地图 API / claude -p)。

兄弟项目都是 markdown wiki + `claude -p`(ai_invest / live_coach / ai_coach),本项目**不走 markdown** — 几十万行只有 SQLite + sidecar JSON 才扛得住。

## 当前状态

**Phase 5 已完成 — 开源版上线**(2026-05-18 首发 + 多次迭代,公开仓 https://github.com/uxtracer/life-lens)

主线 scan 持续跑(用户 27888 张 Apple Photos,~24s/张,合盖待机几天扫完)。Phase 2-4 全部稳定。Phase 5 全部落地:UI 5-tab 重构 + 配置卡片引导 + LLM 砍 claude-p 统一 RESTful + config 跟 `--root` 走支持多 root 并行 + `install.sh` 一行命令安装 + README 重写为外部用户视角(三段:是什么 + 怎么用 + 设计思路)。

**Phase 6 起步:语义索引 inline 化(2026-05-18)** — 主扫描 + reprocess vision 每张处理完都顺手 embed + 写 `photo_embeddings`(`update_fts` 同一点位),不再依赖手动跑 `scripts/build_embeddings.py`。Web 端"配置 > 存储"卡片有"补建缺失 / 全量重建"兜底按钮(后台线程 + 进度轮询)。彻底消除"新照片只能 FTS 命中,语义召回看不到"的隐式坑。

### Phase 2 改造(2026-05)

1. **大批量扫描机制** — db 驱动 + 断点续传 + Run 历史(支撑 10w+ 张扫几天合盖待机)
   - `scanner/runner.py` 重写两阶段:Phase A 流式入队(content_hash 判重 + peek EXIF DateTime)+ Phase B 单线程处理
   - `jobs` 表 db 状态机:pending → processing → done / failed,jobs 行带 `run_id` + `captured_at_local`
   - 新表 `scan_runs`(每次 scan/reprocess 一个 Run)+ 3 个 snapshot 列(time_range / scanned_up_to 冻结防 reprocess 偷 jobs)
   - 按拍照时间正序 ASC(用户看进度直观),`get_pending ORDER BY captured_at_local ASC NULLS LAST`
   - graceful stop:Web 暂停按钮 + Ctrl+C signal → stop_flag,当前张跑完后 run 标 stopped
   - 失败重试 3 次(retry_count<3 重置 pending,>=3 永久 failed)
   - **scan_runs.done/failed 用 inc_run_done/inc_run_failed 增量更新**,resume 时 done 不重置

2. **vision prompt v9.2 — 位置 + age/gender 双重 hint 修人物错位**
   - InsightFace buffalo_l 内置 age + gender(genderage.onnx),`faces/detector.py` 提取并存
   - `persons` 表加 `age_estimate / gender_estimate`,种子上传后从多张种子图平均(`_refresh_cluster_demographics`)
   - `vision/prompts.py::_bbox_position` 从 face bbox 算 9 宫格位置('下中'/'中右'),**100% 准**
   - face_items 升级 dict 格式(idx + name + bbox + image_size + age + gender)注入 prompt
   - 末尾强约束:"先逐一对照红框真实位置,核对程序给的'位置:XX'标签(100% 准),确认 [N]→人映射,再开始写"
   - age/gender 标"参考估计(可能不准,以视觉为准)"— InsightFace 对儿童不准(11 岁男孩可能估成 23 岁女)
   - 评测集:8/11 (v9.1) → **11/11 (v9.2) PASS** ✅

3. **self-check 后置校验 description ↔ struct.actions**
   - `vision/role_check.py::check_description_vs_persons`:对每个 (name, action),在 description 里找 name 后 30 字邻域(截到下个名字前),验证含 action 关键词
   - bigram + char hybrid 命中(bigram 任一 hit 或 char ≥2 hit,防"手表"的"手"误命中"举手机")
   - pipeline + reprocess 完成 vision 后跑,不一致写 `meta.errors[group=vision_role_mismatch]`
   - 前端 Browse 详情**浅蓝色提示 banner**(降级文案"可能是同义表达,需核对")+ "已核对,忽略此提示"按钮
   - `POST /api/photo/{id}/mismatches/acknowledge` 标记 acknowledged,重跑 vision 时清除
   - 重跑页字段筛选 `role_mismatch` 过滤已 acknowledged 行
   - **局限**:字面匹配,struct='举手机自拍' vs description='面带笑容' 同义表达会误报 → acknowledged 机制兜底

4. **评测集 `tests/eval/`(11 张人工 ground truth)**
   - 覆盖:无人(3) / 单人(2) / 双人合影(3) / 四人(1) / 关系反转(1) / 双人背影(1)
   - `description_role_check` 字段防"人名都对但动作搞反"(IMG_0685 baseline 那种)
   - `python tests/eval/run_eval.py` 跑全部对比 expected,`--case IMG_0685` 单跑某张
   - **每改 prompt 必跑,目标不退步**

5. **高德 geocode 全面修复**
   - WGS-84 → GCJ-02 转换(`amap.py::wgs84_to_gcj02`,标准火星坐标算法)— iPhone EXIF 是 WGS-84,直接传高德有 300-700m 偏移,小景点完全错过
   - 50m 网格(`GRID_STEP_DEG=0.0005`),坐标量化 → 同一景区 / 商场合并到一条缓存
   - `provider='amap-gcj02'` 隔离旧错误缓存
   - **`place_name` / `formatted_address` 不入缓存,出口实时派生**(改地点选择策略不必失效缓存)
   - POI 取**距离最近**(高德按权重排序而非距离;'阿那亚金山岭' 是主景区名 dist=418m,但 dist=24m 的'阿那亚山谷音乐厅' 才是用户真实在的位置)
   - `formatted_address` 自拼"行政区划 · AOI · POI"(避免高德 raw 把多个 POI 名拼到尾巴上)
   - 配额管理:`amap_quota` 表按 Asia/Shanghai 日期记 count,免费 5000/天上限设 4800(buffer 200)。耗尽时 `is_quota_exhausted` 阻止 HTTP,runner 自动暂停 run。`/api/scan/resume` 拦截 409

6. **FTS5 中文支持修复**
   - 旧 `tokenize='unicode61 remove_diacritics 2'` **完全不索引中文** → chat '观光车' 返 0 张
   - 改 `tokenize='trigram'`(SQLite 3.34+ 内置),db.py 检测旧 schema 自动 DROP + CREATE + 回填
   - 限制:trigram 数学上要求 ≥ 3 字符,`search.py` query 短词(<3 字)fallback LIKE

7. **Web UI 完整化**
   - 扫描页:数量进度条 + 时间维度文字(扫描区间 + 已扫到拍照时间)+ 暂停/继续按钮
   - 多个 resumable runs 各自卡片(完整元信息:source / 进度 / 时间范围 / 起止时间 / 备注)
   - 运行历史页(Runs):列表 + 详情(复用进度卡)+ 失败诊断 + 重试 / 继续
   - 重跑页(Reprocess):source / 时间范围 / 字段缺失(含 'description 语义对齐差')/ 人物筛选(单下拉:无人/单人/多人/含 XXX)/ FTS 关键词
   - 进度卡:elapsed + 自适应速率(< 1 张/秒 显示 N 秒/张)
   - 高德今日配额小标签(0/4800),≥80% 橙色,耗尽红色

### Phase 3 改造(2026-05,Apple Photos 接线 + UX 完善)

1. **Apple Photos source 完整接入**
   - `sources/photos_library.py` 走 osxphotos:`iter_photos` 用 `p.uuid` + `p.path`,`iter_faces` 解 `face_info` + EXIF orientation bbox 变换(标准 9 宫格)+ `fi.name` 拿真名 → `cluster_id='apple:<name>'`
   - 跳过 InsightFace detect + cluster(信任 Apple 自带,但**留 fallback**:`face_info=[]`时返 `None` 走 InsightFace 兜底,实测 Apple 偶有漏识别)
   - `web/api.py` `_build_source` 接 `photos_library` kind + `POST /sources` 含 TCC 探测(失败 403 引导用户开权限)
   - `cli/main.py` `lens scan <path>` 路径以 `.photoslibrary` 结尾时自动用 ApplePhotosSource
   - **osxphotos pin <0.65**(0.65+ 用 runtime `X | None` 语法,Py 3.9 import 直接 TypeError)

2. **Faces 页 source 区分** + Browse 排序
   - Faces 卡 cluster_id 前缀徽标:`apple:` 蓝(Apple 命名)/ `apple_face:` 浅蓝虚线(Apple 检测未命名,在我这里命名无意义引导回 Photos.app)/ `seed_` 绿(种子人物)/ `c_` 灰(InsightFace 自动)
   - `repo.list_photos` 排序 fallback:`COALESCE(NULLIF(captured_at_utc,''), captured_at_local)` 解决 iPhone 无 OffsetTime 照片被甩到尾巴

3. **Phase A 进度提示** + 扫描 UX
   - `Progress.phase` 三态:`pending → enqueueing → processing`
   - Phase A 入队期(几万张需要 5-10 分钟)前端显示"正在遍历图片库提取照片元信息……" 转圈,不再卡死无反馈

4. **iCloud 备份脚本** `scripts/backup_to_icloud.py`
   - WAL-safe sqlite3 .backup + journal_mode=DELETE 防 wal/shm 副产物污染目标
   - rsync -a 增量同步 seeds / reports / AGENT_GUIDE.md
   - 不备份 `.cache/preprocessed/`(5-6GB 缓存可重生)

5. **scripts/make_cities_map.py** — 一次性地图导出
   - db 查 city 聚合 + 高德 tile + WGS-84→GCJ-02 转换 + 圆点按 sqrt(count) 缩放

### Phase 4 改造(2026-05,语义检索 + chat 体验大改)

1. **语义向量检索**(bge-small-zh-v1.5 + fastembed)
   - `life_lens/embed/`:Embedder 单例 + `build_source_text`(拼 description + scene + tags + **objects** + mood + actions,objects 漏了召回 fail,见 #6)+ `text_hash` 增量
   - `photo_embeddings` 表 (photo_id PK + model + dim + vec BLOB + text_hash + updated_at)
   - `scripts/build_embeddings.py` 回填,**~87 张/s**(M 系列 Mac onnxruntime + CoreML)
   - `life_lens/query/semantic.py`:`SemanticIndex` 全加载内存 brute-force 余弦(15w 张 × 512 维 = 300MB,~50ms/query)+ `rrf_merge_multi` N 路 RRF
   - **不用 sqlite-vec** — brute-force 在 30w 张以内完全够,精确召回 + 零依赖
   - **fastembed 装的时候要 `pip install 'httpx[socks]'`** — 用户机器走 SOCKS 代理,默认 httpx 不带 socksio
   - **fastembed 装会降级 pillow 到 10.4**(Py 3.9 上 pin <11.0),但 pillow-heif 要 ≥11.1 — 装完立即 `pip install 'pillow>=11.1'` 升回(pip 警告冲突但运行时 OK,因为 fastembed 只在 image embedding 路径用 pillow,我们只用 text)

2. **search_photos 改 hybrid + 多 query 扩词**
   - FTS5/LIKE + 语义两路并跑,RRF 合并;`time_from/to` 非空时合并后按 `captured_at` 倒序重排("最近"问题命中最新)
   - `persons_mode='OR'`(默认)/`'AND'`(合影场景):用 EXISTS 子查询确保每个 group 都在;同名跨 source 多 cluster_id 折叠为 OR 一组
   - `query_expansions`:LLM 主动扩词(`"裙子" → ["连衣裙","长裙","短裙"]`),N+1 query 各跑 hybrid,RRF 合并。解 dense embedding short-query vs long-doc 弱点
   - `_resolve_persons_grouped` 返 `(groups, unrecognized)`:AND 模式遇到未识别人名严格返 0(避免退化成 OR 误导)

3. **Round 1/Round 2 prompt 大量补充**
   - 模糊时间词解析约定:"最近"=180 天 / "最近一个月"=30 天 / "今年"=按 today 算 / 不写死 2024
   - `persons_mode='AND'` 教学 + 例子("X 和 Y 的合影"/"X、Y、Z 一起去过哪")
   - `query_expansions` 教学(具体物品扩 3-5 个,人名地名不扩)
   - **photo_id 完整复制铁律**(LLM 偏好截 8 位短 hash,实战必加)
   - **轻量校验指令**:引用前内心评估 description+tags+objects+scene 是否真符合用户意图,不符合不引用,全部不符合按"结果为空"诚实回答

4. **chat jsonl 日志** `~/.life_lens/chat_log/YYYY-MM-DD.jsonl`
   - 每次 /api/chat 记 question / plan(Round 1 args) / result summary / answer / refs / error
   - 用户报"我刚问 X 怎么回答 Y" 直接 tail 日志,不用截图

5. **前端可观察性 + UX**
   - chat 工具调用 trace 框:每条回答上方显示 action + args + rationale + 命中数 + 引用数(+ 未引用数)
   - 缩略图 onerror retry + 红边占位显示 id 前 8 位(诊断 LLM 幻觉 / 瞬态 405)
   - **photo_id 前缀补全**:LLM 偶发截短 8 位时,前端用 candidates 做唯一前缀匹配自动补完整(代码兜底,不依赖 prompt)
   - 回答下方"未引用候选"折叠区(惰性加载),解决"搜到 20 张只显示 4 张"困惑
   - chat 正则 `[photo:xxx]` 支持 Apple uuid 带短横

6. **Apple Photos 漏识别脸延后修**(数据已扫,等扫完批量 reprocess)
   - 见 [project_life_lens_apple_face_misses](path) memory:扫完用 SQL 反查 `face_count=0 AND vision.subject IN (single,portrait,group)` 的照片,批量 reprocess faces + vision

**Phase 1 历史功能**(继续工作):
- `query/search.py` + `query/aggregate.py` — 查询层共用,Web Chat / REST 都调
- `web/llm.py` LLM provider 抽象(`claude-p` + `openai-compat`),多 provider + 前端切换
- `geocode/amap.py` — 高德 reverse geocoding,精确到 AOI/POI
- `lens backup` 子命令 — WAL-safe 快照
- `AGENT_GUIDE.md` — 给第三方 LLM agent 看的 db 查询手册

**未做**:MCP server(等 Python ≥ 3.10)、CLI `lens query`、EXIF GPS→时区根治、sidecar JSON 导出、jieba 分词支持单字搜索、视频(MOV/MP4)、Apple 漏脸批量补、chat 评测集(对应 vision 那套)。

### Phase 6 改造(2026-05-18,语义索引 inline 化)

1. **embedding 写入挪进主扫描流水线**(`store/repo.py::update_embedding`)
   - `scanner/runner.py::_process_phase`:`update_fts` 之后立即调 `update_embedding`,跟 FTS 一起增量更新
   - `scanner/reprocess.py`:reprocess vision 写 db + `update_fts` 之后同样跟一次 `update_embedding`(description 变了 → text_hash 变 → 自动重 embed)
   - **idempotent**:`text_hash` 同就跳过(reprocess derived 时 vision 没变,不会浪费)
   - **embedder 进程级 singleton**(`runner._get_embedder_for_scan`):首次扫描 ~3s 加载 fastembed,后续 run 复用。装失败 → log WARN once,后续静默跳过(scan 不阻塞,FTS 仍 work)

2. **Web 端"重建语义索引"兜底**(`配置 > 存储`卡片)
   - `GET /api/embeddings/status` 返 `{total_with_vision, total_indexed, missing, last_built_at, model, rebuild}`
   - `POST /api/embeddings/rebuild` body `{force?: bool}` 后台线程跑,`_embedding_rebuild_state` 模块级 dict + lock 记进度
   - UI:覆盖率展示("M / N 张" + 百分比)+ 两个按钮("补建 X 张缺失" / "全量重建" — force 用于换模型 / 改 `build_source_text`)+ 运行中 2s 轮询进度条
   - **兜底场景**:fastembed 装失败的存量照片 + 老 db 没 inline 写过的历史数据 + 换模型 / 改拼装规则

3. **彻底消除 SemanticIndex count-only invalidation 的隐式坑**
   - 之前 inline 没写入时,新扫的照片只在 FTS 里,语义召回查不到,但 chat 跑出来"有结果"(纯 FTS 兜底)→ 用户以为"语义索引在自动更新"
   - 现在每张照片扫完都让 `photo_embeddings` 行数 +1 → `SemanticIndex.get_semantic_index` 的 count-only invalidation 会自然触发重建,语义路径永远和 FTS 路径同步

**Phase 6 未做**:`scripts/build_embeddings.py` 仍保留(纯 CLI 场景 / debug);未来考虑 schema_version bump + 启动时检测 `total_with_vision - total_indexed` > 阈值 → banner 提示"语义索引落后,点这里补建"。

### Phase 5 改造(2026-05,开源发布 — 已完成 ✅)

1. **全文脱敏 + 开发纪律** — 真实人名 → 占位符(张三/小明/王女士/李四/赵阿姨/小华/李丽),公司/园区名 → "某园区",路径硬编码 → `Path.home()` / env override。CLAUDE.md 末尾"不要做的事"加规则:今后代码 / prompt / 文档教训禁用真名 + 真实小景点(公共景区/地标可保留),**源头治理优先**,publish.sh grep 只是 last line of defense。
2. **db schema 版本号 + 升级自动备份** — `db.py::SCHEMA_VERSION = 1` + `PRAGMA user_version` 追踪;升级前自动 `sqlite3 .backup` 到 `~/.life_lens/backups/auto-pre-vN-from-vN-1-{ts}.db`。`init_schema` 智能跳过空 db(检测 photos 表是否存在),只对真升级触发备份。
3. **A 层 pytest 测试基础设施(无 LLM 依赖)** — `tests/` 下 conftest + test_db_migration / test_query / test_api_smoke / test_no_private_data,共 25 测试,跑 ~3s。CI 友好,GitHub Actions 也能跑。**B 层 AI 质量评测**(vision/chat eval)继续留私有 monorepo。
4. **scripts/publish.sh 发布脚本** — rsync 抽取(12 个 exclude 覆盖 sample/seeds/.cache/.git/ground_truth.yaml 等私有数据)+ 隐私 grep + pytest 三段。**不自动 push**,只提示人工 cd 到镜像目录 `git add + commit + push`。
5. **用户面文档** — README 重写(57 → 141 行,完整 Ollama 装法 + venv + 配置 + 首次跑通 + 已知坑) + LICENSE (MIT) + config.example.json(3 provider 示例) + tests/eval/ground_truth.example.yaml(占位结构) + CHANGELOG.md(0.4.0 模板)。
6. **`ground_truth.yaml` 策略** — **保留私有 monorepo 跟踪**(便于备份),由 publish.sh rsync `--exclude` 挡公开。比"gitignore + 不跟踪"更稳:私有数据想要 git 备份就 git 备份,发版时 exclude 把关。

7. **Web UI 5-tab 重构(新手友好)** — 老的 8 tab(数据源 / 扫描 / 运行历史 / 重跑 / 浏览 / 人物 / 问相册 / 状态)精简成 5 个:**配置 / 面孔 / 扫描 / 浏览 / 问相册**。
   - **配置 tab** = 平铺 5 张卡片(`#ollama` `#amap` `#llm` `#sources` `#storage`),每张含步骤号 + 标题 + 副标题 + 状态灯。未配时展开显示引导文案 + 官方链接,已配显示当前状态。**不做 modal wizard** — 卡片本身就是引导
   - **面孔 tab** = 独立顶层(种子人物 + 未命名 cluster 命名)— 因为这块功能复杂,跟"配置依赖"性质不同
   - **扫描 tab** = 顶部 pill segment(当前扫描 / 运行历史 / 批量重跑)— 把老 3 个 tab 合并
   - **顶部 banner** = 依赖未配齐时显示 ⚠ "还需配置: Ollama / 高德 / ..." + "去配置 →" 按钮;配齐隐藏
   - **tab 兼任页面标题** — 老每个 section 的 `<h2>` 全删,大号 underline tab(17px + 加粗 + 蓝色 underline active)就是页面标题
   - **header 品牌区** = "Life Lens · 人生透镜" + 副标题"用 AI 把你的照片库变成真正可以检索的人生记录"(flex baseline 对齐)
   - **localStorage 记 last tab** — 切 tab 时 `localStorage.setItem('life_lens.last_tab', ...)`;启动时优先恢复,localStorage 没值才看 `next_step`
   - **macOS 原生文件夹选择器** — 数据源卡片"📁 选文件夹"按钮 → `/api/sources/pick-folder` → 后端 `osascript -e 'POSIX path of (choose folder ...)'` 弹原生对话框,选完路径回填输入框

8. **LLM 简化:砍 claude-p,统一 RESTful API(BREAKING v0.4)** — `web/llm.py` 移除 `kind="claude-p"` 子进程方案(原本用本地 `claude -p` 调 Claude Code),只保留 `openai-compat`。理由:claude-p 依赖本地装 Claude Code,只对作者方便,开源用户大概率没装;DeepSeek v4-flash 对相册 chat 完全够用。
   - 启动时检测旧 config 含 `kind="claude-p"` provider → **自动忽略 + 日志 WARN**(不崩,但用不了那个 provider)
   - `config.example.json` 删 claude-opus,deepseek 升 default
   - `tests/test_no_private_data.py::test_no_subprocess_claude_in_llm` 守住:`llm.py` 不能 import subprocess,config.example 不能含 `kind="claude-p"`

9. **统一 config 读写 + 探活模块** — 后端基础设施给配置 GUI 用:
   - `store/config.py`:`load_config` / `save_config`(原子写 tmp + os.replace + chmod 600)/ `update_amap_key` / `update_llm_provider` / `remove_llm_provider` / `set_llm_default` — 保留 `_comment` 等未知字段
   - `vision/ollama_probe.py::ping()`:HTTP GET `/api/tags`,返 `{ok, models, has_vision_model, vision_model_name, error?}` — 给 UI 探活卡片用
   - 6 个新 endpoint:`GET /api/setup/status`(聚合就绪检查 + `next_step` 提示默认 tab)/ `GET /api/ollama/ping` / `POST /api/config/amap-key`(写)+ `/api/config/amap-key/validate`(用固定北京坐标试调一次,验 HTTP 200)/ `POST /api/config/llm-provider`(强制 kind=openai-compat,旧 claude-p 被拒)/ `POST /api/config/llm-default`
   - `web/llm.py::_load_config` + `geocode/amap.py::get_amap_key` 都改走 `store.config`,统一来源

10. **config 跟 `--root` 走(支持多 root 并行)** — `store/config.py::get_config_path()` 读 `LIFE_LENS_ROOT` env,fallback 到 `Path.home() / ".life_lens"`。`web/server.py::run()` 和 `cli/main.py::main()` 都在启动早期 `os.environ["LIFE_LENS_ROOT"] = str(root)`。**意义**:同一台机器可以 `--root ~/.life_lens_work --port 7879` 同时跑私有 + 测试两套,db 和 config 完全隔离。tests 走 `monkeypatch.setenv("LIFE_LENS_ROOT", str(tmp_path))` 替代之前的 attr patch,更稳。

11. **一行命令安装 `install.sh`(2026-05-18 上线)** — `curl -fsSL https://raw.githubusercontent.com/uxtracer/life-lens/main/install.sh | bash`。脚本只做:Python ≥ 3.9 检查 → git clone → venv → `pip install -e .` → `pip install numpy insightface onnxruntime opencv-python fastembed`(pyproject.toml 没声明,避免新手 pip install 卡 GB 级 wheel)→ pillow 修复(fastembed 把 pillow 降到 10.4,pillow-heif 要 ≥11.1,显式 `pip install 'pillow>=11.1' 'httpx[socks]'`) → `nohup lens serve --port "$PORT"` + 轮询最多 30s 确认起来 + 自动 `open` 浏览器。**不接管** Ollama / 高德 key / LLM key — 留给 web 配置 tab 引导。env 覆盖:`LIFE_LENS_REPO` / `LIFE_LENS_HOME` / `LIFE_LENS_PORT`。

12. **README 重写(外部用户视角)** — 三段:**是什么**(含隐私设计表格)/ **怎么用**(install.sh 一行 + 浏览器开了后看配置 tab 卡片引导)/ **设计思路**(架构图 + 6 个关键决策 + 不做什么)。中文段落统一中文标点(逗号 ,/ 句号 。/ 冒号 :/ 分号 ;/ 问号 ?/ 感叹号 !)。**老 README 的"安装"分步骤章节全删** — 新手只看 install.sh 一行,进阶用户去看 CLAUDE.md。

**已首次公开发布 + 多次迭代**:`aa38c00`(v0.4.0 初版)→ `7ef2bda`(UI 优化 + config 跟 root)→ `b18e61d`(文案微调)→ `612dbd5`(陆续)→ `bc297f4`(install.sh + README 重写)。后续直接 `bash scripts/publish.sh && cd ~/claude/life_lens-public && git add . && git commit && git push` 即可发新版。

### 开源发布流程(双目录,显式 ceremony)

公开仓:https://github.com/uxtracer/life-lens

| 目录 | 用途 | git remote |
|---|---|---|
| `~/claude/life_lens/` | 私有开发主战场,monorepo 子目录 | uxtracer/claude(私有) |
| `~/claude/life_lens-public/` | 公开镜像 working copy(独立 git) | uxtracer/life-lens(公开) |

**发版流程**(私有→公开):
```bash
bash scripts/publish.sh
# A. rsync --delete 抽取私有 HEAD → 镜像目录(exclude sample/seeds/.cache/ground_truth.yaml 等)
# B. 隐私 grep 兜底(任一命中 fail)+ pytest A 层(任一 fail 即停)
# C. 提示人工 cd 镜像目录 → git status / diff / add / commit / push(脚本不自动推)
```

**已知边界**:
- 私有 monorepo history 有真名,公开 repo 首次 `git init` 从干净 HEAD 重新开始 — **不保留 history**(开源 = 干净的代价)
- 后续两边各自演进,**不双向同步** — 公开 PR 进来我手动 cherry-pick 回私有
- 私有 commit 随意,公开按 release ceremony,频率我自己决定

## 推荐工作流(重要)

**先建种子人物再扫描** — 不要反过来。原因:pipeline 顺序是 face → vision,vision 两次调用(description + struct)都注入 set-of-mark,description 一开始就用真名。如果先扫再补种子,需要手动触发 reprocess vision(每张 ~22s,Ollama KV cache 命中后第二次调用快)才能让 description 用上人名。

GUI 流:Sources 加目录 → Faces 添加种子人物 → Scan 触发扫描 → Browse 查看。

## 踩过的坑 + 实测数据(防后续 Claude 重蹈覆辙)

### Ollama num_ctx 默认 2048 会静默截断 ⚠️

Ollama 默认 `num_ctx=2048`,**不显式设置就会截断 prompt**。多模态调用尤其严重:
- 1024×768 图片 ≈ ~800 image tokens
- 我们的 prompt ~500-1000 tokens
- `num_predict` 留 1024 输出空间
- 总计 4-5k,远超默认 2048

后果:prompt 开头的 set-of-mark mapping 会被切掉,LLM 看到图但不知道编号对应谁。

**解决**:`vision/ollama.py` 显式设 `num_ctx=16384`(qwen3-vl 8b 支持 256k,16k 安全留余量)。

### 8B 小模型 instruction following 弱,长 prompt 注意力分散 ⚠️

实测:把 6 条铁律 + JSON schema + set-of-mark mapping + 人名约束塞 1500 字单 prompt,LLM 会**忽略人名约束**用代号写 caption。

**根本对策**:**拆调用**。`describe_description` 和 `describe_struct` 各管一件事,每个 prompt ~300-500 字,LLM 能专注。这是 v9 的核心改造,不能回退到单次调用。

类似的:任何"加新字段"的诱惑都要先想清楚是放进现有 prompt(可能拖累质量)还是再拆一次。

### 拆调用没有 2x 慢 ✅

预期 2 次调用 = 2x 时间,实测**只多 10-20%**(20s → 22s)。原因:Ollama 同一进程同一张图的第二次调用,**image tokens 的 KV cache 命中**,只需重跑文本部分。

意外的好处:拆调用代价比预期小很多,**性能担忧不成立**。

### set-of-mark prompting 的注意事项

`vision/annotate.py` 在 JPEG 上画红框 + `[N]` 编号:
- ✅ LLM 能识别(实测 qwen3-vl 8b 直接问"图上几个红框"回答正确)
- ⚠️ 框/编号会污染 OCR 字段 — 必须在 prompt 里说"ocr_text 不要包含 [N] 编号"
- ⚠️ 不要在 prompt 末尾说"不要描述方框/编号本身",否则 LLM 会过度执行,直接忽略红框 → 不识别人名。要说"用红框 + 编号识别每个人,但 caption 文本里不出现'方框/编号'词汇"
- ⚠️ struct prompt 不需要"必须用真名"约束(name 字段后端用 cluster_id 组装),只需要 actions 按编号顺序

### 跨年龄人脸 max-pooling 设计

`faces/cluster.py` 用 **max-pooling 而非 mean centroid**。如果一个 person 的种子图跨年龄(孩子 5 岁 + 8 岁照片),mean 会拉偏(centroid 变成"四不像"),max-pooling 保留每张种子作为独立判据,新脸总能匹到最相近的那张。这是关键设计,不要随便改回 centroid。

### 背影 / 远景小人影 InsightFace 看不到 ✅(已知局限,接受)

任何 face recognition 系统的硬限制(Apple Photos / Google Photos 也一样)。不要花精力解决 — 靠时间窗 + GPS + 同次合影里的正脸推断即可。

### MCP Python SDK 要求 Python ≥ 3.10,本机 3.9.6 装不了 ⚠️

`pip install mcp` 报 `Requires-Python >=3.10`,所有版本都不行。**临时方案**:web chat 走手写两轮 `claude -p` tool loop(`web/chat.py`),不依赖 MCP。等 Python 升级后再补 MCP server(给 Claude Code/Desktop/Cursor 用)。

教训:写 pyproject `[optional-dependencies].mcp = ["mcp>=0.9"]` 之前应该先检查目标 Python 版本兼容性。

### iPhone EXIF 没 OffsetTime → captured_at_utc 是空字符串 ⚠️

iPhone(尤其早期 iOS)EXIF 里**经常没有 OffsetTimeOriginal tag**,只有 DateTimeOriginal(local 时间)。当前 `exif/extract.py` 没 OffsetTime 时 `captured_at_utc` 留空。

**症状**:`search.py` 的时间窗过滤 `WHERE captured_at_utc >= ?` 把所有照片筛掉(空字符串字典序比任何 ISO 日期小)。Chat 里问"最近三个月" → 0 张。

**当前修复**(query 层兜底):`COALESCE(NULLIF(captured_at_utc,''), captured_at_local)`,**立竿见影,不用重跑 EXIF**。

**TODO 根治**:`exif/extract.py` 在有 GPS 时按经度估时区(中国全境 +08:00),把 utc 算出来存进 exif JSON。

### Web Chat 架构 — 两轮 `claude -p` 子进程(不是 MCP)

`web/chat.py::chat()` 工作流:
1. **Round 1**(ROUND1_SYSTEM):用户问题 → 严格 JSON `{action, args, rationale}`。Python `_extract_json` 容忍 ```json fence 和首末 `{}`
2. **Python dispatch**:`_dispatch(conn, action, args)` 调 `query/search.py` 或 `query/aggregate.py`,**LLM 不写 SQL**(安全 + 可控)
3. **Round 2**(ROUND2_SYSTEM):喂用户问题 + Round1 args + 查询结果 JSON,流式中文回答 + `[photo:xxx]` 标记
4. SSE 帧名:`phase / planned / result / chunk / done / error`,前端正则抠 photo_id 渲染缩略图

**铁律**:LLM **不**写 SQL,LLM **不**操作数据库连接,LLM 只填白名单字段(query/time_from/time_to/persons/location)。任何"让 LLM 直接出 SQL"的诱惑都要先拒掉。

### 高德 reverse geocoding 设计要点

- key 放 `~/.life_lens/config.json`(`chmod 600`,**不进 git**)或 env `AMAP_KEY`
- **必须做 WGS-84 → GCJ-02 转换**(`amap.py::wgs84_to_gcj02`):iPhone EXIF 是 WGS-84,高德用 GCJ-02,中国境内偏移 **300-700m**。之前以为"AOI 范围远大于偏移"可以省掉转换 — **错的**,偏远小景点(音乐厅、寺庙、特定建筑这种几十米直径的)被偏移直接定位到几百米外,丢失 POI(实测金山岭阿那亚音乐厅 IMG_0626 偏 ~600m 完全错过,转换后命中 dist=24m)。国境外不转换(`_out_of_china` 自动跳过)
- **网格缓存**:50m 精度(`GRID_STEP_DEG=0.0005`,~55m),坐标量化为 0.0005 整数倍做 key;**用 WGS-84 做 key**(用户输入侧不变,语义清晰),转换发生在缓存 miss 后调 API 前。表 `geocode_cache` (PK = lat_grid, lng_grid, provider)
- **provider 字段做版本隔离**:当前 `'amap-gcj02'`(已转坐标 + 50m 网格)。历史值 `'amap'`(11m 未转坐标,错误)、`'amap-50m'`(50m 未转坐标,错误)保留但不再命中,可后续清理
- **直辖市坑**:北京/上海/天津/重庆的 `addressComponent.city` 是**空数组**,要 fallback 到 province
- **行政边界**:金山岭长城横跨北京密云/河北承德,GPS 微小偏差就跨省,同一次旅行的照片 city 字段会分散 — 不是 bug,接受
- **配额管理**:`amap_quota` 表按 Asia/Shanghai 日期记 count,免费 5000/天上限设 `AMAP_DAILY_LIMIT=4800`(留 200 buffer)。耗尽时 `reverse_geocode` 直接返 None(不发 HTTP),`runner._process_phase` 在每张完成后 check `is_quota_exhausted` → 暂停整个 run。`/api/scan/resume` 同样拦截,次日 UTC+8 0:00 自动恢复
- 失败/无 key 时 **graceful degradation**:`location_bucket` 字段全 null,不阻塞其他 derived 字段

### Windows 上 `Path.read_text()` 默认编码不是 utf-8 ⚠️

Python `Path.read_text()` 不指定 encoding 时,**Windows 用 cp1252/gbk,Mac/Linux 用 utf-8**。我们 `config.json` 含中文 label("快+便宜"),Mac 没事,Windows 上解码失败被 try/except 静默吞掉,fallback 到内置 default,用户在前端只看到 claude-opus 这一个 provider,**完全猜不到根因**。

修复:**所有 `read_text()` 显式 `encoding="utf-8"`**(目前 `web/llm.py` + `geocode/amap.py` 已改)。

未来写新代码读任何配置/JSON/文本文件,**永远显式 utf-8**,Mac 测出不来的 bug 在用户 PC 上会炸。

### DeepSeek 模型命名:`deepseek-chat` 是 `deepseek-v4-flash` 的别名 ⚠️

`GET /v1/models` 只列权威名(`deepseek-v4-flash` / `deepseek-v4-pro`),但 `deepseek-chat` 这个旧名 DeepSeek 保留兼容。POST 时传 `deepseek-chat`,返回的 `model` 字段是 `deepseek-v4-flash`。

**实操建议**:config 里写权威名(`deepseek-v4-flash`),不要写别名 — 看 config 就知道实际跑啥。

### Reasoning 模型(`deepseek-v4-pro` / OpenAI o1)有 `reasoning_content` 字段 ⚠️

Reasoning 模型 API 返回里 `message.content` 是最终输出,`message.reasoning_content` 是思考过程。流式时 reasoning 阶段没 content delta,前几秒前端会"卡"。`max_tokens` 不够时 thinking 阶段就耗光,content 是空。

实测 `deepseek-v4-pro` 对相册 chat 场景**没有比 `deepseek-v4-flash` 更好**,但慢 + 贵 5-10x。**默认用 flash**,pro 留作未来推理密集任务。

我们当前 `web/llm.py::_openai_compat` 流式只读 `delta.content`,reasoning_content 会被丢掉(就是用户视角"前几秒静默")— 是设计内行为,不需要 fix。

### InsightFace age/gender 对儿童不准 ⚠️

InsightFace buffalo_l 的 `genderage.onnx` 在**正脸成人**上挺准,但**对儿童(< 14 岁)经常错**:
- 实测 11 岁男孩(小明)种子图平均 → age=23, gender='F'(被估成 20+ 岁女)
- 正面成人(张三 ~40 岁)→ age=37, gender='M' ✓ 准
- 侧脸/远景小脸 → 经常 None / 严重偏差

**对策**:
1. age/gender 存到 `persons` 表的 `age_estimate / gender_estimate`(种子图平均 + 多数投票)
2. vision prompt 把这些标成"**参考估计(可能不准,以视觉为准)**" — 给 LLM 一个 hint 但不当真值
3. **不依赖**它做关键决策(比如不能用 gender 推断"男士/女士"),只作辅助 hint
4. 真正锁定 [N]→人 映射的是 **bbox 位置 hint(从坐标算,100% 准)**

### 高德 POI 排序按权重不是距离 ⚠️

高德 v3 regeo 返回的 `pois` 数组**默认按权重排序**(主景区名排前面),不是按 distance。
- 例:阿那亚金山岭景区里,pois[0]='阿那亚金山岭' dist=418m,pois[2]='阿那亚山谷音乐厅' dist=24m
- 取 `pois[0]` 会让所有这片照片都显示成"阿那亚金山岭"(景区粗粒度)
- 真实需要 — 把照片归到最近的具体 POI("音乐厅"/"泳池"/"停车场")

**对策**:`_extract` 用 `min(pois, key=lambda p: float(p.get('distance') or 9e9))` 取距离最近的。

### WGS-84 → GCJ-02 必须做(中国境内偏 300-700m)⚠️⚠️

iPhone EXIF 是 WGS-84(GPS 国际标准),高德用 GCJ-02(中国"火星坐标"),**直接传 WGS-84 给高德有 300-700m 偏移**。
- 偏远小景点(音乐厅、寺庙、特定建筑这种几十米直径的)被偏移**直接定位到几百米外**,丢 POI
- 实测 IMG_0626 真实在音乐厅,WGS-84 直接传 → radius=500m POI/AOI 全空;转 GCJ-02 后 dist=24m 命中音乐厅
- 早期 CLAUDE.md 写"AOI 范围远大于偏移可以省掉转换" — **错的**,推翻

**对策**:`amap.py::wgs84_to_gcj02` 标准火星坐标转换公式(20 行,无外部依赖),`_out_of_china` 自动跳过国境外。**入口前转换**,网格 cache key 仍用 WGS-84(用户输入侧不变)。

### `place_name` / `formatted_address` 不入 cache,出口实时派生 ⚠️

之前 `_extract` 把这两个派生字段写入缓存,改了"POI 优先 vs AOI 优先"策略要全 cache 失效重调高德 → 浪费配额。

**对策**:缓存只存 raw components(country/province/.../aoi_name/poi_name),`_derive_place_and_formatted(rec)` 在 reverse_geocode 出口实时拼。**改策略不必失效 cache**,几次 reprocess derived 走纯缓存 0 配额跑完。

### FTS5 `unicode61` tokenizer 不索引中文 ⚠️

SQLite FTS5 默认的 `unicode61 remove_diacritics 2` 对**所有 CJK 字符忽略不索引**(即使 categories='L* N* Co' 也不行)— 中文搜索 `MATCH '观光车'` 永远返 0 张。

**对策**:`tokenize='trigram'`(SQLite 3.34+ 内置)— 中文按 3 字符滑动窗口索引。**限制:搜索词 ≥ 3 字符**(2 字"长城"匹配不到任何 trigram)。
- `search.py` query 短词 fallback LIKE
- db.py 启动检测旧 unicode61 schema → DROP + CREATE trigram + 回填全部 FTS 行

### scan_runs.snapshot 列防 reprocess 偷 jobs 的副作用 ⚠️

reprocess 把 jobs 行的 `run_id` 改成新 run_id(让 reprocess 进度能实时查 job_stats)。**副作用**:原 run 的 jobs 被偷走后,实时查 `time_range / scanned_up_to` 全 null,历史 run 进度卡显示空白。

**对策**:
1. `scan_runs` 加 3 个快照列:`snapshot_captured_min / max / scanned_up_to`
2. `finish_run` 不主动写(避免覆盖); `set_run_time_range`(enqueue 完成时一次性冻结)+ `bump_run_scanned_up_to`(每张完成时增量推进)
3. `get_run` 读取时优先 snapshot,fallback 实时查

### self-check 字面匹配的固有局限 ⚠️

`vision/role_check.py` 用 bigram + char 字面匹配 description 邻域,**对同义表达无能为力**:
- struct = "举手机自拍" / "侧头看向镜头"
- description = "面带笑容" / "表情平静"
- 两个都对(都是张三/小明的合理描述),但字面对不上 → flag 误报

**对策**:
1. 前端 banner **降级为浅蓝提示**(不是橙黄警告),文案"可能是同义表达,需核对"
2. "已核对,忽略此提示" 按钮 → meta.errors 加 `acknowledged: true`,Browse 详情不再显示
3. `select_photos(missing_field='role_mismatch')` 过滤已 acknowledged 行
4. 重跑 vision 时 self-check 重跑,acknowledged 标记清除(LLM 输出变了重新评估)
5. 真错位(IMG_0685 那种"白T 写给男孩")**仍能被 catch** — self-check 价值是"广撒网" + 评测集精准对账

要彻底消除误报得换 LLM-based self-check(让 LLM 看 description + struct 判断是否一致),但成本贵 — 每张多一次 LLM。**当前不做**。

### LLM 偏好截短 photo_id 到 8 位短 hash ⚠️

Round 2 LLM 在写 `[photo:xxx]` 引用时,部分模型(尤其非顶级)偏好像 git commit short hash 那样**截到 8 位前缀**(比如 `[photo:19D5A112]` 而不是完整 36 字符 Apple uuid)。结果前端 `<img src="/api/thumb/19D5A112">` 直接 404 → 红框。

**两层防御**:
1. **prompt 铁律**:ROUND2_SYSTEM 写"photo_id 必须完整复制" + 给两种格式示例(Apple 36 字符 + filesystem 16 字符) — 减少但不消除
2. **前端 candidates 前缀补全**(`app.js::_fixPhotoId`):拿 result 帧的所有 candidate id 做集合,LLM 写的 id 不在 → 找前缀唯一匹配的完整 id → 自动补全。代码兜底,不依赖 prompt

教训:**只靠 prompt 教不稳,关键不变量必须程序层守住**。

### LLM 还会**伪造**完整格式合法的 UUID(不是截短,是脑补)⚠️⚠️

比截短更隐蔽的一种幻觉:Round 2 LLM 看到 candidates 里几十条 36 字符 Apple uuid,会自己**脑补出新的、格式完全合法、但 db 里根本不存在**的 uuid。例如 candidates 有 `A2E2FB42-1B19-464C-90DE-51839B951DE9`,LLM 回答里冒出 `043B999D-E42B-4E04-9B89-86C2A08AF3A8` — 看上去毫无破绽,但根本不在候选集里。

`_fixPhotoId` 的前缀补全救不了这种 — 它能修"截短了真 id",但没法把假 id 改回真 id。

**对策**(`app.js::_fixPhotoId` + `renderAssistantBody`):
- `_fixPhotoId` 返三种值:**真 id**(在候选 / 唯一前缀匹配 → 渲染)/ **原 id**(候选集为空无法校验 → 渲染,onerror 兜底)/ **null**(候选集非空 + 不在 + 无前缀匹配 → 视作幻觉)
- `renderAssistantBody` 用 `.filter(Boolean)` 把 null 丢掉,**不画红框感叹号**
- 副作用:回答里描述了照片但 id 是幻觉的,那张缩略图静默消失,用户只看到文字 — 比红框诚实,但说明 LLM 的引用不可全信

更狠的根治得改 Round 2 prompt 加"只能从给定列表里抄 id,不能凭格式生成新的"显式约束,或者用 schema-constrained 生成(structured output)。当前对策只是降级到"看不见就当没说",不解决幻觉本身。

**另一个连带 bug**(同一次修复):`renderAssistantBody(div, buf)` 在 chunk 流式时**漏传第 3 参数 `candidateIds`**,导致前缀补全在所有流式渲染期间完全 short-circuit 没机会跑(`if (!candidateIds) return id`)。**任何"程序层兜底"必须验证调用现场真的传了参数,否则 prompt 教不稳 + 兜底未生效 = 双重失守**。

### `_resolve_person_filter` 同名 dict bug 漏 cluster ⚠️⚠️

老实现 `name_to_cid: dict[str, str]` 是单值字典,**同一个 name 多次出现时只保留最后一个 cluster_id**。db 里"张三" 有 2-3 条(InsightFace `seed_xxx` + Apple `apple:张三` + 历史 `c_xxx`),loop 完只剩 `apple:张三`,**漏掉 seed_xxx cluster 命中的所有照片**。

这个 bug **影响所有按人查询路径**(search_photos OR / AND / places_visited),老版本悄悄漏数据 — 用户问"张三的照片"返 ~30 张实际应该 ~130 张。

**修法**:`name_to_cids: dict[str, list[str]]`,所有 cluster_id 都返。同时 `_resolve_persons_grouped` 返 `(groups, unrecognized)` tuple,**AND 模式 unrecognized 非空 → 严格返 1=0**(避免"张三和王女士"因王女士不存在退化成只看张三)。

教训:**dict 单值键覆盖**是 Python 编程经典坑,跨 source 数据合并时格外小心。

### `time_to` day-only 字典序坑 ⚠️

LLM 传 `time_to="2026-05-04"`(只到日)期望含整天。但 SQL 字符串字典序:
`'2026-05-04T12:42:24' <= '2026-05-04'` → **False**(`T` ASCII 0x54 > `'\0'`)

→ 5/4 当天 9 张全部被排除,只剩 5/3 那 1 张。用户问"张三最近"返"只 1 张"。

**修法**:`_build_extra_filters` 检测 `time_to` 长度=10 时自动补 `T23:59:59`。`time_from` 同样情况不用补(`>=` 行为正确,字典序 `'2026-05-04T08:30' >= '2026-05-04'` = True)。

### dense embedding short-query vs long-doc 弱点 ⚠️

bge-small-zh 把整段 200 字 description 压成 512 维向量。短 query("裙子" 2 字)跟长 doc 余弦只有 ~0.40,**关键词在 doc 里只占 3 字会被稀释**。

实测:李丽沙滩连衣裙照片(description 写"穿黑色短袖**连衣裙**"),query "裙子" sem 召回排 200 名外。

**两层修复**:
1. **build_source_text 加 objects 字段**(原漏了): objects 是 LLM 写的精炼物品清单 `["连衣裙","草帽","沙滩"]`,关键词浓缩,加进去 sem 召回从 >200 → 108 排名
2. **LLM query expansion**: Round 1 LLM 主动扩词 `"裙子" → ["连衣裙","长裙","短裙","裙摆"]`,FTS 路径 `LIKE %连衣裙%` 100% 字面命中,sem 多 query 取 max。修后排名 rank 2 ⭐

教训:**dense + sparse 互补是标准做法**(dense 解语义近义,sparse 解字面精确)。"召回率 + 精确率 + 透明度三者兼得"靠 hybrid + LLM 把关。

### LLM 优秀行为不是必然,要 prompt 显式化 ⚠️

某 case:用户问"小华穿裙子",hybrid 召回 117 张(都含小华但实际 0 张穿裙子),Round 2 LLM **诚实说"没找到"+ 举反向证据**。这次是好行为,但**靠运气** — 不同模型 / 同模型不同次都可能飘到"硬塞一张"。

ROUND2_SYSTEM 之前只写了"结果为空说没找到",LLM 自己**泛化**到"召回了但实质 0 张匹配"也叫"为空"。

**修法**:加 prompt 显式指令"引用前轻量校验 description+tags+objects+scene,不符合不引用,全部不符合按结果空处理" — 把涌现行为变成必选项。

### Apple Photos `face_info=[]` 偶有漏识别 ⚠️

Apple Photos.app 后台跑脸识别有时**漏检**(早期照片 / 老 iOS 版本 / 后台 indexing 没跑完)。实测某些 vision LLM 写"三人在花丛中合影"的照片,Apple `face_info=[]`。

老 source 实现是 `face_info=[]` 时 `iter_faces()` 返 `[]`(信任 Apple),**跳过 InsightFace**,这些照片完全没人脸数据。

**修法**:`sources/photos_library.py` 改成 `face_info=[]` 时返 `None` → pipeline fallback 到 InsightFace 兜底。代价每张多 0.3-1s,值得。

**已扫数据修复**:用 SQL 反查 `face_count=0 AND vision.subject IN (single,portrait,group)` 批量 reprocess(等主线扫完再做,memory `project_life_lens_apple_face_misses`)。

### `pip install -e` 在 venv 里登记的是源码绝对路径(同 venv ≠ 独立环境)⚠️

测开源版时踩过坑:`cd ~/claude/life_lens-public && source ~/claude/life_lens/.venv_lens/bin/activate && lens` —— 以为这样跑的是公开镜像的代码,实际上 `lens` 这个 entry point 在 venv 里登记的是**私有目录**的绝对路径(因为是从私有那边 `pip install -e .` 装的)。`cd` 只换 CWD 不换 Python 包加载路径。**改私有 app.js,7879 端口立刻看到效果** —— 完全没在测开源版。

**真要测开源版**:在镜像目录建独立 venv `python3 -m venv .venv_lens && source .venv_lens/bin/activate && pip install -e .`,这样 entry point 才指向镜像目录的代码。或者 `PYTHONPATH=. python -m life_lens ...` 借私有 venv 的依赖、用本目录的代码。

教训:**任何"以为隔离了但其实没隔离"的开发陷阱要先验证一次**,最快的验证是去私有改一行明显的 UI 文案,看公开 server 是否秒变 — 变了就是没隔离。

### bash 子命令 vs 全局参数:`lens --port X` 不对 ⚠️

argparse 全局 parser 只接 `--root`,`--port` 是 `serve` 子命令的 add_argument。`lens --port 7879` 会让 argparse 把 "7879" 当成 cmd 位置参数 → 报 `invalid choice: '7879' (choose from 'serve', 'scan', ...)`。

正确:`lens serve --port 7879`。install.sh 第一版踩了,修复后改成显式 subcommand。

教训:argparse subcommand 下的参数**必须**在 subcommand 后面,即使 `cmd = args.cmd or "serve"` 让 `lens` 默认 = `lens serve`,带参数时也必须 `lens serve --xxx`。

### 中文标点别相信键盘输入,用 `chr(0xff0c)` 显式 ⚠️

README 重写时让用户求"中文段落用中文逗号",我前几轮"以为"自己打的是中文逗号 `,(U+FF0C)`,但实际终端 / 键盘输入法 / 文件 IO 经手后变成 ASCII `,(U+002C)`。两个字符在等宽字体下视觉几乎一样,肉眼分辨不出来。

**根治**:Python 转换脚本里用 `chr(0xff0c)` / `chr(0xff1f)` / `chr(0xff1a)` 等显式声明,不依赖源码里的字面量。`re.sub(rf"({CJK_RE})\s*,", rf"\1{chr(0xff0c)}", text)` 永远对,字面量 `","` 永远不可靠。

验证用 `od -c` 或 Python `for ch in line: print(repr(ch))` 看实际字节。

转换覆盖 5 个标点:`,` → `,` / `.` → `。` / `:` → `:` / `;` → `;` / `?` → `?` / `!` → `!`。注意跳过 fenced code block / inline code / URL / 文件路径(用 `re.split(r"(\`[^\`\n]+\`)")` + `re.split(r"(\`\`\`[\s\S]*?\`\`\`)")` 双切分)。**冒号要尤其小心** —— `http://`、`~/.life_lens/`、`qwen3-vl:8b-instruct` 都用 ASCII `:`,只对"CJK + : + 空格/markdown 元素"的模式转。

### 隐私边界(2026-05 放宽)

原硬约束 "完全本地,零外发" 太严,实操不行:reverse geocoding / claude -p 都需要发部分数据。新边界:

| 数据 | 能否外发 | 谁接 |
|---|---|---|
| **原图字节**(JPEG/HEIC/缩略图) | ❌ 绝对不行 | 永远本地 |
| **照片描述/识别结果**(vision JSON) | ❌ 默认不行 | 永远本地 |
| **GPS 数字**(单独的 lat/lng) | ✅ 可以 | 高德 reverse geocoding |
| **用户问题文本** | ✅ 可以 | claude -p(用户自己同意的) |
| **查询返回的精简结果**(给 LLM 回答用) | ✅ 可以 | claude -p Round2 prompt(只发描述文本 + 缩略图 photo_id,不发图本身) |

判断标准:**外发数据能否让接收方还原出"这是哪张照片"**?GPS 单独发不行,但 GPS + 时间 + 缩略图组合就行 → 所以缩略图字节不能外发。

## CLAUDE.md 更新时机(给未来 Claude 看)

把 CLAUDE.md 当 **working memory** 而不是文档归档。**在下列时机立刻更新,不要拖到 commit 前**:

1. **Phase 完成时** — 更新"当前状态"小节
2. **新陷阱/坑被识别时** — 加入"踩过的坑"小节,**第一时间记录**(否则细节会忘)
3. **架构铁律或硬约束变化时** — 立刻写进规则区
4. **核心字段/API 改名时** — 全局替换,**不要漏**(过去版本里有过 caption/description 字段名变化没改干净的 bug)
5. **性能/实测数据有意外发现时** — 写进对照表
6. **设计选择从 A 改成 B 时** — 记录为什么,防止后续 Claude 又改回 A

反例(不该写):
- 临时调试日志
- 一次性的实验过程
- "今天发现 LLM 输出有点奇怪" 这种没结论的观察

## 架构总览

```
life_lens/
├── sources/                # PhotoSource ABC
│   ├── base.py
│   ├── filesystem.py       # ✅ os.walk + 9 种图片扩展名
│   └── photos_library.py   # ✅ osxphotos(mac-only,<0.65 pin)+ Apple face_info 解析 + fallback InsightFace
├── preprocess/             # 送视觉模型前必经
│   ├── decode.py           # ✅ pillow-heif + EXIF orientation
│   ├── resize.py           # ✅ 长边 ≤1024 Lanczos
│   └── cache.py            # ✅ .cache/preprocessed/{id}.jpg 复用
├── vision/                 # ✅ VisionModel ABC + Ollama qwen3-vl
│   ├── base.py             #    + prompt 模板 v9.2(位置 hint + age/gender 弱辅助)
│   ├── ollama.py
│   ├── ollama_probe.py     # ✅ Phase 5:轻量探活(HTTP /api/tags),给 Web 配置卡用,不引主 vision 依赖
│   ├── prompts.py          # ✅ _bbox_position(9 宫格)+ _desc/_struct_set_of_mark + 末尾强约束
│   ├── annotate.py         # ✅ set-of-mark 红框 + [N] 编号
│   └── role_check.py       # ✅ description vs persons.actions 自检(bigram+char hybrid)
├── exif/extract.py         # ✅ extract + peek_captured_at(轻量 DateTime,扫描入队用)
├── geocode/amap.py         # ✅ WGS-84→GCJ-02 + 50m 网格 + 配额追踪 + POI 距离最近
├── faces/                  # ✅ InsightFace ONNX + max-pooling 聚类 + 种子 + age/gender
│   ├── detector.py         # ✅ buffalo_l detect + age + gender 提取
│   ├── seeds.py            # ✅ add_seeds + _refresh_cluster_demographics 平均 age/gender
│   └── cluster.py          # ✅ max-pooling 跨年龄
├── store/
│   ├── schema.sql          # ✅ photos / jobs / scan_runs / sources / faces / persons / amap_quota / photo_embeddings / FTS5
│   ├── db.py               # ✅ Phase 5:SCHEMA_VERSION + PRAGMA user_version 追踪 + 升级自动备份 + WAL + 幂等迁移
│   ├── repo.py             # ✅ photo CRUD + run CRUD + jobs 状态机 + 配额 + demographics
│   └── config.py           # ✅ Phase 5:~/.life_lens/config.json 原子写 + chmod 600 + 单字段 update(保留 _comment 等)
├── embed/                  # ✅ Phase 4:语义向量(bge-small-zh-v1.5 via fastembed)
│   ├── embedder.py         #    懒加载 singleton(模型 ~3s init,常驻 ~200MB)
│   └── source_text.py      #    description+scene+tags+objects+mood+actions 拼接 + sha1 hash 增量
├── scanner/
│   ├── identity.py         # ✅ content_hash = size+mtime+头尾 64KB SHA1
│   ├── pipeline.py         # ✅ exif → preprocess → face → vision(注入位置 hint) → people → self-check → derived
│   ├── derived.py          # ✅ time_bucket / location_bucket(amap) / photo_type / is_keeper
│   ├── reprocess.py        # ✅ rematch/reprocess_faces + reprocess_vision_for + reprocess_derived + select_photos
│   └── runner.py           # ✅ 两阶段 db 驱动 + graceful stop + 拍照时间正序 + amap quota 监控
├── schema/photo_record.py  # ✅ new_record() + stamp_group_version()
├── query/                  # ✅ search.py(hybrid: FTS + sem + RRF + persons_mode AND/OR)+ aggregate.py(places_visited 也支持 AND)+ semantic.py(内存 brute-force 索引 + rrf_merge_multi)
├── web/
│   ├── server.py           # ✅ 启动跑 init_schema + reset_stuck_jobs + mark running runs as stopped
│   ├── api.py              # ✅ /api/{sources,scan,...,setup/status,ollama/ping,config/amap-key{,/validate},config/llm-provider,config/llm-default,sources/pick-folder}
│   ├── chat.py             # ✅ /api/chat SSE 两轮 LLM tool loop + jsonl 日志(~/.life_lens/chat_log/YYYY-MM-DD.jsonl)
│   ├── llm.py              # ✅ Phase 5:**只支持 RESTful API**(openai-compat),claude-p 已删;启动忽略旧 claude-p providers + WARN
│   └── static/             # ✅ vanilla HTML/JS:5-tab(配置/面孔/扫描/浏览/问相册)+ 配置卡片 + 顶部 banner + 文件夹选择器 + localStorage tab 记忆 + chat trace + photo_id 前缀补全
├── mcp/                    # ⏳ 推迟到 Python ≥ 3.10
├── cli/main.py             # ✅ lens serve/scan/status/init/backup/reprocess + signal handler + ASCII 进度条
└── __main__.py             # ✅ python -m life_lens 入口

scripts/
├── build_embeddings.py     # ✅ Phase 4:回填 photo_embeddings(text_hash 增量,~87 张/s)
├── backup_to_icloud.py     # ✅ Phase 3:WAL-safe sqlite3.backup + rsync 增量同步到 iCloud
├── make_cities_map.py      # ✅ Phase 2 wrap-up:db 查 city 聚合 + 高德 tile + GCJ-02 转换 → standalone HTML 地图
└── publish.sh              # ✅ Phase 5:私有 → 公开镜像 rsync(12 exclude)+ 隐私 grep + pytest + 提示人工 push

../tests/                   # A 层(管道,CI 友好,无 LLM 依赖) + B 层(AI 质量,私有保留)
├── conftest.py             # ✅ Phase 5:fixtures(empty_db / populated_db)
├── test_db_migration.py    # ✅ Phase 5:SCHEMA_VERSION 升级 + 幂等 + 老 db 模拟 v0→v1
├── test_query.py           # ✅ Phase 5:search / aggregate / persons OR-AND / time fixup
├── test_api_smoke.py       # ✅ Phase 5:FastAPI TestClient endpoint contract
├── test_no_private_data.py # ✅ Phase 5:grep 真名 + 用户主目录硬编码 + 公司名(隐私兜底)
└── eval/                   # B 层(私有,publish.sh rsync 排除)
    ├── ground_truth.yaml          # 11 张人工 vision 标注(私有跟踪,不进公开)
    ├── ground_truth.example.yaml  # ✅ Phase 5:公开版占位结构模板
    └── run_eval.py                # `python tests/eval/run_eval.py [--case IMG_xxx]`

../LICENSE                  # ✅ Phase 5:MIT
../README.md                # ✅ Phase 5 重写(141 行,完整安装 + 配置 + 第一次跑通 + 高级)
../CHANGELOG.md             # ✅ Phase 5:0.4.0 起步模板
../config.example.json      # ✅ Phase 5:3 个 LLM provider 示例(claude-p / DeepSeek / OpenAI)
```

**依赖方向严格单向**:
- 查询:`web (api / chat) → query → store/repo` + `geocode` 模块独立
- 写入:`web / cli → scanner → {sources, preprocess, vision, faces, exif, geocode, store}`
- 所有 adapter 只依赖 `schema` 和 `store`

## JSON Schema v1(6 group 顶层稳定)

```jsonc
{
  "schema_version": "0.1",

  "identity": {
    "photo_id":          "...",  // fs 用 content_hash[:16],apple 用 uuid
    "source":            "filesystem|photos_library",
    "source_ref":        "...",  // apple uuid 或绝对路径
    "original_path":     "...",
    "content_hash":      "...",
    "file_size_bytes":   1234567,
    "original_format":   "heic|jpeg|png|...",
    "sidecar_path":      null,
    "preprocessed_path": ".cache/preprocessed/{id}.jpg"
  },

  "exif": {
    "captured_at_local": "2024-08-15T19:23:01",
    "captured_at_utc":   "2024-08-15T11:23:01Z",
    "tz_offset_minutes": 480,
    "gps": { "lat": 31.2304, "lng": 121.4737 }   // 无 GPS 则 null
  },

  "vision": {                  // 全部 LLM 产出(v9 起两次调用合并),中文为主
    "description":  "80-200 字连贯中文叙事,场景+主体+动作+氛围;含人物直接用真名",
    "media_type":   "photo|screenshot|other",
    "subject":      "single|portrait|group|landscape|object|food|pet|mixed|null",
    "scene":        "外滩夜景",
    "objects":      ["天际线", "栏杆"],
    "tags":         ["夕阳", "都市", "傍晚"],
    "ocr_text":     "外滩 The Bund",
    "mood":         "宁静"
  },

  "people": {                   // 人物结构化事实(action 来自 LLM,name 实时 resolve)
    "persons": [
      { "cluster_id": "seed_xxx", "name": "张三", "action": "举相机自拍" },
      { "cluster_id": "c_xxx",    "name": null,   "action": "戴眼镜微笑" }
    ],
    "names":                ["张三", "李四"],   // 派生:已命名去重列表,SQL 查询方便
    "face_count":           4,
    "source_apple_persons": []
  },

  "derived": {                  // 全部 Python 规则,改起来便宜
    "time_bucket":     { "year": 2024, "month": "2024-08", "season": "summer",
                         "time_of_day": "evening", "day_of_week": "thursday",
                         "is_weekend": false, "iso_week": "2024-W33" },
    "location_bucket": { "country": "China", "city": "Shanghai",
                         "place_name": "The Bund",
                         "is_home": false, "is_travel": true },
    "photo_type":      "travel|family|selfie|food|pet|event|daily|screenshot|other",
    "is_keeper":       true
  },

  "meta": {
    "processed_at":   "2026-05-13T20:14:33Z",
    "group_versions": { "exif": "v1", "vision": "...", "people": "...", "derived": "rules-v1" },
    "source_signals": { "albums": [], "keywords": [], "favorite": false, "hidden": false, "place_apple": null },
    "errors":         []
  }
}
```

### 架构铁律:LLM 产物 vs 派生产物 分层

| group | 谁产 | 改起来 | 改的时候做什么 |
|---|---|---|---|
| `vision` | LLM(5-15s/张) | **贵** — 改 prompt 要全库重跑,几十万张 = 几天 | 谨慎,先 100 张样本调稳再大批跑 |
| `derived` | Python 规则 | **便宜** — 几分钟跑完几十万张 | 随便改,`reprocess --group derived` 就行 |

**铁律**:任何新分类 / 枚举 / 桶化字段一律放 `derived/`。**不要让 LLM 多产字段**。LLM 只产原始素材(description / scene / objects / tags / ocr_text / mood + actions[](后端组装到 people.persons[]))。

### `derived.photo_type` 规则(改了重跑 derived,几分钟)

```
media_type=screenshot → photo_type=screenshot
media_type=other      → photo_type=other
media_type=photo:
  subject=food / tags 含美食       → food
  subject=pet                       → pet
  tags 含 婚礼/演唱会/会议/聚会     → event
  subject=single                    → selfie
  subject in (landscape, object)    → travel
  其他                              → daily
```

family 类待人脸识别上线再补:`face_count ≥ 2 且含已命名亲人`。

## Vision Prompt v9.2(拆 description + struct 两次调用 + 位置 hint)

**为什么拆**:8B 模型单次长 prompt 注意力分散 — 实测把 caption 字段约束 + JSON schema + 6 铁律塞一起,LLM 会忽略"用真名"约束。拆开后每个 prompt 300-500 字,LLM 能专注一个任务。

**v9.2 增量(修人物错位)**:prompt 给每个 [N] 编号注入**位置 hint(从 face bbox 算 9 宫格)**+ age/gender 弱辅助。位置 hint 100% 准,LLM 可以用它确认红框 [N] 对应哪个真人,大幅降低错位概率。评测集 baseline 8/11 → v9.2 11/11 PASS。

**Call 1 — DESCRIPTION_PROMPT**(`v9.2-desc-2026-05-position-hint`):
- 喂带 set-of-mark 标注的图(每张脸画红框 + `[N]` 编号)
- prompt 注入 `[1]=张三(位置:下中;参考估计:约 11 岁、男(可能不准,以你看到的为准))`
- 末尾强约束:**先逐一对照红框真实位置和给的标签,确认 [N]→人映射,再开始写**
- 输出 `{ "description": "..." }` 一段 80-200 字叙事

**Call 2 — STRUCT_PROMPT**(`v9.2-struct-2026-05-position-hint`):
- 同样喂带标注图,prompt 也给位置 hint
- 输出 `{ media_type, subject, scene, objects, tags, ocr_text, mood, actions{} }`
- **`actions` 是 dict 不是 array**:`{"1": "蹲坐", "2": "举手机自拍"}`,key 锁死红框编号(v9.1 起,防 LLM 按"从左到右"自然顺序输出导致错位)
- Python 后端按 dict key 排序取 → 组装成 `people.persons[]`

**v9.2 之后还可能错位的兜底**:`vision/role_check.py` self-check 用 struct.actions(精确)验证 description 邻域是否一致,不一致写 `meta.errors[vision_role_mismatch]`,Browse 详情显示"已核对"按钮让用户人肉拍板。

**关键 Ollama 参数**:
- `num_ctx=16384` — Ollama 默认 2048 会静默截断,图片 ~800 tokens + prompt ~500-1000 tokens 经常超
- `format=json` — 强制 JSON 输出
- `temperature=0.2` — 略带随机但稳定
- 视觉调用并发上限 = 1(Ollama 单实例)

### Description 标杆

**好**:
> 黄昏时分的外滩沿江步道,张三举着相机自拍,李四靠在栏杆看江景。远处浦东天际线被晚霞染成橘红色,东方明珠塔顶亮灯。江风中行人穿着短袖,光线明亮。

**坏**:
- "这张照片展示了..." — 套话
- "似乎/可能/像是..." — 推测词
- "**场景**: 外滩\n**时间**: 黄昏..." — markdown
- "左侧男子戴帽" — 已识别还用方位代号

### 改名一致性

`people.persons[].cluster_id` 锚定,查询时通过 `persons` 表 join 拿最新 name(改名不重跑 vision)。`vision.description` 是 LLM 死字符串,改名/补种子后**会残留旧名,接受这个不一致**(改名低频,可通过 `reprocess_vision_for` 按需刷新)。结构化查询走 `people.persons[]`,description 是辅助阅读 + 全文检索。

## LLM 访问层

**当前**:Web Chat + REST(MCP 待 Python ≥ 3.10)。所有接口共享 `query/` 查询层。

### Web Chat — 主消费入口(`web/chat.py`)

`POST /api/chat { "question": "...", "provider_id"?: "..." }` → SSE 流。后端跑两轮 LLM 调用(走 `web/llm.py` 抽象,backend 可切):

1. **Round 1**(`ROUND1_SYSTEM`):用户问题 → JSON `{action, args, rationale}`。action 选自 `search_photos / places_visited / counts_by_year` 三个工具
2. **Python dispatch**:`_dispatch(conn, action, args)` 调 `query/search.py` 或 `query/aggregate.py`,**LLM 永远不写 SQL**
3. **Round 2**(`ROUND2_SYSTEM`):喂查询结果 → 流式中文回答 + `[photo:xxx]` 标记。前端正则抠 ID 渲染缩略图

SSE 事件名:`phase / planned / result / chunk / done / error`。前端 markdown 简渲染 + 头部 provider 下拉。

### LLM provider 抽象(`web/llm.py`)

| kind | 用途 | 配置字段 |
|---|---|---|
| `claude-p` | `claude -p` 子进程,Max plan,免 API key | `model`(默认 `claude-opus-4-7`) |
| `openai-compat` | HTTP /v1/chat/completions 任意兼容服务(DeepSeek/OpenAI/Together/Ollama) | `model` / `api_key` / `base_url` / `timeout?` |

**配置文件** `~/.life_lens/config.json`(`chmod 600`,**不进 git**):

```jsonc
{
  "amap_key": "...",
  "llm": {
    "default": "deepseek",
    "providers": {
      "claude-opus": { "kind": "claude-p", "model": "claude-opus-4-7", "label": "Claude Opus 4.7" },
      "deepseek":    { "kind": "openai-compat", "model": "deepseek-v4-flash",
                       "api_key": "sk-...", "base_url": "https://api.deepseek.com/v1",
                       "label": "DeepSeek Chat (快+便宜)" }
    }
  }
}
```

热读取(`_load_config()` 每次调用都读 JSON 文件),改 config 不需要重启 server。Env 变量 `LIFE_LENS_LLM_PROVIDER/MODEL/API_KEY/BASE_URL` 可临时覆盖当前选中的 provider。

旧的单 provider 格式 `{provider, model, ...}` 自动包成单 provider id="default",**向后兼容**。

新 API:
- `GET /api/llm-providers` — 全部 provider 列表(无 api_key)+ default id
- `GET /api/llm-info?provider_id=xxx` — 单个详情

### query/ 共享层(三轨共用)

| 函数 | 入参 | 用途 |
|---|---|---|
| `search.search_photos` | query, time_from, time_to, persons[], location, limit | FTS5 + persons join + time COALESCE fallback + location LIKE |
| `aggregate.places_visited` | year? | derived.location_bucket 聚合 |
| `aggregate.counts_by_year` | — | 按年照片数 |

### MCP server — 推迟

原计划 `claude mcp add life_lens python -m life_lens.mcp`。**MCP SDK 要求 Python ≥ 3.10,本机 3.9.6 装不了**(见"踩过的坑")。等 Python 升级再补。

### REST API

```
GET  /api/sources       POST /api/sources   DELETE /api/sources/{id}
POST /api/scan          GET  /api/status
GET  /api/photos        GET  /api/photo/{id}
GET  /api/thumb/{id}    GET  /api/original/{id}
GET  /api/persons       POST /api/seed-persons   DELETE /api/seed-persons/{cluster_id}
POST /api/persons/{cluster_id}/name
POST /api/photo/{id}/mismatches/acknowledge   # self-check 误报 → 用户人肉核对,标记忽略

# Phase 2 新增:Run 管理
POST /api/scan          # 启动/续传新 scan(body: { source_ids?: [...] })
POST /api/scan/resume   # 恢复最近一个 stopped/failed 的 run(quota 耗尽时 409)
POST /api/scan/stop     # graceful stop 当前 running run
GET  /api/status        # current_run + resumable_runs + amap_quota + global counts
GET  /api/runs          # 列最近 50 个 run
GET  /api/runs/{run_id}             # 详情(含 progress + time_range + scanned_up_to)
GET  /api/runs/{run_id}/failures    # 失败照片列表
POST /api/runs/{run_id}/retry       # 重置该 run 的 failed → pending,继续跑
POST /api/runs/{run_id}/resume      # 显式恢复特定 run

# Reprocess 选择器
POST /api/reprocess/preview         # 预览匹配数 + 抽样(selector: source/time/missing_field/person_count/person_ids/fts_query)
POST /api/reprocess                 # 创建 reprocess run + 启动(stage: vision|derived|faces)

POST /api/chat          # SSE,见上
```

### CLI(脚本/cron)

```
lens                                   # 默认 = 启动 web + 自动开浏览器
lens scan <path>                       # 默认续传,db 驱动断点恢复
   --retry-failed                      # 启动前把 failed 重置 pending 再跑
   --enqueue-only                      # 只 Phase A 入队不处理
   --no-vision                         # 跳过 vision(只 exif+face+derived)
lens status [--jobs]                   # --jobs 列最近 5 run + 失败原因
lens init                              # 初始化 lens.db
lens backup [--out PATH]               # WAL-safe sqlite3 .backup
lens reprocess --group faces           # CLI 目前只接 faces,Web 走 /api/reprocess 支持 faces/vision/derived
```

argparse,**不要换 click/typer**。`lens query "<text>"` 还没做(Phase 3 候选)。**主操作入口是 Web 端**,CLI 只是兜底。

## 数据布局

- `~/.life_lens/lens.db` — SQLite WAL 主库
- `~/.life_lens/.cache/preprocessed/{photo_id}.jpg` — 1024px JPEG 缓存,**永远复用**(重扫不重转、换 prompt 不重转、换模型不重转)
- 原图保留在用户自己目录,db 只存绝对路径
- `sample/` 是用户私人测试照片(49 HEIC + 3 JPG + 3 MOV,已 gitignore)。**任何处理都用 `sample/` 跑烟测,不要写新测试数据进去**。

## 技术栈硬约束

- **Python 3.9 兼容**(用户机器只有系统 `/usr/bin/python3 = 3.9.6`)。所有新文件必须 `from __future__ import annotations`,不要在运行时表达式里写 `X | Y` 联合类型(类型注解可以,因为 future annotations 延迟求值)。
- **venv 叫 `.venv_lens`**(不是 `.venv`),避免和兄弟项目冲突。
- **不要装新框架**。前端原生 HTML + vanilla JS,**不要引 React / Vue / Tailwind**。后端 FastAPI + Jinja2 已经够。
- **LLM 文本调用**:用户只有 Claude Max plan,**没有 anthropic API key**。所有文本 LLM 调用走 `claude -p` 子进程(参考 `ai_invest/tools/ask_claude.py`),**绝不 `import anthropic`**。
- **LLM 视觉调用**:走本地 Ollama HTTP(`http://localhost:11434/api/generate`),目标模型 `qwen3-vl:8b-instruct`。**入参必须是预处理后的 1024px JPEG**,原 HEIC / 大图直接喂模型会失败。
- **中文文档不加空格**:中文 markdown 中文紧挨英文/数字,不加盘古之白空格(全局规则)。

## 不要做的事

- **不要自动 commit**(全局 `~/.claude/CLAUDE.md` 已规定)。用户说"备份" / "提交" / "commit" 再做。
- **不要为不存在的需求加抽象**。比如 vision adapter 已有 ABC,要加 MLX-VLM 时再写实现,不要预先 stub。
- **不要把视频纳入主流程**。`sources/filesystem.py` 扩展名白名单只含图片。视频(MOV / MP4)是 Phase 4+ 的事。
- **不要把 description 当严格事实源**。改名后 description 残留旧名 — 设计意图,结构化查询走 `people.persons[]`(查询时 join `persons` 表拿最新名)。
- **不要让 LLM 多产字段**。新分类一律进 `derived/` 用 Python 规则算。
- **不要绕过 `query/` 直接写 SQL**。否则 Web Chat / REST / CLI 三个入口行为会漂移。
- **不要让 LLM 写 SQL**。`web/chat.py` Round 1 只让 LLM 填白名单 args(`query/time_from/time_to/persons/location`),Python 拼 SQL。任何"让 LLM 直接出 SQL"或"工具开放任意字段"的诱惑都先拒掉(安全 + 可控)。
- **不要发原图字节到外部**。GPS 数字、问题文本、查询结果**可以**发(claude -p / 高德);**原图 bytes 永远本地**(见"隐私边界")。
- **不要在 ai_invest 风格上加 mcp 依赖**。MCP SDK 要 Python ≥ 3.10,本机 3.9。任何"用 MCP 才能做"的需求先想清楚有没有 web/chat.py 风格的替代。
- **不要在代码 / prompt 例子 / 文档教训里用真名 / 真实小景点 / 真实生活轨迹**。所有 person/place 例子用占位符(张三 / 小明 / 王女士 / 李四 / 赵阿姨 / 小华 / 李丽;某园区 / 某商场 / 某小区)。公共景区(阿那亚 / 敦煌 / 雅丹 / 青海)和知名地标(外滩 / 长城)可保留。**理由:本项目要定期 sync 到公开 GitHub 仓库 (uxtracer/life-lens),真名是定时炸弹,源头治理比 grep 扫尾靠谱**。新发现的 case 教训写"踩过的坑"时,如果原始素材是真名,先在脑子里替换成占位符再下笔。`scripts/publish.sh` 有 grep 兜底,但**那只是 last line of defense,不应该靠它救命**。

## 增量扫描(Phase 2 重做 — db 驱动两阶段)

- Photo identity = `sha1(size || mtime_ns || first_64KB || last_64KB)` — 避免整文件 SHA1
- Apple Photos source 直接用 `PhotoInfo.uuid`
- **Phase A**(入队):流式遍历 `source.iter_photos()`,`identity.content_hash` 判重(已 done 同 hash 跳过),`exif.peek_captured_at` 拿拍照时间 → `enqueue_job(run_id, captured_at_local)`
- **Phase B**(处理):**单线程**主循环(vision/face 已有 lock,workers>1 增益 ~5%,换 graceful 控制 + db 一致性更划算)→ `get_pending ORDER BY captured_at_local ASC` 拉 batch → `pipeline.process_one` → 状态机推进
- jobs 表状态机:`pending → processing → done | failed`,失败 retry_count<3 重置 pending,>=3 永久 failed(Ollama 内部 3 次重试,总共最多 9 次网络重试)
- rescan = source ↔ db diff:Phase A 同一文件 content_hash 相同 + 已 done → skip;源里消失的暂不处理(Phase 3+)
- **断点续传**:server 启动 `reset_stuck_jobs`(processing → pending)+ `mark_running_as_stopped`(scan_runs.status='running' 但进程没了的标 stopped)
- **scan_runs.done/failed 增量**:`inc_run_done` / `inc_run_failed` 在每张完成时 +1,resume 时不重置历史 done
- **scan_runs 时间快照**:`set_run_time_range`(enqueue 后一次性冻结)+ `bump_run_scanned_up_to`(每张完成 max 推进),避免后续 reprocess 偷 jobs 导致历史 run 时间游标丢失
- **graceful stop**:`progress.stop_flag.set()` → 当前张跑完后退出,run 标 stopped。Web 端 `/api/scan/stop`,CLI 用 SIGINT
- **高德 quota 自动暂停**:每张完成后 `amap_geo.is_quota_exhausted(conn)` → 是则 set stop_flag,note 写"高德今日配额耗尽,次日 UTC+8 0:00 重置"

## 人脸 / 种子人物机制

- **检测 + embedding**:InsightFace buffalo_l(ONNX, 512 维, L2 归一化)
- **聚类算法**:**max-pooling**(`faces/cluster.py`)— 新脸和 cluster 内**每个 face embedding** 算余弦取 max,而非和 centroid 平均比。原因:同一 person 跨年龄/光照,平均会拉偏;max 保留每张种子作为独立判据,**自动 cover 跨年龄**。阈值默认 0.5。
- **种子人物**:用户从 GUI 上传(`/api/seed-persons`),系统写 `photos.source='seed'` + faces 表 + persons 命名。**种子图不入主库**(所有 list/browse 接口过滤 `source != 'seed'`)。
- **改名透明**:`people.persons[].cluster_id` 锚定,查询时 join `persons` 表实时 resolve 真名,改名不重跑 vision。
- **description 死字符串**:`vision.description` 文本是 LLM 写时的字符串,改名/加种子后**不会自动更新**。需要手动触发 `reprocess --group vision --photo-ids ...`(GUI 上是"刷新描述"按钮)。

### 两种重跑

| 命令 | 干什么 | 耗时(10000 张) |
|---|---|---|
| `rematch_faces`(quick) | 不跑 detect,用已有 embedding 重新算 max-pooling,把主库脸吸到已命名 anchor。**只动未命名 face**,已命名 cluster 是 ground truth 不动 | 秒级到几十秒 |
| `reprocess_faces`(full) | 重跑 InsightFace detect + assign(改 face 模型/预处理时用) | 几小时 |
| `reprocess_vision_for(photo_ids)` | 对指定 photo 重跑 vision(两次调用),prompt 注入当前最新人名 → description 用真名 + actions 重写 | 每张 ~40s |

## Phase 路线图

- **Phase 0** ✅:骨架 + SQLite + 预处理缓存 + EXIF + GUI 三页(无视觉)
- **Phase 1** ✅:Ollama vision adapter(qwen3-vl 8B) + InsightFace + 种子人物 + FTS5 + Browse + 高德 reverse geocoding + LLM provider 抽象 + Web Chat
- **Phase 2** ✅:**生产化**
  - 大批量扫描机制(jobs db 驱动 + Run 历史 + graceful stop + 拍照时间正序)
  - 高德全面修(WGS-84→GCJ-02 + 50m 网格 + 配额追踪 + POI 距离最近 + formatted 自拼)
  - vision prompt v9.2(位置 hint + age/gender 弱辅助 + struct actions dict)
  - self-check 后置校验 + acknowledged 机制
  - 评测集 11 张人工 ground truth + 回归脚本
  - FTS5 trigram 中文支持 + LIKE 短词 fallback
  - Web UI 完整化(Scan/Runs/Reprocess 三页,Reprocess 多维筛选)
- **Phase 3** 🟢:Apple Photos source 接线 ✅ + iCloud 备份脚本 ✅ + cities 地图 ✅;**未做**:sidecar JSON 导出、MCP server(等 Py ≥ 3.10)、视频(MOV/MP4)、Apple 漏脸批量补
- **Phase 4** 🟢:语义向量检索(bge-small-zh + fastembed) ✅ + hybrid search + RRF ✅ + LLM query expansion ✅ + chat jsonl 日志 ✅;**未做**:`lens query` CLI、jieba 分词支持单字搜索、chat 评测集

## 测试节奏

**两层**:A 层(管道,改任何代码必跑,无 LLM)/ B 层(AI 质量,改 prompt 或 vision 模型必跑,需 Ollama)。

```bash
source .venv_lens/bin/activate

# 0. A 层 pytest(改任何代码必跑;~3 秒,无 LLM 依赖)
pytest tests/ -v
# 检查项:db migration / query (FTS+persons) / API contract / 隐私 grep
# 跟 publish.sh 跑的是同一套(B 阶段)

# 1. B 层评测集回归(改 prompt / vision 模型必跑) — 11 张人工 ground truth
python tests/eval/run_eval.py                  # 跑全部
python tests/eval/run_eval.py --case IMG_0685  # 跑某张
# 目标:11/11 PASS;baseline v9.1 是 8/11,v9.2 是 11/11

# 2. 烟测:本地一个小样本目录跑一遍(几十张就够)
lens scan ~/Pictures/test_album
lens status --jobs        # 详细模式:看最近 5 run + 失败原因

# 看库
sqlite3 ~/.life_lens/lens.db "SELECT photo_id, json_extract(exif, '$.captured_at_local') FROM photos LIMIT 5"

# GUI
lens                              # 自动开浏览器到 http://127.0.0.1:7878
```

**Phase 1 接入 Ollama 后的验证**:`ollama pull qwen3-vl:8b-instruct && ollama serve`,然后 `lens scan sample/`,Browse 页点开能看到 description + objects + people.persons[],FTS5 关键词搜索"猫"命中含"cat/猫" description 的照片。

**压测目标**:单张端到端 < 20s(M 系列 Mac)。

## 关键路径速查

```
life_lens/sources/           数据源 adapter
life_lens/preprocess/cache.py 缓存键: photo_id;改了这个所有缓存失效
life_lens/exif/extract.py    只 4 个字段,EXIF 别加机型/参数
life_lens/scanner/derived.py 派生规则,改了 reprocess --group derived
life_lens/store/schema.sql   改 schema 要 bump db.py::SCHEMA_VERSION,init_schema 启动时自动备份+迁移(老 db 走 _migrate_columns 的 ALTER ADD COLUMN 路径,新表走 CREATE IF NOT EXISTS)
life_lens/web/static/        vanilla HTML/JS,改前端别引框架
life_lens/cli/main.py        argparse,不要换 click/typer
```
