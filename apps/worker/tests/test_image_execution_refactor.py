from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app import provider_pool, upstream
from app.upstream_parts.image_execution import ImageExecutionRequest


def _request(**overrides: Any) -> ImageExecutionRequest:
    values: dict[str, Any] = {
        "action": "edit",
        "prompt": "refine",
        "size": "1024x1024",
        "images": [b"image"],
        "mask": b"mask",
        "n": 2,
        "quality": "high",
        "output_format": "png",
        "output_compression": 80,
        "background": "transparent",
        "moderation": "low",
        "model": "image-model",
        "progress_callback": None,
        "provider_override": object(),
        "user_id": "user-1",
    }
    values.update(overrides)
    return ImageExecutionRequest(**values)


def test_image_execution_request_keeps_downstream_kwarg_boundaries() -> None:
    request = _request()

    assert set(request.action_kwargs()) == {
        "action",
        "prompt",
        "size",
        "images",
        "mask",
        "n",
        "quality",
        "output_format",
        "output_compression",
        "background",
        "moderation",
        "model",
        "progress_callback",
        "provider_override",
        "user_id",
    }
    assert "provider_override" not in request.job_run_kwargs()
    assert {"mask", "n"}.isdisjoint(request.responses_kwargs())
    assert {"model", "user_id"}.isdisjoint(request.direct_edit_kwargs())
    assert {"images", "mask", "model", "user_id"}.isdisjoint(
        request.direct_generate_kwargs()
    )


@pytest.mark.asyncio
async def test_mask_dispatch_rejects_empty_reference_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_direct(**_kwargs: Any) -> list[tuple[str, str | None]]:
        raise AssertionError("empty mask references must fail before dispatch")

    monkeypatch.setattr(
        upstream,
        "_direct_edit_image_with_failover",
        unexpected_direct,
    )
    provider = SimpleNamespace(name="mask-provider", image_jobs_enabled=False)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        async for _ in upstream._run_image_once_for_provider(
            action="edit",
            provider=provider,
            channel="stream_only",
            engine="image2",
            prompt="edit",
            size="1024x1024",
            images=[b""],
            mask=b"mask",
            n=1,
            quality="high",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
        ):
            pass

    assert exc_info.value.error_code == upstream.EC.MISSING_INPUT_IMAGES.value
    assert str(exc_info.value) == "mask requires at least one reference image"


@pytest.mark.asyncio
async def test_responses_fallback_preserves_missing_edit_input_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_responses(**_kwargs: Any) -> tuple[str, str | None]:
        raise upstream.UpstreamError(
            "responses failed",
            status_code=503,
            error_code="server_error",
        )

    async def unexpected_direct(**_kwargs: Any) -> list[tuple[str, str | None]]:
        raise AssertionError("missing edit input must fail before direct dispatch")

    monkeypatch.setattr(upstream, "_race_responses_image", failed_responses)
    monkeypatch.setattr(
        upstream,
        "_direct_edit_image_with_failover",
        unexpected_direct,
    )
    provider = SimpleNamespace(name="edit-provider", image_jobs_enabled=False)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        async for _ in upstream._run_image_once_for_provider(
            action="edit",
            provider=provider,
            channel="stream_only",
            engine="responses",
            prompt="edit",
            size="1024x1024",
            images=None,
            n=1,
            quality="high",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
        ):
            pass

    assert exc_info.value.error_code == upstream.EC.MISSING_INPUT_IMAGES.value
    assert str(exc_info.value) == "edit action requires at least one reference image"


@pytest.mark.asyncio
async def test_responses_race_waits_for_loser_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()
    transports: list[bool] = []

    async def fake_responses(
        *,
        use_httpx: bool,
        **_kwargs: Any,
    ) -> tuple[str, str | None]:
        transports.append(use_httpx)
        if not use_httpx:
            await asyncio.sleep(0.01)
            return "winner", None
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "late", None

    monkeypatch.setattr(
        upstream,
        "_responses_image_stream_with_failover",
        fake_responses,
    )

    result = await upstream._race_responses_image(
        action="generate",
        prompt="image",
        size="1024x1024",
        images=None,
        quality="high",
        lanes=2,
        progress_callback=None,
    )

    assert result == ("winner", None)
    assert sorted(transports) == [False, True]
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_dispatch_close_propagates_to_dual_race_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    async def fake_image2(**_kwargs: Any) -> list[tuple[str, str | None]]:
        await asyncio.sleep(0.01)
        return [("winner", None)]

    async def fake_responses(**_kwargs: Any) -> tuple[str, str | None]:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "late", None

    monkeypatch.setattr(
        upstream,
        "_direct_generate_image_with_failover",
        fake_image2,
    )
    monkeypatch.setattr(
        upstream,
        "_responses_image_stream_with_failover",
        fake_responses,
    )
    provider = SimpleNamespace(name="race-provider", image_jobs_enabled=False)
    image_iter = upstream._run_image_once_for_provider(
        action="generate",
        provider=provider,
        channel="stream_only",
        engine="dual_race",
        prompt="image",
        size="1024x1024",
        images=None,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        model=None,
        progress_callback=None,
    )

    assert await anext(image_iter) == ("winner", None)
    await image_iter.aclose()

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_image_job_cancellation_releases_selected_provider_inflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = provider_pool.ResolvedProvider(
        name="cancel-provider",
        base_url="https://provider.example",
        api_key="secret",
        image_jobs_enabled=True,
    )
    pool = object()
    releases: list[tuple[str, str | None]] = []

    async def fake_get_pool() -> object:
        return pool

    async def fake_select(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [provider]

    async def fake_base_url() -> str:
        return "https://image-job.example"

    async def cancelled_run(**_kwargs: Any) -> tuple[str, str | None]:
        raise upstream.UpstreamCancelled("cancelled")

    monkeypatch.setattr(upstream.provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "_pool_select_compat", fake_select)
    monkeypatch.setattr(upstream, "_resolve_image_job_base_url", fake_base_url)
    monkeypatch.setattr(upstream, "_image_job_run_once", cancelled_run)
    monkeypatch.setattr(
        upstream,
        "_image_request_attempt_claim",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        upstream,
        "_pool_release_inflight",
        lambda _pool, name, endpoint: releases.append((name, endpoint)),
    )

    with pytest.raises(upstream.UpstreamCancelled):
        await upstream._image_job_with_failover(
            action="generate",
            prompt="image",
            size="1024x1024",
            images=None,
            n=1,
            quality="high",
            progress_callback=None,
            endpoint_override="responses",
        )

    assert releases == [("cancel-provider", "responses")]
