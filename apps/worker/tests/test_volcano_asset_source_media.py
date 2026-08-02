from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.tasks import volcano_asset_source_media
from lumen_core.capacity_leases import maintained_capacity_lease
from lumen_core.models import Base, User, Video
from lumen_core.volcano_asset_media import (
    VOLCANO_ASSET_VIDEO_KIND,
    VOLCANO_ASSET_VIDEO_METADATA_KEY,
    VolcanoAssetInstallReceipt,
    VolcanoAssetMediaError,
    VolcanoAssetVideoMp4,
)


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Session:
    def __init__(
        self,
        source: Any,
        *,
        commit_error: BaseException | None = None,
    ) -> None:
        self.source = source
        self.commit_error = commit_error

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, _statement: Any) -> _Result:
        return _Result(self.source)

    async def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        return None


def _operation() -> dict[str, Any]:
    return {
        "asset_type": "Image",
        "local_source_id": "image-1",
        "user_id": "user-1",
        "public_base_url": "https://lumen.example",
    }


@pytest.mark.asyncio
async def test_commit_failure_preserves_receipt_when_outcome_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(id="image-1", metadata_jsonb={})
    receipt = VolcanoAssetInstallReceipt(
        storage_key="images/image-1.volcano.jpg",
        size_bytes=7,
        sha256="a" * 64,
    )
    cleaned: list[VolcanoAssetInstallReceipt] = []

    async def ensure(*_args: Any, **_kwargs: Any) -> tuple[object, Any]:
        return SimpleNamespace(storage_key=receipt.storage_key), receipt

    async def not_durable(**_kwargs: Any) -> bool:
        return False

    async def delete(_root: str, value: Any) -> bool:
        cleaned.append(value)
        return True

    monkeypatch.setattr(
        volcano_asset_source_media,
        "SessionLocal",
        lambda: _Session(source, commit_error=RuntimeError("commit failed")),
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "ensure_volcano_asset_image_variant",
        ensure,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "delete_volcano_asset_install",
        delete,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_image_adoption_is_durable",
        not_durable,
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await volcano_asset_source_media.normalized_source_url(
            _operation(),
            storage_writes=SimpleNamespace(
                capacity=object(),
                lease_ttl_seconds=30,
            ),
        )

    assert cleaned == []


@pytest.mark.asyncio
async def test_successful_commit_adopts_install_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(id="image-1", metadata_jsonb={})
    receipt = VolcanoAssetInstallReceipt(
        storage_key="images/image-1.volcano.jpg",
        size_bytes=7,
        sha256="a" * 64,
    )
    cleaned: list[VolcanoAssetInstallReceipt] = []

    async def ensure(*_args: Any, **_kwargs: Any) -> tuple[object, Any]:
        return SimpleNamespace(storage_key=receipt.storage_key), receipt

    async def delete(_root: str, value: Any) -> bool:
        cleaned.append(value)
        return True

    monkeypatch.setattr(
        volcano_asset_source_media,
        "SessionLocal",
        lambda: _Session(source),
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "ensure_volcano_asset_image_variant",
        ensure,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "delete_volcano_asset_install",
        delete,
    )

    url, kind = await volcano_asset_source_media.normalized_source_url(
        _operation(),
        storage_writes=SimpleNamespace(
            capacity=object(),
            lease_ttl_seconds=30,
        ),
    )

    assert url.startswith("https://lumen.example/")
    assert kind == "volcano_asset_img_v1"
    assert cleaned == []


@pytest.mark.asyncio
async def test_image_commit_response_loss_probes_durable_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant_key = "images/image-1.volcano.jpg"
    receipt = VolcanoAssetInstallReceipt(
        storage_key=variant_key,
        size_bytes=7,
        sha256="a" * 64,
    )
    persisted_metadata: dict[str, Any] = {}
    sessions = [0]
    cleaned: list[VolcanoAssetInstallReceipt] = []

    class _Result:
        def __init__(self, value: Any) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

    class _CommitSession:
        def __init__(self, index: int) -> None:
            self.index = index
            self.execute_count = 0
            self.source = SimpleNamespace(
                id="image-1",
                metadata_jsonb=dict(persisted_metadata),
            )

        async def __aenter__(self) -> _CommitSession:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def execute(self, statement: Any) -> _Result:
            self.execute_count += 1
            if self.index == 1:
                if self.execute_count == 1:
                    return _Result(
                        SimpleNamespace(
                            id="image-1",
                            metadata_jsonb=dict(persisted_metadata),
                        )
                    )
                return _Result("variant-id")
            if "FROM users" in str(statement):
                return _Result("user-1")
            return _Result(self.source)

        async def commit(self) -> None:
            nonlocal persisted_metadata
            persisted_metadata = dict(self.source.metadata_jsonb)
            raise RuntimeError("commit response lost")

        async def rollback(self) -> None:
            return None

    def session_factory() -> _CommitSession:
        index = sessions[0]
        sessions[0] += 1
        return _CommitSession(index)

    async def ensure(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return SimpleNamespace(storage_key=variant_key), receipt

    async def delete(_root: str, value: Any) -> bool:
        cleaned.append(value)
        return True

    monkeypatch.setattr(
        volcano_asset_source_media,
        "SessionLocal",
        session_factory,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "ensure_volcano_asset_image_variant",
        ensure,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "delete_volcano_asset_install",
        delete,
    )

    url, kind = await volcano_asset_source_media.normalized_source_url(
        _operation(),
        storage_writes=SimpleNamespace(
            capacity=object(),
            lease_ttl_seconds=30,
        ),
    )

    assert url.startswith("https://lumen.example/")
    assert kind == "volcano_asset_img_v1"
    assert sessions == [2]
    assert persisted_metadata["video_reference_access_token"]
    assert cleaned == []


@pytest.mark.asyncio
async def test_source_normalization_requires_storage_coordinator() -> None:
    with pytest.raises(RuntimeError, match="storage_write_coordinator"):
        await volcano_asset_source_media.normalized_source_url(_operation())


@pytest.mark.asyncio
async def test_video_file_io_is_outside_transactions_and_adoption_is_bounded(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_key = "videos/source.mp4"
    source_path = tmp_path / storage_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    video = SimpleNamespace(
        id="video-1",
        user_id="user-1",
        storage_key=storage_key,
        sha256="a" * 64,
        etag="video-etag",
        size_bytes=6,
        deleted_at=None,
        metadata_jsonb={},
    )
    rendered_data = b"normalized-video"
    rendered = VolcanoAssetVideoMp4(
        data=rendered_data,
        width=1280,
        height=720,
        duration_ms=2_000,
        fps=30.0,
        has_audio=False,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )
    active_sessions = [0]
    session_index = [0]
    statements: list[tuple[int, str]] = []
    commits: list[int] = []
    rollbacks: list[int] = []

    class _Rows:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows

        def all(self) -> list[Any]:
            return self.rows

    class _VideoResult:
        def __init__(
            self,
            *,
            value: Any = None,
            rows: list[Any] | None = None,
            rowcount: int = 0,
        ) -> None:
            self.value = value
            self.rows = rows
            self.rowcount = rowcount

        def scalar_one_or_none(self) -> Any:
            return self.value

        def scalars(self) -> _Rows:
            return _Rows(self.rows or [])

    class _VideoSession:
        def __init__(self, index: int) -> None:
            self.index = index
            self.execute_count = 0

        async def __aenter__(self) -> _VideoSession:
            active_sessions[0] += 1
            return self

        async def __aexit__(self, *_args: Any) -> None:
            active_sessions[0] -= 1

        async def execute(self, statement: Any) -> _VideoResult:
            self.execute_count += 1
            statements.append((self.index, str(statement)))
            if self.index == 0:
                return _VideoResult(value=video)
            if self.execute_count == 1:
                return _VideoResult(value=video.user_id)
            if self.execute_count == 2:
                return _VideoResult(value=video)
            if self.execute_count == 3:
                return _VideoResult(rows=[video])
            return _VideoResult(rowcount=1)

        async def rollback(self) -> None:
            rollbacks.append(self.index)

        async def commit(self) -> None:
            commits.append(self.index)

    def session_factory() -> _VideoSession:
        index = session_index[0]
        session_index[0] += 1
        return _VideoSession(index)

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

        async def reserve(self, size_bytes: int) -> _Lease:
            assert active_sessions[0] == 0
            self.requests.append(size_bytes)
            return self.lease

    def render(
        _source: Any,
        *,
        timeout_seconds: float,
    ) -> VolcanoAssetVideoMp4:
        assert active_sessions[0] == 0
        assert (
            timeout_seconds
            == volcano_asset_source_media._VIDEO_TRANSCODE_PROCESS_TIMEOUT_SECONDS
        )
        return rendered

    original_write = volcano_asset_source_media._write_video_stage
    original_install = volcano_asset_source_media._install_video_stage_atomic

    def write_stage(path: Any, value: VolcanoAssetVideoMp4) -> None:
        assert active_sessions[0] == 0
        original_write(path, value)

    def install_stage(*args: Any, **kwargs: Any) -> bool:
        assert active_sessions[0] == 0
        return original_install(*args, **kwargs)

    monkeypatch.setattr(
        volcano_asset_source_media.settings,
        "storage_root",
        str(tmp_path),
    )
    monkeypatch.setattr(volcano_asset_source_media, "SessionLocal", session_factory)
    monkeypatch.setattr(
        volcano_asset_source_media,
        "make_volcano_asset_video_mp4",
        render,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_write_video_stage",
        write_stage,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_install_video_stage_atomic",
        install_stage,
    )
    capacity = _Capacity()

    url, kind = await volcano_asset_source_media.normalized_source_url(
        {
            "asset_type": "Video",
            "local_source_id": video.id,
            "user_id": video.user_id,
            "public_base_url": "https://lumen.example",
        },
        storage_writes=SimpleNamespace(
            capacity=capacity,
            lease_ttl_seconds=30,
        ),
    )

    assert url.startswith("https://lumen.example/")
    assert kind == VOLCANO_ASSET_VIDEO_KIND
    assert active_sessions == [0]
    assert rollbacks == [0]
    assert commits == [1]
    assert capacity.requests == [2 * len(rendered_data)]
    assert capacity.lease.release_calls == 1
    installed = list(source_path.parent.glob(f"{video.id}.{kind}.*.mp4"))
    assert len(installed) == 1
    assert installed[0].read_bytes() == rendered_data

    snapshot_sql = [sql for index, sql in statements if index == 0]
    adoption_sql = [sql for index, sql in statements if index == 1]
    assert len(snapshot_sql) == 1
    assert "FOR UPDATE" not in snapshot_sql[0]
    user_locks = [sql for sql in adoption_sql if "FROM users" in sql]
    assert len(user_locks) == 1
    assert "FOR UPDATE" in user_locks[0]
    video_selects = [
        sql
        for sql in adoption_sql
        if sql.lstrip().startswith("SELECT") and "FROM videos" in sql
    ]
    assert len(video_selects) == 2
    assert all("FOR UPDATE" not in sql for sql in video_selects)
    assert any("ORDER BY videos.id" in sql and "LIMIT" in sql for sql in video_selects)
    cas_sql = [sql for sql in adoption_sql if sql.lstrip().startswith("UPDATE")]
    assert len(cas_sql) == 1
    assert "videos.user_id" in cas_sql[0]
    assert "videos.etag" in cas_sql[0]
    assert "videos.metadata_jsonb" in cas_sql[0]


@pytest.mark.asyncio
async def test_video_pipeline_cas_persists_with_real_sqlite(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    storage_key = "videos/source.mp4"
    source_path = tmp_path / storage_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    rendered_data = b"sqlite-normalized-video"
    rendered = VolcanoAssetVideoMp4(
        data=rendered_data,
        width=1280,
        height=720,
        duration_ms=2_000,
        fps=30.0,
        has_audio=False,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )

    class _Lease:
        async def renew(self) -> bool:
            return True

        async def release(self) -> None:
            return None

    class _Capacity:
        async def reserve(self, _size_bytes: int) -> _Lease:
            return _Lease()

    def render(
        _source: Any,
        *,
        timeout_seconds: float,
    ) -> VolcanoAssetVideoMp4:
        assert timeout_seconds > 0
        return rendered

    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[User.__table__, Video.__table__],
                )
            )
        async with factory() as setup:
            setup.add(
                User(
                    id="user-1",
                    email="video-source@example.test",
                    display_name="Video Source",
                )
            )
            setup.add(
                Video(
                    id="video-1",
                    user_id="user-1",
                    storage_key=storage_key,
                    mime="video/mp4",
                    width=1280,
                    height=720,
                    duration_ms=2_000,
                    fps=30.0,
                    size_bytes=len(b"source"),
                    sha256="a" * 64,
                    etag="video-etag",
                    has_audio=False,
                    faststart=True,
                    visibility="private",
                    metadata_jsonb={},
                )
            )
            await setup.commit()

        monkeypatch.setattr(
            volcano_asset_source_media.settings,
            "storage_root",
            str(tmp_path),
        )
        monkeypatch.setattr(volcano_asset_source_media, "SessionLocal", factory)
        monkeypatch.setattr(
            volcano_asset_source_media,
            "make_volcano_asset_video_mp4",
            render,
        )

        url, kind = await volcano_asset_source_media.normalized_source_url(
            {
                "asset_type": "Video",
                "local_source_id": "video-1",
                "user_id": "user-1",
                "public_base_url": "https://lumen.example",
            },
            storage_writes=SimpleNamespace(
                capacity=_Capacity(),
                lease_ttl_seconds=30,
            ),
        )

        async with factory() as observer:
            stored = (
                await observer.execute(select(Video).where(Video.id == "video-1"))
            ).scalar_one()
        variant = stored.metadata_jsonb["volcano_asset_video_variant"]
        assert url.startswith("https://lumen.example/")
        assert kind == VOLCANO_ASSET_VIDEO_KIND
        assert stored.metadata_jsonb["reference_access_token"]
        assert variant["sha256"] == rendered.sha256
        assert (tmp_path / variant["storage_key"]).read_bytes() == rendered_data
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_video_adoption_retries_cas_and_reuses_winner_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant = {
        "kind": VOLCANO_ASSET_VIDEO_KIND,
        "storage_key": "videos/video-1.variant.mp4",
        "mime": "video/mp4",
        "width": 1280,
        "height": 720,
        "duration_ms": 2_000,
        "fps": 30.0,
        "has_audio": False,
        "size_bytes": 10,
        "sha256": "b" * 64,
    }
    snapshot = volcano_asset_source_media._VideoSourceSnapshot(
        id="video-1",
        user_id="user-1",
        storage_key="videos/source.mp4",
        sha256="a" * 64,
        etag="video-etag",
        size_bytes=6,
        metadata_jsonb={VOLCANO_ASSET_VIDEO_METADATA_KEY: variant},
    )
    prepared = volcano_asset_source_media._PreparedVideoVariant(
        variant=variant,
        receipt=None,
        from_snapshot=True,
    )
    current_rows = [
        SimpleNamespace(
            metadata_jsonb={VOLCANO_ASSET_VIDEO_METADATA_KEY: variant},
        ),
        SimpleNamespace(
            metadata_jsonb={
                VOLCANO_ASSET_VIDEO_METADATA_KEY: variant,
                "reference_access_token": "winner-token",
                "reference_access_token_expires_at": (
                    "2099-01-01T00:00:00+00:00"
                ),
            },
        ),
    ]
    sessions_created = [0]
    commits: list[int] = []
    rollbacks: list[int] = []

    class _CasResult:
        def __init__(self, value: Any = None, *, rowcount: int = 0) -> None:
            self.value = value
            self.rowcount = rowcount

        def scalar_one_or_none(self) -> Any:
            return self.value

    class _CasSession:
        def __init__(self, index: int) -> None:
            self.index = index
            self.execute_count = 0

        async def __aenter__(self) -> _CasSession:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def execute(self, _statement: Any) -> _CasResult:
            self.execute_count += 1
            if self.execute_count == 1:
                return _CasResult(snapshot.user_id)
            if self.execute_count == 2:
                return _CasResult(current_rows[self.index])
            return _CasResult(rowcount=self.index)

        async def rollback(self) -> None:
            rollbacks.append(self.index)

        async def commit(self) -> None:
            commits.append(self.index)

    def session_factory() -> _CasSession:
        index = sessions_created[0]
        sessions_created[0] += 1
        return _CasSession(index)

    monkeypatch.setattr(volcano_asset_source_media, "SessionLocal", session_factory)
    state = volcano_asset_source_media._VideoAdoptionState()

    token = await volcano_asset_source_media._adopt_video_variant(
        snapshot,
        prepared,
        state,
    )

    assert token == "winner-token"
    assert sessions_created == [2]
    assert rollbacks == [0]
    assert commits == [1]
    assert state.committed is True


@pytest.mark.asyncio
async def test_video_adoption_failure_cleans_new_content_addressed_install(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"installed-video"
    sha256 = hashlib.sha256(payload).hexdigest()
    storage_key = f"videos/video-1.{VOLCANO_ASSET_VIDEO_KIND}.{sha256}.mp4"
    installed = tmp_path / storage_key
    installed.parent.mkdir(parents=True)
    installed.write_bytes(payload)
    snapshot = volcano_asset_source_media._VideoSourceSnapshot(
        id="video-1",
        user_id="user-1",
        storage_key="videos/source.mp4",
        sha256="a" * 64,
        etag="video-etag",
        size_bytes=6,
        metadata_jsonb={},
    )
    receipt = VolcanoAssetInstallReceipt(
        storage_key=storage_key,
        size_bytes=len(payload),
        sha256=sha256,
    )
    prepared = volcano_asset_source_media._PreparedVideoVariant(
        variant={
            "kind": VOLCANO_ASSET_VIDEO_KIND,
            "storage_key": storage_key,
            "mime": "video/mp4",
            "size_bytes": len(payload),
            "sha256": sha256,
        },
        receipt=receipt,
        from_snapshot=False,
    )

    async def load_snapshot(**_kwargs: Any) -> Any:
        return snapshot

    async def prepare(*_args: Any, **_kwargs: Any) -> Any:
        return prepared

    async def fail_adoption(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("CAS commit failed")

    monkeypatch.setattr(
        volcano_asset_source_media.settings,
        "storage_root",
        str(tmp_path),
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_video_source_snapshot",
        load_snapshot,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_prepare_video_variant",
        prepare,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_adopt_video_variant",
        fail_adoption,
    )

    with pytest.raises(RuntimeError, match="CAS commit failed"):
        await volcano_asset_source_media.normalized_source_url(
            {
                "asset_type": "Video",
                "local_source_id": snapshot.id,
                "user_id": snapshot.user_id,
                "public_base_url": "https://lumen.example",
            },
            storage_writes=SimpleNamespace(
                capacity=object(),
                lease_ttl_seconds=30,
            ),
        )

    for _attempt in range(100):
        if not installed.exists():
            break
        await asyncio.sleep(0.01)
    assert not installed.exists()


@pytest.mark.asyncio
async def test_video_transcode_has_one_total_timeout(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = asyncio.Event()
    snapshot = volcano_asset_source_media._VideoSourceSnapshot(
        id="video-1",
        user_id="user-1",
        storage_key="videos/source.mp4",
        sha256="a" * 64,
        etag="video-etag",
        size_bytes=6,
        metadata_jsonb={},
    )

    async def slow_render(_snapshot: Any) -> VolcanoAssetVideoMp4:
        try:
            await asyncio.sleep(0.02)
            return VolcanoAssetVideoMp4(
                data=b"late",
                width=1280,
                height=720,
                duration_ms=2_000,
                fps=30.0,
                has_audio=False,
                size_bytes=4,
                sha256=hashlib.sha256(b"late").hexdigest(),
            )
        finally:
            done.set()

    monkeypatch.setattr(
        volcano_asset_source_media.settings,
        "storage_root",
        str(tmp_path),
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_render_video",
        slow_render,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_VIDEO_TRANSCODE_TOTAL_TIMEOUT_SECONDS",
        0.001,
    )

    with pytest.raises(VolcanoAssetMediaError) as exc_info:
        await volcano_asset_source_media._transcode_video_to_stage(snapshot)

    assert exc_info.value.code == "volcano_asset_video_transcode_failed"
    await asyncio.wait_for(done.wait(), timeout=1)
    assert list(tmp_path.rglob("*.stage")) == []


@pytest.mark.asyncio
async def test_cancelled_video_stage_install_returns_and_cleans_in_background(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"staged-video"
    sha256 = hashlib.sha256(payload).hexdigest()
    staged = tmp_path / "videos/.video.stage"
    destination = tmp_path / "videos/video.final.mp4"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    started = threading.Event()
    finish = threading.Event()

    def blocking_install(
        target: Any,
        source: Any,
        *,
        size_bytes: int,
        sha256: str,
    ) -> bool:
        assert size_bytes == len(payload)
        assert sha256 == hashlib.sha256(payload).hexdigest()
        os.replace(source, target)
        started.set()
        assert finish.wait(timeout=2)
        return True

    class _Lease:
        def __init__(self) -> None:
            self.release_calls = 0

        async def renew(self) -> bool:
            return True

        async def release(self) -> None:
            self.release_calls += 1

    monkeypatch.setattr(
        volcano_asset_source_media.settings,
        "storage_root",
        str(tmp_path),
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_install_video_stage_atomic",
        blocking_install,
    )
    lease = _Lease()

    async def install_with_lease() -> Any:
        async with maintained_capacity_lease(
            lease,
            ttl_seconds=30,
        ) as guard:
            return await volcano_asset_source_media._install_video_stage(
                storage_key="videos/video.final.mp4",
                staged_path=staged,
                size_bytes=len(payload),
                sha256=sha256,
                guard=guard,
            )

    task = asyncio.create_task(install_with_lease())
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)

    assert destination.exists()
    assert lease.release_calls == 1
    finish.set()
    for _attempt in range(100):
        if not destination.exists():
            break
        await asyncio.sleep(0.01)
    assert not destination.exists()


@pytest.mark.asyncio
async def test_video_prepare_deadline_covers_blocked_final_install(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"prepared-video"
    sha256 = hashlib.sha256(payload).hexdigest()
    staged = tmp_path / "u/user-1/vref/video-1/.video.stage"
    destination_key = (
        f"u/user-1/vref/video-1/video-1.{VOLCANO_ASSET_VIDEO_KIND}."
        f"{sha256}.attempt.mp4"
    )
    destination = tmp_path / destination_key
    staged.parent.mkdir(parents=True)
    staged.write_bytes(payload)
    started = threading.Event()
    finish = threading.Event()

    def blocking_install(
        target: Any,
        source: Any,
        *,
        size_bytes: int,
        sha256: str,
    ) -> bool:
        assert size_bytes == len(payload)
        assert sha256 == hashlib.sha256(payload).hexdigest()
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        started.set()
        assert finish.wait(timeout=2)
        return True

    async def transcode(
        _snapshot: Any,
        _storage_writes: Any,
    ) -> tuple[Any, dict[str, Any]]:
        return staged, {
            "kind": VOLCANO_ASSET_VIDEO_KIND,
            "storage_key": destination_key,
            "mime": "video/mp4",
            "width": 1280,
            "height": 720,
            "duration_ms": 2_000,
            "fps": 30.0,
            "has_audio": False,
            "size_bytes": len(payload),
            "sha256": sha256,
        }

    class _Lease:
        def __init__(self) -> None:
            self.release_calls = 0

        async def renew(self) -> bool:
            return True

        async def release(self) -> None:
            self.release_calls += 1

    class _Capacity:
        def __init__(self) -> None:
            self.lease = _Lease()

        async def reserve(self, _size_bytes: int) -> _Lease:
            return self.lease

    monkeypatch.setattr(
        volcano_asset_source_media.settings,
        "storage_root",
        str(tmp_path),
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_transcode_video_to_stage",
        transcode,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_install_video_stage_atomic",
        blocking_install,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_VIDEO_PREPARE_TOTAL_TIMEOUT_SECONDS",
        0.02,
    )
    capacity = _Capacity()
    snapshot = volcano_asset_source_media._VideoSourceSnapshot(
        id="video-1",
        user_id="user-1",
        storage_key="u/user-1/vref/video-1/source.mp4",
        sha256="a" * 64,
        etag="video-etag",
        size_bytes=6,
        metadata_jsonb={},
    )

    with pytest.raises(VolcanoAssetMediaError) as exc_info:
        await asyncio.wait_for(
            volcano_asset_source_media._prepare_video_variant(
                snapshot,
                SimpleNamespace(
                    capacity=capacity,
                    lease_ttl_seconds=30,
                ),
            ),
            timeout=0.2,
        )

    assert exc_info.value.code == "volcano_asset_video_prepare_timeout"
    assert started.is_set()
    assert capacity.lease.release_calls == 1
    assert destination.exists()

    finish.set()
    for _attempt in range(100):
        if not destination.exists():
            break
        await asyncio.sleep(0.01)
    assert not destination.exists()


@pytest.mark.asyncio
async def test_video_commit_cancellation_marks_outcome_ambiguous() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    state = volcano_asset_source_media._VideoAdoptionState()

    class _CommitSession:
        async def commit(self) -> None:
            started.set()
            await release.wait()

    task = asyncio.create_task(
        volcano_asset_source_media._commit_video_adoption(
            _CommitSession(),
            state,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.committed is False
    assert state.commit_started is True
    assert state.cleanup_safe is False


@pytest.mark.asyncio
async def test_video_adoption_lock_wait_has_bounded_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    class _BlockedSession:
        async def __aenter__(self) -> _BlockedSession:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def execute(self, _statement: Any) -> Any:
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(
        volcano_asset_source_media,
        "SessionLocal",
        _BlockedSession,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "_VIDEO_ADOPTION_TOTAL_TIMEOUT_SECONDS",
        0.01,
    )
    snapshot = volcano_asset_source_media._VideoSourceSnapshot(
        id="video-1",
        user_id="user-1",
        storage_key="videos/source.mp4",
        sha256="a" * 64,
        etag="video-etag",
        size_bytes=6,
        metadata_jsonb={},
    )
    prepared = volcano_asset_source_media._PreparedVideoVariant(
        variant={
            "kind": VOLCANO_ASSET_VIDEO_KIND,
            "storage_key": "videos/variant.mp4",
            "mime": "video/mp4",
            "size_bytes": 10,
            "sha256": "b" * 64,
        },
        receipt=None,
        from_snapshot=False,
    )
    state = volcano_asset_source_media._VideoAdoptionState()

    with pytest.raises(VolcanoAssetMediaError) as exc_info:
        await volcano_asset_source_media._adopt_video_variant_once(
            snapshot,
            prepared,
            state,
        )

    assert started.is_set()
    assert exc_info.value.code == "video_reference_database_timeout"
    assert state.commit_started is False
    assert state.cleanup_safe is True


@pytest.mark.asyncio
async def test_video_commit_error_probes_durable_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = volcano_asset_source_media._VideoSourceSnapshot(
        id="video-1",
        user_id="user-1",
        storage_key="videos/source.mp4",
        sha256="a" * 64,
        etag="video-etag",
        size_bytes=6,
        metadata_jsonb={},
    )
    variant = {
        "kind": VOLCANO_ASSET_VIDEO_KIND,
        "storage_key": "videos/video-1.attempt.mp4",
        "mime": "video/mp4",
        "size_bytes": 10,
        "sha256": "b" * 64,
    }
    prepared = volcano_asset_source_media._PreparedVideoVariant(
        variant=variant,
        receipt=None,
        from_snapshot=False,
    )
    current = SimpleNamespace(
        id=snapshot.id,
        user_id=snapshot.user_id,
        storage_key=snapshot.storage_key,
        sha256=snapshot.sha256,
        etag=snapshot.etag,
        size_bytes=snapshot.size_bytes,
        deleted_at=None,
        metadata_jsonb={},
    )
    state = volcano_asset_source_media._VideoAdoptionState()
    sessions = [0]

    class _Result:
        def __init__(
            self,
            value: Any = None,
            *,
            rowcount: int = 0,
            rows: list[Any] | None = None,
        ) -> None:
            self.value = value
            self.rowcount = rowcount
            self.rows = [] if rows is None else rows

        def scalar_one_or_none(self) -> Any:
            return self.value

        def scalars(self) -> Any:
            return SimpleNamespace(all=lambda: self.rows)

    class _CommitSession:
        def __init__(self, index: int) -> None:
            self.index = index
            self.execute_count = 0

        async def __aenter__(self) -> _CommitSession:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def execute(self, _statement: Any) -> _Result:
            self.execute_count += 1
            if self.index == 1:
                return _Result(current)
            if self.execute_count == 1:
                return _Result(snapshot.user_id)
            if self.execute_count == 2:
                return _Result(current)
            if self.execute_count == 3:
                return _Result(rows=[])
            return _Result(rowcount=1)

        async def rollback(self) -> None:
            return None

        async def commit(self) -> None:
            current.metadata_jsonb = {
                VOLCANO_ASSET_VIDEO_METADATA_KEY: variant,
                "reference_access_token": state.reference_token,
                "reference_access_token_expires_at": (
                    "2099-01-01T00:00:00+00:00"
                ),
            }
            raise RuntimeError("commit response lost")

    def session_factory() -> _CommitSession:
        index = sessions[0]
        sessions[0] += 1
        return _CommitSession(index)

    monkeypatch.setattr(
        volcano_asset_source_media,
        "SessionLocal",
        session_factory,
    )

    token = await volcano_asset_source_media._adopt_video_variant_once(
        snapshot,
        prepared,
        state,
    )

    assert token == state.reference_token
    assert sessions == [2]
    assert state.committed is True
