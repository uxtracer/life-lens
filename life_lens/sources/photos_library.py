"""Apple Photos library 数据源(osxphotos)。

跟 FilesystemSource 的关键区别:**iter_faces() 返回 Apple 自带的人脸数据**,scanner
pipeline 因此跳过 InsightFace detect + cluster,直接用 Apple bbox + 真名喂 vision。

设计要点:
- osxphotos.PhotosDB 加载几秒(整库 SQLite + plist 解析),lazy 初始化
- FaceInfo 大约 50-75% 是 phantom(quality=-1, size=0),必须 `fi.quality > 0 and fi.size > 0` 过滤
- 真名拿 `fi.name`(空字符串 = 未命名),**不要拿 PhotoInfo.persons**(经常跟 face_info 对不上)
- bbox 用 `mwg_rs_area`,按 `original_orientation` 做 EXIF 旋转变换(sensor → display 坐标)
- pillow_heif 已经在 raw decode 时应用 EXIF orientation 到 display 方向,所以 bbox 跟着旋
- cluster_id 策略:命名脸用 `apple:<name>` 当稳定 ID,未命名用 `apple_face:<face_uuid>`
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, Optional, Tuple

from .base import FaceFromSource, PhotoRef, PhotoSource, SourceMetadata

log = logging.getLogger(__name__)


def _transform_bbox(
    x: float, y: float, w: float, h: float, orient: int
) -> Tuple[float, float, float, float]:
    """Apple sensor-space normalized bbox → display-space normalized bbox。

    Apple 把 mwg_rs_area 存在原始 sensor 坐标系,pillow_heif 解码时已经把图旋转到 EXIF
    orientation 标记的 display 方向,所以 bbox 也要做对应旋转才能对齐 display 像素。
    orient 来自 `PhotoInfo.original_orientation`(EXIF orientation 标号 1/3/6/8)。
    """
    if orient == 1:
        return (x,     y,     w, h)   # 正向
    if orient == 3:
        return (1 - x, 1 - y, w, h)   # 180°
    if orient == 6:
        return (1 - y, x,     h, w)   # 90° CW
    if orient == 8:
        return (y,     1 - x, h, w)   # 90° CCW
    # 2/4/5/7 是 mirror flip,iPhone 几乎不出现;按 1 处理
    log.warning(f"未支持的 EXIF orientation={orient},按 1 处理")
    return (x, y, w, h)


class ApplePhotosSource(PhotoSource):
    """通过 osxphotos 读 Apple Photos library。"""

    kind_name = "photos_library"

    def __init__(self, library_path: Path):
        self.library_path = Path(library_path).resolve()
        self.source_id = f"apple:{self.library_path.name}"
        self._db = None
        self._photo_by_uuid: dict = {}

    def _ensure_db(self):
        if self._db is None:
            import osxphotos
            log.info(f"loading Apple Photos library: {self.library_path}")
            self._db = osxphotos.PhotosDB(dbfile=str(self.library_path))
            log.info(f"loaded {self._db}")
        return self._db

    def _get_photo(self, uuid: str):
        if uuid not in self._photo_by_uuid:
            db = self._ensure_db()
            matches = db.photos(uuid=[uuid])
            if not matches:
                raise KeyError(f"Apple Photos uuid not found: {uuid}")
            self._photo_by_uuid[uuid] = matches[0]
        return self._photo_by_uuid[uuid]

    def iter_photos(self) -> Iterator[PhotoRef]:
        db = self._ensure_db()
        for p in db.photos():
            if not p.path:
                continue   # 云端未下载或文件已不在磁盘
            src = Path(p.path)
            if not src.exists():
                continue
            self._photo_by_uuid[p.uuid] = p
            ext = src.suffix.lower().lstrip(".")
            yield PhotoRef(
                source_id=self.source_id,
                source_ref=p.uuid,
                original_path=src,
                original_format=ext,
            )

    def get_metadata(self, ref: PhotoRef) -> SourceMetadata:
        try:
            p = self._get_photo(ref.source_ref)
        except KeyError:
            return SourceMetadata()
        persons = [n for n in (p.persons or []) if n and n != "_UNKNOWN_"]
        return SourceMetadata(
            apple_uuid=p.uuid,
            apple_persons=persons,
            apple_albums=list(p.albums or []),
            apple_keywords=list(p.keywords or []),
            apple_favorite=bool(p.favorite),
            apple_hidden=bool(p.hidden),
            apple_place=(p.place.name if p.place else None),
        )

    def iter_faces(
        self, ref: PhotoRef, image_size: Tuple[int, int]
    ) -> Optional[list[FaceFromSource]]:
        try:
            p = self._get_photo(ref.source_ref)
        except KeyError:
            return None
        # Apple 没识别到脸 → 返回 None 让 pipeline fallback 到 InsightFace。
        # 老设计是 return [](信任 Apple 说"无脸"),但实测 Apple 偶有漏识别(早期照片 /
        # 后台脸识别没跑完 / 老 iOS 版本漏检),会让明明有人的照片完全没人脸数据。
        # 代价:每张 Apple 漏识别的照片多走 0.3-1s InsightFace,值得。
        if not p.face_info:
            return None
        real = [fi for fi in p.face_info if (fi.quality or -1) > 0 and fi.size > 0]
        if not real:
            return None

        W, H = image_size
        orient = p.original_orientation or 1
        out: list[FaceFromSource] = []
        for fi in real:
            a = fi.mwg_rs_area
            cx, cy, ww, hh = _transform_bbox(a.x, a.y, a.w, a.h, orient)
            # display 坐标系下:cx, cy 是 face center;ww, hh 是 normalized 宽高
            px = (cx - ww / 2) * W
            py = (cy - hh / 2) * H
            pw = ww * W
            ph = hh * H
            name = fi.name or None
            if name:
                cluster_id = f"apple:{name}"
            else:
                cluster_id = f"apple_face:{fi.uuid}"
            out.append(FaceFromSource(cluster_id=cluster_id, name=name, bbox=(px, py, pw, ph)))
        return out

    def kind(self) -> str:
        return self.kind_name
