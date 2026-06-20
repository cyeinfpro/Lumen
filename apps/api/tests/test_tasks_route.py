from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import tasks
from lumen_core.constants import (
    CompletionStage,
    CompletionStatus,
    GenerationStage,
    GenerationStatus,
)


class _Result:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Db:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.committed = False

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return self.results.pop(0) if self.results else _Result()

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = "outbox-1"

    async def commit(self) -> None:
        self.committed = True


class _Redis:
    def __init__(
        self,
        values: dict[str, Any] | None = None,
        *,
        fail_delete: bool = False,
    ) -> None:
        self.values = values or {}
        self.fail_delete = fail_delete
        self.calls: list[tuple[Any, ...]] = []

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.calls.append(("set", key, value, ex))

    async def get(self, key: str) -> Any:
        self.calls.append(("get", key))
        return self.values.get(key)

    async def zrem(self, key: str, member: str) -> None:
        self.calls.append(("zrem", key, member))

    async def delete(self, *keys: str) -> None:
        self.calls.append(("delete", *keys))
        if self.fail_delete:
            raise RuntimeError("redis delete failed")


def _user() -> SimpleNamespace:
    return SimpleNamespace(id="user-1", account_mode="wallet")


async def _billing_disabled(_db: Any) -> bool:
    return False


async def _billing_enabled_true(_db: Any) -> bool:
    return True


async def _billing_allow_negative_false(_db: Any) -> bool:
    return False


def test_task_item_exposes_queue_observability_metadata() -> None:
    created = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    task = SimpleNamespace(
        id="gen-1",
        message_id="msg-1",
        status=GenerationStatus.QUEUED.value,
        progress_stage=GenerationStage.QUEUED.value,
        started_at=None,
        created_at=created,
        finished_at=None,
        upstream_request={
            "queue_metadata": {
                "queue_lane": "image:workflow:large",
                "workflow_type": "apparel_model_showcase",
                "workflow_step_key": "showcase_generation",
                "pixel_count": 8_294_400,
                "size_bucket": "large",
                "cost_class": "large",
                "queue_wait_ms": 3200,
            }
        },
        error_code=None,
        error_message=None,
        attempt=0,
    )

    item = tasks._task_item("generation", task)  # noqa: SLF001

    assert item.queue_lane == "image:workflow:large"
    assert item.workflow_type == "apparel_model_showcase"
    assert item.workflow_step_key == "showcase_generation"
    assert item.pixel_count == 8_294_400
    assert item.size_bucket == "large"
    assert item.cost_class == "large"
    assert item.queue_wait_ms == 3200


def test_task_cursor_round_trips_and_rejects_invalid() -> None:
    sort_at = datetime(2026, 5, 19, 10, 15, tzinfo=timezone.utc)
    raw = tasks._encode_task_cursor(sort_at, "generation", "gen-1")  # noqa: SLF001

    assert tasks._decode_task_cursor(raw) == (  # noqa: SLF001
        sort_at,
        "generation",
        "gen-1",
    )
    with pytest.raises(Exception) as exc_info:
        tasks._decode_task_cursor("not-a-cursor")  # noqa: SLF001
    assert getattr(exc_info.value, "status_code", None) == 422


def test_task_cursor_same_timestamp_mode_matches_merged_order() -> None:
    assert (
        tasks._same_timestamp_cursor_mode(  # noqa: SLF001
            model_kind="completion",
            cursor_kind="generation",
        )
        == "all"
    )
    assert (
        tasks._same_timestamp_cursor_mode(  # noqa: SLF001
            model_kind="generation",
            cursor_kind="completion",
        )
        == "none"
    )
    assert (
        tasks._same_timestamp_cursor_mode(  # noqa: SLF001
            model_kind="generation",
            cursor_kind="generation",
        )
        == "same_kind_id"
    )
    assert (
        tasks._same_timestamp_cursor_mode(  # noqa: SLF001
            model_kind="completion",
            cursor_kind="completion",
        )
        == "same_kind_id"
    )


