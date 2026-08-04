from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.schema import Table

from apps.worker.app.tasks.completion_parts import (
    image_storage_runtime as worker_completion_storage,
)
from apps.worker.app.tasks.generation_parts.success import generation_artifact_keys
from apps.worker.app.tasks.generation_parts.takeover_checkpoint import (
    GENERATION_TAKEOVER_CHECKPOINT_KEY,
    GenerationTakeoverCheckpoint,
    GenerationTakeoverPayload,
    batch_extra_generation_id,
)

from app.images.application import (
    http_routes,
    storage_maintenance,
)
from app.images.application.create_variant import (
    CreateVariantService,
    VariantResult,
)
from app.images.application.storage_maintenance import sweep_orphan_image_files
from app.images.adapters.filesystem_store import FileSystemArtifactStore
from app.images.adapters.sqlalchemy_variants import SQLAlchemyVariantRepository
from app.images.domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    PublishedArtifact,
)
from app.images.domain.variants import DISPLAY_VARIANT, deterministic_variant_key
from app.images.ports.image_processing import (
    ImageVariantProcessingRequest,
    PreparedImageVariant,
)
from app.services.video_storage_lifecycle import VideoStorageLifecycle
from app.workflows.adapters import workflow_runtime
from lumen_core.byok_retention import (
    ByokRetentionPolicy,
    prune_expired_byok_user_data,
)
from lumen_core.models import (
    Base,
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    ImageVariantClaim,
    Message,
    ModelLibraryItem,
    User,
    Video,
    VideoGeneration,
)


_NOW = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)


class _CapturedWorkerStorageKeys(RuntimeError):
    pass


class _AlwaysOwnedVariantLease:
    async def renew(self) -> bool:
        return True

    async def release(self) -> None:
        return None


class _UnlimitedVariantCapacity:
    async def reserve(self, _request: Any) -> _AlwaysOwnedVariantLease:
        return _AlwaysOwnedVariantLease()


class _StaticVariantExecutor:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def render_variant(
        self,
        request: ImageVariantProcessingRequest,
    ) -> PreparedImageVariant:
        request.output_path.write_bytes(self.payload)
        return PreparedImageVariant(
            output_path=request.output_path,
            mime="image/webp",
            width=32,
            height=32,
            size_bytes=len(self.payload),
            sha256=hashlib.sha256(self.payload).hexdigest(),
        )


