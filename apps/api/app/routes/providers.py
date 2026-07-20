"""管理员 Provider Pool 管理与探活。

GET  /admin/providers       — 列出 provider（API Key 脱敏）
PUT  /admin/providers       — 结构化保存（支持 key 保留）
POST /admin/providers/probe — 手动探活（支持按名称过滤）
PATCH /admin/providers/{name}/enabled — 单字段启停
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Awaitable, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.byok import (
    build_provider_probe_request,
    extract_response_output_text as _extract_response_output_text,
    extract_sse_output_text as _extract_sse_output_text,
)
from lumen_core.providers import (
    ProviderProxyDefinition,
    endpoint_kind_allowed,
    normalize_provider_purposes,
    normalize_image_edit_input_transport,
    parse_proxy_item,
    resolve_provider_proxy_url,
)
from lumen_core.video_providers import parse_video_provider_config_json
from lumen_core.models import SystemSetting
from lumen_core.runtime_settings import get_spec, validate_providers
from lumen_core.schemas import (
    ProviderItemOut,
    ProviderPurpose,
    ProviderProbeResult,
    ProviderProxyOut,
    ProviderStatsItem,
    ProviderStatsOut,
    ProvidersOut,
    ProvidersProbeIn,
    ProvidersProbeOut,
    ProvidersUpdateIn,
    VideoProviderItemOut,
    VideoProvidersOut,
    VideoProvidersUpdateIn,
)

from ..audit import hash_email, request_ip_hash, write_audit
from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ..services.admin_model_cache import invalidate_admin_models_cache
from ..services.provider_config import (
    ensure_enabled_provider_proxies,
    ensure_enabled_video_provider_proxies,
    parse_provider_config as _parse_config,
    parse_provider_items as _parse_items,
    read_providers as _read_providers,
)
from ._video_provider_update import (
    VideoProviderUpdateError,
    build_video_provider_update,
    validate_video_provider_items,
)

router = APIRouter(prefix="/admin/providers", tags=["admin-providers"])

_PROBE_TIMEOUT_S = 15.0
_PROVIDERS_MAX_LEN = 65536
_VIDEO_PROVIDERS_MAX_LEN = 65536


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


class ProviderEnabledPatchIn(BaseModel):
    enabled: bool


def _responses_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "..." + key[-4:]


def _mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def _normalize_proxy_type(value: str, *, fallback: bool = False) -> str:
    raw = (value or "socks5").strip().lower()
    if raw in {"s5", "socks", "socks5", "socks5h"}:
        return "socks5"
    if raw == "ssh":
        return "ssh"
    return "socks5" if fallback else raw


def _safe_int(value: object, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = (
            int(value)
            if isinstance(value, (str, bytes, bytearray, int, float))
            else default
        )
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _normalize_capability(raw: Any) -> bool | None:
    """Capability tri-state from persisted dict shape. None = 未知，保留旧行为。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
    return None


def _normalize_bool(raw: Any, *, default: bool = False) -> bool:
    parsed = _normalize_capability(raw)
    return default if parsed is None else parsed


def _normalize_purposes(raw: Any) -> list[ProviderPurpose]:
    return [
        cast(ProviderPurpose, purpose) for purpose in normalize_provider_purposes(raw)
    ]


def _normalize_image_edit_transport(
    raw: Any,
) -> Literal["url", "file"]:
    return cast(
        Literal["url", "file"],
        normalize_image_edit_input_transport(raw),
    )


