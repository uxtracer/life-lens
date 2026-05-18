"""Photo identity:用 size+mtime+头尾 64KB 计算稳定 hash,避免整文件 SHA1。"""
from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 64 * 1024  # 64KB


def content_hash(path: Path) -> str:
    st = path.stat()
    h = hashlib.sha1()
    h.update(str(st.st_size).encode())
    h.update(str(st.st_mtime_ns).encode())
    with path.open("rb") as f:
        head = f.read(CHUNK)
        h.update(head)
        if st.st_size > CHUNK * 2:
            f.seek(-CHUNK, 2)
            tail = f.read(CHUNK)
            h.update(tail)
    return h.hexdigest()


def photo_id_for(source_id: str, source_ref: str, content_hash_hex: str) -> str:
    """Filesystem 模式用 content_hash[:16];Apple 模式直接用 uuid(由 source 决定)。"""
    if source_id.startswith("fs:"):
        return content_hash_hex[:16]
    return source_ref