class _ObservedVariantArtifactStore(FileSystemArtifactStore):
    def __init__(
        self,
        root: Path,
        *,
        fence_attempted: asyncio.Event | None = None,
    ) -> None:
        super().__init__(root)
        self.fence_attempted = fence_attempted
        self.publish_created: list[bool] = []

    def artifact_lifecycle_fence(
        self,
        key: ArtifactKey,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        fence = super().artifact_lifecycle_fence(
            key,
            timeout_seconds=timeout_seconds,
        )

        @asynccontextmanager
        async def observed_fence() -> AsyncIterator[None]:
            if self.fence_attempted is not None:
                self.fence_attempted.set()
            async with fence:
                yield

        return observed_fence()

    async def publish_path(
        self,
        source: Path,
        key: ArtifactKey,
        *,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact:
        published = await super().publish_path(
            source,
            key,
            expected=expected,
        )
        self.publish_created.append(published.created)
        return published


def _create_database_tables(
    sync_connection: Any,
    selected_tables: Sequence[Table],
) -> None:
    task_tables = {Generation.__table__, Completion.__table__}
    regular_tables = [table for table in selected_tables if table not in task_tables]
    Base.metadata.create_all(sync_connection, tables=regular_tables)
    for table in selected_tables:
        if table not in task_tables:
            continue
        task_ddl = str(CreateTable(table).compile(dialect=sqlite.dialect())).replace(
            "DEFAULT (ARRAY[]::varchar[])",
            "DEFAULT '[]'",
        )
        sync_connection.exec_driver_sql(task_ddl)


@asynccontextmanager
async def _database(
    tables: Sequence[Table],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    selected_tables = list(tables)
    if User.__table__ not in selected_tables:
        selected_tables.append(User.__table__)
    if VideoGeneration.__table__ not in selected_tables:
        selected_tables.append(VideoGeneration.__table__)
    if Generation.__table__ not in selected_tables:
        selected_tables.append(Generation.__table__)
    if Completion.__table__ not in selected_tables:
        selected_tables.append(Completion.__table__)
    if ImageVariantClaim.__table__ not in selected_tables:
        selected_tables.append(ImageVariantClaim.__table__)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: _create_database_tables(
                sync_connection,
                selected_tables,
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _image(
    *,
    image_id: str,
    storage_key: str,
    deleted_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> Image:
    return Image(
        id=image_id,
        user_id="user-1",
        source="generated",
        storage_key=storage_key,
        mime="image/png",
        width=64,
        height=64,
        size_bytes=5,
        sha256=(image_id[-1:] or "a") * 64,
        metadata_jsonb=metadata or {},
        artifact_status="ready",
        artifact_manifest_jsonb=manifest or {},
        deleted_at=deleted_at,
        created_at=created_at or _NOW,
    )


def _variant(
    *,
    variant_id: str,
    image_id: str,
    kind: str,
    storage_key: str,
) -> ImageVariant:
    return ImageVariant(
        id=variant_id,
        image_id=image_id,
        kind=kind,
        storage_key=storage_key,
        width=32,
        height=32,
    )


def _video(
    *,
    video_id: str,
    storage_key: str,
    poster_storage_key: str | None,
    deleted_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> Video:
    return Video(
        id=video_id,
        user_id="user-1",
        owner_generation_id=None,
        storage_key=storage_key,
        poster_storage_key=poster_storage_key,
        mime="video/mp4",
        width=1280,
        height=720,
        duration_ms=5_000,
        fps=24.0,
        size_bytes=5,
        sha256="v" * 64,
        etag="v" * 64,
        has_audio=True,
        faststart=True,
        visibility="private",
        metadata_jsonb=metadata or {},
        deleted_at=deleted_at,
        created_at=_NOW,
    )


def _video_generation(
    *,
    generation_id: str,
    status: str,
    input_image_storage_key: str | None = None,
    reference_media: list[dict[str, Any]] | None = None,
) -> VideoGeneration:
    return VideoGeneration(
        id=generation_id,
        user_id="user-1",
        action="i2v" if input_image_storage_key else "reference",
        model="seedance-2.0",
        prompt="reference",
        input_image_storage_key=input_image_storage_key,
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        generate_audio=True,
        watermark=False,
        upstream_request={"reference_media": reference_media or []},
        status=status,
        progress_stage="queued",
        progress_pct=0,
        deadline_at=_NOW + timedelta(minutes=10),
        idempotency_key=f"idempotency:{generation_id}",
        request_fingerprint=(generation_id[-1:] or "f") * 64,
        est_token_upper=1,
        est_cost_micro=1,
    )


def _completion(
    *,
    completion_id: str,
    status: str,
    attempt: int,
    execution_epoch: int,
) -> Completion:
    return Completion(
        id=completion_id,
        message_id=f"message:{completion_id}",
        user_id="user-1",
        model="gpt-test",
        input_image_ids=[],
        text="",
        status=status,
        progress_stage="finalizing",
        attempt=attempt,
        execution_epoch=execution_epoch,
        idempotency_key=f"idempotency:{completion_id}",
    )


def _user(
    *,
    user_id: str = "user-1",
    deleted_at: datetime | None = None,
) -> User:
    return User(
        id=user_id,
        email=f"{user_id}@example.com",
        display_name="User 1",
        deleted_at=deleted_at,
    )


def _write_storage_file(root: Path, key: str, payload: bytes = b"image") -> Path:
    path = root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


async def _completion_tool_artifact_keys() -> tuple[str, ...]:
    captured: list[str] = []

    async def unused_async(*_args: Any, **_kwargs: Any) -> None:
        return None

    def format_and_meta(_raw_image: bytes) -> tuple[Any, ...]:
        return (
            "png",
            "image/png",
            64,
            64,
            None,
            b"display",
            (64, 64),
            b"preview",
            (64, 64),
            b"thumb",
            (64, 64),
        )

    async def capture_write(files: list[tuple[str, bytes]]) -> list[str]:
        captured.extend(key for key, _data in files)
        raise _CapturedWorkerStorageKeys

    @asynccontextmanager
    async def unused_cleanup(_keys: list[str]) -> AsyncIterator[None]:
        yield

    service = worker_completion_storage.CompletionToolImageService(
        budget=worker_completion_storage.CompletionToolImageBudget(
            reserve=unused_async,
        ),
        codec=worker_completion_storage.CompletionToolImageCodec(
            decode=lambda _value: b"",
            format_and_meta=format_and_meta,
            sha256=lambda _value: "a" * 64,
            upstream_error_type=RuntimeError,
            bad_response_error_code="bad_response",
        ),
        repository=worker_completion_storage.CompletionToolImageRepository(
            session_factory=lambda: None,
            new_id=lambda: "completion-image",
            acquire_task_lock=unused_async,
            completion_model=object,
            superseded_error_type=RuntimeError,
            record_usage=unused_async,
            image_model=object,
            image_variant_model=object,
            message_model=object,
            public_url=lambda key: key,
        ),
        storage=worker_completion_storage.CompletionToolImageStorage(
            write_files=capture_write,
            cleanup_on_error=unused_cleanup,
            delete_files=unused_async,
        ),
        events=worker_completion_storage.CompletionToolImageEvents(
            publish=unused_async,
            image_event="image",
        ),
    )

    with pytest.raises(_CapturedWorkerStorageKeys):
        await service.store_tool_image(
            session=object(),
            task_id="completion-task",
            attempt_epoch=3,
            execution_epoch=9,
            user_id="user-1",
            message_id="message-1",
            raw_image=b"raw",
            revised_prompt=None,
            billing_budget_micro=0,
        )
    return tuple(captured)


async def _sweep(
    session: AsyncSession,
    storage_root: Path,
) -> dict[str, Any]:
    return await sweep_orphan_image_files(
        session,
        storage_root=str(storage_root),
        dry_run=False,
        max_files=100,
        max_entries=1_000,
        max_bytes=1024 * 1024,
        max_seconds=10,
        minimum_age_seconds=0,
    )


@pytest.mark.asyncio
async def test_orphan_sweep_fences_vref_recheck_and_unlink_with_upload_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_key = "u/user-race/vref/video-race/original.mp4"
    path = _write_storage_file(tmp_path, storage_key, b"deleted")
    upload_storage = VideoStorageLifecycle(tmp_path)
    live_keys: set[str] = set()
    initial_check_complete = asyncio.Event()
    deletion_lock_attempted = asyncio.Event()
    lock_identities: list[tuple[str, str]] = []
    checks: list[set[str]] = []

    class Db:
        async def execute(self, statement: Any) -> object:
            assert "FROM users" in str(statement)
            return object()

    async def known_storage_keys(
        _db: Any,
        candidates: set[str],
    ) -> set[str]:
        checks.append(set(candidates))
        if len(checks) == 1:
            initial_check_complete.set()
        return candidates & live_keys

    real_reference_lock = VideoStorageLifecycle.reference_mutation_lock

    def observed_reference_lock(
        self: VideoStorageLifecycle,
        *,
        user_id: str,
        video_id: str,
        timeout_seconds: float,
    ) -> Any:
        lock_identities.append((user_id, video_id))
        deletion_lock_attempted.set()
        return real_reference_lock(
            self,
            user_id=user_id,
            video_id=video_id,
            timeout_seconds=timeout_seconds,
        )

    async with upload_storage.reference_mutation_lock(
        user_id="user-race",
        video_id="video-race",
    ):
        monkeypatch.setattr(
            storage_maintenance,
            "_known_storage_keys",
            known_storage_keys,
        )
        monkeypatch.setattr(
            VideoStorageLifecycle,
            "reference_mutation_lock",
            observed_reference_lock,
        )
        sweep = asyncio.create_task(_sweep(Db(), tmp_path))  # type: ignore[arg-type]
        await asyncio.wait_for(initial_check_complete.wait(), timeout=2)
        await asyncio.wait_for(deletion_lock_attempted.wait(), timeout=2)
        assert not sweep.done()

        path.write_bytes(b"restored")
        live_keys.add(storage_key)

    result = await asyncio.wait_for(sweep, timeout=2)

    assert checks == [{storage_key}, {storage_key}]
    assert lock_identities == [("user-race", "video-race")]
    assert result["deleted"] == 0
    assert result["orphans"] == []
    assert path.read_bytes() == b"restored"


@pytest.mark.asyncio
async def test_worker_nested_attempt_keys_are_reclaimed_after_soft_delete(
    tmp_path: Path,
) -> None:
    generation_keys = generation_artifact_keys(
        user_id="user-1",
        task_id="generation-task",
        execution_epoch=7,
        attempt_epoch=2,
        orig_ext="png",
    )
    completion_keys = await _completion_tool_artifact_keys()
    assert all(
        "/g/generation-task/executions/7/attempts/2/" in key for key in generation_keys
    )
    assert all(
        ("/completion-tools/completion-task/executions/9/attempts/3/completion-image/")
        in key
        for key in completion_keys
    )

    all_keys = (*generation_keys, *completion_keys)
    paths = [_write_storage_file(tmp_path, key) for key in all_keys]

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add_all(
                [
                    _image(
                        image_id="generation-image",
                        storage_key=generation_keys[0],
                        deleted_at=_NOW,
                    ),
                    _image(
                        image_id="completion-image",
                        storage_key=completion_keys[0],
                        deleted_at=_NOW,
                    ),
                    *[
                        _variant(
                            variant_id=f"generation-variant-{index}",
                            image_id="generation-image",
                            kind=kind,
                            storage_key=key,
                        )
                        for index, (kind, key) in enumerate(
                            zip(
                                ("display2048", "preview1024", "thumb256"),
                                generation_keys[1:],
                                strict=True,
                            )
                        )
                    ],
                    *[
                        _variant(
                            variant_id=f"completion-variant-{index}",
                            image_id="completion-image",
                            kind=kind,
                            storage_key=key,
                        )
                        for index, (kind, key) in enumerate(
                            zip(
                                ("display2048", "preview1024", "thumb256"),
                                completion_keys[1:],
                                strict=True,
                            )
                        )
                    ],
                ]
            )
            await session.commit()

            swept = await _sweep(session, tmp_path)

            assert swept["deleted"] == len(all_keys)
            assert set(swept["orphans"]) == set(all_keys)
            assert swept["failed"] == []
            assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_video_finalization_artifacts_are_live_referenced_then_reclaimed(
    tmp_path: Path,
) -> None:
    primary_key = "u/user-1/v/video-task/final/stable-finalization/output.mp4"
    poster_key = "u/user-1/v/video-task/final/stable-finalization/poster.jpg"
    variant_key = "u/user-1/v/video-task/final/stable-finalization/reference.mp4"
    paths = [
        _write_storage_file(tmp_path, key)
        for key in (primary_key, poster_key, variant_key)
    ]

    async with _database(
        [Image.__table__, ImageVariant.__table__, Video.__table__]
    ) as factory:
        async with factory() as session:
            video = _video(
                video_id="video-row",
                storage_key=primary_key,
                poster_storage_key=poster_key,
                metadata={
                    "upstream_reference_video_variant": {
                        "storage_key": variant_key,
                    }
                },
            )
            session.add(video)
            await session.commit()

            live = await _sweep(session, tmp_path)
            assert live["deleted"] == 0
            assert all(path.exists() for path in paths)

            video.deleted_at = _NOW
            await session.commit()
            reclaimed = await _sweep(session, tmp_path)

            assert reclaimed["deleted"] == 3
            assert set(reclaimed["orphans"]) == {
                primary_key,
                poster_key,
                variant_key,
            }
            assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_active_video_generation_pins_soft_deleted_input_image(
    tmp_path: Path,
) -> None:
    storage_key = "u/user-1/uploads/active-input.png"
    path = _write_storage_file(tmp_path, storage_key)

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add(
                _image(
                    image_id="active-input",
                    storage_key=storage_key,
                    deleted_at=_NOW,
                )
            )
            session.add(_user())
            generation = _video_generation(
                generation_id="generation-input",
                status="queued",
                input_image_storage_key=storage_key,
            )
            session.add(generation)
            await session.commit()

            retained = await _sweep(session, tmp_path)
            assert retained["deleted"] == 0
            assert path.is_file()

            generation.status = "failed"
            await session.commit()
            reclaimed = await _sweep(session, tmp_path)
            assert reclaimed["deleted"] == 1
            assert not path.exists()


@pytest.mark.asyncio
async def test_active_video_generation_pins_soft_deleted_reference_video(
    tmp_path: Path,
) -> None:
    storage_key = "u/user-1/vref/reference-video/original.mp4"
    path = _write_storage_file(tmp_path, storage_key)

    async with _database(
        [Image.__table__, ImageVariant.__table__, Video.__table__]
    ) as factory:
        async with factory() as session:
            session.add(
                _video(
                    video_id="reference-video",
                    storage_key=storage_key,
                    poster_storage_key=None,
                    deleted_at=_NOW,
                )
            )
            session.add(_user())
            generation = _video_generation(
                generation_id="generation-reference",
                status="running",
                reference_media=[
                    {
                        "kind": "video",
                        "video_id": "reference-video",
                        "storage_key": storage_key,
                    }
                ],
            )
            session.add(generation)
            await session.commit()

            retained = await _sweep(session, tmp_path)
            assert retained["deleted"] == 0
            assert path.is_file()

            generation.status = "succeeded"
            await session.commit()
            reclaimed = await _sweep(session, tmp_path)
            assert reclaimed["deleted"] == 1
            assert not path.exists()


@pytest.mark.asyncio
async def test_active_video_generation_pins_deterministic_finalization_output(
    tmp_path: Path,
) -> None:
    storage_key = "u/user-1/v/generation-output/final/stable-finalization/output.mp4"
    path = _write_storage_file(tmp_path, storage_key)

    async with _database(
        [Image.__table__, ImageVariant.__table__, Video.__table__]
    ) as factory:
        async with factory() as session:
            session.add(_user())
            generation = _video_generation(
                generation_id="generation-output",
                status="running",
            )
            session.add(generation)
            await session.commit()

            retained = await _sweep(session, tmp_path)
            assert retained["deleted"] == 0
            assert path.is_file()

            generation.status = "failed"
            await session.commit()
            reclaimed = await _sweep(session, tmp_path)
            assert reclaimed["deleted"] == 1
            assert not path.exists()


@pytest.mark.asyncio
async def test_deleted_user_cancel_fenced_video_generations_do_not_pin_storage(
    tmp_path: Path,
) -> None:
    input_key = "u/user-1/uploads/deleted-account-input.png"
    reference_key = "u/user-1/vref/deleted-account-reference/original.mp4"
    output_key = (
        "u/user-1/v/deleted-account-output/final/stable-finalization/output.mp4"
    )
    paths = {
        key: _write_storage_file(tmp_path, key)
        for key in (input_key, reference_key, output_key)
    }

    async with _database(
        [Image.__table__, ImageVariant.__table__, Video.__table__]
    ) as factory:
        async with factory() as session:
            input_generation = _video_generation(
                generation_id="deleted-account-input",
                status="queued",
                input_image_storage_key=input_key,
            )
            reference_generation = _video_generation(
                generation_id="deleted-account-reference",
                status="running",
                reference_media=[
                    {
                        "kind": "video",
                        "video_id": "deleted-account-reference",
                        "storage_key": reference_key,
                    }
                ],
            )
            output_generation = _video_generation(
                generation_id="deleted-account-output",
                status="submitted",
            )
            for generation in (
                input_generation,
                reference_generation,
                output_generation,
            ):
                generation.cancel_requested_at = _NOW

            session.add_all(
                [
                    _user(deleted_at=_NOW),
                    _image(
                        image_id="deleted-account-input",
                        storage_key=input_key,
                        deleted_at=_NOW,
                    ),
                    _video(
                        video_id="deleted-account-reference",
                        storage_key=reference_key,
                        poster_storage_key=None,
                        deleted_at=_NOW,
                    ),
                    input_generation,
                    reference_generation,
                    output_generation,
                ]
            )
            await session.commit()

            swept = await _sweep(session, tmp_path)

            assert swept["deleted"] == 3
            assert set(swept["orphans"]) == {input_key, reference_key, output_key}
            assert all(not path.exists() for path in paths.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "user_deleted_at", "expected_deleted"),
    [
        pytest.param("queued", None, 0, id="live-queued"),
        pytest.param("running", None, 0, id="live-running"),
        pytest.param("succeeded", None, 1, id="terminal-succeeded"),
        pytest.param("failed", None, 1, id="terminal-failed"),
        pytest.param("canceled", None, 1, id="terminal-canceled"),
        pytest.param("running", _NOW, 1, id="deleted-user-running"),
    ],
)
async def test_generation_takeover_checkpoint_only_pins_recoverable_live_generation(
    tmp_path: Path,
    status: str,
    user_deleted_at: datetime | None,
    expected_deleted: int,
) -> None:
    payload = b"checkpointed-image-result"
    storage_key = (
        "u/user-1/g/generation-checkpoint/executions/4/attempts/1/takeover-result.bin"
    )
    path = _write_storage_file(tmp_path, storage_key, payload)
    checkpoint = GenerationTakeoverCheckpoint.from_legacy_payload(
        execution_epoch=4,
        attempt=1,
        payload=GenerationTakeoverPayload(
            storage_key=storage_key,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            revised_prompt="restored",
        ),
        provider="provider-1",
        route="image2",
        source="image2_direct",
        endpoint="images/generations",
    )

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add(_user(deleted_at=user_deleted_at))
            generation = Generation(
                id="generation-checkpoint",
                message_id="message-checkpoint",
                user_id="user-1",
                action="generate",
                model="gpt-image-test",
                prompt="render",
                size_requested="1024x1024",
                aspect_ratio="1:1",
                input_image_ids=[],
                upstream_request={
                    GENERATION_TAKEOVER_CHECKPOINT_KEY: checkpoint.to_mapping()
                },
                status=status,
                progress_stage=("queued" if status == "queued" else "finalizing"),
                attempt=1,
                execution_epoch=4,
                idempotency_key="idempotency-checkpoint",
            )
            session.add(generation)
            await session.commit()

            swept = await _sweep(session, tmp_path)

            assert swept["deleted"] == expected_deleted
            assert swept["orphans"] == ([storage_key] if expected_deleted else [])
            assert path.exists() is (expected_deleted == 0)


@pytest.mark.asyncio
async def test_multi_payload_generation_checkpoint_pins_every_result(
    tmp_path: Path,
) -> None:
    payloads = [b"checkpoint-primary", b"checkpoint-extra-2", b"checkpoint-extra-3"]
    state = SimpleNamespace(
        task_id="generation-multi-checkpoint",
        user_id="user-1",
        generation=SimpleNamespace(execution_epoch=4),
    )
    checkpoint_payloads = []
    paths: list[Path] = []
    for index, payload in enumerate(payloads, start=1):
        storage_key = (
            "u/user-1/g/generation-multi-checkpoint/executions/4/attempts/1/"
            f"takeover-result-{index}.bin"
        )
        paths.append(_write_storage_file(tmp_path, storage_key, payload))
        checkpoint_payloads.append(
            GenerationTakeoverPayload(
                storage_key=storage_key,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                revised_prompt=f"restored-{index}",
            ).as_result(
                index=index,
                bonus_generation_id=(
                    batch_extra_generation_id(state, index=index, attempt=1)
                    if index > 1
                    else None
                ),
            )
        )
    checkpoint = GenerationTakeoverCheckpoint(
        schema_version=2,
        execution_epoch=4,
        attempt=1,
        expected_count=3,
        collection_complete=True,
        results=tuple(checkpoint_payloads),
        provider="provider-1",
        route="image2",
        source="image2_direct",
        endpoint="images/generations",
    )

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add(_user())
            session.add(
                Generation(
                    id="generation-multi-checkpoint",
                    message_id="message-multi-checkpoint",
                    user_id="user-1",
                    action="generate",
                    model="gpt-image-test",
                    prompt="render",
                    size_requested="1024x1024",
                    aspect_ratio="1:1",
                    input_image_ids=[],
                    upstream_request={
                        GENERATION_TAKEOVER_CHECKPOINT_KEY: checkpoint.to_mapping()
                    },
                    status="running",
                    progress_stage="finalizing",
                    attempt=1,
                    execution_epoch=4,
                    idempotency_key="idempotency-multi-checkpoint",
                )
            )
            await session.commit()

            swept = await _sweep(session, tmp_path)

            assert swept["deleted"] == 0
            assert swept["orphans"] == []
            assert all(path.exists() for path in paths)


@pytest.mark.asyncio
async def test_generation_recovery_reuse_survives_sweep_until_image_commit(
    tmp_path: Path,
) -> None:
    payload = b"reused-generation-output"
    storage_key = generation_artifact_keys(
        user_id="user-1",
        task_id="generation-recovery",
        execution_epoch=7,
        attempt=2,
        orig_ext="png",
    )[0]
    stale_key = generation_artifact_keys(
        user_id="user-1",
        task_id="generation-recovery",
        execution_epoch=6,
        attempt=1,
        orig_ext="png",
    )[0]
    path = _write_storage_file(tmp_path, storage_key, payload)
    stale_path = _write_storage_file(tmp_path, stale_key, b"stale")
    source_path = tmp_path / "recovery-source.png"
    source_path.write_bytes(payload)
    artifacts = FileSystemArtifactStore(tmp_path)
    reused = asyncio.Event()
    allow_commit = asyncio.Event()

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add(_user())
            session.add(
                Generation(
                    id="generation-recovery",
                    message_id="message-recovery",
                    user_id="user-1",
                    action="generate",
                    model="gpt-image-test",
                    prompt="render",
                    size_requested="1024x1024",
                    aspect_ratio="1:1",
                    input_image_ids=[],
                    status="running",
                    progress_stage="finalizing",
                    attempt=2,
                    execution_epoch=7,
                    idempotency_key="idempotency-recovery",
                )
            )
            await session.commit()

        async def recover_and_commit() -> None:
            published = await artifacts.publish_path(
                source_path,
                ArtifactKey(storage_key),
                expected=ArtifactIdentity(
                    sha256=hashlib.sha256(payload).hexdigest(),
                    size_bytes=len(payload),
                ),
            )
            assert published.created is False
            reused.set()
            await allow_commit.wait()
            async with factory() as session:
                generation = await session.get(Generation, "generation-recovery")
                assert generation is not None
                session.add(
                    Image(
                        id="image-recovered",
                        user_id="user-1",
                        owner_generation_id=generation.id,
                        source="generated",
                        storage_key=storage_key,
                        mime="image/png",
                        width=64,
                        height=64,
                        size_bytes=len(payload),
                        sha256=hashlib.sha256(payload).hexdigest(),
                        metadata_jsonb={
                            "artifact_attempt_epoch": 2,
                            "artifact_execution_epoch": 7,
                        },
                        artifact_status="ready",
                    )
                )
                generation.status = "succeeded"
                await session.commit()

        recovery = asyncio.create_task(recover_and_commit())
        await asyncio.wait_for(reused.wait(), timeout=2)

        async with factory() as session:
            swept = await _sweep(session, tmp_path)
        assert swept["deleted"] == 1
        assert swept["orphans"] == [stale_key]
        assert path.read_bytes() == payload
        assert not stale_path.exists()

        allow_commit.set()
        await asyncio.wait_for(recovery, timeout=2)

        async with factory() as session:
            final = await session.get(Image, "image-recovered")
            generation = await session.get(Generation, "generation-recovery")
        assert final is not None
        assert final.storage_key == storage_key
        assert generation is not None
        assert generation.status == "succeeded"
        assert path.read_bytes() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "user_deleted_at", "expected_deleted"),
    [
        pytest.param("queued", None, 0, id="live-queued"),
        pytest.param("streaming", None, 0, id="live-streaming"),
        pytest.param("succeeded", None, 1, id="terminal-succeeded"),
        pytest.param("failed", None, 1, id="terminal-failed"),
        pytest.param("canceled", None, 1, id="terminal-canceled"),
        pytest.param("streaming", _NOW, 1, id="deleted-user-streaming"),
    ],
)
async def test_pending_completion_image_only_pins_recoverable_live_completion(
    tmp_path: Path,
    status: str,
    user_deleted_at: datetime | None,
    expected_deleted: int,
) -> None:
    storage_key = (
        "u/user-1/completion-tools/completion-pending/"
        "executions/9/attempts/3/image-pending/orig.png"
    )
    path = _write_storage_file(tmp_path, storage_key)

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add(_user(deleted_at=user_deleted_at))
            session.add(
                _completion(
                    completion_id="completion-pending",
                    status=status,
                    attempt=3,
                    execution_epoch=9,
                )
            )
            await session.commit()

            swept = await _sweep(session, tmp_path)

            assert swept["deleted"] == expected_deleted
            assert swept["orphans"] == ([storage_key] if expected_deleted else [])
            assert path.exists() is (expected_deleted == 0)


@pytest.mark.asyncio
async def test_pending_completion_only_pins_current_execution_attempt(
    tmp_path: Path,
) -> None:
    current_key = (
        "u/user-1/completion-tools/completion-current/"
        "executions/9/attempts/3/image-current/orig.png"
    )
    stale_key = (
        "u/user-1/completion-tools/completion-current/"
        "executions/8/attempts/2/image-stale/orig.png"
    )
    current_path = _write_storage_file(tmp_path, current_key)
    stale_path = _write_storage_file(tmp_path, stale_key)

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add(_user())
            session.add(
                _completion(
                    completion_id="completion-current",
                    status="streaming",
                    attempt=3,
                    execution_epoch=9,
                )
            )
            await session.commit()

            swept = await _sweep(session, tmp_path)

            assert swept["deleted"] == 1
            assert swept["orphans"] == [stale_key]
            assert current_path.is_file()
            assert not stale_path.exists()


@pytest.mark.asyncio
async def test_variant_claim_serializes_after_final_orphan_check_and_republishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_key = "u/user-1/uploads/source-image.png"
    variant_key = deterministic_variant_key(
        image_id="source-image",
        source_key=source_key,
        kind=DISPLAY_VARIANT,
    )
    stale_key = "u/user-1/uploads/000-stale.webp"
    source_payload = b"source"
    variant_payload = b"reused-variant"
    _write_storage_file(tmp_path, source_key, source_payload)
    variant_path = _write_storage_file(tmp_path, variant_key, variant_payload)
    stale_path = _write_storage_file(tmp_path, stale_key, b"stale")
    final_query_complete = asyncio.Event()
    unlink_entered = threading.Event()
    allow_unlink = threading.Event()
    claim_fence_attempted = asyncio.Event()

    real_known_storage_keys = storage_maintenance._known_storage_keys

    async def observed_known_storage_keys(
        db: AsyncSession,
        candidates: set[str],
    ) -> set[str]:
        known = await real_known_storage_keys(db, candidates)
        if candidates == {variant_key}:
            final_query_complete.set()
        return known

    real_unlink_if_unchanged = storage_maintenance._unlink_if_unchanged

    def blocked_variant_unlink(candidate: Any) -> bool:
        if candidate.key == variant_key:
            unlink_entered.set()
            if not allow_unlink.wait(timeout=5):
                raise RuntimeError("test did not release orphan unlink")
        return real_unlink_if_unchanged(candidate)

    monkeypatch.setattr(
        storage_maintenance,
        "_known_storage_keys",
        observed_known_storage_keys,
    )
    monkeypatch.setattr(
        storage_maintenance,
        "_unlink_if_unchanged",
        blocked_variant_unlink,
    )

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add(_user())
            image = _image(
                image_id="source-image",
                storage_key=source_key,
            )
            image.size_bytes = len(source_payload)
            image.sha256 = hashlib.sha256(source_payload).hexdigest()
            session.add(image)
            await session.commit()

        repository = SQLAlchemyVariantRepository(factory)
        artifacts = _ObservedVariantArtifactStore(
            tmp_path,
            fence_attempted=claim_fence_attempted,
        )
        service = CreateVariantService(
            artifacts=artifacts,
            capacity=_UnlimitedVariantCapacity(),  # type: ignore[arg-type]
            storage_capacity=_UnlimitedVariantCapacity(),  # type: ignore[arg-type]
            repository=repository,
            processing_executor=_StaticVariantExecutor(variant_payload),
            capacity_lease_ttl_seconds=30,
            storage_lease_ttl_seconds=30,
        )

        async def run_sweep() -> dict[str, Any]:
            async with factory() as session:
                return await _sweep(session, tmp_path)

        sweep = asyncio.create_task(run_sweep())
        await asyncio.wait_for(final_query_complete.wait(), timeout=2)
        assert await asyncio.to_thread(unlink_entered.wait, 2)

        create_variant = asyncio.create_task(
            service.ensure_display_variant("source-image"),
        )
        await asyncio.wait_for(claim_fence_attempted.wait(), timeout=2)
        assert not create_variant.done()
        async with factory() as session:
            assert (
                await session.get(
                    ImageVariantClaim,
                    {
                        "image_id": "source-image",
                        "kind": DISPLAY_VARIANT,
                    },
                )
                is None
            )

        allow_unlink.set()
        swept = await asyncio.wait_for(sweep, timeout=2)
        result = await asyncio.wait_for(create_variant, timeout=2)

        async with factory() as session:
            final_row = (
                await session.execute(
                    select(ImageVariant).where(
                        ImageVariant.image_id == "source-image",
                        ImageVariant.kind == DISPLAY_VARIANT,
                    )
                )
            ).scalar_one_or_none()
            claim_row = await session.get(
                ImageVariantClaim,
                {
                    "image_id": "source-image",
                    "kind": DISPLAY_VARIANT,
                },
            )
        assert result == VariantResult(
            image_id="source-image",
            kind=DISPLAY_VARIANT,
            storage_key=variant_key,
            width=32,
            height=32,
            mime="image/webp",
        )
        assert swept["deleted"] == 2
        assert stale_key in swept["orphans"]
        assert not stale_path.exists()
        assert artifacts.publish_created == [True]
        assert final_row is not None
        assert final_row.storage_key == variant_key
        assert claim_row is None
        assert variant_path.read_bytes() == variant_payload


@pytest.mark.asyncio
async def test_variant_reuse_and_finalize_fence_orphan_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_key = "u/user-1/uploads/source-reuse.png"
    variant_key = deterministic_variant_key(
        image_id="source-reuse",
        source_key=source_key,
        kind=DISPLAY_VARIANT,
    )
    stale_key = "u/user-1/uploads/000-reclaim.webp"
    source_payload = b"source-reuse"
    variant_payload = b"reused-variant"
    _write_storage_file(tmp_path, source_key, source_payload)
    variant_path = _write_storage_file(tmp_path, variant_key, variant_payload)
    stale_path = _write_storage_file(tmp_path, stale_key, b"stale")
    initial_liveness_complete = asyncio.Event()
    allow_deletion = asyncio.Event()
    finalize_entered = asyncio.Event()
    allow_finalize = asyncio.Event()
    deletion_lock_attempted = asyncio.Event()

    real_load_known_storage_keys = storage_maintenance._load_known_storage_keys

    async def pause_after_initial_liveness(
        *args: Any,
        **kwargs: Any,
    ) -> tuple[set[str], bool]:
        result = await real_load_known_storage_keys(*args, **kwargs)
        initial_liveness_complete.set()
        await allow_deletion.wait()
        return result

    real_artifact_lifecycle_lock = storage_maintenance.artifact_lifecycle_lock

    def observed_deletion_lock(
        destination: Path,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        fence = real_artifact_lifecycle_lock(
            destination,
            timeout_seconds=timeout_seconds,
        )

        @asynccontextmanager
        async def observed_fence() -> AsyncIterator[None]:
            deletion_lock_attempted.set()
            async with fence:
                yield

        return observed_fence()

    monkeypatch.setattr(
        storage_maintenance,
        "_load_known_storage_keys",
        pause_after_initial_liveness,
    )
    monkeypatch.setattr(
        storage_maintenance,
        "artifact_lifecycle_lock",
        observed_deletion_lock,
    )

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add(_user())
            image = _image(
                image_id="source-reuse",
                storage_key=source_key,
            )
            image.size_bytes = len(source_payload)
            image.sha256 = hashlib.sha256(source_payload).hexdigest()
            session.add(image)
            await session.commit()

        repository = SQLAlchemyVariantRepository(factory)
        real_finalize = repository.finalize

        async def blocked_finalize(*args: Any, **kwargs: Any) -> Any:
            finalize_entered.set()
            await allow_finalize.wait()
            return await real_finalize(*args, **kwargs)

        monkeypatch.setattr(repository, "finalize", blocked_finalize)
        artifacts = _ObservedVariantArtifactStore(tmp_path)
        service = CreateVariantService(
            artifacts=artifacts,
            capacity=_UnlimitedVariantCapacity(),  # type: ignore[arg-type]
            storage_capacity=_UnlimitedVariantCapacity(),  # type: ignore[arg-type]
            repository=repository,
            processing_executor=_StaticVariantExecutor(variant_payload),
            capacity_lease_ttl_seconds=30,
            storage_lease_ttl_seconds=30,
        )

        async def run_sweep() -> dict[str, Any]:
            async with factory() as session:
                return await _sweep(session, tmp_path)

        sweep = asyncio.create_task(run_sweep())
        await asyncio.wait_for(initial_liveness_complete.wait(), timeout=2)

        create_variant = asyncio.create_task(
            service.ensure_display_variant("source-reuse"),
        )
        await asyncio.wait_for(finalize_entered.wait(), timeout=2)
        assert artifacts.publish_created == [False]
        assert variant_path.read_bytes() == variant_payload

        allow_deletion.set()
        await asyncio.wait_for(deletion_lock_attempted.wait(), timeout=2)
        assert not sweep.done()

        allow_finalize.set()
        result = await asyncio.wait_for(create_variant, timeout=2)
        swept = await asyncio.wait_for(sweep, timeout=2)

        async with factory() as session:
            final_row = (
                await session.execute(
                    select(ImageVariant).where(
                        ImageVariant.image_id == "source-reuse",
                        ImageVariant.kind == DISPLAY_VARIANT,
                    )
                )
            ).scalar_one_or_none()

        assert result.storage_key == variant_key
        assert swept["deleted"] == 1
        assert swept["orphans"] == [stale_key]
        assert not stale_path.exists()
        assert final_row is not None
        assert final_row.storage_key == variant_key
        assert variant_path.read_bytes() == variant_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_delta", "image_deleted_at", "user_deleted_at"),
    [
        pytest.param(timedelta(minutes=-1), None, None, id="expired-claim"),
        pytest.param(timedelta(minutes=5), _NOW, None, id="deleted-image"),
        pytest.param(timedelta(minutes=5), None, _NOW, id="deleted-user"),
    ],
)
async def test_inactive_variant_claim_does_not_pin_orphan(
    tmp_path: Path,
    lease_delta: timedelta,
    image_deleted_at: datetime | None,
    user_deleted_at: datetime | None,
) -> None:
    source_key = "u/user-1/uploads/variant-orphan.png"
    variant_key = deterministic_variant_key(
        image_id="variant-orphan",
        source_key=source_key,
        kind=DISPLAY_VARIANT,
    )
    variant_path = _write_storage_file(tmp_path, variant_key)
    now = datetime.now(timezone.utc)

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add(_user(deleted_at=user_deleted_at))
            session.add(
                _image(
                    image_id="variant-orphan",
                    storage_key=source_key,
                    deleted_at=image_deleted_at,
                )
            )
            session.add(
                ImageVariantClaim(
                    image_id="variant-orphan",
                    kind=DISPLAY_VARIANT,
                    token="inactive-claim",
                    source_key=source_key,
                    source_sha256="n" * 64,
                    lease_until=now + lease_delta,
                )
            )
            await session.commit()

            swept = await _sweep(session, tmp_path)

            assert swept["deleted"] == 1
            assert swept["orphans"] == [variant_key]
            assert not variant_path.exists()


@pytest.mark.asyncio
async def test_storyboard_unknown_commit_candidate_stays_live_until_explicit_state(
    tmp_path: Path,
) -> None:
    video_key = "u/user-1/storyboards/run-unknown/assembly/version-1/output.mp4"
    poster_key = "u/user-1/storyboards/run-unknown/assembly/version-1/poster.jpg"
    paths = [
        _write_storage_file(tmp_path, video_key),
        _write_storage_file(tmp_path, poster_key),
    ]
    attempt_token = "attempt-unknown"
    fingerprint = "fingerprint-unknown"
    output_json = {
        "assembly_attempt_token": attempt_token,
        "assembly_fingerprint": fingerprint,
        "assembly_commit_state": "unknown",
        "assembly_commit_candidate": {
            "id": "video-unknown",
            "user_id": "user-1",
            "storage_key": video_key,
            "poster_storage_key": poster_key,
            "metadata_jsonb": {
                "workflow_type": "storyboard",
                "workflow_run_id": "run-unknown",
                "assembly_attempt_token": attempt_token,
                "assembly_fingerprint": fingerprint,
            },
        },
    }
    state = {"compositing": True}

    class _Rows:
        def __init__(
            self,
            *,
            rows: list[Any] | None = None,
            values: list[Any] | None = None,
        ) -> None:
            self.rows = rows or []
            self.values = values or []

        def all(self) -> list[Any]:
            return self.rows

        def scalars(self) -> _Rows:
            return self

    class _StoryboardDb:
        async def execute(self, statement: Any) -> _Rows:
            if "FROM workflow_runs" in str(statement) and state["compositing"]:
                return _Rows(
                    rows=[("run-unknown", "user-1", output_json)],
                )
            return _Rows()

    retained = await _sweep(_StoryboardDb(), tmp_path)  # type: ignore[arg-type]
    assert retained["deleted"] == 0
    assert all(path.is_file() for path in paths)

    state["compositing"] = False
    reclaimed = await _sweep(_StoryboardDb(), tmp_path)  # type: ignore[arg-type]
    assert reclaimed["deleted"] == 2
    assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_compositing_storyboard_pins_files_without_db_candidate(
    tmp_path: Path,
) -> None:
    base = "u/user-1/storyboards/run-unmarked/assembly/version-1"
    paths = [
        _write_storage_file(tmp_path, f"{base}/output.mp4"),
        _write_storage_file(tmp_path, f"{base}/poster.jpg"),
        _write_storage_file(tmp_path, f"{base}/commit-recovery.json"),
    ]
    output_json = {
        "assembly_attempt_token": "attempt-unmarked",
        "assembly_fingerprint": "fingerprint-unmarked",
        "assembly_commit_state": None,
        "assembly_commit_candidate": None,
    }
    state = {"compositing": True}

    class _Rows:
        def __init__(self, rows: list[Any] | None = None) -> None:
            self.rows = rows or []

        def all(self) -> list[Any]:
            return self.rows

        def scalars(self) -> _Rows:
            return self

    class _StoryboardDb:
        async def execute(self, statement: Any) -> _Rows:
            if "FROM workflow_runs" in str(statement) and state["compositing"]:
                return _Rows(
                    [("run-unmarked", "user-1", output_json)],
                )
            return _Rows()

    retained = await _sweep(_StoryboardDb(), tmp_path)  # type: ignore[arg-type]
    assert retained["deleted"] == 0
    assert all(path.is_file() for path in paths)

    state["compositing"] = False
    reclaimed = await _sweep(_StoryboardDb(), tmp_path)  # type: ignore[arg-type]
    assert reclaimed["deleted"] == 3
    assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_image_delete_reclaims_original_normalized_ref_and_all_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_key = "u/user-1/uploads/image-delete.png"
    normalized_key = "u/user-1/uploads/image-delete.ref.webp"
    variant_keys = [
        "u/user-1/uploads/image-delete.thumb256.webp",
        "u/user-1/uploads/image-delete.preview1024.webp",
        "u/user-1/uploads/image-delete.display2048.webp",
    ]
    paths = [
        _write_storage_file(tmp_path, key)
        for key in [original_key, normalized_key, *variant_keys]
    ]

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add(
                _image(
                    image_id="image-delete",
                    storage_key=original_key,
                    metadata={
                        "normalized_ref": {"storage_key": normalized_key},
                    },
                )
            )
            session.add_all(
                [
                    _variant(
                        variant_id=f"variant-{index}",
                        image_id="image-delete",
                        kind=kind,
                        storage_key=key,
                    )
                    for index, (kind, key) in enumerate(
                        zip(
                            ("thumb256", "preview1024", "display2048"),
                            variant_keys,
                            strict=True,
                        )
                    )
                ]
            )
            await session.commit()

            async def allow_delete(*_args: Any, **_kwargs: Any) -> None:
                return None

            async def write_audit_event(*_args: Any, **_kwargs: Any) -> None:
                return None

            monkeypatch.setattr(
                http_routes.asset_ref_service,
                "ensure_asset_not_canvas_referenced",
                allow_delete,
            )
            monkeypatch.setattr(http_routes, "request_ip_hash", lambda _request: "ip")

            result = await http_routes.delete_image_impl(
                "image-delete",
                SimpleNamespace(),
                SimpleNamespace(
                    id="user-1",
                    email="user-1@example.com",
                ),
                session,
                write_audit_event=write_audit_event,
            )

            assert result == {"ok": True}
            assert all(path.is_file() for path in paths)

            swept = await _sweep(session, tmp_path)
            assert swept["deleted"] == 5
            assert swept["failed"] == []
            assert all(not path.exists() for path in paths)

            persisted = await session.get(Image, "image-delete")
            assert persisted is not None
            assert persisted.deleted_at is not None
            variants = (
                (
                    await session.execute(
                        select(ImageVariant).where(
                            ImageVariant.image_id == "image-delete"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(variants) == 3

            repeated = await _sweep(session, tmp_path)
            assert repeated["deleted"] == 0
            assert repeated["failed"] == []


@pytest.mark.asyncio
async def test_deleted_media_sweep_preserves_every_live_or_shared_key(
    tmp_path: Path,
) -> None:
    deleted_original = "u/user-1/uploads/deleted-original.png"
    deleted_normalized = "u/user-1/uploads/deleted-normalized.ref.webp"
    shared_variant = "u/user-1/uploads/deleted-shared.display2048.webp"
    deleted_only_variant = "u/user-1/uploads/deleted-only.preview1024.webp"
    live_variant = "u/user-1/uploads/live.thumb256.webp"
    paths = {
        key: _write_storage_file(tmp_path, key)
        for key in (
            deleted_original,
            deleted_normalized,
            shared_variant,
            deleted_only_variant,
            live_variant,
        )
    }

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add_all(
                [
                    _image(
                        image_id="image-deleted",
                        storage_key=deleted_original,
                        deleted_at=_NOW,
                        metadata={
                            "normalized_ref": {
                                "storage_key": deleted_normalized,
                            }
                        },
                    ),
                    _image(
                        image_id="image-live",
                        storage_key=shared_variant,
                        metadata={
                            "normalized_ref": {
                                "storage_key": deleted_original,
                            }
                        },
                        manifest={
                            "artifacts": {
                                "original": {"storage_key": shared_variant},
                                "normalized_ref": {
                                    "storage_key": deleted_normalized,
                                },
                            }
                        },
                    ),
                    _variant(
                        variant_id="variant-deleted-shared",
                        image_id="image-deleted",
                        kind="display2048",
                        storage_key=shared_variant,
                    ),
                    _variant(
                        variant_id="variant-deleted-only",
                        image_id="image-deleted",
                        kind="preview1024",
                        storage_key=deleted_only_variant,
                    ),
                    _variant(
                        variant_id="variant-live",
                        image_id="image-live",
                        kind="thumb256",
                        storage_key=live_variant,
                    ),
                ]
            )
            await session.commit()

            swept = await _sweep(session, tmp_path)

            assert swept["deleted"] == 1
            assert swept["orphans"] == [deleted_only_variant]
            assert not paths[deleted_only_variant].exists()
            assert all(
                paths[key].is_file()
                for key in (
                    deleted_original,
                    deleted_normalized,
                    shared_variant,
                    live_variant,
                )
            )


@pytest.mark.asyncio
async def test_deleted_media_unlink_failure_is_retryable_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_key = "u/user-1/uploads/retry-delete.png"
    path = _write_storage_file(tmp_path, storage_key)

    async with _database([Image.__table__, ImageVariant.__table__]) as factory:
        async with factory() as session:
            session.add(
                _image(
                    image_id="image-retry",
                    storage_key=storage_key,
                    deleted_at=_NOW,
                )
            )
            await session.commit()

            real_unlink = storage_maintenance._unlink_if_unchanged  # noqa: SLF001
            attempts = 0

            def fail_once(candidate: Any) -> bool:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("simulated unlink failure")
                return real_unlink(candidate)

            monkeypatch.setattr(
                storage_maintenance,
                "_unlink_if_unchanged",
                fail_once,
            )
            failed = await _sweep(session, tmp_path)

            assert failed["deleted"] == 0
            assert failed["failed"] == [storage_key]
            assert failed["budget_exhausted"] is True
            assert failed["next_cursor"] is None
            assert path.is_file()
            persisted = await session.get(Image, "image-retry")
            assert persisted is not None
            assert persisted.deleted_at is not None

            retried = await _sweep(session, tmp_path)
            assert retried["deleted"] == 1
            assert retried["failed"] == []
            assert not path.exists()

            repeated = await _sweep(session, tmp_path)
            assert repeated["deleted"] == 0
            assert repeated["failed"] == []


@pytest.mark.asyncio
async def test_workflow_delete_commits_before_maintenance_reclaims_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_key = "u/user-1/uploads/workflow-image.png"
    normalized_key = "u/user-1/uploads/workflow-image.ref.webp"
    variant_key = "u/user-1/uploads/workflow-image.display2048.webp"
    paths = [
        _write_storage_file(tmp_path, key)
        for key in (original_key, normalized_key, variant_key)
    ]

    tables = [
        Image.__table__,
        ImageVariant.__table__,
        ModelLibraryItem.__table__,
    ]
    async with _database(tables) as factory:
        async with factory() as session:
            session.add_all(
                [
                    _image(
                        image_id="workflow-image",
                        storage_key=original_key,
                        metadata={
                            "normalized_ref": {"storage_key": normalized_key},
                        },
                    ),
                    _variant(
                        variant_id="workflow-variant",
                        image_id="workflow-image",
                        kind="display2048",
                        storage_key=variant_key,
                    ),
                ]
            )
            await session.commit()

            async def workflow_rows(
                _db: AsyncSession,
                _run: Any,
            ) -> tuple[list[Any], list[Any]]:
                return (
                    [
                        SimpleNamespace(
                            image_ids=["workflow-image"],
                            task_ids=[],
                        )
                    ],
                    [],
                )

            async def generation_rows(
                _db: AsyncSession,
                **_kwargs: Any,
            ) -> list[Any]:
                return []

            monkeypatch.setattr(
                workflow_runtime,
                "_workflow_steps_and_candidates",
                workflow_rows,
            )
            monkeypatch.setattr(
                workflow_runtime,
                "_workflow_generation_rows_from_task_ids",
                generation_rows,
            )

            cleanup = await workflow_runtime._soft_delete_workflow_generated_images(  # noqa: SLF001
                session,
                run=SimpleNamespace(
                    id="run-1",
                    user_id="user-1",
                    deleted_at=None,
                ),
                deleted_at=_NOW,
                cancel_message="workflow deleted",
            )

            assert cleanup["images_deleted"] == 1
            assert all(path.is_file() for path in paths)

            await session.commit()
            swept = await _sweep(session, tmp_path)

            assert swept["deleted"] == 3
            assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_byok_retention_commit_exposes_image_media_to_maintenance(
    tmp_path: Path,
) -> None:
    original_key = "u/user-1/uploads/byok-image.png"
    normalized_key = "u/user-1/uploads/byok-image.ref.webp"
    variant_key = "u/user-1/uploads/byok-image.preview1024.webp"
    paths = [
        _write_storage_file(tmp_path, key)
        for key in (original_key, normalized_key, variant_key)
    ]
    old = _NOW - timedelta(days=10)

    tables = [
        User.__table__,
        Conversation.__table__,
        Message.__table__,
        Image.__table__,
        ImageVariant.__table__,
    ]
    async with _database(tables) as factory:
        async with factory() as session:
            session.add_all(
                [
                    User(
                        id="user-1",
                        email="byok@example.com",
                        display_name="BYOK",
                        account_mode="byok",
                    ),
                    Conversation(
                        id="conversation-1",
                        user_id="user-1",
                        title="old",
                        last_activity_at=old,
                    ),
                    Message(
                        id="message-1",
                        conversation_id="conversation-1",
                        role="user",
                        content={},
                        created_at=old,
                    ),
                    _image(
                        image_id="byok-image",
                        storage_key=original_key,
                        metadata={
                            "normalized_ref": {"storage_key": normalized_key},
                        },
                        created_at=old,
                    ),
                    _variant(
                        variant_id="byok-variant",
                        image_id="byok-image",
                        kind="preview1024",
                        storage_key=variant_key,
                    ),
                ]
            )
            await session.commit()

            counts = await prune_expired_byok_user_data(
                session,
                now=_NOW,
                policy=ByokRetentionPolicy(
                    delete_enabled=True,
                    delete_days=7,
                ),
            )

            assert counts["images_deleted"] == 1
            assert all(path.is_file() for path in paths)

            await session.commit()
            swept = await _sweep(session, tmp_path)

            assert swept["deleted"] == 3
            assert all(not path.exists() for path in paths)
