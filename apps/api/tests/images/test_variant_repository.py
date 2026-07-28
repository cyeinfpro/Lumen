from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.images.adapters.sqlalchemy_variants import SQLAlchemyVariantRepository
from app.images.domain.artifact import ArtifactStatus
from app.images.ports.variant_repository import (
    VariantClaim,
    VariantRecord,
    VariantSource,
)
from lumen_core.models import Base, Image, ImageVariant, ImageVariantClaim


_NOW = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
_IMAGE_ID = "image-1"
_KIND = "display2048"


@asynccontextmanager
async def _repository() -> AsyncIterator[
    tuple[
        SQLAlchemyVariantRepository,
        async_sessionmaker[AsyncSession],
    ]
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    Image.__table__,
                    ImageVariant.__table__,
                    ImageVariantClaim.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield SQLAlchemyVariantRepository(factory), factory
    finally:
        await engine.dispose()


def _image() -> Image:
    return Image(
        id=_IMAGE_ID,
        user_id="user-1",
        source="uploaded",
        storage_key="u/user-1/uploads/image-1.png",
        mime="image/png",
        width=1600,
        height=900,
        size_bytes=4096,
        sha256="a" * 64,
        artifact_status=ArtifactStatus.READY.value,
    )


def _source(image: Image) -> VariantSource:
    return VariantSource(
        image_id=image.id,
        user_id=image.user_id,
        storage_key=image.storage_key,
        sha256=image.sha256,
        size_bytes=image.size_bytes,
        width=image.width,
        height=image.height,
    )


def _variant(storage_key: str) -> VariantRecord:
    return VariantRecord(
        image_id=_IMAGE_ID,
        kind=_KIND,
        storage_key=storage_key,
        width=1280,
        height=720,
    )


async def _insert_image(
    factory: async_sessionmaker[AsyncSession],
) -> Image:
    image = _image()
    async with factory() as session:
        session.add(image)
        await session.commit()
    return image


async def _claim_row(
    factory: async_sessionmaker[AsyncSession],
) -> ImageVariantClaim | None:
    async with factory() as session:
        return await session.get(
            ImageVariantClaim,
            {"image_id": _IMAGE_ID, "kind": _KIND},
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@pytest.mark.asyncio
async def test_try_claim_acquires_and_replaces_only_expired_lease() -> None:
    async with _repository() as (repository, factory):
        source = _source(await _insert_image(factory))
        first_lease = _NOW + timedelta(seconds=30)

        first = await repository.try_claim(
            source,
            _KIND,
            token="token-1",
            lease_until=first_lease,
            now=_NOW,
        )
        blocked = await repository.try_claim(
            source,
            _KIND,
            token="token-2",
            lease_until=_NOW + timedelta(seconds=60),
            now=_NOW + timedelta(seconds=29),
        )
        replacement = await repository.try_claim(
            source,
            _KIND,
            token="token-3",
            lease_until=_NOW + timedelta(seconds=90),
            now=_NOW + timedelta(seconds=31),
        )

        assert first == VariantClaim(
            image_id=_IMAGE_ID,
            kind=_KIND,
            token="token-1",
            source_key=source.storage_key,
            source_sha256=source.sha256,
        )
        assert blocked is None
        assert replacement == replace(first, token="token-3")
        row = await _claim_row(factory)
        assert row is not None
        assert row.token == "token-3"
        assert row.source_key == source.storage_key
        assert row.source_sha256 == source.sha256
        assert _as_utc(row.lease_until) == _NOW + timedelta(seconds=90)
        assert row.retry_at is None
        assert row.error_code is None


@pytest.mark.asyncio
async def test_renew_claim_requires_matching_token_and_source_identity() -> None:
    async with _repository() as (repository, factory):
        source = _source(await _insert_image(factory))
        claim = await repository.try_claim(
            source,
            _KIND,
            token="token-1",
            lease_until=_NOW + timedelta(seconds=30),
            now=_NOW,
        )
        assert claim is not None

        assert not await repository.renew_claim(
            replace(claim, token="stale-token"),
            lease_until=_NOW + timedelta(seconds=60),
            now=_NOW + timedelta(seconds=1),
        )
        assert not await repository.renew_claim(
            replace(claim, source_sha256="b" * 64),
            lease_until=_NOW + timedelta(seconds=60),
            now=_NOW + timedelta(seconds=1),
        )
        assert await repository.renew_claim(
            claim,
            lease_until=_NOW + timedelta(seconds=60),
            now=_NOW + timedelta(seconds=1),
        )

        row = await _claim_row(factory)
        assert row is not None
        assert row.token == claim.token
        assert _as_utc(row.lease_until) == _NOW + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_renew_claim_cannot_revive_expired_claim() -> None:
    async with _repository() as (repository, factory):
        source = _source(await _insert_image(factory))
        lease_until = _NOW + timedelta(seconds=30)
        claim = await repository.try_claim(
            source,
            _KIND,
            token="token-1",
            lease_until=lease_until,
            now=_NOW,
        )
        assert claim is not None

        assert not await repository.renew_claim(
            claim,
            lease_until=_NOW + timedelta(seconds=90),
            now=lease_until,
        )
        row = await _claim_row(factory)
        assert row is not None
        assert row.token == claim.token
        assert _as_utc(row.lease_until) == lease_until


@pytest.mark.asyncio
async def test_source_identity_cas_guards_claim_and_finalize() -> None:
    async with _repository() as (repository, factory):
        source = _source(await _insert_image(factory))
        assert (
            await repository.try_claim(
                replace(source, sha256="b" * 64),
                _KIND,
                token="stale-source",
                lease_until=_NOW + timedelta(seconds=30),
                now=_NOW,
            )
            is None
        )
        claim = await repository.try_claim(
            source,
            _KIND,
            token="token-1",
            lease_until=_NOW + timedelta(seconds=30),
            now=_NOW,
        )
        assert claim is not None

        assert (
            await repository.finalize(
                replace(claim, source_sha256="b" * 64),
                _variant("u/user-1/uploads/image-1.display2048.forged.webp"),
                now=_NOW + timedelta(seconds=1),
            )
            is None
        )
        async with factory() as session:
            image = await session.get(Image, _IMAGE_ID)
            assert image is not None
            image.storage_key = "u/user-1/uploads/image-1-replaced.png"
            image.sha256 = "c" * 64
            await session.commit()

        assert (
            await repository.finalize(
                claim,
                _variant("u/user-1/uploads/image-1.display2048.webp"),
                now=_NOW + timedelta(seconds=1),
            )
            is None
        )
        async with factory() as session:
            assert (
                await session.execute(select(ImageVariant))
            ).scalar_one_or_none() is None
        row = await _claim_row(factory)
        assert row is not None
        assert row.token == claim.token


@pytest.mark.asyncio
async def test_finalize_persists_newest_claim_winner_and_fences_old_token() -> None:
    async with _repository() as (repository, factory):
        source = _source(await _insert_image(factory))
        first = await repository.try_claim(
            source,
            _KIND,
            token="token-1",
            lease_until=_NOW + timedelta(seconds=10),
            now=_NOW,
        )
        assert first is not None
        winner = await repository.try_claim(
            source,
            _KIND,
            token="token-2",
            lease_until=_NOW + timedelta(seconds=60),
            now=_NOW + timedelta(seconds=11),
        )
        assert winner is not None

        losing_variant = _variant("u/user-1/uploads/image-1.display2048.loser.webp")
        winning_variant = _variant("u/user-1/uploads/image-1.display2048.webp")
        assert (
            await repository.finalize(
                first,
                losing_variant,
                now=_NOW + timedelta(seconds=12),
            )
            is None
        )
        assert (
            await repository.finalize(
                winner,
                winning_variant,
                now=_NOW + timedelta(seconds=12),
            )
            == winning_variant
        )

        lookup = await repository.lookup(_IMAGE_ID, _KIND)
        assert lookup.source == source
        assert lookup.variant == winning_variant
        assert await _claim_row(factory) is None


@pytest.mark.asyncio
async def test_finalize_rejects_variant_for_different_claim_identity() -> None:
    async with _repository() as (repository, factory):
        source = _source(await _insert_image(factory))
        claim = await repository.try_claim(
            source,
            _KIND,
            token="token-1",
            lease_until=_NOW + timedelta(seconds=30),
            now=_NOW,
        )
        assert claim is not None

        with pytest.raises(ValueError, match="does not match"):
            await repository.finalize(
                claim,
                replace(_variant("u/user-1/uploads/other.webp"), kind="thumb256"),
                now=_NOW + timedelta(seconds=1),
            )

        assert await _claim_row(factory) is not None


@pytest.mark.asyncio
async def test_fail_blocks_retry_until_retry_at_then_clears_error_state() -> None:
    async with _repository() as (repository, factory):
        source = _source(await _insert_image(factory))
        claim = await repository.try_claim(
            source,
            _KIND,
            token="token-1",
            lease_until=_NOW + timedelta(seconds=30),
            now=_NOW,
        )
        assert claim is not None
        retry_at = _NOW + timedelta(seconds=45)

        await repository.fail(
            replace(claim, source_key="u/user-1/uploads/forged.png"),
            error_code="forged",
            retry_at=retry_at,
            now=_NOW + timedelta(seconds=1),
        )
        untouched_row = await _claim_row(factory)
        assert untouched_row is not None
        assert _as_utc(untouched_row.lease_until) == _NOW + timedelta(seconds=30)
        assert untouched_row.retry_at is None
        assert untouched_row.error_code is None

        await repository.fail(
            claim,
            error_code="x" * 80,
            retry_at=retry_at,
            now=_NOW + timedelta(seconds=1),
        )

        failed_row = await _claim_row(factory)
        assert failed_row is not None
        assert failed_row.token == claim.token
        assert _as_utc(failed_row.lease_until) == _NOW + timedelta(seconds=1)
        assert _as_utc(failed_row.retry_at) == retry_at
        assert failed_row.error_code == "x" * 64
        assert (
            await repository.try_claim(
                source,
                _KIND,
                token="token-2",
                lease_until=_NOW + timedelta(seconds=90),
                now=retry_at - timedelta(seconds=1),
            )
            is None
        )

        retry_claim = await repository.try_claim(
            source,
            _KIND,
            token="token-3",
            lease_until=_NOW + timedelta(seconds=90),
            now=retry_at,
        )
        assert retry_claim == replace(claim, token="token-3")
        retried_row = await _claim_row(factory)
        assert retried_row is not None
        assert retried_row.token == "token-3"
        assert retried_row.retry_at is None
        assert retried_row.error_code is None
