"""Library configuration, ORM migration, queries, and item serialization."""

from __future__ import annotations

import logging
from typing import Any, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.models import Image, ModelLibraryHiddenPreset, ModelLibraryItem, User
from lumen_core.providers import (
    ProviderProxyDefinition,
    parse_proxy_json,
    resolve_provider_proxy_url,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    ApparelModelLibraryItemOut,
    ApparelModelLibrarySyncStateOut,
)

from ..config import settings
from ..runtime_settings import get_setting
from ..workflow_domain.apparel_library import (
    MODEL_LIBRARY_FETCH_TIMEOUT_SECONDS,
    MODEL_LIBRARY_SYNC_MODES,
)
from ..workflow_domain.apparel_library import library_item_url as _library_item_url  # noqa: F401
from ..workflow_domain.apparel_library import (
    model_library_folder_for_age as _model_library_folder_for_age,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    normalize_age_segment as _normalize_age_segment,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    normalize_appearance as _normalize_appearance,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    normalize_model_gender as _normalize_model_gender,
)  # noqa: F401
from .library_contracts import (
    MODEL_LIBRARY_SYNC_PROXY_NAME_KEY,
    MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY,
)
from .library_contracts import (
    model_library_download_filename as _model_library_download_filename,
)  # noqa: F401
from .library_storage import default_sync_state as _default_sync_state  # noqa: F401
from .library_storage import library_sync_state_path as _library_sync_state_path  # noqa: F401
from .library_storage import library_user_index_path as _library_user_index_path  # noqa: F401
from .library_storage import load_global_library_index as _load_global_library_index  # noqa: F401
from .library_storage import load_user_library_index as _load_user_library_index  # noqa: F401
from .library_storage import read_json_file as _read_json_file  # noqa: F401
from .serialization import clean_optional_text as _clean_optional_text  # noqa: F401
from .serialization import clean_style_tags as _clean_style_tags  # noqa: F401
from .serialization import dedupe_nonempty as _dedupe_nonempty  # noqa: F401
from .serialization import http as _http  # noqa: F401
from .serialization import now as _now  # noqa: F401
from .serialization import safe_datetime as _safe_datetime  # noqa: F401

logger = logging.getLogger("app.routes.workflows")


def _github_contents_url() -> str:
    return settings.apparel_model_library_github_contents_url.strip()


def _sync_mode() -> str:
    mode = settings.apparel_model_library_sync_mode.strip().lower()
    return mode if mode in MODEL_LIBRARY_SYNC_MODES else "admin_only"


