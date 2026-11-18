from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
from types import SimpleNamespace
from typing import Any

from PIL import Image as PILImage
import pytest

from app.upstream_parts import reference_images
from app.upstream_parts.upstream_impl import (
    build_image_upstream_runtime,
    close_client,
)


TEST_UPSTREAM_RUNTIME = build_image_upstream_runtime()
TEST_UPSTREAM_SERVICES = TEST_UPSTREAM_RUNTIME.services


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.expirations: dict[str, int] = {}

    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    async def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        mapping: dict[str, str] | None = None,
    ) -> int:
        bucket = self.hashes.setdefault(key, {})
        if mapping is not None:
            bucket.update(mapping)
            return len(mapping)
        if field is None:
            raise TypeError("field required when mapping is not provided")
        bucket[field] = value or ""
        return 1

    async def hdel(self, key: str, *fields: str) -> int:
        bucket = self.hashes.get(key, {})
        removed = 0
        for field in fields:
            if field in bucket:
                del bucket[field]
                removed += 1
        return removed

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        bucket = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in bucket:
                added += 1
            bucket[member] = float(score)
        return added

    async def zrem(self, key: str, *members: str) -> int:
        bucket = self.zsets.get(key, {})
        removed = 0
        for member in members:
            if member in bucket:
                del bucket[member]
                removed += 1
        return removed

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def zrange(
        self,
        key: str,
        start: int,
        stop: int,
        withscores: bool = False,
    ) -> list[Any]:
        items = sorted(
            self.zsets.get(key, {}).items(),
            key=lambda item: (item[1], item[0]),
        )
        selected = items[start:] if stop == -1 else items[start : stop + 1]
        if withscores:
            return selected
        return [member for member, _score in selected]

    async def expire(self, key: str, ttl: int) -> bool:
        self.expirations[key] = ttl
        return True


class _FakePool:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis

    def get_redis(self) -> _FakeRedis:
        return self._redis


@pytest.mark.asyncio
async def test_reference_cache_miss_uploads_and_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()
    upload_calls: list[tuple[bytes, str, str, str]] = []

    async def fake_get_pool() -> _FakePool:
        return _FakePool(redis)

    async def fake_push_reference(
        raw: bytes,
        mime: str,
        *,
        base_url: str,
        api_key: str,
    ) -> str | None:
        upload_calls.append((raw, mime, base_url, api_key))
        return "https://refs.example/uploaded.webp"

    async def fake_live(_url: str) -> bool:
        return True

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.provider_pool, "get_pool", fake_get_pool
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.references, "reference_url_is_live", fake_live
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.references,
        "push_reference_to_image_job",
        fake_push_reference,
    )

    ref = b"reference-bytes-miss"
    user_id = "user-1"
    digest = hashlib.sha256(ref).hexdigest()

    result = await TEST_UPSTREAM_SERVICES.references.get_or_upload_reference(
        ref,
        "image/webp",
        base_url="https://sidecar.example",
        api_key="sk-test",
        user_id=user_id,
    )

    cache_key, lru_key = TEST_UPSTREAM_SERVICES.references.reference_cache_keys(user_id)
    assert result == "https://refs.example/uploaded.webp"
    assert upload_calls == [(ref, "image/webp", "https://sidecar.example", "sk-test")]
    assert await redis.zcard(lru_key) == 1

    stored = await redis.hget(cache_key, digest)
    assert stored is not None
    item = json.loads(stored)
    assert item["upload_url"] == "https://refs.example/uploaded.webp"
    assert item["size"] == len(ref)


@pytest.mark.asyncio
async def test_reference_cache_hit_skips_upload_when_head_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()
    upload_calls: list[bytes] = []

    async def fake_get_pool() -> _FakePool:
        return _FakePool(redis)

    async def fake_push_reference(
        raw: bytes,
        mime: str,
        *,
        base_url: str,
        api_key: str,
    ) -> str | None:
        upload_calls.append(raw)
        return "https://refs.example/should-not-be-used.webp"

    async def fake_live(_url: str) -> bool:
        return True

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.provider_pool, "get_pool", fake_get_pool
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.references, "reference_url_is_live", fake_live
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.references,
        "push_reference_to_image_job",
        fake_push_reference,
    )

    ref = b"reference-bytes-hit"
    user_id = "user-2"
    digest = hashlib.sha256(ref).hexdigest()
    cached_url = "https://refs.example/cached.webp"
    await TEST_UPSTREAM_SERVICES.references.reference_cache_store(
        redis,
        user_id=user_id,
        digest=digest,
        url=cached_url,
        size=len(ref),
    )

    result = await TEST_UPSTREAM_SERVICES.references.get_or_upload_reference(
        ref,
        "image/webp",
        base_url="https://sidecar.example",
        api_key="sk-test",
        user_id=user_id,
    )

    cache_key, _lru_key = TEST_UPSTREAM_SERVICES.references.reference_cache_keys(user_id)
    assert result == cached_url
    assert upload_calls == []
    assert (
        json.loads(await redis.hget(cache_key, digest) or "{}")["upload_url"]
        == cached_url
    )


