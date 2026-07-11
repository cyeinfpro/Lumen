from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.routes import admin
from lumen_core.models import OutboxDeadLetter, OutboxEvent


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalars(self) -> Any:
        return self.value


class _Db:
    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.commits = 0
        self.added: list[Any] = []
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.results.pop(0))

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_dlq_retry_restages_outbox_without_premature_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = OutboxEvent(
        id="outbox-1",
        kind="generation",
        payload={"task_id": "gen-1"},
        published_at=datetime.now(timezone.utc),
    )
    dlq = OutboxDeadLetter(
        id="dlq-1",
        outbox_id=outbox.id,
        event_type="outbox.generation",
        payload={"task_id": "gen-1", "user_id": "user-1"},
        error_class="OutboxEnqueueFailed",
        error_message="max_fail_count",
        retry_count=5,
        resolved_at=None,
    )
    db = _Db([dlq, "gen-1", outbox])

    async def fake_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(admin, "write_admin_audit", fake_audit)

    result = await admin.retry_dlq(
        "dlq-1",
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(id="admin-1", email="admin@example.test"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert result["requeued"] is True
    assert result["resolved"] is False
    assert outbox.published_at is None
    assert outbox.payload["outbox_id"] == "outbox-1"
    assert dlq.resolved_at is None
    assert dlq.retry_count == 6
    assert db.commits == 1


@pytest.mark.asyncio
async def test_dlq_retry_rejects_unsupported_type_without_resolving() -> None:
    dlq = OutboxDeadLetter(
        id="dlq-1",
        event_type="custom.unknown",
        payload={},
        error_class="OutboxPublishFailed",
        retry_count=0,
        resolved_at=None,
    )
    db = _Db([dlq])

    with pytest.raises(HTTPException) as exc_info:
        await admin.retry_dlq(
            "dlq-1",
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(id="admin-1", email="admin@example.test"),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"]["code"] == "unsupported_event_type"
    assert dlq.resolved_at is None


@pytest.mark.asyncio
async def test_dlq_sweep_pages_all_cleanable_kinds_without_starvation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_at = datetime(2026, 7, 11, tzinfo=timezone.utc)

    def dlq(
        dlq_id: str,
        event_type: str,
        payload: dict[str, str],
        *,
        offset: int,
    ) -> OutboxDeadLetter:
        return OutboxDeadLetter(
            id=dlq_id,
            event_type=event_type,
            payload=payload,
            error_class="OutboxEnqueueFailed",
            error_message="max_fail_count",
            retry_count=5,
            failed_at=failed_at.replace(microsecond=offset),
            resolved_at=None,
        )

    generation = dlq(
        "dlq-generation",
        "outbox.generation",
        {"task_id": "gen-deleted"},
        offset=1,
    )
    completion = dlq(
        "dlq-completion",
        "outbox.completion",
        {"task_id": "comp-active"},
        offset=2,
    )
    video = dlq(
        "dlq-video",
        "outbox.video_generation",
        {"task_id": "video-deleted"},
        offset=3,
    )
    storyboard = dlq(
        "dlq-storyboard",
        "outbox.storyboard_assembly",
        {"task_id": "storyboard-deleted"},
        offset=4,
    )
    sse = dlq(
        "dlq-sse",
        "outbox.sse",
        {"user_id": "user-deleted"},
        offset=5,
    )
    db = _Db(
        [
            [generation, completion, video],
            ["gen-deleted"],
            [],
            ["video-deleted"],
            [storyboard, sse],
            ["storyboard-deleted"],
            ["user-deleted"],
        ]
    )
    audits: list[dict[str, Any]] = []

    async def fake_audit(*_args: Any, **kwargs: Any) -> None:
        audits.append(kwargs)

    monkeypatch.setattr(admin, "write_admin_audit", fake_audit)

    result = await admin.sweep_dlq_for_deleted_users(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(id="admin-1", email="admin@example.test"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        limit=3,
    )

    assert result == {"ok": True, "swept": 4, "scanned": 5}
    assert generation.resolved_at is not None
    assert completion.resolved_at is None
    assert video.resolved_at is not None
    assert storyboard.resolved_at is not None
    assert sse.resolved_at is not None
    assert db.commits == 1
    assert audits[0]["details"] == {"swept": 4, "scanned": 5}

    first_page_sql = str(
        db.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    second_page_sql = str(
        db.statements[4].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    for event_type in (
        "outbox.generation",
        "outbox.completion",
        "outbox.video_generation",
        "outbox.storyboard_assembly",
        "outbox.sse",
    ):
        assert event_type in first_page_sql
    assert "outbox_dead_letter.failed_at >" in second_page_sql
    assert "outbox_dead_letter.id >" in second_page_sql
