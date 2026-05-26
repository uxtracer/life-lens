# life_lens 数据查询指南(给 LLM agent / 第三方应用看)

life_lens 把整本相册扫成结构化 SQLite 数据。任何带 sqlite3 库的进程(Python / Node / Go / Cursor / Claude Code / ...)拿到 `lens.db` 文件就能查,不需要跑 server。

本文档是写给"用户把 lens.db 丢给你,让你帮忙分析相册"场景的 — 描述表结构、JSON 字段语义、常见查询、容易踩的坑。

---

## 你拿到了什么

用户会给你 **三样东西**(或选其一):

1. **`lens.db`** — SQLite 文件,核心数据。约几百 MB(几十万张相册)
2. **`.cache/preprocessed/`**(可选)— 每张照片的 1024px JPEG 缩略图,文件名 `{photo_id}.jpg`。如果要"看图"需要这个;只做文本分析不需要
3. **原图目录**(可选)— 用户的原始 HEIC/JPEG。`lens.db` 的 `original_path` 字段是这里的绝对路径,你拿到原图目录后才能解引用

最小化场景:**只给 lens.db 就能做大部分文本分析、统计、推荐**。

---

## 表结构速览

```sql
photos (
  photo_id        TEXT PK,         -- sha1(size||mtime||头尾64KB) 头 16 位
  source          TEXT,            -- 'filesystem' | 'photos_library' | 'seed'(种子图,通常要排除)
  original_path   TEXT,            -- 原图绝对路径
  identity        JSON,            -- 全副 identity 信息
  exif            JSON,            -- 时间 / GPS
  vision          JSON,            -- 视觉模型产出(描述/场景/标签/OCR/...)
  people          JSON,            -- 人物列表 + face_count
  derived         JSON,            -- 派生字段(时间桶 / 地点桶 / 类型 / is_keeper)
  meta            JSON,            -- group_versions / errors / 源信号
  captured_at_utc TEXT GEN,        -- 抽自 exif,可能空字符串(见 Pitfalls)
  media_type      TEXT GEN,        -- 抽自 vision.media_type
  is_keeper       INTEGER GEN
)

faces (
  face_id, photo_id (FK), cluster_id, embedding (BLOB 512维 float32), bbox JSON
)

persons (
  cluster_id PK, name,                    -- 已命名的人,name=NULL 即未命名
  age_estimate, gender_estimate           -- InsightFace 估计的种子人物平均年龄/性别(0=女,1=男)
                                          -- 对儿童不准,只是 vision prompt 弱辅助 hint,**不要拿来推断真实年龄性别**
)

photos_fts (FTS5 虚表;列 = photo_id, description, scene, tags, ocr_text, actions, objects)
  tokenize = 'trigram'                    -- 用 trigram 是为了索引中文(unicode61 不索引 CJK)
  ⚠️ trigram 限制:搜索词必须 ≥ 3 字符。2 字短语("长城"/"猫狗") MATCH 永远返 0 张
                    → 短词用 LIKE,不要用 FTS
  历史:列名 Phase 2 之前叫 'caption',现已改为 'description' 和 vision JSON 字段名一致

photo_embeddings (photo_id PK, model, dim, vec BLOB float32, text_hash, updated_at)
  -- Phase 4 加:语义向量(bge-small-zh-v1.5,512 维,~2KB/张)
  -- model='bge-small-zh-v1.5' 是当前活跃版本;text_hash 是 source_text(description+scene+tags+objects+mood+actions 拼接)的 sha1[:16]
  -- 自己跑语义检索:numpy 加载所有 vec 算余弦(15w 张 ~50ms/query),或 sqlite-vec / Qdrant 外挂
  -- 用户跑的 lens server hybrid 检索内部用这个 + FTS RRF 合并;你独立查 db 时:
  --   import numpy as np; rows = conn.execute("SELECT photo_id, vec FROM photo_embeddings")
  --   mat = np.stack([np.frombuffer(r['vec'], np.float32) for r in rows])  # (N, 512) L2 normalized
  --   q_vec = your_embed_func("海边的照片")  # 同模型 bge-small-zh-v1.5
  --   scores = mat @ q_vec  # 余弦
  --   top_ids = np.argsort(-scores)[:k]

geocode_cache (lat_grid, lng_grid, provider, result JSON, fetched_at)
  provider = 'amap-gcj02'(当前活跃,WGS-84→GCJ-02 转换后 50m 网格)
  旧 provider='amap' 和 'amap-50m' 是历史错误数据(未转坐标),保留但不被命中

scan_runs (run_id PK, kind, status, total, done, failed, started_at, finished_at,
           snapshot_captured_min, snapshot_captured_max, snapshot_scanned_up_to, note)
  -- 每次 scan / reprocess 一个 run。kind ∈ {scan, retry-failed, reprocess-vision/derived/faces}

amap_quota (date_local PK, count, last_at)
  -- 高德 reverse-geocoding 每日配额追踪(Asia/Shanghai 日期),免费 5000/天,上限设 4800

sources / jobs                -- 扫描调度,通常你不用看
  jobs 表带 run_id + captured_at_local(Phase 2 加的,db 驱动状态机用)
```

