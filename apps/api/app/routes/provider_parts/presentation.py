from __future__ import annotations

from typing import Any, Literal, cast

from lumen_core.providers_parts.config import (
    normalize_image_edit_input_transport,
    normalize_provider_purposes,
)
from lumen_core.schema_models import (
    ProviderItemOut,
    ProviderProxyOut,
    ProviderPurpose,
)


IMAGE_JOBS_ENDPOINT_VALUES = frozenset({"auto", "generations", "responses"})
IMAGE_CONCURRENCY_MAX = 32


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "..." + key[-4:]


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def normalize_proxy_type(value: str, *, fallback: bool = False) -> str:
    raw = (value or "socks5").strip().lower()
    if raw in {"s5", "socks", "socks5", "socks5h"}:
        return "socks5"
    if raw == "ssh":
        return "ssh"
    return "socks5" if fallback else raw


def safe_int(value: object, default: int, *, minimum: int | None = None) -> int:
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


def normalize_capability(raw: Any) -> bool | None:
    """Parse persisted capability values while preserving unknown state."""
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


def normalize_bool(raw: Any, *, default: bool = False) -> bool:
    parsed = normalize_capability(raw)
    return default if parsed is None else parsed


def normalize_purposes(raw: Any) -> list[ProviderPurpose]:
    return [
        cast(ProviderPurpose, purpose) for purpose in normalize_provider_purposes(raw)
    ]


def normalize_image_edit_transport(raw: Any) -> Literal["url", "file"]:
    return cast(
        Literal["url", "file"],
        normalize_image_edit_input_transport(raw),
    )


def normalize_image_jobs_endpoint(raw: Any) -> str:
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in IMAGE_JOBS_ENDPOINT_VALUES:
            return value
    return "auto"


def normalize_image_jobs_endpoint_lock(raw: Any, endpoint: str) -> bool:
    if endpoint == "auto":
        return False
    return normalize_bool(raw, default=False)


def normalize_image_jobs_base_url(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    if not (value.startswith("http://") or value.startswith("https://")):
        return ""
    return value


def normalize_image_concurrency(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(IMAGE_CONCURRENCY_MAX, value))


def provider_out(item: dict[str, Any], index: int) -> ProviderItemOut:
    endpoint = normalize_image_jobs_endpoint(item.get("image_jobs_endpoint"))
    return ProviderItemOut(
        name=item.get("name") or f"provider-{index}",
        base_url=item.get("base_url", ""),
        api_key_hint=mask_key(item.get("api_key", "")),
        priority=safe_int(item.get("priority"), 0),
        weight=safe_int(item.get("weight"), 1, minimum=1),
        enabled=normalize_bool(item.get("enabled"), default=True),
        purposes=normalize_purposes(item.get("purposes")),
        proxy=item.get("proxy") if isinstance(item.get("proxy"), str) else None,
        image_jobs_enabled=normalize_bool(
            item.get("image_jobs_enabled"),
            default=False,
        ),
        image_streaming_enabled=normalize_bool(
            item.get("image_streaming_enabled"),
            default=False,
        ),
        image_jobs_endpoint=endpoint,
        image_jobs_endpoint_lock=normalize_image_jobs_endpoint_lock(
            item.get("image_jobs_endpoint_lock"), endpoint
        ),
        image_jobs_base_url=normalize_image_jobs_base_url(
            item.get("image_jobs_base_url")
        ),
        image_edit_input_transport=normalize_image_edit_transport(
            item.get("image_edit_input_transport")
        ),
        image_concurrency=normalize_image_concurrency(item.get("image_concurrency")),
        responses_supported=normalize_capability(item.get("responses_supported")),
        vision_supported=normalize_capability(item.get("vision_supported")),
        agent_api=(
            item.get("agent_api")
            if isinstance(item.get("agent_api"), str)
            and item.get("agent_api")
            in {"openai-responses", "openai-completions", "anthropic-messages"}
            else "openai-responses"
        ),
        agent_models=[
            value.strip()
            for value in item.get("agent_models", [])
            if isinstance(value, str) and value.strip()
        ][:128],
        agent_context_window=min(
            2_000_000,
            safe_int(item.get("agent_context_window"), 128000, minimum=4096),
        ),
        agent_max_output_tokens=min(
            128000,
            safe_int(item.get("agent_max_output_tokens"), 16384, minimum=1),
        ),
        agent_reasoning_supported=normalize_bool(
            item.get("agent_reasoning_supported"), default=True
        ),
        image_generations_supported=normalize_capability(
            item.get("image_generations_supported")
        ),
        image_responses_supported=normalize_capability(
            item.get("image_responses_supported")
        ),
    )


def provider_agent_update_fields(
    provider_input: Any,
    old_item: dict[str, Any],
) -> dict[str, Any]:
    def preserved(field: str, fallback: Any) -> Any:
        return (
            getattr(provider_input, field)
            if field in provider_input.model_fields_set
            else old_item.get(field, fallback)
        )

    output = {
        "agent_api": preserved("agent_api", provider_input.agent_api),
        "agent_models": preserved("agent_models", provider_input.agent_models),
        "agent_context_window": preserved(
            "agent_context_window", provider_input.agent_context_window
        ),
        "agent_max_output_tokens": preserved(
            "agent_max_output_tokens", provider_input.agent_max_output_tokens
        ),
        "agent_reasoning_supported": preserved(
            "agent_reasoning_supported",
            provider_input.agent_reasoning_supported,
        ),
    }
    for key in (
        "responses_supported",
        "vision_supported",
        "image_generations_supported",
        "image_responses_supported",
    ):
        value = normalize_capability(
            preserved(key, getattr(provider_input, key, None))
        )
        if value is not None:
            output[key] = value
    return output


def provider_update_credentials(
    provider_input: Any,
    *,
    old_keys: dict[str, str],
    old_item: dict[str, Any],
) -> tuple[str, str]:
    name = provider_input.name.strip()
    submitted_key = provider_input.api_key.strip()
    new_base_url = provider_input.base_url.strip().rstrip("/")
    old_base_url = str(old_item.get("base_url") or "").strip().rstrip("/")
    if (
        not submitted_key
        and old_keys.get(name)
        and old_base_url
        and old_base_url != new_base_url
    ):
        raise ValueError("修改 base_url 后必须重新填写 api_key")
    return new_base_url, submitted_key or old_keys.get(name, "")


def proxy_out(item: dict[str, Any], index: int) -> ProviderProxyOut:
    proxy_type = normalize_proxy_type(
        item.get("type") or item.get("protocol") or "socks5",
        fallback=True,
    )
    port = safe_int(
        item.get("port"),
        22 if proxy_type == "ssh" else 1080,
        minimum=1,
    )
    return ProviderProxyOut(
        name=item.get("name") or f"proxy-{index}",
        type=proxy_type,
        host=item.get("host", ""),
        port=min(65535, port),
        username=(
            item.get("username") if isinstance(item.get("username"), str) else None
        ),
        password_hint=mask_secret(item.get("password")),
        private_key_path=(
            item.get("private_key_path")
            if isinstance(item.get("private_key_path"), str)
            else None
        ),
        enabled=normalize_bool(item.get("enabled"), default=True),
    )
