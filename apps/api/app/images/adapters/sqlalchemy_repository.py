from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen_core.models import Image
from lumen_core.model_entities.media_workflows import ImageReconcileEpoch

from ...services.active_user import lock_active_user
from ..domain.artifact import ArtifactStatus, ensure_artifact_transition


class ArtifactCommitError(RuntimeError):
    pass


class ArtifactTransitionConflict(RuntimeError):
    pass


def _normalized_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _commit_value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(actual, datetime) and isinstance(expected, datetime):
        return _normalized_datetime(actual) == _normalized_datetime(expected)
    return actual == expected


def _row_matches_commit(
    row: Image,
    *,
    target_status: ArtifactStatus,
    expected_values: Mapping[str, Any],
    expected_reconcile_fence: int,
) -> bool:
    if row.artifact_status != target_status.value:
        return False
    if int(getattr(row, "reconcile_fence", 0) or 0) != expected_reconcile_fence:
        return False
    return all(
        _commit_value_matches(getattr(row, key, None), expected)
        for key, expected in expected_values.items()
    )


def _reconcile_candidate_condition(
    status_column: Any,
    reconcile_after_column: Any,
    updated_at_column: Any,
    *,
    due_before: datetime,
    stale_before: datetime,
    quarantined_at_column: Any | None = None,
) -> Any:
    """Select only resumable/due rows; healthy old READY rows are not work.

    STAGING/PROCESSING rows can be recovered once stale. PUBLISHING rows with
    an explicit future schedule must wait for that schedule; only legacy rows
    missing ``reconcile_after`` fall back to the stale timeout. READY rows are
    selected only when another subsystem explicitly schedules an integrity
    repair.
    """

    stale_early_phase = and_(
        status_column.in_(
            [
                ArtifactStatus.STAGING.value,
                ArtifactStatus.PROCESSING.value,
            ]
        ),
        updated_at_column <= stale_before,
    )
    stale_unscheduled_publish = and_(
        status_column == ArtifactStatus.PUBLISHING.value,
        reconcile_after_column.is_(None),
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
    condition = or_(scheduled, stale_early_phase, stale_unscheduled_publish)
    if quarantined_at_column is not None:
        condition = and_(quarantined_at_column.is_(None), condition)
    return condition


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

    @asynccontextmanager
    async def active_user_fence(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
    ) -> AsyncIterator[None]:
        """Retain the identity fence across durable file publication."""
        async with self.session_factory() as session:
            if session_id:
                await lock_active_user(session, user_id, session_id=session_id)
            else:
                await lock_active_user(session, user_id)
            try:
                yield
            finally:
                await session.rollback()

    async def next_reconcile_fence(self) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                update(ImageReconcileEpoch)
                .where(ImageReconcileEpoch.id == 1)
                .values(value=ImageReconcileEpoch.value + 1)
                .returning(ImageReconcileEpoch.value)
            )
            value = result.scalar_one_or_none()
            if value is not None:
                await session.commit()
                return int(value)
            session.add(ImageReconcileEpoch(id=1, value=1))
            try:
                await session.commit()
                return 1
            except IntegrityError:
                await session.rollback()
            result = await session.execute(
                update(ImageReconcileEpoch)
                .where(ImageReconcileEpoch.id == 1)
                .values(value=ImageReconcileEpoch.value + 1)
                .returning(ImageReconcileEpoch.value)
            )
            value = result.scalar_one_or_none()
            if value is None:
                await session.rollback()
                raise ArtifactCommitError("reconcile fence row is missing")
            await session.commit()
            return int(value)

    async def _resolve_commit(
        self,
        session: AsyncSession,
        *,
        image_id: str,
        target_status: ArtifactStatus,
        expected_values: Mapping[str, Any] | None = None,
        expected_reconcile_fence: int = 0,
    ) -> Image:
        expected = dict(expected_values or {})
        try:
            await session.commit()
            row = await session.get(Image, image_id)
            if row is not None and _row_matches_commit(
                row,
                target_status=target_status,
                expected_values=expected,
                expected_reconcile_fence=expected_reconcile_fence,
            ):
                return row
            raise ArtifactCommitError(
                "image row did not match committed artifact state"
            )
        except Exception as exc:
            try:
                await session.rollback()
            except Exception:
                pass
            async with self.session_factory() as recovery:
                row = await recovery.get(Image, image_id)
                if row is not None and _row_matches_commit(
                    row,
                    target_status=target_status,
                    expected_values=expected,
                    expected_reconcile_fence=expected_reconcile_fence,
                ):
                    return row
            raise ArtifactCommitError(
                f"image artifact commit not observed for {target_status.value}"
            ) from exc

    async def create_staging(
        self,
        image: Image,
        *,
        session_id: str | None = None,
    ) -> Image:
        if image.artifact_status != ArtifactStatus.STAGING.value:
            raise ValueError("new upload image must start in staging")
        async with self.session_factory() as session:
            if session_id:
                await lock_active_user(
                    session,
                    image.user_id,
                    session_id=session_id,
                )
            else:
                await lock_active_user(session, image.user_id)
            session.add(image)
            await session.flush()
            return await self._resolve_commit(
                session,
                image_id=image.id,
                target_status=ArtifactStatus.STAGING,
                expected_values={
                    "user_id": image.user_id,
                    "storage_key": image.storage_key,
                    "sha256": image.sha256,
                    "artifact_manifest_jsonb": image.artifact_manifest_jsonb,
                    "deleted_at": image.deleted_at,
                },
                expected_reconcile_fence=int(image.reconcile_fence or 0),
            )

    async def transition(
        self,
        image_id: str,
        *,
        expected: Iterable[ArtifactStatus],
        target: ArtifactStatus,
        values: dict[str, Any] | None = None,
        reconcile_fence: int | None = None,
        active_user_id: str | None = None,
        session_id: str | None = None,
    ) -> Image:
        if session_id and not active_user_id:
            raise ValueError("session_id requires active_user_id")
        expected_set = frozenset(expected)
        if not expected_set:
            raise ValueError("artifact transition requires an expected status")
        for current in expected_set:
            if current != target:
                ensure_artifact_transition(current, target)
        payload = dict(values or {})
        payload["artifact_status"] = target.value
        payload["updated_at"] = datetime.now(timezone.utc)
        payload["reconcile_fence"] = 0
        conditions = [
            Image.id == image_id,
            Image.deleted_at.is_(None),
            Image.artifact_status.in_([status.value for status in expected_set]),
        ]
        if reconcile_fence is not None:
            if reconcile_fence <= 0:
                raise ValueError("reconcile fence must be positive")
            conditions.append(Image.reconcile_fence == reconcile_fence)
        else:
            conditions.append(Image.reconcile_fence == 0)
        async with self.session_factory() as session:
            if active_user_id:
                if session_id:
                    await lock_active_user(
                        session,
                        active_user_id,
                        session_id=session_id,
                    )
                else:
                    await lock_active_user(session, active_user_id)
            result = await session.execute(
                update(Image).where(*conditions).values(**payload)
            )
            if not isinstance(result.rowcount, int) or result.rowcount != 1:
                await session.rollback()
                row = await session.get(Image, image_id)
                if (
                    reconcile_fence is None
                    and row is not None
                    and _row_matches_commit(
                        row,
                        target_status=target,
                        expected_values={
                            **dict(values or {}),
                            "deleted_at": None,
                        },
                        expected_reconcile_fence=0,
                    )
                ):
                    return row
                raise ArtifactTransitionConflict(
                    f"image artifact transition conflict image_id={image_id}"
                )
            return await self._resolve_commit(
                session,
                image_id=image_id,
                target_status=target,
                expected_values={
                    **payload,
                    "deleted_at": None,
                },
                expected_reconcile_fence=0,
            )

    async def update_publishing(
        self,
        image_id: str,
        *,
        values: dict[str, Any],
    ) -> Image:
        payload = dict(values)
        payload["updated_at"] = datetime.now(timezone.utc)
        payload["reconcile_fence"] = 0
        async with self.session_factory() as session:
            result = await session.execute(
                update(Image)
                .where(
                    Image.id == image_id,
                    Image.deleted_at.is_(None),
                    Image.artifact_status == ArtifactStatus.PUBLISHING.value,
                    Image.reconcile_fence == 0,
                )
                .values(**payload)
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
                expected_values={
                    **payload,
                    "deleted_at": None,
                },
                expected_reconcile_fence=0,
            )

    async def update_ready(
        self,
        image_id: str,
        *,
        values: dict[str, Any],
        reconcile_fence: int | None = None,
    ) -> Image:
        payload = dict(values)
        payload["updated_at"] = datetime.now(timezone.utc)
        payload["reconcile_fence"] = 0
        conditions = [
            Image.id == image_id,
            Image.deleted_at.is_(None),
            Image.artifact_status == ArtifactStatus.READY.value,
        ]
        if reconcile_fence is not None:
            if reconcile_fence <= 0:
                raise ValueError("reconcile fence must be positive")
            conditions.append(Image.reconcile_fence == reconcile_fence)
        else:
            conditions.append(Image.reconcile_fence == 0)
        async with self.session_factory() as session:
            result = await session.execute(
                update(Image).where(*conditions).values(**payload)
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
                expected_values={
                    **payload,
                    "deleted_at": None,
                },
                expected_reconcile_fence=0,
            )

    async def claim_reconcile(
        self,
        image_id: str,
        *,
        expected_status: ArtifactStatus,
        expected_updated_at: datetime,
        fence: int,
    ) -> bool:
        if fence <= 0:
            raise ValueError("reconcile fence must be positive")
        async with self.session_factory() as session:
            result = await session.execute(
                update(Image)
                .where(
                    Image.id == image_id,
                    Image.deleted_at.is_(None),
                    Image.quarantined_at.is_(None),
                    Image.artifact_status == expected_status.value,
                    Image.updated_at == expected_updated_at,
                    Image.reconcile_fence < fence,
                )
                .values(
                    reconcile_fence=fence,
                    updated_at=Image.updated_at,
                )
            )
            if not isinstance(result.rowcount, int) or result.rowcount != 1:
                await session.rollback()
                return False
            await session.commit()
            return True

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
            quarantined_at_column=Image.quarantined_at,
        )
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(Image)
                    .where(
                        Image.deleted_at.is_(None),
                        condition,
                    )
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

    async def record_reconcile_failure(
        self,
        image_id: str,
        *,
        expected_status: ArtifactStatus,
        attempts: int,
        error_code: str,
        error_message: str,
        error_at: datetime,
        reconcile_after: datetime | None,
        quarantined_at: datetime | None,
        reconcile_fence: int | None = None,
    ) -> None:
        if attempts < 1:
            raise ValueError("reconcile attempts must be positive")
        conditions = [
            Image.id == image_id,
            Image.deleted_at.is_(None),
            Image.artifact_status == expected_status.value,
        ]
        if reconcile_fence is not None:
            if reconcile_fence <= 0:
                raise ValueError("reconcile fence must be positive")
            conditions.append(Image.reconcile_fence == reconcile_fence)
        else:
            conditions.append(Image.reconcile_fence == 0)
        async with self.session_factory() as session:
            result = await session.execute(
                update(Image)
                .where(*conditions)
                .values(
                    reconcile_attempts=attempts,
                    reconcile_fence=0,
                    reconcile_after=reconcile_after,
                    last_artifact_error=error_message[:2000],
                    last_reconcile_error_code=error_code[:64],
                    last_reconcile_error_at=error_at,
                    quarantined_at=quarantined_at,
                    updated_at=error_at,
                )
            )
            if not isinstance(result.rowcount, int) or result.rowcount != 1:
                await session.rollback()
                raise ArtifactTransitionConflict(
                    f"image reconcile update conflict image_id={image_id}"
                )
            await session.commit()

    async def active_upload_tickets(
        self,
        candidate_tickets: set[str] | None = None,
    ) -> set[str]:
        if candidate_tickets is not None and not candidate_tickets:
            return set()
        conditions = [
            Image.deleted_at.is_(None),
            Image.artifact_status.in_(
                [
                    ArtifactStatus.STAGING.value,
                    ArtifactStatus.PROCESSING.value,
                    ArtifactStatus.PUBLISHING.value,
                ]
            ),
        ]
        if candidate_tickets is not None:
            conditions.append(
                Image.artifact_manifest_jsonb["ticket"]
                .as_string()
                .in_(sorted(candidate_tickets))
            )
        async with self.session_factory() as session:
            manifests = (
                await session.execute(
                    select(Image.artifact_manifest_jsonb).where(*conditions)
                )
            ).scalars()
            tickets: set[str] = set()
            for manifest in manifests:
                if not isinstance(manifest, dict):
                    continue
                ticket = manifest.get("ticket")
                if (
                    isinstance(ticket, str)
                    and ticket
                    and (candidate_tickets is None or ticket in candidate_tickets)
                ):
                    tickets.add(ticket)
            return tickets
