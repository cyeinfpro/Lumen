from __future__ import annotations

import asyncio
import io
import logging
import os
import secrets
import tempfile
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Sequence

from lumen_core.capacity_leases import (
    CapacityLeaseGuard,
    CapacityLeaseLost,
    assert_capacity_leases_owned,
    maintained_capacity_lease,
    race_with_capacity_leases,
)
from lumen_core.storage_capacity import (
    StorageCapacityExceeded,
    StorageCapacityPort,
    StorageCapacityUnavailable,
)
from PIL import Image as PILImage, UnidentifiedImageError

from ..adapters.local_capacity import (
    CapacityExceeded,
    CapacityLimits,
    CapacityUnavailable,
)
from ..domain.artifact import ArtifactIdentity, ArtifactKey, PublishedArtifact
from ..domain.resource_estimate import estimate_image_resources
from ..domain.variants import (
    DISPLAY_VARIANT,
    VIDEO_REFERENCE_VARIANT,
    deterministic_variant_key,
)
from ..ports.artifact_store import ArtifactStorePort
from ..ports.capacity import CapacityPort
from ..ports.image_processing import (
    ImageProcessingExecutorPort,
    ImageVariantProcessingRequest,
    PreparedImageVariant,
)
from ..ports.variant_repository import (
    VariantClaim,
    VariantRecord,
    VariantRepositoryPort,
    VariantSource,
)
from ..processing.service import ProcessingError


