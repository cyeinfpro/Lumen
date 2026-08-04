from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys

import pytest


TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app import tgbot_health  # noqa: E402


class FakeRedis:
    def __init__(self) -> None:
        self.ping_calls = 0

    def ping(self) -> bool:
        self.ping_calls += 1
        return True


def _write_process(
    proc_root: Path,
    *,
    pid: int,
    start_token: int,
    command: bytes = b"python\x00-m\x00app.main\x00",
) -> None:
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    fields = ["S", *(["0"] * 18), str(start_token)]
    (process / "stat").write_text(
        f"{pid} (tgbot health test) {' '.join(fields)}\n",
        encoding="ascii",
    )
    (process / "cmdline").write_bytes(command)


def _state(
    *,
    proc_root: Path,
    pid: int,
    status: tgbot_health.TgbotRuntimeStatus,
    state_since_ns: int = 1_000_000_000,
    heartbeat_ns: int = 10_000_000_000,
    instance_id: str = "1" * 32,
) -> tgbot_health.TgbotHealthState:
    command = (proc_root / str(pid) / "cmdline").read_bytes()
    return tgbot_health.TgbotHealthState(
        schema=1,
        instance_id=instance_id,
        pid=pid,
        process_start_token="5001",
        command_sha256=hashlib.sha256(command).hexdigest(),
        status=status,
        reason="test_state",
        heartbeat_interval_seconds=5,
        state_since_monotonic_ns=state_since_ns,
        heartbeat_monotonic_ns=heartbeat_ns,
    )


@pytest.mark.parametrize(
    "status",
    (
        tgbot_health.TgbotRuntimeStatus.POLLING,
        tgbot_health.TgbotRuntimeStatus.PAUSED_INTENTIONAL,
    ),
)
def test_polling_and_intentional_runtime_pause_are_healthy(
    tmp_path: Path,
    status: tgbot_health.TgbotRuntimeStatus,
) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "tgbot-health.json"
    _write_process(proc_root, pid=101, start_token=5001)
    state = _state(proc_root=proc_root, pid=101, status=status)
    tgbot_health._write_state(state, state_path=state_path)
    redis = FakeRedis()

    checked = tgbot_health.check_tgbot_health(
        redis,
        state_path=state_path,
        proc_root=proc_root,
        now_monotonic_ns=12_000_000_000,
        max_heartbeat_age_seconds=5,
        ready_stability_seconds=5,
    )

    assert checked == state
    assert redis.ping_calls == 1


@pytest.mark.parametrize(
    "status",
    (
        tgbot_health.TgbotRuntimeStatus.STARTING,
        tgbot_health.TgbotRuntimeStatus.POLLING_STARTING,
        tgbot_health.TgbotRuntimeStatus.PAUSED_CONFIGURATION_ERROR,
        tgbot_health.TgbotRuntimeStatus.POLLING_BACKOFF,
        tgbot_health.TgbotRuntimeStatus.FAILED,
        tgbot_health.TgbotRuntimeStatus.STOPPING,
    ),
)
def test_non_polling_or_configuration_error_states_are_unhealthy(
    tmp_path: Path,
    status: tgbot_health.TgbotRuntimeStatus,
) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "tgbot-health.json"
    _write_process(proc_root, pid=101, start_token=5001)
    tgbot_health._write_state(
        _state(proc_root=proc_root, pid=101, status=status),
        state_path=state_path,
    )
    redis = FakeRedis()

    with pytest.raises(tgbot_health.TgbotHealthError, match="status is not ready"):
        tgbot_health.check_tgbot_health(
            redis,
            state_path=state_path,
            proc_root=proc_root,
            now_monotonic_ns=12_000_000_000,
            max_heartbeat_age_seconds=5,
            ready_stability_seconds=0,
        )

    assert redis.ping_calls == 0


def test_stale_heartbeat_is_unhealthy_even_when_process_and_redis_are_live(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "tgbot-health.json"
    _write_process(proc_root, pid=101, start_token=5001)
    tgbot_health._write_state(
        _state(
            proc_root=proc_root,
            pid=101,
            status=tgbot_health.TgbotRuntimeStatus.POLLING,
            heartbeat_ns=2_000_000_000,
        ),
        state_path=state_path,
    )

    with pytest.raises(tgbot_health.TgbotHealthError, match="heartbeat is stale"):
        tgbot_health.check_tgbot_health(
            FakeRedis(),
            state_path=state_path,
            proc_root=proc_root,
            now_monotonic_ns=12_000_000_000,
            max_heartbeat_age_seconds=5,
            ready_stability_seconds=0,
        )


def test_ready_state_must_be_stable_before_compose_accepts_it(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "tgbot-health.json"
    _write_process(proc_root, pid=101, start_token=5001)
    tgbot_health._write_state(
        _state(
            proc_root=proc_root,
            pid=101,
            status=tgbot_health.TgbotRuntimeStatus.POLLING,
            state_since_ns=10_000_000_000,
        ),
        state_path=state_path,
    )

    with pytest.raises(tgbot_health.TgbotHealthError, match="not stable yet"):
        tgbot_health.check_tgbot_health(
            FakeRedis(),
            state_path=state_path,
            proc_root=proc_root,
            now_monotonic_ns=12_000_000_000,
            max_heartbeat_age_seconds=5,
            ready_stability_seconds=5,
        )


def test_pid_reuse_cannot_reuse_a_previous_healthy_state(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "tgbot-health.json"
    _write_process(proc_root, pid=101, start_token=5001)
    state = _state(
        proc_root=proc_root,
        pid=101,
        status=tgbot_health.TgbotRuntimeStatus.POLLING,
    )
    tgbot_health._write_state(state, state_path=state_path)
    (proc_root / "101" / "stat").write_text(
        "101 (replacement) " + " ".join(["S", *(["0"] * 18), "9001"]) + "\n",
        encoding="ascii",
    )

    with pytest.raises(tgbot_health.TgbotHealthError, match="no longer matches"):
        tgbot_health.check_tgbot_health(
            FakeRedis(),
            state_path=state_path,
            proc_root=proc_root,
            now_monotonic_ns=12_000_000_000,
            max_heartbeat_age_seconds=5,
            ready_stability_seconds=0,
        )


def test_state_rewrite_is_atomic_private_and_complete(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "tgbot-health.json"
    _write_process(proc_root, pid=101, start_token=5001)
    starting = _state(
        proc_root=proc_root,
        pid=101,
        status=tgbot_health.TgbotRuntimeStatus.STARTING,
    )
    polling = _state(
        proc_root=proc_root,
        pid=101,
        status=tgbot_health.TgbotRuntimeStatus.POLLING,
    )

    tgbot_health._write_state(starting, state_path=state_path)
    tgbot_health._write_state(polling, state_path=state_path)

    assert tgbot_health._read_state(state_path=state_path) == polling
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".tgbot-health.json.*.tmp"))
    assert state_path.stat().st_uid == os.geteuid()
