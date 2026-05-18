"""BYOK service helpers used by auth, admin and /me routes."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.byok import (
    BYOK_DEFAULT_CHAT_MODEL,
    BYOK_DEFAULT_FAST_MODEL,
    BYOK_DEFAULT_PENDING_TOKEN_TTL_SECONDS,
    BYOK_DEFAULT_VALIDATION_MODEL,
    BYOK_DEFAULT_VALIDATION_TIMEOUT_MS,
    answer_matches_expected,
    api_key_hint,
    build_validation_request,
    encrypt_api_key,
    extract_response_output_text,
    extract_sse_output_text,
    generate_arithmetic_challenge,
    hash_api_key,
    hash_verification_token,
    new_verification_token,
    validate_api_key_shape,
)
from lumen_core.models import (
    ApiSupplierTemplate,
    SystemSetting,
    UserApiCredential,
)
from lumen_core.providers import (
    ProviderProxyDefinition,
    parse_proxy_json,
    resolve_provider_proxy_url,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    ApiSupplierTemplateOut,
    ApiSupplierTemplatePublicOut,
    ByokPurpose,
    ByokSettingsOut,
)
from lumen_core.url_security import assert_public_http_target

from .config import settings
from .runtime_settings import get_setting  # type: ignore[attr-defined]


_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_DEV_ENVS = {"dev", "development", "local", "test"}
_MODEL_UNAVAILABLE_MARKERS = (
    "model_not_found",
    "model_not_available",
    "model not found",
    "does not have access to model",
    "model_not_found",
)

@dataclass(frozen=True)
class ValidationOutcome:
    ok: bool
    error_code: str | None
    http_status: int | None
    latency_ms: int
    key_hint: str | None
    challenge_jsonb: dict[str, Any]


def byok_master_secret() -> str:
    return settings.byok_api_key_master_secret.strip()


def is_dev_env() -> bool:
    return settings.app_env.strip().lower() in _DEV_ENVS


def slugify_supplier(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug[:80] or "supplier"


async def normalize_base_url(raw: str) -> str:
    return await assert_public_http_target(
        raw,
        allow_http=is_dev_env(),
        allow_private=is_dev_env(),
        allow_unresolved=is_dev_env(),
    )


async def read_byok_settings(db: AsyncSession) -> ByokSettingsOut:
    async def _int(key: str, default: int) -> int:
        spec = get_spec(key)
        if spec is None:
            return default
        raw = await get_setting(db, spec)
        try:
            return int(raw) if raw is not None else default
        except ValueError:
            return default

    async def _str(key: str, default: str) -> str:
        spec = get_spec(key)
        if spec is None:
            return default
        raw = await get_setting(db, spec)
        return str(raw).strip() if raw else default

    return ByokSettingsOut(
        mode_enabled=bool(await _int("byok.mode_enabled", 0)),
        byok_signup_enabled=bool(await _int("auth.byok_signup_enabled", 0)),
        byok_signup_bypasses_allowlist=bool(
            await _int("auth.byok_signup_bypasses_allowlist", 0)
        ),
        fallback_to_admin_provider=bool(
            await _int("byok.fallback_to_admin_provider", 0)
        ),
        validation_model=await _str(
            "byok.validation_model", BYOK_DEFAULT_VALIDATION_MODEL
        ),
        validation_timeout_ms=await _int(
            "byok.validation_timeout_ms", BYOK_DEFAULT_VALIDATION_TIMEOUT_MS
        ),
        pending_token_ttl_seconds=await _int(
            "byok.pending_token_ttl_seconds",
            BYOK_DEFAULT_PENDING_TOKEN_TTL_SECONDS,
        ),
    )


# In-process TTL cache for read_byok_settings. The hot path is
# /conversations/{id}/messages, which used to issue 4 SELECTs to
# system_settings on every send. Per-process cache with a 30 s TTL keeps
# the read latency near zero in steady state; admin patches invalidate
# explicitly so changes propagate within one round-trip per process.
# This cache intentionally crosses request boundaries — settings are
# global and not user-scoped, so leaking values across users is fine.
_BYOK_SETTINGS_CACHE: tuple[float, ByokSettingsOut] | None = None
_BYOK_SETTINGS_TTL_SECONDS = 30.0


async def read_byok_settings_cached(db: AsyncSession) -> ByokSettingsOut:
    """Cached variant of read_byok_settings (TTL ~30 s).

    Use this on hot read paths (POST /messages). For write paths or when
    consistency matters within the same request, prefer read_byok_settings.
    """
    global _BYOK_SETTINGS_CACHE
    now = time.monotonic()
    cached = _BYOK_SETTINGS_CACHE
    if cached is not None and now - cached[0] < _BYOK_SETTINGS_TTL_SECONDS:
        return cached[1]
    fresh = await read_byok_settings(db)
    _BYOK_SETTINGS_CACHE = (now, fresh)
    return fresh


def invalidate_byok_settings_cache() -> None:
    """Clear the BYOK settings cache. Call after admin PATCH commits."""
    global _BYOK_SETTINGS_CACHE
    _BYOK_SETTINGS_CACHE = None


async def supplier_to_out(
    db: AsyncSession,
    supplier: ApiSupplierTemplate,
) -> ApiSupplierTemplateOut:
    active_count = int(
        (
            await db.execute(
                select(func.count(UserApiCredential.id)).where(
                    UserApiCredential.supplier_id == supplier.id,
                    UserApiCredential.status == "active",
                    UserApiCredential.deleted_at.is_(None),
                )
            )
        ).scalar()
        or 0
    )
    return ApiSupplierTemplateOut(
        id=supplier.id,
        name=supplier.name,
        slug=supplier.slug,
        base_url=supplier.base_url,
        enabled=supplier.enabled,
        public_signup_enabled=supplier.public_signup_enabled,
        user_bind_enabled=supplier.user_bind_enabled,
        purposes=[cast(ByokPurpose, purpose) for purpose in (supplier.purposes or [])],
        validation_model=supplier.validation_model,
        default_chat_model=supplier.default_chat_model,
        fast_chat_model=supplier.fast_chat_model,
        validation_timeout_ms=supplier.validation_timeout_ms,
        proxy_name=supplier.proxy_name,
        text_concurrency_per_key=supplier.text_concurrency_per_key,
        image_concurrency_per_key=supplier.image_concurrency_per_key,
        capabilities_jsonb=supplier.capabilities_jsonb or {},
        active_credentials=active_count,
        created_at=supplier.created_at,
        updated_at=supplier.updated_at,
    )


def supplier_to_public_out(
    supplier: ApiSupplierTemplate,
) -> ApiSupplierTemplatePublicOut:
    return ApiSupplierTemplatePublicOut(
        id=supplier.id,
        name=supplier.name,
        purposes=[cast(ByokPurpose, purpose) for purpose in (supplier.purposes or [])],
        validation_model=supplier.validation_model,
    )


def _responses_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"


async def resolve_supplier_proxy(
    db: AsyncSession,
    supplier: ApiSupplierTemplate,
) -> ProviderProxyDefinition | None:
    if not supplier.proxy_name:
        return None
    raw = (
        await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == "providers")
        )
    ).scalar_one_or_none()
    proxies, _errors = parse_proxy_json(raw)
    for proxy in proxies:
        if proxy.name == supplier.proxy_name and proxy.enabled:
            return proxy
    return None


async def validate_api_key_with_supplier(
    db: AsyncSession,
    supplier: ApiSupplierTemplate,
    api_key: str,
    *,
    validation_model: str | None = None,
    timeout_ms: int | None = None,
) -> ValidationOutcome:
    started = time.monotonic()
    try:
        key = validate_api_key_shape(api_key)
    except ValueError:
        return ValidationOutcome(
            ok=False,
            error_code="invalid_api_key",
            http_status=None,
            latency_ms=int((time.monotonic() - started) * 1000),
            key_hint=None,
            challenge_jsonb={},
        )
    try:
        base_url = await normalize_base_url(supplier.base_url)
    except ValueError:
        return ValidationOutcome(
            ok=False,
            error_code="invalid_supplier_url",
            http_status=None,
            latency_ms=int((time.monotonic() - started) * 1000),
            key_hint=api_key_hint(key),
            challenge_jsonb={},
        )
    challenge = generate_arithmetic_challenge()
    challenge_jsonb = challenge.as_json()
    model = (
        validation_model or supplier.validation_model or BYOK_DEFAULT_VALIDATION_MODEL
    )
    body = build_validation_request(challenge, model=model)
    headers = {
        "authorization": f"Bearer {key}",
        "content-type": "application/json",
    }
    proxy = await resolve_supplier_proxy(db, supplier)
    proxy_url = await resolve_provider_proxy_url(proxy)
    effective_timeout = max(
        1000,
        min(120000, int(timeout_ms or supplier.validation_timeout_ms or 15000)),
    )
    http_status: int | None = None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(effective_timeout / 1000),
            proxy=proxy_url,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            resp = await client.post(
                _responses_url(base_url),
                json=body,
                headers=headers,
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        http_status = resp.status_code
        if resp.status_code >= 400:
            return ValidationOutcome(
                ok=False,
                error_code=_classify_validation_http_error(resp),
                http_status=http_status,
                latency_ms=latency_ms,
                key_hint=api_key_hint(key),
                challenge_jsonb=challenge_jsonb,
            )
        try:
            payload = resp.json()
            text = extract_response_output_text(payload)
        except Exception:  # noqa: BLE001
            text = extract_sse_output_text(resp.text)
            if not text:
                return ValidationOutcome(
                    ok=False,
                    error_code="invalid_supplier_response",
                    http_status=http_status,
                    latency_ms=latency_ms,
                    key_hint=api_key_hint(key),
                    challenge_jsonb=challenge_jsonb,
                )
        if not answer_matches_expected(text, challenge.expected):
            return ValidationOutcome(
                ok=False,
                error_code="validation_wrong_answer",
                http_status=http_status,
                latency_ms=latency_ms,
                key_hint=api_key_hint(key),
                challenge_jsonb=challenge_jsonb,
            )
        return ValidationOutcome(
            ok=True,
            error_code=None,
            http_status=http_status,
            latency_ms=latency_ms,
            key_hint=api_key_hint(key),
            challenge_jsonb=challenge_jsonb,
        )
    except httpx.TimeoutException:
        return ValidationOutcome(
            ok=False,
            error_code="validation_timeout",
            http_status=http_status,
            latency_ms=int((time.monotonic() - started) * 1000),
            key_hint=api_key_hint(key),
            challenge_jsonb=challenge_jsonb,
        )
    except Exception:  # noqa: BLE001
        return ValidationOutcome(
            ok=False,
            error_code="supplier_transient_error",
            http_status=http_status,
            latency_ms=int((time.monotonic() - started) * 1000),
            key_hint=api_key_hint(key),
            challenge_jsonb=challenge_jsonb,
        )


def _classify_validation_http_error(resp: httpx.Response) -> str:
    status = resp.status_code
    text = resp.text[:2000].lower()
    try:
        obj = resp.json()
    except Exception:  # noqa: BLE001
        obj = None
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            candidates = [
                err.get("code"),
                err.get("type"),
                err.get("message"),
            ]
            text = " ".join(str(c).lower() for c in candidates if c is not None)
    if status in (401, 403):
        return "invalid_api_key"
    if status in (404, 405):
        return "supplier_unsupported"
    if any(marker in text for marker in _MODEL_UNAVAILABLE_MARKERS):
        return "model_not_available"
    if status == 429:
        return "key_rate_limited"
    if 500 <= status < 600:
        return "supplier_transient_error"
    return "invalid_supplier_response"


def encrypt_pending_key(api_key: str) -> tuple[str, str, str]:
    key = validate_api_key_shape(api_key)
    secret = byok_master_secret()
    return encrypt_api_key(key, secret), hash_api_key(key, secret), api_key_hint(key)


def api_key_rate_limit_hash(api_key: str) -> str:
    key = validate_api_key_shape(api_key)
    return hash_api_key(key, byok_master_secret())[:32]


def create_verification_token_hash() -> tuple[str, str]:
    token = new_verification_token()
    return token, hash_verification_token(token, byok_master_secret())


def verification_token_hash(token: str) -> str:
    return hash_verification_token(token, byok_master_secret())


def hash_text_for_audit(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def pending_expires_at(ttl_seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)


def default_supplier_values() -> dict[str, Any]:
    return {
        "validation_model": BYOK_DEFAULT_VALIDATION_MODEL,
        "default_chat_model": BYOK_DEFAULT_CHAT_MODEL,
        "fast_chat_model": BYOK_DEFAULT_FAST_MODEL,
        "validation_timeout_ms": BYOK_DEFAULT_VALIDATION_TIMEOUT_MS,
    }
