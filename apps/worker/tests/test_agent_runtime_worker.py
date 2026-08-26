from __future__ import annotations

import asyncio
import json
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from arq.worker import Worker
from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import agent_reference_previews as agent_reference_previews_module
from app import agent_memory_context as agent_memory_context_module
from app import main
from app import observability as worker_observability
from app.agent_billing import agent_usage_tokens
from app.billing_parts.helpers import generation_agent_billing_meta
from app.agent_context import (
    AgentContextError,
    _capability,
    _current_turn_reference_rows,
    _current_prompt,
    _encode_reference_preview,
    _memory_context,
    _pack_history,
    _reference_visible_after,
    _runtime_tool_policy,
    _runtime_reasoning_effort,
    project_history_message,
    provider_envelope,
)
from app.agent_runtime_client import (
    AgentRuntimeClient,
    AgentRuntimeClientError,
    AgentRuntimeCompaction,
    AgentRuntimeEvent,
    AgentRuntimeHistoryMessage,
    AgentRuntimeImageDefaults,
    AgentRuntimeToolPolicy,
    AgentRuntimeProviderEnvelope,
    AgentRuntimeReference,
    AgentRuntimeRequest,
    _next_stream_chunk,
    _runtime_request_body,
    canonical_runtime_request,
    sign_runtime_request,
)
from app.tasks.agent_run_parts.contracts import AgentRuntimeAccumulator
from app.tasks.agent_run_parts import orchestrator as agent_orchestrator
from app.tasks.agent_run_parts.orchestrator import _terminal_request
from app.tasks.agent_run_parts.persistence import _repair_tools
from lumen_core.model_base import Base
from lumen_core.model_entities import (
    AgentRunReference,
    AgentToolCall,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    User,
)
from lumen_core.agent_capability import verify_agent_capability


TEST_SECRET = "runtime-test-secret-0123456789-abcdef"


def _request() -> AgentRuntimeRequest:
    return AgentRuntimeRequest(
        run_id="run-1",
        agent_session_id="session-1",
        user_id="user-1",
        execution_epoch=1,
        user_message_id="user-message-1",
        assistant_message_id="message-1",
        trace_id="0123456789abcdef0123456789abcdef",
        provider=AgentRuntimeProviderEnvelope(
            provider_id="lumen-provider",
            api="openai-responses",
            base_url="https://provider.example/v1",
            api_key="provider-secret",
            headers={},
            proxy_url=None,
            resolved_ips=[],
            model="gpt-agent",
            context_window=128000,
            max_output_tokens=4096,
            reasoning_supported=True,
            vision_supported=False,
        ),
        system_prompt="Lumen Agent system prompt",
        history=[],
        compaction=None,
        current_prompt="Reply briefly",
        references=[],
        allowed_tools=[],
        image_defaults=AgentRuntimeImageDefaults(
            count=1,
            aspect_ratio="1:1",
            quality="2k",
            render_quality="high",
            background="auto",
            output_format="webp",
        ),
        tool_gateway_url=None,
        tool_capability=None,
        reasoning_effort="low",
        tool_policy=AgentRuntimeToolPolicy(
            max_image_tool_calls=2,
            max_images_per_run=4,
        ),
    )


