"""Responses image-generation streaming fallback."""

from __future__ import annotations

import importlib
import json
from typing import Any

from lumen_core.providers import ProviderProxyDefinition

from .transport import ImageProgressCallback

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    """Resolve compatibility dependencies at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


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


async def _responses_image_stream(
    *,
    prompt: str,
    size: str,
    action: str,
    images: list[bytes] | None = None,
    quality: str = "high",
    output_format: str | None = None,
    output_compression: int | None = None,
    background: str | None = None,
    moderation: str | None = None,
    model: str | None = None,
    progress_callback: ImageProgressCallback | None = None,
    use_httpx: bool = False,
    base_url_override: str | None = None,
    api_key_override: str | None = None,
    proxy_override: ProviderProxyDefinition | None = None,
    user_id: str | None = None,
) -> tuple[str, str | None]:
    """Use ``/v1/responses`` and ``image_generation`` as the streaming fallback."""
    facade = _facade()
    proxy = proxy_override
    if base_url_override is not None and api_key_override is not None:
        base, api_key = base_url_override, api_key_override
    else:
        runtime = await facade._resolve_runtime()
        base, api_key, proxy = facade._runtime_parts(runtime)

    image_urls: list[str] | None = None
    if action == "edit":
        sidecar_base_url: str | None = None
        try:
            sidecar_base_url = await facade._resolve_image_job_base_url()
        except Exception as exc:  # noqa: BLE001
            facade.logger.debug(
                "reference push base_url resolve fallback err=%s",
                exc,
            )
        ref_urls = await facade._resolve_reference_image_urls(
            images,
            base_url=sidecar_base_url,
            api_key=api_key,
            user_id=user_id,
        )
        image_urls = ref_urls or None

    body = facade._build_responses_image_body(
        action=action,
        prompt=prompt,
        size=size,
        images=images,
        image_urls=image_urls,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
        model=model,
    )
    image_quality: str | None = None
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "image_generation":
            continue
        tool_quality = tool.get("quality")
        if isinstance(tool_quality, str):
            image_quality = tool_quality
            break
    if image_quality is None:
        image_quality = facade._normalize_image_quality(quality)

    final_b64: str | None = None
    revised_prompt: str | None = None
    partial_count = 0
    last_event_type: str | None = None
    upstream_error_detail: dict[str, Any] | None = None
    json_fallback_content_type: str | None = None
    json_fallback_body_summary: str | None = None

    await facade._emit_image_progress(
        progress_callback,
        "fallback_started",
        action=action,
        size=size,
    )
    read_timeout_s = facade._select_image_read_timeout(size)
    call_trace_id = facade._generate_trace_id()
    call_headers = facade._auth_headers(api_key, trace_id=call_trace_id)
    proxy_url = await facade.resolve_provider_proxy_url(proxy)
    sse_source = (
        facade._iter_sse_with_runtime(
            base=base,
            api_key=api_key,
            body=body,
            read_timeout_s=read_timeout_s,
            trace_id=call_trace_id,
            proxy_url=proxy_url,
            allow_non_sse_payload=True,
        )
        if use_httpx
        else facade._iter_sse_curl(
            url=facade._responses_url(base),
            json_body=body,
            headers=call_headers,
            timeout_s=read_timeout_s,
            proxy_url=proxy_url,
            allow_non_sse_payload=True,
        )
    )

    async for event in sse_source:
        event_type = event.get("type")
        if isinstance(event_type, str):
            last_event_type = event_type

        if event_type == facade._JSON_PAYLOAD_SENTINEL_TYPE:
            json_payload = event.get("payload")
            json_fallback_content_type = (
                event.get("content_type")
                if isinstance(event.get("content_type"), str)
                else None
            )
            if isinstance(json_payload, dict):
                if isinstance(json_payload.get("usage"), dict):
                    facade._record_usage(json_payload["usage"])
                else:
                    nested_response = json_payload.get("response")
                    if isinstance(nested_response, dict) and isinstance(
                        nested_response.get("usage"),
                        dict,
                    ):
                        facade._record_usage(nested_response["usage"])

                billable = facade._extract_image_billable_count(json_payload)
                if billable is not None:
                    facade.logger.info(
                        "responses fallback json payload images_count=%d "
                        "trace_id=%s action=%s size=%s",
                        billable,
                        call_trace_id,
                        action,
                        size,
                    )

                extracted = facade._extract_image_b64_from_payload(json_payload)
                if extracted:
                    final_b64 = extracted
                    revised_prompt = _json_payload_revised_prompt(json_payload)
                    await facade._emit_image_progress(
                        progress_callback,
                        "final_image",
                    )
                    await facade._emit_image_progress(
                        progress_callback,
                        "completed",
                    )
                else:
                    error = json_payload.get("error")
                    if isinstance(error, dict):
                        upstream_error_detail = error
                    summary_keys = sorted(json_payload.keys())[:10]
                    json_fallback_body_summary = f"keys={summary_keys}"
            else:
                json_fallback_body_summary = (
                    f"non-object payload type={type(json_payload).__name__}"
                )
            continue

        if event_type == "response.image_generation_call.partial_image":
            partial_count += 1
            await facade._emit_image_progress(
                progress_callback,
                "partial_image",
                index=event.get("partial_image_index", partial_count - 1),
                count=partial_count,
                has_preview=isinstance(
                    event.get("partial_image") or event.get("partial_image_b64"),
                    str,
                ),
            )

        extracted = facade._extract_response_image_b64(event)
        if extracted:
            final_b64 = extracted
            revised_prompt = (
                facade._extract_response_revised_prompt(event) or revised_prompt
            )
            await facade._emit_image_progress(progress_callback, "final_image")

        if facade._is_responses_success_terminal(event_type):
            await facade._emit_image_progress(progress_callback, "completed")

        if facade._is_responses_error_terminal(event_type):
            response_object = event.get("response")
            error = None
            if isinstance(response_object, dict):
                error = response_object.get("error") or response_object.get(
                    "incomplete_details"
                )
            if error is None:
                error = event.get("error") or event.get("incomplete_details")
            if isinstance(error, dict):
                upstream_error_detail = error

        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                item_type = item.get("type")
                if (
                    isinstance(item_type, str)
                    and item_type not in facade._KNOWN_OUTPUT_ITEM_TYPES
                ):
                    facade.logger.warning(
                        "output_item.done with unknown item.type=%r last_event=%s",
                        item_type,
                        last_event_type,
                    )
                if item.get("status") in {"failed", "incomplete"}:
                    item_error = item.get("error") or item.get("incomplete_details")
                    if isinstance(item_error, dict):
                        upstream_error_detail = item_error

    if final_b64:
        return final_b64, revised_prompt

    safe_upstream_error = facade._summarize_upstream_error_detail(upstream_error_detail)
    diagnostic: dict[str, Any] = {
        "action": action,
        "size": size,
        "quality": image_quality,
        "endpoint": "responses:image_generation",
        "last_event_type": last_event_type,
        "partial_count": partial_count,
        "has_final_image": False,
        "trace_id": call_trace_id,
        "upstream_error": safe_upstream_error,
    }
    if json_fallback_content_type is not None:
        diagnostic["json_fallback_content_type"] = json_fallback_content_type
    if json_fallback_body_summary is not None:
        diagnostic["json_fallback_body_summary"] = json_fallback_body_summary
    facade.logger.warning(
        "responses fallback drained without image: %s",
        json.dumps(
            diagnostic,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ),
    )

    upstream_code: str | None = None
    upstream_message: str | None = None
    if isinstance(upstream_error_detail, dict):
        raw_code = upstream_error_detail.get("code") or upstream_error_detail.get(
            "type"
        )
        if isinstance(raw_code, str) and raw_code:
            upstream_code = raw_code
        raw_message = upstream_error_detail.get("message")
        if isinstance(raw_message, str) and raw_message:
            upstream_message = raw_message

    raise facade.UpstreamError(
        upstream_message or "responses image fallback returned no image",
        status_code=200,
        error_code=upstream_code or facade.EC.NO_IMAGE_RETURNED.value,
        payload={
            "path": "responses",
            "action": action,
            "size": size,
            "last_event_type": last_event_type,
            "partial_count": partial_count,
            "upstream_error": upstream_error_detail,
            "trace_id": call_trace_id,
            "json_fallback_content_type": json_fallback_content_type,
            "json_fallback_body_summary": json_fallback_body_summary,
        },
    )


__all__ = ["_responses_image_stream"]
