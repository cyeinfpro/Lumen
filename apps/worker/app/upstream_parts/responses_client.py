"""Runtime Responses SSE, completion, and one-shot client helpers."""

from __future__ import annotations

from ..provider_runtime.upstream_services import upstream_services

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from lumen_core.providers import ProviderProxyDefinition

_ERROR_RESPONSE_MAX_BYTES = 64 * 1024


class _ResponseBodyTooLarge(Exception):
    def __init__(self, *, max_bytes: int, received_bytes: int) -> None:
        super().__init__(f"response body exceeded {max_bytes} bytes")
        self.max_bytes = max_bytes
        self.received_bytes = received_bytes


def _http_origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip("."), port


def _runtime_pinned_target(
    runtime: Any,
    *,
    base: str,
    proxy_url: str | None,
) -> Any | None:
    if proxy_url is not None:
        return None
    target = getattr(runtime, "_byok_http_target", None)
    if target is None or not getattr(target, "resolved_ips", ()):
        return None
    if _http_origin(str(getattr(target, "url", ""))) != _http_origin(base):
        raise upstream_services().infrastructure.UpstreamError(
            "validated BYOK target does not match runtime base URL",
            status_code=403,
            error_code=upstream_services().infrastructure.EC.UPSTREAM_INVALID_REQUEST.value,
        )
    return target


async def _read_response_body_limited(
    response: Any,
    *,
    max_bytes: int,
) -> bytes:
    raw_content_length = getattr(response, "headers", {}).get("content-length")
    try:
        content_length = int(raw_content_length)
    except (TypeError, ValueError):
        content_length = None
    if content_length is not None and content_length > max_bytes:
        raise _ResponseBodyTooLarge(
            max_bytes=max_bytes,
            received_bytes=content_length,
        )

    iterator = getattr(response, "aiter_bytes", None)
    if not callable(iterator):
        raw = await response.aread()
        if len(raw) > max_bytes:
            raise _ResponseBodyTooLarge(
                max_bytes=max_bytes,
                received_bytes=len(raw),
            )
        return raw

    body = bytearray()
    received_bytes = 0
    async for chunk in iterator():
        if not chunk:
            continue
        received_bytes += len(chunk)
        if received_bytes > max_bytes:
            raise _ResponseBodyTooLarge(
                max_bytes=max_bytes,
                received_bytes=received_bytes,
            )
        body.extend(chunk)
    return bytes(body)


async def _bounded_body_or_upstream_error(
    response: Any,
    *,
    max_bytes: int,
    label: str,
    url: str,
    trace_id: str,
) -> bytes:
    try:
        return await _read_response_body_limited(response, max_bytes=max_bytes)
    except _ResponseBodyTooLarge as exc:
        raise upstream_services().infrastructure.UpstreamError(
            f"{label} exceeds max bytes",
            status_code=response.status_code,
            error_code=upstream_services().infrastructure.EC.STREAM_TOO_LARGE.value,
            payload={
                "path": "responses",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
                "max_bytes": exc.max_bytes,
                "actual_bytes": exc.received_bytes,
            },
        ) from exc


async def _select_response_client(
    *,
    timeout_config: Any,
    proxy_url: str | None,
    pinned_target: Any | None,
) -> tuple[Any, Any | None]:
    if proxy_url is not None:
        return await upstream_services().lifecycle.get_client(proxy_url), None
    if pinned_target is not None:
        client = upstream_services().lifecycle.build_client(
            timeout_config,
            pinned_target=pinned_target,
        )
        return client, client
    return await upstream_services().lifecycle.get_client(), None


def _count_sse_line(
    line: str,
    *,
    line_count: int,
    byte_count: int,
    status_code: int,
) -> tuple[int, int]:
    line_bytes = len(line.encode("utf-8"))
    line_count += 1
    byte_count += line_bytes
    if line_count > upstream_services().core.SSE_MAX_LINES:
        raise upstream_services().infrastructure.UpstreamError(
            "sse exceeded max lines",
            error_code=upstream_services().infrastructure.EC.STREAM_TOO_LARGE.value,
            status_code=status_code,
        )
    if line_bytes > upstream_services().core.SSE_MAX_LINE_BYTES:
        raise upstream_services().infrastructure.UpstreamError(
            "sse exceeded max line bytes",
            error_code=upstream_services().infrastructure.EC.STREAM_TOO_LARGE.value,
            status_code=status_code,
        )
    if byte_count > upstream_services().core.SSE_MAX_BYTES:
        raise upstream_services().infrastructure.UpstreamError(
            "sse exceeded max bytes",
            error_code=upstream_services().infrastructure.EC.STREAM_TOO_LARGE.value,
            status_code=status_code,
        )
    return line_count, byte_count


