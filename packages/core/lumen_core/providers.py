"""Compatibility facade for shared provider configuration and runtime helpers."""

from __future__ import annotations

from .providers_parts.config import (
    build_effective_provider_config,
    build_effective_providers,
    build_legacy_provider,
    normalize_image_edit_input_transport,
    parse_provider_bool,
    parse_provider_config_json,
    parse_provider_item,
    parse_provider_json,
    parse_proxy_item,
    parse_proxy_json,
)
from .providers_parts.definitions import (
    DEFAULT_IMAGE_EDIT_INPUT_TRANSPORT,
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    IMAGE_EDIT_INPUT_TRANSPORT_VALUES,
    ProviderDefinition,
    ProviderProxyDefinition,
    RoundRobinState,
)
from .providers_parts.proxy_runtime import (
    close_provider_proxy_tunnels,
    resolve_provider_proxy_url,
    socks_proxy_url,
)
from .providers_parts.selection import (
    advance_round_robin_counter,
    endpoint_kind_allowed,
    has_embedding_purpose,
    weighted_priority_order,
    weighted_priority_order_and_advance,
)


__all__ = [
    "DEFAULT_IMAGE_EDIT_INPUT_TRANSPORT",
    "DEFAULT_LEGACY_PROVIDER_BASE_URL",
    "IMAGE_EDIT_INPUT_TRANSPORT_VALUES",
    "ProviderDefinition",
    "ProviderProxyDefinition",
    "RoundRobinState",
    "advance_round_robin_counter",
    "build_effective_provider_config",
    "build_effective_providers",
    "build_legacy_provider",
    "close_provider_proxy_tunnels",
    "endpoint_kind_allowed",
    "has_embedding_purpose",
    "normalize_image_edit_input_transport",
    "parse_provider_bool",
    "parse_provider_config_json",
    "parse_provider_item",
    "parse_provider_json",
    "parse_proxy_item",
    "parse_proxy_json",
    "resolve_provider_proxy_url",
    "socks_proxy_url",
    "weighted_priority_order",
    "weighted_priority_order_and_advance",
]
