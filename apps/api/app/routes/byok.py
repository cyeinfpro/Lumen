"""BYOK supplier template, key verification, and user credential routes."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.byok import ByokCryptoError, decrypt_api_key
from lumen_core.models import (
    ApiSupplierTemplate,
    PendingApiKeyVerification,
    UserApiCredential,
)
from lumen_core.schemas import (
    ApiKeyVerifyIn,
    ApiKeyVerifyOut,
    ApiSupplierProbeIn,
    ApiSupplierStatsOut,
    ApiSupplierTemplateIn,
    ApiSupplierTemplateListOut,
    ApiSupplierTemplateOut,
    ApiSupplierTemplatePatchIn,
    ApiSupplierTemplatePublicListOut,
    ByokSettingsOut,
    ByokSettingsPatchIn,
    UserApiCredentialListOut,
    UserApiCredentialOut,
    UserApiCredentialUpdateIn,
)

from ..audit import hash_email, request_ip_hash, write_audit, write_audit_isolated
from ..byok_service import (
    api_key_rate_limit_hash,
    byok_master_secret,
    create_verification_token_hash,
    encrypt_pending_key,
    hash_text_for_audit,
    invalidate_byok_settings_cache,
    normalize_base_url,
    pending_expires_at,
    read_byok_settings,
    slugify_supplier,
    supplier_to_out,
    supplier_to_public_out,
    validate_api_key_with_supplier,
)
from ..db import get_db
from ..deps import AdminUser, CurrentUser, require_account_mode, verify_csrf
from ..ratelimit import RateLimiter, require_client_ip
from ..redis_client import get_redis
from ..runtime_settings import update_settings


router_admin = APIRouter(prefix="/admin", tags=["admin-byok"])
router_auth_public = APIRouter(prefix="/auth", tags=["auth-byok"])
router_me = APIRouter(
    prefix="/me/api-credentials",
    tags=["me-api-credentials"],
    dependencies=[Depends(require_account_mode("byok"))],
)

_VERIFY_IP_LIMITER = RateLimiter(capacity=5, refill_per_sec=5 / 60, always_on=True)
_VERIFY_SUPPLIER_LIMITER = RateLimiter(
    capacity=60, refill_per_sec=60 / 60, always_on=True
)
_VERIFY_KEY_LIMITER = RateLimiter(capacity=10, refill_per_sec=10 / 900, always_on=True)
_PROBE_IP_LIMITER = RateLimiter(capacity=10, refill_per_sec=10 / 60, always_on=True)
_PROBE_USER_LIMITER = RateLimiter(capacity=12, refill_per_sec=12 / 300, always_on=True)
_PROBE_CREDENTIAL_LIMITER = RateLimiter(
    capacity=3, refill_per_sec=3 / 300, always_on=True
)
_PROBE_SUPPLIER_LIMITER = RateLimiter(
    capacity=30, refill_per_sec=30 / 60, always_on=True
)
_PROBE_KEY_LIMITER = RateLimiter(capacity=6, refill_per_sec=6 / 900, always_on=True)
_MIN_VALIDATION_SECONDS = 0.25


def _http(code: str, msg: str, http: int = 400, **details: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


def _setting_pairs(body: ByokSettingsPatchIn) -> list[tuple[str, str]]:
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
        # Deprecated compatibility field. BYOK must remain strict: no user key
        # means no task, never a fallback to the admin provider pool.
        pairs.append(
            (
                "byok.fallback_to_admin_provider",
                "0",
            )
        )
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


@router_admin.get("/byok-settings", response_model=ByokSettingsOut)
async def get_byok_settings(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ByokSettingsOut:
    return await read_byok_settings(db)


@router_admin.patch(
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
    pairs = _setting_pairs(body)
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
        # Why: drop the in-process cache so the next read_byok_settings_cached
        # call (e.g. from POST /messages) sees the new admin values within
        # one request, instead of waiting for the 30 s TTL to expire.
        invalidate_byok_settings_cache()
    return await read_byok_settings(db)


@router_admin.get("/api-suppliers", response_model=ApiSupplierTemplateListOut)
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


@router_admin.post(
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


@router_admin.patch(
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
    # ORM UPDATE 不带 RETURNING，server onupdate=func.now() 让 updated_at 被 expire；
    # 不显式 refresh 的话 supplier_to_out 读它会触发同步懒加载 → AsyncSession MissingGreenlet → 500
    await db.refresh(supplier, ["updated_at"])
    return await supplier_to_out(db, supplier)


@router_admin.post(
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


@router_admin.get(
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


@router_auth_public.get(
    "/api-suppliers",
    response_model=ApiSupplierTemplatePublicListOut,
)
async def list_public_api_suppliers(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiSupplierTemplatePublicListOut:
    settings_out = await read_byok_settings(db)
    if not settings_out.mode_enabled or not settings_out.byok_signup_enabled:
        return ApiSupplierTemplatePublicListOut(items=[])
    suppliers = (
        (
            await db.execute(
                select(ApiSupplierTemplate)
                .where(
                    ApiSupplierTemplate.deleted_at.is_(None),
                    ApiSupplierTemplate.enabled.is_(True),
                    ApiSupplierTemplate.public_signup_enabled.is_(True),
                )
                .order_by(ApiSupplierTemplate.name.asc())
            )
        )
        .scalars()
        .all()
    )
    return ApiSupplierTemplatePublicListOut(
        items=[supplier_to_public_out(supplier) for supplier in suppliers]
    )


@router_auth_public.post("/api-key/verify", response_model=ApiKeyVerifyOut)
async def verify_api_key_public(
    body: ApiKeyVerifyIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiKeyVerifyOut:
    started = time.monotonic()
    settings_out = await read_byok_settings(db)
    if not settings_out.mode_enabled or not settings_out.byok_signup_enabled:
        raise _http("byok_disabled", "BYOK signup is disabled", 403)

    supplier = await db.get(ApiSupplierTemplate, body.supplier_id)
    if (
        supplier is None
        or supplier.deleted_at is not None
        or not supplier.enabled
        or not supplier.public_signup_enabled
    ):
        raise _http("supplier_not_available", "supplier is not available", 404)

    redis = get_redis()
    ip = require_client_ip(request)
    await _VERIFY_IP_LIMITER.check(redis, f"rl:byok:verify:ip:{ip}")
    await _VERIFY_SUPPLIER_LIMITER.check(
        redis, f"rl:byok:verify:supplier:{supplier.id}"
    )
    try:
        key_hash_for_limit = api_key_rate_limit_hash(body.api_key)
    except ValueError as exc:
        raise _http("invalid_api_key", "API key is invalid", 400) from exc
    await _VERIFY_KEY_LIMITER.check(redis, f"rl:byok:verify:key:{key_hash_for_limit}")

    outcome = await validate_api_key_with_supplier(
        db,
        supplier,
        body.api_key,
        validation_model=settings_out.validation_model,
        timeout_ms=settings_out.validation_timeout_ms,
    )
    elapsed = time.monotonic() - started
    if elapsed < _MIN_VALIDATION_SECONDS:
        await asyncio.sleep(_MIN_VALIDATION_SECONDS - elapsed)

    if not outcome.ok:
        await write_audit_isolated(
            event_type="auth.api_key.verify.fail",
            actor_ip_hash=request_ip_hash(request),
            details={
                "supplier_id": supplier.id,
                "error_code": outcome.error_code,
                "http_status": outcome.http_status,
                "latency_ms": outcome.latency_ms,
            },
        )
        raise _http(
            outcome.error_code or "api_key_validation_failed",
            "API key validation failed",
            400,
            http_status=outcome.http_status,
            latency_ms=outcome.latency_ms,
        )

    token, token_hash = create_verification_token_hash()
    key_ciphertext, key_hash, key_hint = encrypt_pending_key(body.api_key)
    verified_at = datetime.now(timezone.utc)
    pending = PendingApiKeyVerification(
        token_hash=token_hash,
        supplier_id=supplier.id,
        key_ciphertext=key_ciphertext,
        key_hash=key_hash,
        key_hint=key_hint,
        challenge_jsonb=outcome.challenge_jsonb,
        verified_at=verified_at,
        expires_at=pending_expires_at(settings_out.pending_token_ttl_seconds),
        ip_hash=request_ip_hash(request),
        ua_hash=hash_text_for_audit(request.headers.get("user-agent")),
    )
    db.add(pending)
    await write_audit(
        db,
        event_type="auth.api_key.verify.success",
        actor_ip_hash=request_ip_hash(request),
        details={"supplier_id": supplier.id, "latency_ms": outcome.latency_ms},
        autocommit=False,
    )
    await db.commit()
    return ApiKeyVerifyOut(
        ok=True,
        verification_token=token,
        supplier_id=supplier.id,
        key_hint=key_hint,
        verified_at=verified_at,
    )


@router_me.get("", response_model=UserApiCredentialListOut)
async def list_my_api_credentials(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserApiCredentialListOut:
    rows = (
        await db.execute(
            select(UserApiCredential, ApiSupplierTemplate)
            .join(
                ApiSupplierTemplate,
                ApiSupplierTemplate.id == UserApiCredential.supplier_id,
            )
            .where(
                UserApiCredential.user_id == user.id,
                UserApiCredential.deleted_at.is_(None),
            )
            .order_by(UserApiCredential.created_at.desc())
        )
    ).all()
    return UserApiCredentialListOut(
        items=[_credential_out(credential, supplier) for credential, supplier in rows]
    )


@router_me.get("/suppliers", response_model=ApiSupplierTemplatePublicListOut)
async def list_bindable_api_suppliers(
    _user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiSupplierTemplatePublicListOut:
    settings_out = await read_byok_settings(db)
    if not settings_out.mode_enabled:
        return ApiSupplierTemplatePublicListOut(items=[])
    suppliers = (
        (
            await db.execute(
                select(ApiSupplierTemplate)
                .where(
                    ApiSupplierTemplate.deleted_at.is_(None),
                    ApiSupplierTemplate.enabled.is_(True),
                    ApiSupplierTemplate.user_bind_enabled.is_(True),
                )
                .order_by(ApiSupplierTemplate.name.asc())
            )
        )
        .scalars()
        .all()
    )
    return ApiSupplierTemplatePublicListOut(
        items=[supplier_to_public_out(supplier) for supplier in suppliers]
    )


@router_me.post(
    "/{credential_id}/probe",
    response_model=UserApiCredentialOut,
    dependencies=[Depends(verify_csrf)],
)
async def probe_my_api_credential(
    credential_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserApiCredentialOut:
    redis = get_redis()
    ip = require_client_ip(request)
    await _PROBE_IP_LIMITER.check(redis, f"rl:byok:probe:ip:{ip}")
    await _PROBE_USER_LIMITER.check(redis, f"rl:byok:probe:user:{user.id}")
    await _PROBE_CREDENTIAL_LIMITER.check(
        redis, f"rl:byok:probe:credential:{credential_id}"
    )

    row = (
        await db.execute(
            select(UserApiCredential, ApiSupplierTemplate)
            .join(
                ApiSupplierTemplate,
                ApiSupplierTemplate.id == UserApiCredential.supplier_id,
            )
            .where(
                UserApiCredential.id == credential_id,
                UserApiCredential.user_id == user.id,
                UserApiCredential.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise _http("not_found", "credential not found", 404)
    credential, supplier = row
    if credential.status != "active":
        raise _http(
            "credential_not_active",
            "only the active credential can be re-probed",
            409,
        )
    if supplier.deleted_at is not None or not supplier.enabled:
        raise _http("supplier_not_available", "supplier is not available", 404)
    key_ciphertext = credential.key_ciphertext
    supplier_id = supplier.id
    await _PROBE_SUPPLIER_LIMITER.check(
        redis, f"rl:byok:probe:supplier:{supplier_id}"
    )
    await db.commit()

    try:
        api_key = decrypt_api_key(key_ciphertext, byok_master_secret())
    except ByokCryptoError as exc:
        await write_audit_isolated(
            event_type="me.api_credential.probe.decrypt_failed",
            user_id=user.id,
            actor_email=user.email,
            actor_ip_hash=request_ip_hash(request),
            details={"credential_id": credential_id, "supplier_id": supplier_id},
        )
        raise _http(
            "credential_unavailable",
            "credential is temporarily unavailable",
            503,
        ) from exc
    try:
        key_hash_for_limit = api_key_rate_limit_hash(api_key)
    except ValueError as exc:
        raise _http("invalid_api_key", "API key is invalid", 400) from exc
    await _PROBE_KEY_LIMITER.check(redis, f"rl:byok:probe:key:{key_hash_for_limit}")

    settings_out = await read_byok_settings(db)
    outcome = await validate_api_key_with_supplier(
        db,
        supplier,
        api_key,
        validation_model=settings_out.validation_model,
        timeout_ms=settings_out.validation_timeout_ms,
    )
    row = (
        await db.execute(
            select(UserApiCredential, ApiSupplierTemplate)
            .join(
                ApiSupplierTemplate,
                ApiSupplierTemplate.id == UserApiCredential.supplier_id,
            )
            .where(
                UserApiCredential.id == credential_id,
                UserApiCredential.user_id == user.id,
                UserApiCredential.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise _http("not_found", "credential not found", 404)
    credential, supplier = row
    if credential.status != "active":
        raise _http(
            "credential_not_active",
            "only the active credential can be re-probed",
            409,
        )
    if supplier.deleted_at is not None or not supplier.enabled:
        raise _http("supplier_not_available", "supplier is not available", 404)
    now = datetime.now(timezone.utc)
    if outcome.ok:
        credential.last_verified_at = now
        credential.last_error_code = None
        credential.rate_limited_until = None
    else:
        credential.last_failed_at = now
        credential.last_error_code = outcome.error_code or "api_key_validation_failed"
        if credential.last_error_code == "invalid_api_key":
            credential.status = "invalid"
        if credential.last_error_code == "key_rate_limited":
            credential.rate_limited_until = now + timedelta(minutes=5)
        else:
            credential.rate_limited_until = None

    await write_audit(
        db,
        event_type=(
            "me.api_credential.probe.success"
            if outcome.ok
            else "me.api_credential.probe.fail"
        ),
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "credential_id": credential.id,
            "supplier_id": supplier.id,
            "ok": outcome.ok,
            "error_code": outcome.error_code,
            "http_status": outcome.http_status,
            "latency_ms": outcome.latency_ms,
        },
        autocommit=False,
    )
    await db.commit()
    await db.refresh(credential, ["updated_at"])
    return _credential_out(credential, supplier)


@router_me.put(
    "/{supplier_id}",
    response_model=UserApiCredentialOut,
    dependencies=[Depends(verify_csrf)],
)
async def put_my_api_credential(
    supplier_id: str,
    body: UserApiCredentialUpdateIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserApiCredentialOut:
    settings_out = await read_byok_settings(db)
    if not settings_out.mode_enabled:
        raise _http("byok_disabled", "BYOK is disabled", 403)
    supplier = await db.get(ApiSupplierTemplate, supplier_id)
    if (
        supplier is None
        or supplier.deleted_at is not None
        or not supplier.enabled
        or not supplier.user_bind_enabled
    ):
        raise _http("supplier_not_available", "supplier is not available", 404)

    outcome = await validate_api_key_with_supplier(
        db,
        supplier,
        body.api_key,
        validation_model=settings_out.validation_model,
        timeout_ms=settings_out.validation_timeout_ms,
    )
    if not outcome.ok:
        await write_audit_isolated(
            event_type="me.api_credential.verify.fail",
            user_id=user.id,
            actor_email=user.email,
            actor_ip_hash=request_ip_hash(request),
            details={
                "supplier_id": supplier.id,
                "error_code": outcome.error_code,
                "http_status": outcome.http_status,
            },
        )
        raise _http(
            outcome.error_code or "api_key_validation_failed",
            "API key validation failed",
            400,
        )

    key_ciphertext, key_hash, key_hint = encrypt_pending_key(body.api_key)
    now = datetime.now(timezone.utc)
    # Why: single UPDATE ... RETURNING flips every active credential to
    # "replaced" atomically. Replaces the previous "SELECT ... FOR UPDATE +
    # setattr in Python loop" pattern, which had a race window between the
    # SELECT and the INSERT below — concurrent PUTs could both see no active
    # row, both insert, and we'd end up with two active credentials per user.
    # The partial unique index `(user_id) WHERE status='active'` (Group B
    # migration) is the durable enforcement; the IntegrityError path below
    # catches the race that escapes this UPDATE.
    update_result = await db.execute(
        update(UserApiCredential)
        .where(
            UserApiCredential.user_id == user.id,
            UserApiCredential.status == "active",
            UserApiCredential.deleted_at.is_(None),
        )
        .values(status="replaced", updated_at=now)
        .returning(UserApiCredential.id)
    )
    replaced_ids = [row[0] for row in update_result.all()]
    credential = UserApiCredential(
        user_id=user.id,
        supplier_id=supplier.id,
        key_ciphertext=key_ciphertext,
        key_hash=key_hash,
        key_hint=key_hint,
        status="active",
        last_verified_at=now,
    )
    db.add(credential)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Why: partial unique on (user_id) WHERE status='active' caught a
        # concurrent insert that raced past our UPDATE. Surface 409 instead
        # of 500 so the client can retry with the freshest server state.
        await db.rollback()
        raise _http(
            "credential_conflict",
            "concurrent credential update; please retry",
            409,
        ) from exc
    await write_audit(
        db,
        event_type=(
            "me.api_credential.replace" if replaced_ids else "me.api_credential.create"
        ),
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "supplier_id": supplier.id,
            "credential_id": credential.id,
            "replaced_credential_ids": replaced_ids,
        },
        autocommit=False,
    )
    await db.commit()
    return _credential_out(credential, supplier)


@router_me.delete(
    "/{credential_id}",
    dependencies=[Depends(verify_csrf)],
)
async def revoke_my_api_credential(
    credential_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    credential = (
        await db.execute(
            select(UserApiCredential)
            .where(
                UserApiCredential.id == credential_id,
                UserApiCredential.user_id == user.id,
                UserApiCredential.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if credential is None:
        raise _http("not_found", "credential not found", 404)
    credential.status = "revoked"
    credential.deleted_at = datetime.now(timezone.utc)
    await write_audit(
        db,
        event_type="me.api_credential.revoke",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={"credential_id": credential_id, "supplier_id": credential.supplier_id},
        autocommit=False,
    )
    await db.commit()
    return {"ok": True}


def _credential_out(
    credential: UserApiCredential,
    supplier: ApiSupplierTemplate,
) -> UserApiCredentialOut:
    return UserApiCredentialOut(
        id=credential.id,
        supplier_id=credential.supplier_id,
        supplier_name=supplier.name,
        key_hint=credential.key_hint,
        status=credential.status,
        last_verified_at=credential.last_verified_at,
        last_failed_at=credential.last_failed_at,
        last_error_code=credential.last_error_code,
        rate_limited_until=credential.rate_limited_until,
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )
