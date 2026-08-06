from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.audit import AuditPersistenceError
from app.routes import me


def _postgres_url() -> str:
    raw = os.environ.get("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw:
        pytest.skip("LUMEN_TEST_POSTGRES_URL is not configured")
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    if raw.startswith("postgresql+psycopg2://"):
        return raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if not raw.startswith("postgresql+asyncpg://"):
        pytest.fail("LUMEN_TEST_POSTGRES_URL must use PostgreSQL")
    return raw


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": "/me",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


async def _create_tables(engine: AsyncEngine, *, reject_audit: bool) -> None:
    audit_constraint = (
        ", CONSTRAINT ck_reject_delete_audit "
        "CHECK (event_type <> 'me.account.delete')"
        if reject_audit
        else ""
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE users (
                    id varchar(36) PRIMARY KEY,
                    email varchar(255) NOT NULL,
                    account_mode varchar(16) NOT NULL DEFAULT 'wallet',
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    deleted_at timestamptz
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE TABLE auth_sessions (
                    id varchar(36) PRIMARY KEY,
                    user_id varchar(36) NOT NULL REFERENCES users(id),
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    revoked_at timestamptz
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE TABLE conversations (
                    id varchar(36) PRIMARY KEY,
                    user_id varchar(36) NOT NULL REFERENCES users(id),
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    deleted_at timestamptz
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE TABLE images (
                    id varchar(36) PRIMARY KEY,
                    user_id varchar(36) NOT NULL REFERENCES users(id),
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    deleted_at timestamptz
                )
                """
            )
        )
        await connection.execute(
            text(
                f"""
                CREATE TABLE audit_logs (
                    id varchar(36) PRIMARY KEY,
                    user_id varchar(36) REFERENCES users(id) ON DELETE SET NULL,
                    actor_email_hash varchar(64),
                    event_type varchar(64) NOT NULL,
                    actor_ip_hash varchar(64),
                    target_user_id varchar(36),
                    details jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now()
                    {audit_constraint}
                )
                """
            )
        )


async def _seed_account(engine: AsyncEngine, user_id: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO users (id, email, account_mode)
                VALUES (:user_id, 'member@example.test', 'wallet')
                """
            ),
            {"user_id": user_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO auth_sessions (id, user_id)
                VALUES ('session-1', :user_id)
                """
            ),
            {"user_id": user_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO conversations (id, user_id)
                VALUES ('conversation-1', :user_id)
                """
            ),
            {"user_id": user_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO images (id, user_id)
                VALUES ('image-1', :user_id)
                """
            ),
            {"user_id": user_id},
        )


async def _postgres_fixture(*, reject_audit: bool):
    admin_engine = create_async_engine(_postgres_url(), pool_pre_ping=True)
    schema = f"test_me_delete_{uuid4().hex[:12]}"
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        _postgres_url(),
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        await _create_tables(engine, reject_audit=reject_audit)
        yield engine
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            )
        await admin_engine.dispose()


def _zero_cleanup() -> dict[str, int]:
    return {
        "generations_canceled": 0,
        "completions_canceled": 0,
        "video_generations_canceled": 0,
        "videos_deleted": 0,
        "memory_extractions_canceled": 0,
    }


def _patch_external_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    post_commit_calls: list[str],
) -> None:
    async def cancel_account_active_tasks(*_args, **_kwargs):
        return _zero_cleanup()

    async def post_commit_account_task_cleanup(*, user_id: str, cleanup):
        assert cleanup == _zero_cleanup()
        post_commit_calls.append(user_id)

    monkeypatch.setattr(me, "get_redis", lambda: object())
    monkeypatch.setattr(
        me,
        "cancel_account_active_tasks",
        cancel_account_active_tasks,
    )
    monkeypatch.setattr(
        me,
        "post_commit_account_task_cleanup",
        post_commit_account_task_cleanup,
    )


@pytest.mark.asyncio
async def test_delete_me_postgres_does_not_self_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_commit_calls: list[str] = []
    _patch_external_cleanup(monkeypatch, post_commit_calls)
    user_id = f"user-{uuid4().hex[:20]}"

    async for engine in _postgres_fixture(reject_audit=False):
        await _seed_account(engine, user_id)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        response = Response()
        async with factory() as session:
            await session.execute(text("SET LOCAL lock_timeout = '750ms'"))
            await asyncio.wait_for(
                me.delete_my_account(
                    _request(),
                    SimpleNamespace(
                        id=user_id,
                        email="member@example.test",
                        account_mode="wallet",
                    ),
                    response,
                    session,
                ),
                timeout=2,
            )

        async with factory() as session:
            assert await session.scalar(
                text("SELECT deleted_at IS NOT NULL FROM users WHERE id=:id"),
                {"id": user_id},
            )
            assert await session.scalar(
                text(
                    "SELECT revoked_at IS NOT NULL FROM auth_sessions "
                    "WHERE user_id=:id"
                ),
                {"id": user_id},
            )
            assert await session.scalar(
                text(
                    "SELECT deleted_at IS NOT NULL FROM conversations "
                    "WHERE user_id=:id"
                ),
                {"id": user_id},
            )
            assert await session.scalar(
                text(
                    "SELECT deleted_at IS NOT NULL FROM images WHERE user_id=:id"
                ),
                {"id": user_id},
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE event_type='me.account.delete' AND user_id=:id"
                    ),
                    {"id": user_id},
                )
                == 1
            )

    assert response.status_code == 204
    assert post_commit_calls == [user_id]


@pytest.mark.asyncio
async def test_delete_me_postgres_audit_failure_rolls_back_all_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_commit_calls: list[str] = []
    _patch_external_cleanup(monkeypatch, post_commit_calls)
    user_id = f"user-{uuid4().hex[:20]}"

    async for engine in _postgres_fixture(reject_audit=True):
        await _seed_account(engine, user_id)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        response = Response()
        async with factory() as session:
            with pytest.raises(AuditPersistenceError):
                await me.delete_my_account(
                    _request(),
                    SimpleNamespace(
                        id=user_id,
                        email="member@example.test",
                        account_mode="wallet",
                    ),
                    response,
                    session,
                )
            await session.rollback()

        async with factory() as session:
            assert (
                await session.scalar(
                    text("SELECT deleted_at FROM users WHERE id=:id"),
                    {"id": user_id},
                )
                is None
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT revoked_at FROM auth_sessions WHERE user_id=:id"
                    ),
                    {"id": user_id},
                )
                is None
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT deleted_at FROM conversations WHERE user_id=:id"
                    ),
                    {"id": user_id},
                )
                is None
            )
            assert (
                await session.scalar(
                    text("SELECT deleted_at FROM images WHERE user_id=:id"),
                    {"id": user_id},
                )
                is None
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE event_type='me.account.delete'"
                    )
                )
                == 0
            )

    assert post_commit_calls == []
    assert all(key != b"set-cookie" for key, _value in response.raw_headers)
