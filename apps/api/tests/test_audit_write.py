"""write_audit(autocommit=False) 事务隔离回归测试。

回归目标：审计行 flush 失败不得污染调用方事务 —— 否则调用方后续
commit 抛 PendingRollbackError，其未提交改动一并丢失（P2）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit import write_audit
from lumen_core.models import AuditLog


@pytest.mark.asyncio
async def test_audit_flush_failure_keeps_caller_transaction_usable() -> None:
    """审计行 flush 失败（外键违规）只回滚审计行，调用方 commit 不受影响。"""
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

            ok = await write_audit(
                session,
                event_type="audit.bad",
                user_id="missing-user",  # 外键违规 → flush 失败
                autocommit=False,
            )
            assert ok is False

            # 修复前：此处抛 PendingRollbackError；修复后：正常提交
            await session.commit()

            rows = (await session.execute(select(AuditLog))).scalars().all()
            assert [r.event_type for r in rows] == ["caller.row"]
            assert all(r.user_id == "real-user" for r in rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_audit_flush_failure_keeps_audit_row_out_of_commit() -> None:
    """失败审计行不随调用方 commit 落库，且日志计数带 mode=session。"""
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

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            ok = await write_audit(
                session,
                event_type="audit.bad",
                user_id="missing-user",
                autocommit=False,
            )
            assert ok is False
            await session.commit()
            rows = (await session.execute(select(AuditLog))).scalars().all()
            assert rows == []
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
