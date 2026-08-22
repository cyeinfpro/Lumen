"""Process-global provider-proxy runtime rotation."""

from __future__ import annotations

from lumen_core.providers_parts import proxy_runtime
from lumen_core.providers_parts.definitions import ProviderProxyDefinition


class ProviderProxyLifecycle:
    """Own one loop-bound proxy runtime and replace it after shutdown."""

    def __init__(self) -> None:
        self.runtime = proxy_runtime.ProviderProxyRuntime()

    async def resolve(self, proxy: ProviderProxyDefinition | None) -> str | None:
        runtime = self.runtime
        return await proxy_runtime.resolve_provider_proxy_url(
            proxy,
            runtime=runtime,
        )

    async def resolve_for_agent(
        self,
        proxy: ProviderProxyDefinition | None,
        *,
        bind_host: str,
        advertise_host: str,
    ) -> str | None:
        return await proxy_runtime.resolve_provider_proxy_url(
            proxy,
            runtime=self.runtime,
            bind_host=bind_host,
            advertise_host=advertise_host,
        )

    async def close(self) -> None:
        runtime = self.runtime
        try:
            # The runtime lock serializes in-flight resolve and repeated close.
            await proxy_runtime.close_provider_proxy_tunnels(runtime=runtime)
        finally:
            if self.runtime is runtime:
                self.runtime = proxy_runtime.ProviderProxyRuntime()


__all__ = ["ProviderProxyLifecycle", "proxy_runtime"]
