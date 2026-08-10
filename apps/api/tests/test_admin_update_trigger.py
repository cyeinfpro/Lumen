from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.admin import update_trigger


class TypedHttpError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class LockRecorder:
    def __init__(self) -> None:
        self.releases: list[dict[str, object]] = []

    async def acquire(self, **_kwargs: object) -> object:
        return object()

    async def release(self, _lock: object, **kwargs: object) -> None:
        self.releases.append(dict(kwargs))


class TrackingLog(io.StringIO):
    was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()


def _http_error(code: str, message: str, status_code: int) -> TypedHttpError:
    return TypedHttpError(code, message, status_code)


def _runtime(
    tmp_path: Path,
    lock: LockRecorder,
    *,
    open_update_log: Any | None = None,
    clean_proxy_env: Any | None = None,
    runner_unit_available: Any | None = None,
    start_update_via_path_unit: Any | None = None,
    write_marker: Any | None = None,
) -> tuple[update_trigger.TriggerRuntime, Path, Path]:
    lumen_root = tmp_path / "lumen"
    shared_root = lumen_root / "shared"
    scripts_root = lumen_root / "current" / "scripts"
    backup_root = tmp_path / "backup"
    shared_root.mkdir(parents=True)
    scripts_root.mkdir(parents=True)
    backup_root.mkdir(parents=True)
    script = scripts_root / "update.sh"
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    log_path = backup_root / ".update.log"

    async def _unused_async(*_args: object, **_kwargs: object) -> Any:
        return None

    runtime = update_trigger.TriggerRuntime(
        http_error=_http_error,
        response_model=SimpleNamespace,
        response_factory=SimpleNamespace,
        update_script=lambda: script,
        read_marker=lambda: None,
        ensure_not_running=lambda _marker: None,
        maintenance_marker_busy=lambda: False,
        update_channel=_unused_async,
        update_allow_prerelease=_unused_async,
        update_check_ttl=_unused_async,
        resolve_update_proxy=_unused_async,
        lumen_root=lambda: lumen_root,
        update_check_service=object,
        validate_update_tag=lambda value: value,
        derive_idempotency_key=lambda *_args: "idem",
        get_cached_json=_unused_async,
        cache_json=_unused_async,
        lock_service_factory=lambda **_kwargs: lock,
        update_log_path=lambda: log_path,
        open_update_log=(
            open_update_log
            if open_update_log is not None
            else lambda: log_path.open("a", encoding="utf-8")
        ),
        clean_proxy_env=(
            clean_proxy_env
            if clean_proxy_env is not None
            else lambda _env: None
        ),
        apply_proxy_env=lambda _env, _url: None,
        apply_dotenv_proxy_env=lambda _env, _path: None,
        shared_env_path=lambda _script: shared_root / ".env",
        mask_proxy_url=lambda value: value,
        version_from_update_tag=lambda _tag: "1.2.109",
        write_marker=write_marker or (lambda *_args, **_kwargs: True),
        runner_unit_available=runner_unit_available or (lambda: True),
        runner_trigger_only_mode=lambda: True,
        start_update_via_path_unit=(
            start_update_via_path_unit
            or (lambda **_kwargs: (0, "lumen-update-runner.service"))
        ),
        systemd_run_available=lambda: False,
        start_update_systemd_unit=lambda **_kwargs: None,
        write_audit=_unused_async,
        schedule_cleanup=lambda _proc: None,
    )
    return runtime, shared_root, backup_root


async def _launch(
    runtime: update_trigger.TriggerRuntime,
) -> tuple[int, str | None, Any, Any]:
    return await update_trigger._launch(
        SimpleNamespace(),
        SimpleNamespace(id="admin-1"),
        SimpleNamespace(
            target_tag="v1.2.109",
            force_redeploy=False,
        ),
        object(),  # type: ignore[arg-type]
        channel="stable",
        proxy=None,
        proxy_url=None,
        target_tag="v1.2.109",
        idempotency_key="idem-1",
        runtime=runtime,
    )


def _write_journal(
    shared_root: Path,
    *,
    operation_id: str,
    status: str,
) -> Path:
    path = shared_root / ".update-journal.json"
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "operation_id": operation_id,
                "status": status,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_update_log_open_failure_is_typed_and_releases_lock_without_side_effects(
    tmp_path: Path,
) -> None:
    lock = LockRecorder()
    launcher_calls: list[object] = []
    marker_calls: list[object] = []

    def deny_log() -> Any:
        raise PermissionError(13, "permission denied")

    runtime, _shared_root, backup_root = _runtime(
        tmp_path,
        lock,
        open_update_log=deny_log,
        start_update_via_path_unit=lambda **kwargs: launcher_calls.append(kwargs),
        write_marker=lambda *args, **kwargs: marker_calls.append((args, kwargs)),
    )

    with pytest.raises(TypedHttpError) as excinfo:
        await _launch(runtime)

    assert excinfo.value.code == "update_log_unwritable"
    assert excinfo.value.status_code == 503
    assert lock.releases == [
        {"succeeded": False, "reason": "update_log_unwritable"}
    ]
    assert launcher_calls == []
    assert marker_calls == []
    assert not (backup_root / ".update.running").exists()
    assert not (backup_root / ".update.request.json").exists()
    assert not (backup_root / ".update.trigger").exists()


