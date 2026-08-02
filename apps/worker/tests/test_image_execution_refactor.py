from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app import provider_pool
from app.upstream_parts import image_dispatch
from app.upstream_parts import entrypoints as upstream
from app.upstream_parts.image_execution import ImageExecutionRequest
from app.upstream_parts.upstream_impl import build_image_upstream_runtime


TEST_UPSTREAM_RUNTIME = build_image_upstream_runtime()
TEST_UPSTREAM_SERVICES = TEST_UPSTREAM_RUNTIME.services


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
        "upstream_runtime": TEST_UPSTREAM_RUNTIME,
    }
    values.update(overrides)
    return ImageExecutionRequest(**values)


def test_image_execution_request_is_the_typed_downstream_boundary() -> None:
    request = _request()

    assert request.action == "edit"
    assert request.images == [b"image"]
    assert request.mask == b"mask"
    assert request.provider_override is not None
    assert not hasattr(request, "action_kwargs")
    assert not hasattr(request, "responses_kwargs")


@pytest.mark.asyncio
async def test_auto_provider_without_image_jobs_does_not_read_sidecar_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SimpleNamespace(name="stream-provider", image_jobs_enabled=False)

    def unexpected_token_read() -> str:
        raise AssertionError("disabled image jobs must not read sidecar config")

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.image_jobs,
        "image_job_sidecar_token",
        unexpected_token_read,
    )

    route = await image_dispatch._prepare_provider_route(
        _request(provider_override=provider, mask=None),
        channel=TEST_UPSTREAM_SERVICES.core.IMAGE_CHANNEL_AUTO,
        engine=TEST_UPSTREAM_SERVICES.core.IMAGE_ROUTE_RESPONSES,
    )

    assert route.use_jobs is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sidecar_token", "provider_base_url"),
    [
        ("", ""),
        ("short", ""),
        ("s" * 32, "https://image-job.example.com"),
    ],
)
async def test_auto_falls_back_to_stream_when_sidecar_configuration_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    sidecar_token: str,
    provider_base_url: str,
) -> None:
    provider = SimpleNamespace(
        name="jobs-provider",
        image_jobs_enabled=True,
        image_jobs_base_url=provider_base_url,
    )
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.settings,
        "image_job_sidecar_token",
        sidecar_token,
    )

    route = await image_dispatch._prepare_provider_route(
        _request(
            provider_override=provider,
            mask=None,
            progress_callback=events.append,
        ),
        channel=TEST_UPSTREAM_SERVICES.core.IMAGE_CHANNEL_AUTO,
        engine=TEST_UPSTREAM_SERVICES.core.IMAGE_ROUTE_RESPONSES,
    )

    assert route.use_jobs is False
    assert events[-1]["reason"] == "image_job_configuration_unavailable"
    assert events[-1]["fallback_route"] == "stream_only:responses"


@pytest.mark.asyncio
async def test_image_jobs_only_reports_configuration_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SimpleNamespace(name="jobs-provider", image_jobs_enabled=True)
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.settings, "image_job_sidecar_token", ""
    )

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await image_dispatch._prepare_provider_route(
            _request(provider_override=provider, mask=None),
            channel=TEST_UPSTREAM_SERVICES.core.IMAGE_CHANNEL_IMAGE_JOBS_ONLY,
            engine=TEST_UPSTREAM_SERVICES.core.IMAGE_ROUTE_RESPONSES,
        )

    assert exc_info.value.status_code == 503
    assert "configuration unavailable" in str(exc_info.value)
    assert exc_info.value.payload == {
        "path": "image-jobs",
        "configuration": "sidecar_auth",
        "reason": "configuration_unavailable",
    }


@pytest.mark.asyncio
async def test_effective_image_jobs_only_configuration_is_validated_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def strict_channel() -> str:
        return "image_jobs_only"

    calls: list[str] = []

    def valid_token() -> str:
        calls.append("token")
        return "s" * 32

    async def valid_base_url() -> str:
        calls.append("base_url")
        return "https://image-job.internal"

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.core, "resolve_image_channel", strict_channel
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.image_jobs, "image_job_sidecar_token", valid_token
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.direct, "resolve_image_job_base_url", valid_base_url
    )

    await upstream.validate_effective_image_job_configuration(
        runtime=TEST_UPSTREAM_RUNTIME
    )

    assert calls == ["token", "base_url"]


