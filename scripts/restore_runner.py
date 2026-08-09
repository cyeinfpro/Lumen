#!/usr/bin/env python3
"""Validate a restore trigger and invoke the fixed host restore script."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Union

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from maintenance_marker_lock import atomic_replace_bytes, marker_lock

_DEFAULT_TRIGGER = Path("/opt/lumendata/backup/.restore.trigger")
_DEFAULT_JOURNAL = Path("/var/lib/lumen/restore/active.json")
_DEFAULT_BACKUP_ROOT = Path("/opt/lumendata/backup")
_DEFAULT_RUNNING = Path("/opt/lumendata/backup/.restore.running")
_DEFAULT_ADOPTION_RECEIPT = Path(
    "/opt/lumendata/backup/.restore.adoption.json"
)
_TIMESTAMP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,240}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TRIGGER_AGE = timedelta(minutes=5)
_MAX_TRIGGER_BYTES = 4096
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
_RESTORE_PHASES = {
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
_TERMINAL_RESTORE_PHASES = {"committed"}


class RestoreTriggerError(ValueError):
    pass


def _read_trigger(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RestoreTriggerError("cannot open restore trigger") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size > _MAX_TRIGGER_BYTES
        ):
            raise RestoreTriggerError("restore trigger is not a small regular file")
        raw = os.read(fd, _MAX_TRIGGER_BYTES + 1)
        if len(raw) != info.st_size:
            raise RestoreTriggerError("restore trigger changed while being read")
    finally:
        os.close(fd)
    return raw, info


def restore_request_sha256(operation_id: str, timestamp: str) -> str:
    encoded = (
        json.dumps(
            {
                "operation_id": operation_id,
                "schema": 2,
                "timestamp": timestamp,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_restore_request(
    path: Path,
    *,
    allow_stale: bool = False,
) -> dict[str, object]:
    raw, info = _read_trigger(path)
    operation_id: str | None = None
    issued_at: str | None = None
    try:
        decoded = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RestoreTriggerError("restore trigger is not ASCII") from exc
    if decoded.lstrip().startswith("{"):
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise RestoreTriggerError("restore trigger JSON is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "issued_at",
            "operation_id",
            "request_sha256",
            "schema",
            "timestamp",
        }:
            raise RestoreTriggerError("restore trigger fields do not match schema")
        operation_id = payload.get("operation_id")
        timestamp = payload.get("timestamp")
        issued_at = payload.get("issued_at")
        if (
            payload.get("schema") != 2
            or not isinstance(operation_id, str)
            or not _OPERATION_ID_RE.fullmatch(operation_id)
            or not isinstance(timestamp, str)
            or not _TIMESTAMP_RE.fullmatch(timestamp)
            or not isinstance(issued_at, str)
            or payload.get("request_sha256")
            != restore_request_sha256(operation_id, timestamp)
        ):
            raise RestoreTriggerError("restore trigger identity is invalid")
    else:
        timestamp = decoded.strip()
        if not _TIMESTAMP_RE.fullmatch(timestamp):
            raise RestoreTriggerError("restore timestamp is invalid")
    modified = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
    if issued_at is not None:
        try:
            modified = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RestoreTriggerError("restore trigger issued_at is invalid") from exc
        if modified.tzinfo is None:
            raise RestoreTriggerError("restore trigger issued_at must include timezone")
        modified = modified.astimezone(timezone.utc)
    age = datetime.now(timezone.utc) - modified
    if not allow_stale and (age < -timedelta(minutes=1) or age > _MAX_TRIGGER_AGE):
        raise RestoreTriggerError("restore trigger is stale")
    return {
        "operation_id": operation_id,
        "timestamp": timestamp,
    }


def load_timestamp(path: Path, *, allow_stale: bool = False) -> str:
    return str(load_restore_request(path, allow_stale=allow_stale)["timestamp"])


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
        or phase not in _RESTORE_PHASES
    ):
        raise RestoreTriggerError("restore recovery journal identity is invalid")
    return payload


def persist_pending_request(
    path: Path,
    operation_id: str,
    timestamp: str,
    binding: Mapping[str, object],
) -> dict[str, object]:
    helper = trusted_journal_helper()
    helper_info = helper.lstat()
    if not stat.S_ISREG(helper_info.st_mode):
        raise RestoreTriggerError("restore journal helper is not a regular file")
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


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _load_adoption_receipt(path: Path) -> dict[str, object] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RestoreTriggerError("cannot read restore adoption receipt") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size > _MAX_JOURNAL_BYTES
        ):
            raise RestoreTriggerError("restore adoption receipt is invalid")
        raw = os.read(descriptor, _MAX_JOURNAL_BYTES + 1)
        if len(raw) != info.st_size:
            raise RestoreTriggerError("restore adoption receipt changed while reading")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreTriggerError("restore adoption receipt is invalid") from exc
    generation = payload.get("generation") if isinstance(payload, dict) else None
    pid = payload.get("pid") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or not isinstance(payload.get("operation_id"), str)
        or not isinstance(payload.get("request_sha256"), str)
        or not _SHA256_RE.fullmatch(str(payload.get("request_sha256")))
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or payload.get("owner") != "host"
        or payload.get("status") not in {"prepared", "accepted"}
    ):
        raise RestoreTriggerError("restore adoption receipt is invalid")
    return payload


def _write_adoption_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_info = path.parent.lstat()
    if not stat.S_ISDIR(parent_info.st_mode):
        raise RestoreTriggerError("restore adoption receipt directory is unsafe")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise RestoreTriggerError("restore adoption receipt destination is unsafe")
    encoded = (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    atomic_replace_bytes(path, encoded, mode=0o660)


def verify_adoption_receipt(
    path: Path,
    *,
    operation_id: str,
    timestamp: str,
) -> dict[str, object]:
    receipt = _load_adoption_receipt(path)
    if (
        receipt is None
        or receipt.get("operation_id") != operation_id
        or receipt.get("request_sha256")
        != restore_request_sha256(operation_id, timestamp)
        or receipt.get("status") != "accepted"
    ):
        raise RestoreTriggerError("restore adoption receipt identity mismatch")
    return receipt


def _adopt_running_marker_unlocked(
    path: Path,
    receipt_path: Path,
    request: Mapping[str, object],
) -> dict[str, object] | None:
    """Fence or recover the API claim before slow restore work."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RestoreTriggerError("cannot read restore ownership marker") from exc
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value.strip()
    operation_id = values.get("operation_id")
    requested_operation_id = request.get("operation_id")
    timestamp = request.get("timestamp")
    try:
        generation = int(values.get("generation", "0"))
    except ValueError as exc:
        raise RestoreTriggerError("restore ownership generation is invalid") from exc
    if (
        not operation_id
        or not _OPERATION_ID_RE.fullmatch(operation_id)
        or not isinstance(timestamp, str)
        or (
            requested_operation_id is not None
            and requested_operation_id != operation_id
        )
    ):
        return None
    request_digest = restore_request_sha256(operation_id, timestamp)
    owner = values.get("owner")
    if owner == "api" and generation == 0:
        next_generation = 1
    elif owner == "host" and generation >= 1:
        try:
            previous_pid = int(values.get("pid", "0"))
        except ValueError as exc:
            raise RestoreTriggerError("restore ownership pid is invalid") from exc
        if _pid_is_running(previous_pid):
            return None
        previous_receipt = _load_adoption_receipt(receipt_path)
        if previous_receipt is not None and (
            previous_receipt.get("operation_id") != operation_id
            or previous_receipt.get("request_sha256") != request_digest
            or previous_receipt.get("generation") != generation
        ):
            raise RestoreTriggerError("restore ownership receipt conflicts with marker")
        next_generation = generation + 1
    else:
        return None
    accepted_at = datetime.now(timezone.utc).isoformat()
    receipt: dict[str, object] = {
        "schema": 1,
        "operation_id": operation_id,
        "owner": "host",
        "generation": next_generation,
        "request_sha256": request_digest,
        "pid": os.getpid(),
        "accepted_at": accepted_at,
        "status": "prepared",
    }
    _write_adoption_receipt(receipt_path, receipt)
    values["owner"] = "host"
    values["generation"] = str(next_generation)
    values["request_sha256"] = request_digest
    values["pid"] = str(os.getpid())
    values["adopted_at"] = accepted_at
    payload = "".join(f"{key}={value}\n" for key, value in values.items()).encode()
    atomic_replace_bytes(path, payload, mode=0o660)
    receipt["status"] = "accepted"
    _write_adoption_receipt(receipt_path, receipt)
    return receipt