def test_task_recommended_actions_cover_retry_and_terminal_errors() -> None:
    retryable = tasks._task_retryable(  # noqa: SLF001
        "generation",
        GenerationStatus.FAILED.value,
        "upstream_timeout",
    )
    assert retryable is True
    retry_actions = tasks._task_recommended_actions(  # noqa: SLF001
        kind="generation",
        status=GenerationStatus.FAILED.value,
        error_code="upstream_timeout",
        retryable=retryable,
    )
    assert [action.id for action in retry_actions] == ["retry"]

    terminal_retryable = tasks._task_retryable(  # noqa: SLF001
        "generation",
        GenerationStatus.FAILED.value,
        "INSUFFICIENT_BALANCE",
    )
    wallet_actions = tasks._task_recommended_actions(  # noqa: SLF001
        kind="generation",
        status=GenerationStatus.FAILED.value,
        error_code="INSUFFICIENT_BALANCE",
        retryable=terminal_retryable,
    )
    assert terminal_retryable is False
    assert [action.id for action in wallet_actions] == ["open_wallet", "reduce_cost"]


@pytest.mark.asyncio
async def test_retry_generation_requeues_same_row_without_rebuilding_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[dict[str, Any], str]] = []

    async def fake_publish_queued(payload: dict[str, Any], message_id: str) -> None:
        published.append((payload, message_id))

    redis = _Redis()
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_publish_queued", fake_publish_queued)
    monkeypatch.setattr(tasks, "_billing_enabled", _billing_disabled)

    upstream_request = {
        "fast": True,
        "render_quality": "high",
        "output_format": "webp",
        "output_compression": 95,
        "background": "auto",
        "moderation": "low",
    }
    old_time = datetime(2026, 4, 28, tzinfo=timezone.utc)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="assistant-1",
        status=GenerationStatus.FAILED.value,
        progress_stage=GenerationStage.FINALIZING.value,
        attempt=2,
        error_code="upstream_timeout",
        error_message="timeout",
        started_at=old_time,
        finished_at=old_time,
        retry_count=0,
        prompt="render a wide hero image",
        size_requested="3840x2160",
        aspect_ratio="16:9",
        upstream_request=upstream_request,
    )
    db = _Db([_Result(gen)])

    out = await tasks.retry_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.QUEUED.value}
    assert gen.status == GenerationStatus.QUEUED.value
    assert gen.progress_stage == GenerationStage.QUEUED.value
    assert gen.attempt == 0
    assert gen.retry_count == 1
    assert gen.error_code is None
    assert gen.error_message is None
    assert gen.started_at is None
    assert gen.finished_at is None

    assert gen.prompt == "render a wide hero image"
    assert gen.size_requested == "3840x2160"
    assert gen.aspect_ratio == "16:9"
    assert gen.upstream_request is upstream_request

    assert db.committed is True
    assert redis.calls == [("delete", "task:gen-1:cancel")]
    assert len(db.added) == 1
    assert published == [
        (
            {
                "task_id": "gen-1",
                "user_id": "user-1",
                "kind": "generation",
                "outbox_id": "outbox-1",
            },
            "assistant-1",
        )
    ]


@pytest.mark.asyncio
async def test_retry_generation_holds_new_retry_billing_ref_before_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held: list[dict[str, Any]] = []
    invalidated: list[tuple[str, bool]] = []

    async def fake_publish_queued(_payload: dict[str, Any], _message_id: str) -> None:
        return None

    async def estimate_image_cost_for_tier(*_args: Any, **kwargs: Any) -> tuple[int, str]:
        assert kwargs["tier"] == "2k"
        assert kwargs["n"] == 3
        return 75_000, "2k"

    async def hold(_db: _Db, user_id: str, amount_micro: int, **kwargs: Any) -> Any:
        held.append(
            {
                "committed": _db.committed,
                "user_id": user_id,
                "amount_micro": amount_micro,
                **kwargs,
            }
        )
        return SimpleNamespace(balance_after=75_000)

    async def invalidate(user_id: str) -> None:
        invalidated.append((user_id, db.committed))

    redis = _Redis()
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_publish_queued", fake_publish_queued)
    monkeypatch.setattr(tasks, "_billing_enabled", _billing_enabled_true)
    monkeypatch.setattr(tasks, "_billing_allow_negative", _billing_allow_negative_false)
    monkeypatch.setattr(
        tasks.billing_core,
        "estimate_image_cost_for_tier",
        estimate_image_cost_for_tier,
    )
    monkeypatch.setattr(tasks.billing_core, "hold", hold)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", invalidate)

    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="assistant-1",
        status=GenerationStatus.CANCELED.value,
        progress_stage=GenerationStage.FINALIZING.value,
        attempt=1,
        retry_count=0,
        error_code="cancelled",
        error_message="cancelled by user",
        started_at=None,
        finished_at=datetime.now(timezone.utc),
        size_requested="2048x2048",
        upstream_request={"billing_tier": "2k", "n": 3},
    )
    db = _Db([_Result(gen)])

    out = await tasks.retry_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.QUEUED.value}
    assert gen.retry_count == 1
    assert held == [
        {
            "committed": False,
            "user_id": "user-1",
            "amount_micro": 75_000,
            "ref_type": "generation",
            "ref_id": "gen-1:retry:1",
            "idempotency_key": "hold:gen-1:retry:1",
            "allow_negative": False,
            "meta": {
                "generation_id": "gen-1",
                "reason": "generation retry",
                "retry_count": 1,
            },
        }
    ]
    assert invalidated == [("user-1", True)]


