"""Durable sidecar execution and billing boundary helpers."""

from __future__ import annotations

from typing import Any

from lumen_core.upstream_billing import (
    has_proven_undelivered_dispatch,
    has_upstream_dispatch_receipt,
    receipt_execution_identity,
)

from ...provider_runtime.errors import UpstreamError
from ...upstream_clients.image_job_models import (
    ImageJobCostKnowledge,
    ImageJobExecutionHandle,
)


SIDECAR_EXECUTION_KEY = "sidecar_execution"


def sidecar_execution_from_request(
    upstream_request: dict[str, Any] | None,
) -> ImageJobExecutionHandle | None:
    if not isinstance(upstream_request, dict):
        return None
    return ImageJobExecutionHandle.from_mapping(
        upstream_request.get(SIDECAR_EXECUTION_KEY)
    )


def sidecar_cost_requires_settlement(
    upstream_request: dict[str, Any] | None,
) -> bool:
    execution = sidecar_execution_from_request(upstream_request)
    return bool(
        execution is not None
        and execution.cost_knowledge
        in {
            ImageJobCostKnowledge.UNKNOWN,
            ImageJobCostKnowledge.INCURRED,
        }
    )


async def release_or_settle_generation(
    billing: Any,
    session: Any,
    generation: Any,
    *,
    reason: str,
) -> None:
    execution = sidecar_execution_from_request(
        getattr(generation, "upstream_request", None)
    )
    if execution is not None and execution.cost_knowledge in {
        ImageJobCostKnowledge.UNKNOWN,
        ImageJobCostKnowledge.INCURRED,
    }:
        await billing.settle_unknown_upstream(
            session,
            generation,
            reason=reason,
            knowledge=execution.cost_knowledge.value,
        )
        return
    # 直接引擎（images/responses 直连）没有 sidecar 执行句柄，但 dispatch 收据
    # 同样代表上游可能已经扣费：请求已发出、结果不可知时必须结算而不是释放，
    # 与 TaskDomainReconciler._settle_timeout_billing 的语义保持一致（纯转嫁铁律：
    # 只有能证明上游未产生费用的场景才允许 release）。
    #
    # 「结果不可知」仅指已派发但当前 dispatch 未收到任何应答（连接中断/超时等）。
    # 一旦收到明确应答（response 收据与 dispatch 同 attempt/epoch），失败语义就由
    # 上游计费决策表 decide_image_failure_billing 裁定：非
    # IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES 的错误码意味着适配层已证明上游未计费
    # （PROVEN_ABSENT）→ 必须 release；否则把用户 hold 全额扣掉就是多收用户的钱，
    # 正是决策表注释里列明要避免的方向。
    if execution is None:
        execution_epoch = getattr(generation, "execution_epoch", None)
        if (
            has_upstream_dispatch_receipt(
                generation,
                execution_epoch=execution_epoch,
            )
            and not has_proven_undelivered_dispatch(
                generation,
                execution_epoch=execution_epoch,
            )
            and not _current_dispatch_has_response(generation)
        ):
            await billing.settle_unknown_upstream(
                session,
                generation,
                reason=reason,
                knowledge="unknown",
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
    execution = sidecar_execution_from_request(
        getattr(generation, "upstream_request", None)
    )
    if execution is not None:
        return False
    return _current_dispatch_has_response(generation)


__all__ = [
    "SIDECAR_EXECUTION_KEY",
    "release_or_settle_generation",
    "release_would_absorb_upstream_cost",
    "sidecar_cost_requires_settlement",
    "sidecar_execution_from_request",
]
