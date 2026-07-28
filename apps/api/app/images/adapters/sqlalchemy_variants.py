from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen_core.models import Image, ImageVariant, ImageVariantClaim

from ..domain.artifact import ArtifactStatus
from ..ports.variant_repository import (
    VariantClaim,
    VariantLookup,
    VariantRecord,
    VariantSource,
)


def _source(row: Image) -> VariantSource:
    return VariantSource(
        image_id=row.id,
        user_id=row.user_id,
        storage_key=row.storage_key,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        width=row.width,
        height=row.height,
    )


def _variant(row: ImageVariant) -> VariantRecord:
    return VariantRecord(
        image_id=row.image_id,
        kind=row.kind,
        storage_key=row.storage_key,
        width=row.width,
        height=row.height,
    )


class SQLAlchemyVariantRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.session_factory = session_factory

    async def lookup(
        self,
        image_id: str,
        kind: str,
        *,
        expected_user_id: str | None = None,
    ) -> VariantLookup:
        conditions = [
            Image.id == image_id,
            Image.deleted_at.is_(None),
            Image.artifact_status == ArtifactStatus.READY.value,
        ]
        if expected_user_id is not None:
            conditions.append(Image.user_id == expected_user_id)
        async with self.session_factory() as session:
            image = (
                await session.execute(select(Image).where(*conditions))
            ).scalar_one_or_none()
            if image is None:
                return VariantLookup(source=None, variant=None)
            variant = (
                await session.execute(
                    select(ImageVariant).where(
                        ImageVariant.image_id == image_id,
                        ImageVariant.kind == kind,
                    )
                )
            ).scalar_one_or_none()
            return VariantLookup(
                source=_source(image),
                variant=_variant(variant) if variant is not None else None,
            )

    async def try_claim(
        self,
        source: VariantSource,
        kind: str,
        *,
        token: str,
        lease_until: datetime,
        now: datetime,
    ) -> VariantClaim | None:
        async with self.session_factory() as session:
            current = (
                await session.execute(
                    select(Image.id).where(
                        Image.id == source.image_id,
                        Image.storage_key == source.storage_key,
                        Image.sha256 == source.sha256,
                        Image.deleted_at.is_(None),
                        Image.artifact_status == ArtifactStatus.READY.value,
                    )
                )
            ).scalar_one_or_none()
            if current is None:
                await session.rollback()
                return None
            claimed = await session.execute(
                update(ImageVariantClaim)
                .where(
                    ImageVariantClaim.image_id == source.image_id,
                    ImageVariantClaim.kind == kind,
                    ImageVariantClaim.lease_until <= now,
                    or_(
                        ImageVariantClaim.retry_at.is_(None),
                        ImageVariantClaim.retry_at <= now,
                    ),
                )
                .values(
                    token=token,
                    source_key=source.storage_key,
                    source_sha256=source.sha256,
                    lease_until=lease_until,
                    retry_at=None,
                    error_code=None,
                    updated_at=now,
                )
            )
            if isinstance(claimed.rowcount, int) and claimed.rowcount == 1:
                await session.commit()
                return VariantClaim(
                    image_id=source.image_id,
                    kind=kind,
                    token=token,
                    source_key=source.storage_key,
                    source_sha256=source.sha256,
                )
            session.add(
                ImageVariantClaim(
                    image_id=source.image_id,
                    kind=kind,
                    token=token,
                    source_key=source.storage_key,
                    source_sha256=source.sha256,
                    lease_until=lease_until,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            return VariantClaim(
                image_id=source.image_id,
                kind=kind,
                token=token,
                source_key=source.storage_key,
                source_sha256=source.sha256,
            )

    async def renew_claim(
        self,
        claim: VariantClaim,
        *,
        lease_until: datetime,
        now: datetime,
    ) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(
                update(ImageVariantClaim)
                .where(
                    ImageVariantClaim.image_id == claim.image_id,
                    ImageVariantClaim.kind == claim.kind,
                    ImageVariantClaim.token == claim.token,
                    ImageVariantClaim.source_key == claim.source_key,
                    ImageVariantClaim.source_sha256 == claim.source_sha256,
                    ImageVariantClaim.lease_until > now,
                )
                .values(lease_until=lease_until, updated_at=now)
            )
            await session.commit()
            return isinstance(result.rowcount, int) and result.rowcount == 1

    async def finalize(
        self,
        claim: VariantClaim,
        variant: VariantRecord,
        *,
        now: datetime,
    ) -> VariantRecord | None:
        if variant.image_id != claim.image_id or variant.kind != claim.kind:
            raise ValueError("variant result does not match its claim")
        async with self.session_factory() as session:
            claim_row = (
                await session.execute(
                    select(ImageVariantClaim)
                    .where(
                        ImageVariantClaim.image_id == claim.image_id,
                        ImageVariantClaim.kind == claim.kind,
                        ImageVariantClaim.token == claim.token,
                        ImageVariantClaim.source_key == claim.source_key,
                        ImageVariantClaim.source_sha256 == claim.source_sha256,
                        ImageVariantClaim.lease_until > now,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if claim_row is None:
                await session.rollback()
                return None
            image = (
                await session.execute(
                    select(Image)
                    .where(
                        Image.id == claim.image_id,
                        Image.storage_key == claim.source_key,
                        Image.sha256 == claim.source_sha256,
                        Image.deleted_at.is_(None),
                        Image.artifact_status == ArtifactStatus.READY.value,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if image is None:
                await session.rollback()
                return None
            row = (
                await session.execute(
                    select(ImageVariant)
                    .where(
                        ImageVariant.image_id == claim.image_id,
                        ImageVariant.kind == claim.kind,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                row = ImageVariant(
                    image_id=variant.image_id,
                    kind=variant.kind,
                    storage_key=variant.storage_key,
                    width=variant.width,
                    height=variant.height,
                )
                session.add(row)
            else:
                row.storage_key = variant.storage_key
                row.width = variant.width
                row.height = variant.height
            try:
                await session.flush()
                result = _variant(row)
                await session.execute(
                    delete(ImageVariantClaim).where(
                        ImageVariantClaim.image_id == claim.image_id,
                        ImageVariantClaim.kind == claim.kind,
                        ImageVariantClaim.token == claim.token,
                        ImageVariantClaim.source_key == claim.source_key,
                        ImageVariantClaim.source_sha256 == claim.source_sha256,
                    )
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                lookup = await self.lookup(claim.image_id, claim.kind)
                return lookup.variant
            return result

    async def fail(
        self,
        claim: VariantClaim,
        *,
        error_code: str,
        retry_at: datetime,
        now: datetime,
    ) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(ImageVariantClaim)
                .where(
                    ImageVariantClaim.image_id == claim.image_id,
                    ImageVariantClaim.kind == claim.kind,
                    ImageVariantClaim.token == claim.token,
                    ImageVariantClaim.source_key == claim.source_key,
                    ImageVariantClaim.source_sha256 == claim.source_sha256,
                )
                .values(
                    lease_until=now,
                    retry_at=retry_at,
                    error_code=error_code[:64],
                    updated_at=now,
                )
            )
            await session.commit()
