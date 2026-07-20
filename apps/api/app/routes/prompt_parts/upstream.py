"""Single-provider prompt enhancement streaming."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from lumen_core.providers import ProviderDefinition, resolve_provider_proxy_url

from ...task_billing import EnhanceUsageCapture, enhance_pricing_snapshot_key

logger = logging.getLogger(__name__)

RETRYABLE_HTTP_STATUS = {408, 409, 425, 429}
FALLBACK_400_MARKERS = (
    "model",
    "service_tier",
    "tier",
    "reasoning",
    "unsupported",
    "not_found",
    "not found",
)


@dataclass(frozen=True)
class EnhanceAttempt:
    name: str
    model: str
    reasoning_effort: str | None = "low"
    service_tier: str | None = "priority"


ENHANCE_ATTEMPTS = (
    EnhanceAttempt(name="primary", model="gpt-5.5", reasoning_effort="low"),
    EnhanceAttempt(
        name="fallback-gpt-5.4-low",
        model="gpt-5.4",
        reasoning_effort="low",
    ),
    EnhanceAttempt(
        name="fallback-gpt-5.4-low-standard",
        model="gpt-5.4",
        reasoning_effort="low",
        service_tier=None,
    ),
)


@dataclass(frozen=True)
class StreamTimeouts:
    connect: float
    read: float
    write: float
    pool: float


@dataclass
class _ResponseState:
    emitted: bool = False


class EnhanceProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def responses_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"


def build_enhance_body(
    text: str,
    attempt: EnhanceAttempt,
    *,
    system_prompt: str,
    content: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": attempt.model,
        "instructions": system_prompt,
        "input": [
            {
                "role": "user",
                "content": content
                if content is not None
                else [{"type": "input_text", "text": text}],
            }
        ],
        "stream": True,
    }
    if metadata:
        body["metadata"] = metadata
    if attempt.reasoning_effort:
        body["reasoning"] = {"effort": attempt.reasoning_effort}
    if attempt.service_tier:
        body["service_tier"] = attempt.service_tier
    return body


def is_retryable_upstream_error(status_code: int, raw: bytes) -> bool:
    if status_code in RETRYABLE_HTTP_STATUS or status_code >= 500:
        return True
    if status_code not in {400, 404}:
        return False
    text = raw[:2000].decode("utf-8", errors="ignore").lower()
    return any(marker in text for marker in FALLBACK_400_MARKERS)


def extract_error_message(evt: dict[str, Any]) -> str:
    err = evt.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or err.get("type")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        return json.dumps(err, ensure_ascii=False)[:500]
    if isinstance(err, str) and err.strip():
        return err.strip()
    msg = evt.get("message")
    return msg.strip() if isinstance(msg, str) and msg.strip() else "response_failed"


def extract_response_text(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    chunks: list[str] = []
    output = obj.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    text = obj.get("output_text") or obj.get("text")
    if isinstance(text, str) and text:
        chunks.append(text)
    return "".join(chunks)


def iter_sse_payloads_from_buffer(buffer: str) -> tuple[list[str], str]:
    buffer = buffer.replace("\r\n", "\n")
    payloads: list[str] = []
    while "\n\n" in buffer:
        raw_event, buffer = buffer.split("\n\n", 1)
        data_lines = [
            line[len("data:") :].strip()
            for raw_line in raw_event.splitlines()
            if (line := raw_line.strip()).startswith("data:")
        ]
        if data_lines:
            payloads.append("\n".join(data_lines))
    return payloads, buffer


def capture_enhance_usage(
    capture: EnhanceUsageCapture | None,
    event: dict[str, Any],
    *,
    provider: ProviderDefinition,
    attempt: EnhanceAttempt,
) -> None:
    if capture is None:
        return
    response = event.get("response")
    response_obj = response if isinstance(response, dict) else {}
    usage = event.get("usage")
    if not isinstance(usage, dict):
        usage = response_obj.get("usage")
    if not isinstance(usage, dict):
        return
    response_id = response_obj.get("id") or event.get("response_id")
    model = response_obj.get("model") or event.get("model") or attempt.model
    capture.provider_name = provider.name
    capture.model = model if isinstance(model, str) and model.strip() else attempt.model
    capture.service_tier = attempt.service_tier or "standard"
    capture.pricing_snapshot_key = enhance_pricing_snapshot_key(
        attempt.model,
        capture.service_tier,
    )
    capture.response_id = (
        response_id if isinstance(response_id, str) and response_id.strip() else None
    )
    capture.usage = usage


def _text_chunk(text: str) -> str:
    return f"data: {json.dumps({'text': text})}\n\n"


def _delta_chunks(evt: dict[str, Any], state: _ResponseState) -> list[str]:
    delta = evt.get("delta", "")
    if not delta:
        return []
    state.emitted = True
    return [_text_chunk(delta)]


def _done_chunks(evt: dict[str, Any], state: _ResponseState) -> list[str]:
    if state.emitted:
        return []
    text_done = evt.get("text")
    if not isinstance(text_done, str) or not text_done:
        raise EnhanceProviderError("empty_response", retryable=True)
    state.emitted = True
    return [_text_chunk(text_done)]


def _completed_chunks(evt: dict[str, Any], state: _ResponseState) -> list[str]:
    if state.emitted:
        return []
    completed_text = extract_response_text(evt.get("response") or evt)
    if not completed_text:
        raise EnhanceProviderError("empty_response", retryable=True)
    state.emitted = True
    return [_text_chunk(completed_text)]


def _event_chunks(
    evt: dict[str, Any],
    state: _ResponseState,
    *,
    capture: EnhanceUsageCapture | None,
    provider: ProviderDefinition,
    attempt: EnhanceAttempt,
) -> tuple[list[str], bool]:
    capture_enhance_usage(capture, evt, provider=provider, attempt=attempt)
    evt_type = evt.get("type", "")
    if evt_type == "response.output_text.delta":
        return _delta_chunks(evt, state), False
    if evt_type == "response.output_text.done":
        return _done_chunks(evt, state), False
    if evt_type == "response.completed":
        return _completed_chunks(evt, state), True
    if evt_type in {"response.failed", "response.incomplete", "error"}:
        raise EnhanceProviderError(
            extract_error_message(evt),
            retryable=not state.emitted,
        )
    return [], False


async def _stream_response(
    response: httpx.Response,
    *,
    capture: EnhanceUsageCapture | None,
    provider: ProviderDefinition,
    attempt: EnhanceAttempt,
) -> AsyncIterator[str]:
    buffer = ""
    state = _ResponseState()
    async for chunk in response.aiter_text():
        payloads, buffer = iter_sse_payloads_from_buffer(buffer + chunk)
        for payload in payloads:
            if payload == "[DONE]":
                return
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                continue
            chunks, completed = _event_chunks(
                evt,
                state,
                capture=capture,
                provider=provider,
                attempt=attempt,
            )
            for output in chunks:
                yield output
            if completed:
                return
    if state.emitted:
        return
    raise EnhanceProviderError("empty_response", retryable=True)


async def _raise_for_status(
    response: httpx.Response,
    *,
    provider: ProviderDefinition,
    attempt: EnhanceAttempt,
) -> None:
    if response.status_code == 200:
        return
    raw = await response.aread()
    logger.warning(
        "enhance upstream error provider=%s attempt=%s status=%s: %s",
        provider.name,
        attempt.name,
        response.status_code,
        raw[:500],
    )
    raise EnhanceProviderError(
        f"upstream http {response.status_code}",
        retryable=is_retryable_upstream_error(response.status_code, raw),
    )


async def stream_enhance_one(
    text: str,
    provider: ProviderDefinition,
    attempt: EnhanceAttempt,
    capture: EnhanceUsageCapture | None = None,
    *,
    system_prompt: str,
    content: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
    timeouts: StreamTimeouts,
) -> AsyncIterator[str]:
    body = build_enhance_body(
        text,
        attempt,
        system_prompt=system_prompt,
        content=content,
        metadata=metadata,
    )
    try:
        proxy_url = await resolve_provider_proxy_url(provider.proxy)
        timeout = httpx.Timeout(
            connect=timeouts.connect,
            read=timeouts.read,
            write=timeouts.write,
            pool=timeouts.pool,
        )
        async with httpx.AsyncClient(
            timeout=timeout,
            proxy=proxy_url,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                responses_url(provider.base_url),
                json=body,
                headers={
                    "Authorization": f"Bearer {provider.api_key}",
                    "Content-Type": "application/json",
                },
            ) as response:
                await _raise_for_status(
                    response,
                    provider=provider,
                    attempt=attempt,
                )
                async for chunk in _stream_response(
                    response,
                    capture=capture,
                    provider=provider,
                    attempt=attempt,
                ):
                    yield chunk
    except EnhanceProviderError:
        raise
    except httpx.TimeoutException:
        logger.warning(
            "enhance upstream timeout provider=%s attempt=%s read_timeout_s=%s",
            provider.name,
            attempt.name,
            timeouts.read,
        )
        raise EnhanceProviderError("timeout", retryable=True) from None
    except httpx.HTTPError as exc:
        raise EnhanceProviderError(type(exc).__name__, retryable=True) from exc
