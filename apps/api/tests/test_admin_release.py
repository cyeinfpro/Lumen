from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request
from sqlalchemy.exc import OperationalError

from app.routes import admin_release


class _FakeScalars:
    def __init__(self, values: list[str | None]) -> None:
        self._values = values

    def all(self) -> list[str | None]:
        return list(self._values)


class _FakeResult:
    def __init__(self, values: list[str | None]) -> None:
        self._values = values

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._values)


class _FakeDb:
    def __init__(
        self,
        values: list[str | None] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._values = values or []
        self._error = error

    async def execute(self, _statement: Any) -> _FakeResult:
        if self._error is not None:
            raise self._error
        return _FakeResult(self._values)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/release/rollback",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def _write_manifest(
    release_dir: Path,
    *,
    target_id: str,
    head: str = "0057_repair_concurrent_indexes",
) -> None:
    (release_dir / ".lumen_release.json").write_text(
        json.dumps({"id": target_id, "alembic_head_expected": head}),
        encoding="utf-8",
    )


def _configure_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    include_inventory: bool = True,
) -> tuple[str, Path, list[dict[str, object]]]:
    target_id = "20260806-010203"
    release_root = tmp_path / "lumen"
    release_dir = release_root / "releases" / target_id
    release_dir.mkdir(parents=True)
    releases = (
        [
            admin_release.ReleaseInfo(
                id=target_id,
                alembic_head_expected="0057_repair_concurrent_indexes",
            )
        ]
        if include_inventory
        else []
    )
    release_calls: list[dict[str, object]] = []

    class FakeLockService:
        def __init__(self, *, fallback_busy: Any) -> None:
            self.fallback_busy = fallback_busy

        async def acquire(self, **_kwargs: object) -> object:
            return object()

        async def release(self, _lock: object, **kwargs: object) -> None:
            release_calls.append(dict(kwargs))

    monkeypatch.setattr(admin_release, "SystemOperationLockService", FakeLockService)
    monkeypatch.setattr(admin_release, "update_lumen_root", lambda: release_root)
    monkeypatch.setattr(
        admin_release,
        "update_resolve_release",
        lambda _root, _target: release_dir,
    )
    monkeypatch.setattr(
        admin_release,
        "update_list_releases",
        lambda **_kwargs: releases,
    )
    monkeypatch.setattr(admin_release, "update_read_marker", lambda: None)
    monkeypatch.setattr(admin_release, "maintenance_marker_busy", lambda: False)
    monkeypatch.setattr(
        admin_release,
        "update_systemd_run_available",
        lambda: (_ for _ in ()).throw(AssertionError("runner must not be reached")),
    )
    return target_id, release_dir, release_calls


@pytest.mark.asyncio
async def test_rollback_rejects_missing_release_manifest_before_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_id, _release_dir, release_calls = _configure_rollback(
        monkeypatch,
        tmp_path,
    )

    with pytest.raises(Exception) as exc_info:
        await admin_release.rollback_release(
            admin_release.RollbackIn(release_id=target_id),
            _request(),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            _FakeDb(["0057_repair_concurrent_indexes"]),  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 409
    assert exc_info.value.detail["error"]["code"] == "release_manifest_unknown"
    assert release_calls == [
        {"succeeded": False, "reason": "release_manifest_unknown"}
    ]


@pytest.mark.asyncio
async def test_rollback_rejects_multiple_or_mismatched_database_heads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_id, release_dir, release_calls = _configure_rollback(
        monkeypatch,
        tmp_path,
    )
    _write_manifest(release_dir, target_id=target_id)

    with pytest.raises(Exception) as exc_info:
        await admin_release.rollback_release(
            admin_release.RollbackIn(release_id=target_id),
            _request(),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            _FakeDb(["0057_repair_concurrent_indexes", "other_head"]),  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 409
    assert exc_info.value.detail["error"]["code"] == "schema_mismatch"
    assert release_calls == [{"succeeded": False, "reason": "schema_mismatch"}]


@pytest.mark.asyncio
async def test_rollback_db_probe_exception_never_launches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_id, release_dir, release_calls = _configure_rollback(
        monkeypatch,
        tmp_path,
    )
    _write_manifest(release_dir, target_id=target_id)
    error = OperationalError("SELECT", {}, RuntimeError("db down"))

    with pytest.raises(Exception) as exc_info:
        await admin_release.rollback_release(
            admin_release.RollbackIn(release_id=target_id),
            _request(),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            _FakeDb(error=error),  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 503
    assert exc_info.value.detail["error"]["code"] == "database_schema_probe_failed"
    assert release_calls == [
        {"succeeded": False, "reason": "database_schema_probe_failed"}
    ]
