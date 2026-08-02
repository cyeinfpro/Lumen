from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import video_reference_videos
from app.images.application.deleted_media_references import (
    known_live_media_storage_keys,
)
from app.routes import video_generation_routes, video_upload_routes, videos
from app.services import video_storage_accounting as video_storage_accounting_module
from app.services import video_storage_lifecycle as video_storage_lifecycle_module
from app.services.video import submission as video_submission
from app.services.video_storage_capacity import (
    VIDEO_REFERENCE_STORAGE_QUOTA_BYTES,
    VideoTranscodeCapacityManager,
    VideoTranscodeCapacityUnavailable,
)
from app.services.video_storage_lifecycle import (
    VIDEO_STORAGE_CLEANUP_METADATA_KEY,
    VideoArtifactCleanupResult,
    VideoArtifactInspection,
    VideoStorageLifecycle,
    record_video_storage_cleanup,
    video_reference_quota_contribution,
)
from lumen_core import storage_capacity as storage_capacity_module
from lumen_core.capacity_leases import CapacityLeaseLost
from lumen_core.models import Base, OutboxEvent, User, Video, VideoGeneration
from lumen_core.schemas import VideoCreateIn, VideoReferenceMediaIn
from lumen_core.video_billing import VideoCostEstimate


def _payload(size: int, marker: bytes = b"x") -> bytes:
    header = b"\x00\x00\x00\x18ftypisom"
    if size < len(header):
        raise ValueError("video payload must include an ftyp header")
    return header + marker * (size - len(header))


async def _async_value(value: Any) -> Any:
    return value


def _upload(payload: bytes, filename: str = "reference.mp4") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(payload),
        filename=filename,
        headers={"content-type": "video/mp4"},
    )


def _video(
    *,
    video_id: str,
    user_id: str,
    payload: bytes,
    storage_key: str | None = None,
    poster_storage_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Video:
    digest = hashlib.sha256(payload).hexdigest()
    return Video(
        id=video_id,
        user_id=user_id,
        owner_generation_id=None,
        storage_key=(storage_key or f"u/{user_id}/vref/{video_id}/original.mp4"),
        poster_storage_key=poster_storage_key,
        mime="video/mp4",
        width=0,
        height=0,
        duration_ms=0,
        fps=None,
        size_bytes=len(payload),
        sha256=digest,
        etag=digest,
        has_audio=False,
        faststart=False,
        visibility="private",
        metadata_jsonb=metadata or {"source": "uploaded_reference"},
    )


@asynccontextmanager
async def _database(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'video-storage.sqlite'}",
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    User.__table__,
                    Video.__table__,
                    VideoGeneration.__table__,
                    OutboxEvent.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _configure_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    free_bytes: int,
    minimum_free_bytes: int,
) -> None:
    monkeypatch.setattr(videos.settings, "storage_root", str(tmp_path / "storage"))
    monkeypatch.setattr(
        videos.settings,
        "minimum_storage_free_bytes",
        minimum_free_bytes,
    )
    monkeypatch.setattr(
        videos.settings,
        "image_upload_capacity_degraded_policy",
        "scaled_local",
    )
    monkeypatch.setattr(
        storage_capacity_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=free_bytes),
    )


async def _seed_user(
    session: AsyncSession,
    *,
    user_id: str,
) -> None:
    session.add(
        User(
            id=user_id,
            email=f"{user_id}@example.com",
            display_name=user_id,
        )
    )
    await session.commit()


def _active_video_generation(
    *,
    generation_id: str,
    user_id: str,
    video: Video,
) -> VideoGeneration:
    return VideoGeneration(
        id=generation_id,
        user_id=user_id,
        action="reference",
        model="seedance-2.0",
        prompt="reference",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        generate_audio=True,
        watermark=False,
        upstream_request={
            "reference_media": [
                {
                    "kind": "video",
                    "video_id": video.id,
                    "storage_key": video.storage_key,
                }
            ]
        },
        status="queued",
        progress_stage="queued",
        progress_pct=0,
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key=f"idempotency:{generation_id}",
        request_fingerprint="f" * 64,
        est_token_upper=1,
        est_cost_micro=1,
    )


def test_video_storage_contracts_are_immutable_and_compatibly_aliased() -> None:
    precedence = video_generation_routes.SUBMIT_DELIVERY_PRECEDENCE
    assert isinstance(precedence, MappingProxyType)
    assert (
        video_generation_routes._SUBMIT_DELIVERY_PRECEDENCE is precedence  # noqa: SLF001
    )
    with pytest.raises(TypeError):
        precedence["confirmed"] = 0

    assert (
        video_storage_accounting_module._MAX_ISSUES  # noqa: SLF001
        == video_storage_accounting_module.VIDEO_STORAGE_MAX_ISSUES
    )
    assert (
        video_storage_accounting_module._VIDEO_VARIANT_METADATA_KEYS  # noqa: SLF001
        == video_storage_accounting_module.VIDEO_VARIANT_METADATA_KEYS
    )
    assert (
        video_storage_accounting_module._storage_key_parts  # noqa: SLF001
        is video_storage_accounting_module.storage_key_parts
    )
    assert (
        video_storage_accounting_module._starts_with  # noqa: SLF001
        is video_storage_accounting_module.storage_key_starts_with
    )
    assert (
        video_storage_lifecycle_module.storage_key_parts
        is video_storage_accounting_module.storage_key_parts
    )
    assert (
        video_storage_lifecycle_module.storage_key_starts_with
        is video_storage_accounting_module.storage_key_starts_with
    )


@pytest.mark.asyncio
async def test_near_full_disk_returns_507_before_video_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(600)
    _configure_storage(
        monkeypatch,
        tmp_path,
        free_bytes=699,
        minimum_free_bytes=100,
    )
    writes = 0

    def unexpected_write(*_args: Any, **_kwargs: Any) -> None:
        nonlocal writes
        writes += 1

    monkeypatch.setattr(videos, "_write_new_file_atomic", unexpected_write)

    async with _database(tmp_path) as factory:
        async with factory() as session:
            await _seed_user(session, user_id="user-full")

            with pytest.raises(HTTPException) as exc_info:
                await videos.upload_reference_video(
                    SimpleNamespace(id="user-full"),
                    session,
                    _upload(payload),
                )

            assert exc_info.value.status_code == 507
            assert (
                exc_info.value.detail["error"]["code"] == "storage_insufficient_space"
            )
            assert writes == 0
            row_count = int(
                (
                    await session.execute(
                        select(func.count(Video.id)).where(Video.user_id == "user-full")
                    )
                ).scalar_one()
            )
            assert row_count == 0
            assert not any(
                path.is_file()
                for path in Path(videos.settings.storage_root).glob(
                    "u/user-full/vref/**/*"
                )
            )


