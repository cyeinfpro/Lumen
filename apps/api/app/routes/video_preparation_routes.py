"""Authenticated, CSRF-protected reference preparation retry endpoint."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..canvas_services.asset_descriptors import asset_descriptor
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..services.video.errors import video_http_error
from ..services.video_preparation_retry import retry_video_preparation
from ..video_reference_videos import VideoReferenceVideoError

router = APIRouter()


class PreparationRetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_preparation_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=16, max_length=128)


@router.post("/{video_id}/preparation/retry", dependencies=[Depends(verify_csrf)])
async def retry_preparation(
    video_id: str,
    body: PreparationRetryIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    try:
        row = await retry_video_preparation(
            db,
            video_id=video_id,
            user_id=user.id,
            **body.model_dump(),
        )
    except VideoReferenceVideoError as exc:
        raise video_http_error(exc.code, exc.message, exc.status_code) from exc
    return {"asset": asset_descriptor(row)}
