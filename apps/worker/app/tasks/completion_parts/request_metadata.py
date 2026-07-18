"""Pure completion request and upstream metadata helpers."""

from __future__ import annotations

from typing import Any


def _split_csv_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        value = part.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _content_str_list(content: dict[str, Any] | None, key: str) -> list[str]:
    raw = (content or {}).get(key)
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _normalize_reasoning_effort_for_upstream(
    effort: str | None,
) -> str | None:
    if effort == "minimal":
        # Newer GPT-5.x models use "none" for no reasoning; keep accepting
        # historical UI/API values while avoiding upstream 400s.
        return "none"
    return effort


def _completion_upstream_provider_event(event: dict[str, Any]) -> dict[str, str]:
    if event.get("type") != "provider_used":
        return {}
    provider = event.get("provider")
    route = event.get("route")
    endpoint = event.get("endpoint")
    source = event.get("source")
    out: dict[str, str] = {}
    if isinstance(provider, str) and provider.strip():
        out["provider"] = provider.strip()
    if isinstance(route, str) and route.strip():
        out["route"] = route.strip()
    if isinstance(endpoint, str) and endpoint.strip():
        out["endpoint"] = endpoint.strip()
    if isinstance(source, str) and source.strip():
        out["source"] = source.strip()
    return out


def _merge_completion_upstream_metadata(
    upstream_request: dict[str, Any],
    *,
    provider_event: dict[str, str] | None,
    fast_mode: bool,
) -> dict[str, Any]:
    out = dict(upstream_request)
    provider = (provider_event or {}).get("provider")
    route = (provider_event or {}).get("route") or "responses"
    endpoint = (provider_event or {}).get("endpoint") or "responses"
    source = (provider_event or {}).get("source") or "text"

    out["upstream_route"] = route
    out["actual_route"] = route
    out["actual_endpoint"] = endpoint
    out["actual_source"] = source
    if provider:
        out["provider"] = provider
        out["actual_provider"] = provider
        out["request_event_provider"] = provider
    if fast_mode:
        out["service_tier"] = "priority"
    else:
        out.pop("service_tier", None)
    return out
