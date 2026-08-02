from __future__ import annotations

import io
import inspect
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.config import settings
from app.routes import admin, me, me_export
from app.services import account_deletion
from fastapi import Request, Response
from lumen_core.constants import (
    CompletionStatus,
    GenerationStatus,
    VideoGenerationStatus,
)
from sqlalchemy.dialects import postgresql


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.rowcount = 0

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


class _AdminDeleteResult(_Result):
    def __init__(
        self,
        rows: list[Any] | None = None,
        *,
        scalar: Any = None,
        rowcount: int = 0,
    ) -> None:
        super().__init__(rows or [])
        self.scalar = scalar
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> Any:
        return self.scalar


class _AdminDeleteDb:
    def __init__(self, responses: list[_AdminDeleteResult]) -> None:
        self.responses = responses
        self.committed = False

    async def execute(self, _statement: Any) -> _AdminDeleteResult:
        return self.responses.pop(0)

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


def test_export_message_record_strips_internal_fields_but_keeps_memory_writes() -> None:
    record = me._export_message_record(  # noqa: SLF001
        SimpleNamespace(
            conversation_id="conv-1",
            id="message-1",
            role="assistant",
            content={
                "text": "public",
                "memory_writes": [{"kind": "added", "id": "memory-1"}],
                "_memory_extraction": {"owner": "private"},
                "_future_internal": {"secret": True},
            },
            intent="chat",
            status="succeeded",
            created_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        )
    )

    assert record["content"] == {
        "text": "public",
        "memory_writes": [{"kind": "added", "id": "memory-1"}],
    }
    assert record["created_at"] == "2026-07-18T00:00:00+00:00"


@pytest.mark.asyncio
async def test_export_route_rolls_back_before_storage_and_zip_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class ActiveUserResult:
        def scalar_one_or_none(self) -> str:
            return "user-1"

    class Db:
        transaction_open = False

        async def execute(self, _statement: Any) -> ActiveUserResult:
            self.transaction_open = True
            events.append("active-user")
            return ActiveUserResult()

        async def rollback(self) -> None:
            self.transaction_open = False
            events.append("rollback")

    db = Db()
    tempfiles: list[io.BytesIO] = []

    async def check_rate_limit(_redis: Any, key: str) -> None:
        assert key == "rl:me:export:user-1"
        assert db.transaction_open is False
        events.append("rate-limit")

    async def build_export_archive(
        session: Db,
        tmp: io.BytesIO,
        user_id: str,
    ) -> Any:
        assert session is db
        assert user_id == "user-1"
        assert db.transaction_open is False
        events.append("build")
        tmp.write(b"zip-data")
        return me_export.ExportStats(
            messages=501,
            images=2,
            images_skipped=1,
            zip_bytes=8,
        )

    async def write_export_audit(session: Db, **kwargs: Any) -> bool:
        assert session is db
        assert db.transaction_open is False
        assert kwargs["autocommit"] is True
        assert kwargs["event_type"] == "me.data.export"
        assert kwargs["user_id"] == "user-1"
        assert kwargs["actor_email"] == "user@example.com"
        assert kwargs["target_user_id"] == "user-1"
        assert kwargs["details"] == {
            "messages": 501,
            "images": 2,
            "images_skipped": 1,
            "zip_bytes": 8,
        }
        events.append("audit")
        return True

    def temporary_file() -> io.BytesIO:
        tmp = io.BytesIO()
        tempfiles.append(tmp)
        return tmp

    monkeypatch.setattr(me._EXPORT_LIMITER, "check", check_rate_limit)
    monkeypatch.setattr(me, "get_redis", object)
    monkeypatch.setattr(me, "_build_export_archive", build_export_archive)
    monkeypatch.setattr(me, "write_audit", write_export_audit)
    monkeypatch.setattr(me.tempfile, "TemporaryFile", temporary_file)

    response = await me.export_my_data(
        request=Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/me/export",
                "headers": [],
                "client": ("127.0.0.1", 1234),
            }
        ),
        user=SimpleNamespace(id="user-1", email="user@example.com"),
        db=db,  # type: ignore[arg-type]
    )

    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body == b"zip-data"
    assert tempfiles[0].closed is True
    assert response.headers["content-length"] == "8"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="lumen-export-user-1-'
    )
    assert events == [
        "active-user",
        "rollback",
        "rate-limit",
        "build",
        "audit",
    ]


