"""Reference media snapshot construction for video generation routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, Video
from lumen_core.schemas import VideoReferenceMediaIn


HttpErrorFactory = Callable[..., Exception]
ResolveUrl = Callable[[str], Awaitable[str]]
ReferenceIdFactory = Callable[..., str]
ImagePublicUrl = Callable[..., Awaitable[tuple[str | None, dict[str, Any]]]]
VideoPublicUrl = Callable[..., Awaitable[tuple[str, dict[str, Any]]]]

_KIND_LABELS = {
    "image": "Image",
    "video": "Video",
    "audio": "Audio",
}


@dataclass
class _KindCounters:
    values: dict[str, int] = field(
        default_factory=lambda: {kind: 0 for kind in _KIND_LABELS}
    )

    def next(self, kind: str) -> tuple[int, str, str]:
        self.values[kind] += 1
        index = self.values[kind]
        label = f"{_KIND_LABELS[kind]} {index}"
        return index, label, f"ref:{kind}:{index}"


def _snapshot_identity(
    snapshot: dict[str, Any],
    counters: _KindCounters,
    *,
    reference_id: ReferenceIdFactory,
    http_error: HttpErrorFactory,
) -> str:
    kind = snapshot.get("kind")
    if kind not in _KIND_LABELS:
        raise http_error(
            "invalid_reference_media",
            "reference media snapshot kind is invalid",
            409,
        )
    index, default_label, _default_ref_id = counters.next(kind)
    snapshot["ref_id"] = reference_id(
        kind,
        index,
        snapshot.get("ref_id"),
        strict=False,
    )
    label = snapshot.get("label")
    if not isinstance(label, str) or not label.strip():
        snapshot["label"] = default_label
    return kind


def _trim_local_ids(snapshot: dict[str, Any]) -> None:
    for id_key in ("image_id", "video_id"):
        raw_id = snapshot.get(id_key)
        if isinstance(raw_id, str):
            snapshot[id_key] = raw_id.strip() or None


def _has_local_source(snapshot: dict[str, Any], kind: str) -> bool:
    if kind == "image":
        return isinstance(snapshot.get("image_id"), str)
    if kind == "video":
        return isinstance(snapshot.get("video_id"), str)
    return False


async def _normalize_fallback_snapshot(
    snapshot: dict[str, Any],
    counters: _KindCounters,
    resolved_ref_ids: set[str],
    *,
    reference_public_base_url: str | None,
    reference_id: ReferenceIdFactory,
    resolve_url: ResolveUrl,
    http_error: HttpErrorFactory,
) -> None:
    kind = _snapshot_identity(
        snapshot,
        counters,
        reference_id=reference_id,
        http_error=http_error,
    )
    ref_id = str(snapshot["ref_id"])
    if ref_id in resolved_ref_ids:
        raise http_error(
            "duplicate_reference_ref_id",
            "reference media snapshot ref_id values must be unique",
            409,
        )
    resolved_ref_ids.add(ref_id)
    _trim_local_ids(snapshot)
    has_local_source = _has_local_source(snapshot, kind)
    raw_url = snapshot.get("url")
    if has_local_source and reference_public_base_url is None:
        snapshot["url"] = None
    elif isinstance(raw_url, str) and not has_local_source:
        snapshot["url"] = await resolve_url(raw_url)
    if not has_local_source and not isinstance(snapshot.get("url"), str):
        raise http_error(
            "invalid_reference_media",
            "reference media snapshot source is missing",
            409,
        )


async def _owned_image(
    db: AsyncSession,
    *,
    user_id: str,
    image_id: str,
    missing_message: str,
    missing_status: int,
    http_error: HttpErrorFactory,
) -> Image:
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
        raise http_error(
            "reference_image_not_found",
            missing_message,
            missing_status,
        )
    return image


async def _owned_video(
    db: AsyncSession,
    *,
    user_id: str,
    video_id: str,
    missing_message: str,
    missing_status: int,
    http_error: HttpErrorFactory,
) -> Video:
    video = (
        await db.execute(
            select(Video).where(
                Video.id == video_id,
                Video.user_id == user_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise http_error(
            "reference_video_not_found",
            missing_message,
            missing_status,
        )
    return video


async def _refresh_fallback_snapshot(
    snapshot: dict[str, Any],
    *,
    db: AsyncSession,
    user_id: str,
    reference_public_base_url: str | None,
    required_public_media: bool,
    image_public_url: ImagePublicUrl,
    video_public_url: VideoPublicUrl,
    http_error: HttpErrorFactory,
) -> None:
    image_id = snapshot.get("image_id")
    if snapshot.get("kind") == "image" and isinstance(image_id, str):
        image = await _owned_image(
            db,
            user_id=user_id,
            image_id=image_id,
            missing_message="reference image is no longer available",
            missing_status=409,
            http_error=http_error,
        )
        if reference_public_base_url is not None:
            url, metadata = await image_public_url(
                db,
                image,
                reference_public_base_url,
                required=required_public_media,
            )
            snapshot["url"] = url
            snapshot.update(metadata)
        return

    video_id = snapshot.get("video_id")
    if snapshot.get("kind") == "video" and isinstance(video_id, str):
        video = await _owned_video(
            db,
            user_id=user_id,
            video_id=video_id,
            missing_message="reference video is no longer available",
            missing_status=409,
            http_error=http_error,
        )
        if reference_public_base_url is not None:
            url, metadata = await video_public_url(
                db,
                video,
                reference_public_base_url,
            )
            snapshot["url"] = url
            snapshot.update(metadata)


async def _fallback_snapshots(
    db: AsyncSession,
    *,
    user_id: str,
    fallback_snapshots: list[dict[str, Any]],
    reference_public_base_url: str | None,
    required_public_media: bool,
    reference_id: ReferenceIdFactory,
    resolve_url: ResolveUrl,
    image_public_url: ImagePublicUrl,
    video_public_url: VideoPublicUrl,
    http_error: HttpErrorFactory,
) -> list[dict[str, Any]]:
    snapshots = [dict(item) for item in fallback_snapshots]
    counters = _KindCounters()
    resolved_ref_ids: set[str] = set()
    for snapshot in snapshots:
        await _normalize_fallback_snapshot(
            snapshot,
            counters,
            resolved_ref_ids,
            reference_public_base_url=reference_public_base_url,
            reference_id=reference_id,
            resolve_url=resolve_url,
            http_error=http_error,
        )
    for snapshot in snapshots:
        await _refresh_fallback_snapshot(
            snapshot,
            db=db,
            user_id=user_id,
            reference_public_base_url=reference_public_base_url,
            required_public_media=required_public_media,
            image_public_url=image_public_url,
            video_public_url=video_public_url,
            http_error=http_error,
        )
    return snapshots


async def _image_snapshot(
    db: AsyncSession,
    item: VideoReferenceMediaIn,
    *,
    user_id: str,
    label: str,
    ref_id: str,
    reference_public_base_url: str | None,
    required_public_media: bool,
    image_public_url: ImagePublicUrl,
    http_error: HttpErrorFactory,
) -> dict[str, Any]:
    image = await _owned_image(
        db,
        user_id=user_id,
        image_id=str(item.image_id),
        missing_message="reference image not found",
        missing_status=404,
        http_error=http_error,
    )
    url = None
    upstream_metadata: dict[str, Any] = {}
    if reference_public_base_url is not None:
        url, upstream_metadata = await image_public_url(
            db,
            image,
            reference_public_base_url,
            required=required_public_media,
        )
    return {
        "kind": "image",
        "image_id": image.id,
        "label": label,
        "ref_id": ref_id,
        "storage_key": image.storage_key,
        "sha256": image.sha256,
        "mime": image.mime,
        "url": url,
        "source": "image",
        **upstream_metadata,
    }


async def _video_snapshot(
    db: AsyncSession,
    item: VideoReferenceMediaIn,
    *,
    user_id: str,
    label: str,
    ref_id: str,
    reference_public_base_url: str | None,
    video_public_url: VideoPublicUrl,
    http_error: HttpErrorFactory,
) -> dict[str, Any]:
    video = await _owned_video(
        db,
        user_id=user_id,
        video_id=str(item.video_id),
        missing_message="reference video not found",
        missing_status=404,
        http_error=http_error,
    )
    snapshot = {
        "kind": "video",
        "video_id": video.id,
        "label": label,
        "ref_id": ref_id,
        "storage_key": video.storage_key,
        "sha256": video.sha256,
        "mime": video.mime,
        "source": "video",
        "url": None,
    }
    if reference_public_base_url is not None:
        url, upstream_metadata = await video_public_url(
            db,
            video,
            reference_public_base_url,
        )
        snapshot["url"] = url
        snapshot.update(upstream_metadata)
    return snapshot


async def _input_snapshot(
    db: AsyncSession,
    item: VideoReferenceMediaIn,
    counters: _KindCounters,
    *,
    user_id: str,
    reference_public_base_url: str | None,
    required_public_media: bool,
    resolve_url: ResolveUrl,
    image_public_url: ImagePublicUrl,
    video_public_url: VideoPublicUrl,
    http_error: HttpErrorFactory,
) -> dict[str, Any]:
    _index, default_label, default_ref_id = counters.next(item.kind)
    label = (item.label or "").strip() or default_label
    ref_id = (item.ref_id or "").strip() or default_ref_id
    if item.url:
        return {
            "kind": item.kind,
            "url": await resolve_url(item.url),
            "label": label,
            "ref_id": ref_id,
            "source": "url",
        }
    if item.kind == "image" and item.image_id:
        return await _image_snapshot(
            db,
            item,
            user_id=user_id,
            label=label,
            ref_id=ref_id,
            reference_public_base_url=reference_public_base_url,
            required_public_media=required_public_media,
            image_public_url=image_public_url,
            http_error=http_error,
        )
    if item.kind == "video" and item.video_id:
        return await _video_snapshot(
            db,
            item,
            user_id=user_id,
            label=label,
            ref_id=ref_id,
            reference_public_base_url=reference_public_base_url,
            video_public_url=video_public_url,
            http_error=http_error,
        )
    raise http_error("invalid_reference_media", "reference media is invalid", 422)


async def build_reference_media_snapshots(
    db: AsyncSession,
    *,
    user_id: str,
    items: list[VideoReferenceMediaIn],
    fallback_snapshots: list[dict[str, Any]] | None,
    reference_public_base_url: str | None,
    required_public_media: bool,
    reference_id: ReferenceIdFactory,
    resolve_url: ResolveUrl,
    image_public_url: ImagePublicUrl,
    video_public_url: VideoPublicUrl,
    http_error: HttpErrorFactory,
) -> list[dict[str, Any]]:
    if fallback_snapshots is not None:
        return await _fallback_snapshots(
            db,
            user_id=user_id,
            fallback_snapshots=fallback_snapshots,
            reference_public_base_url=reference_public_base_url,
            required_public_media=required_public_media,
            reference_id=reference_id,
            resolve_url=resolve_url,
            image_public_url=image_public_url,
            video_public_url=video_public_url,
            http_error=http_error,
        )
    counters = _KindCounters()
    return [
        await _input_snapshot(
            db,
            item,
            counters,
            user_id=user_id,
            reference_public_base_url=reference_public_base_url,
            required_public_media=required_public_media,
            resolve_url=resolve_url,
            image_public_url=image_public_url,
            video_public_url=video_public_url,
            http_error=http_error,
        )
        for item in items
    ]
