from __future__ import annotations

import asyncio
import hashlib
import io
import threading
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lumen_core.models import Base, User, Video
from lumen_core.volcano_asset_media import _video_reference_declared_bytes
from app.services import video_preparation as preparation
from app.services import video_preparation_posters as posters
from app.services.video_preparation_retry import retry_video_preparation
from app.services.video_storage_accounting import (
    VideoArtifactInspection,
    video_reference_declared_quota_contribution,
    video_reference_quota_contribution,
)
from app.video_reference_videos import (
    VideoReferenceVideoError,
    _reference_storage_usage,
)

INSPECTED = {"width": 320, "height": 180, "duration_ms": 3000, "fps": 24.0}


def jpeg(color="red", size=(32, 18)):
    output = io.BytesIO()
    with Image.new("RGB", size, color) as picture:
        picture.save(output, "JPEG")
    return output.getvalue()


@asynccontextmanager
async def media_session(tmp_path):
    key = "u/user-1/vref/video-1/original.mp4"
    path = tmp_path / key
    path.parent.mkdir(parents=True, exist_ok=True)
    source = b"synthetic original for mocked probe"
    path.write_bytes(source)
    digest = hashlib.sha256(source).hexdigest()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[User.__table__, Video.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        row = Video(
            id="video-1",
            user_id="user-1",
            storage_key=key,
            sha256=digest,
            size_bytes=len(source),
            etag=digest,
            metadata_jsonb={
                "source": "uploaded_reference",
                "unrelated": "kept",
                preparation.PREPARATION_KEY: preparation.pending_preparation(digest),
            },
        )
        db.add(row)
        await db.commit()
        yield db, row, factory
    await engine.dispose()


class Guard:
    def __init__(self, fail_at=0):
        self.calls = 0
        self.fail_at = fail_at

    async def assert_owned(self):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("capacity lease lost")


@pytest.mark.asyncio
async def test_slot_is_accounted_before_disk_write_and_ready_publishes_exact_poster(
    tmp_path,
):
    async with media_session(tmp_path) as (db, row, _factory):
        claim = await preparation.claim_preparation(db, row.id, now=100)
        slot = await preparation.reserve_poster_slot(db, claim, now=110)
        await preparation.stage_poster_metadata(db, claim, jpeg(), now=111)
        slot = row.metadata_jsonb[posters.POSTER_KEY]
        assert slot["state"] == "reserved"
        assert row.poster_storage_key is None
        assert (
            await _reference_storage_usage(db, user_id=row.user_id)
            == row.size_bytes + posters.POSTER_MAX_BYTES
        )
        assert (
            video_reference_declared_quota_contribution(row)[1]
            == row.size_bytes + posters.POSTER_MAX_BYTES
        )
        assert (
            _video_reference_declared_bytes(row)
            == row.size_bytes + posters.POSTER_MAX_BYTES
        )
        inspection = VideoArtifactInspection(1, row.size_bytes, True, row.size_bytes)
        assert (
            video_reference_quota_contribution(row, inspection)[1]
            == row.size_bytes + posters.POSTER_MAX_BYTES
        )
        data = jpeg()
        assert await preparation.finish_preparation(
            db,
            claim,
            now=115,
            inspected=INSPECTED,
            poster_data=data,
            storage_root=str(tmp_path),
            capacity_guard=Guard(),
        )
        assert (tmp_path / row.poster_storage_key).read_bytes() == data
        assert row.metadata_jsonb[posters.POSTER_KEY]["size_bytes"] == len(data)
        assert await _reference_storage_usage(
            db, user_id=row.user_id
        ) == row.size_bytes + len(data)
        assert row.metadata_jsonb["unrelated"] == "kept"
        assert row.sha256 == claim.source_sha256


@pytest.mark.asyncio
async def test_crash_installed_file_remains_accounted_and_next_claim_adopts_same_revision(
    tmp_path,
):
    async with media_session(tmp_path) as (db, row, _factory):
        claim = await preparation.claim_preparation(db, row.id, now=100)
        slot = await preparation.reserve_poster_slot(db, claim, now=110)
        await preparation.stage_poster_metadata(db, claim, jpeg(), now=111)
        slot = row.metadata_jsonb[posters.POSTER_KEY]
        installed = posters.install_preparation_poster(
            claim, slot, jpeg(), storage_root=str(tmp_path)
        )
        assert (
            row.poster_storage_key is None
        )  # Simulated process death before DB publication.
        new_claim = await preparation.claim_preparation(db, row.id, now=250)
        assert new_claim.token != claim.token
        assert new_claim.artifact_revision == claim.artifact_revision
        assert not await preparation.finish_preparation(
            db,
            claim,
            now=251,
            inspected=INSPECTED,
            poster_data=jpeg("blue"),
            storage_root=str(tmp_path),
            capacity_guard=Guard(),
        )
        recovered = await preparation.reserve_poster_slot(db, new_claim, now=251)
        assert recovered["storage_key"] == slot["storage_key"]
        assert await preparation.finish_preparation(
            db,
            new_claim,
            now=252,
            inspected=INSPECTED,
            poster_data=jpeg(),
            storage_root=str(tmp_path),
            capacity_guard=Guard(),
        )
        assert row.metadata_jsonb[posters.POSTER_KEY]["sha256"] == installed["sha256"]
        assert (tmp_path / row.poster_storage_key).read_bytes() == jpeg()


@pytest.mark.asyncio
async def test_capacity_lost_after_install_cannot_publish_ready(tmp_path):
    async with media_session(tmp_path) as (db, row, _factory):
        claim = await preparation.claim_preparation(db, row.id, now=100)
        slot = await preparation.reserve_poster_slot(db, claim, now=110)
        await preparation.stage_poster_metadata(db, claim, jpeg(), now=111)
        slot = row.metadata_jsonb[posters.POSTER_KEY]
        with pytest.raises(RuntimeError, match="lease lost"):
            await preparation.finish_preparation(
                db,
                claim,
                now=115,
                inspected=INSPECTED,
                poster_data=jpeg(),
                storage_root=str(tmp_path),
                capacity_guard=Guard(fail_at=2),
            )
        await db.rollback()
        await db.refresh(row)
        assert row.poster_storage_key is None
        assert row.metadata_jsonb[preparation.PREPARATION_KEY]["state"] == "preparing"
        assert row.metadata_jsonb[posters.POSTER_KEY]["state"] == "reserved"
        assert (tmp_path / slot["storage_key"]).is_file()


@pytest.mark.asyncio
async def test_source_changed_before_publication_is_rejected_without_touching_original(
    tmp_path,
):
    async with media_session(tmp_path) as (db, row, _factory):
        claim = await preparation.claim_preparation(db, row.id, now=100)
        await preparation.reserve_poster_slot(db, claim, now=110)
        await preparation.stage_poster_metadata(db, claim, jpeg(), now=111)
        (tmp_path / row.storage_key).write_bytes(b"replacement")
        with pytest.raises(VideoReferenceVideoError, match="original changed"):
            await preparation.finish_preparation(
                db,
                claim,
                now=115,
                inspected=INSPECTED,
                poster_data=jpeg(),
                storage_root=str(tmp_path),
                capacity_guard=Guard(),
            )
        assert (tmp_path / row.storage_key).read_bytes() == b"replacement"
        assert row.poster_storage_key is None


@pytest.mark.asyncio
async def test_retry_is_owner_hash_revision_fenced_and_idempotent_after_failure(
    tmp_path,
):
    async with media_session(tmp_path) as (db, row, _factory):
        claim = await preparation.claim_preparation(db, row.id, now=100)
        await preparation.finish_preparation(
            db, claim, now=110, error_code="video_poster_failed"
        )
        expected = row.metadata_jsonb[preparation.PREPARATION_KEY]["revision"]
        args = dict(
            video_id=row.id,
            user_id=row.user_id,
            expected_source_sha256=row.sha256,
            expected_preparation_revision=expected,
            idempotency_key="one-retry-key-12345",
        )
        for override, status in [
            ({"user_id": "foreign"}, 404),
            ({"expected_source_sha256": "f" * 64}, 409),
            ({"expected_preparation_revision": 0}, 409),
        ]:
            with pytest.raises(VideoReferenceVideoError) as error:
                await retry_video_preparation(db, **{**args, **override})
            assert error.value.status_code == status
        await retry_video_preparation(db, **args)
        pending_revision = row.metadata_jsonb[preparation.PREPARATION_KEY]["revision"]
        assert pending_revision == expected + 1
        await retry_video_preparation(db, **args)
        assert (
            row.metadata_jsonb[preparation.PREPARATION_KEY]["revision"]
            == pending_revision
        )
        next_claim = await preparation.claim_preparation(db, row.id, now=120)
        await preparation.finish_preparation(
            db, next_claim, now=121, error_code="still_unavailable"
        )
        failed_revision = row.metadata_jsonb[preparation.PREPARATION_KEY]["revision"]
        await retry_video_preparation(db, **args)
        assert row.metadata_jsonb[preparation.PREPARATION_KEY]["state"] == "failed"
        assert (
            row.metadata_jsonb[preparation.PREPARATION_KEY]["revision"]
            == failed_revision
        )
        with pytest.raises(VideoReferenceVideoError):
            await retry_video_preparation(
                db, **{**args, "idempotency_key": "other-retry-key-123"}
            )
        await retry_video_preparation(
            db,
            **{
                **args,
                "expected_preparation_revision": failed_revision,
                "idempotency_key": "other-retry-key-123",
            },
        )
        assert row.metadata_jsonb[preparation.PREPARATION_KEY]["state"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "preparing", "ready"])