def _to_out(it: dict, idx: int) -> ProviderItemOut:
    endpoint = _normalize_image_jobs_endpoint(it.get("image_jobs_endpoint"))
    return ProviderItemOut(
        name=it.get("name") or f"provider-{idx}",
        base_url=it.get("base_url", ""),
        api_key_hint=_mask_key(it.get("api_key", "")),
        priority=_safe_int(it.get("priority"), 0),
        weight=_safe_int(it.get("weight"), 1, minimum=1),
        enabled=_normalize_bool(it.get("enabled"), default=True),
        purposes=_normalize_purposes(it.get("purposes")),
        proxy=it.get("proxy") if isinstance(it.get("proxy"), str) else None,
        image_jobs_enabled=_normalize_bool(
            it.get("image_jobs_enabled"),
            default=False,
        ),
        image_jobs_endpoint=endpoint,
        image_jobs_endpoint_lock=_normalize_image_jobs_endpoint_lock(
            it.get("image_jobs_endpoint_lock"), endpoint
        ),
        image_jobs_base_url=_normalize_image_jobs_base_url(
            it.get("image_jobs_base_url")
        ),
        image_edit_input_transport=_normalize_image_edit_transport(
            it.get("image_edit_input_transport")
        ),
        image_concurrency=_normalize_image_concurrency(it.get("image_concurrency")),
        responses_supported=_normalize_capability(it.get("responses_supported")),
        image_generations_supported=_normalize_capability(
            it.get("image_generations_supported")
        ),
        image_responses_supported=_normalize_capability(
            it.get("image_responses_supported")
        ),
    )


_IMAGE_JOBS_ENDPOINT_VALUES = {"auto", "generations", "responses"}


def _normalize_image_jobs_endpoint(raw: Any) -> str:
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in _IMAGE_JOBS_ENDPOINT_VALUES:
            return value
    return "auto"


def _normalize_image_jobs_endpoint_lock(raw: Any, endpoint: str) -> bool:
    # auto 时 lock 没有意义——避免 UI 残留 lock=true 但 endpoint 改回 auto 的脏配置。
    if endpoint == "auto":
        return False
    return _normalize_bool(raw, default=False)


def _normalize_image_jobs_base_url(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    if not (value.startswith("http://") or value.startswith("https://")):
        return ""
    return value


_IMAGE_CONCURRENCY_MAX = 32


def _normalize_image_concurrency(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(_IMAGE_CONCURRENCY_MAX, value))


def _to_proxy_out(it: dict, idx: int) -> ProviderProxyOut:
    proxy_type = _normalize_proxy_type(
        it.get("type") or it.get("protocol") or "socks5",
        fallback=True,
    )
    port = _safe_int(
        it.get("port"),
        22 if proxy_type == "ssh" else 1080,
        minimum=1,
    )
    port = min(65535, port)
    return ProviderProxyOut(
        name=it.get("name") or f"proxy-{idx}",
        type=proxy_type,
        host=it.get("host", ""),
        port=port,
        username=it.get("username") if isinstance(it.get("username"), str) else None,
        password_hint=_mask_secret(it.get("password")),
        private_key_path=(
            it.get("private_key_path")
            if isinstance(it.get("private_key_path"), str)
            else None
        ),
        enabled=_normalize_bool(it.get("enabled"), default=True),
    )


async def _read_setting_value(db: AsyncSession, key: str) -> str | None:
    return (
        await db.execute(select(SystemSetting.value).where(SystemSetting.key == key))
    ).scalar_one_or_none()


async def _upsert_setting_value(db: AsyncSession, key: str, value: str) -> None:
    existing = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    ).scalar_one_or_none()
    if existing is None:
        db.add(SystemSetting(key=key, value=value))
    else:
        existing.value = value


async def _delete_setting_value(db: AsyncSession, key: str) -> None:
    existing = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    ).scalar_one_or_none()
    if existing is not None:
        await db.delete(existing)


async def _read_video_providers_raw(db: AsyncSession) -> tuple[str | None, str]:
    row = await _read_setting_value(db, "video.providers")
    if row is not None and row != "":
        return row, "db"
    spec = get_spec("video.providers")
    if spec:
        env_val = os.environ.get(spec.env_fallback)
        if env_val is not None and env_val != "":
            return env_val, "env"
    return None, "none"


async def _read_video_enabled(db: AsyncSession) -> bool:
    raw = await _read_setting_value(db, "video.enabled")
    if raw is None or raw == "":
        spec = get_spec("video.enabled")
        raw = os.environ.get(spec.env_fallback) if spec else None
    return _normalize_bool(raw, default=False)