@pytest.mark.asyncio
async def test_retry_generation_fails_if_prior_cancel_signal_cannot_be_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_publish_queued(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("retry must not enqueue while stale cancel may remain")

    redis = _Redis(fail_delete=True)
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_publish_queued", fail_publish_queued)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="assistant-1",
        status=GenerationStatus.CANCELED.value,
        progress_stage=GenerationStage.FINALIZING.value,
        attempt=1,
        error_code="cancelled",
        error_message="cancelled by user",
        started_at=None,
        finished_at=datetime.now(timezone.utc),
    )
    db = _Db([_Result(gen)])

    with pytest.raises(Exception) as exc_info:
        await tasks.retry_generation(
            "gen-1",
            _user(),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 503
    assert gen.status == GenerationStatus.CANCELED.value
    assert db.added == []
    assert db.committed is False
    assert redis.calls == [("delete", "task:gen-1:cancel")]


@pytest.mark.asyncio
async def test_cancel_running_generation_keeps_row_active_until_worker_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis(
        {"generation:image_queue:task_provider:gen-1": b"provider-a"}
    )
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        status=GenerationStatus.RUNNING.value,
        finished_at=None,
    )
    db = _Db([_Result(gen)])

    out = await tasks.cancel_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.RUNNING.value}
    assert gen.status == GenerationStatus.RUNNING.value
    assert gen.finished_at is None
    # Why no commit on the RUNNING branch: there is no field mutation on
    # `gen` (status stays RUNNING, finished_at stays None). The SELECT FOR
    # UPDATE row lock is released by the FastAPI session context manager at
    # request exit, so an explicit commit here would just be a wasted
    # round-trip. See cancel_generation() comment.
    assert db.committed is False
    assert redis.calls == [("set", "task:gen-1:cancel", "1", 3600)]


@pytest.mark.asyncio
async def test_cancel_streaming_completion_returns_canceling_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        status=CompletionStatus.STREAMING.value,
    )
    db = _Db([_Result(comp)])

    out = await tasks.cancel_completion(
        "comp-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": "canceling", "cancel_requested": True}
    assert comp.status == CompletionStatus.STREAMING.value
    assert db.committed is False
    assert redis.calls == [("set", "task:comp-1:cancel", "1", 3600)]


