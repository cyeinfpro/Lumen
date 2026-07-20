from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

from app import byok_runtime, provider_pool, upstream
from app.provider_runtime import byok_context, contracts, errors
from app.upstream_parts import errors as upstream_errors


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


def test_provider_runtime_contracts_keep_facade_identity() -> None:
    assert provider_pool.ProviderConfig is contracts.ProviderConfig
    assert provider_pool.ProviderHealth is contracts.ProviderHealth
    assert provider_pool.ResolvedProvider is contracts.ResolvedProvider
    assert upstream.UpstreamError is errors.UpstreamError
    assert upstream_errors.UpstreamError is errors.UpstreamError
    assert byok_runtime.UpstreamError is errors.UpstreamError
    assert (
        byok_runtime.current_byok_http_target is byok_context.current_byok_http_target
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


def test_worker_provider_facades_stay_below_file_size_budget() -> None:
    root = Path(__file__).resolve().parents[1] / "app"

    assert len((root / "upstream.py").read_text().splitlines()) < 1500
    assert len((root / "provider_pool.py").read_text().splitlines()) < 1500
    assert (
        len((root / "upstream_parts" / "image_jobs.py").read_text().splitlines()) < 1000
    )
    assert (
        len((root / "upstream_parts" / "direct_requests.py").read_text().splitlines())
        < 1000
    )


@pytest.mark.asyncio
async def test_image_probe_hook_reads_current_upstream_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = contracts.ProviderConfig(
        name="probe-provider",
        base_url="https://probe.example",
        api_key="sk-probe",
    )

    async def fake_probe(**_kwargs: object) -> tuple[str, None]:
        return "x" * provider_pool._IMAGE_PROBE_MIN_B64_LEN, None

    monkeypatch.setattr(upstream, "_responses_image_stream", fake_probe)

    assert await provider_pool.ProviderPool()._probe_image_one(provider)