@pytest.mark.asyncio
async def test_post_write_lease_loss_removes_published_video(
    tmp_path: Path,
) -> None:
    payload = _payload(128)
    path = tmp_path / "storage/u/user-lease/vref/video-lease/original.mp4"

    class LoseAfterWrite:
        @asynccontextmanager
        async def reserve(self, _bytes_required: int) -> AsyncIterator[None]:
            yield None
            raise CapacityLeaseLost("simulated lease loss")

    def write(candidate: Path, source: Any) -> None:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        source.seek(0)
        candidate.write_bytes(source.read())

    with pytest.raises(HTTPException) as exc_info:
        await video_upload_routes._write_reserved(  # noqa: SLF001
            path=path,
            file=_upload(payload),
            size=len(payload),
            deps=SimpleNamespace(
                storage_capacity=LoseAfterWrite(),
                write_new_file_atomic=write,
                unlink_file_if_exists=lambda candidate: candidate.unlink(
                    missing_ok=True
                ),
                logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
                http_error=videos._http,
            ),
        )

    assert exc_info.value.status_code == 503
    assert not path.exists()


@pytest.mark.asyncio
async def test_cancelled_video_write_waits_for_thread_then_cleans_file(
    tmp_path: Path,
) -> None:
    payload = _payload(128)
    path = tmp_path / "storage/u/user-cancel/vref/video-cancel/original.mp4"
    write_started = threading.Event()
    release_write = threading.Event()

    class Capacity:
        @asynccontextmanager
        async def reserve(self, _bytes_required: int) -> AsyncIterator[None]:
            yield None

    def write(candidate: Path, source: Any) -> None:
        write_started.set()
        if not release_write.wait(timeout=5):
            raise TimeoutError("cancelled video write was not released")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        source.seek(0)
        candidate.write_bytes(source.read())

    task = asyncio.create_task(
        video_upload_routes._write_reserved(  # noqa: SLF001
            path=path,
            file=_upload(payload),
            size=len(payload),
            deps=SimpleNamespace(
                storage_capacity=Capacity(),
                write_new_file_atomic=write,
                unlink_file_if_exists=lambda candidate: candidate.unlink(
                    missing_ok=True
                ),
                logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
                http_error=videos._http,
            ),
        )
    )
    assert await asyncio.to_thread(write_started.wait, 2)
    task.cancel()
    release_write.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not path.exists()


@pytest.mark.asyncio
async def test_concurrent_video_uploads_share_global_byte_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_payload = _payload(600, b"a")
    second_payload = _payload(400, b"b")
    _configure_storage(
        monkeypatch,
        tmp_path,
        free_bytes=1_000,
        minimum_free_bytes=100,
    )
    first_write_started = threading.Event()
    release_first_write = threading.Event()
    second_writes = 0
    real_write = videos._write_new_file_atomic

    def blocking_write(path: Path, source: Any) -> None:
        nonlocal second_writes
        if "video-first" in path.parts:
            first_write_started.set()
            if not release_first_write.wait(timeout=5):
                raise TimeoutError("first video write was not released")
        else:
            second_writes += 1
        real_write(path, source)

    monkeypatch.setattr(videos, "_write_new_file_atomic", blocking_write)

    async with _database(tmp_path) as factory:
        async with factory() as setup:
            setup.add_all(
                [
                    User(
                        id="user-first",
                        email="first@example.com",
                        display_name="first",
                    ),
                    User(
                        id="user-second",
                        email="second@example.com",
                        display_name="second",
                    ),
                    _video(
                        video_id="video-first",
                        user_id="user-first",
                        payload=first_payload,
                    ),
                    _video(
                        video_id="video-second",
                        user_id="user-second",
                        payload=second_payload,
                    ),
                ]
            )
            await setup.commit()

        async with factory() as first_session, factory() as second_session:
            first_task = asyncio.create_task(
                videos.upload_reference_video(
                    SimpleNamespace(id="user-first"),
                    first_session,
                    _upload(first_payload, "first.mp4"),
                )
            )
            assert await asyncio.to_thread(first_write_started.wait, 2)

            with pytest.raises(HTTPException) as exc_info:
                await videos.upload_reference_video(
                    SimpleNamespace(id="user-second"),
                    second_session,
                    _upload(second_payload, "second.mp4"),
                )

            assert exc_info.value.status_code == 507
            assert second_writes == 0
            release_first_write.set()
            first_result = await first_task
            assert first_result.id == "video-first"

        lease_state = (
            Path(videos.settings.storage_root) / ".lumen-capacity/storage-leases.json"
        ).read_text()
        assert '"leases":{}' in lease_state


@pytest.mark.asyncio
async def test_upload_delete_reupload_reuses_one_row_and_one_artifact_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(1_024, b"r")
    _configure_storage(
        monkeypatch,
        tmp_path,
        free_bytes=10 * 1024 * 1024,
        minimum_free_bytes=100,
    )

    async def allow_delete(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        videos.asset_ref_service,
        "ensure_asset_not_canvas_referenced",
        allow_delete,
    )

    async with _database(tmp_path) as factory:
        async with factory() as session:
            await _seed_user(session, user_id="user-loop")
            first = await videos.upload_reference_video(
                SimpleNamespace(id="user-loop"),
                session,
                _upload(payload),
            )
            assert first.created is True

            for _index in range(5):
                response = await videos.delete_video(
                    first.id,
                    SimpleNamespace(id="user-loop"),
                    session,
                )
                assert response.status_code == 204

                recovered = await videos.upload_reference_video(
                    SimpleNamespace(id="user-loop"),
                    session,
                    _upload(payload),
                )
                assert recovered.id == first.id
                assert recovered.created is False

            row_count = int(
                (
                    await session.execute(
                        select(func.count(Video.id)).where(Video.user_id == "user-loop")
                    )
                ).scalar_one()
            )
            assert row_count == 1

            video = await session.get(Video, first.id)
            assert video is not None
            inspection = await VideoStorageLifecycle(
                videos.settings.storage_root
            ).inspect(video)
            assert inspection.artifact_count == 1
            assert inspection.bytes_on_disk == len(payload)
            assert inspection.primary_present is True


@pytest.mark.asyncio
async def test_active_video_generation_blocks_reference_video_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(700, b"a")
    _configure_storage(
        monkeypatch,
        tmp_path,
        free_bytes=10 * 1024 * 1024,
        minimum_free_bytes=100,
    )
    storage_root = Path(videos.settings.storage_root)
    artifact = storage_root / "u/user-active/vref/video-active/original.mp4"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(payload)

    async def allow_delete(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        videos.asset_ref_service,
        "ensure_asset_not_canvas_referenced",
        allow_delete,
    )

    async with _database(tmp_path) as factory:
        async with factory() as session:
            await _seed_user(session, user_id="user-active")
            video = _video(
                video_id="video-active",
                user_id="user-active",
                payload=payload,
            )
            session.add(video)
            session.add(
                _active_video_generation(
                    generation_id="generation-active",
                    user_id="user-active",
                    video=video,
                )
            )
            await session.commit()

            with pytest.raises(HTTPException) as exc_info:
                await videos.delete_video(
                    video.id,
                    SimpleNamespace(id="user-active"),
                    session,
                )

            assert exc_info.value.status_code == 409
            assert (
                exc_info.value.detail["error"]["code"]
                == "video_generation_reference_active"
            )
            await session.refresh(video)
            assert video.deleted_at is None
            assert artifact.read_bytes() == payload


