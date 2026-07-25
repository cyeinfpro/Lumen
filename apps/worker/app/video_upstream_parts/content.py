"""Public prompt and reference-media helpers for video upstream adapters."""

from __future__ import annotations

import re
from typing import Any, Literal

from ..video_upstream_content import build_seedance_content


def clean_reference_label(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    value = " ".join(raw.split())
    return value[:80] or None


def reference_anchor_token(
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


def reference_order_aliases(
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
        clean_reference_label(label),
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


def _reference_identity(item: Any, indexes: dict[str, int]) -> tuple[str, ...]:
    indexes[item.kind] += 1
    index = indexes[item.kind]
    names = {
        "image": ("Image", "图片", "reference image"),
        "video": ("Video", "视频", "reference video"),
        "audio": ("Audio", "音频", "reference audio"),
    }
    official, localized, description = names[item.kind]
    anchor = reference_anchor_token(item.kind, index, item.ref_id)
    return (
        f"{official} {index}",
        f"{localized} {index}",
        f"{description} #{index}",
        anchor,
        str(index),
    )


def prompt_with_reference_order(req: Any) -> str:
    if req.action != "reference" or not req.reference_media:
        return req.prompt

    lines: list[str] = []
    indexes = {"image": 0, "video": 0, "audio": 0}
    for item in req.reference_media:
        if item.kind not in indexes:
            continue
        official, localized, description, anchor, raw_index = _reference_identity(
            item,
            indexes,
        )
        aliases = reference_order_aliases(
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


def prompt_with_official_reference_names(req: Any) -> str:
    if req.action != "reference" or not req.reference_media:
        return req.prompt

    prompt = req.prompt
    indexes = {"image": 0, "video": 0, "audio": 0}
    nouns = {"image": "图片", "video": "视频", "audio": "音频"}
    for item in req.reference_media:
        indexes[item.kind] += 1
        index = indexes[item.kind]
        anchor = reference_anchor_token(item.kind, index, item.ref_id)
        prompt = re.sub(
            re.escape(anchor),
            f"{nouns[item.kind]}{index}",
            prompt,
            flags=re.IGNORECASE,
        )
    return prompt


__all__ = [
    "build_seedance_content",
    "clean_reference_label",
    "prompt_with_official_reference_names",
    "prompt_with_reference_order",
    "reference_anchor_token",
    "reference_order_aliases",
]
