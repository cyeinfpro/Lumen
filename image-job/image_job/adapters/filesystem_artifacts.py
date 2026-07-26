"""Filesystem-backed reference artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import threading
import warnings
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from PIL import Image, UnidentifiedImageError

from ..config import ImageJobSettings


# `Image.MAX_IMAGE_PIXELS` 与 `warnings.catch_warnings()` 都是进程级全局状态，
# 而 _inspect 跑在 asyncio.to_thread 的线程池里，可能并发进入。用锁把
# 「改全局 → 解码 → 还原」串起来，语义与 image_artifacts.image_metadata 一致。
_IMAGE_VERIFY_LOCK = threading.RLock()


_MIME_EXT: Mapping[str, str] = MappingProxyType(
    {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/webp": "webp",
    }
)
_FORMAT_EXT: Mapping[str, str] = MappingProxyType(
    {"png": "png", "jpeg": "jpg", "webp": "webp"}
)


class ArtifactFailure(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class FilesystemArtifactStore:
    def __init__(
        self,
        settings: ImageJobSettings,
        repository: object,
    ) -> None:
        self.settings = settings
        self.repository = repository

    def _file_path(self, token: str, ext: str) -> Path:
        return self.settings.refs_dir / f"{token}.{ext}"

    def _inspect(self, data: bytes) -> str:
        # H-15：Pillow 的 DecompressionBombError 既不是 OSError 也不是
        # ValueError，原来的 except 元组接不住它，一张解压炸弹参考图会以
        # 未捕获异常的形式冒到 ASGI 层变成 500（而不是干净的 413）；同时不钉住
        # Image.MAX_IMAGE_PIXELS 的话，判定用的是 Pillow 默认阈值而不是本服务
        # 配置的 max_image_pixels，配置调大调小都不生效。
        max_pixels = self.settings.max_image_pixels
        try:
            with _IMAGE_VERIFY_LOCK:
                previous_max_pixels = Image.MAX_IMAGE_PIXELS
                Image.MAX_IMAGE_PIXELS = max_pixels
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter(
                            "error",
                            Image.DecompressionBombWarning,
                        )
                        with Image.open(BytesIO(data)) as image:
                            width, height = image.size
                            if width <= 0 or height <= 0 or width * height > max_pixels:
                                raise ArtifactFailure(
                                    413,
                                    "reference image exceeds pixel limit",
                                )
                            fmt = (image.format or "").lower()
                            image.verify()
                finally:
                    Image.MAX_IMAGE_PIXELS = previous_max_pixels
        except ArtifactFailure:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise ArtifactFailure(
                413,
                "reference image exceeds pixel limit",
            ) from exc
        except (
            EOFError,
            OSError,
            SyntaxError,
            UnidentifiedImageError,
            ValueError,
        ) as exc:
            raise ArtifactFailure(400, "reference is not a valid image") from exc
        ext = _FORMAT_EXT.get(fmt)
        if ext is None:
            raise ArtifactFailure(
                400,
                "reference is not a supported image format",
            )
        return ext

    def _existing(self, owner_hash: str, sha: str) -> tuple[str, str] | None:
        row = self.repository._one_sync(  # type: ignore[attr-defined]
            "SELECT token, ext FROM refs WHERE auth_hash = ? AND sha256 = ?",
            (owner_hash, sha),
        )
        if row is None:
            return None
        return str(row["token"]), str(row["ext"])

    def _write(
        self,
        owner_hash: str,
        sha: str,
        token: str,
        ext: str,
        data: bytes,
    ) -> None:
        self.settings.refs_dir.mkdir(parents=True, exist_ok=True)
        target = self._file_path(token, ext)
        tmp = target.with_suffix(target.suffix + f".tmp-{secrets.token_hex(4)}")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
            self.repository._execute_sync(  # type: ignore[attr-defined]
                """
                INSERT OR IGNORE INTO refs(
                    auth_hash, sha256, token, ext, size, created_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (owner_hash, sha, token, ext, len(data)),
            )
        finally:
            tmp.unlink(missing_ok=True)

    async def put_reference(
        self,
        *,
        owner_hash: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, object]:
        if content_type not in _MIME_EXT:
            raise ArtifactFailure(
                400,
                f"unsupported content-type {content_type!r}; "
                "expected image/png|jpeg|webp",
            )
        ext = await asyncio.to_thread(self._inspect, data)
        sha = hashlib.sha256(data).hexdigest()
        existing = await asyncio.to_thread(self._existing, owner_hash, sha)
        if existing is not None:
            token, existing_ext = existing
            return self._response(token, existing_ext, sha, len(data), True)

        token = secrets.token_urlsafe(24)
        await asyncio.to_thread(
            self._write,
            owner_hash,
            sha,
            token,
            ext,
            data,
        )
        final = await asyncio.to_thread(self._existing, owner_hash, sha)
        final_token, final_ext = final or (token, ext)
        return self._response(
            final_token,
            final_ext,
            sha,
            len(data),
            final_token != token,
        )

    def _response(
        self,
        token: str,
        ext: str,
        sha: str,
        size: int,
        deduped: bool,
    ) -> dict[str, object]:
        return {
            "url": f"{self.settings.public_base_url}/refs/{token}.{ext}",
            "sha256": sha,
            "size": size,
            "deduped": deduped,
        }

    async def readiness_probe(self) -> bool:
        def probe() -> bool:
            try:
                self.settings.data_dir.mkdir(parents=True, exist_ok=True)
                self.settings.refs_dir.mkdir(parents=True, exist_ok=True)
                probe_path = self.settings.refs_dir / ".readiness"
                probe_path.write_bytes(b"ok")
                probe_path.unlink()
                return True
            except OSError:
                return False

        return await asyncio.to_thread(probe)
