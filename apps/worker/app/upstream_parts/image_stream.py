"""Responses image-generation streaming fallback."""

from __future__ import annotations

from ..provider_runtime.contracts import ImageProbeRequest
from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)

import json
from dataclasses import dataclass
from typing import Any

from lumen_core.providers import ProviderProxyDefinition

from .image_execution import ImageExecutionRequest
from .transport import ImageProgressCallback


async def run_image_probe(
    request: ImageProbeRequest,
    *,
    runtime: ImageUpstreamRuntime,
) -> tuple[str, str | None]:
    services = resolve_image_upstream_services(runtime)
    provider = request.provider
    return await services.responses.responses_image_stream(
        ImageExecutionRequest(
            action="generate",
            prompt=request.prompt,
            size=request.size,
            images=None,
            mask=None,
            n=1,
            quality=request.quality,
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
            provider_override=None,
            user_id=None,
            upstream_runtime=runtime,
        ),
        base_url_override=provider.base_url,
        api_key_override=provider.api_key,
        proxy_override=provider.proxy,
    )


def _json_payload_revised_prompt(payload: dict[str, Any]) -> str | None:
    candidates: list[Any] = []
    data = payload.get("data")
    if isinstance(data, list):
        candidates.extend(
            entry.get("revised_prompt") for entry in data if isinstance(entry, dict)
        )
    for container in (
        payload,
        payload.get("response") if isinstance(payload.get("response"), dict) else None,
    ):
        if not isinstance(container, dict):
            continue
        outputs = container.get("output")
        if isinstance(outputs, list):
            candidates.extend(
                entry.get("revised_prompt")
                for entry in outputs
                if isinstance(entry, dict)
            )
    item = payload.get("item")
    if isinstance(item, dict):
        candidates.append(item.get("revised_prompt"))
    return next(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate, str) and candidate
        ),
        None,
    )


@dataclass
class _ImageStreamState:
    final_b64: str | None = None
    revised_prompt: str | None = None
    partial_count: int = 0
    last_event_type: str | None = None
    upstream_error_detail: dict[str, Any] | None = None
    json_fallback_content_type: str | None = None
    json_fallback_body_summary: str | None = None


def _image_quality_from_body(
    body: dict[str, Any],
    requested_quality: str,
    *,
    services: UpstreamServices,
) -> str:
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "image_generation":
            tool_quality = tool.get("quality")
            if isinstance(tool_quality, str):
                return tool_quality
    return services.core.normalize_image_quality(requested_quality)


def _json_payload_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    nested_response = payload.get("response")
    if isinstance(nested_response, dict):
        nested_usage = nested_response.get("usage")
        if isinstance(nested_usage, dict):
            return nested_usage
    return None


async def _consume_json_fallback_event(
    state: _ImageStreamState,
    event: dict[str, Any],
    *,
    progress_callback: ImageProgressCallback | None,
    trace_id: str,
    action: str,
    size: str,
    services: UpstreamServices,
) -> None:
    content_type = event.get("content_type")
    state.json_fallback_content_type = (
        content_type if isinstance(content_type, str) else None
    )
    payload = event.get("payload")
    if not isinstance(payload, dict):
        state.json_fallback_body_summary = (
            f"non-object payload type={type(payload).__name__}"
        )
        return

    usage = _json_payload_usage(payload)
    if usage is not None:
        services.core.record_usage(usage)
    billable = services.core.extract_image_billable_count(payload)
    if billable is not None:
        services.infrastructure.logger.info(
            "responses fallback json payload images_count=%d "
            "trace_id=%s action=%s size=%s",
            billable,
            trace_id,
            action,
            size,
        )

    extracted = services.core.extract_image_b64_from_payload(payload)
    if extracted:
        state.final_b64 = extracted
        state.revised_prompt = _json_payload_revised_prompt(payload)
        await services.transport.emit_image_progress(progress_callback, "final_image")
        await services.transport.emit_image_progress(progress_callback, "completed")
        return

    error = payload.get("error")
    if isinstance(error, dict):
        state.upstream_error_detail = error
    state.json_fallback_body_summary = f"keys={sorted(payload.keys())[:10]}"


def _terminal_event_error(
    event: dict[str, Any],
    event_type: Any,
    *,
    services: UpstreamServices,
) -> dict[str, Any] | None:
    if not services.core.is_responses_error_terminal(event_type):
        return None
    response_object = event.get("response")
    error = None
    if isinstance(response_object, dict):
        error = response_object.get("error") or response_object.get(
            "incomplete_details"
        )
    if error is None:
        error = event.get("error") or event.get("incomplete_details")
    return error if isinstance(error, dict) else None


