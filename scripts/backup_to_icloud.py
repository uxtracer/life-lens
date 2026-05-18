"""增量备份 ~/.life_lens 到 iCloud Drive。

- lens.db 用 sqlite3 .backup API 做 WAL-safe 原子快照(直接 cp 在 WAL 模式可能 inconsistent)
- 其他文件(config.json / seeds / reports)用 rsync 增量同步
- 排除:.cache/preprocessed(5-6GB+ 缓存,删了下次自动重生)、backups/(本地快照)、 lens.db-wal/shm
- AGENT_GUIDE.md 从项目目录拷一份过去(给第三方 agent 看)

跑法:
    python scripts/backup_to_icloud.py            # 增量同步
    python scripts/backup_to_icloud.py --dry-run  # 看会做什么不真做
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path

SOURCE_ROOT = Path.home() / ".life_lens"
# 默认 macOS iCloud Drive 目录,可通过环境变量 LIFE_LENS_ICLOUD_TARGET 覆盖
_DEFAULT_ICLOUD = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "life_lens"
ICLOUD_TARGET = Path(os.environ.get("LIFE_LENS_ICLOUD_TARGET") or _DEFAULT_ICLOUD)
PROJECT_AGENT_GUIDE = Path(__file__).resolve().parent.parent / "AGENT_GUIDE.md"


def backup_sqlite(src: Path, dst: Path, *, dry: bool) -> None:
    """WAL-safe 原子快照(等价于 lens backup,内部走 sqlite3 .backup API)。

    输出 db 显式切换到 DELETE journal mode,避免 iCloud 上残留 -wal/-shm 副产物
    (有些应用打开 SQLite 默认会切 WAL,会污染备份目录)。
    """
    print(f"[db]  {src} → {dst}")
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 清理可能残留的 -wal/-shm
    for suffix in ("-wal", "-shm"):
        stale = dst.with_name(dst.name + suffix)
        if stale.exists():
            stale.unlink()

    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
            # force journal_mode=DELETE,把数据全冲到主 db 文件,不留 wal/shm
            dst_conn.execute("PRAGMA journal_mode=DELETE")
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    # 再保险清一次(切换 journal mode 后理论上已没 wal/shm,但别的应用打开过又会生成)
    for suffix in ("-wal", "-shm"):
        stale = dst.with_name(dst.name + suffix)
        if stale.exists():
            stale.unlink()


def rsync_dir(src: Path, dst: Path, *, dry: bool, label: str) -> None:
    """rsync -a 增量同步目录(不带 --delete,目标多了文件不会被删,符合用户'保存历史'意图)。"""
    if not src.exists():
        print(f"[skip {label}] {src} 不存在")
        return
    dst.mkdir(parents=True, exist_ok=True)
    # macOS 系统 rsync 是 2.6.9(2006 年),不支持 --info=stats1。-v 通用
    cmd = ["rsync", "-a", "-v"]
    if dry:
        cmd.append("--dry-run")
    cmd.append(f"{src}/")    # 末尾 / 表"同步目录内容,不创建外层"
    cmd.append(f"{dst}/")
    print(f"[rsync {label}] {src} → {dst}")
    subprocess.run(cmd, check=True)


def copy_file(src: Path, dst: Path, *, dry: bool, label: str) -> None:
    if not src.exists():
        print(f"[skip {label}] {src} 不存在")
        return
    print(f"[copy {label}] {src} → {dst}")
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    dry = args.dry_run

    if not SOURCE_ROOT.exists():
        print(f"错误:{SOURCE_ROOT} 不存在", file=sys.stderr)
        sys.exit(1)
    ICLOUD_TARGET.mkdir(parents=True, exist_ok=True)

    print(f"=== 备份 {SOURCE_ROOT} → {ICLOUD_TARGET} ({datetime.now():%Y-%m-%d %H:%M}) ===")

    # 1. lens.db — WAL-safe 快照
    backup_sqlite(SOURCE_ROOT / "lens.db", ICLOUD_TARGET / "lens.db", dry=dry)

    # 2. config.json(含 amap key + llm api key,iCloud 自己用 OK,不进 git)
    copy_file(SOURCE_ROOT / "config.json", ICLOUD_TARGET / "config.json", dry=dry, label="config")

    # 3. seeds/(种子人物原图,几 MB)
    rsync_dir(SOURCE_ROOT / "seeds", ICLOUD_TARGET / "seeds", dry=dry, label="seeds")

    # 4. reports/(地图等一次性导出,可选)
    rsync_dir(SOURCE_ROOT / "reports", ICLOUD_TARGET / "reports", dry=dry, label="reports")

    # 5. AGENT_GUIDE.md(给第三方 agent 看的 db 查询手册)
    copy_file(PROJECT_AGENT_GUIDE, ICLOUD_TARGET / "AGENT_GUIDE.md", dry=dry, label="AGENT_GUIDE")

    # 不备份:
    #   - .cache/preprocessed/  5-6 GB+ 可重生的缩略图缓存
    #   - backups/              本地历史快照(目标本身已是云端,不递归备份自己)
    #   - lens.db-wal / -shm    WAL 副产物,sqlite3 .backup 已 checkpoint

    print(f"=== 完成 {datetime.now():%Y-%m-%d %H:%M} ===")
    if dry:
        print("(dry-run,实际未写)")


if __name__ == "__main__":
    main()
