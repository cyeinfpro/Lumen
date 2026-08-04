"""Filesystem-backed reference artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import sqlite3
import stat
import threading
import warnings
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from PIL import Image, UnidentifiedImageError

from ..config import ImageJobSettings
from ..durable_files import atomic_write_bytes, durable_mkdir, durable_unlink
from ..persistence import REFERENCE_TIMESTAMP_NOW_SQL
from ..retention_walk import (
    DirectoryHandle,
    DirectoryPathGuard,
    directory_path_matches,
    open_directory,
)


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
_SAFE_EXTENSIONS = frozenset(_FORMAT_EXT.values())
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+\Z")


def _utc_reference_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
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
        # Pillow verification temporarily mutates process-wide decoder state.
        # The owning sidecar runtime serializes that critical section.
        self._image_verify_lock = threading.RLock()

    def _file_path(self, token: str, ext: str) -> Path:
        return self.settings.refs_dir / f"{token}.{ext}"

    def _is_valid_reference_at(
        self,
        refs: DirectoryHandle,
        token: str,
        ext: str,
        expected_size: int,
        expected_sha: str,
    ) -> bool:
        if (
            not token
            or _SAFE_TOKEN_RE.fullmatch(token) is None
            or Path(token).name != token
            or ext not in _SAFE_EXTENSIONS
            or expected_size < 0
        ):
            return False
        filename = f"{token}.{ext}"
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            return False
        flags = (
            os.O_RDONLY
            | no_follow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(filename, flags, dir_fd=refs.fd)
        except (OSError, ValueError):
            return False
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
                return False
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(fd)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns
            ):
                return False
            return secrets.compare_digest(digest.hexdigest(), expected_sha)
        except OSError:
            return False
        finally:
            os.close(fd)

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        if conn.in_transaction:
            conn.execute("ROLLBACK")

    @staticmethod
    def _reference_root_matches(
        refs: DirectoryHandle,
        guard: DirectoryPathGuard,
    ) -> bool:
        try:
            return refs.matches(os.fstat(refs.fd)) and directory_path_matches(
                guard
            )
        except OSError:
            return False

    def _existing_without_transaction(
        self,
        owner_hash: str,
        sha: str,
        refs: DirectoryHandle,
        guard: DirectoryPathGuard,
    ) -> tuple[str, str] | None:
        row = self.repository._one_sync(  # type: ignore[attr-defined]
            """
            SELECT token, ext, size, created_at
            FROM refs
            WHERE auth_hash = ? AND sha256 = ?
            """,
            (owner_hash, sha),
        )
        if row is None or not self._reference_root_matches(refs, guard):
            return None
        token = str(row["token"])
        ext = str(row["ext"])
        try:
            expected_size = int(row["size"])
        except (TypeError, ValueError):
            expected_size = -1
        if not self._is_valid_reference_at(
            refs,
            token,
            ext,
            expected_size,
            sha,
        ):
            raise ArtifactFailure(
                503,
                "reference metadata cleanup requires transactional storage",
            )
        if not self._reference_root_matches(refs, guard):
            return None
        renewed = self.repository._execute_sync(  # type: ignore[attr-defined]
            """
            UPDATE refs
            SET created_at = ?
            WHERE auth_hash = ?
              AND sha256 = ?
              AND token = ?
              AND ext = ?
              AND size = ?
              AND created_at = ?
            """,
            (
                _utc_reference_timestamp(),
                owner_hash,
                sha,
                token,
                ext,
                row["size"],
                row["created_at"],
            ),
        )
        if renewed != 1 or not self._reference_root_matches(refs, guard):
            return None
        return token, ext

    def _existing_with_absent_root(
        self,
        owner_hash: str,
        sha: str,
        open_db: object,
    ) -> tuple[str, str] | None:
        guard = DirectoryPathGuard.absent(self.settings.refs_dir)
        conn = open_db()  # type: ignore[operator]
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT token, ext, size, created_at
                FROM refs
                WHERE auth_hash = ? AND sha256 = ?
                """,
                (owner_hash, sha),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            if not directory_path_matches(guard):
                self._rollback(conn)
                return None
            deleted = conn.execute(
                """
                DELETE FROM refs
                WHERE auth_hash = ?
                  AND sha256 = ?
                  AND token = ?
                  AND ext = ?
                  AND size = ?
                  AND created_at = ?
                """,
                (
                    owner_hash,
                    sha,
                    row["token"],
                    row["ext"],
                    row["size"],
                    row["created_at"],
                ),
            ).rowcount
            if deleted != 1 or not directory_path_matches(guard):
                self._rollback(conn)
                return None
            conn.execute("COMMIT")
            return None
        except BaseException:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    def _existing_transactional(
        self,
        owner_hash: str,
        sha: str,
        refs: DirectoryHandle,
        guard: DirectoryPathGuard,
        open_db: object,
    ) -> tuple[str, str] | None:
        conn = open_db()  # type: ignore[operator]
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT token, ext, size, created_at
                FROM refs
                WHERE auth_hash = ? AND sha256 = ?
                """,
                (owner_hash, sha),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            if not self._reference_root_matches(refs, guard):
                self._rollback(conn)
                return None
            token = str(row["token"])
            ext = str(row["ext"])
            try:
                expected_size = int(row["size"])
            except (TypeError, ValueError):
                expected_size = -1
            valid = self._is_valid_reference_at(
                refs,
                token,
                ext,
                expected_size,
                sha,
            )
            if not self._reference_root_matches(refs, guard):
                self._rollback(conn)
                return None
            if valid:
                changed = conn.execute(
                    """
                    UPDATE refs
                    SET created_at = ?
                    WHERE auth_hash = ?
                      AND sha256 = ?
                      AND token = ?
                      AND ext = ?
                      AND size = ?
                      AND created_at = ?
                    """,
                    (
                        _utc_reference_timestamp(),
                        owner_hash,
                        sha,
                        token,
                        ext,
                        row["size"],
                        row["created_at"],
                    ),
                ).rowcount
            else:
                changed = conn.execute(
                    """
                    DELETE FROM refs
                    WHERE auth_hash = ?
                      AND sha256 = ?
                      AND token = ?
                      AND ext = ?
                      AND size = ?
                      AND created_at = ?
                    """,
                    (
                        owner_hash,
                        sha,
                        token,
                        ext,
                        row["size"],
                        row["created_at"],
                    ),
                ).rowcount
            if (
                changed != 1
                or not self._reference_root_matches(refs, guard)
            ):
                self._rollback(conn)
                return None
            conn.execute("COMMIT")
            return (token, ext) if valid else None
        except BaseException:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    def _unlink_target_and_sync(self, target: Path) -> None:
        durable_unlink(target, missing_ok=True)

    def _inspect(self, data: bytes) -> str:
        # H-15：Pillow 的 DecompressionBombError 既不是 OSError 也不是
        # ValueError，原来的 except 元组接不住它，一张解压炸弹参考图会以
        # 未捕获异常的形式冒到 ASGI 层变成 500（而不是干净的 413）；同时不钉住
        # Image.MAX_IMAGE_PIXELS 的话，判定用的是 Pillow 默认阈值而不是本服务
        # 配置的 max_image_pixels，配置调大调小都不生效。
        max_pixels = self.settings.max_image_pixels
        try:
            with self._image_verify_lock:
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
        open_db = getattr(self.repository, "_open", None)
        try:
            refs = open_directory(self.settings.refs_dir)
        except FileNotFoundError:
            if callable(open_db):
                return self._existing_with_absent_root(
                    owner_hash,
                    sha,
                    open_db,
                )
            return None
        except OSError:
            return None
        with refs:
            guard = DirectoryPathGuard.from_handle(refs)
            if callable(open_db):
                return self._existing_transactional(
                    owner_hash,
                    sha,
                    refs,
                    guard,
                    open_db,
                )
            return self._existing_without_transaction(
                owner_hash,
                sha,
                refs,
                guard,
            )

    def _write(
        self,
        owner_hash: str,
        sha: str,
        token: str,
        ext: str,
        data: bytes,
    ) -> bool:
        durable_mkdir(self.settings.refs_dir)
        target = self._file_path(token, ext)
        tmp = target.with_suffix(target.suffix + f".tmp-{secrets.token_hex(4)}")
        open_db = getattr(self.repository, "_open", None)
        conn: sqlite3.Connection | None = None
        published = False
        try:
            if callable(open_db):
                conn = open_db()
                conn.execute("BEGIN IMMEDIATE")
            atomic_write_bytes(target, data, temporary_path=tmp)
            published = True
            try:
                sql = f"""
                    INSERT OR IGNORE INTO refs(
                        auth_hash, sha256, token, ext, size, created_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, {REFERENCE_TIMESTAMP_NOW_SQL}
                    )
                    """
                params = (owner_hash, sha, token, ext, len(data))
                if conn is None:
                    inserted = self.repository._execute_sync(  # type: ignore[attr-defined]
                        sql,
                        params,
                    )
                else:
                    inserted = conn.execute(sql, params).rowcount
            except BaseException:
                if conn is not None and conn.in_transaction:
                    conn.execute("ROLLBACK")
                self._unlink_target_and_sync(target)
                published = False
                raise
            if inserted != 1:
                self._unlink_target_and_sync(target)
                published = False
                if conn is not None:
                    conn.execute("COMMIT")
                return False
            if conn is not None:
                conn.execute("COMMIT")
            return True
        except BaseException:
            if conn is not None and conn.in_transaction:
                conn.execute("ROLLBACK")
            if published:
                self._unlink_target_and_sync(target)
            raise
        finally:
            durable_unlink(tmp, missing_ok=True)
            if conn is not None:
                conn.close()

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
        while True:
            existing = await asyncio.to_thread(self._existing, owner_hash, sha)
            if existing is not None:
                token, existing_ext = existing
                return self._response(token, existing_ext, sha, len(data), True)

            token = secrets.token_urlsafe(24)
            inserted = await asyncio.to_thread(
                self._write,
                owner_hash,
                sha,
                token,
                ext,
                data,
            )
            if inserted:
                return self._response(token, ext, sha, len(data), False)
            winner = await asyncio.to_thread(self._existing, owner_hash, sha)
            if winner is not None:
                winner_token, winner_ext = winner
                return self._response(
                    winner_token,
                    winner_ext,
                    sha,
                    len(data),
                    True,
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
