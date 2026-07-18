from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.tasks import storyboard_assembly


def _postgres_url() -> str:
    raw = os.getenv("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw:
        pytest.skip("LUMEN_TEST_POSTGRES_URL is not configured")
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    return raw


@pytest.mark.parametrize("pending_status", ("waiting", "compositing"))
@pytest.mark.asyncio
async def test_postgres_two_async_sessions_allow_exactly_one_storyboard_claim(
    monkeypatch: pytest.MonkeyPatch,
    pending_status: str,
) -> None:
    postgres_url = _postgres_url()
    admin_engine = create_async_engine(postgres_url, pool_pre_ping=True)
    schema = f"test_storyboard_claim_{uuid.uuid4().hex[:12]}"
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_engine(
        postgres_url,
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    step_id = f"assembly-{uuid.uuid4().hex[:20]}"
    initial_output = {
        "assembly_attempt_token": "attempt-1",
        "assembly_fingerprint": "fingerprint-1",
        "assembly_claimed_at": None,
    }
    claimed_output = {
        **initial_output,
        "assembly_claimed_at": "2026-07-18T00:00:00+00:00",
    }
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE workflow_steps (
                        id varchar(36) PRIMARY KEY,
                        step_key varchar(64) NOT NULL,
                        status varchar(32) NOT NULL,
                        output_json jsonb NOT NULL,
                        updated_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO workflow_steps (id, step_key, status, output_json)
                    VALUES (:step_id, 'assembly', :status, CAST(:output_json AS jsonb))
                    """
                ),
                {
                    "step_id": step_id,
                    "status": pending_status,
                    "output_json": json.dumps(initial_output),
                },
            )

        rowcounts: list[int] = []
        affected_rows = storyboard_assembly.affected_rows

        def record_affected_rows(result: Any) -> int:
            rowcount = affected_rows(result)
            rowcounts.append(rowcount)
            return rowcount

        monkeypatch.setattr(
            storyboard_assembly,
            "affected_rows",
            record_affected_rows,
        )

        ready = asyncio.Event()
        ready_lock = asyncio.Lock()
        ready_count = 0

        async def claim(session: AsyncSession) -> bool:
            nonlocal ready_count
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            async with ready_lock:
                ready_count += 1
                if ready_count == 2:
                    ready.set()
            await ready.wait()
            claimed = await storyboard_assembly._claim_waiting_assembly(  # noqa: SLF001
                session,
                step_id=step_id,
                attempt_token="attempt-1",
                fingerprint="fingerprint-1",
                output_json=claimed_output,
                status=pending_status,
            )
            if claimed:
                await asyncio.sleep(0.05)
            await session.commit()
            return claimed

        session_one = session_factory()
        session_two = session_factory()
        assert session_one is not session_two
        try:
            claims = await asyncio.gather(
                claim(session_one),
                claim(session_two),
            )
        finally:
            await session_one.close()
            await session_two.close()

        assert sorted(rowcounts) == [0, 1]
        assert sorted(claims) == [False, True]

        async with session_factory() as session:
            persisted = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT status, output_json
                        FROM workflow_steps
                        WHERE id = :step_id
                        """
                        ),
                        {"step_id": step_id},
                    )
                )
                .mappings()
                .one()
            )
        assert persisted["status"] == "compositing"
        assert persisted["output_json"] == claimed_output
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
