-- life_lens SQLite schema v0.1
-- WAL 模式由 db.py 在连接时设置。
-- 6 个 group 用 JSON 列存,顶层 photo_id 主键,常用维度抽生成列做索引。

CREATE TABLE IF NOT EXISTS photos (
    photo_id        TEXT PRIMARY KEY,
    source          TEXT NOT NULL,        -- 'photos_library' | 'filesystem'
    source_ref      TEXT NOT NULL,        -- Apple uuid 或绝对路径
    original_path   TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    schema_version  TEXT NOT NULL DEFAULT '0.1',

    -- 6 个 group 用 JSON 存,字段细节见 schema/photo_record.py
    identity        TEXT NOT NULL,         -- JSON
    exif            TEXT,                  -- JSON, 可空(扫描中)
    vision          TEXT,                  -- JSON, 可空(Phase 0 / 未到 vision 阶段)
    people          TEXT,                  -- JSON, 可空
    derived         TEXT,                  -- JSON
    meta            TEXT NOT NULL,         -- JSON, 含 processed_at / group_versions / errors

    -- 生成列(从 JSON 抽出来做索引;captured_at 派生于 exif)
    captured_at_utc TEXT GENERATED ALWAYS AS (json_extract(exif, '$.captured_at_utc')) STORED,
    media_type      TEXT GENERATED ALWAYS AS (json_extract(vision, '$.media_type')) STORED,
    is_keeper       INTEGER GENERATED ALWAYS AS (json_extract(derived, '$.is_keeper')) STORED,
    favorite        INTEGER GENERATED ALWAYS AS (json_extract(derived, '$.favorite')) STORED,

    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_photos_captured ON photos(captured_at_utc);
CREATE INDEX IF NOT EXISTS idx_photos_source   ON photos(source);
CREATE INDEX IF NOT EXISTS idx_photos_media    ON photos(media_type);
-- idx_photos_favorite 由 db.py::_migrate_columns 建(老库 favorite 列是 ALTER 后加的,
-- executescript 阶段还不存在,放这里会"no such column";_migrate_columns 在加列后建)

-- 扫描状态机(老库通过 db.py::init_schema 的 ALTER ADD COLUMN 平滑迁移)
CREATE TABLE IF NOT EXISTS jobs (
    photo_id           TEXT PRIMARY KEY,
    status             TEXT NOT NULL,         -- 'pending' | 'processing' | 'done' | 'failed' | 'tombstone'
    stage              TEXT,                  -- 'exif' | 'preprocess' | 'vision' | 'people' | 'derived'
    retry_count        INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    enqueued_at        TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT,
    run_id             TEXT,                  -- 关联到 scan_runs(老 jobs 行 NULL 兼容)
    captured_at_local  TEXT,                  -- 拍照时间(本地,EXIF DateTimeOriginal),驱动处理顺序

    FOREIGN KEY (photo_id) REFERENCES photos(photo_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
-- idx_jobs_captured / idx_jobs_run 由 db.py::_migrate_jobs_columns 创建
-- (老库的 captured_at_local / run_id 列要 ALTER ADD 之后才能建索引)

-- 扫描/重跑历史:每次启动是一个 Run,jobs 行 run_id 关联到此
CREATE TABLE IF NOT EXISTS scan_runs (
    run_id        TEXT PRIMARY KEY,           -- 'run_YYYYMMDD_HHMM_xxxx'
    kind          TEXT NOT NULL,              -- 'scan' | 'retry-failed' | 'reprocess-vision' | 'reprocess-derived' | 'reprocess-faces'
    source_ids    TEXT,                       -- JSON list(可跨 source)
    selector      TEXT,                       -- JSON(reprocess 选择器)
    status        TEXT NOT NULL,              -- 'running' | 'completed' | 'stopped' | 'failed'
    triggered_by  TEXT NOT NULL,              -- 'cli' | 'web'
    total         INTEGER NOT NULL DEFAULT 0,
    done          INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    note          TEXT
);

CREATE INDEX IF NOT EXISTS idx_scan_runs_started ON scan_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_scan_runs_status  ON scan_runs(status);

-- 相册名解析缓存:相册名 → 城市 + 具体地点 + 事件关键词(本地 LLM 解析,见 scanner/album.py)
-- 相册名是高度重复小集合,按 album_name 缓存,unique 名字只调一次本地 Ollama
CREATE TABLE IF NOT EXISTS album_parse_cache (
    album_name  TEXT PRIMARY KEY,
    country     TEXT,                -- 国外国家名(日本/美国);国内或无法判断 null
    city        TEXT,
    province    TEXT,
    place       TEXT,                -- 具体景点/地标(颐和园/外滩);只到城市颗粒度时 null
    tags_json   TEXT,                -- JSON list[str]
    parsed_at   TEXT
);

-- 高德 reverse-geocoding 每日配额追踪(Asia/Shanghai 时区)
CREATE TABLE IF NOT EXISTS amap_quota (
    date_local  TEXT PRIMARY KEY,    -- 'YYYY-MM-DD' (UTC+8)
    count       INTEGER NOT NULL DEFAULT 0,
    last_at     TEXT
);

-- 数据源(Sources 页管理)
CREATE TABLE IF NOT EXISTS sources (
    source_id       TEXT PRIMARY KEY,      -- 'photos_library' | 'fs:<absolute_path>'
    kind            TEXT NOT NULL,         -- 'photos_library' | 'filesystem'
    config          TEXT NOT NULL,         -- JSON 配置
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_scan_at    TEXT,
    created_at      TEXT NOT NULL
);

-- 人脸(Phase 3 备案才用,Phase 0 留空表)
CREATE TABLE IF NOT EXISTS faces (
    face_id         TEXT PRIMARY KEY,
    photo_id        TEXT NOT NULL,
    cluster_id      TEXT,
    embedding       BLOB,                  -- 512 维 float32
    bbox            TEXT,                  -- JSON [x,y,w,h]
    created_at      TEXT NOT NULL,
    FOREIGN KEY (photo_id) REFERENCES photos(photo_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);
CREATE INDEX IF NOT EXISTS idx_faces_photo   ON faces(photo_id);

-- cluster_id → 人名(随时可改) + 视觉 demographics(从种子图平均算,用于 vision prompt hint)
CREATE TABLE IF NOT EXISTS persons (
    cluster_id      TEXT PRIMARY KEY,
    name            TEXT,
    age_estimate    INTEGER,    -- 平均年龄(InsightFace 估计,种子图算)
    gender_estimate INTEGER,    -- 0=female 1=male,种子图多数投票
    updated_at      TEXT NOT NULL
);

-- 全文索引:description / scene / tags / ocr_text / actions / objects
-- tokenize='trigram':SQLite 3.34+ 内置 trigram tokenizer,对中文友好(unicode61 不索引 CJK 字符)
-- 限制:搜索短语 < 3 字符不能命中(对场景/活动/人名/地点等典型查询无影响)
-- 历史:列名曾叫 'caption',Phase 2 改为 'description' 和 vision JSON 字段名一致
CREATE VIRTUAL TABLE IF NOT EXISTS photos_fts USING fts5(
    photo_id UNINDEXED,
    description,
    scene,
    tags,
    ocr_text,
    actions,
    objects,
    tokenize = 'trigram'
);

-- 语义向量(bge-small-zh-v1.5,512 维 float32 BLOB)
-- text_hash = sha1(source_text)[:16],变了重 embed;model 字段做版本隔离(换模型时旧数据失效)
-- 几万张 × 2KB ≈ 几十 MB,全加载内存 brute-force 余弦相似度(详见 query/semantic.py)
CREATE TABLE IF NOT EXISTS photo_embeddings (
    photo_id    TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vec         BLOB NOT NULL,
    text_hash   TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (photo_id) REFERENCES photos(photo_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON photo_embeddings(model);