@pytest.mark.asyncio
async def test_export_batch_iterators_cap_batches_and_rollback_before_yield() -> None:
    created_at = datetime(2026, 7, 18, tzinfo=timezone.utc)
    messages = [
        SimpleNamespace(
            conversation_id="conv-1",
            id=f"message-{index:04d}",
            role="assistant",
            content={"text": str(index)},
            intent="chat",
            status="succeeded",
            created_at=created_at + timedelta(microseconds=index),
        )
        for index in range(501)
    ]
    images = [
        SimpleNamespace(
            id=f"image-{index:04d}",
            storage_key=f"u/user-1/image-{index:04d}.png",
            mime="image/png",
            created_at=created_at + timedelta(microseconds=index),
        )
        for index in range(501)
    ]

    class RowsResult:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def all(self) -> list[Any]:
            return self._rows

    class Db:
        def __init__(self) -> None:
            self.transaction_open = False
            self.rollback_count = 0
            self.limits: list[int] = []
            self._responses = [
                messages[:500],
                messages[500:],
                [],
                images[:500],
                images[500:],
                [],
            ]

        async def execute(self, statement: Any) -> RowsResult:
            self.transaction_open = True
            self.limits.append(statement._limit_clause.value)
            return RowsResult(self._responses.pop(0))

        async def rollback(self) -> None:
            self.transaction_open = False
            self.rollback_count += 1

    db = Db()
    message_batch_sizes: list[int] = []
    async for batch in me_export.iter_export_message_batches(
        db,  # type: ignore[arg-type]
        "user-1",
    ):
        assert db.transaction_open is False
        message_batch_sizes.append(len(batch))

    image_batch_sizes: list[int] = []
    async for batch in me_export.iter_export_image_batches(
        db,  # type: ignore[arg-type]
        "user-1",
    ):
        assert db.transaction_open is False
        image_batch_sizes.append(len(batch))

    assert inspect.isasyncgenfunction(me_export.iter_export_message_batches)
    assert inspect.isasyncgenfunction(me_export.iter_export_image_batches)
    assert message_batch_sizes == [500, 1]
    assert image_batch_sizes == [500, 1]
    assert db.limits == [500] * 6
    assert db.rollback_count == 6