def _decode_sse_data_line(
    data_raw: str,
    *,
    current_event: str | None,
    log_prefix: str,
) -> dict[str, Any] | None:
    try:
        event = json.loads(data_raw)
    except json.JSONDecodeError:
        upstream_services().infrastructure.logger.warning(
            "%s invalid json line: %s",
            log_prefix,
            data_raw[:200],
        )
        return None
    if not isinstance(event, dict):
        return None
    if "type" not in event and current_event:
        event["type"] = current_event
    return event


async def _iter_httpx_sse_events(
    response: Any,
    *,
    url: str,
    trace_id: str,
    interruption_error_code: str,
    interruption_message: str,
    log_prefix: str,
) -> AsyncIterator[dict[str, Any]]:
    current_event: str | None = None
    line_count = 0
    byte_count = 0
    try:
        async for line in response.aiter_lines():
            line_count, byte_count = _count_sse_line(
                line,
                line_count=line_count,
                byte_count=byte_count,
                status_code=response.status_code,
            )
            if line == "":
                current_event = None
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                current_event = line[len("event:") :].strip() or None
                continue
            if not line.startswith("data:"):
                continue
            data_raw = line[len("data:") :].lstrip()
            if data_raw == "[DONE]":
                return
            event = _decode_sse_data_line(
                data_raw,
                current_event=current_event,
                log_prefix=log_prefix,
            )
            if event is not None:
                upstream_services().transport.maybe_record_usage_from_event(event)
                yield event
    except upstream_services().infrastructure.UpstreamError:
        raise
    except asyncio.CancelledError:
        raise
    except httpx.HTTPError as exc:
        raise upstream_services().infrastructure.UpstreamError(
            f"{interruption_message}: {exc}",
            status_code=response.status_code,
            error_code=interruption_error_code,
            payload={
                "path": "responses",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
            },
        ) from exc


async def _raise_response_status_error(
    response: Any,
    *,
    url: str,
    trace_id: str,
    log_prefix: str,
) -> None:
    raw = await _bounded_body_or_upstream_error(
        response,
        max_bytes=min(
            upstream_services().core.NON_SSE_JSON_MAX_BYTES,
            _ERROR_RESPONSE_MAX_BYTES,
        ),
        label="upstream error payload",
        url=url,
        trace_id=trace_id,
    )
    raw_text = raw.decode("utf-8", errors="replace")
    response_headers = getattr(response, "headers", None)
    request_id = (
        response_headers.get("x-request-id") if response_headers is not None else None
    )
    upstream_services().infrastructure.logger.warning(
        "%s non-2xx status=%s url=%s body=%.1000s trace_id=%s x_request_id=%s",
        log_prefix,
        response.status_code,
        url,
        raw_text,
        trace_id,
        request_id,
    )
    try:
        payload = json.loads(raw_text)
    except Exception:  # noqa: BLE001
        payload = {"raw": raw_text}
    raise upstream_services().core.with_error_context(
        upstream_services().core.parse_error(
            payload if isinstance(payload, dict) else {},
            response.status_code,
        ),
        path="responses",
        method="POST",
        url=url,
    )


def _response_error_detail(event: dict[str, Any]) -> dict[str, Any] | None:
    response_object = event.get("response")
    error = None
    if isinstance(response_object, dict):
        error = response_object.get("error") or response_object.get(
            "incomplete_details"
        )
    if error is None:
        error = event.get("error") or event.get("incomplete_details")
    return error if isinstance(error, dict) else None


