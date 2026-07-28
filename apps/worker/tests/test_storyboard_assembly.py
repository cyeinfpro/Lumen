from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.storage_writes import StorageWriteCoordinator
from app.tasks import storyboard_assembly
from lumen_core.storage_capacity import StorageCapacityExceeded


def _claim(
    *,
    attempt_token: str = "attempt-1",
    fingerprint: str = "fingerprint-1",
) -> storyboard_assembly._AssemblyClaim:  # noqa: SLF001
    return storyboard_assembly._AssemblyClaim(  # noqa: SLF001
        run_id="run-1",
        user_id="user-1",
        step_id="assembly-1",
        attempt_token=attempt_token,
        fingerprint=fingerprint,
        idempotency_key="sb:run-1:assembly:fingerprint-1",
        segment_ids=("video-gen-1", "video-gen-2"),
        output_json={
            "segment_ids": ["video-gen-1", "video-gen-2"],
            "assembly_attempt_token": attempt_token,
            "assembly_fingerprint": fingerprint,
        },
    )


@pytest.mark.parametrize("pending_status", ("waiting", "compositing"))
@pytest.mark.asyncio
async def test_pending_to_compositing_claim_compiles_full_cas_predicate(
    pending_status: str,
) -> None:
    statements: list[Any] = []

    class Result:
        rowcount = 1

    class Session:
        async def execute(self, statement: Any) -> Result:
            statements.append(statement)
            return Result()

    output = {
        "assembly_attempt_token": "attempt-1",
        "assembly_fingerprint": "fingerprint-1",
        "assembly_claimed_at": "2026-07-18T00:00:00+00:00",
    }
    claimed = await storyboard_assembly._claim_waiting_assembly(  # noqa: SLF001
        Session(),
        step_id="assembly-1",
        attempt_token="attempt-1",
        fingerprint="fingerprint-1",
        output_json=output,
        status=pending_status,
    )

    assert claimed is True
    assert len(statements) == 1
    where_sql = str(
        statements[0].whereclause.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert f"workflow_steps.status = '{pending_status}'" in where_sql
    assert (
        "CAST((workflow_steps.output_json ->> 'assembly_attempt_token') AS VARCHAR) "
        "= 'attempt-1'"
    ) in where_sql
    assert (
        "CAST((workflow_steps.output_json ->> 'assembly_fingerprint') AS VARCHAR) "
        "= 'fingerprint-1'"
    ) in where_sql
    assert (
        "CAST((workflow_steps.output_json ->> 'assembly_claimed_at') AS VARCHAR) "
        "IS NULL"
    ) in where_sql


@pytest.mark.asyncio
async def test_heartbeat_renews_current_attempt_lease_with_fake_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)

    class Result:
        rowcount = 1

    class Session:
        def __init__(self) -> None:
            self.statement: Any | None = None
            self.committed = False

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def execute(self, statement: Any) -> Result:
            self.statement = statement
            return Result()

        async def commit(self) -> None:
            self.committed = True

    session = Session()
    monkeypatch.setattr(storyboard_assembly, "SessionLocal", lambda: session)
    monkeypatch.setattr(storyboard_assembly, "_now", lambda: now)

    renewed = await storyboard_assembly._renew_assembly_lease(  # noqa: SLF001
        _claim()
    )

    assert renewed is True
    assert session.committed is True
    assert session.statement is not None
    compiled = session.statement.compile(dialect=postgresql.dialect())
    output = next(
        value for value in compiled.params.values() if isinstance(value, dict)
    )
    assert output["assembly_heartbeat_at"] == now.isoformat()
    assert (
        output["assembly_lease_expires_at"]
        == (
            now + timedelta(seconds=storyboard_assembly.STORYBOARD_ASSEMBLY_LEASE_TTL_S)
        ).isoformat()
    )
    assert "attempt-1" in compiled.params.values()
    assert "fingerprint-1" in compiled.params.values()


