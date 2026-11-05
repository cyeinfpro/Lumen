from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request, UploadFile
from PIL import Image as PILImage
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.images.application import gif_routes as gif
from app.images.application import gif_import, gif_support, image_access
from app.images.application import http_routes as images
from app.images.application.upload import UploadPolicy
from app.images.processing.isolated import IsolatedImageProcessingExecutor
from app.images.processing.service import ImageProcessor, ProcessingError
from lumen_core.gif_editing import inspect_gif, retime_gif


def animation():
    out = io.BytesIO()
    PILImage.new("RGB", (8, 6), "red").save(
        out,
        "GIF",
        save_all=True,
        append_images=[PILImage.new("RGB", (8, 6), "blue")],
        duration=[100, 300],
        loop=2,
    )
    return out.getvalue()


def request():
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def image(**kw):
    data = animation()
    values = dict(
        id="source",
        user_id="owner",
        mime="image/gif",
        storage_key="source.gif",
        sha256=hashlib.sha256(data).hexdigest(),
        deleted_at=None,
        artifact_status="ready",
        metadata_jsonb={},
        created_at=None,
    )
    values.update(kw)
    return SimpleNamespace(**values)


class DB:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.statements = []
        self.committed = False

    async def execute(self, statement):
        self.statements.append(statement)
        value = next(self.rows)
        return SimpleNamespace(scalar_one_or_none=lambda: value)

    async def commit(self):
        self.committed = True


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(gif, "check_upload_rate_limit", AsyncMock())
    monkeypatch.setattr(gif_import, "check_upload_rate_limit", AsyncMock())
    monkeypatch.setattr(gif, "_check_gif_read_rate_limit", AsyncMock())
    visibility = AsyncMock()
    for module, name in [
        (images, "_ensure_image_visible_to_user"),
        (image_access, "ensure_image_visible_to_user"),
        (gif, "ensure_image_visible_to_user"),
        (gif_import, "ensure_image_visible_to_user"),
    ]:
        monkeypatch.setattr(module, name, visibility)
    for module in (gif, gif_import):
        monkeypatch.setattr(
            module, "image_out", AsyncMock(side_effect=lambda db, row: row)
        )
    monkeypatch.setattr(gif, "check_active_user", AsyncMock())
    monkeypatch.setattr(gif, "lock_user_key", AsyncMock())
    monkeypatch.setattr(gif_import, "check_active_user", AsyncMock())
    monkeypatch.setattr(gif_import, "lock_user_key", AsyncMock())