async def test_retry_does_not_restart_nonfailed_preparation(tmp_path, state):
    async with media_session(tmp_path) as (db, row, _factory):
        meta = dict(row.metadata_jsonb)
        meta[preparation.PREPARATION_KEY] = {
            **meta[preparation.PREPARATION_KEY],
            "state": state,
        }
        row.metadata_jsonb = meta
        await db.commit()
        await retry_video_preparation(
            db,
            video_id=row.id,
            user_id=row.user_id,
            expected_source_sha256=row.sha256,
            expected_preparation_revision=0,
            idempotency_key="noop-retry-key-123",
        )
        assert row.metadata_jsonb[preparation.PREPARATION_KEY]["state"] == state
        assert row.metadata_jsonb[preparation.PREPARATION_KEY]["revision"] == 0


def test_poster_validation_rejects_bad_bytes_and_oversized_dimensions():
    for data in (b"not a jpeg", jpeg(size=(posters.POSTER_MAX_SIDE + 1, 10))):
        with pytest.raises(VideoReferenceVideoError):
            posters.validate_poster(data)


def test_owner_paths_reject_traversal_cross_tenant_and_symlinks(tmp_path):
    directory = tmp_path / "u/owner/vref/video"
    directory.mkdir(parents=True)
    for key in (
        "/tmp/file",
        "u/foreign/vref/video/original.mp4",
        "u/owner/vref/video/../original.mp4",
    ):
        with pytest.raises(VideoReferenceVideoError):
            posters.owned_path(str(tmp_path), key, user_id="owner", video_id="video")
    (directory / "original.mp4").symlink_to(tmp_path / "foreign.mp4")
    with pytest.raises(VideoReferenceVideoError):
        posters.owned_path(
            str(tmp_path),
            "u/owner/vref/video/original.mp4",
            user_id="owner",
            video_id="video",
        )


