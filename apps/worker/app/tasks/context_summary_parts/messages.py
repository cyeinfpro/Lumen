from __future__ import annotations

from collections.abc import Sequence

from lumen_core.context_window import estimate_message_tokens
from lumen_core.models import Message

from .common import LoadedSummaryMessages


def loaded_summary_prefix(
    loaded: LoadedSummaryMessages,
    covered_message_count: int,
) -> LoadedSummaryMessages:
    count = min(max(0, covered_message_count), len(loaded.messages))
    if count >= len(loaded.messages):
        return loaded

    messages = list(loaded.messages[:count])
    captioned_attachment_count = 0
    generated_caption_ids: set[str] = set()
    for message in messages:
        content = message.content if isinstance(message.content, dict) else {}
        for attachment in content.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            caption = attachment.get("caption")
            if isinstance(caption, str) and caption.strip():
                captioned_attachment_count += 1
                continue
            image_id = attachment.get("image_id")
            if (
                isinstance(image_id, str)
                and image_id
                and loaded.image_captions
                and image_id in loaded.image_captions
            ):
                generated_caption_ids.add(image_id)

    return LoadedSummaryMessages(
        messages=messages,
        source_message_count=len(messages),
        source_token_estimate=sum(
            estimate_message_tokens(message.role, message.content)
            for message in messages
        ),
        image_caption_count=captioned_attachment_count + len(generated_caption_ids),
        image_captions=loaded.image_captions,
    )


def uncaptioned_image_ids(messages: Sequence[Message]) -> list[str]:
    seen: set[str] = set()
    image_ids: list[str] = []
    for message in messages:
        content = message.content if isinstance(message.content, dict) else {}
        for attachment in content.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            image_id = attachment.get("image_id")
            caption = attachment.get("caption")
            if (
                isinstance(image_id, str)
                and image_id
                and not (isinstance(caption, str) and caption.strip())
                and image_id not in seen
            ):
                seen.add(image_id)
                image_ids.append(image_id)
    return image_ids
