"""Explicit service groups used by worker upstream implementation modules."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterator

_INFRASTRUCTURE_NAMES = frozenset(
    {
        "EC",
        "PILImage",
        "PublicHttpBodyTooLarge",
        "UPSTREAM_MODEL",
        "UnidentifiedImageError",
        "UpstreamCancelled",
        "UpstreamError",
        "asyncio",
        "base64",
        "close_provider_proxy_tunnels",
        "download_public_http_url",
        "endpoint_kind_allowed",
        "hashlib",
        "httpx",
        "logger",
        "parse_provider_bool",
        "pinned_async_http_transport",
        "provider_pool",
        "provider_supports_route",
        "resolve",
        "resolve_provider_proxy_url",
        "resolve_public_http_target",
        "settings",
        "time",
        "upstream_image_requests",
        "validate_image_job_sidecar_token",
    }
)

_MODULE_GROUPS = (
    ("client_lifecycle", "lifecycle"),
    ("direct_failover", "direct"),
    ("direct_images", "direct"),
    ("direct_requests", "direct"),
    ("image_dispatch", "dispatch"),
    ("image_job_failover", "image_jobs"),
    ("image_jobs", "image_jobs"),
    ("image_race", "race"),
    ("image_stream", "responses"),
    ("provider_selection", "providers"),
    ("reference_images", "references"),
    ("request_targets", "requests"),
    ("responses", "responses"),
    ("responses_client", "responses"),
    ("retry_policy", "retry"),
    ("transport", "transport"),
)

_MODULE_BINDINGS = (
    ("upstream_client_lifecycle", "lifecycle"),
    ("upstream_direct_failover", "direct"),
    ("upstream_direct_images", "direct"),
    ("upstream_direct_requests", "direct"),
    ("upstream_image_dispatch", "dispatch"),
    ("upstream_image_job_failover", "image_jobs"),
    ("upstream_image_jobs", "image_jobs"),
    ("upstream_image_race", "race"),
    ("upstream_image_stream", "responses"),
    ("upstream_provider_selection", "providers"),
    ("upstream_reference_images", "references"),
    ("upstream_request_targets", "requests"),
    ("upstream_responses", "responses"),
    ("upstream_responses_client", "responses"),
    ("upstream_retry_policy", "retry"),
    ("upstream_transport", "transport"),
)

_REQUEST_SERVICE_EXPORTS = (
    "add_image_output_options",
    "append_transparent_matte_prompt",
    "apply_retry_cache_busters",
    "attach_image_idempotency_key",
    "build_responses_image_body",
    "image_file_fingerprints",
    "image_idempotency_key",
    "image_job_body_base",
    "image_job_payload",
    "is_transparent_image_request",
    "json_dumps_stable",
    "normalize_image_background",
    "normalize_image_moderation",
    "normalize_image_output_compression",
    "normalize_image_output_format",
    "normalize_image_quality",
    "parse_size_pixels",
    "transparent_matte_upstream_options",
    "wrap_inpaint_prompt",
)


@dataclass(frozen=True)
class UpstreamServices:
    infrastructure: SimpleNamespace
    core: SimpleNamespace
    lifecycle: SimpleNamespace
    direct: SimpleNamespace
    dispatch: SimpleNamespace
    image_jobs: SimpleNamespace
    providers: SimpleNamespace
    race: SimpleNamespace
    references: SimpleNamespace
    requests: SimpleNamespace
    responses: SimpleNamespace
    retry: SimpleNamespace
    transport: SimpleNamespace


@dataclass
class UpstreamLifecycleState:
    retired_client_close_tasks: set[Any]
    retired_clients: set[Any]

    @classmethod
    def create(cls) -> UpstreamLifecycleState:
        return cls(
            retired_client_close_tasks=set(),
            retired_clients=set(),
        )


_PROCESS_SERVICES: ContextVar[UpstreamServices | None] = ContextVar(
    "upstream_process_services",
    default=None,
)
_SERVICES_OVERRIDE: ContextVar[UpstreamServices | None] = ContextVar(
    "upstream_services_override",
    default=None,
)


def service_group_for(name: str, value: Any) -> str:
    if name in _INFRASTRUCTURE_NAMES:
        return "infrastructure"
    module_name = str(getattr(value, "__module__", ""))
    marker = ".upstream_parts."
    if marker in module_name:
        owner = module_name.rsplit(marker, 1)[1].split(".", 1)[0]
        group = next(
            (group for module, group in _MODULE_GROUPS if module == owner),
            None,
        )
        if group is not None:
            return group
    return "core"


def service_name(name: str) -> str:
    return name.lstrip("_")


def build_upstream_services(namespace: dict[str, Any]) -> UpstreamServices:
    groups: dict[str, SimpleNamespace] = {
        field: SimpleNamespace() for field in UpstreamServices.__dataclass_fields__
    }
    for name, value in namespace.items():
        if name.startswith("__"):
            continue
        group = service_group_for(name, value)
        setattr(groups[group], service_name(name), value)
    for binding, group in _MODULE_BINDINGS:
        module = namespace[binding]
        for name in module.__all__:
            setattr(groups[group], service_name(name), getattr(module, name))
    request_module = namespace["upstream_image_requests"]
    for name in _REQUEST_SERVICE_EXPORTS:
        setattr(groups["requests"], name, getattr(request_module, f"_{name}"))
    groups["responses"].responses_client_call = namespace[
        "upstream_responses_client"
    ].responses_call
    lifecycle_state = namespace["lifecycle_state"]
    groups[
        "core"
    ].retired_client_close_tasks = lifecycle_state.retired_client_close_tasks
    groups["core"].retired_clients = lifecycle_state.retired_clients
    return UpstreamServices(**groups)


def install_upstream_services(services: UpstreamServices) -> None:
    _PROCESS_SERVICES.set(services)


def upstream_services() -> UpstreamServices:
    services = _SERVICES_OVERRIDE.get() or _PROCESS_SERVICES.get()
    if services is None:
        raise RuntimeError("upstream services have not been composed")
    return services


def bind_upstream_services(
    services: UpstreamServices,
) -> Token[UpstreamServices | None]:
    return _SERVICES_OVERRIDE.set(services)


def reset_upstream_services(token: Token[UpstreamServices | None]) -> None:
    _SERVICES_OVERRIDE.reset(token)


@contextmanager
def use_upstream_services(
    services: UpstreamServices,
) -> Iterator[UpstreamServices]:
    token = bind_upstream_services(services)
    try:
        yield services
    finally:
        reset_upstream_services(token)


__all__ = [
    "UpstreamLifecycleState",
    "UpstreamServices",
    "bind_upstream_services",
    "build_upstream_services",
    "install_upstream_services",
    "reset_upstream_services",
    "service_group_for",
    "service_name",
    "upstream_services",
    "use_upstream_services",
]
