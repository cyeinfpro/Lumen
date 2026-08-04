"""Instance-bound Telegram bot runtime health state and Compose checks."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Any


DEFAULT_HEALTH_STATE_FILE = "/tmp/lumen-tgbot-health.json"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5
DEFAULT_MAX_HEARTBEAT_AGE_SECONDS = 20
DEFAULT_READY_STABILITY_SECONDS = 5
DEFAULT_PROC_ROOT = "/proc"
MAX_STATE_BYTES = 4096
MAX_HEALTH_SECONDS = 3600
_INSTANCE_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_READY_STATUSES = frozenset({"polling", "paused_intentional"})

logger = logging.getLogger(__name__)


class TgbotHealthError(RuntimeError):
    """The local tgbot runtime state is not authoritative or ready."""


class TgbotRuntimeStatus(StrEnum):
    STARTING = "starting"
    POLLING_STARTING = "polling_starting"
    POLLING = "polling"
    PAUSED_INTENTIONAL = "paused_intentional"
    PAUSED_CONFIGURATION_ERROR = "paused_configuration_error"
    POLLING_BACKOFF = "polling_backoff"
    FAILED = "failed"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class TgbotHealthState:
    schema: int
    instance_id: str
    pid: int
    process_start_token: str
    command_sha256: str
    status: TgbotRuntimeStatus
    reason: str
    heartbeat_interval_seconds: int
    state_since_monotonic_ns: int
    heartbeat_monotonic_ns: int

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> TgbotHealthState:
        try:
            state = cls(
                schema=int(payload["schema"]),
                instance_id=str(payload["instance_id"]),
                pid=int(payload["pid"]),
                process_start_token=str(payload["process_start_token"]),
                command_sha256=str(payload["command_sha256"]),
                status=TgbotRuntimeStatus(str(payload["status"])),
                reason=str(payload["reason"]),
                heartbeat_interval_seconds=int(payload["heartbeat_interval_seconds"]),
                state_since_monotonic_ns=int(payload["state_since_monotonic_ns"]),
                heartbeat_monotonic_ns=int(payload["heartbeat_monotonic_ns"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TgbotHealthError("tgbot health state is malformed") from exc
        state.validate()
        return state

    def validate(self) -> None:
        if self.schema != 1:
            raise TgbotHealthError("unsupported tgbot health state schema")
        if not _INSTANCE_RE.fullmatch(self.instance_id):
            raise TgbotHealthError("tgbot instance id is invalid")
        if self.pid <= 0:
            raise TgbotHealthError("tgbot pid is invalid")
        if not self.process_start_token.isdigit():
            raise TgbotHealthError("tgbot process start token is invalid")
        if not _DIGEST_RE.fullmatch(self.command_sha256):
            raise TgbotHealthError("tgbot command digest is invalid")
        if not _REASON_RE.fullmatch(self.reason):
            raise TgbotHealthError("tgbot health reason is invalid")
        if not 1 <= self.heartbeat_interval_seconds <= MAX_HEALTH_SECONDS:
            raise TgbotHealthError("tgbot heartbeat interval is invalid")
        if (
            self.state_since_monotonic_ns <= 0
            or self.heartbeat_monotonic_ns < self.state_since_monotonic_ns
        ):
            raise TgbotHealthError("tgbot health timestamps are invalid")


def _state_path(raw: str | None = None) -> Path:
    path = Path(raw or os.getenv("TGBOT_HEALTH_STATE_FILE", DEFAULT_HEALTH_STATE_FILE))
    if not path.is_absolute():
        raise TgbotHealthError("tgbot health state path must be absolute")
    try:
        parent_metadata = path.parent.lstat()
    except FileNotFoundError as exc:
        raise TgbotHealthError("tgbot health state directory is missing") from exc
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
        parent_metadata.st_mode
    ):
        raise TgbotHealthError("tgbot health state directory is unsafe")
    return path


def _read_proc_identity(proc_root: Path, pid: int) -> tuple[str, str]:
    process_dir = proc_root / str(pid)
    try:
        stat_payload = (process_dir / "stat").read_bytes()
        command = (process_dir / "cmdline").read_bytes()
    except OSError as exc:
        raise TgbotHealthError("tgbot process is not present") from exc
    separator = stat_payload.rfind(b") ")
    if separator < 0:
        raise TgbotHealthError("tgbot process stat is malformed")
    fields = stat_payload[separator + 2 :].split()
    if len(fields) < 20 or fields[0] == b"Z":
        raise TgbotHealthError("tgbot process is dead or stat is incomplete")
    start_token = fields[19].decode("ascii", errors="strict")
    if not start_token.isdigit() or not command:
        raise TgbotHealthError("tgbot process identity is incomplete")
    return start_token, hashlib.sha256(command).hexdigest()


def _read_state(
    *,
    state_path: Path | None = None,
    expected_owner_uid: int | None = None,
) -> TgbotHealthState:
    path = _state_path(str(state_path) if state_path is not None else None)
    owner_uid = os.geteuid() if expected_owner_uid is None else expected_owner_uid
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TgbotHealthError("tgbot health state is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size <= 0
            or metadata.st_size > MAX_STATE_BYTES
        ):
            raise TgbotHealthError("tgbot health state metadata is unsafe")
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
        raise TgbotHealthError("tgbot health state is invalid JSON") from exc
    if not isinstance(document, dict):
        raise TgbotHealthError("tgbot health state must be an object")
    return TgbotHealthState.from_mapping(document)


def _write_state(state: TgbotHealthState, *, state_path: Path) -> None:
    state.validate()
    path = _state_path(str(state_path))
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()
    ):
        raise TgbotHealthError("tgbot health state destination is unsafe")

    document = asdict(state)
    document["status"] = state.status.value
    payload = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    temporary = path.with_name(f".{path.name}.{state.instance_id}.tmp")
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
                raise OSError("short write while persisting tgbot health state")
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


class TgbotHealthReporter:
    """Persist the current lifecycle state and refresh its event-loop heartbeat."""

    def __init__(
        self,
        *,
        state_path: Path,
        instance_id: str,
        pid: int,
        process_start_token: str,
        command_sha256: str,
        heartbeat_interval_seconds: int,
    ) -> None:
        self._state_path = state_path
        self._instance_id = instance_id
        self._pid = pid
        self._process_start_token = process_start_token
        self._command_sha256 = command_sha256
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._status = TgbotRuntimeStatus.STARTING
        self._reason = "process_start"
        self._state_since_monotonic_ns = time.monotonic_ns()
        self._heartbeat_task: asyncio.Task[None] | None = None

    @classmethod
    def from_environment(cls) -> TgbotHealthReporter:
        interval = _seconds_from_env(
            "TGBOT_HEALTH_HEARTBEAT_INTERVAL_SECONDS",
            DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        )
        pid = os.getpid()
        start_token, command_sha256 = _read_proc_identity(
            Path(os.getenv("TGBOT_PROC_ROOT", DEFAULT_PROC_ROOT)),
            pid,
        )
        return cls(
            state_path=_state_path(),
            instance_id=secrets.token_hex(16),
            pid=pid,
            process_start_token=start_token,
            command_sha256=command_sha256,
            heartbeat_interval_seconds=interval,
        )

    async def start(self) -> None:
        self.transition(TgbotRuntimeStatus.STARTING, "process_start")
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="lumen-tgbot-health-heartbeat",
        )

    def transition(self, status: TgbotRuntimeStatus, reason: str) -> None:
        if not _REASON_RE.fullmatch(reason):
            raise TgbotHealthError("tgbot health reason is invalid")
        now = time.monotonic_ns()
        if status != self._status or reason != self._reason:
            self._status = status
            self._reason = reason
            self._state_since_monotonic_ns = now
        self._persist(now)

    async def stop(self, status: TgbotRuntimeStatus, reason: str) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.transition(status, reason)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_seconds)
            try:
                self._persist(time.monotonic_ns())
            except Exception:  # noqa: BLE001
                logger.exception("failed to refresh tgbot health heartbeat")

    def _persist(self, now: int) -> None:
        _write_state(
            TgbotHealthState(
                schema=1,
                instance_id=self._instance_id,
                pid=self._pid,
                process_start_token=self._process_start_token,
                command_sha256=self._command_sha256,
                status=self._status,
                reason=self._reason,
                heartbeat_interval_seconds=self._heartbeat_interval_seconds,
                state_since_monotonic_ns=self._state_since_monotonic_ns,
                heartbeat_monotonic_ns=now,
            ),
            state_path=self._state_path,
        )


def check_tgbot_health(
    redis_client: Any,
    *,
    state_path: Path | None = None,
    proc_root: Path | None = None,
    expected_owner_uid: int | None = None,
    now_monotonic_ns: int | None = None,
    max_heartbeat_age_seconds: int = DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
    ready_stability_seconds: int = DEFAULT_READY_STABILITY_SECONDS,
) -> TgbotHealthState:
    if not 1 <= max_heartbeat_age_seconds <= MAX_HEALTH_SECONDS:
        raise TgbotHealthError("tgbot maximum heartbeat age is invalid")
    if not 0 <= ready_stability_seconds <= MAX_HEALTH_SECONDS:
        raise TgbotHealthError("tgbot ready stability interval is invalid")

    state = _read_state(
        state_path=state_path,
        expected_owner_uid=expected_owner_uid,
    )
    start_token, command_sha256 = _read_proc_identity(
        proc_root or Path(os.getenv("TGBOT_PROC_ROOT", DEFAULT_PROC_ROOT)),
        state.pid,
    )
    if (
        start_token != state.process_start_token
        or command_sha256 != state.command_sha256
    ):
        raise TgbotHealthError("tgbot process identity no longer matches")
    if state.status.value not in _READY_STATUSES:
        raise TgbotHealthError(
            f"tgbot runtime status is not ready: {state.status.value}"
        )

    now = time.monotonic_ns() if now_monotonic_ns is None else now_monotonic_ns
    if state.heartbeat_monotonic_ns > now:
        raise TgbotHealthError("tgbot heartbeat timestamp is in the future")
    max_age_ns = max_heartbeat_age_seconds * 1_000_000_000
    if now - state.heartbeat_monotonic_ns > max_age_ns:
        raise TgbotHealthError("tgbot heartbeat is stale")
    stability_ns = ready_stability_seconds * 1_000_000_000
    if now - state.state_since_monotonic_ns < stability_ns:
        raise TgbotHealthError("tgbot ready state is not stable yet")

    redis_client.ping()
    return state


def _seconds_from_env(name: str, default: int, *, allow_zero: bool = False) -> int:
    raw = os.getenv(name, str(default))
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise TgbotHealthError(f"{name} must be an integer") from exc
    minimum = 0 if allow_zero else 1
    if not minimum <= seconds <= MAX_HEALTH_SECONDS:
        raise TgbotHealthError(f"{name} is out of range")
    return seconds


def run_health_check() -> int:
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
        check_tgbot_health(
            redis_client,
            max_heartbeat_age_seconds=_seconds_from_env(
                "TGBOT_HEALTH_MAX_AGE_SECONDS",
                DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
            ),
            ready_stability_seconds=_seconds_from_env(
                "TGBOT_HEALTH_READY_STABILITY_SECONDS",
                DEFAULT_READY_STABILITY_SECONDS,
                allow_zero=True,
            ),
        )
    except (
        KeyError,
        OSError,
        TgbotHealthError,
        ValueError,
        redis.exceptions.RedisError,
    ) as exc:
        print(f"tgbot health error: {exc}", file=sys.stderr)
        return 1
    finally:
        client = locals().get("redis_client")
        close = getattr(client, "close", None)
        if callable(close):
            close()
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["check"]:
        return run_health_check()
    print("usage: python -m app.tgbot_health check", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
