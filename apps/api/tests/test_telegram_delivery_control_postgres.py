from __future__ import annotations

import asyncio
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.services.telegram_delivery import (
    StaleDeliveryOwner,
    begin_delivery_attempt,
    finish_delivery_attempt,
)
from app.services.telegram_quarantine import (
    QuarantineConflict,
    finish_control_command,
    persist_quarantine,
    queue_quarantine_redrive,
)
from lumen_core.models import AuditLog
from lumen_core.model_entities.control_operations import (
    TelegramControlCommand,
    TelegramDeliveryAttempt,
    TelegramDeliveryQuarantine,
)


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0060_telegram_delivery_control.py"
)


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


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "telegram_delivery_control_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sync_postgres_url() -> sa.URL:
    raw = os.environ.get("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw:
        pytest.skip("LUMEN_TEST_POSTGRES_URL is not configured")
    url = sa.make_url(raw)
    if url.get_backend_name() != "postgresql":
        pytest.fail("LUMEN_TEST_POSTGRES_URL must use PostgreSQL")
    return url.set(drivername="postgresql+psycopg2")


async def _create_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE users (id varchar(36) PRIMARY KEY)")
        )
        await connection.execute(
            text(
                """
                CREATE TABLE generations (
                    id varchar(36) PRIMARY KEY,
                    user_id varchar(36) NOT NULL REFERENCES users(id)
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE TABLE images (
                    id varchar(36) PRIMARY KEY,
                    owner_generation_id varchar(36)
                        REFERENCES generations(id) ON DELETE SET NULL,
                    deleted_at timestamptz
                )
                """
            )
        )
        await connection.run_sync(AuditLog.__table__.create)
        await connection.run_sync(TelegramDeliveryAttempt.__table__.create)
        await connection.run_sync(TelegramControlCommand.__table__.create)
        await connection.run_sync(TelegramDeliveryQuarantine.__table__.create)


async def _postgres_fixture():
    admin_engine = create_async_engine(_postgres_url(), pool_pre_ping=True)
    schema = f"test_telegram_control_{uuid4().hex[:12]}"
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        _postgres_url(),
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        await _create_tables(engine)
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            )
        await admin_engine.dispose()


async def _seed_delivery_target(
    engine: AsyncEngine,
    *,
    user_id: str = "user-1",
    generation_id: str = "generation-1",
    image_id: str = "image-1",
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO users (id) VALUES (:id)"),
            {"id": user_id},
        )
        await connection.execute(
            text(
                "INSERT INTO generations (id, user_id) "
                "VALUES (:generation_id, :user_id)"
            ),
            {"generation_id": generation_id, "user_id": user_id},
        )
        await connection.execute(
            text(
                "INSERT INTO images (id, owner_generation_id) "
                "VALUES (:image_id, :generation_id)"
            ),
            {"image_id": image_id, "generation_id": generation_id},
        )


@pytest.mark.asyncio
async def test_concurrent_delivery_reservation_has_exactly_one_sender() -> None:
    async for engine, factory in _postgres_fixture():
        await _seed_delivery_target(engine)

        async def reserve(owner_token: str) -> str:
            async with factory() as session:
                decision = await begin_delivery_attempt(
                    session,
                    generation_id="generation-1",
                    image_id="image-1",
                    chat_id=7,
                    owner_token=owner_token,
                )
                await session.commit()
                return decision.state

        states = await asyncio.wait_for(
            asyncio.gather(reserve("a" * 32), reserve("b" * 32)),
            timeout=5,
        )

        assert sorted(states) == ["result_unknown", "send_allowed"]
        async with factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(TelegramDeliveryAttempt)
            )
            assert count == 1