@pytest.mark.asyncio
async def test_mask_dispatch_rejects_empty_reference_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_direct(
        _request: ImageExecutionRequest,
    ) -> list[tuple[str, str | None]]:
        raise AssertionError("empty mask references must fail before dispatch")

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.direct,
        "direct_edit_image_with_failover",
        unexpected_direct,
    )
    provider = SimpleNamespace(name="mask-provider", image_jobs_enabled=False)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        async for _ in TEST_UPSTREAM_SERVICES.dispatch.run_image_once_for_provider(
            _request(
                provider_override=provider,
                images=[b""],
            ),
            channel="stream_only",
            engine="image2",
        ):
            pass

    assert (
        exc_info.value.error_code
        == TEST_UPSTREAM_SERVICES.infrastructure.EC.MISSING_INPUT_IMAGES.value
    )
    assert str(exc_info.value) == "mask requires at least one reference image"


@pytest.mark.asyncio
async def test_responses_fallback_preserves_missing_edit_input_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_responses(
        _request: ImageExecutionRequest,
        *,
        lanes: int,
    ) -> tuple[str, str | None]:
        _ = lanes
        raise upstream.UpstreamError(
            "responses failed",
            status_code=503,
            error_code="server_error",
        )

    async def unexpected_direct(
        _request: ImageExecutionRequest,
    ) -> list[tuple[str, str | None]]:
        raise AssertionError("missing edit input must fail before direct dispatch")

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.race, "race_responses_image", failed_responses
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.direct,
        "direct_edit_image_with_failover",
        unexpected_direct,
    )
    provider = SimpleNamespace(name="edit-provider", image_jobs_enabled=False)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        async for _ in TEST_UPSTREAM_SERVICES.dispatch.run_image_once_for_provider(
            _request(
                provider_override=provider,
                images=None,
                mask=None,
            ),
            channel="stream_only",
            engine="responses",
        ):
            pass

    assert (
        exc_info.value.error_code
        == TEST_UPSTREAM_SERVICES.infrastructure.EC.MISSING_INPUT_IMAGES.value
    )
    assert str(exc_info.value) == "edit action requires at least one reference image"


@pytest.mark.asyncio
async def test_responses_race_waits_for_loser_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()
    transports: list[bool] = []

    async def fake_responses(
        _request: ImageExecutionRequest,
        *,
        use_httpx: bool,
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
        TEST_UPSTREAM_SERVICES.direct,
        "responses_image_stream_with_failover",
        fake_responses,
    )

    result = await TEST_UPSTREAM_SERVICES.race.race_responses_image(
        _request(
            action="generate",
            prompt="image",
            images=None,
            mask=None,
            provider_override=None,
        ),
        lanes=2,
    )

    assert result == ("winner", None)
    assert sorted(transports) == [False, True]
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_dispatch_close_propagates_to_dual_race_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    async def fake_image2(
        _request: ImageExecutionRequest,
    ) -> list[tuple[str, str | None]]:
        await asyncio.sleep(0.01)
        return [("winner", None)]

    async def fake_responses(
        _request: ImageExecutionRequest,
        *,
        use_httpx: bool,
    ) -> tuple[str, str | None]:
        _ = use_httpx
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "late", None

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.direct,
        "direct_generate_image_with_failover",
        fake_image2,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.direct,
        "responses_image_stream_with_failover",
        fake_responses,
    )
    provider = SimpleNamespace(name="race-provider", image_jobs_enabled=False)
    image_iter = TEST_UPSTREAM_SERVICES.dispatch.run_image_once_for_provider(
        _request(
            action="generate",
            prompt="image",
            images=None,
            mask=None,
            provider_override=provider,
        ),
        channel="stream_only",
        engine="dual_race",
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

    async def fake_base_url(
        *,
        runtime: Any | None = None,
    ) -> str:
        _ = runtime
        return "https://image-job.example"

    async def cancelled_run(
        _request: ImageExecutionRequest,
        **_kwargs: Any,
    ) -> tuple[str, str | None]:
        raise TEST_UPSTREAM_SERVICES.infrastructure.UpstreamCancelled("cancelled")

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.provider_pool, "get_pool", fake_get_pool
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.providers, "pool_select_compat", fake_select
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.direct, "resolve_image_job_base_url", fake_base_url
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.image_jobs, "image_job_run_once", cancelled_run
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.providers,
        "image_request_attempt_claim",
        lambda *_args, **_kwargs: (
            lambda _attempt: None,
            lambda **_kw: None,
        ),
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.providers,
        "pool_release_inflight",
        lambda _pool, name, endpoint: releases.append((name, endpoint)),
    )

    with pytest.raises(TEST_UPSTREAM_SERVICES.infrastructure.UpstreamCancelled):
        await TEST_UPSTREAM_SERVICES.image_jobs.image_job_with_failover(
            _request(
                action="generate",
                prompt="image",
                images=None,
                mask=None,
                provider_override=None,
            ),
            endpoint_override="responses",
        )

    assert releases == [("cancel-provider", "responses")]
