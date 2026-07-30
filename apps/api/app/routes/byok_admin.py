"""Administrator BYOK settings and supplier-template routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    ApiSupplierTemplate,
    UserApiCredential,
)
from lumen_core.schema_models import (
    ApiSupplierProbeIn,
    ApiSupplierStatsOut,
    ApiSupplierTemplateIn,
    ApiSupplierTemplateListOut,
    ApiSupplierTemplateOut,
    ApiSupplierTemplatePatchIn,
    ByokSettingsOut,
    ByokSettingsPatchIn,
)

from ..audit import hash_email, request_ip_hash, write_audit
from ..byok_service import (
    invalidate_byok_settings_cache,
    normalize_base_url,
    read_byok_settings,
    slugify_supplier,
    supplier_to_out,
    validate_api_key_with_supplier,
)
from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ..runtime_settings import update_settings


router = APIRouter(prefix="/admin", tags=["admin-byok"])


def _http(code: str, msg: str, http: int = 400, **details: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


def setting_pairs(body: ByokSettingsPatchIn) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if body.mode_enabled is not None:
        pairs.append(("byok.mode_enabled", "1" if body.mode_enabled else "0"))
    if body.byok_signup_enabled is not None:
        pairs.append(
            ("auth.byok_signup_enabled", "1" if body.byok_signup_enabled else "0")
        )
    if body.byok_signup_bypasses_allowlist is not None:
        pairs.append(
            (
                "auth.byok_signup_bypasses_allowlist",
                "1" if body.byok_signup_bypasses_allowlist else "0",
            )
        )
    if body.fallback_to_admin_provider is not None:
        pairs.append(("byok.fallback_to_admin_provider", "0"))
    if body.validation_model is not None:
        pairs.append(("byok.validation_model", body.validation_model.strip()))
    if body.validation_timeout_ms is not None:
        pairs.append(("byok.validation_timeout_ms", str(body.validation_timeout_ms)))
    if body.pending_token_ttl_seconds is not None:
        pairs.append(
            ("byok.pending_token_ttl_seconds", str(body.pending_token_ttl_seconds))
        )
    if body.retention_hide_enabled is not None:
        pairs.append(
            (
                "byok.retention_hide_enabled",
                "1" if body.retention_hide_enabled else "0",
            )
        )
    if body.retention_delete_enabled is not None:
        pairs.append(
            (
                "byok.retention_delete_enabled",
                "1" if body.retention_delete_enabled else "0",
            )
        )
    if body.retention_hide_days is not None:
        pairs.append(("byok.retention_hide_days", str(body.retention_hide_days)))
    if body.retention_delete_days is not None:
        pairs.append(("byok.retention_delete_days", str(body.retention_delete_days)))
    return pairs


@router.get("/byok-settings", response_model=ByokSettingsOut)
async def get_byok_settings(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ByokSettingsOut:
    return await read_byok_settings(db)


@router.patch(
    "/byok-settings",
    response_model=ByokSettingsOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_byok_settings(
    body: ByokSettingsPatchIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ByokSettingsOut:
    if (
        body.retention_hide_enabled is not None
        or body.retention_delete_enabled is not None
        or body.retention_hide_days is not None
        or body.retention_delete_days is not None
    ):
        current = await read_byok_settings(db)
        hide_enabled = (
            body.retention_hide_enabled
            if body.retention_hide_enabled is not None
            else current.retention_hide_enabled
        )
        delete_enabled = (
            body.retention_delete_enabled
            if body.retention_delete_enabled is not None
            else current.retention_delete_enabled
        )
        hide_days = (
            body.retention_hide_days
            if body.retention_hide_days is not None
            else current.retention_hide_days
        )
        delete_days = (
            body.retention_delete_days
            if body.retention_delete_days is not None
            else current.retention_delete_days
        )
        if hide_enabled and delete_enabled and delete_days < hide_days:
            raise _http(
                "invalid_retention_window",
                "delete days must be greater than or equal to hide days",
                422,
            )
    pairs = setting_pairs(body)
    if pairs:
        await update_settings(db, pairs)
        await write_audit(
            db,
            event_type="admin.byok_settings.update",
            user_id=admin.id,
            actor_email_hash=hash_email(admin.email),
            actor_ip_hash=request_ip_hash(request),
            details={"keys": [key for key, _ in pairs]},
            autocommit=False,
        )
        await db.commit()
        invalidate_byok_settings_cache()
    return await read_byok_settings(db)


@router.get("/api-suppliers", response_model=ApiSupplierTemplateListOut)
async def list_api_suppliers(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiSupplierTemplateListOut:
    suppliers = (
        (
            await db.execute(
                select(ApiSupplierTemplate)
                .where(ApiSupplierTemplate.deleted_at.is_(None))
                .order_by(ApiSupplierTemplate.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return ApiSupplierTemplateListOut(
        items=[await supplier_to_out(db, supplier) for supplier in suppliers]
    )


@router.post(
    "/api-suppliers",
    response_model=ApiSupplierTemplateOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_api_supplier(
    body: ApiSupplierTemplateIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiSupplierTemplateOut:
    supplier = ApiSupplierTemplate(
        name=body.name.strip(),
        slug=slugify_supplier(body.slug or body.name),
        base_url=await normalize_base_url(body.base_url),
        enabled=body.enabled,
        public_signup_enabled=body.public_signup_enabled,
        user_bind_enabled=body.user_bind_enabled,
        purposes=list(body.purposes),
        validation_model=body.validation_model.strip(),
        default_chat_model=body.default_chat_model.strip(),
        fast_chat_model=(body.fast_chat_model or "").strip() or None,
        validation_timeout_ms=body.validation_timeout_ms,
        proxy_name=(body.proxy_name or "").strip() or None,
        text_concurrency_per_key=body.text_concurrency_per_key,
        image_concurrency_per_key=body.image_concurrency_per_key,
        capabilities_jsonb=body.capabilities_jsonb,
        created_by=admin.id,
    )
    db.add(supplier)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _http(
            "duplicate_supplier_slug", "supplier slug already exists", 409
        ) from exc
    await write_audit(
        db,
        event_type="admin.api_supplier.create",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"supplier_id": supplier.id, "slug": supplier.slug},
        autocommit=False,
    )
    await db.commit()
    return await supplier_to_out(db, supplier)


@router.patch(
    "/api-suppliers/{supplier_id}",
    response_model=ApiSupplierTemplateOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_api_supplier(
    supplier_id: str,
    body: ApiSupplierTemplatePatchIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiSupplierTemplateOut:
    supplier = (
        await db.execute(
            select(ApiSupplierTemplate)
            .where(
                ApiSupplierTemplate.id == supplier_id,
                ApiSupplierTemplate.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if supplier is None:
        raise _http("not_found", "supplier not found", 404)
    if body.name is not None:
        supplier.name = body.name.strip()
    if body.slug is not None:
        supplier.slug = slugify_supplier(body.slug or supplier.name)
    if body.base_url is not None:
        supplier.base_url = await normalize_base_url(body.base_url)
    for field in (
        "enabled",
        "public_signup_enabled",
        "user_bind_enabled",
        "validation_timeout_ms",
        "text_concurrency_per_key",
        "image_concurrency_per_key",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(supplier, field, value)
    if body.purposes is not None:
        supplier.purposes = list(body.purposes)
    if body.validation_model is not None:
        supplier.validation_model = body.validation_model.strip()
    if body.default_chat_model is not None:
        supplier.default_chat_model = body.default_chat_model.strip()
    if body.fast_chat_model is not None:
        supplier.fast_chat_model = body.fast_chat_model.strip() or None
    if body.proxy_name is not None:
        supplier.proxy_name = body.proxy_name.strip() or None
    if body.capabilities_jsonb is not None:
        supplier.capabilities_jsonb = body.capabilities_jsonb
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _http(
            "duplicate_supplier_slug", "supplier slug already exists", 409
        ) from exc
    await write_audit(
        db,
        event_type="admin.api_supplier.update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"supplier_id": supplier.id, "slug": supplier.slug},
        autocommit=False,
    )
    await db.commit()
    await db.refresh(supplier, ["updated_at"])
    return await supplier_to_out(db, supplier)


@router.post(
    "/api-suppliers/{supplier_id}/probe",
    dependencies=[Depends(verify_csrf)],
)
async def probe_api_supplier(
    supplier_id: str,
    body: ApiSupplierProbeIn,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    _ = admin
    supplier = await db.get(ApiSupplierTemplate, supplier_id)
    if supplier is None or supplier.deleted_at is not None:
        raise _http("not_found", "supplier not found", 404)
    outcome = await validate_api_key_with_supplier(db, supplier, body.api_key)
    return {
        "ok": outcome.ok,
        "error_code": outcome.error_code,
        "http_status": outcome.http_status,
        "latency_ms": outcome.latency_ms,
        "key_hint": outcome.key_hint,
    }


@router.get(
    "/api-suppliers/{supplier_id}/stats",
    response_model=ApiSupplierStatsOut,
)
async def get_api_supplier_stats(
    supplier_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiSupplierStatsOut:
    active = int(
        (
            await db.execute(
                select(func.count(UserApiCredential.id)).where(
                    UserApiCredential.supplier_id == supplier_id,
                    UserApiCredential.status == "active",
                    UserApiCredential.deleted_at.is_(None),
                )
            )
        ).scalar()
        or 0
    )
    rows = (
        await db.execute(
            select(UserApiCredential.last_error_code, func.count(UserApiCredential.id))
            .where(
                UserApiCredential.supplier_id == supplier_id,
                UserApiCredential.last_error_code.is_not(None),
            )
            .group_by(UserApiCredential.last_error_code)
        )
    ).all()
    return ApiSupplierStatsOut(
        supplier_id=supplier_id,
        active_credentials=active,
        recent_error_counts={str(code): int(count) for code, count in rows if code},
    )
