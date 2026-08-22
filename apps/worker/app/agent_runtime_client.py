"""Authenticated, bounded NDJSON client for the private Node Agent Runtime."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


RUNTIME_REQUEST_PATH = "/v1/runs"
RUNTIME_AUTH_VERSION = "v1"
RUNTIME_TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled"})
RUNTIME_EVENT_TYPES = frozenset(
    {
        "run.started",
        "provider.dispatched",
        "provider.response",
        "text.delta",
        "turn.completed",
        "tool.started",
        "tool.succeeded",
        "tool.failed",
        "limit.reached",
        *RUNTIME_TERMINAL_EVENTS,
    }
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AgentRuntimeProviderEnvelope(_StrictModel):
    provider_id: str = Field(min_length=1, max_length=64)
    api: Literal["openai-responses", "openai-completions", "anthropic-messages"]
    base_url: str = Field(min_length=8, max_length=2048)
    api_key: str = Field(min_length=1, max_length=8192, repr=False)
    headers: dict[str, str] = Field(default_factory=dict)
    proxy_url: str | None = Field(default=None, max_length=2048, repr=False)
    resolved_ips: list[str] = Field(default_factory=list, max_length=4)
    model: str = Field(min_length=1, max_length=256)
    context_window: int = Field(ge=4096, le=2_000_000)
    max_output_tokens: int = Field(ge=1, le=128000)
    reasoning_supported: bool
    vision_supported: bool

    @field_validator("resolved_ips")
    @classmethod
    def validate_resolved_ips(cls, values: list[str]) -> list[str]:
        try:
            return [str(ipaddress.ip_address(value)) for value in values]
        except ValueError as exc:
            raise ValueError("resolved_ips contains an invalid address") from exc


class AgentRuntimeHistoryMessage(_StrictModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=20000)


class AgentRuntimeReference(_StrictModel):
    reference_label: str = Field(pattern=r"^ref_[1-4]$")
    role: str = Field(min_length=1, max_length=32)
    display_label: str | None = Field(default=None, max_length=80)
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    data_base64: str = Field(min_length=4, max_length=700000, repr=False)


class AgentRuntimeImageDefaults(_StrictModel):
    count: int = Field(ge=1, le=4)
    aspect_ratio: str = Field(min_length=3, max_length=5)
    quality: Literal["1k", "2k", "4k"]
    render_quality: Literal["auto", "low", "medium", "high"]
    background: Literal["auto", "opaque", "transparent"]
    output_format: Literal["png", "jpeg", "webp"]


class AgentRuntimeLimits(_StrictModel):
    max_turns: int = Field(ge=1, le=12)
    max_tool_calls: int = Field(ge=0, le=12)
    max_image_tool_calls: int = Field(ge=0, le=8)
    max_images_per_run: int = Field(ge=1, le=16)
    max_output_tokens: int = Field(ge=1, le=32000)
    run_timeout_seconds: int = Field(ge=10, le=1800)
    tool_timeout_seconds: int = Field(ge=5, le=300)
    max_output_chars: int = Field(ge=1024, le=1_000_000)


class AgentRuntimeRequest(_StrictModel):
    version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=96)
    agent_session_id: str = Field(min_length=1, max_length=96)
    user_id: str = Field(min_length=1, max_length=96)
    execution_epoch: int = Field(ge=1)
    assistant_message_id: str = Field(min_length=1, max_length=96)
    trace_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    provider: AgentRuntimeProviderEnvelope
    system_prompt: str = Field(max_length=65536)
    history: list[AgentRuntimeHistoryMessage] = Field(max_length=256)
    current_prompt: str = Field(min_length=1, max_length=40000)
    references: list[AgentRuntimeReference] = Field(max_length=4)
    allowed_tools: list[Literal["lumen_create_image"]] = Field(max_length=1)
    image_defaults: AgentRuntimeImageDefaults
    tool_gateway_url: str | None = Field(default=None, max_length=2048)
    tool_capability: str | None = Field(default=None, max_length=8192, repr=False)
    reasoning_effort: (
        Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = None
    limits: AgentRuntimeLimits

    @model_validator(mode="after")
    def validate_bindings(self) -> "AgentRuntimeRequest":
        tools_enabled = bool(self.allowed_tools)
        if tools_enabled != bool(self.tool_gateway_url and self.tool_capability):
            raise ValueError("tool gateway bindings do not match allowed_tools")
        if self.references and not self.provider.vision_supported:
            raise ValueError("reference images require a vision-capable provider")
        return self


class AgentRuntimeUsage(_StrictModel):
    input_tokens: int = Field(ge=0, le=24_000_000)
    output_tokens: int = Field(ge=0, le=384_000)
    cache_read_tokens: int = Field(ge=0, le=24_000_000)
    cache_write_tokens: int = Field(ge=0, le=24_000_000)
    cache_write_1h_tokens: int = Field(default=0, ge=0, le=24_000_000)
    reasoning_tokens: int = Field(default=0, ge=0, le=384_000)
    total_tokens: int = Field(ge=0, le=24_384_000)

    @model_validator(mode="after")
    def validate_breakdown(self) -> "AgentRuntimeUsage":
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning usage exceeds output usage")
        if self.cache_write_1h_tokens > self.cache_write_tokens:
            raise ValueError("one-hour cache usage exceeds cache-write usage")
        canonical_total = (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )
        if self.total_tokens != canonical_total:
            raise ValueError("Runtime usage total is not canonical")
        return self


class AgentRuntimeEvent(_StrictModel):
    version: Literal[1]
    type: str
    seq: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=96)
    execution_epoch: int = Field(ge=1)
    delta: str | None = Field(default=None, max_length=8192)
    status: str | int | None = None
    error_code: str | None = Field(default=None, max_length=64)
    usage: AgentRuntimeUsage | None = None
    turn: int | None = Field(default=None, ge=1, le=64)
    turn_count: int | None = Field(default=None, ge=0, le=64)
    tool_call_count: int | None = Field(default=None, ge=0, le=64)
    tool_call_id: str | None = Field(default=None, max_length=128)
    ordinal: int | None = Field(default=None, ge=0, le=64)
    name: str | None = Field(default=None, max_length=64)
    mode: str | None = Field(default=None, max_length=32)
    generation_ids: list[str] | None = Field(default=None, max_length=4)
    replayed: bool | None = None
    result_unknown: bool | None = None
    reason: str | None = Field(default=None, max_length=64)
    stop_reason: str | None = Field(default=None, max_length=32)
    tools: list[str] | None = Field(default=None, max_length=1)
    runtime_version: str | None = Field(default=None, max_length=64)
    provider_dispatch_count: int | None = Field(default=None, ge=0, le=64)
    provider_completed_count: int | None = Field(default=None, ge=0, le=64)

    @field_validator("type")
    @classmethod
    def supported_type(cls, value: str) -> str:
        if value not in RUNTIME_EVENT_TYPES:
            raise ValueError("unsupported Runtime event")
        return value


class AgentRuntimeClientError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        delivery: Literal["proven_absent", "unknown"] = "unknown",
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.delivery = delivery
        self.status_code = status_code
        super().__init__("Agent Runtime request failed")


@dataclass(slots=True)
class _RuntimeEventDecoder:
    request: AgentRuntimeRequest
    max_line_bytes: int
    max_stream_bytes: int
    max_events: int
    buffer: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0
    event_count: int = 0
    expected_seq: int = 1
    terminal_seen: bool = False

    def feed(self, chunk: bytes) -> list[AgentRuntimeEvent]:
        self.total_bytes += len(chunk)
        if self.total_bytes > self.max_stream_bytes:
            raise AgentRuntimeClientError("agent_runtime_stream_too_large")
        self.buffer.extend(chunk)
        events: list[AgentRuntimeEvent] = []
        while (newline := self.buffer.find(b"\n")) >= 0:
            raw_line = bytes(self.buffer[:newline])
            del self.buffer[: newline + 1]
            events.append(self._decode_line(raw_line))
        if len(self.buffer) > self.max_line_bytes:
            raise AgentRuntimeClientError("agent_runtime_line_too_large")
        return events

    def _decode_line(self, raw_line: bytes) -> AgentRuntimeEvent:
        if not raw_line or raw_line.endswith(b"\r"):
            raise AgentRuntimeClientError("agent_runtime_invalid_framing")
        if len(raw_line) > self.max_line_bytes:
            raise AgentRuntimeClientError("agent_runtime_line_too_large")
        try:
            raw_event = json.loads(raw_line.decode("utf-8", "strict"))
            event = AgentRuntimeEvent.model_validate(raw_event)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentRuntimeClientError("agent_runtime_invalid_event") from exc
        self.event_count += 1
        if self.event_count > self.max_events:
            raise AgentRuntimeClientError("agent_runtime_event_limit")
        if (
            event.run_id != self.request.run_id
            or event.execution_epoch != self.request.execution_epoch
            or event.seq != self.expected_seq
        ):
            raise AgentRuntimeClientError("agent_runtime_event_scope_mismatch")
        if self.terminal_seen:
            raise AgentRuntimeClientError("agent_runtime_event_after_terminal")
        if event.type == "run.started" and not event.runtime_version:
            raise AgentRuntimeClientError("agent_runtime_invalid_event")
        if event.type in RUNTIME_TERMINAL_EVENTS:
            if (
                event.provider_dispatch_count is None
                or event.provider_completed_count is None
                or event.provider_completed_count > event.provider_dispatch_count
            ):
                raise AgentRuntimeClientError("agent_runtime_invalid_event")
        self._validate_usage_bounds(event)
        self.expected_seq += 1
        self.terminal_seen = event.type in RUNTIME_TERMINAL_EVENTS
        return event

    def _validate_usage_bounds(self, event: AgentRuntimeEvent) -> None:
        usage = event.usage
        if usage is None:
            return
        turn_limit = self.request.limits.max_turns
        multiplier = 1 if event.type == "turn.completed" else turn_limit
        input_total = (
            usage.input_tokens
            + usage.cache_read_tokens
            + usage.cache_write_tokens
        )
        if input_total > self.request.provider.context_window * multiplier:
            raise AgentRuntimeClientError("agent_runtime_usage_out_of_bounds")
        if usage.output_tokens > self.request.limits.max_output_tokens * multiplier:
            raise AgentRuntimeClientError("agent_runtime_usage_out_of_bounds")

    def finish(self) -> None:
        if self.buffer:
            raise AgentRuntimeClientError("agent_runtime_truncated_line")
        if not self.terminal_seen:
            raise AgentRuntimeClientError("agent_runtime_terminal_missing")


async def _next_stream_chunk(
    iterator: AsyncIterator[bytes],
    *,
    cancel_requested: asyncio.Event | None,
    timeout_seconds: float,
) -> bytes:
    if cancel_requested is not None and cancel_requested.is_set():
        raise AgentRuntimeClientError("agent_cancelled")
    next_chunk = asyncio.create_task(iterator.__anext__())
    cancel_wait = (
        asyncio.create_task(cancel_requested.wait())
        if cancel_requested is not None
        else None
    )
    waiters = {next_chunk, *([cancel_wait] if cancel_wait is not None else [])}
    done, pending = await asyncio.wait(
        waiters,
        timeout=timeout_seconds,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if not done:
        raise AgentRuntimeClientError("agent_runtime_event_timeout")
    if cancel_wait is not None and cancel_wait in done:
        next_chunk.cancel()
        await asyncio.gather(next_chunk, return_exceptions=True)
        raise AgentRuntimeClientError("agent_cancelled")
    return next_chunk.result()


def canonical_runtime_request(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return (
        f"{RUNTIME_AUTH_VERSION}\n{method.upper()}\n{path}\n"
        f"{timestamp}\n{nonce}\n{digest}"
    ).encode("ascii")


def sign_runtime_request(
    secret: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        canonical_runtime_request(method, path, timestamp, nonce, body),
        hashlib.sha256,
    ).hexdigest()


@dataclass(slots=True)
class AgentRuntimeClient:
    base_url: str
    shared_secret: str = field(repr=False)
    connect_timeout_seconds: float = 5.0
    event_idle_timeout_seconds: float = 45.0
    max_line_bytes: int = 64 * 1024
    max_stream_bytes: int = 8 * 1024 * 1024
    max_events: int = 4096
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    @property
    def configured(self) -> bool:
        return len(self.shared_secret.encode("utf-8")) >= 32

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(
                    connect=self.connect_timeout_seconds,
                    read=None,
                    write=10.0,
                    pool=self.connect_timeout_seconds,
                ),
                follow_redirects=False,
                trust_env=False,
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            )
        return self._client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def stream(
        self,
        request: AgentRuntimeRequest,
        *,
        cancel_requested: asyncio.Event | None = None,
        on_request_starting: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[AgentRuntimeEvent]:
        if not self.configured:
            raise AgentRuntimeClientError(
                "agent_runtime_unconfigured", delivery="proven_absent"
            )
        body = json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        signature = sign_runtime_request(
            self.shared_secret,
            "POST",
            RUNTIME_REQUEST_PATH,
            timestamp,
            nonce,
            body,
        )
        if on_request_starting is not None:
            await on_request_starting()
        try:
            stream_context = self._http().stream(
                "POST",
                RUNTIME_REQUEST_PATH,
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-lumen-agent-timestamp": timestamp,
                    "x-lumen-agent-nonce": nonce,
                    "x-lumen-agent-signature": signature,
                },
            )
            async with stream_context as response:
                if response.status_code != 200:
                    raise AgentRuntimeClientError(
                        "agent_runtime_rejected",
                        delivery="proven_absent",
                        status_code=response.status_code,
                    )
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith("application/x-ndjson"):
                    raise AgentRuntimeClientError("agent_runtime_invalid_response")
                async for event in self._events(
                    response,
                    request=request,
                    cancel_requested=cancel_requested,
                ):
                    yield event
        except AgentRuntimeClientError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise AgentRuntimeClientError(
                "agent_runtime_unavailable", delivery="proven_absent"
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AgentRuntimeClientError("agent_runtime_disconnected") from exc

    async def _events(
        self,
        response: httpx.Response,
        *,
        request: AgentRuntimeRequest,
        cancel_requested: asyncio.Event | None,
    ) -> AsyncIterator[AgentRuntimeEvent]:
        iterator = response.aiter_bytes().__aiter__()
        decoder = _RuntimeEventDecoder(
            request=request,
            max_line_bytes=self.max_line_bytes,
            max_stream_bytes=self.max_stream_bytes,
            max_events=self.max_events,
        )
        while True:
            try:
                chunk = await _next_stream_chunk(
                    iterator,
                    cancel_requested=cancel_requested,
                    timeout_seconds=self.event_idle_timeout_seconds,
                )
            except StopAsyncIteration:
                break
            for event in decoder.feed(chunk):
                yield event
        decoder.finish()


__all__ = [
    "AgentRuntimeClient",
    "AgentRuntimeClientError",
    "AgentRuntimeEvent",
    "AgentRuntimeHistoryMessage",
    "AgentRuntimeImageDefaults",
    "AgentRuntimeLimits",
    "AgentRuntimeProviderEnvelope",
    "AgentRuntimeReference",
    "AgentRuntimeRequest",
    "AgentRuntimeUsage",
    "canonical_runtime_request",
    "sign_runtime_request",
]
