"""Durable filesystem protocol for host storage apply operations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_IDENTITY_FILE_RE = re.compile(
    r"^(?P<operation_id>[0-9a-f]{32})\.(?P<fence>[1-9][0-9]*)\.json$"
)


def read_json(path: Path) -> dict | None:
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_atomic(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _write_immutable(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        try:
            os.link(tmp, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != content:
                raise RuntimeError(f"immutable storage request conflicts: {path.name}")
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def stage_lock(state_dir: Path, name: str) -> Iterator[None]:
    lock_path = state_dir / f".{name}.stage.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def storage_apply_request_path(
    requests_dir: Path,
    operation_id: str,
    fence: int,
) -> Path:
    return requests_dir / f"{operation_id}.{fence}.json"


def storage_apply_result_path(
    results_dir: Path,
    operation_id: str,
    fence: int,
) -> Path:
    return results_dir / f"{operation_id}.{fence}.json"


def stage_storage_apply(
    *,
    state_dir: Path,
    requests_dir: Path,
    operation_id: str,
    fence: int,
    desired_config_sha256: str,
    conf_text: str,
) -> None:
    if not _OPERATION_ID_RE.fullmatch(operation_id):
        raise ValueError("invalid storage apply operation_id")
    if isinstance(fence, bool) or not isinstance(fence, int) or fence <= 0:
        raise ValueError("invalid storage apply fence")
    actual_digest = hashlib.sha256(conf_text.encode("utf-8")).hexdigest()
    if desired_config_sha256 != actual_digest:
        raise ValueError("storage apply config digest mismatch")
    request = {
        "schema": 1,
        "operation_id": operation_id,
        "fence": fence,
        "config_sha256": desired_config_sha256,
        "config": conf_text,
    }
    content = json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
    with stage_lock(state_dir, "apply"):
        _write_immutable(
            storage_apply_request_path(requests_dir, operation_id, fence),
            content,
            mode=0o660,
        )


def read_host_fence_floor(
    *,
    claim_path: Path,
    results_dir: Path,
    requests_dir: Path | None = None,
    latest_result_path: Path | None = None,
) -> int:
    fences: list[int] = []
    if claim_path.exists():
        claim = read_json(claim_path)
        if claim is None:
            raise RuntimeError("storage host claim is unreadable")
        fences.append(_validated_identity_fence(claim, source=claim_path.name))
    for directory in (results_dir, requests_dir):
        if directory is None or not directory.exists():
            continue
        try:
            entries = tuple(directory.iterdir())
        except OSError as exc:
            raise RuntimeError(
                f"cannot scan storage host fence directory: {directory}"
            ) from exc
        for path in entries:
            match = _IDENTITY_FILE_RE.fullmatch(path.name)
            if match is not None:
                fences.append(int(match.group("fence")))
    if latest_result_path is not None and latest_result_path.exists():
        latest = read_json(latest_result_path)
        if latest is not None and "fence" in latest:
            fences.append(
                _validated_identity_fence(
                    latest,
                    source=latest_result_path.name,
                )
            )
    return max(fences, default=0)


def _validated_identity_fence(payload: dict, *, source: str) -> int:
    operation_id = str(
        payload.get("operation_id") or payload.get("call_id") or ""
    ).strip()
    fence = payload.get("fence")
    if (
        not _OPERATION_ID_RE.fullmatch(operation_id)
        or isinstance(fence, bool)
        or not isinstance(fence, int)
        or fence <= 0
    ):
        raise RuntimeError(f"storage host fence identity is invalid: {source}")
    return fence


__all__ = [
    "read_host_fence_floor",
    "read_json",
    "stage_lock",
    "stage_storage_apply",
    "storage_apply_request_path",
    "storage_apply_result_path",
    "write_atomic",
]
