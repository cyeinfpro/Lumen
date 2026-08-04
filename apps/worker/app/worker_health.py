"""Instance-bound worker startup and Compose health checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import sys
from typing import Any


DEFAULT_HEALTH_INTERVAL_SECONDS = 30
DEFAULT_HEALTH_KEY_PREFIX = "arq:queue:health-check"
DEFAULT_HEALTH_STATE_FILE = "/tmp/lumen-worker-health.json"
DEFAULT_PROC_ROOT = "/proc"
MAX_HEALTH_INTERVAL_SECONDS = 3600
MAX_STATE_BYTES = 4096
_INSTANCE_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkerHealthError(RuntimeError):
    """The local worker identity or Redis heartbeat is not authoritative."""


@dataclass(frozen=True, slots=True)
class WorkerHealthIdentity:
    schema: int
    instance_id: str
    health_key: str
    pid: int
    process_start_token: str
    command_sha256: str
    interval_seconds: int

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> WorkerHealthIdentity:
        try:
            identity = cls(
                schema=int(payload["schema"]),
                instance_id=str(payload["instance_id"]),
                health_key=str(payload["health_key"]),
                pid=int(payload["pid"]),
                process_start_token=str(payload["process_start_token"]),
                command_sha256=str(payload["command_sha256"]),
                interval_seconds=int(payload["interval_seconds"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerHealthError("worker health identity is malformed") from exc
        identity.validate()
        return identity

    def validate(self) -> None:
        if self.schema != 1:
            raise WorkerHealthError("unsupported worker health identity schema")
        if not _INSTANCE_RE.fullmatch(self.instance_id):
            raise WorkerHealthError("worker instance id is invalid")
        key_prefix, separator, key_instance = self.health_key.rpartition(":")
        if (
            not separator
            or not key_prefix
            or key_instance != self.instance_id
            or any(ord(char) < 33 for char in self.health_key)
            or len(self.health_key) > 256
        ):
            raise WorkerHealthError("worker health key is not instance-bound")
        if self.pid <= 0:
            raise WorkerHealthError("worker pid is invalid")
        if not self.process_start_token.isdigit():
            raise WorkerHealthError("worker process start token is invalid")
        if not _DIGEST_RE.fullmatch(self.command_sha256):
            raise WorkerHealthError("worker command digest is invalid")
        if not 1 <= self.interval_seconds <= MAX_HEALTH_INTERVAL_SECONDS:
            raise WorkerHealthError("worker health interval is invalid")


def _state_path(raw: str | None = None) -> Path:
    path = Path(
        raw or os.getenv("LUMEN_WORKER_HEALTH_STATE_FILE", DEFAULT_HEALTH_STATE_FILE)
    )
    if not path.is_absolute():
        raise WorkerHealthError("worker health state path must be absolute")
    try:
        parent_metadata = path.parent.lstat()
    except FileNotFoundError as exc:
        raise WorkerHealthError("worker health state directory is missing") from exc
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
        parent_metadata.st_mode
    ):
        raise WorkerHealthError("worker health state directory is unsafe")
    return path


def _read_proc_identity(proc_root: Path, pid: int) -> tuple[str, str]:
    process_dir = proc_root / str(pid)
    try:
        stat_payload = (process_dir / "stat").read_bytes()
        command = (process_dir / "cmdline").read_bytes()
    except OSError as exc:
        raise WorkerHealthError("worker process is not present") from exc
    separator = stat_payload.rfind(b") ")
    if separator < 0:
        raise WorkerHealthError("worker process stat is malformed")
    fields = stat_payload[separator + 2 :].split()
    if len(fields) < 20 or fields[0] == b"Z":
        raise WorkerHealthError("worker process is dead or stat is incomplete")
    start_token = fields[19].decode("ascii", errors="strict")
    if not start_token.isdigit() or not command:
        raise WorkerHealthError("worker process identity is incomplete")
    return start_token, hashlib.sha256(command).hexdigest()


def build_worker_health_identity(
    *,
    instance_id: str,
    key_prefix: str,
    interval_seconds: int,
    pid: int | None = None,
    proc_root: Path | None = None,
) -> WorkerHealthIdentity:
    normalized_prefix = key_prefix.rstrip(":")
    if (
        not normalized_prefix
        or any(ord(char) < 33 for char in normalized_prefix)
        or len(normalized_prefix) > 220
    ):
        raise WorkerHealthError("worker health key prefix is invalid")
    process_id = pid or os.getpid()
    start_token, command_sha256 = _read_proc_identity(
        proc_root or Path(DEFAULT_PROC_ROOT),
        process_id,
    )
    identity = WorkerHealthIdentity(
        schema=1,
        instance_id=instance_id,
        health_key=f"{normalized_prefix}:{instance_id}",
        pid=process_id,
        process_start_token=start_token,
        command_sha256=command_sha256,
        interval_seconds=interval_seconds,
    )
    identity.validate()
    return identity


def write_worker_health_identity(
    identity: WorkerHealthIdentity,
    *,
    state_path: Path | None = None,
) -> None:
    identity.validate()
    path = _state_path(str(state_path) if state_path is not None else None)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()
    ):
        raise WorkerHealthError("worker health state destination is unsafe")

    payload = (
        json.dumps(asdict(identity), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    temporary = path.with_name(f".{path.name}.{identity.instance_id}.tmp")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while persisting worker health identity")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def read_worker_health_identity(
    *,
    state_path: Path | None = None,
    expected_owner_uid: int | None = None,
) -> WorkerHealthIdentity:
    path = _state_path(str(state_path) if state_path is not None else None)
    owner_uid = os.geteuid() if expected_owner_uid is None else expected_owner_uid
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkerHealthError("worker health state is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size <= 0
            or metadata.st_size > MAX_STATE_BYTES
        ):
            raise WorkerHealthError("worker health state metadata is unsafe")
        chunks: list[bytes] = []
        remaining = MAX_STATE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerHealthError("worker health state is invalid JSON") from exc
    if not isinstance(document, dict):
        raise WorkerHealthError("worker health state must be an object")
    return WorkerHealthIdentity.from_mapping(document)


def remove_worker_health_identity(
    identity: WorkerHealthIdentity,
    *,
    state_path: Path | None = None,
) -> None:
    path = _state_path(str(state_path) if state_path is not None else None)
    try:
        current = read_worker_health_identity(state_path=path)
    except WorkerHealthError:
        return
    if current.instance_id != identity.instance_id:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


def check_worker_health(
    redis_client: Any,
    *,
    state_path: Path | None = None,
    proc_root: Path | None = None,
    expected_owner_uid: int | None = None,
) -> WorkerHealthIdentity:
    identity = read_worker_health_identity(
        state_path=state_path,
        expected_owner_uid=expected_owner_uid,
    )
    start_token, command_sha256 = _read_proc_identity(
        proc_root or Path(os.getenv("LUMEN_WORKER_PROC_ROOT", DEFAULT_PROC_ROOT)),
        identity.pid,
    )
    if (
        start_token != identity.process_start_token
        or command_sha256 != identity.command_sha256
    ):
        raise WorkerHealthError("worker process identity no longer matches")

    value = redis_client.get(identity.health_key)
    ttl_ms = redis_client.pttl(identity.health_key)
    max_ttl_ms = (identity.interval_seconds + 2) * 1000
    if not value:
        raise WorkerHealthError("worker heartbeat is missing")
    if (
        isinstance(ttl_ms, bool)
        or not isinstance(ttl_ms, int)
        or ttl_ms <= 0
        or ttl_ms > max_ttl_ms
    ):
        raise WorkerHealthError("worker heartbeat freshness is invalid")
    return identity


def _interval_from_env() -> int:
    raw = os.getenv(
        "LUMEN_WORKER_HEALTH_INTERVAL",
        str(DEFAULT_HEALTH_INTERVAL_SECONDS),
    )
    try:
        interval = int(raw)
    except ValueError as exc:
        raise WorkerHealthError("worker health interval must be an integer") from exc
    if not 1 <= interval <= MAX_HEALTH_INTERVAL_SECONDS:
        raise WorkerHealthError("worker health interval is out of range")
    return interval


def run_worker() -> int:
    interval = _interval_from_env()
    identity = build_worker_health_identity(
        instance_id=secrets.token_hex(16),
        key_prefix=os.getenv(
            "LUMEN_WORKER_HEALTH_KEY_PREFIX",
            DEFAULT_HEALTH_KEY_PREFIX,
        ),
        interval_seconds=interval,
    )
    state_path = _state_path()
    write_worker_health_identity(identity, state_path=state_path)
    try:
        import logging.config

        from arq.logs import default_log_config
        from arq.worker import run_worker as run_arq_worker

        from .main import WorkerSettings

        logging.config.dictConfig(default_log_config(False))
        run_arq_worker(
            WorkerSettings,
            health_check_interval=interval,
            health_check_key=identity.health_key,
        )
    finally:
        remove_worker_health_identity(identity, state_path=state_path)
    return 0


def run_health_check(*, expected_owner_uid: int | None = None) -> int:
    try:
        import redis
    except ImportError:
        return 1
    try:
        redis_client = redis.from_url(
            os.environ["REDIS_URL"],
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        check_worker_health(
            redis_client,
            expected_owner_uid=expected_owner_uid,
        )
    except (
        KeyError,
        OSError,
        redis.exceptions.RedisError,
        WorkerHealthError,
        ValueError,
    ):
        return 1
    return 0


def _parse_expected_owner_uid(raw: str) -> int:
    if os.geteuid() != 0:
        raise WorkerHealthError("--expected-owner-uid requires root")
    if not re.fullmatch(r"0|[1-9][0-9]*", raw):
        raise WorkerHealthError("expected owner uid must be a canonical decimal integer")
    owner_uid = int(raw)
    try:
        pwd.getpwuid(owner_uid)
    except KeyError as exc:
        raise WorkerHealthError("expected owner uid does not exist") from exc
    return owner_uid


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["run"]:
        return run_worker()
    if arguments == ["check"]:
        return run_health_check()
    if len(arguments) == 3 and arguments[:2] == [
        "check",
        "--expected-owner-uid",
    ]:
        try:
            expected_owner_uid = _parse_expected_owner_uid(arguments[2])
        except WorkerHealthError as exc:
            print(f"worker health error: {exc}", file=sys.stderr)
            return 2
        return run_health_check(expected_owner_uid=expected_owner_uid)
    print(
        "usage: python -m app.worker_health "
        "{run|check [--expected-owner-uid UID]}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
