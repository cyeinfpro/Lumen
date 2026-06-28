from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from lumen_core.byok_retention import (
    DEFAULT_BYOK_RETENTION_POLICY,
    ByokRetentionPolicy,
    is_user_visible,
    prune_expired_byok_user_data,
    retention_state,
)


class _WriteResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Db:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _WriteResult:
        self.statements.append(statement)
        return _WriteResult(1)


def test_byok_visibility_does_not_affect_wallet_users() -> None:
    now = datetime(2026, 6, 28, tzinfo=timezone.utc)
    old = now - timedelta(days=30)

    assert is_user_visible(
        account_mode="wallet",
        created_at=old,
        now=now,
        policy=ByokRetentionPolicy(hide_enabled=True, hide_days=3),
    )


def test_default_byok_retention_keeps_delete_opt_in() -> None:
    assert DEFAULT_BYOK_RETENTION_POLICY.hide_enabled is True
    assert DEFAULT_BYOK_RETENTION_POLICY.delete_enabled is False


def test_normalized_policy_never_deletes_before_hidden_window() -> None:
    policy = ByokRetentionPolicy(
        hide_enabled=True,
        delete_enabled=True,
        hide_days=7,
        delete_days=3,
    ).normalized()

    assert policy.hide_days == 7
    assert policy.delete_days == 7


def test_byok_retention_state_respects_admin_windows() -> None:
    now = datetime(2026, 6, 28, tzinfo=timezone.utc)
    policy = ByokRetentionPolicy(
        hide_enabled=True,
        delete_enabled=True,
        hide_days=5,
        delete_days=10,
    )

    assert (
        retention_state(
            account_mode="byok",
            created_at=now - timedelta(days=6),
            now=now,
            policy=policy,
        )
        == "hidden"
    )
    assert (
        retention_state(
            account_mode="byok",
            created_at=now - timedelta(days=11),
            now=now,
            policy=policy,
        )
        == "deleted"
    )


@pytest.mark.asyncio
async def test_prune_expired_byok_user_data_filters_to_byok_users() -> None:
    db = _Db()

    counts = await prune_expired_byok_user_data(
        db,  # type: ignore[arg-type]
        now=datetime(2026, 6, 28, tzinfo=timezone.utc),
        policy=ByokRetentionPolicy(delete_enabled=True, delete_days=7),
    )

    assert counts == {
        "messages_deleted": 1,
        "images_deleted": 1,
        "conversations_deleted": 1,
    }
    rendered = "\n".join(
        str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        for statement in db.statements
    )
    assert "users.account_mode = 'byok'" in rendered
    assert "UPDATE messages" in rendered
    assert "UPDATE images" in rendered
    assert "UPDATE conversations" in rendered


@pytest.mark.asyncio
async def test_prune_expired_byok_user_data_skips_when_disabled() -> None:
    db = _Db()

    counts = await prune_expired_byok_user_data(
        db,  # type: ignore[arg-type]
        policy=ByokRetentionPolicy(delete_enabled=False),
    )

    assert counts == {
        "messages_deleted": 0,
        "images_deleted": 0,
        "conversations_deleted": 0,
    }
    assert db.statements == []
