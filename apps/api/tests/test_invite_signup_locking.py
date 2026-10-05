from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from app.routes.auth_parts import signup
from lumen_core.model_entities import AllowedEmail, InviteLink, User


def runtime():
    async def reject(**kwargs):
        raise HTTPException(403, kwargs["reason"])

    rt = SimpleNamespace(
        ensure_utc=lambda value: value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value,
        _reject_signup=reject,
        _reject_byok_signup=reject,
    )
    rt._invite_validity_reason = lambda inv, now, creator: signup.invite_validity_reason(rt, inv, now, creator)
    return rt


async def access(kind, db, *, token="test-token", email="new@example.test"):
    body = SimpleNamespace(invite_token=token, password="test-password")
    if kind == "standard":
        return await signup.standard_signup_access(runtime(), db, body, None, email)
    return await signup.byok_signup_access(
        runtime(), db, body=body, request=None, email=email,
        password=body.password, now=datetime.now(timezone.utc), bypasses_allowlist=False,
    )


class Rows:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return None

    def first(self):
        return self.value


class Db:
    def __init__(self, row):
        self.row = row
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return Rows(self.row)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["standard", "byok"])
@pytest.mark.parametrize(
    "invalid", [None, "missing", "creator_missing", "creator_deleted", "used", "revoked", "expired", "email_mismatch"],
)
async def test_signup_invite_lock_and_validation(kind, invalid):
    now = datetime.now(timezone.utc)
    inv = SimpleNamespace(
        token="test-token", role="member", email=None,
        used_at=now if invalid == "used" else None,
        revoked_at=now if invalid == "revoked" else None,
        expires_at=now - timedelta(days=1) if invalid == "expired" else now + timedelta(days=1),
    )
    if invalid == "email_mismatch":
        inv.email = "someone-else@example.test"
    creator = None if invalid == "creator_missing" else SimpleNamespace(
        deleted_at=now if invalid == "creator_deleted" else None,
    )
    db = Db(None if invalid == "missing" else (inv, creator))
    if invalid:
        expected = {
            "missing": "invalid_invite", "creator_missing": "creator_deleted",
            "email_mismatch": "invite_email_mismatch",
        }.get(invalid, invalid)
        with pytest.raises(HTTPException) as error:
            await access(kind, db)
        assert error.value.detail == expected
    else:
        result = await access(kind, db)
        assert (result.invite if kind == "standard" else result[1]) is inv
    stmt = db.statements[-1]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "LEFT OUTER JOIN users" in sql
    assert sql.endswith("FOR UPDATE OF invite_links")
    assert stmt.get_execution_options()["populate_existing"] is True


@pytest_asyncio.fixture
async def pg_sessions():
    # Opt in only to a disposable PostgreSQL test instance, never application DB settings.
    url = os.getenv("LUMEN_INVITE_TEST_PG_URL")
    if not url:
        pytest.skip("LUMEN_INVITE_TEST_PG_URL is required for real PostgreSQL locking tests")
    schema = "invite_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as conn:
            for model in (User, AllowedEmail, InviteLink):
                # This narrow fixture does not create unrelated system_prompts.
                ddl = CreateTable(model.__table__, include_foreign_key_constraints=[])
                await conn.execute(ddl)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            db.add(User(id="creator", email="creator@example.test"))
            db.add(InviteLink(id="invite", token="test-token", created_by="creator"))
            await db.commit()
        yield sessions
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_postgres_reproduces_legacy_nullable_join_lock(pg_sessions):
    async with pg_sessions() as db:
        stmt = select(InviteLink, User).join(
            User, User.id == InviteLink.created_by, isouter=True,
        ).with_for_update()
        with pytest.raises(Exception, match="nullable side of an outer join"):
            await db.execute(stmt)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["standard", "byok"])
async def test_postgres_serializes_single_invite_consumption(pg_sessions, kind):
    async with pg_sessions() as first, pg_sessions() as second:
        result = await access(kind, first)
        inv = result.invite if kind == "standard" else result[1]
        waiter = asyncio.create_task(access(kind, second, email="second@example.test"))
        try:
            await asyncio.sleep(0.1)
            assert not waiter.done(), "second consumer must wait for invite row lock"
            inv.used_at = datetime.now(timezone.utc)
            await first.commit()
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(waiter, 5)
            assert error.value.detail == "used"
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            await second.rollback()
