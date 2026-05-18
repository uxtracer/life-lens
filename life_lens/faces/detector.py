"""InsightFace 人脸检测 + embedding 封装。

懒加载模型(首次调用时下载 buffalo_l ~280MB 到 ~/.insightface/);
跨平台 provider:mac CoreML / 其他 CPU。

不引入硬依赖 — 模块顶部 try-import,缺包时调用方拿到 None,链路自动跳过 face 阶段。
"""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

try:
    from insightface.app import FaceAnalysis  # type: ignore
    _IF_AVAILABLE = True
except Exception:  # ImportError / ABI 不匹配 / 缺 onnxruntime
    FaceAnalysis = None  # type: ignore
    _IF_AVAILABLE = False


@dataclass
class FaceDetection:
    bbox: tuple[float, float, float, float]   # x, y, w, h(像素,基于输入 jpeg 的坐标)
    embedding: np.ndarray                       # 512 维 float32,L2 归一化
    det_score: float                            # 检测置信度 0-1
    age: Optional[int] = None                   # InsightFace age 估计(0-100)
    gender: Optional[int] = None                # InsightFace 性别估计 0=female 1=male


_DETECTOR: Optional["FaceAnalysis"] = None


def available() -> bool:
    return _IF_AVAILABLE


def _provider() -> list[str]:
    """优先 CoreML(Mac),回退 CPU。环境变量 LENS_ORT_PROVIDER 可强制覆盖。"""
    if "LENS_ORT_PROVIDER" in os.environ:
        return [os.environ["LENS_ORT_PROVIDER"]]
    import platform
    if platform.system() == "Darwin":
        return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def get_detector() -> Optional["FaceAnalysis"]:
    global _DETECTOR
    if not _IF_AVAILABLE:
        return None
    if _DETECTOR is None:
        log.info("loading InsightFace buffalo_l (首次会下载 ~280MB)...")
        det = FaceAnalysis(name="buffalo_l", providers=_provider())
        det.prepare(ctx_id=0, det_size=(640, 640))
        _DETECTOR = det
        log.info("InsightFace ready")
    return _DETECTOR


def detect(jpeg_bytes: bytes) -> list[FaceDetection]:
    """对一张 JPEG 检测所有人脸。返回空列表表示没检测到或模型不可用。"""
    det = get_detector()
    if det is None:
        return []
    try:
        im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        arr = np.asarray(im)  # H, W, 3 RGB
        # InsightFace 默认吃 BGR
        bgr = arr[:, :, ::-1]
        faces = det.get(bgr)
    except Exception as e:
        log.exception("face detection failed: %s", e)
        return []

    out: list[FaceDetection] = []
    for f in faces:
        x1, y1, x2, y2 = f.bbox.astype(float)
        # L2 归一化 embedding(后续余弦距离 = 点积)
        emb = f.normed_embedding.astype(np.float32) if hasattr(f, "normed_embedding") else _l2(f.embedding.astype(np.float32))
        # InsightFace buffalo_l 内置 age + sex 估计;None 兜底
        age = getattr(f, "age", None)
        try: age = int(age) if age is not None else None
        except Exception: age = None
        # InsightFace 的 face 对象有 sex(字符串 'M'/'F')和 gender(整型 0/1)两个属性,
        # 不同版本可能只有其中一个;统一规范成 int(0=female, 1=male)
        sex_raw = getattr(f, "sex", None)
        if sex_raw is None:
            sex_raw = getattr(f, "gender", None)
        if isinstance(sex_raw, str):
            sex = 1 if sex_raw.upper().startswith("M") else 0
        else:
            try: sex = int(sex_raw) if sex_raw is not None else None
            except Exception: sex = None
        out.append(FaceDetection(
            bbox=(x1, y1, x2 - x1, y2 - y1),
            embedding=emb,
            det_score=float(getattr(f, "det_score", 1.0)),
            age=age,
            gender=sex,
        ))
    return out


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v if n == 0 else v / n
