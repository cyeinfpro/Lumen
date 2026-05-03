from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.routes import messages
from lumen_core.constants import (
    DEFAULT_IMAGE_RESPONSES_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    MAX_PROMPT_CHARS,
)
from lumen_core.schemas import ChatParamsIn, ImageParamsIn, PostMessageIn


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

    def first(self) -> Any:
        return self.value


class _Db:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False
        self._id_seq = 0

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return self.results.pop(0) if self.results else _Result()

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        now = datetime.now(timezone.utc)
        for item in self.added:
            if getattr(item, "id", None) is None:
                self._id_seq += 1
                item.id = f"new-{self._id_seq}"
            if getattr(item, "created_at", None) is None:
                item.created_at = now
            if getattr(item, "updated_at", None) is None:
                item.updated_at = now

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def refresh(self, item: Any) -> None:
        if getattr(item, "created_at", None) is None:
            item.created_at = datetime.now(timezone.utc)


class _Pipe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.executed = False

    def publish(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("publish", args, kwargs))

    def xadd(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("xadd", args, kwargs))

    async def execute(self) -> None:
        self.executed = True


class _Redis:
    def __init__(self) -> None:
        self.pipe = _Pipe()

    def pipeline(self, *, transaction: bool = False) -> _Pipe:
        assert transaction is False
        return self.pipe


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


def test_image_upstream_request_uses_explicit_render_quality_for_4k() -> None:
    from lumen_core.sizing import resolve_size

    resolved = resolve_size("16:9", "fixed", "3840x2160")

    medium = messages._image_upstream_request(  # noqa: SLF001
        ImageParamsIn(
            aspect_ratio="16:9",
            size_mode="fixed",
            fixed_size="3840x2160",
            render_quality="medium",
        ),
        resolved,
        prompt="make a 4k landscape",
    )
    assert medium["render_quality"] == "medium"
    assert medium["responses_model"] == DEFAULT_IMAGE_RESPONSES_MODEL
    assert medium["output_compression"] == 0

    fast = messages._image_upstream_request(  # noqa: SLF001
        ImageParamsIn(
            aspect_ratio="16:9",
            size_mode="fixed",
            fixed_size="3840x2160",
            fast=True,
            render_quality="high",
            output_compression=95,
        ),
        resolved,
        prompt="make a 4k landscape fast",
    )
    assert fast["render_quality"] == "high"
    assert fast["responses_model"] == DEFAULT_IMAGE_RESPONSES_MODEL_FAST
    assert fast["output_compression"] == 95


def test_silent_generation_prompt_limit_uses_shared_constant() -> None:
    messages.SilentGenerationIn(
        idempotency_key="idem-silent",
        parent_message_id="msg-1",
        prompt="x" * MAX_PROMPT_CHARS,
    )

    with pytest.raises(ValidationError):
        messages.SilentGenerationIn(
            idempotency_key="idem-silent",
            parent_message_id="msg-1",
            prompt="x" * (MAX_PROMPT_CHARS + 1),
        )


