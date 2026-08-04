from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, AsyncContextManager, Iterable

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen_core.models import Image
from lumen_core.model_entities.media_workflows import ImageReconcileEpoch

from ...services.active_user import lock_active_user
from ..domain.artifact import (
    ArtifactManifestItem,
    ArtifactStatus,
    ensure_artifact_transition,
)
from .sqlalchemy_reconcile_cleanup import reconcile_publish_cleanup_guard


class ArtifactCommitError(RuntimeError):
    pass


class ArtifactTransitionConflict(RuntimeError):
    pass


_STORAGE_INTENT_KEY = "storage_intent"
_STORAGE_INTENT_VERSION = 1
_STORAGE_INTENT_PENDING = "pending"
_STORAGE_INTENT_ADOPTED = "adopted"
_UPLOAD_ARTIFACT_NAMES = ("original", "normalized_ref")


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


def _validate_storage_intent_token(token: str) -> None:
    if not token or len(token) > 256:
        raise ValueError("invalid image storage intent token")


def _storage_intent_matches(
    manifest: Any,
    *,
    user_id: str,
    image_id: str,
    token: str,
    state: str,
) -> bool:
    if not isinstance(manifest, dict):
        return False
    intent = manifest.get(_STORAGE_INTENT_KEY)
    if not isinstance(intent, dict):
        return False
    actual_token = intent.get("token")
    return (
        intent.get("version") == _STORAGE_INTENT_VERSION
        and intent.get("state") == state
        and intent.get("user_id") == user_id
        and intent.get("image_id") == image_id
        and isinstance(actual_token, str)
        and secrets.compare_digest(actual_token, token)
    )


def _storage_intent_payload(
    *,
    user_id: str,
    image_id: str,
    token: str,
    state: str,
) -> dict[str, Any]:
    return {
        "version": _STORAGE_INTENT_VERSION,
        "state": state,
        "user_id": user_id,
        "image_id": image_id,
        "token": token,
    }


def _manifest_items(
    manifest: Any,
) -> tuple[ArtifactManifestItem, ArtifactManifestItem] | None:
    if not isinstance(manifest, dict):
        return None
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    original = ArtifactManifestItem.from_json(artifacts.get("original"))
    normalized_ref = ArtifactManifestItem.from_json(artifacts.get("normalized_ref"))
    if original is None or normalized_ref is None:
        return None
    return original, normalized_ref


def _manifest_artifacts_match(expected: Any, actual: Any) -> bool:
    expected_items = _manifest_items(expected)
    actual_items = _manifest_items(actual)
    if expected_items is None or actual_items is None:
        return False
    if expected.get("version") != actual.get("version") or expected.get(
        "ticket"
    ) != actual.get("ticket"):
        return False
    for expected_item, actual_item in zip(
        expected_items,
        actual_items,
        strict=True,
    ):
        if (
            expected_item.key != actual_item.key
            or expected_item.mime != actual_item.mime
            or expected_item.required != actual_item.required
            or expected_item.identity.sha256 != actual_item.identity.sha256
            or expected_item.identity.size_bytes != actual_item.identity.size_bytes
        ):
            return False
    return True