@pytest.mark.asyncio
async def test_post_open_exception_closes_log_and_releases_lock(
    tmp_path: Path,
) -> None:
    lock = LockRecorder()
    log = TrackingLog()

    def fail_after_open(_env: dict[str, str]) -> None:
        raise RuntimeError("setup failed")

    runtime, _shared_root, backup_root = _runtime(
        tmp_path,
        lock,
        open_update_log=lambda: log,
        clean_proxy_env=fail_after_open,
    )

    with pytest.raises(RuntimeError, match="setup failed"):
        await _launch(runtime)

    assert log.was_closed is True
    assert lock.releases == [{"succeeded": False, "reason": "launch_failed"}]
    assert not (backup_root / ".update.running").exists()
    assert not (backup_root / ".update.request.json").exists()
    assert not (backup_root / ".update.trigger").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("journal_status", "journal_operation_id", "resume_operation_id"),
    [
        ("running", "update-old", "update-old"),
        ("failed", "update-old", None),
        ("failed", "update-old", "update-other"),
        (None, None, "update-orphan"),
        ("manual_required", "update-old", None),
    ],
)
async def test_unhandled_journal_or_resume_state_fails_closed_before_launch(
    tmp_path: Path,
    journal_status: str | None,
    journal_operation_id: str | None,
    resume_operation_id: str | None,
) -> None:
    lock = LockRecorder()
    open_calls: list[object] = []
    launcher_calls: list[object] = []
    runtime, shared_root, backup_root = _runtime(
        tmp_path,
        lock,
        open_update_log=lambda: open_calls.append(object()),
        start_update_via_path_unit=lambda **kwargs: launcher_calls.append(kwargs),
    )
    if journal_status is not None and journal_operation_id is not None:
        _write_journal(
            shared_root,
            operation_id=journal_operation_id,
            status=journal_status,
        )
    if resume_operation_id is not None:
        (shared_root / ".update-resume").write_text(
            f"{resume_operation_id}\n",
            encoding="utf-8",
        )

    with pytest.raises(TypedHttpError) as excinfo:
        await _launch(runtime)

    assert excinfo.value.code == "update_recovery_pending"
    assert excinfo.value.status_code == 409
    assert lock.releases == [
        {"succeeded": False, "reason": "update_recovery_pending"}
    ]
    assert open_calls == []
    assert launcher_calls == []
    assert not (backup_root / ".update.running").exists()
    assert not (backup_root / ".update.request.json").exists()
    assert not (backup_root / ".update.trigger").exists()


@pytest.mark.asyncio
async def test_existing_request_and_trigger_are_preserved_instead_of_overwritten(
    tmp_path: Path,
) -> None:
    lock = LockRecorder()
    runtime, shared_root, backup_root = _runtime(tmp_path, lock)
    _write_journal(
        shared_root,
        operation_id="update-complete",
        status="complete",
    )
    request_path = backup_root / ".update.request.json"
    trigger_path = backup_root / ".update.trigger"
    request_path.write_bytes(b'{"legacy":"request"}\n')
    trigger_path.write_bytes(b'{"legacy":"trigger"}\n')

    with pytest.raises(TypedHttpError) as excinfo:
        await _launch(runtime)

    assert excinfo.value.code == "update_recovery_pending"
    assert excinfo.value.status_code == 409
    assert request_path.read_bytes() == b'{"legacy":"request"}\n'
    assert trigger_path.read_bytes() == b'{"legacy":"trigger"}\n'
    assert lock.releases == [
        {"succeeded": False, "reason": "update_recovery_pending"}
    ]


@pytest.mark.asyncio
async def test_malformed_legacy_journal_fails_closed_as_unreadable(
    tmp_path: Path,
) -> None:
    lock = LockRecorder()
    runtime, shared_root, backup_root = _runtime(tmp_path, lock)
    (shared_root / ".update-journal.json").write_text(
        '{"schema":1,"status":"failed"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypedHttpError) as excinfo:
        await _launch(runtime)

    assert excinfo.value.code == "update_recovery_state_unreadable"
    assert excinfo.value.status_code == 503
    assert lock.releases == [
        {"succeeded": False, "reason": "update_recovery_state_unreadable"}
    ]
    assert not (backup_root / ".update.running").exists()
    assert not (backup_root / ".update.request.json").exists()
    assert not (backup_root / ".update.trigger").exists()


@pytest.mark.asyncio
async def test_clean_terminal_journal_allows_new_launch(
    tmp_path: Path,
) -> None:
    lock = LockRecorder()
    runtime, shared_root, _backup_root = _runtime(tmp_path, lock)
    _write_journal(
        shared_root,
        operation_id="update-complete",
        status="complete",
    )

    pid, unit, proc, _started_at = await _launch(runtime)

    assert (pid, unit, proc) == (0, "lumen-update-runner.service", None)
    assert lock.releases == [{"succeeded": True, "reason": "launched"}]