@pytest.mark.asyncio
async def test_reference_cache_head_failure_invalidates_and_reuploads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()
    upload_calls: list[bytes] = []

    async def fake_get_pool() -> _FakePool:
        return _FakePool(redis)

    async def fake_push_reference(
        raw: bytes,
        mime: str,
        *,
        base_url: str,
        api_key: str,
    ) -> str | None:
        upload_calls.append(raw)
        return "https://refs.example/reuploaded.webp"

    async def fake_live(_url: str) -> bool:
        return False

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.provider_pool, "get_pool", fake_get_pool
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.references, "reference_url_is_live", fake_live
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.references,
        "push_reference_to_image_job",
        fake_push_reference,
    )

    ref = b"reference-bytes-stale"
    user_id = "user-3"
    digest = hashlib.sha256(ref).hexdigest()
    stale_url = "https://refs.example/stale.webp"
    now = 1_000.0
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.time,
        "time",
        lambda: now,
    )
    await TEST_UPSTREAM_SERVICES.references.reference_cache_store(
        redis,
        user_id=user_id,
        digest=digest,
        url=stale_url,
        size=len(ref),
    )

    now = 1_065.0
    result = await TEST_UPSTREAM_SERVICES.references.get_or_upload_reference(
        ref,
        "image/webp",
        base_url="https://sidecar.example",
        api_key="sk-test",
        user_id=user_id,
    )

    cache_key, _lru_key = TEST_UPSTREAM_SERVICES.references.reference_cache_keys(user_id)
    assert result == "https://refs.example/reuploaded.webp"
    assert upload_calls == [ref]
    item = json.loads(await redis.hget(cache_key, digest) or "{}")
    assert item["upload_url"] == "https://refs.example/reuploaded.webp"
    assert item["upload_url"] != stale_url


@pytest.mark.asyncio
async def test_reference_cache_lru_trims_to_ten_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()
    user_id = "user-4"
    current = 1_000.0

    def fake_time() -> float:
        nonlocal current
        current += 1.0
        return current

    monkeypatch.setattr(TEST_UPSTREAM_SERVICES.infrastructure.time, "time", fake_time)

    digests: list[str] = []
    for idx in range(11):
        ref = f"reference-{idx}".encode("utf-8")
        digest = hashlib.sha256(ref).hexdigest()
        digests.append(digest)
        await TEST_UPSTREAM_SERVICES.references.reference_cache_store(
            redis,
            user_id=user_id,
            digest=digest,
            url=f"https://refs.example/{idx}.webp",
            size=len(ref),
        )

    cache_key, lru_key = TEST_UPSTREAM_SERVICES.references.reference_cache_keys(user_id)
    assert await redis.zcard(lru_key) == 10
    assert len(redis.hashes[cache_key]) == 10
    assert digests[0] not in redis.hashes[cache_key]
    assert digests[-1] in redis.hashes[cache_key]