@pytest.mark.asyncio
async def test_cancellation_holds_bounded_work_until_thread_stops():
    started, release = threading.Event(), threading.Event()

    def work():
        started.set()
        assert release.wait(2)

    task = asyncio.create_task(preparation.bounded_thread(work))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_worker_uses_background_capacity_and_does_not_call_provider(
    tmp_path, monkeypatch
):
    async with media_session(tmp_path) as (db, row, factory):
        events = []

        class Capacity:
            @asynccontextmanager
            async def hold(self, **_kwargs):
                events.append("hold")
                yield
                events.append("release")

            @asynccontextmanager
            async def reserve(self, required):
                assert required == 2 * posters.POSTER_MAX_BYTES
                events.append("reserve")
                yield Guard()
                events.append("unreserve")

        monkeypatch.setattr(
            preparation, "build_video_transcode_capacity_manager", Capacity
        )
        monkeypatch.setattr(
            preparation, "build_video_storage_capacity_manager", Capacity
        )
        monkeypatch.setattr(
            preparation, "inspect_video_reference_original", lambda **_kw: INSPECTED
        )
        monkeypatch.setattr(
            preparation, "render_preparation_poster", lambda *_args, **_kw: jpeg()
        )
        assert await preparation.prepare_video_metadata(
            factory, row.id, storage_root=str(tmp_path)
        )
        await db.refresh(row)
        assert row.metadata_jsonb[preparation.PREPARATION_KEY]["state"] == "ready"
        assert events == ["hold", "reserve", "unreserve", "release"]
        assert not await preparation.prepare_video_metadata(
            factory, row.id, storage_root=str(tmp_path)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", "foreign"),
        ("storage_key", "elsewhere"),
        ("size_bytes", 999),
        ("lease_until", 999),
    ],
)
async def test_full_claim_identity_is_required(tmp_path, field, value):
    async with media_session(tmp_path) as (db, row, _factory):
        claim = await preparation.claim_preparation(db, row.id, now=100)
        assert not await preparation.finish_preparation(
            db, replace(claim, **{field: value}), now=110, inspected=INSPECTED
        )


