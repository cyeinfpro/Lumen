"""Validation helpers for video creation schemas."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Callable
from urllib.parse import urlsplit


def _normalize_reference_fields(media: Any) -> None:
    media.image_id = (media.image_id or "").strip() or None
    media.video_id = (media.video_id or "").strip() or None
    media.label = " ".join((media.label or "").split()) or None


def _validate_reference_id(media: Any, reference_id_re: re.Pattern[str]) -> None:
    if not media.ref_id:
        return
    ref_id = media.ref_id.strip().lower()
    match = reference_id_re.fullmatch(ref_id)
    if match is None:
        raise ValueError("reference media ref_id must look like ref:<kind>:1")
    if match.group(1) != media.kind:
        raise ValueError("reference media ref_id kind must match kind")
    media.ref_id = ref_id


def _validate_reference_source_contract(media: Any) -> None:
    sources = [
        bool(media.image_id),
        bool(media.video_id),
        bool((media.url or "").strip()),
    ]
    if sum(sources) != 1:
        raise ValueError("reference media must include exactly one source")
    if media.kind == "image" and not (media.image_id or media.url):
        raise ValueError("image reference requires image_id or url")
    if media.kind == "video" and not (media.video_id or media.url):
        raise ValueError("video reference requires video_id or url")
    if media.kind == "audio" and not (media.url or "").strip():
        raise ValueError("audio reference requires url")
    if media.kind == "image" and media.video_id:
        raise ValueError("image reference must not include video_id")
    if media.kind == "video" and media.image_id:
        raise ValueError("video reference must not include image_id")
    if media.kind == "audio" and (media.image_id or media.video_id):
        raise ValueError("audio reference supports url only")


def _normalize_reference_url(
    media: Any,
    *,
    normalize_asset_url: Callable[[str], str | None],
    private_host: Callable[[str], bool],
) -> None:
    if not media.url:
        return
    asset_url = normalize_asset_url(media.url)
    if asset_url is not None:
        if not asset_url:
            raise ValueError("reference media asset url must not be empty")
        media.url = asset_url
        return
    value = media.url.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("reference media url must be an https or asset URL")
    if parsed.username or parsed.password:
        raise ValueError("reference media url must not include credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("reference media url port is invalid") from exc
    if private_host(parsed.hostname):
        raise ValueError("reference media url host is not allowed")
    media.url = value


def validate_video_reference_media(
    media: Any,
    *,
    reference_id_re: re.Pattern[str],
    normalize_asset_url: Callable[[str], str | None],
    private_host: Callable[[str], bool],
) -> Any:
    _normalize_reference_fields(media)
    _validate_reference_id(media, reference_id_re)
    _validate_reference_source_contract(media)
    _normalize_reference_url(
        media,
        normalize_asset_url=normalize_asset_url,
        private_host=private_host,
    )
    return media


def _normalize_video_create_fields(request: Any) -> None:
    request.model = request.model.strip()
    request.prompt = request.prompt.strip()
    request.input_image_id = (request.input_image_id or "").strip() or None
    request.idempotency_key = request.idempotency_key.strip()
    if not request.model:
        raise ValueError("model must not be empty")
    if not request.prompt:
        raise ValueError("prompt must not be empty")
    if not request.idempotency_key:
        raise ValueError("idempotency_key must not be empty")


def _validate_video_duration(
    request: Any,
    *,
    allowed_resolutions: Callable[[str], tuple[str, ...] | None],
    duration_is_valid: Callable[[int, str], bool],
) -> None:
    if request.duration_s != -1 and request.duration_s < 3:
        raise ValueError("duration_s must be -1 or between 3 and 15")
    resolutions = allowed_resolutions(request.model)
    if resolutions is None:
        return
    if not duration_is_valid(request.duration_s, request.model):
        raise ValueError("Seedance 2.0 duration_s must be -1 or between 4 and 15")
    if request.resolution not in resolutions:
        raise ValueError("resolution is not supported by this Seedance 2.0 model")


def _normalize_prompt_anchors(
    request: Any,
    *,
    reference_id_re: re.Pattern[str],
    anchor_candidate_re: re.Pattern[str],
) -> set[str]:
    prompt_ref_ids: set[str] = set()
    for match in anchor_candidate_re.finditer(request.prompt):
        ref_id = match.group(1).strip().lower()
        if reference_id_re.fullmatch(ref_id) is None:
            raise ValueError("prompt reference anchors must look like [ref:<kind>:1]")
        prompt_ref_ids.add(ref_id)
    request.prompt = anchor_candidate_re.sub(
        lambda match: f"[{match.group(1).strip().lower()}]",
        request.prompt,
    )
    return prompt_ref_ids


def _validate_video_action_inputs(request: Any, prompt_ref_ids: set[str]) -> None:
    if request.action != "reference" and prompt_ref_ids:
        raise ValueError("reference anchors require action=reference")
    if request.action == "t2v":
        if request.input_image_id:
            raise ValueError("t2v must not include input_image_id")
        if request.reference_media:
            raise ValueError("t2v must not include reference_media")
    elif request.action == "i2v":
        if not request.input_image_id:
            raise ValueError("i2v requires input_image_id")
        if request.reference_media:
            raise ValueError("i2v must not include reference_media")
    elif request.input_image_id:
        raise ValueError("reference must not include input_image_id")


def _resolved_reference_ids(reference_media: list[Any]) -> set[str]:
    indexes: Counter[str] = Counter()
    resolved: set[str] = set()
    for item in reference_media:
        indexes[item.kind] += 1
        ref_id = item.ref_id or f"ref:{item.kind}:{indexes[item.kind]}"
        if ref_id in resolved:
            raise ValueError("reference media ref_id values must be unique")
        item.ref_id = ref_id
        resolved.add(ref_id)
    return resolved


def _validate_reference_request(request: Any, prompt_ref_ids: set[str]) -> None:
    if not request.reference_media:
        raise ValueError("reference requires at least one reference media")
    counts = Counter(item.kind for item in request.reference_media)
    limits = (
        ("image", 9, "reference supports at most 9 images"),
        ("video", 3, "reference supports at most 3 videos"),
        ("audio", 3, "reference supports at most 3 audio references"),
    )
    for kind, limit, message in limits:
        if counts[kind] > limit:
            raise ValueError(message)
    if counts["audio"] and not (counts["image"] or counts["video"]):
        raise ValueError("reference audio must be combined with an image or video")
    unknown_ref_ids = sorted(
        prompt_ref_ids - _resolved_reference_ids(request.reference_media)
    )
    if unknown_ref_ids:
        raise ValueError(
            "prompt references unknown media anchors: " + ", ".join(unknown_ref_ids)
        )


def validate_video_create(
    request: Any,
    *,
    reference_id_re: re.Pattern[str],
    anchor_candidate_re: re.Pattern[str],
    allowed_resolutions: Callable[[str], tuple[str, ...] | None],
    duration_is_valid: Callable[[int, str], bool],
) -> Any:
    _normalize_video_create_fields(request)
    _validate_video_duration(
        request,
        allowed_resolutions=allowed_resolutions,
        duration_is_valid=duration_is_valid,
    )
    prompt_ref_ids = _normalize_prompt_anchors(
        request,
        reference_id_re=reference_id_re,
        anchor_candidate_re=anchor_candidate_re,
    )
    _validate_video_action_inputs(request, prompt_ref_ids)
    if request.action == "reference":
        _validate_reference_request(request, prompt_ref_ids)
    return request


__all__ = ["validate_video_create", "validate_video_reference_media"]
