"""Build persisted video provider configuration from admin updates."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from lumen_core.schemas import VideoProviderItemIn
from lumen_core.video_providers import (
    DEFAULT_VOLCANO_PROJECT_NAME,
    DEFAULT_VOLCANO_REGION,
)


class VideoProviderUpdateError(ValueError):
    """Admin-submitted video provider configuration is invalid."""


@dataclass(frozen=True)
class VideoProviderUpdatePayload:
    rows: list[dict[str, Any]]
    raw_json: str


def validate_video_provider_items(items: list[VideoProviderItemIn]) -> None:
    seen_names: set[str] = set()
    for item in items:
        name = item.name.strip()
        if not name:
            raise VideoProviderUpdateError("视频供应商名称不能为空")
        if name in seen_names:
            raise VideoProviderUpdateError(f"视频供应商名称重复：{name}")
        seen_names.add(name)


def _old_provider_indexes(
    old_items: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    items_by_name: dict[str, dict[str, Any]] = {}
    api_keys: dict[str, str] = {}
    for old_item in old_items:
        old_name = old_item.get("name")
        old_api_key = old_item.get("api_key")
        if isinstance(old_name, str) and old_name.strip():
            items_by_name[old_name.strip()] = old_item
        if isinstance(old_name, str) and isinstance(old_api_key, str) and old_api_key:
            api_keys[old_name.strip()] = old_api_key
    return items_by_name, api_keys


def _normalized_models(item: VideoProviderItemIn) -> dict[str, str]:
    return {
        str(key).strip(): str(value).strip()
        for key, value in item.models.items()
        if str(key).strip() and str(value).strip()
    }


def _old_volcano_item(old_item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(old_item, dict):
        return None
    if str(old_item.get("kind", "volcano")).strip().lower() != "volcano":
        return None
    return old_item


def _preserved_secret_pair(
    old_item: dict[str, Any] | None,
) -> tuple[str, str]:
    old_volcano = _old_volcano_item(old_item)
    if old_volcano is None:
        return "", ""
    raw_access_key_id = old_volcano.get("access_key_id", "")
    raw_secret_access_key = old_volcano.get("secret_access_key", "")
    access_key_id = (
        raw_access_key_id.strip() if isinstance(raw_access_key_id, str) else ""
    )
    secret_access_key = (
        raw_secret_access_key.strip() if isinstance(raw_secret_access_key, str) else ""
    )
    if not access_key_id or not secret_access_key:
        return "", ""
    return access_key_id, secret_access_key


def _apply_volcano_fields(
    row: dict[str, Any],
    item: VideoProviderItemIn,
    *,
    provider_name: str,
    old_item: dict[str, Any] | None,
) -> None:
    submitted_access_key_id = item.access_key_id.strip()
    submitted_secret_access_key = item.secret_access_key.strip()
    if bool(submitted_access_key_id) != bool(submitted_secret_access_key):
        raise VideoProviderUpdateError(
            f"视频供应商「{provider_name}」的 Access Key ID 与 "
            "Secret Access Key 必须同时填写"
        )
    old_access_key_id, old_secret_access_key = _preserved_secret_pair(old_item)
    access_key_id = submitted_access_key_id or old_access_key_id
    secret_access_key = submitted_secret_access_key or old_secret_access_key
    if access_key_id:
        row["access_key_id"] = access_key_id
    if secret_access_key:
        row["secret_access_key"] = secret_access_key

    old_volcano = _old_volcano_item(old_item)
    old_project_name = old_volcano.get("project_name") if old_volcano else None
    old_region = old_volcano.get("region") if old_volcano else None
    project_name = item.project_name.strip()
    region = item.region.strip()
    if (
        "project_name" not in item.model_fields_set
        and isinstance(old_project_name, str)
        and old_project_name.strip()
    ):
        project_name = old_project_name.strip()
    if (
        "region" not in item.model_fields_set
        and isinstance(old_region, str)
        and old_region.strip()
    ):
        region = old_region.strip()
    row["project_name"] = project_name or DEFAULT_VOLCANO_PROJECT_NAME
    row["region"] = region or DEFAULT_VOLCANO_REGION


def _provider_row(
    item: VideoProviderItemIn,
    *,
    old_item: dict[str, Any] | None,
    old_api_key: str,
    shared_proxy_names: set[str],
    old_video_proxy_by_name: dict[str, dict[str, Any]],
    referenced_video_proxies: set[str],
) -> dict[str, Any]:
    provider_name = item.name.strip()
    proxy_name = (item.proxy or "").strip() or None
    if proxy_name and (
        proxy_name not in shared_proxy_names
        and proxy_name not in old_video_proxy_by_name
    ):
        raise VideoProviderUpdateError(
            f"视频供应商「{provider_name}」引用了不存在的代理：{proxy_name}"
        )
    if proxy_name in old_video_proxy_by_name:
        referenced_video_proxies.add(proxy_name)

    row: dict[str, Any] = {
        "name": provider_name,
        "kind": item.kind,
        "base_url": item.base_url.strip(),
        "api_key": item.api_key.strip() or old_api_key,
        "enabled": item.enabled,
        "priority": item.priority,
        "weight": max(1, item.weight),
        "concurrency": max(1, min(32, int(item.concurrency or 1))),
        "supports_idempotency": item.supports_idempotency,
        "models": _normalized_models(item),
    }
    if proxy_name:
        row["proxy"] = proxy_name
    if item.kind == "volcano":
        _apply_volcano_fields(
            row,
            item,
            provider_name=provider_name,
            old_item=old_item,
        )
    return row


def build_video_provider_update(
    items: list[VideoProviderItemIn],
    *,
    old_items: list[dict[str, Any]],
    old_video_proxies: list[dict[str, Any]],
    shared_proxies: list[dict[str, Any]],
) -> VideoProviderUpdatePayload:
    validate_video_provider_items(items)
    old_item_by_name, old_keys = _old_provider_indexes(old_items)
    shared_proxy_names = {
        proxy.get("name")
        for proxy in shared_proxies
        if isinstance(proxy.get("name"), str)
    }
    old_video_proxy_by_name = {
        proxy.get("name"): proxy
        for proxy in old_video_proxies
        if isinstance(proxy.get("name"), str)
    }
    referenced_video_proxies: set[str] = set()
    rows = [
        _provider_row(
            item,
            old_item=old_item_by_name.get(item.name.strip()),
            old_api_key=old_keys.get(item.name.strip(), ""),
            shared_proxy_names=shared_proxy_names,
            old_video_proxy_by_name=old_video_proxy_by_name,
            referenced_video_proxies=referenced_video_proxies,
        )
        for item in items
    ]
    kept_video_proxies = [
        proxy
        for name, proxy in old_video_proxy_by_name.items()
        if name in referenced_video_proxies
    ]
    return VideoProviderUpdatePayload(
        rows=rows,
        raw_json=json.dumps(
            {"providers": rows, "proxies": kept_video_proxies},
            ensure_ascii=False,
        ),
    )
