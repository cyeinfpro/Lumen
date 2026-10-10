from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lumen_core.models import Base, User, Video
from app.services.video_preparation import (
    claim_preparation,
    finish_preparation,
    pending_preparation,
)


@asynccontextmanager
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[User.__table__, Video.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        row = Video(
            id="video-1",
            user_id="user-1",
            storage_key="upload/a.mp4",
            sha256="a" * 64,
            size_bytes=10,
            etag="a" * 64,
            metadata_jsonb={
                "source": "uploaded_reference",
                "unrelated": "preserve",
                "canvas_preparation": pending_preparation("a" * 64),
            },
        )
        db.add(row)
        await db.commit()
        yield db, row
    await engine.dispose()


@pytest.mark.asyncio
async def test_claim_is_durable_and_not_reclaimed_before_lease_expiry():
    async with session() as (db, row):
        claim = await claim_preparation(db, row.id, now=100)
        assert claim is not None
        assert row.metadata_jsonb["canvas_preparation"]["state"] == "preparing"
        assert await claim_preparation(db, row.id, now=110) is None
        reclaimed = await claim_preparation(db, row.id, now=250)
        assert reclaimed.token != claim.token
        assert not await finish_preparation(db, claim, error_code="old", now=251)


@pytest.mark.asyncio
async def test_success_publishes_trusted_metadata_without_touching_original():
    async with session() as (db, row):
        claim = await claim_preparation(db, row.id, now=100)
        result = await finish_preparation(
            db,
            claim,
            now=110,
            inspected={
                "width": 1280,
                "height": 720,
                "duration_ms": 5000,
                "fps": 24.0,
                "has_audio": True,
            },
        )
        assert result
        assert (row.width, row.height, row.duration_ms) == (1280, 720, 5000)
        assert row.metadata_jsonb["canvas_preparation"]["state"] == "ready"
        assert row.metadata_jsonb["unrelated"] == "preserve"
        assert row.storage_key == "upload/a.mp4"
        assert row.sha256 == "a" * 64
        assert row.size_bytes == 10
        assert await claim_preparation(db, row.id, now=300) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "delete", "expire"])
async def test_stale_or_deleted_original_cannot_accept_metadata(change):
    async with session() as (db, row):
        claim = await claim_preparation(db, row.id, now=100)
        now = 110
        if change == "source":
            row.sha256 = "b" * 64
        elif change == "delete":
            from datetime import datetime, timezone

            row.deleted_at = datetime.now(timezone.utc)
        else:
            now = 221
        await db.commit()
        assert not await finish_preparation(
            db,
            claim,
            now=now,
            inspected={
                "width": 1280,
                "height": 720,
                "duration_ms": 5000,
            },
        )


@pytest.mark.asyncio
async def test_failed_probe_is_durable_and_not_treated_as_ready():
    async with session() as (db, row):
        claim = await claim_preparation(db, row.id, now=100)
        assert await finish_preparation(db, claim, now=110, error_code="invalid_video")
        assert row.metadata_jsonb["canvas_preparation"]["state"] == "failed"
        assert row.width == 0
        assert await claim_preparation(db, row.id, now=300) is None