@pytest.mark.asyncio
async def test_export_batches_write_outside_transactions_and_preserve_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_at = datetime(2026, 7, 18, tzinfo=timezone.utc)
    message = SimpleNamespace(
        conversation_id="conv-1",
        id="message-1",
        role="assistant",
        content={
            "text": "public",
            "memory_writes": [{"kind": "added", "id": "memory-1"}],
            "_memory_extraction": {"owner": "private"},
        },
        intent="chat",
        status="succeeded",
        created_at=created_at,
    )
    image = SimpleNamespace(
        id="image-1",
        storage_key="u/user-1/image.png",
        mime="image/png",
        created_at=created_at,
    )

    class RowsResult:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def all(self) -> list[Any]:
            return self._rows

    class Db:
        def __init__(self) -> None:
            self.transaction_open = False
            self._responses = [[message], [], [image], []]

        async def execute(self, _statement: Any) -> RowsResult:
            self.transaction_open = True
            return RowsResult(self._responses.pop(0))

        async def rollback(self) -> None:
            self.transaction_open = False

    db = Db()

    class GuardedBuffer(io.BytesIO):
        def _assert_transaction_closed(self) -> None:
            assert db.transaction_open is False

        def flush(self) -> None:
            self._assert_transaction_closed()
            super().flush()

        def seek(self, *args: Any, **kwargs: Any) -> int:
            self._assert_transaction_closed()
            return super().seek(*args, **kwargs)

        def tell(self) -> int:
            self._assert_transaction_closed()
            return super().tell()

        def write(self, data: bytes) -> int:
            self._assert_transaction_closed()
            return super().write(data)

    class GuardedReader(io.BytesIO):
        def read(self, *args: Any, **kwargs: Any) -> bytes:
            assert db.transaction_open is False
            return super().read(*args, **kwargs)

    image_missing = SimpleNamespace(
        id="image-missing",
        storage_key="u/user-1/missing.png",
        mime="image/png",
        created_at=created_at + timedelta(microseconds=1),
    )
    image_escape = SimpleNamespace(
        id="image-escape",
        storage_key="../outside.png",
        mime="image/png",
        created_at=created_at + timedelta(microseconds=2),
    )
    db._responses[2].extend([image_missing, image_escape])

    def open_storage_file(storage_key: str | None) -> io.BytesIO | None:
        assert db.transaction_open is False
        if storage_key == "u/user-1/image.png":
            return GuardedReader(b"image-data")
        return None

    monkeypatch.setattr(me_export, "open_storage_file_safe", open_storage_file)

    archive_file = GuardedBuffer()
    stats = await me_export.build_export_archive(
        db,  # type: ignore[arg-type]
        archive_file,
        "user-1",
    )

    archive_file.seek(0)
    with zipfile.ZipFile(archive_file) as archive:
        message_record = json.loads(archive.read("messages.ndjson"))
        assert archive.namelist() == ["messages.ndjson", "images/image-1.png"]
        assert archive.read("images/image-1.png") == b"image-data"

    assert message_record == {
        "conversation_id": "conv-1",
        "id": "message-1",
        "role": "assistant",
        "content": {
            "text": "public",
            "memory_writes": [{"kind": "added", "id": "memory-1"}],
        },
        "intent": "chat",
        "status": "succeeded",
        "created_at": "2026-07-18T00:00:00+00:00",
    }
    assert stats == me_export.ExportStats(
        messages=1,
        images=1,
        images_skipped=2,
        zip_bytes=len(archive_file.getvalue()),
    )


