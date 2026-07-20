"""Pricing administration routes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import PricingRule, new_uuid7
from lumen_core.schemas import (
    AdminPricingBulkIn,
    PricingImportIn,
    PricingRulesOut,
    PricingRulesUpdateIn,
)

from ...audit import hash_email
from ...db import get_db
from ...deps import AdminUser, CurrentUser, verify_csrf
from .compat import current_runtime


router = APIRouter()


def _pricing_out(b: Any, rows: list[PricingRule], db: AsyncSession) -> PricingRulesOut:
    return PricingRulesOut(
        items=[b._pricing_rule_out(row) for row in rows],
        image_size_thresholds=b._image_thresholds(db),
        billing_enabled=False,
        show_estimate_in_composer=True,
    )


async def _pricing_response(
    b: Any,
    db: AsyncSession,
    *,
    order: tuple[Any, ...],
) -> PricingRulesOut:
    rows = list(
        (await db.execute(select(PricingRule).order_by(*order))).scalars().all()
    )
    return PricingRulesOut(
        items=[b._pricing_rule_out(row) for row in rows],
        image_size_thresholds=await b._image_thresholds(db),
        billing_enabled=b.billing_core.parse_bool_setting(
            await b._setting_raw(db, "billing.enabled"), False
        ),
        show_estimate_in_composer=b.billing_core.parse_bool_setting(
            await b._setting_raw(db, "billing.show_estimate_in_composer"), True
        ),
    )


@router.get("/me/pricing", response_model=PricingRulesOut)
async def get_my_pricing(
    _user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    b = current_runtime()
    rows = list(
        (
            await db.execute(
                select(PricingRule)
                .where(PricingRule.enabled.is_(True))
                .order_by(PricingRule.scope, PricingRule.key, PricingRule.unit)
            )
        )
        .scalars()
        .all()
    )
    return PricingRulesOut(
        items=[b._pricing_rule_out(row) for row in rows],
        image_size_thresholds=await b._image_thresholds(db),
        billing_enabled=b.billing_core.parse_bool_setting(
            await b._setting_raw(db, "billing.enabled"), False
        ),
        show_estimate_in_composer=b.billing_core.parse_bool_setting(
            await b._setting_raw(db, "billing.show_estimate_in_composer"), True
        ),
    )


@router.get("/admin/pricing", response_model=PricingRulesOut)
async def admin_list_pricing(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    b = current_runtime()
    return await _pricing_response(
        b,
        db,
        order=(
            PricingRule.scope,
            PricingRule.variant,
            PricingRule.priority.desc(),
            PricingRule.key,
            PricingRule.unit,
        ),
    )


@router.get("/admin/billing/pricing", response_model=PricingRulesOut)
async def admin_list_billing_pricing(
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    b = current_runtime()
    return await b.admin_list_pricing(admin, db)


@router.put(
    "/admin/pricing",
    response_model=PricingRulesOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_update_pricing(
    body: PricingRulesUpdateIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    b = current_runtime()
    now = datetime.now(timezone.utc)
    values: list[dict[str, Any]] = []
    for item in body.items:
        price = b._rmb_to_micro_or_422(item.price_rmb, field="price_rmb")
        if price < 0:
            raise b._http("invalid_amount", "price must be non-negative", 422)
        b._validate_enabled_pricing_value(
            unit=item.unit,
            price_micro=price,
            enabled=item.enabled,
            field="price_rmb",
        )
        values.append(
            {
                "id": new_uuid7(),
                "scope": item.scope,
                "key": item.key,
                "variant": item.variant,
                "unit": item.unit,
                "price_micro": price,
                "priority": item.priority,
                "enabled": item.enabled,
                "note": item.note,
                "updated_at": now,
            }
        )
    thresholds_to_write = body.image_size_thresholds
    thresholds_for_check = thresholds_to_write or await b._image_thresholds(db)
    await b._validate_thresholds_have_prices(
        db,
        thresholds_for_check,
        values,
        force=body.force,
    )
    await _upsert_pricing_values(db, values, now=now)
    await b._align_pricing_group_priorities(db, values, now=now)
    if thresholds_to_write is not None:
        await b.update_settings(
            db,
            [
                (
                    "billing.image_size_thresholds",
                    json.dumps(thresholds_to_write, ensure_ascii=False),
                )
            ],
        )
    await b.write_audit(
        db,
        event_type="pricing.update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=b.request_ip_hash(request),
        details={
            "count": len(values),
            "thresholds_updated": thresholds_to_write is not None,
            "force": body.force,
        },
        autocommit=False,
    )
    await db.commit()
    for value in values:
        if value["scope"] == "chat_model":
            await b._invalidate_pricing_cache(str(value["key"]), str(value["variant"]))
    return await b.admin_list_pricing(admin, db)


@router.post(
    "/admin/billing/pricing/bulk",
    response_model=PricingRulesOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_bulk_pricing(
    body: AdminPricingBulkIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    b = current_runtime()
    now = datetime.now(timezone.utc)
    model = body.model.strip()
    variant = (body.channel or "default").strip() or "default"
    rates = body.rates.model_dump()
    values: list[dict[str, Any]] = []
    for field, unit in b._BULK_RATE_UNITS.items():
        micro = b._bulk_numeric_micro(rates.get(field), field=f"rates.{field}")
        if micro is not None:
            values.append(
                _pricing_value(
                    model,
                    variant,
                    unit,
                    micro,
                    body,
                    now,
                )
            )
    if body.rates.long_context_threshold is not None:
        threshold = int(body.rates.long_context_threshold)
        if threshold < 0:
            raise b._http(
                "invalid_amount",
                "rates.long_context_threshold: threshold must be non-negative",
                422,
            )
        values.append(
            _pricing_value(
                model, variant, "long_context_threshold", threshold, body, now
            )
        )
    for field in (
        "long_context_input_multiplier",
        "long_context_output_multiplier",
    ):
        multiplier = b._bulk_multiplier_x10000(rates.get(field), field=f"rates.{field}")
        if multiplier is not None:
            values.append(_pricing_value(model, variant, field, multiplier, body, now))
    if not values:
        raise b._http("invalid_request", "at least one pricing rate is required", 422)
    for value in values:
        b._validate_enabled_pricing_value(
            unit=str(value["unit"]),
            price_micro=int(value["price_micro"]),
            enabled=bool(value["enabled"]),
            field=f"rates.{value['unit']}",
        )
    await _upsert_pricing_values(db, values, now=now)
    await b._align_pricing_group_priorities(db, values, now=now)
    await b.write_audit(
        db,
        event_type="pricing.bulk_update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=b.request_ip_hash(request),
        details={
            "model": model,
            "channel": None if variant == "default" else variant,
            "priority": body.priority,
            "count": len(values),
            "units": [value["unit"] for value in values],
        },
        autocommit=False,
    )
    await db.commit()
    await b._invalidate_pricing_cache(model, variant)
    return await b.admin_list_pricing(admin, db)


@router.post(
    "/admin/pricing/import_openai",
    response_model=PricingRulesOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_import_openai_pricing(
    body: PricingImportIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PricingRulesOut:
    b = current_runtime()
    rows = b._parse_price_rows(body.content)
    items = []
    for row in rows:
        model = str(row.get("model") or "").strip()
        if not model:
            continue
        if "input_usd_per_1m" in row:
            items.append(
                {
                    "scope": "chat_model",
                    "key": model,
                    "variant": "default",
                    "unit": "per_1k_tokens_in",
                    "price_rmb": billing_core.micro_to_rmb_str(
                        b._openai_price_micro(row["input_usd_per_1m"], body.rate)
                    ),
                    "enabled": True,
                    "note": f"OpenAI input USD/1M={row['input_usd_per_1m']} rate={body.rate}",
                }
            )
        if "output_usd_per_1m" in row:
            items.append(
                {
                    "scope": "chat_model",
                    "key": model,
                    "variant": "default",
                    "unit": "per_1k_tokens_out",
                    "price_rmb": billing_core.micro_to_rmb_str(
                        b._openai_price_micro(row["output_usd_per_1m"], body.rate)
                    ),
                    "enabled": True,
                    "note": f"OpenAI output USD/1M={row['output_usd_per_1m']} rate={body.rate}",
                }
            )
    if not items:
        raise b._http("invalid_price_file", "no model prices found", 422)
    update_body = PricingRulesUpdateIn.model_validate({"items": items})
    return await b.admin_update_pricing(update_body, request, admin, db)


def _pricing_value(
    model: str,
    variant: str,
    unit: str,
    price_micro: int,
    body: AdminPricingBulkIn,
    now: datetime,
) -> dict[str, Any]:
    return {
        "id": new_uuid7(),
        "scope": "chat_model",
        "key": model,
        "variant": variant,
        "unit": unit,
        "price_micro": price_micro,
        "priority": body.priority,
        "enabled": body.enabled,
        "note": body.note,
        "updated_at": now,
    }


async def _upsert_pricing_values(
    db: AsyncSession,
    values: list[dict[str, Any]],
    *,
    now: datetime,
) -> None:
    bind = await db.connection()
    if bind.dialect.name == "postgresql":
        insert_stmt = pg_insert(PricingRule).values(values)
        await db.execute(
            insert_stmt.on_conflict_do_update(
                constraint="uq_pricing_scope_key_variant_unit",
                set_={
                    "price_micro": insert_stmt.excluded.price_micro,
                    "priority": insert_stmt.excluded.priority,
                    "enabled": insert_stmt.excluded.enabled,
                    "note": insert_stmt.excluded.note,
                    "updated_at": now,
                },
            )
        )
        return
    for value in values:
        existing = (
            await db.execute(
                select(PricingRule).where(
                    PricingRule.scope == value["scope"],
                    PricingRule.key == value["key"],
                    PricingRule.variant == value["variant"],
                    PricingRule.unit == value["unit"],
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(PricingRule(**value))
        else:
            existing.price_micro = value["price_micro"]
            existing.priority = value["priority"]
            existing.enabled = value["enabled"]
            existing.note = value["note"]
            existing.updated_at = now