def adopt_running_marker(
    path: Path,
    receipt_path: Path,
    request: Mapping[str, object],
) -> dict[str, object] | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    with marker_lock(path.parent):
        return _adopt_running_marker_unlocked(path, receipt_path, request)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1:
        print("restore runner rejected trigger: unexpected arguments", file=sys.stderr)
        return 2
    trigger = Path(args[0]) if args else _DEFAULT_TRIGGER
    journal = Path(os.environ.get("LUMEN_RESTORE_JOURNAL_FILE", _DEFAULT_JOURNAL))
    running = Path(os.environ.get("LUMEN_RESTORE_RUNNING", _DEFAULT_RUNNING))
    receipt_path = Path(
        os.environ.get(
            "LUMEN_RESTORE_ADOPTION_RECEIPT",
            _DEFAULT_ADOPTION_RECEIPT,
        )
    )
    script = trusted_restore_script()
    helper = trusted_journal_helper()
    timestamp: Optional[str] = None
    backup_root: Optional[Path] = None
    operation_id: Optional[str] = None
    request_digest: Optional[str] = None
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
                trigger_request = load_restore_request(trigger)
            except FileNotFoundError:
                print("restore runner found no pending trigger or journal")
                return 0
            adoption = adopt_running_marker(running, receipt_path, trigger_request)
            if adoption is None:
                raise RestoreTriggerError(
                    "new restore requires API-to-host ownership handoff"
                )
            operation_id = str(adoption["operation_id"])
            timestamp = str(trigger_request["timestamp"])
            request_digest = restore_request_sha256(operation_id, timestamp)
            backup_root = restore_backup_root(os.environ)
            binding = load_backup_pair_binding(helper, backup_root, timestamp)
            recovery = persist_pending_request(
                journal,
                operation_id,
                timestamp,
                binding,
            )
        else:
            phase = str(recovery["phase"])
            journal_timestamp = str(recovery["timestamp"])
            operation_id = str(recovery["operation_id"])
            try:
                trigger_request = load_restore_request(trigger, allow_stale=True)
            except FileNotFoundError:
                trigger_request = None
            if trigger_request is not None:
                trigger_timestamp = str(trigger_request["timestamp"])
                trigger_operation_id = trigger_request.get("operation_id")
                if (
                    trigger_timestamp != journal_timestamp
                    or (
                        trigger_operation_id is not None
                        and trigger_operation_id != operation_id
                    )
                ):
                    raise RestoreTriggerError(
                        "restore journal belongs to a different request"
                    )
            if phase in _TERMINAL_RESTORE_PHASES:
                print("restore runner found a terminal journal; no restore pending")
                return 0
            request_digest = restore_request_sha256(
                operation_id,
                journal_timestamp,
            )
            verify_adoption_receipt(
                receipt_path,
                operation_id=operation_id,
                timestamp=journal_timestamp,
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
    if operation_id is not None:
        child_env["RESTORE_REQUEST_OPERATION_ID"] = operation_id
    if request_digest is not None:
        child_env["LUMEN_RESTORE_REQUEST_SHA256"] = request_digest
    os.execve(
        "/bin/bash",
        ["/bin/bash", str(script), restore_arg],
        child_env,
    )
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
