"""Pure image request-shaping helpers.

This module has no access to worker runtime state. Callers pass policy values,
retry state, validation, and reference-image normalization explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, NotRequired, Protocol, TypedDict, Unpack


class NormalizeImageOutputCompression(Protocol):
    def __call__(
        self,
        value: int | None,
        *,
        output_format: str,
    ) -> int | None: ...


class AddImageOutputOptions(Protocol):
    def __call__(
        self,
        body: dict[str, Any],
        *,
        output_format: str | None,
        output_compression: int | None,
        background: str | None,
        moderation: str | None,
    ) -> None: ...


NormalizeImageString = Callable[[str | None], str]
StableSortTools = Callable[[list[Any]], list[Any]]
NormalizeReferenceImage = Callable[[bytes], tuple[bytes, str]]
ApplyRetryCacheBusters = Callable[[dict[str, Any], int, str, str], None]
ValidateResponsesBody = Callable[[dict[str, Any]], None]
ParseSizePixels = Callable[[str], int | None]
JsonDumpsStable = Callable[[Any], str]
ImageRequestFiles = list[tuple[str, tuple[str, bytes, str]]] | None
ImageFileFingerprints = Callable[[ImageRequestFiles], list[dict[str, Any]]]


class ImageIdempotencyKey(Protocol):
    def __call__(
        self,
        *,
        trace_id: str,
        endpoint: str,
        body: dict[str, Any] | None = None,
        files: ImageRequestFiles = None,
    ) -> str: ...


@dataclass(frozen=True)
class ImageRequestPolicy:
    upstream_model: str | None
    default_responses_model: str | None
    default_image_instructions: str
    image_qualities: Collection[str]
    image_output_formats: Collection[str]
    image_backgrounds: Collection[str]
    image_moderations: Collection[str]
    default_image_quality: str
    default_image_output_format: str
    default_image_output_compression: int
    default_image_background: str
    default_image_moderation: str
    partial_images_max_pixels: int
    image_job_retention_days: int


@dataclass(frozen=True)
class ImageOutputOptionsHooks:
    normalize_image_background: NormalizeImageString
    normalize_image_output_format: NormalizeImageString
    normalize_image_output_compression: NormalizeImageOutputCompression
    normalize_image_moderation: NormalizeImageString


@dataclass(frozen=True)
class ImageJobBodyHooks:
    normalize_image_quality: NormalizeImageString
    add_image_output_options: AddImageOutputOptions


@dataclass(frozen=True)
class ResponsesImageBodyHooks:
    normalize_image_quality: NormalizeImageString
    add_image_output_options: AddImageOutputOptions
    parse_size_pixels: ParseSizePixels
    normalize_reference_image: NormalizeReferenceImage
    stable_sort_tools: StableSortTools
    apply_retry_cache_busters: ApplyRetryCacheBusters
    validate_responses_body: ValidateResponsesBody


@dataclass(frozen=True)
class ImageIdempotencyKeyHooks:
    json_dumps_stable: JsonDumpsStable
    image_file_fingerprints: ImageFileFingerprints


@dataclass(frozen=True)
class AttachImageIdempotencyKeyHooks:
    image_idempotency_key: ImageIdempotencyKey


class _ResponsesImageBodyArgs(TypedDict):
    action: str
    prompt: str
    size: str
    images: list[bytes] | None
    quality: str
    output_format: str | None
    output_compression: int | None
    background: str | None
    moderation: str | None
    model: str | None
    image_urls: NotRequired[list[str] | None]
    retry_attempt: int
    policy: ImageRequestPolicy
    hooks: ResponsesImageBodyHooks


@dataclass(frozen=True)
class _ResponsesImageBodyRequest:
    action: str
    prompt: str
    size: str
    images: list[bytes] | None
    quality: str
    output_format: str | None
    output_compression: int | None
    background: str | None
    moderation: str | None
    model: str | None
    retry_attempt: int
    policy: ImageRequestPolicy
    hooks: ResponsesImageBodyHooks
    image_urls: list[str] | None = None


def _json_dumps_stable(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _image_file_fingerprints(
    files: ImageRequestFiles,
) -> list[dict[str, Any]]:
    if not files:
        return []
    result: list[dict[str, Any]] = []
    for field, file_tuple in files:
        try:
            filename, raw, content_type = file_tuple
        except Exception:  # noqa: BLE001
            continue
        raw_bytes = raw if isinstance(raw, bytes | bytearray) else bytes(raw)
        result.append(
            {
                "field": field,
                "filename": filename,
                "content_type": content_type,
                "size": len(raw_bytes),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            }
        )
    return result


def _image_idempotency_key(
    *,
    trace_id: str,
    endpoint: str,
    body: dict[str, Any] | None = None,
    files: ImageRequestFiles = None,
    hooks: ImageIdempotencyKeyHooks | None = None,
) -> str:
    if hooks is None:
        hooks = ImageIdempotencyKeyHooks(
            json_dumps_stable=_json_dumps_stable,
            image_file_fingerprints=_image_file_fingerprints,
        )
    seed = {
        "trace_id": trace_id,
        "endpoint": endpoint,
        "body": body or {},
        "files": hooks.image_file_fingerprints(files),
    }
    digest = hashlib.sha256(hooks.json_dumps_stable(seed).encode("utf-8")).hexdigest()
    return f"lumen-image2-{digest[:32]}"


def _attach_image_idempotency_key(
    headers: dict[str, str],
    *,
    trace_id: str,
    endpoint: str,
    body: dict[str, Any] | None = None,
    files: ImageRequestFiles = None,
    hooks: AttachImageIdempotencyKeyHooks | None = None,
) -> None:
    if hooks is None:
        hooks = AttachImageIdempotencyKeyHooks(
            image_idempotency_key=_image_idempotency_key,
        )
    headers.setdefault(
        "Idempotency-Key",
        hooks.image_idempotency_key(
            trace_id=trace_id,
            endpoint=endpoint,
            body=body,
            files=files,
        ),
    )


def _apply_retry_cache_busters(
    body: dict[str, Any],
    retry_attempt: int,
    prompt: str,
    size: str,
) -> None:
    if retry_attempt <= 1:
        return
    seed = hashlib.md5(
        f"{prompt[:200]}|{size}|{retry_attempt}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:16]
    body["prompt_cache_key"] = f"lumen-retry-{seed}"
    rotation = ("medium", "minimal", "high", "minimal")
    body["reasoning"] = {
        "effort": rotation[(retry_attempt - 1) % len(rotation)],
        "summary": "auto",
    }
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "image_generation":
            tool.pop("partial_images", None)


def _normalize_image_quality(
    value: str | None,
    *,
    policy: ImageRequestPolicy,
) -> str:
    if isinstance(value, str) and value in policy.image_qualities:
        return value
    return policy.default_image_quality


def _normalize_image_output_format(
    value: str | None,
    *,
    policy: ImageRequestPolicy,
) -> str:
    if isinstance(value, str) and value in policy.image_output_formats:
        return value
    return policy.default_image_output_format


def _normalize_image_output_compression(
    value: int | None,
    *,
    output_format: str,
    policy: ImageRequestPolicy,
) -> int | None:
    if output_format not in {"jpeg", "webp"}:
        return None
    if value is None:
        return policy.default_image_output_compression
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return policy.default_image_output_compression


def _normalize_image_background(
    value: str | None,
    *,
    policy: ImageRequestPolicy,
) -> str:
    if isinstance(value, str) and value in policy.image_backgrounds:
        return value
    return policy.default_image_background


def _normalize_image_moderation(
    value: str | None,
    *,
    policy: ImageRequestPolicy,
) -> str:
    if isinstance(value, str) and value in policy.image_moderations:
        return value
    return policy.default_image_moderation


def _add_image_output_options(
    body: dict[str, Any],
    *,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    hooks: ImageOutputOptionsHooks,
) -> None:
    bg = hooks.normalize_image_background(background)
    fmt = hooks.normalize_image_output_format(output_format)
    if bg == "transparent" and fmt == "jpeg":
        fmt = "png"
    body["output_format"] = fmt
    compression = hooks.normalize_image_output_compression(
        output_compression,
        output_format=fmt,
    )
    if compression is not None:
        body["output_compression"] = compression
    body["background"] = bg
    body["moderation"] = hooks.normalize_image_moderation(moderation)


def _stable_sort_tools(tools: list[Any]) -> list[Any]:
    if not isinstance(tools, list):
        return tools

    def _key(tool: Any) -> tuple[int, str]:
        if not isinstance(tool, dict):
            return (1, "")
        name = tool.get("name") or tool.get("type") or ""
        return (0 if name else 1, str(name))

    return sorted(tools, key=_key)


def _wrap_inpaint_prompt(user_intent: str) -> str:
    return (
        f"Inside the masked region, {user_intent.strip()}.\n"
        "Preserve everything outside the mask exactly: colors, geometry, lighting.\n"
        "Do not add anything outside the masked area.\n"
        "Blend the result seamlessly with the surrounding unchanged area."
    )


def _image_job_body_base(
    *,
    prompt: str,
    size: str,
    n: int,
    quality: str,
    output_format: str | None,
    output_compression: int | None,
    background: str | None,
    moderation: str | None,
    policy: ImageRequestPolicy,
    hooks: ImageJobBodyHooks,
) -> dict[str, Any]:
    assert policy.upstream_model, "model must be set"
    body: dict[str, Any] = {
        "model": policy.upstream_model,
        "prompt": prompt,
        "size": size,
        "quality": hooks.normalize_image_quality(quality),
        "n": n,
    }
    hooks.add_image_output_options(
        body,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
    )
    return body


def _image_job_payload(
    *,
    request_type: str,
    endpoint: str,
    body: dict[str, Any],
    image_edit_input_transport: str | None = None,
    policy: ImageRequestPolicy,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "request_type": request_type,
        "endpoint": endpoint,
        "body": body,
        "retention_days": policy.image_job_retention_days,
    }
    if image_edit_input_transport is not None:
        payload["image_edit_input_transport"] = image_edit_input_transport
    return payload


def _build_responses_image_body(
    **kwargs: Unpack[_ResponsesImageBodyArgs],
) -> dict[str, Any]:
    request = _ResponsesImageBodyRequest(**kwargs)
    policy = request.policy
    hooks = request.hooks
    assert policy.upstream_model, "model must be set"
    image_model = request.model or policy.default_responses_model
    assert image_model, "model must be set"
    image_quality = hooks.normalize_image_quality(request.quality)
    tool: dict[str, Any] = {
        "type": "image_generation",
        "model": policy.upstream_model,
        "action": request.action,
        "size": request.size,
        "quality": image_quality,
    }
    hooks.add_image_output_options(
        tool,
        output_format=request.output_format,
        output_compression=request.output_compression,
        background=request.background,
        moderation=request.moderation,
    )
    pixels = hooks.parse_size_pixels(request.size)
    if (
        image_quality != "low"
        and pixels is not None
        and pixels <= policy.partial_images_max_pixels
    ):
        tool["partial_images"] = 3

    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": request.prompt}
    ]
    if request.action == "edit":
        if request.image_urls:
            for url in request.image_urls:
                if isinstance(url, str) and url:
                    content.append({"type": "input_image", "image_url": url})
        else:
            for raw in request.images or []:
                ref_bytes, mime = hooks.normalize_reference_image(raw)
                image_b64 = base64.b64encode(ref_bytes).decode("ascii")
                content.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{mime};base64,{image_b64}",
                    }
                )

    input_payload: list[dict[str, Any]] = [
        {"type": "message", "role": "user", "content": content}
    ]
    body: dict[str, Any] = {
        "model": image_model,
        "instructions": policy.default_image_instructions,
        "input": input_payload,
        "tools": hooks.stable_sort_tools([tool]),
        "tool_choice": {"type": "image_generation"},
        "parallel_tool_calls": True,
        "include": ["reasoning.encrypted_content"],
        "stream": True,
        "store": False,
        "reasoning": {"effort": "medium", "summary": "auto"},
    }
    hooks.apply_retry_cache_busters(
        body,
        request.retry_attempt,
        request.prompt,
        request.size,
    )
    hooks.validate_responses_body(body)
    return body


def _parse_size_pixels(size: str) -> int | None:
    if not isinstance(size, str) or "x" not in size:
        return None
    width_text, _, height_text = size.partition("x")
    try:
        width, height = int(width_text), int(height_text)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width * height
