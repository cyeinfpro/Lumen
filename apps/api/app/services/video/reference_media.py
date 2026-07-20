"""Video reference-media validation and upstream snapshot services."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, Video
from lumen_core.schemas import (
    VideoCreateIn,
    VideoReferenceMediaIn,
    normalize_asset_reference_url,
)
from lumen_core.url_security import is_private_host, resolve_public_http_target
from lumen_core.volcano_assets import volcano_asset_reference_url

from ...config import settings
from ...public_urls import resolve_public_base_url
from ...volcano_asset_media import (
    VOLCANO_ASSET_IMAGE_KIND,
    VOLCANO_ASSET_VIDEO_KIND,
)
from ...video_reference_images import (
    VIDEO_REFERENCE_IMAGE_KIND,
    VideoReferenceImageError,
    ensure_video_reference_image_variant,
)
from ...video_reference_videos import (
    VIDEO_REFERENCE_VIDEO_KIND,
    VIDEO_REFERENCE_VIDEO_MIME,
    VideoReferenceVideoError,
    ensure_video_reference_video_variant,
)
from .errors import video_http_error
from .reference_snapshots import build_reference_media_snapshots


ImageVariantLoader = Callable[..., Awaitable[Any]]
VideoVariantLoader = Callable[..., Awaitable[Any]]
PublicBaseResolver = Callable[[Request, AsyncSession], Awaitable[str]]
PublicTargetResolver = Callable[..., Awaitable[Any]]
ImagePublicUrl = Callable[..., Awaitable[tuple[str | None, dict[str, Any]]]]
VideoPublicUrl = Callable[..., Awaitable[tuple[str, dict[str, Any]]]]
ResolveUrl = Callable[[str], Awaitable[str]]

REFERENCE_ACCESS_TOKEN_TTL = timedelta(hours=24)
HAPPYHORSE_ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")
OMNI_FLASH_ASPECT_RATIOS = ("adaptive", "16:9", "9:16", "1:1")

logger = logging.getLogger(__name__)


def reference_token_expiry(now: datetime | None = None) -> str:
    return (
        (now or datetime.now(timezone.utc)) + REFERENCE_ACCESS_TOKEN_TTL
    ).isoformat()


def parse_reference_token_expiry(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def reference_token_is_valid(
    metadata: dict[str, Any],
    *,
    token_key: str,
    expires_key: str,
    token: str,
    updated_at: datetime | None,
) -> bool:
    expected = metadata.get(token_key)
    if not isinstance(expected, str) or not secrets.compare_digest(expected, token):
        return False
    expires_at = parse_reference_token_expiry(metadata.get(expires_key))
    now = datetime.now(timezone.utc)
    if expires_at is not None:
        return expires_at > now
    if updated_at is None:
        return False
    fallback_updated_at = (
        updated_at.replace(tzinfo=timezone.utc)
        if updated_at.tzinfo is None
        else updated_at.astimezone(timezone.utc)
    )
    return fallback_updated_at + REFERENCE_ACCESS_TOKEN_TTL > now


def ensure_reference_access_token(
    metadata: dict[str, Any],
    *,
    token_key: str,
    expires_key: str,
) -> str:
    token = metadata.get(token_key)
    expires_at = parse_reference_token_expiry(metadata.get(expires_key))
    if (
        not isinstance(token, str)
        or not token
        or expires_at is None
        or expires_at <= datetime.now(timezone.utc)
    ):
        token = secrets.token_urlsafe(32)
    metadata[token_key] = token
    metadata[expires_key] = reference_token_expiry()
    return token


def ensure_reference_video_access_token(video: Video) -> str:
    metadata = dict(video.metadata_jsonb or {})
    token = ensure_reference_access_token(
        metadata,
        token_key="reference_access_token",
        expires_key="reference_access_token_expires_at",
    )
    video.metadata_jsonb = metadata
    return token


def reference_video_public_url(
    video: Video,
    public_base_url: str,
    *,
    variant: str | None = None,
) -> str:
    token = ensure_reference_video_access_token(video)
    if variant == VOLCANO_ASSET_VIDEO_KIND:
        return volcano_asset_reference_url(
            public_base_url,
            resource_id=video.id,
            asset_type="Video",
            token=token,
        )
    query_args = {"token": token}
    if variant:
        query_args["variant"] = variant
    query = urlencode(query_args)
    return (
        f"{public_base_url.rstrip('/')}/api/videos/reference/{video.id}/binary?{query}"
    )


def ensure_reference_image_access_token(image: Image) -> str:
    metadata = dict(image.metadata_jsonb or {})
    token = ensure_reference_access_token(
        metadata,
        token_key="video_reference_access_token",
        expires_key="video_reference_access_token_expires_at",
    )
    image.metadata_jsonb = metadata
    return token


def reference_image_public_url(
    image: Image,
    public_base_url: str,
    *,
    variant: str | None = None,
) -> str:
    token = ensure_reference_image_access_token(image)
    if variant == VOLCANO_ASSET_IMAGE_KIND:
        return volcano_asset_reference_url(
            public_base_url,
            resource_id=image.id,
            asset_type="Image",
            token=token,
        )
    query_args = {"token": token}
    if variant:
        query_args["variant"] = variant
    query = urlencode(query_args)
    return (
        f"{public_base_url.rstrip('/')}/api/images/reference/{image.id}/binary?{query}"
    )


async def reference_image_upstream_public_url(
    db: AsyncSession,
    image: Image,
    public_base_url: str,
    *,
    required: bool = False,
    variant_loader: ImageVariantLoader = ensure_video_reference_image_variant,
) -> tuple[str | None, dict[str, Any]]:
    try:
        variant = await variant_loader(
            db,
            image,
            storage_root=settings.storage_root,
        )
    except VideoReferenceImageError as exc:
        if required:
            raise video_http_error(exc.code, exc.message, exc.status_code) from exc
        logger.warning(
            "video reference image variant unavailable; falling back to inline "
            "media image_id=%s code=%s",
            image.id,
            exc.code,
            exc_info=True,
        )
        return None, {
            "upstream_reference_variant": None,
            "upstream_reference_variant_error": {
                "code": exc.code,
                "message": exc.message[:300],
            },
        }
    except Exception as exc:  # noqa: BLE001
        if required:
            raise video_http_error(
                "video_reference_variant_failed",
                "video reference image variant failed",
                503,
            ) from exc
        logger.warning(
            "video reference image variant failed; falling back to inline media "
            "image_id=%s",
            image.id,
            exc_info=True,
        )
        return None, {
            "upstream_reference_variant": None,
            "upstream_reference_variant_error": {
                "code": "video_reference_variant_failed",
                "message": str(exc)[:300],
            },
        }
    return (
        reference_image_public_url(
            image,
            public_base_url,
            variant=VIDEO_REFERENCE_IMAGE_KIND,
        ),
        {
            "upstream_reference_variant": VIDEO_REFERENCE_IMAGE_KIND,
            "upstream_reference_storage_key": variant.storage_key,
            "upstream_reference_mime": "image/jpeg",
            "upstream_reference_width": variant.width,
            "upstream_reference_height": variant.height,
        },
    )


async def reference_video_upstream_public_url(
    db: AsyncSession,
    video: Video,
    public_base_url: str,
    *,
    variant_loader: VideoVariantLoader = ensure_video_reference_video_variant,
) -> tuple[str, dict[str, Any]]:
    try:
        variant = await variant_loader(
            db,
            video,
            storage_root=settings.storage_root,
        )
    except VideoReferenceVideoError as exc:
        raise video_http_error(exc.code, exc.message, exc.status_code) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "video reference video variant failed video_id=%s",
            video.id,
            exc_info=True,
        )
        raise video_http_error(
            "video_reference_variant_failed",
            "video reference variant failed",
            503,
        ) from exc
    return (
        reference_video_public_url(
            video,
            public_base_url,
            variant=VIDEO_REFERENCE_VIDEO_KIND,
        ),
        {
            "upstream_reference_variant": VIDEO_REFERENCE_VIDEO_KIND,
            "upstream_reference_storage_key": variant["storage_key"],
            "upstream_reference_mime": VIDEO_REFERENCE_VIDEO_MIME,
            "upstream_reference_width": variant["width"],
            "upstream_reference_height": variant["height"],
            "upstream_reference_size_bytes": variant["size_bytes"],
            "upstream_reference_sha256": variant["sha256"],
        },
    )


def provider_requires_public_media(provider: Any) -> bool:
    return getattr(provider, "kind", None) in {"dashscope", "volcano_newapi"}


def provider_prefers_public_media_url(provider: Any) -> bool:
    return provider_requires_public_media(provider) or getattr(
        provider,
        "kind",
        None,
    ) in {"volcano_third_party", "volcano_newapi", "omni_flash"}


def reference_snapshot_ref_id(
    kind: str,
    index: int,
    raw: Any,
    *,
    strict: bool = True,
) -> str:
    default = f"ref:{kind}:{index}"
    if raw is None or isinstance(raw, str) and not raw.strip():
        return default
    if not isinstance(raw, str):
        if not strict:
            return default
        raise video_http_error(
            "invalid_reference_ref_id",
            "reference media ref_id is invalid",
            409,
        )
    value = raw.strip().lower()
    parts = value.split(":")
    if (
        len(parts) == 3
        and parts[0] == "ref"
        and parts[1] == kind
        and parts[2].isdigit()
        and 1 <= int(parts[2]) <= 999
    ):
        return value
    if not strict:
        return default
    raise video_http_error(
        "invalid_reference_ref_id",
        "reference media ref_id is invalid",
        409,
    )


def needs_reference_public_base_url(
    body: VideoCreateIn,
    fallback_snapshots: list[dict[str, Any]] | None,
    *,
    requires_public_media: bool = False,
    prefers_public_media_url: bool = False,
) -> bool:
    use_public_media = requires_public_media or prefers_public_media_url
    if use_public_media and body.action == "i2v" and body.input_image_id:
        return True
    if use_public_media and any(
        item.kind == "image" and item.image_id for item in body.reference_media
    ):
        return True
    if any(item.kind == "video" and item.video_id for item in body.reference_media):
        return True
    if fallback_snapshots is None:
        return False
    return any(
        (
            item.get("kind") == "video"
            and isinstance(item.get("video_id"), str)
            and not isinstance(item.get("url"), str)
        )
        or (
            use_public_media
            and item.get("kind") == "image"
            and isinstance(item.get("image_id"), str)
            and not isinstance(item.get("url"), str)
        )
        for item in fallback_snapshots
        if isinstance(item, dict)
    )


async def reference_public_base_url(
    request: Request | None,
    db: AsyncSession,
    body: VideoCreateIn,
    fallback_snapshots: list[dict[str, Any]] | None,
    *,
    requires_public_media: bool = False,
    prefers_public_media_url: bool = False,
    resolver: PublicBaseResolver = resolve_public_base_url,
) -> str | None:
    hard_requires_public_base = needs_reference_public_base_url(
        body,
        fallback_snapshots,
        requires_public_media=requires_public_media,
    )
    if not needs_reference_public_base_url(
        body,
        fallback_snapshots,
        requires_public_media=requires_public_media,
        prefers_public_media_url=prefers_public_media_url,
    ):
        return None
    if request is None:
        if hard_requires_public_base:
            raise video_http_error(
                "video_reference_public_url_missing",
                "PUBLIC_BASE_URL or site.public_base_url is required for upstream-readable video media",
                503,
            )
        return None
    try:
        return await resolver(request, db)
    except Exception as exc:
        if not hard_requires_public_base:
            logger.info(
                "video reference public URL unavailable; falling back to inline media",
                exc_info=True,
            )
            return None
        raise video_http_error(
            "video_reference_public_url_missing",
            "PUBLIC_BASE_URL or site.public_base_url is required for upstream-readable video media",
            503,
        ) from exc


async def input_image_snapshot(
    db: AsyncSession,
    *,
    user_id: str,
    image_id: str | None,
    fallback_snapshot: tuple[str | None, str | None, str | None] | None = None,
    reference_public_base_url: str | None = None,
    required_public_media: bool = False,
    image_public_url: ImagePublicUrl = reference_image_upstream_public_url,
) -> tuple[str | None, str | None, str | None]:
    if image_id is None:
        if fallback_snapshot is not None:
            return fallback_snapshot
        return None, None, None
    if (
        fallback_snapshot is not None
        and fallback_snapshot[0]
        and reference_public_base_url is None
    ):
        return fallback_snapshot
    image = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if image is None:
        if fallback_snapshot is not None:
            return fallback_snapshot
        raise video_http_error(
            "input_image_not_found",
            "input image not found",
            404,
        )
    url = None
    if reference_public_base_url is not None:
        url, _meta = await image_public_url(
            db,
            image,
            reference_public_base_url,
            required=required_public_media,
        )
    return image.storage_key, image.sha256, url


def validate_reference_url(raw_url: str) -> str:
    asset_url = normalize_asset_reference_url(raw_url)
    if asset_url is not None:
        if not asset_url:
            raise video_http_error(
                "invalid_reference_url",
                "asset reference is empty",
                422,
            )
        return asset_url
    value = raw_url.strip()
    parts = urlsplit(value)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise video_http_error(
            "invalid_reference_url",
            "reference URL must be https or asset://",
            422,
        )
    if parts.username or parts.password:
        raise video_http_error(
            "invalid_reference_url",
            "reference URL must not include credentials",
            422,
        )
    try:
        _ = parts.port
    except ValueError as exc:
        raise video_http_error(
            "invalid_reference_url",
            "reference URL port is invalid",
            422,
        ) from exc
    if is_private_host(parts.hostname):
        raise video_http_error(
            "invalid_reference_url",
            "reference URL host is not allowed",
            422,
        )
    return value


async def resolve_reference_url(
    raw_url: str,
    *,
    resolver: PublicTargetResolver = resolve_public_http_target,
) -> str:
    value = validate_reference_url(raw_url)
    if normalize_asset_reference_url(value) is not None:
        return value
    try:
        target = await resolver(
            value,
            allow_http=False,
            strip_trailing_slash=False,
        )
    except ValueError as exc:
        raise video_http_error(
            "invalid_reference_url",
            "reference URL host is not allowed",
            422,
        ) from exc
    return target.url


async def reference_media_snapshots(
    db: AsyncSession,
    *,
    user_id: str,
    items: list[VideoReferenceMediaIn],
    fallback_snapshots: list[dict[str, Any]] | None = None,
    reference_public_base_url: str | None = None,
    required_public_media: bool = False,
    resolve_url: ResolveUrl = resolve_reference_url,
    image_public_url: ImagePublicUrl = reference_image_upstream_public_url,
    video_public_url: VideoPublicUrl = reference_video_upstream_public_url,
) -> list[dict[str, Any]]:
    return await build_reference_media_snapshots(
        db,
        user_id=user_id,
        items=items,
        fallback_snapshots=fallback_snapshots,
        reference_public_base_url=reference_public_base_url,
        required_public_media=required_public_media,
        reference_id=reference_snapshot_ref_id,
        resolve_url=resolve_url,
        image_public_url=image_public_url,
        video_public_url=video_public_url,
        http_error=video_http_error,
    )


def validate_provider_reference_media(
    provider_kind: str,
    reference_snapshots: list[dict[str, Any]],
) -> None:
    if provider_kind == "dashscope" and any(
        item.get("kind") == "video"
        for item in reference_snapshots
        if isinstance(item, dict)
    ):
        raise video_http_error(
            "unsupported_reference_media",
            "HappyHorse reference-to-video supports image references only",
            422,
        )
    if provider_kind == "omni_flash" and any(
        item.get("kind") in {"video", "audio"}
        for item in reference_snapshots
        if isinstance(item, dict)
    ):
        raise video_http_error(
            "unsupported_reference_media",
            "Omni Flash unified video create supports image references only",
            422,
        )
    image_count = sum(
        1
        for item in reference_snapshots
        if isinstance(item, dict) and item.get("kind") == "image"
    )
    video_count = sum(
        1
        for item in reference_snapshots
        if isinstance(item, dict) and item.get("kind") == "video"
    )
    audio_count = sum(
        1
        for item in reference_snapshots
        if isinstance(item, dict) and item.get("kind") == "audio"
    )
    if provider_kind not in {"volcano", "volcano_newapi"} and audio_count:
        raise video_http_error(
            "unsupported_reference_media",
            "reference audio is only available for Volcano Seedance providers",
            422,
        )
    if provider_kind not in {"volcano", "volcano_newapi"}:
        return

    image_limit = 9 if provider_kind == "volcano" else 4
    audio_limit = 3 if provider_kind == "volcano" else 1
    provider_label = (
        "Volcano Seedance" if provider_kind == "volcano" else "Volcano New API"
    )
    if image_count > image_limit:
        raise video_http_error(
            "too_many_reference_images",
            f"{provider_label} supports at most {image_limit} reference images",
            422,
        )
    if video_count > 3:
        raise video_http_error(
            "too_many_reference_videos",
            f"{provider_label} supports at most 3 reference videos",
            422,
        )
    if audio_count > audio_limit:
        raise video_http_error(
            "too_many_reference_audios",
            f"{provider_label} supports at most {audio_limit} reference audio"
            + (" references" if audio_limit != 1 else ""),
            422,
        )
    if audio_count and not (image_count or video_count):
        raise video_http_error(
            "reference_audio_requires_visual",
            "reference audio must be combined with an image or video",
            422,
        )
