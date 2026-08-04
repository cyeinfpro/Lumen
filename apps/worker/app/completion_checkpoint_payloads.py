"""Validation and normalization for durable completion image payloads."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image as PILImage
from PIL import UnidentifiedImageError

from .completion_checkpoint_schema import COMPLETION_CHECKPOINT_IMAGES_KEY
from .tasks.completion_parts.artifact_codec import (
    decode_upstream_image_b64,
    sha256,
)
from .tasks.completion_parts.image_storage_runtime import (
    COMPLETION_IMAGE_EVENT_OUTBOX_ID_KEY,
    COMPLETION_IMAGE_EVENT_PUBLISHED_KEY,
)

COMPLETION_CHECKPOINT_IMAGE_PENDING = "pending"
COMPLETION_CHECKPOINT_IMAGE_COMMITTED = "committed"
COMPLETION_CHECKPOINT_IMAGE_QUARANTINED = "quarantined"
COMPLETION_CHECKPOINT_IMAGE_QUARANTINE_REASON_KEY = "quarantine_reason"
COMPLETION_CHECKPOINT_IMAGE_EVENT_OUTBOX_ID_KEY = "event_outbox_id"
COMPLETION_CHECKPOINT_IMAGE_EVENT_PUBLISHED_KEY = "event_published"


class CompletionCheckpointCorrupt(ValueError):
    """Raised when a durable completion checkpoint cannot be trusted."""


def _validated_image_bytes(image_b64: str) -> tuple[int, str]:
    try:
        raw_image = decode_upstream_image_b64(image_b64)
    except (TypeError, ValueError) as exc:
        raise CompletionCheckpointCorrupt(
            "pending image contains invalid base64"
        ) from exc
    if not raw_image:
        raise CompletionCheckpointCorrupt("pending image payload is empty")
    try:
        with PILImage.open(io.BytesIO(raw_image)) as image:
            image.verify()
        with PILImage.open(io.BytesIO(raw_image)) as image:
            image.load()
            if image.width <= 0 or image.height <= 0:
                raise CompletionCheckpointCorrupt(
                    "pending image has invalid dimensions"
                )
    except CompletionCheckpointCorrupt:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise CompletionCheckpointCorrupt(
            "pending image payload is not a valid image"
        ) from exc
    return len(raw_image), sha256(raw_image)


def _normalized_pending_image(raw_image: dict[str, Any]) -> dict[str, Any]:
    image_b64 = raw_image.get("image_b64")
    dedupe_key = raw_image.get("dedupe_key")
    if not isinstance(image_b64, str) or not image_b64.strip():
        raise CompletionCheckpointCorrupt("pending image base64 is missing")
    if not isinstance(dedupe_key, str) or not dedupe_key.strip():
        raise CompletionCheckpointCorrupt("pending image dedupe key is missing")
    actual_size, actual_sha = _validated_image_bytes(image_b64)
    stored_size = raw_image.get("size_bytes")
    if stored_size is not None:
        if isinstance(stored_size, bool):
            raise CompletionCheckpointCorrupt("pending image size is invalid")
        try:
            normalized_size = int(stored_size)
        except (TypeError, ValueError) as exc:
            raise CompletionCheckpointCorrupt(
                "pending image size is invalid"
            ) from exc
        if normalized_size != actual_size:
            raise CompletionCheckpointCorrupt("pending image size does not match")
    stored_sha = raw_image.get("sha256")
    if stored_sha is not None:
        if (
            not isinstance(stored_sha, str)
            or len(stored_sha) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in stored_sha)
        ):
            raise CompletionCheckpointCorrupt("pending image hash is invalid")
        if stored_sha.lower() != actual_sha:
            raise CompletionCheckpointCorrupt("pending image hash does not match")
    return {
        **raw_image,
        "image_b64": image_b64,
        "dedupe_key": dedupe_key,
        "size_bytes": actual_size,
        "sha256": actual_sha,
    }


def _normalized_committed_image(raw_image: dict[str, Any]) -> dict[str, Any]:
    image_id = raw_image.get("image_id")
    payload = raw_image.get("payload")
    if not isinstance(image_id, str) or not image_id.strip():
        raise CompletionCheckpointCorrupt("checkpoint image id is missing")
    if not isinstance(payload, dict) or payload.get("image_id") != image_id:
        raise CompletionCheckpointCorrupt(
            "committed image payload identity is invalid"
        )
    event_id = raw_image.get(COMPLETION_CHECKPOINT_IMAGE_EVENT_OUTBOX_ID_KEY)
    if event_id is not None and (
        not isinstance(event_id, str) or not event_id.strip()
    ):
        raise CompletionCheckpointCorrupt(
            "committed image event outbox id is invalid"
        )
    if (
        event_id is not None
        and not isinstance(
            raw_image.get(COMPLETION_CHECKPOINT_IMAGE_EVENT_PUBLISHED_KEY),
            bool,
        )
    ):
        raise CompletionCheckpointCorrupt(
            "committed image event state is invalid"
        )
    return dict(raw_image)


def _normalized_quarantined_image(raw_image: dict[str, Any]) -> dict[str, Any]:
    image_id = raw_image.get("image_id")
    dedupe_key = raw_image.get("dedupe_key")
    reason = raw_image.get(COMPLETION_CHECKPOINT_IMAGE_QUARANTINE_REASON_KEY)
    if not isinstance(image_id, str) or not image_id.strip():
        raise CompletionCheckpointCorrupt("quarantined image id is missing")
    if not isinstance(dedupe_key, str) or not dedupe_key.strip():
        raise CompletionCheckpointCorrupt(
            "quarantined image dedupe key is missing"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise CompletionCheckpointCorrupt(
            "quarantined image reason is missing"
        )
    normalized = {
        "image_id": image_id,
        "dedupe_key": dedupe_key,
        "state": COMPLETION_CHECKPOINT_IMAGE_QUARANTINED,
        COMPLETION_CHECKPOINT_IMAGE_QUARANTINE_REASON_KEY: reason[:500],
    }
    source_index = raw_image.get("source_index")
    if isinstance(source_index, int) and not isinstance(source_index, bool):
        normalized["source_index"] = max(0, source_index)
    original_image_id = raw_image.get("original_image_id")
    if isinstance(original_image_id, str) and original_image_id:
        normalized["original_image_id"] = original_image_id
    budget_micro = raw_image.get("budget_micro")
    if isinstance(budget_micro, int) and not isinstance(budget_micro, bool):
        if budget_micro > 0:
            normalized["budget_micro"] = budget_micro
    return normalized


def _quarantine_image_id(
    raw_image: Any,
    *,
    source_index: int,
    seen_ids: set[str],
) -> tuple[str, str | None]:
    original = raw_image.get("image_id") if isinstance(raw_image, dict) else None
    base = (
        original.strip()
        if isinstance(original, str) and original.strip()
        else f"corrupt-checkpoint-image-{source_index + 1}"
    )
    image_id = base
    suffix = 1
    while image_id in seen_ids:
        image_id = f"{base}:quarantined:{source_index + 1}:{suffix}"
        suffix += 1
    return image_id, original if isinstance(original, str) and original else None


def quarantined_checkpoint_image(
    raw_image: Any,
    *,
    source_index: int,
    reason: str,
    seen_ids: set[str] | None = None,
) -> dict[str, Any]:
    used_ids = seen_ids if seen_ids is not None else set()
    image_id, original_image_id = _quarantine_image_id(
        raw_image,
        source_index=source_index,
        seen_ids=used_ids,
    )
    raw_dedupe = (
        raw_image.get("dedupe_key") if isinstance(raw_image, dict) else None
    )
    dedupe_key = (
        raw_dedupe.strip()
        if isinstance(raw_dedupe, str) and raw_dedupe.strip()
        else f"quarantined:{source_index}:{image_id}"
    )
    quarantined = {
        "image_id": image_id,
        "dedupe_key": dedupe_key,
        "state": COMPLETION_CHECKPOINT_IMAGE_QUARANTINED,
        COMPLETION_CHECKPOINT_IMAGE_QUARANTINE_REASON_KEY: reason[:500],
        "source_index": source_index,
    }
    if original_image_id is not None and original_image_id != image_id:
        quarantined["original_image_id"] = original_image_id
    if isinstance(raw_image, dict):
        budget_micro = raw_image.get("budget_micro")
        if isinstance(budget_micro, int) and not isinstance(budget_micro, bool):
            if budget_micro > 0:
                quarantined["budget_micro"] = budget_micro
    return quarantined


def _normalized_checkpoint_image(raw_image: Any) -> dict[str, Any]:
    if not isinstance(raw_image, dict):
        raise CompletionCheckpointCorrupt("checkpoint image record is invalid")
    state = raw_image.get("state")
    if state == COMPLETION_CHECKPOINT_IMAGE_PENDING:
        image_id = raw_image.get("image_id")
        if not isinstance(image_id, str) or not image_id.strip():
            raise CompletionCheckpointCorrupt("checkpoint image id is missing")
        return _normalized_pending_image(raw_image)
    if state == COMPLETION_CHECKPOINT_IMAGE_COMMITTED:
        return _normalized_committed_image(raw_image)
    if state == COMPLETION_CHECKPOINT_IMAGE_QUARANTINED:
        return _normalized_quarantined_image(raw_image)
    raise CompletionCheckpointCorrupt("checkpoint image state is invalid")


def validated_checkpoint_images(request: dict[str, Any]) -> list[dict[str, Any]]:
    raw_images = request.get(COMPLETION_CHECKPOINT_IMAGES_KEY, [])
    if not isinstance(raw_images, list):
        raise CompletionCheckpointCorrupt("checkpoint images must be a list")
    images: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for source_index, raw_image in enumerate(raw_images):
        try:
            image = _normalized_checkpoint_image(raw_image)
            if image["image_id"] in seen_ids:
                raise CompletionCheckpointCorrupt(
                    "checkpoint image id is duplicated"
                )
        except CompletionCheckpointCorrupt as exc:
            image = quarantined_checkpoint_image(
                raw_image,
                source_index=source_index,
                reason=str(exc),
                seen_ids=seen_ids,
            )
        seen_ids.add(str(image["image_id"]))
        images.append(image)
    return images


def checkpoint_images_validation_error(request: dict[str, Any]) -> str | None:
    try:
        validated_checkpoint_images(request)
    except CompletionCheckpointCorrupt as exc:
        return str(exc)
    return None


def checkpoint_image_quarantine_count(request: dict[str, Any]) -> int:
    return sum(
        image["state"] == COMPLETION_CHECKPOINT_IMAGE_QUARANTINED
        for image in validated_checkpoint_images(request)
    )


def normalized_pending_checkpoint_image(
    *,
    image_id: str,
    dedupe_key: str,
    image_b64: str,
    revised_prompt: str | None = None,
) -> dict[str, Any]:
    return _normalized_pending_image(
        {
            "image_id": image_id,
            "dedupe_key": dedupe_key,
            "state": COMPLETION_CHECKPOINT_IMAGE_PENDING,
            "image_b64": image_b64,
            **(
                {"revised_prompt": revised_prompt}
                if isinstance(revised_prompt, str) and revised_prompt
                else {}
            ),
        }
    )


def _public_image_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key
        not in {
            COMPLETION_IMAGE_EVENT_OUTBOX_ID_KEY,
            COMPLETION_IMAGE_EVENT_PUBLISHED_KEY,
        }
    }


def build_completed_checkpoint_images(
    state: Any,
    image_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    seen_image_ids: set[str] = set()
    for payload in state.streaming.tool_images:
        image_id = payload.get("image_id") if isinstance(payload, dict) else None
        if not isinstance(image_id, str) or not image_id or image_id in seen_image_ids:
            continue
        seen_image_ids.add(image_id)
        record = {
            "image_id": image_id,
            "dedupe_key": f"persisted:{image_id}",
            "state": COMPLETION_CHECKPOINT_IMAGE_COMMITTED,
            "payload": _public_image_payload(payload),
        }
        event_id = payload.get(COMPLETION_IMAGE_EVENT_OUTBOX_ID_KEY)
        if isinstance(event_id, str) and event_id:
            record[COMPLETION_CHECKPOINT_IMAGE_EVENT_OUTBOX_ID_KEY] = event_id
            record[COMPLETION_CHECKPOINT_IMAGE_EVENT_PUBLISHED_KEY] = bool(
                payload.get(COMPLETION_IMAGE_EVENT_PUBLISHED_KEY)
            )
        images.append(record)

    seen_dedupe = set(state.streaming.stored_image_call_ids)
    for source_index, image_event in enumerate(image_events):
        image_b64 = state.ports.tools._extract_response_image_b64(image_event)
        if not image_b64:
            continue
        dedupe_key = state.ports.tools._tool_image_dedupe_key(
            image_event,
            image_b64,
        )
        if dedupe_key in seen_dedupe:
            continue
        seen_dedupe.add(dedupe_key)
        revised_prompt = state.ports.tools._extract_response_revised_prompt(
            image_event
        )
        image_id = state.ports.persistence.new_uuid7()
        try:
            image = normalized_pending_checkpoint_image(
                image_id=image_id,
                dedupe_key=dedupe_key,
                image_b64=image_b64,
                revised_prompt=revised_prompt,
            )
        except CompletionCheckpointCorrupt as exc:
            image = quarantined_checkpoint_image(
                {
                    "image_id": image_id,
                    "dedupe_key": dedupe_key,
                    "state": COMPLETION_CHECKPOINT_IMAGE_PENDING,
                    "image_b64": image_b64,
                },
                source_index=source_index,
                reason=str(exc),
            )
        images.append(image)
    return images


def mark_checkpoint_image_committed(
    images: list[dict[str, Any]],
    *,
    image_id: str,
    payload: dict[str, Any],
    budget_micro: int,
) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    matched = False
    for image in images:
        if image.get("image_id") != image_id:
            updated.append(dict(image))
            continue
        matched = True
        committed = {
            "image_id": image_id,
            "dedupe_key": image.get("dedupe_key"),
            "state": COMPLETION_CHECKPOINT_IMAGE_COMMITTED,
            "payload": _public_image_payload(payload),
        }
        event_id = payload.get(COMPLETION_IMAGE_EVENT_OUTBOX_ID_KEY)
        if isinstance(event_id, str) and event_id:
            committed[COMPLETION_CHECKPOINT_IMAGE_EVENT_OUTBOX_ID_KEY] = event_id
            committed[COMPLETION_CHECKPOINT_IMAGE_EVENT_PUBLISHED_KEY] = bool(
                payload.get(COMPLETION_IMAGE_EVENT_PUBLISHED_KEY)
            )
        if budget_micro > 0:
            committed["budget_micro"] = int(budget_micro)
        updated.append(committed)
    if not matched:
        raise ValueError(f"completion checkpoint image not found: {image_id}")
    return updated


def mark_checkpoint_image_quarantined(
    images: list[dict[str, Any]],
    *,
    image_id: str,
    reason: str,
) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    matched = False
    for source_index, image in enumerate(images):
        if image.get("image_id") != image_id:
            updated.append(dict(image))
            continue
        matched = True
        updated.append(
            quarantined_checkpoint_image(
                image,
                source_index=source_index,
                reason=reason,
            )
        )
    if not matched:
        raise ValueError(f"completion checkpoint image not found: {image_id}")
    return updated


def mark_checkpoint_image_event_published(
    images: list[dict[str, Any]],
    *,
    image_id: str,
) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    matched = False
    for image in images:
        record = dict(image)
        if image.get("image_id") == image_id:
            matched = True
            record[COMPLETION_CHECKPOINT_IMAGE_EVENT_PUBLISHED_KEY] = True
        updated.append(record)
    if not matched:
        raise ValueError(f"completion checkpoint image not found: {image_id}")
    return updated


__all__ = [
    "COMPLETION_CHECKPOINT_IMAGES_KEY",
    "COMPLETION_CHECKPOINT_IMAGE_COMMITTED",
    "COMPLETION_CHECKPOINT_IMAGE_PENDING",
    "COMPLETION_CHECKPOINT_IMAGE_QUARANTINED",
    "COMPLETION_CHECKPOINT_IMAGE_EVENT_OUTBOX_ID_KEY",
    "COMPLETION_CHECKPOINT_IMAGE_EVENT_PUBLISHED_KEY",
    "COMPLETION_CHECKPOINT_IMAGE_QUARANTINE_REASON_KEY",
    "CompletionCheckpointCorrupt",
    "build_completed_checkpoint_images",
    "checkpoint_image_quarantine_count",
    "checkpoint_images_validation_error",
    "mark_checkpoint_image_committed",
    "mark_checkpoint_image_event_published",
    "mark_checkpoint_image_quarantined",
    "normalized_pending_checkpoint_image",
    "quarantined_checkpoint_image",
    "validated_checkpoint_images",
]