@pytest.mark.asyncio
async def test_cancel_queued_generation_marks_terminal_and_clears_queue_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, Any]] = []
    released: list[tuple[str, str, str, str]] = []
    invalidated: list[tuple[str, bool]] = []

    async def fake_publish_sse_event(
        _redis: Any,
        *,
        user_id: str,
        channel: str,
        event_name: str,
        data: dict[str, Any],
    ) -> str:
        published.append(
            {
                "user_id": user_id,
                "channel": channel,
                "event_name": event_name,
                "data": data,
            }
        )
        return "sse-1"

    redis = _Redis(
        {"generation:image_queue:task_provider:gen-1": b"provider-a"}
    )

    async def release_queued_task_hold(
        db: _Db,
        *,
        user_id: str,
        ref_type: str,
        ref_id: str,
        reason: str,
    ) -> bool:
        released.append((user_id, ref_type, ref_id, reason))
        invalidated.append(("release-before-commit", db.committed))
        return True

    async def invalidate_balance_cache(user_id: str) -> None:
        invalidated.append((user_id, db.committed))

    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "publish_sse_event", fake_publish_sse_event)
    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    monkeypatch.setattr(tasks, "_task_wallet_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", invalidate_balance_cache)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="msg-1",
        status=GenerationStatus.QUEUED.value,
        finished_at=None,
    )
    db = _Db([_Result(gen)])

    out = await tasks.cancel_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.CANCELED.value}
    assert gen.status == GenerationStatus.CANCELED.value
    assert gen.finished_at is not None
    assert released == [
        (
            "user-1",
            "generation",
            "gen-1",
            "queued generation cancelled by user",
        )
    ]
    assert invalidated == [("release-before-commit", False), ("user-1", True)]
    assert redis.calls == [
        ("get", "generation:image_queue:task_provider:gen-1"),
        ("zrem", "generation:image_queue:active", "gen-1"),
        ("zrem", "generation:image_queue:provider_active:provider-a", "gen-1"),
        ("delete", "generation:image_queue:task_provider:gen-1"),
        ("delete", "task:gen-1:lease"),
    ]
    assert published == [
        {
            "user_id": "user-1",
            "channel": tasks.task_channel("gen-1"),
            "event_name": "generation.canceled",
            "data": {
                "generation_id": "gen-1",
                "message_id": "msg-1",
                "stage": GenerationStage.FINALIZING.value,
                "substage": "cancelled",
                "cancelled": True,
                "code": "cancelled",
                "message": "cancelled by user",
                "retriable": True,
                "recommended_actions": [
                    {"id": "retry", "label": "重新开始", "kind": "retry"}
                ],
            },
        }
    ]


@pytest.mark.asyncio
async def test_cancel_queued_generation_skips_wallet_release_for_byok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, Any]] = []
    released: list[str] = []

    async def fake_publish_sse_event(*_args: Any, **kwargs: Any) -> str:
        published.append(kwargs)
        return "sse-1"

    async def release_queued_task_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
        return True

    async def wallet_exists(*_args: Any, **_kwargs: Any) -> bool:
        return False

    redis = _Redis()
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "publish_sse_event", fake_publish_sse_event)
    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    monkeypatch.setattr(tasks, "_task_wallet_exists", wallet_exists)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="msg-1",
        status=GenerationStatus.QUEUED.value,
        finished_at=None,
    )
    db = _Db([_Result(gen)])

    out = await tasks.cancel_generation(
        "gen-1",
        SimpleNamespace(id="user-1", account_mode="byok"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.CANCELED.value}
    assert released == []
    assert db.committed is True
    assert published


@pytest.mark.asyncio
async def test_cancel_queued_completion_releases_wallet_hold_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    released: list[tuple[str, str, str, str]] = []
    invalidated: list[tuple[str, bool]] = []
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        status=CompletionStatus.QUEUED.value,
        progress_stage=CompletionStage.QUEUED.value,
        finished_at=None,
        upstream_request={"billing_retry_count": 1},
    )
    db = _Db([_Result(comp)])

    async def release_queued_task_hold(
        db: _Db,
        *,
        user_id: str,
        ref_type: str,
        ref_id: str,
        reason: str,
    ) -> bool:
        released.append((user_id, ref_type, ref_id, reason))
        invalidated.append(("release-before-commit", db.committed))
        return True

    async def invalidate_balance_cache(user_id: str) -> None:
        invalidated.append((user_id, db.committed))

    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", invalidate_balance_cache)
    monkeypatch.setattr(tasks, "_task_wallet_exists", lambda *_args, **_kwargs: True)

    out = await tasks.cancel_completion(
        "comp-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": CompletionStatus.CANCELED.value}
    assert comp.status == CompletionStatus.CANCELED.value
    assert comp.progress_stage == CompletionStage.FINALIZING.value
    assert comp.finished_at is not None
    assert released == [
        (
            "user-1",
            "completion",
            "comp-1:retry:1",
            "queued completion cancelled by user",
        )
    ]
    assert invalidated == [("release-before-commit", False), ("user-1", True)]