@pytest.mark.asyncio
async def test_owner_query_and_visibility_are_required(setup):
    db = DB([image()])
    await gif._owner_image(db, SimpleNamespace(id="owner"), "source")
    sql = str(
        db.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "user_id = 'owner'" in sql
    assert "deleted_at IS NULL" in sql
    assert "artifact_status = 'ready'" in sql
    images._ensure_image_visible_to_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_hidden_or_foreign_source_cannot_decode(setup):
    with pytest.raises(HTTPException) as error:
        await gif._owner_image(DB([None]), SimpleNamespace(id="intruder"), "source")
    assert error.value.status_code == 404
    images._ensure_image_visible_to_user.side_effect = images._http(
        "not_found", "hidden", 404
    )
    with pytest.raises(HTTPException):
        await gif._owner_image(DB([image()]), SimpleNamespace(id="owner"), "source")


@pytest.mark.asyncio
async def test_non_gif_source_rejected(setup):
    with pytest.raises(HTTPException) as error:
        await gif._owner_image(
            DB([image(mime="image/png")]), SimpleNamespace(id="owner"), "source"
        )
    assert error.value.status_code == 400


def test_source_integrity_and_size(tmp_path, monkeypatch):
    path = tmp_path / "source.gif"
    path.write_bytes(animation())
    monkeypatch.setattr(gif_support, "storage_path", lambda key, **kwargs: path)
    assert gif.read_gif_source(image()) == animation()
    with pytest.raises(ValueError, match="integrity"):
        gif.read_gif_source(image(sha256="wrong"))
    monkeypatch.setattr(gif_support, "MAX_GIF_BYTES", 10)
    with pytest.raises(ValueError, match="32 MiB"):
        gif.read_gif_source(image())


@pytest.mark.parametrize("speed", [0, -1, 0.09, 10.01, float("inf"), float("nan")])
def test_speed_validation(speed):
    with pytest.raises(ValidationError):
        gif.RetimeRequest(speed=speed)


@pytest.mark.parametrize("key", [None, "", "short", "x" * 129, "abcde/fg", "abcde fg"])
def test_idempotency_key_validation(key):
    with pytest.raises(HTTPException):
        gif.validate_operation_key(key)


@pytest.mark.asyncio
async def test_save_new_version_and_replay_without_second_transform(setup, monkeypatch):
    source = image()
    saved = image(id="derived")
    captured = {}

    async def execute(**kwargs):
        captured.update(kwargs)
        saved.metadata_jsonb = kwargs["policy"].initial_metadata
        saved.parent_image_id = kwargs["policy"].parent_image_id
        assert inspect_gif(await kwargs["upload_file"].read())["durations_ms"] == [
            50,
            150,
        ]
        return saved

    service = SimpleNamespace(execute=execute)
    run = AsyncMock(return_value=retime_gif(animation(), 2))
    monkeypatch.setattr(gif, "_run_gif", run)
    user = SimpleNamespace(id="owner")
    params = dict(
        user=user,
        request=request(),
        service=service,
        image_id="source",
        operation="retime",
        key="test-key-123",
        speed=2.0,
    )
    db = DB([source, None, source])
    assert await gif._save_edit(db=db, **params) is saved
    assert saved.parent_image_id == "source"
    assert source.metadata_jsonb == {}
    assert db.committed
    assert captured["policy"].allowed_mime == {"image/gif"}
    db = DB([source, saved])
    assert await gif._save_edit(db=db, **params) is saved
    assert run.await_count == 1
    with pytest.raises(HTTPException) as err:
        await gif._save_edit(db=DB([source, saved]), **{**params, "speed": 3.0})
    assert err.value.status_code == 409
    saved.artifact_status = "publishing"
    with pytest.raises(HTTPException) as err:
        await gif._save_edit(db=DB([source, saved]), **params)
    assert err.value.status_code == 409


@pytest.mark.asyncio
async def test_extract_response_is_private_png(setup, monkeypatch):
    monkeypatch.setattr(gif, "_run_gif", AsyncMock(return_value=b"png"))
    response = await gif.extract_image_gif_frame(
        "source",
        1,
        SimpleNamespace(id="owner"),
        DB([image()]),
        object(),
    )
    assert response.media_type == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_replacement_fake_png_rejected_before_transform(setup):
    file = UploadFile(file=io.BytesIO(b"JPEG"), filename="fake.png")
    with pytest.raises(HTTPException) as err:
        await gif.replace_image_gif_frame(
            "source",
            0,
            request(),
            SimpleNamespace(id="owner"),
            DB([image()]),
            object(),
            file,
            "test-key-123",
        )
    assert err.value.status_code == 400
    assert file.file.closed


@pytest.mark.asyncio
async def test_import_preserves_bytes_and_replays(setup):
    saved = image(id="imported")
    calls = []

    async def execute(**kwargs):
        calls.append(kwargs)
        assert await kwargs["upload_file"].read() == animation()
        saved.metadata_jsonb = kwargs["policy"].initial_metadata
        return saved

    service = SimpleNamespace(execute=execute)
    user = SimpleNamespace(id="owner")
    for prior in [None, saved]:
        file = UploadFile(file=io.BytesIO(animation()), filename="input.gif")
        assert (
            await gif_import.import_gif(
                request(), user, DB([prior]), service, file, "import-key-1"
            )
            is saved
        )
    assert len(calls) == 1


def test_static_upload_still_rejects_gif(tmp_path):
    path = tmp_path / "image.gif"
    path.write_bytes(animation())
    with pytest.raises(ProcessingError) as exc:
        ImageProcessor().inspect(
            path,
            upload_bytes=path.stat().st_size,
            allowed_mime={"image/png", "image/jpeg", "image/webp"},
            normalizable_mime=set(),
            max_pixels=64000000,
            max_long_side=4096,
        )
    assert exc.value.code == "unsupported_mime"


def test_explicit_gif_inspection_uses_total_decoded_budget(tmp_path):
    path = tmp_path / "image.gif"
    path.write_bytes(animation())
    result = ImageProcessor().inspect(
        path,
        upload_bytes=path.stat().st_size,
        allowed_mime={"image/gif"},
        normalizable_mime=set(),
        max_pixels=64000000,
        max_long_side=4096,
    )
    assert result.estimate.pixels == 8 * 6 * 2
    assert result.output_mime == "image/gif"


@pytest.mark.asyncio
async def test_real_isolated_executor_retimes_and_reports_invalid_input():
    executor = IsolatedImageProcessingExecutor(result_timeout_seconds=15)
    try:
        data = await executor.gif(animation(), "retime", speed=2)
        assert inspect_gif(data)["durations_ms"] == [50, 150]
        png = await executor.gif(animation(), "extract", frame_index=1)
        assert png.startswith(b"\x89PNG")
        with pytest.raises(ProcessingError) as exc:
            await executor.gif(b"not gif", "inspect")
        assert exc.value.status_code == 400
    finally:
        await executor.aclose()


@pytest.mark.asyncio
async def test_import_saga_keeps_original_bytes_and_durable_marker(tmp_path):
    from .test_artifact_saga import _Repository, _Capacity
    from app.images.adapters.filesystem_store import FileSystemArtifactStore
    from app.images.application.upload import UploadCommandService

    class RecordingRepository(_Repository):
        async def create_staging(self, row):
            self.initial_marker = dict(row.metadata_jsonb)
            self.initial_parent = row.parent_image_id
            return await super().create_staging(row)

    repo = RecordingRepository()
    executor = IsolatedImageProcessingExecutor(result_timeout_seconds=20)
    service = UploadCommandService(
        artifacts=FileSystemArtifactStore(str(tmp_path)),
        capacity=_Capacity(),
        storage_capacity=_Capacity(),
        repository=repo,
        processing_executor=executor,
    )
    marker = {"local_gif_edit": {"key": "durable", "fingerprint": "exact"}}
    policy = UploadPolicy(
        allowed_mime={"image/gif"},
        normalizable_mime=set(),
        extensions={"image/gif": "gif"},
        max_bytes=32 * 1024 * 1024,
        max_pixels=64000000,
        max_long_side=64000000,
        mask_requested=False,
        reference_size=None,
        parent_image_id="source",
        initial_metadata=marker,
    )
    original = animation()
    file = UploadFile(file=io.BytesIO(original), filename="input.gif")
    try:
        saved = await service.execute(
            user_id="owner",
            upload_file=file,
            filename="input.gif",
            policy=policy,
            metadata_finalizer=lambda _id, _ext, meta: meta.update(marker),
        )
        assert repo.initial_marker == marker
        assert repo.initial_parent == "source"
        assert saved.artifact_status == "ready"
        assert saved.parent_image_id == "source"
        assert saved.metadata_jsonb["local_gif_edit"] == marker["local_gif_edit"]
        assert (tmp_path / saved.storage_key).read_bytes() == original
        assert inspect_gif((tmp_path / saved.storage_key).read_bytes())["loop"] == 2
    finally:
        await file.close()
        await service.aclose()


def test_mutations_require_csrf_and_all_routes_are_registered():
    routes = {route.path: route for route in images.router.routes}
    for path in [
        "/{image_id}/gif/retime",
        "/{image_id}/gif/frames/{frame_index}",
        "/gif/upload",
    ]:
        route = next(
            item
            for item in images.router.routes
            if item.path == path and "POST" in item.methods
        )
        assert any(
            dep.call is images.verify_csrf for dep in route.dependant.dependencies
        )
    assert "/gif/history" in routes


@pytest.mark.asyncio
async def test_history_owner_filter_and_hidden_items(setup, monkeypatch):
    hidden, visible = image(id="hidden"), image(id="visible")

    class HistoryDB:
        async def execute(self, statement):
            sql = str(
                statement.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
            assert "user_id = 'owner'" in sql and "LIMIT 100" in sql
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [hidden, visible])
            )

    async def visibility(db, row, user):
        if row is hidden:
            raise images._http("not_found", "hidden", 404)

    monkeypatch.setattr(gif_import, "ensure_image_visible_to_user", visibility)
    assert await gif_import.gif_history(SimpleNamespace(id="owner"), HistoryDB()) == [
        visible
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,status",
    [
        ("image_processing_timeout", 503),
        ("image_processing_unavailable", 503),
        ("invalid_gif", 400),
    ],
)
async def test_processing_error_status_is_not_lost(monkeypatch, code, status):
    from .test_artifact_saga import _Capacity

    monkeypatch.setattr(gif, "read_gif_source", lambda row: animation())
    service = SimpleNamespace(
        capacity=_Capacity(),
        processing_executor=SimpleNamespace(
            gif=AsyncMock(side_effect=ProcessingError(code, "test error", status)),
        ),
    )
    with pytest.raises(HTTPException) as error:
        await gif._run_gif(service, image(), "inspect")
    assert error.value.status_code == status
    assert error.value.detail["error"]["code"] == code


