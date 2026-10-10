"""Creation of reference-video records after durable upload adoption."""

from __future__ import annotations

import secrets
from typing import Any

from lumen_core.model_entities import Video

from ..services.video_preparation import pending_preparation


def new_reference_video(
    *,
    plan: Any,
    user_id: str,
    filename: str,
    mime: str,
    size: int,
    sha256: str,
    deps: Any,
) -> Video:
    video = Video(
        id=plan.video_id,
        user_id=user_id,
        owner_generation_id=None,
        storage_key=plan.storage_key,
        poster_storage_key=None,
        mime=mime,
        width=0,
        height=0,
        duration_ms=0,
        fps=None,
        size_bytes=size,
        sha256=sha256,
        etag=sha256,
        has_audio=False,
        faststart=False,
        visibility="private",
        metadata_jsonb={
            "source": "uploaded_reference",
            "canvas_preparation": pending_preparation(sha256),
            "filename": filename,
            "reference_access_token": secrets.token_urlsafe(32),
            "reference_access_token_expires_at": deps.token_expiry(),
        },
    )
    return video
