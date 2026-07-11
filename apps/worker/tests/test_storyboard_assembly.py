from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.tasks import storyboard_assembly


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
async def test_pending_to_compositing_cas_allows_only_one_concurrent_claim(
    pending_status: str,
) -> None:
    state: dict[str, Any] = {"status": pending_status, "claimed": False}
    lock = asyncio.Lock()
    statements: list[Any] = []

    class Result:
        def __init__(self, rowcount: int) -> None:
            self.rowcount = rowcount

    class Session:
        async def execute(self, statement: Any) -> Result:
            statements.append(statement)
            async with lock:
                await asyncio.sleep(0)
                if state["claimed"]:
                    return Result(0)
                state["claimed"] = True
                state["status"] = "compositing"
                return Result(1)

    output = {
        "assembly_attempt_token": "attempt-1",
        "assembly_fingerprint": "fingerprint-1",
    }
    claimed = await asyncio.gather(
        *(
            storyboard_assembly._claim_waiting_assembly(  # noqa: SLF001
                Session(),
                step_id="assembly-1",
                attempt_token="attempt-1",
                fingerprint="fingerprint-1",
                output_json=output,
                status=pending_status,
            )
            for _ in range(2)
        )
    )

    assert sorted(claimed) == [False, True]
    assert state["status"] == "compositing"
    compiled = statements[0].compile(dialect=postgresql.dialect())
    rendered = str(compiled)
    assert "workflow_steps.status" in rendered
    assert "assembly_attempt_token" in compiled.params.values()
    assert "assembly_fingerprint" in compiled.params.values()
    assert "assembly_claimed_at" in compiled.params.values()
    assert pending_status in compiled.params.values()


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
    lock = asyncio.Lock()
    claimed = False
    concat_calls = 0

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

    async def publish(*_args: Any, **_kwargs: Any) -> None:
        return None

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
    ) -> Any:
        assert processed["video_bytes"] == b"video"
        assert diagnostics == {}
        return SimpleNamespace(id="video-1")

    monkeypatch.setattr(storyboard_assembly, "_claim_assembly", claim_once)
    monkeypatch.setattr(storyboard_assembly, "_publish", publish)
    monkeypatch.setattr(storyboard_assembly, "_load_segment_paths", load_paths)
    monkeypatch.setattr(storyboard_assembly, "_concat_segments_sync", concat)
    monkeypatch.setattr(storyboard_assembly, "_postprocess_video_bytes", postprocess)
    monkeypatch.setattr(storyboard_assembly, "_store_assembly_result", store)

    await asyncio.gather(
        storyboard_assembly.run_storyboard_assembly({"redis": object()}, "run-1"),
        storyboard_assembly.run_storyboard_assembly({"redis": object()}, "run-1"),
    )

    assert concat_calls == 1


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


class _Storage:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.deleted: list[str] = []

    def put_bytes_result(self, key: str, data: bytes) -> Any:
        self.written.append(key)
        return SimpleNamespace(size=len(data), created=True)

    def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return True


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
async def test_commit_failure_cleans_new_video_and_poster_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _Storage()
    ids = iter(["version-1", "video-1"])

    async def fail_commit(
        _claim: storyboard_assembly._AssemblyClaim,  # noqa: SLF001
        _video: Any,
    ) -> bool:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(storyboard_assembly, "storage", fake_storage)
    monkeypatch.setattr(storyboard_assembly, "new_uuid7", lambda: next(ids))
    monkeypatch.setattr(storyboard_assembly, "_complete_assembly", fail_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        await storyboard_assembly._store_assembly_result(  # noqa: SLF001
            _claim(),
            processed=_processed(),
            diagnostics={},
        )

    assert len(fake_storage.written) == 2
    assert set(fake_storage.deleted) == set(fake_storage.written)


@pytest.mark.asyncio
async def test_superseded_completion_cleans_new_video_and_poster_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _Storage()
    ids = iter(["version-1", "video-1"])

    async def lose_attempt(
        _claim: storyboard_assembly._AssemblyClaim,  # noqa: SLF001
        _video: Any,
    ) -> bool:
        return False

    monkeypatch.setattr(storyboard_assembly, "storage", fake_storage)
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
        )

    assert set(fake_storage.deleted) == set(fake_storage.written)


@pytest.mark.asyncio
async def test_cancellation_after_storage_write_cleans_new_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = _Storage()
    ids = iter(["version-1", "video-1"])

    async def cancel_commit(
        _claim: storyboard_assembly._AssemblyClaim,  # noqa: SLF001
        _video: Any,
    ) -> bool:
        raise asyncio.CancelledError

    monkeypatch.setattr(storyboard_assembly, "storage", fake_storage)
    monkeypatch.setattr(storyboard_assembly, "new_uuid7", lambda: next(ids))
    monkeypatch.setattr(storyboard_assembly, "_complete_assembly", cancel_commit)

    with pytest.raises(asyncio.CancelledError):
        await storyboard_assembly._store_assembly_result(  # noqa: SLF001
            _claim(),
            processed=_processed(),
            diagnostics={},
        )

    assert set(fake_storage.deleted) == set(fake_storage.written)
