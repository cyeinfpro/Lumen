from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app import video_billing, video_provider_slots, video_upstream
from app.video_submit_cache import load_submit_result
from app.video_upstream import (
    DashScopeHappyHorseAdapter,
    UnifiedVideoCreateAdapter,
    VideoReferenceMedia,
    VideoSubmitRequest,
    VideoUpstreamError,
)
from lumen_core.models import VideoGeneration
from lumen_core.video_providers import VideoProviderDefinition


_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png"


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", "-Infinity"],
)
def test_video_usage_parsers_reject_non_finite_values(value: object) -> None:
    assert video_upstream._int_or_none(value) is None  # noqa: SLF001
    assert (
        video_upstream._duration_usage_total_tokens(  # noqa: SLF001
            {"usage": {"duration": value}}
        )
        is None
    )


@pytest.mark.parametrize(
    ("data", "mime", "max_bytes", "message", "status_code"),
    [
        (b"not-an-image", "image/png", 1024, "supported image", 422),
        (_PNG_BYTES, "image/jpeg", 1024, "does not match", 422),
        (_PNG_BYTES, "image/png", len(_PNG_BYTES) - 1, "too large", 413),
    ],
)
def test_inline_image_data_urls_validate_magic_mime_and_size(
    data: bytes,
    mime: str,
    max_bytes: int,
    message: str,
    status_code: int,
) -> None:
    with pytest.raises(VideoUpstreamError, match=message) as excinfo:
        video_upstream._image_data_url(  # noqa: SLF001
            data,
            mime,
            field="test reference",
            max_bytes=max_bytes,
        )

    assert excinfo.value.status_code == status_code


def test_seedance_reference_bytes_use_shared_image_validation() -> None:
    request = VideoSubmitRequest(
        task_id="video-1",
        user_id="user-1",
        action="reference",
        model="seedance-2.0",
        upstream_model="seedance-upstream",
        prompt="animate the reference",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        reference_media=[
            VideoReferenceMedia(
                kind="image",
                data=b"not-an-image",
                mime="image/png",
            )
        ],
    )

    with pytest.raises(VideoUpstreamError, match="supported image"):
        video_upstream._seedance_content(request)  # noqa: SLF001


def test_omni_inline_bytes_use_shared_image_validation() -> None:
    provider = VideoProviderDefinition(
        name="omni",
        kind="omni_flash",
        base_url="https://gateway.example.com",
        api_key="sk-test",
        models={"omni-flash:i2v": "gemini_omni_flash"},
    )
    adapter = UnifiedVideoCreateAdapter(provider)
    request = VideoSubmitRequest(
        task_id="video-1",
        user_id="user-1",
        action="i2v",
        model="omni-flash",
        upstream_model="gemini_omni_flash",
        prompt="animate the image",
        duration_s=6,
        resolution="720p",
        aspect_ratio="16:9",
        input_image_bytes=_PNG_BYTES,
        input_image_mime="image/jpeg",
    )

    with pytest.raises(VideoUpstreamError, match="does not match"):
        adapter._images(request)  # noqa: SLF001


@pytest.mark.asyncio
async def test_happyhorse_success_preserves_explicit_billable_false() -> None:
    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _path: str) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [{"url": "https://cdn.example/video.mp4"}],
                        "usage": {"duration": 3, "billable": False},
                    }
                },
            )

    provider = VideoProviderDefinition(
        name="dashscope",
        kind="dashscope",
        base_url="https://dashscope.example.com",
        api_key="sk-test",
        models={"happyhorse-1.0:t2v": "happyhorse-1.0-t2v"},
    )
    adapter = DashScopeHappyHorseAdapter(provider)
    adapter._client = Client  # type: ignore[method-assign]

    result = await adapter.poll("task-1")

    assert result.status == "succeeded"
    assert result.upstream_billable is False


@pytest.mark.asyncio
async def test_non_utf8_submit_cache_is_a_miss() -> None:
    class Redis:
        async def get(self, _key: str) -> bytes:
            return b"\xff\xfe\xfa"

    assert await load_submit_result(Redis(), "video-1") is None


@pytest.mark.asyncio
async def test_provider_slot_concurrent_admission_is_atomic() -> None:
    class Redis:
        def __init__(self) -> None:
            self.active: dict[str, float] = {}
            self.lock = asyncio.Lock()
            self.eval_calls = 0

        async def eval(self, script: str, numkeys: int, *args: Any) -> int:
            assert numkeys == 2
            assert "ZCARD" in script and "ZADD" in script
            self.eval_calls += 1
            task_id = str(args[2])
            now = float(args[3])
            cutoff = float(args[4])
            concurrency = int(args[5])
            async with self.lock:
                await asyncio.sleep(0)
                self.active = {
                    member: score
                    for member, score in self.active.items()
                    if score > cutoff
                }
                if task_id in self.active:
                    self.active[task_id] = now
                    return 1
                if len(self.active) >= concurrency:
                    return 0
                self.active[task_id] = now
                return 1

    redis = Redis()
    results = await asyncio.gather(
        *(
            video_provider_slots.acquire_provider_slot(
                redis,
                "provider-a",
                concurrency=1,
                task_id=f"video-{index}",
            )
            for index in range(8)
        )
    )

    assert sum(results) == 1
    assert len(redis.active) == 1
    assert redis.eval_calls == 8


@pytest.mark.asyncio
async def test_video_billing_treats_infinite_usage_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.info: dict[str, object] = {}

        def add(self, _value: object) -> None:
            return None

    async def held_amount_for_ref(*_args: object, **_kwargs: object) -> int:
        return 1_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        video_billing.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        video_billing.worker_billing,
        "allow_negative_balance",
        allow_negative_balance,
    )
    monkeypatch.setattr(video_billing.billing_core, "settle", settle)
    generation = VideoGeneration(
        id="video-1",
        user_id="user-1",
        action="t2v",
        model="seedance-2.0",
        provider_name="provider-a",
        provider_kind="volcano",
        provider_task_id="upstream-1",
        prompt="make a clip",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        deadline_at=datetime.now(timezone.utc),
        idempotency_key="idem-1",
        request_fingerprint="f" * 64,
        est_token_upper=60_000,
        est_cost_micro=1_000,
    )

    result = await video_billing.resolve_video_billing(
        Session(),  # type: ignore[arg-type]
        generation,
        poll_result=SimpleNamespace(
            status="succeeded",
            usage_total_tokens=float("inf"),
            upstream_billable=None,
        ),
        reason="succeeded",
    )

    assert result.decision == "missing_usage_default_charge"
    assert result.actual_tokens is None