def _parse_video_raw_config(raw: str | None) -> tuple[list[dict], list[dict]]:
    if not raw:
        return [], []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [], []
    if isinstance(value, list):
        return [it for it in value if isinstance(it, dict)], []
    if not isinstance(value, dict):
        return [], []
    providers = value.get("providers", [])
    proxies = value.get("proxies", [])
    if not isinstance(providers, list):
        providers = []
    if not isinstance(proxies, list):
        proxies = []
    return (
        [it for it in providers if isinstance(it, dict)],
        [it for it in proxies if isinstance(it, dict)],
    )


def _to_video_provider_out(provider: Any) -> VideoProviderItemOut:
    is_volcano = provider.kind == "volcano"
    return VideoProviderItemOut(
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        api_key_hint=_mask_key(provider.api_key),
        access_key_id_hint=(_mask_key(provider.access_key_id) if is_volcano else None),
        secret_access_key_hint=(
            _mask_secret(provider.secret_access_key) if is_volcano else None
        ),
        project_name=provider.project_name if is_volcano else None,
        region=provider.region if is_volcano else None,
        asset_management_ready=provider.asset_management_ready,
        enabled=provider.enabled,
        priority=provider.priority,
        weight=provider.weight,
        concurrency=provider.concurrency,
        supports_idempotency=provider.supports_idempotency,
        proxy=provider.proxy_name,
        models=dict(provider.models or {}),
    )


def _video_proxy_options(
    raw_video: str | None,
    raw_shared: str | None,
) -> list[ProviderProxyOut]:
    _shared_items, shared_proxies = _parse_config(raw_shared or "")
    _video_items, video_proxies = _parse_video_raw_config(raw_video)
    items = [*shared_proxies, *video_proxies]
    out: list[ProviderProxyOut] = []
    seen: set[str] = set()
    for idx, item in enumerate(items):
        proxy = _to_proxy_out(item, idx)
        if proxy.name in seen:
            continue
        seen.add(proxy.name)
        out.append(proxy)
    return out