@pytest.mark.asyncio
async def test_deleted_account_video_rows_do_not_keep_storage_references_live(
    tmp_path: Path,
) -> None:
    payload = _payload(700, b"d")
    storage_key = "u/user-deleted/vref/video-deleted/original.mp4"

    async with _database(tmp_path) as factory:
        async with factory() as session:
            await _seed_user(session, user_id="user-deleted")
            user = await session.get(User, "user-deleted")
            assert user is not None
            video = _video(
                video_id="video-deleted",
                user_id=user.id,
                payload=payload,
                storage_key=storage_key,
            )
            video.deleted_at = datetime.now(timezone.utc)
            generation = _active_video_generation(
                generation_id="generation-deleted",
                user_id=user.id,
                video=video,
            )
            generation.cancel_requested_at = datetime.now(timezone.utc)
            user.deleted_at = datetime.now(timezone.utc)
            session.add_all([video, generation])
            await session.commit()

            assert await known_live_media_storage_keys(session, {storage_key}) == set()


@pytest.mark.asyncio
async def test_reference_created_after_tombstone_restores_video_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(700, b"r")
    _configure_storage(
        monkeypatch,
        tmp_path,
        free_bytes=10 * 1024 * 1024,
        minimum_free_bytes=100,
    )
    storage_root = Path(videos.settings.storage_root)
    artifact = storage_root / "u/user-race/vref/video-race/original.mp4"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(payload)

    async def allow_delete(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        videos.asset_ref_service,
        "ensure_asset_not_canvas_referenced",
        allow_delete,
    )
    real_reject = video_generation_routes._reject_active_video_reference
    guard_calls = 0

    async def insert_reference_before_second_guard(
        db: AsyncSession,
        *,
        video: Video,
        deps: Any,
        restore_soft_delete: bool,
    ) -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            db.add(
                _active_video_generation(
                    generation_id="generation-raced",
                    user_id="user-race",
                    video=video,
                )
            )
            await db.commit()
        await real_reject(
            db,
            video=video,
            deps=deps,
            restore_soft_delete=restore_soft_delete,
        )

    monkeypatch.setattr(
        video_generation_routes,
        "_reject_active_video_reference",
        insert_reference_before_second_guard,
    )

    async with _database(tmp_path) as factory:
        async with factory() as session:
            await _seed_user(session, user_id="user-race")
            video = _video(
                video_id="video-race",
                user_id="user-race",
                payload=payload,
            )
            session.add(video)
            await session.commit()

            with pytest.raises(HTTPException) as exc_info:
                await videos.delete_video(
                    video.id,
                    SimpleNamespace(id="user-race"),
                    session,
                )

            assert exc_info.value.status_code == 409
            assert guard_calls == 2
            await session.refresh(video)
            assert video.deleted_at is None
            assert VIDEO_STORAGE_CLEANUP_METADATA_KEY not in video.metadata_jsonb
            assert artifact.read_bytes() == payload