@pytest.mark.asyncio
async def test_post_message_rejects_explicit_image_to_image_without_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(messages.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    db = _Db([_Result(_conv()), _Result(None), _Result(None)])

    with pytest.raises(Exception) as excinfo:
        await messages.post_message(
            "conv-1",
            PostMessageIn(
                idempotency_key="idem-1",
                text="edit this",
                intent="image_to_image",
            ),
            _user(),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "missing_reference_image"
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_post_message_publishes_appended_events_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    appended_calls: list[dict[str, Any]] = []
    assistant_published: list[str] = []

    async def fake_publish_appended(**kwargs: Any) -> None:
        assert db.committed is True
        appended_calls.append(kwargs)

    async def fake_publish_assistant_task(**kwargs: Any) -> None:
        assistant_published.append(kwargs["assistant_msg_id"])

    redis = object()
    monkeypatch.setattr(messages.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(messages, "get_redis", lambda: redis)
    monkeypatch.setattr(messages, "_publish_message_appended", fake_publish_appended)
    monkeypatch.setattr(messages, "_publish_assistant_task", fake_publish_assistant_task)

    db = _Db([_Result(_conv()), _Result(None), _Result(None)])
    out = await messages.post_message(
        "conv-1",
        PostMessageIn(idempotency_key="idem-2", text="hello", intent="chat"),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert appended_calls == [
        {
            "redis": redis,
            "user_id": "user-1",
            "conv_id": "conv-1",
            "message_ids": [out.user_message.id, out.assistant_message.id],
        }
    ]
    assert assistant_published == [out.assistant_message.id]


@pytest.mark.asyncio
async def test_post_message_returns_when_post_commit_publish_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def slow_publish_appended(**_kwargs: Any) -> None:
        await asyncio.sleep(0.05)

    assistant_published: list[str] = []

    async def fake_publish_assistant_task(**kwargs: Any) -> None:
        assistant_published.append(kwargs["assistant_msg_id"])

    monkeypatch.setattr(messages.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(messages, "_publish_message_appended", slow_publish_appended)
    monkeypatch.setattr(messages, "_publish_assistant_task", fake_publish_assistant_task)
    monkeypatch.setattr(messages, "_POST_COMMIT_PUBLISH_TIMEOUT_S", 0.001)

    db = _Db([_Result(_conv()), _Result(None), _Result(None)])
    out = await messages.post_message(
        "conv-1",
        PostMessageIn(idempotency_key="idem-timeout", text="hello", intent="chat"),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out.user_message.id
    assert assistant_published == [out.assistant_message.id]


@pytest.mark.asyncio
async def test_post_message_persists_web_search_chat_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_publish_appended(**_kwargs: Any) -> None:
        return None

    async def fake_publish_assistant_task(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(messages.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(messages, "_publish_message_appended", fake_publish_appended)
    monkeypatch.setattr(messages, "_publish_assistant_task", fake_publish_assistant_task)

    db = _Db([_Result(_conv()), _Result(None), _Result(None)])
    await messages.post_message(
        "conv-1",
        PostMessageIn(
            idempotency_key="idem-web",
            text="今天有什么新闻？",
            intent="chat",
            chat_params=ChatParamsIn(reasoning_effort="none", web_search=True),
        ),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    user_msg = next(item for item in db.added if getattr(item, "role", None) == "user")
    comp = next(item for item in db.added if item.__class__.__name__ == "Completion")
    assert user_msg.content["reasoning_effort"] == "none"
    assert user_msg.content["web_search"] is True
    assert comp.upstream_request == {"web_search": True}


@pytest.mark.asyncio
async def test_post_message_persists_chat_tool_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_publish_appended(**_kwargs: Any) -> None:
        return None

    async def fake_publish_assistant_task(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(messages.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(messages, "_publish_message_appended", fake_publish_appended)
    monkeypatch.setattr(messages, "_publish_assistant_task", fake_publish_assistant_task)

    db = _Db([_Result(_conv()), _Result(None), _Result(None)])
    await messages.post_message(
        "conv-1",
        PostMessageIn(
            idempotency_key="idem-tools",
            text="分析这个数据并生成一张图",
            intent="chat",
            chat_params=ChatParamsIn(
                file_search=True,
                vector_store_ids=["vs_1", "vs_1", "vs_2"],
                code_interpreter=True,
                image_generation=True,
            ),
        ),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    user_msg = next(item for item in db.added if getattr(item, "role", None) == "user")
    comp = next(item for item in db.added if item.__class__.__name__ == "Completion")
    assert user_msg.content["file_search"] is True
    assert user_msg.content["code_interpreter"] is True
    assert user_msg.content["image_generation"] is True
    assert user_msg.content["vector_store_ids"] == ["vs_1", "vs_1", "vs_2"]
    assert comp.upstream_request == {
        "file_search": True,
        "vector_store_ids": ["vs_1", "vs_2"],
        "code_interpreter": True,
        "image_generation": True,
    }


@pytest.mark.asyncio
async def test_post_message_persists_image_render_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_publish_appended(**_kwargs: Any) -> None:
        return None

    async def fake_publish_assistant_task(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(messages.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(messages, "_publish_message_appended", fake_publish_appended)
    monkeypatch.setattr(messages, "_publish_assistant_task", fake_publish_assistant_task)

    db = _Db([_Result(_conv()), _Result(None), _Result(None)])
    await messages.post_message(
        "conv-1",
        PostMessageIn(
            idempotency_key="idem-img-options",
            text="make a product hero image",
            intent="text_to_image",
            image_params=ImageParamsIn(
                aspect_ratio="16:9",
                size_mode="fixed",
                fixed_size="2048x1152",
                fast=False,
                render_quality="medium",
                output_format="webp",
                output_compression=88,
                background="opaque",
                moderation="auto",
            ),
        ),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    gen = next(item for item in db.added if item.__class__.__name__ == "Generation")
    assert gen.upstream_request == {
        "fast": False,
        "responses_model": DEFAULT_IMAGE_RESPONSES_MODEL,
        "render_quality": "medium",
        "output_format": "webp",
        "output_format_source": "request",
        "background": "opaque",
        "moderation": "auto",
        "output_compression": 88,
    }


@pytest.mark.asyncio
async def test_image_output_format_system_setting_is_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_publish_appended(**_kwargs: Any) -> None:
        return None

    async def fake_publish_assistant_task(**_kwargs: Any) -> None:
        return None

    async def fake_get_setting(_db: Any, spec: Any) -> str | None:
        assert spec.key == "image.output_format"
        return "png"

    monkeypatch.setattr(messages.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(messages, "_publish_message_appended", fake_publish_appended)
    monkeypatch.setattr(messages, "_publish_assistant_task", fake_publish_assistant_task)
    monkeypatch.setattr(messages, "get_setting", fake_get_setting)

    db = _Db([_Result(_conv()), _Result(None), _Result(None)])
    await messages.post_message(
        "conv-1",
        PostMessageIn(
            idempotency_key="idem-system-format",
            text="make a product hero image",
            intent="text_to_image",
            image_params=ImageParamsIn(
                aspect_ratio="16:9",
                size_mode="fixed",
                fixed_size="2048x1152",
                render_quality="medium",
            ),
        ),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    gen = next(item for item in db.added if item.__class__.__name__ == "Generation")
    assert gen.upstream_request["output_format"] == "png"
    assert gen.upstream_request["output_format_source"] == "system_default"
    assert "output_compression" not in gen.upstream_request


@pytest.mark.asyncio
async def test_image_prompt_transparent_background_forces_png(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_publish_appended(**_kwargs: Any) -> None:
        return None

    async def fake_publish_assistant_task(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(messages.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(messages, "_publish_message_appended", fake_publish_appended)
    monkeypatch.setattr(messages, "_publish_assistant_task", fake_publish_assistant_task)

    db = _Db([_Result(_conv()), _Result(None), _Result(None)])
    await messages.post_message(
        "conv-1",
        PostMessageIn(
            idempotency_key="idem-transparent",
            text="做一个透明底的产品照片",
            intent="text_to_image",
            image_params=ImageParamsIn(
                aspect_ratio="1:1",
                size_mode="fixed",
                fixed_size="2048x2048",
                output_format="webp",
                background="auto",
            ),
        ),
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    gen = next(item for item in db.added if item.__class__.__name__ == "Generation")
    assert gen.upstream_request["background"] == "transparent"
    assert gen.upstream_request["output_format"] == "png"
    assert gen.upstream_request["output_format_source"] == "transparent_background"
    assert "output_compression" not in gen.upstream_request
    assert "true transparent alpha background" in gen.prompt


@pytest.mark.asyncio
async def test_publish_message_appended_payload_contains_conversation_and_message_ids() -> None:
    redis = _Redis()

    await messages._publish_message_appended(
        redis=redis,
        user_id="user-1",
        conv_id="conv-1",
        message_ids=["msg-1"],
    )

    assert redis.pipe.executed is True
    publish = redis.pipe.calls[0]
    xadd = redis.pipe.calls[1]
    assert publish[1][0] == "conv:conv-1"
    publish_payload = json.loads(publish[1][1])
    assert publish_payload == {
        "event": "conv.message.appended",
        "data": {"conversation_id": "conv-1", "message_id": "msg-1"},
    }
    assert xadd[1][0] == "events:user:user-1"
    assert xadd[1][1]["event"] == "conv.message.appended"


@pytest.mark.asyncio
async def test_get_message_query_is_scoped_to_conversation_owner() -> None:
    db = _Db([_Result(None)])

    with pytest.raises(Exception) as excinfo:
        await messages.get_message(
            "conv-1",
            "msg-1",
            _user(),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    rendered = str(db.statements[0])
    assert getattr(excinfo.value, "status_code", None) == 404
    assert "messages.conversation_id" in rendered
    assert "conversations.user_id" in rendered


@pytest.mark.asyncio
async def test_silent_generation_parent_query_filters_deleted_messages() -> None:
    db = _Db([_Result(_conv()), _Result(None)])

    with pytest.raises(Exception) as excinfo:
        await messages.create_silent_generation(
            "conv-1",
            messages.SilentGenerationIn(
                idempotency_key="silent-1",
                parent_message_id="deleted-parent",
            ),
            _user(),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 404
    rendered = str(db.statements[1])
    assert "messages.deleted_at IS NULL" in rendered


@pytest.mark.asyncio
async def test_publish_assistant_task_does_not_rollback_on_redis_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingPipe:
        def publish(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def xadd(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def execute(self) -> None:
            raise RuntimeError("redis down")

    class _FailingRedis:
        def pipeline(self, *, transaction: bool = False) -> _FailingPipe:
            return _FailingPipe()

    class _Pool:
        async def enqueue_job(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    async def fake_get_arq_pool() -> _Pool:
        return _Pool()

    monkeypatch.setattr(messages, "get_arq_pool", fake_get_arq_pool)
    db = _Db([])

    await messages._publish_assistant_task(
        db=db,  # type: ignore[arg-type]
        redis=_FailingRedis(),
        user_id="user-1",
        conv_id="conv-1",
        assistant_msg_id="assistant-1",
        outbox_payloads=[
            {
                "task_id": "task-1",
                "kind": "generation",
            }
        ],
        outbox_rows=[],
    )

    assert db.rolled_back is False