@pytest.mark.asyncio
async def test_cancel_account_memory_extractions_fences_active_runs() -> None:
    class Db:
        def __init__(self) -> None:
            self.statement: Any = None

        async def execute(self, statement: Any) -> SimpleNamespace:
            self.statement = statement
            return SimpleNamespace(rowcount=3)

    db = Db()
    canceled_at = datetime.now(timezone.utc)
    count = await account_deletion.cancel_account_memory_extractions(
        db,  # type: ignore[arg-type]
        user_id="user-1",
        canceled_at=canceled_at,
    )

    rendered = str(db.statement.compile(dialect=postgresql.dialect()))
    assert count == 3
    assert "UPDATE memory_extraction_runs" in rendered
    assert "memory_extraction_runs.user_id" in rendered
    assert "memory_extraction_runs.status IN" in rendered
    assert "memory_extraction_runs.fence + " in rendered


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
    video_queued = SimpleNamespace(
        id="video-queued",
        status=VideoGenerationStatus.QUEUED.value,
        cancel_requested_at=None,
    )
    existing_video_cancel = datetime(2026, 7, 30, tzinfo=timezone.utc)
    video_running = SimpleNamespace(
        id="video-running",
        status=VideoGenerationStatus.RUNNING.value,
        cancel_requested_at=existing_video_cancel,
    )
    db = _Db(
        [
            [gen_queued, gen_running],
            [comp_queued, comp_streaming],
            [video_queued, video_running],
        ]
    )
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
        account_deletion,
        "_release_account_delete_task_hold",
        release_account_delete_task_hold,
    )
    monkeypatch.setattr(
        account_deletion,
        "_account_wallet_exists",
        lambda *_args, **_kwargs: False,
    )

    cleanup = await account_deletion.cancel_account_active_tasks(
        db,  # type: ignore[arg-type]
        user_id="user-1",
        canceled_at=datetime.now(timezone.utc),
    )

    assert cleanup == {
        "generations_canceled": 2,
        "completions_canceled": 2,
        "video_generations_canceled": 2,
        "videos_deleted": 0,
        "memory_extractions_canceled": 0,
        "holds_released": 2,
        "task_ids": [
            "gen-queued",
            "gen-running",
            "comp-queued",
            "comp-streaming",
        ],
        "queued_generation_ids": ["gen-queued"],
        "queued_generation_execution_epochs": {"gen-queued": 0},
        "queued_generation_queue_tokens": {},
        "running_generation_ids": ["gen-running"],
        "streaming_completion_ids": ["comp-streaming"],
        "deferred_generation_ids": [],
        "deferred_completion_ids": [],
        "active_video_generation_ids": ["video-queued", "video-running"],
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
    assert video_queued.status == VideoGenerationStatus.QUEUED.value
    assert video_running.status == VideoGenerationStatus.RUNNING.value
    assert video_queued.cancel_requested_at is not None
    assert video_running.cancel_requested_at == existing_video_cancel


@pytest.mark.asyncio
async def test_admin_delete_fences_video_work_and_tombstones_videos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = SimpleNamespace(
        id="user-admin-delete",
        email="member@example.com",
        account_mode="byok",
        deleted_at=None,
    )
    video_generation = SimpleNamespace(
        id="video-generation-admin-delete",
        status=VideoGenerationStatus.SUBMITTED.value,
        cancel_requested_at=None,
    )
    db = _AdminDeleteDb(
        [
            _AdminDeleteResult(scalar=target),
            _AdminDeleteResult(rowcount=1),
            _AdminDeleteResult(rowcount=1),
            _AdminDeleteResult(rowcount=1),
            _AdminDeleteResult(),
            _AdminDeleteResult(),
            _AdminDeleteResult(rows=[video_generation]),
            _AdminDeleteResult(rowcount=1),
            _AdminDeleteResult(rowcount=0),
        ]
    )
    audits: list[dict[str, Any]] = []
    post_commit: list[dict[str, Any]] = []

    async def write_admin_audit(*_args: Any, **kwargs: Any) -> None:
        audits.append(kwargs)

    async def post_commit_account_task_cleanup(
        *_args: Any,
        **kwargs: Any,
    ) -> None:
        post_commit.append(kwargs)

    monkeypatch.setattr(admin, "write_admin_audit", write_admin_audit)
    monkeypatch.setattr(
        admin,
        "post_commit_account_task_cleanup",
        post_commit_account_task_cleanup,
    )

    out = await admin.delete_user(
        target.id,
        SimpleNamespace(),
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        db,  # type: ignore[arg-type]
    )

    assert out == {"ok": True}
    assert target.deleted_at is not None
    assert video_generation.cancel_requested_at is not None
    assert db.committed is True
    assert audits[0]["event_type"] == "admin.user.delete"
    assert post_commit == [
        {
            "user_id": target.id,
            "cleanup": {
                "generations_canceled": 0,
                "completions_canceled": 0,
                "video_generations_canceled": 1,
                "videos_deleted": 1,
                "memory_extractions_canceled": 0,
                "holds_released": 0,
                "task_ids": [],
                "queued_generation_ids": [],
                "queued_generation_execution_epochs": {},
                "queued_generation_queue_tokens": {},
                "running_generation_ids": [],
                "streaming_completion_ids": [],
                "deferred_generation_ids": [],
                "deferred_completion_ids": [],
                "active_video_generation_ids": [video_generation.id],
            },
        }
    ]


@pytest.mark.asyncio
async def test_delete_my_account_keeps_audit_detail_and_post_commit_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(
        id="user-self-delete",
        email="member@example.com",
        account_mode="wallet",
    )
    db = _AdminDeleteDb(
        [
            _AdminDeleteResult(scalar=user.id),
            _AdminDeleteResult(rowcount=1),
            _AdminDeleteResult(rowcount=2),
            _AdminDeleteResult(rowcount=3),
            _AdminDeleteResult(rowcount=4),
        ]
    )
    cleanup = {
        "generations_canceled": 5,
        "completions_canceled": 6,
        "video_generations_canceled": 7,
        "videos_deleted": 8,
        "memory_extractions_canceled": 9,
    }
    audits: list[dict[str, Any]] = []
    post_commit: list[tuple[dict[str, Any], bool]] = []

    async def cancel_account_active_tasks(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["queue_redis"] is queue_redis
        return cleanup

    async def write_audit(*_args: Any, **kwargs: Any) -> None:
        audits.append(kwargs)

    async def post_commit_account_task_cleanup(**kwargs: Any) -> None:
        post_commit.append((kwargs, db.committed))

    queue_redis = object()
    monkeypatch.setattr(me, "get_redis", lambda: queue_redis)
    monkeypatch.setattr(me, "cancel_account_active_tasks", cancel_account_active_tasks)
    monkeypatch.setattr(me, "write_audit", write_audit)
    monkeypatch.setattr(
        me,
        "post_commit_account_task_cleanup",
        post_commit_account_task_cleanup,
    )

    response = Response()
    out = await me.delete_my_account(
        None,  # type: ignore[arg-type]
        user,
        response,
        db,  # type: ignore[arg-type]
    )

    assert out is response
    assert response.status_code == 204
    assert db.committed is True
    assert audits == [
        {
            "event_type": "me.account.delete",
            "user_id": user.id,
            "actor_email": user.email,
            "actor_ip_hash": None,
            "target_user_id": user.id,
            "details": {
                "users": 1,
                "sessions_revoked": 2,
                "conversations_deleted": 3,
                "images_deleted": 4,
                "generations_canceled": 5,
                "completions_canceled": 6,
                "video_generations_canceled": 7,
                "videos_deleted": 8,
                "memory_extractions_canceled": 9,
            },
        }
    ]
    assert post_commit == [({"user_id": user.id, "cleanup": cleanup}, True)]
    cookie_headers = [
        value.decode("latin-1")
        for key, value in response.raw_headers
        if key == b"set-cookie"
    ]
    assert any(header.startswith("session=") for header in cookie_headers)
    assert any(header.startswith("csrf=") for header in cookie_headers)


@pytest.mark.asyncio
async def test_cancel_account_active_tasks_defers_receipt_bearing_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = SimpleNamespace(
        id="gen-retry",
        status=GenerationStatus.QUEUED.value,
        execution_epoch=11,
        cancel_requested_at=None,
        upstream_request={
            "upstream_response_received_at": "2026-07-30T00:00:01+00:00",
            "upstream_response_attempt": 2,
            "upstream_response_execution_epoch": 11,
            "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
            "upstream_dispatch_attempt": 2,
            "upstream_dispatch_execution_epoch": 11,
        },
    )
    comp = SimpleNamespace(
        id="comp-retry",
        status=CompletionStatus.QUEUED.value,
        execution_epoch=12,
        cancel_requested_at=None,
        tokens_in=256,
        upstream_request={"completion_usage_execution_epoch": 12},
    )
    db = _Db([[gen], [comp]])
    released: list[str] = []

    async def release_account_delete_task_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
        return True

    monkeypatch.setattr(
        account_deletion,
        "_release_account_delete_task_hold",
        release_account_delete_task_hold,
    )

    cleanup = await account_deletion.cancel_account_active_tasks(
        db,  # type: ignore[arg-type]
        user_id="user-1",
        canceled_at=datetime.now(timezone.utc),
    )

    assert cleanup["holds_released"] == 0
    assert cleanup["queued_generation_ids"] == []
    assert cleanup["deferred_generation_ids"] == ["gen-retry"]
    assert cleanup["deferred_completion_ids"] == ["comp-retry"]
    assert gen.status == GenerationStatus.QUEUED.value
    assert comp.status == CompletionStatus.QUEUED.value
    assert gen.cancel_requested_at is not None
    assert comp.cancel_requested_at is not None
    assert released == []


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
        account_deletion,
        "_release_account_delete_task_hold",
        release_account_delete_task_hold,
    )

    async def wallet_exists(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(account_deletion, "_account_wallet_exists", wallet_exists)

    cleanup = await account_deletion.cancel_account_active_tasks(
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

    async def release_generation_queue_state(
        _redis: Redis,
        task_id: str,
        *,
        expected_execution_epoch: int,
        ownership_token: Any,
    ) -> bool:
        assert ownership_token.provider_name == "provider-13"
        queue_released.append((f"{task_id}:{expected_execution_epoch}", db.committed))
        return True

    monkeypatch.setattr(account_deletion, "get_redis", lambda: Redis())
    monkeypatch.setattr(
        account_deletion,
        "invalidate_balance_cache",
        invalidate_balance_cache,
    )
    monkeypatch.setattr(
        account_deletion,
        "_release_account_generation_queue_state",
        release_generation_queue_state,
    )

    await db.commit()
    await account_deletion.post_commit_account_task_cleanup(
        user_id="user-1",
        cleanup={
            "holds_released": 1,
            "queued_generation_ids": ["gen-queued"],
            "queued_generation_execution_epochs": {"gen-queued": 13},
            "queued_generation_queue_tokens": {
                "gen-queued": {
                    "task_id": "gen-queued",
                    "execution_epoch": 13,
                    "provider_name": "provider-13",
                    "lease_token": "worker:execution:13:attempt:1",
                    "reservation_token": "reservation-13",
                }
            },
            "running_generation_ids": ["gen-running"],
            "streaming_completion_ids": ["comp-1"],
            "deferred_generation_ids": ["gen-deferred"],
            "deferred_completion_ids": ["comp-deferred"],
        },
    )

    assert invalidated == [("user-1", True)]
    assert queue_released == [("gen-queued:13", True)]
    assert redis_calls == [
        ("task:gen-running:cancel", "1", 3600),
        ("task:comp-1:cancel", "1", 3600),
        ("task:gen-deferred:cancel", "1", 3600),
        ("task:comp-deferred:cancel", "1", 3600),
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

    async def release_generation_queue_state(
        _redis: Redis,
        task_id: str,
        *,
        expected_execution_epoch: int,
        ownership_token: Any,
    ) -> bool:
        assert ownership_token.provider_name == "provider-14"
        queue_released.append(f"{task_id}:{expected_execution_epoch}")
        return True

    monkeypatch.setattr(account_deletion, "get_redis", lambda: Redis())
    monkeypatch.setattr(
        account_deletion,
        "invalidate_balance_cache",
        invalidate_balance_cache,
    )
    monkeypatch.setattr(
        account_deletion,
        "_release_account_generation_queue_state",
        release_generation_queue_state,
    )

    await account_deletion.post_commit_account_task_cleanup(
        user_id="user-1",
        cleanup={
            "holds_released": 1,
            "queued_generation_ids": ["gen-queued"],
            "queued_generation_execution_epochs": {"gen-queued": 14},
            "queued_generation_queue_tokens": {
                "gen-queued": {
                    "task_id": "gen-queued",
                    "execution_epoch": 14,
                    "provider_name": "provider-14",
                    "lease_token": "worker:execution:14:attempt:1",
                    "reservation_token": "reservation-14",
                }
            },
            "running_generation_ids": ["gen-running"],
            "streaming_completion_ids": ["comp-1"],
        },
    )

    assert queue_released == ["gen-queued:14"]
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

    monkeypatch.setattr(account_deletion, "get_redis", get_redis)
    monkeypatch.setattr(
        account_deletion,
        "invalidate_balance_cache",
        invalidate_balance_cache,
    )

    await account_deletion.post_commit_account_task_cleanup(
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
