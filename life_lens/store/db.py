"""SQLite 连接管理。WAL 模式 + foreign_keys + 行工厂 + schema 版本号 + 升级自动备份。"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Schema 版本号。每次破坏性 / 需要 migration 的改动都 +1。
# - v0: 未追踪版本(2026-05 之前所有老 db)
# - v1: 首次正式追踪版本(写入 user_version PRAGMA 的起点)
#       涵盖: jobs.run_id+captured_at_local / scan_runs.snapshot_* /
#             persons.age+gender_estimate / photos_fts trigram / amap_quota /
#             photo_embeddings(语义向量)
# 后续 v2+ 时:加 _migrate_v1_to_v2 之类函数,init_schema 按版本递增调用。
SCHEMA_VERSION = 1

log = logging.getLogger(__name__)


def get_db_path(root: Path) -> Path:
    return root / "lens.db"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")   # 30s,防偶发 'database is locked'
    return conn


def _get_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_user_version(conn: sqlite3.Connection, v: int) -> None:
    # PRAGMA user_version 不支持参数化,直接 f-string(v 来自整数常量,无注入风险)
    conn.execute(f"PRAGMA user_version = {int(v)}")


def _conn_db_path(conn: sqlite3.Connection) -> Path | None:
    """从 connection 反查物理 db 路径(:memory: 返 None)。"""
    row = conn.execute("PRAGMA database_list").fetchone()
    if not row:
        return None
    file_str = row["file"] if "file" in row.keys() else row[2]
    if not file_str:
        return None
    return Path(file_str)


def _auto_backup_before_upgrade(conn: sqlite3.Connection, from_ver: int) -> None:
    """schema 升级前自动 sqlite3 .backup 一份到 ~/.life_lens/backups/。
    用 backup API 是 WAL-safe 的,不会跟正在运行的 web 抢锁。

    跳过条件:
      - 内存库 / 不存在的 db 文件 → 跳过(测试环境)
      - photos 表不存在(全新初始化,不是升级) → 跳过(首次 init 不需要"备份")
    """
    db_path = _conn_db_path(conn)
    if db_path is None or str(db_path) == ":memory:" or not db_path.exists():
        return
    # 全新空 db(photos 表都没建) → 不是升级,无需备份
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='photos'"
    ).fetchone()
    if not row:
        return

    backups_dir = db_path.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backups_dir / f"auto-pre-v{SCHEMA_VERSION}-from-v{from_ver}-{ts}.db"

    log.warning(
        "db schema 升级 v%d → v%d,自动备份 → %s(出问题手动 mv 回 lens.db 可回滚)",
        from_ver, SCHEMA_VERSION, backup_path,
    )
    bk = sqlite3.connect(str(backup_path))
    try:
        conn.backup(bk)
    finally:
        bk.close()


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等初始化 + 老库迁移 + schema 版本号追踪。

    流程:
      1. 读 PRAGMA user_version(0 = 未追踪 = 老库 / 全新空库)
      2. 如果 < SCHEMA_VERSION 且 db 非空 → 自动 sqlite3 .backup 一份
      3. 执行 schema.sql(CREATE IF NOT EXISTS 安全,新表 / 新索引会建,旧的不动)
      4. 跑 _migrate_columns(对老库 ALTER ADD COLUMN 等)
      5. PRAGMA user_version = SCHEMA_VERSION
    """
    cur_ver = _get_user_version(conn)
    if cur_ver < SCHEMA_VERSION:
        _auto_backup_before_upgrade(conn, cur_ver)

    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    _migrate_columns(conn)

    if cur_ver < SCHEMA_VERSION:
        _set_user_version(conn, SCHEMA_VERSION)
        log.info("db schema 升级完成 → v%d", SCHEMA_VERSION)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "run_id" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN run_id TEXT")
    if "captured_at_local" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN captured_at_local TEXT")
    # 索引(CREATE IF NOT EXISTS 幂等,执行成本极低)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_captured ON jobs(captured_at_local)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_run      ON jobs(run_id)")

    # scan_runs 加 3 个时间快照列(避免 reprocess 偷 jobs 行后历史 run 时间游标丢失)
    run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scan_runs)").fetchall()}
    if "snapshot_captured_min" not in run_cols:
        conn.execute("ALTER TABLE scan_runs ADD COLUMN snapshot_captured_min TEXT")
    if "snapshot_captured_max" not in run_cols:
        conn.execute("ALTER TABLE scan_runs ADD COLUMN snapshot_captured_max TEXT")
    if "snapshot_scanned_up_to" not in run_cols:
        conn.execute("ALTER TABLE scan_runs ADD COLUMN snapshot_scanned_up_to TEXT")

    # persons 加 age/gender estimate(给 vision prompt 做位置 + demographics hint 用)
    p_cols = {row["name"] for row in conn.execute("PRAGMA table_info(persons)").fetchall()}
    if "age_estimate" not in p_cols:
        conn.execute("ALTER TABLE persons ADD COLUMN age_estimate INTEGER")
    if "gender_estimate" not in p_cols:
        conn.execute("ALTER TABLE persons ADD COLUMN gender_estimate INTEGER")

    # photos_fts 旧 schema 用 unicode61(不索引中文),迁移到 trigram。
    # FTS5 虚拟表不能 ALTER tokenizer / 列名,只能 DROP + CREATE。
    # 两种迁移触发条件:
    #   1) 旧 unicode61 tokenizer(中文不索引)
    #   2) 旧 'caption' 列名(应该是 'description')
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='photos_fts'"
    ).fetchone()
    needs_migrate = row and row[0] and (
        ("unicode61" in row[0] and "trigram" not in row[0])
        or "caption" in row[0]
    )
    if needs_migrate:
        log = __import__("logging").getLogger(__name__)
        log.info("photos_fts: 检测到旧 schema(unicode61 或 caption 列名),DROP + CREATE 新版 + 回填...")
        conn.execute("DROP TABLE photos_fts")
        conn.execute("""
            CREATE VIRTUAL TABLE photos_fts USING fts5(
                photo_id UNINDEXED, description, scene, tags, ocr_text, actions, objects,
                tokenize = 'trigram'
            )
        """)
        _refill_fts_after_migration(conn)


def _refill_fts_after_migration(conn: sqlite3.Connection) -> None:
    """FTS 迁移后,对所有已 vision 完成的 photo 重建 FTS 行。"""
    import json as _json
    rows = conn.execute(
        "SELECT photo_id, vision, people FROM photos WHERE vision IS NOT NULL"
    ).fetchall()
    for r in rows:
        vision = _json.loads(r["vision"]) if r["vision"] else None
        people = _json.loads(r["people"]) if r["people"] else None
        if not vision:
            continue
        actions = " ".join(p.get("action", "") for p in ((people or {}).get("persons") or []))
        objects = " ".join(vision.get("objects") or [])
        tags = " ".join(vision.get("tags") or [])
        conn.execute(
            """
            INSERT INTO photos_fts(photo_id, description, scene, tags, ocr_text, actions, objects)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["photo_id"],
                vision.get("description", "") or "",
                vision.get("scene", "") or "",
                tags,
                vision.get("ocr_text", "") or "",
                actions,
                objects,
            ),
        )


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
