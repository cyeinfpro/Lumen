from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen_core.models import Image

from ..domain.artifact import ArtifactStatus, ensure_artifact_transition


class ArtifactCommitError(RuntimeError):
    pass


class ArtifactTransitionConflict(RuntimeError):
    pass


def _reconcile_candidate_condition(
    status_column: Any,
    reconcile_after_column: Any,
    updated_at_column: Any,
    *,
    due_before: datetime,
    stale_before: datetime,
) -> Any:
    """Select only resumable/due rows; healthy old READY rows are not work.

    STAGING/PROCESSING/PUBLISHING can be recovered when stale even if an older
    writer failed before setting ``reconcile_after``. READY rows are selected
    only when another subsystem explicitly schedules an integrity repair.
    """

    stale_incomplete = and_(
        status_column.in_(
            [
                ArtifactStatus.STAGING.value,
                ArtifactStatus.PROCESSING.value,
                ArtifactStatus.PUBLISHING.value,
            ]
        ),
        updated_at_column <= stale_before,
    )
    scheduled = and_(
        status_column.in_(
            [
                ArtifactStatus.PUBLISHING.value,
                ArtifactStatus.READY.value,
            ]
        ),
        reconcile_after_column.is_not(None),
        reconcile_after_column <= due_before,
    )
    return or_(scheduled, stale_incomplete)


def _reconcile_priority(status_column: Any) -> Any:
    return case(
        (status_column == ArtifactStatus.PUBLISHING.value, 0),
        (status_column == ArtifactStatus.STAGING.value, 1),
        (status_column == ArtifactStatus.PROCESSING.value, 2),
        (status_column == ArtifactStatus.READY.value, 3),
        else_=4,
    )


class SQLAlchemyImageRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.session_factory = session_factory

    async def get(self, image_id: str) -> Image | None:
        async with self.session_factory() as session:
            return await session.get(Image, image_id)

    async def _resolve_commit(
        self,
        session: AsyncSession,
        *,
        image_id: str,
        target_status: ArtifactStatus,
    ) -> Image:
        try:
            await session.commit()
            row = await session.get(Image, image_id)
            if row is None:
                raise ArtifactCommitError("image row disappeared after commit")
            return row
        except Exception as exc:
            try:
                await session.rollback()
            except Exception:
                pass
            async with self.session_factory() as recovery:
                row = await recovery.get(Image, image_id)
                if row is not None and row.artifact_status == target_status.value:
                    return row
            raise ArtifactCommitError(
                f"image artifact commit not observed for {target_status.value}"
            ) from exc

    async def create_staging(self, image: Image) -> Image:
        if image.artifact_status != ArtifactStatus.STAGING.value:
            raise ValueError("new upload image must start in staging")
        async with self.session_factory() as session:
            session.add(image)
            await session.flush()
            return await self._resolve_commit(
                session,
                image_id=image.id,
                target_status=ArtifactStatus.STAGING,
            )

    async def transition(
        self,
        image_id: str,
        *,
        expected: Iterable[ArtifactStatus],
        target: ArtifactStatus,
        values: dict[str, Any] | None = None,
    ) -> Image:
        expected_set = frozenset(expected)
        if not expected_set:
            raise ValueError("artifact transition requires an expected status")
        for current in expected_set:
            if current != target:
                ensure_artifact_transition(current, target)
        payload = dict(values or {})
        payload["artifact_status"] = target.value
        payload["updated_at"] = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            result = await session.execute(
                update(Image)
                .where(
                    Image.id == image_id,
                    Image.artifact_status.in_(
                        [status.value for status in expected_set]
                    ),
                )
                .values(**payload)
            )
            if not isinstance(result.rowcount, int) or result.rowcount != 1:
                await session.rollback()
                row = await session.get(Image, image_id)
                if row is not None and row.artifact_status == target.value:
                    return row
                raise ArtifactTransitionConflict(
                    f"image artifact transition conflict image_id={image_id}"
                )
            return await self._resolve_commit(
                session,
                image_id=image_id,
                target_status=target,
            )

    async def update_publishing(
        self,
        image_id: str,
        *,
        values: dict[str, Any],
    ) -> Image:
        async with self.session_factory() as session:
            result = await session.execute(
                update(Image)
                .where(
                    Image.id == image_id,
                    Image.artifact_status == ArtifactStatus.PUBLISHING.value,
                )
                .values(**values, updated_at=datetime.now(timezone.utc))
            )
            if not isinstance(result.rowcount, int) or result.rowcount != 1:
                await session.rollback()
                raise ArtifactTransitionConflict(
                    f"image publishing update conflict image_id={image_id}"
                )
            return await self._resolve_commit(
                session,
                image_id=image_id,
                target_status=ArtifactStatus.PUBLISHING,
            )

    async def update_ready(
        self,
        image_id: str,
        *,
        values: dict[str, Any],
    ) -> Image:
        async with self.session_factory() as session:
            result = await session.execute(
                update(Image)
                .where(
                    Image.id == image_id,
                    Image.artifact_status == ArtifactStatus.READY.value,
                )
                .values(**values, updated_at=datetime.now(timezone.utc))
            )
            if not isinstance(result.rowcount, int) or result.rowcount != 1:
                await session.rollback()
                raise ArtifactTransitionConflict(
                    f"image ready update conflict image_id={image_id}"
                )
            return await self._resolve_commit(
                session,
                image_id=image_id,
                target_status=ArtifactStatus.READY,
            )

    async def list_reconcile_candidates(
        self,
        *,
        due_before: datetime,
        stale_before: datetime,
        limit: int,
    ) -> list[Image]:
        condition = _reconcile_candidate_condition(
            Image.artifact_status,
            Image.reconcile_after,
            Image.updated_at,
            due_before=due_before,
            stale_before=stale_before,
        )
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(Image)
                    .where(condition)
                    .order_by(
                        _reconcile_priority(Image.artifact_status),
                        case((Image.reconcile_after.is_(None), 1), else_=0),
                        Image.reconcile_after.asc(),
                        Image.updated_at.asc(),
                        Image.id.asc(),
                    )
                    .limit(limit)
                )
            ).scalars()
            return list(rows)

    async def active_upload_tickets(self) -> set[str]:
        async with self.session_factory() as session:
            manifests = (
                await session.execute(
                    select(Image.artifact_manifest_jsonb).where(
                        Image.artifact_status.in_(
                            [
                                ArtifactStatus.STAGING.value,
                                ArtifactStatus.PROCESSING.value,
                                ArtifactStatus.PUBLISHING.value,
                            ]
                        )
                    )
                )
            ).scalars()
            tickets: set[str] = set()
            for manifest in manifests:
                if not isinstance(manifest, dict):
                    continue
                ticket = manifest.get("ticket")
                if isinstance(ticket, str) and ticket:
                    tickets.add(ticket)
            return tickets
