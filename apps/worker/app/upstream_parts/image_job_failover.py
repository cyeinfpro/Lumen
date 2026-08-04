"""Endpoint and provider failover for asynchronous image-job requests."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from lumen_core.providers import ProviderProxyDefinition
from lumen_core.upstream_billing import IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from ..upstream_clients.image_job_models import (
    ImageJobCostKnowledge,
    ImageJobExecutionHandle,
)
from .delivery_evidence import transport_error_proves_undelivered
from .image_execution import (
    ImageExecutionRequest,
    ImageResult,
)


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def _image_jobs_endpoint_fallback_chain(primary: str) -> list[str]:
    if primary == "generations":
        return ["generations", "responses"]
    if primary == "responses":
        return ["responses", "generations"]
    return ["generations", "responses"]


def _image_job_error_class(exc: BaseException) -> str | None:
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        error_class = payload.get("image_job_error_class")
        return error_class if isinstance(error_class, str) else None
    return None


def _sidecar_execution_from_error(
    exc: BaseException,
) -> ImageJobExecutionHandle | None:
    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict):
        return None
    return ImageJobExecutionHandle.from_mapping(payload.get("sidecar_execution"))


def _sidecar_execution_accepted(exc: BaseException) -> bool:
    payload = getattr(exc, "payload", None)
    return bool(
        isinstance(payload, dict)
        and (
            payload.get("sidecar_execution_accepted") is True
            or _sidecar_execution_from_error(exc) is not None
        )
    )


def _accepted_execution_result_unknown(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> BaseException:
    services = _runtime_services(runtime)
    if (
        isinstance(exc, services.infrastructure.UpstreamError)
        and (exc.error_code or "") in IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES
    ):
        return exc
    execution = _sidecar_execution_from_error(exc)
    if (
        execution is None
        or execution.cost_knowledge == ImageJobCostKnowledge.NONE
    ):
        return exc
    payload = (
        dict(exc.payload)
        if isinstance(exc, services.infrastructure.UpstreamError)
        and isinstance(exc.payload, dict)
        else {}
    )
    payload.update(
        {
            "upstream_result_unknown": True,
            "sidecar_execution_accepted": True,
            "sidecar_execution": execution.to_dict(),
            "recovery_only": execution.recovery_outcome.value != "terminal",
            "wrapped_error_code": getattr(exc, "error_code", None),
        }
    )
    error = services.infrastructure.UpstreamError(
        "image job execution was accepted, but its result state is unknown; "
        "recover the original execution instead of submitting another job",
        status_code=getattr(exc, "status_code", None),
        error_code=services.infrastructure.EC.IMAGE_JOB_RESULT_UNKNOWN.value,
        payload=payload,
    )
    error.__cause__ = exc
    return error


def submit_failure_result_unknown(exc: BaseException) -> bool:
    """image-job 提交失败是否「结果未知」（可能已扣费）。

    换 endpoint 时 body 变、幂等键跟着变，sidecar 无法去重，重跑就是第二笔
    上游成本。只有 connect 类错误能证明请求从未到达 sidecar（与 runner 层
    proven-undelivered 白名单一致）；读超时/连接重置发生在请求发出之后，
    可能已被 sidecar 接受并创建作业，一律按未知处理。5xx 同理：sidecar 已
    应答（无论是否带响应体，请求都可能已被转发并让真实上游计费），结果
    不可知；只有 4xx / 408 / 425 / 429 这类显式拒绝能证明未产生上游成本。
    """
    if getattr(exc, "operation", None) != "submit":
        return False
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and status_code > 0:
        if 300 <= status_code < 500:
            return False
        # 1xx / 2xx 畸形响应或任意 5xx 都无法证明 sidecar 未创建真实上游作业。
        # 即使 body 为空、非 JSON，收到响应本身也只证明请求到达了 sidecar。
        return True
    if transport_error_proves_undelivered(exc.__cause__):
        return False
    if getattr(exc, "result_unknown", False):
        return True
    if not getattr(exc, "transient", False):
        return False
    return True


def image_job_submit_unknown_error(
    exc: BaseException,
    *,
    method: str,
    url: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> BaseException:
    """把提交结果未知的失败转成 IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES 一员。

    与 direct 路径把读超时映射成 DIRECT_IMAGE_RESULT_UNKNOWN 一致：后续的
    endpoint/provider failover、is_retriable、计费决策全部自动继承「禁止重跑、
    按 hold 结算」语义，不需要每一层单独判断。
    """
    services = _runtime_services(runtime)
    return services.infrastructure.UpstreamError(
        (
            "image job submit result is unknown; the request may already "
            "have been accepted and billed upstream"
        ),
        status_code=getattr(exc, "status_code", None) or 0,
        error_code=services.infrastructure.EC.IMAGE_JOB_RESULT_UNKNOWN.value,
        payload={
            "path": "image-jobs",
            "method": method,
            "url": url,
            "operation": getattr(exc, "operation", "submit"),
            "upstream_result_unknown": True,
        },
    )


def _upstream_cost_already_incurred(
    exc: BaseException,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    """这次失败是否（或可能）已经产生上游成本。

    命中后**任何**形式的重跑都必须停：换 endpoint 是同一家供应商的第二次调用，
    换 provider 是另一家的第一次调用——两者都会新增一笔上游成本，而这次生成只
    hold 了一份钱，多出来的部分无处转嫁，只能由平台吸收。

    覆盖两类失败：
    - 失败点在上游 2xx 之后（结果确定不可知：超时/uncertain/no_image）；
    - image-job 提交阶段网络错（读超时/连接重置）：请求可能已被 sidecar 接受并
      创建作业，换 endpoint 会带新幂等键创建第二个作业，同样禁止重跑
      （见 ``image_job_submit_unknown_error`` 的 IMAGE_JOB_RESULT_UNKNOWN 映射，
      与 lumen_core.upstream_billing 的 IMAGE_UPSTREAM_RESULT_UNKNOWN 对齐）。
    """
    services = _runtime_services(runtime)
    execution = _sidecar_execution_from_error(exc)
    return submit_failure_result_unknown(exc) or (
        isinstance(exc, services.infrastructure.UpstreamError)
        and (exc.error_code or "") in IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES
    ) or bool(
        execution is not None
        and execution.cost_knowledge != ImageJobCostKnowledge.NONE
    ) or bool(
        execution is None and _sidecar_execution_accepted(exc)
    )


def _should_continue_image_job_failover(
    exc: BaseException,
    *,
    retriable: bool,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    """Return whether endpoint or provider failover can recover the job."""
    services = _runtime_services(runtime)
    if services.providers.is_quota_accounting_unavailable(exc):
        return False
    if _sidecar_execution_accepted(exc):
        return False
    # 必须在 `retriable` 之前：成本已经发生这件事不该被「可重试」翻盘。
    if _upstream_cost_already_incurred(exc, runtime=runtime):
        return False
    if retriable:
        return True
    error_class = services.image_jobs.image_job_error_class(exc)
    if error_class in services.core.IMAGE_JOB_FAILOVER_CLASSES:
        return True
    if isinstance(exc, services.infrastructure.UpstreamError):
        if exc.status_code == 429:
            return True
        if exc.status_code is not None and 500 <= exc.status_code < 600:
            return True
        # NO_IMAGE_RETURNED 已由上面的「上游已产生成本」守卫拦下，不再列在这里。
        if exc.error_code in {
            services.infrastructure.EC.UPSTREAM_TIMEOUT.value,
            services.infrastructure.EC.TIMEOUT.value,
            services.infrastructure.EC.DIRECT_IMAGE_REQUEST_FAILED.value,
        }:
            return True
    return isinstance(exc, services.core.RETRY_HTTPX_EXC)


async def _image_job_run_once(
    request: ImageExecutionRequest,
    *,
    endpoint: str,
    api_key: str,
    base_url: str,
    proxy: ProviderProxyDefinition | None,
    image_edit_input_transport: str = "url",
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, str | None]:
    """Dispatch one sidecar request by action and endpoint kind."""
    services = _runtime_services(request.upstream_runtime)
    common: dict[str, Any] = {
        "api_key_override": api_key,
        "provider_id": str(getattr(request.provider_override, "name", "") or ""),
        "endpoint": endpoint,
        "base_url_override": base_url or None,
        "before_attempt": before_attempt,
    }
    if proxy is not None:
        common["proxy_override"] = proxy
    if endpoint == "responses":
        return await services.image_jobs.image_job_responses_once(
            request,
            **common,
        )
    if request.action == "edit":
        if not request.images:
            raise services.infrastructure.UpstreamError(
                "edit action requires at least one reference image",
                error_code=services.infrastructure.EC.MISSING_INPUT_IMAGES.value,
                status_code=400,
            )
        return await services.image_jobs.image_job_edit_once(
            request,
            image_edit_input_transport=image_edit_input_transport,
            **common,
        )
    return await services.image_jobs.image_job_generate_once(request, **common)


@dataclass(frozen=True)
class _ImageJobProviderPlan:
    endpoints: tuple[str, ...]
    base_url: str
    error: BaseException | None = None


@dataclass(frozen=True)
class _ImageJobAttemptFailure:
    error: BaseException
    error_class: str | None
    decision_reason: str
    failover_reason: str
    retriable: bool
    duration_ms: float


@dataclass(frozen=True)
class _ImageJobProviderOutcome:
    result: ImageResult | None = None
    error: BaseException | None = None


def _requested_image_job_kind(
    endpoint_override: str | None,
    endpoint_preference: str | None,
) -> str | None:
    if endpoint_override in ("generations", "responses"):
        return endpoint_override
    if endpoint_preference in ("generations", "responses"):
        return endpoint_preference
    return None


def _provider_image_job_plan(
    provider: Any,
    *,
    action: str,
    pool: Any,
    fallback_base_url: str,
    endpoint_override: str | None,
    endpoint_preference: str | None,
    runtime: ImageUpstreamRuntime | None = None,
) -> _ImageJobProviderPlan:
    services = _runtime_services(runtime)
    configured = getattr(provider, "image_jobs_endpoint", "auto")
    try:
        endpoint_locked = services.infrastructure.parse_provider_bool(
            getattr(provider, "image_jobs_endpoint_lock", False),
            default=False,
        )
    except ValueError:
        endpoint_locked = False
    endpoint_locked = endpoint_locked and configured in ("generations", "responses")
    requested_kind = _requested_image_job_kind(
        endpoint_override,
        endpoint_preference,
    )
    if requested_kind is not None:
        unavailable = services.providers.provider_endpoint_unavailable_error(
            provider,
            requested_kind,
        )
        if unavailable is not None:
            services.infrastructure.logger.info(
                "image_jobs skip provider=%s configured=%s requested_kind=%s reason=%s",
                getattr(provider, "name", "unknown"),
                configured,
                requested_kind,
                unavailable.payload.get("reason"),
            )
            return _ImageJobProviderPlan((), "", unavailable)
    if endpoint_override is not None:
        endpoints = [endpoint_override]
    elif endpoint_locked:
        endpoints = [configured]
    elif endpoint_preference is not None:
        endpoints = services.image_jobs.image_jobs_endpoint_fallback_chain(
            endpoint_preference
        )
    else:
        endpoints = pool.endpoint_chain(provider.name, action, configured)
    allowed = tuple(
        endpoint
        for endpoint in endpoints
        if services.providers.provider_allows_image_endpoint(
            provider, endpoint
        )
    )
    if not allowed:
        error = services.infrastructure.UpstreamError(
            f"provider {provider.name} has no supported image-job endpoint",
            error_code=services.infrastructure.EC.NO_PROVIDERS.value,
            status_code=503,
            payload={
                "provider": provider.name,
                "reason": "capability_unsupported",
            },
        )
        return _ImageJobProviderPlan((), "", error)
    raw_base_url = getattr(provider, "image_jobs_base_url", "") or fallback_base_url
    try:
        base_url = services.requests.validate_image_job_base_url(
            raw_base_url
        )
    except services.infrastructure.UpstreamError as exc:
        return _ImageJobProviderPlan((), "", exc)
    return _ImageJobProviderPlan(allowed, base_url)


def _classify_image_job_attempt(
    exc: BaseException,
    *,
    duration_ms: float,
    runtime: ImageUpstreamRuntime | None = None,
) -> _ImageJobAttemptFailure:
    from ..retry import is_retriable as classify_retriable

    decision = classify_retriable(
        getattr(exc, "error_code", None),
        getattr(exc, "status_code", None),
        error_message=str(exc),
    )
    services = _runtime_services(runtime)
    error_class = services.image_jobs.image_job_error_class(exc)
    return _ImageJobAttemptFailure(
        error=exc,
        error_class=error_class,
        decision_reason=decision.reason,
        failover_reason=error_class or decision.reason,
        retriable=decision.retriable,
        duration_ms=duration_ms,
    )


async def _emit_image_job_success(
    request: ImageExecutionRequest,
    *,
    provider: Any,
    provider_index: int,
    endpoint: str,
    endpoint_index: int,
    latency_ms: float,
    pool: Any,
    inflight_endpoint_kind: str | None,
    source_label: str,
) -> None:
    services = _runtime_services(request.upstream_runtime)
    if not services.providers.is_byok_provider(provider):
        pool.record_endpoint_success(
            provider.name,
            endpoint,
            latency_ms=latency_ms,
        )
        services.providers.pool_report_image_success(
            pool,
            provider.name,
            endpoint_kind=inflight_endpoint_kind,
            record_endpoint=False,
        )
    await services.transport.emit_image_progress(
        request.progress_callback,
        "provider_used",
        provider=provider.name,
        route="image_jobs",
        source=source_label,
        endpoint=f"image-jobs:{endpoint}",
        **services.providers.provider_attempt_context(
            provider,
            attempt=provider_index + 1,
            endpoint_attempt=endpoint_index + 1,
            duration_ms=latency_ms,
            status="succeeded",
        ),
    )
    await services.transport.emit_image_progress(
        request.progress_callback,
        "final_image",
        source=source_label,
        endpoint_used=endpoint,
    )
    await services.transport.emit_image_progress(
        request.progress_callback,
        "completed",
        source=source_label,
        endpoint_used=endpoint,
    )


async def _run_image_job_endpoint(
    request: ImageExecutionRequest,
    *,
    provider: Any,
    provider_index: int,
    endpoint: str,
    endpoint_index: int,
    plan: _ImageJobProviderPlan,
    pool: Any,
    inflight_endpoint_kind: str | None,
    source_label: str,
) -> ImageResult | _ImageJobAttemptFailure:
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    started = time.monotonic()
    # 每次 submit 尝试前预留账号配额槽位；失败/重试必须释放（见 claim 的
    # docstring），否则每次 submit 重试都会永久占用一个账号当日配额。
    claim, release_failed_attempts = (
        services.providers.image_request_attempt_claim(
            pool,
            provider,
            route=f"image_jobs:{endpoint}",
            request_context=request.request_context,
        )
    )
    try:
        result = await services.image_jobs.image_job_run_once(
            request.with_provider(provider),
            endpoint=endpoint,
            api_key=provider.api_key,
            base_url=plan.base_url,
            proxy=services.core.provider_proxy(provider),
            image_edit_input_transport=provider.image_edit_input_transport,
            before_attempt=claim,
        )
    except (
        asyncio.CancelledError,
        services.infrastructure.UpstreamCancelled,
    ):
        raise
    except Exception as exc:  # noqa: BLE001
        exc = _accepted_execution_result_unknown(exc, runtime=runtime)
        if not _upstream_cost_already_incurred(exc, runtime=runtime):
            # 失败未产生上游成本（非 result-unknown）：释放本次尝试链的全部
            # 预留。结果未知类失败（可能已在上游创建作业/扣费）保留预留。
            await release_failed_attempts()
        if not services.providers.is_byok_provider(provider):
            pool.record_endpoint_failure(provider.name, endpoint)
        failure = _classify_image_job_attempt(
            exc,
            duration_ms=(time.monotonic() - started) * 1000,
            runtime=runtime,
        )
        services.infrastructure.logger.warning(
            "image job %s/%s endpoint=%s error_class=%s decision=%s: %r",
            request.action,
            provider.name,
            endpoint,
            failure.error_class,
            failure.decision_reason,
            exc,
        )
        return failure
    # 成功：保留最后一次尝试的预留作为入账记录，释放链上更早的失败尝试。
    await release_failed_attempts(keep_last=True)
    latency_ms = (time.monotonic() - started) * 1000.0
    await _emit_image_job_success(
        request,
        provider=provider,
        provider_index=provider_index,
        endpoint=endpoint,
        endpoint_index=endpoint_index,
        latency_ms=latency_ms,
        pool=pool,
        inflight_endpoint_kind=inflight_endpoint_kind,
        source_label=source_label,
    )
    return result


async def _emit_image_job_endpoint_failover(
    request: ImageExecutionRequest,
    failure: _ImageJobAttemptFailure,
    *,
    provider: Any,
    provider_index: int,
    endpoint: str,
    endpoint_index: int,
    remaining: int,
) -> None:
    services = _runtime_services(request.upstream_runtime)
    await services.transport.emit_image_progress(
        request.progress_callback,
        "endpoint_failover",
        provider=provider.name,
        from_endpoint=endpoint,
        remaining=remaining,
        reason=failure.failover_reason,
        route="image_jobs",
        **services.providers.provider_attempt_context(
            provider,
            attempt=provider_index + 1,
            endpoint_attempt=endpoint_index + 1,
            duration_ms=failure.duration_ms,
            status="failed",
            reason=failure.failover_reason,
            exc=failure.error,
        ),
    )


async def _run_image_job_provider(
    request: ImageExecutionRequest,
    *,
    provider: Any,
    provider_index: int,
    plan: _ImageJobProviderPlan,
    pool: Any,
    inflight_endpoint_kind: str | None,
    source_label: str,
) -> _ImageJobProviderOutcome:
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    for endpoint_index, endpoint in enumerate(plan.endpoints):
        outcome = await _run_image_job_endpoint(
            request,
            provider=provider,
            provider_index=provider_index,
            endpoint=endpoint,
            endpoint_index=endpoint_index,
            plan=plan,
            pool=pool,
            inflight_endpoint_kind=inflight_endpoint_kind,
            source_label=source_label,
        )
        if not isinstance(outcome, _ImageJobAttemptFailure):
            return _ImageJobProviderOutcome(result=outcome)
        remaining = len(plan.endpoints) - endpoint_index - 1
        # endpoint 切换此前是无条件的：只要还有下一个 endpoint 就重跑，不看错误
        # 类型。上游已回 2xx 时这等于在同一家供应商上再买一次，第二笔无处转嫁。
        if remaining > 0 and not _upstream_cost_already_incurred(
            outcome.error,
            runtime=runtime,
        ):
            await _emit_image_job_endpoint_failover(
                request,
                outcome,
                provider=provider,
                provider_index=provider_index,
                endpoint=endpoint,
                endpoint_index=endpoint_index,
                remaining=remaining,
            )
            continue
        provider_retriable = (
            services.retry.should_continue_image_provider_failover(
                outcome.error,
                retriable=outcome.retriable,
                runtime=runtime,
            )
        )
        if not services.image_jobs.should_continue_image_job_failover(
            outcome.error,
            retriable=provider_retriable,
            runtime=runtime,
        ):
            raise outcome.error
        return _ImageJobProviderOutcome(error=outcome.error)
    raise RuntimeError("image job provider plan has no endpoints")


async def _record_image_job_provider_failure(
    request: ImageExecutionRequest,
    error: BaseException,
    *,
    provider: Any,
    provider_index: int,
    provider_count: int,
    pool: Any,
) -> None:
    services = _runtime_services(request.upstream_runtime)
    is_rate_limited, retry_after = (
        services.providers.is_image_rate_limit_error(error)
    )
    if not services.providers.is_byok_provider(provider):
        if is_rate_limited:
            pool.report_image_rate_limited(
                provider.name,
                retry_after_s=retry_after,
            )
        else:
            services.providers.pool_report_image_failure(pool, provider.name)
    remaining = provider_count - provider_index - 1
    if remaining <= 0:
        return
    services.infrastructure.logger.warning(
        "image job provider_failover: from=%s remaining=%d action=%s",
        provider.name,
        remaining,
        request.action,
    )
    await services.transport.emit_image_progress(
        request.progress_callback,
        "provider_failover",
        from_provider=provider.name,
        remaining=remaining,
        reason="image_job_failed",
        route="image_jobs",
        **services.providers.provider_attempt_context(
            provider,
            attempt=provider_index + 1,
            duration_ms=None,
            status="failed",
            reason="image_job_failed",
            exc=error,
        ),
    )


async def _image_job_with_failover(
    request: ImageExecutionRequest,
    *,
    endpoint_override: str | None = None,
    endpoint_preference: str | None = None,
) -> tuple[str, str | None]:
    """Fail over across image-job endpoints and providers."""
    runtime = request.upstream_runtime
    services = _runtime_services(runtime)
    services.image_jobs.image_job_sidecar_token()
    pool = await services.infrastructure.provider_pool.get_pool()
    forced_kind = _requested_image_job_kind(endpoint_override, endpoint_preference)
    lane_owns_inflight = request.provider_override is None
    providers = (
        [request.provider_override]
        if request.provider_override is not None
        else await services.providers.pool_select_compat(
            pool,
            route="image_jobs",
            ignore_cooldown=True,
            endpoint_kind=forced_kind,
            requires_mask=request.mask is not None,
        )
    )
    errors: list[BaseException] = []
    source_label = (
        "image_jobs" if request.action == "generate" else "image_jobs_edit"
    )
    fallback_base_url = ""
    if any(
        not str(getattr(provider, "image_jobs_base_url", "") or "").strip()
        for provider in providers
    ):
        fallback_base_url = (
            await services.direct.resolve_image_job_base_url()
        )

    for provider_index, provider in enumerate(providers):
        if lane_owns_inflight and provider_index > 0:
            services.providers.pool_acquire_inflight(
                pool, provider.name, forced_kind
            )
        try:
            plan = _provider_image_job_plan(
                provider,
                action=request.action,
                pool=pool,
                fallback_base_url=fallback_base_url,
                endpoint_override=endpoint_override,
                endpoint_preference=endpoint_preference,
                runtime=runtime,
            )
            if plan.error is not None:
                errors.append(plan.error)
                continue
            outcome = await _run_image_job_provider(
                request,
                provider=provider,
                provider_index=provider_index,
                plan=plan,
                pool=pool,
                inflight_endpoint_kind=forced_kind,
                source_label=source_label,
            )
            if outcome.result is not None:
                return outcome.result
            if outcome.error is None:
                continue
            errors.append(outcome.error)
            await _record_image_job_provider_failure(
                request,
                outcome.error,
                provider=provider,
                provider_index=provider_index,
                provider_count=len(providers),
                pool=pool,
            )
        finally:
            if lane_owns_inflight:
                services.providers.pool_release_inflight(
                    pool,
                    provider.name,
                    forced_kind,
                )

    merged = services.retry.merge_fallback_errors(
        errors,
        error_code=services.infrastructure.EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        message=f"all {len(providers)} image job providers failed",
        runtime=runtime,
    )
    merged.payload["provider_errors"] = (
        services.retry.provider_error_details(
            providers,
            errors,
            runtime=runtime,
        )
    )
    raise merged


__all__ = [
    "_image_job_error_class",
    "_sidecar_execution_accepted",
    "_image_job_run_once",
    "_image_job_with_failover",
    "_image_jobs_endpoint_fallback_chain",
    "_should_continue_image_job_failover",
]