**铁律**:除非有特殊原因,查询永远加 `WHERE source != 'seed' AND vision IS NOT NULL`(排除种子图 + 扫到一半的 placeholder)。

---

## JSON 字段速查

### `vision`(LLM 看图产物,中文)

```jsonc
{
  "description": "黄昏外滩,张三举相机自拍,李四靠栏杆看江景...",   // 80-200 字中文叙事
  "media_type": "photo|screenshot|other",
  "subject":    "single|portrait|group|landscape|object|food|pet|mixed|null",
  "scene":      "外滩夜景",                                       // 自由文本
  "objects":    ["天际线", "栏杆", "人群"],                       // 3-8 个
  "tags":       ["夕阳", "都市", "夏末", "傍晚"],                 // 3-10 个,检索主力
  "ocr_text":   "外滩 The Bund",                                  // 图里可见文字
  "mood":       "宁静"
}
```

### `people`

```jsonc
{
  "persons": [
    { "cluster_id": "seed_xxx", "name": "张三", "action": "举相机自拍" },
    { "cluster_id": "c_xxx",    "name": null,   "action": "戴眼镜微笑" }
  ],
  "names":       ["张三", "李四"],   // 派生:已命名的去重列表(SQL 查询方便)
  "face_count":  4
}
```

⚠️ **改名透明**:`persons[].name` 是 vision 调用时的快照,可能过时。要拿最新名,JOIN `persons` 表:

```sql
SELECT p.photo_id, ps.name
FROM photos p
JOIN json_each(p.people, '$.persons') je
JOIN persons ps ON ps.cluster_id = json_extract(je.value, '$.cluster_id')
WHERE ps.name IS NOT NULL
```

### `derived`(Python 规则派生)

```jsonc
{
  "time_bucket": {
    "year": 2024, "month": "2024-08", "season": "summer",
    "time_of_day": "morning|noon|afternoon|evening|night|late_night",
    "day_of_week": "thursday", "is_weekend": false,
    "iso_week": "2024-W33"
  },
  "location_bucket": {                  // 高德 reverse geocoding 填的(WGS-84→GCJ-02 转换后查)
    "country":     "中国",
    "province":    "河北省",
    "city":        "承德市",
    "district":    "滦平县",
    "township":    "涝洼镇",
    "aoi_name":    "阿那亚金山岭",       // 景区/小区(大区域);⚠️ 经常 null —— 高德 AOI 多边形覆盖不全
    "poi_name":    "阿那亚山谷音乐厅",   // 距离最近的具体地标(POI),~24m 内
    "place_name":  "阿那亚山谷音乐厅",   // = poi_name or aoi_name(POI 优先,具体地标 > 大景区)
    "formatted_address":                 // ★ **推荐 LLM 用这个,包含全粒度**
      "河北省承德市滦平县涝洼镇 · 阿那亚金山岭 · 阿那亚山谷音乐厅"
  },
  "photo_type": "travel|family|selfie|food|pet|event|daily|screenshot|other",
  "is_keeper":  true                   // 大致等价于 media_type == 'photo'
}
```

### `exif`

