from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import sys
from dataclasses import replace
from typing import Any

import pytest

from app import byok_runtime, provider_pool
from app.provider_runtime import byok_context, contracts, errors
from app.provider_runtime.probe_runtime import build_provider_probe_runtime
from app.upstream_parts import (
    direct_failover,
    direct_requests,
    entrypoints as upstream,
    image_dispatch,
    image_job_failover,
    image_jobs,
    image_race,
    image_stream,
    provider_selection,
    reference_images,
    request_targets,
    responses_client,
    retry_policy,
    transport,
)


def _load_architecture_gate() -> Any:
    root = Path(__file__).resolve().parents[3]
    module_name = "_worker_architecture_gate"
    spec = importlib.util.spec_from_file_location(
        module_name,
        root / "scripts" / "check_architecture.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_provider_runtime_contracts_keep_entrypoint_identity() -> None:
    assert provider_pool.ProviderConfig is contracts.ProviderConfig
    assert provider_pool.ProviderHealth is contracts.ProviderHealth
    assert provider_pool.ResolvedProvider is contracts.ResolvedProvider
    assert upstream.UpstreamError is errors.UpstreamError
    assert byok_runtime.UpstreamError is errors.UpstreamError
    assert (
        byok_runtime.validate_byok_http_target is byok_context.validate_byok_http_target
    )


def test_worker_provider_runtime_graph_is_acyclic() -> None:
    root = Path(__file__).resolve().parents[3]
    check_architecture = _load_architecture_gate()
    spec = check_architecture.PackageSpec(
        "worker",
        root / "apps" / "worker" / "app",
        "app",
    )
    graph = check_architecture.build_package_graph(spec)

    assert check_architecture.strongly_connected_components(graph.edges) == []


def test_worker_provider_entrypoints_stay_below_file_size_budget() -> None:
    root = Path(__file__).resolve().parents[1] / "app"

    assert not (root / "upstream.py").exists()
    assert (
        len((root / "upstream_parts" / "entrypoints.py").read_text().splitlines())
        < 1500
    )
    assert len((root / "provider_pool.py").read_text().splitlines()) < 1500
    assert (
        len((root / "upstream_parts" / "image_jobs.py").read_text().splitlines()) < 1000
    )
    assert (
        len((root / "upstream_parts" / "direct_requests.py").read_text().splitlines())
        < 1000
    )


def test_upstream_parts_do_not_import_or_read_parent_facade() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "upstream_parts"
    source = "\n".join(path.read_text() for path in sorted(root.glob("*.py")))

    assert "_facade(" not in source
    assert "_FacadeProxy" not in source
    assert "_UPSTREAM_MODULE_NAME" not in source
    assert "import_module(" not in source
    assert "facade._" not in source
    assert "_runtime._" not in source


def test_provider_runtime_has_no_context_local_runtime_registry() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "provider_runtime"
    source = "\n".join(
        path.read_text()
        for path in (
            root / "probe_runtime.py",
            root / "upstream_services.py",
        )
    )

    assert "ContextVar" not in source
    assert "use_provider_probe_runtime" not in source
    assert "use_upstream_services" not in source


def test_provider_probe_runtime_is_owned_by_each_pool() -> None:
    first_runtime = replace(build_provider_probe_runtime(), probe_timeout_s=1.0)
    second_runtime = replace(build_provider_probe_runtime(), probe_timeout_s=2.0)

    first = provider_pool.ProviderPool(probe_runtime=first_runtime)
    second = provider_pool.ProviderPool(probe_runtime=second_runtime)

    assert first._probe_runtime is first_runtime
    assert second._probe_runtime is second_runtime
    assert first._probe_runtime is not second._probe_runtime


def test_generation_composition_has_no_dynamic_runtime_locator() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    generation_parts = app_root / "tasks" / "generation_parts"
    paths = (
        app_root / "main.py",
        app_root / "tasks" / "generation.py",
        generation_parts / "composition.py",
        generation_parts / "composition_ports.py",
        generation_parts / "composition_support.py",
        generation_parts / "default_runtime.py",
        generation_parts / "runtime.py",
    )
    source = "\n".join(path.read_text() for path in paths)

    for forbidden in (
        "Callable[..., Any]",
        "ContextVar",
        "DEFAULT_GENERATION_RUNTIME",
        "RuntimeSlot",
        "SimpleNamespace",
        "_PROCESS_SERVICES",
        "upstream_services()",
    ):
        assert forbidden not in source


def test_generation_upstream_functions_do_not_read_ambient_registry() -> None:
    functions = (
        image_dispatch.generate_image,
        image_dispatch.edit_image,
        image_dispatch._dispatch_image,
        direct_failover._direct_generate_image_with_failover,
        direct_failover._direct_edit_image_with_failover,
        direct_failover._responses_image_stream_with_failover,
        direct_requests._direct_generate_image_once,
        direct_requests._direct_edit_image_once,
        image_job_failover._image_job_with_failover,
        image_jobs._image_job_generate_once,
        image_jobs._image_job_edit_once,
        image_jobs._image_job_responses_once,
        image_race._race_responses_image,
        image_race._dual_race_image_action,
        image_race._dual_race_image_jobs_action,
        image_stream.run_image_probe,
        image_stream._responses_image_stream,
        provider_selection._reserve_admin_image_call,
        reference_images._resolve_reference_image_urls,
        responses_client._iter_sse_with_runtime,
        retry_policy._responses_image_stream_with_retry,
        transport._curl_post_multipart,
        transport._iter_sse_curl,
        request_targets._validated_byok_target_for_request,
    )

    for function in functions:
        assert "upstream_services()" not in inspect.getsource(function), function


@pytest.mark.asyncio
async def test_image_probe_receives_explicit_typed_request() -> None:
    provider = contracts.ProviderConfig(
        name="probe-provider",
        base_url="https://probe.example",
        api_key="sk-probe",
    )

    async def fake_probe(request: contracts.ImageProbeRequest) -> tuple[str, None]:
        assert request.provider is provider
        return "x" * provider_pool._IMAGE_PROBE_MIN_B64_LEN, None

    assert await provider_pool.ProviderPool()._probe_image_one(provider, fake_probe)
