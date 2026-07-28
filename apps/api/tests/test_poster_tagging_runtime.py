from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as PILImage

from app.services.poster_styles import tagging
from app.services.poster_styles.capacity import RedisCapacityLease
from app.services.poster_styles.tagging import (
    POSTER_TAGGING_PREVIEW_MAX_BYTES,
    POSTER_TAGGING_PREVIEW_MAX_SIDE,
    POSTER_TAGGING_REQUEST_MAX_BYTES,
    PosterTaggingPreviewError,
    PosterTaggingRequestTooLarge,
    call_tagging_upstream,
    checked_tagging_request_body,
    load_tagging_preview,
)
from app.services.poster_styles.tagging_runtime import PosterTaggingHttpClientPool


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalar_one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None


class _Db:
    def __init__(self, batches: list[list[Any]]) -> None:
        self.batches = list(batches)

    async def execute(self, _statement: Any) -> _Result:
        return _Result(self.batches.pop(0))


def _async_value(value: Any) -> Any:
    async def getter(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return getter


def _webp_bytes(size: tuple[int, int], *, quality: int = 86) -> bytes:
    output = io.BytesIO()
    with PILImage.new("RGB", size, (180, 40, 80)) as image:
        image.save(output, format="WEBP", quality=quality)
    return output.getvalue()


@pytest.mark.asyncio
async def test_tagging_preview_prefers_existing_preview1024(
    tmp_path: Path,
) -> None:
    preview_path = tmp_path / "preview.webp"
    preview_path.write_bytes(_webp_bytes((1024, 512)))
    image = SimpleNamespace(
        id="image-1",
        user_id="user-1",
        storage_key="original.png",
        mime="image/png",
    )
    variant = SimpleNamespace(
        image_id="image-1",
        kind="preview1024",
        storage_key="preview.webp",
        width=1024,
        height=512,
    )

    def storage_path(key: str) -> Path:
        if key == "original.png":
            raise AssertionError("original must not be read when preview1024 exists")
        return tmp_path / key

    runtime = SimpleNamespace(
        Image=SimpleNamespace(
            id=object(),
            user_id=object(),
            deleted_at=SimpleNamespace(is_=lambda _value: object()),
        ),
        ImageVariant=SimpleNamespace(
            image_id=object(),
            kind=object(),
        ),
        _storage_path=storage_path,
        logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
    )

    preview = await load_tagging_preview(
        runtime,
        _Db([[image], [variant]]),
        image_id="image-1",
        user_id="user-1",
    )

    assert preview is not None
    assert preview.source_kind == "preview1024"
    assert preview.width == 1024
    assert preview.height == 512
    assert len(preview.content) <= POSTER_TAGGING_PREVIEW_MAX_BYTES


@pytest.mark.asyncio
async def test_tagging_preview_fallback_is_oriented_and_bounded(
    tmp_path: Path,
) -> None:
    original_path = tmp_path / "original.jpg"
    with PILImage.new("RGB", (3000, 1000), (20, 120, 210)) as image:
        exif = image.getexif()
        exif[274] = 6
        image.save(original_path, format="JPEG", quality=92, exif=exif)
    image = SimpleNamespace(
        id="image-1",
        user_id="user-1",
        storage_key="original.jpg",
        mime="image/jpeg",
    )
    runtime = SimpleNamespace(
        Image=SimpleNamespace(
            id=object(),
            user_id=object(),
            deleted_at=SimpleNamespace(is_=lambda _value: object()),
        ),
        ImageVariant=SimpleNamespace(
            image_id=object(),
            kind=object(),
        ),
        _storage_path=lambda key: tmp_path / key,
        logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
    )

    preview = await load_tagging_preview(
        runtime,
        _Db([[image], []]),
        image_id="image-1",
        user_id="user-1",
    )

    assert preview is not None
    assert preview.source_kind == "bounded_preview"
    assert max(preview.width, preview.height) <= POSTER_TAGGING_PREVIEW_MAX_SIDE
    assert preview.height > preview.width
    assert len(preview.content) <= POSTER_TAGGING_PREVIEW_MAX_BYTES
    assert preview.content != original_path.read_bytes()


def test_oversized_source_is_rejected_before_decode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "oversized.fake"
    source_path.write_bytes(b"header")

    class OversizedImage:
        size = (10_000, 10_000)

        def __enter__(self) -> "OversizedImage":
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        def draft(self, *_args: Any) -> None:
            return None

        def load(self) -> None:
            raise AssertionError("oversized source must be rejected before decode")

    monkeypatch.setattr(tagging.PILImage, "open", lambda _path: OversizedImage())

    with pytest.raises(PosterTaggingPreviewError, match="pixel limit"):
        tagging._load_preview_file(  # noqa: SLF001
            source_path,
            source_kind="bounded_preview",
        )


@pytest.mark.asyncio
async def test_provider_receives_bounded_preview_not_original(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    preview_path = tmp_path / "preview.webp"
    preview_path.write_bytes(_webp_bytes((1024, 512)))
    image = SimpleNamespace(
        id="image-1",
        user_id="user-1",
        storage_key="original.png",
        mime="image/png",
    )
    variant = SimpleNamespace(
        image_id="image-1",
        kind="preview1024",
        storage_key="preview.webp",
    )
    provider = SimpleNamespace(name="provider-1")
    captured: dict[str, Any] = {}

    def storage_path(key: str) -> Path:
        if key == "original.png":
            raise AssertionError("provider path must not read original")
        return tmp_path / key

    runtime = SimpleNamespace(
        _storage_path=storage_path,
        logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
        get_spec=lambda _key: object(),
        get_setting=_async_value([{"name": "provider-1"}]),
    )

    monkeypatch.setattr(
        tagging,
        "build_effective_provider_config",
        lambda **_kwargs: ([provider], [], []),
    )
    monkeypatch.setattr(
        tagging,
        "endpoint_kind_allowed",
        lambda _provider, _kind: True,
    )
    monkeypatch.setattr(
        tagging,
        "weighted_priority_order",
        lambda providers, _state: list(providers),
    )

    async def request_provider(
        _tagging_runtime: Any,
        _provider: Any,
        *,
        request_body: dict[str, Any],
    ) -> tuple[str, None]:
        captured["body"] = request_body
        return '{"style_tags":["扁平"],"category":"illustration"}', None

    monkeypatch.setattr(tagging, "_request_provider", request_provider)

    result = await call_tagging_upstream(
        runtime,
        _Db([[image], [variant]]),
        image_id="image-1",
        user_id="user-1",
        tagging_runtime=SimpleNamespace(),
    )

    request_body = captured["body"]
    image_url = request_body["input"][0]["content"][1]["image_url"]
    encoded = json.dumps(
        request_body,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    preview_bytes = base64.b64decode(image_url.split(",", 1)[1])
    with PILImage.open(io.BytesIO(preview_bytes)) as preview:
        assert max(preview.size) <= POSTER_TAGGING_PREVIEW_MAX_SIDE
    assert len(preview_bytes) <= POSTER_TAGGING_PREVIEW_MAX_BYTES
    assert len(encoded) <= POSTER_TAGGING_REQUEST_MAX_BYTES
    assert result["style_tags"] == ["扁平"]


def test_checked_request_body_rejects_final_serialized_bytes() -> None:
    with pytest.raises(PosterTaggingRequestTooLarge):
        checked_tagging_request_body(
            image_id="image-1",
            image_url="data:image/webp;base64," + ("A" * 1024),
            instructions="tag",
            max_bytes=128,
        )


@pytest.mark.asyncio
async def test_http_client_pool_reuses_client() -> None:
    created: list[Any] = []

    class Client:
        async def aclose(self) -> None:
            return None

    def factory(**_kwargs: Any) -> Client:
        client = Client()
        created.append(client)
        return client

    pool = PosterTaggingHttpClientPool(client_factory=factory)
    first = await pool.client_for(None)
    second = await pool.client_for(None)
    await pool.aclose()

    assert first is second
    assert len(created) == 1


@pytest.mark.asyncio
async def test_redis_capacity_is_shared_across_runtime_instances() -> None:
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
            del ex
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

        async def eval(self, _script: str, _keys: int, key: str, token: str) -> int:
            if self.values.get(key) != token:
                return 0
            self.values.pop(key, None)
            return 1

    redis = Redis()
    first = RedisCapacityLease(redis, limit=1, ttl_seconds=30)
    second = RedisCapacityLease(redis, limit=1, ttl_seconds=30)
    lease = await first.try_acquire(owner_token="owner-1")

    assert lease is not None
    assert await second.try_acquire(owner_token="owner-2") is None
    await lease.release()
    assert await second.try_acquire(owner_token="owner-2") is not None
