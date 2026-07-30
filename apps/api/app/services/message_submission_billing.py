"""Idempotency and billing preflight helpers for message submission."""

from __future__ import annotations

import hashlib
import logging
import os
from types import MappingProxyType
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.constants import (
    IMAGE_MULTI_GEN_STAGGER_CAP_S,
    IMAGE_MULTI_GEN_STAGGER_S,
)
from lumen_core.models import SystemSetting
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import ChatParamsIn

from ..runtime_settings import get_setting
from ..task_billing import (
    ChatWalletPreflight,
    apply_rate_multiplier_micro,
    user_rate_multiplier_x10000,
)

logger = logging.getLogger(__name__)
AsyncCallable = Callable[..., Awaitable[Any]]

_CHAT_TOOL_BUDGET_SETTINGS = MappingProxyType(
    {
        "web_search": ("chat.tool_web_search_micro", "CHAT_TOOL_WEB_SEARCH_MICRO"),
        "file_search": ("chat.tool_file_search_micro", "CHAT_TOOL_FILE_SEARCH_MICRO"),
        "code_interpreter": (
            "chat.tool_code_interpreter_micro",
            "CHAT_TOOL_CODE_INTERPRETER_MICRO",
        ),
        "image_generation": (
            "chat.tool_image_generation_micro",
            "CHAT_TOOL_IMAGE_GENERATION_MICRO",
        ),
    }
)
_MAX_TOOL_INVOCATIONS_DEFAULT = 8


def http_error(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        err["details"] = extra
    return HTTPException(status_code=http, detail={"error": err})


def idempotency_lock_key(
    user_id: str,
    conv_id: str,
    idempotency_key: str,
) -> str:
    return f"{user_id}:{conv_id}:{idempotency_key}"


def stored_idempotency_key(conv_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"{conv_id}:{idempotency_key}".encode("utf-8", errors="replace")
    ).hexdigest()
    return f"cv:{digest[:61]}"


def generation_child_idempotency_key(base_key: str, index: int) -> str:
    if index <= 1:
        return base_key
    suffix = f":g{index}"
    prefix_len = 64 - len(suffix)
    return f"{base_key[:prefix_len]}{suffix}"


def image_multi_generation_defer_s(index: int) -> int:
    if index <= 1:
        return 0
    return min(IMAGE_MULTI_GEN_STAGGER_CAP_S, (index - 1) * IMAGE_MULTI_GEN_STAGGER_S)


def idempotency_lookup_keys(
    conv_id: str,
    idempotency_key: str,
) -> tuple[str, str]:
    return (idempotency_key, stored_idempotency_key(conv_id, idempotency_key))


async def billing_setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    try:
        return await get_setting(db, spec)
    except (AssertionError, IndexError):
        if key.startswith("billing."):
            return None
        raise


async def billing_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await billing_setting_raw(db, "billing.enabled"),
        False,
    )


async def billing_allow_negative(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await billing_setting_raw(db, "billing.allow_negative_balance"),
        False,
    )