@pytest.mark.asyncio
async def test_heartbeat_marks_attempt_lost_when_token_is_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_wait(_seconds: float) -> None:
        return None

    async def superseded(
        _claim: storyboard_assembly._AssemblyClaim,  # noqa: SLF001
    ) -> bool:
        return False

    monkeypatch.setattr(storyboard_assembly.asyncio, "sleep", no_wait)
    monkeypatch.setattr(storyboard_assembly, "_renew_assembly_lease", superseded)
    attempt_lost = asyncio.Event()

    await storyboard_assembly._assembly_heartbeat(  # noqa: SLF001
        _claim(),
        attempt_lost,
    )

    assert attempt_lost.is_set()


@pytest.mark.asyncio
async def test_concurrent_workers_only_one_claim_reaches_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    storage_writes_marker = object()
    lock = asyncio.Lock()
    claimed = False
    concat_calls = 0
    published_events: list[str] = []

    async def claim_once(
        _run_id: str,
        *,
        expected_attempt_token: str | None,
    ) -> storyboard_assembly._AssemblyClaim | None:  # noqa: SLF001
        nonlocal claimed
        assert expected_attempt_token is None
        async with lock:
            if claimed:
                return None
            claimed = True
            return claim

    async def publish(*_args: Any, **kwargs: Any) -> None:
        published_events.append(kwargs["event_name"])

    async def load_paths(
        _claim: storyboard_assembly._AssemblyClaim,  # noqa: SLF001
    ) -> list[Path]:
        return [Path("/tmp/segment-1.mp4"), Path("/tmp/segment-2.mp4")]

    def concat(_paths: list[Path]) -> bytes:
        nonlocal concat_calls
        concat_calls += 1
        return b"concat"

    def postprocess(_data: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {
                "video_bytes": b"video",
                "poster_bytes": None,
                "width": 16,
                "height": 9,
                "duration_ms": 1000,
            },
            {},
        )

    async def store(
        _claim: storyboard_assembly._AssemblyClaim,  # noqa: SLF001
        *,
        processed: dict[str, Any],
        diagnostics: dict[str, Any],
        storage_writes: Any,
    ) -> Any:
        assert processed["video_bytes"] == b"video"
        assert diagnostics == {}
        assert storage_writes is storage_writes_marker
        return SimpleNamespace(id="video-1")

    monkeypatch.setattr(storyboard_assembly, "_claim_assembly", claim_once)
    monkeypatch.setattr(storyboard_assembly, "_publish", publish)
    monkeypatch.setattr(storyboard_assembly, "_load_segment_paths", load_paths)
    monkeypatch.setattr(storyboard_assembly, "_concat_segments_sync", concat)
    monkeypatch.setattr(storyboard_assembly, "_postprocess_video_bytes", postprocess)
    monkeypatch.setattr(storyboard_assembly, "_store_assembly_result", store)

    ctx = {
        "redis": object(),
        "storage_write_coordinator": storage_writes_marker,
    }
    await asyncio.gather(
        storyboard_assembly.run_storyboard_assembly(ctx, "run-1"),
        storyboard_assembly.run_storyboard_assembly(ctx, "run-1"),
    )

    assert concat_calls == 1
    assert published_events == [
        "storyboard.assembling",
        "storyboard.assembled",
    ]


@pytest.mark.asyncio
async def test_run_requires_storage_write_coordinator_before_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_called = False

    async def claim(*_args: Any, **_kwargs: Any) -> None:
        nonlocal claim_called
        claim_called = True

    monkeypatch.setattr(storyboard_assembly, "_claim_assembly", claim)

    with pytest.raises(KeyError, match="storage_write_coordinator"):
        await storyboard_assembly.run_storyboard_assembly(
            {"redis": object()},
            "run-1",
        )

    assert claim_called is False


@pytest.mark.asyncio
async def test_late_failure_does_not_overwrite_completed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    terminal = {"status": "done", "video_id": "video-complete"}
    published = False

    class Result:
        def __init__(self, rowcount: int) -> None:
            self.rowcount = rowcount

    class Session:
        def __init__(self) -> None:
            self.statements: list[Any] = []
            self.rolled_back = False

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def execute(self, statement: Any) -> Result:
            self.statements.append(statement)
            return Result(1 if terminal["status"] == "compositing" else 0)

        async def rollback(self) -> None:
            self.rolled_back = True

    async def publish(*_args: Any, **_kwargs: Any) -> None:
        nonlocal published
        published = True

    session = Session()
    monkeypatch.setattr(storyboard_assembly, "SessionLocal", lambda: session)
    monkeypatch.setattr(storyboard_assembly, "_publish", publish)

    updated = await storyboard_assembly._fail_assembly(  # noqa: SLF001
        object(),
        claim=claim,
        code="late_failure",
        message="too late",
    )

    assert updated is False
    assert terminal == {"status": "done", "video_id": "video-complete"}
    assert session.rolled_back is True
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    assert "compositing" in compiled.params.values()
    assert claim.attempt_token in compiled.params.values()
    assert claim.fingerprint in compiled.params.values()
    assert published is False


