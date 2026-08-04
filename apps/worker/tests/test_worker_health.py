from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest

import app.worker_health as worker_health
from app.worker_health import (
    WorkerHealthError,
    build_worker_health_identity,
    check_worker_health,
    write_worker_health_identity,
)


class FakeRedis:
    def __init__(
        self,
        *,
        values: dict[str, bytes] | None = None,
        ttls: dict[str, int] | None = None,
    ) -> None:
        self.values = values or {}
        self.ttls = ttls or {}

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def pttl(self, key: str) -> int:
        return self.ttls.get(key, -2)


def _write_process(
    proc_root: Path,
    *,
    pid: int,
    start_token: int,
    command: bytes = b"python\x00-m\x00app.worker_health\x00run\x00",
    state: str = "S",
) -> None:
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    fields = [state, *(["0"] * 18), str(start_token)]
    (process / "stat").write_text(
        f"{pid} (worker health test) {' '.join(fields)}\n",
        encoding="ascii",
    )
    (process / "cmdline").write_bytes(command)


def _identity(
    proc_root: Path,
    *,
    pid: int,
    instance_id: str,
    interval_seconds: int = 30,
):
    return build_worker_health_identity(
        instance_id=instance_id,
        key_prefix="arq:queue:health-check",
        interval_seconds=interval_seconds,
        pid=pid,
        proc_root=proc_root,
    )


