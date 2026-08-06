"""write_audit(autocommit=False) 事务原子性回归测试。"""

from __future__ import annotations

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import PendingRollbackError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit import AuditPersistenceError, write_audit
from lumen_core.models import AuditLog


@pytest.mark.asyncio
async def test_audit_flush_failure_poisoned_transaction_cannot_commit() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: AuditLog.metadata.create_all(c, tables=[AuditLog.__table__])
        )
        await conn.exec_driver_sql("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)")
        await conn.exec_driver_sql("INSERT INTO users (id) VALUES ('real-user')")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            # 调用方未提交改动
            caller_row = AuditLog(
                user_id="real-user", event_type="caller.row", details={}
            )
            session.add(caller_row)

            with pytest.raises(AuditPersistenceError) as exc_info:
                await write_audit(
                    session,
                    event_type="audit.bad",
                    user_id="missing-user",  # 外键违规 → flush 失败
                    autocommit=False,
                )
            assert exc_info.value.event_type == "audit.bad"

            with pytest.raises(PendingRollbackError):
                await session.commit()
            await session.rollback()

        async with factory() as verification_session:
            rows = (
                await verification_session.execute(select(AuditLog))
            ).scalars().all()
            assert rows == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_audit_flush_failure_allows_explicit_rollback_and_session_reuse() -> None:
    """调用方可显式回滚外层事务，随后复用 session 提交新事务。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: AuditLog.metadata.create_all(c, tables=[AuditLog.__table__])
        )
        await conn.exec_driver_sql("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)")
        await conn.exec_driver_sql("INSERT INTO users (id) VALUES ('real-user')")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            session.add(
                AuditLog(
                    user_id="real-user",
                    event_type="caller.before.rollback",
                    details={},
                )
            )

            with pytest.raises(AuditPersistenceError):
                await write_audit(
                    session,
                    event_type="audit.bad",
                    user_id="missing-user",
                    autocommit=False,
                )

            await session.rollback()
            session.add(
                AuditLog(
                    user_id="real-user",
                    event_type="caller.after.rollback",
                    details={},
                )
            )
            await session.commit()

            rows = (await session.execute(select(AuditLog))).scalars().all()
            assert [row.event_type for row in rows] == ["caller.after.rollback"]
            assert all(row.user_id == "real-user" for row in rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_audit_session_write_commits_with_caller_transaction() -> None:
    """成功路径：autocommit=False 的审计行与调用方同事务提交。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: AuditLog.metadata.create_all(c, tables=[AuditLog.__table__])
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            ok = await write_audit(
                session,
                event_type="share.create",
                user_id=None,
                actor_email_hash="h" * 64,
                autocommit=False,
            )
            assert ok is True
            await session.commit()
            rows = (await session.execute(select(AuditLog))).scalars().all()
            assert len(rows) == 1
            assert rows[0].event_type == "share.create"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_business_commit_failure_leaves_no_ghost_audit() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: AuditLog.metadata.create_all(c, tables=[AuditLog.__table__])
        )
        await conn.exec_driver_sql(
            "CREATE TABLE business_rows (id VARCHAR(36) PRIMARY KEY)"
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("INSERT INTO business_rows (id) VALUES ('business-1')")
            )
            await write_audit(
                session,
                event_type="business.update",
                actor_email_hash="h" * 64,
                autocommit=False,
            )

            @event.listens_for(engine.sync_engine, "commit", once=True)
            def _fail_commit(_connection) -> None:
                raise RuntimeError("injected commit failure")

            with pytest.raises(RuntimeError, match="injected commit failure"):
                await session.commit()
            await session.rollback()

        async with factory() as verification_session:
            business_count = (
                await verification_session.execute(
                    text("SELECT COUNT(*) FROM business_rows")
                )
            ).scalar_one()
            audits = (
                await verification_session.execute(select(AuditLog))
            ).scalars().all()
            assert business_count == 0
            assert audits == []
    finally:
        await engine.dispose()
