"""Completion image-tool decoding, persistence, billing, and publishing."""

from __future__ import annotations

import binascii
import hashlib
import io
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from PIL import Image as PILImage

from lumen_core.constants import ImageSource
from lumen_core.context_window import count_tokens

from ... import billing as worker_billing
from ... import completion_billing, image_artifacts
from ...upstream import UpstreamError
from .tool_state import _IMAGE_GENERATION_TOOL_TYPE


_decode_upstream_image_b64 = image_artifacts._decode_upstream_image_b64


def _tool_image_dedupe_key(event: dict[str, Any], b64_image: str) -> str:
    item = event.get("item")
    if isinstance(item, dict):
        raw_id = item.get("id")
        if isinstance(raw_id, str) and raw_id:
            return f"id:{raw_id}"
    raw = b64_image.strip()
    if raw[:5].lower() == "data:" and "," in raw:
        raw = raw.split(",", 1)[1]
    if len(raw) <= 8192:
        normalized_b64 = "".join(raw.split())
        digest = hashlib.sha1(
            normalized_b64.encode("ascii", errors="ignore"),
            usedforsecurity=False,
        ).hexdigest()
        return f"b64sha1:{digest}"
    sample = f"{len(raw)}:{raw[:512]}:{raw[-512:]}"
    digest = hashlib.sha1(
        sample.encode("ascii", errors="ignore"),
        usedforsecurity=False,
    ).hexdigest()
    return f"b64sig:{len(raw)}:{digest}"


