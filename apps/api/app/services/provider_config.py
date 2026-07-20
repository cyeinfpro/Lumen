"""Shared provider configuration loading, parsing, and validation."""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import SystemSetting
from lumen_core.providers import (
    DEFAULT_IMAGE_EDIT_INPUT_TRANSPORT,
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    DEFAULT_PROVIDER_PURPOSES,
    build_legacy_provider,
    parse_provider_config_json,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.video_providers import parse_video_provider_config_json


ProviderConfigSource = Literal["db", "env", "none"]


def legacy_env_providers_raw() -> str | None:
    legacy = build_legacy_provider(
        base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    if legacy is None:
        return None
    return json.dumps(
        [
            {
                "name": legacy.name,
                "base_url": legacy.base_url,
                "api_key": legacy.api_key,
                "priority": legacy.priority,
                "weight": legacy.weight,
                "enabled": legacy.enabled,
                "purposes": list(DEFAULT_PROVIDER_PURPOSES),
                "image_jobs_enabled": False,
                "image_jobs_endpoint": "auto",
                "image_jobs_endpoint_lock": False,
                "image_jobs_base_url": "",
                "image_edit_input_transport": DEFAULT_IMAGE_EDIT_INPUT_TRANSPORT,
                "image_concurrency": 1,
            }
        ],
        ensure_ascii=False,
    )


async def read_providers(
    db: AsyncSession,
) -> tuple[str | None, ProviderConfigSource]:
    """Return the persisted provider JSON and its effective source."""
    row = (
        await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == "providers")
        )
    ).scalar_one_or_none()
    if row is not None and row != "":
        return row, "db"
    spec = get_spec("providers")
    if spec:
        env_val = os.environ.get(spec.env_fallback)
        if env_val is not None and env_val != "":
            return env_val, "env"
    legacy = legacy_env_providers_raw()
    if legacy is not None:
        return legacy, "env"
    return None, "none"


def parse_provider_config(
    raw: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [], []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)], []
    if not isinstance(value, dict):
        return [], []
    providers = value.get("providers", [])
    proxies = value.get("proxies", [])
    if not isinstance(providers, list):
        providers = []
    if not isinstance(proxies, list):
        proxies = []
    return (
        [item for item in providers if isinstance(item, dict)],
        [item for item in proxies if isinstance(item, dict)],
    )


def parse_provider_items(raw: str) -> list[dict[str, Any]]:
    items, _proxies = parse_provider_config(raw)
    return items


def ensure_enabled_provider_proxies(raw: str) -> None:
    providers, _proxies, errors = parse_provider_config_json(raw)
    if errors:
        raise ValueError("; ".join(errors))
    for provider in providers:
        if not provider.enabled or not provider.proxy_name:
            continue
        if provider.proxy is None:
            raise ValueError(
                f"provider「{provider.name}」引用了不存在的代理：{provider.proxy_name}"
            )
        if not provider.proxy.enabled:
            raise ValueError(
                f"provider「{provider.name}」引用了已禁用的代理：{provider.proxy_name}"
            )


def ensure_enabled_video_provider_proxies(
    raw: str,
    *,
    shared_provider_raw: str | None,
) -> None:
    providers, _proxies, errors = parse_video_provider_config_json(
        raw,
        shared_provider_raw=shared_provider_raw,
    )
    if errors:
        raise ValueError("; ".join(errors))
    for provider in providers:
        if not provider.enabled or not provider.proxy_name:
            continue
        if provider.proxy is None:
            raise ValueError(
                f"视频供应商「{provider.name}」引用了不存在的代理：{provider.proxy_name}"
            )
        if not provider.proxy.enabled:
            raise ValueError(
                f"视频供应商「{provider.name}」引用了已禁用的代理：{provider.proxy_name}"
            )