@router.get("", response_model=ProvidersOut)
async def list_providers(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProvidersOut:
    raw, source = await _read_providers(db)
    if not raw:
        return ProvidersOut(items=[], source=source)
    items, proxies = _parse_config(raw)
    return ProvidersOut(
        items=[_to_out(it, i) for i, it in enumerate(items)],
        proxies=[_to_proxy_out(it, i) for i, it in enumerate(proxies)],
        source=source,
    )


@router.get("/video", response_model=VideoProvidersOut)
async def list_video_providers(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoProvidersOut:
    raw_video, source = await _read_video_providers_raw(db)
    raw_shared, _shared_source = await _read_providers(db)
    providers, _proxies, errors = parse_video_provider_config_json(
        raw_video,
        shared_provider_raw=raw_shared,
    )
    if errors:
        raise _http("invalid_request", "; ".join(errors), 422)
    return VideoProvidersOut(
        enabled=await _read_video_enabled(db),
        items=[_to_video_provider_out(provider) for provider in providers],
        proxies=_video_proxy_options(raw_video, raw_shared),
        source=source,
    )


def _validated_video_provider_update(
    body: VideoProvidersUpdateIn,
    *,
    old_raw: str | None,
    raw_shared: str | None,
) -> tuple[list[dict[str, Any]], str]:
    if body.enabled and not body.items:
        raise _http(
            "invalid_request",
            "开启视频生成前至少需要一个视频供应商",
            422,
        )
    try:
        validate_video_provider_items(body.items)
    except VideoProviderUpdateError as exc:
        raise _http("invalid_request", str(exc), 422) from exc

    old_items, old_video_proxies = _parse_video_raw_config(old_raw)
    try:
        payload = build_video_provider_update(
            body.items,
            old_items=old_items,
            old_video_proxies=old_video_proxies,
            shared_proxies=_parse_config(raw_shared or "")[1],
        )
    except VideoProviderUpdateError as exc:
        raise _http("invalid_request", str(exc), 422) from exc

    rows = payload.rows
    raw_json = payload.raw_json
    if rows and len(raw_json) > _VIDEO_PROVIDERS_MAX_LEN:
        raise _http(
            "invalid_request",
            f"video.providers JSON 超过 {_VIDEO_PROVIDERS_MAX_LEN} 字符",
            422,
        )
    if not rows:
        return rows, raw_json

    parsed, _proxies, errors = parse_video_provider_config_json(
        raw_json,
        shared_provider_raw=raw_shared,
    )
    if errors:
        raise _http("invalid_request", "; ".join(errors), 422)
    if not parsed:
        raise _http("invalid_request", "video.providers 缺少供应商", 422)
    try:
        ensure_enabled_video_provider_proxies(
            raw_json,
            shared_provider_raw=raw_shared,
        )
    except ValueError as exc:
        raise _http("invalid_request", str(exc), 422) from exc
    return rows, raw_json


@router.put(
    "/video",
    response_model=VideoProvidersOut,
    dependencies=[Depends(verify_csrf)],
)
async def update_video_providers(
    body: VideoProvidersUpdateIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoProvidersOut:
    old_raw, _old_source = await _read_video_providers_raw(db)
    raw_shared, _shared_source = await _read_providers(db)
    rows, raw_json = _validated_video_provider_update(
        body,
        old_raw=old_raw,
        raw_shared=raw_shared,
    )

    await _upsert_setting_value(db, "video.enabled", "1" if body.enabled else "0")
    if rows:
        await _upsert_setting_value(db, "video.providers", raw_json)
    else:
        await _delete_setting_value(db, "video.providers")
    await write_audit(
        db,
        event_type="admin.video_providers.update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "enabled": body.enabled,
            "count": len(rows),
            "names": [item["name"] for item in rows],
        },
    )
    await db.commit()
    return await list_video_providers(admin, db)


def _validate_provider_update_names(body: ProvidersUpdateIn) -> None:
    seen_names: set[str] = set()
    for provider_input in body.items:
        name = provider_input.name.strip()
        if not name:
            raise _http("invalid_request", "provider 名称不能为空", 422)
        if name in seen_names:
            raise _http("invalid_request", f"provider 名称重复：{name}", 422)
        seen_names.add(name)

    seen_proxy_names: set[str] = set()
    for proxy_input in body.proxies:
        name = proxy_input.name.strip()
        if not name:
            raise _http("invalid_request", "proxy 名称不能为空", 422)
        if name in seen_proxy_names:
            raise _http("invalid_request", f"proxy 名称重复：{name}", 422)
        seen_proxy_names.add(name)


def _stored_provider_secrets(
    old_raw: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    old_keys: dict[str, str] = {}
    old_proxy_passwords: dict[str, str] = {}
    if not old_raw:
        return old_keys, old_proxy_passwords
    old_items, old_proxies = _parse_config(old_raw)
    for old_item in old_items:
        name = old_item.get("name")
        key = old_item.get("api_key")
        if isinstance(name, str) and name.strip() and isinstance(key, str) and key:
            old_keys[name.strip()] = key
    for old_proxy in old_proxies:
        name = old_proxy.get("name")
        password = old_proxy.get("password")
        if (
            isinstance(name, str)
            and name.strip()
            and isinstance(password, str)
            and password
        ):
            old_proxy_passwords[name.strip()] = password
    return old_keys, old_proxy_passwords


def _provider_proxy_rows(
    body: ProvidersUpdateIn,
    old_proxy_passwords: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proxy_input in body.proxies:
        name = proxy_input.name.strip()
        password = proxy_input.password.strip() or old_proxy_passwords.get(name, "")
        rows.append(
            {
                "name": name,
                "type": _normalize_proxy_type(proxy_input.type),
                "host": proxy_input.host.strip(),
                "port": proxy_input.port,
                "username": (proxy_input.username or "").strip() or None,
                "password": password,
                "private_key_path": (proxy_input.private_key_path or "").strip()
                or None,
                "enabled": proxy_input.enabled,
            }
        )
    return rows


def _provider_rows(
    body: ProvidersUpdateIn,
    *,
    old_keys: dict[str, str],
    proxy_names: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider_input in body.items:
        name = provider_input.name.strip()
        api_key = provider_input.api_key.strip() or old_keys.get(name, "")
        if not api_key and provider_input.enabled:
            raise _http(
                "invalid_request",
                f"provider「{name}」缺少 api_key",
                422,
            )
        proxy_name = (provider_input.proxy or "").strip() or None
        if proxy_name and proxy_name not in proxy_names:
            raise _http(
                "invalid_request",
                f"provider「{name}」引用了不存在的代理：{proxy_name}",
                422,
            )
        endpoint = _normalize_image_jobs_endpoint(provider_input.image_jobs_endpoint)
        row: dict[str, Any] = {
            "name": name,
            "base_url": provider_input.base_url.strip(),
            "api_key": api_key,
            "priority": provider_input.priority,
            "weight": max(1, provider_input.weight),
            "enabled": provider_input.enabled,
            "purposes": _normalize_purposes(provider_input.purposes),
            "image_jobs_enabled": provider_input.image_jobs_enabled,
            "image_jobs_endpoint": endpoint,
            "image_jobs_endpoint_lock": _normalize_image_jobs_endpoint_lock(
                provider_input.image_jobs_endpoint_lock,
                endpoint,
            ),
            "image_jobs_base_url": _normalize_image_jobs_base_url(
                provider_input.image_jobs_base_url
            ),
            "image_edit_input_transport": _normalize_image_edit_transport(
                provider_input.image_edit_input_transport
            ),
            "image_concurrency": _normalize_image_concurrency(
                provider_input.image_concurrency
            ),
        }
        for key in (
            "responses_supported",
            "image_generations_supported",
            "image_responses_supported",
        ):
            value = _normalize_capability(getattr(provider_input, key, None))
            if value is not None:
                row[key] = value
        if proxy_name:
            row["proxy"] = proxy_name
        rows.append(row)
    return rows


def _provider_update_json(
    provider_rows: list[dict[str, Any]],
    proxy_rows: list[dict[str, Any]],
) -> str:
    raw_json = json.dumps(
        {"providers": provider_rows, "proxies": proxy_rows},
        ensure_ascii=False,
    )
    if len(raw_json) > _PROVIDERS_MAX_LEN:
        raise _http(
            "invalid_request",
            f"providers JSON 超过 {_PROVIDERS_MAX_LEN} 字符",
            422,
        )
    try:
        validate_providers(raw_json)
        ensure_enabled_provider_proxies(raw_json)
    except ValueError as exc:
        raise _http("invalid_request", str(exc), 422) from exc
    return raw_json


def _provider_update_out(
    provider_rows: list[dict[str, Any]],
    proxy_rows: list[dict[str, Any]],
) -> ProvidersOut:
    items = [_to_out(row, index) for index, row in enumerate(provider_rows)]
    proxies = [_to_proxy_out(row, index) for index, row in enumerate(proxy_rows)]
    return ProvidersOut(items=items, proxies=proxies, source="db")


@router.put(
    "",
    response_model=ProvidersOut,
    dependencies=[Depends(verify_csrf)],
)
async def update_providers(
    body: ProvidersUpdateIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProvidersOut:
    # 清空场景
    if not body.items:
        existing = (
            await db.execute(
                select(SystemSetting).where(SystemSetting.key == "providers")
            )
        ).scalar_one_or_none()
        if existing:
            await db.delete(existing)
        await write_audit(
            db,
            event_type="admin.providers.clear",
            user_id=admin.id,
            actor_email_hash=hash_email(admin.email),
            actor_ip_hash=request_ip_hash(request),
            details={},
        )
        await db.commit()
        invalidate_admin_models_cache()
        return ProvidersOut(items=[], proxies=[], source="none")

    _validate_provider_update_names(body)
    old_raw, _ = await _read_providers(db)
    old_keys, old_proxy_passwords = _stored_provider_secrets(old_raw)
    proxy_rows = _provider_proxy_rows(body, old_proxy_passwords)
    provider_rows = _provider_rows(
        body,
        old_keys=old_keys,
        proxy_names={row["name"] for row in proxy_rows},
    )
    raw_json = _provider_update_json(provider_rows, proxy_rows)

    existing = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == "providers"))
    ).scalar_one_or_none()
    if existing is None:
        db.add(SystemSetting(key="providers", value=raw_json))
    else:
        existing.value = raw_json

    await write_audit(
        db,
        event_type="admin.providers.update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "count": len(provider_rows),
            "names": [row["name"] for row in provider_rows],
        },
    )
    await db.commit()
    invalidate_admin_models_cache()
    return _provider_update_out(provider_rows, proxy_rows)


@router.patch(
    "/{provider_name}/enabled",
    response_model=ProviderItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_provider_enabled(
    provider_name: str,
    body: ProviderEnabledPatchIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProviderItemOut:
    raw, _source = await _read_providers(db)
    if not raw:
        raise _http("not_found", "provider not found", 404)
    items, proxies = _parse_config(raw)
    target_idx: int | None = None
    for idx, item in enumerate(items):
        if str(item.get("name") or "").strip() == provider_name:
            target_idx = idx
            break
    if target_idx is None:
        raise _http("not_found", "provider not found", 404)

    target = items[target_idx]
    target["enabled"] = body.enabled
    for item in items:
        item["purposes"] = _normalize_purposes(item.get("purposes"))

    raw_json = json.dumps(
        {"providers": items, "proxies": proxies},
        ensure_ascii=False,
    )
    if len(raw_json) > _PROVIDERS_MAX_LEN:
        raise _http(
            "invalid_request",
            f"providers JSON 超过 {_PROVIDERS_MAX_LEN} 字符",
            422,
        )
    try:
        validate_providers(raw_json)
        ensure_enabled_provider_proxies(raw_json)
    except ValueError as exc:
        raise _http("invalid_request", str(exc), 422) from exc

    existing = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == "providers"))
    ).scalar_one_or_none()
    if existing is None:
        db.add(SystemSetting(key="providers", value=raw_json))
    else:
        existing.value = raw_json

    await write_audit(
        db,
        event_type="admin.providers.enabled",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"name": provider_name, "enabled": body.enabled},
    )
    await db.commit()
    invalidate_admin_models_cache()
    return _to_out(target, target_idx)


