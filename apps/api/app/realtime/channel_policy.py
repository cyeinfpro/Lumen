from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    VideoGeneration,
    WorkflowRun,
)


MAX_SSE_CHANNELS = 64
COMPACTION_CHANNEL_PREFIX = "lumen:events:conversation:"


@dataclass(frozen=True)
class ChannelPolicyError(Exception):
    code: str
    message: str
    status_code: int = 400
    extra: dict[str, int] | None = None


@dataclass
class ChannelRequest:
    parsed: list[tuple[str, str, str]] = field(default_factory=list)
    conversation_ids: set[str] = field(default_factory=set)
    task_ids: set[str] = field(default_factory=set)
    storyboard_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ChannelOwnership:
    conversation_ids: set[str]
    task_ids: set[str]
    storyboard_ids: set[str]


@dataclass(frozen=True)
class ChannelSelection:
    requested: list[str]
    client_requested: list[str]
    user_channel: str


def parse_channel(raw: str, user_id: str) -> tuple[str, str, str] | None:
    channel = raw.strip()
    if not channel or ":" not in channel:
        return None
    prefix, _, ref = channel.partition(":")
    if prefix not in {"user", "conv", "task", "storyboard"}:
        return None
    if prefix == "user" and ref != user_id:
        raise ChannelPolicyError(
            "forbidden_channel",
            f"cannot subscribe to user:{ref}",
            403,
        )
    return channel, prefix, ref


def parse_channel_request(channels: list[str], user_id: str) -> ChannelRequest:
    request = ChannelRequest()
    for raw in channels:
        parsed = parse_channel(raw, user_id)
        if parsed is None:
            continue
        request.parsed.append(parsed)
        remember_channel_reference(request, parsed)
    return request


def remember_channel_reference(
    request: ChannelRequest,
    parsed: tuple[str, str, str],
) -> None:
    _channel, prefix, ref = parsed
    if prefix == "conv":
        request.conversation_ids.add(ref)
    elif prefix == "task":
        request.task_ids.add(ref)
    elif prefix == "storyboard":
        request.storyboard_ids.add(ref)


async def owned_conversation_ids(
    db: AsyncSession,
    conversation_ids: set[str],
    user_id: str,
) -> set[str]:
    if not conversation_ids:
        return set()
    rows = await db.execute(
        select(Conversation.id).where(
            Conversation.id.in_(conversation_ids),
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
    )
    return set(rows.scalars().all())


async def owned_task_ids(
    db: AsyncSession,
    task_ids: set[str],
    user_id: str,
) -> set[str]:
    if not task_ids:
        return set()
    owned: set[str] = set()
    for model in (Generation, Completion, VideoGeneration):
        rows = await db.execute(
            select(model.id).where(
                model.id.in_(task_ids),
                model.user_id == user_id,
            )
        )
        owned.update(rows.scalars().all())
    return owned


async def owned_storyboard_ids(
    db: AsyncSession,
    storyboard_ids: set[str],
    user_id: str,
) -> set[str]:
    if not storyboard_ids:
        return set()
    rows = await db.execute(
        select(WorkflowRun.id).where(
            WorkflowRun.id.in_(storyboard_ids),
            WorkflowRun.user_id == user_id,
            WorkflowRun.type == "storyboard",
            WorkflowRun.deleted_at.is_(None),
        )
    )
    return set(rows.scalars().all())


async def load_channel_ownership(
    request: ChannelRequest,
    user_id: str,
    db: AsyncSession,
) -> ChannelOwnership:
    conversation_ids = await owned_conversation_ids(
        db,
        request.conversation_ids,
        user_id,
    )
    task_ids = await owned_task_ids(db, request.task_ids, user_id)
    storyboard_ids = await owned_storyboard_ids(
        db,
        request.storyboard_ids,
        user_id,
    )
    return ChannelOwnership(conversation_ids, task_ids, storyboard_ids)


def authorized_channel(
    parsed: tuple[str, str, str],
    ownership: ChannelOwnership,
) -> str:
    channel, prefix, ref = parsed
    if prefix == "conv" and ref not in ownership.conversation_ids:
        raise ChannelPolicyError("forbidden_channel", f"conv {ref} not owned", 403)
    if prefix == "task" and ref not in ownership.task_ids:
        raise ChannelPolicyError("forbidden_channel", f"task {ref} not owned", 403)
    if prefix == "storyboard" and ref not in ownership.storyboard_ids:
        raise ChannelPolicyError(
            "forbidden_channel",
            f"storyboard {ref} not owned",
            403,
        )
    return channel


def authorized_channels(
    parsed_channels: list[tuple[str, str, str]],
    ownership: ChannelOwnership,
) -> list[str]:
    return [authorized_channel(parsed, ownership) for parsed in parsed_channels]


async def validate_channels(
    channels: list[str],
    user_id: str,
    db: AsyncSession,
) -> list[str]:
    request = parse_channel_request(channels, user_id)
    ownership = await load_channel_ownership(request, user_id, db)
    return authorized_channels(request.parsed, ownership)


def select_channels(channels: str, user_id: str) -> ChannelSelection:
    client_requested = list(
        dict.fromkeys(
            channel.strip() for channel in channels.split(",") if channel.strip()
        )
    )
    user_channel = f"user:{user_id}"
    requested = list(client_requested or [user_channel])
    return ChannelSelection(requested, client_requested, user_channel)


def validate_channel_limit(selection: ChannelSelection) -> None:
    if len(selection.requested) <= MAX_SSE_CHANNELS:
        return
    raise ChannelPolicyError(
        "too_many_channels",
        f"cannot subscribe to more than {MAX_SSE_CHANNELS} channels",
        400,
        {
            "max_channels": MAX_SSE_CHANNELS,
            "requested_count": len(selection.client_requested),
            "effective_count": len(selection.requested),
        },
    )


def replay_channel_selection(
    valid_channels: list[str],
    selection: ChannelSelection,
) -> set[str]:
    requested_channels = set(valid_channels)
    if (
        selection.client_requested
        and selection.user_channel not in selection.client_requested
    ):
        requested_channels.discard(selection.user_channel)
    return requested_channels


def compaction_bridge_channels(channels: list[str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for channel in channels:
        prefix, _, ref = channel.partition(":")
        if prefix == "conv" and ref:
            mapped[f"{COMPACTION_CHANNEL_PREFIX}{ref}"] = channel
    return mapped
