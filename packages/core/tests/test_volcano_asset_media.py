from __future__ import annotations

import asyncio
import hashlib
import io
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lumen_core import volcano_asset_media
from lumen_core.model_base import Base
from lumen_core.models import Image, ImageVariant, User, Video


class _StorageLease:
    def __init__(self) -> None:
        self.release_calls = 0

    async def renew(self) -> bool:
        return True

    async def release(self) -> None:
        self.release_calls += 1


class _StorageCapacity:
    def __init__(
        self,
        *,
        on_reserve: Callable[[], None] | None = None,
    ) -> None:
        self.on_reserve = on_reserve
        self.requests: list[int] = []
        self.lease = _StorageLease()

    async def reserve(self, bytes_required: int) -> _StorageLease:
        if self.on_reserve is not None:
            self.on_reserve()
        self.requests.append(bytes_required)
        return self.lease


class _PauseAfterUserLock:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.paused = False
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.session, name)

    async def execute(self, statement: Any) -> Any:
        result = await self.session.execute(statement)
        sql = str(statement)
        if (
            not self.paused
            and "FROM users" in sql
            and "FOR UPDATE" in sql
        ):
            self.paused = True
            await self.release.wait()
        return result


def _jpeg_bytes(size: tuple[int, int] = (300, 300)) -> bytes:
    output = io.BytesIO()
    PILImage.new("RGB", size, color=(120, 80, 40)).save(output, format="JPEG")
    return output.getvalue()


def _image(storage_key: str, *, image_id: str = "image-1") -> Image:
    return Image(
        id=image_id,
        user_id="user-1",
        source="uploaded",
        storage_key=storage_key,
        mime="image/png",
        width=300,
        height=300,
        size_bytes=100,
        sha256="0" * 64,
        visibility="private",
        metadata_jsonb={},
    )


def _video(
    storage_key: str,
    metadata: dict[str, object],
    *,
    video_id: str = "video-1",
) -> Video:
    return Video(
        id=video_id,
        user_id="user-1",
        storage_key=storage_key,
        mime="video/mp4",
        width=1280,
        height=720,
        duration_ms=2_000,
        fps=30.0,
        size_bytes=100,
        sha256="0" * 64,
        etag=f"{video_id}-etag",
        has_audio=False,
        faststart=True,
        visibility="private",
        metadata_jsonb=metadata,
    )


def _user() -> User:
    return User(
        id="user-1",
        email="volcano-media@example.test",
        display_name="Volcano Media",
    )


async def _sqlite_database(
    path: Path,
    *,
    tables: list[Any],
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=tables,
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.01)


