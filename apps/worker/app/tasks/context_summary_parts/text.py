from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from lumen_core.models import Message

from .common import iso, truncate


def looks_like_file_read(text: str) -> tuple[str, int] | None:
    stripped = text.lstrip()
    first_line = stripped.splitlines()[0] if stripped else ""
    match = re.match(r"(?:cat|Read)\s+([~/A-Za-z0-9_.\-/]+)", first_line)
    if not match:
        match = re.match(r"#\s*([~/A-Za-z0-9_.\-/]+)", first_line)
    if not match:
        return None
    return match.group(1), len(stripped.splitlines())


def summarize_json_blob(text: str) -> str | None:
    stripped = text.strip()
    if len(stripped) <= 800 or not stripped.startswith(("{", "[")):
        return None
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        keys = ", ".join(sorted(str(k) for k in payload.keys())[:40])
    elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
        keys = ", ".join(sorted(str(k) for k in payload[0].keys())[:40])
        keys = f"list[{len(payload)}] item_keys={keys}"
    else:
        keys = f"{type(payload).__name__}"
    return f"{stripped[:200]}\n[json summary: top-level keys={keys}]\n{stripped[-100:]}"


def extract_code_anchors(text: str) -> list[str]:
    anchors: list[str] = []
    patterns = (
        r"^\s*(?:async\s+def|def|class)\s+[A-Za-z_][\w_]*[^\n]*",
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+[A-Za-z_][\w_]*[^\n]*",
        r"^\s*(?:const|let|var)\s+[A-Za-z_][\w_]*\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
        r"^\s*(?:public|private|protected)?\s*(?:static\s+)?"
        r"[A-Za-z_<>,\[\]]+\s+[A-Za-z_][\w_]*\([^)]*\)",
    )
    for line in text.splitlines():
        for pattern in patterns:
            if re.match(pattern, line):
                anchors.append(line.strip())
                break
        if len(anchors) >= 40:
            break
    return anchors


def summarize_code_blob(text: str) -> str:
    lines = text.splitlines()
    anchors = extract_code_anchors(text)
    code_blocks = re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.DOTALL)
    block_summaries: list[str] = []
    for block in code_blocks[:12]:
        block_lines = block.strip("\n").splitlines()
        if not block_lines:
            continue
        first = block_lines[0].strip()
        last = block_lines[-1].strip()
        block_summaries.append(
            f"[code block: first={first!r} last={last!r} lines={len(block_lines)}]"
        )
    parts = [truncate(text[:800], 800)]
    if anchors:
        parts.append("[code anchors]\n" + "\n".join(anchors))
    if block_summaries:
        parts.append("\n".join(block_summaries))
    parts.append(f"[... {max(0, len(lines) - 20)} lines elided ...]")
    return "\n".join(part for part in parts if part)


def summarize_text_blob(text: str) -> str:
    """Serialize large text for summary input without mutating source messages."""
    if not text:
        return ""
    if len(text) <= 1500:
        return text
    file_read = looks_like_file_read(text)
    if file_read is not None:
        path, line_count = file_read
        return f"[file read summary: {path} - {line_count} lines]"
    json_summary = summarize_json_blob(text)
    if json_summary is not None:
        return json_summary
    if "```" in text or extract_code_anchors(text):
        return summarize_code_blob(text)
    return f"{text[:600].rstrip()}\n[... elided ...]\n{text[-400:].lstrip()}"


def _generated_image_lines(
    content: dict[str, Any],
    truncate_fn: Callable[[str, int], str],
) -> list[str]:
    generated: list[dict[str, Any]] = []
    seen_generated_ids: set[str] = set()

    def add_generated(candidate: Any) -> None:
        if not isinstance(candidate, dict):
            return
        image_id = candidate.get("image_id")
        dedupe_key = (
            str(image_id)
            if image_id
            else json.dumps(candidate, sort_keys=True, default=str)
        )
        if dedupe_key in seen_generated_ids:
            return
        seen_generated_ids.add(dedupe_key)
        generated.append(candidate)

    add_generated(content.get("generation_summary"))
    images = content.get("images")
    if isinstance(images, list):
        for image in images:
            add_generated(image)

    lines: list[str] = []
    for generated_image in generated:
        caption = generated_image.get("caption") or ""
        lines.append(
            f"[generated_image image_id={generated_image.get('image_id')} "
            f"width={generated_image.get('width')} "
            f"height={generated_image.get('height')} "
            f"caption={truncate_fn(str(caption), 280)!r}]"
        )
    return lines


def _attachment_summary_lines(
    content: dict[str, Any],
    image_captions: Mapping[str, str] | None,
    truncate_fn: Callable[[str, int], str],
) -> list[str]:
    lines: list[str] = []
    for attachment in content.get("attachments") or []:
        line = _attachment_summary_line(
            attachment,
            image_captions=image_captions,
            truncate_fn=truncate_fn,
        )
        if line:
            lines.append(line)
    return lines


