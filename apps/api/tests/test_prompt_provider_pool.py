from __future__ import annotations

import json
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable

import pytest
from fastapi import FastAPI


class _StubStreamResponse:
    def __init__(
        self,
        status_code: int,
        chunks: list[str] | None = None,
        raw: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._chunks = chunks or []
        self._raw = raw

    async def __aenter__(self) -> "_StubStreamResponse":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def aread(self) -> bytes:
        return self._raw

    async def aiter_text(self):
        for chunk in self._chunks:
            yield chunk


class _StubAsyncClient:
    def __init__(self, responses: list[_StubStreamResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_StubAsyncClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: Any) -> _StubStreamResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected extra stream call")
        return self.responses.pop(0)


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def test_prompt_enhance_normalizes_responses_url() -> None:
    from app.routes import prompts

    assert prompts._responses_url("https://upstream.example") == (
        "https://upstream.example/v1/responses"
    )
    assert prompts._responses_url("https://upstream.example/v1") == (
        "https://upstream.example/v1/responses"
    )


class _PromptIdempotencyDb:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.commits = 0
        self.rollbacks = 0

    async def get(self, _model: Any, key: str) -> Any | None:
        return self.rows.get(key)

    def add(self, row: Any) -> None:
        self.rows[row.id] = row

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def test_prompt_enhance_paid_idempotency_key_and_fingerprint_contract() -> None:
    from fastapi import HTTPException

    from app.routes.prompt_parts import idempotency

    assert idempotency.resolve_client_idempotency_key("request-1") == "request-1"
    first = idempotency.canonical_request_fingerprint(
        idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
        {"text": "cat", "params": {"b": 2, "a": 1}},
    )
    reordered = idempotency.canonical_request_fingerprint(
        idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
        {"params": {"a": 1, "b": 2}, "text": "cat"},
    )
    other_operation = idempotency.canonical_request_fingerprint(
        idempotency.VIDEO_PROMPT_ENHANCE_OPERATION,
        {"params": {"a": 1, "b": 2}, "text": "cat"},
    )

    assert first == reordered
    assert first != other_operation
    for raw in (None, "", " request-1", "request-1 ", "请求-1"):
        with pytest.raises(HTTPException) as excinfo:
            idempotency.resolve_client_idempotency_key(raw)
        assert excinfo.value.status_code == 422


@pytest.mark.asyncio
async def test_prompt_enhance_paid_operation_replays_and_rejects_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from app.routes.prompt_parts import idempotency

    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(idempotency, "lock_user_key", no_lock)
    db = _PromptIdempotencyDb()
    operation = idempotency.prompt_enhance_operation(
        user_id="user-1",
        idempotency_key="request-1",
        operation_namespace=idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
        payload={"text": "cat"},
    )

    first = await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
        db,
        operation,
    )
    assert first.replay_chunks is None
    assert "request-1" not in repr(db.rows[operation.record_id].metadata_jsonb)

    with pytest.raises(HTTPException) as pending:
        await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
            db,
            operation,
        )
    assert pending.value.status_code == 425

    chunks = ['data: {"text":"better cat"}\n\n', "data: [DONE]\n\n"]
    await idempotency.persist_terminal_response(  # type: ignore[arg-type]
        db,
        operation,
        chunks=chunks,
        terminal_state="succeeded",
    )
    replay = await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
        db,
        operation,
    )
    assert replay.replay_chunks == tuple(chunks)

    changed = idempotency.prompt_enhance_operation(
        user_id="user-1",
        idempotency_key="request-1",
        operation_namespace=idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
        payload={"text": "dog"},
    )
    with pytest.raises(HTTPException) as conflict:
        await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
            db,
            changed,
        )
    assert conflict.value.status_code == 409
    assert conflict.value.detail["error"]["code"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_prompt_enhance_route_replay_skips_rate_limit_provider_and_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from app.routes.prompt_parts import idempotency

    async def replay(*_args: Any, **_kwargs: Any):
        return idempotency.PromptEnhanceReservation(
            replay_chunks=(
                'data: {"text":"better cat"}\n\n',
                "data: [DONE]\n\n",
            )
        )

    async def must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("replay must not start paid work")

    def passthrough(source: AsyncIterator[str], **_kwargs: Any) -> AsyncIterator[str]:
        return source

    db = _PromptIdempotencyDb()
    monkeypatch.setattr(
        prompts._prompt_idempotency,
        "reserve_prompt_enhance_operation",
        replay,
    )
    monkeypatch.setattr(prompts.PROMPTS_ENHANCE_LIMITER, "check", must_not_run)
    monkeypatch.setattr(prompts, "_resolve_provider_order", must_not_run)
    monkeypatch.setattr(prompts, "_prepare_prompt_enhance_billing", must_not_run)
    monkeypatch.setattr(prompts, "_stream_with_keepalive", passthrough)

    response = await prompts.enhance_prompt(
        prompts.EnhanceIn(text="cat"),
        SimpleNamespace(id="user-1", account_mode="wallet"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        prompts._PromptRuntime(),
        "request-1",
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == ['data: {"text":"better cat"}\n\n', "data: [DONE]\n\n"]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_prompt_enhance_durable_producer_survives_consumer_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes.prompt_parts import idempotency, responses

    operation = idempotency.prompt_enhance_operation(
        user_id="user-1",
        idempotency_key="request-1",
        operation_namespace=idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
        payload={"text": "cat"},
    )
    release = asyncio.Event()
    persisted: list[tuple[list[str], str]] = []

    class SessionContext:
        async def __aenter__(self) -> _PromptIdempotencyDb:
            return _PromptIdempotencyDb()

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    async def persist(
        _db: Any,
        _operation: Any,
        *,
        chunks: list[str],
        terminal_state: str,
    ) -> None:
        persisted.append((list(chunks), terminal_state))

    async def source(_db: Any):
        yield 'data: {"text":"better "}\n\n'
        await release.wait()
        yield 'data: {"text":"cat"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(idempotency, "persist_terminal_response", persist)
    consumer, task = responses.durable_stream(
        operation=operation,
        session_factory=SessionContext,
        source_factory=source,
        logger=SimpleNamespace(exception=lambda *_args, **_kwargs: None),
    )

    assert await anext(consumer) == 'data: {"text":"better "}\n\n'
    await consumer.aclose()
    release.set()
    await task

    assert persisted == [
        (
            [
                'data: {"text":"better "}\n\n',
                'data: {"text":"cat"}\n\n',
                "data: [DONE]\n\n",
            ],
            "succeeded",
        )
    ]


@pytest.mark.asyncio
async def test_prompt_enhance_stale_reservation_recovers_before_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes.prompt_parts import idempotency

    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    current = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(idempotency, "lock_user_key", no_lock)
    monkeypatch.setattr(idempotency, "_utcnow", lambda: current)
    db = _PromptIdempotencyDb()
    operation = idempotency.prompt_enhance_operation(
        user_id="user-1",
        idempotency_key="crash-before-producer",
        operation_namespace=idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
        payload={"text": "cat"},
    )

    first = await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
        db,
        operation,
    )
    assert first.attempt is not None
    billing_snapshot = {
        "version": 1,
        "mode": "wallet",
        "request_id": first.attempt.billing_request_id,
        "user_id": "user-1",
        "rate_multiplier_x10000": 10_000,
        "cache_aware": True,
        "allow_negative": False,
        "hold_amount_micro": 10_000,
        "pricing_snapshots": {},
    }
    await idempotency.bind_billing_snapshot(  # type: ignore[arg-type]
        db,
        operation,
        first.attempt,
        billing_snapshot,
    )
    await db.commit()

    current += timedelta(seconds=46)
    takeover = await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
        db,
        operation,
    )

    assert takeover.attempt is not None
    assert takeover.attempt.number == first.attempt.number + 1
    assert takeover.attempt.owner_token != first.attempt.owner_token
    assert takeover.attempt.billing_request_id == first.attempt.billing_request_id
    assert takeover.billing_snapshot == billing_snapshot
    assert takeover.recovery is None


@pytest.mark.asyncio
async def test_prompt_enhance_stale_producer_cannot_overwrite_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes.prompt_parts import idempotency

    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    current = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(idempotency, "lock_user_key", no_lock)
    monkeypatch.setattr(idempotency, "_utcnow", lambda: current)
    db = _PromptIdempotencyDb()
    operation = idempotency.prompt_enhance_operation(
        user_id="user-1",
        idempotency_key="stale-writer",
        operation_namespace=idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
        payload={"text": "cat"},
    )
    first = await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
        db,
        operation,
    )
    assert first.attempt is not None
    await idempotency.bind_billing_snapshot(  # type: ignore[arg-type]
        db,
        operation,
        first.attempt,
        {
            "version": 1,
            "mode": "none",
            "request_id": first.attempt.billing_request_id,
        },
    )
    await db.commit()

    current += timedelta(seconds=46)
    takeover = await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
        db,
        operation,
    )
    assert takeover.attempt is not None

    with pytest.raises(idempotency.AttemptOwnershipLost):
        await idempotency.checkpoint_response_chunk(  # type: ignore[arg-type]
            db,
            operation,
            first.attempt,
            sequence=0,
            chunk='data: {"text":"stale"}\n\n',
        )

    winner_chunks = ['data: {"text":"winner"}\n\n', "data: [DONE]\n\n"]
    await idempotency.checkpoint_response_chunk(  # type: ignore[arg-type]
        db,
        operation,
        takeover.attempt,
        sequence=0,
        chunk=winner_chunks[0],
    )
    await idempotency.checkpoint_finalization(  # type: ignore[arg-type]
        db,
        operation,
        takeover.attempt,
        terminal_state="succeeded",
        terminal_chunk=winner_chunks[-1],
        billing_action="none",
    )
    await idempotency.persist_terminal_response(  # type: ignore[arg-type]
        db,
        operation,
        attempt=takeover.attempt,
        chunks=winner_chunks,
        terminal_state="succeeded",
    )

    with pytest.raises(idempotency.AttemptOwnershipLost):
        await idempotency.persist_terminal_response(  # type: ignore[arg-type]
            db,
            operation,
            attempt=first.attempt,
            chunks=['data: {"text":"stale"}\n\n', "data: [DONE]\n\n"],
            terminal_state="succeeded",
        )

    replay = await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
        db,
        operation,
    )
    assert replay.replay_chunks == tuple(winner_chunks)


