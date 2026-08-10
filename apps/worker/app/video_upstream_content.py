"""Seedance prompt and reference-media payload construction."""

from __future__ import annotations

import re
from typing import Any, Callable, Literal

from lumen_core.video_providers import (
    seedance_allows_audio_only_reference,
    seedance_reference_media_limits,
)

ErrorFactory = Callable[..., Exception]
ImageDataUrl = Callable[[bytes, str | None], str]

_REFERENCE_KINDS = ("image", "video", "audio")
_REFERENCE_ID_RE = re.compile(r"^ref:(image|video|audio):([1-9][0-9]{0,2})$")
_REFERENCE_ANCHOR_CANDIDATE_RE = re.compile(
    r"\[\s*(ref:[^\]\r\n]*)\s*\]",
    re.IGNORECASE,
)
# Leave headroom below the upstream 64 MiB JSON request-body ceiling.
_SEEDANCE_CONTENT_MAX_ESTIMATED_BYTES = 60 * 1024 * 1024
_SEEDANCE_CONTENT_BASE_OVERHEAD_BYTES = 512
_SEEDANCE_REFERENCE_ITEM_OVERHEAD_BYTES = 192


def _clean_reference_label(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    value = " ".join(raw.split())
    if not value:
        return None
    return value[:80]


def _reference_anchor_token(
    kind: str,
    index: int,
    ref_id: str | None = None,
) -> str:
    clean = (ref_id or "").strip().lower()
    parts = clean.split(":")
    if (
        len(parts) == 3
        and parts[0] == "ref"
        and parts[1] == kind
        and parts[2].isdigit()
        and int(parts[2]) > 0
    ):
        return f"[{clean}]"
    return f"[ref:{kind}:{index}]"


def _reference_order_aliases(
    *,
    kind: Literal["image", "video", "audio"],
    index: int,
    label: str | None,
    official: str,
    localized: str,
    anchor: str,
) -> list[str]:
    aliases: list[str] = []
    zh_digits = {
        1: "一",
        2: "二",
        3: "三",
        4: "四",
        5: "五",
        6: "六",
        7: "七",
        8: "八",
        9: "九",
    }
    noun = "图片" if kind == "image" else "音频" if kind == "audio" else "视频"
    short_noun = "图" if kind == "image" else noun
    for alias in (
        anchor,
        anchor.strip("[]"),
        _clean_reference_label(label),
        localized,
        f"[{localized}]",
        f"{noun}{index}",
        f"{short_noun}{index}",
        f"视频素材{index}" if kind == "video" else None,
        f"视频素材 {index}" if kind == "video" else None,
        f"参考视频{index}" if kind == "video" else None,
        f"参考视频 {index}" if kind == "video" else None,
        f"音频素材{index}" if kind == "audio" else None,
        f"音频素材 {index}" if kind == "audio" else None,
        f"参考音频{index}" if kind == "audio" else None,
        f"参考音频 {index}" if kind == "audio" else None,
        f"动作参考{index}" if kind == "video" else None,
        f"动作参考 {index}" if kind == "video" else None,
        f"运动参考{index}" if kind == "video" else None,
        f"运动参考 {index}" if kind == "video" else None,
        f"第{index}张{noun}" if kind == "image" else f"第{index}个{noun}",
        f"第{index}张{short_noun}" if kind == "image" else f"第{index}段{noun}",
        f"第{index}段素材" if kind == "video" else None,
        f"第{index}个视频素材" if kind == "video" else None,
        f"第{index}段音频素材" if kind == "audio" else None,
        f"第{index}个音频素材" if kind == "audio" else None,
        f"第{zh_digits[index]}张{noun}"
        if index in zh_digits and kind == "image"
        else None,
        f"第{zh_digits[index]}张{short_noun}"
        if index in zh_digits and kind == "image"
        else None,
        f"第{zh_digits[index]}个{noun}"
        if index in zh_digits and kind == "video"
        else None,
        f"第{zh_digits[index]}段{noun}"
        if index in zh_digits and kind == "video"
        else None,
        f"第{zh_digits[index]}段素材"
        if index in zh_digits and kind == "video"
        else None,
        f"第{zh_digits[index]}个视频素材"
        if index in zh_digits and kind == "video"
        else None,
        f"第{zh_digits[index]}个{noun}"
        if index in zh_digits and kind == "audio"
        else None,
        f"第{zh_digits[index]}段{noun}"
        if index in zh_digits and kind == "audio"
        else None,
        f"第{zh_digits[index]}段音频素材"
        if index in zh_digits and kind == "audio"
        else None,
        f"第{zh_digits[index]}个音频素材"
        if index in zh_digits and kind == "audio"
        else None,
    ):
        if alias and alias not in aliases and alias != official:
            aliases.append(alias)
    return aliases


def _reference_entries(req: Any) -> list[tuple[Any, int, str]]:
    indexes = {kind: 0 for kind in _REFERENCE_KINDS}
    resolved: set[str] = set()
    entries: list[tuple[Any, int, str]] = []
    for item in req.reference_media:
        if item.kind not in indexes:
            raise ValueError(f"unsupported reference media kind: {item.kind}")
        indexes[item.kind] += 1
        index = indexes[item.kind]
        expected_ref_id = f"ref:{item.kind}:{index}"
        ref_id = (item.ref_id or "").strip().lower()
        if ref_id:
            if ref_id in resolved:
                raise ValueError("reference media ref_id values must be unique")
            if _REFERENCE_ID_RE.fullmatch(ref_id) is None:
                raise ValueError("reference media ref_id must look like ref:<kind>:1")
            if ref_id != expected_ref_id:
                raise ValueError(
                    "reference media ref_id must match its same-kind content order"
                )
        resolved.add(expected_ref_id)
        entries.append((item, index, f"[{expected_ref_id}]"))
    return entries


def _reference_identity(
    item: Any,
    index: int,
    anchor: str,
) -> tuple[str, ...]:
    names = {
        "image": ("Image", "图片", "reference image"),
        "video": ("Video", "视频", "reference video"),
        "audio": ("Audio", "音频", "reference audio"),
    }
    official, localized, description = names[item.kind]
    return (
        f"{official} {index}",
        f"{localized} {index}",
        f"{description} #{index}",
        anchor,
        str(index),
    )


def _prompt_with_reference_order(req: Any) -> str:
    if req.action != "reference" or not req.reference_media:
        return req.prompt

    lines: list[str] = []
    for item, index, anchor in _reference_entries(req):
        official, localized, description, anchor, raw_index = _reference_identity(
            item,
            index,
            anchor,
        )
        aliases = _reference_order_aliases(
            kind=item.kind,
            index=int(raw_index),
            label=item.label,
            official=official,
            localized=localized,
            anchor=anchor,
        )
        alias_text = f"; user-prompt aliases: {', '.join(aliases)}" if aliases else ""
        lines.append(
            f"- {official}: {description} in the content array; stable anchor: "
            f"{anchor}{alias_text}."
        )

    if not lines:
        return req.prompt
    return (
        "Reference asset contract for this video request. Interpret the user's "
        "asset mentions by the stable anchors and official type + number below. "
        "If the user prompt includes an anchor such as [ref:image:1], bind that "
        "instruction only to the matching reference asset:\n"
        + "\n".join(lines)
        + "\n\nUser prompt:\n"
        + req.prompt
    )


def _prompt_with_official_reference_names(req: Any) -> str:
    if req.action != "reference" or not req.reference_media:
        return req.prompt

    nouns = {"image": "图片", "video": "视频", "audio": "音频"}
    names = {
        anchor[1:-1]: f"@{nouns[item.kind]}{index}"
        for item, index, anchor in _reference_entries(req)
    }
    return _REFERENCE_ANCHOR_CANDIDATE_RE.sub(
        lambda match: names.get(
            match.group(1).strip().lower(),
            match.group(0),
        ),
        req.prompt,
    )


def _selected_prompt(
    req: Any,
    *,
    include_reference_order_prompt: bool,
    use_official_reference_names: bool,
) -> str:
    if use_official_reference_names:
        return _prompt_with_official_reference_names(req)
    if include_reference_order_prompt:
        return _prompt_with_reference_order(req)
    return req.prompt


def _i2v_content_item(
    req: Any,
    *,
    allow_input_image_url: bool,
    image_data_url: ImageDataUrl,
    inline_image_max_bytes: int,
    error_factory: ErrorFactory,
) -> dict[str, Any] | None:
    if req.action != "i2v":
        return None
    image_url = req.input_image_url if allow_input_image_url else None
    if not image_url:
        if not req.input_image_bytes:
            raise error_factory(
                "missing input image bytes",
                error_code="invalid_input",
                status_code=422,
            )
        if len(req.input_image_bytes) > inline_image_max_bytes:
            raise error_factory(
                "input image is too large for inline video submission",
                error_code="invalid_input",
                status_code=413,
            )
        image_url = image_data_url(req.input_image_bytes, req.input_image_mime)
    return {
        "type": "image_url",
        "role": "first_frame",
        "image_url": {"url": image_url},
    }


def _validate_prompt_reference_anchors(
    req: Any,
    entries: list[tuple[Any, int, str]],
    error_factory: ErrorFactory,
) -> None:
    resolved = {anchor[1:-1] for _, _, anchor in entries}
    for match in _REFERENCE_ANCHOR_CANDIDATE_RE.finditer(req.prompt):
        ref_id = match.group(1).strip().lower()
        if _REFERENCE_ID_RE.fullmatch(ref_id) is None:
            raise error_factory(
                "prompt reference anchors must look like [ref:<kind>:1]",
                error_code="invalid_input",
                status_code=422,
            )
        if ref_id not in resolved:
            raise error_factory(
                f"prompt references unknown media anchor: {ref_id}",
                error_code="invalid_input",
                status_code=422,
            )


def _validate_reference_counts(
    req: Any,
    error_factory: ErrorFactory,
) -> list[tuple[Any, int, str]]:
    try:
        entries = _reference_entries(req)
    except ValueError as exc:
        raise error_factory(
            str(exc),
            error_code="invalid_input",
            status_code=422,
        ) from exc
    counts = {
        kind: sum(1 for item in req.reference_media if item.kind == kind)
        for kind in _REFERENCE_KINDS
    }
    allows_audio_only = seedance_allows_audio_only_reference(
        req.model,
        req.upstream_model,
    )
    if (
        not counts["image"]
        and not counts["video"]
        and (not counts["audio"] or not allows_audio_only)
    ):
        raise error_factory(
            "reference audio must be combined with a reference image or video",
            error_code="invalid_input",
            status_code=422,
        )
    limits = seedance_reference_media_limits(
        req.model,
        req.upstream_model,
    ) or {
        "image": 9,
        "video": 3,
        "audio": 3,
    }
    if any(counts[kind] > limits[kind] for kind in counts):
        raise error_factory(
            "too many reference media items",
            error_code="invalid_input",
            status_code=422,
        )
    _validate_prompt_reference_anchors(req, entries, error_factory)
    return entries


def _base64_encoded_size(byte_count: int) -> int:
    return 4 * ((byte_count + 2) // 3)


def _estimated_reference_payload_bytes(
    prompt: str,
    entries: list[tuple[Any, int, str]],
) -> int:
    total = len(prompt.encode("utf-8")) + _SEEDANCE_CONTENT_BASE_OVERHEAD_BYTES
    for item, _, _ in entries:
        total += _SEEDANCE_REFERENCE_ITEM_OVERHEAD_BYTES
        if item.kind == "image" and not item.url and item.data:
            total += (
                _base64_encoded_size(len(item.data))
                + len((item.mime or "image/jpeg").encode("utf-8"))
                + 32
            )
        elif item.url:
            total += len(item.url.encode("utf-8"))
    return total


def _validate_reference_payload_size(
    prompt: str,
    entries: list[tuple[Any, int, str]],
    *,
    inline_image_max_bytes: int,
    error_factory: ErrorFactory,
) -> None:
    for item, _, _ in entries:
        if (
            item.kind == "image"
            and not item.url
            and item.data
            and len(item.data) > inline_image_max_bytes
        ):
            raise error_factory(
                "reference image is too large for inline video submission",
                error_code="invalid_input",
                status_code=413,
                raw={
                    "actual_bytes": len(item.data),
                    "max_bytes": inline_image_max_bytes,
                },
            )
    estimated_bytes = _estimated_reference_payload_bytes(prompt, entries)
    if estimated_bytes > _SEEDANCE_CONTENT_MAX_ESTIMATED_BYTES:
        raise error_factory(
            "Seedance reference payload is too large for safe JSON submission",
            error_code="invalid_input",
            status_code=413,
            raw={
                "estimated_bytes": estimated_bytes,
                "max_bytes": _SEEDANCE_CONTENT_MAX_ESTIMATED_BYTES,
            },
        )


def _reference_content_item(
    item: Any,
    *,
    image_data_url: ImageDataUrl,
    error_factory: ErrorFactory,
) -> dict[str, Any] | None:
    if item.kind == "image":
        url = item.url
        if not url:
            if not item.data:
                raise error_factory(
                    "missing reference image data",
                    error_code="invalid_input",
                    status_code=422,
                )
            url = image_data_url(item.data, item.mime)
        return {
            "type": "image_url",
            "role": "reference_image",
            "image_url": {"url": url},
        }
    if item.kind == "video":
        if not item.url:
            raise error_factory(
                "reference video requires a public URL or asset ID",
                error_code="invalid_input",
                status_code=422,
            )
        return {
            "type": "video_url",
            "role": "reference_video",
            "video_url": {"url": item.url},
        }
    if item.kind == "audio":
        if not item.url:
            raise error_factory(
                "reference audio requires a public URL or asset ID",
                error_code="invalid_input",
                status_code=422,
            )
        return {
            "type": "audio_url",
            "role": "reference_audio",
            "audio_url": {"url": item.url},
        }
    return None


def build_seedance_content(
    req: Any,
    *,
    allow_input_image_url: bool,
    include_reference_order_prompt: bool,
    use_official_reference_names: bool,
    image_data_url: ImageDataUrl,
    inline_image_max_bytes: int,
    error_factory: ErrorFactory,
) -> list[dict[str, Any]]:
    reference_entries: list[tuple[Any, int, str]] = []
    if req.action == "reference":
        reference_entries = _validate_reference_counts(req, error_factory)
    prompt = _selected_prompt(
        req,
        include_reference_order_prompt=include_reference_order_prompt,
        use_official_reference_names=use_official_reference_names,
    )
    if reference_entries:
        _validate_reference_payload_size(
            prompt,
            reference_entries,
            inline_image_max_bytes=inline_image_max_bytes,
            error_factory=error_factory,
        )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    image = _i2v_content_item(
        req,
        allow_input_image_url=allow_input_image_url,
        image_data_url=image_data_url,
        inline_image_max_bytes=inline_image_max_bytes,
        error_factory=error_factory,
    )
    if image is not None:
        content.append(image)
    if req.action != "reference":
        return content
    for item, _, _ in reference_entries:
        reference = _reference_content_item(
            item,
            image_data_url=image_data_url,
            error_factory=error_factory,
        )
        if reference is not None:
            content.append(reference)
    return content


__all__ = [
    "_clean_reference_label",
    "_prompt_with_official_reference_names",
    "_prompt_with_reference_order",
    "_reference_anchor_token",
    "_reference_order_aliases",
    "build_seedance_content",
]
