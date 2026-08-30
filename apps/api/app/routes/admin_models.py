"""Admin model catalog aggregation.

GET /admin/models fans out to each enabled Provider's /v1/models endpoint and
returns a de-duplicated model list. Provider failures are reported per-provider
instead of failing the whole admin UI.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.agent_model_profiles import GPT_56_AGENT_CONTEXT_WINDOW
from lumen_core.agent_provider_contract import agent_endpoint_contract
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    build_effective_provider_config,
)
from lumen_core.providers_parts.selection import provider_supports_route
from lumen_core.schemas import (
    AdminModelOut,
    AdminModelsErrorOut,
    AdminModelsOut,
)
from lumen_core.schema_models.providers import (
    AdminProviderModelOut,
    AdminProviderModelProfileOut,
    AdminProviderModelsDiscoverIn,
    AdminProviderModelsDiscoverOut,
)

from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ..proxy_pool import resolve_provider_proxy_url
from ..services.admin_model_cache import admin_model_cache_from_request
from ..services.provider_config import read_providers as _read_providers

router = APIRouter(prefix="/admin", tags=["admin-models"])

_MODELS_TIMEOUT_S = 5.0
_DEFAULT_CONTEXT_WINDOW = 128_000
_DEFAULT_MAX_OUTPUT_TOKENS = 16_384


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


def _model_items(payload: Any) -> list[dict[str, Any]]:
    raw_items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _bounded_metadata_int(
    item: dict[str, Any],
    keys: tuple[str, ...],
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    containers = [
        item,
        item.get("capabilities"),
        item.get("architecture"),
        item.get("top_provider"),
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            raw = container.get(key)
            if isinstance(raw, bool):
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if minimum <= value <= maximum:
                return value
    return None


def _metadata_bool(item: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
    for container in (item, item.get("capabilities"), item.get("architecture")):
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if isinstance(value, bool):
                return value
    return None


def _metadata_strings(item: dict[str, Any], keys: tuple[str, ...]) -> set[str]:
    values: set[str] = set()
    for container in (item, item.get("capabilities"), item.get("architecture")):
        if not isinstance(container, dict):
            continue
        for key in keys:
            raw = container.get(key)
            if isinstance(raw, list):
                values.update(
                    str(value).strip().lower()
                    for value in raw
                    if isinstance(value, str) and value.strip()
                )
    return values


def _known_model_family(
    model_id: str,
) -> tuple[bool | None, bool, int, int] | None:
    value = model_id.strip().lower()
    canonical = value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if canonical.startswith("gpt-5.6"):
        return True, True, GPT_56_AGENT_CONTEXT_WINDOW, 16_384
    if canonical.startswith(("gpt-5", "gpt-4.1", "gpt-4o")):
        return True, canonical.startswith("gpt-5"), 128_000, 16_384
    if canonical.startswith(("o1", "o3", "o4")):
        return None, True, 128_000, 16_384
    if "claude" in value:
        return True, True, 200_000, 16_384
    if "gemini" in value:
        return True, True, 128_000, 16_384
    return None


def _model_profile(
    model_id: str,
    item: dict[str, Any],
    *,
    agent_api: str,
) -> AdminProviderModelProfileOut:
    known = _known_model_family(model_id)
    known_vision, known_reasoning, known_context, known_output = (
        known
        if known is not None
        else (None, False, _DEFAULT_CONTEXT_WINDOW, _DEFAULT_MAX_OUTPUT_TOKENS)
    )
    context_window = _bounded_metadata_int(
        item,
        (
            "context_window",
            "context_length",
            "context_length_tokens",
            "max_context_length",
        ),
        minimum=4096,
        maximum=2_000_000,
    )
    max_output_tokens = _bounded_metadata_int(
        item,
        ("max_output_tokens", "max_completion_tokens", "output_token_limit"),
        minimum=1,
        maximum=128_000,
    )
    modalities = _metadata_strings(item, ("input_modalities", "modalities"))
    supported_parameters = _metadata_strings(
        item,
        ("supported_parameters", "parameters"),
    )
    vision = _metadata_bool(
        item,
        ("vision_supported", "supports_vision", "vision"),
    )
    if vision is None and modalities:
        vision = "image" in modalities or "vision" in modalities
    reasoning = _metadata_bool(
        item,
        ("reasoning_supported", "supports_reasoning", "reasoning"),
    )
    if reasoning is None and supported_parameters:
        reasoning = bool(
            {"reasoning", "reasoning_effort", "thinking"} & supported_parameters
        )
    provider_metadata = any(
        value is not None
        for value in (context_window, max_output_tokens, vision, reasoning)
    ) or bool(modalities or supported_parameters)
    return AdminProviderModelProfileOut(
        agent_api=agent_api,  # type: ignore[arg-type]
        responses_supported=(True if agent_api == "openai-responses" else None),
        vision_supported=vision if vision is not None else known_vision,
        context_window=context_window or known_context,
        max_output_tokens=max_output_tokens or known_output,
        reasoning_supported=(reasoning if reasoning is not None else known_reasoning),
        source=(
            "provider"
            if provider_metadata
            else "known_family"
            if known is not None
            else "conservative"
        ),
    )


def _normalize_probe_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not include credentials")
    return value


def _models_headers(agent_api: str, api_key: str) -> dict[str, str]:
    if agent_api == "anthropic-messages":
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    return {"authorization": f"Bearer {api_key}"}


async def _fetch_models_payload(
    *,
    base_url: str,
    agent_base_url: str | None,
    api_key: str,
    proxy: Any | None,
    agent_api: str,
) -> tuple[Any | None, str | None]:
    try:
        endpoint = agent_endpoint_contract(
            base_url,
            agent_api,
            agent_base_url=agent_base_url,
        )
        proxy_url = await resolve_provider_proxy_url(proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_MODELS_TIMEOUT_S),
            proxy=proxy_url,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(
                endpoint.models_url,
                headers=_models_headers(agent_api, api_key),
            )
        if response.status_code >= 400:
            return None, f"HTTP {response.status_code}"
        try:
            return response.json(), None
        except Exception:  # noqa: BLE001
            return None, "bad_json"
    except httpx.TimeoutException:
        return None, "timeout"
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__


async def _fetch_provider_models(
    provider: ProviderDefinition,
) -> tuple[str, list[str], str | None]:
    payload, error = await _fetch_models_payload(
        base_url=provider.base_url,
        agent_base_url=provider.agent_base_url,
        api_key=provider.api_key,
        proxy=provider.proxy,
        agent_api=provider.agent_api,
    )
    return provider.name, _model_ids(payload) if error is None else [], error


async def _build_models_response(db: AsyncSession) -> AdminModelsOut:
    raw, _source = await _read_providers(db)
    providers, _proxies, parse_errors = build_effective_provider_config(
        raw_providers=raw,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    enabled = [
        provider
        for provider in providers
        if provider.enabled
        and "chat" in provider.purposes
        and provider_supports_route(
            provider,
            route="agent",
            endpoint_kind=None,
        )
    ]
    results = await asyncio.gather(
        *[_fetch_provider_models(provider) for provider in enabled],
        return_exceptions=False,
    )

    providers_by_model: dict[str, set[str]] = {}
    errors: list[AdminModelsErrorOut] = [
        AdminModelsErrorOut(provider="config", message=err) for err in parse_errors
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
    request: Request,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminModelsOut:
    cache = admin_model_cache_from_request(request)
    return await cache.get(db, _build_models_response)


@router.post(
    "/models/discover",
    response_model=AdminProviderModelsDiscoverOut,
    dependencies=[Depends(verify_csrf)],
)
async def discover_provider_models(
    body: AdminProviderModelsDiscoverIn,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminProviderModelsDiscoverOut:
    try:
        base_url = _normalize_probe_base_url(body.base_url)
    except ValueError as exc:
        return AdminProviderModelsDiscoverOut(
            models=[],
            fetched_at=datetime.now(timezone.utc),
            error=str(exc),
        )
    raw, _source = await _read_providers(db)
    providers, proxies, _errors = build_effective_provider_config(
        raw_providers=raw,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    saved = next(
        (
            provider
            for provider in providers
            if body.provider_name and provider.name == body.provider_name.strip()
        ),
        None,
    )
    try:
        requested_endpoint = agent_endpoint_contract(
            base_url,
            body.agent_api,
            agent_base_url=body.agent_base_url.strip() or None,
        )
    except ValueError as exc:
        return AdminProviderModelsDiscoverOut(
            models=[],
            fetched_at=datetime.now(timezone.utc),
            error=str(exc),
        )
    requested_proxy_name = (body.proxy or "").strip()
    saved_proxy_name = saved.proxy_name if saved is not None else None
    agent_target_matches = bool(
        saved is not None
        and saved.agent_api == body.agent_api
        and saved.agent_base_url == requested_endpoint.sdk_base_url
    )
    effective_proxy_name = requested_proxy_name or (
        saved_proxy_name if agent_target_matches else None
    )
    connection_matches = bool(
        agent_target_matches
        and (effective_proxy_name or None) == (saved_proxy_name or None)
    )
    api_key = body.api_key.strip()
    if not api_key and connection_matches and saved is not None:
        api_key = saved.api_key
    if not api_key:
        return AdminProviderModelsDiscoverOut(
            models=[],
            fetched_at=datetime.now(timezone.utc),
            error="API key is required when the Agent connection changes",
        )
    proxy = next(
        (item for item in proxies if item.name == effective_proxy_name),
        None,
    )
    if effective_proxy_name and (proxy is None or not proxy.enabled):
        return AdminProviderModelsDiscoverOut(
            models=[],
            fetched_at=datetime.now(timezone.utc),
            error="selected proxy is unavailable",
        )
    effective_agent_base = requested_endpoint.sdk_base_url
    payload, error = await _fetch_models_payload(
        base_url=base_url,
        agent_base_url=effective_agent_base,
        api_key=api_key,
        proxy=proxy,
        agent_api=body.agent_api,
    )
    if error is not None:
        return AdminProviderModelsDiscoverOut(
            models=[],
            fetched_at=datetime.now(timezone.utc),
            error=error,
        )
    models_by_id: dict[str, AdminProviderModelOut] = {}
    for item in _model_items(payload):
        raw_id = item.get("id")
        if not isinstance(raw_id, str):
            continue
        model_id = raw_id.strip()
        if not model_id or len(model_id) > 256:
            continue
        models_by_id[model_id] = AdminProviderModelOut(
            id=model_id,
            profile=_model_profile(
                model_id,
                item,
                agent_api=body.agent_api,
            ),
        )
    return AdminProviderModelsDiscoverOut(
        models=[models_by_id[key] for key in sorted(models_by_id)],
        fetched_at=datetime.now(timezone.utc),
        error=None if models_by_id else "provider returned no model IDs",
    )


__all__ = ["router"]