```jsonc
{
  "captured_at_local": "2024-08-15T19:23:01",   // 始终有(只要 EXIF 没坏)
  "captured_at_utc":   "2024-08-15T11:23:01Z",  // 可能为空字符串!见 Pitfalls
  "tz_offset_minutes": 480,
  "gps": { "lat": 31.2304, "lng": 121.4737 }    // 无 GPS 则 null
}
```

---

## 常用查询(SQL 示例)

### 1. 列出所有去过的地点(按频次降序)

```sql
-- ★ 推荐用 formatted_address(完整粒度,aoi_name 经常 null,不要硬依赖)
SELECT
  json_extract(derived, '$.location_bucket.formatted_address') AS address,
  COUNT(*)                                                     AS cnt
FROM photos
WHERE source != 'seed' AND vision IS NOT NULL
  AND json_extract(derived, '$.location_bucket.formatted_address') IS NOT NULL
GROUP BY address
ORDER BY cnt DESC;
```

返回示例:
```
"河北省承德市滦平县涝洼镇 · 阿那亚金山岭 · 阿那亚山谷音乐厅"   28
"河北省承德市滦平县涝洼镇 · 阿那亚金山岭上院"                  20
"北京市密云区新城子镇 · 老道洞"                                 26
```

**地理聚合到"目的地"层级**:formatted_address 中间段(AOI/景区段)相同的可以归一类
(比如所有"阿那亚金山岭"前缀的可以归"阿那亚金山岭"目的地)。**用字符串 LIKE 或 SQL 拼接**,
比按 aoi_name GROUP BY 稳(因为 aoi_name 经常 null)。

### 2. 某个人出现的所有照片

```sql
SELECT p.photo_id,
       json_extract(p.vision, '$.description') AS description,
       json_extract(p.derived, '$.location_bucket.place_name') AS place,
       p.captured_at_utc
FROM photos p
JOIN faces f ON f.photo_id = p.photo_id
JOIN persons ps ON ps.cluster_id = f.cluster_id
WHERE ps.name = '张三' AND p.source != 'seed'
ORDER BY p.captured_at_utc DESC;
```

### 3. 关键词全文搜索(FTS5 trigram)

```sql
-- ≥ 3 字短语用 FTS(快,毫秒级,bm25 排序)
SELECT p.photo_id, json_extract(p.vision, '$.description')
FROM photos_fts fts
JOIN photos p ON p.photo_id = fts.photo_id
WHERE photos_fts MATCH '阿那亚 OR 金山岭'
  AND p.source != 'seed' AND p.vision IS NOT NULL
ORDER BY bm25(photos_fts) ASC
LIMIT 20;
```

```sql
-- ⚠️ < 3 字短语("猫"/"狗"/"长城")用 LIKE,FTS trigram 没法索引
SELECT photo_id, json_extract(vision, '$.description')
FROM photos
WHERE source != 'seed' AND vision IS NOT NULL
  AND (json_extract(vision, '$.description') LIKE '%长城%'
       OR json_extract(vision, '$.scene') LIKE '%长城%'
       OR json_extract(vision, '$.tags') LIKE '%长城%');
```

### 4. 语义向量检索(自然语言模糊查询)

`photo_embeddings` 里每张已扫照片有一条 bge-small-zh-v1.5 向量。Phase 6 起,主扫描 + reprocess vision 时 inline 写入,**新照片自动有 embedding**(以前要手动跑 `scripts/build_embeddings.py`)。

适合 "看上去像沙滩日落的照片" 这种**字面对不上但语义近**的查询。FTS 不会命中"连衣裙" 跟 "裙子" 的关系,语义会。

```python
import numpy as np, sqlite3
from fastembed import TextEmbedding

conn = sqlite3.connect('lens.db'); conn.row_factory = sqlite3.Row

# 加载全库向量(15w 张 ~300MB,brute-force 余弦在 30w 张内完全够)
rows = conn.execute(
    "SELECT photo_id, vec FROM photo_embeddings WHERE model='bge-small-zh-v1.5'"
).fetchall()
ids = [r['photo_id'] for r in rows]
mat = np.stack([np.frombuffer(r['vec'], np.float32) for r in rows])  # (N, 512) 已 L2 normalized

# 用同一个模型 embed query(必须同模型,不同 embedding space 不能比)
embedder = TextEmbedding(model_name='BAAI/bge-small-zh-v1.5')
q_vec = next(embedder.query_embed(['海边日落的合影'])); q_vec = q_vec / np.linalg.norm(q_vec)

scores = mat @ q_vec
top_k = np.argsort(-scores)[:20]
for i in top_k:
    print(ids[i], f"{scores[i]:.3f}")
```

