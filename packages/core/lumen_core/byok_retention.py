"""BYOK user data retention helpers.

BYOK accounts keep user-visible conversations, message history and generated
images for a short window only. Non-BYOK accounts are deliberately unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Conversation, Image, Message, User

BYOK_ACCOUNT_MODE = "byok"
BYOK_DEFAULT_HIDE_DAYS = 3
BYOK_DEFAULT_DELETE_DAYS = 7
BYOK_DEFAULT_HIDE_ENABLED = True
BYOK_DEFAULT_DELETE_ENABLED = False
ByokRetentionState = Literal["active", "hidden", "deleted"]


@dataclass(frozen=True)
class ByokRetentionCutoffs:
    visible_after: datetime
    delete_before: datetime


@dataclass(frozen=True)
class ByokRetentionPolicy:
    hide_enabled: bool = BYOK_DEFAULT_HIDE_ENABLED
    delete_enabled: bool = BYOK_DEFAULT_DELETE_ENABLED
    hide_days: int = BYOK_DEFAULT_HIDE_DAYS
    delete_days: int = BYOK_DEFAULT_DELETE_DAYS

    def normalized(self) -> "ByokRetentionPolicy":
        hide_days = max(1, int(self.hide_days))
        delete_days = max(1, int(self.delete_days))
        if self.hide_enabled and self.delete_enabled:
            delete_days = max(delete_days, hide_days)
        return ByokRetentionPolicy(
            hide_enabled=bool(self.hide_enabled),
            delete_enabled=bool(self.delete_enabled),
            hide_days=hide_days,
            delete_days=delete_days,
        )


DEFAULT_BYOK_RETENTION_POLICY = ByokRetentionPolicy()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def cutoffs(
    now: datetime | None = None,
    policy: ByokRetentionPolicy | None = None,
) -> ByokRetentionCutoffs:
    current = ensure_aware_utc(now or utcnow())
    effective = (policy or DEFAULT_BYOK_RETENTION_POLICY).normalized()
    return ByokRetentionCutoffs(
        visible_after=current - timedelta(days=effective.hide_days),
        delete_before=current - timedelta(days=effective.delete_days),
    )


def applies_to_account_mode(account_mode: str | None) -> bool:
    return (account_mode or "").strip().lower() == BYOK_ACCOUNT_MODE


def applies_to_user(user: Any) -> bool:
    return applies_to_account_mode(getattr(user, "account_mode", None))


def user_visible_filter(
    user: Any,
    timestamp_column: Any,
    now: datetime | None = None,
    policy: ByokRetentionPolicy | None = None,
):
    effective = (policy or DEFAULT_BYOK_RETENTION_POLICY).normalized()
    if not applies_to_user(user) or not effective.hide_enabled:
        return None
    return timestamp_column >= cutoffs(now, effective).visible_after


def owner_visible_filter(
    account_mode_column: Any,
    timestamp_column: Any,
    now: datetime | None = None,
    policy: ByokRetentionPolicy | None = None,
):
    effective = (policy or DEFAULT_BYOK_RETENTION_POLICY).normalized()
    if not effective.hide_enabled:
        return None
    return or_(
        account_mode_column != BYOK_ACCOUNT_MODE,
        timestamp_column >= cutoffs(now, effective).visible_after,
    )


def is_user_visible(
    *,
    account_mode: str | None,
    created_at: datetime,
    now: datetime | None = None,
    policy: ByokRetentionPolicy | None = None,
) -> bool:
    effective = (policy or DEFAULT_BYOK_RETENTION_POLICY).normalized()
    if not applies_to_account_mode(account_mode) or not effective.hide_enabled:
        return True
    return ensure_aware_utc(created_at) >= cutoffs(now, effective).visible_after


def retention_state(
    *,
    account_mode: str | None,
    created_at: datetime,
    now: datetime | None = None,
    policy: ByokRetentionPolicy | None = None,
) -> ByokRetentionState:
    if not applies_to_account_mode(account_mode):
        return "active"
    effective = (policy or DEFAULT_BYOK_RETENTION_POLICY).normalized()
    created = ensure_aware_utc(created_at)
    limits = cutoffs(now, effective)
    if effective.delete_enabled and created < limits.delete_before:
        return "deleted"
    if effective.hide_enabled and created < limits.visible_after:
        return "hidden"
    return "active"


async def prune_expired_byok_user_data(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    policy: ByokRetentionPolicy | None = None,
) -> dict[str, int]:
    """Soft-delete BYOK user data older than the deletion window.

    Generation/completion task rows remain as operational audit records; user
    visible content is removed via messages, conversations and images.
    """

    effective = (policy or DEFAULT_BYOK_RETENTION_POLICY).normalized()
    if not effective.delete_enabled:
        return {
            "messages_deleted": 0,
            "images_deleted": 0,
            "conversations_deleted": 0,
        }

    deleted_at = ensure_aware_utc(now or utcnow())
    cutoff = cutoffs(deleted_at, effective).delete_before
    byok_user_ids = select(User.id).where(User.account_mode == BYOK_ACCOUNT_MODE)
    byok_conversation_ids = select(Conversation.id).where(
        Conversation.user_id.in_(byok_user_ids)
    )

    message_result = await db.execute(
        update(Message)
        .where(
            Message.conversation_id.in_(byok_conversation_ids),
            Message.deleted_at.is_(None),
            Message.created_at < cutoff,
        )
        .values(deleted_at=deleted_at)
        .execution_options(synchronize_session=False)
    )
    image_result = await db.execute(
        update(Image)
        .where(
            Image.user_id.in_(byok_user_ids),
            Image.deleted_at.is_(None),
            Image.created_at < cutoff,
        )
        .values(deleted_at=deleted_at)
        .execution_options(synchronize_session=False)
    )
    conversation_result = await db.execute(
        update(Conversation)
        .where(
            Conversation.user_id.in_(byok_user_ids),
            Conversation.deleted_at.is_(None),
            Conversation.last_activity_at < cutoff,
        )
        .values(deleted_at=deleted_at)
        .execution_options(synchronize_session=False)
    )
    return {
        "messages_deleted": int(message_result.rowcount or 0),
        "images_deleted": int(image_result.rowcount or 0),
        "conversations_deleted": int(conversation_result.rowcount or 0),
    }


__all__ = [
    "BYOK_ACCOUNT_MODE",
    "BYOK_DEFAULT_DELETE_ENABLED",
    "BYOK_DEFAULT_DELETE_DAYS",
    "BYOK_DEFAULT_HIDE_ENABLED",
    "BYOK_DEFAULT_HIDE_DAYS",
    "DEFAULT_BYOK_RETENTION_POLICY",
    "ByokRetentionCutoffs",
    "ByokRetentionPolicy",
    "ByokRetentionState",
    "applies_to_account_mode",
    "applies_to_user",
    "cutoffs",
    "ensure_aware_utc",
    "is_user_visible",
    "owner_visible_filter",
    "prune_expired_byok_user_data",
    "retention_state",
    "user_visible_filter",
]
