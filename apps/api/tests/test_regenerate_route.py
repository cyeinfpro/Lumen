from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import regenerate
from app.routes.messages import AssistantTaskResult
from lumen_core.constants import GenerationAction, GenerationStatus
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
    return SimpleNamespace(id="user-1", default_system_prompt_id=None)


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