@pytest.mark.asyncio
async def test_delivery_receipt_fences_owner_and_replays_terminal_result() -> None:
    async for engine, factory in _postgres_fixture():
        await _seed_delivery_target(engine)
        async with factory() as session:
            first = await begin_delivery_attempt(
                session,
                generation_id="generation-1",
                image_id="image-1",
                chat_id=7,
                owner_token="a" * 32,
            )
            await session.commit()
            assert first.state == "send_allowed"

        async with factory() as session:
            failed = await finish_delivery_attempt(
                session,
                attempt_id=first.attempt_id,
                owner_token="a" * 32,
                state="failed_before_accept",
                error_class="TelegramBadRequest",
            )
            await session.commit()
            assert failed.newly_finished is True

        async with factory() as session:
            retry = await begin_delivery_attempt(
                session,
                generation_id="generation-1",
                image_id="image-1",
                chat_id=7,
                owner_token="b" * 32,
            )
            await session.commit()
            assert retry.state == "send_allowed"

        async with factory() as session:
            with pytest.raises(StaleDeliveryOwner):
                await finish_delivery_attempt(
                    session,
                    attempt_id=retry.attempt_id,
                    owner_token="a" * 32,
                    state="delivered",
                    telegram_message_id=100,
                )
            await session.rollback()

        async with factory() as session:
            delivered = await finish_delivery_attempt(
                session,
                attempt_id=retry.attempt_id,
                owner_token="b" * 32,
                state="delivered",
                telegram_message_id=101,
            )
            await session.commit()
            assert delivered.newly_finished is True

        async with factory() as session:
            replay = await begin_delivery_attempt(
                session,
                generation_id="generation-1",
                image_id="image-1",
                chat_id=7,
                owner_token="c" * 32,
            )
            assert replay.state == "already_delivered"
            assert replay.message_id == 101
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.event_type.like("telegram.delivery.%"))
            )
            assert audit_count == 2


@pytest.mark.asyncio
async def test_stale_dispatch_is_reconciled_to_result_unknown() -> None:
    async for engine, factory in _postgres_fixture():
        await _seed_delivery_target(engine)
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        async with factory() as session:
            first = await begin_delivery_attempt(
                session,
                generation_id="generation-1",
                image_id="image-1",
                chat_id=7,
                owner_token="a" * 32,
                now=old,
            )
            await session.commit()

        async with factory() as session:
            decision = await begin_delivery_attempt(
                session,
                generation_id="generation-1",
                image_id="image-1",
                chat_id=7,
                owner_token="b" * 32,
                now=datetime.now(timezone.utc),
            )
            await session.commit()
            assert decision.state == "result_unknown"
            row = await session.get(TelegramDeliveryAttempt, first.attempt_id)
            assert row is not None
            assert row.state == "delivery_result_unknown"