**short-query vs long-doc 弱点**:bge 把 200 字 description 压成 512 维,短 query("裙子" 2 字)跟长 doc 余弦只 ~0.40,容易被稀释。两个对策:
1. **query expansion**:扩词成 3-5 个具体物品(`"裙子" → ["连衣裙","长裙","短裙"]`),分别 embed 取 max,再 RRF
2. **hybrid with FTS**:FTS 字面 `LIKE %连衣裙%` 走 sparse 路径精确命中,语义走 dense 路径覆盖近义。`life_lens/query/semantic.py::rrf_merge_multi` 是参考实现

### 5. 怎么选 FTS / 语义 / hybrid?

| 查询形态 | 用什么 | 例子 |
|---|---|---|
| 精确关键词、地名、人名(≥ 3 字) | FTS5 MATCH | "阿那亚金山岭" / "外滩夜景" |
| 短词 / 单字 | LIKE on description+scene+tags | "猫" / "长城" / "雪" |
| 描述性、模糊、近义 | 语义向量(bge) | "看上去像家庭聚餐" / "孩子在草地上玩" |
| 用户自然语言提问(生产) | hybrid(FTS + 语义 RRF) | "夏天我和家人在海边" |

**生产建议**:hybrid 永远是最稳的(dense 解语义,sparse 解字面),实现见 `life_lens/query/search.py::search_photos`。独立读 db 做一次性分析时,根据问题挑一种就够。

### 6. 某段时间在某城市的照片

```sql
SELECT photo_id, json_extract(derived, '$.location_bucket.place_name') AS place
FROM photos
WHERE source != 'seed' AND vision IS NOT NULL
  AND COALESCE(NULLIF(captured_at_utc, ''), json_extract(exif, '$.captured_at_local'))
        BETWEEN '2024-01-01' AND '2024-12-31'
  AND (json_extract(derived, '$.location_bucket.city') LIKE '%上海%'
       OR json_extract(derived, '$.location_bucket.province') LIKE '%上海%');
```

### 7. 一个人单独的自拍(无其他人脸)

```sql
SELECT photo_id, json_extract(vision, '$.description')
FROM photos
WHERE source != 'seed' AND vision IS NOT NULL
  AND json_extract(people, '$.face_count') = 1
  AND EXISTS (
    SELECT 1 FROM faces f
    JOIN persons ps ON ps.cluster_id = f.cluster_id
    WHERE f.photo_id = photos.photo_id AND ps.name = '张三'
  );
```

### 8. 按年统计

```sql
SELECT json_extract(derived, '$.time_bucket.year') AS year, COUNT(*)
FROM photos
WHERE source != 'seed' AND vision IS NOT NULL
GROUP BY year ORDER BY year;
```

---

## Pitfalls(踩过的坑,务必记住)

### A. `captured_at_utc` 经常是空字符串

iPhone 在 EXIF 里**经常没有 OffsetTimeOriginal tag**(只有 local 时间),所以生成列 `captured_at_utc` 会是 `''`(空字符串,**不是 NULL**)。

查时间窗永远写:

```sql
COALESCE(NULLIF(captured_at_utc, ''), json_extract(exif, '$.captured_at_local'))
```

直接 `WHERE captured_at_utc >= '2024-01-01'` 会把所有空 utc 的照片(可能几万张)漏掉。

### B. `vision.description` 含的人名是"快照",可能过时

description 是 LLM 写的死字符串,后续给 cluster 改名,description 不会自动更新。**结构化人物查询走 `faces JOIN persons`,不要从 description 文本里 grep 名字**。

### C. `seed` 照片要排除