def _extract_image_events_from_response(
    response: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    output = response.get("output")
    if not isinstance(output, list):
        return []
    events: list[dict[str, Any]] = []
    for item in output:
        if (
            isinstance(item, dict)
            and item.get("type") == _IMAGE_GENERATION_TOOL_TYPE + "_call"
        ):
            events.append({"type": "response.output_item.done", "item": item})
    return events


def _compute_blurhash(
    img: PILImage.Image,
    *,
    compute_blurhash: Callable[[PILImage.Image], str | None],
) -> str | None:
    width, height = img.size
    if width < 4 or height < 4:
        return None
    return compute_blurhash(img)


@dataclass(frozen=True)
class ToolImageFormatHooks:
    compute_blurhash: Callable[[PILImage.Image], str | None]
    make_display: Callable[[PILImage.Image], tuple[bytes, tuple[int, int]]]
    make_preview: Callable[[PILImage.Image], tuple[bytes, tuple[int, int]]]
    make_thumb: Callable[[PILImage.Image], tuple[bytes, tuple[int, int]]]
    upstream_error_type: type[UpstreamError]
    bad_response_error_code: str


def _image_format_and_meta(
    raw_image: bytes,
    *,
    hooks: ToolImageFormatHooks,
) -> tuple[
    str,
    str,
    int,
    int,
    str | None,
    bytes,
    tuple[int, int],
    bytes,
    tuple[int, int],
    bytes,
    tuple[int, int],
]:
    try:
        with PILImage.open(io.BytesIO(raw_image)) as pil:
            pil.load()
            if pil.format not in ("PNG", "WEBP", "JPEG"):
                raise hooks.upstream_error_type(
                    f"upstream returned unexpected image format: {pil.format}",
                    error_code=hooks.bad_response_error_code,
                    status_code=200,
                )
            width, height = pil.size
            if width < 1 or height < 1 or width > 10000 or height > 10000:
                raise hooks.upstream_error_type(
                    f"upstream image dimensions out of range: {width}x{height}",
                    error_code=hooks.bad_response_error_code,
                    status_code=200,
                )
            blurhash_str = hooks.compute_blurhash(pil)
            display_bytes, display_size = hooks.make_display(pil)
            preview_bytes, preview_size = hooks.make_preview(pil)
            thumb_bytes, thumb_size = hooks.make_thumb(pil)
            orig_format = str(pil.format)
    except hooks.upstream_error_type:
        raise
    except Exception as exc:  # noqa: BLE001
        raise hooks.upstream_error_type(
            f"pillow could not decode image: {exc}",
            error_code=hooks.bad_response_error_code,
            status_code=200,
        ) from exc

    ext_by_format = {"PNG": "png", "WEBP": "webp", "JPEG": "jpg"}
    mime_by_format = {
        "PNG": "image/png",
        "WEBP": "image/webp",
        "JPEG": "image/jpeg",
    }
    return (
        ext_by_format[orig_format],
        mime_by_format[orig_format],
        width,
        height,
        blurhash_str,
        display_bytes,
        display_size,
        preview_bytes,
        preview_size,
        thumb_bytes,
        thumb_size,
    )


@dataclass(frozen=True)
class ToolImageUsageHooks:
    acquire_lock: Callable[[Any, str], Awaitable[None]]
    completion_model: Any
    running_statuses: tuple[str, ...]
    superseded_error_type: type[Exception]
    fallback_image_tokens: Callable[..., Awaitable[int]]


async def _record_completion_tool_image_usage(
    *,
    session: Any,
    task_id: str,
    attempt_epoch: int,
    budget_micro: int,
    hooks: ToolImageUsageHooks,
) -> None:
    await hooks.acquire_lock(session, task_id)
    completion = await session.get(hooks.completion_model, task_id)
    if (
        completion is None
        or completion.attempt != attempt_epoch
        or completion.status not in hooks.running_statuses
    ):
        raise hooks.superseded_error_type(
            f"completion tool image superseded task={task_id} "
            f"attempt_epoch={attempt_epoch}"
        )
    upstream_request = dict(completion.upstream_request or {})
    try:
        persisted_budget_micro = max(
            0,
            int(upstream_request.get("tool_image_reserved_micro") or 0),
        )
    except (TypeError, ValueError):
        persisted_budget_micro = 0
    total_budget_micro = persisted_budget_micro + max(0, int(budget_micro or 0))
    upstream_request["tool_image_reserved_micro"] = total_budget_micro
    completion.upstream_request = upstream_request
    image_tokens = await hooks.fallback_image_tokens(
        session,
        completion,
        budget_micro=total_budget_micro,
    )
    if image_tokens <= 0:
        return
    completion.image_output_tokens = image_tokens
    completion.tokens_out = max(
        int(getattr(completion, "tokens_out", 0) or 0),
        image_tokens,
    )


@dataclass(frozen=True)
class ToolImageStorageHooks:
    image_format_and_meta: Callable[..., tuple[Any, ...]]
    new_uuid7: Callable[[], str]
    sha256: Callable[[bytes], str]
    write_generation_files: Callable[[list[tuple[str, bytes]]], Awaitable[list[str]]]
    cleanup_storage_on_error: Callable[[list[str]], Any]
    record_image_usage: Callable[..., Awaitable[None]]
    image_model: Any
    image_variant_model: Any
    message_model: Any
    storage_public_url: Callable[[str], str]


async def _store_completion_tool_image(
    *,
    session: Any,
    task_id: str,
    attempt_epoch: int,
    user_id: str,
    message_id: str,
    raw_image: bytes,
    revised_prompt: str | None,
    billing_budget_micro: int,
    hooks: ToolImageStorageHooks,
) -> dict[str, Any]:
    (
        orig_ext,
        orig_mime,
        width,
        height,
        blurhash_str,
        display_bytes,
        display_size,
        preview_bytes,
        preview_size,
        thumb_bytes,
        thumb_size,
    ) = hooks.image_format_and_meta(raw_image)
    image_id = hooks.new_uuid7()
    sha = hooks.sha256(raw_image)
    key_prefix = f"u/{user_id}/completion-tools/{task_id}/{image_id}"
    key_orig = f"{key_prefix}/orig.{orig_ext}"
    key_display = f"{key_prefix}/display2048.webp"
    key_preview = f"{key_prefix}/preview1024.webp"
    key_thumb = f"{key_prefix}/thumb256.jpg"

    created_storage_keys = await hooks.write_generation_files(
        [
            (key_orig, raw_image),
            (key_display, display_bytes),
            (key_preview, preview_bytes),
            (key_thumb, thumb_bytes),
        ]
    )
    async with hooks.cleanup_storage_on_error(created_storage_keys):
        image = hooks.image_model(
            id=image_id,
            user_id=user_id,
            owner_generation_id=None,
            source=ImageSource.GENERATED,
            parent_image_id=None,
            storage_key=key_orig,
            mime=orig_mime,
            width=width,
            height=height,
            size_bytes=len(raw_image),
            sha256=sha,
            blurhash=blurhash_str,
            visibility="private",
            metadata_jsonb={
                "source": "completion_tool",
                "completion_id": task_id,
                **({"revised_prompt": revised_prompt} if revised_prompt else {}),
            },
        )
        session.add(image)
        session.add(
            hooks.image_variant_model(
                image_id=image_id,
                kind="display2048",
                storage_key=key_display,
                width=display_size[0],
                height=display_size[1],
            )
        )
        session.add(
            hooks.image_variant_model(
                image_id=image_id,
                kind="preview1024",
                storage_key=key_preview,
                width=preview_size[0],
                height=preview_size[1],
            )
        )
        session.add(
            hooks.image_variant_model(
                image_id=image_id,
                kind="thumb256",
                storage_key=key_thumb,
                width=thumb_size[0],
                height=thumb_size[1],
            )
        )

        message = await session.get(hooks.message_model, message_id)
        if message is not None:
            content = dict(message.content or {})
            images_list = list(content.get("images") or [])
            images_list.append(
                {
                    "image_id": image_id,
                    "from_completion_id": task_id,
                    "width": width,
                    "height": height,
                    "mime": orig_mime,
                    "url": hooks.storage_public_url(key_orig),
                    "display_url": f"/api/images/{image_id}/variants/display2048",
                    "preview_url": f"/api/images/{image_id}/variants/preview1024",
                    "thumb_url": f"/api/images/{image_id}/variants/thumb256",
                    **({"revised_prompt": revised_prompt} if revised_prompt else {}),
                }
            )
            content["images"] = images_list
            message.content = content

        await hooks.record_image_usage(
            session=session,
            task_id=task_id,
            attempt_epoch=attempt_epoch,
            budget_micro=billing_budget_micro,
        )
        image_payload = {
            "image_id": image_id,
            "from_completion_id": task_id,
            "actual_size": f"{width}x{height}",
            "mime": orig_mime,
            "url": hooks.storage_public_url(key_orig),
            "display_url": f"/api/images/{image_id}/variants/display2048",
            "preview_url": f"/api/images/{image_id}/variants/preview1024",
            "thumb_url": f"/api/images/{image_id}/variants/thumb256",
            **({"revised_prompt": revised_prompt} if revised_prompt else {}),
        }
        # Keep the DB commit inside the storage cleanup scope. A failed or
        # cancelled commit must remove only the objects created by this attempt.
        await session.commit()
        return image_payload


def _fallback_completion_usage_tokens(
    input_list: list[dict[str, Any]],
    output_text: str,
    *,
    tokens_in: int,
    tokens_out: int,
) -> tuple[int, int]:
    next_in = tokens_in
    next_out = tokens_out
    if next_out <= 0 and output_text:
        next_out = max(1, count_tokens(output_text))
    if next_in <= 0 and input_list:
        try:
            next_in = max(1, count_tokens(json.dumps(input_list, ensure_ascii=False)))
        except Exception:  # noqa: BLE001
            next_in = 1
    return next_in, next_out


async def _settle_cancelled_completion_billing(
    session: Any,
    completion: Any,
    *,
    has_partial: bool,
    input_list: list[dict[str, Any]] | None,
    accumulated_text: str,
    tokens_in: int,
    tokens_out: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    cache_creation_5m_tokens: int,
    cache_creation_1h_tokens: int,
    reasoning_tokens: int,
    image_output_tokens: int,
    tool_images: list[dict[str, Any]],
    reserved_tool_image_budget_micro: int,
    reason: str,
) -> None:
    def persisted_tokens(name: str) -> int:
        try:
            return max(0, int(getattr(completion, name, 0) or 0))
        except (TypeError, ValueError):
            return 0

    tokens_in = max(tokens_in, persisted_tokens("tokens_in"))
    tokens_out = max(tokens_out, persisted_tokens("tokens_out"))
    cache_read_tokens = max(
        cache_read_tokens,
        persisted_tokens("cache_read_tokens"),
    )
    cache_creation_tokens = max(
        cache_creation_tokens,
        persisted_tokens("cache_creation_tokens"),
    )
    cache_creation_5m_tokens = max(
        cache_creation_5m_tokens,
        persisted_tokens("cache_creation_5m_tokens"),
    )
    cache_creation_1h_tokens = max(
        cache_creation_1h_tokens,
        persisted_tokens("cache_creation_1h_tokens"),
    )
    reasoning_tokens = max(
        reasoning_tokens,
        persisted_tokens("reasoning_tokens"),
    )
    image_output_tokens = max(
        image_output_tokens,
        persisted_tokens("image_output_tokens"),
    )
    usage_values = (
        tokens_in,
        tokens_out,
        cache_read_tokens,
        cache_creation_tokens,
        cache_creation_5m_tokens,
        cache_creation_1h_tokens,
        reasoning_tokens,
        image_output_tokens,
    )
    if not has_partial and not any(value > 0 for value in usage_values):
        await worker_billing.release_completion(
            session,
            completion,
            reason=reason,
        )
        return

    if input_list is not None:
        tokens_in, tokens_out = _fallback_completion_usage_tokens(
            input_list,
            accumulated_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
    elif accumulated_text and tokens_out <= 0:
        tokens_out = max(1, count_tokens(accumulated_text))
    if (
        tool_images
        and image_output_tokens <= 0
        and reserved_tool_image_budget_micro > 0
    ):
        image_output_tokens = (
            await completion_billing.fallback_completion_tool_image_tokens(
                session,
                completion,
                budget_micro=reserved_tool_image_budget_micro,
            )
        )
        tokens_out = max(tokens_out, image_output_tokens)

    completion.tokens_in = tokens_in
    completion.tokens_out = tokens_out
    completion.cache_read_tokens = cache_read_tokens
    completion.cache_creation_tokens = cache_creation_tokens
    completion.cache_creation_5m_tokens = cache_creation_5m_tokens
    completion.cache_creation_1h_tokens = cache_creation_1h_tokens
    completion.reasoning_tokens = reasoning_tokens
    completion.image_output_tokens = image_output_tokens
    if any(
        value > 0
        for value in (
            tokens_in,
            tokens_out,
            cache_read_tokens,
            cache_creation_tokens,
            cache_creation_5m_tokens,
            cache_creation_1h_tokens,
            reasoning_tokens,
            image_output_tokens,
        )
    ):
        await worker_billing.charge_completion(session, completion)
        return
    await worker_billing.release_completion(
        session,
        completion,
        reason=reason,
    )


@dataclass(frozen=True)
class ToolImageBudgetHooks:
    runtime_settings: Any
    session_factory: Callable[[], Any]
    completion_model: Any
    worker_billing: Any
    billing_core: Any
    insufficient_balance_error_type: type[UpstreamError]
    budget_setting: str


async def _ensure_completion_tool_image_wallet_budget(
    *,
    user_id: str,
    task_id: str,
    reserved_micro: int = 0,
    hooks: ToolImageBudgetHooks,
) -> int:
    base_budget_micro = await hooks.runtime_settings.resolve_int(
        hooks.budget_setting,
        0,
    )
    if base_budget_micro <= 0:
        return 0
    async with hooks.session_factory() as session:
        completion = await session.get(hooks.completion_model, task_id)
        billing_ref_id = (
            hooks.worker_billing.completion_billing_ref_id(completion)
            if completion is not None
            else task_id
        )
        if not await hooks.worker_billing._wallet_billing_applies(  # noqa: SLF001
            session,
            user_id=user_id,
            ref_type="completion",
            ref_id=billing_ref_id,
        ):
            return 0
        if not await hooks.worker_billing.billing_enabled():
            return 0
        snapshot_multiplier = (
            hooks.worker_billing._snapshot_rate_multiplier_x10000(  # noqa: SLF001
                completion
            )
            if completion is not None
            else None
        )
        rate_multiplier = (
            snapshot_multiplier
            if snapshot_multiplier is not None
            else await hooks.worker_billing._rate_multiplier_x10000(  # noqa: SLF001
                session,
                user_id,
            )
        )
        budget_micro = hooks.worker_billing._apply_rate_multiplier_micro(  # noqa: SLF001
            base_budget_micro,
            rate_multiplier,
        )
        if budget_micro <= 0:
            return 0
        already_reserved_micro = max(0, int(reserved_micro or 0))
        required_micro = already_reserved_micro + int(budget_micro)
        wallet = await hooks.billing_core.get_wallet(
            session,
            user_id,
            lock=True,
            create=False,
        )
        balance_micro = int(getattr(wallet, "balance_micro", 0) or 0) if wallet else 0
        held_micro = await hooks.worker_billing.held_amount_for_ref(
            session,
            user_id,
            "completion",
            billing_ref_id,
        )
        available_micro = balance_micro + int(held_micro or 0)
        if (
            available_micro >= required_micro
            or await hooks.worker_billing.allow_negative_balance()
        ):
            return int(budget_micro)
        raise hooks.insufficient_balance_error_type(
            "insufficient wallet balance for image_generation tool",
            error_code="INSUFFICIENT_BALANCE",
            status_code=402,
            payload={
                "required_micro": int(budget_micro),
                "cumulative_required_micro": int(required_micro),
                "balance_micro": balance_micro,
                "held_micro": int(held_micro or 0),
                "reserved_micro": already_reserved_micro,
                "rate_multiplier_x10000": rate_multiplier,
                "completion_id": task_id,
            },
        )


@dataclass(frozen=True)
class ToolImagePublishHooks:
    ensure_wallet_budget: Callable[..., Awaitable[int]]
    decode_upstream_image_b64: Callable[[str], bytes]
    session_factory: Callable[[], Any]
    store_tool_image: Callable[..., Awaitable[dict[str, Any]]]
    publish_event: Callable[..., Awaitable[None]]
    upstream_error_type: type[UpstreamError]
    bad_response_error_code: str
    image_event: str


async def _store_and_publish_completion_tool_image(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    task_id: str,
    message_id: str,
    attempt: int,
    attempt_epoch: int,
    b64_image: str,
    revised_prompt: str | None,
    reserved_tool_image_micro: int = 0,
    hooks: ToolImagePublishHooks,
) -> tuple[dict[str, Any] | None, int]:
    budget_reserved_micro = await hooks.ensure_wallet_budget(
        user_id=user_id,
        task_id=task_id,
        reserved_micro=reserved_tool_image_micro,
    )
    try:
        raw_image = hooks.decode_upstream_image_b64(b64_image)
    except binascii.Error as exc:
        raise hooks.upstream_error_type(
            f"bad base64 from image_generation tool: {exc}",
            error_code=hooks.bad_response_error_code,
            status_code=200,
        ) from exc
    async with hooks.session_factory() as session:
        image_payload = await hooks.store_tool_image(
            session=session,
            task_id=task_id,
            attempt_epoch=attempt_epoch,
            user_id=user_id,
            message_id=message_id,
            raw_image=raw_image,
            revised_prompt=revised_prompt,
            billing_budget_micro=budget_reserved_micro,
        )

    await hooks.publish_event(
        redis,
        user_id,
        channel,
        hooks.image_event,
        {
            "completion_id": task_id,
            "message_id": message_id,
            "attempt": attempt,
            "attempt_epoch": attempt_epoch,
            "images": [image_payload],
        },
    )
    return image_payload, budget_reserved_micro
