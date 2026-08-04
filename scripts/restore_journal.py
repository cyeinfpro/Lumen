#!/usr/bin/env python3
"""Secure persistence for interrupted Lumen backup and restore operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_TIMESTAMP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")
_ALLOWED_PHASES = frozenset(
    {
        "request_pending",
        "writers_stopping",
        "redis_stopping",
        "redis_stashing",
        "redis_stashed",
        "redis_applying",
        "redis_applied",
        "redis_started",
        "redis_rolling_back",
        "redis_rolled_back",
        "pg_promoting",
        "pg_promoted",
        "pg_rolled_back",
        "committed",
    }
)
_ALLOWED_SERVICES = frozenset({"api", "worker", "tgbot"})
_ALLOWED_SITE_SERVICES = frozenset({"web"})
_BACKUP_ALLOWED_PHASES = frozenset(
    {"writers_stopping", "writers_stopped", "writers_starting"}
)
_REDIS_DURABLE_PHASES = frozenset(
    {
        "redis_stashing",
        "redis_stashed",
        "redis_applying",
        "redis_applied",
        "redis_rolling_back",
        "redis_rolled_back",
    }
)
_REDIS_DATA_ITEMS = ("dump.rdb", "appendonly.aof", "appendonlydir")
_REDIS_STATES = frozenset(
    {
        "untouched",
        "stashing",
        "stashed",
        "applying",
        "applied",
        "rolling_back",
        "rolled_back",
        "rollback_failed",
        "committed",
    }
)
_STRING_FIELDS = (
    "operation_id",
    "timestamp",
    "phase",
    "pg_db",
    "pg_container",
    "redis_container",
    "pg_temp_db",
    "pg_rollback_db",
    "redis_host_dir",
    "redis_backup_dir",
    "redis_original_manifest",
    "redis_state",
)
_RESTORE_BINDING_STRING_FIELDS = (
    "backup_operation_id",
    "backup_pair_marker",
    "pg_backup_path",
    "redis_backup_path",
    "pg_backup_sha256",
    "redis_backup_sha256",
)
_RESTORE_BINDING_INT_FIELDS = (
    "pg_backup_size",
    "redis_backup_size",
)
_BOOL_FIELDS = (
    "services_stopped",
    "redis_needs_start",
    "pg_swap_in_progress",
    "pg_promoted",
)


class JournalMissingError(FileNotFoundError):
    """Raised when no active restore journal exists."""


def _secure_directory(path: Path) -> tuple[int, str]:
    if not path.is_absolute():
        raise ValueError("restore journal path must be absolute")
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError("restore journal directory cannot be a symlink")
    resolved_parent = parent.resolve(strict=True)
    metadata = resolved_parent.stat()
    expected_uid = os.geteuid()
    if metadata.st_uid != expected_uid:
        raise PermissionError("restore journal directory owner mismatch")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("restore journal parent is not a directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PermissionError("restore journal directory mode must be 0700")
    name = path.name
    if not name or name in {".", ".."} or "/" in name:
        raise ValueError("invalid restore journal filename")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(resolved_parent, flags), name


def _validate_file(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("restore journal is not a regular file")
    if metadata.st_uid != os.geteuid():
        raise PermissionError("restore journal owner mismatch")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PermissionError("restore journal mode must be 0600")


def _existing_metadata(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _read_document(
    path: Path,
    validator: Any = None,
) -> dict[str, Any]:
    directory_fd, name = _secure_directory(path)
    try:
        metadata = _existing_metadata(directory_fd, name)
        if metadata is None:
            raise JournalMissingError(str(path))
        _validate_file(metadata)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            opened = os.fstat(descriptor)
            _validate_file(opened)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise RuntimeError("restore journal changed while opening")
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
                document = json.load(stream)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)
    if not isinstance(document, dict):
        raise ValueError("restore journal root must be an object")
    return (validator or _validate_document)(document)


def _validate_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"restore journal field {field} must be a string")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"restore journal field {field} contains control characters")
    return value


def _empty_restore_binding() -> dict[str, Any]:
    return {
        **dict.fromkeys(_RESTORE_BINDING_STRING_FIELDS, ""),
        **dict.fromkeys(_RESTORE_BINDING_INT_FIELDS, 0),
    }


def _validate_restore_binding(
    document: dict[str, Any],
    *,
    timestamp: str,
    required: bool,
) -> dict[str, Any]:
    raw_strings = {
        field: _validate_text(document.get(field, ""), field)
        for field in _RESTORE_BINDING_STRING_FIELDS
    }
    raw_sizes = {field: document.get(field, 0) for field in _RESTORE_BINDING_INT_FIELDS}
    has_binding = any(raw_strings.values()) or any(raw_sizes.values())
    if not has_binding:
        if required:
            raise ValueError("restore journal backup binding is missing")
        return _empty_restore_binding()

    if not all(raw_strings.values()):
        raise ValueError("restore journal backup binding is incomplete")
    operation_id = raw_strings["backup_operation_id"]
    if len(operation_id) > 240:
        raise ValueError("restore journal backup operation identity is invalid")
    pg_size = _positive_size(raw_sizes["pg_backup_size"], "pg_backup_size")
    redis_size = _positive_size(
        raw_sizes["redis_backup_size"],
        "redis_backup_size",
    )
    pg_hash = _sha256_text(
        raw_strings["pg_backup_sha256"],
        "pg_backup_sha256",
    )
    redis_hash = _sha256_text(
        raw_strings["redis_backup_sha256"],
        "redis_backup_sha256",
    )
    marker = Path(raw_strings["backup_pair_marker"])
    pg_path = Path(raw_strings["pg_backup_path"])
    redis_path = Path(raw_strings["redis_backup_path"])
    if (
        not marker.is_absolute()
        or pg_path != marker.parent / "pg" / f"{timestamp}.pg.dump.gz"
        or redis_path != marker.parent / "redis" / f"{timestamp}.redis.tgz"
        or marker.name != f".backup-pair.{timestamp}.json"
    ):
        raise ValueError("restore journal backup paths do not match pair identity")
    return {
        **raw_strings,
        "pg_backup_size": pg_size,
        "redis_backup_size": redis_size,
        "pg_backup_sha256": pg_hash,
        "redis_backup_sha256": redis_hash,
    }


def _validate_document(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported restore journal schema")
    normalized: dict[str, Any] = {"schema_version": _SCHEMA_VERSION}
    for field in _STRING_FIELDS:
        normalized[field] = _validate_text(document.get(field), field)
    if normalized["phase"] not in _ALLOWED_PHASES:
        raise ValueError("unsupported restore journal phase")
    if (
        not normalized["operation_id"]
        or len(normalized["operation_id"]) > 240
        or not _TIMESTAMP_RE.fullmatch(normalized["timestamp"])
    ):
        raise ValueError("restore journal identity is incomplete")
    normalized.update(
        _validate_restore_binding(
            document,
            timestamp=normalized["timestamp"],
            required=normalized["phase"] == "request_pending",
        )
    )
    for field in _BOOL_FIELDS:
        value = document.get(field)
        if not isinstance(value, bool):
            raise ValueError(f"restore journal field {field} must be a boolean")
        normalized[field] = value
    if normalized["redis_state"] not in _REDIS_STATES:
        raise ValueError("restore journal redis state is invalid")
    if normalized["phase"] == "committed" and (
        not normalized["pg_promoted"]
        or normalized["pg_swap_in_progress"]
        or normalized["redis_state"] != "committed"
    ):
        raise ValueError(
            "committed restore journal requires promoted postgres and committed redis"
        )
    if normalized["phase"] == "pg_rolled_back" and (
        normalized["pg_promoted"]
        or normalized["pg_swap_in_progress"]
        or normalized["pg_rollback_db"]
    ):
        raise ValueError(
            "pg_rolled_back restore journal requires the pre-restore postgres state"
        )
    services = document.get("active_writer_services")
    if not isinstance(services, list) or not all(
        isinstance(service, str) and service in _ALLOWED_SERVICES
        for service in services
    ):
        raise ValueError("restore journal writer services are invalid")
    if len(set(services)) != len(services):
        raise ValueError("restore journal writer services contain duplicates")
    normalized["active_writer_services"] = services
    site_services = document.get("active_site_services", [])
    if not isinstance(site_services, list) or not all(
        isinstance(service, str) and service in _ALLOWED_SITE_SERVICES
        for service in site_services
    ):
        raise ValueError("restore journal site services are invalid")
    if len(set(site_services)) != len(site_services):
        raise ValueError("restore journal site services contain duplicates")
    normalized["active_site_services"] = site_services
    normalized["updated_at"] = _validate_text(document.get("updated_at"), "updated_at")
    return normalized


def _write_document(
    path: Path,
    document: dict[str, Any],
    validator: Any = None,
) -> None:
    normalized = (validator or _validate_document)(document)
    payload = (
        json.dumps(normalized, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    directory_fd, name = _secure_directory(path)
    temp_name = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        existing = _existing_metadata(directory_fd, name)
        if existing is not None:
            _validate_file(existing)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while persisting restore journal")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temp_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _clear_document(path: Path) -> None:
    directory_fd, name = _secure_directory(path)
    try:
        metadata = _existing_metadata(directory_fd, name)
        if metadata is None:
            return
        _validate_file(metadata)
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _shell_assignment(name: str, value: str) -> str:
    return f"{name}={shlex.quote(value)}"


def _emit_shell(document: dict[str, Any]) -> None:
    for field in _STRING_FIELDS:
        print(_shell_assignment(f"RESTORE_JOURNAL_{field.upper()}", document[field]))
    for field in _RESTORE_BINDING_STRING_FIELDS:
        print(_shell_assignment(f"RESTORE_JOURNAL_{field.upper()}", document[field]))
    for field in _RESTORE_BINDING_INT_FIELDS:
        print(
            _shell_assignment(
                f"RESTORE_JOURNAL_{field.upper()}",
                str(document[field]),
            )
        )
    for field in _BOOL_FIELDS:
        print(
            _shell_assignment(
                f"RESTORE_JOURNAL_{field.upper()}",
                "1" if document[field] else "0",
            )
        )
    print(
        _shell_assignment(
            "RESTORE_JOURNAL_ACTIVE_WRITER_SERVICES",
            " ".join(document["active_writer_services"]),
        )
    )
    print(
        _shell_assignment(
            "RESTORE_JOURNAL_ACTIVE_SITE_SERVICES",
            " ".join(document["active_site_services"]),
        )
    )


def _build_document(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "operation_id": args.operation_id,
        "timestamp": args.timestamp,
        "phase": args.phase,
        "pg_db": args.pg_db,
        "pg_container": args.pg_container,
        "redis_container": args.redis_container,
        "pg_temp_db": args.pg_temp_db,
        "pg_rollback_db": args.pg_rollback_db,
        "redis_host_dir": args.redis_host_dir,
        "redis_backup_dir": args.redis_backup_dir,
        "redis_original_manifest": args.redis_original_manifest,
        "redis_state": args.redis_state,
        "backup_operation_id": args.backup_operation_id,
        "backup_pair_marker": args.backup_pair_marker,
        "pg_backup_path": args.pg_backup_path,
        "redis_backup_path": args.redis_backup_path,
        "pg_backup_size": args.pg_backup_size,
        "redis_backup_size": args.redis_backup_size,
        "pg_backup_sha256": args.pg_backup_sha256,
        "redis_backup_sha256": args.redis_backup_sha256,
        "services_stopped": args.services_stopped == "1",
        "redis_needs_start": args.redis_needs_start == "1",
        "pg_swap_in_progress": args.pg_swap_in_progress == "1",
        "pg_promoted": args.pg_promoted == "1",
        "active_writer_services": list(dict.fromkeys(args.service)),
        "active_site_services": list(dict.fromkeys(args.site_service)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _build_request_document(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "operation_id": args.operation_id,
        "timestamp": args.timestamp,
        "phase": "request_pending",
        "pg_db": "",
        "pg_container": "",
        "redis_container": "",
        "pg_temp_db": "",
        "pg_rollback_db": "",
        "redis_host_dir": "",
        "redis_backup_dir": "",
        "redis_original_manifest": "",
        "redis_state": "untouched",
        "backup_operation_id": args.backup_operation_id,
        "backup_pair_marker": args.backup_pair_marker,
        "pg_backup_path": args.pg_backup_path,
        "redis_backup_path": args.redis_backup_path,
        "pg_backup_size": args.pg_backup_size,
        "redis_backup_size": args.redis_backup_size,
        "pg_backup_sha256": args.pg_backup_sha256,
        "redis_backup_sha256": args.redis_backup_sha256,
        "services_stopped": False,
        "redis_needs_start": False,
        "pg_swap_in_progress": False,
        "pg_promoted": False,
        "active_writer_services": [],
        "active_site_services": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _validate_backup_document(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported backup journal schema")
    operation_id = _validate_text(document.get("operation_id"), "operation_id")
    phase = _validate_text(document.get("phase"), "phase")
    if not operation_id:
        raise ValueError("backup journal operation_id is incomplete")
    if phase not in _BACKUP_ALLOWED_PHASES:
        raise ValueError("unsupported backup journal phase")
    services = document.get("active_writer_services")
    if not isinstance(services, list) or not all(
        isinstance(service, str) and service in _ALLOWED_SERVICES
        for service in services
    ):
        raise ValueError("backup journal writer services are invalid")
    if len(set(services)) != len(services):
        raise ValueError("backup journal writer services contain duplicates")
    return {
        "schema_version": _SCHEMA_VERSION,
        "operation_id": operation_id,
        "phase": phase,
        "active_writer_services": services,
        "updated_at": _validate_text(document.get("updated_at"), "updated_at"),
    }


def _build_backup_document(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "operation_id": args.operation_id,
        "phase": args.phase,
        "active_writer_services": list(dict.fromkeys(args.service)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _emit_backup_shell(document: dict[str, Any]) -> None:
    print(_shell_assignment("BACKUP_JOURNAL_OPERATION_ID", document["operation_id"]))
    print(_shell_assignment("BACKUP_JOURNAL_PHASE", document["phase"]))
    print(
        _shell_assignment(
            "BACKUP_JOURNAL_ACTIVE_WRITER_SERVICES",
            " ".join(document["active_writer_services"]),
        )
    )


def _read_small_regular_json(path: Path) -> dict[str, Any]:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"cannot stat JSON state: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > 64 * 1024
    ):
        raise ValueError(f"JSON state is not a small regular file: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError(f"JSON state changed while opening: {path}")
        raw = b""
        while True:
            chunk = os.read(descriptor, 64 * 1024 + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
            if len(raw) > 64 * 1024:
                raise ValueError(f"JSON state is too large: {path}")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON state is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON state root must be an object: {path}")
    return payload


def _fsync_opened_path(
    path: Path,
    metadata: os.stat_result,
    *,
    directory: bool,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError(f"redis durability path changed while opening: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(path: Path, metadata: os.stat_result | None = None) -> None:
    info = metadata or os.lstat(path)
    if stat.S_ISREG(info.st_mode):
        _fsync_opened_path(path, info, directory=False)
        return
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(
            f"redis durability path is not a regular file/directory: {path}"
        )
    with os.scandir(path) as entries:
        children = sorted(
            (Path(entry.path), entry.stat(follow_symlinks=False)) for entry in entries
        )
    for child, child_info in children:
        _fsync_tree(child, child_info)
    _fsync_opened_path(path, info, directory=True)


def _fsync_optional_tree(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    _fsync_tree(path, metadata)


def _fsync_redis_state(args: argparse.Namespace) -> None:
    host_dir = args.host_dir
    backup_dir = args.backup_dir
    manifest = args.manifest
    if (
        args.phase not in _REDIS_DURABLE_PHASES
        or not host_dir.is_absolute()
        or backup_dir.parent != host_dir
        or manifest != backup_dir / ".original-items"
    ):
        raise ValueError("redis durability identity is invalid")
    host_info = os.lstat(host_dir)
    backup_info = os.lstat(backup_dir)
    manifest_info = os.lstat(manifest)
    if not stat.S_ISDIR(host_info.st_mode) or not stat.S_ISDIR(backup_info.st_mode):
        raise ValueError("redis durability roots must be directories")
    if not stat.S_ISREG(manifest_info.st_mode):
        raise ValueError("redis rollback manifest must be a regular file")
    for root in (host_dir, backup_dir):
        for item in _REDIS_DATA_ITEMS:
            _fsync_optional_tree(root / item)
    _fsync_tree(manifest, manifest_info)
    _fsync_opened_path(backup_dir, backup_info, directory=True)
    _fsync_opened_path(host_dir, host_info, directory=True)


def _backup_pair_paths(
    backup_root: Path,
    timestamp: str,
) -> tuple[Path, Path, Path]:
    if not _TIMESTAMP_RE.fullmatch(timestamp):
        raise ValueError("backup pair timestamp is invalid")
    return (
        backup_root / "pg" / f"{timestamp}.pg.dump.gz",
        backup_root / "redis" / f"{timestamp}.redis.tgz",
        backup_root / f".backup-pair.{timestamp}.json",
    )


def _digest_regular_file(
    path: Path,
    expected_size: int,
) -> tuple[str, list[int]]:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"backup payload is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
        raise ValueError(f"backup payload size or type changed: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError(f"backup payload changed while opening: {path}")
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), [
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    ]


def _validate_backup_pair_marker(
    marker_path: Path,
    *,
    operation_id: str | None,
    timestamp: str,
    pg_path: Path,
    redis_path: Path,
    pg_size: int,
    redis_size: int,
    pg_hash: str,
    redis_hash: str,
) -> None:
    marker = _read_small_regular_json(marker_path)
    if (
        marker.get("schema") != 1
        or marker.get("timestamp") != timestamp
        or (operation_id is not None and marker.get("operation_id") != operation_id)
    ):
        raise ValueError("backup pair marker identity is invalid")
    expected = dict(
        pg=dict(name=pg_path.name, size=pg_size, sha256=pg_hash),
        redis=dict(name=redis_path.name, size=redis_size, sha256=redis_hash),
    )
    for key, expected_payload in expected.items():
        payload = marker.get(key)
        if not isinstance(payload, dict):
            raise ValueError(f"backup pair marker {key} payload is invalid")
        for field, value in expected_payload.items():
            if payload.get(field) != value:
                raise ValueError(
                    f"backup pair marker {key}.{field} does not match payload"
                )


def _positive_size(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed <= 0:
        raise ValueError(f"{field} is invalid")
    return parsed


def _sha256_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field} is invalid")
    return value


def _load_backup_pair_binding(
    backup_root: Path,
    timestamp: str,
) -> dict[str, Any]:
    if not backup_root.is_absolute():
        raise ValueError("backup root must be absolute")
    pg_path, redis_path, marker_path = _backup_pair_paths(
        backup_root,
        timestamp,
    )
    marker = _read_small_regular_json(marker_path)
    operation_id = _validate_text(
        marker.get("operation_id"),
        "backup_operation_id",
    )
    if not operation_id or len(operation_id) > 240:
        raise ValueError("backup pair operation identity is invalid")
    pg_payload = marker.get("pg")
    redis_payload = marker.get("redis")
    if not isinstance(pg_payload, dict) or not isinstance(redis_payload, dict):
        raise ValueError("backup pair marker payload is invalid")
    pg_size = _positive_size(pg_payload.get("size"), "pg_size")
    redis_size = _positive_size(redis_payload.get("size"), "redis_size")
    pg_hash = _sha256_text(pg_payload.get("sha256"), "pg_sha256")
    redis_hash = _sha256_text(redis_payload.get("sha256"), "redis_sha256")
    actual_pg_hash, _ = _digest_regular_file(pg_path, pg_size)
    actual_redis_hash, _ = _digest_regular_file(redis_path, redis_size)
    if actual_pg_hash != pg_hash or actual_redis_hash != redis_hash:
        raise ValueError("backup payload hash does not match pair marker")
    _validate_backup_pair_marker(
        marker_path,
        operation_id=operation_id,
        timestamp=timestamp,
        pg_path=pg_path,
        redis_path=redis_path,
        pg_size=pg_size,
        redis_size=redis_size,
        pg_hash=pg_hash,
        redis_hash=redis_hash,
    )
    return {
        "backup_operation_id": operation_id,
        "backup_pair_marker": str(marker_path),
        "pg_backup_path": str(pg_path),
        "redis_backup_path": str(redis_path),
        "pg_backup_size": pg_size,
        "redis_backup_size": redis_size,
        "pg_backup_sha256": pg_hash,
        "redis_backup_sha256": redis_hash,
    }


def _emit_backup_pair_binding(binding: dict[str, Any]) -> None:
    print(
        json.dumps(
            binding,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _emit_backup_pair_shell(binding: dict[str, Any]) -> None:
    shell_names = {
        "backup_operation_id": "RESTORE_BACKUP_OPERATION_ID",
        "backup_pair_marker": "RESTORE_BACKUP_PAIR_MARKER",
        "pg_backup_path": "RESTORE_BACKUP_PG_PATH",
        "redis_backup_path": "RESTORE_BACKUP_REDIS_PATH",
        "pg_backup_size": "RESTORE_BACKUP_PG_SIZE",
        "redis_backup_size": "RESTORE_BACKUP_REDIS_SIZE",
        "pg_backup_sha256": "RESTORE_BACKUP_PG_SHA256",
        "redis_backup_sha256": "RESTORE_BACKUP_REDIS_SHA256",
    }
    for field, shell_name in shell_names.items():
        print(
            _shell_assignment(
                shell_name,
                str(binding[field]),
            )
        )


def _verify_new_backup_pair(args: argparse.Namespace) -> None:
    try:
        started_epoch = int(args.started_epoch)
        lines = args.output_file.read_text(encoding="utf-8").splitlines()
        baseline = json.loads(args.baseline_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read update backup verification state") from exc
    if not isinstance(baseline, dict):
        raise ValueError("update backup baseline is invalid")
    payload = None
    required = {
        "operation_id",
        "pair_marker",
        "pg_sha256",
        "pg_size",
        "redis_sha256",
        "redis_size",
        "timestamp",
    }
    for line in reversed(lines):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and required.issubset(candidate):
            payload = candidate
            break
    if payload is None:
        raise ValueError("backup output has no committed pair record")
    timestamp = _validate_text(payload.get("timestamp"), "timestamp")
    operation_id = _validate_text(payload.get("operation_id"), "operation_id")
    if args.operation_id and operation_id != args.operation_id:
        raise ValueError("backup operation identity mismatch")
    pg_size = _positive_size(payload.get("pg_size"), "pg_size")
    redis_size = _positive_size(payload.get("redis_size"), "redis_size")
    pg_hash = _sha256_text(payload.get("pg_sha256"), "pg_sha256")
    redis_hash = _sha256_text(payload.get("redis_sha256"), "redis_sha256")
    pg_path, redis_path, marker_path = _backup_pair_paths(
        args.backup_root,
        timestamp,
    )
    if Path(_validate_text(payload.get("pair_marker"), "pair_marker")) != marker_path:
        raise ValueError("backup pair marker path mismatch")
    timestamp_epoch = (
        datetime.strptime(
            timestamp,
            "%Y%m%d-%H%M%S",
        )
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    if (
        timestamp_epoch < started_epoch - 1
        or timestamp_epoch > datetime.now(timezone.utc).timestamp() + 5
    ):
        raise ValueError("backup pair timestamp is outside the update window")
    actual_pg_hash, pg_signature = _digest_regular_file(pg_path, pg_size)
    actual_redis_hash, redis_signature = _digest_regular_file(redis_path, redis_size)
    if actual_pg_hash != pg_hash or actual_redis_hash != redis_hash:
        raise ValueError("backup payload hash does not match committed pair")
    if baseline.get(str(pg_path.relative_to(args.backup_root))) == pg_signature:
        raise ValueError("postgres backup was not created by this update")
    if baseline.get(str(redis_path.relative_to(args.backup_root))) == redis_signature:
        raise ValueError("redis backup was not created by this update")
    _validate_backup_pair_marker(
        marker_path,
        operation_id=operation_id,
        timestamp=timestamp,
        pg_path=pg_path,
        redis_path=redis_path,
        pg_size=pg_size,
        redis_size=redis_size,
        pg_hash=pg_hash,
        redis_hash=redis_hash,
    )
    values = (
        timestamp,
        str(pg_path),
        str(redis_path),
        str(pg_size),
        str(redis_size),
        pg_hash,
        redis_hash,
    )
    if any("\t" in value or "\n" in value for value in values):
        raise ValueError("backup pair output contains control characters")
    print("\t".join(values))


def _verify_bound_backup_pair(args: argparse.Namespace) -> None:
    pg_size = _positive_size(args.pg_size, "pg_size")
    redis_size = _positive_size(args.redis_size, "redis_size")
    pg_hash = _sha256_text(args.pg_sha256, "pg_sha256")
    redis_hash = _sha256_text(args.redis_sha256, "redis_sha256")
    pg_path, redis_path, marker_path = _backup_pair_paths(
        args.backup_root,
        args.timestamp,
    )
    if args.pg_path != pg_path or args.redis_path != redis_path:
        raise ValueError("bound backup payload path mismatch")
    if args.pair_marker is not None and args.pair_marker != marker_path:
        raise ValueError("bound backup pair marker path mismatch")
    actual_pg_hash, _ = _digest_regular_file(pg_path, pg_size)
    actual_redis_hash, _ = _digest_regular_file(redis_path, redis_size)
    if actual_pg_hash != pg_hash or actual_redis_hash != redis_hash:
        raise ValueError("bound backup payload hash mismatch")
    _validate_backup_pair_marker(
        marker_path,
        operation_id=args.operation_id,
        timestamp=args.timestamp,
        pg_path=pg_path,
        redis_path=redis_path,
        pg_size=pg_size,
        redis_size=redis_size,
        pg_hash=pg_hash,
        redis_hash=redis_hash,
    )


def _add_restore_binding_arguments(
    parser: argparse.ArgumentParser,
    *,
    required: bool,
) -> None:
    default = None if required else ""
    parser.add_argument(
        "--backup-operation-id",
        required=required,
        default=default,
    )
    parser.add_argument(
        "--backup-pair-marker",
        required=required,
        default=default,
    )
    parser.add_argument(
        "--pg-backup-path",
        required=required,
        default=default,
    )
    parser.add_argument(
        "--redis-backup-path",
        required=required,
        default=default,
    )
    parser.add_argument(
        "--pg-backup-size",
        required=required,
        default=0,
        type=int,
    )
    parser.add_argument(
        "--redis-backup-size",
        required=required,
        default=0,
        type=int,
    )
    parser.add_argument(
        "--pg-backup-sha256",
        required=required,
        default=default,
    )
    parser.add_argument(
        "--redis-backup-sha256",
        required=required,
        default=default,
    )


def _add_write_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", type=Path)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--timestamp", required=True)
    parser.add_argument("--phase", required=True, choices=sorted(_ALLOWED_PHASES))
    parser.add_argument("--pg-db", required=True)
    parser.add_argument("--pg-container", required=True)
    parser.add_argument("--redis-container", required=True)
    parser.add_argument("--pg-temp-db", default="")
    parser.add_argument("--pg-rollback-db", default="")
    parser.add_argument("--redis-host-dir", default="")
    parser.add_argument("--redis-backup-dir", default="")
    parser.add_argument("--redis-original-manifest", default="")
    parser.add_argument("--redis-state", required=True)
    _add_restore_binding_arguments(parser, required=False)
    parser.add_argument("--services-stopped", required=True, choices=("0", "1"))
    parser.add_argument("--redis-needs-start", required=True, choices=("0", "1"))
    parser.add_argument("--pg-swap-in-progress", required=True, choices=("0", "1"))
    parser.add_argument("--pg-promoted", required=True, choices=("0", "1"))
    parser.add_argument(
        "--service",
        action="append",
        choices=sorted(_ALLOWED_SERVICES),
        default=[],
    )
    parser.add_argument(
        "--site-service",
        action="append",
        choices=sorted(_ALLOWED_SITE_SERVICES),
        default=[],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    write_parser = subparsers.add_parser("write")
    _add_write_arguments(write_parser)
    request_write = subparsers.add_parser("request-write")
    request_write.add_argument("path", type=Path)
    request_write.add_argument("--operation-id", required=True)
    request_write.add_argument("--timestamp", required=True)
    _add_restore_binding_arguments(request_write, required=True)
    load_parser = subparsers.add_parser("load-shell")
    load_parser.add_argument("path", type=Path)
    clear_parser = subparsers.add_parser("clear")
    clear_parser.add_argument("path", type=Path)
    backup_write = subparsers.add_parser("backup-write")
    backup_write.add_argument("path", type=Path)
    backup_write.add_argument("--operation-id", required=True)
    backup_write.add_argument(
        "--phase",
        required=True,
        choices=sorted(_BACKUP_ALLOWED_PHASES),
    )
    backup_write.add_argument(
        "--service",
        action="append",
        choices=sorted(_ALLOWED_SERVICES),
        default=[],
    )
    backup_load = subparsers.add_parser("backup-load-shell")
    backup_load.add_argument("path", type=Path)
    backup_clear = subparsers.add_parser("backup-clear")
    backup_clear.add_argument("path", type=Path)
    pair_new = subparsers.add_parser("backup-pair-verify-new")
    pair_new.add_argument("output_file", type=Path)
    pair_new.add_argument("backup_root", type=Path)
    pair_new.add_argument("started_epoch")
    pair_new.add_argument("baseline_file", type=Path)
    pair_new.add_argument("--operation-id", default="")
    pair_bound = subparsers.add_parser("backup-pair-verify-bound")
    pair_bound.add_argument("backup_root", type=Path)
    pair_bound.add_argument("timestamp")
    pair_bound.add_argument("pg_path", type=Path)
    pair_bound.add_argument("redis_path", type=Path)
    pair_bound.add_argument("pg_size")
    pair_bound.add_argument("redis_size")
    pair_bound.add_argument("pg_sha256")
    pair_bound.add_argument("redis_sha256")
    pair_bound.add_argument("--operation-id", default=None)
    pair_bound.add_argument("--pair-marker", type=Path, default=None)
    pair_bind = subparsers.add_parser("backup-pair-bind-json")
    pair_bind.add_argument("backup_root", type=Path)
    pair_bind.add_argument("timestamp")
    pair_bind_shell = subparsers.add_parser("backup-pair-bind-shell")
    pair_bind_shell.add_argument("backup_root", type=Path)
    pair_bind_shell.add_argument("timestamp")
    redis_fsync = subparsers.add_parser("redis-state-fsync")
    redis_fsync.add_argument(
        "--phase", required=True, choices=sorted(_REDIS_DURABLE_PHASES)
    )
    redis_fsync.add_argument("--host-dir", required=True, type=Path)
    redis_fsync.add_argument("--backup-dir", required=True, type=Path)
    redis_fsync.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    try:
        if args.command == "write":
            _write_document(args.path, _build_document(args))
        elif args.command == "request-write":
            _write_document(args.path, _build_request_document(args))
        elif args.command == "load-shell":
            _emit_shell(_read_document(args.path))
        elif args.command == "backup-write":
            _write_document(
                args.path,
                _build_backup_document(args),
                _validate_backup_document,
            )
        elif args.command == "backup-load-shell":
            _emit_backup_shell(_read_document(args.path, _validate_backup_document))
        elif args.command == "backup-pair-verify-new":
            _verify_new_backup_pair(args)
        elif args.command == "backup-pair-verify-bound":
            _verify_bound_backup_pair(args)
        elif args.command == "backup-pair-bind-json":
            _emit_backup_pair_binding(
                _load_backup_pair_binding(args.backup_root, args.timestamp)
            )
        elif args.command == "backup-pair-bind-shell":
            _emit_backup_pair_shell(
                _load_backup_pair_binding(args.backup_root, args.timestamp)
            )
        elif args.command == "redis-state-fsync":
            _fsync_redis_state(args)
        else:
            _clear_document(args.path)
    except JournalMissingError:
        return 3
    except (OSError, ValueError, PermissionError, json.JSONDecodeError) as exc:
        print(f"restore journal error: {exc}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
