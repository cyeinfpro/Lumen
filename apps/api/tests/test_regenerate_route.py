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


_NO_CONVERSATION_OVERRIDE = object()


class _Db:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False
        self._conversation: Any | None = None
        self.locked_conversation_override: Any = _NO_CONVERSATION_OVERRIDE
        self.locked_user = _user()

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        rendered = str(statement).lower()
        if "from users" in rendered:
            return _Result(self.locked_user)
        if (
            "from conversations" in rendered
            and getattr(statement, "_for_update_arg", None) is not None
        ):
            if self.locked_conversation_override is not _NO_CONVERSATION_OVERRIDE:
                return _Result(self.locked_conversation_override)
            if self._conversation is not None:
                return _Result(self._conversation)
        result = self.results.pop(0) if self.results else _Result()
        if "from conversations" in rendered and result.value is not None:
            self._conversation = result.value
        return result

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


def test_regenerate_fingerprint_changes_with_target_or_intent() -> None:
    original = regenerate._regenerate_request_fingerprint(  # noqa: SLF001
        target_message_id="assistant-old",
        intent="chat",
    )
    changed_target = regenerate._regenerate_request_fingerprint(  # noqa: SLF001
        target_message_id="assistant-other",
        intent="chat",
    )
    changed_intent = regenerate._regenerate_request_fingerprint(  # noqa: SLF001
        target_message_id="assistant-old",
        intent="text_to_image",
    )

    assert original != changed_target
    assert original != changed_intent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_operation", "stored_fingerprint"),
    [
        ("conversation.message.create", "f" * 64),
        ("conversation.message.regenerate", "0" * 64),
    ],
)
async def test_regenerate_lookup_rejects_cross_operation_or_changed_target(
    stored_operation: str,
    stored_fingerprint: str,
) -> None:
    completion = SimpleNamespace(
        id="completion-1",
        message_id="assistant-new",
        upstream_request=regenerate._idempotency_request_metadata(  # noqa: SLF001
            None,
            operation_namespace=stored_operation,
            request_fingerprint=stored_fingerprint,
        ),
    )
    db = _Db([_Result(completion), _Result(None)])
    request_fingerprint = regenerate._regenerate_request_fingerprint(  # noqa: SLF001
        target_message_id="assistant-old",
        intent="chat",
    )

    with pytest.raises(Exception) as excinfo:
        await regenerate._lookup_idempotent_regenerate(  # noqa: SLF001
            db,  # type: ignore[arg-type]
            "user-1",
            "conv-1",
            "same-key",
            request_fingerprint=request_fingerprint,
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert excinfo.value.detail["error"]["code"] == "idempotency_conflict"


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
async def test_image_params_from_target_falls_back_for_invalid_stored_aspect() -> None:
    gen = Generation(
        id="gen-old",
        message_id="assistant-old",
        user_id="user-1",
        action=GenerationAction.GENERATE.value,
        prompt="old prompt",
        size_requested="2048x2048",
        aspect_ratio="invalid",
        input_image_ids=[],
        status=GenerationStatus.SUCCEEDED.value,
        idempotency_key="old-idem",
        upstream_request={"render_quality": "medium"},
    )
    db = _Db([_Result(all_values=[gen])])

    out = await regenerate._image_params_from_target(
        db,  # type: ignore[arg-type]
        user_id="user-1",
        conv_id="conv-1",
        target_msg_id="assistant-old",
    )

    assert out == regenerate.ImageParamsIn()


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
    monkeypatch.setattr(
        regenerate, "_create_assistant_task", fake_create_assistant_task
    )
    monkeypatch.setattr(regenerate, "_publish_message_appended", fake_publish_appended)
    monkeypatch.setattr(
        regenerate, "_publish_assistant_task", fake_publish_assistant_task
    )

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
async def test_regenerate_rejects_conversation_deleted_before_write_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(regenerate.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(regenerate, "get_redis", lambda: object())

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
    db.locked_conversation_override = None

    with pytest.raises(Exception) as excinfo:
        await regenerate.regenerate_message(
            "conv-1",
            "assistant-old",
            RegenerateIn(intent="chat", idempotency_key="deleted-race"),
            _user(),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 404
    assert excinfo.value.detail["error"]["code"] == "not_found"
    assert target.status == "streaming"
    assert db.added == []
    assert db.committed is False


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
    monkeypatch.setattr(
        regenerate, "_create_assistant_task", fake_create_assistant_task
    )
    monkeypatch.setattr(regenerate, "_publish_message_appended", fake_publish_appended)
    monkeypatch.setattr(
        regenerate, "_publish_assistant_task", fake_publish_assistant_task
    )
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
    expected_fingerprint = regenerate._regenerate_request_fingerprint(  # noqa: SLF001
        target_message_id="assistant-old",
        intent="text_to_image",
    )
    assert captured["request_metadata"] == regenerate._idempotency_request_metadata(  # noqa: SLF001
        None,
        operation_namespace=regenerate._MESSAGE_REGENERATE_IDEMPOTENCY_OPERATION,  # noqa: SLF001
        request_fingerprint=expected_fingerprint,
    )


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
        billing_retry_count=1,
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
        "holds_released": 2,
        "queued_generation_ids": ["gen-queued"],
        "queued_generation_execution_epochs": {"gen-queued": 0},
        "queued_generation_queue_tokens": {},
        "running_generation_ids": ["gen-running"],
        "streaming_completion_ids": ["comp-streaming"],
        "deferred_generation_ids": [],
        "deferred_completion_ids": [],
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
async def test_cancel_regenerate_target_defers_receipt_bearing_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = SimpleNamespace(
        id="gen-retry",
        status=GenerationStatus.QUEUED.value,
        execution_epoch=3,
        cancel_requested_at=None,
        upstream_request={
            "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
            "upstream_dispatch_attempt": 2,
            "upstream_dispatch_execution_epoch": 3,
        },
    )
    comp = SimpleNamespace(
        id="comp-retry",
        status=CompletionStatus.QUEUED.value,
        execution_epoch=4,
        cancel_requested_at=None,
        tokens_in=88,
        upstream_request={"completion_usage_execution_epoch": 4},
    )
    db = _ActiveTaskDb([[gen], [comp]])
    released: list[str] = []

    async def release_regenerate_cancel_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
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

    async def release_generation_queue_state(
        _redis: Redis,
        task_id: str,
        *,
        expected_execution_epoch: int,
        ownership_token: Any,
    ) -> bool:
        assert ownership_token.provider_name == "provider-5"
        queue_released.append((f"{task_id}:{expected_execution_epoch}", db.committed))
        return True

    monkeypatch.setattr(
        regenerate, "invalidate_balance_cache", invalidate_balance_cache
    )
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
            "queued_generation_execution_epochs": {"gen-queued": 5},
            "queued_generation_queue_tokens": {
                "gen-queued": {
                    "task_id": "gen-queued",
                    "execution_epoch": 5,
                    "provider_name": "provider-5",
                    "lease_token": "worker:execution:5:attempt:1",
                    "reservation_token": "reservation-5",
                }
            },
            "running_generation_ids": ["gen-running"],
            "streaming_completion_ids": ["comp-streaming"],
            "deferred_generation_ids": ["gen-deferred"],
            "deferred_completion_ids": ["comp-deferred"],
        },
    )

    assert invalidated == [("user-1", True)]
    assert queue_released == [("gen-queued:5", True)]
    assert redis_calls == [
        ("task:gen-running:cancel", "1", 3600),
        ("task:comp-streaming:cancel", "1", 3600),
        ("task:gen-deferred:cancel", "1", 3600),
        ("task:comp-deferred:cancel", "1", 3600),
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

    async def release_generation_queue_state(
        _redis: Redis,
        task_id: str,
        *,
        expected_execution_epoch: int,
        ownership_token: Any,
    ) -> bool:
        assert ownership_token.provider_name == "provider-6"
        queue_released.append(f"{task_id}:{expected_execution_epoch}")
        return True

    monkeypatch.setattr(
        regenerate, "invalidate_balance_cache", invalidate_balance_cache
    )
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
            "queued_generation_execution_epochs": {"gen-queued": 6},
            "queued_generation_queue_tokens": {
                "gen-queued": {
                    "task_id": "gen-queued",
                    "execution_epoch": 6,
                    "provider_name": "provider-6",
                    "lease_token": "worker:execution:6:attempt:1",
                    "reservation_token": "reservation-6",
                }
            },
            "running_generation_ids": ["gen-running"],
            "streaming_completion_ids": ["comp-streaming"],
        },
    )

    assert queue_released == ["gen-queued:6"]
    assert redis_calls == [
        ("task:gen-running:cancel", "1", 3600),
        ("task:comp-streaming:cancel", "1", 3600),
    ]