@pytest.mark.asyncio
async def test_cancel_queued_completion_skips_wallet_release_for_byok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []
    monkeypatch.setattr(tasks, "get_redis", lambda: _Redis())
    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        status=CompletionStatus.QUEUED.value,
        progress_stage=CompletionStage.QUEUED.value,
        finished_at=None,
    )
    db = _Db([_Result(comp)])

    async def release_queued_task_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
        return True

    async def wallet_exists(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    monkeypatch.setattr(tasks, "_task_wallet_exists", wallet_exists)

    out = await tasks.cancel_completion(
        "comp-1",
        SimpleNamespace(id="user-1", account_mode="byok"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": CompletionStatus.CANCELED.value}
    assert released == []
    assert db.committed is True


@pytest.mark.asyncio
async def test_retry_completion_records_new_billing_retry_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[dict[str, Any], str]] = []

    async def fake_publish_queued(payload: dict[str, Any], message_id: str) -> None:
        published.append((payload, message_id))

    redis = _Redis()
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_publish_queued", fake_publish_queued)

    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        message_id="assistant-1",
        status=CompletionStatus.FAILED.value,
        progress_stage=CompletionStage.FINALIZING.value,
        attempt=2,
        error_code="upstream_timeout",
        error_message="timeout",
        started_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        upstream_request={"web_search": True},
    )
    db = _Db([_Result(comp)])

    out = await tasks.retry_completion(
        "comp-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": CompletionStatus.QUEUED.value}
    assert comp.status == CompletionStatus.QUEUED.value
    assert comp.progress_stage == CompletionStage.QUEUED.value
    assert comp.attempt == 0
    assert comp.error_code is None
    assert comp.error_message is None
    assert comp.started_at is None
    assert comp.finished_at is None
    assert comp.upstream_request == {
        "web_search": True,
        "billing_retry_count": 1,
    }
    assert db.committed is True
    assert redis.calls == [("delete", "task:comp-1:cancel")]
    assert published == [
        (
            {
                "task_id": "comp-1",
                "user_id": "user-1",
                "kind": "completion",
                "outbox_id": "outbox-1",
            },
            "assistant-1",
        )
    ]


@pytest.mark.asyncio
async def test_retry_completion_holds_new_retry_billing_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[dict[str, Any], str]] = []
    hold_calls: list[dict[str, Any]] = []
    invalidated: list[tuple[str, bool]] = []

    async def fake_publish_queued(payload: dict[str, Any], message_id: str) -> None:
        published.append((payload, message_id))

    async def hold(_db: Any, user_id: str, amount_micro: int, **kwargs: Any) -> Any:
        hold_calls.append(
            {"user_id": user_id, "amount_micro": amount_micro, **kwargs}
        )
        return SimpleNamespace(balance_after=80_000, hold_after=20_000)

    async def invalidate_balance_cache(user_id: str) -> None:
        invalidated.append((user_id, db.committed))

    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        message_id="assistant-1",
        status=CompletionStatus.CANCELED.value,
        progress_stage=CompletionStage.FINALIZING.value,
        attempt=1,
        error_code="cancelled",
        error_message="cancelled",
        started_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        upstream_request={},
    )
    previous_hold = SimpleNamespace(amount_micro=-20_000)
    db = _Db([_Result(comp), _Result(previous_hold)])

    monkeypatch.setattr(tasks, "get_redis", lambda: _Redis())
    monkeypatch.setattr(tasks, "_publish_queued", fake_publish_queued)
    monkeypatch.setattr(tasks, "_billing_enabled", _billing_enabled_true)
    monkeypatch.setattr(tasks, "_billing_allow_negative", _billing_allow_negative_false)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", invalidate_balance_cache)
    monkeypatch.setattr(tasks.billing_core, "hold", hold)

    out = await tasks.retry_completion(
        "comp-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": CompletionStatus.QUEUED.value}
    assert comp.upstream_request["billing_retry_count"] == 1
    assert hold_calls == [
        {
            "user_id": "user-1",
            "amount_micro": 20_000,
            "ref_type": "completion",
            "ref_id": "comp-1:retry:1",
            "idempotency_key": "hold:comp-1:retry:1",
            "allow_negative": False,
            "meta": {
                "completion_id": "comp-1",
                "reason": "completion retry",
                "billing_retry_count": 1,
                "previous_billing_retry_count": 0,
            },
        }
    ]
    assert invalidated == [("user-1", True)]
    assert published