def _parse_nonnegative_micro(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _enabled_chat_tools(chat_params: ChatParamsIn | None) -> list[str]:
    if chat_params is None:
        return []
    tools: list[str] = []
    if chat_params.web_search:
        tools.append("web_search")
    if chat_params.file_search:
        tools.append("file_search")
    if chat_params.code_interpreter:
        tools.append("code_interpreter")
    if chat_params.image_generation:
        tools.append("image_generation")
    return tools


async def chat_tool_budget_setting_micro(
    db: AsyncSession,
    tool_name: str,
) -> int:
    setting = _CHAT_TOOL_BUDGET_SETTINGS.get(tool_name)
    if setting is None:
        return 0
    setting_key, env_key = setting
    raw: object | None = None
    try:
        raw = (
            await db.execute(
                select(SystemSetting.value).where(SystemSetting.key == setting_key)
            )
        ).scalar_one_or_none()
    except Exception:
        logger.warning(
            "chat tool budget setting lookup failed key=%s",
            setting_key,
            exc_info=True,
        )
    if raw in (None, ""):
        raw = os.environ.get(env_key)
    return _parse_nonnegative_micro(raw)


async def chat_max_tool_invocations(db: AsyncSession) -> int:
    raw: object | None = None
    try:
        raw = (
            await db.execute(
                select(SystemSetting.value).where(
                    SystemSetting.key == "chat.max_tool_invocations"
                )
            )
        ).scalar_one_or_none()
    except Exception:
        logger.warning("chat max_tool_invocations lookup failed", exc_info=True)
    if raw in (None, ""):
        raw = os.environ.get("CHAT_MAX_TOOL_INVOCATIONS")
    if isinstance(raw, bool):
        return _MAX_TOOL_INVOCATIONS_DEFAULT
    if isinstance(raw, int):
        parsed = raw
    elif isinstance(raw, str):
        try:
            parsed = int(raw.strip())
        except ValueError:
            return _MAX_TOOL_INVOCATIONS_DEFAULT
    else:
        return _MAX_TOOL_INVOCATIONS_DEFAULT
    return min(64, max(1, parsed))


async def _estimate_chat_tool_budget_micro(
    db: AsyncSession,
    chat_params: ChatParamsIn | None,
    *,
    chat_tool_budget_setting_fn: AsyncCallable = chat_tool_budget_setting_micro,
    chat_max_tool_invocations_fn: AsyncCallable = chat_max_tool_invocations,
) -> tuple[int, dict[str, int]]:
    budget_by_tool: dict[str, int] = {}
    max_tool_invocations = int(await chat_max_tool_invocations_fn(db))
    for tool_name in _enabled_chat_tools(chat_params):
        amount = int(await chat_tool_budget_setting_fn(db, tool_name))
        if amount > 0:
            budget_by_tool[tool_name] = amount * max_tool_invocations
    return sum(budget_by_tool.values()), budget_by_tool


async def billing_image_thresholds(db: AsyncSession) -> dict[str, int]:
    return billing_core.parse_thresholds(
        await billing_setting_raw(db, "billing.image_size_thresholds")
    )


def billing_http_error(exc: billing_core.BillingError) -> HTTPException:
    return http_error(exc.code, exc.message, exc.status_code)


async def ensure_chat_wallet_preflight(
    db: AsyncSession,
    *,
    user_id: str,
    user_email: str | None,
    account_mode: str,
    model: str,
    chat_params: ChatParamsIn | None = None,
    billing_enabled_fn: AsyncCallable = billing_enabled,
    billing_allow_negative_fn: AsyncCallable = billing_allow_negative,
    user_rate_multiplier_fn: AsyncCallable = user_rate_multiplier_x10000,
    chat_tool_budget_setting_fn: AsyncCallable = chat_tool_budget_setting_micro,
    chat_max_tool_invocations_fn: AsyncCallable = chat_max_tool_invocations,
) -> ChatWalletPreflight | None:
    _ = user_email
    if account_mode != "wallet" or not await billing_enabled_fn(db):
        return None
    wallet = await billing_core.get_wallet(db, user_id, lock=True)
    if wallet is None:
        raise http_error("WALLET_UNAVAILABLE", "wallet could not be initialized", 503)
    rate_multiplier_x10000 = int(await user_rate_multiplier_fn(db, user_id))
    if rate_multiplier_x10000 > 0 and wallet.balance_micro < 10_000:
        raise http_error(
            "INSUFFICIENT_BALANCE",
            "insufficient wallet balance",
            402,
            required_micro=10_000,
            balance_micro=int(wallet.balance_micro),
        )
    try:
        pricing_snapshot = await billing_core.completion_pricing_snapshot(
            db,
            model=model,
        )
        cost_preview = billing_core.completion_breakdown_from_snapshot(
            pricing_snapshot,
            model=model,
            tokens=billing_core.UsageTokens(input_tokens=1, output_tokens=1),
            rate_multiplier_x10000=rate_multiplier_x10000,
        ).actual_cost_micro
    except billing_core.BillingError as exc:
        raise billing_http_error(exc) from exc
    if cost_preview <= 0 and rate_multiplier_x10000 > 0:
        raise billing_http_error(
            billing_core.BillingError(
                "PRICING_MISSING",
                f"missing enabled chat pricing rule for {model}",
                503,
            )
        )
    tool_budget_micro, budget_by_tool = await _estimate_chat_tool_budget_micro(
        db,
        chat_params,
        chat_tool_budget_setting_fn=chat_tool_budget_setting_fn,
        chat_max_tool_invocations_fn=chat_max_tool_invocations_fn,
    )
    budget_by_tool = {
        tool_name: apply_rate_multiplier_micro(amount, rate_multiplier_x10000)
        for tool_name, amount in budget_by_tool.items()
    }
    tool_budget_micro = sum(budget_by_tool.values())
    preauth_micro = (
        0
        if rate_multiplier_x10000 == 0
        else max(10_000, int(cost_preview or 0) + tool_budget_micro)
    )
    if wallet.balance_micro < preauth_micro and not await billing_allow_negative_fn(db):
        raise http_error(
            "INSUFFICIENT_BALANCE",
            "insufficient wallet balance",
            402,
            required_micro=preauth_micro,
            balance_micro=int(wallet.balance_micro),
            estimated_model_micro=int(cost_preview or 0),
            tool_budget_micro=tool_budget_micro,
        )
    return ChatWalletPreflight(
        estimated_model_micro=int(cost_preview or 0),
        tool_budget_micro=tool_budget_micro,
        preauth_micro=preauth_micro,
        tool_budget_by_tool=budget_by_tool,
        pricing_snapshot=pricing_snapshot,
        rate_multiplier_x10000=rate_multiplier_x10000,
    )
