"""Durable filesystem primitives for video uploads."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import BinaryIO, Callable


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def mkdir_parents_durable(
    path: Path,
    *,
    fsync: Callable[[Path], None],
) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir():
                raise
        else:
            fsync(directory.parent)


def write_new_file_atomic(
    path: Path,
    source: BinaryIO,
    *,
    mkdir_parents: Callable[[Path], None],
    fsync: Callable[[Path], None],
) -> None:
    mkdir_parents(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            source.seek(0)
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fsync(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
