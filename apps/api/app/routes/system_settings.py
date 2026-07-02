"""管理员可调系统设置（V1.0 收尾）。

GET /admin/settings 与 PUT /admin/settings；写入仅限 SUPPORTED_SETTINGS 列表里的 key，
type-check int/float 失败即 422。
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import PricingRule
from lumen_core.runtime_settings import get_spec, parse_value
from lumen_core.schemas import (
    SystemSettingsOut,
    SystemSettingsUpdateIn,
)

from ..audit import hash_email, request_ip_hash, write_audit
from ..db import get_db
from ..deps import AdminUser, verify_csrf
from .providers import (
    ensure_enabled_provider_proxies,
    ensure_enabled_video_provider_proxies,
)
from ..runtime_settings import get_setting, get_settings_view, update_settings
from ..services.redemption_secret import (
    PreviousRedemptionSecretLocked,
    remember_previous_redemption_secret,
)


router = APIRouter(prefix="/admin/settings", tags=["admin-settings"])


def _http(code: str, msg: str, http: int = 400, **details) -> HTTPException:
    err: dict = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


async def _validate_threshold_pricing_alignment(
    db: AsyncSession,
    raw_thresholds: str,
) -> None:
    try:
        parsed = json.loads(raw_thresholds)
    except json.JSONDecodeError as exc:
        raise _http(
            "INVALID_THRESHOLDS_JSON",
            "billing.image_size_thresholds must be valid JSON",
            422,
        ) from exc
    if not isinstance(parsed, dict):
        raise _http(
            "INVALID_THRESHOLDS_JSON",
            "billing.image_size_thresholds must be a JSON object",
            422,
        )
    enabled_keys = set(
        (
            await db.execute(
                select(PricingRule.key).where(
                    PricingRule.scope == "image_size",
                    PricingRule.unit == "per_image",
                    PricingRule.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    missing = sorted(str(key) for key in parsed if str(key) not in enabled_keys)
    if missing:
        raise _http(
            "THRESHOLDS_PRICING_MISMATCH",
            "every image size threshold must have an enabled pricing rule",
            422,
            missing=missing,
        )


async def _validate_provider_setting_semantics(
    db: AsyncSession,
    pair_map: dict[str, str],
) -> None:
    errors: list[dict[str, str]] = []
    providers_raw = pair_map.get("providers")
    if providers_raw is not None:
        try:
            ensure_enabled_provider_proxies(providers_raw)
        except ValueError as exc:
            errors.append(
                {
                    "key": "providers",
                    "reason": "invalid_provider_proxy",
                    "message": str(exc),
                }
            )
    video_raw = pair_map.get("video.providers")
    if video_raw is not None:
        shared_raw = providers_raw
        if shared_raw is None:
            providers_spec = get_spec("providers")
            if providers_spec is not None:
                shared_raw = await get_setting(db, providers_spec)
        try:
            ensure_enabled_video_provider_proxies(
                video_raw,
                shared_provider_raw=shared_raw,
            )
        except ValueError as exc:
            errors.append(
                {
                    "key": "video.providers",
                    "reason": "invalid_provider_proxy",
                    "message": str(exc),
                }
            )
    if errors:
        raise _http(
            "invalid_request",
            "one or more setting items are invalid",
            422,
            errors=errors,
        )


@router.get("", response_model=SystemSettingsOut)
async def get_settings_endpoint(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemSettingsOut:
    items = await get_settings_view(db)
    return SystemSettingsOut(items=items)


@router.put(
    "",
    response_model=SystemSettingsOut,
    dependencies=[Depends(verify_csrf)],
)
async def put_settings_endpoint(
    body: SystemSettingsUpdateIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemSettingsOut:
    # 收 (key, value) 元组先做合法性 + 类型校验
    pairs: list[tuple[str, str]] = []
    invalid: list[dict] = []
    _MAX_VALUE_LEN = 2048
    _MAX_PROVIDERS_LEN = 16384
    for it in body.items:
        spec = get_spec(it.key)
        if spec is None:
            invalid.append({"key": it.key, "reason": "unknown_key"})
            continue
        max_len = _MAX_PROVIDERS_LEN if it.key == "providers" else _MAX_VALUE_LEN
        if len(it.value) > max_len:
            invalid.append(
                {
                    "key": it.key,
                    "reason": "value_too_long",
                    "message": f"value exceeds {max_len} chars",
                }
            )
            continue
        try:
            parse_value(spec, it.value)
        except (TypeError, ValueError) as exc:
            invalid.append(
                {
                    "key": it.key,
                    "reason": f"invalid_{spec.parser.__name__}",
                    "message": str(exc),
                }
            )
            continue
        pairs.append((it.key, it.value))

    if invalid:
        raise _http(
            "invalid_request",
            "one or more setting items are invalid",
            422,
            errors=invalid,
        )

    pair_map = {key: value for key, value in pairs}
    await _validate_provider_setting_semantics(db, pair_map)
    if "billing.image_size_thresholds" in pair_map:
        await _validate_threshold_pricing_alignment(
            db, pair_map["billing.image_size_thresholds"]
        )

    secret_spec = get_spec("billing.redemption_code_secret")
    old_secret = (
        await get_setting(db, secret_spec)
        if secret_spec is not None and "billing.redemption_code_secret" in pair_map
        else None
    )

    try:
        await update_settings(db, pairs)
    except ValueError as exc:
        await db.rollback()
        raise _http("invalid_request", str(exc), 422)
    await write_audit(
        db,
        event_type="admin.settings.update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"keys": [k for k, _ in pairs]},
        autocommit=False,
    )
    new_secret = pair_map.get("billing.redemption_code_secret")
    if new_secret and new_secret != old_secret:
        try:
            transition_expires_at = await remember_previous_redemption_secret(
                db, old_secret
            )
        except PreviousRedemptionSecretLocked as exc:
            await db.rollback()
            raise _http(
                "previous_secret_locked",
                "another rotation is still inside the 24h transition window",
                409,
            ) from exc
        secret_hash8 = hashlib.sha256(new_secret.encode("utf-8")).hexdigest()[:8]
        await write_audit(
            db,
            event_type="billing.secret.rotate"
            if old_secret
            else "billing.secret.configure",
            user_id=admin.id,
            actor_email_hash=hash_email(admin.email),
            actor_ip_hash=request_ip_hash(request),
            details={
                "secret_hash8": secret_hash8,
                "previous_secret_valid_until": transition_expires_at,
                "revoked_unredeemed_count": 0,
            },
            autocommit=False,
        )
    await db.commit()

    items = await get_settings_view(db)
    return SystemSettingsOut(items=items)


__all__ = ["router"]