@pytest.mark.asyncio
async def test_prompt_enhance_repeated_recovery_converges_after_terminal_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes.prompt_parts import idempotency, responses

    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    current = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(idempotency, "lock_user_key", no_lock)
    monkeypatch.setattr(idempotency, "_utcnow", lambda: current)
    db = _PromptIdempotencyDb()
    operation = idempotency.prompt_enhance_operation(
        user_id="user-1",
        idempotency_key="terminal-recovery",
        operation_namespace=idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
        payload={"text": "cat"},
    )
    first = await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
        db,
        operation,
    )
    assert first.attempt is not None
    await idempotency.bind_billing_snapshot(  # type: ignore[arg-type]
        db,
        operation,
        first.attempt,
        {
            "version": 1,
            "mode": "wallet",
            "request_id": first.attempt.billing_request_id,
            "user_id": "user-1",
            "rate_multiplier_x10000": 10_000,
            "cache_aware": True,
            "allow_negative": False,
            "hold_amount_micro": 10_000,
            "pricing_snapshots": {},
        },
    )
    text_chunk = 'data: {"text":"enhanced"}\n\n'
    await idempotency.checkpoint_response_chunk(  # type: ignore[arg-type]
        db,
        operation,
        first.attempt,
        sequence=0,
        chunk=text_chunk,
    )
    await idempotency.checkpoint_finalization(  # type: ignore[arg-type]
        db,
        operation,
        first.attempt,
        terminal_state="succeeded",
        terminal_chunk="data: [DONE]\n\n",
        billing_action="charge",
        billing_capture={"usage": {"input_tokens": 1, "output_tokens": 1}},
    )

    charged_ids = {first.attempt.billing_request_id}
    billing_attempts = 1
    current += timedelta(seconds=46)
    second = await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
        db,
        operation,
    )
    assert second.attempt is not None
    assert second.recovery is not None
    real_persist = idempotency.persist_terminal_response
    persist_calls = 0

    async def flaky_persist(*args: Any, **kwargs: Any) -> None:
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls <= 3:
            raise RuntimeError("terminal write unavailable")
        await real_persist(*args, **kwargs)

    async def recover_billing(
        _db: Any,
        _recovery: idempotency.PromptEnhanceRecovery,
    ) -> None:
        nonlocal billing_attempts
        billing_attempts += 1
        charged_ids.add(first.attempt.billing_request_id)

    class SessionContext:
        async def __aenter__(self) -> _PromptIdempotencyDb:
            return db

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    async def must_not_produce(_db: Any):
        raise AssertionError("recovery must not call the upstream producer")
        yield ""

    monkeypatch.setattr(idempotency, "persist_terminal_response", flaky_persist)
    consumer, task = responses.durable_stream(
        operation=operation,
        attempt=second.attempt,
        recovery=second.recovery,
        session_factory=SessionContext,
        source_factory=must_not_produce,
        recovery_handler=recover_billing,
        heartbeat_interval_seconds=0,
        logger=SimpleNamespace(
            exception=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
        ),
    )
    assert [chunk async for chunk in consumer] == [
        'data: {"error": "idempotency_terminal_persist_unknown"}\n\n'
    ]
    await task

    current += timedelta(seconds=46)
    third = await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
        db,
        operation,
    )
    assert third.attempt is not None
    assert third.recovery is not None
    consumer, task = responses.durable_stream(
        operation=operation,
        attempt=third.attempt,
        recovery=third.recovery,
        session_factory=SessionContext,
        source_factory=must_not_produce,
        recovery_handler=recover_billing,
        heartbeat_interval_seconds=0,
        logger=SimpleNamespace(
            exception=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
        ),
    )
    assert [chunk async for chunk in consumer] == [
        text_chunk,
        "data: [DONE]\n\n",
    ]
    await task

    replay = await idempotency.reserve_prompt_enhance_operation(  # type: ignore[arg-type]
        db,
        operation,
    )
    assert replay.replay_chunks == (text_chunk, "data: [DONE]\n\n")
    assert persist_calls == 4
    assert billing_attempts == 3
    assert charged_ids == {first.attempt.billing_request_id}