# ---------------------------------------------------------------------------
# 探活
# ---------------------------------------------------------------------------


@dataclass
class _ProbeOutcome:
    ok: bool
    latency_ms: int
    error: str | None
    http_status: int | None
    # 详见 ProviderProbeResult.capability_signal 注释
    capability_signal: str | None

    def __iter__(self):
        # 向后兼容：旧 caller 解包 ``ok, latency, err = await _probe_one(...)``
        # 仍然工作；新调用方走属性访问。
        yield self.ok
        yield self.latency_ms
        yield self.error


def _classify_probe_status(status: int) -> tuple[str, str | None]:
    """根据 HTTP status 给 capability_signal + 默认 error 描述。

    - 404/405 → unsupported（端点不存在 / 方法不被允许，可据此把 capability=False）
    - 401/403 → auth（鉴权 / 权限，不能判定能力）
    - 429/5xx → transient（临时不健康，不能判定能力）
    - 其它 4xx → 不明确（可能是请求体问题），保守返回 None
    """
    if status in (404, 405):
        return "unsupported", f"HTTP {status}"
    if status in (401, 403):
        return "auth", f"HTTP {status}"
    if status == 429 or 500 <= status < 600:
        return "transient", f"HTTP {status}"
    return "unsupported" if status == 501 else "", f"HTTP {status}"  # 511 等