def test_stale_key_without_worker_process_is_unhealthy(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "worker-health.json"
    instance_id = "1" * 32
    _write_process(proc_root, pid=101, start_token=5001)
    identity = _identity(proc_root, pid=101, instance_id=instance_id)
    write_worker_health_identity(identity, state_path=state_path)
    shutil.rmtree(proc_root / "101")
    redis = FakeRedis(
        values={identity.health_key: b"old heartbeat"},
        ttls={identity.health_key: 20_000},
    )

    with pytest.raises(WorkerHealthError, match="process is not present"):
        check_worker_health(
            redis,
            state_path=state_path,
            proc_root=proc_root,
        )


def test_wrong_instance_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "worker-health.json"
    _write_process(proc_root, pid=102, start_token=5002)
    identity = _identity(proc_root, pid=102, instance_id="2" * 32)
    write_worker_health_identity(identity, state_path=state_path)
    wrong_key = f"arq:queue:health-check:{'3' * 32}"
    redis = FakeRedis(
        values={wrong_key: b"fresh but belongs to another worker"},
        ttls={wrong_key: 20_000},
    )

    with pytest.raises(WorkerHealthError, match="heartbeat is missing"):
        check_worker_health(
            redis,
            state_path=state_path,
            proc_root=proc_root,
        )


def test_pid_reuse_with_old_identity_is_unhealthy(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "worker-health.json"
    _write_process(proc_root, pid=107, start_token=5007)
    identity = _identity(proc_root, pid=107, instance_id="8" * 32)
    write_worker_health_identity(identity, state_path=state_path)
    shutil.rmtree(proc_root / "107")
    _write_process(proc_root, pid=107, start_token=9007)
    redis = FakeRedis(
        values={identity.health_key: b"old heartbeat"},
        ttls={identity.health_key: 20_000},
    )

    with pytest.raises(WorkerHealthError, match="no longer matches"):
        check_worker_health(
            redis,
            state_path=state_path,
            proc_root=proc_root,
        )


def test_service_user_self_check_with_owned_state_is_healthy(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "worker-health.json"
    _write_process(proc_root, pid=103, start_token=5003)
    identity = _identity(proc_root, pid=103, instance_id="4" * 32)
    write_worker_health_identity(identity, state_path=state_path)
    redis = FakeRedis(
        values={identity.health_key: b"fresh heartbeat"},
        ttls={identity.health_key: 20_000},
    )

    checked = check_worker_health(
        redis,
        state_path=state_path,
        proc_root=proc_root,
    )

    assert checked == identity


def test_root_check_accepts_service_owned_state_with_expected_uid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "worker-health.json"
    _write_process(proc_root, pid=108, start_token=5008)
    identity = _identity(proc_root, pid=108, instance_id="9" * 32)
    write_worker_health_identity(identity, state_path=state_path)
    owner_uid = state_path.stat().st_uid
    redis = FakeRedis(
        values={identity.health_key: b"fresh heartbeat"},
        ttls={identity.health_key: 20_000},
    )
    monkeypatch.setattr(worker_health.os, "geteuid", lambda: 0)

    checked = check_worker_health(
        redis,
        state_path=state_path,
        proc_root=proc_root,
        expected_owner_uid=owner_uid,
    )

    assert checked == identity


def test_expected_owner_uid_mismatch_is_unhealthy(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "worker-health.json"
    _write_process(proc_root, pid=109, start_token=5009)
    identity = _identity(proc_root, pid=109, instance_id="a" * 32)
    write_worker_health_identity(identity, state_path=state_path)
    redis = FakeRedis(
        values={identity.health_key: b"fresh heartbeat"},
        ttls={identity.health_key: 20_000},
    )

    with pytest.raises(WorkerHealthError, match="metadata is unsafe"):
        check_worker_health(
            redis,
            state_path=state_path,
            proc_root=proc_root,
            expected_owner_uid=state_path.stat().st_uid + 1,
        )


def test_worker_health_state_rejects_symlink_and_group_readable_mode(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    _write_process(proc_root, pid=110, start_token=5010)
    identity = _identity(proc_root, pid=110, instance_id="b" * 32)
    redis = FakeRedis(
        values={identity.health_key: b"fresh heartbeat"},
        ttls={identity.health_key: 20_000},
    )
    target = tmp_path / "owned-state.json"
    write_worker_health_identity(identity, state_path=target)
    symlink = tmp_path / "symlink-state.json"
    symlink.symlink_to(target)

    with pytest.raises(WorkerHealthError, match="state is unavailable"):
        check_worker_health(
            redis,
            state_path=symlink,
            proc_root=proc_root,
        )

    target.chmod(0o640)
    with pytest.raises(WorkerHealthError, match="metadata is unsafe"):
        check_worker_health(
            redis,
            state_path=target,
            proc_root=proc_root,
        )


def test_root_cli_forwards_strict_expected_owner_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_uid = os.getuid()
    captured: list[int | None] = []
    monkeypatch.setattr(worker_health.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        worker_health,
        "run_health_check",
        lambda *, expected_owner_uid=None: (
            captured.append(expected_owner_uid) or 0
        ),
    )

    result = worker_health.main(
        ["check", "--expected-owner-uid", str(owner_uid)]
    )

    assert result == 0
    assert captured == [owner_uid]


def test_nonroot_cli_cannot_override_expected_owner_uid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(worker_health.os, "geteuid", lambda: 1000)

    result = worker_health.main(["check", "--expected-owner-uid", "1000"])

    assert result == 2
    assert "requires root" in capsys.readouterr().err


@pytest.mark.parametrize("raw_uid", ["", "+1", "-1", "01", "1.0", " 1"])
def test_root_cli_rejects_noncanonical_expected_owner_uid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw_uid: str,
) -> None:
    monkeypatch.setattr(worker_health.os, "geteuid", lambda: 0)

    result = worker_health.main(
        ["check", "--expected-owner-uid", raw_uid]
    )

    assert result == 2
    assert "canonical decimal integer" in capsys.readouterr().err


@pytest.mark.parametrize("ttl_ms", (-1, 0, 33_000))
def test_live_worker_rejects_stale_or_unbounded_heartbeat(
    tmp_path: Path,
    ttl_ms: int,
) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "worker-health.json"
    _write_process(proc_root, pid=104, start_token=5004)
    identity = _identity(proc_root, pid=104, instance_id="5" * 32)
    write_worker_health_identity(identity, state_path=state_path)
    redis = FakeRedis(
        values={identity.health_key: b"heartbeat"},
        ttls={identity.health_key: ttl_ms},
    )

    with pytest.raises(WorkerHealthError, match="freshness is invalid"):
        check_worker_health(
            redis,
            state_path=state_path,
            proc_root=proc_root,
        )


def test_crash_restart_rebinds_state_and_ignores_old_fresh_key(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    state_path = tmp_path / "worker-health.json"
    _write_process(proc_root, pid=105, start_token=5005)
    old_identity = _identity(proc_root, pid=105, instance_id="6" * 32)
    write_worker_health_identity(old_identity, state_path=state_path)
    redis = FakeRedis(
        values={old_identity.health_key: b"old heartbeat"},
        ttls={old_identity.health_key: 20_000},
    )
    shutil.rmtree(proc_root / "105")

    with pytest.raises(WorkerHealthError, match="process is not present"):
        check_worker_health(
            redis,
            state_path=state_path,
            proc_root=proc_root,
        )

    _write_process(proc_root, pid=106, start_token=5006)
    new_identity = _identity(proc_root, pid=106, instance_id="7" * 32)
    write_worker_health_identity(new_identity, state_path=state_path)
    redis.values[new_identity.health_key] = b"new heartbeat"
    redis.ttls[new_identity.health_key] = 20_000

    checked = check_worker_health(
        redis,
        state_path=state_path,
        proc_root=proc_root,
    )

    assert checked == new_identity
    assert old_identity.health_key != new_identity.health_key
