from __future__ import annotations

import pytest

from lumen_core.constants import GenerationErrorCode as EC
from lumen_core.upstream_billing import (
    IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES,
    LocalBillingAction,
    UpstreamCostKnowledge,
    classify_upstream_cost,
    decide_image_failure_billing,
    decide_upstream_billing,
    has_upstream_response_receipt,
    is_no_upstream_cost_receipt,
    mark_upstream_dispatch_started,
    mark_upstream_response_received,
    resolve_billing_action,
)


def test_billable_true_is_proven_present() -> None:
    # 上游明说扣了费——无论有没有本地收据，都不许 release。
    assert (
        classify_upstream_cost(upstream_billable=True)
        is UpstreamCostKnowledge.PROVEN_PRESENT
    )
    assert (
        classify_upstream_cost(
            upstream_billable=True,
            receipt_reasons=("pre_submit_cancel",),
        )
        is UpstreamCostKnowledge.PROVEN_PRESENT
    )


def test_upstream_response_receipt_requires_a_positive_response_marker() -> None:
    request = mark_upstream_dispatch_started(
        {},
        at="2026-07-26T00:00:00+00:00",
        attempt=1,
    )
    assert has_upstream_response_receipt(request) is False

    request = mark_upstream_response_received(
        request,
        at="2026-07-26T00:00:01+00:00",
        attempt=1,
    )
    assert has_upstream_response_receipt(request) is True
    assert request["upstream_response_attempt"] == 1


def test_billable_false_without_local_receipt_stays_unknown() -> None:
    # E-1：上游的 billable=False 单独不可信，必须有本地收据佐证。
    assert (
        classify_upstream_cost(upstream_billable=False) is UpstreamCostKnowledge.UNKNOWN
    )
    assert (
        classify_upstream_cost(
            upstream_billable=False,
            receipt_reasons=("upstream_said_so", None, ""),
        )
        is UpstreamCostKnowledge.UNKNOWN
    )


def test_billable_none_is_always_unknown() -> None:
    # E-2/E-3：submit 结果不可知时既不能 release，也不能凭 None 走特殊分支。
    assert (
        classify_upstream_cost(upstream_billable=None) is UpstreamCostKnowledge.UNKNOWN
    )
    assert (
        classify_upstream_cost(
            upstream_billable=None,
            receipt_reasons=("pre_submit_cancel",),
        )
        is UpstreamCostKnowledge.UNKNOWN
    )


@pytest.mark.parametrize(
    "reason",
    [
        "pre_submit_cancel",
        "pre_submit_expired",
        "deadline_expired_before_submit",
        "submit_failed_before_upstream_cost",
    ],
)
def test_whitelisted_receipt_with_billable_false_proves_absence(reason: str) -> None:
    assert is_no_upstream_cost_receipt(reason) is True
    assert (
        classify_upstream_cost(
            upstream_billable=False,
            receipt_reasons=(None, reason),
        )
        is UpstreamCostKnowledge.PROVEN_ABSENT
    )


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "",
        "   ",
        "submit_failed_ambiguous_upstream_cost",
        "poll_timeout",
        "pre_submit",
    ],
)
def test_non_whitelisted_receipts_are_rejected(reason: str | None) -> None:
    assert is_no_upstream_cost_receipt(reason) is False


def test_receipt_whitespace_is_tolerated() -> None:
    assert is_no_upstream_cost_receipt("  pre_submit_cancel  ") is True


@pytest.mark.parametrize(
    ("knowledge", "actual_cost_known", "expected"),
    [
        (UpstreamCostKnowledge.PROVEN_ABSENT, True, LocalBillingAction.RELEASE),
        (UpstreamCostKnowledge.PROVEN_ABSENT, False, LocalBillingAction.RELEASE),
        (
            UpstreamCostKnowledge.PROVEN_PRESENT,
            True,
            LocalBillingAction.SETTLE_ACTUAL,
        ),
        (
            UpstreamCostKnowledge.PROVEN_PRESENT,
            False,
            LocalBillingAction.SETTLE_DEFAULT,
        ),
        (UpstreamCostKnowledge.UNKNOWN, True, LocalBillingAction.SETTLE_ACTUAL),
        (UpstreamCostKnowledge.UNKNOWN, False, LocalBillingAction.SETTLE_DEFAULT),
    ],
)
def test_decision_table_rows(
    knowledge: UpstreamCostKnowledge,
    actual_cost_known: bool,
    expected: LocalBillingAction,
) -> None:
    assert (
        resolve_billing_action(knowledge, actual_cost_known=actual_cost_known)
        is expected
    )