`source = 'seed'` 的行是用户上传的种子图(用来训人脸识别 anchor),**不是真实相册的一部分**。除非你专门查种子,加 `WHERE source != 'seed'`。

### D. photos_fts 列名(Phase 2 起 caption → description)

Phase 2 起 FTS5 列名改成 `description`(和 vision JSON 字段一致)。如果你拿到的是早期 db 还看到 `caption` 列,内容也是 description(只是名字没改)。新 db 一律用 `description`:

```sql
SELECT * FROM photos_fts WHERE description MATCH '长城';  -- ✓ 新 schema
SELECT * FROM photos_fts WHERE caption MATCH '长城';      -- ✗ 老 schema(Phase 2 之前)
```

### E. JSON 字段可能整体为 NULL

扫描中的 placeholder 行 `vision IS NULL`,带上 `WHERE vision IS NOT NULL` 过滤掉。

### F. 行政边界跨省

景区/城市边界附近的照片,GPS 微小偏差会跨省/跨城。比如金山岭长城横跨北京密云 / 河北承德,同一次旅行的照片 `city` 字段会分散。**聚合"一次行程"时要按时间窗 + AOI/place_name 一起判断,不要只看 city**。

### G. 跨年龄人脸 max-pooling

`faces.cluster_id` 是用 max-pooling 算的(对每个 cluster 内每张 face 余弦取 max),所以同一个人小时候和长大后的脸可能 anchor 到同一个 cluster。但有时候也会落进两个不同 cluster — 看到一个人有多个 cluster_id 不奇怪,改名时把它们都指到同一个 name 即可。

### H. 缩略图位置

如果用户给了 `.cache/preprocessed/`,缩略图文件是 `{photo_id}.jpg`(用 `identity.photo_id` 拼)。多模态 LLM 拿这个 1024px JPEG 看图。原图路径在 `identity.original_path`(可能在用户机器上不可访问)。

### I. `aoi_name` 经常 null,聚合不要硬依赖

高德 AOI 是景区/小区多边形,**只对在多边形边界内部的点位返回 AOI**。同一景区不同位置的 GPS 可能落在 AOI 外,`aoi_name = null`,只剩 `poi_name`。

**用 `formatted_address` 字段更稳**(中间段是 AOI 段,即使 aoi_name 字段缺也通常在 formatted 里)。

### J. `place_name` 优先 POI(细粒度)

Phase 2 起 `place_name = poi_name or aoi_name`(POI 优先,具体地标 > 大景区)。同一景区不同位置的 place_name 可能不同(上院/泳池/音乐厅/停车场分别是 4 个 place)。聚合到"目的地"层级用 formatted_address 中间段。

### K. GPS 是 WGS-84,高德查询前转 GCJ-02

`exif.gps` 是 iPhone EXIF 原始 WGS-84(国际标准)。内部 `geocode/amap.py::wgs84_to_gcj02` 转换后查高德(中国大陆偏移 300-700m,不转会把偏远小景点定位到旁边)。**db 里存的 gps 是 WGS-84**(不变)。

### L. `vision_role_mismatch` self-check 警告(可能误报)

`meta.errors` 里 `group='vision_role_mismatch'` 是 self-check 算法检测的"description 邻域和 struct.actions 不一致"。**很多是误报**(LLM 用同义表达 — struct='举手机自拍' / description='面带笑容'),不是真错位。

- 带 `acknowledged: true` 的是用户人肉核对过忽略的
- 真要确认错位,看图 + 看 `vision.description` 上下文
- 不要把这个 group 当成"错位铁证"

---

## Python 最小示例

```python
import sqlite3, json
conn = sqlite3.connect('lens.db')
conn.row_factory = sqlite3.Row

# 找 2024 年在金山岭的所有照片
for r in conn.execute("""
  SELECT photo_id,
         json_extract(vision, '$.description') AS desc,
         json_extract(derived, '$.location_bucket.aoi_name') AS aoi
  FROM photos
  WHERE source != 'seed' AND vision IS NOT NULL
    AND json_extract(derived, '$.location_bucket.aoi_name') LIKE '%金山岭%'
    AND COALESCE(NULLIF(captured_at_utc, ''), json_extract(exif, '$.captured_at_local'))
          LIKE '2024-%'
"""):
    print(r['photo_id'], r['aoi'], '|', r['desc'][:60])
```

