"""Reference-file persistence implementation."""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReferencePersistenceFacade:
    db_one_sync: Callable[
        [str, tuple[Any, ...]],
        sqlite3.Row | None,
    ]
    db_exec_sync: Callable[[str, tuple[Any, ...]], int]
    refs_dir: Callable[[], Path]
    now_iso: Callable[[], str]
    token_hex: Callable[[int], str] = secrets.token_hex
    file_path_fn: Callable[[str, str], Path] | None = None

    def file_path(self, token: str, ext: str) -> Path:
        return self.refs_dir() / f"{token}.{ext}"

    def existing_ref(
        self,
        auth_digest: str,
        sha: str,
    ) -> tuple[str, str] | None:
        row = self.db_one_sync(
            "SELECT token, ext FROM refs WHERE auth_hash = ? AND sha256 = ?",
            (auth_digest, sha),
        )
        if row is None:
            return None
        token = row["token"]
        ext = row["ext"]
        file_path = self.file_path_fn or self.file_path
        if file_path(token, ext).exists():
            return token, ext
        self.db_exec_sync(
            "DELETE FROM refs WHERE auth_hash = ? AND sha256 = ?",
            (auth_digest, sha),
        )
        return None

    def write_ref(
        self,
        auth_digest: str,
        sha: str,
        token: str,
        ext: str,
        raw: bytes,
    ) -> None:
        file_path = self.file_path_fn or self.file_path
        path = file_path(token, ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{self.token_hex(8)}.tmp")
        try:
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        try:
            self.db_exec_sync(
                """
                INSERT INTO refs (
                    auth_hash, sha256, token, ext, size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    auth_digest,
                    sha,
                    token,
                    ext,
                    len(raw),
                    self.now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            pass