@pytest.mark.asyncio
async def test_reference_cache_fresh_hit_skips_head_within_window_and_reprobes_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()
    head_calls: list[str] = []
    upload_calls: list[bytes] = []
    now = 1_000.0

    async def fake_get_pool() -> _FakePool:
        return _FakePool(redis)

    async def fake_live(url: str) -> bool:
        head_calls.append(url)
        return True

    async def fake_push_reference(
        raw: bytes,
        mime: str,
        *,
        base_url: str,
        api_key: str,
    ) -> str | None:
        del mime, base_url, api_key
        upload_calls.append(raw)
        return "https://refs.example/uploaded.webp"

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.provider_pool, "get_pool", fake_get_pool
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.references, "reference_url_is_live", fake_live
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.references,
        "push_reference_to_image_job",
        fake_push_reference,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.time, "time", lambda: now
    )

    ref = b"reference-bytes-fresh-window"
    user_id = "user-fresh"
    cached_url = "https://refs.example/fresh.webp"
    digest = hashlib.sha256(ref).hexdigest()
    await TEST_UPSTREAM_SERVICES.references.reference_cache_store(
        redis,
        user_id=user_id,
        digest=digest,
        url=cached_url,
        size=len(ref),
    )

    # Within 60s safety window and well before expires_at: skip HEAD probe.
    now = 1_030.0
    first = await TEST_UPSTREAM_SERVICES.references.get_or_upload_reference(
        ref,
        "image/webp",
        base_url="https://sidecar.example",
        api_key="sk-test",
        user_id=user_id,
    )
    assert first == cached_url
    assert head_calls == []
    assert upload_calls == []

    # Past 60s window: run HEAD probe and refresh verified_at.
    now = 1_065.0
    second = await TEST_UPSTREAM_SERVICES.references.get_or_upload_reference(
        ref,
        "image/webp",
        base_url="https://sidecar.example",
        api_key="sk-test",
        user_id=user_id,
    )
    assert second == cached_url
    assert head_calls == [cached_url]

    # Subsequent call within 60s of refreshed verified_at skips HEAD again.
    now = 1_090.0
    third = await TEST_UPSTREAM_SERVICES.references.get_or_upload_reference(
        ref,
        "image/webp",
        base_url="https://sidecar.example",
        api_key="sk-test",
        user_id=user_id,
    )
    assert third == cached_url
    assert head_calls == [cached_url]

    # Close to expires_at (< 120s margin): force HEAD probe even if verified recently.
    cache_key, _lru_key = TEST_UPSTREAM_SERVICES.references.reference_cache_keys(
        user_id
    )
    near_expiry_item = json.loads(await redis.hget(cache_key, digest) or "{}")
    near_expiry_item["verified_at"] = 2_740.0
    await redis.hset(cache_key, digest, json.dumps(near_expiry_item))
    now = 2_750.0
    fourth = await TEST_UPSTREAM_SERVICES.references.get_or_upload_reference(
        ref,
        "image/webp",
        base_url="https://sidecar.example",
        api_key="sk-test",
        user_id=user_id,
    )
    assert fourth == cached_url
    assert len(head_calls) == 2


