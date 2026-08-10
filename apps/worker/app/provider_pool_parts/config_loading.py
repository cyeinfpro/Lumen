"""Typed provider configuration loading with bounded last-known-good use."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from lumen_core.providers_parts.config import (
    build_legacy_provider,
    parse_provider_config_json,
)
from lumen_core.providers_parts.definitions import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderProxyDefinition,
)

from ..provider_runtime.contracts import ProviderConfig, ProviderHealth
from ..provider_runtime.errors import UpstreamError

logger = logging.getLogger("app.provider_pool")

_CONFIG_TTL_S = 5.0
_CONFIG_UNAVAILABLE_RETRY_S = 1.0
_CONFIG_LKG_MAX_STALE_S = 60.0

ProviderLoadState = Literal[
    "configured",
    "explicit_empty",
    "invalid",
    "unavailable",
]


@dataclass(frozen=True, slots=True)
class ProviderLoad:
    state: ProviderLoadState
    providers: tuple[ProviderConfig, ...] = ()
    proxies: dict[str, ProviderProxyDefinition] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


def _runtime_provider(definition: Any) -> ProviderConfig:
    return ProviderConfig(
        name=definition.name,
        base_url=definition.base_url,
        api_key=definition.api_key,
        priority=definition.priority,
        weight=definition.weight,
        enabled=definition.enabled,
        purposes=definition.purposes,
        proxy_name=definition.proxy_name,
        proxy=definition.proxy,
        image_rate_limit=definition.image_rate_limit,
        image_daily_quota=definition.image_daily_quota,
        image_jobs_enabled=definition.image_jobs_enabled,
        image_streaming_enabled=definition.image_streaming_enabled,
        image_jobs_endpoint=definition.image_jobs_endpoint,
        image_jobs_endpoint_lock=definition.image_jobs_endpoint_lock,
        image_jobs_base_url=definition.image_jobs_base_url,
        image_edit_input_transport=definition.image_edit_input_transport,
        image_concurrency=definition.image_concurrency,
        responses_supported=getattr(definition, "responses_supported", None),
        image_generations_supported=getattr(
            definition,
            "image_generations_supported",
            None,
        ),
        image_responses_supported=getattr(
            definition,
            "image_responses_supported",
            None,
        ),
    )


def _validated_runtime_provider(provider: ProviderConfig, base_url: str) -> ProviderConfig:
    return ProviderConfig(
        name=provider.name,
        base_url=base_url,
        api_key=provider.api_key,
        priority=provider.priority,
        weight=provider.weight,
        enabled=provider.enabled,
        purposes=provider.purposes,
        proxy_name=provider.proxy_name,
        proxy=provider.proxy,
        image_rate_limit=provider.image_rate_limit,
        image_daily_quota=provider.image_daily_quota,
        image_jobs_enabled=provider.image_jobs_enabled,
        image_streaming_enabled=provider.image_streaming_enabled,
        image_jobs_endpoint=provider.image_jobs_endpoint,
        image_jobs_endpoint_lock=provider.image_jobs_endpoint_lock,
        image_jobs_base_url=provider.image_jobs_base_url,
        image_edit_input_transport=provider.image_edit_input_transport,
        image_concurrency=provider.image_concurrency,
        responses_supported=provider.responses_supported,
        image_generations_supported=provider.image_generations_supported,
        image_responses_supported=provider.image_responses_supported,
    )


def _quota_errors(providers: list[ProviderConfig]) -> tuple[str, ...]:
    from .. import account_limiter

    errors: list[str] = []
    for provider in providers:
        parsed = account_limiter.parse_rate_limit(provider.image_rate_limit)
        if parsed.state == "invalid":
            errors.append(
                f"provider {provider.name}: invalid image_rate_limit "
                f"{provider.image_rate_limit!r}: {parsed.reason}"
            )
        daily_quota = provider.image_daily_quota
        if daily_quota is not None and (
            isinstance(daily_quota, bool)
            or not isinstance(daily_quota, int)
            or daily_quota <= 0
        ):
            errors.append(
                f"provider {provider.name}: invalid image_daily_quota "
                f"{daily_quota!r}"
            )
    return tuple(errors)


class ProviderConfigLoadingMixin:
    async def _load_provider_config(self) -> ProviderLoad:
        from .. import runtime_settings

        resolution = await runtime_settings.resolve_state("providers")
        if resolution.state == "unavailable":
            return ProviderLoad(
                "unavailable",
                errors=("providers setting unavailable",),
            )

        if resolution.state == "missing":
            legacy = build_legacy_provider(
                base_url=(
                    os.environ.get("UPSTREAM_BASE_URL")
                    or DEFAULT_LEGACY_PROVIDER_BASE_URL
                ),
                api_key=os.environ.get("UPSTREAM_API_KEY"),
            )
            if legacy is None:
                return ProviderLoad("explicit_empty")
            provider_defs = [legacy]
            proxy_defs: list[ProviderProxyDefinition] = []
        else:
            raw = resolution.value
            if raw is None or raw.strip() == "":
                return ProviderLoad(
                    "invalid",
                    errors=("providers setting is empty",),
                )
            provider_defs, proxy_defs, errors = parse_provider_config_json(raw)
            if errors:
                return ProviderLoad("invalid", errors=tuple(errors))
            if not provider_defs:
                return ProviderLoad(
                    "explicit_empty",
                    proxies={proxy.name: proxy for proxy in proxy_defs},
                )

        providers = [_runtime_provider(definition) for definition in provider_defs]
        errors = _quota_errors(providers)
        if errors:
            return ProviderLoad("invalid", errors=errors)
        return ProviderLoad(
            "configured",
            providers=tuple(providers),
            proxies={proxy.name: proxy for proxy in proxy_defs},
        )

    async def _load_config(self) -> list[ProviderConfig]:
        load = await self._load_provider_config()
        if load.state == "configured":
            return list(load.providers)
        if load.state == "explicit_empty":
            return []
        self._raise_provider_load_error(load)
        raise AssertionError("unreachable")

    @staticmethod
    def _raise_provider_load_error(load: ProviderLoad) -> None:
        if load.state == "unavailable":
            raise UpstreamError(
                "provider control plane unavailable and LKG expired",
                error_code="provider_control_plane_unavailable",
                status_code=503,
                payload={"errors": list(load.errors), "retryable": True},
            )
        raise UpstreamError(
            "provider configuration is invalid",
            error_code="provider_configuration_invalid",
            status_code=503,
            payload={"errors": list(load.errors), "retryable": False},
        )

    def _raise_cached_config_error(self) -> None:
        if self._config_source not in {"invalid", "unavailable"}:
            return
        self._raise_provider_load_error(
            ProviderLoad(
                self._config_source,
                errors=((self._config_error or self._config_source),),
            )
        )

    def _clear_provider_config(self) -> None:
        self._providers = []
        self._proxies = {}
        self._health.clear()
        self._rr_state.clear()

    async def _validate_provider_load(
        self,
        load: ProviderLoad,
    ) -> ProviderLoad:
        validated: list[ProviderConfig] = []
        errors: list[str] = []
        for provider in load.providers:
            try:
                url = await self._validate_provider_base_url(provider.base_url)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"provider {provider.name}: invalid base URL: {exc}")
                continue
            validated.append(_validated_runtime_provider(provider, url))
        if errors:
            return ProviderLoad("invalid", errors=tuple(errors))
        return ProviderLoad(
            "configured",
            providers=tuple(validated),
            proxies=dict(load.proxies),
        )

    def _apply_provider_load(self, load: ProviderLoad, *, now: float) -> None:
        new_providers = list(load.providers)
        old_names = set(self._health)
        new_names = {provider.name for provider in new_providers}
        for removed in old_names - new_names:
            del self._health[removed]
        for name in new_names - old_names:
            self._health[name] = ProviderHealth()
        for priority, state in list(self._rr_state.items()):
            for name in list(state):
                if name not in new_names:
                    del state[name]
            if not state:
                del self._rr_state[priority]

        changed = [provider.name for provider in self._providers] != [
            provider.name for provider in new_providers
        ]
        self._providers = new_providers
        self._proxies = dict(load.proxies)
        self._config_loaded_at = now
        self._config_last_good_at = now
        self._config_error = None
        self._config_source = (
            "authoritative_empty"
            if load.state == "explicit_empty"
            else "database_or_env"
        )
        if changed and new_providers:
            desc = ", ".join(
                f"{provider.name}(p={provider.priority},w={provider.weight},"
                f"proxy={provider.proxy_name or 'none'})"
                for provider in new_providers
            )
            logger.info("provider_pool reloaded: providers=[%s]", desc)

    def _use_lkg_or_raise(self, load: ProviderLoad, *, now: float) -> None:
        age = (
            float("inf")
            if self._config_last_good_at is None
            else now - self._config_last_good_at
        )
        if self._config_last_good_at is not None and age <= _CONFIG_LKG_MAX_STALE_S:
            self._config_error = f"control_plane_unavailable; lkg_age={age:.1f}s"
            self._config_source = "lkg"
            self._config_loaded_at = now - _CONFIG_TTL_S + _CONFIG_UNAVAILABLE_RETRY_S
            logger.error(
                "provider config unavailable; bounded LKG in use age=%.1fs",
                age,
            )
            return
        self._config_error = "; ".join(load.errors)
        self._config_source = "unavailable"
        self._config_loaded_at = now
        self._raise_provider_load_error(load)

    async def _maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self._config_loaded_at < _CONFIG_TTL_S:
            self._raise_cached_config_error()
            return
        async with self._config_lock:
            if now - self._config_loaded_at < _CONFIG_TTL_S:
                self._raise_cached_config_error()
                return
            load = await self._load_provider_config()
            if load.state == "unavailable":
                self._use_lkg_or_raise(load, now=now)
                return
            if load.state == "invalid":
                self._install_invalid_load(load, now=now)
            if load.state == "configured":
                load = await self._validate_provider_load(load)
                if load.state == "invalid":
                    self._install_invalid_load(load, now=now)
            self._apply_provider_load(load, now=now)

    def _install_invalid_load(self, load: ProviderLoad, *, now: float) -> None:
        self._clear_provider_config()
        self._config_last_good_at = None
        self._config_error = "; ".join(load.errors)
        self._config_source = "invalid"
        self._config_loaded_at = now
        self._raise_provider_load_error(load)

    def config_status(self) -> dict[str, Any]:
        now = time.monotonic()
        lkg_age = (
            None
            if self._config_last_good_at is None
            else max(0.0, now - self._config_last_good_at)
        )
        ready = self._config_source not in {"invalid", "unavailable"}
        if self._config_source == "lkg" and (
            lkg_age is None or lkg_age > _CONFIG_LKG_MAX_STALE_S
        ):
            ready = False
        return {
            "ready": ready,
            "source": self._config_source,
            "config_error": self._config_error,
            "lkg_age_seconds": lkg_age,
        }


__all__ = ["ProviderConfigLoadingMixin", "ProviderLoad"]