def _truncate_probe_error(value: str, *, limit: int = 240) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 8].rstrip() + "…\n（已截断）"


def _probe_error_detail_from_payload(payload: object) -> str | None:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
            if isinstance(message, str) and message.strip():
                return _truncate_probe_error(message)
        if isinstance(error, str) and error.strip():
            return _truncate_probe_error(error)
        message = payload.get("message") or payload.get("detail")
        if isinstance(message, str) and message.strip():
            return _truncate_probe_error(message)
    return None


def _probe_http_error_message(resp: httpx.Response, fallback: str | None) -> str:
    detail: str | None = None
    try:
        detail = _probe_error_detail_from_payload(resp.json())
    except Exception:  # noqa: BLE001
        detail = None
    if not detail and resp.text:
        detail = _truncate_probe_error(resp.text)
    prefix = fallback or f"HTTP {resp.status_code}"
    return f"{prefix}: {detail}" if detail else prefix


async def _probe_one(
    base_url: str,
    api_key: str,
    *,
    proxy: ProviderProxyDefinition | None = None,
) -> _ProbeOutcome:
    url = _responses_url(base_url)
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    body = build_provider_probe_request()
    t0 = time.monotonic()
    try:
        proxy_url = await resolve_provider_proxy_url(proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_PROBE_TIMEOUT_S),
            proxy=proxy_url,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            resp = await client.post(url, json=body, headers=headers)
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            signal, err = _classify_probe_status(resp.status_code)
            return _ProbeOutcome(
                ok=False,
                latency_ms=latency,
                error=_probe_http_error_message(resp, err),
                http_status=resp.status_code,
                capability_signal=signal or None,
            )
        try:
            payload = resp.json()
            text = _extract_response_output_text(payload)
        except Exception:  # noqa: BLE001
            text = _extract_sse_output_text(resp.text)
            if not text:
                return _ProbeOutcome(
                    ok=False,
                    latency_ms=latency,
                    error="bad_json",
                    http_status=resp.status_code,
                    capability_signal=None,
                )
        if "9801" in text:
            # 200 + 文本能解析 → 端点确认支持
            return _ProbeOutcome(
                ok=True,
                latency_ms=latency,
                error=None,
                http_status=resp.status_code,
                capability_signal="supported",
            )
        # 200 但答错——可能是模型口径不一致；不是 capability 问题
        return _ProbeOutcome(
            ok=False,
            latency_ms=latency,
            error="wrong_answer",
            http_status=resp.status_code,
            capability_signal=None,
        )
    except httpx.TimeoutException:
        latency = int((time.monotonic() - t0) * 1000)
        return _ProbeOutcome(
            ok=False,
            latency_ms=latency,
            error="timeout",
            http_status=None,
            capability_signal="transient",
        )
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        message = _truncate_probe_error(str(exc))
        error = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
        return _ProbeOutcome(
            ok=False,
            latency_ms=latency,
            error=error,
            http_status=None,
            capability_signal=None,
        )