@pytest.mark.asyncio
async def test_old_attempt_cannot_complete_or_overwrite_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {
        "status": "compositing",
        "attempt_token": "attempt-new",
        "video_id": None,
    }

    class Result:
        def __init__(self, rowcount: int) -> None:
            self.rowcount = rowcount

    class Session:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.statement: Any | None = None
            self.rolled_back = False
            self.committed = False

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def execute(self, statement: Any) -> Result:
            self.statement = statement
            compiled = statement.compile(dialect=postgresql.dialect())
            matches_current = (
                current["status"] == "compositing"
                and current["attempt_token"] in compiled.params.values()
            )
            if matches_current:
                current["status"] = "done"
                current["video_id"] = "video-old"
            return Result(int(matches_current))

        async def rollback(self) -> None:
            self.rolled_back = True
            self.added.clear()

        async def commit(self) -> None:
            self.committed = True

    session = Session()
    monkeypatch.setattr(storyboard_assembly, "SessionLocal", lambda: session)

    completed = await storyboard_assembly._complete_assembly(  # noqa: SLF001
        _claim(attempt_token="attempt-old"),
        SimpleNamespace(id="video-old"),  # type: ignore[arg-type]
    )

    assert completed is False
    assert current == {
        "status": "compositing",
        "attempt_token": "attempt-new",
        "video_id": None,
    }
    assert session.rolled_back is True
    assert session.committed is False
    assert session.added == []
    assert session.statement is not None
    compiled = session.statement.compile(dialect=postgresql.dialect())
    assert "attempt-old" in compiled.params.values()
    assert "fingerprint-1" in compiled.params.values()
    assert "done" in compiled.params.values()


class _Lease:
    def __init__(self) -> None:
        self.release_calls = 0

    async def renew(self) -> bool:
        return True

    async def release(self) -> None:
        self.release_calls += 1


class _Capacity:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.requests: list[int] = []
        self.lease = _Lease()

    async def reserve(self, bytes_required: int) -> _Lease:
        self.requests.append(bytes_required)
        if self.error is not None:
            raise self.error
        return self.lease


class _Storage:
    def __init__(self, *, existing_keys: set[str] | None = None) -> None:
        self.existing_keys = existing_keys or set()
        self.written: list[str] = []
        self.deleted: list[str] = []

    def put_bytes_result(
        self,
        key: str,
        data: bytes,
        *,
        max_bytes: int | None = None,
    ) -> Any:
        assert max_bytes == len(data)
        self.written.append(key)
        return SimpleNamespace(
            size=len(data),
            created=key not in self.existing_keys,
        )

    def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return True


def _coordinator(
    fake_storage: _Storage,
    capacity: _Capacity | None = None,
) -> StorageWriteCoordinator:
    return StorageWriteCoordinator(
        storage=fake_storage,  # type: ignore[arg-type]
        capacity=capacity or _Capacity(),  # type: ignore[arg-type]
        lease_ttl_seconds=30,
    )


def _processed() -> dict[str, Any]:
    return {
        "video_bytes": b"video-bytes",
        "poster_bytes": b"poster-bytes",
        "width": 1920,
        "height": 1080,
        "duration_ms": 2000,
        "fps": 24.0,
        "has_audio": True,
        "faststart": True,
    }


