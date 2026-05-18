"""单张照片处理链:source → exif → preprocess → faces → vision(×2) → people → derived → store

face 在 vision 之前 — vision 调用时把已命名 person 名注入 prompt,LLM 在 description 中直接用真名。
vision 阶段是**两次 LLM 调用**(v9):description + struct,各自专注一个任务避免长 prompt 注意力分散。
推荐工作流:**先上传种子人物,再扫描照片**,这样 description 一开始就用真名,不用回头 reprocess。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..sources.base import PhotoRef, PhotoSource
from ..exif import extract as exif_extract
from ..preprocess.cache import ensure_preprocessed
from ..schema.photo_record import new_record, stamp_group_version, record_error
from ..vision.base import VisionModel
from ..faces import detector as face_det, cluster as face_cluster
from ..store import repo
from . import identity as ident
from . import derived as derive_mod


def process_one(
    root: Path,
    source: PhotoSource,
    ref: PhotoRef,
    vision: Optional[VisionModel] = None,
    vision_lock=None,
    enable_faces: bool = True,
    faces_lock=None,
    db_conn=None,
) -> dict:
    """处理一张照片,返回完整 PhotoRecord(dict)。失败的 group 会写到 meta.errors。"""

    # ---- identity ----
    chash = ident.content_hash(ref.original_path)
    photo_id = ident.photo_id_for(ref.source_id, ref.source_ref, chash)
    rec = new_record(
        photo_id=photo_id,
        source=source.kind(),
        source_ref=ref.source_ref,
        original_path=str(ref.original_path),
        content_hash=chash,
        file_size_bytes=ref.original_path.stat().st_size,
        original_format=ref.original_format,
    )

    # ---- preprocess(送视觉模型/缩略图都用同一个缓存)----
    try:
        cache_path = ensure_preprocessed(root, photo_id, ref.original_path)
        rec["identity"]["preprocessed_path"] = str(cache_path)
        stamp_group_version(rec, "preprocess", "v1")
    except Exception as e:
        record_error(rec, "preprocess", str(e))

    # ---- exif ----
    try:
        rec["exif"] = exif_extract.extract(ref.original_path)
        stamp_group_version(rec, "exif", "v1")
    except Exception as e:
        record_error(rec, "exif", str(e))

    # ---- source metadata(免费信号)----
    apple_persons: list[str] = []
    try:
        md = source.get_metadata(ref)
        apple_persons = list(md.apple_persons or [])
        rec["meta"]["source_signals"] = {
            "albums":     md.apple_albums,
            "keywords":   md.apple_keywords,
            "favorite":   md.apple_favorite,
            "hidden":     md.apple_hidden,
            "place_apple": md.apple_place,
        }
    except Exception as e:
        record_error(rec, "source_metadata", str(e))

    # ---- faces(优先 source 自带 → fallback InsightFace)— 先跑,让 vision 能用上人名 ----
    face_cluster_ids: list[str] = []
    face_named: list[str] = []
    face_unnamed: list[str] = []
    face_items_for_vision: list[tuple[int, Optional[str]]] = []   # [(1, "张三"), (2, None), ...]
    face_bboxes_for_annotate: list[tuple[float, float, float, float]] = []   # 与 face_items 同顺序
    preprocessed_path = rec["identity"].get("preprocessed_path")

    # 1) 先问 source 自己有没有 face 数据(Apple Photos 走这条),它给的 cluster_id + bbox 已经
    #    是 preprocessed image 像素坐标,完全跳过 InsightFace detect + cluster
    source_faces = None
    if enable_faces and db_conn is not None and preprocessed_path:
        try:
            from PIL import Image
            with Image.open(preprocessed_path) as _im:
                image_size = _im.size
            source_faces = source.iter_faces(ref, image_size)
        except Exception as e:
            record_error(rec, "faces_source_probe", f"{type(e).__name__}: {e}")

    if source_faces is not None:
        try:
            _store_source_faces(
                db_conn, rec, source_faces,
                face_cluster_ids, face_named, face_unnamed,
                face_items_for_vision, face_bboxes_for_annotate,
            )
            stamp_group_version(rec, "faces", f"{source.kind()}@v1")
        except Exception as e:
            record_error(rec, "faces", f"{type(e).__name__}: {e}")
    elif enable_faces and face_det.available() and db_conn is not None and preprocessed_path:
        try:
            jpeg_bytes = Path(preprocessed_path).read_bytes()
            # InsightFace 不像 Ollama 那么独占,但同一时间多次 detect 会抢 GPU/CoreML;用 lock 串行更稳
            if faces_lock is not None:
                with faces_lock:
                    detections = face_det.detect(jpeg_bytes)
                    _assign_and_store(db_conn, rec, detections, face_cluster_ids, face_named, face_unnamed, face_items_for_vision, face_bboxes_for_annotate)
            else:
                detections = face_det.detect(jpeg_bytes)
                _assign_and_store(db_conn, rec, detections, face_cluster_ids, face_named, face_unnamed, face_items_for_vision, face_bboxes_for_annotate)
            stamp_group_version(rec, "faces", "insightface-buffalo_l@v1")
        except Exception as e:
            record_error(rec, "faces", f"{type(e).__name__}: {e}")

    # ---- vision(拆两次 LLM 调用,用同一张 set-of-mark 标注图) ----
    actions_from_struct: list[str] = []
    if vision is not None and rec["identity"].get("preprocessed_path"):
        try:
            jpeg_raw = Path(rec["identity"]["preprocessed_path"]).read_bytes()
            jpeg_for_llm = jpeg_raw
            if face_bboxes_for_annotate:
                from ..vision.annotate import annotate_faces
                jpeg_for_llm = annotate_faces(jpeg_raw, face_bboxes_for_annotate)

            # 给 prompt 组装新 face_items(v9.2):带 bbox 算位置 + persons.age/gender 弱辅助
            face_items_for_prompt: list = list(face_items_for_vision)  # 默认 tuple 格式兜底
            if face_items_for_vision and face_bboxes_for_annotate and db_conn is not None:
                try:
                    from PIL import Image as _Image
                    import io as _io
                    with _Image.open(_io.BytesIO(jpeg_raw)) as _im:
                        image_size = _im.size
                    demo_map = repo.cluster_demographics_map(db_conn)
                    face_items_for_prompt = []
                    for (idx, name), bbox, cid in zip(
                        face_items_for_vision, face_bboxes_for_annotate, face_cluster_ids
                    ):
                        demo = demo_map.get(cid, {})
                        face_items_for_prompt.append({
                            "idx": idx,
                            "name": name,
                            "bbox": bbox,
                            "image_size": image_size,
                            "age": demo.get("age"),
                            "gender": demo.get("gender"),
                        })
                except Exception as e:
                    log_err = f"face_items_v2 组装失败: {type(e).__name__}: {e}"
                    record_error(rec, "vision_prompt_build", log_err)
                    face_items_for_prompt = list(face_items_for_vision)

            # Ollama 串行(同一进程 Ollama 单实例不能并行),用 lock 保证 semaphore=1
            def _run_vision():
                desc_res = vision.describe_description(jpeg_for_llm, face_items=face_items_for_prompt or None)
                struct_res = vision.describe_struct(jpeg_for_llm, face_items=face_items_for_prompt or None)
                return desc_res, struct_res
            if vision_lock is not None:
                with vision_lock:
                    desc_res, struct_res = _run_vision()
            else:
                desc_res, struct_res = _run_vision()

            # 合并:vision = description + 结构化字段(不含 actions,actions 进 people.persons)
            vision_group: dict = {}
            if struct_res.parsed:
                struct = struct_res.parsed
                for k in ("media_type", "subject", "scene", "objects", "tags", "ocr_text", "mood"):
                    vision_group[k] = struct.get(k)
                actions_from_struct = list(struct.get("actions") or [])
                stamp_group_version(rec, "vision_struct", vision.struct_version)
                for w in (struct_res.warnings or []):
                    record_error(rec, "vision_struct_warn", w)
            else:
                record_error(rec, "vision_struct", struct_res.error or "no_parsed")
                rec["meta"].setdefault("vision_debug", {})["struct_raw"] = struct_res.raw_text

            if desc_res.parsed:
                vision_group["description"] = desc_res.parsed.get("description", "")
                stamp_group_version(rec, "vision_description", vision.description_version)
                for w in (desc_res.warnings or []):
                    record_error(rec, "vision_desc_warn", w)
            else:
                record_error(rec, "vision_description", desc_res.error or "no_parsed")
                rec["meta"].setdefault("vision_debug", {})["desc_raw"] = desc_res.raw_text

            if vision_group:
                rec["vision"] = vision_group
        except Exception as e:
            record_error(rec, "vision", f"{type(e).__name__}: {e}")

    # ---- people(persons[] 含 cluster_id + 实时 resolved name + LLM 给的 action) ----
    persons: list[dict] = []
    for (idx, name), cid in zip(face_items_for_vision, face_cluster_ids):
        action = actions_from_struct[idx - 1] if idx - 1 < len(actions_from_struct) else ""
        persons.append({"cluster_id": cid, "name": name, "action": action})
    rec["people"] = {
        "persons":              persons,
        "names":                sorted(set(apple_persons + face_named)),
        "face_count":           len(face_cluster_ids),
        "source_apple_persons": apple_persons,
    }
    stamp_group_version(rec, "people", "apple+insightface@v9")

    # ---- self-check:description vs persons[].action 一致性 ----
    # struct call 输出的 actions 是精确的(dict 锁红框编号 → action),description 是自由文本,
    # 8B 模型容易把"X 的动作"写到 Y 名下。这里把不一致 surface 为 meta.errors 让用户能看到。
    try:
        from ..vision.role_check import check_description_vs_persons
        vision_desc = (rec.get("vision") or {}).get("description") or ""
        if vision_desc and persons:
            issues = check_description_vs_persons(vision_desc, persons)
            for issue in issues:
                record_error(rec, "vision_role_mismatch", issue)
    except Exception as e:
        record_error(rec, "vision_role_check_failed", f"{type(e).__name__}: {e}")

    # ---- derived(纯规则,基于 exif + vision)----
    try:
        rec["derived"] = derive_mod.compute(rec, conn=db_conn)
        stamp_group_version(rec, "derived", "rules-v1")
    except Exception as e:
        record_error(rec, "derived", str(e))

    return rec


def _assign_and_store(
    conn,
    rec: dict,
    detections,
    out_cluster_ids: list,
    out_named: list,
    out_unnamed: list,
    out_face_items: list = None,    # [(1, name_or_None), ...] 1-based
    out_bboxes: list = None,         # bbox 列表,与 out_face_items 同顺序,供 annotate 使用
) -> None:
    """对每张检测脸:assign cluster → insert faces 表 → 按命名状态分桶 → 输出 face_items + bboxes 供 vision/annotate 用。"""
    photo_id = rec["identity"]["photo_id"]
    repo.ensure_photo_row(conn, rec["identity"])
    repo.delete_faces_of_photo(conn, photo_id)
    for idx, d in enumerate(detections):
        cid, _is_new = face_cluster.assign(conn, d.embedding)
        face_id = f"{photo_id}-{idx}"
        repo.insert_face(
            conn, face_id, photo_id, cid,
            d.embedding.tobytes(), d.bbox,
        )
        out_cluster_ids.append(cid)
        name = face_cluster.display_name(conn, cid)
        is_named = bool(name and name != cid)
        if is_named:
            out_named.append(name)
        else:
            out_unnamed.append(cid)
        if out_face_items is not None:
            out_face_items.append((idx + 1, name if is_named else None))
        if out_bboxes is not None:
            out_bboxes.append(tuple(d.bbox))


def _store_source_faces(
    conn,
    rec: dict,
    source_faces,
    out_cluster_ids: list,
    out_named: list,
    out_unnamed: list,
    out_face_items: list,
    out_bboxes: list,
) -> None:
    """用 source 自带的 face 数据(已带 cluster_id + name + 像素 bbox)写 faces / persons 表。

    不依赖 InsightFace embedding,faces.embedding 写空 blob;persons 表 upsert 命名脸的真名。
    """
    photo_id = rec["identity"]["photo_id"]
    repo.ensure_photo_row(conn, rec["identity"])
    repo.delete_faces_of_photo(conn, photo_id)
    for idx, f in enumerate(source_faces):
        face_id = f"{photo_id}-{idx}"
        repo.insert_face(conn, face_id, photo_id, f.cluster_id, b"", tuple(f.bbox))
        if f.name:
            # source 已命名 → 写进 persons 表,后续 query 透明 resolve
            repo.set_person_name(conn, f.cluster_id, f.name)
        out_cluster_ids.append(f.cluster_id)
        if f.name:
            out_named.append(f.name)
        else:
            out_unnamed.append(f.cluster_id)
        out_face_items.append((idx + 1, f.name))
        out_bboxes.append(tuple(f.bbox))


# 旧的 _fill_vision_people_names 已删除 — 新设计里 LLM 不再输出 people_in_photo.name,
# Python 直接用 cluster_id 组装 people.persons[],resolve 时 join persons 表拿真名。