@pytest.mark.asyncio
async def test_prompt_enhance_cancels_producer_and_heartbeat_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes.prompt_parts import idempotency, responses

    operation = idempotency.prompt_enhance_operation(
        user_id="user-1",
        idempotency_key="heartbeat-cancel",
        operation_namespace=idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
        payload={"text": "cat"},
    )
    attempt = idempotency.PromptEnhanceAttempt(
        number=1,
        owner_token="owner-1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        billing_request_id=operation.record_id,
    )
    producer_started = asyncio.Event()
    producer_closed = asyncio.Event()
    heartbeat_started = asyncio.Event()
    heartbeat_cancelled = asyncio.Event()

    class SessionContext:
        async def __aenter__(self) -> _PromptIdempotencyDb:
            return _PromptIdempotencyDb()

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    async def source(_db: Any):
        try:
            producer_started.set()
            await asyncio.sleep(60)
            yield "unreachable"
        finally:
            producer_closed.set()

    async def renew(*_args: Any, **_kwargs: Any) -> None:
        heartbeat_started.set()
        try:
            await asyncio.sleep(60)
        finally:
            heartbeat_cancelled.set()

    monkeypatch.setattr(idempotency, "renew_attempt_lease", renew)
    consumer, task = responses.durable_stream(
        operation=operation,
        attempt=attempt,
        session_factory=SessionContext,
        source_factory=source,
        heartbeat_interval_seconds=0.001,
        logger=SimpleNamespace(
            exception=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
        ),
    )
    await asyncio.wait_for(producer_started.wait(), timeout=1)
    await asyncio.wait_for(heartbeat_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await consumer.aclose()

    assert producer_closed.is_set()
    assert heartbeat_cancelled.is_set()


@pytest.mark.asyncio
async def test_prompt_runtime_is_owned_and_drained_by_router_lifespan() -> None:
    from app.routes import prompts

    app = FastAPI()
    app.include_router(prompts.router)
    release = asyncio.Event()

    async def pending_release() -> None:
        await release.wait()

    async with app.router.lifespan_context(app):
        runtime = getattr(app.state, prompts._PROMPT_RUNTIME_STATE_KEY)
        assert isinstance(runtime, prompts._PromptRuntime)
        task = asyncio.create_task(pending_release())
        runtime.track_release_task(task)
        assert task in runtime.release_tasks
        release.set()

    assert task.done()
    assert not hasattr(app.state, prompts._PROMPT_RUNTIME_STATE_KEY)
    assert not hasattr(prompts, "_prompt_runtime_state")


def test_video_prompt_enhance_body_uses_media_content() -> None:
    from app.routes import prompts

    content = [
        {"type": "input_text", "text": "视频提示词"},
        {"type": "input_image", "image_url": "https://example.com/ref.png"},
    ]

    body = prompts._build_enhance_body(  # noqa: SLF001
        "",
        prompts._ENHANCE_ATTEMPTS[0],  # noqa: SLF001
        system_prompt=prompts.VIDEO_ENHANCE_SYSTEM_PROMPT,
        content=content,
        metadata={"purpose": "video_prompt_enhance"},
    )

    assert "AI video generation" in body["instructions"]
    assert body["input"][0]["content"] == content
    assert body["metadata"] == {"purpose": "video_prompt_enhance"}


def test_video_prompt_enhance_defaults_to_single_motion_first_prompt() -> None:
    from app.routes import prompts

    body = prompts.VideoEnhanceIn(text="一个女孩站在城市街头")
    system_prompt = prompts._video_enhance_system_prompt(  # noqa: SLF001
        body.variant_count
    )

    assert body.variant_count == 1
    assert system_prompt == prompts.VIDEO_ENHANCE_SYSTEM_PROMPT
    assert "<variant" not in system_prompt
    assert "motion/camera-first" in system_prompt
    assert "Volcano/Seedance-style video generation prompts" in system_prompt
    assert "Also apply Vibe Creating when appropriate" in system_prompt
    assert (
        "visual anchor, main action/state, local mood, or video theme/style"
        in system_prompt
    )
    assert "Preserve exact dialogue, voiceover, music, sound effects" in system_prompt
    assert "2-3 distinguishing visible features" in system_prompt
    assert "镜头1/镜头2/镜头3" in system_prompt
    assert "one primary camera move per shot" in system_prompt
    assert "keep identity, outfit/product details" in system_prompt
    assert "Do NOT repeat or inventory existing subjects" in system_prompt
    assert "motion trajectory" in system_prompt
    assert "camera movement" in system_prompt
    assert "De-emphasize low-value technical camera controls" in system_prompt
    assert "[ref:image:1]" in system_prompt
    assert "ambiguous phrase without an anchor" in system_prompt
    assert "Do not invent subtitles" in system_prompt
    assert "seed values" in system_prompt
    assert "Output ONLY the enhanced video prompt text" in system_prompt


@pytest.mark.asyncio
async def test_video_prompt_enhance_variant_count_three_requires_parseable_variants() -> (
    None
):
    from app.routes import prompts

    body = prompts.VideoEnhanceIn(
        text="一个女孩站在城市街头",
        variant_count=3,
    )

    system_prompt = prompts._video_enhance_system_prompt(  # noqa: SLF001
        body.variant_count
    )
    content, token_changed = await prompts._build_video_enhance_content(  # noqa: SLF001
        body,
        request=object(),  # type: ignore[arg-type]
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
    )
    content_text = content[0]["text"]

    assert token_changed is False
    assert "Output exactly 3 variants" in system_prompt
    assert (
        '<variant action="direct_rewrite" title="short unique title">' in system_prompt
    )
    assert "ask_first" in system_prompt
    assert "keep_original" in system_prompt
    assert "optional_vc" in system_prompt
    assert "output only one ask_first variant" in system_prompt
    assert "</variant>" in system_prompt
    assert "The first <variant> must be the recommended best option" in system_prompt
    assert "distinct generation strategy" in system_prompt
    assert "候选方案数量：3" in content_text
    assert '<variant action="direct_rewrite" title="...">...</variant>' in content_text
    assert "第一项为推荐最佳" in content_text
    assert "火山/Seedance 视频提示词结构" in content_text
    assert "Vibe Creating 判断" in content_text
    assert "视觉锚点、行为/状态、局部调性或视频主题/风格" in content_text
    assert "参考素材锚点合同" in content_text
    assert 'action="ask_first"' in content_text
    assert "direct_pass、light_refine、direct_rewrite、ask_first" in content_text
    assert "动作轨迹" in content_text
    assert "运镜/镜头语言" in content_text
    assert "不要生成字幕、水印、UI 文案、seed 或命令参数" in content_text


@pytest.mark.asyncio
async def test_video_prompt_enhance_content_accepts_reference_only_input() -> None:
    from app.routes import prompts
    from lumen_core.schemas import VideoReferenceMediaIn

    body = prompts.VideoEnhanceIn(
        text="",
        action="reference",
        reference_media=[
            VideoReferenceMediaIn(
                kind="image",
                url="https://example.com/ref.png",
                label="产品图",
                ref_id="ref:image:1",
            ),
            VideoReferenceMediaIn(
                kind="video",
                url="https://example.com/motion.mp4",
                label="动作参考",
                ref_id="ref:video:1",
            ),
        ],
    )

    content, token_changed = await prompts._build_video_enhance_content(  # noqa: SLF001
        body,
        request=object(),  # type: ignore[arg-type]
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
    )

    assert token_changed is False
    assert {
        "type": "input_image",
        "image_url": "https://example.com/ref.png",
    } in content
    assert any(
        item.get("type") == "input_text" and "[ref:image:1]" in item.get("text", "")
        for item in content
    )
    assert any(
        item.get("type") == "input_text"
        and "https://example.com/motion.mp4" in item.get("text", "")
        for item in content
    )
    assert any(
        item.get("type") == "input_text" and "[ref:video:1]" in item.get("text", "")
        for item in content
    )


@pytest.mark.asyncio
async def test_video_prompt_enhance_content_accepts_asset_reference() -> None:
    from app.routes import prompts
    from lumen_core.schemas import VideoReferenceMediaIn

    body = prompts.VideoEnhanceIn(
        text="",
        action="reference",
        reference_media=[
            VideoReferenceMediaIn(
                kind="image",
                url="asset://asset-20260609161523-stlqd",
                label="真人素材",
                ref_id="ref:image:3",
            ),
        ],
    )

    content, token_changed = await prompts._build_video_enhance_content(  # noqa: SLF001
        body,
        request=object(),  # type: ignore[arg-type]
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
    )

    assert token_changed is False
    assert all(item.get("type") != "input_image" for item in content)
    assert any(
        item.get("type") == "input_text"
        and "asset://asset-20260609161523-stlqd" in item.get("text", "")
        and "[ref:image:3]" in item.get("text", "")
        for item in content
    )


def test_video_prompt_enhance_media_budget_downgrades_large_data_urls() -> None:
    from app.routes import prompts

    content: list[dict[str, Any]] = []
    small_url = "data:image/png;base64,abc"
    appended, used_bytes = prompts._append_input_image_with_budget(  # noqa: SLF001
        content,
        small_url,
        media_payload_bytes=0,
    )

    assert appended is True
    assert content == [{"type": "input_image", "image_url": small_url}]

    huge_url = (
        "data:image/png;base64,"
        + "a" * (prompts._PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES + 1)  # noqa: SLF001
    )
    appended, next_used_bytes = prompts._append_input_image_with_budget(  # noqa: SLF001
        content,
        huge_url,
        media_payload_bytes=used_bytes,
    )

    assert appended is False
    assert next_used_bytes == used_bytes
    assert all(item.get("image_url") != huge_url for item in content)


@pytest.mark.asyncio
async def test_video_prompt_enhance_content_does_not_echo_large_data_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from app.routes.prompt_parts import enhance_content
    from lumen_core.schemas import VideoReferenceMediaIn

    # 预算常量在 enhance_content 模块命名空间中被 build_video_enhance_content
    # 读取(prompts 只是 re-export),必须在该模块上打补丁。
    monkeypatch.setattr(enhance_content, "PROMPT_ENHANCE_MEDIA_TOTAL_MAX_BYTES", 64)
    huge_url = "data:image/png;base64," + "a" * 128
    body = prompts.VideoEnhanceIn.model_construct(
        text="",
        action="reference",
        reference_media=[
            VideoReferenceMediaIn.model_construct(
                kind="image",
                url=huge_url,
                label="大图",
            ),
        ],
    )

    content, token_changed = await prompts._build_video_enhance_content(  # noqa: SLF001
        body,
        request=object(),  # type: ignore[arg-type]
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
    )

    assert token_changed is False
    assert all(item.get("image_url") != huge_url for item in content)
    assert all(huge_url not in item.get("text", "") for item in content)
    assert any(
        item.get("type") == "input_text"
        and "外部图片数据 URL 过大" in item.get("text", "")
        for item in content
    )


@pytest.mark.asyncio
async def test_prompt_enhance_resolves_provider_pool_without_legacy_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts

    values = {
        "providers": json.dumps(
            [
                {
                    "name": "primary",
                    "base_url": "https://primary.example",
                    "api_key": "sk-primary",
                    "priority": 10,
                }
            ]
        ),
    }

    async def fake_get_setting(_db: object, spec: object) -> str | None:
        return values.get(getattr(spec, "key", ""))

    monkeypatch.setattr(prompts, "get_setting", fake_get_setting)
    runtime = prompts._PromptRuntime()

    providers = await prompts._resolve_provider_order(  # type: ignore[arg-type]
        object(),
        runtime,
    )

    assert [(p.name, p.base_url, p.api_key, p.priority) for p in providers] == [
        ("primary", "https://primary.example", "sk-primary", 10),
    ]


@pytest.mark.asyncio
async def test_prompt_enhance_skips_providers_locked_to_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts

    values = {
        "providers": json.dumps(
            [
                {
                    "name": "image2-only",
                    "base_url": "https://image2.example",
                    "api_key": "sk-image2",
                    "priority": 10,
                    "image_jobs_endpoint": "generations",
                    "image_jobs_endpoint_lock": True,
                },
                {
                    "name": "responses",
                    "base_url": "https://responses.example",
                    "api_key": "sk-responses",
                    "priority": 5,
                },
            ]
        ),
    }

    async def fake_get_setting(_db: object, spec: object) -> str | None:
        return values.get(getattr(spec, "key", ""))

    monkeypatch.setattr(prompts, "get_setting", fake_get_setting)
    runtime = prompts._PromptRuntime()

    providers = await prompts._resolve_provider_order(  # type: ignore[arg-type]
        object(),
        runtime,
    )

    assert [p.name for p in providers] == ["responses"]


@pytest.mark.asyncio
async def test_prompt_enhance_skips_image_only_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts

    values = {
        "providers": json.dumps(
            [
                {
                    "name": "image-only",
                    "base_url": "https://image.example",
                    "api_key": "sk-image",
                    "priority": 10,
                    "purposes": ["image"],
                },
                {
                    "name": "chat",
                    "base_url": "https://chat.example",
                    "api_key": "sk-chat",
                    "priority": 5,
                    "purposes": ["chat", "image"],
                },
            ]
        ),
    }

    async def fake_get_setting(_db: object, spec: object) -> str | None:
        return values.get(getattr(spec, "key", ""))

    monkeypatch.setattr(prompts, "get_setting", fake_get_setting)
    runtime = prompts._PromptRuntime()

    providers = await prompts._resolve_provider_order(  # type: ignore[arg-type]
        object(),
        runtime,
    )

    assert [p.name for p in providers] == ["chat"]


@pytest.mark.asyncio
async def test_prompt_enhance_checks_per_user_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: list[tuple[object, str]] = []

    async def fake_check(redis: object, key: str) -> None:
        calls.append((redis, key))

    async def fake_resolve_provider_order(
        _db: object,
        _runtime: prompts._PromptRuntime,
    ) -> list[ProviderDefinition]:
        return [
            ProviderDefinition(
                name="primary",
                base_url="https://primary.example",
                api_key="sk-primary",
            )
        ]

    async def fake_reserve(*_args: Any, **_kwargs: Any):
        from app.routes.prompt_parts import idempotency

        operation = _args[1]
        return idempotency.PromptEnhanceReservation(
            attempt=idempotency.PromptEnhanceAttempt(
                number=1,
                owner_token="owner-1",
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                billing_request_id=operation.record_id,
            ),
            active_user_snapshot=SimpleNamespace(
                user=SimpleNamespace(id="user-1", account_mode="byok"),
                account_mode="byok",
            ),
        )

    async def fake_prepare_reserved(*_args: Any, **_kwargs: Any):
        return None, False

    def fake_response(*_args: Any, **_kwargs: Any) -> object:
        return object()

    redis = object()
    monkeypatch.setattr(prompts.PROMPTS_ENHANCE_LIMITER, "check", fake_check)
    monkeypatch.setattr(prompts, "get_redis", lambda: redis)
    monkeypatch.setattr(prompts, "_resolve_provider_order", fake_resolve_provider_order)
    monkeypatch.setattr(
        prompts._prompt_idempotency,
        "reserve_prompt_enhance_operation",
        fake_reserve,
    )
    monkeypatch.setattr(
        prompts,
        "_durable_prompt_enhance_response",
        fake_response,
    )
    monkeypatch.setattr(
        prompts,
        "_prepare_reserved_billing",
        fake_prepare_reserved,
    )

    await prompts.enhance_prompt(
        prompts.EnhanceIn(text="cat"),
        SimpleNamespace(id="user-1", account_mode="byok"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        prompts._PromptRuntime(),
        "prompt-request-1",
    )

    assert calls == [(redis, "rl:prompt_enhance:user-1")]


@pytest.mark.asyncio
async def test_video_prompt_enhance_does_not_forward_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    captured: dict[str, Any] = {}

    async def fake_check(_redis: object, _key: str) -> None:
        return None

    async def fake_resolve_provider_order(
        _db: object,
        _runtime: prompts._PromptRuntime,
    ) -> list[ProviderDefinition]:
        return [
            ProviderDefinition(
                name="primary",
                base_url="https://primary.example",
                api_key="sk-primary",
            )
        ]

    async def fake_reserve(*_args: Any, **_kwargs: Any):
        from app.routes.prompt_parts import idempotency

        operation = _args[1]
        return idempotency.PromptEnhanceReservation(
            attempt=idempotency.PromptEnhanceAttempt(
                number=1,
                owner_token="owner-1",
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                billing_request_id=operation.record_id,
            ),
            active_user_snapshot=SimpleNamespace(
                user=SimpleNamespace(id="user-1", account_mode="byok"),
                account_mode="byok",
            ),
        )

    async def fake_prepare_reserved(*_args: Any, **_kwargs: Any):
        return None, False

    def fake_durable_response(
        operation: Any,
        _reservation: Any,
        **kwargs: Any,
    ):
        from fastapi.responses import StreamingResponse

        captured["operation"] = operation
        captured["kwargs"] = kwargs

        async def chunks():
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    async def fake_stream_enhance(*_args: Any, **_kwargs: Any):
        yield "data: [DONE]\n\n"

    def passthrough_stream(source, **_kwargs: Any):
        return source

    monkeypatch.setattr(prompts.PROMPTS_ENHANCE_LIMITER, "check", fake_check)
    monkeypatch.setattr(prompts, "get_redis", lambda: object())
    monkeypatch.setattr(prompts, "_resolve_provider_order", fake_resolve_provider_order)
    monkeypatch.setattr(prompts, "_stream_enhance", fake_stream_enhance)
    monkeypatch.setattr(prompts, "_stream_with_keepalive", passthrough_stream)
    monkeypatch.setattr(
        prompts._prompt_idempotency,
        "reserve_prompt_enhance_operation",
        fake_reserve,
    )
    monkeypatch.setattr(
        prompts,
        "_durable_prompt_enhance_response",
        fake_durable_response,
    )
    monkeypatch.setattr(
        prompts,
        "_prepare_reserved_billing",
        fake_prepare_reserved,
    )

    response = await prompts.enhance_video_prompt(
        prompts.VideoEnhanceIn(text="一个女孩在城市街头奔跑"),
        object(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1", account_mode="byok"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        prompts._PromptRuntime(),
        "video-prompt-request-1",
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == ["data: [DONE]\n\n"]
    assert captured["operation"].idempotency_key == "video-prompt-request-1"
    assert captured["kwargs"]["system_prompt"] == prompts.VIDEO_ENHANCE_SYSTEM_PROMPT
    assert captured["kwargs"]["content"][0]["type"] == "input_text"


@pytest.mark.asyncio
async def test_prompt_enhance_uses_legacy_env_when_providers_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts

    async def fake_get_setting(_db: object, spec: object) -> str | None:
        assert getattr(spec, "key", "") == "providers"
        return None

    monkeypatch.setattr(prompts, "get_setting", fake_get_setting)
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://legacy.example")
    monkeypatch.setenv("UPSTREAM_API_KEY", "sk-legacy")
    runtime = prompts._PromptRuntime()

    providers = await prompts._resolve_provider_order(  # type: ignore[arg-type]
        object(),
        runtime,
    )

    assert [(p.name, p.base_url, p.api_key, p.priority) for p in providers] == [
        ("default", "https://legacy.example", "sk-legacy", 0),
    ]


@pytest.mark.asyncio
async def test_prompt_enhance_falls_back_to_gpt54_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(500, raw=b"server down"),
            _StubStreamResponse(
                200,
                [
                    _sse(
                        {"type": "response.output_text.delta", "delta": "better prompt"}
                    ),
                    _sse({"type": "response.completed"}),
                ],
            ),
        ]
    )
    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)

    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [chunk async for chunk in prompts._stream_enhance("cat", [provider])]

    assert chunks == [
        'data: {"text": "better prompt"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert client.calls[0]["json"]["model"] == "gpt-5.5"
    assert client.calls[1]["json"]["model"] == "gpt-5.4"
    assert client.calls[1]["json"]["reasoning"] == {"effort": "low"}
    assert client.calls[1]["json"]["service_tier"] == "priority"


@pytest.mark.asyncio
async def test_prompt_enhance_uses_bounded_upstream_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(
                200,
                [
                    _sse({"type": "response.output_text.delta", "delta": "ok"}),
                    _sse({"type": "response.completed"}),
                ],
            ),
        ]
    )
    captured: dict[str, Any] = {}

    def make_client(**kwargs: Any) -> _StubAsyncClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr(prompts.httpx, "AsyncClient", make_client)

    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [chunk async for chunk in prompts._stream_enhance("cat", [provider])]

    assert chunks == ['data: {"text": "ok"}\n\n', "data: [DONE]\n\n"]
    timeout = captured["timeout"]
    assert timeout.connect == prompts._PROMPT_ENHANCE_CONNECT_TIMEOUT_SECONDS
    assert timeout.read == prompts._PROMPT_ENHANCE_READ_TIMEOUT_SECONDS
    assert timeout.write == prompts._PROMPT_ENHANCE_WRITE_TIMEOUT_SECONDS
    assert timeout.pool == prompts._PROMPT_ENHANCE_POOL_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_prompt_enhance_fallback_can_drop_priority_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(400, raw=b"model not found"),
            _StubStreamResponse(400, raw=b"unsupported service_tier"),
            _StubStreamResponse(
                200,
                [
                    _sse({"type": "response.output_text.done", "text": "clean prompt"}),
                ],
            ),
        ]
    )
    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)

    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [chunk async for chunk in prompts._stream_enhance("cat", [provider])]

    assert chunks == [
        'data: {"text": "clean prompt"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert client.calls[2]["json"]["model"] == "gpt-5.4"
    assert client.calls[2]["json"]["reasoning"] == {"effort": "low"}
    assert "service_tier" not in client.calls[2]["json"]


@pytest.mark.asyncio
async def test_prompt_enhance_done_only_stream_is_terminal_failure_not_success_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(200, ["data: [DONE]\n\n"])
            for _attempt in prompts._ENHANCE_ATTEMPTS
        ]
    )
    calls: dict[str, Any] = {}

    async def charge(*_args: Any, **_kwargs: Any) -> None:
        calls["charged"] = True

    async def settle(
        _billing: Any,
        *,
        reason: str,
    ) -> None:
        calls["settled"] = reason

    async def release(*_args: Any, **_kwargs: Any) -> None:
        calls["released"] = True

    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(prompts, "_charge_prompt_enhance", charge)
    monkeypatch.setattr(prompts, "_settle_prompt_enhance_default_hold", settle)
    monkeypatch.setattr(prompts, "_release_prompt_enhance_hold", release)
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )
    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="done-only",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == ['data: {"error": "upstream_error"}\n\n']
    assert calls == {"settled": "no_success"}
    assert len(client.calls) == len(prompts._ENHANCE_ATTEMPTS)