@pytest.mark.asyncio
async def test_store_assembly_reserves_and_writes_video_and_poster_as_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _Storage()
    capacity = _Capacity()
    storage_writes = _coordinator(fake_storage, capacity)
    ids = iter(["version-1", "video-1"])
    operation_order: list[str] = []
    original_put = fake_storage.put_bytes_result

    def record_put(
        key: str,
        data: bytes,
        *,
        max_bytes: int | None = None,
    ) -> Any:
        operation_order.append(f"write:{key}")
        return original_put(key, data, max_bytes=max_bytes)

    async def complete(
        _claim: storyboard_assembly._AssemblyClaim,  # noqa: SLF001
        _video: Any,
    ) -> bool:
        operation_order.append("complete")
        return True

    monkeypatch.setattr(fake_storage, "put_bytes_result", record_put)
    monkeypatch.setattr(storyboard_assembly, "new_uuid7", lambda: next(ids))
    monkeypatch.setattr(storyboard_assembly, "_complete_assembly", complete)

    video = await storyboard_assembly._store_assembly_result(  # noqa: SLF001
        _claim(),
        processed=_processed(),
        diagnostics={},
        storage_writes=storage_writes,
    )

    expected_keys = {
        "u/user-1/storyboards/run-1/assembly/version-1/output.mp4",
        "u/user-1/storyboards/run-1/assembly/version-1/poster.jpg",
    }
    assert capacity.requests == [2 * (len(b"video-bytes") + len(b"poster-bytes"))]
    assert capacity.lease.release_calls == 1
    assert set(fake_storage.written) == expected_keys
    assert operation_order[-1] == "complete"
    assert video.storage_key in expected_keys
    assert video.poster_storage_key in expected_keys


@pytest.mark.asyncio
async def test_capacity_rejection_happens_before_storyboard_storage_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _Storage()
    capacity = _Capacity(error=StorageCapacityExceeded("full"))
    storage_writes = _coordinator(fake_storage, capacity)
    complete_called = False

    async def complete(
        _claim: storyboard_assembly._AssemblyClaim,  # noqa: SLF001
        _video: Any,
    ) -> bool:
        nonlocal complete_called
        complete_called = True
        return True

    monkeypatch.setattr(storyboard_assembly, "new_uuid7", lambda: "version-1")
    monkeypatch.setattr(storyboard_assembly, "_complete_assembly", complete)

    with pytest.raises(OSError):
        await storyboard_assembly._store_assembly_result(  # noqa: SLF001
            _claim(),
            processed=_processed(),
            diagnostics={},
            storage_writes=storage_writes,
        )

    assert capacity.requests == [2 * (len(b"video-bytes") + len(b"poster-bytes"))]
    assert capacity.lease.release_calls == 0
    assert fake_storage.written == []
    assert fake_storage.deleted == []
    assert complete_called is False


@pytest.mark.asyncio
async def test_partial_storyboard_write_failure_cleans_created_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PartialFailureStorage(_Storage):
        def put_bytes_result(
            self,
            key: str,
            data: bytes,
            *,
            max_bytes: int | None = None,
        ) -> Any:
            assert max_bytes == len(data)
            self.written.append(key)
            if key.endswith("/poster.jpg"):
                raise RuntimeError("poster write failed")
            return SimpleNamespace(size=len(data), created=True)

    fake_storage = PartialFailureStorage()
    capacity = _Capacity()
    storage_writes = _coordinator(fake_storage, capacity)
    monkeypatch.setattr(storyboard_assembly, "new_uuid7", lambda: "version-1")

    with pytest.raises(RuntimeError, match="poster write failed"):
        await storyboard_assembly._store_assembly_result(  # noqa: SLF001
            _claim(),
            processed=_processed(),
            diagnostics={},
            storage_writes=storage_writes,
        )

    video_key = "u/user-1/storyboards/run-1/assembly/version-1/output.mp4"
    assert set(fake_storage.written) == {
        video_key,
        "u/user-1/storyboards/run-1/assembly/version-1/poster.jpg",
    }
    assert fake_storage.deleted == [video_key]
    assert capacity.lease.release_calls == 1


