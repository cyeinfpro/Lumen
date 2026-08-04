#!/usr/bin/env python3
"""Validate a restore trigger and invoke the fixed host restore script."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Union


_DEFAULT_TRIGGER = Path("/opt/lumendata/backup/.restore.trigger")
_DEFAULT_JOURNAL = Path("/var/lib/lumen/restore/active.json")
_DEFAULT_BACKUP_ROOT = Path("/opt/lumendata/backup")
_TIMESTAMP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")
_MAX_TRIGGER_AGE = timedelta(minutes=5)
_MAX_JOURNAL_BYTES = 64 * 1024
_TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_UNSAFE_ENV_KEYS = {
    "BASH_ENV",
    "BASHOPTS",
    "CDPATH",
    "ENV",
    "GLOBIGNORE",
    "LUMEN_RESTORE_SCRIPT",
    "LUMEN_RESTORE_TRIGGER",
    "PYTHONHOME",
    "PYTHONPATH",
    "SHELLOPTS",
}
_BACKUP_BINDING_STRING_FIELDS = (
    "backup_operation_id",
    "backup_pair_marker",
    "pg_backup_path",
    "redis_backup_path",
    "pg_backup_sha256",
    "redis_backup_sha256",
)
_BACKUP_BINDING_SIZE_FIELDS = (
    "pg_backup_size",
    "redis_backup_size",
)


class RestoreTriggerError(ValueError):
    pass


def load_timestamp(path: Path, *, allow_stale: bool = False) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RestoreTriggerError("cannot open restore trigger") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > 64:
            raise RestoreTriggerError("restore trigger is not a small regular file")
        raw = os.read(fd, 65)
        if len(raw) != info.st_size:
            raise RestoreTriggerError("restore trigger changed while being read")
    finally:
        os.close(fd)
    try:
        timestamp = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RestoreTriggerError("restore timestamp is not ASCII") from exc
    if not _TIMESTAMP_RE.fullmatch(timestamp):
        raise RestoreTriggerError("restore timestamp is invalid")
    modified = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
    age = datetime.now(timezone.utc) - modified
    if not allow_stale and (age < -timedelta(minutes=1) or age > _MAX_TRIGGER_AGE):
        raise RestoreTriggerError("restore trigger is stale")
    return timestamp


def trusted_restore_script(
    runner_file: Optional[Union[str, Path]] = None,
) -> Path:
    runner = Path(runner_file or __file__).resolve(strict=True)
    return runner.with_name("restore.sh")


def trusted_journal_helper(
    runner_file: Optional[Union[str, Path]] = None,
) -> Path:
    runner = Path(runner_file or __file__).resolve(strict=True)
    return runner.with_name("restore_journal.py")


def restore_backup_root(source: Mapping[str, str]) -> Path:
    raw = source.get("BACKUP_ROOT") or source.get("LUMEN_BACKUP_ROOT")
    root = Path(raw) if raw else _DEFAULT_BACKUP_ROOT
    if not root.is_absolute():
        raise RestoreTriggerError("backup root must be absolute")
    return root


def _normalize_backup_binding(payload: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for field in _BACKUP_BINDING_STRING_FIELDS:
        value = payload.get(field)
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or "\n" in value
            or "\r" in value
        ):
            raise RestoreTriggerError("restore backup binding is invalid")
        normalized[field] = value
    for field in _BACKUP_BINDING_SIZE_FIELDS:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RestoreTriggerError("restore backup binding is invalid")
        normalized[field] = value
    return normalized


def load_backup_pair_binding(
    helper: Path,
    backup_root: Path,
    timestamp: str,
) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(helper),
            "backup-pair-bind-json",
            str(backup_root),
            timestamp,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": _TRUSTED_PATH,
        },
        check=False,
    )
    if result.returncode != 0:
        raise RestoreTriggerError("backup pair marker or payload is invalid")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RestoreTriggerError("backup pair binding output is invalid") from exc
    if not isinstance(payload, dict):
        raise RestoreTriggerError("backup pair binding output is invalid")
    return _normalize_backup_binding(payload)


def load_journal(path: Path) -> dict[str, object] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RestoreTriggerError("cannot open restore recovery journal") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size > _MAX_JOURNAL_BYTES
        ):
            raise RestoreTriggerError("restore recovery journal is invalid")
        raw = os.read(fd, _MAX_JOURNAL_BYTES + 1)
        if len(raw) != info.st_size:
            raise RestoreTriggerError("restore recovery journal changed while reading")
    finally:
        os.close(fd)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreTriggerError("restore recovery journal is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RestoreTriggerError("restore recovery journal schema is invalid")
    operation_id = payload.get("operation_id")
    timestamp = payload.get("timestamp")
    phase = payload.get("phase")
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or not isinstance(timestamp, str)
        or not _TIMESTAMP_RE.fullmatch(timestamp)
        or not isinstance(phase, str)
        or not phase
    ):
        raise RestoreTriggerError("restore recovery journal identity is invalid")
    return payload


def persist_pending_request(
    path: Path,
    timestamp: str,
    binding: Mapping[str, object],
) -> dict[str, object]:
    helper = trusted_journal_helper()
    helper_info = helper.lstat()
    if not stat.S_ISREG(helper_info.st_mode):
        raise RestoreTriggerError("restore journal helper is not a regular file")
    operation_id = f"restore-{timestamp}-{os.getpid()}-{secrets.token_hex(6)}"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(helper),
            "request-write",
            str(path),
            "--operation-id",
            operation_id,
            "--timestamp",
            timestamp,
            "--backup-operation-id",
            str(binding["backup_operation_id"]),
            "--backup-pair-marker",
            str(binding["backup_pair_marker"]),
            "--pg-backup-path",
            str(binding["pg_backup_path"]),
            "--redis-backup-path",
            str(binding["redis_backup_path"]),
            "--pg-backup-size",
            str(binding["pg_backup_size"]),
            "--redis-backup-size",
            str(binding["redis_backup_size"]),
            "--pg-backup-sha256",
            str(binding["pg_backup_sha256"]),
            "--redis-backup-sha256",
            str(binding["redis_backup_sha256"]),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": _TRUSTED_PATH,
        },
        check=False,
    )
    if result.returncode != 0:
        raise RestoreTriggerError("cannot persist durable restore request")
    journal = load_journal(path)
    if (
        journal is None
        or journal.get("operation_id") != operation_id
        or journal.get("timestamp") != timestamp
        or journal.get("phase") != "request_pending"
        or _normalize_backup_binding(journal) != dict(binding)
    ):
        raise RestoreTriggerError("durable restore request verification failed")
    return journal


def sanitized_restore_environment(source: Mapping[str, str]) -> Dict[str, str]:
    env = {key: value for key, value in source.items() if key not in _UNSAFE_ENV_KEYS}
    env["PATH"] = _TRUSTED_PATH
    return env


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1:
        print("restore runner rejected trigger: unexpected arguments", file=sys.stderr)
        return 2
    trigger = Path(args[0]) if args else _DEFAULT_TRIGGER
    journal = Path(os.environ.get("LUMEN_RESTORE_JOURNAL_FILE", _DEFAULT_JOURNAL))
    script = trusted_restore_script()
    helper = trusted_journal_helper()
    timestamp: Optional[str] = None
    backup_root: Optional[Path] = None
    try:
        script_info = script.lstat()
        if not stat.S_ISREG(script_info.st_mode):
            raise RestoreTriggerError("restore script is not a regular file")
        helper_info = helper.lstat()
        if not stat.S_ISREG(helper_info.st_mode):
            raise RestoreTriggerError("restore journal helper is not a regular file")
        recovery = load_journal(journal)
        if recovery is None:
            try:
                timestamp = load_timestamp(trigger)
            except FileNotFoundError:
                print("restore runner found no pending trigger or journal")
                return 0
            backup_root = restore_backup_root(os.environ)
            binding = load_backup_pair_binding(helper, backup_root, timestamp)
            recovery = persist_pending_request(journal, timestamp, binding)
        else:
            phase = recovery["phase"]
            journal_timestamp = str(recovery["timestamp"])
            try:
                trigger_timestamp = load_timestamp(trigger, allow_stale=True)
            except FileNotFoundError:
                trigger_timestamp = None
            if trigger_timestamp is not None and trigger_timestamp != journal_timestamp:
                raise RestoreTriggerError(
                    "restore journal belongs to a different request"
                )
            if phase == "request_pending":
                timestamp = journal_timestamp
                backup_root = restore_backup_root(os.environ)
                expected_binding = _normalize_backup_binding(recovery)
                actual_binding = load_backup_pair_binding(
                    helper,
                    backup_root,
                    timestamp,
                )
                if actual_binding != expected_binding:
                    raise RestoreTriggerError(
                        "restore backup pair no longer matches durable request"
                    )
            elif trigger_timestamp is not None:
                timestamp = trigger_timestamp
    except (OSError, RestoreTriggerError) as exc:
        print(f"restore runner rejected trigger: {exc}", file=sys.stderr)
        return 2
    if timestamp is None:
        print("restore runner accepted persistent recovery journal", flush=True)
        restore_arg = "--recover-only"
    else:
        print(f"restore runner accepted timestamp={timestamp}", flush=True)
        restore_arg = timestamp
    child_env = sanitized_restore_environment(os.environ)
    if backup_root is not None:
        child_env["BACKUP_ROOT"] = str(backup_root)
    os.execve(
        "/bin/bash",
        ["/bin/bash", str(script), restore_arg],
        child_env,
    )
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
