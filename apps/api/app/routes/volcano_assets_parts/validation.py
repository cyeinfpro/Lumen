"""Validation and HTTP error helpers for Volcano asset routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException

from lumen_core.url_security import is_private_host
from lumen_core.video_providers import (
    VideoProviderDefinition,
    select_video_provider,
)


AIGC_GROUP_TYPE = "AIGC"


def http_error(
    code: str,
    message: str,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
    **details: Any,
) -> HTTPException:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return HTTPException(
        status_code=status_code,
        detail={"error": error},
        headers=headers,
    )


def capability(
    providers: list[VideoProviderDefinition],
    *,
    model: str,
    errors: list[str] | None = None,
) -> tuple[VideoProviderDefinition | None, str | None]:
    if errors:
        return None, "video_provider_config_invalid"
    provider = select_video_provider(
        providers,
        model=model,
        action="reference",
    )
    if provider is None:
        return None, "reference_provider_missing"
    if provider.kind != "volcano":
        return provider, "reference_provider_not_official_volcano"
    if not provider.asset_management_ready:
        return provider, "volcano_asset_credentials_missing"
    return provider, None


def is_admin(user: Any) -> bool:
    return getattr(user, "role", "") == "admin"


def require_group_shape(
    group: dict[str, Any],
    provider: VideoProviderDefinition,
    *,
    http_error: Callable[..., HTTPException],
) -> None:
    if not group.get("id"):
        raise http_error(
            "volcano_asset_invalid_response",
            "Volcano asset service returned a group without an id",
            502,
        )
    if str(group.get("group_type") or "").upper() != AIGC_GROUP_TYPE:
        raise http_error(
            "volcano_asset_scope_mismatch",
            "the asset group is outside the AIGC scope",
            403,
        )
    if group.get("project_name") != provider.project_name:
        raise http_error(
            "volcano_asset_scope_mismatch",
            "the asset group is outside the configured project",
            403,
        )


def require_asset_shape(
    asset: dict[str, Any],
    provider: VideoProviderDefinition,
    *,
    http_error: Callable[..., HTTPException],
) -> None:
    if not asset.get("id") or not asset.get("group_id"):
        raise http_error(
            "volcano_asset_invalid_response",
            "Volcano asset service returned an incomplete asset",
            502,
        )
    if asset.get("project_name") != provider.project_name:
        raise http_error(
            "volcano_asset_scope_mismatch",
            "the asset is outside the configured project",
            403,
        )


def validate_public_reference_url(
    url: str,
    *,
    http_error: Callable[..., HTTPException],
) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or is_private_host(parts.hostname)
    ):
        raise http_error(
            "video_asset_public_url_invalid",
            "a public HTTPS URL is required for Volcano asset ingestion",
            503,
        )
    return url


def http_error_code(exc: HTTPException) -> str | None:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    error = detail.get("error") if isinstance(detail, dict) else None
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return str(code) if code else None