def _line(event_type: str, seq: int, **extra: Any) -> bytes:
    return (
        json.dumps(
            {
                "version": 1,
                "type": event_type,
                "seq": seq,
                "run_id": "run-1",
                "execution_epoch": 1,
                **extra,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _terminal_stream() -> bytes:
    usage = {
        "input_tokens": 12,
        "output_tokens": 4,
        "cache_read_tokens": 2,
        "cache_write_tokens": 0,
        "total_tokens": 18,
    }
    return b"".join(
        (
            _line("run.started", 1, tools=[], runtime_version="pi-0.84.2"),
            _line("provider.dispatched", 2, turn=1),
            _line("provider.response", 3, turn=1, status=200),
            _line("run.heartbeat", 4),
            _line("text.delta", 5, delta="hello"),
            _line("turn.completed", 6, turn=1, usage=usage, stop_reason="stop"),
            _line(
                "run.completed",
                7,
                status="succeeded",
                error_code=None,
                usage=usage,
                turn_count=1,
                tool_call_count=0,
                provider_dispatch_count=1,
                provider_completed_count=1,
            ),
        )
    )


def test_runtime_signing_matches_node_test_vector() -> None:
    body = b'{"run_id":"run-1"}'
    canonical = canonical_runtime_request(
        "post",
        "/v1/runs",
        "1700000000",
        "nonce-0123456789",
        body,
    )
    assert canonical.decode() == (
        "v1\nPOST\n/v1/runs\n1700000000\nnonce-0123456789\n"
        "923135756928e8f394c6f67aac4b80ee48f168af81e526267db142176f625896"
    )
    assert (
        sign_runtime_request(
            TEST_SECRET,
            "POST",
            "/v1/runs",
            "1700000000",
            "nonce-0123456789",
            body,
        )
        == "9529a430573f53c55e9df878ad284705db0cee2b4196cd2c3fc03da45eb09bd2"
    )


@pytest.mark.asyncio
async def test_each_heartbeat_resets_the_worker_idle_budget() -> None:
    async def delayed_chunks():
        for sequence in range(1, 4):
            await asyncio.sleep(0.01)
            yield _line("run.heartbeat", sequence)

    iterator = delayed_chunks().__aiter__()
    chunks = [
        await _next_stream_chunk(
            iterator,
            cancel_requested=None,
            timeout_seconds=0.02,
        )
        for _ in range(3)
    ]

    assert [json.loads(chunk)["seq"] for chunk in chunks] == [1, 2, 3]


@pytest.mark.asyncio
async def test_runtime_client_validates_signed_monotonic_terminal_stream() -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = await request.aread()
        seen["signature"] = request.headers["x-lumen-agent-signature"]
        assert request.headers["x-lumen-agent-nonce"]
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=_terminal_stream(),
        )

    client = AgentRuntimeClient(
        base_url="http://agent-runtime:8090",
        shared_secret=TEST_SECRET,
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    starting = 0

    async def on_starting() -> None:
        nonlocal starting
        starting += 1

    events = [
        event
        async for event in client.stream(
            _request(),
            on_request_starting=on_starting,
        )
    ]
    await client.close()

    assert starting == 1
    assert [event.seq for event in events] == list(range(1, 8))
    assert events[-1].type == "run.completed"
    assert events[2].status == 200
    assert b"provider-secret" in seen["body"]
    assert "provider-secret" not in repr(_request())
    assert len(seen["signature"]) == 64


@pytest.mark.asyncio
async def test_runtime_client_rechecks_cancellation_after_starting_fence() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    client = AgentRuntimeClient(
        base_url="http://agent-runtime:8090",
        shared_secret=TEST_SECRET,
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    cancelled = asyncio.Event()

    async def cancel_at_fence() -> None:
        cancelled.set()

    with pytest.raises(AgentRuntimeClientError) as captured:
        _ = [
            event
            async for event in client.stream(
                _request(),
                cancel_requested=cancelled,
                on_request_starting=cancel_at_fence,
            )
        ]
    await client.close()

    assert captured.value.code == "agent_cancelled"
    assert captured.value.delivery == "proven_absent"
    assert requests == 0


@pytest.mark.asyncio
async def test_runtime_client_refuses_to_downgrade_pi_native_lifecycle() -> None:
    payloads: list[dict[str, Any]] = []
    checkpoint_summary = "😀" * 15_000

    async def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads((await request.aread()).decode("utf-8")))
        return httpx.Response(422, json={"error": "invalid_runtime_request"})

    request = _request().model_copy(
        update={
            "history": [
                AgentRuntimeHistoryMessage(
                    message_id="history-user-1",
                    role="user",
                    text="legacy-compatible history",
                )
            ],
            "compaction": AgentRuntimeCompaction(
                summary=checkpoint_summary,
                first_kept_message_id="history-user-1",
                next_message_id="user-message-1",
                tokens_before=260_000,
            ),
        }
    )
    client = AgentRuntimeClient(
        base_url="http://agent-runtime:8090",
        shared_secret=TEST_SECRET,
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    starting = 0

    async def on_starting() -> None:
        nonlocal starting
        starting += 1

    with pytest.raises(AgentRuntimeClientError) as captured:
        _ = [
            event
            async for event in client.stream(
                request,
                on_request_starting=on_starting,
            )
        ]
    await client.close()

    assert starting == 1
    assert captured.value.code == "agent_runtime_rejected"
    assert captured.value.delivery == "proven_absent"
    assert len(payloads) == 1
    assert payloads[0]["compaction"]["summary"] == checkpoint_summary
    assert payloads[0]["history"][0]["message_id"] == "history-user-1"
    assert payloads[0]["version"] == 2
    assert payloads[0]["event_features"] == ["heartbeat-v1", "text-reset-v1"]
    assert payloads[0]["tool_policy"] == {
        "max_image_tool_calls": 2,
        "max_images_per_run": 4,
    }
    assert "operation" not in payloads[0]
    assert "thinking_level_map" not in payloads[0]["provider"]
    assert "tool_calls" not in payloads[0]["history"][0]


def test_runtime_v3_serializes_typed_optional_fields_receiver_first() -> None:
    request = _request().model_copy(
        update={
            "version": 3,
            "operation": "prompt",
            "provider": _request().provider.model_copy(
                update={"thinking_level_map": {"max": "max"}}
            ),
            "history": [
                AgentRuntimeHistoryMessage(
                    message_id="assistant-1",
                    role="assistant",
                    text="tool turn",
                    api="openai-responses",
                    provider_id="provider-history",
                    model="model-history",
                    stop_reason="toolUse",
                    tool_calls=[
                        {
                            "id": "tool-1",
                            "name": "lumen_create_image",
                            "arguments": {"prompt": "x"},
                        }
                    ],
                    tool_results=[
                        {
                            "tool_call_id": "tool-1",
                            "name": "lumen_create_image",
                            "text": '{"status":"succeeded"}',
                            "is_error": False,
                        }
                    ],
                )
            ],
        }
    )

    payload = json.loads(_runtime_request_body(request))

    assert payload["version"] == 3
    assert payload["operation"] == "prompt"
    assert payload["provider"]["thinking_level_map"] == {"max": "max"}
    assert payload["history"][0]["tool_calls"][0]["id"] == "tool-1"


@pytest.mark.asyncio
async def test_runtime_client_sends_large_v2_history_only_once() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(400, json={"error": "invalid_runtime_request"})

    request = _request().model_copy(
        update={
            "history": [
                AgentRuntimeHistoryMessage(
                    message_id=f"history-{index}",
                    role="user" if index % 2 == 0 else "assistant",
                    text="complete history",
                )
                for index in range(257)
            ]
        }
    )
    client = AgentRuntimeClient(
        base_url="http://agent-runtime:8090",
        shared_secret=TEST_SECRET,
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AgentRuntimeClientError) as captured:
        _ = [event async for event in client.stream(request)]
    await client.close()

    assert captured.value.code == "agent_runtime_rejected"
    assert captured.value.delivery == "proven_absent"
    assert requests == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage_calls", "provider_calls", "accepted"),
    [(7, 7, True), (8, 7, False)],
)
async def test_runtime_terminal_usage_is_bounded_by_reported_provider_calls(
    usage_calls: int,
    provider_calls: int,
    accepted: bool,
) -> None:
    input_tokens = 128_000 * usage_calls
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": input_tokens,
    }
    content = _line(
        "run.started",
        1,
        tools=[],
        runtime_version="pi-0.84.2",
    ) + _line(
        "run.completed",
        2,
        status="succeeded",
        error_code=None,
        usage=usage,
        turn_count=6,
        tool_call_count=0,
        provider_dispatch_count=provider_calls,
        provider_completed_count=provider_calls,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=content,
        )

    client = AgentRuntimeClient(
        base_url="http://agent-runtime:8090",
        shared_secret=TEST_SECRET,
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    if accepted:
        events = [event async for event in client.stream(_request())]
        assert events[-1].type == "run.completed"
    else:
        with pytest.raises(AgentRuntimeClientError) as captured:
            _ = [event async for event in client.stream(_request())]
        assert captured.value.code == "agent_runtime_usage_out_of_bounds"
    await client.close()


@pytest.mark.asyncio
async def test_runtime_client_rejects_oversized_request_before_delivery() -> None:
    client = AgentRuntimeClient(
        base_url="http://agent-runtime:8090",
        shared_secret=TEST_SECRET,
        max_request_bytes=128,
    )
    starting = 0

    async def on_starting() -> None:
        nonlocal starting
        starting += 1

    with pytest.raises(AgentRuntimeClientError) as captured:
        _ = [
            event
            async for event in client.stream(
                _request(),
                on_request_starting=on_starting,
            )
        ]
    assert captured.value.code == "agent_runtime_request_too_large"
    assert captured.value.delivery == "proven_absent"
    assert starting == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "code"),
    [
        (
            _line("run.started", 1, tools=[], runtime_version="pi-0.84.2"),
            "agent_runtime_terminal_missing",
        ),
        (
            _line("run.started", 2, tools=[], runtime_version="pi-0.84.2")
            + _line(
                "run.failed",
                3,
                status="failed",
                usage={
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "total_tokens": 0,
                },
                turn_count=0,
                tool_call_count=0,
            ),
            "agent_runtime_event_scope_mismatch",
        ),
        (
            _line("run.started", 1, tools=[]).replace(b"\n", b"\r\n"),
            "agent_runtime_invalid_framing",
        ),
        (
            _line("run.started", 1, tools=[], runtime_version="pi-0.84.2")
            + _line(
                "compaction.completed",
                2,
                checkpoint_version=1,
                pi_runtime_version="pi-0.84.2",
                summary="forged checkpoint",
                first_kept_message_id="message-outside-request",
                tokens_before=260_000,
                provider_call_count=1,
                usage={
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "cache_write_1h_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 2,
                },
            ),
            "agent_runtime_invalid_event",
        ),
    ],
)
async def test_runtime_client_rejects_missing_terminal_scope_and_crlf(
    content: bytes,
    code: str,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=content,
        )

    client = AgentRuntimeClient(
        base_url="http://agent-runtime:8090",
        shared_secret=TEST_SECRET,
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AgentRuntimeClientError) as captured:
        _ = [event async for event in client.stream(_request())]
    await client.close()
    assert captured.value.code == code


def test_agent_history_projection_omits_image_ids_and_raw_tool_payloads() -> None:
    message = Message(
        id="message-history",
        conversation_id="conversation-1",
        role="assistant",
        content={
            "text": "Image request accepted.",
            "attachments": [
                {
                    "image_id": "private-image-id",
                    "role": "product",
                    "label": "Product",
                }
            ],
            "tool_calls": [
                {
                    "name": "lumen_create_image",
                    "status": "succeeded",
                    "mode": "image_to_image",
                    "generation_count": 2,
                    "arguments": {"prompt": "private tool prompt"},
                }
            ],
            "_internal_error": "private stack",
        },
    )

    projected = project_history_message(message)

    assert projected is not None
    assert projected.message_id == "message-history"
    assert "Image request accepted" in projected.text
    assert "role product" in projected.text
    assert "jobs 2" in projected.text
    assert "private-image-id" not in projected.text
    assert "private tool prompt" not in projected.text
    assert "private stack" not in projected.text


def test_agent_history_projection_preserves_typed_tool_call_and_result() -> None:
    message = Message(
        id="assistant-history",
        conversation_id="conversation-1",
        role="assistant",
        content={"text": "Image accepted."},
    )
    run = SimpleNamespace(
        provider_name="provider-a",
        model="model-a",
        dispatch_jsonb={"provider_api": "openai-responses"},
    )
    tool = SimpleNamespace(
        pi_tool_call_id="pi-tool-1",
        name="lumen_create_image",
        arguments_jsonb={"prompt": "campaign image"},
        result_jsonb={"generation_ids": ["generation-1"]},
        status="succeeded",
        mode="text_to_image",
        error_code=None,
    )

    projected = project_history_message(message, run=run, tool_rows=[tool])

    assert projected is not None
    assert projected.api == "openai-responses"
    assert projected.model == "model-a"
    assert projected.stop_reason == "toolUse"
    assert projected.tool_calls[0].arguments == {"prompt": "campaign image"}
    assert projected.tool_results[0].is_error is False
    assert "generation-1" in projected.tool_results[0].text


def test_context_packing_counts_typed_tool_arguments_and_results() -> None:
    message = Message(
        id="assistant-tool-budget",
        conversation_id="conversation-1",
        role="assistant",
        content={"text": "Image accepted."},
    )
    run = SimpleNamespace(
        id="run-tool-budget",
        provider_name="provider-a",
        model="model-a",
        dispatch_jsonb={"provider_api": "openai-responses"},
    )
    tool = SimpleNamespace(
        pi_tool_call_id="pi-tool-budget",
        name="lumen_create_image",
        arguments_jsonb={"prompt": "x" * 20_000},
        result_jsonb={"generation_ids": ["generation-1"]},
        status="succeeded",
        mode="text_to_image",
        error_code=None,
    )
    provider = _request().provider.model_copy(
        update={"context_window": 4096, "max_output_tokens": 128}
    )

    with pytest.raises(AgentContextError) as captured:
        _pack_history(
            [message],
            provider=provider,
            system_prompt="system",
            current_prompt="current",
            max_output_tokens=128,
            runs_by_assistant={message.id: run},
            tools_by_run={run.id: [tool]},
        )

    assert captured.value.code == "agent_context_window_exceeded"


def test_history_projection_preserves_image_blocks_without_binary_placeholder() -> None:
    message = Message(
        id="user-history-image",
        conversation_id="conversation-1",
        role="user",
        content={
            "text": "Use this image",
            "attachments": [{"image_id": "private-image", "role": "reference"}],
        },
    )
    preview = AgentRuntimeReference(
        reference_label="ref_1",
        role="reference",
        display_label=None,
        mime_type="image/webp",
        data_base64="dGVzdA==",
        estimated_input_tokens=512,
    )

    projected = project_history_message(message, image_previews=[preview])

    assert projected is not None
    assert projected.images[0].data_base64 == "dGVzdA=="
    assert "binary omitted" not in projected.text


def test_current_prompt_preserves_complete_user_tail_before_optional_context() -> None:
    user = Message(
        id="prompt-user",
        conversation_id="conversation-1",
        role="user",
        content={"text": "request-start " + "x" * 9_950 + " request-tail"},
    )

    prompt = _current_prompt(user, [], "memory " * 10_000)

    assert len(prompt) <= 40_000
    assert prompt.endswith("request-tail")


@pytest.mark.asyncio
async def test_gpt_56_agent_envelope_preserves_context_and_defaults_to_auto() -> None:
    provider = SimpleNamespace(
        name="provider",
        proxy=None,
        base_url="https://provider.example/v1",
        api_key="secret",
        agent_api="openai-responses",
        agent_context_window=128_000,
        agent_max_output_tokens=16_384,
        agent_reasoning_supported=True,
        vision_supported=True,
    )

    envelope = await provider_envelope(provider, model="gpt-5.6-sol")
    effort = _runtime_reasoning_effort(
        SimpleNamespace(reasoning_effort=None),
        provider,
    )

    assert envelope.context_window == 128_000
    assert effort is None
    provider.agent_context_window = 272_000
    assert (
        await provider_envelope(provider, model="gpt-5.6-sol")
    ).context_window == 272_000
    provider.agent_reasoning_supported = False
    assert (
        _runtime_reasoning_effort(
            SimpleNamespace(reasoning_effort="max"),
            provider,
        )
        is None
    )


def test_reference_preview_is_valid_bounded_webp_and_rejects_invalid_bytes() -> None:
    source = io.BytesIO()
    PILImage.new("RGB", (2048, 1024), (210, 20, 30)).save(source, format="PNG")

    preview = _encode_reference_preview(source.getvalue(), 64 * 1024)

    assert len(preview) <= 64 * 1024
    with PILImage.open(io.BytesIO(preview)) as image:
        assert image.format == "WEBP"
        assert max(image.size) <= 1024
    with pytest.raises(AgentContextError) as captured:
        _encode_reference_preview(b"not-an-image", 64 * 1024)
    assert captured.value.code == "agent_reference_preview_invalid"


def test_current_turn_reference_selection_excludes_catalog_from_text_turns() -> None:
    rows = [
        SimpleNamespace(image_id="image-1", reference_label="ref_1"),
        SimpleNamespace(image_id="image-2", reference_label="ref_2"),
    ]
    text_only = Message(
        id="message-text-only",
        conversation_id="conversation-1",
        role="user",
        content={"text": "continue", "attachments": []},
    )
    selected = Message(
        id="message-selected",
        conversation_id="conversation-1",
        role="user",
        content={
            "text": "use one",
            "attachments": [{"image_id": "image-2"}],
        },
    )

    assert _current_turn_reference_rows(text_only, rows) == []
    assert _current_turn_reference_rows(selected, rows) == [rows[1]]


@pytest.mark.asyncio
async def test_agent_memory_failure_is_retried_and_reported_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def fail_memory(*_args: Any, **_kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("memory unavailable")

    monkeypatch.setattr(
        agent_memory_context_module.memory_extraction,
        "assemble_user_memory_prompt",
        fail_memory,
    )
    result = await _memory_context(
        SimpleNamespace(),
        run=SimpleNamespace(id="run-1", user_id="user-1"),
        conversation=SimpleNamespace(id="conversation-1", memory_disabled=False),
        current_user=SimpleNamespace(id="message-1", content={"text": "hello"}),
        redis=object(),
    )

    assert attempts == 2
    assert result == ([], [], "", "", "degraded")


@pytest.mark.asyncio
async def test_memory_integrity_failure_does_not_poison_context_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[User.__table__, Conversation.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as seed:
        user = User(
            id="memory-user",
            email="memory@example.test",
            display_name="Memory",
            account_mode="wallet",
        )
        conversation = Conversation(
            id="memory-conversation",
            user_id=user.id,
            title="Memory",
            default_params={},
        )
        seed.add_all([user, conversation])
        await seed.commit()

    attempts = 0

    async def fail_with_integrity(session, **_kwargs):
        nonlocal attempts
        attempts += 1
        session.add(
            User(
                id="memory-user",
                email=f"duplicate-{attempts}@example.test",
                display_name="Duplicate",
                account_mode="wallet",
            )
        )
        await session.flush()

    monkeypatch.setattr(agent_memory_context_module, "SessionLocal", factory)
    monkeypatch.setattr(
        agent_memory_context_module.memory_extraction,
        "assemble_user_memory_prompt",
        fail_with_integrity,
    )
    async with factory() as context_db:
        conversation = await context_db.get(Conversation, "memory-conversation")
        assert conversation is not None
        result = await _memory_context(
            context_db,
            run=SimpleNamespace(id="run-memory", user_id="memory-user"),
            conversation=conversation,
            current_user=SimpleNamespace(id="message-1", content={"text": "hello"}),
            redis=object(),
        )
        assert result[-1] == "degraded"
        assert (
            await context_db.scalar(select(User.id).where(User.id == "memory-user"))
            == "memory-user"
        )
    assert attempts == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_starting_fence_prevents_runtime_http_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched = False
    finished_error: AgentRuntimeClientError | None = None

    class RuntimeClient:
        async def stream(self, _request: Any, **kwargs: Any):
            nonlocal dispatched
            await kwargs["on_request_starting"]()
            dispatched = True
            if False:
                yield None

    class Background:
        cancel_requested = asyncio.Event()

        async def close(self) -> None:
            return None

    async def stale_fence(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def finish_failure(_state: Any, error: AgentRuntimeClientError) -> None:
        nonlocal finished_error
        finished_error = error

    monkeypatch.setattr(agent_orchestrator, "update_dispatch_state", stale_fence)
    monkeypatch.setattr(
        agent_orchestrator._BackgroundTasks,
        "start",
        lambda _state: Background(),
    )
    monkeypatch.setattr(agent_orchestrator, "_finish_runtime_failure", finish_failure)
    state = agent_orchestrator._PreparedExecution(
        ctx={},
        redis=object(),
        runtime_client=RuntimeClient(),
        claim=SimpleNamespace(run_id="run-1", execution_epoch=2),
        accumulator=AgentRuntimeAccumulator(),
        build=SimpleNamespace(
            request=SimpleNamespace(
                agent_session_id="session-1",
                trace_id="0" * 32,
                user_id="user-1",
                provider=SimpleNamespace(model="model-1"),
            ),
            provider=SimpleNamespace(name="provider-1"),
        ),
        pool=None,
    )

    await agent_orchestrator._run_prepared_execution(state)

    assert dispatched is False
    assert finished_error is not None
    assert finished_error.code == "agent_stale_execution_epoch"
    assert finished_error.delivery == "proven_absent"


def _running_tool(tool_id: str) -> AgentToolCall:
    return AgentToolCall(
        id=tool_id,
        agent_run_id="run-1",
        capability_id="capability-0123456789",
        pi_tool_call_id=f"pi-{tool_id}",
        ordinal=0,
        execution_epoch=1,
        name="lumen_create_image",
        mode="text_to_image",
        status="running",
        request_hash="a" * 64,
        semantic_key="b" * 64,
        arguments_jsonb={"prompt": "safe"},
        result_jsonb={},
        generation_count=0,
    )


def test_tool_terminal_repair_preserves_existing_generation_and_unknown_semantics() -> (
    None
):
    accepted = _running_tool("tool-accepted")
    unknown = _running_tool("tool-unknown")
    generation = Generation(
        id="generation-1",
        user_id="user-1",
        message_id="message-1",
        upstream_request={"agent_tool_call_id": accepted.id},
    )

    _repair_tools(
        [accepted, unknown],
        [generation],
        now=datetime.now(timezone.utc),
        unknown=True,
    )

    assert accepted.status == "succeeded"
    assert accepted.result_jsonb["generation_ids"] == ["generation-1"]
    assert accepted.generation_count == 1
    assert unknown.status == "timed_out"
    assert unknown.error_code == "agent_tool_result_unknown"


def test_agent_usage_and_runtime_accumulator_are_cache_aware() -> None:
    tokens = agent_usage_tokens(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 40,
            "cache_write_tokens": 5,
        }
    )
    assert tokens.input_tokens == 100
    assert tokens.output_tokens == 20
    assert tokens.cache_read_tokens == 40
    assert tokens.cache_creation_tokens == 5
    assert tokens.cache_creation_5m_tokens == 5

    detailed = agent_usage_tokens(
        {
            "output_tokens": 20,
            "reasoning_tokens": 7,
            "cache_write_tokens": 5,
            "cache_write_1h_tokens": 2,
        }
    )
    assert detailed.reasoning_tokens == 7
    assert detailed.cache_creation_5m_tokens == 3
    assert detailed.cache_creation_1h_tokens == 2

    accumulator = AgentRuntimeAccumulator()
    accumulator.provider_dispatch_count = 1
    accumulator.apply(
        AgentRuntimeEvent(
            version=1,
            type="provider.response",
            seq=1,
            run_id="run-1",
            execution_epoch=1,
            status=429,
            turn=1,
        )
    )
    assert accumulator.response_proves_no_cost is True
    accumulator.provider_response_statuses.append(500)
    assert accumulator.response_proves_no_cost is False


def test_agent_usage_is_monotonic_and_later_unknown_dispatch_stays_unknown() -> None:
    accumulator = AgentRuntimeAccumulator()
    accumulator.apply(
        AgentRuntimeEvent(
            version=1,
            type="provider.dispatched",
            seq=1,
            run_id="run-1",
            execution_epoch=1,
        )
    )
    accumulator.apply(
        AgentRuntimeEvent(
            version=1,
            type="turn.completed",
            seq=2,
            run_id="run-1",
            execution_epoch=1,
            turn=1,
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cache_write_1h_tokens": 0,
                "reasoning_tokens": 5,
                "total_tokens": 120,
            },
        )
    )
    accumulator.apply(
        AgentRuntimeEvent(
            version=1,
            type="provider.dispatched",
            seq=3,
            run_id="run-1",
            execution_epoch=1,
        )
    )
    accumulator.apply(
        AgentRuntimeEvent(
            version=1,
            type="run.completed",
            seq=4,
            run_id="run-1",
            execution_epoch=1,
            status="partial",
            error_code="agent_tool_result_unknown",
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cache_write_1h_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            },
            turn_count=1,
            tool_call_count=1,
            provider_dispatch_count=2,
            provider_completed_count=1,
        )
    )

    assert accumulator.usage["input_tokens"] == 100
    assert accumulator.usage["reasoning_tokens"] == 5
    assert _terminal_request(accumulator) == (
        "partial",
        "agent_tool_result_unknown",
        "unknown",
        "runtime_partial",
    )


def test_pi_retry_resets_streamed_text_before_regeneration() -> None:
    accumulator = AgentRuntimeAccumulator(
        text="truncated draft",
        pending_delta="truncated draft",
    )

    accumulator.apply(
        AgentRuntimeEvent(
            version=1,
            type="text.reset",
            seq=1,
            run_id="run-1",
            execution_epoch=1,
        )
    )
    accumulator.apply(
        AgentRuntimeEvent(
            version=1,
            type="text.delta",
            seq=2,
            run_id="run-1",
            execution_epoch=1,
            delta="complete answer",
        )
    )

    assert accumulator.text == "complete answer"
    assert accumulator.pending_delta == "complete answer"
    assert accumulator.text_reset_pending is True


def test_unknown_billing_does_not_override_pi_success() -> None:
    accumulator = AgentRuntimeAccumulator(text="complete answer")
    accumulator.terminal_status = "succeeded"
    accumulator.provider_dispatch_count = 2
    accumulator.provider_completed_count = 1
    accumulator.provider_response_statuses = [200, 200]

    assert _terminal_request(accumulator) == (
        "succeeded",
        None,
        "unknown",
        "runtime_success_with_unknown_billing",
    )


def test_agent_history_is_passed_complete_for_pi_native_compaction() -> None:
    rows = [
        Message(
            id=f"message-{index:03d}",
            conversation_id="conversation-context",
            role="assistant" if index % 2 else "user",
            content={"text": "context " * 500},
        )
        for index in range(130)
    ]
    provider = _request().provider.model_copy(
        update={"context_window": 272_000, "max_output_tokens": 4096}
    )

    packed = _pack_history(
        rows,
        provider=provider,
        system_prompt="system",
        current_prompt="current",
        max_output_tokens=4096,
        reference_count=0,
    )

    assert len(packed) == len(rows)


def test_agent_history_over_direct_limit_is_admitted_for_pi_pre_prompt_compaction() -> None:
    rows = [
        Message(
            id=f"compaction-message-{index:03d}",
            conversation_id="conversation-compaction-plan",
            role="assistant" if index % 2 else "user",
            content={"text": "x" * 4_000},
        )
        for index in range(85)
    ]
    provider = _request().provider.model_copy(
        update={"context_window": 128_000, "max_output_tokens": 16_384}
    )

    packed = _pack_history(
        rows,
        provider=provider,
        system_prompt="s" * 22_000,
        current_prompt="c" * 22_000,
        max_output_tokens=16_384,
        reference_count=0,
    )

    assert len(packed) == len(rows)


@pytest.mark.asyncio
async def test_agent_reference_retention_is_rechecked_at_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _reference_visible_after("wallet") is None

    async def resolve_int(key: str, default: int) -> int:
        return {
            "byok.retention_hide_enabled": 1,
            "byok.retention_hide_days": 3,
        }.get(key, default)

    monkeypatch.setattr(
        agent_reference_previews_module.runtime_settings,
        "resolve_int",
        resolve_int,
    )
    before = datetime.now(timezone.utc)
    visible_after = await _reference_visible_after("byok")
    after = datetime.now(timezone.utc)

    assert visible_after is not None
    assert before.timestamp() - visible_after.timestamp() <= 3 * 86400
    assert after.timestamp() - visible_after.timestamp() >= 3 * 86400
    assert after.timestamp() - visible_after.timestamp() < 3 * 86400 + 1


@pytest.mark.asyncio
async def test_agent_reference_preview_cache_hit_skips_storage_and_pil(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = Image(
        id="preview-cache-image",
        user_id="preview-cache-user",
        storage_key="images/original.webp",
        source="upload",
        width=32,
        height=16,
        mime="image/webp",
        size_bytes=128,
        sha256="a" * 64,
        artifact_status="ready",
    )
    variant = ImageVariant(
        id="preview-cache-variant",
        image_id=image.id,
        kind="preview1024",
        storage_key="images/preview.webp",
        width=32,
        height=16,
    )
    reference = AgentRunReference(
        agent_run_id="preview-cache-run",
        user_id=image.user_id,
        image_id=image.id,
        ordinal=0,
        reference_label="ref_1",
        role="reference",
        display_label=None,
    )

    class Result:
        def __init__(self, rows: list[object]) -> None:
            self.rows = rows

        def scalars(self) -> "Result":
            return self

        def all(self) -> list[object]:
            return self.rows

    class Db:
        calls = 0

        async def execute(self, _statement: object) -> Result:
            self.calls += 1
            return Result([image] if self.calls % 2 else [variant])

    class Redis:
        values: dict[str, str] = {}

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def set(self, key: str, value: str, **_kwargs: object) -> None:
            self.values[key] = value

    source = io.BytesIO()
    PILImage.new("RGB", (32, 16), (10, 20, 30)).save(source, format="WEBP")
    storage_reads = 0

    async def read_storage(_key: str) -> bytes:
        nonlocal storage_reads
        storage_reads += 1
        return source.getvalue()

    monkeypatch.setattr(
        agent_reference_previews_module.storage,
        "aget_bytes",
        read_storage,
    )
    db = Db()
    redis = Redis()
    first = await agent_reference_previews_module.reference_previews(
        db,  # type: ignore[arg-type]
        [reference],
        run_user_id=image.user_id,
        visible_after=None,
        provider_api="openai-responses",
        redis=redis,
    )

    async def unexpected_storage(_key: str) -> bytes:
        raise AssertionError("storage must not be read on preview cache hit")

    def unexpected_pil(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("PIL must not run on preview cache hit")

    monkeypatch.setattr(
        agent_reference_previews_module.storage,
        "aget_bytes",
        unexpected_storage,
    )
    monkeypatch.setattr(agent_reference_previews_module.PILImage, "open", unexpected_pil)
    second = await agent_reference_previews_module.reference_previews(
        db,  # type: ignore[arg-type]
        [reference],
        run_user_id=image.user_id,
        visible_after=None,
        provider_api="openai-responses",
        redis=redis,
    )

    assert storage_reads == 1
    assert second == first


def test_context_packing_rejects_fixed_content_above_model_window() -> None:
    provider = _request().provider.model_copy(
        update={"context_window": 4096, "max_output_tokens": 2048}
    )
    with pytest.raises(AgentContextError) as captured:
        _pack_history(
            [],
            provider=provider,
            system_prompt="system " * 1000,
            current_prompt="current " * 1000,
            max_output_tokens=2048,
            reference_count=1,
        )
    assert captured.value.code == "agent_context_window_exceeded"


@pytest.mark.asyncio
async def test_capability_ttl_covers_the_complete_run_and_tool_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added: list[object] = []

    class Db:
        def add(self, value: object) -> None:
            added.append(value)

        async def flush(self) -> None:
            return None

    secret = "c" * 48
    monkeypatch.setattr(
        agent_orchestrator.settings, "agent_tool_capability_secret", secret
    )
    # agent_context and orchestrator share the same Settings singleton.
    run = SimpleNamespace(
        id="run-capability",
        user_id="user-capability",
        agent_session_id="session-capability",
        execution_epoch=3,
        request_snapshot_jsonb={
            "security_policy": {"capability_ttl_seconds": 86_400},
            "tool_policy": {
                "max_image_tool_calls": 2,
                "max_images_per_run": 4,
            },
        },
    )
    callback_base = "http://api-green:8000/internal/agent"
    run.request_snapshot_jsonb["internal_agent_callback_base_url"] = callback_base
    url, token = await _capability(
        Db(),  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        references=[],
        tools=["lumen_create_image"],
    )
    assert url == f"{callback_base}/runs/{run.id}/tools/create-image"
    assert token is not None
    claims = verify_agent_capability(secret, token)
    assert claims.expires_at - claims.issued_at == 86_400
    grant = added[0]
    assert getattr(grant, "max_redemptions") == 2


def test_timeout_with_preserved_text_becomes_partial() -> None:
    accumulator = AgentRuntimeAccumulator(text="usable partial answer")
    accumulator.terminal_status = "failed"
    accumulator.terminal_error_code = "agent_run_timeout"
    accumulator.provider_dispatch_count = 1
    accumulator.provider_response_statuses = [200]

    requested, code, _knowledge, _reason = _terminal_request(accumulator)

    assert requested == "failed"
    assert code == "agent_run_timeout"
    assert (
        agent_orchestrator._preserve_partial_text(requested, accumulator) == "partial"
    )


def test_runtime_tool_policy_defaults_without_lifecycle_deadlines() -> None:
    run = SimpleNamespace(request_snapshot_jsonb={})

    assert _runtime_tool_policy(run).model_dump() == {  # type: ignore[arg-type]
        "max_image_tool_calls": 2,
        "max_images_per_run": 4,
    }


def test_runtime_tool_policy_restores_legacy_snapshots() -> None:
    run = SimpleNamespace(
        request_snapshot_jsonb={
            "limits": {
                "max_image_tool_calls": 3,
                "max_images_per_run": 6,
            },
        }
    )
    assert _runtime_tool_policy(run).model_dump() == {  # type: ignore[arg-type]
        "max_image_tool_calls": 3,
        "max_images_per_run": 6,
    }


def test_agent_generation_billing_metadata_distinguishes_t2i_and_i2i() -> None:
    t2i = Generation(
        action="generate",
        upstream_request={
            "source": "agent",
            "agent_session_id": "session-1",
            "agent_run_id": "run-1",
            "agent_tool_call_id": "tool-1",
            "prompt": "must not be copied",
        },
    )
    i2i = Generation(action="edit", upstream_request={"source": "agent"})

    assert generation_agent_billing_meta(t2i) == {
        "source": "agent",
        "action_source": "agent.create_image",
        "agent_image_mode": "text_to_image",
        "agent_session_id": "session-1",
        "agent_run_id": "run-1",
        "agent_tool_call_id": "tool-1",
    }
    assert generation_agent_billing_meta(i2i)["agent_image_mode"] == ("image_to_image")


@pytest.mark.asyncio
async def test_text_flush_keeps_delta_appended_during_database_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    snapshots: list[tuple[str, str, bool]] = []

    async def flush(
        _redis: object,
        *,
        run_id: str,
        execution_epoch: int,
        text: str,
        delta: str,
        replace: bool = False,
    ) -> bool:
        assert run_id == "run-1"
        assert execution_epoch == 1
        snapshots.append((text, delta, replace))
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(agent_orchestrator, "flush_agent_text", flush)
    accumulator = AgentRuntimeAccumulator(text="first", pending_delta="first")
    task = asyncio.create_task(
        agent_orchestrator._flush_if_needed(
            object(),
            run_id="run-1",
            execution_epoch=1,
            accumulator=accumulator,
            force=True,
        )
    )
    await entered.wait()
    accumulator.text += "second"
    accumulator.pending_delta += "second"
    release.set()

    assert await task is True
    assert snapshots == [("first", "first", False)]
    assert accumulator.pending_delta == "second"


def test_run_agent_is_registered_with_worker_and_outbox_contract() -> None:
    assert main.WorkerSettings.job_timeout == 1800
    agent_functions = [
        function
        for function in main.WorkerSettings.functions
        if getattr(function, "__name__", getattr(function, "name", "")) == "run_agent"
    ]
    assert len(agent_functions) == 1
    assert (
        getattr(agent_functions[0], "timeout_s", None)
        == main._PI_NATIVE_ARQ_TIMEOUT_SECONDS  # noqa: SLF001
    )
    worker = Worker(
        functions=main.WorkerSettings.functions,
        cron_jobs=main.WorkerSettings.cron_jobs,
        job_timeout=main.WorkerSettings.job_timeout,
        burst=True,
        handle_signals=False,
    )
    assert worker.job_timeout_s == 1800
    assert worker.in_progress_timeout_s == (
        main._PI_NATIVE_ARQ_TIMEOUT_SECONDS + 10  # noqa: SLF001
    )
    names = {
        getattr(function, "__name__", getattr(function, "name", ""))
        for function in main.WorkerSettings.functions
    }
    assert "run_agent" in names
    assert any(
        getattr(job, "name", None) == "cron:reconcile_agent_runs"
        for job in main.WorkerSettings.cron_jobs
    )


def test_agent_sentry_frame_variables_are_scrubbed() -> None:
    event = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "vars": {
                                    "body": {"provider": {"api_key": "secret-key"}},
                                    "tool_capability": "capability-secret",
                                    "proxy_url": "http://user:password@proxy:8080",
                                }
                            }
                        ]
                    }
                }
            ]
        }
    }
    scrubbed = worker_observability._sentry_before_send(  # noqa: SLF001
        event,  # type: ignore[arg-type]
        {},
    )
    rendered = json.dumps(scrubbed)
    assert "secret-key" not in rendered
    assert "capability-secret" not in rendered
    assert "password@proxy" not in rendered
