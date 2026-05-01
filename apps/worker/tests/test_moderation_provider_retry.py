"""task 层 moderation_blocked 升级 retriable 的纯函数判定测试。

retry.is_retriable() 仍把 moderation 视为 terminal（避免单 provider 浪费配额）；
generation._decide_moderation_retry_upgrade() 在多 provider 部署下叠加上下文，
让 task 层退避后换号再试。
"""
from __future__ import annotations

from app.retry import RetryDecision
from app.tasks.generation import (
    _MODERATION_RETRY_CAP,
    _decide_moderation_retry_upgrade,
)


_TERMINAL_MOD = RetryDecision(retriable=False, reason="terminal safety_policy")
_RETRIABLE = RetryDecision(retriable=True, reason="rate_limited")


def _upgrade(**overrides):
    base = dict(
        base_decision=_TERMINAL_MOD,
        err_code="moderation_blocked",
        err_msg="request blocked by upstream safety policy",
        is_dual_race=False,
        reserved_provider_name="acc-a",
        enabled_provider_count=3,
        already_avoided_count=0,
    )
    base.update(overrides)
    return _decide_moderation_retry_upgrade(**base)


def test_upgrade_when_room_remains() -> None:
    decision = _upgrade()
    assert decision is not None
    assert decision.retriable is True
    assert "moderation" in decision.reason


def test_no_upgrade_when_already_retriable() -> None:
    assert _upgrade(base_decision=_RETRIABLE) is None


def test_no_upgrade_when_not_moderation() -> None:
    assert _upgrade(err_code="rate_limit_error", err_msg="too many requests") is None


def test_no_upgrade_when_dual_race() -> None:
    # dual_race 同一 attempt 内已经在两路 provider 上跑，不需要 task 层换号。
    assert _upgrade(is_dual_race=True) is None


def test_no_upgrade_when_no_reserved_provider() -> None:
    assert _upgrade(reserved_provider_name=None) is None
    assert _upgrade(reserved_provider_name="") is None


def test_no_upgrade_when_single_provider() -> None:
    # 单 provider 部署：换号没意义，避免烧配额。
    assert _upgrade(enabled_provider_count=1) is None
    assert _upgrade(enabled_provider_count=0) is None


def test_no_upgrade_when_cap_reached() -> None:
    # avoided=cap-1 时本次失败后正好达 cap，不再升级。
    assert (
        _upgrade(
            already_avoided_count=_MODERATION_RETRY_CAP - 1,
            enabled_provider_count=10,
        )
        is None
    )


def test_no_upgrade_when_all_providers_tried() -> None:
    # avoided=enabled-1 时本次失败后所有 enabled provider 都试过，terminal。
    assert (
        _upgrade(already_avoided_count=2, enabled_provider_count=3) is None
    )


def test_upgrade_until_cap_with_many_providers() -> None:
    # 10 个 provider 时上限受 _MODERATION_RETRY_CAP 控制——不会试完所有 10 个。
    last_upgrade = _MODERATION_RETRY_CAP - 2
    assert _upgrade(
        already_avoided_count=last_upgrade, enabled_provider_count=10
    ) is not None
    assert _upgrade(
        already_avoided_count=last_upgrade + 1, enabled_provider_count=10
    ) is None


def test_upgrade_recognizes_safety_policy_message_only() -> None:
    # err_code 不是 moderation enum，但 err_msg 含关键词时也应升级。
    decision = _upgrade(
        err_code="all_providers_failed",
        err_msg="all 1 upstream providers failed: blocked by upstream safety policy",
    )
    assert decision is not None
    assert decision.retriable is True
