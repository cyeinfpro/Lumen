from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.tasks import outbox
from lumen_core.models import OutboxDeadLetter, OutboxEvent


def _postgres_url() -> str:
    raw = os.getenv("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw:
        pytest.skip("LUMEN_TEST_POSTGRES_URL is not configured")
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    return raw


@pytest.mark.asyncio
async def test_outbox_dlq_fk_insert_uses_parent_locking_transaction() -> None:
    engine = create_async_engine(_postgres_url(), pool_pre_ping=True)
    suffix = uuid.uuid4().hex[:12]
    parent = f"test_outbox_parent_{suffix}"
    child = f"test_outbox_child_{suffix}"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'CREATE TABLE "{parent}" ('
                    "id text PRIMARY KEY, published_at timestamptz)"
                )
            )
            await connection.execute(
                text(
                    f'CREATE TABLE "{child}" ('
                    "id text PRIMARY KEY, "
                    f'outbox_id text REFERENCES "{parent}"(id))'
                )
            )
            await connection.execute(
                text(f"INSERT INTO \"{parent}\" (id) VALUES ('event-1')")
            )

        connection_one = await engine.connect()
        transaction_one = await connection_one.begin()
        try:
            await connection_one.execute(
                text(f"SELECT id FROM \"{parent}\" WHERE id = 'event-1' FOR UPDATE")
            )
            async with engine.connect() as connection_two:
                transaction_two = await connection_two.begin()
                try:
                    await connection_two.execute(
                        text("SET LOCAL lock_timeout = '500ms'")
                    )
                    with pytest.raises(DBAPIError):
                        await connection_two.execute(
                            text(
                                f'INSERT INTO "{child}" (id, outbox_id) '
                                "VALUES ('nested-session', 'event-1')"
                            )
                        )
                finally:
                    await transaction_two.rollback()

            await connection_one.execute(
                text(
                    f'INSERT INTO "{child}" (id, outbox_id) '
                    "VALUES ('same-session', 'event-1')"
                )
            )
            await transaction_one.commit()
        finally:
            await connection_one.close()

        async with engine.connect() as connection:
            count = (
                await connection.execute(text(f'SELECT count(*) FROM "{child}"'))
            ).scalar_one()
            assert count == 1
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP TABLE IF EXISTS "{child}"'))
            await connection.execute(text(f'DROP TABLE IF EXISTS "{parent}"'))
        await engine.dispose()


@pytest.mark.asyncio
async def test_actual_outbox_batch_persists_poison_event_dlq_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_engine = create_async_engine(_postgres_url(), pool_pre_ping=True)
    suffix = uuid.uuid4().hex[:12]
    schema = f"test_outbox_batch_{suffix}"
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_engine(
        _postgres_url(),
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )

    class Redis:
        def __init__(self) -> None:
            self.mirrored: list[str] = []

        async def get(self, _key: str) -> None:
            return None

        async def lpush(self, _key: str, value: str) -> None:
            self.mirrored.append(value)

        async def ltrim(self, *_args: object) -> None:
            return None

    redis = Redis()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(outbox, "SessionLocal", session_factory)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(OutboxEvent.__table__.create)
            await connection.run_sync(OutboxDeadLetter.__table__.create)

        async with session_factory() as session:
            event = OutboxEvent(kind="invalid-kind", payload={})
            session.add(event)
            await session.commit()
            event_id = event.id

        processed = await outbox._process_outbox_batch(  # noqa: SLF001
            redis,
            cutoff=outbox.datetime.now(outbox.timezone.utc),
            limit=10,
        )

        assert processed == 0
        async with session_factory() as session:
            event = await session.get(OutboxEvent, event_id)
            dlq = (
                await session.execute(
                    select(OutboxDeadLetter).where(
                        OutboxDeadLetter.outbox_id == event_id
                    )
                )
            ).scalar_one()
            assert event is not None and event.published_at is not None
            assert dlq.error_class == "OutboxInvalidPayload"
        assert len(redis.mirrored) == 1
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
