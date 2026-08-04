from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.images.adapters.local_capacity import CapacityLimits, ScaledLocalCapacity
from lumen_core.storage_capacity import StorageCapacityExceeded
from app.images.application.create_variant import (
    CreateVariantService,
    VariantError,
    VariantResult,
)
from app.images.domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    PublishedArtifact,
)
from app.images.ports.image_processing import (
    ImageVariantProcessingRequest,
    PreparedImageVariant,
)
from app.images.ports.variant_repository import (
    VariantClaim,
    VariantLookup,
    VariantRecord,
    VariantSource,
)


_SOURCE_SHA = "a" * 64
_SOURCE_IDENTITY = ArtifactIdentity(
    sha256=_SOURCE_SHA,
    size_bytes=123,
    device=1,
    inode=2,
)


def _source(image_id: str = "image-1") -> VariantSource:
    return VariantSource(
        image_id=image_id,
        user_id="user-1",
        storage_key=f"u/user-1/{image_id}.png",
        sha256=_SOURCE_SHA,
        size_bytes=123,
        width=320,
        height=160,
    )


class _Repository:
    def __init__(self, sources: list[VariantSource]) -> None:
        self.sources = {source.image_id: source for source in sources}
        self.variants: dict[tuple[str, str], VariantRecord] = {}
        self.claims: list[VariantClaim] = []
        self.failed: list[tuple[str, str]] = []
        self.finalize_calls = 0
        self.renew_results: deque[bool] = deque()
        self.finalize_hook: Any | None = None

    async def lookup(
        self,
        image_id: str,
        kind: str,
        *,
        expected_user_id: str | None = None,
    ) -> VariantLookup:
        source = self.sources.get(image_id)
        if source is not None and expected_user_id not in {None, source.user_id}:
            source = None
        return VariantLookup(
            source=source,
            variant=self.variants.get((image_id, kind)),
        )

    async def try_claim(
        self,
        source: VariantSource,
        kind: str,
        *,
        token: str,
        lease_until: datetime,
        now: datetime,
    ) -> VariantClaim:
        del lease_until, now
        claim = VariantClaim(
            image_id=source.image_id,
            kind=kind,
            token=token,
            source_key=source.storage_key,
            source_sha256=source.sha256,
        )
        self.claims.append(claim)
        return claim

    async def renew_claim(
        self,
        claim: VariantClaim,
        *,
        lease_until: datetime,
        now: datetime,
    ) -> bool:
        del claim, lease_until, now
        if self.renew_results:
            return self.renew_results.popleft()
        return True

    async def finalize(
        self,
        claim: VariantClaim,
        variant: VariantRecord,
        *,
        now: datetime,
    ) -> VariantRecord:
        del claim, now
        if self.finalize_hook is not None:
            self.finalize_hook()
        self.finalize_calls += 1
        self.variants[(variant.image_id, variant.kind)] = variant
        return variant

    async def fail(
        self,
        claim: VariantClaim,
        *,
        error_code: str,
        retry_at: datetime,
        now: datetime,
    ) -> None:
        del retry_at, now
        self.failed.append((claim.image_id, error_code))