@pytest.mark.asyncio
async def test_expiry_during_install_keeps_slot_without_publishing(
    tmp_path, monkeypatch
):
    async with media_session(tmp_path) as (db, row, _factory):
        claim = await preparation.claim_preparation(db, row.id, now=100)
        slot = await preparation.reserve_poster_slot(db, claim, now=110)
        await preparation.stage_poster_metadata(db, claim, jpeg(), now=111)
        slot = row.metadata_jsonb[posters.POSTER_KEY]
        clock = [115]
        real_install = preparation.install_preparation_poster

        def expire_after_install(*args, **kwargs):
            result = real_install(*args, **kwargs)
            clock[0] = 221
            return result

        monkeypatch.setattr(preparation.time, "time", lambda: clock[0])
        monkeypatch.setattr(
            preparation, "install_preparation_poster", expire_after_install
        )
        assert not await preparation.finish_preparation(
            db,
            claim,
            inspected=INSPECTED,
            poster_data=jpeg(),
            storage_root=str(tmp_path),
            capacity_guard=Guard(),
        )
        await db.refresh(row)
        assert row.poster_storage_key is None
        assert row.metadata_jsonb[posters.POSTER_KEY]["state"] == "reserved"
        assert (tmp_path / slot["storage_key"]).exists()


@pytest.mark.asyncio
async def test_quota_rejects_before_any_artifact_reservation(tmp_path, monkeypatch):
    from app.services.video_storage_capacity import VideoReferenceStorageQuotaExceeded

    async with media_session(tmp_path) as (db, row, _factory):
        claim = await preparation.claim_preparation(db, row.id, now=100)

        async def at_quota(*_args, **_kwargs):
            return 1024 * 1024 * 1024

        monkeypatch.setattr(preparation, "reference_storage_usage", at_quota)
        with pytest.raises(VideoReferenceStorageQuotaExceeded):
            await preparation.reserve_poster_slot(db, claim, now=110)
        assert posters.POSTER_KEY not in row.metadata_jsonb
        assert list((tmp_path / row.storage_key).parent.iterdir()) == [
            tmp_path / row.storage_key
        ]


def test_retry_schema_is_strict_and_route_has_csrf():
    from pydantic import ValidationError
    from app.routes.video_preparation_routes import PreparationRetryIn, router
    from app.deps import verify_csrf

    args = dict(
        expected_source_sha256="a" * 64,
        expected_preparation_revision=2,
        idempotency_key="retry-request-key-123",
    )
    assert PreparationRetryIn(**args).expected_preparation_revision == 2
    for override in (
        {"expected_source_sha256": "mime.mp4"},
        {"expected_preparation_revision": True},
        {"expected_preparation_revision": -1},
        {"idempotency_key": "short"},
    ):
        with pytest.raises(ValidationError):
            PreparationRetryIn(**{**args, **override})
    route = router.routes[0]
    assert any(dep.call is verify_csrf for dep in route.dependant.dependencies)


