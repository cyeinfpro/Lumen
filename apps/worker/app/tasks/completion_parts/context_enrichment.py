"""Completion context metadata and memory-enrichment contracts."""

from __future__ import annotations

from typing import Any

from .context import PackedContext, make_quality_probes


def context_metadata(packed: PackedContext) -> dict[str, Any]:
    return {
        "estimated_input_tokens": packed.estimated_tokens,
        "included_messages_count": packed.included_messages_count,
        "summary_used": packed.summary_used,
        "summary_created": packed.summary_created,
        "sticky_used": packed.sticky_used,
        "summary_up_to_message_id": packed.summary_up_to_message_id,
        "fallback_reason": packed.fallback_reason,
        "compressor_model": packed.compressor_model,
        "image_caption_count": packed.image_caption_count,
        "quality_probes": packed.quality_probes or make_quality_probes(packed),
    }


def append_text_to_first_system(
    input_list: list[dict[str, Any]],
    text: str,
) -> None:
    if not text:
        return
    for item in input_list:
        if item.get("role") != "system":
            continue
        content = item.get("content")
        if not isinstance(content, list) or not content:
            continue
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "input_text":
            old = first.get("text") if isinstance(first.get("text"), str) else ""
            first["text"] = f"{old.rstrip()}\n\n{text}" if old else text
            return
    input_list.insert(
        0,
        {"role": "system", "content": [{"type": "input_text", "text": text}]},
    )


def insert_user_context_after_summary(
    input_list: list[dict[str, Any]],
    text: str,
) -> None:
    if not text:
        return
    item = {"role": "user", "content": [{"type": "input_text", "text": text}]}
    insert_at = 1
    for index, existing in enumerate(input_list):
        content = existing.get("content")
        if not isinstance(content, list):
            continue
        joined = "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"input_text", "output_text"}
        )
        if "CONVERSATION SUMMARY" in joined or "会话摘要" in joined:
            insert_at = index + 1
    input_list.insert(insert_at, item)


async def inject_user_memory_context(
    session: Any,
    *,
    input_list: list[dict[str, Any]],
    user_id: str,
    conversation_id: str | None,
    parent_user_message_id: str | None,
    memory_extraction: Any | None,
    message_model: Any,
    redis: Any | None = None,
) -> dict[str, Any]:
    if (
        memory_extraction is None
        or conversation_id is None
        or not parent_user_message_id
    ):
        return {"used_memory_ids": [], "used_memory_summary": []}
    parent = await session.get(message_model, parent_user_message_id)
    if parent is None:
        return {"used_memory_ids": [], "used_memory_summary": []}
    parent_content = parent.content if isinstance(parent.content, dict) else {}
    user_text = parent_content.get("text") if isinstance(parent_content, dict) else ""
    if not isinstance(user_text, str):
        user_text = ""
    assembled = await memory_extraction.assemble_user_memory_prompt(
        session,
        user_id=user_id,
        conversation_id=conversation_id,
        user_text=user_text,
        redis=redis,
        parent_user_message_id=parent_user_message_id,
    )
    head_sections = "\n\n".join(
        section
        for section in (
            assembled.scope_hint_text,
            assembled.profile_text,
            assembled.constraints_text,
            assembled.confirmation_instruction,
        )
        if section
    )
    if head_sections:
        append_text_to_first_system(input_list, head_sections)
    if assembled.context_text:
        insert_user_context_after_summary(input_list, assembled.context_text)
    return {
        "used_memory_ids": assembled.used_memory_ids,
        "used_memory_summary": assembled.used_memory_summary,
        "confirmation_candidate_id": assembled.confirmation_candidate_id,
    }


async def record_completion_context_metadata(
    session: Any,
    *,
    task_id: str,
    attempt_epoch: int,
    packed: PackedContext,
    completion_model: Any,
) -> None:
    if not packed.compression_enabled:
        return
    completion = await session.get(completion_model, task_id)
    if completion is None or completion.attempt != attempt_epoch:
        return
    upstream_request = dict(completion.upstream_request or {})
    upstream_request["context"] = context_metadata(packed)
    completion.upstream_request = upstream_request
    await session.commit()
