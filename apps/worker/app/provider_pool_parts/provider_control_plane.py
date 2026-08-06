"""Fail-closed runtime setting helpers shared by provider dispatch."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from ..provider_runtime.errors import UpstreamError
from ..runtime_settings import SettingResolution, SettingUnavailable

SettingResolver = Callable[[str], Awaitable[str | None]]


class ImageDispatchConfigurationError(UpstreamError):
    """Image dispatch settings are present but invalid."""


def image_control_plane_error(key: str) -> UpstreamError:
    return UpstreamError(
        f"image dispatch setting unavailable: {key}",
        error_code="image_control_plane_unavailable",
        status_code=503,
        payload={"setting": key, "retryable": True},
    )


def invalid_image_dispatch_setting(key: str, value: str) -> UpstreamError:
    return ImageDispatchConfigurationError(
        f"invalid {key}={value!r}",
        error_code="image_dispatch_configuration_invalid",
        status_code=503,
        payload={"setting": key, "value": value, "retryable": False},
    )


def legacy_route_to_channel_engine(route: str | None) -> tuple[str, str]:
    value = (route or "").strip().lower()
    if value == "image2":
        return "auto", "image2"
    if value == "image_jobs":
        return "image_jobs_only", "responses"
    if value == "dual_race":
        return "auto", "dual_race"
    return "auto", "responses"


async def resolve_explicit_image_dispatch_setting(
    resolve_db: SettingResolver,
    key: str,
    env_name: str,
    *,
    logger: logging.Logger,
) -> SettingResolution:
    try:
        raw = await resolve_db(key)
    except SettingUnavailable:
        logger.error("image dispatch DB setting unavailable key=%s", key, exc_info=True)
        return SettingResolution("unavailable")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "image dispatch DB setting lookup failed key=%s err=%s",
            key,
            exc,
            exc_info=True,
        )
        return SettingResolution("unavailable")
    if raw is not None:
        return SettingResolution("value", str(raw), "database")
    env_value = os.environ.get(env_name)
    if env_value is None or env_value.strip() == "":
        return SettingResolution("missing")
    return SettingResolution("value", env_value, "environment")


def validated_image_dispatch_value(
    resolution: SettingResolution,
    *,
    key: str,
    allowed: frozenset[str],
) -> str | None:
    if resolution.state == "unavailable":
        raise image_control_plane_error(key)
    if resolution.state == "missing":
        return None
    value = str(resolution.value or "").strip().lower()
    if value not in allowed:
        raise invalid_image_dispatch_setting(key, value)
    return value


async def resolve_legacy_image_primary_route(
    resolve: SettingResolver,
    *,
    keys: tuple[str, ...],
    allowed: frozenset[str],
    logger: logging.Logger,
) -> str | None:
    for key in keys:
        try:
            raw = await resolve(key)
        except SettingUnavailable as exc:
            logger.error("legacy image route unavailable key=%s", key, exc_info=True)
            raise image_control_plane_error(key) from exc
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "legacy image route lookup failed key=%s err=%s",
                key,
                exc,
                exc_info=True,
            )
            raise image_control_plane_error(key) from exc
        if raw is None:
            continue
        value = str(raw).strip().lower()
        if value not in allowed:
            raise invalid_image_dispatch_setting(key, value)
        return value
    return None


__all__ = [
    "ImageDispatchConfigurationError",
    "image_control_plane_error",
    "invalid_image_dispatch_setting",
    "legacy_route_to_channel_engine",
    "resolve_explicit_image_dispatch_setting",
    "resolve_legacy_image_primary_route",
    "validated_image_dispatch_value",
]
