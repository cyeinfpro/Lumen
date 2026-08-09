from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import billing as worker_billing
from app.billing_parts import completion_pricing
from lumen_core.pricing import UsageTokens


def _postgres_url() -> str:
    raw = os.getenv("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw:
        pytest.skip("LUMEN_TEST_POSTGRES_URL is not configured")
    if not raw.startswith(("postgresql+asyncpg://", "postgresql://")):
        pytest.fail("LUMEN_TEST_POSTGRES_URL must use PostgreSQL")
    return raw.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.mark.asyncio
async def test_pricing_statement_abort_preserves_committed_reservation() -> None:
    engine = create_async_engine(_postgres_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(
                    "CREATE TEMP TABLE billing_abort_guard "
                    "(ref_id text PRIMARY KEY, held_micro bigint NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO billing_abort_guard (ref_id, held_micro) "
                    "VALUES ('completion-1', 2000)"
                )
            )
            await connection.commit()

            session = AsyncSession(bind=connection, expire_on_commit=False)
            completion = SimpleNamespace(
                id="completion-1",
                user_id="user-1",
                model="gpt-5.5",
                upstream_request={"tool_image_reserved_micro": 2_000},
            )

            async def aborted_breakdown(
                db: AsyncSession,
                *_args: Any,
                **_kwargs: Any,
            ) -> None:
                await db.execute(text("SELECT 1 / 0"))

            deps = SimpleNamespace(
                billing_core=worker_billing.billing_core,
                completion_cost_breakdown=aborted_breakdown,
                audit=worker_billing._audit,
            )

            with pytest.raises(SQLAlchemyError):
                async with session.begin():
                    await completion_pricing.resolve_completion_breakdown(
                        session,
                        completion,
                        billing_ref_id="completion-1",
                        usage=UsageTokens(input_tokens=1, output_tokens=1),
                        rate_multiplier=10_000,
                        service_tier="standard",
                        deps=deps,  # type: ignore[arg-type]
                    )

            held = (
                await session.execute(
                    text(
                        "SELECT held_micro FROM billing_abort_guard "
                        "WHERE ref_id = 'completion-1'"
                    )
                )
            ).scalar_one()
            assert held == 2_000
            assert "completion_billing_state" not in completion.upstream_request
            await session.close()
    finally:
        await engine.dispose()