class _ArtifactStore:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.identities: dict[str, deque[ArtifactIdentity | None]] = {}
        self.publish_calls = 0

    def set_identities(
        self,
        key: str,
        *values: ArtifactIdentity | None,
    ) -> None:
        self.identities[key] = deque(values)

    async def identity(self, key: ArtifactKey) -> ArtifactIdentity | None:
        values = self.identities.get(key.value)
        if not values:
            return None
        if len(values) > 1:
            return values.popleft()
        return values[0]

    def processing_path(self, key: ArtifactKey) -> Path:
        return self.tmp_path / Path(key.value).name

    @asynccontextmanager
    async def artifact_lifecycle_fence(
        self,
        _key: ArtifactKey,
        *,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[None]:
        del timeout_seconds
        yield

    async def publish_path(
        self,
        source: Path,
        key: ArtifactKey,
        *,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        assert source.read_bytes() == b"variant"
        self.publish_calls += 1
        self.identities[key.value] = deque([expected])
        return PublishedArtifact(key=key, identity=expected, created=True)


class _Lease:
    def __init__(self, renew_results: list[bool] | None = None) -> None:
        self.renew_results = deque(renew_results or [])
        self.released = 0

    async def renew(self) -> bool:
        if self.renew_results:
            return self.renew_results.popleft()
        return True

    async def release(self) -> None:
        self.released += 1


class _Capacity:
    def __init__(self, lease: _Lease | None = None) -> None:
        self.lease = lease or _Lease()
        self.reserve_calls = 0

    async def reserve(self, _estimate: Any) -> _Lease:
        self.reserve_calls += 1
        return self.lease


class _StorageCapacity:
    def __init__(
        self,
        lease: _Lease | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.lease = lease or _Lease()
        self.error = error
        self.reservations: list[int] = []

    async def reserve(self, bytes_required: int) -> _Lease:
        self.reservations.append(bytes_required)
        if self.error is not None:
            raise self.error
        return self.lease


class _Executor:
    def __init__(self, *, block: bool = False, delay: float = 0.0) -> None:
        self.block = block
        self.delay = delay
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def render_variant(
        self,
        request: ImageVariantProcessingRequest,
    ) -> PreparedImageVariant:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.block:
                await self.release.wait()
            elif self.delay:
                await asyncio.sleep(self.delay)
            request.output_path.write_bytes(b"variant")
            return PreparedImageVariant(
                output_path=request.output_path,
                mime=(
                    "image/jpeg"
                    if request.variant == "video_reference_jpeg"
                    else "image/webp"
                ),
                width=128,
                height=64,
                size_bytes=len(b"variant"),
                sha256=hashlib.sha256(b"variant").hexdigest(),
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.active -= 1


def _service(
    *,
    tmp_path: Path,
    repository: _Repository,
    artifacts: _ArtifactStore,
    executor: _Executor,
    capacity: Any | None = None,
    storage_capacity: Any | None = None,
    capacity_ttl: float = 1.0,
) -> CreateVariantService:
    return CreateVariantService(
        artifacts=artifacts,  # type: ignore[arg-type]
        capacity=capacity or _Capacity(),  # type: ignore[arg-type]
        storage_capacity=storage_capacity or _StorageCapacity(),  # type: ignore[arg-type]
        repository=repository,
        processing_executor=executor,
        capacity_lease_ttl_seconds=capacity_ttl,
        storage_lease_ttl_seconds=capacity_ttl,
    )


@pytest.mark.asyncio
async def test_variant_generation_checks_source_identity_and_finalizes(
    tmp_path: Path,
) -> None:
    source = _source()
    repository = _Repository([source])
    artifacts = _ArtifactStore(tmp_path)
    artifacts.set_identities(source.storage_key, _SOURCE_IDENTITY)
    executor = _Executor()

    result = await _service(
        tmp_path=tmp_path,
        repository=repository,
        artifacts=artifacts,
        executor=executor,
    ).ensure_display_variant(source.image_id, expected_user_id=source.user_id)

    assert result == VariantResult(
        image_id=source.image_id,
        kind="display2048",
        storage_key=f"u/user-1/{source.image_id}.display2048.webp",
        width=128,
        height=64,
        mime="image/webp",
    )
    assert artifacts.publish_calls == 1
    assert repository.finalize_calls == 1
    assert repository.failed == []


@pytest.mark.asyncio
async def test_variant_storage_reservation_is_held_through_finalize(
    tmp_path: Path,
) -> None:
    source = _source()
    repository = _Repository([source])
    artifacts = _ArtifactStore(tmp_path)
    artifacts.set_identities(source.storage_key, _SOURCE_IDENTITY)
    executor = _Executor()
    storage = _StorageCapacity()
    repository.finalize_hook = lambda: (
        storage.lease.released == 0
        or (_ for _ in ()).throw(
            AssertionError("storage reservation released before finalize")
        )
    )

    result = await _service(
        tmp_path=tmp_path,
        repository=repository,
        artifacts=artifacts,
        executor=executor,
        storage_capacity=storage,
    ).ensure_display_variant(source.image_id)

    assert isinstance(result, VariantResult)
    assert len(storage.reservations) == 1
    assert storage.reservations[0] >= 2048 * 2048 * 8
    assert storage.lease.released == 1


@pytest.mark.asyncio
async def test_variant_fails_before_render_when_storage_reservation_is_exhausted(
    tmp_path: Path,
) -> None:
    source = _source()
    repository = _Repository([source])
    artifacts = _ArtifactStore(tmp_path)
    artifacts.set_identities(source.storage_key, _SOURCE_IDENTITY)
    executor = _Executor()
    storage = _StorageCapacity(error=StorageCapacityExceeded("full"))

    with pytest.raises(VariantError) as exc_info:
        await _service(
            tmp_path=tmp_path,
            repository=repository,
            artifacts=artifacts,
            executor=executor,
            storage_capacity=storage,
        ).ensure_display_variant(source.image_id)

    assert exc_info.value.code == "storage_insufficient_space"
    assert executor.calls == 0
    assert repository.finalize_calls == 0


@pytest.mark.asyncio
async def test_variant_generation_rejects_manifest_mismatch_before_render(
    tmp_path: Path,
) -> None:
    source = _source()
    repository = _Repository([source])
    artifacts = _ArtifactStore(tmp_path)
    artifacts.set_identities(
        source.storage_key,
        ArtifactIdentity(sha256="b" * 64, size_bytes=123, device=1, inode=2),
    )
    executor = _Executor()

    with pytest.raises(VariantError) as exc_info:
        await _service(
            tmp_path=tmp_path,
            repository=repository,
            artifacts=artifacts,
            executor=executor,
        ).ensure_display_variant(source.image_id)

    assert exc_info.value.code == "source_changed"
    assert executor.calls == 0
    assert artifacts.publish_calls == 0
    assert repository.finalize_calls == 0
    assert repository.failed == [(source.image_id, "source_changed")]


@pytest.mark.asyncio
async def test_variant_generation_detects_source_replacement_during_render(
    tmp_path: Path,
) -> None:
    source = _source()
    repository = _Repository([source])
    artifacts = _ArtifactStore(tmp_path)
    artifacts.set_identities(
        source.storage_key,
        _SOURCE_IDENTITY,
        ArtifactIdentity(
            sha256=_SOURCE_SHA,
            size_bytes=123,
            device=1,
            inode=999,
        ),
    )
    executor = _Executor()

    with pytest.raises(VariantError) as exc_info:
        await _service(
            tmp_path=tmp_path,
            repository=repository,
            artifacts=artifacts,
            executor=executor,
        ).ensure_display_variant(source.image_id)

    assert exc_info.value.code == "source_changed"
    assert artifacts.publish_calls == 0
    assert repository.finalize_calls == 0


@pytest.mark.asyncio
async def test_request_cancellation_stops_render_and_releases_claim(
    tmp_path: Path,
) -> None:
    source = _source()
    repository = _Repository([source])
    artifacts = _ArtifactStore(tmp_path)
    artifacts.set_identities(source.storage_key, _SOURCE_IDENTITY)
    executor = _Executor(block=True)
    lease = _Lease()
    service = _service(
        tmp_path=tmp_path,
        repository=repository,
        artifacts=artifacts,
        executor=executor,
        capacity=_Capacity(lease),
    )

    task = asyncio.create_task(service.ensure_display_variant(source.image_id))
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert executor.cancelled
    assert lease.released == 1
    assert repository.failed == [(source.image_id, "variant_cancelled")]
    assert artifacts.publish_calls == 0
    assert repository.finalize_calls == 0


@pytest.mark.asyncio
async def test_capacity_lease_loss_cancels_render_before_publish(
    tmp_path: Path,
) -> None:
    source = _source()
    repository = _Repository([source])
    artifacts = _ArtifactStore(tmp_path)
    artifacts.set_identities(source.storage_key, _SOURCE_IDENTITY)
    executor = _Executor(block=True)
    lease = _Lease([True, False])

    with pytest.raises(VariantError) as exc_info:
        await asyncio.wait_for(
            _service(
                tmp_path=tmp_path,
                repository=repository,
                artifacts=artifacts,
                executor=executor,
                capacity=_Capacity(lease),
                capacity_ttl=0.2,
            ).ensure_display_variant(source.image_id),
            timeout=2,
        )

    assert exc_info.value.code == "variant_capacity_unavailable"
    assert executor.cancelled
    assert lease.released == 1
    assert artifacts.publish_calls == 0
    assert repository.finalize_calls == 0


@pytest.mark.asyncio
async def test_hundred_distinct_variants_stay_within_shared_capacity(
    tmp_path: Path,
) -> None:
    sources = [_source(f"image-{index}") for index in range(100)]
    repository = _Repository(sources)
    artifacts = _ArtifactStore(tmp_path)
    for source in sources:
        artifacts.set_identities(source.storage_key, _SOURCE_IDENTITY)
    executor = _Executor(delay=0.05)
    capacity = ScaledLocalCapacity(
        CapacityLimits(
            max_concurrency=4,
            max_peak_bytes=16 * 1024 * 1024 * 1024,
            lease_ttl_seconds=1,
        ),
        process_count=1,
    )
    service = _service(
        tmp_path=tmp_path,
        repository=repository,
        artifacts=artifacts,
        executor=executor,
        capacity=capacity,
    )

    results = await asyncio.gather(
        *(service.ensure_display_variant(source.image_id) for source in sources),
        return_exceptions=True,
    )

    assert executor.max_active <= 4
    assert sum(isinstance(result, VariantResult) for result in results) == 4
    failures = [result for result in results if isinstance(result, VariantError)]
    assert len(failures) == 96
    assert {failure.code for failure in failures} == {"variant_capacity_unavailable"}