@pytest.mark.asyncio
async def test_renderer_uses_bounded_existing_policy_and_rejects_changed_source(
    tmp_path, monkeypatch
):
    async with media_session(tmp_path) as (db, row, _factory):
        claim = await preparation.claim_preparation(db, row.id, now=100)
        slot = posters.poster_slot(claim)
        monkeypatch.setattr(posters.shutil, "which", lambda name: "/existing/" + name)
        calls = []

        def run(command, **kwargs):
            from pathlib import Path

            calls.append((command, kwargs))
            Path(command[-1]).write_bytes(jpeg())
            return type("Result", (), {"returncode": 0})()

        monkeypatch.setattr(posters.subprocess, "run", run)
        assert (
            posters.render_preparation_poster(claim, slot, storage_root=str(tmp_path))
            == jpeg()
        )
        command, options = calls[0]
        assert options["timeout"] == 60
        assert command[command.index("-max_alloc") + 1] == str(256 * 1024 * 1024)
        assert command[command.index("-frames:v") + 1] == "1"
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert "640:640" in command[command.index("-vf") + 1]

        def changed(command, **kwargs):
            result = run(command, **kwargs)
            (tmp_path / claim.storage_key).write_bytes(b"changed")
            return result

        monkeypatch.setattr(posters.subprocess, "run", changed)
        with pytest.raises(VideoReferenceVideoError, match="original changed"):
            posters.render_preparation_poster(claim, slot, storage_root=str(tmp_path))


@pytest.mark.asyncio
async def test_existing_unmanifested_or_wrong_hash_poster_is_not_adopted(tmp_path):
    async with media_session(tmp_path) as (db, row, _factory):
        claim = await preparation.claim_preparation(db, row.id, now=100)
        slot = posters.poster_slot(claim)
        (tmp_path / slot["storage_key"]).write_bytes(jpeg("blue"))
        with pytest.raises(VideoReferenceVideoError, match="identity changed"):
            posters.render_preparation_poster(claim, slot, storage_root=str(tmp_path))
        slot = {
            **slot,
            "sha256": hashlib.sha256(jpeg()).hexdigest(),
            "rendered_size_bytes": len(jpeg()),
        }
        with pytest.raises(VideoReferenceVideoError, match="identity changed"):
            posters.install_preparation_poster(
                claim, slot, jpeg(), storage_root=str(tmp_path)
            )


@pytest.mark.asyncio
async def test_capacity_child_cancel_does_not_cancel_parent_or_next_job(monkeypatch):
    calls = []

    async def work(_factory, video_id, **_kwargs):
        calls.append(video_id)
        if video_id == "lost":
            raise asyncio.CancelledError()
        return True

    monkeypatch.setattr(preparation, "prepare_video_metadata", work)
    assert not await preparation.run_preparation_job(
        None, "lost", storage_root="/unused"
    )
    assert await preparation.run_preparation_job(None, "next", storage_root="/unused")
    assert calls == ["lost", "next"]
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.asyncio
async def test_parent_cancellation_waits_for_child_cleanup_and_propagates(monkeypatch):
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def work(*_args, **_kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(preparation, "prepare_video_metadata", work)
    parent = asyncio.create_task(
        preparation.run_preparation_job(None, "job", storage_root="/unused")
    )
    await started.wait()
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_retry_http_owner_error_envelope_and_real_csrf_rejection(tmp_path):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.db import get_db
    from app.deps import get_current_user, verify_csrf
    from app.routes.video_preparation_routes import router

    async with media_session(tmp_path) as (db, row, _factory):
        claim = await preparation.claim_preparation(db, row.id, now=100)
        await preparation.finish_preparation(
            db, claim, now=110, error_code="unavailable"
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/videos")

        async def database():
            yield db

        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=row.user_id
        )
        body = {
            "expected_source_sha256": row.sha256,
            "expected_preparation_revision": 2,
            "idempotency_key": "http-retry-key-123",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/videos/{row.id}/preparation/retry", json=body
            )
            assert response.status_code == 401
            app.dependency_overrides[verify_csrf] = lambda: None
            response = await client.post(
                f"/api/videos/{row.id}/preparation/retry", json=body
            )
            assert response.status_code == 200
            asset = response.json()["asset"]
            assert asset["asset_id"] == row.id
            assert asset["preparation_state"] == "pending"
            assert "storage_key" not in asset
            response = await client.post(
                f"/api/videos/{row.id}/preparation/retry",
                json={**body, "expected_source_sha256": "f" * 64},
            )
            assert response.status_code == 409
            assert (
                response.json()["detail"]["error"]["code"]
                == "video_preparation_changed"
            )
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                id="foreign"
            )
            response = await client.post(
                f"/api/videos/{row.id}/preparation/retry", json=body
            )
            assert response.status_code == 404
