"""Explicit service groups used by worker upstream implementation modules."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType, SimpleNamespace
from typing import Any

_INFRASTRUCTURE_BINDING_NAMES = tuple(
    """
    EC PILImage PublicHttpBodyTooLarge UPSTREAM_MODEL
    UnidentifiedImageError UpstreamCancelled UpstreamError asyncio
    base64 close_provider_proxy_tunnels download_public_http_url
    download_public_http_url_to_file endpoint_kind_allowed hashlib httpx logger
    parse_provider_bool pinned_async_http_transport provider_pool
    provider_supports_route resolve resolve_provider_proxy_url
    resolve_public_http_target settings time upstream_image_requests
    validate_image_job_sidecar_token
    """.split()
)
_INFRASTRUCTURE_NAMES = frozenset(_INFRASTRUCTURE_BINDING_NAMES)

_CORE_BINDING_NAMES = tuple(
    """
    DEFAULT_RESOLVE_RUNTIME CURL_BIN DEFAULT_IMAGE_BACKGROUND
    DEFAULT_IMAGE_INSTRUCTIONS DEFAULT_IMAGE_JOB_BASE_URL
    DEFAULT_IMAGE_MODERATION DEFAULT_IMAGE_OUTPUT_COMPRESSION
    DEFAULT_IMAGE_OUTPUT_FORMAT DEFAULT_IMAGE_RESPONSES_MODEL
    DUAL_RACE_BONUS_GRACE_4K_S DUAL_RACE_BONUS_GRACE_S
    DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_4K_S
    DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S FALLBACK_429_DEFAULT_WAIT_S
    FALLBACK_429_MAX_WAIT_S FALLBACK_MAX_ATTEMPTS FALLBACK_MAX_ATTEMPTS_429
    FALLBACK_MAX_ATTEMPTS_4XX FALLBACK_MAX_ATTEMPTS_5XX
    FALLBACK_RETRY_BACKOFF_BASE_S FALLBACK_RETRY_BACKOFF_MAX_S
    FALLBACK_RETRY_ERROR_CODES IMAGE_4K_PIXELS IMAGE_BACKGROUNDS
    IMAGE_CHANNEL_AUTO IMAGE_CHANNEL_IMAGE_JOBS_ONLY IMAGE_CHANNEL_STREAM_ONLY
    IMAGE_JOB_DOWNLOAD_MAX_BYTES IMAGE_JOB_FAILOVER_CLASSES
    IMAGE_JOB_POLL_INTERVAL_S IMAGE_JOB_RETENTION_DAYS IMAGE_JOB_TIMEOUT_S
    IMAGE_MODERATIONS IMAGE_OUTPUT_FORMATS IMAGE_PROVIDER_FAILOVER_ERROR_CODES
    IMAGE_QUALITIES IMAGE_READ_TIMEOUT_4K_S IMAGE_READ_TIMEOUT_MIN_S
    IMAGE_ROUTE_DUAL_RACE IMAGE_ROUTE_IMAGE2 IMAGE_ROUTE_RESPONSES
    JSON_PAYLOAD_SENTINEL_TYPE KNOWN_OUTPUT_ITEM_TYPES
    MAX_REFERENCE_IMAGE_PIXELS MAX_NORMALIZED_IMAGE_BYTES
    MAX_REFERENCE_IMAGE_BYTES NON_SSE_JSON_MAX_BYTES PARTIAL_IMAGES_MAX_PIXELS
    PROXIED_CLIENT_CACHE_MAX PROXIED_CLIENT_CLOSE_DELAY_SECONDS
    PROXIED_CLIENT_IDLE_CLOSE_TIMEOUT_SECONDS RACE_CANCEL_WAIT_S
    RACE_SINGLE_LANE_PIXELS REFERENCE_CACHE_HEAD_TIMEOUT_S
    REFERENCE_CACHE_KEY_PREFIX REFERENCE_CACHE_LRU_SUFFIX
    REFERENCE_CACHE_MAX_ENTRIES REFERENCE_CACHE_TTL_S REFERENCE_PUSH_TIMEOUT_S
    RETRY_HTTPX_EXC RETRY_STATUS SAFETY_POLICY_ERROR_MARKERS SSE_MAX_BYTES
    SSE_MAX_LINES SSE_MAX_LINE_BYTES TEXT_STREAM_INTERRUPTED_ERROR_CODE
    add_image_output_options apply_retry_cache_busters
    attach_image_idempotency_key auth_headers b64_value_if_str client
    client_timeout_config configure_pil_max_image_pixels
    extract_image_b64_from_payload extract_image_billable_count
    extract_image_result extract_image_results extract_response_image_b64
    extract_response_revised_prompt has_explicit_image_dispatch_setting
    image_file_fingerprints image_idempotency_key image_request_policy
    images_client images_client_timeout_config generate_trace_id
    is_responses_error_terminal
    is_responses_success_terminal json_dumps_stable log_upstream_call
    normalize_image_background normalize_image_moderation
    normalize_image_output_compression normalize_image_output_format
    normalize_image_quality parse_error parse_retry_after_seconds
    post_with_retry provider_proxy proxied_clients proxied_images_clients
    record_usage resolve_image_channel resolve_image_engine
    resolve_legacy_image_primary_route resolve_runtime runtime_parts
    runtime_provider_name stable_sort_tools summarize_upstream_error_detail
    validate_responses_body
    with_error_context resolve_db resolve_image_primary_route responses_call
    tempfile
    """.split()
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

_RUNTIME_BINDINGS = MappingProxyType({
    "core": tuple(
        """
        add_image_output_options apply_retry_cache_busters
        attach_image_idempotency_key auth_headers
        b64_value_if_str extract_image_b64_from_payload
        extract_image_billable_count extract_image_result extract_image_results
        extract_response_image_b64 extract_response_revised_prompt
        has_explicit_image_dispatch_setting image_file_fingerprints
        image_idempotency_key image_request_policy
        json_dumps_stable normalize_image_background normalize_image_moderation
        normalize_image_output_compression normalize_image_output_format
        normalize_image_quality resolve_image_channel resolve_image_engine
        resolve_image_primary_route resolve_legacy_image_primary_route
        responses_call
        """.split()
    ),
    "direct": tuple(
        """
        direct_image_result_unknown_error download_result_url_bytes
        fetch_image_url_as_bytes image_request_timeout is_direct_image_result_unknown
        minimum_image_read_timeout resolve_image_job_base_url
        select_image_read_timeout wrap_inpaint_prompt
        """.split()
    ),
    "dispatch": tuple(
        """
        image_dispatch_candidates image_endpoint_kind_for_engine
        image_jobs_endpoint_for_engine is_image_job_configuration_error
        provider_supports_image_jobs should_use_image_jobs
        validate_selected_image_job_configuration
        """.split()
    ),
    "image_jobs": tuple(
        """
        build_image_job_client download_image_job_result image_job_body_base
        image_job_error image_job_payload image_job_reference_image_entries
        image_job_sidecar_token submit_and_wait_image_job
        should_continue_image_job_failover
        validate_effective_image_job_configuration
        """.split()
    ),
    "lifecycle": tuple(
        """
        build_client build_images_client cache_proxied_client close_client
        close_retired_clients_now delayed_aclose get_client get_images_client
        resolve_timeout_config schedule_delayed_aclose
        """.split()
    ),
    "retry": tuple(
        """
        fallback_retry_backoff_seconds is_retryable_fallback_exception
        max_attempts_for_exception mentions_safety_policy merge_fallback_errors
        merge_image_path_errors provider_error_details retry_after_seconds
        should_continue_image_provider_failover summarize_exception
        truncate_lane_summary
        """.split()
    ),
    "providers": tuple(
        """
        image_quota_claim image_request_attempt_claim is_image_rate_limit_error
        is_quota_accounting_unavailable pool_select_compat
        provider_allows_image_endpoint provider_attempt_context
        provider_capability_error provider_endpoint_locked_error
        provider_endpoint_unavailable_error record_admin_image_call_or_raise
        release_image_reservation_best_effort release_unused_image_reservation
        reserve_admin_image_call
        """.split()
    ),
    "references": tuple(
        """
        get_or_upload_reference normalize_reference_image
        push_reference_to_image_job reference_cache_delete reference_cache_get
        reference_cache_keys reference_cache_store reference_cache_trim
        reference_url_is_live resolve_reference_image_urls
        """.split()
    ),
    "requests": ("validate_image_job_base_url", "validated_byok_target_for_request"),
    "race": ("cancel_and_wait_tasks",),
    "responses": (
        "iter_sse",
        "iter_sse_with_runtime",
        "responses_call",
        "responses_client_call",
        "stream_completion",
    ),
    "transport": tuple(
        """
        curl_post_multipart curl_post_multipart_using_paths emit_image_progress
        iter_sse_curl maybe_record_usage_from_event stage_multipart_bytes_to_tmp
        """.split()
    ),
})

_REQUEST_SERVICE_EXPORTS = (
    "add_image_output_options",
    "apply_retry_cache_busters",
    "attach_image_idempotency_key",
    "build_responses_image_body",
    "image_file_fingerprints",
    "image_idempotency_key",
    "image_job_body_base",
    "image_job_payload",
    "json_dumps_stable",
    "normalize_image_background",
    "normalize_image_moderation",
    "normalize_image_output_compression",
    "normalize_image_output_format",
    "normalize_image_quality",
    "parse_size_pixels",
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


@dataclass(frozen=True, slots=True)
class ImageUpstreamRuntime:
    """Explicit service graph carried by production image requests."""

    services: UpstreamServices


@dataclass
class UpstreamLifecycleState:
    retired_client_close_tasks: set[Any]
    retired_clients: set[Any]
    client_lock: asyncio.Lock
    images_client_lock: asyncio.Lock

    @classmethod
    def create(cls) -> UpstreamLifecycleState:
        return cls(
            retired_client_close_tasks=set(),
            retired_clients=set(),
            client_lock=asyncio.Lock(),
            images_client_lock=asyncio.Lock(),
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


def compose_upstream_namespace(
    *,
    core_values: tuple[Any, ...],
    infrastructure_values: tuple[Any, ...],
    module_values: tuple[Any, ...],
    lifecycle_state: UpstreamLifecycleState,
) -> dict[str, Any]:
    """Bind the explicit upstream composition contract without module reflection."""

    module_names = tuple(binding for binding, _group in _MODULE_BINDINGS)
    groups = (
        ("core", _CORE_BINDING_NAMES, core_values),
        ("infrastructure", _INFRASTRUCTURE_BINDING_NAMES, infrastructure_values),
        ("module", module_names, module_values),
    )
    namespace: dict[str, Any] = {}
    for label, names, values in groups:
        if len(names) != len(values):
            raise RuntimeError(
                f"upstream {label} composition expected {len(names)} values, "
                f"received {len(values)}"
            )
        namespace.update(zip(names, values))
    namespace["lifecycle_state"] = lifecycle_state
    return namespace


def build_upstream_services(namespace: dict[str, Any]) -> UpstreamServices:
    missing = [
        name
        for name in ("settings", "upstream_image_requests")
        if name not in namespace
    ]
    missing.extend(
        binding for binding, _group in _MODULE_BINDINGS if binding not in namespace
    )
    if missing:
        names = ", ".join(sorted(missing))
        raise RuntimeError(
            f"upstream composition is missing required dependencies: {names}"
        )

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
    lifecycle_state = (
        namespace.get("lifecycle_state") or UpstreamLifecycleState.create()
    )
    groups[
        "core"
    ].retired_client_close_tasks = lifecycle_state.retired_client_close_tasks
    groups["core"].retired_clients = lifecycle_state.retired_clients
    groups["core"].client_lock = lifecycle_state.client_lock
    groups["core"].images_client_lock = lifecycle_state.images_client_lock
    return UpstreamServices(**groups)


def resolve_image_upstream_services(
    runtime: ImageUpstreamRuntime | None,
) -> UpstreamServices:
    if runtime is None:
        raise TypeError("ImageUpstreamRuntime is required")
    return runtime.services


def bind_upstream_runtime(runtime: ImageUpstreamRuntime) -> ImageUpstreamRuntime:
    """Bind runtime-aware service functions to one explicit runtime graph."""

    for group_name, names in _RUNTIME_BINDINGS.items():
        group = getattr(runtime.services, group_name)
        for name in names:
            setattr(group, name, partial(getattr(group, name), runtime=runtime))
    return runtime


__all__ = [
    "ImageUpstreamRuntime",
    "UpstreamLifecycleState",
    "UpstreamServices",
    "bind_upstream_runtime",
    "build_upstream_services",
    "compose_upstream_namespace",
    "resolve_image_upstream_services",
    "service_group_for",
    "service_name",
]
