"""Materialize preset and user-owned model library image records."""

from __future__ import annotations

import hashlib
from typing import Any, cast

from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.constants import ImageSource, ImageVisibility
from lumen_core.models import Image, ModelLibraryItem
from lumen_core.schemas import ModelAgeSegment

from .library_runtime import runtime as _runtime


async def _owned_image(db: AsyncSession, *, user_id: str, image_id: str) -> Image:
    runtime = _runtime()
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if img is None:
        raise runtime._http(
            "invalid_image", "image is not owned by current user or was deleted", 400
        )
    return img


def _image_url(image_id: str) -> str:
    return f"/api/images/{image_id}/binary"


def _model_library_download_filename(
    *,
    image_id: str,
    mime: str | None,
    age_segment: str | None,
    gender: str | None,
    appearance_direction: str | None,
    style_tags: list[str],
) -> str:
    runtime = _runtime()
    ext = "png"
    if isinstance(mime, str) and mime.startswith("image/"):
        ext = "jpg" if mime == "image/jpeg" else mime.removeprefix("image/")
    return runtime.model_image_filename(
        image_id=image_id,
        ext=ext,
        age_segment=cast(ModelAgeSegment | None, age_segment),
        gender=gender,
        appearance_direction=appearance_direction,
        style_tags=style_tags,
    )


def _model_library_image_metadata_from_fields(
    *,
    image_id: str,
    age_segment: str | None,
    gender: str | None,
    appearance_direction: str | None,
    style_tags: list[str],
    prompt_hint: str | None = None,
    source: str = "model_library",
    mime: str | None = None,
) -> dict[str, Any]:
    runtime = _runtime()
    payload = runtime.build_model_image_metadata(
        age_segment=age_segment,
        gender=gender,
        appearance_direction=appearance_direction,
        style_tags=style_tags,
        source=source,
        prompt_hint=prompt_hint,
    )
    return {
        "model_library": payload,
        "suggested_filename": runtime._model_library_download_filename(
            image_id=image_id,
            mime=mime,
            age_segment=age_segment,
            gender=gender,
            appearance_direction=appearance_direction,
            style_tags=style_tags,
        ),
    }


async def _create_user_image_from_preset(
    db: AsyncSession,
    *,
    user_id: str,
    item: dict[str, Any],
) -> Image:
    runtime = _runtime()
    item_id = str(item.get("id") or "").strip()
    existing = (
        await db.execute(
            select(Image).where(
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
                Image.metadata_jsonb["apparel_model_library_item_id"].astext == item_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    image_key = str(item.get("image_storage_key") or "").strip()
    path = runtime._storage_path(image_key)
    if not path.is_file():
        raise runtime._http("not_found", "preset image binary is missing", 404)
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    width = 0
    height = 0
    try:
        with PILImage.open(path) as im:
            width, height = im.size
    except Exception:
        runtime.logger.warning(
            "failed to inspect preset image dimensions key=%s",
            image_key,
        )
    image_id = runtime.new_uuid7()
    suffix = path.suffix.lower() or ".webp"
    copy_key = f"u/{user_id}/apparel-model-library/{image_id}{suffix}"
    # 先把字节落盘，再写 DB 行：避免 DB 行存在但二进制 404 的孤儿
    copy_path = runtime._storage_path(copy_key)
    runtime._write_bytes_replace(copy_path, data)
    try:
        img = Image(
            id=image_id,
            user_id=user_id,
            source=ImageSource.UPLOADED.value,
            storage_key=copy_key,
            mime=runtime._guess_mime(path),
            width=width,
            height=height,
            size_bytes=copy_path.stat().st_size,
            sha256=sha,
            blurhash=None,
            visibility=ImageVisibility.PRIVATE.value,
            metadata_jsonb={
                "apparel_model_library_item_id": item_id,
                "apparel_model_library_source": "preset",
                "preset_id": item.get("preset_id"),
                "preset_version": item.get("version"),
                "cached_from_storage_key": image_key,
                "shared_storage": False,
            },
        )
        db.add(img)
        await db.flush()
    except Exception:
        # DB flush 失败时清理刚写的孤儿文件，避免下次重试时 sha 命中残留路径
        copy_path.unlink(missing_ok=True)
        raise
    return img


async def _add_user_library_item(
    db: AsyncSession,
    *,
    user_id: str,
    source: str,
    image_id: str,
    title: str,
    age_segment: str,
    gender: str | None,
    appearance_direction: str | None,
    style_tags: list[str],
) -> dict[str, Any]:
    """Insert one row into ``model_library_items``. Each call is a
    standalone INSERT — concurrent favorites no longer race a shared
    JSON file.
    """
    runtime = _runtime()
    image = await runtime._owned_image(db, user_id=user_id, image_id=image_id)
    normalized_age = runtime._normalize_age_segment(age_segment)
    normalized_gender = runtime._normalize_model_gender(gender)
    cleaned_appearance = runtime._clean_optional_text(
        appearance_direction,
        max_len=80,
    )
    cleaned_tags = runtime._clean_style_tags(style_tags)
    metadata_jsonb = runtime._model_library_image_metadata_from_fields(
        image_id=image_id,
        age_segment=normalized_age,
        gender=normalized_gender,
        appearance_direction=cleaned_appearance,
        style_tags=cleaned_tags,
        prompt_hint=title,
        source=source,
        mime=getattr(image, "mime", None),
    )
    row = ModelLibraryItem(
        id=f"user:{runtime.new_uuid7()}",
        user_id=user_id,
        source=source,
        image_id=image_id,
        title=title.strip()[:120],
        age_segment=normalized_age,
        gender=normalized_gender,
        appearance_direction=cleaned_appearance,
        style_tags=cleaned_tags,
        library_folder=runtime._model_library_folder_for_age(
            normalized_age,
            normalized_gender,
        ),
        metadata_jsonb=metadata_jsonb,
    )
    image_metadata = dict(getattr(image, "metadata_jsonb", None) or {})
    image_metadata.update(metadata_jsonb)
    image.metadata_jsonb = image_metadata
    db.add(row)
    await db.flush()
    return runtime._model_library_row_to_dict(row)