@pytest.mark.asyncio
async def test_cancellation_during_storyboard_write_cleans_only_created_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_key = "u/user-1/storyboards/run-1/assembly/version-1/output.mp4"
    poster_key = "u/user-1/storyboards/run-1/assembly/version-1/poster.jpg"

    class BlockingStorage(_Storage):
        def __init__(self) -> None:
            super().__init__(existing_keys={video_key})
            self.poster_started = threading.Event()
            self.allow_poster = threading.Event()

        def put_bytes_result(
            self,
            key: str,
            data: bytes,
            *,
            max_bytes: int | None = None,
        ) -> Any:
            assert max_bytes == len(data)
            self.written.append(key)
            if key == poster_key:
                self.poster_started.set()
                if not self.allow_poster.wait(timeout=5):
                    raise TimeoutError("poster write did not resume")
            return SimpleNamespace(
                size=len(data),
                created=key not in self.existing_keys,
            )

    fake_storage = BlockingStorage()
    capacity = _Capacity()
    storage_writes = _coordinator(fake_storage, capacity)
    monkeypatch.setattr(storyboard_assembly, "new_uuid7", lambda: "version-1")

    task = asyncio.create_task(
        storyboard_assembly._store_assembly_result(  # noqa: SLF001
            _claim(),
            processed=_processed(),
            diagnostics={},
            storage_writes=storage_writes,
        )
    )
    assert await asyncio.to_thread(fake_storage.poster_started.wait, 2)
    task.cancel()
    fake_storage.allow_poster.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert set(fake_storage.written) == {video_key, poster_key}
    assert fake_storage.deleted == [poster_key]
    assert capacity.lease.release_calls == 1


@pytest.mark.asyncio
async def test_commit_failure_cleans_new_video_and_poster_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _Storage()
    storage_writes = _coordinator(fake_storage)
    ids = iter(["version-1", "video-1"])

    async def fail_commit(
        _claim: storyboard_assembly._AssemblyClaim,  # noqa: SLF001
        _video: Any,
    ) -> bool:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(storyboard_assembly, "new_uuid7", lambda: next(ids))
    monkeypatch.setattr(storyboard_assembly, "_complete_assembly", fail_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        await storyboard_assembly._store_assembly_result(  # noqa: SLF001
            _claim(),
            processed=_processed(),
            diagnostics={},
            storage_writes=storage_writes,
        )

    assert len(fake_storage.written) == 2
    assert set(fake_storage.deleted) == set(fake_storage.written)


@pytest.mark.asyncio
async def test_superseded_completion_cleans_new_video_and_poster_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _Storage()
    storage_writes = _coordinator(fake_storage)
    ids = iter(["version-1", "video-1"])

    async def lose_attempt(
        _claim: storyboard_assembly._AssemblyClaim,  # noqa: SLF001
        _video: Any,
    ) -> bool:
        return False

    monkeypatch.setattr(storyboard_assembly, "new_uuid7", lambda: next(ids))
    monkeypatch.setattr(storyboard_assembly, "_complete_assembly", lose_attempt)

    with pytest.raises(
        storyboard_assembly._AssemblyAttemptLost,  # noqa: SLF001
        match="superseded",
    ):
        await storyboard_assembly._store_assembly_result(  # noqa: SLF001
            _claim(),
            processed=_processed(),
            diagnostics={},
            storage_writes=storage_writes,
        )

    assert set(fake_storage.deleted) == set(fake_storage.written)


@pytest.mark.asyncio
async def test_cancellation_after_storage_write_cleans_new_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_key = "u/user-1/storyboards/run-1/assembly/version-1/output.mp4"
    poster_key = "u/user-1/storyboards/run-1/assembly/version-1/poster.jpg"
    fake_storage = _Storage(existing_keys={video_key})
    storage_writes = _coordinator(fake_storage)
    ids = iter(["version-1", "video-1"])

    async def cancel_commit(
        _claim: storyboard_assembly._AssemblyClaim,  # noqa: SLF001
        _video: Any,
    ) -> bool:
        raise asyncio.CancelledError

    monkeypatch.setattr(storyboard_assembly, "new_uuid7", lambda: next(ids))
    monkeypatch.setattr(storyboard_assembly, "_complete_assembly", cancel_commit)

    with pytest.raises(asyncio.CancelledError):
        await storyboard_assembly._store_assembly_result(  # noqa: SLF001
            _claim(),
            processed=_processed(),
            diagnostics={},
            storage_writes=storage_writes,
        )

    assert set(fake_storage.written) == {video_key, poster_key}
    assert fake_storage.deleted == [poster_key]
