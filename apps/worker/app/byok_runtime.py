"""Resolve and maintain BYOK user API credential runtimes."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.byok import ByokCryptoError, decrypt_api_key
from lumen_core.constants import GenerationErrorCode as EC
from lumen_core.models import (
    ApiSupplierTemplate,
    SystemSetting,
    UserApiCredential,
)
from lumen_core.providers import parse_proxy_json
from lumen_core.url_security import assert_public_http_target

from .config import settings
from .provider_pool import ResolvedProvider
from .upstream import UpstreamError


logger = logging.getLogger(__name__)


# 由 tasks 模块（generation.py / completion.py / upstream.py）共享的 BYOK provider
# 名前缀。任何 admin pool 报告（report_image_*, account_limiter.record_*）必须
# 跳过 BYOK provider，否则会污染共享 provider 池的健康度 / 配额计数。
_BYOK_PROVIDER_PREFIX = "user:"
_DEV_ENVS = {"dev", "development", "local", "test"}
_BASE_URL_VALIDATION_TTL_SECONDS = 10 * 60.0
_BASE_URL_VALIDATION_CACHE: dict[tuple[str, bool], tuple[float, str]] = {}


def is_byok_provider(provider: Any) -> bool:
    """True 表示此 provider 来自 BYOK 用户凭证（不参与 admin pool 计数）。"""
    name = getattr(provider, "name", "") or ""
    return isinstance(name, str) and name.startswith(_BYOK_PROVIDER_PREFIX)


# 兼容下划线前缀别名（review #2 要求）。
_is_byok_provider = is_byok_provider


def _credential_provider_name(supplier: ApiSupplierTemplate, credential_id: str) -> str:
    prefix = credential_id.replace("-", "")[:12]
    return f"{_BYOK_PROVIDER_PREFIX}{supplier.slug}:{prefix}"


def _is_dev_env() -> bool:
    return settings.app_env.strip().lower() in _DEV_ENVS


def clear_base_url_validation_cache() -> None:
    _BASE_URL_VALIDATION_CACHE.clear()


async def _validate_supplier_base_url(raw_base_url: str) -> str:
    dev_env = _is_dev_env()
    cache_key = (raw_base_url.strip(), dev_env)
    now = time.monotonic()
    cached = _BASE_URL_VALIDATION_CACHE.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    safe_base_url = await assert_public_http_target(
        raw_base_url,
        allow_http=dev_env,
        allow_private=dev_env,
        allow_unresolved=dev_env,
    )
    _BASE_URL_VALIDATION_CACHE[cache_key] = (
        now + _BASE_URL_VALIDATION_TTL_SECONDS,
        safe_base_url,
    )
    return safe_base_url


async def resolve_user_credential_runtime(
    db: AsyncSession,
    credential_id: str,
) -> ResolvedProvider:
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserApiCredential, ApiSupplierTemplate)
        .join(ApiSupplierTemplate, ApiSupplierTemplate.id == UserApiCredential.supplier_id)
        .where(
            UserApiCredential.id == credential_id,
            UserApiCredential.deleted_at.is_(None),
            UserApiCredential.status == "active",
            ApiSupplierTemplate.deleted_at.is_(None),
            ApiSupplierTemplate.enabled.is_(True),
            or_(
                UserApiCredential.rate_limited_until.is_(None),
                UserApiCredential.rate_limited_until <= now,
            ),
        )
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        # 进一步区分 rate_limited 还在窗口内（可恢复）和 credential 不可用（终态）
        raw = await db.get(UserApiCredential, credential_id)
        if (
            raw is not None
            and raw.rate_limited_until is not None
            and raw.rate_limited_until > now
        ):
            retry_after = max(1, int((raw.rate_limited_until - now).total_seconds()))
            raise UpstreamError(
                "user API key is rate limited",
                status_code=429,
                error_code=EC.UPSTREAM_RATE_LIMITED.value,
                payload={
                    "credential_id": credential_id,
                    "reason": "rate_limited",
                    "retry_after": retry_after,
                },
            )
        raise UpstreamError(
            "user API credential is not active",
            status_code=403,
            error_code=EC.UPSTREAM_AUTH_ERROR.value,
            payload={"credential_id": credential_id},
        )
    credential, supplier = row
    try:
        api_key = decrypt_api_key(
            credential.key_ciphertext,
            settings.byok_api_key_master_secret,
        )
    except ByokCryptoError as exc:
        logger.error(
            "byok decrypt failed for credential %s: %s",
            credential_id,
            type(exc).__name__,
        )
        # decrypt 失败极可能是 master_secret 配错（vs 真正的 invalid key），不要把
        # credential 标记成 invalid——交给 record_user_credential_runtime_error
        # 按 byok_master_secret_mismatch 跳过 status 改写。
        raise UpstreamError(
            "user API credential cannot be decrypted",
            status_code=500,
            error_code="byok_master_secret_mismatch",
            payload={"credential_id": credential_id},
        ) from exc
    proxy = await _resolve_supplier_proxy(db, supplier.proxy_name)
    try:
        safe_base_url = await _validate_supplier_base_url(supplier.base_url)
    except ValueError as exc:
        raise UpstreamError(
            "user API supplier URL is not allowed",
            status_code=403,
            error_code=EC.UPSTREAM_INVALID_REQUEST.value,
            payload={"credential_id": credential_id},
        ) from exc
    caps = supplier.capabilities_jsonb or {}
    image_jobs_enabled = bool(caps.get("image_jobs_enabled", False))
    image_jobs_endpoint = str(caps.get("image_jobs_endpoint", "auto"))
    return ResolvedProvider(
        name=_credential_provider_name(supplier, credential.id),
        base_url=safe_base_url,
        api_key=api_key,
        proxy=proxy,
        image_jobs_enabled=image_jobs_enabled,
        image_jobs_endpoint=image_jobs_endpoint,
        image_jobs_endpoint_lock=False,
        image_jobs_base_url="",
        image_edit_input_transport="file",
        image_concurrency=max(1, int(supplier.image_concurrency_per_key or 1)),
        # supplier.purposes 没配时 purposes 应当是空，由 task 入口根据自己的 purpose
        # 拒绝；避免老代码 fallback 成 ["chat","image"] 让任意 supplier 误用 image。
        purposes=tuple(supplier.purposes or []),
        responses_supported=True,
        image_generations_supported=None,
        image_responses_supported=True,
    )


async def _resolve_supplier_proxy(db: AsyncSession, proxy_name: str | None) -> Any | None:
    if not proxy_name:
        return None
    raw = (
        await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == "providers")
        )
    ).scalar_one_or_none()
    proxies, _errors = parse_proxy_json(raw)
    for proxy in proxies:
        if proxy.name == proxy_name and proxy.enabled:
            return proxy
    return None


def classify_user_credential_error(exc: BaseException) -> tuple[bool, str | None]:
    status = getattr(exc, "status_code", None)
    code = str(getattr(exc, "error_code", "") or "").lower()
    message = str(exc).lower()
    # decrypt 失败专属 error_code：不污染 credential 状态。
    if code == "byok_master_secret_mismatch":
        return True, "byok_master_secret_mismatch"
    if status in (401, 403) or code in {
        EC.AUTHENTICATION_ERROR.value,
        EC.PERMISSION_ERROR.value,
        EC.UNAUTHORIZED.value,
        EC.UPSTREAM_AUTH_ERROR.value,
    }:
        return True, "invalid_api_key"
    if status == 429 or code in {
        EC.RATE_LIMIT_ERROR.value,
        EC.RATE_LIMIT_EXCEEDED.value,
        EC.RATE_LIMITED.value,
        EC.UPSTREAM_RATE_LIMITED.value,
    }:
        return True, "key_rate_limited"
    # 仅在上游没给 status_code / error_code 时，才允许 message 启发式做最后兜底。
    # 上游给了 status_code 时一律按 status_code 分类，避免 message 把 200 类的
    # "rate limit hint" 误升级为 terminal credential 写回。
    if status is None and not code:
        if "rate limit" in message or "quota" in message:
            return True, "key_rate_limited"
    return False, None


async def record_user_credential_runtime_error(
    credential_id: str | None,
    exc: BaseException,
) -> None:
    if not credential_id:
        return
    _terminal, error_code = classify_user_credential_error(exc)
    if not error_code:
        return
    # decrypt 失败 = 部署级错误（master_secret 漂移），不应改写用户 credential 状态。
    if error_code == "byok_master_secret_mismatch":
        logger.warning(
            "byok credential %s decrypt failed; status preserved", credential_id
        )
        return
    from .db import SessionLocal

    async with SessionLocal() as session:
        credential = await session.get(UserApiCredential, credential_id)
        if credential is None:
            return
        now = datetime.now(timezone.utc)
        credential.last_failed_at = now
        credential.last_error_code = error_code
        if error_code == "invalid_api_key":
            credential.status = "invalid"
        elif error_code == "key_rate_limited":
            credential.rate_limited_until = now + timedelta(minutes=5)
        try:
            await session.commit()
        except Exception as commit_exc:  # noqa: BLE001
            # commit 失败不能让主任务异常链丢失原始 upstream error，仅 warn + rollback。
            logger.warning(
                "byok credential %s record commit failed: %s",
                credential_id,
                commit_exc,
            )
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                pass


def byok_error_message(error_code: str) -> str:
    if error_code == "invalid_api_key":
        return "user API key is invalid or unauthorized"
    if error_code == "key_rate_limited":
        return "user API key is rate limited or out of quota"
    if error_code == "byok_master_secret_mismatch":
        return "BYOK key cannot be decrypted (master secret mismatch)"
    if error_code == "byok_purpose_mismatch":
        return "user API key supplier does not allow this task purpose"
    return "user API key failed"


def byok_error_to_generation_code(error_code: str) -> str:
    if error_code == "invalid_api_key":
        return EC.UPSTREAM_AUTH_ERROR.value
    if error_code == "key_rate_limited":
        return EC.UPSTREAM_RATE_LIMITED.value
    if error_code == "byok_master_secret_mismatch":
        return EC.UPSTREAM_ERROR.value
    if error_code == "byok_purpose_mismatch":
        return EC.UPSTREAM_AUTH_ERROR.value
    return EC.UPSTREAM_ERROR.value
