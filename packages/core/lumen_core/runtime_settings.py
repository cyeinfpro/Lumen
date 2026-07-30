"""可调系统设置元数据。

API 与 Worker 都消费它：
- API：管理员通过 /admin/settings 读写
- Worker：每次构造上游请求前 resolve（DB 优先，env fallback）

DB 中只持久化 SUPPORTED_SETTINGS 列表里的 key；其它 key 视为非法。
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from .providers import parse_provider_bool
from .providers_parts.config import normalize_provider_purposes
from .video_providers import validate_video_providers

from .runtime_setting_specs import SUPPORTED_SETTINGS, SettingSpec


def get_spec(key: str) -> SettingSpec | None:
    for s in SUPPORTED_SETTINGS:
        if s.key == key:
            return s
    return None


def _provider_config_items(value: object) -> tuple[list[object], list[object]]:
    if isinstance(value, list):
        return value, []
    if not isinstance(value, dict):
        raise ValueError("providers must be a non-empty JSON array or object")
    provider_items = value.get("providers")
    if not isinstance(provider_items, list) or not provider_items:
        raise ValueError("providers.providers must be a non-empty JSON array")
    proxy_items = value.get("proxies", [])
    if proxy_items is None:
        proxy_items = []
    if not isinstance(proxy_items, list):
        raise ValueError("providers.proxies must be a JSON array")
    return provider_items, proxy_items


def _validate_proxy_item(item: object, index: int) -> tuple[str, bool]:
    if not isinstance(item, dict):
        raise ValueError(f"proxies[{index}] must be an object")
    name = item.get("name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"proxies[{index}].name is required")
    protocol = item.get("type", item.get("protocol", "socks5"))
    if not isinstance(protocol, str) or protocol.strip().lower() not in {
        "s5",
        "socks",
        "socks5",
        "socks5h",
        "ssh",
    }:
        raise ValueError(f"proxies[{index}].type must be socks5 or ssh")
    host = item.get("host", "")
    if not isinstance(host, str) or not host.strip():
        raise ValueError(f"proxies[{index}].host is required")
    port = item.get("port", 22 if protocol.strip().lower() == "ssh" else 1080)
    try:
        port_int = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"proxies[{index}].port must be an integer") from exc
    if port_int < 1 or port_int > 65535:
        raise ValueError(f"proxies[{index}].port must be between 1 and 65535")
    try:
        enabled = parse_provider_bool(item.get("enabled"), default=True)
    except ValueError as exc:
        raise ValueError(f"proxies[{index}].enabled must be a boolean") from exc
    return name.strip(), enabled


def _validated_proxy_map(proxies: list[object]) -> dict[str, bool]:
    enabled_by_name: dict[str, bool] = {}
    for index, item in enumerate(proxies):
        proxy_name, proxy_enabled = _validate_proxy_item(item, index)
        if proxy_name in enabled_by_name:
            raise ValueError(f"proxies[{index}].name is duplicated: {proxy_name}")
        enabled_by_name[proxy_name] = proxy_enabled
    return enabled_by_name


def _remember_provider_name(
    item: dict[str, object],
    index: int,
    provider_names: set[str],
) -> None:
    name = item.get("name", f"provider-{index}")
    if not isinstance(name, str) or not name.strip():
        return
    provider_name = name.strip()
    if provider_name in provider_names:
        raise ValueError(f"providers[{index}].name is duplicated: {provider_name}")
    provider_names.add(provider_name)


def _validate_provider_base_url(base_url: object, index: int) -> None:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError(f"providers[{index}].base_url is required")
    parts = urlsplit(base_url.strip())
    if not parts.scheme:
        raise ValueError(
            f"providers[{index}].base_url has no scheme (must be http:// or https://)"
        )
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"providers[{index}].base_url must use http or https")
    if not parts.hostname:
        raise ValueError(f"providers[{index}].base_url must include a hostname")
    if parts.username or parts.password:
        raise ValueError(f"providers[{index}].base_url must not include credentials")


def _validate_provider_proxy_reference(
    item: dict[str, object],
    *,
    index: int,
    enabled: bool,
    proxy_enabled_by_name: dict[str, bool],
) -> None:
    raw_proxy_name = item.get("proxy", item.get("proxy_name"))
    if not isinstance(raw_proxy_name, str) or not raw_proxy_name.strip():
        return
    proxy_name = raw_proxy_name.strip()
    if proxy_name not in proxy_enabled_by_name and enabled:
        raise ValueError(
            f"providers[{index}].proxy references unknown proxy: {proxy_name}"
        )
    if enabled and not proxy_enabled_by_name.get(proxy_name, True):
        raise ValueError(
            f"providers[{index}].proxy references disabled proxy: {proxy_name}"
        )


def _validate_provider_config_item(
    item: object,
    *,
    index: int,
    provider_names: set[str],
    proxy_enabled_by_name: dict[str, bool],
) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"providers[{index}] must be an object")
    _remember_provider_name(item, index, provider_names)
    _validate_provider_base_url(item.get("base_url", ""), index)
    try:
        enabled = parse_provider_bool(item.get("enabled"), default=True)
    except ValueError as exc:
        raise ValueError(f"providers[{index}].enabled must be a boolean") from exc
    api_key = item.get("api_key", "")
    if not isinstance(api_key, str):
        raise ValueError(f"providers[{index}].api_key must be a string")
    if enabled and not api_key.strip():
        raise ValueError(f"providers[{index}].api_key is required")
    _validate_provider_proxy_reference(
        item,
        index=index,
        enabled=enabled,
        proxy_enabled_by_name=proxy_enabled_by_name,
    )
    try:
        normalize_provider_purposes(item.get("purposes"))
    except ValueError as exc:
        raise ValueError(f"providers[{index}].purposes invalid: {exc}") from exc


def validate_providers(raw: str) -> str:
    """Validate provider-pool JSON. Returns raw string if valid.

    Backward compatible formats:
    - old: `[{"name": "...", "base_url": "...", "api_key": "..."}]`
    - new: `{"providers": [...], "proxies": [...]}`
    """
    value = raw.strip()
    if not value:
        raise ValueError("providers must not be empty")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"providers is not valid JSON: {exc}") from exc
    items, proxies = _provider_config_items(parsed)
    if not items:
        raise ValueError("providers must be a non-empty JSON array")
    proxy_enabled_by_name = _validated_proxy_map(proxies)
    provider_names: set[str] = set()
    for index, item in enumerate(items):
        _validate_provider_config_item(
            item,
            index=index,
            provider_names=provider_names,
            proxy_enabled_by_name=proxy_enabled_by_name,
        )
    return value


def validate_public_base_url(raw: str) -> str:
    """Validate and normalize the public web origin used in copied links."""
    value = raw.strip().rstrip("/")
    if not value:
        raise ValueError("site.public_base_url must not be empty")
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("site.public_base_url must use http or https")
    if not parts.hostname:
        raise ValueError("site.public_base_url must include a hostname")
    if parts.username or parts.password:
        raise ValueError("site.public_base_url must not include credentials")
    if parts.query or parts.fragment:
        raise ValueError("site.public_base_url must not include query or fragment")
    if parts.path not in {"", "/"}:
        raise ValueError("site.public_base_url must be the web root, without a path")
    return value


def validate_image_size_thresholds(raw: str) -> str:
    """Validate the JSON shape of billing.image_size_thresholds.

    Expect an object whose values are positive integers (pixel counts).
    """
    value = raw.strip()
    if not value:
        raise ValueError("billing.image_size_thresholds must not be empty")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"billing.image_size_thresholds is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError(
            "billing.image_size_thresholds must be a non-empty JSON object"
        )
    for key, item in parsed.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                "billing.image_size_thresholds keys must be non-empty strings"
            )
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ValueError(
                f"billing.image_size_thresholds[{key!r}] must be a non-negative integer"
            )
    return value


def validate_video_token_hold_estimates(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("video.token_hold_estimates must not be empty")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"video.token_hold_estimates is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("video.token_hold_estimates must be a non-empty JSON object")
    for model, model_value in parsed.items():
        if not isinstance(model, str) or not model.strip():
            raise ValueError("video.token_hold_estimates model keys must be strings")
        if not isinstance(model_value, dict) or not model_value:
            raise ValueError(
                f"video.token_hold_estimates[{model!r}] must be a non-empty object"
            )
        for action, action_value in model_value.items():
            if action not in {
                "t2v",
                "i2v",
                "reference",
                "reference_image",
                "reference_video",
            }:
                raise ValueError(
                    f"video.token_hold_estimates[{model!r}] action must be t2v, i2v, reference, reference_image or reference_video"
                )
            if not isinstance(action_value, dict) or not action_value:
                raise ValueError(
                    f"video.token_hold_estimates[{model!r}][{action!r}] must be a non-empty object"
                )
            for key, estimate in action_value.items():
                if not isinstance(key, str) or ":" not in key:
                    raise ValueError(
                        f"video.token_hold_estimates[{model!r}][{action!r}] keys must look like resolution:duration"
                    )
                if (
                    not isinstance(estimate, int)
                    or isinstance(estimate, bool)
                    or estimate <= 0
                ):
                    raise ValueError(
                        f"video.token_hold_estimates[{model!r}][{action!r}][{key!r}] must be a positive integer"
                    )
    return value


def validate_redemption_code_secret(raw: str) -> str:
    value = raw.strip()
    if len(value) < 16:
        raise ValueError(
            "billing.redemption_code_secret must be at least 16 characters"
        )
    return value


def validate_image_job_base_url(raw: str) -> str:
    """Validate and normalize the async image job service base URL."""
    value = raw.strip().rstrip("/")
    if not value:
        raise ValueError("image.job_base_url must not be empty")
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("image.job_base_url must use http or https")
    if not parts.hostname:
        raise ValueError("image.job_base_url must include a hostname")
    if parts.username or parts.password:
        raise ValueError("image.job_base_url must not include credentials")
    if parts.query or parts.fragment:
        raise ValueError("image.job_base_url must not include query or fragment")
    return value


def _special_setting_value(spec: SettingSpec, raw: str) -> tuple[bool, object | None]:
    validators = {
        "providers": validate_providers,
        "video.providers": lambda value: validate_video_providers(
            value,
            allow_missing_proxy=True,
        ),
        "video.token_hold_estimates": validate_video_token_hold_estimates,
        "site.public_base_url": validate_public_base_url,
        "image.job_base_url": validate_image_job_base_url,
        "billing.image_size_thresholds": validate_image_size_thresholds,
        "billing.redemption_code_secret": validate_redemption_code_secret,
    }
    validator = validators.get(spec.key)
    if validator is None:
        return False, None
    return True, validator(raw)


def _validated_string_value(spec: SettingSpec, raw: str) -> str:
    if spec.allowed_values is not None and raw not in spec.allowed_values:
        allowed = ", ".join(spec.allowed_values)
        raise ValueError(f"{spec.key} must be one of: {allowed}")
    return raw


def _numeric_setting_value(spec: SettingSpec, raw: str) -> int | float:
    if spec.parser is int:
        return int(raw)
    if spec.parser is float:
        return float(raw)
    raise ValueError(f"unsupported parser {spec.parser!r}")


def _validate_numeric_setting(
    spec: SettingSpec,
    raw: str,
    value: int | float,
) -> None:
    if spec.allowed_values is not None:
        # Compare normalized literals before numeric coercion so "00" and "+0"
        # do not silently satisfy an administrator's explicit ("0", "1") set.
        normalized = raw.strip()
        if normalized not in {allowed.strip() for allowed in spec.allowed_values}:
            allowed = ", ".join(spec.allowed_values)
            raise ValueError(f"{spec.key} must be one of: {allowed}")
    if spec.min_value is not None and value < spec.min_value:
        raise ValueError(f"{spec.key}={value} below min ({spec.min_value})")
    if spec.max_value is not None and value > spec.max_value:
        raise ValueError(f"{spec.key}={value} above max ({spec.max_value})")


def parse_value(spec: SettingSpec, raw: str) -> object:
    """根据 spec.parser 把字符串解析成正确类型；失败抛 ValueError。

    数值类型同时校验 min_value / max_value（若 spec 中已配置）。
    """
    is_special, special_value = _special_setting_value(spec, raw)
    if is_special:
        return special_value
    if spec.parser is str:
        return _validated_string_value(spec, raw)
    value = _numeric_setting_value(spec, raw)
    _validate_numeric_setting(spec, raw, value)
    return value


__all__ = [
    "SettingSpec",
    "SUPPORTED_SETTINGS",
    "get_spec",
    "parse_value",
    "validate_image_job_base_url",
    "validate_image_size_thresholds",
    "validate_providers",
    "validate_public_base_url",
    "validate_redemption_code_secret",
    "validate_video_token_hold_estimates",
    "validate_video_providers",
]
