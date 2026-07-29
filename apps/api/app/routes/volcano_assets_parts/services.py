"""Provider, ownership, source, and audit services for Volcano asset routes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, Video
from lumen_core.schemas import VideoAssetCreateIn
from lumen_core.video_providers import VideoProviderDefinition
from lumen_core.volcano_assets import (
    normalize_asset,
    normalize_asset_group,
)

from ...services.volcano_asset_client import VolcanoAssetClient


HttpError = Callable[..., HTTPException]


@dataclass(frozen=True)
class LocalAssetSource:
    asset_type: str
    local_id: str


async def provider_state(
    db: AsyncSession,
    *,
    model: str,
    load_provider_state: Callable[
        [AsyncSession],
        Awaitable[tuple[list[VideoProviderDefinition], list[str]]],
    ],
    capability: Callable[
        ...,
        tuple[VideoProviderDefinition | None, str | None],
    ],
) -> tuple[VideoProviderDefinition | None, str | None]:
    providers, errors = await load_provider_state(db)
    return capability(providers, model=model, errors=errors)


async def require_provider(
    db: AsyncSession,
    *,
    model: str,
    get_provider_state: Callable[
        ...,
        Awaitable[tuple[VideoProviderDefinition | None, str | None]],
    ],
    http_error: HttpError,
) -> VideoProviderDefinition:
    provider, reason = await get_provider_state(db, model=model)
    errors = {
        "video_provider_config_invalid": (
            "video_provider_config_invalid",
            "video provider configuration is invalid",
            503,
        ),
        "reference_provider_missing": (
            "video_asset_provider_missing",
            "no enabled video provider supports reference assets for this model",
            503,
        ),
        "reference_provider_not_official_volcano": (
            "video_asset_provider_unsupported",
            "the selected reference provider does not support Volcano assets",
            409,
        ),
        "volcano_asset_credentials_missing": (
            "volcano_asset_credentials_missing",
            "the selected Volcano provider is missing asset credentials",
            503,
        ),
    }
    if reason in errors:
        code, message, status_code = errors[reason]
        raise http_error(code, message, status_code)
    if provider is None:
        raise http_error(
            "video_asset_provider_missing",
            "no enabled video provider supports reference assets for this model",
            503,
        )
    return provider


async def require_resource_owner(
    db: AsyncSession,
    *,
    user: Any,
    provider: VideoProviderDefinition,
    resource_type: str,
    resource_id: str,
    is_admin: Callable[[Any], bool],
    resource_owner_user_id: Callable[..., Awaitable[str | None]],
    http_error: HttpError,
) -> None:
    if is_admin(user):
        return
    owner_user_id = await resource_owner_user_id(
        db,
        provider=provider,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    if owner_user_id != str(user.id):
        raise http_error(
            "video_asset_forbidden",
            "only the resource owner or an administrator may access this resource",
            403,
            resource_type=resource_type,
            resource_id=resource_id,
        )


async def get_group(
    client: VolcanoAssetClient,
    provider: VideoProviderDefinition,
    group_id: str,
    *,
    require_group_shape: Callable[
        [dict[str, Any], VideoProviderDefinition],
        None,
    ],
) -> dict[str, Any]:
    raw = await client.request(
        "GetAssetGroup",
        {
            "Id": group_id,
            "ProjectName": provider.project_name,
        },
    )
    group = normalize_asset_group(
        raw,
        project_name=provider.project_name,
        fallback={
            "id": group_id,
            "project_name": provider.project_name,
        },
    )
    require_group_shape(group, provider)
    return group


async def get_asset(
    client: VolcanoAssetClient,
    provider: VideoProviderDefinition,
    asset_id: str,
    *,
    require_asset_shape: Callable[
        [dict[str, Any], VideoProviderDefinition],
        None,
    ],
    get_group: Callable[..., Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    raw = await client.request(
        "GetAsset",
        {
            "Id": asset_id,
            "ProjectName": provider.project_name,
        },
    )
    asset = normalize_asset(
        raw,
        project_name=provider.project_name,
        fallback={
            "id": asset_id,
            "project_name": provider.project_name,
        },
    )
    require_asset_shape(asset, provider)
    await get_group(client, provider, str(asset["group_id"]))
    return asset


async def public_base_url(
    request: Request,
    db: AsyncSession,
    *,
    resolve_public_base_url: Callable[[Request, AsyncSession], Awaitable[str]],
    validate_public_reference_url: Callable[[str], str],
    http_error: HttpError,
) -> str:
    try:
        resolved = await resolve_public_base_url(request, db)
    except Exception as exc:  # noqa: BLE001
        raise http_error(
            "video_asset_public_url_missing",
            "PUBLIC_BASE_URL or site.public_base_url is required for asset ingestion",
            503,
        ) from exc
    return validate_public_reference_url(resolved)


async def resolve_local_asset_source(
    *,
    body: VideoAssetCreateIn,
    user_id: str,
    db: AsyncSession,
    http_error: HttpError,
) -> LocalAssetSource:
    if body.image_id is not None:
        image = (
            await db.execute(
                select(Image).where(
                    Image.id == body.image_id,
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if image is None:
            raise http_error(
                "video_asset_image_not_found",
                "asset image was not found",
                404,
            )
        return LocalAssetSource(
            asset_type="Image",
            local_id=image.id,
        )

    video = (
        await db.execute(
            select(Video).where(
                Video.id == body.video_id,
                Video.user_id == user_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise http_error(
            "video_asset_video_not_found",
            "asset video was not found",
            404,
        )
    return LocalAssetSource(
        asset_type="Video",
        local_id=video.id,
    )


async def audit_write(
    *,
    db: AsyncSession,
    request: Request,
    user: Any,
    event_type: str,
    details: dict[str, Any],
    write_audit: Callable[..., Awaitable[Any]],
    hash_email: Callable[[str], str],
    request_ip_hash: Callable[[Request], str],
) -> None:
    await write_audit(
        db,
        event_type=event_type,
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details=details,
        autocommit=False,
    )
    await db.commit()


async def audit_write_best_effort(
    *,
    db: AsyncSession,
    request: Request,
    user: Any,
    event_type: str,
    details: dict[str, Any],
    audit_write: Callable[..., Awaitable[None]],
    logger: logging.Logger,
) -> None:
    try:
        await audit_write(
            db=db,
            request=request,
            user=user,
            event_type=event_type,
            details=details,
        )
    except Exception:  # noqa: BLE001
        logger.error(
            "video_asset.audit_write_failed event_type=%s",
            event_type,
            exc_info=True,
        )
