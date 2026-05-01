"""Admin model catalog aggregation.

GET /admin/models fans out to each enabled Provider's /v1/models endpoint and
returns a de-duplicated model list. Provider failures are reported per-provider
instead of failing the whole admin UI.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    build_effective_provider_config,
    endpoint_kind_allowed,
    resolve_provider_proxy_url,
)
from lumen_core.schemas import (
    AdminModelOut,
    AdminModelsErrorOut,
    AdminModelsOut,
)

from ..db import get_db
from ..deps import AdminUser
from .providers import _read_providers

router = APIRouter(prefix="/admin", tags=["admin-models"])

_MODELS_TIMEOUT_S = 5.0
_CACHE_TTL_S = 60.0
_CACHE_LOCK = asyncio.Lock()
_CACHE: tuple[float, AdminModelsOut] | None = None


def _models_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def _model_ids(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        raw_items = payload.get("data")
    else:
        raw_items = payload
    if not isinstance(raw_items, list):
        return []
    ids: list[str] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            ids.append(model_id.strip())
    return ids


async def _fetch_provider_models(
    provider: ProviderDefinition,
) -> tuple[str, list[str], str | None]:
    url = _models_url(provider.base_url)
    try:
        proxy_url = await resolve_provider_proxy_url(provider.proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_MODELS_TIMEOUT_S),
            proxy=proxy_url,
        ) as client:
            resp = await client.get(
                url,
                headers={"authorization": f"Bearer {provider.api_key}"},
            )
        if resp.status_code >= 400:
            return provider.name, [], f"HTTP {resp.status_code}"
        try:
            ids = _model_ids(resp.json())
        except Exception:  # noqa: BLE001
            return provider.name, [], "bad_json"
        return provider.name, ids, None
    except httpx.TimeoutException:
        return provider.name, [], "timeout"
    except Exception as exc:  # noqa: BLE001
        return provider.name, [], type(exc).__name__


async def _build_models_response(db: AsyncSession) -> AdminModelsOut:
    raw, _source = await _read_providers(db)
    providers, _proxies, parse_errors = build_effective_provider_config(
        raw_providers=raw,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL")
            or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    enabled = [
        p for p in providers if p.enabled and endpoint_kind_allowed(p, "models")
    ]
    results = await asyncio.gather(
        *[_fetch_provider_models(provider) for provider in enabled],
        return_exceptions=False,
    )

    providers_by_model: dict[str, set[str]] = {}
    errors: list[AdminModelsErrorOut] = [
        AdminModelsErrorOut(provider="config", message=err)
        for err in parse_errors
    ]
    for provider_name, model_ids, error in results:
        if error:
            errors.append(AdminModelsErrorOut(provider=provider_name, message=error))
            continue
        for model_id in model_ids:
            providers_by_model.setdefault(model_id, set()).add(provider_name)

    models = [
        AdminModelOut(id=model_id, providers=sorted(provider_names))
        for model_id, provider_names in sorted(providers_by_model.items())
    ]
    return AdminModelsOut(
        models=models,
        fetched_at=datetime.now(timezone.utc),
        errors=errors,
    )


@router.get("/models", response_model=AdminModelsOut)
async def list_admin_models(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminModelsOut:
    global _CACHE
    now = time.monotonic()
    cached = _CACHE
    if cached is not None and cached[0] > now:
        return cached[1]

    async with _CACHE_LOCK:
        cached = _CACHE
        if cached is not None and cached[0] > now:
            return cached[1]
        data = await _build_models_response(db)
        _CACHE = (now + _CACHE_TTL_S, data)
        return data


def invalidate_admin_models_cache() -> None:
    global _CACHE
    _CACHE = None


__all__ = ["router", "invalidate_admin_models_cache"]
