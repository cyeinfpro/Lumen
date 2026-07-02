from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import regenerate
from app.routes.messages import AssistantTaskResult
from lumen_core.constants import CompletionStatus, GenerationAction, GenerationStatus
from lumen_core.models import Generation
from lumen_core.schemas import RegenerateIn


class _Result:
    def __init__(self, value: Any = None, all_values: list[Any] | None = None) -> None:
        self.value = value
        self.all_values = all_values if all_values is not None else []

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self.all_values


class _Db:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return self.results.pop(0) if self.results else _Result()

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def refresh(self, item: Any) -> None:
        if getattr(item, "created_at", None) is None:
            item.created_at = datetime.now(timezone.utc)


class _ActiveTaskDb:
    def __init__(self, responses: list[list[Any]]) -> None:
        self.responses = responses
        self.statements: list[Any] = []
        self.committed = False

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(all_values=self.responses.pop(0) if self.responses else [])

    async def commit(self) -> None:
        self.committed = True


def _conv() -> SimpleNamespace:
    return SimpleNamespace(
        id="conv-1",
        user_id="user-1",
        deleted_at=None,
        default_system=None,
        default_system_prompt_id=None,
        last_activity_at=datetime.now(timezone.utc),
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(
        id="user-1",
        default_system_prompt_id=None,
        account_mode="wallet",
    )


def _target() -> SimpleNamespace:
    return SimpleNamespace(
        id="assistant-old",
        conversation_id="conv-1",
        role="assistant",
        parent_message_id="user-msg",
        status="streaming",
    )


def _parent_user(content: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id="user-msg",
        conversation_id="conv-1",
        role="user",
        content=content,
    )


@pytest.mark.asyncio
async def test_image_params_from_target_is_scoped_to_conversation() -> None:
    db = _Db([_Result(all_values=[])])

    out = await regenerate._image_params_from_target(
        db,  # type: ignore[arg-type]
        user_id="user-1",
        conv_id="conv-1",
        target_msg_id="assistant-old",
    )

    assert out.count == 1
    rendered = str(db.statements[0])
    assert "JOIN messages" in rendered
    assert "messages.conversation_id" in rendered


@pytest.mark.asyncio
async def test_image_params_from_target_does_not_inherit_old_default_jpeg() -> None:
    gen = Generation(
        id="gen-old",
        message_id="assistant-old",
        user_id="user-1",
        action=GenerationAction.GENERATE.value,
        prompt="old prompt",
        size_requested="2048x2048",
        aspect_ratio="1:1",
        input_image_ids=[],
        status=GenerationStatus.SUCCEEDED.value,
        idempotency_key="old-idem",
        upstream_request={
            "fast": False,
            "render_quality": "medium",
            "output_format": "jpeg",
            "output_compression": 0,
            "background": "auto",
            "moderation": "low",
        },
    )
    db = _Db([_Result(all_values=[gen])])

    out = await regenerate._image_params_from_target(
        db,  # type: ignore[arg-type]
        user_id="user-1",
        conv_id="conv-1",
        target_msg_id="assistant-old",
    )

    assert out.output_format is None
    assert out.output_compression is None


@pytest.mark.asyncio
async def test_image_params_from_target_treats_string_false_fast_as_disabled() -> None:
    gen = Generation(
        id="gen-old",
        message_id="assistant-old",
        user_id="user-1",
        action=GenerationAction.GENERATE.value,
        prompt="old prompt",
        size_requested="2048x2048",
        aspect_ratio="1:1",
        input_image_ids=[],
        status=GenerationStatus.SUCCEEDED.value,
        idempotency_key="old-idem",
        upstream_request={"fast": "false"},
    )
    db = _Db([_Result(all_values=[gen])])

    out = await regenerate._image_params_from_target(
        db,  # type: ignore[arg-type]
        user_id="user-1",
        conv_id="conv-1",
        target_msg_id="assistant-old",
    )

    assert out.fast is False


@pytest.mark.asyncio
async def test_image_params_from_target_preserves_explicit_format() -> None:
    gen = Generation(
        id="gen-old",
        message_id="assistant-old",
        user_id="user-1",
        action=GenerationAction.GENERATE.value,
        prompt="old prompt",
        size_requested="2048x2048",
        aspect_ratio="1:1",
        input_image_ids=[],
        status=GenerationStatus.SUCCEEDED.value,
        idempotency_key="old-idem",
        upstream_request={
            "fast": False,
            "render_quality": "medium",
            "output_format": "jpeg",
            "output_format_source": "request",
            "output_compression": 0,
            "background": "auto",
            "moderation": "low",
        },
    )
    db = _Db([_Result(all_values=[gen])])

    out = await regenerate._image_params_from_target(
        db,  # type: ignore[arg-type]
        user_id="user-1",
        conv_id="conv-1",
        target_msg_id="assistant-old",
    )

    assert out.output_format == "jpeg"
    assert out.output_compression == 0


@pytest.mark.asyncio
async def test_mask_image_id_from_target_preserves_alive_mask() -> None:
    # Why: mask must come from the SAME canonical "first generation" row
    # that _image_params_from_target uses (gens[0]). This test feeds an
    # ordered list with a mask on gens[0] and expects that mask back.
    first = SimpleNamespace(mask_image_id="mask-1")
    db = _Db([_Result(all_values=[first]), _Result("mask-1")])

    out = await regenerate._mask_image_id_from_target(
        db,  # type: ignore[arg-type]
        user_id="user-1",
        conv_id="conv-1",
        target_msg_id="assistant-old",
    )

    assert out == "mask-1"


@pytest.mark.asyncio
async def test_mask_image_id_from_target_does_not_scan_later_generations() -> None:
    first = SimpleNamespace(mask_image_id=None)
    second = SimpleNamespace(mask_image_id="mask-2")
    db = _Db([_Result(all_values=[first, second])])

    out = await regenerate._mask_image_id_from_target(
        db,  # type: ignore[arg-type]
        user_id="user-1",
        conv_id="conv-1",
        target_msg_id="assistant-old",
    )

    assert out is None
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_regenerate_rejects_image_to_image_without_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(regenerate.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(regenerate, "get_redis", lambda: object())
    db = _Db(
        [
            _Result(_conv()),
            _Result(_target()),
            _Result(_parent_user({"text": "edit this", "attachments": []})),
            _Result(None),
            _Result(None),
        ]
    )

    with pytest.raises(Exception) as excinfo:
        await regenerate.regenerate_message(
            "conv-1",
            "assistant-old",
            RegenerateIn(intent="image_to_image", idempotency_key="regen-1"),
            _user(),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "missing_reference_image"
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_regenerate_publishes_appended_event_for_new_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_create_assistant_task(**_kwargs: Any) -> AssistantTaskResult:
        return AssistantTaskResult(
            assistant_msg=SimpleNamespace(id="assistant-new"),  # type: ignore[arg-type]
            completion_id="completion-1",
            generation_ids=[],
            outbox_payloads=[],
            outbox_rows=[],
        )

    appended_calls: list[dict[str, Any]] = []
    task_publish_calls: list[str] = []

    async def fake_publish_appended(**kwargs: Any) -> None:
        assert db.committed is True
        appended_calls.append(kwargs)

    async def fake_publish_assistant_task(**kwargs: Any) -> None:
        task_publish_calls.append(kwargs["assistant_msg_id"])

    redis = object()
    monkeypatch.setattr(regenerate.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(regenerate, "get_redis", lambda: redis)
    monkeypatch.setattr(regenerate, "_create_assistant_task", fake_create_assistant_task)
    monkeypatch.setattr(regenerate, "_publish_message_appended", fake_publish_appended)
    monkeypatch.setattr(regenerate, "_publish_assistant_task", fake_publish_assistant_task)

    target = _target()
    db = _Db(
        [
            _Result(_conv()),
            _Result(target),
            _Result(_parent_user({"text": "hello", "attachments": []})),
            _Result(None),
            _Result(None),
            _Result(None),
            _Result(None),
            _Result(all_values=[]),
            _Result(None),
        ]
    )

    out = await regenerate.regenerate_message(
        "conv-1",
        "assistant-old",
        RegenerateIn(intent="chat", idempotency_key="regen-2"),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out.assistant_message_id == "assistant-new"
    assert target.status == "canceled"
    assert appended_calls == [
        {
            "redis": redis,
            "user_id": "user-1",
            "conv_id": "conv-1",
            "message_ids": ["assistant-new"],
        }
    ]
    assert task_publish_calls == ["assistant-new"]


@pytest.mark.asyncio
async def test_regenerate_uses_current_image_output_format_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    captured: dict[str, Any] = {}

    async def fake_create_assistant_task(**kwargs: Any) -> AssistantTaskResult:
        captured.update(kwargs)
        return AssistantTaskResult(
            assistant_msg=SimpleNamespace(id="assistant-new"),  # type: ignore[arg-type]
            completion_id=None,
            generation_ids=["gen-new"],
            outbox_payloads=[],
            outbox_rows=[],
        )

    async def fake_publish_appended(**_kwargs: Any) -> None:
        return None

    async def fake_publish_assistant_task(**_kwargs: Any) -> None:
        return None

    async def fake_get_setting(_db: Any, spec: Any) -> str | None:
        assert spec.key == "image.output_format"
        return "png"

    monkeypatch.setattr(regenerate.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(regenerate, "get_redis", lambda: object())
    monkeypatch.setattr(regenerate, "_create_assistant_task", fake_create_assistant_task)
    monkeypatch.setattr(regenerate, "_publish_message_appended", fake_publish_appended)
    monkeypatch.setattr(regenerate, "_publish_assistant_task", fake_publish_assistant_task)
    monkeypatch.setattr(regenerate, "get_setting", fake_get_setting)

    gen = Generation(
        id="gen-old",
        message_id="assistant-old",
        user_id="user-1",
        action=GenerationAction.GENERATE.value,
        prompt="old prompt",
        size_requested="2048x2048",
        aspect_ratio="1:1",
        input_image_ids=[],
        status=GenerationStatus.SUCCEEDED.value,
        idempotency_key="old-idem",
        upstream_request={
            "output_format": "jpeg",
            "output_compression": 0,
            "background": "auto",
        },
    )
    target = _target()
    db = _Db(
        [
            _Result(_conv()),
            _Result(target),
            _Result(_parent_user({"text": "make image", "attachments": []})),
            _Result(None),
            _Result(None),
            _Result(None),
            _Result(None),
            _Result(all_values=[gen]),
            _Result(None),
        ]
    )

    out = await regenerate.regenerate_message(
        "conv-1",
        "assistant-old",
        RegenerateIn(intent="text_to_image", idempotency_key="regen-png"),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out.generation_ids == ["gen-new"]
    assert captured["default_image_output_format"] == "png"
    assert captured["image_params"].output_format is None


@pytest.mark.asyncio
async def test_cancel_regenerate_target_active_tasks_releases_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen_queued = SimpleNamespace(
        id="gen-queued",
        status=GenerationStatus.QUEUED.value,
        progress_stage="queued",
        finished_at=None,
        error_code=None,
        error_message=None,
    )
    gen_running = SimpleNamespace(
        id="gen-running",
        status=GenerationStatus.RUNNING.value,
        progress_stage="rendering",
        finished_at=None,
        error_code=None,
        error_message=None,
    )
    comp_queued = SimpleNamespace(
        id="comp-queued",
        status=CompletionStatus.QUEUED.value,
        progress_stage="queued",
        finished_at=None,
        error_code=None,
        error_message=None,
        upstream_request={"billing_retry_count": 1},
    )
    comp_streaming = SimpleNamespace(
        id="comp-streaming",
        status=CompletionStatus.STREAMING.value,
        progress_stage="streaming",
        finished_at=None,
        error_code=None,
        error_message=None,
    )
    db = _ActiveTaskDb([[gen_queued, gen_running], [comp_queued, comp_streaming]])
    released: list[dict[str, Any]] = []

    async def release_regenerate_cancel_hold(
        db: _ActiveTaskDb,
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
        regenerate,
        "_release_regenerate_cancel_hold",
        release_regenerate_cancel_hold,
    )

    cleanup = await regenerate._cancel_regenerate_target_active_tasks(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        target_msg_id="assistant-old",
        user_id="user-1",
        canceled_at=datetime.now(timezone.utc),
        account_mode="wallet",
    )

    assert cleanup == {
        "generations_canceled": 2,
        "completions_canceled": 2,
        "holds_released": 4,
        "queued_generation_ids": ["gen-queued"],
        "running_generation_ids": ["gen-running"],
        "streaming_completion_ids": ["comp-streaming"],
    }
    assert [call["ref_id"] for call in released] == [
        "gen-queued",
        "gen-running",
        "comp-queued:retry:1",
        "comp-streaming",
    ]
    assert all(call["committed"] is False for call in released)
    assert gen_queued.status == GenerationStatus.CANCELED.value
    assert gen_running.status == GenerationStatus.CANCELED.value
    assert comp_queued.status == CompletionStatus.CANCELED.value
    assert comp_streaming.status == CompletionStatus.CANCELED.value


@pytest.mark.asyncio
async def test_cancel_regenerate_target_active_tasks_releases_holds_for_byok_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen_queued = SimpleNamespace(
        id="gen-queued",
        status=GenerationStatus.QUEUED.value,
        progress_stage="queued",
        finished_at=None,
        error_code=None,
        error_message=None,
    )
    db = _ActiveTaskDb([[gen_queued], []])
    released: list[str] = []

    async def release_regenerate_cancel_hold(
        db: _ActiveTaskDb,
        *,
        user_id: str,
        ref_type: str,
        ref_id: str,
    ) -> bool:
        released.append(f"{ref_type}:{ref_id}:{db.committed}")
        return True

    async def wallet_exists(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        regenerate,
        "_release_regenerate_cancel_hold",
        release_regenerate_cancel_hold,
    )
    monkeypatch.setattr(regenerate, "_regenerate_wallet_exists", wallet_exists)

    cleanup = await regenerate._cancel_regenerate_target_active_tasks(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        target_msg_id="assistant-old",
        user_id="user-1",
        canceled_at=datetime.now(timezone.utc),
        account_mode="byok",
    )

    assert cleanup["holds_released"] == 1
    assert released == ["generation:gen-queued:False"]


@pytest.mark.asyncio
async def test_post_commit_regenerate_cancel_cleanup_runs_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _ActiveTaskDb([])
    invalidated: list[tuple[str, bool]] = []
    queue_released: list[tuple[str, bool]] = []
    redis_calls: list[tuple[str, str, int]] = []

    class Redis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            redis_calls.append((key, value, ex))

    async def invalidate_balance_cache(user_id: str) -> None:
        invalidated.append((user_id, db.committed))

    async def release_generation_queue_state(_redis: Redis, task_id: str) -> None:
        queue_released.append((task_id, db.committed))

    monkeypatch.setattr(regenerate, "invalidate_balance_cache", invalidate_balance_cache)
    monkeypatch.setattr(
        regenerate,
        "_release_generation_queue_state",
        release_generation_queue_state,
    )

    await db.commit()
    await regenerate._post_commit_regenerate_cancel_cleanup(  # noqa: SLF001
        Redis(),
        user_id="user-1",
        cleanup={
            "holds_released": 2,
            "queued_generation_ids": ["gen-queued"],
            "running_generation_ids": ["gen-running"],
            "streaming_completion_ids": ["comp-streaming"],
        },
    )

    assert invalidated == [("user-1", True)]
    assert queue_released == [("gen-queued", True)]
    assert redis_calls == [
        ("task:gen-running:cancel", "1", 3600),
        ("task:comp-streaming:cancel", "1", 3600),
    ]


@pytest.mark.asyncio
async def test_post_commit_regenerate_cancel_cleanup_keeps_cancel_when_cache_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_released: list[str] = []
    redis_calls: list[tuple[str, str, int]] = []

    class Redis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            redis_calls.append((key, value, ex))

    async def invalidate_balance_cache(_user_id: str) -> None:
        raise RuntimeError("cache unavailable")

    async def release_generation_queue_state(_redis: Redis, task_id: str) -> None:
        queue_released.append(task_id)

    monkeypatch.setattr(regenerate, "invalidate_balance_cache", invalidate_balance_cache)
    monkeypatch.setattr(
        regenerate,
        "_release_generation_queue_state",
        release_generation_queue_state,
    )

    await regenerate._post_commit_regenerate_cancel_cleanup(  # noqa: SLF001
        Redis(),
        user_id="user-1",
        cleanup={
            "holds_released": 1,
            "queued_generation_ids": ["gen-queued"],
            "running_generation_ids": ["gen-running"],
            "streaming_completion_ids": ["comp-streaming"],
        },
    )

    assert queue_released == ["gen-queued"]
    assert redis_calls == [
        ("task:gen-running:cancel", "1", 3600),
        ("task:comp-streaming:cancel", "1", 3600),
    ]