@dataclass
class _ResponsesCallTerminal:
    completed: dict[str, Any] | None = None
    last_event_type: str | None = None
    error: dict[str, Any] | None = None

    def observe(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if isinstance(event_type, str):
            self.last_event_type = event_type
        if upstream_services().core.is_responses_success_terminal(event_type):
            response_object = event.get("response")
            if isinstance(response_object, dict):
                self.completed = response_object
        elif upstream_services().core.is_responses_error_terminal(event_type):
            self.error = _response_error_detail(event)


def _responses_terminal_result(
    terminal: _ResponsesCallTerminal,
    *,
    status_code: int,
    url: str,
    trace_id: str,
) -> dict[str, Any]:
    if terminal.completed is not None:
        return terminal.completed
    payload = {
        "path": "responses",
        "method": "POST",
        "url": url,
        "x_trace_id": trace_id,
        "last_event_type": terminal.last_event_type,
    }
    if terminal.error is None:
        raise upstream_services().infrastructure.UpstreamError(
            "responses_call sse missing terminal frame",
            status_code=status_code,
            error_code=upstream_services().infrastructure.EC.BAD_RESPONSE.value,
            payload=payload,
        )
    upstream_code = terminal.error.get("code") or terminal.error.get("type")
    upstream_message = terminal.error.get("message")
    payload["upstream_error"] = terminal.error
    raise upstream_services().infrastructure.UpstreamError(
        (
            upstream_message
            if isinstance(upstream_message, str) and upstream_message
            else "responses_call sse error terminal"
        ),
        status_code=status_code,
        error_code=(
            upstream_code
            if isinstance(upstream_code, str) and upstream_code
            else upstream_services().infrastructure.EC.BAD_RESPONSE.value
        ),
        payload=payload,
    )


async def _responses_call_sse_result(
    response: Any,
    *,
    url: str,
    trace_id: str,
) -> dict[str, Any]:
    terminal = _ResponsesCallTerminal()
    async for event in _iter_httpx_sse_events(
        response,
        url=url,
        trace_id=trace_id,
        interruption_error_code=upstream_services().infrastructure.EC.TEXT_STREAM_INTERRUPTED.value,
        interruption_message="responses_call sse interrupted",
        log_prefix="responses_call sse",
    ):
        terminal.observe(event)
    return _responses_terminal_result(
        terminal,
        status_code=response.status_code,
        url=url,
        trace_id=trace_id,
    )


async def _responses_call_json_result(
    response: Any,
    *,
    url: str,
    trace_id: str,
) -> dict[str, Any]:
    raw = await _bounded_body_or_upstream_error(
        response,
        max_bytes=upstream_services().core.NON_SSE_JSON_MAX_BYTES,
        label="responses json payload",
        url=url,
        trace_id=trace_id,
    )
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        raise upstream_services().infrastructure.UpstreamError(
            "responses_call returned invalid JSON",
            status_code=response.status_code,
            error_code=upstream_services().infrastructure.EC.BAD_RESPONSE.value,
            payload={
                "path": "responses",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
            },
        ) from exc
    if not isinstance(payload, dict):
        raise upstream_services().infrastructure.UpstreamError(
            "responses_call returned non-object payload",
            status_code=response.status_code,
            error_code=upstream_services().infrastructure.EC.BAD_RESPONSE.value,
            payload={
                "path": "responses",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
            },
        )
    if isinstance(payload.get("usage"), dict):
        upstream_services().core.record_usage(payload["usage"])
    return payload


async def _iter_sse_with_runtime(
    *,
    base: str,
    api_key: str,
    body: dict[str, Any],
    read_timeout_s: float | None = None,
    interruption_error_code: str = "stream_interrupted",
    trace_id: str | None = None,
    proxy_url: str | None = None,
    pinned_target: Any | None = None,
    allow_non_sse_payload: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """POST ``/v1/responses`` with httpx and yield bounded SSE events."""
    call_trace_id = trace_id or upstream_services().core.generate_trace_id()
    timeout_config = await upstream_services().lifecycle.resolve_timeout_config()
    client, owned_client = await _select_response_client(
        timeout_config=timeout_config,
        proxy_url=proxy_url,
        pinned_target=pinned_target,
    )
    url = upstream_services().requests.responses_url(base)
    stream_kwargs: dict[str, Any] = {
        "json": body,
        "headers": upstream_services().core.auth_headers(
            api_key, trace_id=call_trace_id
        ),
    }
    if read_timeout_s is not None and read_timeout_s > timeout_config.read:
        stream_kwargs["timeout"] = timeout_config.to_httpx(read=read_timeout_s)

    started = time.monotonic()
    final_status = 0
    final_response_headers: Any = None
    try:
        async with client.stream("POST", url, **stream_kwargs) as response:
            final_status = response.status_code
            final_response_headers = getattr(response, "headers", None)
            if not 200 <= response.status_code < 300:
                await _raise_response_status_error(
                    response,
                    url=url,
                    trace_id=call_trace_id,
                    log_prefix="httpx sse",
                )

            if allow_non_sse_payload:
                content_type = (
                    final_response_headers.get("content-type")
                    if final_response_headers is not None
                    else ""
                ) or ""
                if "text/event-stream" not in content_type.lower():
                    raw = await _bounded_body_or_upstream_error(
                        response,
                        max_bytes=upstream_services().core.NON_SSE_JSON_MAX_BYTES,
                        label="non-sse json payload",
                        url=url,
                        trace_id=call_trace_id,
                    )
                    raw_text = raw.decode("utf-8", errors="replace")
                    try:
                        json_payload = json.loads(raw_text)
                    except Exception as exc:  # noqa: BLE001
                        raise upstream_services().infrastructure.UpstreamError(
                            f"non-sse payload is not valid JSON: {exc}",
                            status_code=response.status_code,
                            error_code=upstream_services().infrastructure.EC.BAD_RESPONSE.value,
                            payload={
                                "path": "responses",
                                "method": "POST",
                                "url": url,
                                "x_trace_id": call_trace_id,
                                "content_type": content_type,
                                "body_summary": raw_text[:200],
                            },
                        ) from exc
                    yield {
                        "type": upstream_services().core.JSON_PAYLOAD_SENTINEL_TYPE,
                        "payload": json_payload,
                        "content_type": content_type,
                    }
                    return

            async for event in _iter_httpx_sse_events(
                response,
                url=url,
                trace_id=call_trace_id,
                interruption_error_code=interruption_error_code,
                interruption_message="responses stream interrupted",
                log_prefix="sse:",
            ):
                yield event
    except asyncio.CancelledError:
        raise
    finally:
        duration_ms = (time.monotonic() - started) * 1000.0
        try:
            upstream_services().core.log_upstream_call(
                endpoint="responses",
                status=final_status,
                duration_ms=duration_ms,
                trace_id=call_trace_id,
                response_headers=final_response_headers,
            )
        except Exception:  # noqa: BLE001
            upstream_services().infrastructure.logger.debug(
                "failed to log upstream call meta",
                exc_info=True,
            )
        if owned_client is not None:
            await upstream_services().lifecycle.aclose_client_cancel_safe(owned_client)


async def _iter_sse(
    body: dict[str, Any],
    *,
    runtime_override: Any | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Validate a Responses request, resolve its runtime, and yield SSE events."""
    upstream_services().core.validate_responses_body(body)
    if isinstance(body.get("tools"), list):
        body["tools"] = upstream_services().core.stable_sort_tools(body["tools"])
    assert body.get("model"), "model must be set"

    runtime = runtime_override or await upstream_services().core.resolve_runtime()
    base, api_key, proxy = upstream_services().core.runtime_parts(runtime)
    provider_name = upstream_services().core.runtime_provider_name(runtime)
    if provider_name:
        yield {
            "type": "provider_used",
            "provider": provider_name,
            "route": "responses",
            "endpoint": "responses",
            "source": "text",
        }
    proxy_url = await upstream_services().infrastructure.resolve_provider_proxy_url(
        proxy
    )
    runtime_kwargs: dict[str, Any] = {
        "base": base,
        "api_key": api_key,
        "body": body,
        "interruption_error_code": upstream_services().core.TEXT_STREAM_INTERRUPTED_ERROR_CODE,
        "proxy_url": proxy_url,
    }
    pinned_target = _runtime_pinned_target(
        runtime,
        base=base,
        proxy_url=proxy_url,
    )
    if pinned_target is not None:
        runtime_kwargs["pinned_target"] = pinned_target
    async for event in upstream_services().responses.iter_sse_with_runtime(
        **runtime_kwargs,
    ):
        yield event


async def stream_completion(
    body: dict[str, Any],
    *,
    runtime_override: Any | None = None,
) -> AsyncIterator[dict[str, Any]]:
    async for event in upstream_services().responses.iter_sse(
        body,
        runtime_override=runtime_override,
    ):
        yield event


async def responses_call(
    body: dict[str, Any],
    *,
    route: str = "text",
    api_key_override: str | None = None,
    base_url_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    timeout_s: float | None = None,
    endpoint_label: str = "responses",
    pinned_target_override: Any | None = None,
) -> dict[str, Any]:
    """Make one Responses request and accept either SSE or JSON success bodies."""
    upstream_services().core.validate_responses_body(body)
    if isinstance(body.get("tools"), list):
        body["tools"] = upstream_services().core.stable_sort_tools(body["tools"])
    assert body.get("model"), "model must be set"

    proxy = proxy_override
    if api_key_override is not None and base_url_override is not None:
        base, api_key = base_url_override, api_key_override
    else:
        _ = route
        runtime = await upstream_services().core.resolve_runtime()
        base, api_key, proxy = upstream_services().core.runtime_parts(runtime)

    url = upstream_services().requests.responses_url(base)
    call_trace_id = upstream_services().core.generate_trace_id()
    headers = upstream_services().core.auth_headers(api_key, trace_id=call_trace_id)
    stream_kwargs: dict[str, Any] = {"json": body, "headers": headers}
    timeout_config = await upstream_services().lifecycle.resolve_timeout_config()
    effective_timeout = (
        float(timeout_s) if timeout_s is not None else timeout_config.read
    )
    stream_kwargs["timeout"] = timeout_config.to_httpx(read=effective_timeout)

    proxy_url = await upstream_services().infrastructure.resolve_provider_proxy_url(
        proxy
    )
    client, owned_client = await _select_response_client(
        timeout_config=timeout_config,
        proxy_url=proxy_url,
        pinned_target=(pinned_target_override if proxy_url is None else None),
    )
    started = time.monotonic()
    final_status = 0
    final_response_headers: Any = None
    try:
        try:
            async with client.stream("POST", url, **stream_kwargs) as response:
                final_status = response.status_code
                final_response_headers = getattr(response, "headers", None)

                if not 200 <= response.status_code < 300:
                    await _raise_response_status_error(
                        response,
                        url=url,
                        trace_id=call_trace_id,
                        log_prefix="responses_call",
                    )

                content_type = (
                    final_response_headers.get("content-type")
                    if final_response_headers is not None
                    else ""
                ) or ""
                if "text/event-stream" in content_type.lower():
                    return await _responses_call_sse_result(
                        response,
                        url=url,
                        trace_id=call_trace_id,
                    )
                return await _responses_call_json_result(
                    response,
                    url=url,
                    trace_id=call_trace_id,
                )
        except httpx.TimeoutException as exc:
            raise upstream_services().infrastructure.UpstreamError(
                f"responses_call upstream timeout: {exc}",
                status_code=None,
                error_code=upstream_services().infrastructure.EC.UPSTREAM_TIMEOUT.value,
                payload={
                    "path": "responses",
                    "method": "POST",
                    "url": url,
                    "x_trace_id": call_trace_id,
                },
            ) from exc
        except httpx.HTTPError as exc:
            raise upstream_services().infrastructure.UpstreamError(
                f"responses_call upstream network error: {exc}",
                status_code=None,
                error_code=upstream_services().infrastructure.EC.UPSTREAM_ERROR.value,
                payload={
                    "path": "responses",
                    "method": "POST",
                    "url": url,
                    "x_trace_id": call_trace_id,
                },
            ) from exc
    except asyncio.CancelledError:
        raise
    finally:
        duration_ms = (time.monotonic() - started) * 1000.0
        try:
            upstream_services().core.log_upstream_call(
                endpoint=endpoint_label,
                status=final_status,
                duration_ms=duration_ms,
                trace_id=call_trace_id,
                response_headers=final_response_headers,
            )
        except Exception:  # noqa: BLE001
            upstream_services().infrastructure.logger.debug(
                "responses_call meta log failed",
                exc_info=True,
            )
        if owned_client is not None:
            await upstream_services().lifecycle.aclose_client_cancel_safe(owned_client)


__all__ = [
    "_iter_sse",
    "_iter_sse_with_runtime",
    "responses_call",
    "stream_completion",
]