@pytest.mark.asyncio
async def test_reference_snapshot_user_lock_blocks_post_second_guard_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, value: Any = None) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

    class RaceState:
        def __init__(self) -> None:
            self.user_lock = asyncio.Lock()
            self.video = SimpleNamespace(
                id="video-user-lock-race",
                user_id="user-user-lock-race",
                deleted_at=None,
                metadata_jsonb={"source": "uploaded_reference"},
                size_bytes=700,
            )
            self.snapshot_started = asyncio.Event()
            self.allow_creation_commit = asyncio.Event()
            self.delete_user_lock_attempted = asyncio.Event()
            self.delete_user_lock_acquired = asyncio.Event()
            self.committed_generation: VideoGeneration | None = None
            self.guard_generation_ids: list[str | None] = []

    state = RaceState()

    class Session:
        def __init__(self, role: str) -> None:
            self.role = role
            self.holds_user_lock = False
            self.pending_generation: VideoGeneration | None = None

        async def execute(self, statement: Any) -> Result:
            statement_sql = str(statement)
            if "FROM users" in statement_sql:
                if self.role == "delete":
                    state.delete_user_lock_attempted.set()
                await state.user_lock.acquire()
                self.holds_user_lock = True
                if self.role == "delete":
                    state.delete_user_lock_acquired.set()
                return Result(state.video.user_id)
            if "FROM video_generations" in statement_sql:
                return Result()
            if "FROM videos" in statement_sql:
                return Result(state.video)
            raise AssertionError(f"unexpected statement for {self.role}: {statement}")

        def add(self, row: Any) -> None:
            if isinstance(row, VideoGeneration):
                self.pending_generation = row

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            if self.pending_generation is not None:
                state.committed_generation = self.pending_generation
            self._release_user_lock()

        async def rollback(self) -> None:
            self._release_user_lock()

        async def refresh(self, _row: Any) -> None:
            return None

        def _release_user_lock(self) -> None:
            if self.holds_user_lock:
                self.holds_user_lock = False
                state.user_lock.release()

    async def no_advisory_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def estimate(*_args: Any, **_kwargs: Any) -> VideoCostEstimate:
        return VideoCostEstimate(
            estimated_tokens=1,
            hold_micro=1,
            unit_price_micro=1,
            source="test",
        )

    async def hold(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def reference_snapshots(
        _db: Any,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        assert creation.holds_user_lock is True
        state.snapshot_started.set()
        await state.allow_creation_commit.wait()
        return [
            {
                "kind": "video",
                "video_id": state.video.id,
                "storage_key": "u/user-user-lock-race/vref/video/original.mp4",
            }
        ]

    async def render(_db: Any, generation: VideoGeneration) -> VideoGeneration:
        return generation

    async def active_reference(
        _db: Any,
        *,
        video: Any,
    ) -> str | None:
        assert video is state.video
        generation_id = (
            state.committed_generation.id
            if state.committed_generation is not None
            else None
        )
        state.guard_generation_ids.append(generation_id)
        return generation_id

    async def no_canvas_reference(*_args: Any, **_kwargs: Any) -> None:
        return None

    class Lifecycle:
        async def cleanup(self, _video: Any) -> Any:
            raise AssertionError("a committed reference must block cleanup")

    monkeypatch.setattr(video_submission, "lock_user_key", no_advisory_lock)
    monkeypatch.setattr(video_submission, "estimate_video_cost", estimate)
    monkeypatch.setattr(video_submission.billing_core, "hold", hold)
    monkeypatch.setattr(
        video_generation_routes,
        "active_video_generation_reference_id",
        active_reference,
    )

    creation = Session("create")
    deletion = Session("delete")
    body = VideoCreateIn(
        action="reference",
        model="seedance-2.0-fast",
        prompt="race",
        reference_media=[VideoReferenceMediaIn(kind="video", video_id=state.video.id)],
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="reference-user-lock-race",
    )
    provider = SimpleNamespace(
        kind="test",
        name="test-provider",
        upstream_model_for=lambda _model, _action: "test-upstream",
    )
    services = video_submission.VideoSubmissionServices(
        require_ready=lambda *_args, **_kwargs: _async_value((provider, {})),
        public_base_loader=lambda *_args, **_kwargs: _async_value(None),
        input_snapshot_loader=lambda *_args, **_kwargs: _async_value(
            (None, None, None)
        ),
        reference_snapshot_loader=reference_snapshots,
        reference_validator=lambda *_args, **_kwargs: None,
        allow_negative_loader=lambda *_args, **_kwargs: _async_value(False),
        wallet_loader=lambda *_args, **_kwargs: _async_value(
            SimpleNamespace(balance_micro=10)
        ),
        generation_renderer=render,
        balance_invalidator=lambda *_args, **_kwargs: _async_value(None),
        queued_publisher=lambda *_args, **_kwargs: _async_value(None),
    )
    deps = SimpleNamespace(
        http_error=videos._http,  # noqa: SLF001
        ensure_not_canvas_referenced=no_canvas_reference,
        storage_lifecycle=Lifecycle(),
        logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
    )

    creation_task = asyncio.create_task(
        video_submission.create_video_generation_record(
            creation,  # type: ignore[arg-type]
            body,
            SimpleNamespace(id=state.video.user_id),
            services=services,
        )
    )
    await state.snapshot_started.wait()

    deletion_task = asyncio.create_task(
        video_generation_routes.delete_video(
            video_id=state.video.id,
            user=SimpleNamespace(id=state.video.user_id),
            db=deletion,  # type: ignore[arg-type]
            deps=deps,  # type: ignore[arg-type]
        )
    )
    await state.delete_user_lock_attempted.wait()
    assert not state.delete_user_lock_acquired.is_set()
    assert state.guard_generation_ids == []

    state.allow_creation_commit.set()
    generation = await creation_task
    assert state.committed_generation is generation

    with pytest.raises(HTTPException) as exc_info:
        await deletion_task

    assert exc_info.value.status_code == 409
    assert state.guard_generation_ids == [generation.id]
    assert state.video.deleted_at is None


@pytest.mark.asyncio
async def test_account_delete_commit_blocks_resumed_video_submission_before_hold_or_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = "user-delete-submission-race"
    submission_ready = asyncio.Event()
    resume_submission = asyncio.Event()
    hold_calls: list[dict[str, Any]] = []

    async def no_idempotent_generation(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def pause_after_auth(*_args: Any, **_kwargs: Any) -> None:
        submission_ready.set()
        await resume_submission.wait()

    async def estimate(*_args: Any, **_kwargs: Any) -> VideoCostEstimate:
        return VideoCostEstimate(
            estimated_tokens=1,
            hold_micro=1,
            unit_price_micro=1,
            source="test",
        )

    async def hold(*_args: Any, **kwargs: Any) -> None:
        hold_calls.append(dict(kwargs))

    async def render(_db: Any, generation: VideoGeneration) -> VideoGeneration:
        return generation

    provider = SimpleNamespace(
        kind="test",
        name="test-provider",
        upstream_model_for=lambda _model, _action: "test-upstream",
    )
    services = video_submission.VideoSubmissionServices(
        require_ready=lambda *_args, **_kwargs: _async_value((provider, {})),
        public_base_loader=lambda *_args, **_kwargs: _async_value(None),
        input_snapshot_loader=lambda *_args, **_kwargs: _async_value(
            (None, None, None)
        ),
        reference_snapshot_loader=lambda *_args, **_kwargs: _async_value([]),
        reference_validator=lambda *_args, **_kwargs: None,
        allow_negative_loader=lambda *_args, **_kwargs: _async_value(False),
        wallet_loader=lambda *_args, **_kwargs: _async_value(
            SimpleNamespace(balance_micro=10)
        ),
        generation_renderer=render,
        balance_invalidator=lambda *_args, **_kwargs: _async_value(None),
        queued_publisher=lambda *_args, **_kwargs: _async_value(None),
    )
    body = VideoCreateIn(
        action="reference",
        model="seedance-2.0-fast",
        prompt="account deletion race",
        reference_media=[
            VideoReferenceMediaIn(kind="video", video_id="reference-media")
        ],
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="account-delete-submission-race",
    )

    monkeypatch.setattr(
        video_submission,
        "_find_idempotent_generation",
        no_idempotent_generation,
    )
    monkeypatch.setattr(video_submission, "lock_user_key", pause_after_auth)
    monkeypatch.setattr(video_submission, "estimate_video_cost", estimate)
    monkeypatch.setattr(video_submission.billing_core, "hold", hold)

    async with _database(tmp_path) as factory:
        async with factory() as setup:
            await _seed_user(setup, user_id=user_id)

        async with factory() as submission_db, factory() as deletion_db:
            submission_task = asyncio.create_task(
                video_submission.create_video_generation_record(
                    submission_db,
                    body,
                    SimpleNamespace(id=user_id),
                    services=services,
                )
            )
            await submission_ready.wait()

            deleting_user = await deletion_db.get(User, user_id)
            assert deleting_user is not None
            deleting_user.deleted_at = datetime.now(timezone.utc)
            await deletion_db.commit()

            resume_submission.set()
            with pytest.raises(HTTPException) as exc_info:
                await submission_task

            assert exc_info.value.status_code == 401
            assert exc_info.value.detail["error"]["code"] == "user_deleted"

        async with factory() as observer:
            generation_count = int(
                (
                    await observer.execute(
                        select(func.count(VideoGeneration.id)).where(
                            VideoGeneration.user_id == user_id
                        )
                    )
                ).scalar_one()
            )
            outbox_count = int(
                (
                    await observer.execute(select(func.count(OutboxEvent.id)))
                ).scalar_one()
            )

    assert hold_calls == []
    assert generation_count == 0
    assert outbox_count == 0


@pytest.mark.asyncio
async def test_account_delete_commit_blocks_resumed_reference_upload_before_media_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = "user-delete-upload-race"
    video_id = "video-delete-upload-race"
    payload = _payload(256, b"d")
    upload_ready = asyncio.Event()
    resume_upload = asyncio.Event()
    write_paths: list[Path] = []
    _configure_storage(
        monkeypatch,
        tmp_path,
        free_bytes=10 * 1024 * 1024,
        minimum_free_bytes=100,
    )
    real_inspect_upload = videos._inspect_reference_video_upload  # noqa: SLF001

    async def pause_before_inventory(
        file: UploadFile,
    ) -> tuple[int, str, bytes]:
        upload_ready.set()
        await resume_upload.wait()
        return await real_inspect_upload(file)

    async def unexpected_media_write(*_args: Any, **kwargs: Any) -> None:
        write_paths.append(kwargs["path"])
        raise AssertionError("deleted user reached reference-media write")

    monkeypatch.setattr(
        videos,
        "_inspect_reference_video_upload",
        pause_before_inventory,
    )
    monkeypatch.setattr(
        video_upload_routes,
        "_write_reserved",
        unexpected_media_write,
    )

    async with _database(tmp_path) as factory:
        async with factory() as setup:
            await _seed_user(setup, user_id=user_id)
            tombstoned_video = _video(
                video_id=video_id,
                user_id=user_id,
                payload=payload,
            )
            tombstoned_video.deleted_at = datetime.now(timezone.utc)
            setup.add(tombstoned_video)
            await setup.commit()

        async with factory() as upload_db, factory() as deletion_db:
            upload_task = asyncio.create_task(
                videos.upload_reference_video(
                    SimpleNamespace(id=user_id),
                    upload_db,
                    _upload(payload),
                )
            )
            await upload_ready.wait()

            deleting_user = await deletion_db.get(User, user_id)
            assert deleting_user is not None
            deleting_user.deleted_at = datetime.now(timezone.utc)
            await deletion_db.commit()

            resume_upload.set()
            with pytest.raises(HTTPException) as exc_info:
                await upload_task

            assert exc_info.value.status_code == 401
            assert exc_info.value.detail["error"]["code"] == "user_deleted"

        async with factory() as observer:
            restored_video = await observer.get(Video, video_id)

    assert write_paths == []
    assert restored_video is not None
    assert restored_video.deleted_at is not None
    assert not any(
        path.is_file()
        for path in Path(videos.settings.storage_root).glob(f"u/{user_id}/vref/**/*")
    )


@pytest.mark.asyncio
async def test_cleanup_failure_stays_counted_then_recovers_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(700, b"c")
    _configure_storage(
        monkeypatch,
        tmp_path,
        free_bytes=10 * 1024 * 1024,
        minimum_free_bytes=100,
    )
    storage_root = Path(videos.settings.storage_root)
    artifact_root = storage_root / "u/user-clean/vref/video-clean"
    files = {
        "original.mp4": payload,
        "poster.jpg": b"poster",
        "video-clean.video_ref_seedance_r2v_mp4.mp4": b"reference-variant",
        "video-clean.volcano_asset_video_v1.mp4": b"transcoded-variant",
        "stale-recovery.tmp": b"stale",
    }
    artifact_root.mkdir(parents=True)
    for name, data in files.items():
        (artifact_root / name).write_bytes(data)

    async def allow_delete(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        videos.asset_ref_service,
        "ensure_asset_not_canvas_referenced",
        allow_delete,
    )
    real_unlink = VideoStorageLifecycle._unlink_entry
    failed_once = False

    def fail_poster_once(
        self: VideoStorageLifecycle,
        name: str,
        *,
        directory_fd: int,
    ) -> None:
        nonlocal failed_once
        if name == "poster.jpg" and not failed_once:
            failed_once = True
            raise PermissionError(errno.EACCES, "simulated cleanup failure")
        real_unlink(self, name, directory_fd=directory_fd)

    monkeypatch.setattr(
        VideoStorageLifecycle,
        "_unlink_entry",
        fail_poster_once,
    )
    real_cleanup = VideoStorageLifecycle.cleanup
    cleanup_calls = 0

    async with _database(tmp_path) as factory:

        async def assert_durable_tombstone(
            self: VideoStorageLifecycle,
            row: Video,
        ) -> Any:
            nonlocal cleanup_calls
            cleanup_calls += 1
            async with factory() as observer:
                persisted = await observer.get(Video, row.id)
                assert persisted is not None
                assert persisted.deleted_at is not None
                state = persisted.metadata_jsonb[VIDEO_STORAGE_CLEANUP_METADATA_KEY][
                    "state"
                ]
                assert state == ("pending" if cleanup_calls <= 2 else "complete")
            return await real_cleanup(self, row)

        monkeypatch.setattr(
            VideoStorageLifecycle,
            "cleanup",
            assert_durable_tombstone,
        )
        async with factory() as session:
            await _seed_user(session, user_id="user-clean")
            video = _video(
                video_id="video-clean",
                user_id="user-clean",
                payload=payload,
                poster_storage_key=("u/user-clean/vref/video-clean/poster.jpg"),
                metadata={
                    "source": "uploaded_reference",
                    "upstream_reference_video_variant": {
                        "kind": "video_ref_seedance_r2v_mp4",
                        "storage_key": (
                            "u/user-clean/vref/video-clean/"
                            "video-clean.video_ref_seedance_r2v_mp4.mp4"
                        ),
                        "sha256": "a" * 64,
                    },
                    "volcano_asset_video_variant": {
                        "kind": "volcano_asset_video_v1",
                        "storage_key": (
                            "u/user-clean/vref/video-clean/"
                            "video-clean.volcano_asset_video_v1.mp4"
                        ),
                        "sha256": "b" * 64,
                    },
                },
            )
            session.add(video)
            await session.commit()

            with pytest.raises(HTTPException) as exc_info:
                await videos.delete_video(
                    video.id,
                    SimpleNamespace(id="user-clean"),
                    session,
                )

            assert exc_info.value.status_code == 503
            await session.refresh(video)
            assert video.deleted_at is not None
            assert (
                video.metadata_jsonb[VIDEO_STORAGE_CLEANUP_METADATA_KEY]["state"]
                == "pending"
            )
            inspection = await VideoStorageLifecycle(
                videos.settings.storage_root
            ).inspect(video)
            count, accounted_bytes = video_reference_quota_contribution(
                video,
                inspection,
            )
            assert count == 1
            assert accounted_bytes == len(b"poster")
            assert (artifact_root / "poster.jpg").is_file()
            assert sum(path.is_file() for path in artifact_root.iterdir()) == 1

            response = await videos.delete_video(
                video.id,
                SimpleNamespace(id="user-clean"),
                session,
            )
            assert response.status_code == 204
            await session.refresh(video)
            assert video.deleted_at is not None
            assert not any(path.is_file() for path in artifact_root.iterdir())

            repeated = await videos.delete_video(
                video.id,
                SimpleNamespace(id="user-clean"),
                session,
            )
            assert repeated.status_code == 204


@pytest.mark.asyncio
async def test_cleanup_never_deletes_variant_outside_owned_video_root(
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    own_root = storage_root / "u/user-safe/vref/video-safe"
    other_root = storage_root / "u/user-safe/vref/video-other"
    own_root.mkdir(parents=True)
    other_root.mkdir(parents=True)
    own_file = own_root / "original.mp4"
    other_file = other_root / "must-remain.mp4"
    own_file.write_bytes(b"own")
    other_file.write_bytes(b"other")
    video = _video(
        video_id="video-safe",
        user_id="user-safe",
        payload=b"own",
        metadata={
            "source": "uploaded_reference",
            "upstream_reference_video_variant": {
                "kind": "video_ref_seedance_r2v_mp4",
                "storage_key": ("u/user-safe/vref/video-other/must-remain.mp4"),
                "sha256": "c" * 64,
            },
        },
    )

    result = await VideoStorageLifecycle(storage_root).cleanup(video)

    assert result.complete is False
    assert not own_file.exists()
    assert other_file.read_bytes() == b"other"
    assert any(
        error == "artifact_outside_owned_root:upstream_reference_video_variant"
        for error in result.errors
    )


def test_completed_video_cleanup_compacts_sensitive_metadata() -> None:
    video = SimpleNamespace(
        poster_storage_key="u/user-1/vref/video-1/poster.jpg",
        metadata_jsonb={
            "source": "uploaded_reference",
            "filename": "x" * 10_000,
            "reference_access_token": "secret-token",
            "reference_access_token_expires_at": "2099-01-01T00:00:00+00:00",
            "upstream_reference_video_variant": {
                "storage_key": "u/user-1/vref/video-1/variant.mp4",
            },
        },
    )
    empty = VideoArtifactInspection(
        artifact_count=0,
        bytes_on_disk=0,
        primary_present=False,
        primary_size_bytes=0,
    )

    record_video_storage_cleanup(
        video,
        VideoArtifactCleanupResult(
            complete=True,
            deleted_artifacts=2,
            remaining=empty,
        ),
    )

    assert video.poster_storage_key is None
    assert set(video.metadata_jsonb) == {
        "source",
        VIDEO_STORAGE_CLEANUP_METADATA_KEY,
    }
    assert (
        video.metadata_jsonb[VIDEO_STORAGE_CLEANUP_METADATA_KEY]["state"] == "complete"
    )


@pytest.mark.asyncio
async def test_stale_not_adopted_marker_never_deletes_replaced_file(
    tmp_path: Path,
) -> None:
    lifecycle = VideoStorageLifecycle(tmp_path)
    storage_key = "u/user-race/vref/video-race/original.mp4"
    path = tmp_path / storage_key
    path.parent.mkdir(parents=True)
    path.write_bytes(b"first")
    marker = await lifecycle.record_upload_adoption_pending(
        video_id="video-race",
        user_id="user-race",
        storage_key=storage_key,
        sha256=hashlib.sha256(b"first").hexdigest(),
    )
    replacement = path.with_name("replacement.mp4")
    replacement.write_bytes(b"adopted-by-new-request")
    replacement.replace(path)

    removed = await lifecycle.discard_unadopted_upload(marker)

    assert removed is False
    assert path.read_bytes() == b"adopted-by-new-request"
    assert not marker.marker_path.exists()


@pytest.mark.asyncio
async def test_video_transcode_capacity_fails_fast_at_global_limit() -> None:
    class Redis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        async def set(
            self,
            key: str,
            value: str,
            *,
            nx: bool,
            ex: int,
        ) -> bool:
            assert nx is True
            assert ex > 0
            if key in self.values:
                return False
            self.values[key] = value
            return True

        async def eval(
            self,
            script: str,
            _keys: int,
            key: str,
            owner: str,
            *_args: str,
        ) -> int:
            if self.values.get(key) != owner:
                return 0
            if "DEL" in script:
                del self.values[key]
            return 1

    manager = VideoTranscodeCapacityManager(
        Redis(),
        limit=1,
        wait_timeout_seconds=0.05,
        lease_ttl_seconds=30,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def occupy() -> None:
        async with manager.hold(user_id="user-first"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(occupy())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        with pytest.raises(VideoTranscodeCapacityUnavailable):
            async with manager.hold(user_id="user-second"):
                pytest.fail("second transcode must not enter")
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_reference_variant_reserves_max_output_before_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = _video(
        video_id="video-transcode",
        user_id="user-transcode",
        payload=_payload(128),
    )
    source = tmp_path / video.storage_key
    source.parent.mkdir(parents=True)
    source.write_bytes(_payload(128))
    order: list[str] = []
    reserved: list[int] = []

    class Result:
        def scalar_one_or_none(self) -> Video:
            return video

    class Db:
        async def execute(self, _statement: Any) -> Result:
            return Result()

    class TranscodeCapacity:
        @asynccontextmanager
        async def hold(self, *, user_id: str) -> AsyncIterator[None]:
            assert user_id == video.user_id
            order.append("transcode-capacity")
            yield

    class StorageCapacity:
        @asynccontextmanager
        async def reserve(self, bytes_required: int) -> AsyncIterator[None]:
            reserved.append(bytes_required)
            order.append("storage-capacity")
            yield

    async def render(source_path: Path, destination: Path) -> Any:
        assert source_path == source
        order.append("render")
        destination.write_bytes(b"variant")
        return video_reference_videos.VideoReferenceMp4(
            width=1280,
            height=720,
            duration_ms=5_000,
            fps=30.0,
            has_audio=True,
            size_bytes=7,
            sha256=hashlib.sha256(b"variant").hexdigest(),
        )

    monkeypatch.setattr(
        video_reference_videos,
        "_render_reference_variant",
        render,
    )

    variant = await video_reference_videos.ensure_video_reference_video_variant(
        Db(),  # type: ignore[arg-type]
        video,
        storage_root=str(tmp_path),
        storage_capacity=StorageCapacity(),  # type: ignore[arg-type]
        transcode_capacity=TranscodeCapacity(),  # type: ignore[arg-type]
    )

    assert order == ["transcode-capacity", "storage-capacity", "render"]
    assert reserved == [2 * video_reference_videos.VIDEO_REFERENCE_VIDEO_MAX_BYTES]
    assert variant["size_bytes"] == 7


@pytest.mark.asyncio
async def test_deleted_reference_inventory_is_cleaned_in_bounded_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_storage(
        monkeypatch,
        tmp_path,
        free_bytes=100 * 1024 * 1024,
        minimum_free_bytes=100,
    )
    user_id = "user-history"
    deleted_payload = _payload(128, b"d")

    async with _database(tmp_path) as factory:
        async with factory() as session:
            await _seed_user(session, user_id=user_id)
            for index in range(40):
                row = _video(
                    video_id=f"deleted-{index:02d}",
                    user_id=user_id,
                    payload=deleted_payload,
                    metadata={
                        "source": "uploaded_reference",
                        "filename": "x" * 10_000,
                    },
                )
                row.deleted_at = datetime.now(timezone.utc)
                session.add(row)
            await session.commit()

            with pytest.raises(HTTPException) as exc_info:
                await videos.upload_reference_video(
                    SimpleNamespace(id=user_id),
                    session,
                    _upload(_payload(256, b"a"), "first.mp4"),
                )

            assert exc_info.value.status_code == 503
            assert exc_info.value.detail["error"]["code"] == "video_cleanup_backlog"
            rows = (
                (await session.execute(select(Video).where(Video.user_id == user_id)))
                .scalars()
                .all()
            )
            assert (
                sum(
                    row.metadata_jsonb.get(
                        VIDEO_STORAGE_CLEANUP_METADATA_KEY,
                        {},
                    ).get("state")
                    == "complete"
                    for row in rows
                )
                == 32
            )

            created = await videos.upload_reference_video(
                SimpleNamespace(id=user_id),
                session,
                _upload(_payload(256, b"b"), "second.mp4"),
            )
            assert created.created is True

            deleted_rows = (
                (
                    await session.execute(
                        select(Video).where(
                            Video.user_id == user_id,
                            Video.deleted_at.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(deleted_rows) == 40
            assert all(
                set(row.metadata_jsonb)
                == {"source", VIDEO_STORAGE_CLEANUP_METADATA_KEY}
                for row in deleted_rows
            )

            inspected_counts: list[int] = []
            real_inspect_many = VideoStorageLifecycle.inspect_many

            async def inspect_many(
                self: VideoStorageLifecycle,
                candidates: Any,
            ) -> dict[str, VideoArtifactInspection]:
                candidate_rows = list(candidates)
                inspected_counts.append(len(candidate_rows))
                return await real_inspect_many(self, candidate_rows)

            monkeypatch.setattr(VideoStorageLifecycle, "inspect_many", inspect_many)
            await videos.upload_reference_video(
                SimpleNamespace(id=user_id),
                session,
                _upload(_payload(300, b"c"), "third.mp4"),
            )

            assert inspected_counts == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["repair-existing", "create-new"])
@pytest.mark.parametrize(
    "outcome",
    [
        video_upload_routes.VideoUploadAdoption.ADOPTED,
        video_upload_routes.VideoUploadAdoption.NOT_ADOPTED,
        video_upload_routes.VideoUploadAdoption.UNKNOWN,
    ],
)
async def test_reference_upload_commit_ack_uses_three_state_adoption(
    branch: str,
    outcome: video_upload_routes.VideoUploadAdoption,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(512, b"k")
    user_id = f"user-{branch}-{outcome.value}"
    _configure_storage(
        monkeypatch,
        tmp_path,
        free_bytes=100 * 1024 * 1024,
        minimum_free_bytes=100,
    )

    class AmbiguousCommitSession:
        def __init__(self, session: AsyncSession) -> None:
            self.session = session

        def __getattr__(self, name: str) -> Any:
            return getattr(self.session, name)

        async def commit(self) -> None:
            if outcome is video_upload_routes.VideoUploadAdoption.ADOPTED:
                await self.session.commit()
            raise TimeoutError("commit acknowledgement unknown")

        async def rollback(self) -> None:
            await self.session.rollback()

    async with _database(tmp_path) as factory:
        monkeypatch.setattr(videos, "SessionLocal", factory)
        existing_id = "video-repair"
        async with factory() as setup:
            await _seed_user(setup, user_id=user_id)
            if branch == "repair-existing":
                setup.add(
                    _video(
                        video_id=existing_id,
                        user_id=user_id,
                        payload=payload,
                        storage_key=(f"u/{user_id}/vref/{existing_id}/original.mov"),
                    )
                )
                await setup.commit()

        if outcome is video_upload_routes.VideoUploadAdoption.UNKNOWN:

            async def unknown_probe(
                **_kwargs: Any,
            ) -> video_upload_routes.VideoUploadAdoptionProbe:
                return video_upload_routes.VideoUploadAdoptionProbe(
                    video_upload_routes.VideoUploadAdoption.UNKNOWN
                )

            monkeypatch.setattr(
                videos,
                "_probe_reference_video_adoption",
                unknown_probe,
            )

        async with factory() as primary:
            session = AmbiguousCommitSession(primary)
            if outcome is video_upload_routes.VideoUploadAdoption.ADOPTED:
                result = await videos.upload_reference_video(
                    SimpleNamespace(id=user_id),
                    session,  # type: ignore[arg-type]
                    _upload(payload, "reference.mp4"),
                )
                assert result.created is (branch == "create-new")
            elif outcome is video_upload_routes.VideoUploadAdoption.NOT_ADOPTED:
                with pytest.raises(TimeoutError):
                    await videos.upload_reference_video(
                        SimpleNamespace(id=user_id),
                        session,  # type: ignore[arg-type]
                        _upload(payload, "reference.mp4"),
                    )
            else:
                with pytest.raises(HTTPException) as exc_info:
                    await videos.upload_reference_video(
                        SimpleNamespace(id=user_id),
                        session,  # type: ignore[arg-type]
                        _upload(payload, "reference.mp4"),
                    )
                assert exc_info.value.status_code == 503
                assert (
                    exc_info.value.detail["error"]["code"]
                    == "video_upload_commit_unknown"
                )

        storage_root = Path(videos.settings.storage_root)
        stored_files = list(storage_root.glob(f"u/{user_id}/vref/*/original.mp4"))
        markers = await VideoStorageLifecycle(
            storage_root
        ).aged_upload_adoption_markers(
            user_id=user_id,
            minimum_age_seconds=0,
        )
        async with factory() as observer:
            rows = (
                (await observer.execute(select(Video).where(Video.user_id == user_id)))
                .scalars()
                .all()
            )

        if outcome is video_upload_routes.VideoUploadAdoption.ADOPTED:
            assert len(stored_files) == 1
            assert stored_files[0].read_bytes() == payload
            assert len(rows) == 1
            assert rows[0].storage_key.endswith("/original.mp4")
            assert markers == ()
        elif outcome is video_upload_routes.VideoUploadAdoption.NOT_ADOPTED:
            assert stored_files == []
            assert len(rows) == (1 if branch == "repair-existing" else 0)
            if rows:
                assert rows[0].storage_key.endswith("/original.mov")
            assert markers == ()
        else:
            assert len(stored_files) == 1
            assert stored_files[0].read_bytes() == payload
            assert len(rows) == (1 if branch == "repair-existing" else 0)
            if rows:
                assert rows[0].storage_key.endswith("/original.mov")
            assert len(markers) == 1


@pytest.mark.asyncio
async def test_upload_quota_counts_generated_reference_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_storage(
        monkeypatch,
        tmp_path,
        free_bytes=2 * VIDEO_REFERENCE_STORAGE_QUOTA_BYTES,
        minimum_free_bytes=100,
    )
    user_id = "user-derived-quota"
    generated = _video(
        video_id="generated-video",
        user_id=user_id,
        payload=_payload(128),
        storage_key=f"u/{user_id}/v/generated/final.mp4",
        metadata={
            "upstream_reference_video_variant": {
                "kind": "video_ref_seedance_r2v_mp4",
                "storage_key": (
                    f"u/{user_id}/v/generated/generated-video.reference.mp4"
                ),
                "size_bytes": VIDEO_REFERENCE_STORAGE_QUOTA_BYTES - 16,
                "sha256": "a" * 64,
            }
        },
    )

    async with _database(tmp_path) as factory:
        async with factory() as session:
            await _seed_user(session, user_id=user_id)
            session.add(generated)
            await session.commit()

            with pytest.raises(HTTPException) as exc_info:
                await videos.upload_reference_video(
                    SimpleNamespace(id=user_id),
                    session,
                    _upload(_payload(32, b"q")),
                )

            assert exc_info.value.status_code == 429
            assert (
                exc_info.value.detail["error"]["code"]
                == "reference_video_quota_exceeded"
            )


@pytest.mark.asyncio
async def test_seedance_variant_quota_is_checked_after_user_lock_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = "user-seedance-quota"
    source_payload = _payload(128)
    source = _video(
        video_id="generated-source",
        user_id=user_id,
        payload=source_payload,
        storage_key=f"u/{user_id}/v/generation/final.mp4",
    )
    retained = _video(
        video_id="retained-reference",
        user_id=user_id,
        payload=_payload(64),
    )
    retained.size_bytes = VIDEO_REFERENCE_STORAGE_QUOTA_BYTES - 5
    source_path = tmp_path / source.storage_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source_payload)

    class TranscodeCapacity:
        @asynccontextmanager
        async def hold(self, *, user_id: str) -> AsyncIterator[None]:
            assert user_id == source.user_id
            yield

    class StorageCapacity:
        @asynccontextmanager
        async def reserve(self, _bytes_required: int) -> AsyncIterator[None]:
            yield

    async def render(_source: Path, staged: Path) -> Any:
        payload = b"0123456789"
        staged.write_bytes(payload)
        return video_reference_videos.VideoReferenceMp4(
            width=1280,
            height=720,
            duration_ms=5_000,
            fps=30.0,
            has_audio=False,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    monkeypatch.setattr(
        video_reference_videos,
        "_render_reference_variant",
        render,
    )

    async with _database(tmp_path) as factory:
        async with factory() as session:
            await _seed_user(session, user_id=user_id)
            session.add_all([source, retained])
            await session.commit()

            with pytest.raises(
                video_reference_videos.VideoReferenceVideoError
            ) as exc_info:
                await video_reference_videos.ensure_video_reference_video_variant(
                    session,
                    source,
                    storage_root=str(tmp_path),
                    storage_capacity=StorageCapacity(),  # type: ignore[arg-type]
                    transcode_capacity=TranscodeCapacity(),  # type: ignore[arg-type]
                )

            assert exc_info.value.code == "reference_video_quota_exceeded"
            assert exc_info.value.status_code == 429
            destination = tmp_path / video_reference_videos.video_reference_video_key(
                source
            )
            assert not destination.exists()
            assert "upstream_reference_video_variant" not in source.metadata_jsonb


@pytest.mark.asyncio
async def test_reference_upload_adoption_requires_matching_file_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(videos.settings, "storage_root", str(storage_root))
    user_id = "user-adoption-identity"
    payload = _payload(128)
    row = _video(
        video_id="video-adoption-identity",
        user_id=user_id,
        payload=payload,
    )

    async with _database(tmp_path) as factory:
        monkeypatch.setattr(videos, "SessionLocal", factory)
        async with factory() as setup:
            await _seed_user(setup, user_id=user_id)
            setup.add(row)
            await setup.commit()

        probe = await videos._probe_reference_video_adoption(  # noqa: SLF001
            video_id=row.id,
            user_id=user_id,
            storage_key=row.storage_key,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
        )
        assert probe.outcome is video_upload_routes.VideoUploadAdoption.UNKNOWN

        path = storage_root / row.storage_key
        path.parent.mkdir(parents=True)
        path.write_bytes(b"z" * len(payload))
        probe = await videos._probe_reference_video_adoption(  # noqa: SLF001
            video_id=row.id,
            user_id=user_id,
            storage_key=row.storage_key,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
        )
        assert probe.outcome is video_upload_routes.VideoUploadAdoption.UNKNOWN

        path.write_bytes(payload)
        probe = await videos._probe_reference_video_adoption(  # noqa: SLF001
            video_id=row.id,
            user_id=user_id,
            storage_key=row.storage_key,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
        )
        assert probe.outcome is video_upload_routes.VideoUploadAdoption.ADOPTED


def test_seedance_final_install_fsyncs_new_parents_and_final_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / "staged.mp4"
    payload = b"seedance-variant"
    staged.write_bytes(payload)
    destination = tmp_path / "new" / "nested" / "reference.mp4"
    events: list[tuple[str, Path]] = []
    original_replace = video_reference_videos.os.replace

    def replace(source: Path, target: Path) -> None:
        events.append(("replace", target))
        original_replace(source, target)

    def fsync_directory(path: Path) -> None:
        events.append(("fsync", path))

    monkeypatch.setattr(video_reference_videos.os, "replace", replace)
    monkeypatch.setattr(
        video_reference_videos,
        "_fsync_directory",
        fsync_directory,
    )

    video_reference_videos._install_staged_variant(  # noqa: SLF001
        staged,
        destination,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    replace_index = events.index(("replace", destination))
    assert ("fsync", tmp_path) in events[:replace_index]
    assert ("fsync", destination.parent.parent) in events[:replace_index]
    assert events[-1] == ("fsync", destination.parent)
    assert destination.read_bytes() == payload


@pytest.mark.asyncio
async def test_adoption_marker_fsyncs_final_directory_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = VideoStorageLifecycle(tmp_path)
    storage_key = "u/user-marker/vref/video-marker/original.mp4"
    artifact = tmp_path / storage_key
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"marker-artifact")
    events: list[tuple[str, Path]] = []
    original_replace = video_storage_lifecycle_module.os.replace

    def replace(source: Path, target: Path) -> None:
        events.append(("replace", target))
        original_replace(source, target)

    def fsync_directory(path: Path) -> None:
        events.append(("fsync", path))

    monkeypatch.setattr(
        video_storage_lifecycle_module.os,
        "replace",
        replace,
    )
    monkeypatch.setattr(
        VideoStorageLifecycle,
        "_fsync_directory",
        staticmethod(fsync_directory),
    )

    marker = await lifecycle.record_upload_adoption_pending(
        video_id="video-marker",
        user_id="user-marker",
        storage_key=storage_key,
        sha256=hashlib.sha256(b"marker-artifact").hexdigest(),
    )

    replace_index = events.index(("replace", marker.marker_path))
    assert events[-1] == ("fsync", marker.marker_path.parent)
    assert replace_index < len(events) - 1
    assert marker.marker_path.is_file()


@pytest.mark.asyncio
async def test_locked_video_requery_refreshes_concurrent_restore_identity(
    tmp_path: Path,
) -> None:
    user_id = "user-refresh"
    payload = _payload(128)
    row = _video(
        video_id="video-refresh",
        user_id=user_id,
        payload=payload,
    )

    async with _database(tmp_path) as factory:
        async with factory() as setup:
            await _seed_user(setup, user_id=user_id)
            setup.add(row)
            await setup.commit()

        async with factory() as deleting, factory() as restoring:
            stale = await video_generation_routes._locked_owned_video(  # noqa: SLF001
                deleting,
                video_id=row.id,
                user_id=user_id,
            )
            assert stale is not None
            stale.deleted_at = datetime.now(timezone.utc)
            await deleting.commit()

            restored = await restoring.get(Video, row.id)
            assert restored is not None
            restored.deleted_at = None
            await restoring.commit()

            refreshed = await video_generation_routes._locked_owned_video(  # noqa: SLF001
                deleting,
                video_id=row.id,
                user_id=user_id,
            )
            assert refreshed is stale
            assert refreshed.deleted_at is None


@pytest.mark.asyncio
async def test_deleted_video_locked_requery_none_never_uses_stale_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = SimpleNamespace(
        id="video-deletion-race",
        user_id="user-deletion-race",
        deleted_at=None,
        metadata_jsonb={},
        size_bytes=128,
    )
    video_reads = 0
    cleanup_calls = 0

    class Result:
        def __init__(self, value: Any) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

    class Db:
        async def execute(self, statement: Any) -> Result:
            nonlocal video_reads
            if "FROM videos" in str(statement):
                assert statement.get_execution_options()["populate_existing"] is True
                video_reads += 1
                return Result(video if video_reads == 1 else None)
            return Result(None)

        async def commit(self) -> None:
            return None

    class Lifecycle:
        async def cleanup(self, _video: Any) -> Any:
            nonlocal cleanup_calls
            cleanup_calls += 1
            raise AssertionError("stale deleted video must never reach cleanup")

    async def no_active_reference(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_canvas_reference(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        video_generation_routes,
        "active_video_generation_reference_id",
        no_active_reference,
    )
    deps = SimpleNamespace(
        http_error=videos._http,  # noqa: SLF001
        ensure_not_canvas_referenced=no_canvas_reference,
        storage_lifecycle=Lifecycle(),
        logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
    )

    with pytest.raises(HTTPException) as exc_info:
        await video_generation_routes.delete_video(
            video_id=video.id,
            user=SimpleNamespace(id=video.user_id),
            db=Db(),  # type: ignore[arg-type]
            deps=deps,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 404
    assert cleanup_calls == 0