def _output_item_error(
    event: dict[str, Any],
    *,
    last_event_type: str | None,
    services: UpstreamServices,
) -> dict[str, Any] | None:
    if event.get("type") != "response.output_item.done":
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if (
        isinstance(item_type, str)
        and item_type not in services.core.KNOWN_OUTPUT_ITEM_TYPES
    ):
        services.infrastructure.logger.warning(
            "output_item.done with unknown item.type=%r last_event=%s",
            item_type,
            last_event_type,
        )
    if item.get("status") not in {"failed", "incomplete"}:
        return None
    error = item.get("error") or item.get("incomplete_details")
    return error if isinstance(error, dict) else None


async def _consume_image_stream_event(
    state: _ImageStreamState,
    event: dict[str, Any],
    *,
    progress_callback: ImageProgressCallback | None,
    trace_id: str,
    action: str,
    size: str,
    services: UpstreamServices,
) -> None:
    event_type = event.get("type")
    if isinstance(event_type, str):
        state.last_event_type = event_type
    if event_type == services.core.JSON_PAYLOAD_SENTINEL_TYPE:
        await _consume_json_fallback_event(
            state,
            event,
            progress_callback=progress_callback,
            trace_id=trace_id,
            action=action,
            size=size,
            services=services,
        )
        return

    if event_type == "response.image_generation_call.partial_image":
        state.partial_count += 1
        await services.transport.emit_image_progress(
            progress_callback,
            "partial_image",
            index=event.get("partial_image_index", state.partial_count - 1),
            count=state.partial_count,
            has_preview=isinstance(
                event.get("partial_image") or event.get("partial_image_b64"),
                str,
            ),
        )

    extracted = services.core.extract_response_image_b64(event)
    if extracted:
        state.final_b64 = extracted
        state.revised_prompt = (
            services.core.extract_response_revised_prompt(event) or state.revised_prompt
        )
        await services.transport.emit_image_progress(progress_callback, "final_image")
    if services.core.is_responses_success_terminal(event_type):
        await services.transport.emit_image_progress(progress_callback, "completed")

    terminal_error = _terminal_event_error(
        event,
        event_type,
        services=services,
    )
    if terminal_error is not None:
        state.upstream_error_detail = terminal_error
    item_error = _output_item_error(
        event,
        last_event_type=state.last_event_type,
        services=services,
    )
    if item_error is not None:
        state.upstream_error_detail = item_error


