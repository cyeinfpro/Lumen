"""Runtime Responses SSE, completion, and one-shot client helpers."""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from lumen_core.providers import ProviderProxyDefinition

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    """Resolve compatibility dependencies at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


async def _iter_sse_with_runtime(
    *,
    base: str,
    api_key: str,
    body: dict[str, Any],
    read_timeout_s: float | None = None,
    interruption_error_code: str = "stream_interrupted",
    trace_id: str | None = None,
    proxy_url: str | None = None,
    allow_non_sse_payload: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """POST ``/v1/responses`` with httpx and yield bounded SSE events."""
    facade = _facade()
    call_trace_id = trace_id or facade._generate_trace_id()
    timeout_config = await facade._resolve_timeout_config()
    client = await (
        facade._get_client(proxy_url) if proxy_url else facade._get_client()
    )
    url = facade._responses_url(base)
    stream_kwargs: dict[str, Any] = {
        "json": body,
        "headers": facade._auth_headers(api_key, trace_id=call_trace_id),
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
            if response.status_code >= 400:
                raw = await response.aread()
                raw_text = raw.decode("utf-8", errors="replace")
                request_id = (
                    final_response_headers.get("x-request-id")
                    if final_response_headers is not None
                    else None
                )
                facade.logger.warning(
                    "httpx sse non-2xx status=%s url=%s body=%.1000s trace_id=%s "
                    "x_request_id=%s",
                    response.status_code,
                    url,
                    raw_text,
                    call_trace_id,
                    request_id,
                )
                try:
                    payload = json.loads(raw_text)
                except Exception:  # noqa: BLE001
                    payload = {"raw": raw_text}
                raise facade._with_error_context(
                    facade._parse_error(
                        payload if isinstance(payload, dict) else {},
                        response.status_code,
                    ),
                    path="responses",
                    method="POST",
                    url=url,
                )

            if allow_non_sse_payload:
                content_type = (
                    final_response_headers.get("content-type")
                    if final_response_headers is not None
                    else ""
                ) or ""
                if "text/event-stream" not in content_type.lower():
                    raw = await response.aread()
                    if len(raw) > facade._NON_SSE_JSON_MAX_BYTES:
                        raise facade.UpstreamError(
                            "non-sse json payload exceeds max bytes",
                            status_code=response.status_code,
                            error_code=facade.EC.STREAM_TOO_LARGE.value,
                            payload={
                                "path": "responses",
                                "method": "POST",
                                "url": url,
                                "x_trace_id": call_trace_id,
                                "max_bytes": facade._NON_SSE_JSON_MAX_BYTES,
                                "actual_bytes": len(raw),
                            },
                        )
                    raw_text = raw.decode("utf-8", errors="replace")
                    try:
                        json_payload = json.loads(raw_text)
                    except Exception as exc:  # noqa: BLE001
                        raise facade.UpstreamError(
                            f"non-sse payload is not valid JSON: {exc}",
                            status_code=response.status_code,
                            error_code=facade.EC.BAD_RESPONSE.value,
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
                        "type": facade._JSON_PAYLOAD_SENTINEL_TYPE,
                        "payload": json_payload,
                        "content_type": content_type,
                    }
                    return

            current_event: str | None = None
            line_count = 0
            byte_count = 0
            try:
                async for line in response.aiter_lines():
                    line_bytes = len(line.encode("utf-8"))
                    line_count += 1
                    byte_count += line_bytes
                    if line_count > facade._SSE_MAX_LINES:
                        raise facade.UpstreamError(
                            "sse exceeded max lines",
                            error_code=facade.EC.STREAM_TOO_LARGE.value,
                            status_code=response.status_code,
                        )
                    if line_bytes > facade._SSE_MAX_LINE_BYTES:
                        raise facade.UpstreamError(
                            "sse exceeded max line bytes",
                            error_code=facade.EC.STREAM_TOO_LARGE.value,
                            status_code=response.status_code,
                        )
                    if byte_count > facade._SSE_MAX_BYTES:
                        raise facade.UpstreamError(
                            "sse exceeded max bytes",
                            error_code=facade.EC.STREAM_TOO_LARGE.value,
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
                    try:
                        data = json.loads(data_raw)
                    except json.JSONDecodeError:
                        facade.logger.warning(
                            "sse: invalid json line: %s",
                            data_raw[:200],
                        )
                        continue
                    if isinstance(data, dict):
                        if "type" not in data and current_event:
                            data["type"] = current_event
                        facade._maybe_record_usage_from_event(data)
                        yield data
            except facade.UpstreamError:
                raise
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as exc:
                raise facade.UpstreamError(
                    f"responses stream interrupted: {exc}",
                    status_code=response.status_code,
                    error_code=interruption_error_code,
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
            facade._log_upstream_call(
                endpoint="responses",
                status=final_status,
                duration_ms=duration_ms,
                trace_id=call_trace_id,
                response_headers=final_response_headers,
            )
        except Exception:  # noqa: BLE001
            facade.logger.debug(
                "failed to log upstream call meta",
                exc_info=True,
            )


async def _iter_sse(
    body: dict[str, Any],
    *,
    runtime_override: Any | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Validate a Responses request, resolve its runtime, and yield SSE events."""
    facade = _facade()
    facade._validate_responses_body(body)
    if isinstance(body.get("tools"), list):
        body["tools"] = facade._stable_sort_tools(body["tools"])
    assert body.get("model"), "model must be set"

    runtime = runtime_override or await facade._resolve_runtime()
    base, api_key, proxy = facade._runtime_parts(runtime)
    provider_name = facade._runtime_provider_name(runtime)
    if provider_name:
        yield {
            "type": "provider_used",
            "provider": provider_name,
            "route": "responses",
            "endpoint": "responses",
            "source": "text",
        }
    proxy_url = await facade.resolve_provider_proxy_url(proxy)
    async for event in facade._iter_sse_with_runtime(
        base=base,
        api_key=api_key,
        body=body,
        interruption_error_code=facade._TEXT_STREAM_INTERRUPTED_ERROR_CODE,
        proxy_url=proxy_url,
    ):
        yield event


