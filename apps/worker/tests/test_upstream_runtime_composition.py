from __future__ import annotations

from dataclasses import dataclass

import pytest

from app import upstream
from app.provider_runtime.composition import build_upstream_runtime
from app.provider_runtime.runtime import (
    get_upstream_runtime,
    use_upstream_runtime,
)
from app.provider_runtime.upstream_services import (
    UpstreamServices,
    upstream_services,
    use_upstream_services,
)


@dataclass
class FakePort:
    name: str


def _runtime(name: str):
    return build_upstream_runtime(
        settings=FakePort(f"{name}-settings"),
        providers=FakePort(f"{name}-providers"),
        supplier_transport=FakePort(f"{name}-supplier"),
        image_job_client=FakePort(f"{name}-image-jobs"),
        result_downloads=FakePort(f"{name}-downloads"),
        progress=FakePort(f"{name}-progress"),
        clock=FakePort(f"{name}-clock"),
        metrics=FakePort(f"{name}-metrics"),
    )


def test_two_upstream_runtimes_are_independent() -> None:
    first = _runtime("first")
    second = _runtime("second")

    assert first.settings.name == "first-settings"
    assert second.settings.name == "second-settings"
    assert first.supplier_transport is not second.supplier_transport


def test_runtime_override_does_not_require_monkeypatching_upstream() -> None:
    runtime = _runtime("fake")

    with use_upstream_runtime(runtime):
        assert get_upstream_runtime() is runtime
        assert get_upstream_runtime().settings.name == "fake-settings"


def test_service_override_is_context_local() -> None:
    process_services = upstream_services()
    fake = UpstreamServices(
        **{
            name: getattr(process_services, name)
            for name in process_services.__dataclass_fields__
        }
    )

    with use_upstream_services(fake):
        assert upstream_services() is fake

    assert upstream_services() is process_services


@pytest.mark.asyncio
async def test_close_client_uses_runtime_lifecycle_owner() -> None:
    class Lifecycle:
        closed = False

        async def close(self) -> None:
            self.closed = True

    runtime = _runtime("fake")
    lifecycle = Lifecycle()
    runtime = build_upstream_runtime(
        settings=runtime.settings,
        providers=runtime.providers,
        supplier_transport=lifecycle,
        image_job_client=runtime.image_job_client,
        result_downloads=runtime.result_downloads,
        progress=runtime.progress,
        clock=runtime.clock,
        metrics=runtime.metrics,
    )

    with use_upstream_runtime(runtime):
        await upstream.close_client()

    assert lifecycle.closed is True