def _upstream_error_identity(
    detail: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    if not isinstance(detail, dict):
        return None, None
    raw_code = detail.get("code") or detail.get("type")
    raw_message = detail.get("message")
    code = raw_code if isinstance(raw_code, str) and raw_code else None
    message = raw_message if isinstance(raw_message, str) and raw_message else None
    return code, message


def _raise_missing_image(
    state: _ImageStreamState,
    *,
    action: str,
    size: str,
    quality: str,
    trace_id: str,
    services: UpstreamServices,
) -> None:
    diagnostic: dict[str, Any] = {
        "action": action,
        "size": size,
        "quality": quality,
        "endpoint": "responses:image_generation",
        "last_event_type": state.last_event_type,
        "partial_count": state.partial_count,
        "has_final_image": False,
        "trace_id": trace_id,
        "upstream_error": services.core.summarize_upstream_error_detail(
            state.upstream_error_detail
        ),
    }
    if state.json_fallback_content_type is not None:
        diagnostic["json_fallback_content_type"] = state.json_fallback_content_type
    if state.json_fallback_body_summary is not None:
        diagnostic["json_fallback_body_summary"] = state.json_fallback_body_summary
    services.infrastructure.logger.warning(
        "responses fallback drained without image: %s",
        json.dumps(
            diagnostic,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ),
    )

    upstream_code, upstream_message = _upstream_error_identity(
        state.upstream_error_detail
    )
    raise services.infrastructure.UpstreamError(
        upstream_message or "responses image fallback returned no image",
        status_code=200,
        error_code=upstream_code or services.infrastructure.EC.NO_IMAGE_RETURNED.value,
        payload={
            "path": "responses",
            "action": action,
            "size": size,
            "last_event_type": state.last_event_type,
            "partial_count": state.partial_count,
            "upstream_error": state.upstream_error_detail,
            "trace_id": trace_id,
            "json_fallback_content_type": state.json_fallback_content_type,
            "json_fallback_body_summary": state.json_fallback_body_summary,
        },
    )


async def _responses_image_stream(
    request: ImageExecutionRequest,
    *,
    use_httpx: bool = False,
    base_url_override: str | None = None,
    api_key_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    pinned_target_override: Any | None = None,
) -> tuple[str, str | None]:
    """Use ``/v1/responses`` and ``image_generation`` as the streaming fallback."""
    services = resolve_image_upstream_services(request.upstream_runtime)
    context = request.request_context
    proxy = proxy_override
    if base_url_override is not None and api_key_override is not None:
        base, api_key = base_url_override, api_key_override
    else:
        provider_runtime = await services.core.resolve_runtime()
        base, api_key, proxy = services.core.runtime_parts(provider_runtime)
        if pinned_target_override is None:
            pinned_target_override = getattr(
                provider_runtime,
                "_byok_http_target",
                None,
            )

    image_urls: list[str] | None = None
    if request.action == "edit":
        sidecar_base_url: str | None = None
        sidecar_token: str | None = None
        try:
            sidecar_base_url = await services.direct.resolve_image_job_base_url()
            sidecar_token = services.image_jobs.image_job_sidecar_token()
        except Exception as exc:  # noqa: BLE001
            services.infrastructure.logger.debug(
                "reference push sidecar configuration fallback err=%s",
                exc,
            )
        ref_urls = await services.references.resolve_reference_image_urls(
            request.images,
            base_url=sidecar_base_url,
            api_key=sidecar_token,
            user_id=request.user_id,
            request_context=context,
        )
        image_urls = ref_urls or None

    body = services.image_jobs.build_responses_image_body(
        request,
        image_urls=image_urls,
    )
    image_quality = _image_quality_from_body(
        body,
        request.quality,
        services=services,
    )
    state = _ImageStreamState()

    await services.transport.emit_image_progress(
        request.progress_callback,
        "fallback_started",
        action=request.action,
        size=request.size,
    )
    read_timeout_s = services.direct.select_image_read_timeout(request.size)
    call_trace_id = context.trace_id
    call_headers = services.core.auth_headers(api_key, trace_id=call_trace_id)
    proxy_url = await services.infrastructure.resolve_provider_proxy_url(proxy)
    response_url = services.requests.responses_url(base)
    pinned_target = (
        None
        if proxy_url
        else services.requests.validated_byok_target_for_request(
            pinned_target_override,
            response_url,
        )
    )

    async def record_dispatch_ready() -> None:
        await services.transport.emit_image_progress(
            request.progress_callback,
            "dispatch_ready",
        )

    async def record_response_ready() -> None:
        await services.transport.emit_image_progress(
            request.progress_callback,
            "response_ready",
        )

    if use_httpx:
        runtime_kwargs: dict[str, Any] = {
            "base": base,
            "api_key": api_key,
            "body": body,
            "read_timeout_s": read_timeout_s,
            "trace_id": call_trace_id,
            "proxy_url": proxy_url,
            "allow_non_sse_payload": True,
            "on_dispatch_ready": record_dispatch_ready,
            "on_response_ready": record_response_ready,
        }
        if pinned_target is not None:
            runtime_kwargs["pinned_target"] = pinned_target
        sse_source = services.responses.iter_sse_with_runtime(**runtime_kwargs)
    else:
        curl_kwargs: dict[str, Any] = {
            "url": response_url,
            "json_body": body,
            "headers": call_headers,
            "timeout_s": read_timeout_s,
            "proxy_url": proxy_url,
            "allow_non_sse_payload": True,
            "on_dispatch_ready": record_dispatch_ready,
            "on_response_ready": record_response_ready,
        }
        if pinned_target is not None:
            curl_kwargs["pinned_target"] = pinned_target
        sse_source = services.transport.iter_sse_curl(**curl_kwargs)

    async for event in sse_source:
        await _consume_image_stream_event(
            state,
            event,
            progress_callback=request.progress_callback,
            trace_id=call_trace_id,
            action=request.action,
            size=request.size,
            services=services,
        )

    if state.final_b64:
        return state.final_b64, state.revised_prompt
    _raise_missing_image(
        state,
        action=request.action,
        size=request.size,
        quality=image_quality,
        trace_id=call_trace_id,
        services=services,
    )
    raise AssertionError("unreachable")


__all__ = ["_responses_image_stream"]
