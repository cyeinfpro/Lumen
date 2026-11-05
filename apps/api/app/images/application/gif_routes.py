"""Owner-only GIF editing. Never calls an image model."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Header, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.capacity_leases import (
    maintained_capacity_lease,
    race_with_capacity_leases,
)
from lumen_core.gif_editing import MAX_GIF_BYTES, probe_gif
from lumen_core.model_entities import Image
from lumen_core.schema_models import ImageOut
from ...db import get_db
from ...deps import CurrentUser, durable_session_id, verify_csrf
from ...ratelimit import RateLimiter
from ...redis_client import get_redis
from ...idempotency.advisory import lock_user_key
from ..adapters.local_capacity import CapacityLimits
from ..composition import get_upload_command_service
from ..domain.artifact import ArtifactStatus
from ..domain.resource_estimate import ImageResourceEstimate
from .upload import (
    UploadCommandService,
    UploadPolicy,
    UploadCommandError,
    map_upload_error,
)

from .gif_support import (
    check_active_user,
    check_upload_rate_limit,
    validate_operation_key,
    upload_digest,
    read_gif_source,
    ensure_gif_storage_space,
)
from .image_access import get_owned_image, ensure_image_visible_to_user
from .http_route_parts.presentation import image_out
from .http_route_parts.upload import http_error

router = APIRouter()
# Navigation must not spend the upload bucket's four initial write tokens.
# Shared processing leases still admit every actual decode.
GIF_READ_LIMITER = RateLimiter(
    capacity=120,
    refill_per_sec=2,
    always_on=True,
    initial_tokens=16,
)


async def _check_gif_read_rate_limit(user_id: str) -> None:
    await GIF_READ_LIMITER.check(get_redis(), f"rl:gif-read:{user_id}")


class RetimeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed: float = Field(ge=0.1, le=10, allow_inf_nan=False)


async def _owner_image(db: AsyncSession, user: Any, image_id: str) -> Image:
    image = await get_owned_image(db, user, image_id)
    if image.mime != "image/gif":
        raise http_error("invalid_gif", "source must be a GIF", 400)
    return image


async def _replacement_bytes(value) -> bytes:
    if isinstance(value, bytes):
        return value
    await value.seek(0)
    data = await value.read(MAX_GIF_BYTES + 1)
    if len(data) > MAX_GIF_BYTES:
        raise ValueError("replacement exceeds 32 MiB")
    return data


async def _run_gif(
    service, image, operation, *, frame_index=0, speed=1, replacement=b""
):
    # Admit bounded file reads before decoding. Pillow runs in the existing
    # killable subprocess executor under renewed shared-capacity leases.
    initial = ImageResourceEstimate(MAX_GIF_BYTES * 2, 0, 0, MAX_GIF_BYTES, 0, 1)
    ttl = CapacityLimits.from_env().lease_ttl_seconds
    try:
        lease = await service.capacity.reserve(initial)
        async with maintained_capacity_lease(lease, ttl_seconds=ttl) as guard:
            source = await race_with_capacity_leases(
                asyncio.to_thread(read_gif_source, image), (guard,)
            )
            info = await race_with_capacity_leases(
                asyncio.to_thread(probe_gif, source), (guard,)
            )
            del source
        pixels = info["width"] * info["height"] * info["frame_count"]
        estimate = ImageResourceEstimate(
            MAX_GIF_BYTES * 2,
            pixels * 4,
            pixels * 24,
            MAX_GIF_BYTES * 6,
            pixels,
            max(1, min(64, (pixels + 999999) // 1000000)),
        )
        lease = await service.capacity.reserve(estimate)
        async with maintained_capacity_lease(lease, ttl_seconds=ttl) as guard:
            # Re-read only after the decoded-work lease is admitted. No large
            # buffer survives the gap between capacity reservations.
            source = await race_with_capacity_leases(
                asyncio.to_thread(read_gif_source, image), (guard,)
            )
            replacement_data = await race_with_capacity_leases(
                _replacement_bytes(replacement), (guard,)
            )
            return await race_with_capacity_leases(
                service.processing_executor.gif(
                    source,
                    operation,
                    frame_index=frame_index,
                    speed=speed,
                    replacement=replacement_data,
                ),
                (guard,),
            )
    except Exception as exc:
        mapped = map_upload_error(exc)
        if mapped is not None:
            raise http_error(mapped.code, mapped.message, mapped.status_code) from exc
        if isinstance(exc, ValueError):
            raise http_error("invalid_gif", str(exc), 400) from exc
        raise


async def _save_edit(
    *,
    db,
    user,
    request,
    service,
    image_id,
    operation,
    key,
    frame_index=0,
    speed=1,
    replacement=b"",
):
    key = validate_operation_key(key)
    await check_upload_rate_limit(user.id)
    await check_active_user(db, user, request)
    image = await _owner_image(db, user, image_id)
    replacement_digest = (
        hashlib.sha256(replacement).hexdigest()
        if isinstance(replacement, bytes)
        else await upload_digest(replacement)
    )
    operation_key = hashlib.sha256(key.encode()).hexdigest()
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                image_id,
                image.sha256,
                operation,
                frame_index,
                speed,
                replacement_digest,
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    # Hold this lock across the artifact service's independent commits.
    # The operation marker is persisted in its FIRST staging transaction.
    await lock_user_key(db, "gif-edit", user.id, key)
    previous = (
        await db.execute(
            select(Image)
            .where(
                Image.user_id == user.id,
                Image.metadata_jsonb["local_gif_edit"]["key"].as_string()
                == operation_key,
            )
            .order_by(Image.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if previous is not None:
        marker = previous.metadata_jsonb.get("local_gif_edit", {})
        if marker.get("fingerprint") != fingerprint:
            raise http_error(
                "idempotency_conflict", "this key was used for different GIF edits", 409
            )
        if (
            previous.deleted_at is not None
            or previous.artifact_status != ArtifactStatus.READY.value
        ):
            raise http_error(
                "gif_edit_pending",
                "this edit is pending or unavailable; do not submit it again",
                409,
            )
        await ensure_image_visible_to_user(db, previous, user)
        result = await image_out(db, previous)
        await db.commit()
        return result
    output = await _run_gif(
        service,
        image,
        operation,
        frame_index=frame_index,
        speed=speed,
        replacement=replacement,
    )
    await check_active_user(db, user, request)
    await _owner_image(db, user, image_id)
    marker = {
        "key": operation_key,
        "fingerprint": fingerprint,
        "operation": operation,
        "source_image_id": image.id,
        "frame_index": frame_index if operation == "replace" else None,
        "speed": speed if operation == "retime" else None,
    }
    policy = UploadPolicy(
        allowed_mime=frozenset({"image/gif"}),
        normalizable_mime=frozenset(),
        extensions={"image/gif": "gif"},
        max_bytes=MAX_GIF_BYTES,
        max_pixels=64_000_000,
        max_long_side=64_000_000,
        mask_requested=False,
        reference_size=None,
        purpose="local_gif_edit",
        parent_image_id=image.id,
        initial_metadata={"local_gif_edit": marker},
    )
    file = UploadFile(file=io.BytesIO(output), filename="edited.gif")
    try:
        saved = await service.execute(
            user_id=user.id,
            session_id=durable_session_id(request),
            upload_file=file,
            filename="edited.gif",
            policy=policy,
            metadata_finalizer=lambda _id, _ext, metadata: metadata.update(
                local_gif_edit=marker
            ),
            storage_guard=ensure_gif_storage_space,
        )
    except UploadCommandError as exc:
        raise http_error(exc.code, exc.message, exc.status_code) from exc
    finally:
        await file.close()
    result = await image_out(db, saved)
    await db.commit()
    return result


@router.get("/{image_id}/gif")
async def inspect_image_gif(
    image_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[UploadCommandService, Depends(get_upload_command_service)],
):
    await _check_gif_read_rate_limit(user.id)
    image = await _owner_image(db, user, image_id)
    return await _run_gif(service, image, "inspect")


@router.get("/{image_id}/gif/frames/{frame_index}")
async def extract_image_gif_frame(
    image_id: str,
    frame_index: int,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[UploadCommandService, Depends(get_upload_command_service)],
):
    await _check_gif_read_rate_limit(user.id)
    image = await _owner_image(db, user, image_id)
    data = await _run_gif(service, image, "extract", frame_index=frame_index)
    return Response(
        data,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="frame-{frame_index + 1}.png"',
        },
    )


@router.post(
    "/{image_id}/gif/retime",
    response_model=ImageOut,
    dependencies=[Depends(verify_csrf)],
)
async def retime_image_gif(
    image_id: str,
    payload: RetimeRequest,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[UploadCommandService, Depends(get_upload_command_service)],
    idempotency_key: Annotated[str | None, Header()] = None,
):
    return await _save_edit(
        db=db,
        user=user,
        request=request,
        service=service,
        image_id=image_id,
        operation="retime",
        key=idempotency_key,
        speed=payload.speed,
    )


@router.post(
    "/{image_id}/gif/frames/{frame_index}",
    response_model=ImageOut,
    dependencies=[Depends(verify_csrf)],
)
async def replace_image_gif_frame(
    image_id: str,
    frame_index: int,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[UploadCommandService, Depends(get_upload_command_service)],
    file: UploadFile = File(...),
    idempotency_key: Annotated[str | None, Header()] = None,
):
    validate_operation_key(idempotency_key)
    await _owner_image(db, user, image_id)
    try:
        signature = await file.read(8)
        await file.seek(0)
        if signature != b"\x89PNG\r\n\x1a\n":
            raise http_error("invalid_png", "replacement must be a PNG", 400)
        return await _save_edit(
            db=db,
            user=user,
            request=request,
            service=service,
            image_id=image_id,
            operation="replace",
            key=idempotency_key,
            frame_index=frame_index,
            replacement=file,
        )
    finally:
        await file.close()
