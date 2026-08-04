"""Durable sidecar execution and billing boundary helpers."""

from __future__ import annotations

from typing import Any

from lumen_core.upstream_billing import (
    decide_dispatch_evidence_billing,
    receipt_execution_identity,
)

from ...provider_runtime.errors import UpstreamError
from ...upstream_clients.image_job_models import (
    ImageJobCostKnowledge,
    ImageJobExecutionHandle,
    ImageJobResultState,
)


SIDECAR_EXECUTION_KEY = "sidecar_execution"
SIDECAR_EXECUTIONS_KEY = "sidecar_executions"


def _execution_lane_order(execution: ImageJobExecutionHandle) -> tuple[int, str]:
    endpoint_order = {"generations": 0, "responses": 1}
    return (
        endpoint_order.get(execution.endpoint, 2),
        execution.endpoint,
    )


def _preferred_sidecar_execution(
    executions: tuple[ImageJobExecutionHandle, ...],
) -> ImageJobExecutionHandle | None:
    if not executions:
        return None
    recovery_order = {"deliver": 0, "poll": 1, "terminal": 2}
    cost_order = {
        ImageJobCostKnowledge.INCURRED: 0,
        ImageJobCostKnowledge.UNKNOWN: 1,
        ImageJobCostKnowledge.NONE: 2,
    }
    return min(
        executions,
        key=lambda execution: (
            recovery_order[execution.recovery_outcome.value],
            cost_order[execution.cost_knowledge],
            *_execution_lane_order(execution),
        ),
    )


def sidecar_executions_from_request(
    upstream_request: dict[str, Any] | None,
) -> tuple[ImageJobExecutionHandle, ...]:
    if not isinstance(upstream_request, dict):
        return ()
    by_endpoint: dict[str, ImageJobExecutionHandle] = {}
    legacy = ImageJobExecutionHandle.from_mapping(
        upstream_request.get(SIDECAR_EXECUTION_KEY)
    )
    if legacy is not None:
        by_endpoint[legacy.endpoint] = legacy
    raw_executions = upstream_request.get(SIDECAR_EXECUTIONS_KEY)
    values = (
        raw_executions.values()
        if isinstance(raw_executions, dict)
        else raw_executions
        if isinstance(raw_executions, (list, tuple))
        else ()
    )
    for raw_execution in values:
        execution = ImageJobExecutionHandle.from_mapping(raw_execution)
        if execution is not None:
            by_endpoint[execution.endpoint] = execution
    return tuple(sorted(by_endpoint.values(), key=_execution_lane_order))


def upsert_sidecar_execution(
    upstream_request: dict[str, Any] | None,
    execution: ImageJobExecutionHandle,
) -> dict[str, Any]:
    request = dict(upstream_request or {})
    by_endpoint = {
        existing.endpoint: existing
        for existing in sidecar_executions_from_request(request)
    }
    by_endpoint[execution.endpoint] = execution
    executions = tuple(sorted(by_endpoint.values(), key=_execution_lane_order))
    preferred = _preferred_sidecar_execution(executions)
    if preferred is not None:
        request[SIDECAR_EXECUTION_KEY] = preferred.to_dict()
    if len(executions) > 1:
        request[SIDECAR_EXECUTIONS_KEY] = {
            item.endpoint: item.to_dict() for item in executions
        }
    else:
        request.pop(SIDECAR_EXECUTIONS_KEY, None)
    return request


def sidecar_execution_from_request(
    upstream_request: dict[str, Any] | None,
) -> ImageJobExecutionHandle | None:
    return _preferred_sidecar_execution(
        sidecar_executions_from_request(upstream_request)
    )


def _normalized_sidecar_endpoint(value: Any) -> str:
    endpoint = str(value or "").strip()
    return endpoint.removeprefix("image-jobs:")


def _execution_has_terminal_cost(
    execution: ImageJobExecutionHandle,
) -> bool:
    return bool(
        execution.cost_knowledge
        in {
            ImageJobCostKnowledge.UNKNOWN,
            ImageJobCostKnowledge.INCURRED,
        }
        and (
            execution.cancel_outcome is not None
            or execution.result_state != ImageJobResultState.PENDING
        )
    )


