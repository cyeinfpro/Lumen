"""「上游计费可知性 → 本地计费动作」统一决策表。

纯转嫁是业务铁律：**平台绝不吸收上游成本**。只要上游有可能已经扣费，本地
就必须结算（settle）而不是释放（release）；只有能够*证明*上游没有产生费用
时才允许 release。反过来，能证明上游未扣费的场景也不得按上限扣费，否则就是
多收用户的钱。

在此之前这套判断散落在各调用点的 if/else 里，同时在两个方向上出错：
- submit 结果不可知时默认 release（平台吸收上游成本）；
- 明确未扣费的场景却落进「按预估上限扣费」分支（多扣用户）。

收敛到这张表之后，调用点只负责提供**证据**（上游 billable 标志 + 收据原因 +
是否拿到实际用量），动作由表决定，任何新的调用点都自动继承同一套语义。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .constants import GenerationErrorCode

GENERATION_TAKEOVER_CHECKPOINT_KEY = "generation_takeover_checkpoint"
UPSTREAM_DISPATCH_STARTED_AT = "upstream_dispatch_started_at"
UPSTREAM_RESPONSE_RECEIVED_AT = "upstream_response_received_at"
UPSTREAM_DISPATCH_ATTEMPT = "upstream_dispatch_attempt"
UPSTREAM_RESPONSE_ATTEMPT = "upstream_response_attempt"
UPSTREAM_DISPATCH_EXECUTION_EPOCH = "upstream_dispatch_execution_epoch"
UPSTREAM_RESPONSE_EXECUTION_EPOCH = "upstream_response_execution_epoch"
UPSTREAM_DISPATCH_DELIVERY = "upstream_dispatch_delivery"
UPSTREAM_DISPATCH_PROVEN_UNDELIVERED = "proven_undelivered"
UPSTREAM_DISPATCH_PROVEN_NO_COST = "proven_no_cost"
PROVIDER_IDEMPOTENCY_KEY = "provider_idempotency_key"
PROVIDER_IDEMPOTENCY_STABLE = "provider_idempotency_stable"
UPSTREAM_TRACE_ID = "trace_id"
UPSTREAM_SIDECAR_EXECUTION = "sidecar_execution"

_UPSTREAM_EXECUTION_RECEIPT_KEYS = frozenset(
    {
        UPSTREAM_DISPATCH_STARTED_AT,
        UPSTREAM_RESPONSE_RECEIVED_AT,
        UPSTREAM_DISPATCH_ATTEMPT,
        UPSTREAM_RESPONSE_ATTEMPT,
        UPSTREAM_DISPATCH_EXECUTION_EPOCH,
        UPSTREAM_RESPONSE_EXECUTION_EPOCH,
        UPSTREAM_DISPATCH_DELIVERY,
    }
)

_UPSTREAM_EXECUTION_IDENTITY_KEYS = frozenset(
    {
        PROVIDER_IDEMPOTENCY_KEY,
        PROVIDER_IDEMPOTENCY_STABLE,
        UPSTREAM_TRACE_ID,
        UPSTREAM_SIDECAR_EXECUTION,
        GENERATION_TAKEOVER_CHECKPOINT_KEY,
        "execution_epoch",
        "provider",
        "actual_provider",
        "request_event_provider",
        "actual_route",
        "actual_endpoint",
        "actual_source",
        "upstream_route",
        "provider_attempts",
        "route_diagnostics",
        "generation_diagnostics",
        "upstream_duration_ms",
        "safe_error_summary",
    }
)
_UPSTREAM_EXECUTION_IDENTITY_PREFIXES = ("image_job_",)


class UpstreamCostKnowledge(StrEnum):
    """我们对「上游到底扣没扣费」这件事的确知程度。"""

    # 有确凿收据证明上游根本没有产生费用（请求还没发出去、提交前就取消等）。
    PROVEN_ABSENT = "proven_absent"
    # 上游明确告知已经计费，或已经返回了成功结果。
    PROVEN_PRESENT = "proven_present"
    # 不可知：连接中断、网关超时、上游标志不可信。默认按「已扣费」处理。
    UNKNOWN = "unknown"


class LocalBillingAction(StrEnum):
    """决策表的输出：本地钱包该做什么。"""

    # 释放 hold，用户不付钱。仅在 PROVEN_ABSENT 下允许。
    RELEASE = "release"
    # 按上游返回的真实用量结算，全额转嫁。
    SETTLE_ACTUAL = "settle_actual"
    # 拿不到真实用量时按默认金额（hold 与预估的较大者）结算。
    SETTLE_DEFAULT = "settle_default"


# 能够证明「上游未产生费用」的收据原因白名单。
#
# 为什么是白名单而不是黑名单：上游的 billable=False 标志本身并不可信（E-1），
# 只有当我们自己在本地留下了"请求根本没到达上游"的证据时，才敢 release。
# 白名单之外的一切原因——哪怕上游说没扣费——都按 UNKNOWN 处理并结算。
NO_UPSTREAM_COST_RECEIPTS: frozenset[str] = frozenset(
    {
        # 持久化 dispatch 收据已明确证明请求未送达或送达后上游未产生费用。
        UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
        UPSTREAM_DISPATCH_PROVEN_NO_COST,
        # 提交前用户主动取消，HTTP 请求尚未发出。
        "pre_submit_cancel",
        # 提交前任务已过期，同上。
        "pre_submit_expired",
        # 截止时间在提交前就到了。
        "deadline_expired_before_submit",
        # 提交失败且失败点明确早于上游计费（例如本地参数校验、DNS 解析失败）。
        "submit_failed_before_upstream_cost",
        # 图片生成走到 failed 终态：适配层与 image-job sidecar 都只在能判定
        # 「上游没有交付且没有计费」时才写 failed，可能已扣费的场景一律另走
        # IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES。
        "image_failed_before_upstream_cost",
    }
)

# 图片失败路径里代表「上游已经产生成本」的 generation error_code。
#
# 判据不是字面的「结果不可知」，而是**失败点在上游 2xx 之后**：
#   - DIRECT_IMAGE_RESULT_UNKNOWN：direct 请求超时，无法确认上游是否已出图；
#   - IMAGE_JOB_RESULT_UNKNOWN：image-job sidecar 的 uncertain 终态；
#   - NO_IMAGE_RETURNED：上游明确回了 2xx 却没交付图片。
#
# 最后一个是后补的，此前一直被当成普通失败，同时踩了纯转嫁的三条线：可重试
# （第二笔上游成本）、可 failover 换供应商（第三笔）、失败后 release（平台吸收
# 第一笔）。它比另外两个**更确定**上游已扣费——响应确实到达了，上游确实处理完
# 了请求。需要说明的是它不会误吞内容审核类拒绝：上游给出明确错误码时抛的是那个
# 码（见 image_stream.py 的 ``upstream_code or NO_IMAGE_RETURNED`` 回落），只有
# 上游什么都没说、就是不给图时才落到这里。
#
# 命中即禁止 release，也禁止自动重试 / failover 换供应商。
IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES: frozenset[str] = frozenset(
    {
        GenerationErrorCode.DIRECT_IMAGE_RESULT_UNKNOWN.value,
        GenerationErrorCode.IMAGE_JOB_RESULT_UNKNOWN.value,
        GenerationErrorCode.NO_IMAGE_RETURNED.value,
    }
)

# 图片 failed 终态使用的本地收据原因（见 NO_UPSTREAM_COST_RECEIPTS 注释）。
IMAGE_FAILED_BEFORE_UPSTREAM_COST = "image_failed_before_upstream_cost"


@dataclass(frozen=True)
class UpstreamBillingDecision:
    """决策表的一行结果：可知性 + 动作。"""

    knowledge: UpstreamCostKnowledge
    action: LocalBillingAction

    @property
    def released(self) -> bool:
        return self.action is LocalBillingAction.RELEASE


def upstream_request_dict(task_or_request: object) -> dict[str, object]:
    request = getattr(task_or_request, "upstream_request", task_or_request)
    return dict(request) if isinstance(request, dict) else {}


def task_execution_epoch(task_or_request: object) -> int | None:
    raw = getattr(task_or_request, "execution_epoch", None)
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _normalized_nonnegative_int(value: object) -> int | None:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _receipt_matches_execution(
    task_or_request: object,
    *,
    marker_key: str,
    epoch_key: str,
    execution_epoch: int | None,
) -> bool:
    request = upstream_request_dict(task_or_request)
    marker = request.get(marker_key)
    if not isinstance(marker, str) or not marker.strip():
        return False
    expected_epoch = (
        task_execution_epoch(task_or_request)
        if execution_epoch is None
        else _normalized_nonnegative_int(execution_epoch)
    )
    if expected_epoch is None:
        return True
    marker_epoch = _normalized_nonnegative_int(request.get(epoch_key))
    if marker_epoch is None:
        # Legacy receipts predate durable execution epochs. They are valid only
        # for the initial epoch; any manual retry advances the row beyond them.
        return expected_epoch == 0
    return marker_epoch == expected_epoch


def clear_upstream_execution_receipts(
    task_or_request: object,
) -> dict[str, object]:
    request = upstream_request_dict(task_or_request)
    for key in _UPSTREAM_EXECUTION_RECEIPT_KEYS:
        request.pop(key, None)
    return request


def clear_upstream_execution_state(
    task_or_request: object,
) -> dict[str, object]:
    """Remove receipts and provider identity before a new manual execution."""

    request = clear_upstream_execution_receipts(task_or_request)
    for key in (
        "billing_pricing_snapshot",
        "billing_rate_multiplier_x10000",
        "billing_admission_billable",
        "billing_admission_source",
        "billing_admission_ref_id",
        "billing_free",
        "billing_label",
        "billing_exempt_reason",
        "bonus_billing_obligation",
        "billing_obligation_state",
        "billing_obligation_terminal_at",
        "billing_obligation_terminal_reason",
    ):
        request.pop(key, None)
    for key in tuple(request):
        if key in _UPSTREAM_EXECUTION_IDENTITY_KEYS or key.startswith(
            _UPSTREAM_EXECUTION_IDENTITY_PREFIXES
        ):
            request.pop(key, None)
    return request


def mark_upstream_dispatch_started(
    task_or_request: object,
    *,
    at: str,
    attempt: int,
    execution_epoch: int | None = None,
) -> dict[str, object]:
    request = upstream_request_dict(task_or_request)
    request[UPSTREAM_DISPATCH_STARTED_AT] = at
    request[UPSTREAM_DISPATCH_ATTEMPT] = max(0, int(attempt))
    epoch = (
        task_execution_epoch(task_or_request)
        if execution_epoch is None
        else _normalized_nonnegative_int(execution_epoch)
    )
    if epoch is not None:
        request[UPSTREAM_DISPATCH_EXECUTION_EPOCH] = epoch
    request.pop(UPSTREAM_DISPATCH_DELIVERY, None)
    return request


def mark_upstream_dispatch_proven_undelivered(
    task_or_request: object,
    *,
    at: str,
    attempt: int,
    execution_epoch: int | None = None,
) -> dict[str, object]:
    request = mark_upstream_dispatch_started(
        task_or_request,
        at=at,
        attempt=attempt,
        execution_epoch=execution_epoch,
    )
    request[UPSTREAM_DISPATCH_DELIVERY] = UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
    request.pop(UPSTREAM_RESPONSE_RECEIVED_AT, None)
    request.pop(UPSTREAM_RESPONSE_ATTEMPT, None)
    request.pop(UPSTREAM_RESPONSE_EXECUTION_EPOCH, None)
    return request


def mark_upstream_dispatch_proven_no_cost(
    task_or_request: object,
    *,
    at: str,
    attempt: int,
    execution_epoch: int | None = None,
) -> dict[str, object]:
    request = mark_upstream_dispatch_started(
        task_or_request,
        at=at,
        attempt=attempt,
        execution_epoch=execution_epoch,
    )
    request[UPSTREAM_DISPATCH_DELIVERY] = UPSTREAM_DISPATCH_PROVEN_NO_COST
    request.pop(UPSTREAM_RESPONSE_RECEIVED_AT, None)
    request.pop(UPSTREAM_RESPONSE_ATTEMPT, None)
    request.pop(UPSTREAM_RESPONSE_EXECUTION_EPOCH, None)
    return request


def mark_upstream_response_received(
    task_or_request: object,
    *,
    at: str,
    attempt: int,
    execution_epoch: int | None = None,
) -> dict[str, object]:
    request = mark_upstream_dispatch_started(
        task_or_request,
        at=at,
        attempt=attempt,
        execution_epoch=execution_epoch,
    )
    request[UPSTREAM_RESPONSE_RECEIVED_AT] = at
    request[UPSTREAM_RESPONSE_ATTEMPT] = max(0, int(attempt))
    epoch = (
        task_execution_epoch(task_or_request)
        if execution_epoch is None
        else _normalized_nonnegative_int(execution_epoch)
    )
    if epoch is not None:
        request[UPSTREAM_RESPONSE_EXECUTION_EPOCH] = epoch
    return request


def has_upstream_dispatch_receipt(
    task_or_request: object,
    *,
    execution_epoch: int | None = None,
) -> bool:
    return _receipt_matches_execution(
        task_or_request,
        marker_key=UPSTREAM_DISPATCH_STARTED_AT,
        epoch_key=UPSTREAM_DISPATCH_EXECUTION_EPOCH,
        execution_epoch=execution_epoch,
    )


def has_upstream_response_receipt(
    task_or_request: object,
    *,
    execution_epoch: int | None = None,
) -> bool:
    return _receipt_matches_execution(
        task_or_request,
        marker_key=UPSTREAM_RESPONSE_RECEIVED_AT,
        epoch_key=UPSTREAM_RESPONSE_EXECUTION_EPOCH,
        execution_epoch=execution_epoch,
    )


def has_proven_undelivered_dispatch(
    task_or_request: object,
    *,
    execution_epoch: int | None = None,
) -> bool:
    request = upstream_request_dict(task_or_request)
    return bool(
        has_upstream_dispatch_receipt(
            task_or_request,
            execution_epoch=execution_epoch,
        )
        and request.get(UPSTREAM_DISPATCH_DELIVERY)
        == UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
    )


def has_proven_no_cost_dispatch(
    task_or_request: object,
    *,
    execution_epoch: int | None = None,
) -> bool:
    request = upstream_request_dict(task_or_request)
    return bool(
        has_upstream_dispatch_receipt(
            task_or_request,
            execution_epoch=execution_epoch,
        )
        and request.get(UPSTREAM_DISPATCH_DELIVERY) == UPSTREAM_DISPATCH_PROVEN_NO_COST
    )


def has_stable_provider_idempotency_key(task_or_request: object) -> bool:
    request = upstream_request_dict(task_or_request)
    key = request.get(PROVIDER_IDEMPOTENCY_KEY)
    stable = request.get(PROVIDER_IDEMPOTENCY_STABLE)
    return isinstance(key, str) and bool(key.strip()) and stable is True


def upstream_dispatch_can_replay(
    task_or_request: object,
    *,
    execution_epoch: int | None = None,
) -> bool:
    if not has_upstream_dispatch_receipt(
        task_or_request,
        execution_epoch=execution_epoch,
    ):
        return True
    return (
        has_proven_undelivered_dispatch(
            task_or_request,
            execution_epoch=execution_epoch,
        )
        or has_proven_no_cost_dispatch(
            task_or_request,
            execution_epoch=execution_epoch,
        )
        or has_stable_provider_idempotency_key(task_or_request)
    )


def upstream_dispatch_result_unknown(
    task_or_request: object,
    *,
    execution_epoch: int | None = None,
) -> bool:
    return bool(
        has_upstream_dispatch_receipt(
            task_or_request,
            execution_epoch=execution_epoch,
        )
        and not upstream_dispatch_can_replay(
            task_or_request,
            execution_epoch=execution_epoch,
        )
    )


def receipt_execution_identity(
    task_or_request: object,
    *,
    response: bool = False,
) -> tuple[int | None, int | None]:
    request = upstream_request_dict(task_or_request)
    attempt_key = UPSTREAM_RESPONSE_ATTEMPT if response else UPSTREAM_DISPATCH_ATTEMPT
    epoch_key = (
        UPSTREAM_RESPONSE_EXECUTION_EPOCH
        if response
        else UPSTREAM_DISPATCH_EXECUTION_EPOCH
    )
    return (
        _normalized_nonnegative_int(request.get(attempt_key)),
        _normalized_nonnegative_int(request.get(epoch_key)),
    )


def is_no_upstream_cost_receipt(reason: str | None) -> bool:
    """判断单个收据原因是否属于「可证明上游未扣费」白名单。"""
    if not isinstance(reason, str):
        return False
    return reason.strip() in NO_UPSTREAM_COST_RECEIPTS


def classify_upstream_cost(
    *,
    upstream_billable: bool | None,
    receipt_reasons: Iterable[str | None] = (),
) -> UpstreamCostKnowledge:
    """把调用点收集到的证据归类成可知性。

    ``upstream_billable`` 是上游/适配层给出的三态标志：True=已计费、
    False=声称未计费、None=不可知。``receipt_reasons`` 是本地留下的收据原因
    （任意一条命中白名单即可）。

    注意 False 单独出现**不足以** release——必须同时有本地收据佐证，
    否则退回 UNKNOWN 走结算。这正是 E-1 要求的「反转信任模型」。
    """
    if upstream_billable is True:
        return UpstreamCostKnowledge.PROVEN_PRESENT
    if upstream_billable is False and any(
        is_no_upstream_cost_receipt(reason) for reason in receipt_reasons
    ):
        return UpstreamCostKnowledge.PROVEN_ABSENT
    return UpstreamCostKnowledge.UNKNOWN


def resolve_billing_action(
    knowledge: UpstreamCostKnowledge,
    *,
    actual_cost_known: bool,
) -> LocalBillingAction:
    """决策表本体。

    | 可知性          | 拿到真实用量 | 动作            |
    |-----------------|--------------|-----------------|
    | PROVEN_ABSENT   | 任意         | RELEASE         |
    | PROVEN_PRESENT  | 是           | SETTLE_ACTUAL   |
    | PROVEN_PRESENT  | 否           | SETTLE_DEFAULT  |
    | UNKNOWN         | 是           | SETTLE_ACTUAL   |
    | UNKNOWN         | 否           | SETTLE_DEFAULT  |

    UNKNOWN 与 PROVEN_PRESENT 输出相同动作是**刻意**的：不可知时默认按已扣费
    处理，把不确定性的成本留给对账而不是留给平台。
    """
    if knowledge is UpstreamCostKnowledge.PROVEN_ABSENT:
        return LocalBillingAction.RELEASE
    return (
        LocalBillingAction.SETTLE_ACTUAL
        if actual_cost_known
        else LocalBillingAction.SETTLE_DEFAULT
    )


def decide_upstream_billing(
    *,
    upstream_billable: bool | None,
    actual_cost_known: bool,
    receipt_reasons: Iterable[str | None] = (),
) -> UpstreamBillingDecision:
    """一步到位：证据进，动作出。调用点应当只用这个入口。"""
    knowledge = classify_upstream_cost(
        upstream_billable=upstream_billable,
        receipt_reasons=tuple(receipt_reasons),
    )
    return UpstreamBillingDecision(
        knowledge=knowledge,
        action=resolve_billing_action(knowledge, actual_cost_known=actual_cost_known),
    )


def decide_dispatch_evidence_billing(
    task_or_request: object,
    *,
    actual_cost_known: bool,
    execution_epoch: int | None = None,
) -> UpstreamBillingDecision:
    """Resolve billing from durable dispatch evidence for one execution.

    A response or ambiguous dispatch may have incurred provider cost, so both
    fail closed to settlement. Release is allowed only before dispatch or when
    durable delivery evidence proves the request was undelivered/no-cost.
    """

    if has_proven_undelivered_dispatch(
        task_or_request,
        execution_epoch=execution_epoch,
    ):
        return decide_upstream_billing(
            upstream_billable=False,
            actual_cost_known=actual_cost_known,
            receipt_reasons=(UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,),
        )
    if has_proven_no_cost_dispatch(
        task_or_request,
        execution_epoch=execution_epoch,
    ):
        return decide_upstream_billing(
            upstream_billable=False,
            actual_cost_known=actual_cost_known,
            receipt_reasons=(UPSTREAM_DISPATCH_PROVEN_NO_COST,),
        )
    if has_upstream_response_receipt(
        task_or_request,
        execution_epoch=execution_epoch,
    ) or has_upstream_dispatch_receipt(
        task_or_request,
        execution_epoch=execution_epoch,
    ):
        return decide_upstream_billing(
            upstream_billable=None,
            actual_cost_known=actual_cost_known,
        )
    return decide_upstream_billing(
        upstream_billable=False,
        actual_cost_known=actual_cost_known,
        receipt_reasons=("submit_failed_before_upstream_cost",),
    )


def decide_image_failure_billing(
    error_code: str | None,
    *,
    task_or_request: object | None = None,
    execution_epoch: int | None = None,
) -> UpstreamBillingDecision:
    """图片生成失败时该 release 还是 settle。

    error code 只能描述失败类型，不能证明请求是否到达或上游是否计费。只有
    当前 execution 没有 dispatch 收据，或已有 durable 的 undelivered/no-cost
    收据时才允许释放 hold；其余一律按 UNKNOWN 默认结算。
    """
    if task_or_request is not None:
        if has_proven_undelivered_dispatch(
            task_or_request,
            execution_epoch=execution_epoch,
        ):
            return decide_upstream_billing(
                upstream_billable=False,
                actual_cost_known=False,
                receipt_reasons=(UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,),
            )
        if has_proven_no_cost_dispatch(
            task_or_request,
            execution_epoch=execution_epoch,
        ):
            return decide_upstream_billing(
                upstream_billable=False,
                actual_cost_known=False,
                receipt_reasons=(UPSTREAM_DISPATCH_PROVEN_NO_COST,),
            )
    if (error_code or "").strip() in IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES:
        return decide_upstream_billing(
            upstream_billable=None,
            actual_cost_known=False,
        )
    if task_or_request is not None:
        if not has_upstream_dispatch_receipt(
            task_or_request,
            execution_epoch=execution_epoch,
        ):
            return decide_upstream_billing(
                upstream_billable=False,
                actual_cost_known=False,
                receipt_reasons=(IMAGE_FAILED_BEFORE_UPSTREAM_COST,),
            )
    return decide_upstream_billing(
        upstream_billable=None,
        actual_cost_known=False,
    )


__all__ = [
    "GENERATION_TAKEOVER_CHECKPOINT_KEY",
    "IMAGE_FAILED_BEFORE_UPSTREAM_COST",
    "IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES",
    "NO_UPSTREAM_COST_RECEIPTS",
    "PROVIDER_IDEMPOTENCY_KEY",
    "PROVIDER_IDEMPOTENCY_STABLE",
    "UPSTREAM_DISPATCH_ATTEMPT",
    "UPSTREAM_DISPATCH_DELIVERY",
    "UPSTREAM_DISPATCH_EXECUTION_EPOCH",
    "UPSTREAM_DISPATCH_PROVEN_UNDELIVERED",
    "UPSTREAM_DISPATCH_PROVEN_NO_COST",
    "UPSTREAM_DISPATCH_STARTED_AT",
    "UPSTREAM_RESPONSE_ATTEMPT",
    "UPSTREAM_RESPONSE_EXECUTION_EPOCH",
    "UPSTREAM_RESPONSE_RECEIVED_AT",
    "UPSTREAM_SIDECAR_EXECUTION",
    "UPSTREAM_TRACE_ID",
    "LocalBillingAction",
    "UpstreamBillingDecision",
    "UpstreamCostKnowledge",
    "classify_upstream_cost",
    "clear_upstream_execution_receipts",
    "clear_upstream_execution_state",
    "decide_dispatch_evidence_billing",
    "decide_image_failure_billing",
    "decide_upstream_billing",
    "has_proven_undelivered_dispatch",
    "has_proven_no_cost_dispatch",
    "has_stable_provider_idempotency_key",
    "has_upstream_dispatch_receipt",
    "has_upstream_response_receipt",
    "is_no_upstream_cost_receipt",
    "mark_upstream_dispatch_started",
    "mark_upstream_dispatch_proven_undelivered",
    "mark_upstream_dispatch_proven_no_cost",
    "mark_upstream_response_received",
    "receipt_execution_identity",
    "resolve_billing_action",
    "task_execution_epoch",
    "upstream_dispatch_can_replay",
    "upstream_dispatch_result_unknown",
    "upstream_request_dict",
]
