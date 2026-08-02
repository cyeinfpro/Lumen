from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql.schema import Table

from apps.worker.app.tasks.completion_parts import (
    image_storage_runtime as worker_completion_storage,
)
from apps.worker.app.tasks.generation_parts.success import generation_artifact_keys

from app.images.application import (
    http_routes,
    orphan_storage_deletion,
    storage_maintenance,
)
from app.images.application.storage_maintenance import sweep_orphan_image_files
from app.workflows.adapters import workflow_runtime
from lumen_core.byok_retention import (
    ByokRetentionPolicy,
    prune_expired_byok_user_data,
)
from lumen_core.models import (
    Base,
    Conversation,
    Image,
    ImageVariant,
    Message,
    ModelLibraryItem,
    User,
    Video,
    VideoGeneration,
)


_NOW = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)


class _CapturedWorkerStorageKeys(RuntimeError):
    pass


@asynccontextmanager
async def _database(
    tables: Sequence[Table],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    selected_tables = list(tables)
    if User.__table__ not in selected_tables:
        selected_tables.append(User.__table__)
    if VideoGeneration.__table__ not in selected_tables:
        selected_tables.append(VideoGeneration.__table__)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=selected_tables,
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


def _user(*, deleted_at: datetime | None = None) -> User:
    return User(
        id="user-1",
        email="user-1@example.com",
        display_name="User 1",
        deleted_at=deleted_at,
    )


def _write_storage_file(root: Path, key: str) -> Path:
    path = root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"image")
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
async def test_orphan_delete_waits_for_same_user_restore_before_final_reference_fence() -> (
    None
):
    storage_key = "u/user-race/vref/video-race/original.mp4"
    user_lock = asyncio.Lock()
    await user_lock.acquire()
    live_keys: set[str] = set()
    unlinked: list[str] = []

    class Db:
        owns_lock = False

        async def execute(self, statement: Any) -> object:
            assert "FROM users" in str(statement)
            await user_lock.acquire()
            self.owns_lock = True
            return object()

        def release(self) -> None:
            if self.owns_lock:
                self.owns_lock = False
                user_lock.release()

    async def known_storage_keys(
        _db: Any,
        candidates: set[str],
    ) -> set[str]:
        return candidates & live_keys

    db = Db()
    deletion = asyncio.create_task(
        orphan_storage_deletion.delete_orphan_candidates(
            db,  # type: ignore[arg-type]
            [SimpleNamespace(key=storage_key)],
            max_seconds=10,
            started=0,
            monotonic=lambda: 0,
            assert_owned=None,
            known_storage_keys=known_storage_keys,
            unlink_if_unchanged=lambda candidate: (
                unlinked.append(candidate.key) or True
            ),
        )
    )
    await asyncio.sleep(0)
    assert not deletion.done()

    live_keys.add(storage_key)
    user_lock.release()
    try:
        result = await deletion
    finally:
        db.release()

    assert result.deleted == 0
    assert unlinked == []


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