@pytest.mark.asyncio
async def test_capacity_rejection_prevents_source_read(monkeypatch):
    from app.images.adapters.local_capacity import CapacityExceeded

    def read(row):
        pytest.fail("must reserve before reading source")

    monkeypatch.setattr(gif, "read_gif_source", read)
    service = SimpleNamespace(
        capacity=SimpleNamespace(
            reserve=AsyncMock(side_effect=CapacityExceeded("busy")),
        )
    )
    with pytest.raises(HTTPException) as error:
        await gif._run_gif(service, image(), "inspect")
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_upload_hash_is_streamed_and_rewound():
    class TrackedUpload(UploadFile):
        async def read(self, size=-1):
            assert 0 < size <= 256 * 1024
            return await super().read(size)

    data = b"x" * (600 * 1024)
    file = TrackedUpload(file=io.BytesIO(data), filename="input.gif")
    assert await gif.upload_digest(file) == hashlib.sha256(data).hexdigest()
    assert file.file.tell() == 0
    await file.close()


@pytest.mark.asyncio
async def test_upload_hash_enforces_byte_limit(monkeypatch):
    monkeypatch.setattr(gif_support, "MAX_GIF_BYTES", 10)
    file = UploadFile(file=io.BytesIO(b"x" * 11), filename="input.gif")
    try:
        with pytest.raises(HTTPException) as error:
            await gif.upload_digest(file)
        assert error.value.status_code == 413
    finally:
        await file.close()


@pytest.mark.asyncio
async def test_read_limit_has_its_own_bucket_and_frame_navigation_burst(monkeypatch):
    redis = object()
    check = AsyncMock()
    monkeypatch.setattr(gif, "get_redis", lambda: redis)
    monkeypatch.setattr(gif.GIF_READ_LIMITER, "check", check)
    assert gif.GIF_READ_LIMITER.always_on
    assert gif.GIF_READ_LIMITER.initial_tokens == 16
    await gif._check_gif_read_rate_limit("owner")
    check.assert_awaited_once_with(redis, "rl:gif-read:owner")