@pytest.mark.asyncio
async def test_prompt_enhance_whitespace_only_stream_is_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(
                200,
                [
                    _sse(
                        {
                            "type": "response.output_text.delta",
                            "delta": "   ",
                        }
                    ),
                    _sse({"type": "response.completed"}),
                ],
            )
            for _attempt in prompts._ENHANCE_ATTEMPTS
        ]
    )
    calls: dict[str, Any] = {}

    async def charge(*_args: Any, **_kwargs: Any) -> None:
        calls["charged"] = True

    async def settle(
        _billing: Any,
        *,
        reason: str,
    ) -> None:
        calls["settled"] = reason

    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(prompts, "_charge_prompt_enhance", charge)
    monkeypatch.setattr(prompts, "_settle_prompt_enhance_default_hold", settle)
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )
    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="whitespace-only",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == ['data: {"error": "upstream_error"}\n\n']
    assert calls == {"settled": "no_success"}
    assert len(client.calls) == len(prompts._ENHANCE_ATTEMPTS)


@pytest.mark.asyncio
async def test_prompt_enhance_retries_response_failed_before_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(
                200,
                [
                    _sse(
                        {
                            "type": "response.failed",
                            "error": {"message": "temporary upstream failure"},
                        }
                    ),
                ],
            ),
            _StubStreamResponse(
                200,
                [
                    _sse(
                        {
                            "type": "response.completed",
                            "response": {
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "fallback prompt",
                                            }
                                        ],
                                    }
                                ]
                            },
                        }
                    ),
                ],
            ),
        ]
    )
    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)

    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [chunk async for chunk in prompts._stream_enhance("cat", [provider])]

    assert chunks == [
        'data: {"text": "fallback prompt"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert client.calls[1]["json"]["model"] == "gpt-5.4"


@pytest.mark.asyncio
async def test_prompt_enhance_keepalive_wraps_slow_stream() -> None:
    from app.routes import prompts

    async def delayed_stream():
        await asyncio.sleep(0.02)
        yield 'data: {"text": "ready"}\n\n'

    stream = prompts._stream_with_keepalive(  # noqa: SLF001
        delayed_stream(),
        interval_seconds=0.001,
    )

    assert await anext(stream) == ": keep-alive\n\n"
    assert await anext(stream) == ": keep-alive\n\n"
    chunks: list[str] = []
    for _ in range(20):
        chunk = await anext(stream)
        if chunk.startswith("data:"):
            chunks.append(chunk)
            break
    assert chunks == ['data: {"text": "ready"}\n\n']


@pytest.mark.asyncio
async def test_prompt_enhance_keepalive_teardown_closes_source() -> None:
    """Cancellation or explicit close of the keepalive wrapper must terminate
    the source generator deterministically instead of leaving it to async
    generator GC finalization (which can run after the request session is
    closed and makes the billing hold release fail)."""
    from app.routes import prompts

    events: list[str] = []

    async def slow_stream():
        try:
            yield 'data: {"text": "ready"}\n\n'
            await asyncio.sleep(60)
        finally:
            events.append("source.closed")

    # Consumer cancelled while suspended inside the generator chain.
    stream = prompts._stream_with_keepalive(  # noqa: SLF001
        slow_stream(),
        interval_seconds=10,
    )

    async def consume():
        assert await anext(stream) == ": keep-alive\n\n"
        assert await anext(stream) == 'data: {"text": "ready"}\n\n'
        await anext(stream)  # blocks on the queue while the source sleeps

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await asyncio.sleep(0.05)
    assert events == ["source.closed"]

    # Wrapper closed explicitly mid-stream (as GC finalization does when the
    # response is dropped without cancellation).
    events.clear()
    stream = prompts._stream_with_keepalive(  # noqa: SLF001
        slow_stream(),
        interval_seconds=10,
    )
    assert await anext(stream) == ": keep-alive\n\n"
    await stream.aclose()
    await asyncio.sleep(0.05)
    assert events == ["source.closed"]


@pytest.mark.asyncio
async def test_prompt_enhance_charges_completed_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(
                200,
                [
                    _sse({"type": "response.output_text.delta", "delta": "better"}),
                    _sse(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-1",
                                "model": "gpt-5.5",
                                "usage": {
                                    "input_tokens": 12,
                                    "output_tokens": 5,
                                },
                            },
                        }
                    ),
                ],
            ),
        ]
    )
    charged: list[
        tuple[prompts._EnhanceBillingContext, prompts._EnhanceUsageCapture]
    ] = []

    async def fake_charge(
        billing: prompts._EnhanceBillingContext,
        capture: prompts._EnhanceUsageCapture,
    ) -> None:
        charged.append((billing, capture))

    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(prompts, "_charge_prompt_enhance", fake_charge)
    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == [
        'data: {"text": "better"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert len(charged) == 1
    _billing, capture = charged[0]
    assert capture.response_id == "resp-1"
    assert capture.model == "gpt-5.5"
    assert capture.service_tier == "priority"
    assert capture.usage == {"input_tokens": 12, "output_tokens": 5}


@pytest.mark.asyncio
async def test_prompt_enhance_charge_uses_completion_wallet_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts
    from lumen_core.pricing import CostBreakdown

    calls: dict[str, Any] = {}
    audits: list[dict[str, Any]] = []

    class Db:
        async def commit(self) -> None:
            calls["committed"] = True

    async def estimate_breakdown(*_args: Any, **kwargs: Any) -> CostBreakdown:
        calls["estimate"] = kwargs
        return CostBreakdown(
            input_cost_micro=12,
            output_cost_micro=10,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=True,
            rate_multiplier_x10000=kwargs["rate_multiplier_x10000"],
            total_cost_micro=22,
            actual_cost_micro=22,
            pricing_source="db",
        )

    async def charge(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["charge"] = kwargs
        return SimpleNamespace(amount_micro=-22, balance_after=978)

    async def write_audit(_db: object, **kwargs: Any) -> bool:
        audits.append(kwargs)
        return True

    monkeypatch.setattr(
        prompts.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )
    monkeypatch.setattr(prompts.billing_core, "charge", charge)
    monkeypatch.setattr(prompts, "write_audit", write_audit)

    billing = prompts._EnhanceBillingContext(
        db=Db(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=15_000,
        cache_aware=True,
        allow_negative=False,
    )
    capture = prompts._EnhanceUsageCapture(
        provider_name="primary",
        model="gpt-5.5",
        service_tier="priority",
        response_id="resp-1",
        usage={"input_tokens": 12, "output_tokens": 5},
    )

    await prompts._charge_prompt_enhance(billing, capture)

    assert calls["estimate"]["model"] == "gpt-5.5"
    assert calls["estimate"]["tokens"].input_tokens == 12
    assert calls["estimate"]["tokens"].output_tokens == 5
    assert calls["estimate"]["service_tier"] == "priority"
    assert calls["estimate"]["rate_multiplier_x10000"] == 15_000
    assert calls["charge"]["kind"] == "charge_completion"
    assert calls["charge"]["ref_type"] == "prompt_enhance"
    assert calls["charge"]["ref_id"] == "enhance-1"
    assert calls["charge"]["idempotency_key"] == "prompt_enhance:enhance-1"
    assert calls["committed"] is True
    assert audits[-1]["event_type"] == "wallet.charge.completion"
    assert audits[-1]["details"]["route"] == "prompts.enhance"


@pytest.mark.asyncio
async def test_prompt_enhance_settles_default_hold_when_charge_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(
                200,
                [
                    _sse({"type": "response.output_text.delta", "delta": "better"}),
                    _sse(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-1",
                                "model": "gpt-5.5",
                                "usage": {
                                    "input_tokens": 12,
                                    "output_tokens": 5,
                                },
                            },
                        }
                    ),
                ],
            ),
        ]
    )
    calls: dict[str, Any] = {}

    async def fail_charge(
        _billing: prompts._EnhanceBillingContext,
        _capture: prompts._EnhanceUsageCapture,
    ) -> None:
        raise RuntimeError("db commit failed")

    async def settle_default(
        _billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["settle_reason"] = reason

    async def release_hold(
        _billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["released"] = reason

    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(prompts, "_charge_prompt_enhance", fail_charge)
    monkeypatch.setattr(prompts, "_settle_prompt_enhance_default_hold", settle_default)
    monkeypatch.setattr(prompts, "_release_prompt_enhance_hold", release_hold)

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == [
        'data: {"text": "better"}\n\n',
        'data: {"error": "billing_failed"}\n\n',
    ]
    # 内容已交付、上游必然已计费:计费失败改为默认金额结算,不得 fail-open 释放
    assert calls["settle_reason"] == "charge_failed"
    assert "released" not in calls


@pytest.mark.asyncio
async def test_prompt_enhance_audit_failure_never_falls_back_to_default_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.audit import AuditPersistenceError
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(
                200,
                [
                    _sse({"type": "response.output_text.delta", "delta": "better"}),
                    _sse(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-1",
                                "model": "gpt-5.5",
                                "usage": {
                                    "input_tokens": 12,
                                    "output_tokens": 5,
                                },
                            },
                        }
                    ),
                ],
            ),
        ]
    )
    calls: dict[str, Any] = {}

    class Db:
        async def rollback(self) -> None:
            calls["rolled_back"] = True

    async def fail_charge(
        _billing: prompts._EnhanceBillingContext,
        _capture: prompts._EnhanceUsageCapture,
    ) -> None:
        raise AuditPersistenceError("billing.pricing.fallback_used")

    async def settle_default(
        _billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["settled"] = reason

    async def release_hold(
        _billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["released"] = reason

    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(prompts, "_charge_prompt_enhance", fail_charge)
    monkeypatch.setattr(prompts, "_settle_prompt_enhance_default_hold", settle_default)
    monkeypatch.setattr(prompts, "_release_prompt_enhance_hold", release_hold)

    billing = prompts._EnhanceBillingContext(
        db=Db(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-audit-failure",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == [
        'data: {"text": "better"}\n\n',
        'data: {"error": "billing_failed"}\n\n',
    ]
    assert calls == {"rolled_back": True}
    assert billing.settle_outcome.attempted is True


@pytest.mark.asyncio
async def test_prompt_enhance_false_transactional_audit_stops_before_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.audit import AuditPersistenceError
    from app.routes import prompts
    from lumen_core.pricing import CostBreakdown

    calls: dict[str, Any] = {}

    class Db:
        async def commit(self) -> None:
            calls["committed"] = True

    async def estimate_breakdown(*_args: Any, **_kwargs: Any) -> CostBreakdown:
        return CostBreakdown(
            input_cost_micro=20,
            output_cost_micro=0,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=10_000,
            total_cost_micro=20,
            actual_cost_micro=20,
            pricing_source="fallback",
        )

    async def fail_if_settled(*_args: Any, **_kwargs: Any) -> None:
        calls["settled"] = True

    async def false_audit(*_args: Any, **_kwargs: Any) -> bool:
        calls["audit"] = True
        return False

    monkeypatch.setattr(
        prompts.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )
    monkeypatch.setattr(prompts.billing_core, "settle", fail_if_settled)
    monkeypatch.setattr(prompts, "write_audit", false_audit)

    billing = prompts._EnhanceBillingContext(
        db=Db(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-false-audit",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    capture = prompts._EnhanceUsageCapture(
        provider_name="primary",
        model="gpt-5.5",
        response_id="resp-1",
        usage={"input_tokens": 12, "output_tokens": 5},
    )

    with pytest.raises(AuditPersistenceError):
        await prompts._charge_prompt_enhance(billing, capture)

    assert calls == {"audit": True}


@pytest.mark.asyncio
async def test_prompt_enhance_default_settlement_retries_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts

    calls: dict[str, Any] = {"settle": 0, "rollback": 0, "commit": 0}

    class Db:
        async def rollback(self) -> None:
            calls["rollback"] += 1

        async def commit(self) -> None:
            calls["commit"] += 1

    async def flaky_settle(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        calls["settle"] += 1
        if calls["settle"] < 3:
            raise RuntimeError("transient db failure")
        return SimpleNamespace(id="settle-1")

    async def invalidate(_user_id: str) -> None:
        calls["invalidated"] = True

    async def no_sleep(_delay: float) -> None:
        return None

    async def no_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(prompts.billing_core, "settle", flaky_settle)
    monkeypatch.setattr(prompts, "invalidate_balance_cache", invalidate)
    monkeypatch.setattr(prompts._prompt_billing.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        prompts._prompt_billing,
        "_audit_default_settlement",
        no_audit,
    )

    billing = prompts._EnhanceBillingContext(
        db=Db(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-retry-settle",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )

    await prompts._settle_prompt_enhance_default_hold(
        billing,
        reason="charge_failed",
    )

    assert calls == {
        "settle": 3,
        "rollback": 2,
        "commit": 1,
        "invalidated": True,
    }
    assert billing.settle_outcome.attempted is True


@pytest.mark.asyncio
async def test_prompt_enhance_default_settlement_audit_failure_never_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.audit import AuditPersistenceError
    from app.routes import prompts

    calls: dict[str, int] = {"settle": 0, "audit": 0, "rollback": 0, "commit": 0}

    class Db:
        async def rollback(self) -> None:
            calls["rollback"] += 1

        async def commit(self) -> None:
            calls["commit"] += 1

    async def settle(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        calls["settle"] += 1
        return SimpleNamespace(
            id=f"settle-{calls['settle']}",
            amount_micro=0,
            balance_after=9_000,
        )

    async def fail_audit(*_args: Any, **_kwargs: Any) -> None:
        calls["audit"] += 1
        raise AuditPersistenceError("wallet.charge.completion")

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(prompts.billing_core, "settle", settle)
    monkeypatch.setattr(
        prompts._prompt_billing,
        "_audit_default_settlement",
        fail_audit,
    )
    monkeypatch.setattr(prompts._prompt_billing.asyncio, "sleep", no_sleep)

    billing = prompts._EnhanceBillingContext(
        db=Db(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-default-audit-failure",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )

    await prompts._settle_prompt_enhance_default_hold(
        billing,
        reason="charge_failed",
    )

    assert calls == {
        "settle": 3,
        "audit": 3,
        "rollback": 3,
        "commit": 0,
    }


@pytest.mark.asyncio
async def test_prompt_enhance_settles_default_hold_when_success_has_no_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient(
        [
            _StubStreamResponse(
                200,
                [
                    _sse({"type": "response.output_text.delta", "delta": "better"}),
                    _sse(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-1",
                                "model": "gpt-5.5",
                            },
                        }
                    ),
                ],
            ),
        ]
    )
    calls: dict[str, Any] = {}

    async def settle_default(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
        runtime: prompts._prompt_billing.BillingRuntime,
    ) -> None:
        assert isinstance(runtime, prompts._prompt_billing.BillingRuntime)
        calls["settle_user"] = billing.user_id if billing is not None else None
        calls["settle_reason"] = reason

    async def estimate_breakdown(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(
            "missing usage must settle the default hold before pricing"
        )

    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(
        prompts._prompt_billing,
        "settle_prompt_enhance_default_hold",
        settle_default,
    )
    monkeypatch.setattr(
        prompts.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == ['data: {"text": "better"}\n\n', "data: [DONE]\n\n"]
    # 内容已完整交付、上游必然已计费:用量缺失按默认金额结算,不得 fail-open 释放
    assert calls == {"settle_user": "user-1", "settle_reason": "missing_usage"}


@pytest.mark.asyncio
async def test_prompt_enhance_settles_default_hold_when_stream_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}

    async def cancelled_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
        *,
        on_dispatched: Callable[[], None] | None = None,
    ):
        # 模拟真实 upstream:POST 发出后先回调 dispatch,再产出内容
        if on_dispatched is not None:
            on_dispatched()
        yield 'data: {"text": "partial"}\n\n'
        raise asyncio.CancelledError()

    async def settle_after_cancel(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
        runtime: prompts._PromptRuntime,
    ) -> None:
        assert isinstance(runtime, prompts._PromptRuntime)
        calls["settle_user"] = billing.user_id if billing is not None else None
        calls["settle_reason"] = reason

    monkeypatch.setattr(prompts, "_stream_enhance_one", cancelled_stream)
    monkeypatch.setattr(
        prompts,
        "_settle_prompt_enhance_hold_after_cancel",
        settle_after_cancel,
    )

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )
    stream = prompts._stream_enhance("cat", [provider], billing)

    assert await anext(stream) == 'data: {"text": "partial"}\n\n'
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)

    # 已产出内容后断流:上游已按 token 计费,按默认金额结算而非 fail-open 释放
    assert calls == {"settle_user": "user-1", "settle_reason": "stream_cancelled"}


@pytest.mark.asyncio
async def test_prompt_enhance_releases_hold_when_cancelled_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST 发出前(阻塞在代理解析 await)客户端取消:请求从未送达上游,
    可证明未扣费 → 释放 hold,不得按默认金额结算。

    回归:曾因 state.started 在候选生成器启动前即置位,取消分支据此按 hold
    全额结算;修复后 dispatch 由 upstream 在 POST 真正发出时回调上报。
    """
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}

    async def stalled_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
        *,
        on_dispatched: Callable[[], None] | None = None,
    ):
        # 模拟阻塞在 resolve_provider_proxy_url(发送前第一个 await):
        # 永不回调 dispatch,POST 从未发出。
        del on_dispatched
        await asyncio.sleep(60)
        yield "unreachable"  # pragma: no cover - async generator marker

    async def release_after_cancel(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
        runtime: prompts._PromptRuntime,
    ) -> None:
        assert isinstance(runtime, prompts._PromptRuntime)
        calls["release_user"] = billing.user_id if billing is not None else None
        calls["release_reason"] = reason

    async def settle_after_cancel(
        _billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
        runtime: prompts._PromptRuntime,
    ) -> None:
        calls["settled"] = reason

    monkeypatch.setattr(prompts, "_stream_enhance_one", stalled_stream)
    monkeypatch.setattr(
        prompts,
        "_release_prompt_enhance_hold_after_cancel",
        release_after_cancel,
    )
    monkeypatch.setattr(
        prompts,
        "_settle_prompt_enhance_hold_after_cancel",
        settle_after_cancel,
    )

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )
    stream = prompts._stream_enhance("cat", [provider], billing)

    # 首个 anext 启动生成器,阻塞在代理解析 await;取消该读取任务 = 客户端断流
    reader = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader

    # POST 从未发出:释放 hold,不得按默认金额结算
    assert calls == {"release_user": "user-1", "release_reason": "stream_cancelled"}
    assert "settled" not in calls


@pytest.mark.asyncio
async def test_prompt_enhance_settles_default_hold_when_stream_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}

    async def slow_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
        *,
        on_dispatched: Callable[[], None] | None = None,
    ):
        # 模拟真实 upstream:POST 发出后先回调 dispatch,再产出内容
        if on_dispatched is not None:
            on_dispatched()
        yield 'data: {"text": "partial"}\n\n'
        await asyncio.sleep(60)

    async def settle_default(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["settle_user"] = billing.user_id if billing is not None else None
        calls["settle_reason"] = reason

    monkeypatch.setattr(prompts, "_stream_enhance_one", slow_stream)
    monkeypatch.setattr(prompts, "_settle_prompt_enhance_default_hold", settle_default)

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )
    stream = prompts._stream_enhance("cat", [provider], billing)

    assert await anext(stream) == 'data: {"text": "partial"}\n\n'
    await stream.aclose()

    # 已产出内容后关闭:上游已按 token 计费,按默认金额结算而非 fail-open 释放
    assert calls == {"settle_user": "user-1", "settle_reason": "stream_cancelled"}


@pytest.mark.asyncio
async def test_prompt_enhance_settles_default_hold_after_provider_error_post_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已产出内容后上游失败(流式读超时)→ 按默认金额结算,不得 release。"""
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}

    async def failing_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
        *,
        on_dispatched: Callable[[], None] | None = None,
    ):
        # 模拟真实 upstream:POST 发出后先回调 dispatch,再产出内容
        if on_dispatched is not None:
            on_dispatched()
        yield 'data: {"text": "partial"}\n\n'
        raise prompts._EnhanceProviderError("timeout", retryable=True)

    async def settle_default(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["settle_user"] = billing.user_id if billing is not None else None
        calls["settle_reason"] = reason

    async def release_hold(
        _billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["released"] = reason

    monkeypatch.setattr(prompts, "_stream_enhance_one", failing_stream)
    monkeypatch.setattr(prompts, "_settle_prompt_enhance_default_hold", settle_default)
    monkeypatch.setattr(prompts, "_release_prompt_enhance_hold", release_hold)

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == [
        'data: {"text": "partial"}\n\n',
        'data: {"error": "timeout"}\n\n',
    ]
    assert calls == {
        "settle_user": "user-1",
        "settle_reason": "provider_error_after_emit",
    }
    assert "released" not in calls


@pytest.mark.asyncio
async def test_prompt_enhance_releases_hold_when_all_candidates_fail_pre_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部候选在连接层失败(请求从未送达,可证明未扣费)→ no_success 仍 release。"""
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}

    async def connect_failed_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
        *,
        on_dispatched: Callable[[], None] | None = None,
    ):
        # 连接层失败:POST 从未发出,不回调 dispatch
        del on_dispatched
        raise prompts._EnhanceProviderError(
            "ConnectError", retryable=True, no_upstream_cost=True
        )
        yield  # pragma: no cover - async generator marker

    async def release_hold(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["release_user"] = billing.user_id if billing is not None else None
        calls["release_reason"] = reason

    async def settle_default(
        _billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["settled"] = reason

    monkeypatch.setattr(prompts, "_stream_enhance_one", connect_failed_stream)
    monkeypatch.setattr(prompts, "_release_prompt_enhance_hold", release_hold)
    monkeypatch.setattr(prompts, "_settle_prompt_enhance_default_hold", settle_default)

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == ['data: {"error": "upstream_error"}\n\n']
    assert calls == {"release_user": "user-1", "release_reason": "no_success"}
    assert "settled" not in calls


@pytest.mark.asyncio
async def test_prompt_enhance_dispatch_checkpoint_failure_never_calls_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    client = _StubAsyncClient([])
    calls: dict[str, Any] = {"outcomes": []}

    async def checkpoint_dispatch() -> None:
        calls["checkpoint_attempts"] = calls.get("checkpoint_attempts", 0) + 1
        raise RuntimeError("dispatch checkpoint unavailable")

    async def checkpoint_outcome(cost_possible: bool) -> None:
        calls["outcomes"].append(cost_possible)

    async def release_hold(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["released"] = (billing.user_id if billing is not None else None, reason)

    async def settle_default(
        _billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["settled"] = reason

    monkeypatch.setattr(prompts.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(prompts, "_release_prompt_enhance_hold", release_hold)
    monkeypatch.setattr(prompts, "_settle_prompt_enhance_default_hold", settle_default)
    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [
        chunk
        async for chunk in prompts._stream_enhance(
            "cat",
            [provider],
            billing,
            record_dispatch_intent=checkpoint_dispatch,
            record_candidate_outcome=checkpoint_outcome,
        )
    ]

    assert chunks == ['data: {"error": "internal"}\n\n']
    assert client.calls == []
    assert calls["checkpoint_attempts"] == len(prompts._ENHANCE_ATTEMPTS)
    assert calls["outcomes"] == [False] * len(prompts._ENHANCE_ATTEMPTS)
    assert calls["released"] == ("user-1", "no_success")
    assert "settled" not in calls


@pytest.mark.asyncio
async def test_prompt_enhance_settles_default_hold_when_candidates_fail_post_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部候选在读超时/处理中失败(请求已送达,结果不可知)→ no_success 按默认结算。"""
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}

    async def read_timeout_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
        *,
        on_dispatched: Callable[[], None] | None = None,
    ):
        # 读超时 = POST 已发出:先回调 dispatch,再抛读超时错误
        if on_dispatched is not None:
            on_dispatched()
        raise prompts._EnhanceProviderError("timeout", retryable=True)
        yield  # pragma: no cover - async generator marker

    async def settle_default(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["settle_user"] = billing.user_id if billing is not None else None
        calls["settle_reason"] = reason

    async def release_hold(
        _billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["released"] = reason

    monkeypatch.setattr(prompts, "_stream_enhance_one", read_timeout_stream)
    monkeypatch.setattr(prompts, "_settle_prompt_enhance_default_hold", settle_default)
    monkeypatch.setattr(prompts, "_release_prompt_enhance_hold", release_hold)

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == ['data: {"error": "timeout"}\n\n']
    assert calls == {"settle_user": "user-1", "settle_reason": "no_success"}
    assert "released" not in calls


@pytest.mark.asyncio
async def test_prompt_enhance_releases_hold_on_upstream_rejection_before_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游非 2xx 显式拒绝(未产出内容,可证明未扣费)→ 仍 release。"""
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}

    async def rejected_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
        *,
        on_dispatched: Callable[[], None] | None = None,
    ):
        del on_dispatched
        raise prompts._EnhanceProviderError(
            "upstream http 404", retryable=False, no_upstream_cost=True
        )
        yield  # pragma: no cover - async generator marker

    async def release_hold(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["release_user"] = billing.user_id if billing is not None else None
        calls["release_reason"] = reason

    async def settle_default(
        _billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["settled"] = reason

    monkeypatch.setattr(prompts, "_stream_enhance_one", rejected_stream)
    monkeypatch.setattr(prompts, "_release_prompt_enhance_hold", release_hold)
    monkeypatch.setattr(prompts, "_settle_prompt_enhance_default_hold", settle_default)

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )

    chunks = [
        chunk async for chunk in prompts._stream_enhance("cat", [provider], billing)
    ]

    assert chunks == ['data: {"error": "upstream_error"}\n\n']
    assert calls == {"release_user": "user-1", "release_reason": "provider_error"}
    assert "settled" not in calls


@pytest.mark.asyncio
async def test_prompt_enhance_guarded_response_releases_orphan_hold_on_unstarted_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首字节窗口断连(响应从未迭代 body)→ 链外守卫兜底释放孤儿 hold。"""
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}
    body_iterated = False

    def schedule_release(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
        runtime: prompts._PromptRuntime,
    ) -> asyncio.Task[None] | None:
        assert isinstance(runtime, prompts._PromptRuntime)
        calls["release_user"] = billing.user_id if billing is not None else None
        calls["release_reason"] = reason
        return None

    async def slow_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
        *,
        on_dispatched: Callable[[], None] | None = None,
    ):
        del on_dispatched
        nonlocal body_iterated
        body_iterated = True
        yield 'data: {"text": "partial"}\n\n'
        await asyncio.sleep(60)

    monkeypatch.setattr(prompts, "_stream_enhance_one", slow_stream)
    monkeypatch.setattr(
        prompts,
        "_schedule_prompt_enhance_hold_release",
        schedule_release,
    )

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )
    runtime = prompts._PromptRuntime()
    response = prompts._GuardedEnhanceStreamingResponse(
        prompts._stream_with_keepalive(
            prompts._stream_enhance("cat", [provider], billing, runtime=runtime)
        ),
        media_type="text/event-stream",
        on_teardown=prompts._schedule_orphan_hold_release(billing, runtime),
    )

    async def disconnected_send(_message: dict[str, Any]) -> None:
        raise RuntimeError("client disconnected")

    # 模拟客户端在首字节发送前断开:send(start) 抛异常,body 从未被迭代
    with pytest.raises(RuntimeError, match="client disconnected"):
        await response.stream_response(disconnected_send)

    assert body_iterated is False
    assert calls == {"release_user": "user-1", "release_reason": "stream_orphaned"}


@pytest.mark.asyncio
async def test_prompt_enhance_guarded_response_completes_without_orphan_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常流完成路径:链内 charge/settle 已消费 hold,兜底 release 不触发额外释放。"""
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}

    def schedule_release(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
        runtime: prompts._PromptRuntime,
    ) -> asyncio.Task[None] | None:
        calls["orphan_release"] = reason
        return None

    async def fast_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
        *,
        on_dispatched: Callable[[], None] | None = None,
    ):
        # 模拟真实 upstream:POST 发出后先回调 dispatch,再产出内容
        if on_dispatched is not None:
            on_dispatched()
        yield 'data: {"text": "hi"}\n\n'

    async def charge_hold(
        billing: prompts._EnhanceBillingContext,
        capture: prompts._EnhanceUsageCapture,
    ) -> None:
        calls["charged"] = billing.user_id

    monkeypatch.setattr(prompts, "_stream_enhance_one", fast_stream)
    monkeypatch.setattr(prompts, "_charge_prompt_enhance", charge_hold)
    monkeypatch.setattr(
        prompts,
        "_schedule_prompt_enhance_hold_release",
        schedule_release,
    )

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )
    runtime = prompts._PromptRuntime()
    response = prompts._GuardedEnhanceStreamingResponse(
        prompts._stream_with_keepalive(
            prompts._stream_enhance("cat", [provider], billing, runtime=runtime)
        ),
        media_type="text/event-stream",
        on_teardown=prompts._schedule_orphan_hold_release(billing, runtime),
    )

    received: list[str] = []

    async def collect_send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            received.append(message["body"])

    await response.stream_response(collect_send)

    assert calls["charged"] == "user-1"
    # 正常耗尽:failover 链内已 charge/settle,守卫不再调度 fresh-session 兜底任务
    assert "orphan_release" not in calls


@pytest.mark.asyncio
async def test_prompt_enhance_orphan_release_skipped_after_settle_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结算已调度/执行(settle_outcome.attempted)后,孤儿兜底释放必须跳过:
    结算失败时 hold 留在钱包中由管理端孤儿扫描对账,不得 fail-open 退款。"""
    from app.routes import prompts

    calls: dict[str, Any] = {}

    def schedule_release(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
        runtime: prompts._PromptRuntime,
    ) -> asyncio.Task[None] | None:
        calls["orphan_release"] = reason
        return None

    monkeypatch.setattr(
        prompts,
        "_schedule_prompt_enhance_hold_release",
        schedule_release,
    )

    def make_billing() -> prompts._EnhanceBillingContext:
        return prompts._EnhanceBillingContext(
            db=object(),  # type: ignore[arg-type]
            user_id="user-1",
            user_email="u@example.com",
            request_id="enhance-1",
            rate_multiplier_x10000=10_000,
            cache_aware=True,
            allow_negative=False,
            hold_amount_micro=10_000,
        )

    # 结算已被调度/执行:兜底跳过,hold 只由 settle 消费
    billing = make_billing()
    runtime = prompts._PromptRuntime()
    billing.settle_outcome.attempted = True
    prompts._schedule_orphan_hold_release(billing, runtime)()  # noqa: SLF001
    assert "orphan_release" not in calls

    # 真正的孤儿 hold(从未结算):仍兜底释放
    orphan = make_billing()
    prompts._schedule_orphan_hold_release(orphan, runtime)()  # noqa: SLF001
    assert calls == {"orphan_release": "stream_orphaned"}


@pytest.mark.asyncio
async def test_prompt_enhance_settle_schedule_marks_attempted_before_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """断流路径:detached 结算任务调度前同步置位 settle_outcome.attempted,
    链外孤儿兜底释放先于 detached settle 执行时也能看到并跳过,hold 不会被
    竞态中的 release 先行退款。"""
    from app.routes import prompts

    calls: dict[str, Any] = {}

    async def fake_settle_detached(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
    ) -> None:
        calls["settled"] = reason

    monkeypatch.setattr(
        prompts,
        "_settle_prompt_enhance_default_hold_detached",
        fake_settle_detached,
    )

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    runtime = prompts._PromptRuntime()
    task = prompts._schedule_prompt_enhance_default_settle(  # noqa: SLF001
        billing,
        reason="stream_cancelled",
        runtime=runtime,
    )
    assert task is not None
    # 标记在建任务之前同步置位:响应 teardown 先于 detached settle 执行时
    # 也能看到,从而跳过孤儿释放。
    assert billing.settle_outcome.attempted is True
    await task
    assert calls == {"settled": "stream_cancelled"}


@pytest.mark.asyncio
async def test_prompt_enhance_settle_failure_leaves_hold_for_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已 emit 内容后上游失败,默认结算落库失败:attempted 置位,兜底孤儿释放
    跳过,hold 保留在钱包中由管理端孤儿扫描对账,不得 fail-open 退款。"""
    from app.routes import prompts
    from lumen_core.providers import ProviderDefinition

    calls: dict[str, Any] = {}

    async def failing_stream(
        _text: str,
        _provider: ProviderDefinition,
        _attempt: prompts._EnhanceAttempt,
        _capture: prompts._EnhanceUsageCapture | None = None,
        *,
        on_dispatched: Callable[[], None] | None = None,
    ):
        # 模拟真实 upstream:POST 发出后先回调 dispatch,再产出内容
        if on_dispatched is not None:
            on_dispatched()
        yield 'data: {"text": "partial"}\n\n'
        raise prompts._EnhanceProviderError("timeout", retryable=True)

    async def failing_settle(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("db commit failed")

    def schedule_release(
        billing: prompts._EnhanceBillingContext | None,
        *,
        reason: str,
        runtime: prompts._PromptRuntime,
    ) -> asyncio.Task[None] | None:
        calls["orphan_release"] = reason
        return None

    monkeypatch.setattr(prompts, "_stream_enhance_one", failing_stream)
    monkeypatch.setattr(prompts.billing_core, "settle", failing_settle)
    monkeypatch.setattr(
        prompts,
        "_schedule_prompt_enhance_hold_release",
        schedule_release,
    )

    billing = prompts._EnhanceBillingContext(
        db=object(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    provider = ProviderDefinition(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-primary",
    )
    runtime = prompts._PromptRuntime()
    response = prompts._GuardedEnhanceStreamingResponse(
        prompts._stream_with_keepalive(
            prompts._stream_enhance("cat", [provider], billing, runtime=runtime)
        ),
        media_type="text/event-stream",
        on_teardown=prompts._schedule_orphan_hold_release(billing, runtime),
    )

    received: list[bytes] = []

    async def collect_send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            received.append(message["body"])

    await response.stream_response(collect_send)

    body = b"".join(received).decode("utf-8")
    assert 'data: {"text": "partial"}' in body
    assert 'data: {"error": "timeout"}' in body
    # 结算尝试已标记;无论响应兜底是否触发,孤儿释放都不得执行,hold 留待对账
    assert billing.settle_outcome.attempted is True
    assert "orphan_release" not in calls


@pytest.mark.asyncio
async def test_prompt_enhance_billing_preauthorizes_before_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts
    from lumen_core.pricing import CostBreakdown

    calls: dict[str, Any] = {}

    class Db:
        committed = False

        async def commit(self) -> None:
            self.committed = True

    async def true_setting(_db: Any) -> bool:
        return True

    async def false_setting(_db: Any) -> bool:
        return False

    async def snapshot(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.setdefault("snapshots", []).append(kwargs)
        return {"model": kwargs["model"]}

    def breakdown(
        _snapshot: dict[str, Any],
        **kwargs: Any,
    ) -> CostBreakdown:
        calls.setdefault("breakdowns", []).append(kwargs)
        return CostBreakdown(
            input_cost_micro=100,
            output_cost_micro=23,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=kwargs["rate_multiplier_x10000"],
            total_cost_micro=123,
            actual_cost_micro=123,
            pricing_source="snapshot",
        )

    async def hold(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["hold"] = kwargs
        return SimpleNamespace(id="tx-hold")

    async def invalidate(user_id: str) -> None:
        calls["invalidated"] = user_id

    monkeypatch.setattr(prompts, "_billing_enabled", true_setting)
    monkeypatch.setattr(prompts, "_billing_cache_aware", true_setting)
    monkeypatch.setattr(prompts, "_billing_allow_negative", false_setting)
    monkeypatch.setattr(prompts.billing_core, "completion_pricing_snapshot", snapshot)
    monkeypatch.setattr(
        prompts.billing_core,
        "completion_breakdown_from_snapshot",
        breakdown,
    )
    monkeypatch.setattr(prompts.billing_core, "hold", hold)
    monkeypatch.setattr(prompts, "invalidate_balance_cache", invalidate)

    db = Db()
    out = await prompts._prepare_prompt_enhance_billing(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        SimpleNamespace(
            id="user-1",
            email="u@example.com",
            account_mode="wallet",
            billing_rate_multiplier=1,
        ),
    )

    assert out is not None
    assert db.committed is True
    assert out.hold_amount_micro == 10_000
    assert {item["model"] for item in calls["snapshots"]} == {"gpt-5.4", "gpt-5.5"}
    assert all(item["rate_multiplier_x10000"] == 10_000 for item in calls["breakdowns"])
    assert calls["hold"]["ref_type"] == "prompt_enhance"
    assert calls["hold"]["ref_id"] == out.request_id
    assert calls["hold"]["idempotency_key"] == f"prompt_enhance:hold:{out.request_id}"
    assert len(calls["hold"]["meta"]["pricing_snapshots"]) == 3
    assert calls["invalidated"] == "user-1"


@pytest.mark.asyncio
async def test_prompt_enhance_takeover_reuses_billing_snapshot_without_second_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import prompts
    from app.routes.prompt_parts import idempotency

    operation = idempotency.prompt_enhance_operation(
        user_id="user-1",
        idempotency_key="stable-billing",
        operation_namespace=idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
        payload={"text": "cat"},
    )
    attempt = idempotency.PromptEnhanceAttempt(
        number=2,
        owner_token="takeover-owner",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        billing_request_id=operation.record_id,
    )
    snapshot = {
        "version": 1,
        "mode": "wallet",
        "request_id": operation.record_id,
        "user_id": "user-1",
        "rate_multiplier_x10000": 10_000,
        "cache_aware": True,
        "allow_negative": False,
        "hold_amount_micro": 10_000,
        "pricing_snapshots": {"gpt-5.5::priority": {"model": "gpt-5.5"}},
    }

    async def must_not_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("takeover must not create another billing hold")

    monkeypatch.setattr(
        prompts,
        "_prepare_prompt_enhance_billing",
        must_not_prepare,
    )
    billing, invalidate_hold = await prompts._prepare_reserved_billing(  # noqa: SLF001
        object(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1", email="u@example.com"),
        operation,
        idempotency.PromptEnhanceReservation(
            attempt=attempt,
            billing_snapshot=snapshot,
        ),
        runtime=prompts._PromptRuntime(),
    )

    assert billing is not None
    assert billing.request_id == operation.record_id
    assert billing.hold_amount_micro == 10_000
    assert invalidate_hold is False


@pytest.mark.asyncio
async def test_prompt_enhance_zero_rate_skips_preauthorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts
    from lumen_core.pricing import CostBreakdown

    calls: dict[str, Any] = {}

    class Db:
        committed = False

        async def commit(self) -> None:
            self.committed = True

    async def true_setting(_db: Any) -> bool:
        return True

    async def false_setting(_db: Any) -> bool:
        return False

    async def snapshot(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"model": kwargs["model"]}

    def breakdown(_snapshot: dict[str, Any], **kwargs: Any) -> CostBreakdown:
        return CostBreakdown(
            input_cost_micro=1,
            output_cost_micro=1,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=kwargs["rate_multiplier_x10000"],
            total_cost_micro=2,
            actual_cost_micro=0,
            pricing_source="snapshot",
        )

    async def fail_hold(*_args: Any, **_kwargs: Any) -> None:
        calls["hold"] = True
        raise AssertionError("zero-rate enhance must not reserve wallet balance")

    monkeypatch.setattr(prompts, "_billing_enabled", true_setting)
    monkeypatch.setattr(prompts, "_billing_cache_aware", true_setting)
    monkeypatch.setattr(prompts, "_billing_allow_negative", false_setting)
    monkeypatch.setattr(prompts.billing_core, "completion_pricing_snapshot", snapshot)
    monkeypatch.setattr(
        prompts.billing_core,
        "completion_breakdown_from_snapshot",
        breakdown,
    )
    monkeypatch.setattr(prompts.billing_core, "hold", fail_hold)

    db = Db()
    out = await prompts._prepare_prompt_enhance_billing(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        SimpleNamespace(
            id="user-1",
            email="u@example.com",
            account_mode="wallet",
            billing_rate_multiplier=0,
        ),
    )

    assert out is not None
    assert out.rate_multiplier_x10000 == 0
    assert out.hold_amount_micro == 0
    assert db.committed is False
    assert "hold" not in calls


@pytest.mark.asyncio
async def test_prompt_enhance_charge_settles_existing_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.routes import prompts
    from lumen_core.pricing import CostBreakdown

    calls: dict[str, Any] = {}

    class Db:
        committed = False

        async def commit(self) -> None:
            self.committed = True

    async def estimate_breakdown(*_args: Any, **kwargs: Any) -> CostBreakdown:
        return CostBreakdown(
            input_cost_micro=12,
            output_cost_micro=10,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=kwargs["rate_multiplier_x10000"],
            total_cost_micro=22,
            actual_cost_micro=22,
            pricing_source="db",
        )

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        calls["settle"] = kwargs
        return SimpleNamespace(amount_micro=-22, balance_after=978)

    async def charge(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("preauthorized enhance must settle, not charge")

    async def write_audit(_db: object, **kwargs: Any) -> bool:
        calls["audit"] = kwargs
        return True

    async def invalidate(user_id: str) -> None:
        calls["invalidated"] = user_id

    monkeypatch.setattr(
        prompts.billing_core,
        "estimate_completion_breakdown",
        estimate_breakdown,
    )
    monkeypatch.setattr(prompts.billing_core, "settle", settle)
    monkeypatch.setattr(prompts.billing_core, "charge", charge)
    monkeypatch.setattr(prompts, "write_audit", write_audit)
    monkeypatch.setattr(prompts, "invalidate_balance_cache", invalidate)

    billing = prompts._EnhanceBillingContext(
        db=Db(),  # type: ignore[arg-type]
        user_id="user-1",
        user_email="u@example.com",
        request_id="enhance-1",
        rate_multiplier_x10000=10_000,
        cache_aware=True,
        allow_negative=False,
        hold_amount_micro=10_000,
    )
    capture = prompts._EnhanceUsageCapture(
        provider_name="primary",
        model="gpt-5.5",
        response_id="resp-1",
        usage={"input_tokens": 12, "output_tokens": 5},
    )

    await prompts._charge_prompt_enhance(billing, capture)  # noqa: SLF001

    assert calls["settle"]["ref_type"] == "prompt_enhance"
    assert calls["settle"]["ref_id"] == "enhance-1"
    assert calls["settle"]["actual_micro"] == 22
    assert calls["settle"]["idempotency_key"] == "prompt_enhance:settle:enhance-1"
    assert calls["audit"]["details"]["response_id"] == "resp-1"
    assert calls["invalidated"] == "user-1"
