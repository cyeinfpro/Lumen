from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from app import upstream


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

    monkeypatch.setattr(upstream.provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "_reference_url_is_live", fake_live)
    monkeypatch.setattr(upstream, "_push_reference_to_image_job", fake_push_reference)

    ref = b"reference-bytes-miss"
    user_id = "user-1"
    digest = hashlib.sha256(ref).hexdigest()

    result = await upstream._get_or_upload_reference(
        ref,
        "image/webp",
        base_url="https://sidecar.example",
        api_key="sk-test",
        user_id=user_id,
    )

    cache_key, lru_key = upstream._reference_cache_keys(user_id)
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

    monkeypatch.setattr(upstream.provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "_reference_url_is_live", fake_live)
    monkeypatch.setattr(upstream, "_push_reference_to_image_job", fake_push_reference)

    ref = b"reference-bytes-hit"
    user_id = "user-2"
    digest = hashlib.sha256(ref).hexdigest()
    cached_url = "https://refs.example/cached.webp"
    await upstream._reference_cache_store(
        redis,
        user_id=user_id,
        digest=digest,
        url=cached_url,
        size=len(ref),
    )

    result = await upstream._get_or_upload_reference(
        ref,
        "image/webp",
        base_url="https://sidecar.example",
        api_key="sk-test",
        user_id=user_id,
    )

    cache_key, _lru_key = upstream._reference_cache_keys(user_id)
    assert result == cached_url
    assert upload_calls == []
    assert json.loads(await redis.hget(cache_key, digest) or "{}")["upload_url"] == cached_url


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

    monkeypatch.setattr(upstream.provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "_reference_url_is_live", fake_live)
    monkeypatch.setattr(upstream, "_push_reference_to_image_job", fake_push_reference)

    ref = b"reference-bytes-stale"
    user_id = "user-3"
    digest = hashlib.sha256(ref).hexdigest()
    stale_url = "https://refs.example/stale.webp"
    await upstream._reference_cache_store(
        redis,
        user_id=user_id,
        digest=digest,
        url=stale_url,
        size=len(ref),
    )

    result = await upstream._get_or_upload_reference(
        ref,
        "image/webp",
        base_url="https://sidecar.example",
        api_key="sk-test",
        user_id=user_id,
    )

    cache_key, _lru_key = upstream._reference_cache_keys(user_id)
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

    monkeypatch.setattr(upstream.time, "time", fake_time)

    digests: list[str] = []
    for idx in range(11):
        ref = f"reference-{idx}".encode("utf-8")
        digest = hashlib.sha256(ref).hexdigest()
        digests.append(digest)
        await upstream._reference_cache_store(
            redis,
            user_id=user_id,
            digest=digest,
            url=f"https://refs.example/{idx}.webp",
            size=len(ref),
        )

    cache_key, lru_key = upstream._reference_cache_keys(user_id)
    assert await redis.zcard(lru_key) == 10
    assert len(redis.hashes[cache_key]) == 10
    assert digests[0] not in redis.hashes[cache_key]
    assert digests[-1] in redis.hashes[cache_key]
