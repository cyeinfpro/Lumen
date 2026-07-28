from __future__ import annotations

import pytest

from app import upstream
from app.config import settings
from app.provider_runtime.contracts import (
    ImageProbeRequest,
    ProviderConfig,
    ResolvedProvider,
)
from app.provider_runtime.upstream_services import build_upstream_services
from app.tasks.generation_parts.composition import build_generation_runtime
from app.tasks.generation_parts.services import (
    GenerationProviderContext,
    GenerationProviderRequest,
)
from app.upstream_parts import upstream_impl


TEST_UPSTREAM_RUNTIME = upstream_impl.build_image_upstream_runtime()
TEST_UPSTREAM_SERVICES = TEST_UPSTREAM_RUNTIME.services


def test_composition_root_includes_runtime_settings_and_generation_modules() -> None:
    services = TEST_UPSTREAM_SERVICES

    assert services.infrastructure.settings is settings
    assert (
        services.retry.responses_image_stream_with_retry
        is upstream_impl.upstream_retry_policy._responses_image_stream_with_retry
    )
    assert (
        services.transport.iter_sse_curl.func
        is upstream_impl.upstream_transport._iter_sse_curl
    )
    assert services.transport.iter_sse_curl.keywords["runtime"] is TEST_UPSTREAM_RUNTIME
    assert (
        services.lifecycle.get_client.func
        is upstream_impl.upstream_client_lifecycle._get_client
    )
    assert services.lifecycle.get_client.keywords["runtime"] is TEST_UPSTREAM_RUNTIME


def test_composition_rejects_missing_runtime_settings() -> None:
    namespace = dict(vars(upstream_impl))
    namespace.pop("settings")

    with pytest.raises(
        RuntimeError,
        match="missing required dependencies: settings",
    ):
        build_upstream_services(namespace)


@pytest.mark.asyncio
async def test_generation_image_path_uses_explicit_runtime_service_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_runtime = upstream_impl.build_image_upstream_runtime()
    services = image_runtime.services

    async def resolve_channel() -> str:
        return "stream_only"

    async def resolve_engine() -> str:
        return "responses"

    async def race_responses_image(
        request: object,
        *,
        lanes: int,
    ) -> tuple[str, None]:
        assert lanes >= 1
        assert getattr(request, "upstream_runtime") is image_runtime
        return "generated-image", None

    monkeypatch.setattr(services.core, "resolve_image_channel", resolve_channel)
    monkeypatch.setattr(services.core, "resolve_image_engine", resolve_engine)
    monkeypatch.setattr(
        services.providers,
        "provider_endpoint_unavailable_error",
        lambda _provider, _endpoint: None,
    )
    monkeypatch.setattr(services.race, "race_responses_image", race_responses_image)

    generation_runtime = build_generation_runtime(
        image_upstream_runtime=image_runtime,
    )
    assert generation_runtime.image_upstream_runtime is image_runtime
    provider = generation_runtime.deps.provider
    request = GenerationProviderRequest(
        prompt="runtime isolation",
        size="1024x1024",
        n=1,
        quality="high",
        output_format="png",
        output_compression=None,
        background="auto",
        moderation="low",
        model=None,
        progress_callback=None,
        provider_override=ResolvedProvider(
            name="explicit-provider",
            base_url="https://provider.example/v1",
            api_key="sk-explicit",
        ),
        user_id="user-1",
        context=GenerationProviderContext(
            trace_id="trace-explicit",
            retry_attempt=1,
            quota_task_id="generation-explicit",
            quota_attempt_epoch=1,
        ),
    )

    results = [item async for item in provider.generate(request)]

    assert results == [("generated-image", None)]


@pytest.mark.asyncio
async def test_image_runtime_bound_helpers_share_explicit_service_graph() -> None:
    image_runtime = upstream_impl.build_image_upstream_runtime()
    services = image_runtime.services

    assert services.core.normalize_image_quality("high") == "high"
    assert services.retry.fallback_retry_backoff_seconds(3) == 4.0
    assert services.direct.wrap_inpaint_prompt("replace the object")
    assert services.references.reference_cache_keys("user-1")[0].endswith("user-1")
    assert services.providers.provider_attempt_context(
        ResolvedProvider(
            name="explicit-provider",
            base_url="https://provider.example/v1",
            api_key="sk-explicit",
        )
    )["byok"] is False
    assert services.image_jobs.image_job_payload(
        request_type="generations",
        endpoint="/v1/images/generations",
        body={"prompt": "test"},
    )["request_type"] == "generations"
    await services.transport.emit_image_progress(None, "completed")


@pytest.mark.asyncio
async def test_image_probe_uses_explicit_runtime_service_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_runtime = upstream_impl.build_image_upstream_runtime()
    services = image_runtime.services

    async def responses_image_stream(
        request: object,
        **kwargs: object,
    ) -> tuple[str, None]:
        assert getattr(request, "upstream_runtime") is image_runtime
        assert kwargs["base_url_override"] == "https://probe.example/v1"
        return "probe-image", None

    monkeypatch.setattr(
        services.responses,
        "responses_image_stream",
        responses_image_stream,
    )
    result = await upstream_impl.upstream_image_stream.run_image_probe(
        ImageProbeRequest(
            prompt="probe",
            size="1024x1024",
            quality="low",
            provider=ProviderConfig(
                name="probe-provider",
                base_url="https://probe.example/v1",
                api_key="sk-probe",
            ),
        ),
        runtime=image_runtime,
    )

    assert result == ("probe-image", None)


@pytest.mark.asyncio
async def test_close_client_uses_composed_lifecycle_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False

    async def close_client(*, runtime: object) -> None:
        nonlocal closed
        assert runtime is TEST_UPSTREAM_RUNTIME
        closed = True

    monkeypatch.setattr(
        upstream_impl.upstream_client_lifecycle,
        "close_client",
        close_client,
    )
    await upstream.close_client(runtime=TEST_UPSTREAM_RUNTIME)

    assert closed is True