def test_normalize_reference_image_uses_pyvips_and_falls_back_to_pil(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buf = io.BytesIO()
    PILImage.new("L", (4, 3), color=128).save(buf, format="PNG")
    src_png = buf.getvalue()

    webp_buf = io.BytesIO()
    PILImage.new("RGB", (4, 3), color=(128, 128, 128)).save(
        webp_buf, format="WEBP", quality=90
    )
    encoded_webp = webp_buf.getvalue()

    calls: list[tuple[Any, ...]] = []

    class FakeVipsImage:
        def __init__(self, width: int = 4, height: int = 3, bands: int = 1) -> None:
            self.width = width
            self.height = height
            self.bands = bands
            self.interpretation = "b-w"
            self.format = "uchar"

        def get_typeof(self, _field: str) -> int:
            return 0

        def colourspace(self, space: str) -> FakeVipsImage:
            calls.append(("colourspace", space))
            clone = FakeVipsImage(self.width, self.height, bands=3)
            clone.interpretation = space
            return clone

        def webp_save_buffer(
            self,
            *,
            Q: int,
            effort: int,
            strip: bool,
        ) -> bytes:
            calls.append(("webp_save_buffer", Q, effort, strip))
            return encoded_webp

    class FakeImageNamespace:
        @staticmethod
        def new_from_buffer(
            raw: bytes,
            options: str,
            *,
            access: str,
        ) -> FakeVipsImage:
            calls.append(("new_from_buffer", len(raw), options, access))
            return FakeVipsImage()

    monkeypatch.setattr(
        reference_images,
        "_load_pyvips",
        lambda: SimpleNamespace(Image=FakeImageNamespace),
    )

    out_bytes, mime = TEST_UPSTREAM_SERVICES.references.normalize_reference_image(
        src_png
    )
    assert mime == "image/webp"
    assert out_bytes == encoded_webp
    assert calls == [
        ("new_from_buffer", len(src_png), "", "sequential"),
        ("colourspace", "srgb"),
        ("webp_save_buffer", 90, 3, True),
    ]

    # Oversized pixel dimensions are rejected before WebP encoding.
    monkeypatch.setattr(TEST_UPSTREAM_SERVICES.core, "MAX_REFERENCE_IMAGE_PIXELS", 6)
    with pytest.raises(
        TEST_UPSTREAM_SERVICES.infrastructure.UpstreamError
    ) as pixel_exc:
        TEST_UPSTREAM_SERVICES.references.normalize_reference_image(src_png)
    assert pixel_exc.value.status_code == 413
    assert pixel_exc.value.error_code == "reference_image_too_large"
    monkeypatch.undo()

    # Fallback to PIL when pyvips is unavailable.
    rgb_buf = io.BytesIO()
    PILImage.new("RGBA", (3, 2), color=(10, 20, 30, 200)).save(rgb_buf, format="PNG")
    monkeypatch.setattr(reference_images, "_load_pyvips", lambda: None)
    pil_bytes, pil_mime = TEST_UPSTREAM_SERVICES.references.normalize_reference_image(
        rgb_buf.getvalue()
    )
    assert pil_mime == "image/webp"
    with PILImage.open(io.BytesIO(pil_bytes)) as reloaded:
        assert reloaded.format == "WEBP"
        assert reloaded.size == (3, 2)


@pytest.mark.asyncio
async def test_resolve_reference_image_urls_bounded_concurrency_and_order() -> None:
    runtime = build_image_upstream_runtime()
    services = runtime.services
    main_thread_id = threading.get_ident()
    normalize_thread_ids: set[int] = set()
    active = 0
    max_active = 0

    def fake_normalize(raw: bytes) -> tuple[bytes, str]:
        normalize_thread_ids.add(threading.get_ident())
        return b"norm:" + raw, "image/webp"

    async def fake_upload(
        raw: bytes,
        mime: str,
        *,
        base_url: str,
        api_key: str,
        user_id: str | None,
    ) -> str | None:
        nonlocal active, max_active
        del mime, base_url, api_key, user_id
        idx = int(raw.decode("ascii").rsplit("-", 1)[1])
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.005 * (8 - idx))
            return f"https://refs.example/ordered-{idx}.webp"
        finally:
            active -= 1

    services.references.normalize_reference_image = fake_normalize
    services.references.get_or_upload_reference = fake_upload

    inputs = [f"img-{idx}".encode("ascii") for idx in range(7)]
    urls = await services.references.resolve_reference_image_urls(
        inputs,
        base_url="https://sidecar.example",
        api_key="sk-test",
        user_id="user-concurrency",
    )

    assert urls == [f"https://refs.example/ordered-{idx}.webp" for idx in range(7)]
    assert 1 < max_active <= 4
    assert normalize_thread_ids
    assert main_thread_id not in normalize_thread_ids
    await close_client(runtime=runtime)


@pytest.mark.asyncio
async def test_runtime_reuses_keepalive_clients_and_closes_on_close_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_image_upstream_runtime()
    services = runtime.services
    created_clients: list[PooledClient] = []

    class PooledClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.post_calls = 0
            self.head_calls = 0
            self.closed = False
            created_clients.append(self)

        async def post(self, _url: str, **_kwargs: Any) -> Any:
            self.post_calls += 1
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"url": f"https://refs.example/{self.post_calls}.webp"},
            )

        async def head(self, _url: str) -> Any:
            self.head_calls += 1
            return SimpleNamespace(status_code=200)

        async def aclose(self) -> None:
            self.closed = True

    async def fake_resolve(url: str, *, allow_http: bool) -> Any:
        del allow_http
        return SimpleNamespace(url=url, resolved_ips=("203.0.113.10",))

    monkeypatch.setattr(services.infrastructure.httpx, "AsyncClient", PooledClient)
    monkeypatch.setattr(
        services.infrastructure,
        "resolve_public_http_target",
        fake_resolve,
    )
    monkeypatch.setattr(
        services.infrastructure,
        "pinned_async_http_transport",
        lambda _target: "pinned-transport",
    )

    upload_results = await asyncio.gather(
        *(
            services.references.push_reference_to_image_job(
                f"payload-{idx}".encode("ascii"),
                "image/webp",
                base_url="https://sidecar.example",
                api_key="sk-sidecar",
            )
            for idx in range(4)
        )
    )
    assert len(upload_results) == 4
    assert len(created_clients) == 1
    assert created_clients[0].post_calls == 4
    assert created_clients[0].closed is False

    live_results = [
        await services.references.reference_url_is_live(
            f"https://refs.example/{idx}.webp"
        )
        for idx in range(3)
    ]
    assert live_results == [True, True, True]
    assert len(created_clients) == 2
    assert created_clients[1].head_calls == 3
    assert created_clients[1].closed is False

    await close_client(runtime=runtime)
    assert all(client.closed for client in created_clients)