def _model_library_http_client_kwargs(proxy_url: str | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(MODEL_LIBRARY_FETCH_TIMEOUT_SECONDS),
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


async def _resolve_model_library_sync_proxy(
    db: AsyncSession,
) -> tuple[ProviderProxyDefinition | None, str | None]:
    use_spec = get_spec(MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY)
    use_raw = await get_setting(db, use_spec) if use_spec is not None else None
    if str(use_raw or "0").strip() != "1":
        return None, None

    providers_spec = get_spec("providers")
    raw_providers = (
        await get_setting(db, providers_spec) if providers_spec is not None else None
    )
    proxies, errors = parse_proxy_json(raw_providers)
    for err in errors:
        logger.warning("model library sync proxy config warning: %s", err)

    enabled = [proxy for proxy in proxies if proxy.enabled]
    if not enabled:
        raise _http(
            "proxy_unavailable",
            "model library sync proxy pool is enabled but has no enabled proxies",
            409,
        )

    name_spec = get_spec(MODEL_LIBRARY_SYNC_PROXY_NAME_KEY)
    name_raw = await get_setting(db, name_spec) if name_spec is not None else None
    target_name = str(name_raw or "").strip()
    if target_name:
        proxy = next((p for p in enabled if p.name == target_name), None)
        if proxy is None:
            raise _http(
                "proxy_not_found",
                f"model library sync proxy '{target_name}' not found or disabled",
                409,
            )
    else:
        proxy = enabled[0]

    proxy_url = await resolve_provider_proxy_url(proxy)
    if not proxy_url:
        raise _http(
            "proxy_resolve_failed",
            f"model library sync proxy '{proxy.name}' could not be resolved",
            409,
        )
    return proxy, proxy_url


def _can_sync_library(user: User) -> bool:
    mode = _sync_mode()
    if mode == "disabled":
        return False
    if mode == "any_authenticated":
        return True
    return user.role == "admin"


def _sync_state_out(user: User) -> ApparelModelLibrarySyncStateOut:
    state = _read_json_file(
        _library_sync_state_path(),
        _default_sync_state(),
    )
    return ApparelModelLibrarySyncStateOut(
        last_success_at=_safe_datetime(state.get("last_success_at")),
        last_error=_clean_optional_text(
            state.get("last_error"),
            max_len=1000,
        ),
        can_sync=_can_sync_library(user),
        github_contents_url=_github_contents_url() or None,
    )


def _model_library_item_out(raw: dict[str, Any]) -> ApparelModelLibraryItemOut:
    item_id = str(raw.get("id") or "").strip()
    source = str(raw.get("source") or "").strip()
    if source not in {"preset", "favorite", "user_upload", "generated"}:
        source = "user_upload"
    image_id = _clean_optional_text(raw.get("image_id"), max_len=64)
    image_url = (
        f"/api/images/{image_id}/binary"
        if image_id
        else _library_item_url(item_id, "binary")
    )
    # user item 走 display2048 variant（按需 materialize）；preset 没有独立
    # display 变体，回落到 binary 原图。lightbox / 大图预览走这个。
    display_url = (
        f"/api/images/{image_id}/variants/display2048"
        if image_id
        else _library_item_url(item_id, "binary")
    )
    # 卡片小封面用：user item 复用 display2048（thumb256 variant 不一定生成
    # 且 endpoint 不按需 materialize，回落到 display2048 较稳）；preset 自带
    # 真小 thumb 文件。
    thumb_url = (
        f"/api/images/{image_id}/variants/display2048"
        if image_id
        else _library_item_url(item_id, "thumb")
    )
    created_at = (
        _safe_datetime(raw.get("created_at"))
        or _safe_datetime(raw.get("updated_at"))
        or _now()
    )
    visibility_scope = "global_preset" if source == "preset" else "user_private"
    style_tags = _clean_style_tags(raw.get("style_tags") or raw.get("tags") or [])
    gender = _clean_optional_text(raw.get("gender"), max_len=40)
    age_segment = _normalize_age_segment(raw.get("age_segment"))
    appearance_direction = _clean_optional_text(
        raw.get("appearance_direction"), max_len=80
    )
    metadata_filename = None
    metadata = raw.get("metadata_jsonb")
    if isinstance(metadata, dict):
        metadata_filename = _clean_optional_text(
            metadata.get("suggested_filename"), max_len=160
        )
    if not metadata_filename and image_id:
        image_metadata = raw.get("image_metadata_jsonb")
        if isinstance(image_metadata, dict):
            metadata_filename = _clean_optional_text(
                image_metadata.get("suggested_filename"), max_len=160
            )
    return ApparelModelLibraryItemOut(
        id=item_id,
        source=source,  # type: ignore[arg-type]
        visibility_scope=visibility_scope,  # type: ignore[arg-type]
        title=str(raw.get("title") or "未命名模特").strip()[:120],
        age_segment=age_segment,  # type: ignore[arg-type]
        gender=gender,
        appearance_direction=appearance_direction,
        style_tags=style_tags,
        image_url=image_url,
        display_url=display_url,
        thumb_url=thumb_url,
        image_id=image_id,
        preset_id=_clean_optional_text(raw.get("preset_id"), max_len=160),
        version=raw.get("version") if isinstance(raw.get("version"), int) else None,
        library_folder=_clean_optional_text(
            raw.get("library_folder")
            or _model_library_folder_for_age(
                raw.get("age_segment"),
                raw.get("gender"),
            ),
            max_len=40,
        ),
        prompt_hint=_clean_optional_text(
            raw.get("prompt_hint"),
            max_len=300,
        ),
        download_filename=metadata_filename
        or _model_library_download_filename(
            image_id=image_id or item_id,
            mime=None,
            age_segment=age_segment,
            gender=gender,
            appearance_direction=appearance_direction,
            style_tags=style_tags,
        ),
        created_at=created_at,
        updated_at=_safe_datetime(raw.get("updated_at")),
    )


def _model_library_row_to_dict(row: ModelLibraryItem) -> dict[str, Any]:
    """Adapter so DB rows feed ``_model_library_item_out`` unchanged."""
    return {
        "id": row.id,
        "source": row.source,
        "image_id": row.image_id,
        "title": row.title,
        "age_segment": row.age_segment,
        "gender": row.gender,
        "appearance_direction": row.appearance_direction,
        "style_tags": list(row.style_tags or []),
        "library_folder": row.library_folder,
        "prompt_hint": row.prompt_hint,
        "auto_tagged_at": row.auto_tagged_at.isoformat()
        if row.auto_tagged_at
        else None,
        "auto_tag_notes": row.auto_tag_notes,
        "metadata_jsonb": dict(row.metadata_jsonb or {}),
        "owner_user_id": row.user_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _legacy_library_item_insert_values(
    *,
    user_id: str,
    raw: dict[str, Any],
    valid_image_ids: set[str],
) -> dict[str, Any] | None:
    item_id = str(raw.get("id") or "").strip()
    image_id = str(raw.get("image_id") or "").strip()
    if not item_id or not image_id or image_id not in valid_image_ids:
        return None
    source = str(raw.get("source") or "user_upload").strip()
    if source not in {"favorite", "user_upload", "generated"}:
        source = "user_upload"
    normalized_age = _normalize_age_segment(raw.get("age_segment"))
    normalized_gender = _normalize_model_gender(raw.get("gender"))
    created_at = (
        _safe_datetime(
            raw.get("created_at") if isinstance(raw.get("created_at"), str) else None
        )
        or _now()
    )
    updated_at = (
        _safe_datetime(
            raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None
        )
        or created_at
    )
    known_keys = {
        "id",
        "user_id",
        "owner_user_id",
        "source",
        "image_id",
        "title",
        "age_segment",
        "gender",
        "appearance_direction",
        "style_tags",
        "tags",
        "library_folder",
        "prompt_hint",
        "auto_tagged_at",
        "auto_tag_notes",
        "created_at",
        "updated_at",
    }
    return {
        "id": item_id,
        "user_id": user_id,
        "source": source,
        "image_id": image_id,
        "title": str(raw.get("title") or "").strip()[:120],
        "age_segment": normalized_age,
        "gender": normalized_gender,
        "appearance_direction": _clean_optional_text(
            raw.get("appearance_direction"), max_len=80
        ),
        "style_tags": _clean_style_tags(raw.get("style_tags") or raw.get("tags") or []),
        "library_folder": _clean_optional_text(
            raw.get("library_folder")
            or _model_library_folder_for_age(
                normalized_age,
                normalized_gender,
            ),
            max_len=64,
        ),
        "prompt_hint": _clean_optional_text(
            raw.get("prompt_hint"),
            max_len=1000,
        ),
        "auto_tagged_at": _safe_datetime(
            raw.get("auto_tagged_at")
            if isinstance(raw.get("auto_tagged_at"), str)
            else None
        ),
        "auto_tag_notes": _clean_optional_text(
            raw.get("auto_tag_notes"),
            max_len=200,
        ),
        "metadata_jsonb": {k: v for k, v in raw.items() if k not in known_keys},
        "created_at": created_at,
        "updated_at": updated_at,
    }


async def _ensure_legacy_user_library_migrated(db: AsyncSession, user_id: str) -> bool:
    """Lazily backfill one user's legacy JSON index into PostgreSQL.

    The schema migration creates empty tables; deployments may not run the
    one-off script immediately. This guard keeps old saved models visible and
    functional by migrating valid rows on first access. It flushes, but leaves
    commit ownership to the route that called it.
    """
    index_path = _library_user_index_path(user_id)
    if not index_path.is_file():
        return False
    index = _load_user_library_index(user_id)
    raw_items = [item for item in (index.get("items") or []) if isinstance(item, dict)]
    raw_hidden_ids = _dedupe_nonempty(index.get("hidden_preset_ids") or [])
    if not raw_items and not raw_hidden_ids:
        return False

    migrated = False
    item_ids = _dedupe_nonempty(str(item.get("id") or "") for item in raw_items)
    existing_item_ids: set[str] = set()
    if item_ids:
        rows = await db.execute(
            select(ModelLibraryItem.id).where(ModelLibraryItem.id.in_(item_ids))
        )
        existing_item_ids = set(rows.scalars().all())

    image_ids = _dedupe_nonempty(str(item.get("image_id") or "") for item in raw_items)
    valid_image_ids: set[str] = set()
    if image_ids:
        rows = await db.execute(
            select(Image.id).where(
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
                Image.id.in_(image_ids),
            )
        )
        valid_image_ids = set(rows.scalars().all())

    item_values = [
        values
        for raw in raw_items
        if str(raw.get("id") or "").strip() not in existing_item_ids
        if (
            values := _legacy_library_item_insert_values(
                user_id=user_id,
                raw=raw,
                valid_image_ids=valid_image_ids,
            )
        )
        is not None
    ]
    if item_values:
        await db.execute(
            pg_insert(ModelLibraryItem)
            .values(item_values)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        migrated = True

    if raw_hidden_ids:
        rows = await db.execute(
            select(ModelLibraryHiddenPreset.preset_id).where(
                ModelLibraryHiddenPreset.user_id == user_id,
                ModelLibraryHiddenPreset.preset_id.in_(raw_hidden_ids),
            )
        )
        existing_hidden = set(rows.scalars().all())
        hidden_values = [
            {"user_id": user_id, "preset_id": preset_id}
            for preset_id in raw_hidden_ids
            if preset_id not in existing_hidden
        ]
        if hidden_values:
            await db.execute(
                pg_insert(ModelLibraryHiddenPreset)
                .values(hidden_values)
                .on_conflict_do_nothing(index_elements=["user_id", "preset_id"])
            )
            migrated = True

    if migrated:
        await db.flush()
    return migrated


async def _load_user_library_items(
    db: AsyncSession, user_id: str
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(ModelLibraryItem, Image.metadata_jsonb)
            .join(Image, Image.id == ModelLibraryItem.image_id)
            .where(
                ModelLibraryItem.user_id == user_id,
                Image.deleted_at.is_(None),
            )
            .order_by(ModelLibraryItem.created_at.desc())
        )
    ).all()
    out: list[dict[str, Any]] = []
    for row, image_metadata_jsonb in rows:
        raw = _model_library_row_to_dict(row)
        raw["image_metadata_jsonb"] = (
            dict(image_metadata_jsonb) if isinstance(image_metadata_jsonb, dict) else {}
        )
        out.append(raw)
    return out


async def _load_user_hidden_preset_ids(db: AsyncSession, user_id: str) -> set[str]:
    rows = (
        (
            await db.execute(
                select(ModelLibraryHiddenPreset.preset_id).where(
                    ModelLibraryHiddenPreset.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {pid for pid in rows if isinstance(pid, str)}


async def _combined_library_items(
    db: AsyncSession, user_id: str
) -> tuple[list[dict[str, Any]], bool]:
    migrated = await _ensure_legacy_user_library_migrated(db, user_id)
    global_index = _load_global_library_index()
    hidden = await _load_user_hidden_preset_ids(db, user_id)
    preset_items = [
        dict(item)
        for item in global_index.get("preset_items", [])
        if isinstance(item, dict) and str(item.get("id") or "") not in hidden
    ]
    user_items = await _load_user_library_items(db, user_id)
    return [*preset_items, *user_items], migrated


def _filter_library_items(
    items: Iterable[dict[str, Any]],
    *,
    source: str,
    age_segment: str,
    appearance: str,
    q: str,
) -> list[dict[str, Any]]:
    query = q.strip().lower()
    filtered: list[dict[str, Any]] = []
    for item in items:
        item_source = str(item.get("source") or "")
        if source != "all" and item_source != source:
            continue
        item_age = _normalize_age_segment(item.get("age_segment"))
        if age_segment != "all" and item_age != age_segment:
            continue
        if appearance != "all":
            item_appearance = _normalize_appearance(item.get("appearance_direction"))
            if item_appearance != appearance:
                continue
        if query:
            haystack = " ".join(
                [
                    str(item.get("title") or ""),
                    str(item.get("gender") or ""),
                    str(item.get("appearance_direction") or ""),
                    " ".join(
                        _clean_style_tags(
                            item.get("style_tags") or item.get("tags") or []
                        )
                    ),
                ]
            ).lower()
            if query not in haystack:
                continue
        filtered.append(item)
    source_rank = {"preset": 0, "favorite": 1, "user_upload": 2, "generated": 3}
    return sorted(
        filtered,
        key=lambda item: (
            source_rank.get(str(item.get("source") or ""), 9),
            _normalize_age_segment(item.get("age_segment")),
            str(item.get("title") or ""),
            str(item.get("id") or ""),
        ),
    )


async def _find_library_item(
    db: AsyncSession, *, user_id: str, item_id: str
) -> dict[str, Any] | None:
    """Resolve a library item by id.

    Presets are intentionally global, with user-level hide rows acting as
    per-user deletes. User-owned rows must still point at a live image owned by
    the same user; stale or tampered library rows are not usable.
    """
    await _ensure_legacy_user_library_migrated(db, user_id)
    if item_id.startswith("preset:") or not item_id.startswith("user:"):
        for item in (
            _load_global_library_index().get(
                "preset_items",
                [],
            )
            or []
        ):
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "") != item_id:
                continue
            hidden = await _load_user_hidden_preset_ids(db, user_id)
            if item_id in hidden:
                return None
            return dict(item)
    if item_id.startswith("user:"):
        row = (
            await db.execute(
                select(ModelLibraryItem)
                .join(Image, Image.id == ModelLibraryItem.image_id)
                .where(
                    ModelLibraryItem.id == item_id,
                    ModelLibraryItem.user_id == user_id,
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return _model_library_row_to_dict(row)
    return None


# Public workflow contracts.
can_sync_library = _can_sync_library
combined_library_items = _combined_library_items
ensure_legacy_user_library_migrated = _ensure_legacy_user_library_migrated
filter_library_items = _filter_library_items
find_library_item = _find_library_item
github_contents_url = _github_contents_url
legacy_library_item_insert_values = _legacy_library_item_insert_values
load_user_hidden_preset_ids = _load_user_hidden_preset_ids
load_user_library_items = _load_user_library_items
model_library_http_client_kwargs = _model_library_http_client_kwargs
model_library_item_out = _model_library_item_out
model_library_row_to_dict = _model_library_row_to_dict
resolve_model_library_sync_proxy = _resolve_model_library_sync_proxy
sync_mode = _sync_mode
sync_state_out = _sync_state_out
