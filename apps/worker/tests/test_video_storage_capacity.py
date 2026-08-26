from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.storage import LocalStorage
from app.storage_writes import StorageWriteCoordinator
from app.tasks.video_generation_parts import persistence
from app.tasks.video_generation_parts import entrypoints as video_generation
from app.tasks.video_generation_parts.default_runtime import (
    build_video_generation_runtime,
)
from app.tasks.video_generation_parts.runtime import _VIDEO_PORTS
from app.video_artifacts import DownloadedVideo, ProcessedVideoFile


class _Lease:
    def __init__(self) -> None:
        self.release_calls = 0

    async def renew(self) -> bool:
        return True

    async def release(self) -> None:
        self.release_calls += 1


class _Capacity:
    def __init__(self) -> None:
        self.requests: list[int] = []
        self.lease = _Lease()

    async def reserve(self, bytes_required: int) -> _Lease:
        self.requests.append(bytes_required)
        return self.lease


def _runtime_ports(
    storage: LocalStorage,
    coordinator: StorageWriteCoordinator,
    **changes: Any,
) -> Any:
    runtime = build_video_generation_runtime(storage_writes=coordinator)
    store = replace(runtime.ports.store, storage=storage, **changes)
    return replace(runtime.ports, store=store)


@pytest.mark.asyncio
async def test_byte_video_and_poster_share_one_capacity_reservation(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    capacity = _Capacity()
    coordinator = StorageWriteCoordinator(
        storage=storage,
        capacity=capacity,  # type: ignore[arg-type]
        lease_ttl_seconds=30,
    )

    def postprocess(
        _data: bytes,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {
                "video_bytes": b"video",
                "poster_bytes": b"poster",
                "mime": "video/mp4",
                "extension": ".mp4",
                "width": 16,
                "height": 9,
                "duration_ms": 1000,
                "faststart": True,
            },
            {},
        )

    ports = _runtime_ports(
        storage,
        coordinator,
        _postprocess_video_bytes=postprocess,
    )
    generation = SimpleNamespace(id="video-1", user_id="user-1")
    with _VIDEO_PORTS.use(ports):
        stored = await persistence.store_video_asset(
            generation,
            b"upstream",
            artifact_attempt_id="attempt-1",
        )

    assert capacity.requests == [2 * (len(b"video") + len(b"poster"))]
    assert capacity.lease.release_calls == 1
    assert stored.created_storage_keys == (
        "u/user-1/v/video-1/final/attempt-1/output.mp4",
        "u/user-1/v/video-1/final/attempt-1/poster.jpg",
    )
    assert storage.get_bytes(stored.video.storage_key) == b"video"
    assert storage.get_bytes(stored.video.poster_storage_key) == b"poster"


@pytest.mark.asyncio
async def test_file_video_copy_and_poster_share_one_capacity_reservation(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    capacity = _Capacity()
    coordinator = StorageWriteCoordinator(
        storage=storage,
        capacity=capacity,  # type: ignore[arg-type]
        lease_ttl_seconds=30,
    )
    processed_path = tmp_path / "processed.mp4"
    processed_path.write_bytes(b"video-file")
    download_path = tmp_path / "download.part"
    download_path.write_bytes(b"download")
    processed = ProcessedVideoFile(
        path=processed_path,
        mime="video/mp4",
        extension=".mp4",
        size_bytes=len(b"video-file"),
        sha256="a" * 64,
        poster_bytes=b"poster",
        faststart=True,
        metadata={"width": 16, "height": 9, "duration_ms": 1000},
    )
    downloaded = DownloadedVideo(
        path=download_path,
        mime="video/mp4",
        extension=".mp4",
        size_bytes=len(b"download"),
        temporary=False,
    )
    ports = _runtime_ports(
        storage,
        coordinator,
        _postprocess_video_file=lambda _downloaded: processed,
        copy_video_file_exclusive_result=lambda *_args, **_kwargs: SimpleNamespace(
            created=True
        ),
    )
    generation = SimpleNamespace(id="video-1", user_id="user-1")
    with _VIDEO_PORTS.use(ports):
        stored = await persistence.store_downloaded_video_asset(
            generation,
            downloaded,
            lease_lost=None,
            artifact_attempt_id="attempt-1",
        )

    assert capacity.requests == [2 * (len(b"video-file") + len(b"poster"))]
    assert capacity.lease.release_calls == 1
    assert stored.created_storage_keys == (
        "u/user-1/v/video-1/final/attempt-1/output.mp4",
        "u/user-1/v/video-1/final/attempt-1/poster.jpg",
    )


@pytest.mark.asyncio
async def test_video_entrypoint_requires_startup_runtime() -> None:
    with pytest.raises(
        TypeError,
        match=r"ctx\['video_generation_runtime'\]",
    ):
        await video_generation.run_video_generation(
            {"redis": object()},
            "video-1",
        )
