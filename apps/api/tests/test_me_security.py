from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.config import settings
from app.routes import me
from lumen_core.constants import CompletionStatus, GenerationStatus


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self.rows


class _Db:
    def __init__(self, responses: list[list[Any]] | None = None) -> None:
        self.responses = responses or []
        self.committed = False

    async def execute(self, _statement: Any) -> _Result:
        return _Result(self.responses.pop(0) if self.responses else [])

    async def commit(self) -> None:
        self.committed = True


def test_export_storage_path_stays_under_storage_root(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    old_root = settings.storage_root
    settings.storage_root = str(root)
    try:
        assert (
            me._fs_path_safe("u/user_1/image.png")
            == (root / "u/user_1/image.png").resolve()
        )
        assert me._fs_path_safe("") is None
        assert me._fs_path_safe("   ") is None
        assert me._fs_path_safe("bad\x00name.png") is None
        assert me._fs_path_safe(str(root / "u/user_1/image.png")) is None
        assert me._fs_path_safe("../storage_sibling/image.png") is None
        assert me._fs_path_safe("/u/user_1/image.png") is None
    finally:
        settings.storage_root = old_root


def test_export_storage_path_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    old_root = settings.storage_root
    settings.storage_root = str(root)
    try:
        assert me._fs_path_safe("link/image.png") is None
    finally:
        settings.storage_root = old_root


def test_open_storage_file_safe_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    fifo = root / "pipe"
    os.mkfifo(fifo)
    old_root = settings.storage_root
    settings.storage_root = str(root)
    try:
        assert me._open_storage_file_safe("pipe") is None
    finally:
        settings.storage_root = old_root


def test_export_tempfile_iterator_closes_on_early_close() -> None:
    tmp = io.BytesIO(b"export-data")
    gen = me._iter_tempfile_and_close(tmp)

    assert next(gen) == b"export-data"
    gen.close()

    assert tmp.closed is True


@pytest.mark.asyncio
async def test_cancel_account_active_tasks_releases_only_queued_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen_queued = SimpleNamespace(
        id="gen-queued",
        status=GenerationStatus.QUEUED.value,
        billing_retry_count=1,
    )
    gen_running = SimpleNamespace(
        id="gen-running",
        status=GenerationStatus.RUNNING.value,
    )
    comp_queued = SimpleNamespace(
        id="comp-queued",
        status=CompletionStatus.QUEUED.value,
        upstream_request={"billing_retry_count": 1},
    )
    comp_streaming = SimpleNamespace(
        id="comp-streaming",
        status=CompletionStatus.STREAMING.value,
    )
    db = _Db([[gen_queued, gen_running], [comp_queued, comp_streaming]])
    released: list[dict[str, Any]] = []

    async def release_account_delete_task_hold(
        db: _Db,
        *,
        user_id: str,
        ref_type: str,
        ref_id: str,
    ) -> bool:
        released.append(
            {
                "committed": db.committed,
                "user_id": user_id,
                "ref_type": ref_type,
                "ref_id": ref_id,
            }
        )
        return True

    monkeypatch.setattr(
        me,
        "_release_account_delete_task_hold",
        release_account_delete_task_hold,
    )
    monkeypatch.setattr(me, "_account_wallet_exists", lambda *_args, **_kwargs: False)

    cleanup = await me._cancel_account_active_tasks(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        user_id="user-1",
        canceled_at=datetime.now(timezone.utc),
    )

    assert cleanup == {
        "generations_canceled": 2,
        "completions_canceled": 2,
        "holds_released": 2,
        "task_ids": [
            "gen-queued",
            "gen-running",
            "comp-queued",
            "comp-streaming",
        ],
        "queued_generation_ids": ["gen-queued"],
        "running_generation_ids": ["gen-running"],
        "streaming_completion_ids": ["comp-streaming"],
    }
    assert [call["ref_id"] for call in released] == [
        "gen-queued:retry:1",
        "comp-queued:retry:1",
    ]
    assert all(call["committed"] is False for call in released)
    assert gen_queued.status == GenerationStatus.CANCELED.value
    assert gen_running.status == GenerationStatus.RUNNING.value
    assert comp_queued.status == CompletionStatus.CANCELED.value
    assert comp_streaming.status == CompletionStatus.STREAMING.value


@pytest.mark.asyncio
async def test_cancel_account_active_tasks_skips_holds_for_byok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = SimpleNamespace(id="gen-1", status=GenerationStatus.RUNNING.value)
    comp = SimpleNamespace(id="comp-1", status=CompletionStatus.STREAMING.value)
    db = _Db([[gen], [comp]])
    released: list[str] = []

    async def release_account_delete_task_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
        return True

    monkeypatch.setattr(
        me,
        "_release_account_delete_task_hold",
        release_account_delete_task_hold,
    )
    async def wallet_exists(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(me, "_account_wallet_exists", wallet_exists)

    cleanup = await me._cancel_account_active_tasks(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        user_id="user-1",
        canceled_at=datetime.now(timezone.utc),
        account_mode="byok",
    )

    assert cleanup["holds_released"] == 0
    assert released == []
    assert gen.status == GenerationStatus.RUNNING.value
    assert comp.status == CompletionStatus.STREAMING.value


@pytest.mark.asyncio
async def test_post_commit_account_task_cleanup_runs_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _Db()
    invalidated: list[tuple[str, bool]] = []
    redis_calls: list[tuple[str, str, int]] = []
    queue_released: list[tuple[str, bool]] = []

    class Redis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            redis_calls.append((key, value, ex))

    async def invalidate_balance_cache(user_id: str) -> None:
        invalidated.append((user_id, db.committed))

    async def release_generation_queue_state(_redis: Redis, task_id: str) -> None:
        queue_released.append((task_id, db.committed))

    monkeypatch.setattr(me, "get_redis", lambda: Redis())
    monkeypatch.setattr(me, "invalidate_balance_cache", invalidate_balance_cache)
    monkeypatch.setattr(
        me,
        "_release_account_generation_queue_state",
        release_generation_queue_state,
    )

    await db.commit()
    await me._post_commit_account_task_cleanup(  # noqa: SLF001
        user_id="user-1",
        cleanup={
            "holds_released": 1,
            "queued_generation_ids": ["gen-queued"],
            "running_generation_ids": ["gen-running"],
            "streaming_completion_ids": ["comp-1"],
        },
    )

    assert invalidated == [("user-1", True)]
    assert queue_released == [("gen-queued", True)]
    assert redis_calls == [
        ("task:gen-running:cancel", "1", 3600),
        ("task:comp-1:cancel", "1", 3600),
    ]


@pytest.mark.asyncio
async def test_post_commit_account_task_cleanup_keeps_cancel_when_cache_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_calls: list[tuple[str, str, int]] = []
    queue_released: list[str] = []

    class Redis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            redis_calls.append((key, value, ex))

    async def invalidate_balance_cache(_user_id: str) -> None:
        raise RuntimeError("cache unavailable")

    async def release_generation_queue_state(_redis: Redis, task_id: str) -> None:
        queue_released.append(task_id)

    monkeypatch.setattr(me, "get_redis", lambda: Redis())
    monkeypatch.setattr(me, "invalidate_balance_cache", invalidate_balance_cache)
    monkeypatch.setattr(
        me,
        "_release_account_generation_queue_state",
        release_generation_queue_state,
    )

    await me._post_commit_account_task_cleanup(  # noqa: SLF001
        user_id="user-1",
        cleanup={
            "holds_released": 1,
            "queued_generation_ids": ["gen-queued"],
            "running_generation_ids": ["gen-running"],
            "streaming_completion_ids": ["comp-1"],
        },
    )

    assert queue_released == ["gen-queued"]
    assert redis_calls == [
        ("task:gen-running:cancel", "1", 3600),
        ("task:comp-1:cancel", "1", 3600),
    ]


@pytest.mark.asyncio
async def test_post_commit_account_task_cleanup_invalidates_hold_only_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidated: list[str] = []
    redis_requested = False

    async def invalidate_balance_cache(user_id: str) -> None:
        invalidated.append(user_id)

    def get_redis() -> object:
        nonlocal redis_requested
        redis_requested = True
        return object()

    monkeypatch.setattr(me, "get_redis", get_redis)
    monkeypatch.setattr(me, "invalidate_balance_cache", invalidate_balance_cache)

    await me._post_commit_account_task_cleanup(  # noqa: SLF001
        user_id="user-1",
        cleanup={
            "holds_released": 1,
            "queued_generation_ids": [],
            "running_generation_ids": [],
            "streaming_completion_ids": [],
        },
    )

    assert invalidated == ["user-1"]
    assert redis_requested is False