def test_import_does_not_eager_load_media_or_change_pillow_global() -> None:
    script = """
import sys
from PIL import Image
before = Image.MAX_IMAGE_PIXELS
import lumen_core
assert "lumen_core.volcano_asset_media" not in sys.modules
import lumen_core.volcano_asset_media
assert Image.MAX_IMAGE_PIXELS == before
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_atomic_install_replaces_different_content(tmp_path: Path) -> None:
    destination = tmp_path / "variant.bin"
    destination.write_bytes(b"stale")
    expected = b"fresh"

    volcano_asset_media._install_file_atomic(
        destination,
        expected,
        sha256=hashlib.sha256(expected).hexdigest(),
    )

    assert destination.read_bytes() == expected


def test_atomic_install_fsyncs_new_parents_and_final_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "new" / "nested" / "variant.bin"
    payload = b"durable-variant"
    events: list[tuple[str, Path]] = []
    original_replace = volcano_asset_media.os.replace

    def replace(source: Path, target: Path) -> None:
        events.append(("replace", target))
        original_replace(source, target)

    def fsync_directory(path: Path) -> None:
        events.append(("fsync", path))

    monkeypatch.setattr(volcano_asset_media.os, "replace", replace)
    monkeypatch.setattr(
        volcano_asset_media,
        "_fsync_directory",
        fsync_directory,
    )

    volcano_asset_media._install_file_atomic(
        destination,
        payload,
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    replace_index = events.index(("replace", destination))
    assert ("fsync", tmp_path) in events[:replace_index]
    assert ("fsync", destination.parent.parent) in events[:replace_index]
    assert events[-1] == ("fsync", destination.parent)
    assert destination.read_bytes() == payload


def test_atomic_install_detects_a_competing_different_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "variant.bin"
    expected = b"fresh"
    original_replace = volcano_asset_media.os.replace

    def replace_then_overwrite(source: Path, target: Path) -> None:
        original_replace(source, target)
        target.write_bytes(b"competing-content")

    monkeypatch.setattr(volcano_asset_media.os, "replace", replace_then_overwrite)

    with pytest.raises(volcano_asset_media.VolcanoAssetMediaError) as exc_info:
        volcano_asset_media._install_file_atomic(
            destination,
            expected,
            sha256=hashlib.sha256(expected).hexdigest(),
        )

    assert exc_info.value.code == "volcano_asset_media_storage_conflict"


@pytest.mark.asyncio
async def test_image_prepare_io_is_outside_transaction_and_commit_is_caller_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _sqlite_database(
        tmp_path / "image-boundary.db",
        tables=[User.__table__, Image.__table__, ImageVariant.__table__],
    )
    storage_key = "images/source.png"
    source_path = tmp_path / storage_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    image = _image(storage_key)
    legacy_key = volcano_asset_media.volcano_asset_image_key(image)
    legacy_path = tmp_path / legacy_key
    legacy_path.write_bytes(b"not-a-jpeg")
    existing = ImageVariant(
        id="variant-1",
        image_id=image.id,
        kind=volcano_asset_media.VOLCANO_ASSET_IMAGE_KIND,
        storage_key=legacy_key,
        width=300,
        height=300,
    )
    rendered_data = _jpeg_bytes()
    rendered = volcano_asset_media.VolcanoAssetImageJpeg(
        data=rendered_data,
        width=300,
        height=300,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )

    try:
        async with factory() as setup:
            setup.add_all([_user(), image, existing])
            await setup.commit()

        async with factory() as session:
            loaded = (
                await session.execute(select(Image).where(Image.id == image.id))
            ).scalar_one()
            phases: list[str] = []

            def outside(name: str) -> None:
                assert session.in_transaction() is False
                phases.append(name)

            original_validate = volcano_asset_media._image_variant_file_is_valid
            original_install = volcano_asset_media._install_file_atomic

            def validate(*args: Any, **kwargs: Any) -> bool:
                outside("existing-check")
                return original_validate(*args, **kwargs)

            def render(_source: Path) -> Any:
                outside("transcode")
                return rendered

            def install(*args: Any, **kwargs: Any) -> bool:
                outside("install")
                return original_install(*args, **kwargs)

            monkeypatch.setattr(
                volcano_asset_media,
                "_image_variant_file_is_valid",
                validate,
            )
            monkeypatch.setattr(
                volcano_asset_media,
                "make_volcano_asset_image_jpeg",
                render,
            )
            monkeypatch.setattr(
                volcano_asset_media,
                "_install_file_atomic",
                install,
            )
            capacity = _StorageCapacity(on_reserve=lambda: outside("reserve"))

            result, receipt = (
                await volcano_asset_media.ensure_volcano_asset_image_variant(
                    session,
                    loaded,
                    storage_root=str(tmp_path),
                    storage_capacity=capacity,
                    storage_lease_ttl_seconds=30,
                )
            )

            assert phases == [
                "existing-check",
                "transcode",
                "reserve",
                "install",
            ]
            assert session.in_transaction() is True
            assert receipt is not None
            assert result.storage_key == receipt.storage_key
            assert result.storage_key != legacy_key
            assert (tmp_path / result.storage_key).read_bytes() == rendered_data
            assert legacy_path.read_bytes() == b"not-a-jpeg"

            async with factory() as observer:
                before_commit = (
                    await observer.execute(
                        select(ImageVariant).where(ImageVariant.id == existing.id)
                    )
                ).scalar_one()
                assert before_commit.storage_key == legacy_key

            await session.commit()

        async with factory() as observer:
            after_commit = (
                await observer.execute(
                    select(ImageVariant).where(ImageVariant.id == existing.id)
                )
            ).scalar_one()
            assert after_commit.storage_key == result.storage_key
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_video_prepare_io_is_outside_transaction_and_commit_is_caller_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _sqlite_database(
        tmp_path / "video-boundary.db",
        tables=[User.__table__, Video.__table__],
    )
    storage_key = "videos/source.mp4"
    source_path = tmp_path / storage_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    video = _video(storage_key, {})
    legacy_key = volcano_asset_media.volcano_asset_video_key(video)
    stale_data = b"stale-video"
    legacy_path = tmp_path / legacy_key
    legacy_path.write_bytes(stale_data)
    stale_variant = {
        "kind": volcano_asset_media.VOLCANO_ASSET_VIDEO_KIND,
        "storage_key": legacy_key,
        "mime": volcano_asset_media.VOLCANO_ASSET_VIDEO_MIME,
        "width": 1280,
        "height": 720,
        "duration_ms": 2_000,
        "fps": 30.0,
        "has_audio": False,
        "size_bytes": len(stale_data),
        "sha256": hashlib.sha256(b"different-video").hexdigest(),
    }
    video.metadata_jsonb = {
        volcano_asset_media.VOLCANO_ASSET_VIDEO_METADATA_KEY: stale_variant
    }
    rendered_data = b"fresh-video"
    rendered = volcano_asset_media.VolcanoAssetVideoMp4(
        data=rendered_data,
        width=1280,
        height=720,
        duration_ms=2_000,
        fps=30.0,
        has_audio=False,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )

    try:
        async with factory() as setup:
            setup.add_all([_user(), video])
            await setup.commit()

        async with factory() as session:
            loaded = (
                await session.execute(select(Video).where(Video.id == video.id))
            ).scalar_one()
            phases: list[str] = []

            def outside(name: str) -> None:
                assert session.in_transaction() is False
                phases.append(name)

            original_validate = volcano_asset_media._video_variant_file_is_valid
            original_install = volcano_asset_media._install_file_atomic

            def validate(*args: Any, **kwargs: Any) -> bool:
                outside("existing-check")
                return original_validate(*args, **kwargs)

            def render(_source: Path) -> Any:
                outside("transcode")
                return rendered

            def install(*args: Any, **kwargs: Any) -> bool:
                outside("install")
                return original_install(*args, **kwargs)

            monkeypatch.setattr(
                volcano_asset_media,
                "_video_variant_file_is_valid",
                validate,
            )
            monkeypatch.setattr(
                volcano_asset_media,
                "make_volcano_asset_video_mp4",
                render,
            )
            monkeypatch.setattr(
                volcano_asset_media,
                "_install_file_atomic",
                install,
            )
            capacity = _StorageCapacity(on_reserve=lambda: outside("reserve"))

            result, receipt = (
                await volcano_asset_media.ensure_volcano_asset_video_variant(
                    session,
                    loaded,
                    storage_root=str(tmp_path),
                    storage_capacity=capacity,
                    storage_lease_ttl_seconds=30,
                )
            )

            assert phases == [
                "existing-check",
                "transcode",
                "reserve",
                "install",
            ]
            assert session.in_transaction() is True
            assert receipt is not None
            assert result["storage_key"] == receipt.storage_key
            assert result["storage_key"] != legacy_key
            assert (tmp_path / str(result["storage_key"])).read_bytes() == rendered_data
            assert legacy_path.read_bytes() == stale_data

            async with factory() as observer:
                before_commit = (
                    await observer.execute(select(Video).where(Video.id == video.id))
                ).scalar_one()
                assert (
                    before_commit.metadata_jsonb[
                        volcano_asset_media.VOLCANO_ASSET_VIDEO_METADATA_KEY
                    ]["storage_key"]
                    == legacy_key
                )

            await session.commit()

        async with factory() as observer:
            after_commit = (
                await observer.execute(select(Video).where(Video.id == video.id))
            ).scalar_one()
            assert (
                after_commit.metadata_jsonb[
                    volcano_asset_media.VOLCANO_ASSET_VIDEO_METADATA_KEY
                ]
                == result
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_video_quota_failure_rolls_back_and_cleans_own_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _sqlite_database(
        tmp_path / "video-quota.db",
        tables=[User.__table__, Video.__table__],
    )
    source = _video("videos/source.mp4", {})
    source_path = tmp_path / source.storage_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    retained = _video(
        "u/user-1/vref/retained/original.mp4",
        {},
        video_id="retained",
    )
    retained.size_bytes = 15
    rendered_data = b"0123456789"
    rendered = volcano_asset_media.VolcanoAssetVideoMp4(
        data=rendered_data,
        width=1280,
        height=720,
        duration_ms=2_000,
        fps=30.0,
        has_audio=False,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )
    installed: list[Path] = []
    original_install = volcano_asset_media._install_file_atomic

    def install(path: Path, *args: Any, **kwargs: Any) -> bool:
        installed.append(path)
        return original_install(path, *args, **kwargs)

    monkeypatch.setattr(
        volcano_asset_media,
        "make_volcano_asset_video_mp4",
        lambda _source: rendered,
    )
    monkeypatch.setattr(
        volcano_asset_media,
        "_install_file_atomic",
        install,
    )
    monkeypatch.setattr(
        volcano_asset_media,
        "VIDEO_REFERENCE_STORAGE_QUOTA_BYTES",
        20,
    )

    try:
        async with factory() as setup:
            setup.add_all([_user(), source, retained])
            await setup.commit()

        async with factory() as session:
            loaded = (
                await session.execute(select(Video).where(Video.id == source.id))
            ).scalar_one()
            with pytest.raises(
                volcano_asset_media.VolcanoAssetMediaError
            ) as exc_info:
                await volcano_asset_media.ensure_volcano_asset_video_variant(
                    session,
                    loaded,
                    storage_root=str(tmp_path),
                    storage_capacity=_StorageCapacity(),
                    storage_lease_ttl_seconds=30,
                )

            assert exc_info.value.code == "reference_video_quota_exceeded"
            assert exc_info.value.status_code == 429
            assert session.in_transaction() is False
            assert len(installed) == 1
            assert installed[0].exists() is False

        async with factory() as observer:
            stored = (
                await observer.execute(select(Video).where(Video.id == source.id))
            ).scalar_one()
            assert volcano_asset_media.VOLCANO_ASSET_VIDEO_METADATA_KEY not in (
                stored.metadata_jsonb
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_image_concurrent_loser_keeps_winner_file_with_real_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _sqlite_database(
        tmp_path / "image-race.db",
        tables=[User.__table__, Image.__table__, ImageVariant.__table__],
    )
    image = _image("images/source.png")
    source_path = tmp_path / image.storage_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    rendered_data = _jpeg_bytes()
    rendered = volcano_asset_media.VolcanoAssetImageJpeg(
        data=rendered_data,
        width=300,
        height=300,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )
    barrier = threading.Barrier(2)

    def render(_source: Path) -> Any:
        barrier.wait(timeout=3)
        return rendered

    monkeypatch.setattr(
        volcano_asset_media,
        "make_volcano_asset_image_jpeg",
        render,
    )

    async def contender() -> tuple[str, Any]:
        async with factory() as session:
            loaded = (
                await session.execute(select(Image).where(Image.id == image.id))
            ).scalar_one()
            variant, receipt = (
                await volcano_asset_media.ensure_volcano_asset_image_variant(
                    session,
                    loaded,
                    storage_root=str(tmp_path),
                    storage_capacity=_StorageCapacity(),
                    storage_lease_ttl_seconds=30,
                )
            )
            await session.commit()
            return str(variant.storage_key), receipt

    try:
        async with factory() as setup:
            setup.add_all([_user(), image])
            await setup.commit()

        results = await asyncio.gather(contender(), contender())
        storage_keys = {storage_key for storage_key, _receipt in results}
        receipts = [receipt for _storage_key, receipt in results]

        assert len(storage_keys) == 1
        assert sum(receipt is not None for receipt in receipts) == 1
        winner_key = next(iter(storage_keys))
        winner_path = tmp_path / winner_key
        assert winner_path.read_bytes() == rendered_data
        installed = list(
            source_path.parent.glob(
                f"{image.id}.{volcano_asset_media.VOLCANO_ASSET_IMAGE_KIND}.*.jpg"
            )
        )
        assert installed == [winner_path]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_video_concurrent_loser_keeps_winner_file_with_real_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _sqlite_database(
        tmp_path / "video-race.db",
        tables=[User.__table__, Video.__table__],
    )
    video = _video("videos/source.mp4", {})
    source_path = tmp_path / video.storage_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    rendered_data = b"concurrent-video"
    rendered = volcano_asset_media.VolcanoAssetVideoMp4(
        data=rendered_data,
        width=1280,
        height=720,
        duration_ms=2_000,
        fps=30.0,
        has_audio=False,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )
    barrier = threading.Barrier(2)

    def render(_source: Path) -> Any:
        barrier.wait(timeout=3)
        return rendered

    monkeypatch.setattr(
        volcano_asset_media,
        "make_volcano_asset_video_mp4",
        render,
    )

    async def contender() -> tuple[str, Any]:
        async with factory() as session:
            loaded = (
                await session.execute(select(Video).where(Video.id == video.id))
            ).scalar_one()
            variant, receipt = (
                await volcano_asset_media.ensure_volcano_asset_video_variant(
                    session,
                    loaded,
                    storage_root=str(tmp_path),
                    storage_capacity=_StorageCapacity(),
                    storage_lease_ttl_seconds=30,
                )
            )
            await session.commit()
            return str(variant["storage_key"]), receipt

    try:
        async with factory() as setup:
            setup.add_all([_user(), video])
            await setup.commit()

        results = await asyncio.gather(contender(), contender())
        storage_keys = {storage_key for storage_key, _receipt in results}
        receipts = [receipt for _storage_key, receipt in results]

        assert len(storage_keys) == 1
        assert sum(receipt is not None for receipt in receipts) == 1
        winner_key = next(iter(storage_keys))
        winner_path = tmp_path / winner_key
        assert winner_path.read_bytes() == rendered_data
        installed = list(
            source_path.parent.glob(
                f"{video.id}.{volcano_asset_media.VOLCANO_ASSET_VIDEO_KIND}.*.mp4"
            )
        )
        assert installed == [winner_path]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_install_returns_without_waiting_and_cleans_in_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _sqlite_database(
        tmp_path / "cancel-install.db",
        tables=[User.__table__, Image.__table__, ImageVariant.__table__],
    )
    image = _image("images/source.png")
    source_path = tmp_path / image.storage_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    rendered_data = _jpeg_bytes()
    rendered = volcano_asset_media.VolcanoAssetImageJpeg(
        data=rendered_data,
        width=300,
        height=300,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )
    started = threading.Event()
    finish = threading.Event()
    installed: list[Path] = []

    def blocking_install(
        path: Path,
        data: bytes,
        *,
        sha256: str,
    ) -> bool:
        assert hashlib.sha256(data).hexdigest() == sha256
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        installed.append(path)
        started.set()
        assert finish.wait(timeout=3)
        return True

    monkeypatch.setattr(
        volcano_asset_media,
        "make_volcano_asset_image_jpeg",
        lambda _source: rendered,
    )
    monkeypatch.setattr(
        volcano_asset_media,
        "_install_file_atomic",
        blocking_install,
    )
    capacity = _StorageCapacity()

    try:
        async with factory() as setup:
            setup.add_all([_user(), image])
            await setup.commit()

        async with factory() as session:
            loaded = (
                await session.execute(select(Image).where(Image.id == image.id))
            ).scalar_one()
            task = asyncio.create_task(
                volcano_asset_media.ensure_volcano_asset_image_variant(
                    session,
                    loaded,
                    storage_root=str(tmp_path),
                    storage_capacity=capacity,
                    storage_lease_ttl_seconds=30,
                )
            )
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.5)

            assert session.in_transaction() is False
            assert len(installed) == 1
            assert installed[0].exists()

            finish.set()
            await _wait_until(lambda: installed[0].exists() is False)
            await _wait_until(lambda: capacity.lease.release_calls == 1)
    finally:
        finish.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_prepare_total_deadline_does_not_wait_for_install_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _sqlite_database(
        tmp_path / "prepare-timeout.db",
        tables=[User.__table__, Image.__table__, ImageVariant.__table__],
    )
    image = _image("images/source.png")
    source_path = tmp_path / image.storage_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    rendered_data = _jpeg_bytes()
    rendered = volcano_asset_media.VolcanoAssetImageJpeg(
        data=rendered_data,
        width=300,
        height=300,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )
    started = threading.Event()
    finish = threading.Event()
    installed: list[Path] = []

    def blocking_install(
        path: Path,
        data: bytes,
        *,
        sha256: str,
    ) -> bool:
        assert hashlib.sha256(data).hexdigest() == sha256
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        installed.append(path)
        started.set()
        assert finish.wait(timeout=3)
        return True

    monkeypatch.setattr(
        volcano_asset_media,
        "make_volcano_asset_image_jpeg",
        lambda _source: rendered,
    )
    monkeypatch.setattr(
        volcano_asset_media,
        "_install_file_atomic",
        blocking_install,
    )
    monkeypatch.setattr(
        volcano_asset_media,
        "VOLCANO_ASSET_PREPARE_TIMEOUT_SECONDS",
        0.05,
    )

    try:
        async with factory() as setup:
            setup.add_all([_user(), image])
            await setup.commit()

        async with factory() as session:
            loaded = (
                await session.execute(select(Image).where(Image.id == image.id))
            ).scalar_one()
            with pytest.raises(
                volcano_asset_media.VolcanoAssetMediaError
            ) as exc_info:
                await volcano_asset_media.ensure_volcano_asset_image_variant(
                    session,
                    loaded,
                    storage_root=str(tmp_path),
                    storage_capacity=_StorageCapacity(),
                    storage_lease_ttl_seconds=30,
                )

            assert exc_info.value.code == "volcano_asset_media_prepare_timeout"
            assert await asyncio.to_thread(started.wait, 1)
            assert session.in_transaction() is False
            assert len(installed) == 1
            assert installed[0].exists()

            finish.set()
            await _wait_until(lambda: installed[0].exists() is False)
    finally:
        finish.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_final_transaction_timeout_rolls_back_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _sqlite_database(
        tmp_path / "final-timeout.db",
        tables=[User.__table__, Image.__table__, ImageVariant.__table__],
    )
    image = _image("images/source.png")
    source_path = tmp_path / image.storage_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    rendered_data = _jpeg_bytes()
    rendered = volcano_asset_media.VolcanoAssetImageJpeg(
        data=rendered_data,
        width=300,
        height=300,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )
    installed: list[Path] = []
    original_install = volcano_asset_media._install_file_atomic

    def install(path: Path, *args: Any, **kwargs: Any) -> bool:
        installed.append(path)
        return original_install(path, *args, **kwargs)

    monkeypatch.setattr(
        volcano_asset_media,
        "make_volcano_asset_image_jpeg",
        lambda _source: rendered,
    )
    monkeypatch.setattr(
        volcano_asset_media,
        "_install_file_atomic",
        install,
    )
    monkeypatch.setattr(
        volcano_asset_media,
        "VOLCANO_ASSET_FINALIZE_TIMEOUT_SECONDS",
        0.1,
    )

    try:
        async with factory() as setup:
            setup.add_all([_user(), image])
            await setup.commit()

        async with factory() as inner:
            loaded = (
                await inner.execute(select(Image).where(Image.id == image.id))
            ).scalar_one()
            session = _PauseAfterUserLock(inner)
            with pytest.raises(
                volcano_asset_media.VolcanoAssetMediaError
            ) as exc_info:
                await volcano_asset_media.ensure_volcano_asset_image_variant(
                    session,
                    loaded,
                    storage_root=str(tmp_path),
                    storage_capacity=_StorageCapacity(),
                    storage_lease_ttl_seconds=30,
                )

            assert exc_info.value.code == "volcano_asset_media_database_timeout"
            assert session.paused is True
            assert inner.in_transaction() is False
            assert len(installed) == 1
            assert installed[0].exists() is False
    finally:
        await engine.dispose()


def test_video_transcode_semaphore_is_scoped_to_running_loop() -> None:
    async def capture() -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
        return (
            volcano_asset_media._video_transcode_semaphore(),
            volcano_asset_media._video_transcode_semaphore(),
        )

    first, first_again = asyncio.run(capture())
    second, second_again = asyncio.run(capture())

    assert first is first_again
    assert second is second_again
    assert first is not second


def test_video_transcode_runtime_reset_clears_loop_semaphores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = volcano_asset_media._VideoTranscodeRuntime()
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    try:
        runtime.semaphore_for_running_loop()
        assert len(runtime.semaphores) == 1
        runtime.reset()
        assert len(runtime.semaphores) == 0
    finally:
        loop.close()