---

## REST API(如果用户跑着 lens server)

主要端点(`http://127.0.0.1:7878/api/`):

- `GET  /photos?page=0&page_size=120`
- `GET  /photo/{photo_id}` → 完整 PhotoRecord + `role_mismatches`(self-check 警告)
- `GET  /thumb/{photo_id}` → 1024px JPEG bytes
- `GET  /original/{photo_id}` → 原图 bytes
- `POST /chat` body `{question, provider_id?}` → SSE 流式自然语言问答
  - chat 内部走 `query/search.py` 和 `query/aggregate.py`,Round 2 给 LLM 的 location 字段统一是 `formatted_address`
- `GET  /runs` / `GET /runs/{id}` / `POST /runs/{id}/retry|resume` → 扫描 Run 历史管理(Phase 2)
- `POST /scan` / `POST /scan/resume` / `POST /scan/stop` → 启动 / 续传 / 暂停扫描
- `GET  /status` → current_run + resumable_runs + amap_quota
- `POST /reprocess/preview` / `POST /reprocess` → 选择器预览 + 批量重跑
- `POST /photo/{id}/mismatches/acknowledge` → 标记 self-check 警告已核对

如果你能让用户开 server,直接用 REST 比读 db 文件更安全(锁、并发都不操心)。

---

## 备份恢复

WAL 模式不能直接 cp 文件(可能拷到中间态)。用户应该用:

```bash
lens --root <root> backup        # 拍一个 WAL-safe 快照到 <root>/backups/lens-YYYYMMDD-HHMM.db
```

或者 SQL:

```sql
.backup '/path/to/snapshot.db'
-- 或
VACUUM INTO '/path/to/snapshot.db'
```

snapshot 是 self-contained 普通 SQLite 文件,拷哪都行。

---

## 版本

- 写于 schema v0.1 + **vision prompt v9.2** + derived rules-v2-geocode
- Phase 2(2026-05)主要变化:
  - photos_fts tokenizer 改 trigram(中文支持,但限 ≥ 3 字)
  - location_bucket.place_name 优先 POI(以前 AOI 优先)
  - **formatted_address 推荐用法**(完整粒度,聚合更稳)
  - GPS 内部 WGS-84→GCJ-02 转换(db 里 gps 仍是 WGS-84)
  - 新表 scan_runs / amap_quota
  - persons 加 age_estimate / gender_estimate(InsightFace 估计,对儿童不准)
  - meta.errors 新增 `vision_role_mismatch` group(可能误报)
- Phase 3(2026-05)Apple Photos source 接入:
  - source='photos_library' 行的 face cluster_id 用 `apple:<name>`(命名)或 `apple_face:<face_uuid>`(未命名,跨照片不聚类)
  - face_info 漏识别时 pipeline fallback InsightFace,所以 source='photos_library' 的照片也会有 `c_xxx` cluster
- Phase 4(2026-05)语义向量检索:
  - 新表 **photo_embeddings**(bge-small-zh-v1.5,512 维 float32 BLOB)— 见上面表结构
  - 用户跑的 lens server `search_photos` 默认 hybrid(FTS + sem + RRF),你独立读 db 时各自取用即可
- Phase 6(2026-05)inline 化:
  - 主扫描 + reprocess vision 每张顺手 embed + 写 `photo_embeddings`,新照片自动有向量,无需手动跑 `scripts/build_embeddings.py`
  - 老 db 可能有 `photos.vision IS NOT NULL` 但 `photo_embeddings` 缺行的存量,Web 端"配置 → 存储"卡片有"补建缺失 / 全量重建"兜底
- chat 历史日志:`~/.life_lens/chat_log/YYYY-MM-DD.jsonl`(每行一个 JSON,字段 question/plan/result/answer/refs/error)
- 字段语义可能演进。生产前查 `photos.schema_version` 和 `meta.group_versions` 确认版本一致

有问题看 `CLAUDE.md`(项目根目录,主架构 + 踩过的坑全记录)。