def test_release_is_only_reachable_from_proven_absent() -> None:
    """纯转嫁核心不变式：只有能证明上游没扣费才退款。"""
    releasing = [
        knowledge
        for knowledge in UpstreamCostKnowledge
        for known in (True, False)
        if resolve_billing_action(knowledge, actual_cost_known=known)
        is LocalBillingAction.RELEASE
    ]
    assert set(releasing) == {UpstreamCostKnowledge.PROVEN_ABSENT}


def test_decide_upstream_billing_end_to_end_release() -> None:
    decision = decide_upstream_billing(
        upstream_billable=False,
        actual_cost_known=False,
        receipt_reasons=("pre_submit_cancel",),
    )
    assert decision.knowledge is UpstreamCostKnowledge.PROVEN_ABSENT
    assert decision.action is LocalBillingAction.RELEASE
    assert decision.released is True


def test_decide_upstream_billing_end_to_end_unknown_settles() -> None:
    # 连接中断：证据全无 → 默认结算，绝不 release。
    decision = decide_upstream_billing(
        upstream_billable=None,
        actual_cost_known=False,
        receipt_reasons=("submit_unknown_timeout",),
    )
    assert decision.knowledge is UpstreamCostKnowledge.UNKNOWN
    assert decision.action is LocalBillingAction.SETTLE_DEFAULT
    assert decision.released is False


def test_decide_upstream_billing_prefers_actual_usage() -> None:
    decision = decide_upstream_billing(
        upstream_billable=True,
        actual_cost_known=True,
        receipt_reasons=(),
    )
    assert decision.action is LocalBillingAction.SETTLE_ACTUAL
    assert decision.released is False


@pytest.mark.parametrize(
    "error_code",
    sorted(IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES),
)
def test_image_result_unknown_codes_settle_default_when_cost_is_unknown(
    error_code: str,
) -> None:
    # A post-dispatch unknown may already have cost the provider. Pure
    # pass-through settles the hold instead of silently refunding it.
    decision = decide_image_failure_billing(error_code)
    assert decision.knowledge is UpstreamCostKnowledge.UNKNOWN
    assert decision.action is LocalBillingAction.SETTLE_DEFAULT
    assert decision.released is False


def test_image_result_unknown_codes_cover_every_post_2xx_failure() -> None:
    # 判据是「失败点在上游 2xx 之后」，不是字面的结果不可知。NO_IMAGE_RETURNED
    # 曾被漏在集合外当普通失败处理（可重试 + 可 failover + 失败退款），三条路
    # 各自制造一笔平台吸收的上游成本；它的每个抛出点都在上游成功响应之后。
    assert IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES == {
        EC.DIRECT_IMAGE_RESULT_UNKNOWN.value,
        EC.IMAGE_JOB_RESULT_UNKNOWN.value,
        EC.NO_IMAGE_RETURNED.value,
    }


@pytest.mark.parametrize(
    "error_code",
    [
        EC.BAD_REFERENCE_IMAGE.value,
        EC.MODERATION_BLOCKED.value,
        "",
        None,
    ],
)
def test_ordinary_image_failure_still_releases(error_code: str | None) -> None:
    # 其余 failed 码保持既有语义：适配层能判定未交付且未计费 → 退款。
    # NO_IMAGE_RETURNED 已移出本组——它的失败点在上游 2xx 之后，适配层恰恰
    # **无法**判定未计费，退款会让平台吸收成本。
    decision = decide_image_failure_billing(error_code)
    assert decision.knowledge is UpstreamCostKnowledge.PROVEN_ABSENT
    assert decision.action is LocalBillingAction.RELEASE
    assert decision.released is True


def test_image_failure_decision_never_settles_actual() -> None:
    # 图片没有 token 账单，任何结算都只能按 hold 默认金额走。
    for code in (*IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES, EC.INVALID_VALUE.value):
        assert (
            decide_image_failure_billing(code).action
            is not LocalBillingAction.SETTLE_ACTUAL
        )
