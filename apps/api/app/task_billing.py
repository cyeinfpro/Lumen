"""Task-billing snapshots shared by message and prompt creation paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.billing import parse_rate_multiplier_x10000
from lumen_core.models import User
from lumen_core.schemas import ImageParamsIn
from lumen_core.sizing import ResolvedSize


_IMAGE_BILLING_TIER_VALUES = frozenset({"1k", "2k", "4k"})
_IMAGE_RENDER_QUALITY_VALUES = frozenset({"low", "medium", "high"})


@dataclass(frozen=True)
class ChatWalletPreflight:
    estimated_model_micro: int
    tool_budget_micro: int
    preauth_micro: int
    tool_budget_by_tool: dict[str, int]
    pricing_snapshot: dict[str, Any]
    rate_multiplier_x10000: int = 10_000

    def upstream_metadata(self) -> dict[str, Any]:
        return {
            "billing_pricing_snapshot": self.pricing_snapshot,
            "billing_rate_multiplier_x10000": self.rate_multiplier_x10000,
        }

    def hold_metadata(self) -> dict[str, Any]:
        return {
            "estimated_model_micro": self.estimated_model_micro,
            "tool_budget_micro": self.tool_budget_micro,
            "tool_budget_by_tool": self.tool_budget_by_tool,
            "pricing_snapshot": self.pricing_snapshot,
            "rate_multiplier_x10000": self.rate_multiplier_x10000,
        }

    def audit_metadata(self) -> dict[str, int]:
        return {
            "estimated_model_micro": self.estimated_model_micro,
            "tool_budget_micro": self.tool_budget_micro,
            "rate_multiplier_x10000": self.rate_multiplier_x10000,
        }


@dataclass
class EnhanceSettleOutcome:
    """prompt enhance 结算尝试标记(可变对象,dataclasses.replace 后仍共享同一实例)。

    默认结算被调度或执行时置位 attempted,链外孤儿 hold 兜底释放据此跳过:
    结算已落库则 release 本应幂等 no-op;结算落库失败则 hold 必须保留在钱包
    中交由管理端孤儿扫描对账,不能 fail-open 退款让平台吸收上游已产生的成本。
    """

    attempted: bool = False


@dataclass
class EnhanceBillingContext:
    db: AsyncSession
    user_id: str
    user_email: str | None
    request_id: str
    rate_multiplier_x10000: int
    cache_aware: bool
    allow_negative: bool
    hold_amount_micro: int = 0
    pricing_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    settle_outcome: EnhanceSettleOutcome = field(default_factory=EnhanceSettleOutcome)


@dataclass
class EnhanceUsageCapture:
    provider_name: str | None = None
    model: str | None = None
    service_tier: str = "standard"
    pricing_snapshot_key: str | None = None
    response_id: str | None = None
    usage: dict[str, Any] | None = None


def rate_multiplier_x10000(value: Any) -> int:
    # 换算走 lumen_core.billing 的 Decimal 实现：float 中转会把 1.0009 这类
    # 四位小数算成 10008（少一档），差额等于平台替用户垫付。
    raw = getattr(value, "billing_rate_multiplier", value)
    return parse_rate_multiplier_x10000(raw)


async def user_rate_multiplier_x10000(
    db: AsyncSession,
    user_id: str,
) -> int:
    if not isinstance(db, AsyncSession):
        return 10_000
    raw = (
        await db.execute(select(User.billing_rate_multiplier).where(User.id == user_id))
    ).scalar_one_or_none()
    return rate_multiplier_x10000(raw)


def apply_rate_multiplier_micro(amount_micro: int, multiplier_x10000: int) -> int:
    amount = max(0, int(amount_micro or 0))
    multiplier = max(0, int(multiplier_x10000 or 0))
    if amount == 0 or multiplier == 0:
        return 0
    return max(1, (amount * multiplier) // 10_000)


def enhance_pricing_snapshot_key(model: str, service_tier: str) -> str:
    return f"{model.strip().lower()}::{service_tier.strip().lower()}"


def requested_image_billing_tier(image_params: ImageParamsIn) -> str | None:
    return (
        image_params.quality
        if image_params.quality in _IMAGE_BILLING_TIER_VALUES
        else None
    )


def resolve_image_render_quality(
    image_params: ImageParamsIn,
    resolved_size: ResolvedSize,
) -> str:
    _ = resolved_size
    if image_params.render_quality in _IMAGE_RENDER_QUALITY_VALUES:
        return image_params.render_quality
    return "medium"