@pytest.mark.asyncio
async def test_control_active_slot_ack_and_command_type_are_durable() -> None:
    async for engine, factory in _postgres_fixture():
        await _seed_delivery_target(engine)
        async with factory() as session:
            session.add(
                TelegramControlCommand(
                    id="command-a",
                    target="tgbot",
                    command="restart",
                    requested_by="user-1",
                    payload={},
                    status="pending",
                    active_slot=1,
                )
            )
            await session.commit()

        async with factory() as session:
            session.add(
                TelegramControlCommand(
                    id="command-b",
                    target="tgbot",
                    command="restart",
                    requested_by="user-1",
                    payload={},
                    status="pending",
                    active_slot=1,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

        async with factory() as session:
            with pytest.raises(QuarantineConflict, match="does not match"):
                await finish_control_command(
                    session,
                    command_id="command-a",
                    expected_command="redrive_quarantine",
                    status="accepted",
                )
            await session.rollback()

        async with factory() as session:
            accepted = await finish_control_command(
                session,
                command_id="command-a",
                expected_command="restart",
                status="accepted",
            )
            await session.commit()
            assert accepted.newly_terminal is True

        async with factory() as session:
            session.add(
                TelegramControlCommand(
                    id="command-b",
                    target="tgbot",
                    command="restart",
                    requested_by="user-1",
                    payload={},
                    status="pending",
                    active_slot=1,
                )
            )
            await session.commit()
            first = await session.get(TelegramControlCommand, "command-a")
            assert first is not None
            assert first.status == "accepted"
            assert first.active_slot is None


@pytest.mark.asyncio
async def test_quarantine_is_idempotent_redrivable_and_operator_visible() -> None:
    async for engine, factory in _postgres_fixture():
        await _seed_delivery_target(engine)
        async with factory() as session:
            first = await persist_quarantine(
                session,
                source_stream="events:user:user-1",
                source_id="100-0",
                stream_user_id="user-1",
                event="generation.succeeded",
                generation_id="generation-1",
                payload_raw='{"event":"generation.succeeded","data":{}}',
                reason="delivery failed",
                attempts=3,
            )
            duplicate = await persist_quarantine(
                session,
                source_stream="events:user:user-1",
                source_id="100-0",
                stream_user_id="user-1",
                event="generation.succeeded",
                generation_id="generation-1",
                payload_raw='{"event":"generation.succeeded","data":{}}',
                reason="delivery failed",
                attempts=3,
            )
            await session.commit()
            assert duplicate.id == first.id
            quarantine_id = first.id

        async with factory() as session:
            command = await queue_quarantine_redrive(
                session,
                quarantine_id=quarantine_id,
                requested_by="user-1",
            )
            await session.commit()
            first_command_id = command.id

        async with factory() as session:
            failed = await finish_control_command(
                session,
                command_id=first_command_id,
                expected_command="redrive_quarantine",
                status="failed",
                error="tracker unavailable",
            )
            await session.commit()
            assert failed.status == "failed"

        async with factory() as session:
            row = await session.get(TelegramDeliveryQuarantine, quarantine_id)
            assert row is not None
            assert row.status == "pending"
            assert row.last_error == "tracker unavailable"
            command = await queue_quarantine_redrive(
                session,
                quarantine_id=quarantine_id,
                requested_by="user-1",
            )
            await session.commit()
            second_command_id = command.id

        async with factory() as session:
            accepted = await finish_control_command(
                session,
                command_id=second_command_id,
                expected_command="redrive_quarantine",
                status="accepted",
            )
            await session.commit()
            assert accepted.status == "accepted"
            row = await session.get(TelegramDeliveryQuarantine, quarantine_id)
            assert row is not None
            assert row.status == "resolved"
            assert row.resolved_at is not None
            assert row.redrive_count == 2


def test_telegram_control_migration_round_trips_on_postgres() -> None:
    url = _sync_postgres_url()
    schema = f"test_telegram_migration_{uuid4().hex[:12]}"
    admin_engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    schema_engine: sa.Engine | None = None
    try:
        with admin_engine.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        schema_engine = sa.create_engine(
            url,
            poolclass=sa.pool.NullPool,
            connect_args={"options": f"-csearch_path={schema}"},
        )
        with schema_engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE users (id varchar(36) PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE generations (id varchar(36) PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE images (id varchar(36) PRIMARY KEY)"
            )
            migration = _load_migration()
            context = MigrationContext.configure(connection)
            operations = Operations(context)
            original_op = migration.op
            migration.op = operations
            try:
                migration.upgrade()
                tables = set(sa.inspect(connection).get_table_names())
                assert {
                    "telegram_control_commands",
                    "telegram_delivery_attempts",
                    "telegram_delivery_quarantines",
                } <= tables
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO telegram_control_commands (
                            id, target, command, payload, status, active_slot
                        ) VALUES (
                            'active-command', 'tgbot', 'restart',
                            '{}'::json, 'pending', 1
                        )
                        """
                    )
                )
                with pytest.raises(RuntimeError, match="active Telegram operations"):
                    migration.downgrade()
                connection.execute(
                    sa.text(
                        "DELETE FROM telegram_control_commands "
                        "WHERE id='active-command'"
                    )
                )
                export_path = Path(f"/tmp/{schema}-telegram-export.json")
                os.environ["LUMEN_MIGRATION_EXPORT_PATH"] = str(export_path)
                try:
                    migration.downgrade()
                finally:
                    os.environ.pop("LUMEN_MIGRATION_EXPORT_PATH", None)
                    export_path.unlink(missing_ok=True)
                remaining = set(sa.inspect(connection).get_table_names())
                assert "telegram_control_commands" not in remaining
                assert "telegram_delivery_attempts" not in remaining
                assert "telegram_delivery_quarantines" not in remaining
            finally:
                migration.op = original_op
    finally:
        if schema_engine is not None:
            schema_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        admin_engine.dispose()
