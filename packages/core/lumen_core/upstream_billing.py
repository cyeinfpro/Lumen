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

UPSTREAM_DISPATCH_STARTED_AT = "upstream_dispatch_started_at"
UPSTREAM_RESPONSE_RECEIVED_AT = "upstream_response_received_at"


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


def mark_upstream_dispatch_started(
    task_or_request: object,
    *,
    at: str,
    attempt: int,
) -> dict[str, object]:
    request = upstream_request_dict(task_or_request)
    request[UPSTREAM_DISPATCH_STARTED_AT] = at
    request["upstream_dispatch_attempt"] = max(0, int(attempt))
    return request


def mark_upstream_response_received(
    task_or_request: object,
    *,
    at: str,
    attempt: int,
) -> dict[str, object]:
    request = mark_upstream_dispatch_started(
        task_or_request,
        at=at,
        attempt=attempt,
    )
    request[UPSTREAM_RESPONSE_RECEIVED_AT] = at
    request["upstream_response_attempt"] = max(0, int(attempt))
    return request


def has_upstream_response_receipt(task_or_request: object) -> bool:
    value = upstream_request_dict(task_or_request).get(UPSTREAM_RESPONSE_RECEIVED_AT)
    return isinstance(value, str) and bool(value.strip())


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


def decide_image_failure_billing(error_code: str | None) -> UpstreamBillingDecision:
    """图片生成失败时该 release 还是 settle。

    上游结果不可知或已在 2xx 后失败时按 UNKNOWN 走默认结算；只有适配层能
    证明失败发生在上游计费前，才允许释放 hold。
    """
    if (error_code or "").strip() in IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES:
        return decide_upstream_billing(
            upstream_billable=None,
            actual_cost_known=False,
        )
    return decide_upstream_billing(
        upstream_billable=False,
        actual_cost_known=False,
        receipt_reasons=(IMAGE_FAILED_BEFORE_UPSTREAM_COST,),
    )


__all__ = [
    "IMAGE_FAILED_BEFORE_UPSTREAM_COST",
    "IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES",
    "NO_UPSTREAM_COST_RECEIPTS",
    "UPSTREAM_DISPATCH_STARTED_AT",
    "UPSTREAM_RESPONSE_RECEIVED_AT",
    "LocalBillingAction",
    "UpstreamBillingDecision",
    "UpstreamCostKnowledge",
    "classify_upstream_cost",
    "decide_image_failure_billing",
    "decide_upstream_billing",
    "has_upstream_response_receipt",
    "is_no_upstream_cost_receipt",
    "mark_upstream_dispatch_started",
    "mark_upstream_response_received",
    "resolve_billing_action",
    "upstream_request_dict",
]
