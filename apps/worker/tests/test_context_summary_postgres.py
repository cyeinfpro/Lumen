from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.tasks import context_summary
from lumen_core.context_window import SUMMARY_KIND, SUMMARY_VERSION
from lumen_core.models import Conversation


def _postgres_url() -> str:
    raw = os.getenv("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw:
        pytest.skip("LUMEN_TEST_POSTGRES_URL is not configured")
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    return raw


def _summary(message_number: int, text_value: str) -> dict[str, object]:
    return {
        "version": SUMMARY_VERSION,
        "kind": SUMMARY_KIND,
        "up_to_message_id": f"msg-{message_number:03d}",
        "up_to_created_at": (f"2026-01-01T00:00:{message_number:02d}+00:00"),
        "first_user_message_id": "msg-001",
        "text": text_value,
        "tokens": 10,
        "compression_runs": 1,
        "compressed_at": "2026-01-01T00:01:00+00:00",
    }


@pytest.mark.asyncio
async def test_postgres_concurrent_cas_refreshes_stale_identity_map() -> None:
    admin_engine = create_async_engine(_postgres_url(), pool_pre_ping=True)
    schema = f"test_context_summary_{uuid.uuid4().hex[:12]}"
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_engine(
        _postgres_url(),
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    conv_id = f"conv-{uuid.uuid4().hex[:20]}"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE conversations (
                        id varchar(36) PRIMARY KEY,
                        user_id varchar(36) NOT NULL,
                        title varchar(255) NOT NULL DEFAULT '',
                        pinned boolean NOT NULL DEFAULT false,
                        archived boolean NOT NULL DEFAULT false,
                        last_activity_at timestamptz NOT NULL DEFAULT now(),
                        default_params jsonb NOT NULL DEFAULT '{}'::jsonb,
                        default_system text,
                        default_system_prompt_id varchar(36),
                        summary_jsonb jsonb,
                        memory_disabled boolean NOT NULL DEFAULT false,
                        active_scope_id varchar(36),
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        deleted_at timestamptz
                    )
                    """
                )
            )

        async with session_factory() as session:
            session.add(
                Conversation(
                    id=conv_id,
                    user_id="user-1",
                    summary_jsonb=_summary(1, "initial"),
                )
            )
            await session.commit()

        stale_loaded = asyncio.Event()
        farther_committed = asyncio.Event()

        async def write_nearer_from_stale_session() -> bool:
            async with session_factory() as session:
                stale = await session.get(Conversation, conv_id)
                assert stale is not None
                assert stale.summary_jsonb == _summary(1, "initial")
                stale_loaded.set()
                await farther_committed.wait()
                return await context_summary._cas_write_summary(
                    session,
                    conv_id,
                    _summary(3, "nearer"),
                )

        async def write_farther_summary() -> bool:
            await stale_loaded.wait()
            try:
                async with session_factory() as session:
                    return await context_summary._cas_write_summary(
                        session,
                        conv_id,
                        _summary(9, "farther"),
                    )
            finally:
                farther_committed.set()

        nearer_wrote, farther_wrote = await asyncio.gather(
            write_nearer_from_stale_session(),
            write_farther_summary(),
        )

        assert farther_wrote is True
        assert nearer_wrote is False
        async with session_factory() as session:
            persisted = await session.get(
                Conversation,
                conv_id,
                populate_existing=True,
            )
            assert persisted is not None
            assert persisted.summary_jsonb == _summary(9, "farther")
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