def _attachment_summary_line(
    attachment: Any,
    *,
    image_captions: Mapping[str, str] | None,
    truncate_fn: Callable[[str, int], str],
) -> str | None:
    if not isinstance(attachment, dict):
        return None
    kind = attachment.get("kind")
    image_id = attachment.get("image_id")
    if kind == "image" or image_id:
        return _user_image_line(
            attachment,
            image_id=image_id,
            image_captions=image_captions,
            truncate_fn=truncate_fn,
        )
    if kind == "file":
        return (
            f"[user_file name={attachment.get('name')!r} "
            f"mime={attachment.get('mime')!r} size={attachment.get('size')}]"
        )
    return f"[attachment kind={kind!r}]"


def _user_image_line(
    attachment: dict[str, Any],
    *,
    image_id: Any,
    image_captions: Mapping[str, str] | None,
    truncate_fn: Callable[[str, int], str],
) -> str:
    ref = f"[user_image image_id={image_id}]"
    caption = attachment.get("caption")
    if (
        (not isinstance(caption, str) or not caption.strip())
        and image_id
        and image_captions
    ):
        caption = image_captions.get(str(image_id))
    if isinstance(caption, str) and caption.strip():
        ref += f" caption={truncate_fn(caption.strip(), 280)!r}"
    return ref


def message_to_summary_line(
    msg: Message,
    image_captions: Mapping[str, str] | None = None,
    *,
    iso_fn: Callable[[datetime | None], str | None] = iso,
    truncate_fn: Callable[[str, int], str] = truncate,
    summarize_text_fn: Callable[[str], str] = summarize_text_blob,
) -> str:
    role = str(getattr(msg, "role", "") or "").upper() or "UNKNOWN"
    created_at = getattr(msg, "created_at", None)
    created = iso_fn(created_at) if isinstance(created_at, datetime) else ""
    parts: list[str] = [f"[{role} #{getattr(msg, 'id', '')} @ {created}]"]

    content = getattr(msg, "content", None)
    if not isinstance(content, dict):
        content = {}

    text = content.get("text") or ""
    if isinstance(text, str):
        text = summarize_text_fn(text)
        if text:
            parts.append(text)

    parts.extend(
        _attachment_summary_lines(
            content,
            image_captions,
            truncate_fn,
        )
    )

    if role == "ASSISTANT":
        parts.extend(_generated_image_lines(content, truncate_fn))

    return "\n".join(parts)


def _select_fallback_source_prefix(
    lines: Sequence[str],
    *,
    source_budget: int,
    truncate_fn: Callable[[str, int], str],
) -> list[str]:
    selected: list[str] = []
    used = 0
    for line in lines:
        remaining = source_budget - used - 2
        if remaining < 200:
            break
        item = truncate_fn(line, min(1200, remaining))
        cost = len(item) + 2
        if used + cost > source_budget:
            break
        selected.append(item)
        used += cost
    return selected


def build_local_fallback_summary(
    *,
    previous_summary: str | None,
    messages: Sequence[Message],
    target_tokens: int,
    extra_instruction: str | None,
    image_captions: Mapping[str, str] | None,
    message_to_line: Callable[..., str],
    truncate_fn: Callable[[str, int], str],
) -> tuple[str | None, int]:
    lines = [
        message_to_line(message, image_captions=image_captions) for message in messages
    ]
    if not lines and not previous_summary:
        return None, 0

    budget_chars = min(
        max(2000, target_tokens * 4),
        max(1000, int(target_tokens * 1.5 * 4)),
    )
    parts: list[str] = [
        "## Earlier Context Summary",
        "### Local Fallback",
        "Upstream summarization did not finish; this deterministic fallback preserves the latest compacted source facts.",
    ]
    if previous_summary and previous_summary.strip():
        parts.extend(
            [
                "### Previous Summary",
                truncate_fn(previous_summary.strip(), max(800, budget_chars // 3)),
            ]
        )
    if extra_instruction and extra_instruction.strip():
        parts.extend(["### Additional Hints From User", extra_instruction.strip()])

    source_budget = max(0, budget_chars - sum(len(part) for part in parts) - 400)
    selected = _select_fallback_source_prefix(
        lines,
        source_budget=source_budget,
        truncate_fn=truncate_fn,
    )

    def render() -> str:
        source_parts = ["### Source Messages"]
        omitted = max(0, len(lines) - len(selected))
        if omitted > 0:
            source_parts.append(
                f"[{omitted} later source messages deferred by local fallback budget]"
            )
        source_parts.extend(selected)
        return "\n\n".join(part for part in [*parts, *source_parts] if part)

    text = render()
    while selected and len(text) > budget_chars:
        selected.pop()
        text = render()
    return truncate_fn(text, budget_chars), len(selected)


def local_fallback_summary_text(
    *,
    previous_summary: str | None,
    messages: Sequence[Message],
    target_tokens: int,
    extra_instruction: str | None,
    image_captions: Mapping[str, str] | None,
    message_to_line: Callable[..., str],
    truncate_fn: Callable[[str, int], str],
) -> str | None:
    text, _covered_message_count = build_local_fallback_summary(
        previous_summary=previous_summary,
        messages=messages,
        target_tokens=target_tokens,
        extra_instruction=extra_instruction,
        image_captions=image_captions,
        message_to_line=message_to_line,
        truncate_fn=truncate_fn,
    )
    return text