def _probe_blocked_by_endpoint_lock(it: dict[str, Any]) -> bool:
    endpoint = _normalize_image_jobs_endpoint(it.get("image_jobs_endpoint"))
    if endpoint == "auto":
        return False
    probe_view = {
        "image_jobs_endpoint": endpoint,
        "image_jobs_endpoint_lock": _normalize_image_jobs_endpoint_lock(
            it.get("image_jobs_endpoint_lock"), endpoint
        ),
    }
    return not endpoint_kind_allowed(probe_view, "responses")


@router.post(
    "/probe",
    response_model=ProvidersProbeOut,
    dependencies=[Depends(verify_csrf)],
)
async def probe_providers(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: ProvidersProbeIn | None = None,
) -> ProvidersProbeOut:
    raw, _ = await _read_providers(db)
    if not raw:
        return ProvidersProbeOut(items=[], probed_at=None)
    items, proxy_items = _parse_config(raw)
    if not items:
        return ProvidersProbeOut(items=[], probed_at=None)
    proxy_by_name: dict[str, ProviderProxyDefinition] = {}
    for i, proxy_item in enumerate(proxy_items):
        try:
            parsed = parse_proxy_item(proxy_item, index=i)
        except Exception:  # noqa: BLE001
            continue
        proxy_by_name[parsed.name] = parsed

    names_filter = set(body.names) if body and body.names else None

    async def _do(it: dict, idx: int) -> ProviderProbeResult:
        name = it.get("name") or f"provider-{idx}"
        base_url = it.get("base_url", "")
        api_key = it.get("api_key", "")

        if names_filter and name not in names_filter:
            return ProviderProbeResult(name=name, ok=False, status="skipped")

        if not _normalize_bool(it.get("enabled"), default=True):
            return ProviderProbeResult(name=name, ok=False, status="disabled")

        if _probe_blocked_by_endpoint_lock(it):
            return ProviderProbeResult(
                name=name,
                ok=False,
                status="skipped",
                error="endpoint_locked_to_generations",
            )

        if not base_url or not api_key:
            return ProviderProbeResult(
                name=name,
                ok=False,
                error="missing config",
                status="unhealthy",
            )

        proxy_name = it.get("proxy")
        proxy = (
            proxy_by_name.get(proxy_name)
            if isinstance(proxy_name, str) and proxy_name
            else None
        )
        outcome = await _probe_one(base_url, api_key, proxy=proxy)
        return ProviderProbeResult(
            name=name,
            ok=outcome.ok,
            latency_ms=outcome.latency_ms,
            error=outcome.error,
            status="healthy" if outcome.ok else "unhealthy",
            capability_signal=outcome.capability_signal,
            http_status=outcome.http_status,
        )

    results = await asyncio.gather(*[_do(it, i) for i, it in enumerate(items)])
    now = datetime.now(timezone.utc).isoformat()
    return ProvidersProbeOut(items=list(results), probed_at=now)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=ProviderStatsOut)
