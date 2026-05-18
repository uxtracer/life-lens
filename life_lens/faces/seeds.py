"""种子人物管理。

种子图 = 用户提供的"X 是这个人"参考脸,作为该 person 的预置 embedding。
存储:
  - <root>/seeds/{cluster_id}/{n}.jpg 原图(用户上传的)
  - <root>/.cache/preprocessed/seed_{photo_id}.jpg 预处理后(走主缓存机制)
  - photos 表:source='seed',source_ref=种子原图路径
  - faces 表:cluster_id 指向 person
  - persons 表:cluster_id → name

数据流:create_or_extend_seed_person(name, [Path, ...]) → 检测每张最大脸 → 写入数据库
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

import numpy as np

from ..exif.extract import extract as exif_extract
from ..preprocess.cache import ensure_preprocessed
from ..scanner.identity import content_hash
from ..schema.photo_record import new_record, stamp_group_version
from ..store import repo
from . import detector

log = logging.getLogger(__name__)


def seeds_dir(root: Path, cluster_id: str) -> Path:
    d = root / "seeds" / cluster_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_or_create_person_by_name(conn: sqlite3.Connection, name: str) -> str:
    """按 name 找已存在的 person → 复用 cluster_id;否则新建。"""
    row = conn.execute("SELECT cluster_id FROM persons WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    cluster_id = f"seed_{uuid.uuid4().hex[:8]}"
    repo.set_person_name(conn, cluster_id, name)
    return cluster_id


def list_named_persons(conn: sqlite3.Connection) -> list[dict]:
    """所有已命名人物。统一显示种子 + 主库匹配的总脸数。

    代表脸优先用种子图(种子是用户刻意校准的标杆,质量高);没种子时用任意 face。
    """
    rows = conn.execute(
        """
        SELECT
            p.cluster_id, p.name,
            SUM(CASE WHEN ph.source = 'seed' THEN 1 ELSE 0 END) AS seed_count,
            COUNT(f.face_id) AS total_face_count
        FROM persons p
        LEFT JOIN faces f ON f.cluster_id = p.cluster_id
        LEFT JOIN photos ph ON ph.photo_id = f.photo_id
        WHERE p.name IS NOT NULL
        GROUP BY p.cluster_id, p.name
        ORDER BY p.name
        """
    ).fetchall()
    result = []
    for r in rows:
        cid = r["cluster_id"]
        # 优先种子图的脸
        seed_faces = conn.execute(
            """
            SELECT f.face_id FROM faces f
            JOIN photos ph ON ph.photo_id = f.photo_id
            WHERE f.cluster_id = ? AND ph.source = 'seed'
            LIMIT 3
            """,
            (cid,),
        ).fetchall()
        sample_face_ids = [x[0] for x in seed_faces]
        # 不够 3 个,用主库的脸补
        if len(sample_face_ids) < 3:
            extra = conn.execute(
                """
                SELECT f.face_id FROM faces f
                JOIN photos ph ON ph.photo_id = f.photo_id
                WHERE f.cluster_id = ? AND ph.source != 'seed'
                LIMIT ?
                """,
                (cid, 3 - len(sample_face_ids)),
            ).fetchall()
            sample_face_ids.extend([x[0] for x in extra])
        result.append({
            "cluster_id":       cid,
            "name":             r["name"],
            "seed_count":       int(r["seed_count"] or 0),
            "total_face_count": int(r["total_face_count"] or 0),
            "sample_face_ids":  sample_face_ids,
        })
    return result


def add_seeds(
    conn: sqlite3.Connection,
    root: Path,
    cluster_id: str,
    image_paths: list[Path],
) -> tuple[int, list[str]]:
    """把若干张种子图入库,关联到给定 cluster_id。

    每张图 → 复制到 seeds_dir → 跑预处理 → InsightFace 检测最大脸 → 写 photos + faces。
    返回 (成功数, 警告消息列表)。
    """
    target_dir = seeds_dir(root, cluster_id)
    warnings: list[str] = []
    success = 0
    # 累计 age + gender 用于结束时算平均(给 vision prompt 提供 demographics hint)
    ages_collected: list[int] = []
    genders_collected: list[int] = []

    for src in image_paths:
        if not src.exists() or not src.is_file():
            warnings.append(f"文件不存在: {src}")
            continue
        try:
            # 1) 把原图复制到 seeds_dir(用 content_hash 命名避免冲突)
            chash = content_hash(src)
            ext = src.suffix.lower() or ".jpg"
            seed_filename = f"{chash[:16]}{ext}"
            seed_dest = target_dir / seed_filename
            if not seed_dest.exists():
                shutil.copy2(src, seed_dest)

            # 2) 走预处理缓存(JPEG q=85, 长边 1024)
            photo_id = f"seed_{chash[:16]}"
            cache_path = ensure_preprocessed(root, photo_id, seed_dest)

            # 3) InsightFace 检测最大脸
            jpeg_bytes = cache_path.read_bytes()
            detections = detector.detect(jpeg_bytes)
            if not detections:
                warnings.append(f"未检测到人脸: {src.name}")
                continue
            # 取最大的那张(bbox 面积)
            best = max(detections, key=lambda d: d.bbox[2] * d.bbox[3])

            # 4) 写 photos 行(source='seed',对外不可见)
            identity = {
                "photo_id":          photo_id,
                "source":            "seed",
                "source_ref":        str(seed_dest),
                "original_path":     str(seed_dest),
                "content_hash":      chash,
                "file_size_bytes":   seed_dest.stat().st_size,
                "original_format":   ext.lstrip("."),
                "sidecar_path":      None,
                "preprocessed_path": str(cache_path),
            }
            repo.ensure_photo_row(conn, identity)

            # 5) 写 face 行
            face_id = f"{photo_id}-0"
            repo.insert_face(
                conn, face_id, photo_id, cluster_id,
                best.embedding.tobytes(), best.bbox,
            )
            # 累计 demographics 给 vision prompt 用
            if best.age is not None:
                ages_collected.append(best.age)
            if best.gender is not None:
                genders_collected.append(best.gender)
            success += 1
        except Exception as e:
            log.exception("add_seed failed for %s", src)
            warnings.append(f"{src.name}: {type(e).__name__}: {e}")

    # 算 cluster 的 demographics:age 平均、gender 多数投票
    # 合并本次和数据库里已有的(避免追加种子时丢历史 face)
    _refresh_cluster_demographics(conn, cluster_id)

    return success, warnings


def _refresh_cluster_demographics(conn: sqlite3.Connection, cluster_id: str) -> None:
    """重新算 cluster 的 age / gender estimate(用该 cluster 所有种子图的 face embedding 对应
    的 age/gender — 但 db 里只存了 embedding,没存原始 age/gender)。

    这里改成:对该 cluster 的所有种子图,从 .cache/preprocessed/ 重新 detect 一次拿 age/gender。
    这是种子上传后一次性操作,慢点没关系(每张 ~1s)。
    """
    from collections import Counter
    rows = conn.execute(
        """
        SELECT ph.photo_id FROM photos ph
        JOIN faces f ON f.photo_id = ph.photo_id
        WHERE f.cluster_id = ? AND ph.source = 'seed'
        """,
        (cluster_id,),
    ).fetchall()
    if not rows:
        return

    # 这里复用 photos.identity.preprocessed_path 拿 .cache 路径
    from ..preprocess.cache import cache_path as _cache_path
    import sqlite3 as _sqlite3
    ages: list[int] = []
    genders: list[int] = []
    for r in rows:
        pid = r[0]
        row = conn.execute("SELECT identity FROM photos WHERE photo_id=?", (pid,)).fetchone()
        if not row:
            continue
        import json as _json
        ident = _json.loads(row[0])
        cp_str = ident.get("preprocessed_path")
        cp = Path(cp_str) if cp_str else None
        if not cp or not cp.exists():
            continue
        try:
            detections = detector.detect(cp.read_bytes())
            if not detections:
                continue
            best = max(detections, key=lambda d: d.bbox[2] * d.bbox[3])
            if best.age is not None:
                ages.append(best.age)
            if best.gender is not None:
                genders.append(best.gender)
        except Exception:
            log.exception("demographics extract failed for seed %s", pid)

    age_estimate = int(round(sum(ages) / len(ages))) if ages else None
    gender_estimate = None
    if genders:
        # 多数投票(female=0, male=1)
        c = Counter(genders)
        gender_estimate, _ = c.most_common(1)[0]
    repo.set_person_demographics(conn, cluster_id, age_estimate, gender_estimate)


def delete_seed_person(conn: sqlite3.Connection, root: Path, cluster_id: str) -> None:
    """彻底删除一个种子人物:persons + faces + seed photos + 磁盘种子目录。"""
    # 先拿 seed photo_ids
    rows = conn.execute(
        "SELECT photo_id FROM photos WHERE source = 'seed' AND photo_id IN "
        "(SELECT photo_id FROM faces WHERE cluster_id = ?)",
        (cluster_id,),
    ).fetchall()
    seed_photo_ids = [r[0] for r in rows]

    # 删 photos(级联删 faces) + persons
    conn.executemany("DELETE FROM photos WHERE photo_id = ?", [(pid,) for pid in seed_photo_ids])
    conn.execute("DELETE FROM persons WHERE cluster_id = ?", (cluster_id,))

    # 清磁盘
    sd = root / "seeds" / cluster_id
    if sd.exists():
        shutil.rmtree(sd, ignore_errors=True)