def _upload_manifest_is_owned(
    manifest: Any,
    *,
    user_id: str,
    image_id: str,
    storage_key: str | None,
) -> bool:
    items = _manifest_items(manifest)
    if items is None:
        return False
    original, normalized_ref = items
    original_prefix = f"u/{user_id}/uploads/{image_id}."
    original_suffix = original.key.value.removeprefix(original_prefix)
    return (
        original.key.value.startswith(original_prefix)
        and bool(original_suffix)
        and "/" not in original_suffix
        and "." not in original_suffix
        and (storage_key is None or original.key.value == storage_key)
        and normalized_ref.key.value == f"u/{user_id}/uploads/{image_id}.ref.webp"
        and original.key != normalized_ref.key
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

    @staticmethod
    async def _lock_live_upload(
        session: AsyncSession,
        *,
        image_id: str,
        user_id: str,
    ) -> Image | None:
        return (
            await session.execute(
                select(Image)
                .where(
                    Image.id == image_id,
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def create_storage_intent(
        self,
        image_id: str,
        *,
        user_id: str,
        token: str,
        session_id: str | None = None,
    ) -> Image:
        _validate_storage_intent_token(token)
        async with self.session_factory() as session:
            if session_id:
                await lock_active_user(session, user_id, session_id=session_id)
            else:
                await lock_active_user(session, user_id)
            row = await self._lock_live_upload(
                session,
                image_id=image_id,
                user_id=user_id,
            )
            if (
                row is None
                or row.artifact_status != ArtifactStatus.PUBLISHING.value
                or int(row.reconcile_fence or 0) != 0
            ):
                await session.rollback()
                raise ArtifactTransitionConflict(
                    f"image storage intent conflict image_id={image_id}"
                )
            manifest = dict(row.artifact_manifest_jsonb or {})
            if _storage_intent_matches(
                manifest,
                user_id=user_id,
                image_id=image_id,
                token=token,
                state=_STORAGE_INTENT_PENDING,
            ):
                return await self._resolve_commit(
                    session,
                    image_id=image_id,
                    target_status=ArtifactStatus.PUBLISHING,
                    expected_values={
                        "user_id": user_id,
                        "artifact_manifest_jsonb": manifest,
                        "deleted_at": None,
                    },
                    expected_reconcile_fence=0,
                )
            if _STORAGE_INTENT_KEY in manifest or not _upload_manifest_is_owned(
                manifest,
                user_id=user_id,
                image_id=image_id,
                storage_key=row.storage_key,
            ):
                await session.rollback()
                raise ArtifactTransitionConflict(
                    f"image storage intent payload conflict image_id={image_id}"
                )
            manifest[_STORAGE_INTENT_KEY] = _storage_intent_payload(
                user_id=user_id,
                image_id=image_id,
                token=token,
                state=_STORAGE_INTENT_PENDING,
            )
            updated_at = datetime.now(timezone.utc)
            row.artifact_manifest_jsonb = manifest
            row.updated_at = updated_at
            return await self._resolve_commit(
                session,
                image_id=image_id,
                target_status=ArtifactStatus.PUBLISHING,
                expected_values={
                    "user_id": user_id,
                    "artifact_manifest_jsonb": manifest,
                    "updated_at": updated_at,
                    "deleted_at": None,
                },
                expected_reconcile_fence=0,
            )

    async def adopt_storage_intent(
        self,
        image_id: str,
        *,
        user_id: str,
        token: str,
        manifest: Mapping[str, Any],
        ready_at: datetime,
        session_id: str | None = None,
    ) -> Image:
        _validate_storage_intent_token(token)
        adopted_manifest = dict(manifest)
        adopted_manifest.pop(_STORAGE_INTENT_KEY, None)
        if not _upload_manifest_is_owned(
            adopted_manifest,
            user_id=user_id,
            image_id=image_id,
            storage_key=None,
        ):
            raise ValueError("invalid adopted image artifact manifest")
        async with self.session_factory() as session:
            if session_id:
                await lock_active_user(session, user_id, session_id=session_id)
            else:
                await lock_active_user(session, user_id)
            row = await self._lock_live_upload(
                session,
                image_id=image_id,
                user_id=user_id,
            )
            if row is None or int(row.reconcile_fence or 0) != 0:
                await session.rollback()
                raise ArtifactTransitionConflict(
                    f"image storage adoption conflict image_id={image_id}"
                )
            if (
                row.artifact_status == ArtifactStatus.READY.value
                and _storage_intent_matches(
                    row.artifact_manifest_jsonb,
                    user_id=user_id,
                    image_id=image_id,
                    token=token,
                    state=_STORAGE_INTENT_ADOPTED,
                )
                and _manifest_artifacts_match(
                    row.artifact_manifest_jsonb,
                    adopted_manifest,
                )
            ):
                return await self._resolve_commit(
                    session,
                    image_id=image_id,
                    target_status=ArtifactStatus.READY,
                    expected_values={
                        "user_id": user_id,
                        "artifact_manifest_jsonb": row.artifact_manifest_jsonb,
                        "deleted_at": None,
                    },
                    expected_reconcile_fence=0,
                )
            if (
                row.artifact_status
                not in {
                    ArtifactStatus.PUBLISHING.value,
                    ArtifactStatus.READY.value,
                }
                or not _storage_intent_matches(
                    row.artifact_manifest_jsonb,
                    user_id=user_id,
                    image_id=image_id,
                    token=token,
                    state=_STORAGE_INTENT_PENDING,
                )
                or not _manifest_artifacts_match(
                    row.artifact_manifest_jsonb,
                    adopted_manifest,
                )
                or not _upload_manifest_is_owned(
                    adopted_manifest,
                    user_id=user_id,
                    image_id=image_id,
                    storage_key=row.storage_key,
                )
            ):
                await session.rollback()
                raise ArtifactTransitionConflict(
                    f"image storage adoption token conflict image_id={image_id}"
                )
            if row.artifact_status == ArtifactStatus.PUBLISHING.value:
                ensure_artifact_transition(
                    ArtifactStatus.PUBLISHING,
                    ArtifactStatus.READY,
                )
            updated_at = datetime.now(timezone.utc)
            adopted_manifest[_STORAGE_INTENT_KEY] = _storage_intent_payload(
                user_id=user_id,
                image_id=image_id,
                token=token,
                state=_STORAGE_INTENT_ADOPTED,
            )
            payload = {
                "artifact_status": ArtifactStatus.READY.value,
                "artifact_manifest_jsonb": adopted_manifest,
                "reconcile_after": None,
                "last_artifact_error": None,
                "ready_at": ready_at,
                "updated_at": updated_at,
                "reconcile_fence": 0,
            }
            for name, value in payload.items():
                setattr(row, name, value)
            return await self._resolve_commit(
                session,
                image_id=image_id,
                target_status=ArtifactStatus.READY,
                expected_values={
                    **payload,
                    "user_id": user_id,
                    "deleted_at": None,
                },
                expected_reconcile_fence=0,
            )

    async def record_storage_intent_failure(
        self,
        image_id: str,
        *,
        user_id: str,
        token: str,
        error_message: str,
        retry_at: datetime | None,
    ) -> bool:
        _validate_storage_intent_token(token)
        async with self.session_factory() as session:
            row = await self._lock_live_upload(
                session,
                image_id=image_id,
                user_id=user_id,
            )
            if (
                row is None
                or row.artifact_status != ArtifactStatus.PUBLISHING.value
                or int(row.reconcile_fence or 0) != 0
                or not _storage_intent_matches(
                    row.artifact_manifest_jsonb,
                    user_id=user_id,
                    image_id=image_id,
                    token=token,
                    state=_STORAGE_INTENT_PENDING,
                )
            ):
                await session.rollback()
                return False
            updated_at = datetime.now(timezone.utc)
            expected_values: dict[str, Any] = {
                "last_artifact_error": error_message[:2000],
                "updated_at": updated_at,
                "artifact_manifest_jsonb": row.artifact_manifest_jsonb,
                "deleted_at": None,
            }
            row.last_artifact_error = error_message[:2000]
            row.updated_at = updated_at
            if retry_at is not None:
                row.reconcile_after = retry_at
                expected_values["reconcile_after"] = retry_at
            await self._resolve_commit(
                session,
                image_id=image_id,
                target_status=ArtifactStatus.PUBLISHING,
                expected_values=expected_values,
                expected_reconcile_fence=0,
            )
            return True

    async def abandon_storage_intent(
        self,
        image_id: str,
        *,
        user_id: str,
        token: str,
        error_message: str,
    ) -> bool:
        _validate_storage_intent_token(token)
        async with self.session_factory() as session:
            row = await self._lock_live_upload(
                session,
                image_id=image_id,
                user_id=user_id,
            )
            if (
                row is None
                or row.artifact_status != ArtifactStatus.PUBLISHING.value
                or int(row.reconcile_fence or 0) != 0
                or not _storage_intent_matches(
                    row.artifact_manifest_jsonb,
                    user_id=user_id,
                    image_id=image_id,
                    token=token,
                    state=_STORAGE_INTENT_PENDING,
                )
            ):
                await session.rollback()
                return False
            ensure_artifact_transition(
                ArtifactStatus.PUBLISHING,
                ArtifactStatus.FAILED,
            )
            abandoned_at = datetime.now(timezone.utc)
            payload = {
                "artifact_status": ArtifactStatus.FAILED.value,
                "deleted_at": abandoned_at,
                "last_artifact_error": error_message[:2000],
                "reconcile_after": None,
                "updated_at": abandoned_at,
                "reconcile_fence": 0,
            }
            for name, value in payload.items():
                setattr(row, name, value)
            await self._resolve_commit(
                session,
                image_id=image_id,
                target_status=ArtifactStatus.FAILED,
                expected_values={
                    **payload,
                    "user_id": user_id,
                },
                expected_reconcile_fence=0,
            )
            return True

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

    def guard_reconcile_publish_cleanup(
        self,
        image_id: str,
        *,
        stale_fence: int,
    ) -> AsyncContextManager[bool]:
        return reconcile_publish_cleanup_guard(
            self.session_factory,
            image_id,
            stale_fence=stale_fence,
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