def dual_race_bonus_execution_from_request(
    upstream_request: dict[str, Any] | None,
    *,
    winner_endpoint: str | None,
) -> ImageJobExecutionHandle | None:
    executions = sidecar_executions_from_request(upstream_request)
    if len(executions) < 2:
        return None
    cancelled = tuple(
        execution
        for execution in executions
        if execution.cancel_outcome is not None
        and _execution_has_terminal_cost(execution)
    )
    if cancelled:
        return min(cancelled, key=_execution_lane_order)
    normalized_winner = _normalized_sidecar_endpoint(winner_endpoint)
    if not normalized_winner:
        return None
    candidates = tuple(
        execution
        for execution in executions
        if execution.endpoint != normalized_winner
        and _execution_has_terminal_cost(execution)
    )
    return min(candidates, key=_execution_lane_order) if candidates else None


def sidecar_cost_requires_settlement(
    upstream_request: dict[str, Any] | None,
) -> bool:
    return any(
        execution.cost_knowledge
        in {
            ImageJobCostKnowledge.UNKNOWN,
            ImageJobCostKnowledge.INCURRED,
        }
        for execution in sidecar_executions_from_request(upstream_request)
    )


async def release_or_settle_generation(
    billing: Any,
    session: Any,
    generation: Any,
    *,
    reason: str,
) -> None:
    executions = sidecar_executions_from_request(
        getattr(generation, "upstream_request", None)
    )
    settlement_execution = next(
        (
            execution
            for knowledge in (
                ImageJobCostKnowledge.INCURRED,
                ImageJobCostKnowledge.UNKNOWN,
            )
            for execution in executions
            if execution.cost_knowledge == knowledge
        ),
        None,
    )
    if settlement_execution is not None:
        await billing.settle_unknown_upstream(
            session,
            generation,
            reason=reason,
            knowledge=settlement_execution.cost_knowledge.value,
        )
        return
    decision = decide_dispatch_evidence_billing(
        generation,
        actual_cost_known=False,
    )
    if not decision.released:
        await billing.settle_unknown_upstream(
            session,
            generation,
            reason=reason,
            knowledge=decision.knowledge.value,
        )
        return
    await billing.release(session, generation, reason=reason)


def _current_dispatch_has_response(generation: Any) -> bool:
    """当前 dispatch 是否已收到上游明确应答。

    response 收据必须与 dispatch 收据属于同一次 attempt/epoch 才算数：同 epoch
    内更早 attempt（重试）留下的 response 收据不代表当前请求已有答案，结果仍然
    不可知。
    """
    dispatch_attempt, dispatch_epoch = receipt_execution_identity(generation)
    response_attempt, response_epoch = receipt_execution_identity(
        generation,
        response=True,
    )
    return bool(
        dispatch_attempt is not None
        and response_attempt == dispatch_attempt
        and response_epoch == dispatch_epoch
    )


def unknown_generation_requires_settlement(generation: Any) -> bool:
    executions = sidecar_executions_from_request(
        getattr(generation, "upstream_request", None)
    )
    if any(
        execution.cost_knowledge
        in {
            ImageJobCostKnowledge.UNKNOWN,
            ImageJobCostKnowledge.INCURRED,
        }
        for execution in executions
    ):
        return True
    return _current_dispatch_has_response(generation)


def release_would_absorb_upstream_cost(
    exc: BaseException | None,
    generation: Any,
) -> bool:
    """决策表判 release 时，是否仍会把已发生的上游成本退还给用户。

    决策表的 release 前提是「非 IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES 的错误码 =
    适配层已证明上游未计费」。该前提只对适配层抛出的 UpstreamError 成立；本地
    失败（DB/存储等，例如 artifact commit 未被采纳）发生在上游 2xx 之后，当前
    dispatch 的响应收据说明图片已经产出、上游已经扣费，此时必须结算而不是
    release，否则平台吸收这笔成本（纯转嫁铁律）。sidecar 执行的成本可知性由
    release_or_settle_generation 内部分支决定，不在此处拦截。
    """
    if isinstance(exc, UpstreamError):
        return False
    executions = sidecar_executions_from_request(
        getattr(generation, "upstream_request", None)
    )
    if executions:
        return False
    return _current_dispatch_has_response(generation)


__all__ = [
    "SIDECAR_EXECUTION_KEY",
    "SIDECAR_EXECUTIONS_KEY",
    "dual_race_bonus_execution_from_request",
    "release_or_settle_generation",
    "release_would_absorb_upstream_cost",
    "sidecar_cost_requires_settlement",
    "sidecar_execution_from_request",
    "sidecar_executions_from_request",
    "unknown_generation_requires_settlement",
    "upsert_sidecar_execution",
]
