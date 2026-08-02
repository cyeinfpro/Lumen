from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import video_reference_videos
from app.video_reference_videos import VideoReferenceVideoError


@pytest.mark.parametrize("streams", [None, {}, "not-a-list"])
def test_probe_video_rejects_non_list_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    streams: object,
) -> None:
    payload = json.dumps({"streams": streams}).encode()

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=0,
            stdout=payload,
            stderr=b"",
        )

    monkeypatch.setattr(video_reference_videos.subprocess, "run", fake_run)

    with pytest.raises(VideoReferenceVideoError) as exc_info:
        video_reference_videos._probe_video("ffprobe", tmp_path / "source.mp4")

    assert exc_info.value.code == "invalid_video"
    assert exc_info.value.message == "reference video has no video stream"


@asynccontextmanager
async def _variant_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(text("select 1"))
            yield session
    finally:
        await engine.dispose()


def _write_variant(tmp_path: Path) -> Path:
    path = tmp_path / "variant.mp4"
    path.write_bytes(b"transcoded reference video payload")
    return path


def _register_cleanup(
    session: AsyncSession,
    path: Path,
    *,
    size_bytes: int | None = None,
    sha256: str | None = None,
) -> None:
    payload = path.read_bytes()
    video_reference_videos._discard_orphaned_variant_on_rollback(
        session,
        path,
        size_bytes=path.stat().st_size if size_bytes is None else size_bytes,
        sha256=video_reference_videos._sha256_file(path) if sha256 is None else sha256,
    )
    assert payload == path.read_bytes()


@pytest.mark.asyncio
async def test_rollback_discards_installed_variant(tmp_path: Path) -> None:
    async with _variant_session() as session:
        path = _write_variant(tmp_path)
        _register_cleanup(session, path)
        await session.rollback()
        assert not path.exists()


@pytest.mark.asyncio
async def test_commit_keeps_installed_variant(tmp_path: Path) -> None:
    async with _variant_session() as session:
        path = _write_variant(tmp_path)
        _register_cleanup(session, path)
        await session.commit()
        assert path.exists()
        await session.execute(text("select 1"))
        await session.rollback()
        assert path.exists()


@pytest.mark.asyncio
async def test_close_without_commit_discards_installed_variant(
    tmp_path: Path,
) -> None:
    async with _variant_session() as session:
        path = _write_variant(tmp_path)
        _register_cleanup(session, path)
        await session.close()
        assert not path.exists()


@pytest.mark.asyncio
async def test_rollback_keeps_unrelated_file(tmp_path: Path) -> None:
    async with _variant_session() as session:
        path = _write_variant(tmp_path)
        _register_cleanup(
            session,
            path,
            sha256="0" * 64,
        )
        await session.rollback()
        assert path.exists()


@pytest.mark.asyncio
async def test_savepoint_release_alone_keeps_variant_then_rollback_discards(
    tmp_path: Path,
) -> None:
    async with _variant_session() as session:
        path = _write_variant(tmp_path)
        _register_cleanup(session, path)
        async with session.begin_nested():
            await session.execute(text("select 1"))
        assert path.exists()
        await session.rollback()
        assert not path.exists()


@pytest.mark.asyncio
async def test_savepoint_rollback_discards_variant_on_root_commit(
    tmp_path: Path,
) -> None:
    async with _variant_session() as session:
        path = _write_variant(tmp_path)
        try:
            async with session.begin_nested():
                _register_cleanup(session, path)
                await session.execute(text("select 1"))
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        await session.commit()
        assert not path.exists()


@pytest.mark.asyncio
async def test_unrelated_savepoint_rollback_keeps_root_installed_variant(
    tmp_path: Path,
) -> None:
    async with _variant_session() as session:
        path = _write_variant(tmp_path)
        _register_cleanup(session, path)
        try:
            async with session.begin_nested():
                await session.execute(text("select 1"))
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        await session.commit()
        assert path.exists()
