"""Explicit GIF import, separate from static model-input uploads."""

from __future__ import annotations

import hashlib
from typing import Annotated
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.gif_editing import MAX_GIF_BYTES
from lumen_core.model_entities import Image
from lumen_core.schema_models import ImageOut
from ...db import get_db
from ...deps import CurrentUser, durable_session_id, verify_csrf
from ...idempotency.advisory import lock_user_key
from ..composition import get_upload_command_service
from ..domain.artifact import ArtifactStatus
from .gif_support import (
    check_active_user,
    check_upload_rate_limit,
    validate_operation_key,
    upload_digest,
    ensure_gif_storage_space,
)
from .image_access import ensure_image_visible_to_user
from .http_route_parts.presentation import image_out
from .http_route_parts.upload import http_error
from .upload import UploadCommandError, UploadCommandService, UploadPolicy

router = APIRouter()


async def _import_gif_impl(request, user, db, service, file, idempotency_key):
    key = validate_operation_key(idempotency_key)
    await check_upload_rate_limit(user.id)
    await check_active_user(db, user, request)
    # Hash the already-spooled upload in bounded chunks, then let the normal
    # artifact service stage/inspect/decode it under its existing leases.
    fingerprint = await upload_digest(file)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    await lock_user_key(db, "gif-import", user.id, key)
    previous = (
        await db.execute(
            select(Image)
            .where(
                Image.user_id == user.id,
                Image.metadata_jsonb["local_gif_import"]["key"].as_string() == key_hash,
            )
            .order_by(Image.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if previous is not None:
        if previous.metadata_jsonb["local_gif_import"]["fingerprint"] != fingerprint:
            raise http_error(
                "idempotency_conflict", "this key was used for another GIF", 409
            )
        if (
            previous.deleted_at is not None
            or previous.artifact_status != ArtifactStatus.READY.value
        ):
            raise http_error(
                "gif_edit_pending",
                "this import is pending or unavailable; do not submit it again",
                409,
            )
        await ensure_image_visible_to_user(db, previous, user)
        result = await image_out(db, previous)
        await db.commit()
        return result
    marker = {"key": key_hash, "fingerprint": fingerprint}
    policy = UploadPolicy(
        allowed_mime=frozenset({"image/gif"}),
        normalizable_mime=frozenset(),
        extensions={"image/gif": "gif"},
        max_bytes=MAX_GIF_BYTES,
        max_pixels=64_000_000,
        max_long_side=64_000_000,
        mask_requested=False,
        reference_size=None,
        purpose="local_gif_import",
        initial_metadata={"local_gif_import": marker},
    )
    try:
        saved = await service.execute(
            user_id=user.id,
            session_id=durable_session_id(request),
            upload_file=file,
            filename="imported.gif",
            policy=policy,
            metadata_finalizer=lambda _id, _ext, metadata: metadata.update(
                local_gif_import=marker
            ),
            storage_guard=ensure_gif_storage_space,
        )
    except UploadCommandError as exc:
        raise http_error(exc.code, exc.message, exc.status_code) from exc
    result = await image_out(db, saved)
    await db.commit()
    return result


@router.post(
    "/gif/upload", response_model=ImageOut, dependencies=[Depends(verify_csrf)]
)
async def import_gif(
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[UploadCommandService, Depends(get_upload_command_service)],
    file: UploadFile = File(...),
    idempotency_key: Annotated[str | None, Header()] = None,
):
    try:
        return await _import_gif_impl(request, user, db, service, file, idempotency_key)
    finally:
        await file.close()


@router.get("/gif/history", response_model=list[ImageOut])
async def gif_history(user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]):
    rows = (
        (
            await db.execute(
                select(Image)
                .where(
                    Image.user_id == user.id,
                    Image.mime == "image/gif",
                    Image.deleted_at.is_(None),
                    Image.artifact_status == ArtifactStatus.READY.value,
                    or_(
                        Image.metadata_jsonb["local_gif_import"]["key"]
                        .as_string()
                        .is_not(None),
                        Image.metadata_jsonb["local_gif_edit"]["key"]
                        .as_string()
                        .is_not(None),
                    ),
                )
                .order_by(Image.created_at.desc(), Image.id.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    visible = []
    for row in rows:
        try:
            await ensure_image_visible_to_user(db, row, user)
        except HTTPException as exc:
            if exc.status_code == 404:
                continue
            raise
        visible.append(await image_out(db, row))
        if len(visible) == 20:
            break
    return visible