class VariantError(ValueError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VariantResult:
    image_id: str
    kind: str
    storage_key: str
    width: int
    height: int
    mime: str


@dataclass(frozen=True)
class _VariantConfig:
    processor_variant: Literal["display_webp", "video_reference_jpeg"]
    extension: str
    mime: str
    max_side: int


_VARIANT_CONFIG = MappingProxyType(
    {
        DISPLAY_VARIANT: _VariantConfig(
            processor_variant="display_webp",
            extension="webp",
            mime="image/webp",
            max_side=2048,
        ),
        VIDEO_REFERENCE_VARIANT: _VariantConfig(
            processor_variant="video_reference_jpeg",
            extension="jpg",
            mime="image/jpeg",
            max_side=2048,
        ),
    }
)
_CLAIM_TTL_SECONDS = 90.0
_RETRY_DELAY = timedelta(seconds=5)
_MAX_IMAGE_PIXELS = 64_000_000


def make_display_variant(
    path: Path,
    *,
    max_pixels: int,
    max_side: int = 2048,
) -> tuple[bytes, tuple[int, int]]:
    """Compatibility helper for the narrow legacy unit-test surface.

    Production routes use ``CreateVariantService``; this pure helper remains
    useful for callers that only need to validate the old encoding contract.
    """
    try:
        with PILImage.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise VariantError(
                    "too_many_pixels",
                    "image exceeds safe pixel limit",
                    413,
                )
            image.load()
            image.thumbnail((max_side, max_side))
            output = io.BytesIO()
            with image.convert("RGB") as rgb:
                rgb.save(output, format="WEBP", quality=86, method=4)
            return output.getvalue(), image.size
    except VariantError:
        raise
    except PILImage.DecompressionBombError as exc:
        raise VariantError(
            "too_many_pixels",
            "image exceeds safe pixel limit",
            413,
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise VariantError("invalid_image", "unreadable image", 400) from exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _result(record: VariantRecord) -> VariantResult:
    return VariantResult(
        image_id=record.image_id,
        kind=record.kind,
        storage_key=record.storage_key,
        width=record.width,
        height=record.height,
        mime=_VARIANT_CONFIG[record.kind].mime,
    )


class CreateVariantService:
    def __init__(
        self,
        *,
        artifacts: ArtifactStorePort,
        capacity: CapacityPort,
        storage_capacity: StorageCapacityPort,
        repository: VariantRepositoryPort,
        processing_executor: ImageProcessingExecutorPort,
        max_pixels: int = _MAX_IMAGE_PIXELS,
        capacity_lease_ttl_seconds: float | None = None,
        storage_lease_ttl_seconds: float | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.capacity = capacity
        self.storage_capacity = storage_capacity
        self.repository = repository
        self.processing_executor = processing_executor
        self.max_pixels = max_pixels
        self.capacity_lease_ttl_seconds = (
            CapacityLimits.from_env().lease_ttl_seconds
            if capacity_lease_ttl_seconds is None
            else capacity_lease_ttl_seconds
        )
        self.storage_lease_ttl_seconds = (
            CapacityLimits.from_env().lease_ttl_seconds
            if storage_lease_ttl_seconds is None
            else storage_lease_ttl_seconds
        )

    async def ensure_display_variant(
        self,
        image_id: str,
        *,
        expected_user_id: str | None = None,
    ) -> VariantResult:
        return await self.ensure_variant(
            image_id,
            DISPLAY_VARIANT,
            expected_user_id=expected_user_id,
        )

    async def ensure_video_reference_variant(
        self,
        image_id: str,
    ) -> VariantResult:
        return await self.ensure_variant(image_id, VIDEO_REFERENCE_VARIANT)

    async def ensure_variant(
        self,
        image_id: str,
        kind: str,
        *,
        expected_user_id: str | None = None,
    ) -> VariantResult:
        config = _VARIANT_CONFIG.get(kind)
        if config is None:
            raise VariantError("invalid_variant", "unsupported image variant", 400)
        lookup = await self.repository.lookup(
            image_id,
            kind,
            expected_user_id=expected_user_id,
        )
        if lookup.source is None:
            raise VariantError("not_found", "image not found", 404)
        ready = await self._ready_record(lookup.variant)
        if ready is not None:
            return ready
        source = lookup.source
        claim = await self._try_claim(source, kind)
        if claim is None:
            return await self._winner_or_busy(
                image_id,
                kind,
                expected_user_id=expected_user_id,
            )
        return await self._execute_claim(
            image_id=image_id,
            kind=kind,
            expected_user_id=expected_user_id,
            source=source,
            claim=claim,
            config=config,
        )

    async def _try_claim(
        self,
        source: VariantSource,
        kind: str,
    ) -> VariantClaim | None:
        now = _now()
        return await self.repository.try_claim(
            source,
            kind,
            token=secrets.token_urlsafe(32),
            lease_until=now + timedelta(seconds=_CLAIM_TTL_SECONDS),
            now=now,
        )

    async def _winner_or_busy(
        self,
        image_id: str,
        kind: str,
        *,
        expected_user_id: str | None,
    ) -> VariantResult:
        winner = await self._wait_for_winner(
            image_id,
            kind,
            expected_user_id=expected_user_id,
        )
        if winner is not None:
            return winner
        raise VariantError(
            "variant_generation_busy",
            "image variant generation is still in progress",
            503,
        )

    async def _execute_claim(
        self,
        *,
        image_id: str,
        kind: str,
        expected_user_id: str | None,
        source: VariantSource,
        claim: VariantClaim,
        config: _VariantConfig,
    ) -> VariantResult:
        output_path = self._new_output_path(source, kind)
        try:
            return await self._render_publish_finalize(
                image_id=image_id,
                expected_user_id=expected_user_id,
                source=source,
                claim=claim,
                kind=kind,
                config=config,
                output_path=output_path,
            )
        except asyncio.CancelledError:
            await self._fail_claim(
                claim,
                error_code="variant_cancelled",
            )
            raise
        except CapacityLeaseLost as exc:
            await self._fail_claim(
                claim,
                error_code="capacity_lease_lost",
            )
            raise VariantError(
                "variant_capacity_unavailable",
                "image variant capacity is temporarily unavailable",
                503,
            ) from exc
        except (CapacityExceeded, CapacityUnavailable) as exc:
            await self._fail_claim(
                claim,
                error_code="capacity_unavailable",
            )
            raise VariantError(
                "variant_capacity_unavailable",
                "image variant capacity is temporarily unavailable",
                503,
            ) from exc
        except StorageCapacityExceeded as exc:
            await self._fail_claim(
                claim,
                error_code="storage_capacity_exhausted",
            )
            raise VariantError(
                "storage_insufficient_space",
                "not enough free storage to create this image variant",
                507,
            ) from exc
        except StorageCapacityUnavailable as exc:
            await self._fail_claim(
                claim,
                error_code="storage_capacity_unavailable",
            )
            raise VariantError(
                "variant_storage_unavailable",
                "image variant storage is temporarily unavailable",
                503,
            ) from exc
        except ProcessingError as exc:
            await self._fail_claim(
                claim,
                error_code=exc.code,
            )
            raise VariantError(exc.code, exc.message, exc.status_code) from exc
        except VariantError as exc:
            await self._fail_claim(claim, error_code=exc.code)
            raise
        except Exception as exc:
            await self._fail_claim(
                claim,
                error_code="variant_failed",
            )
            raise VariantError(
                "variant_generation_failed",
                "image variant generation failed",
                503,
            ) from exc
        finally:
            output_path.unlink(missing_ok=True)

    async def _render_publish_finalize(
        self,
        *,
        image_id: str,
        expected_user_id: str | None,
        source: VariantSource,
        claim: VariantClaim,
        kind: str,
        config: _VariantConfig,
        output_path: Path,
    ) -> VariantResult:
        estimate = estimate_image_resources(
            width=source.width,
            height=source.height,
            mode="RGBA",
            upload_bytes=source.size_bytes,
            reference_max_side=config.max_side,
        )
        # The lease covers the temporary output and a destination copy on
        # filesystems where hard links are unavailable.
        storage_bytes = max(
            estimate.output_reserve_bytes,
            config.max_side * config.max_side * 8 + 2 * 1024 * 1024,
        )
        storage_lease = await self.storage_capacity.reserve(storage_bytes)
        try:
            capacity_lease = await self.capacity.reserve(estimate)
        except BaseException:
            await storage_lease.release()
            raise

        async with AsyncExitStack() as stack:
            lease_guards: list[CapacityLeaseGuard] = [
                await stack.enter_async_context(
                    maintained_capacity_lease(
                        storage_lease,
                        ttl_seconds=self.storage_lease_ttl_seconds,
                    )
                )
            ]
            lease_guards.append(
                await stack.enter_async_context(
                    maintained_capacity_lease(
                        capacity_lease,
                        ttl_seconds=self.capacity_lease_ttl_seconds,
                    )
                )
            )
            prepared, published = await self._render_with_guard(
                source=source,
                claim=claim,
                kind=kind,
                config=config,
                output_path=output_path,
                lease_guards=tuple(lease_guards),
                storage_reservation_bytes=storage_bytes,
            )
            return await self._finalize_rendered(
                image_id=image_id,
                kind=kind,
                expected_user_id=expected_user_id,
                source=source,
                claim=claim,
                prepared=prepared,
                published=published,
                lease_guards=tuple(lease_guards),
            )

    async def _render_with_guard(
        self,
        *,
        source: VariantSource,
        claim: VariantClaim,
        kind: str,
        config: _VariantConfig,
        output_path: Path,
        lease_guards: Sequence[CapacityLeaseGuard],
        storage_reservation_bytes: int | None,
    ) -> tuple[PreparedImageVariant, PublishedArtifact]:
        await self._require_claim_owned(claim, lease_guards=lease_guards)
        source_key = ArtifactKey(source.storage_key)
        source_identity = await self._assert_source_identity(
            source,
            source_key=source_key,
        )
        source_path = self.artifacts.processing_path(source_key)
        await assert_capacity_leases_owned(lease_guards)
        renew_task = asyncio.create_task(
            self._renew_claim_until_stopped(
                claim,
                lease_guards=lease_guards,
            ),
            name="image-variant-claim-renew",
        )
        try:
            prepared = await race_with_capacity_leases(
                self.processing_executor.render_variant(
                    ImageVariantProcessingRequest(
                        source_path=source_path,
                        output_path=output_path,
                        variant=config.processor_variant,
                        max_pixels=self.max_pixels,
                        max_side=config.max_side,
                    )
                ),
                lease_guards,
            )
            if (
                storage_reservation_bytes is not None
                and prepared.size_bytes * 2 > storage_reservation_bytes
            ):
                raise StorageCapacityExceeded(
                    "image variant exceeded its storage reservation"
                )
            await self._assert_source_identity(
                source,
                source_key=source_key,
                expected_runtime_identity=source_identity,
            )
            await self._require_claim_owned(claim, lease_guards=lease_guards)
            await assert_capacity_leases_owned(lease_guards)
            published = await race_with_capacity_leases(
                self._publish_prepared(
                    source=source,
                    kind=kind,
                    config=config,
                    prepared=prepared,
                ),
                lease_guards,
            )
            await assert_capacity_leases_owned(lease_guards)
            await self._assert_source_identity(
                source,
                source_key=source_key,
                expected_runtime_identity=source_identity,
            )
            await self._require_claim_owned(claim, lease_guards=lease_guards)
            return prepared, published
        finally:
            renew_task.cancel()
            await asyncio.gather(renew_task, return_exceptions=True)

    async def _publish_prepared(
        self,
        *,
        source: VariantSource,
        kind: str,
        config: _VariantConfig,
        prepared: PreparedImageVariant,
    ) -> PublishedArtifact:
        key = ArtifactKey(
            deterministic_variant_key(
                image_id=source.image_id,
                source_key=source.storage_key,
                kind=kind,
                extension=config.extension,
            )
        )
        return await self.artifacts.publish_path(
            prepared.output_path,
            key,
            expected=ArtifactIdentity(
                sha256=prepared.sha256,
                size_bytes=prepared.size_bytes,
            ),
        )

    async def _finalize_rendered(
        self,
        *,
        image_id: str,
        kind: str,
        expected_user_id: str | None,
        source: VariantSource,
        claim: VariantClaim,
        prepared: PreparedImageVariant,
        published: PublishedArtifact,
        lease_guards: Sequence[CapacityLeaseGuard] = (),
    ) -> VariantResult:
        await assert_capacity_leases_owned(lease_guards)
        await self._require_claim_owned(claim, lease_guards=lease_guards)
        finalized = await self.repository.finalize(
            claim,
            VariantRecord(
                image_id=source.image_id,
                kind=kind,
                storage_key=published.key.value,
                width=prepared.width,
                height=prepared.height,
            ),
            now=_now(),
        )
        await assert_capacity_leases_owned(lease_guards)
        if finalized is not None:
            return _result(finalized)
        winner = await self._read_ready_variant(
            image_id,
            kind,
            expected_user_id=expected_user_id,
        )
        if winner is not None:
            return winner
        raise VariantError(
            "variant_claim_lost",
            "image variant generation ownership changed",
            503,
        )

    async def _require_claim_owned(
        self,
        claim: VariantClaim,
        *,
        lease_guards: Sequence[CapacityLeaseGuard] = (),
    ) -> None:
        now = _now()
        try:
            owned = await self.repository.renew_claim(
                claim,
                lease_until=now + timedelta(seconds=_CLAIM_TTL_SECONDS),
                now=now,
            )
        except Exception as exc:
            self._mark_guards_lost(lease_guards)
            raise CapacityLeaseLost(
                "image variant claim could not be confirmed"
            ) from exc
        if owned:
            return
        self._mark_guards_lost(lease_guards)
        raise CapacityLeaseLost("image variant claim was lost")

    @staticmethod
    def _mark_guards_lost(lease_guards: Sequence[CapacityLeaseGuard]) -> None:
        for guard in lease_guards:
            guard.mark_lost()

    def _new_output_path(self, source: VariantSource, kind: str) -> Path:
        suffix = ".jpg" if kind == VIDEO_REFERENCE_VARIANT else ".webp"
        source_path = self.artifacts.processing_path(ArtifactKey(source.storage_key))
        fd, name = tempfile.mkstemp(
            prefix=".lumen-variant-",
            suffix=suffix,
            dir=str(source_path.parent),
        )
        os.close(fd)
        path = Path(name)
        path.chmod(0o600)
        return path

    async def _renew_claim_until_stopped(
        self,
        claim: VariantClaim,
        *,
        lease_guards: Sequence[CapacityLeaseGuard],
    ) -> None:
        interval = _CLAIM_TTL_SECONDS / 3.0
        while True:
            await asyncio.sleep(interval)
            try:
                renew_now = _now()
                owned = await self.repository.renew_claim(
                    claim,
                    lease_until=renew_now + timedelta(seconds=_CLAIM_TTL_SECONDS),
                    now=renew_now,
                )
            except Exception:
                owned = False
            if not owned:
                self._mark_guards_lost(lease_guards)
                return

    async def _fail_claim(self, claim: VariantClaim, *, error_code: str) -> None:
        now = _now()
        try:
            await self.repository.fail(
                claim,
                error_code=error_code,
                retry_at=now + _RETRY_DELAY,
                now=now,
            )
        except Exception:
            logger.warning(
                "image variant claim failure could not be recorded",
                exc_info=True,
            )

    async def _assert_source_identity(
        self,
        source: VariantSource,
        *,
        source_key: ArtifactKey,
        expected_runtime_identity: ArtifactIdentity | None = None,
    ) -> ArtifactIdentity:
        actual = await self.artifacts.identity(source_key)
        if actual is None:
            raise VariantError("not_found", "image binary is missing", 404)
        expected = ArtifactIdentity(
            sha256=source.sha256,
            size_bytes=source.size_bytes,
        )
        if not expected.matches(actual):
            raise VariantError(
                "source_changed",
                "image binary no longer matches its manifest",
                409,
            )
        if (
            expected_runtime_identity is not None
            and not expected_runtime_identity.matches(actual)
        ):
            raise VariantError(
                "source_changed",
                "image binary changed during variant generation",
                409,
            )
        return actual

    async def _ready_record(
        self,
        record: VariantRecord | None,
    ) -> VariantResult | None:
        if record is None:
            return None
        if await self.artifacts.identity(ArtifactKey(record.storage_key)) is None:
            return None
        return _result(record)

    async def _read_ready_variant(
        self,
        image_id: str,
        kind: str,
        *,
        expected_user_id: str | None,
    ) -> VariantResult | None:
        lookup = await self.repository.lookup(
            image_id,
            kind,
            expected_user_id=expected_user_id,
        )
        return await self._ready_record(lookup.variant)

    async def _wait_for_winner(
        self,
        image_id: str,
        kind: str,
        *,
        expected_user_id: str | None,
    ) -> VariantResult | None:
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            winner = await self._read_ready_variant(
                image_id,
                kind,
                expected_user_id=expected_user_id,
            )
            if winner is not None:
                return winner
            await asyncio.sleep(0.2)
        return None
