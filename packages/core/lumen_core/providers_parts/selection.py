from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, Callable, Protocol, TypeVar

from .config import parse_optional_bool
from .definitions import ProviderDefinition, RoundRobinState


class _WeightedProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def priority(self) -> int: ...

    @property
    def weight(self) -> int: ...

    @property
    def enabled(self) -> bool: ...


_WeightedProviderT = TypeVar("_WeightedProviderT", bound=_WeightedProvider)


def endpoint_kind_allowed(provider: Any, endpoint_kind: str | None) -> bool:
    """Return whether a provider may be used for an upstream endpoint kind."""
    if endpoint_kind not in {"generations", "responses", "models"}:
        return True
    if isinstance(provider, dict):
        locked = parse_optional_bool(provider.get("image_jobs_endpoint_lock")) is True
        configured = provider.get("image_jobs_endpoint", "auto")
    else:
        locked = (
            parse_optional_bool(getattr(provider, "image_jobs_endpoint_lock", False))
            is True
        )
        configured = getattr(provider, "image_jobs_endpoint", "auto")
    if not locked or configured not in {"generations", "responses"}:
        return True
    if endpoint_kind == "models":
        return False
    return configured == endpoint_kind


def _provider_capability(provider: Any, attr: str) -> bool | None:
    if isinstance(provider, dict):
        value = provider.get(attr)
    else:
        value = getattr(provider, attr, None)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None


def provider_supports_route(
    provider: Any,
    *,
    route: str,
    endpoint_kind: str | None,
) -> bool:
    """Apply explicit provider capability flags to route selection."""
    if route == "models":
        return _provider_capability(provider, "responses_supported") is not False

    if route == "agent":
        api = (
            provider.get("agent_api")
            if isinstance(provider, dict)
            else getattr(provider, "agent_api", None)
        )
        if api not in {
            "openai-responses",
            "openai-completions",
            "anthropic-messages",
        }:
            return False
        return api != "openai-responses" or (
            _provider_capability(provider, "responses_supported") is not False
        )

    if route != "image":
        return _provider_capability(provider, "responses_supported") is not False

    if endpoint_kind == "responses":
        if _provider_capability(provider, "image_responses_supported") is False:
            return False
        if _provider_capability(provider, "responses_supported") is False:
            return False
        return True
    if endpoint_kind == "generations":
        return (
            _provider_capability(provider, "image_generations_supported") is not False
        )
    img_resp = _provider_capability(provider, "image_responses_supported")
    img_gen = _provider_capability(provider, "image_generations_supported")
    return not (img_resp is False and img_gen is False)


def route_to_purpose(route: str | None) -> str:
    """Map legacy high-level provider routes to account-level purposes."""
    if route in {
        "image",
        "image_jobs",
        "image2",
        "image2_direct",
        "image2_edit_direct",
    }:
        return "image"
    if route == "embedding":
        return "embedding"
    return "chat"


def has_embedding_purpose(providers: list[ProviderDefinition]) -> bool:
    return any(p.enabled and "embedding" in p.purposes for p in providers)


def advance_round_robin_counter(
    rr_counters: MutableMapping[int, int] | RoundRobinState,
    priority: int,
) -> int:
    """Return the current counter for ``priority``, then advance it."""
    if isinstance(rr_counters, RoundRobinState):
        return rr_counters.advance(priority)
    counter = rr_counters.get(priority, 0)
    rr_counters[priority] = counter + 1
    return counter


def _weighted_priority_order(
    providers: list[_WeightedProviderT],
    counter_for_priority: Callable[[int], int] | None,
) -> list[_WeightedProviderT]:
    enabled = [p for p in providers if p.enabled]
    by_priority: dict[int, list[_WeightedProviderT]] = {}
    for provider in enabled:
        by_priority.setdefault(provider.priority, []).append(provider)

    result: list[_WeightedProviderT] = []
    for priority in sorted(by_priority.keys(), reverse=True):
        group = by_priority[priority]
        if len(group) <= 1:
            result.extend(group)
            continue
        total_weight = sum(max(1, provider.weight) for provider in group)
        counter = counter_for_priority(priority) if counter_for_priority else 0
        offset = counter % max(total_weight, 1)

        seen: set[str] = set()
        accumulated = 0
        for provider in group:
            accumulated += max(1, provider.weight)
            if accumulated > offset and provider.name not in seen:
                seen.add(provider.name)
                result.append(provider)
        for provider in group:
            if provider.name not in seen:
                seen.add(provider.name)
                result.append(provider)
    return result


def weighted_priority_order_and_advance(
    providers: list[_WeightedProviderT],
    rr_counters: MutableMapping[int, int] | RoundRobinState,
) -> list[_WeightedProviderT]:
    return _weighted_priority_order(
        providers,
        lambda priority: advance_round_robin_counter(rr_counters, priority),
    )


def weighted_priority_order(
    providers: list[_WeightedProviderT],
    rr_counters: MutableMapping[int, int] | RoundRobinState | None = None,
) -> list[_WeightedProviderT]:
    """Return weighted provider order without mutating state by default."""
    if rr_counters is not None:
        return weighted_priority_order_and_advance(providers, rr_counters)
    return _weighted_priority_order(providers, None)
