"""本地文件系统对象存储适配器。V1 先用 fs；生产换 S3 时只需实现同一接口。

key 规范（对齐 DESIGN §6.6）：`u/{user_id}/g/{generation_id}/{kind}.{ext}` 等。
"""

from __future__ import annotations

import asyncio
import errno
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from lumen_core.constants import GenerationErrorCode as EC

from .config import settings

_LINK_UNSUPPORTED_ERRNOS = {
    errno.EPERM,
    errno.EACCES,
    errno.EXDEV,
    getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    errno.EOPNOTSUPP,
}
_LINK_FALLBACK_MAX_ATTEMPTS = 3


class StorageDiskFullError(OSError):
    error_code = EC.DISK_FULL.value
    status_code = None

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(errno.ENOSPC, f"storage disk full while writing key={key}")


@dataclass(frozen=True)
class StoragePutResult:
    size: int
    created: bool


class LocalStorage:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or settings.storage_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        if not key or "\x00" in key:
            raise ValueError(f"invalid storage key: {key}")
        key_path = Path(key)
        if key_path.is_absolute():
            raise ValueError(f"invalid storage key: {key}")
        path = (self.root / key_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"invalid storage key: {key}") from exc
        return path

    def put_bytes_result(self, key: str, data: bytes) -> StoragePutResult:
        p = self.path_for(key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.{secrets.token_hex(8)}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    os.link(tmp, p)
                except OSError as exc:
                    if isinstance(exc, FileExistsError):
                        if p.read_bytes() == data:
                            return StoragePutResult(size=len(data), created=False)
                        raise
                    if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
                        raise
                    created = self._put_bytes_without_link(p, data)
                    if not created:
                        return StoragePutResult(size=len(data), created=False)
            finally:
                tmp.unlink(missing_ok=True)
            return StoragePutResult(size=len(data), created=True)
        except OSError as exc:
            if isinstance(exc, StorageDiskFullError):
                raise
            if exc.errno == errno.ENOSPC:
                raise StorageDiskFullError(key) from exc
            raise

    def _put_bytes_without_link(self, path: Path, data: bytes) -> bool:
        """Fallback for filesystems without hardlinks. Returns True when created."""
        last_exists: FileExistsError | None = None
        for _attempt in range(_LINK_FALLBACK_MAX_ATTEMPTS):
            try:
                existing = path.read_bytes()
            except FileNotFoundError:
                existing = None

            if existing is not None:
                if existing == data:
                    return False
                raise FileExistsError(path)

            try:
                self._write_bytes_exclusive(path, data)
                return True
            except FileExistsError as exc:
                last_exists = exc
                try:
                    existing = path.read_bytes()
                except FileNotFoundError:
                    continue
                if existing == data:
                    return False
                raise
        if last_exists is not None:
            raise last_exists
        self._write_bytes_exclusive(path, data)
        return True

    @staticmethod
    def _write_bytes_exclusive(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def put_bytes(self, key: str, data: bytes) -> int:
        return self.put_bytes_result(key, data).size

    async def aput_bytes(self, key: str, data: bytes) -> int:
        return await asyncio.to_thread(self.put_bytes, key, data)

    def get_bytes(self, key: str) -> bytes:
        return self.path_for(key).read_bytes()

    async def aget_bytes(self, key: str) -> bytes:
        return await asyncio.to_thread(self.get_bytes, key)

    def delete(self, key: str) -> bool:
        try:
            self.path_for(key).unlink()
            return True
        except FileNotFoundError:
            return False

    def public_url(self, key: str) -> str:
        # API 的图像反代路径（DESIGN §8.3）；Agent B 在 images.py 里实现 `/images/:id/binary`。
        # 返回相对路径 —— 前端反代 /api/* → 后端 /*；避免把 host 焊死到 DB/响应中。
        return f"/api/images/_/by-key/{key}"


storage = LocalStorage()


__all__ = ["LocalStorage", "StorageDiskFullError", "StoragePutResult", "storage"]
