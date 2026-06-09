"""A 层测试:Apple Photos 数据源在入口跳过「隐藏」相册照片(隐私保证)。

用假 PhotosDB 注入,不依赖 osxphotos / 真实 Photos 库。
回归点:hidden 照片绝不能被 iter_photos yield —— 否则会进 Phase A 队列、跑 vision、入库。
"""
from __future__ import annotations

from pathlib import Path

from life_lens.sources.photos_library import ApplePhotosSource


class _FakePhoto:
    def __init__(self, uuid: str, path: str, hidden: bool, ismovie: bool = False):
        self.uuid = uuid
        self.path = path
        self.hidden = hidden
        self.ismovie = ismovie


class _FakeDB:
    def __init__(self, photos):
        self._photos = photos

    def photos(self):
        return list(self._photos)


def test_iter_photos_skips_hidden(tmp_path: Path):
    visible1 = tmp_path / "a.jpg"; visible1.write_bytes(b"x")
    visible2 = tmp_path / "b.heic"; visible2.write_bytes(b"y")
    secret = tmp_path / "secret.jpg"; secret.write_bytes(b"z")

    src = ApplePhotosSource(tmp_path)        # __init__ 懒加载,不会真去读 osxphotos
    src._db = _FakeDB([                       # 直接注入假库,绕过 _ensure_db 的真实加载
        _FakePhoto("u1", str(visible1), hidden=False),
        _FakePhoto("hidden-uuid", str(secret), hidden=True),   # 必须被跳过
        _FakePhoto("u2", str(visible2), hidden=False),
    ])

    refs = list(src.iter_photos())
    got = {r.source_ref for r in refs}
    assert got == {"u1", "u2"}               # 只有非隐藏的两张
    assert "hidden-uuid" not in got          # 隐藏照片连 ref 都不产生(→ 不进队列/不扫描/不入库)


def test_iter_photos_hidden_skipped_before_path_check(tmp_path: Path):
    """隐藏判断在 path 存在性检查之前 —— 即使隐藏照片文件存在也照样跳过。"""
    exists = tmp_path / "h.jpg"; exists.write_bytes(b"z")
    src = ApplePhotosSource(tmp_path)
    src._db = _FakeDB([_FakePhoto("h", str(exists), hidden=True)])
    assert list(src.iter_photos()) == []


def test_iter_photos_skips_videos(tmp_path: Path):
    """视频(p.ismovie)不纳入主流程,与隐藏同样在入口跳过。"""
    img = tmp_path / "a.jpg"; img.write_bytes(b"x")
    mov = tmp_path / "v.mov"; mov.write_bytes(b"y")
    src = ApplePhotosSource(tmp_path)
    src._db = _FakeDB([
        _FakePhoto("img", str(img), hidden=False, ismovie=False),
        _FakePhoto("vid", str(mov), hidden=False, ismovie=True),   # 应被跳过
    ])
    got = {r.source_ref for r in src.iter_photos()}
    assert got == {"img"}
