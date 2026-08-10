"""Direct Images API response handling for generation requests."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from .generated_payload import (
    GeneratedImageResult,
    decode_inline_image_base64,
)
from .image_execution import ImageExecutionRequest, ImageRequestContext
from .response_evidence import (
    direct_image_response_metadata,
    direct_image_response_metadata_from_headers,
)
from .transport import CurlSSEResponseContext


_FINAL_EVENTS = frozenset(
    {
        "image_generation.completed",
        "image_edit.completed",
        "response.output_item.done",
        "response.completed",
        "response.done",
    }
)
_PARTIAL_EVENTS = frozenset(
    {
        "image_generation.partial_image",
        "image_edit.partial_image",
        "response.image_generation_call.partial_image",
    }
)
_ERROR_EVENTS = frozenset(
    {
        "error",
        "image_generation.failed",
        "image_edit.failed",
        "response.failed",
        "response.incomplete",
    }
)
_RETRY_STATUSES = frozenset({502, 503, 504})


@dataclass(frozen=True)
class DirectGenerationStreamCall:
    services: UpstreamServices
    request: ImageExecutionRequest
    body: dict[str, Any]
    headers: dict[str, str]
    url: str
    trace_id: str
    context: ImageRequestContext
    read_timeout_s: float
    proxy_url: str | None
    pinned_target: Any | None
    prepare_attempt: Callable[[int], Awaitable[None]]


def direct_image_response_result_unknown_error(
    exc: BaseException,
    *,
    path: str,
    method: str,
    url: str,
    trace_id: str,
    status_code: int,
    response_metadata: dict[str, Any] | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> BaseException:
    services = resolve_image_upstream_services(runtime)
    payload: dict[str, Any] = {
        "path": path,
        "method": method,
        "url": url,
        "x_trace_id": trace_id,
        "upstream_result_unknown": True,
        "response_received": True,
        "wrapped_error_code": services.infrastructure.EC.BAD_RESPONSE.value,
        "exception": type(exc).__name__,
    }
    if response_metadata:
        payload.update(response_metadata)
    return services.infrastructure.UpstreamError(
        (
            f"{path} returned HTTP {status_code}, but the response could not be "
            "decoded; upstream result is unknown and was not retried automatically"
        ),
        status_code=status_code,
        error_code=services.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value,
        payload=payload,
    )


def _revised_prompt(event: dict[str, Any]) -> str | None:
    candidates: list[Any] = [event.get("revised_prompt")]
    item = event.get("item")
    if isinstance(item, dict):
        candidates.append(item.get("revised_prompt"))
    response = event.get("response")
    if isinstance(response, dict):
        outputs = response.get("output")
        if isinstance(outputs, list):
            candidates.extend(
                output.get("revised_prompt")
                for output in outputs
                if isinstance(output, dict)
            )
    return next(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate, str) and candidate
        ),
        None,
    )


def _stream_error(
    call: DirectGenerationStreamCall,
    event: dict[str, Any],
) -> BaseException:
    detail = event.get("error")
    if not isinstance(detail, dict):
        detail = event
    raw_code = detail.get("code") or detail.get("type")
    raw_message = detail.get("message")
    return call.services.infrastructure.UpstreamError(
        (
            raw_message
            if isinstance(raw_message, str) and raw_message
            else "direct image stream returned an error event"
        ),
        status_code=200,
        error_code=(
            raw_code
            if isinstance(raw_code, str) and raw_code
            else call.services.infrastructure.EC.UPSTREAM_ERROR.value
        ),
        payload={
            "path": "images/generations",
            "method": "POST",
            "url": call.url,
            "x_trace_id": call.trace_id,
            "response_received": True,
            "upstream_error": detail,
        },
    )


async def _consume_stream(
    call: DirectGenerationStreamCall,
    *,
    http_attempt: int,
    response_metadata: dict[str, Any],
) -> list[GeneratedImageResult]:
    expected_results = max(1, int(call.request.n))
    results: list[GeneratedImageResult] = []
    seen_results: set[bytes] = set()
    partial_count = 0

    async def record_response_head(
        status_code: int,
        response_headers: dict[str, str],
    ) -> None:
        metadata = direct_image_response_metadata_from_headers(
            status_code=status_code,
            response_headers=response_headers,
            http_attempts=http_attempt,
        )
        response_metadata.clear()
        response_metadata.update(metadata)
        await call.services.transport.emit_image_progress(
            call.request.progress_callback,
            "response_ready" if 200 <= status_code < 300 else "response_received",
            **metadata,
        )

    source = call.services.transport.iter_sse_curl(
        url=call.url,
        json_body=call.body,
        headers=call.headers,
        timeout_s=call.read_timeout_s,
        proxy_url=call.proxy_url,
        pinned_target=call.pinned_target,
        allow_non_sse_payload=True,
        on_dispatch_ready=lambda: call.prepare_attempt(http_attempt),
        on_response_head=record_response_head,
        response_context=CurlSSEResponseContext(
            endpoint_label="images_generations",
            error_path="images/generations",
        ),
    )
    async with aclosing(source) as events:
        async for event in events:
            event_type = event.get("type")
            if event_type == call.services.core.JSON_PAYLOAD_SENTINEL_TYPE:
                payload = event.get("payload")
                if isinstance(payload, dict):
                    call.services.core.record_usage(payload.get("usage"))
                return await call.services.core.extract_image_results(
                    payload,
                    200,
                    proxy_url=call.proxy_url,
                    request_context=call.context,
                )
            if event_type in _PARTIAL_EVENTS:
                partial_count += 1
                await call.services.transport.emit_image_progress(
                    call.request.progress_callback,
                    "partial_image",
                    index=partial_count - 1,
                    count=partial_count,
                    has_preview=isinstance(
                        event.get("partial_image")
                        or event.get("partial_image_b64")
                        or event.get("b64_json"),
                        str,
                    ),
                )
                continue
            if event_type in _ERROR_EVENTS:
                raise _stream_error(call, event)
            if event_type not in _FINAL_EVENTS:
                continue

            encoded = call.services.core.extract_image_b64_from_payload(event)
            if not isinstance(encoded, str) or not encoded:
                continue
            digest = hashlib.sha256(encoded.encode("utf-8")).digest()
            if digest in seen_results:
                continue
            seen_results.add(digest)
            results.append(
                (
                    decode_inline_image_base64(encoded),
                    _revised_prompt(event),
                )
            )
            if len(results) >= expected_results:
                return results

    if results:
        return results
    raise direct_image_response_result_unknown_error(
        RuntimeError("direct image stream ended before a final image event"),
        path="images/generations",
        method="POST",
        url=call.url,
        trace_id=call.trace_id,
        status_code=200,
        response_metadata=response_metadata,
        runtime=call.request.upstream_runtime,
    )


async def stream_direct_generation_response(
    call: DirectGenerationStreamCall,
) -> list[GeneratedImageResult]:
    response_metadata: dict[str, Any] = {}
    for http_attempt in range(1, 3):
        try:
            return await _consume_stream(
                call,
                http_attempt=http_attempt,
                response_metadata=response_metadata,
            )
        except call.services.infrastructure.UpstreamError as exc:
            status_code = int(getattr(exc, "status_code", None) or 0)
            payload = getattr(exc, "payload", None)
            if isinstance(payload, dict) and "upstream_error" in payload:
                raise
            if status_code in _RETRY_STATUSES and http_attempt < 2:
                await call.services.infrastructure.asyncio.sleep(1.0)
                continue
            if 500 <= status_code < 600:
                metadata = dict(response_metadata)
                metadata.setdefault("upstream_response_status_code", status_code)
                metadata["upstream_response_http_attempts"] = http_attempt
                raise call.services.infrastructure.UpstreamError(
                    "direct image stream returned an ambiguous server failure; "
                    "upstream result is unknown after bounded same-key delivery",
                    status_code=status_code,
                    error_code=(
                        call.services.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
                    ),
                    payload={
                        "path": "images/generations",
                        "method": "POST",
                        "url": call.url,
                        "x_trace_id": call.trace_id,
                        "upstream_result_unknown": True,
                        "response_received": True,
                        **metadata,
                    },
                ) from exc
            if (
                status_code == 0
                or (
                    200 <= status_code < 300
                    and isinstance(payload, dict)
                    and payload.get("response_received") is True
                )
            ):
                metadata = dict(response_metadata)
                metadata["upstream_response_http_attempts"] = http_attempt
                raise call.services.infrastructure.UpstreamError(
                    "direct image stream ended ambiguously after dispatch; "
                    "upstream result is unknown and was not replayed",
                    status_code=status_code,
                    error_code=(
                        call.services.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value
                    ),
                    payload={
                        "path": "images/generations",
                        "method": "POST",
                        "url": call.url,
                        "x_trace_id": call.trace_id,
                        "upstream_result_unknown": True,
                        **metadata,
                    },
                ) from exc
            raise
    raise AssertionError("unreachable")


async def complete_direct_generation_response(
    *,
    services: UpstreamServices,
    request: ImageExecutionRequest,
    response: Any,
    started: float,
    trace_id: str,
    url: str,
    context: ImageRequestContext,
    proxy_url: str | None,
    http_attempts: int,
) -> list[tuple[str, str | None]]:
    duration_ms = (services.infrastructure.time.monotonic() - started) * 1000.0
    services.core.log_upstream_call(
        endpoint="images_generations",
        status=response.status_code,
        duration_ms=duration_ms,
        trace_id=trace_id,
        response_headers=getattr(response, "headers", None),
    )
    response_metadata = direct_image_response_metadata(
        response,
        http_attempts=http_attempts,
    )
    await services.transport.emit_image_progress(
        request.progress_callback,
        (
            "response_ready"
            if 200 <= response.status_code < 300
            else "response_received"
        ),
        **response_metadata,
    )
    if 500 <= response.status_code < 600:
        raise services.infrastructure.UpstreamError(
            "direct image POST returned an ambiguous server failure; "
            "upstream result is unknown after bounded same-key delivery",
            status_code=response.status_code,
            error_code=services.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value,
            payload={
                "path": "images/generations",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
                "upstream_result_unknown": True,
                "response_received": True,
                **response_metadata,
            },
        )

    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        if 200 <= response.status_code < 300:
            raise direct_image_response_result_unknown_error(
                exc,
                path="images/generations",
                method="POST",
                url=url,
                trace_id=trace_id,
                status_code=response.status_code,
                response_metadata=response_metadata,
                runtime=request.upstream_runtime,
            ) from exc
        raise services.infrastructure.UpstreamError(
            "upstream returned invalid JSON",
            status_code=response.status_code,
            error_code=services.infrastructure.EC.BAD_RESPONSE.value,
            payload={
                "path": "images/generations",
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
                "response_received": True,
                **response_metadata,
            },
        ) from exc

    if response.status_code >= 400:
        error = services.core.with_error_context(
            services.core.parse_error(
                payload if isinstance(payload, dict) else {},
                response.status_code,
            ),
            path="images/generations",
            method="POST",
            url=url,
        )
        error.payload.setdefault("x_trace_id", trace_id)
        error.payload["response_received"] = True
        error.payload.update(response_metadata)
        raise error
    if isinstance(payload, dict):
        services.core.record_usage(payload.get("usage"))
    return await services.core.extract_image_results(
        payload,
        response.status_code,
        proxy_url=proxy_url,
        request_context=context,
    )


__all__ = [
    "DirectGenerationStreamCall",
    "complete_direct_generation_response",
    "direct_image_response_result_unknown_error",
    "stream_direct_generation_response",
]
