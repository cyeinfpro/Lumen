"""Poster-style resource, CRUD, and binary-serving services.

Every function accepts a runtime facade supplied by the route module.  This
keeps legacy monkeypatch points working while preventing the service layer
from importing ``app.routes.poster_styles``.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Iterable

from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    POSTER_STYLE_CATEGORIES,
    POSTER_STYLE_MAX_BINARY_BYTES,
    POSTER_STYLE_MAX_SAMPLES,
    POSTER_STYLE_SOURCES,
)
from lumen_core.models import (
    Image,
    PosterStyleHiddenPreset,
    PosterStyleItem,
    new_uuid7,
)
from lumen_core.schemas import (
    PosterStyleBatchDeleteIn,
    PosterStyleBatchDeleteOut,
    PosterStyleCreateIn,
    PosterStyleItemOut,
    PosterStyleListOut,
    PosterStylePatchIn,
    PosterStyleSyncStateOut,
)

from . import storage as poster_style_storage
from .serialization import (
    dedupe_nonempty,
    filter_preset_items,
    item_out_from_preset,
    item_out_from_row,
    safe_datetime,
)


def storage_root(runtime: Any) -> Path:
    return poster_style_storage.resolve_storage_root(runtime.settings.storage_root)


def storage_path(runtime: Any, storage_key: str) -> Path:
    return poster_style_storage.resolve_storage_path(
        storage_key,
        root=storage_root(runtime),
    )


def fsync_dir(path: Path) -> None:
    poster_style_storage.fsync_dir(path)


def write_bytes_replace(path: Path, data: bytes) -> None:
    poster_style_storage.write_bytes_replace(path, data)


def write_json_atomic(
    runtime: Any,
    path: Path,
    data: dict[str, Any],
) -> None:
    poster_style_storage.write_json_atomic(
        path,
        data,
        max_bytes=int(
            getattr(runtime, "POSTER_STYLE_MAX_INDEX_BYTES", 32 * 1024 * 1024)
        ),
    )


def read_json_file(
    runtime: Any,
    path: Path,
    default: dict[str, Any],
) -> dict[str, Any]:
    return poster_style_storage.read_json_file(
        path,
        default,
        max_bytes=int(
            getattr(runtime, "POSTER_STYLE_MAX_INDEX_BYTES", 32 * 1024 * 1024)
        ),
    )


def guess_mime(path: Path) -> str:
    return poster_style_storage.guess_mime(path)


def preset_storage_root(runtime: Any) -> Path:
    return storage_path(runtime, runtime.POSTER_STYLE_ROOT_KEY)


def global_preset_index_path(runtime: Any) -> Path:
    return preset_storage_root(runtime) / "index.json"


def library_sync_state_path(runtime: Any) -> Path:
    return preset_storage_root(runtime) / "sync-state.json"


def library_sync_lock_path(runtime: Any) -> Path:
    return preset_storage_root(runtime) / ".sync-state.lock"


def default_global_index(runtime: Any) -> dict[str, Any]:
    return {
        "schema_version": runtime.POSTER_STYLE_SCHEMA_VERSION,
        "updated_at": None,
        "preset_items": [],
    }


def default_sync_state(runtime: Any) -> dict[str, Any]:
    return {
        "schema_version": runtime.POSTER_STYLE_SCHEMA_VERSION,
        "last_success_at": None,
        "last_error": None,
        "last_attempt_at": None,
        "last_result": None,
        "sync_lease": None,
    }


def load_global_preset_index(runtime: Any) -> dict[str, Any]:
    index = read_json_file(
        runtime,
        runtime._global_preset_index_path(),
        default_global_index(runtime),
    )
    items = index.get("preset_items")
    max_items = int(getattr(runtime, "POSTER_STYLE_MAX_PRESET_ITEMS", 4096))
    if not isinstance(items, list) or len(items) > max_items:
        raise runtime._http(
            "invalid_index",
            f"invalid poster style index: {runtime._global_preset_index_path().name}",
            500,
        )
    return index


def save_global_preset_index(runtime: Any, index: dict[str, Any]) -> None:
    items = index.get("preset_items")
    max_items = int(getattr(runtime, "POSTER_STYLE_MAX_PRESET_ITEMS", 4096))
    if not isinstance(items, list) or len(items) > max_items:
        raise ValueError("poster style preset item limit exceeded")
    index["schema_version"] = runtime.POSTER_STYLE_SCHEMA_VERSION
    index["updated_at"] = runtime._iso_now()
    write_json_atomic(runtime, runtime._global_preset_index_path(), index)


def save_sync_state(runtime: Any, state: dict[str, Any]) -> None:
    state["schema_version"] = runtime.POSTER_STYLE_SCHEMA_VERSION
    write_json_atomic(runtime, runtime._library_sync_state_path(), state)


async def sync_mode(runtime: Any, db: AsyncSession) -> str:
    spec = runtime.get_spec(runtime.POSTER_STYLE_SYNC_MODE_KEY)
    raw = await runtime.get_setting(db, spec) if spec is not None else None
    mode = str(raw or runtime._DEFAULT_SYNC_MODE).strip().lower()
    if mode not in {"admin_only", "any_authenticated", "disabled"}:
        return runtime._DEFAULT_SYNC_MODE
    return mode


async def can_sync_library(runtime: Any, db: AsyncSession, user: Any) -> bool:
    mode = await runtime._sync_mode(db)
    if mode == "disabled":
        return False
    if mode == "any_authenticated":
        return True
    return getattr(user, "role", "") == "admin"


async def resolve_sync_proxy(
    runtime: Any,
    db: AsyncSession,
) -> tuple[Any | None, str | None]:
    use_spec = runtime.get_spec(runtime.POSTER_STYLE_SYNC_USE_PROXY_POOL_KEY)
    use_raw = await runtime.get_setting(db, use_spec) if use_spec is not None else None
    if str(use_raw or "0").strip() != "1":
        return None, None

    providers_spec = runtime.get_spec("providers")
    raw_providers = (
        await runtime.get_setting(db, providers_spec)
        if providers_spec is not None
        else None
    )
    proxies, errors = runtime.parse_proxy_json(raw_providers)
    for error in errors:
        runtime.logger.warning(
            "poster style sync proxy config warning: %s",
            error,
        )
    enabled = [proxy for proxy in proxies if proxy.enabled]
    if not enabled:
        raise runtime._http(
            "proxy_unavailable",
            "poster style sync proxy pool is enabled but has no enabled proxies",
            409,
        )

    name_spec = runtime.get_spec(runtime.POSTER_STYLE_SYNC_PROXY_NAME_KEY)
    name_raw = (
        await runtime.get_setting(db, name_spec) if name_spec is not None else None
    )
    target_name = str(name_raw or "").strip()
    if target_name:
        proxy = next((item for item in enabled if item.name == target_name), None)
        if proxy is None:
            raise runtime._http(
                "proxy_not_found",
                f"poster style sync proxy '{target_name}' not found or disabled",
                409,
            )
    else:
        proxy = enabled[0]
    proxy_url = await runtime.resolve_provider_proxy_url(proxy)
    if not proxy_url:
        raise runtime._http(
            "proxy_resolve_failed",
            f"poster style sync proxy '{proxy.name}' could not be resolved",
            409,
        )
    return proxy, proxy_url


def http_client_kwargs(runtime: Any, proxy_url: str | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "timeout": runtime.httpx.Timeout(runtime.POSTER_STYLE_FETCH_TIMEOUT_S),
        "follow_redirects": False,
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


async def sync_state_out(
    runtime: Any,
    db: AsyncSession,
    user: Any,
) -> PosterStyleSyncStateOut:
    state = await asyncio.to_thread(
        runtime._read_json_file,
        runtime._library_sync_state_path(),
        runtime._default_sync_state(),
    )
    return PosterStyleSyncStateOut(
        last_success_at=safe_datetime(state.get("last_success_at")),
        last_error=runtime._clean_optional_text(
            state.get("last_error"),
            max_len=1000,
        ),
        can_sync=await runtime._can_sync_library(db, user),
        github_contents_url=runtime._github_contents_url() or None,
    )


async def load_user_hidden_preset_ids(
    runtime: Any,
    db: AsyncSession,
    user_id: str,
) -> set[str]:
    rows = (
        (
            await db.execute(
                select(PosterStyleHiddenPreset.preset_id).where(
                    PosterStyleHiddenPreset.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {value for value in rows if isinstance(value, str)}


async def load_user_items(
    runtime: Any,
    db: AsyncSession,
    *,
    user_id: str,
    category: str,
    q: str,
    tags: list[str],
) -> list[PosterStyleItem]:
    statement = select(PosterStyleItem).where(PosterStyleItem.user_id == user_id)
    if category != "all":
        statement = statement.where(PosterStyleItem.category == category)
    rows = list(
        (await db.execute(statement.order_by(desc(PosterStyleItem.created_at))))
        .scalars()
        .all()
    )
    if q.strip():
        query = q.strip().lower()
        rows = [
            row
            for row in rows
            if query
            in (
                f"{row.title or ''} {row.mood or ''} "
                f"{row.prompt_template or ''} "
                f"{' '.join(row.style_tags or [])}".lower()
            )
        ]
    if tags:
        tag_set = {tag.strip().lower() for tag in tags if tag and tag.strip()}
        rows = [
            row
            for row in rows
            if tag_set.intersection(
                {str(tag).strip().lower() for tag in (row.style_tags or [])}
            )
        ]
    return rows


async def find_user_item(
    runtime: Any,
    db: AsyncSession,
    *,
    user_id: str,
    item_id: str,
) -> PosterStyleItem | None:
    if not item_id.startswith("user:"):
        return None
    return (
        await db.execute(
            select(PosterStyleItem).where(
                PosterStyleItem.id == item_id,
                PosterStyleItem.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def find_preset_item(
    runtime: Any,
    db: AsyncSession,
    *,
    user_id: str,
    item_id: str,
) -> dict[str, Any] | None:
    if not item_id.startswith("preset:"):
        return None
    hidden = await runtime._load_user_hidden_preset_ids(db, user_id)
    if item_id in hidden:
        return None
    index = await asyncio.to_thread(runtime._load_global_preset_index)
    for item in index.get("preset_items") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == item_id:
            return dict(item)
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def open_storage_file(
    runtime: Any,
    storage_key: str,
) -> tuple[Path, str, int]:
    path = runtime._storage_path(storage_key)
    if not path.is_file():
        raise runtime._http("not_found", "library binary missing", 404)
    size = path.stat().st_size
    max_bytes = int(
        getattr(runtime, "POSTER_STYLE_MAX_BINARY_BYTES", POSTER_STYLE_MAX_BINARY_BYTES)
    )
    if size > max_bytes:
        raise runtime._http(
            "library_binary_too_large",
            f"library binary exceeds {max_bytes} bytes",
            413,
        )
    return path, runtime._guess_mime(path), size


def stream_file(path: Path, max_bytes: int) -> Iterable[bytes]:
    remaining = max(0, max_bytes)
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


async def binary_response(
    runtime: Any,
    storage_key: str,
    request: Request,
) -> Response:
    path, media_type, size = await asyncio.to_thread(
        runtime._open_storage_file,
        storage_key,
    )
    sha = await asyncio.to_thread(runtime._sha256_file, path)
    etag = f'"{sha}"'
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, max-age=86400"},
        )
    return StreamingResponse(
        runtime._stream_file(path, size),
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "ETag": etag,
            "Content-Length": str(size),
        },
    )


async def validate_owned_image_ids(
    runtime: Any,
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: list[str],
) -> list[str]:
    cleaned = dedupe_nonempty(image_ids)
    if not cleaned:
        return []
    rows = (
        (
            await db.execute(
                select(Image.id).where(
                    Image.id.in_(cleaned),
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    owned = {image_id for image_id in rows if isinstance(image_id, str)}
    missing = [image_id for image_id in cleaned if image_id not in owned]
    if missing:
        raise runtime._http(
            "invalid_image",
            "one or more images are not owned by the current user or were deleted",
            400,
            missing=missing,
        )
    return cleaned


async def list_poster_styles(
    runtime: Any,
    *,
    user: Any,
    db: AsyncSession,
    category: str,
    source: str,
    q: str,
    tags: list[str],
    limit: int,
    offset: int,
) -> PosterStyleListOut:
    category = category.strip() or "all"
    if category not in POSTER_STYLE_CATEGORIES:
        raise runtime._http("invalid_category", "invalid poster style category", 422)
    source = source.strip() or "all"
    if source not in POSTER_STYLE_SOURCES:
        raise runtime._http("invalid_source", "invalid poster style source", 422)

    await runtime._bootstrap_local_presets_if_empty()
    items_out: list[PosterStyleItemOut] = []
    preset_total = 0
    if source in {"all", "preset"}:
        index = await asyncio.to_thread(runtime._load_global_preset_index)
        hidden = await runtime._load_user_hidden_preset_ids(db, user.id)
        preset_items = [
            item
            for item in index.get("preset_items") or []
            if isinstance(item, dict) and str(item.get("id") or "") not in hidden
        ]
        filtered = filter_preset_items(
            runtime,
            preset_items,
            category=category,
            q=q,
            tags=tags,
        )
        preset_total = len(filtered)
        items_out.extend(item_out_from_preset(runtime, item) for item in filtered)

    user_total = 0
    if source in {"all", "favorite", "user_upload", "generated"}:
        user_items = await runtime._load_user_items(
            db,
            user_id=user.id,
            category=category if category != "user_favorites" else "user_favorites",
            q=q,
            tags=tags,
        )
        if source != "all":
            user_items = [row for row in user_items if row.source == source]
        user_total = len(user_items)
        items_out.extend(item_out_from_row(runtime, row) for row in user_items)

    total = preset_total + user_total
    return PosterStyleListOut(
        items=items_out[offset : offset + limit],
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
        sync=await runtime._sync_state_out(db, user),
    )


def preset_cover_storage_key(preset: dict[str, Any]) -> str:
    samples = preset.get("samples") or []
    if isinstance(samples, list) and samples and isinstance(samples[0], dict):
        return str(samples[0].get("image_storage_key") or "")
    return ""


def preset_thumb_for_cover(preset: dict[str, Any]) -> str:
    samples = preset.get("samples") or []
    if isinstance(samples, list) and samples and isinstance(samples[0], dict):
        return str(
            samples[0].get("thumb_storage_key")
            or samples[0].get("image_storage_key")
            or ""
        )
    return ""


async def get_preset_binary(
    runtime: Any,
    *,
    item_id: str,
    request: Request,
    user: Any,
    db: AsyncSession,
    thumbnail: bool = False,
) -> Response:
    if item_id.startswith("user:"):
        raise runtime._http(
            "use_image_api",
            "user library image is served by image API",
            400,
        )
    raw = await runtime._find_preset_item(db, user_id=user.id, item_id=item_id)
    if raw is None:
        raise runtime._http("not_found", "poster style item not found", 404)
    storage_key = (
        preset_thumb_for_cover(raw) if thumbnail else preset_cover_storage_key(raw)
    )
    if thumbnail:
        storage_key = storage_key or preset_cover_storage_key(raw)
    if not storage_key:
        raise runtime._http(
            "no_cover",
            "preset has no synced sample image yet",
            404,
        )
    return await runtime._binary_response(storage_key, request)


async def get_preset_sample(
    runtime: Any,
    *,
    item_id: str,
    sample_index: int,
    request: Request,
    user: Any,
    db: AsyncSession,
) -> Response:
    if item_id.startswith("user:"):
        raise runtime._http(
            "use_image_api",
            "user library samples are served by image API",
            400,
        )
    raw = await runtime._find_preset_item(db, user_id=user.id, item_id=item_id)
    if raw is None:
        raise runtime._http("not_found", "poster style item not found", 404)
    samples = raw.get("samples") or []
    max_samples = int(
        getattr(runtime, "POSTER_STYLE_MAX_SAMPLES", POSTER_STYLE_MAX_SAMPLES)
    )
    if (
        not isinstance(samples, list)
        or sample_index < 0
        or sample_index >= max_samples
        or sample_index >= len(samples)
    ):
        raise runtime._http("invalid_sample", "sample index out of range", 404)
    sample = samples[sample_index]
    if not isinstance(sample, dict):
        raise runtime._http("invalid_sample", "sample entry invalid", 500)
    storage_key = str(sample.get("image_storage_key") or "")
    if not storage_key:
        raise runtime._http(
            "no_sample",
            "preset sample has no synced binary yet",
            404,
        )
    return await runtime._binary_response(storage_key, request)


async def create_item(
    runtime: Any,
    *,
    body: PosterStyleCreateIn,
    user: Any,
    db: AsyncSession,
    background_tasks: Any,
) -> PosterStyleItemOut:
    extra_samples = [
        image_id
        for image_id in body.sample_image_ids
        if image_id != body.cover_image_id
    ]
    await runtime._validate_owned_image_ids(
        db,
        user_id=user.id,
        image_ids=[body.cover_image_id, *extra_samples],
    )
    category = runtime._normalize_category(body.category)
    row = PosterStyleItem(
        id=f"user:{new_uuid7()}",
        user_id=user.id,
        source=body.source,
        cover_image_id=body.cover_image_id,
        sample_image_ids=extra_samples,
        title=body.title.strip()[:120],
        category=category,
        mood=runtime._clean_optional_text(body.mood, max_len=120),
        prompt_template=runtime._clean_optional_text(
            body.prompt_template,
            max_len=2000,
        ),
        palette=runtime._normalize_palette(body.palette),
        recommended_aspects=runtime._normalize_recommended_aspects(
            body.recommended_aspects
        ),
        style_tags=runtime._normalize_style_tags(body.style_tags),
        library_folder=runtime._poster_style_folder_for_category(category),
        metadata_jsonb={},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    if body.auto_tag:
        background_tasks.add_task(runtime._run_auto_tag_in_background, user.id, row.id)
    return runtime._item_out_from_row(row)


async def patch_item(
    runtime: Any,
    *,
    item_id: str,
    body: PosterStylePatchIn,
    user: Any,
    db: AsyncSession,
) -> PosterStyleItemOut:
    if not item_id.startswith("user:"):
        raise runtime._http(
            "preset_readonly",
            "preset items are read-only; delete to hide",
            400,
        )
    row = await runtime._find_user_item(db, user_id=user.id, item_id=item_id)
    if row is None:
        raise runtime._http("not_found", "poster style item not found", 404)
    if body.title is not None:
        row.title = body.title.strip()[:120]
    if body.category is not None:
        row.category = runtime._normalize_category(body.category)
        row.library_folder = runtime._poster_style_folder_for_category(row.category)
    if body.mood is not None:
        row.mood = runtime._clean_optional_text(body.mood, max_len=120)
    if body.prompt_template is not None:
        row.prompt_template = runtime._clean_optional_text(
            body.prompt_template,
            max_len=2000,
        )
    if body.palette is not None:
        row.palette = runtime._normalize_palette(body.palette)
    if body.recommended_aspects is not None:
        row.recommended_aspects = runtime._normalize_recommended_aspects(
            body.recommended_aspects
        )
    if body.style_tags is not None:
        row.style_tags = runtime._normalize_style_tags(body.style_tags)
    await db.commit()
    await db.refresh(row)
    return runtime._item_out_from_row(row)


async def delete_item_for_user(
    runtime: Any,
    db: AsyncSession,
    *,
    user_id: str,
    item_id: str,
) -> bool:
    if item_id.startswith("user:"):
        row = await runtime._find_user_item(db, user_id=user_id, item_id=item_id)
        if row is None:
            return False
        await db.delete(row)
        return True
    raw = await runtime._find_preset_item(db, user_id=user_id, item_id=item_id)
    if raw is None or raw.get("source") != "preset":
        return False
    existing = (
        await db.execute(
            select(PosterStyleHiddenPreset).where(
                PosterStyleHiddenPreset.user_id == user_id,
                PosterStyleHiddenPreset.preset_id == item_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(PosterStyleHiddenPreset(user_id=user_id, preset_id=item_id))
    return True


async def delete_item(
    runtime: Any,
    *,
    item_id: str,
    user: Any,
    db: AsyncSession,
) -> dict[str, bool]:
    deleted = await runtime._delete_poster_style_item_for_user(
        db,
        user_id=user.id,
        item_id=item_id,
    )
    if not deleted:
        raise runtime._http("not_found", "poster style item not found", 404)
    await db.commit()
    return {"ok": True}


async def batch_delete_items(
    runtime: Any,
    *,
    body: PosterStyleBatchDeleteIn,
    user: Any,
    db: AsyncSession,
) -> PosterStyleBatchDeleteOut:
    item_ids = dedupe_nonempty(body.item_ids)
    deleted = 0
    not_found: list[str] = []
    for item_id in item_ids:
        if await runtime._delete_poster_style_item_for_user(
            db,
            user_id=user.id,
            item_id=item_id,
        ):
            deleted += 1
        else:
            not_found.append(item_id)
    await db.commit()
    return PosterStyleBatchDeleteOut(deleted=deleted, not_found=not_found)