async def provider_stats(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProviderStatsOut:
    """从 Redis 读取 per-provider 请求统计；从 DB 读取自动探活间隔设置。"""
    from ..redis_client import get_redis

    raw, _ = await _read_providers(db)
    if not raw:
        return ProviderStatsOut(
            items=[], auto_probe_interval=120, auto_image_probe_interval=0
        )

    provider_names = [
        it.get("name", f"provider-{i}") for i, it in enumerate(_parse_items(raw))
    ]

    r = get_redis()
    items: list[ProviderStatsItem] = []
    grand_total = 0

    for name in provider_names:
        key = f"lumen:provider_stats:{name}"
        vals = await cast(
            Awaitable[dict[str, str]],
            r.hgetall(key),
        )
        total = int(vals.get("total", 0))
        success = int(vals.get("success", 0))
        fail = int(vals.get("fail", 0))
        grand_total += total
        items.append(
            ProviderStatsItem(
                name=name,
                total=total,
                success=success,
                fail=fail,
                success_rate=success / total if total > 0 else 0.0,
            )
        )

    for it in items:
        it.traffic_pct = it.total / grand_total if grand_total > 0 else 0.0

    # 读取自动探活间隔（文本 + image 各一个开关）
    interval_rows = (
        await db.execute(
            select(SystemSetting.key, SystemSetting.value).where(
                SystemSetting.key.in_(
                    [
                        "providers.auto_probe_interval",
                        "providers.auto_image_probe_interval",
                    ]
                )
            )
        )
    ).all()
    interval_map = {row.key: row.value for row in interval_rows}

    def _to_int(val: str | None, default: int) -> int:
        if val is None or val == "":
            return default
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    interval = _to_int(interval_map.get("providers.auto_probe_interval"), 120)
    image_interval = _to_int(interval_map.get("providers.auto_image_probe_interval"), 0)

    return ProviderStatsOut(
        items=items,
        auto_probe_interval=interval,
        auto_image_probe_interval=image_interval,
    )


__all__ = ["router"]