async def stream_completion(
    body: dict[str, Any],
    *,
    runtime_override: Any | None = None,
) -> AsyncIterator[dict[str, Any]]:
    facade = _facade()
    async for event in facade._iter_sse(
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
) -> dict[str, Any]:
    """Make one Responses request and accept either SSE or JSON success bodies."""
    facade = _facade()
    facade._validate_responses_body(body)
    if isinstance(body.get("tools"), list):
        body["tools"] = facade._stable_sort_tools(body["tools"])
    assert body.get("model"), "model must be set"

    proxy = proxy_override
    if api_key_override is not None and base_url_override is not None:
        base, api_key = base_url_override, api_key_override
    else:
        _ = route
        runtime = await facade._resolve_runtime()
        base, api_key, proxy = facade._runtime_parts(runtime)

    url = facade._responses_url(base)
    call_trace_id = facade._generate_trace_id()
    headers = facade._auth_headers(api_key, trace_id=call_trace_id)
    stream_kwargs: dict[str, Any] = {"json": body, "headers": headers}
    timeout_config = await facade._resolve_timeout_config()
    effective_timeout = (
        float(timeout_s) if timeout_s is not None else timeout_config.read
    )
    stream_kwargs["timeout"] = timeout_config.to_httpx(read=effective_timeout)

    proxy_url = await facade.resolve_provider_proxy_url(proxy)
    client = await (
        facade._get_client(proxy_url) if proxy_url else facade._get_client()
    )
    started = time.monotonic()
    final_status = 0
    final_response_headers: Any = None
    try:
        try:
            async with client.stream("POST", url, **stream_kwargs) as response:
                final_status = response.status_code
                final_response_headers = getattr(response, "headers", None)

                if response.status_code >= 400:
                    raw = await response.aread()
                    raw_text = raw.decode("utf-8", errors="replace")
                    request_id = (
                        final_response_headers.get("x-request-id")
                        if final_response_headers is not None
                        else None
                    )
                    facade.logger.warning(
                        "responses_call non-2xx status=%s url=%s body=%.1000s "
                        "trace_id=%s x_request_id=%s",
                        response.status_code,
                        url,
                        raw_text,
                        call_trace_id,
                        request_id,
                    )
                    try:
                        error_payload = json.loads(raw_text)
                    except Exception:  # noqa: BLE001
                        error_payload = {"raw": raw_text}
                    raise facade._with_error_context(
                        facade._parse_error(
                            error_payload
                            if isinstance(error_payload, dict)
                            else {},
                            response.status_code,
                        ),
                        path="responses",
                        method="POST",
                        url=url,
                    )

                content_type = (
                    final_response_headers.get("content-type")
                    if final_response_headers is not None
                    else ""
                ) or ""
                if "text/event-stream" in content_type.lower():
                    completed: dict[str, Any] | None = None
                    last_event_type: str | None = None
                    error_terminal: dict[str, Any] | None = None
                    line_count = 0
                    byte_count = 0
                    current_event: str | None = None
                    try:
                        async for line in response.aiter_lines():
                            line_bytes = len(line.encode("utf-8"))
                            line_count += 1
                            byte_count += line_bytes
                            if line_count > facade._SSE_MAX_LINES:
                                raise facade.UpstreamError(
                                    "sse exceeded max lines",
                                    error_code=facade.EC.STREAM_TOO_LARGE.value,
                                    status_code=response.status_code,
                                )
                            if line_bytes > facade._SSE_MAX_LINE_BYTES:
                                raise facade.UpstreamError(
                                    "sse exceeded max line bytes",
                                    error_code=facade.EC.STREAM_TOO_LARGE.value,
                                    status_code=response.status_code,
                                )
                            if byte_count > facade._SSE_MAX_BYTES:
                                raise facade.UpstreamError(
                                    "sse exceeded max bytes",
                                    error_code=facade.EC.STREAM_TOO_LARGE.value,
                                    status_code=response.status_code,
                                )
                            if line == "":
                                current_event = None
                                continue
                            if line.startswith(":"):
                                continue
                            if line.startswith("event:"):
                                current_event = (
                                    line[len("event:") :].strip() or None
                                )
                                continue
                            if not line.startswith("data:"):
                                continue
                            data_raw = line[len("data:") :].lstrip()
                            if data_raw == "[DONE]":
                                break
                            try:
                                event = json.loads(data_raw)
                            except json.JSONDecodeError:
                                facade.logger.warning(
                                    "responses_call sse invalid json line=%s",
                                    data_raw[:200],
                                )
                                continue
                            if not isinstance(event, dict):
                                continue
                            if "type" not in event and current_event:
                                event["type"] = current_event
                            event_type = event.get("type")
                            if isinstance(event_type, str):
                                last_event_type = event_type
                            facade._maybe_record_usage_from_event(event)
                            if facade._is_responses_success_terminal(event_type):
                                response_object = event.get("response")
                                if isinstance(response_object, dict):
                                    completed = response_object
                            elif facade._is_responses_error_terminal(event_type):
                                error = None
                                response_object = event.get("response")
                                if isinstance(response_object, dict):
                                    error = response_object.get(
                                        "error"
                                    ) or response_object.get(
                                        "incomplete_details"
                                    )
                                if error is None:
                                    error = event.get("error") or event.get(
                                        "incomplete_details"
                                    )
                                if isinstance(error, dict):
                                    error_terminal = error
                    except facade.UpstreamError:
                        raise
                    except asyncio.CancelledError:
                        raise
                    except httpx.HTTPError as exc:
                        raise facade.UpstreamError(
                            f"responses_call sse interrupted: {exc}",
                            status_code=response.status_code,
                            error_code=facade.EC.TEXT_STREAM_INTERRUPTED.value,
                            payload={
                                "path": "responses",
                                "method": "POST",
                                "url": url,
                                "x_trace_id": call_trace_id,
                            },
                        ) from exc

                    if completed is not None:
                        return completed
                    if error_terminal is not None:
                        upstream_code = error_terminal.get(
                            "code"
                        ) or error_terminal.get("type")
                        upstream_message = error_terminal.get("message")
                        raise facade.UpstreamError(
                            upstream_message
                            if isinstance(upstream_message, str)
                            and upstream_message
                            else "responses_call sse error terminal",
                            status_code=response.status_code,
                            error_code=(
                                upstream_code
                                if isinstance(upstream_code, str)
                                and upstream_code
                                else facade.EC.BAD_RESPONSE.value
                            ),
                            payload={
                                "path": "responses",
                                "method": "POST",
                                "url": url,
                                "x_trace_id": call_trace_id,
                                "last_event_type": last_event_type,
                                "upstream_error": error_terminal,
                            },
                        )
                    raise facade.UpstreamError(
                        "responses_call sse missing terminal frame",
                        status_code=response.status_code,
                        error_code=facade.EC.BAD_RESPONSE.value,
                        payload={
                            "path": "responses",
                            "method": "POST",
                            "url": url,
                            "x_trace_id": call_trace_id,
                            "last_event_type": last_event_type,
                        },
                    )

                raw = await response.aread()
                try:
                    payload = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception as exc:  # noqa: BLE001
                    raise facade.UpstreamError(
                        "responses_call returned invalid JSON",
                        status_code=response.status_code,
                        error_code=facade.EC.BAD_RESPONSE.value,
                        payload={
                            "path": "responses",
                            "method": "POST",
                            "url": url,
                            "x_trace_id": call_trace_id,
                        },
                    ) from exc
                if not isinstance(payload, dict):
                    raise facade.UpstreamError(
                        "responses_call returned non-object payload",
                        status_code=response.status_code,
                        error_code=facade.EC.BAD_RESPONSE.value,
                        payload={
                            "path": "responses",
                            "method": "POST",
                            "url": url,
                            "x_trace_id": call_trace_id,
                        },
                    )
                if isinstance(payload.get("usage"), dict):
                    facade._record_usage(payload["usage"])
                return payload
        except httpx.TimeoutException as exc:
            raise facade.UpstreamError(
                f"responses_call upstream timeout: {exc}",
                status_code=None,
                error_code=facade.EC.UPSTREAM_TIMEOUT.value,
                payload={
                    "path": "responses",
                    "method": "POST",
                    "url": url,
                    "x_trace_id": call_trace_id,
                },
            ) from exc
        except httpx.HTTPError as exc:
            raise facade.UpstreamError(
                f"responses_call upstream network error: {exc}",
                status_code=None,
                error_code=facade.EC.UPSTREAM_ERROR.value,
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
            facade._log_upstream_call(
                endpoint=endpoint_label,
                status=final_status,
                duration_ms=duration_ms,
                trace_id=call_trace_id,
                response_headers=final_response_headers,
            )
        except Exception:  # noqa: BLE001
            facade.logger.debug(
                "responses_call meta log failed",
                exc_info=True,
            )


__all__ = [
    "_iter_sse",
    "_iter_sse_with_runtime",
    "responses_call",
    "stream_completion",
]
