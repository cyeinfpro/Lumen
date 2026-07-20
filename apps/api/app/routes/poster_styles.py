# ruff: noqa: F401
"""Poster-style library HTTP facade.

The route module intentionally keeps the historical private names used by
workflow code and tests.  Domain work lives in
``app.services.poster_styles``; the facade supplies this module as a runtime
object so legacy monkeypatches remain effective without creating service to
route imports.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Iterable

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    POSTER_STYLE_CATEGORIES,
    POSTER_STYLE_IMAGE_SUFFIXES,
    POSTER_STYLE_MAX_BINARY_BYTES,
    POSTER_STYLE_MAX_SAMPLES,
    POSTER_STYLE_SOURCES,
)
from lumen_core.models import (
    Generation,
    Image,
    PosterStyleItem,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.providers import parse_proxy_json, resolve_provider_proxy_url
from lumen_core.schemas import (
    ChatParamsIn,
    ImageParamsIn,
    PosterStyleAutoTagOut,
    PosterStyleBatchDeleteIn,
    PosterStyleBatchDeleteOut,
    PosterStyleCreateIn,
    PosterStyleGenerateIn,
    PosterStyleGenerateOut,
    PosterStyleItemOut,
    PosterStyleJobOut,
    PosterStyleJobsOut,
    PosterStyleListOut,
    PosterStylePatchIn,
    PosterStyleSyncOut,
    PosterStyleSyncStateOut,
)

from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..runtime_settings import get_setting
from ..services.poster_styles import generation as poster_style_generation
from ..services.poster_styles import resources as poster_style_resources
from ..services.poster_styles import serialization as poster_style_serialization
from ..services.poster_styles import sync as poster_style_sync
from ..services.poster_styles import tagging as poster_style_tagging
from ..services.poster_styles import storage as poster_style_storage
from ..workflow_services.library_sync_operation import (
    _do_poster_style_sync,
)
from ._poster_library import (
    POSTER_STYLE_FETCH_TIMEOUT_S,
    POSTER_STYLE_GENERATE_STEP_KEY,
    POSTER_STYLE_GENERATE_WORKER_ACTION,
    POSTER_STYLE_MAX_GITHUB_DEPTH,
    POSTER_STYLE_MAX_GITHUB_DIRECTORIES,
    POSTER_STYLE_MAX_GITHUB_FILES,
    POSTER_STYLE_MAX_GITHUB_METADATA_BYTES,
    POSTER_STYLE_MAX_GITHUB_RESPONSE_BYTES,
    POSTER_STYLE_MAX_INDEX_BYTES,
    POSTER_STYLE_MAX_META_BYTES,
    POSTER_STYLE_MAX_PRESET_ITEMS,
    POSTER_STYLE_MAX_REDIRECTS,
    POSTER_STYLE_MAX_SYNC_DOWNLOAD_BYTES,
    POSTER_STYLE_ROOT_KEY,
    POSTER_STYLE_SCHEMA_VERSION,
    POSTER_STYLE_SYNC_COOLDOWN_S,
    POSTER_STYLE_SYNC_FAILURE_COOLDOWN_S,
    POSTER_STYLE_SYNC_LEASE_RENEW_SECONDS,
    POSTER_STYLE_SYNC_LEASE_SECONDS,
    POSTER_STYLE_SYNC_MODE_KEY,
    POSTER_STYLE_SYNC_PROXY_NAME_KEY,
    POSTER_STYLE_SYNC_USE_PROXY_POOL_KEY,
    WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
    _DEFAULT_GITHUB_CONTENTS_URL,
    _DEFAULT_SYNC_MODE,
    _SYNC_LOCK,
    _category_from_folder_name,
    _clean_optional_text,
    _github_contents_url,
    _library_item_url,
    _library_sample_url,
    _metadata_from_meta_json,
    _normalize_category,
    _normalize_palette,
    _normalize_recommended_aspects,
    _normalize_style_tags,
    _poster_style_folder_for_category,
    _poster_style_sync_file_lock,
    _preset_item_id,
    _preset_storage_key,
    _preset_thumb_storage_key,
    _scan_local_presets,
)

router = APIRouter(prefix="/poster-styles", tags=["poster-styles"])
logger = logging.getLogger(__name__)

_GITHUB_API_HOST = "api.github.com"
_GITHUB_RAW_HOSTS = frozenset(
    {
        "raw.githubusercontent.com",
        "media.githubusercontent.com",
    }
)
_HTTP_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_POSTER_STYLE_AUTO_TAG_CONCURRENCY = poster_style_tagging.auto_tag_concurrency()
_POSTER_STYLE_AUTO_TAG_SEMAPHORE: asyncio.Semaphore | None = None
_POSTER_STYLE_AUTO_TAG_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None


def _runtime() -> Any:
    return sys.modules[__name__]


def get_redis() -> Any:
    """Preserve the old lazy import seam used by generation publishing."""

    from .. import redis_client

    return redis_client.get_redis()


def _poster_style_auto_tag_concurrency() -> int:
    return poster_style_tagging.auto_tag_concurrency()


def _poster_style_auto_tag_semaphore() -> asyncio.Semaphore:
    global _POSTER_STYLE_AUTO_TAG_SEMAPHORE, _POSTER_STYLE_AUTO_TAG_SEMAPHORE_LOOP
    loop = asyncio.get_running_loop()
    if (
        _POSTER_STYLE_AUTO_TAG_SEMAPHORE is None
        or _POSTER_STYLE_AUTO_TAG_SEMAPHORE_LOOP is not loop
    ):
        _POSTER_STYLE_AUTO_TAG_SEMAPHORE = asyncio.Semaphore(
            _poster_style_auto_tag_concurrency()
        )
        _POSTER_STYLE_AUTO_TAG_SEMAPHORE_LOOP = loop
    return _POSTER_STYLE_AUTO_TAG_SEMAPHORE


def _http(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    error: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        error["details"] = extra
    return HTTPException(status_code=http, detail={"error": error})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _now().isoformat().replace("+00:00", "Z")


def _safe_datetime(value: Any) -> datetime | None:
    return poster_style_serialization.safe_datetime(value)


def _dedupe_nonempty(values: Iterable[str]) -> list[str]:
    return poster_style_serialization.dedupe_nonempty(values)


def _clean_string_list(
    values: Iterable[Any],
    *,
    max_items: int,
    max_len: int,
) -> list[str]:
    return poster_style_serialization.clean_string_list(
        values,
        max_items=max_items,
        max_len=max_len,
    )


# ----- Storage and index compatibility facade ------------------------------


def _storage_root() -> Path:
    return poster_style_resources.storage_root(_runtime())


def _storage_path(storage_key: str) -> Path:
    return poster_style_resources.storage_path(_runtime(), storage_key)


def _fsync_dir(path: Path) -> None:
    poster_style_resources.fsync_dir(path)


def _write_bytes_replace(path: Path, data: bytes) -> None:
    poster_style_resources.write_bytes_replace(path, data)


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    poster_style_resources.write_json_atomic(_runtime(), path, data)


def _read_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    return poster_style_resources.read_json_file(_runtime(), path, default)


def _guess_mime(path: Path) -> str:
    return poster_style_resources.guess_mime(path)


def _preset_storage_root() -> Path:
    return poster_style_resources.preset_storage_root(_runtime())


def _global_preset_index_path() -> Path:
    return poster_style_resources.global_preset_index_path(_runtime())


def _library_sync_state_path() -> Path:
    return poster_style_resources.library_sync_state_path(_runtime())


def _library_sync_lock_path() -> Path:
    return poster_style_resources.library_sync_lock_path(_runtime())


def _local_presets_root() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "assets" / "poster-style-presets"
        if candidate.is_dir():
            return candidate
    return None


def _default_global_index() -> dict[str, Any]:
    return poster_style_resources.default_global_index(_runtime())


def _default_sync_state() -> dict[str, Any]:
    return poster_style_resources.default_sync_state(_runtime())


def _load_global_preset_index() -> dict[str, Any]:
    return poster_style_resources.load_global_preset_index(_runtime())


def _save_global_preset_index(index: dict[str, Any]) -> None:
    poster_style_resources.save_global_preset_index(_runtime(), index)


def _save_sync_state(state: dict[str, Any]) -> None:
    poster_style_resources.save_sync_state(_runtime(), state)


async def _sync_mode(db: AsyncSession) -> str:
    return await poster_style_resources.sync_mode(_runtime(), db)


async def _can_sync_library(db: AsyncSession, user: Any) -> bool:
    return await poster_style_resources.can_sync_library(_runtime(), db, user)


async def _resolve_sync_proxy(
    db: AsyncSession,
) -> tuple[Any | None, str | None]:
    return await poster_style_resources.resolve_sync_proxy(_runtime(), db)


def _http_client_kwargs(proxy_url: str | None) -> dict[str, Any]:
    return poster_style_resources.http_client_kwargs(_runtime(), proxy_url)


async def _sync_state_out(
    db: AsyncSession,
    user: Any,
) -> PosterStyleSyncStateOut:
    return await poster_style_resources.sync_state_out(_runtime(), db, user)


def _item_out_from_row(row: PosterStyleItem) -> PosterStyleItemOut:
    return poster_style_serialization.item_out_from_row(_runtime(), row)


def _item_out_from_preset(raw: dict[str, Any]) -> PosterStyleItemOut:
    return poster_style_serialization.item_out_from_preset(_runtime(), raw)


async def _load_user_hidden_preset_ids(
    db: AsyncSession,
    user_id: str,
) -> set[str]:
    return await poster_style_resources.load_user_hidden_preset_ids(
        _runtime(),
        db,
        user_id,
    )


def _filter_preset_items(
    items: Iterable[dict[str, Any]],
    *,
    category: str,
    q: str,
    tags: Iterable[str],
) -> list[dict[str, Any]]:
    return poster_style_serialization.filter_preset_items(
        _runtime(),
        items,
        category=category,
        q=q,
        tags=tags,
    )


async def _load_user_items(
    db: AsyncSession,
    *,
    user_id: str,
    category: str,
    q: str,
    tags: list[str],
) -> list[PosterStyleItem]:
    return await poster_style_resources.load_user_items(
        _runtime(),
        db,
        user_id=user_id,
        category=category,
        q=q,
        tags=tags,
    )


# ----- GitHub sync compatibility facade ------------------------------------


_PosterStyleSyncLimitExceeded = poster_style_sync.PosterStyleSyncLimitExceeded
_PosterStyleSyncLeaseLost = poster_style_sync.PosterStyleSyncLeaseLost


def _decoded_url_path_segments(
    url: str,
    *,
    allow_trailing_slash: bool = False,
) -> list[str] | None:
    return poster_style_sync.decoded_url_path_segments(
        url,
        allow_trailing_slash=allow_trailing_slash,
    )


def _validate_github_contents_url(url: str) -> str:
    return poster_style_sync.validate_github_contents_url(_runtime(), url)


def _validate_github_download_url(url: str) -> str | None:
    return poster_style_sync.validate_github_download_url(_runtime(), url)


def _require_github_download_url(url: str) -> str:
    return poster_style_sync.require_github_download_url(_runtime(), url)


def _github_api_child_url(base_url: str, child_name: str) -> str:
    return poster_style_sync.github_api_child_url(base_url, child_name)


async def _fetch_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    validate_url: Callable[[str], str],
    headers: dict[str, str] | None = None,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> bytes:
    return await poster_style_sync.fetch_bytes(
        _runtime(),
        client,
        url,
        max_bytes=max_bytes,
        validate_url=validate_url,
        headers=headers,
        progress=progress,
    )


async def _fetch_github_contents_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> bytes:
    return await poster_style_sync.fetch_github_contents_bytes(
        _runtime(),
        client,
        url,
        max_bytes=max_bytes,
        progress=progress,
    )


async def _fetch_github_download_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int | None = None,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> bytes:
    return await poster_style_sync.fetch_github_download_bytes(
        _runtime(),
        client,
        url,
        max_bytes=max_bytes,
        progress=progress,
    )


async def _walk_github_contents(
    client: httpx.AsyncClient,
    contents_url: str,
    *,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    return await poster_style_sync.walk_github_contents(
        _runtime(),
        client,
        contents_url,
        progress=progress,
    )


def _github_entry_size(entry: dict[str, Any]) -> int | None:
    return poster_style_sync.github_entry_size(entry)


def _sync_download_limit(
    *,
    downloaded_bytes: int,
    expected_size: int | None,
) -> int:
    return poster_style_sync.sync_download_limit(
        _runtime(),
        downloaded_bytes=downloaded_bytes,
        expected_size=expected_size,
    )


def _github_entry_path(entry: dict[str, Any]) -> Path | None:
    return poster_style_sync.github_entry_path(entry)


def _poster_relative_parts(path: Path) -> list[str]:
    return poster_style_sync.poster_relative_parts(path)


async def _fetch_meta_json(
    client: httpx.AsyncClient,
    entry: dict[str, Any],
    *,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any] | None:
    return await poster_style_sync.fetch_meta_json(
        _runtime(),
        client,
        entry,
        progress=progress,
    )


def _sync_lease_owner(state: dict[str, Any]) -> tuple[str, datetime] | None:
    return poster_style_sync.sync_lease_owner(state)


def _claim_library_sync_lease_sync() -> tuple[str | None, dict[str, Any]]:
    return poster_style_sync.claim_library_sync_lease_sync(_runtime())


async def _claim_library_sync_lease() -> tuple[str | None, dict[str, Any]]:
    return await poster_style_sync.claim_library_sync_lease(_runtime())


def _renew_library_sync_lease_sync(token: str) -> bool:
    return poster_style_sync.renew_library_sync_lease_sync(_runtime(), token)


async def _renew_library_sync_lease(token: str) -> bool:
    return await poster_style_sync.renew_library_sync_lease(_runtime(), token)


def _complete_library_sync_lease_sync(
    token: str,
    index: dict[str, Any],
    result: dict[str, Any],
    completed_at: datetime,
) -> None:
    poster_style_sync.complete_library_sync_lease_sync(
        _runtime(),
        token,
        index,
        result,
        completed_at,
    )


async def _complete_library_sync_lease(
    token: str,
    index: dict[str, Any],
    result: dict[str, Any],
    completed_at: datetime,
) -> None:
    await poster_style_sync.complete_library_sync_lease(
        _runtime(),
        token,
        index,
        result,
        completed_at,
    )


def _fail_library_sync_lease_sync(
    token: str,
    *,
    message: str,
    result: dict[str, Any],
) -> bool:
    return poster_style_sync.fail_library_sync_lease_sync(
        _runtime(),
        token,
        message=message,
        result=result,
    )


async def _fail_library_sync_lease(
    token: str,
    *,
    message: str,
    result: dict[str, Any],
) -> bool:
    return await poster_style_sync.fail_library_sync_lease(
        _runtime(),
        token,
        message=message,
        result=result,
    )


def _cached_sync_response(state: dict[str, Any]) -> PosterStyleSyncOut:
    return poster_style_sync.cached_sync_response(_runtime(), state)


async def _sync_library_presets_from_github_folder(
    contents_url: str,
    *,
    proxy_url: str | None = None,
) -> PosterStyleSyncOut:
    return await poster_style_sync.sync_library_presets_from_github_folder(
        _runtime(),
        contents_url,
        proxy_url=proxy_url,
    )


def _build_preset_entry(
    *,
    parsed_meta: dict[str, Any],
    samples_for_storage: list[dict[str, Any]],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    return poster_style_sync.build_preset_entry(
        _runtime(),
        parsed_meta=parsed_meta,
        samples_for_storage=samples_for_storage,
        previous=previous,
    )


def _preset_changed(prev: dict[str, Any], cur: dict[str, Any]) -> bool:
    return poster_style_sync.preset_changed(prev, cur)


async def _do_sync_library_presets(
    contents_url: str,
    state: dict[str, Any],
    *,
    proxy_url: str | None = None,
    lease_token: str | None = None,
) -> PosterStyleSyncOut:
    return await _do_poster_style_sync(
        _runtime(),
        contents_url,
        state,
        proxy_url=proxy_url,
        lease_token=lease_token,
    )


def _publish_local_bootstrap_sync(items: list[dict[str, Any]]) -> bool:
    return poster_style_sync.publish_local_bootstrap_sync(_runtime(), items)


async def _bootstrap_local_presets_if_empty() -> None:
    await poster_style_sync.bootstrap_local_presets_if_empty(_runtime())


# ----- Resource compatibility facade ---------------------------------------


async def _find_user_item(
    db: AsyncSession,
    *,
    user_id: str,
    item_id: str,
) -> PosterStyleItem | None:
    return await poster_style_resources.find_user_item(
        _runtime(),
        db,
        user_id=user_id,
        item_id=item_id,
    )


async def _find_preset_item(
    db: AsyncSession,
    *,
    user_id: str,
    item_id: str,
) -> dict[str, Any] | None:
    return await poster_style_resources.find_preset_item(
        _runtime(),
        db,
        user_id=user_id,
        item_id=item_id,
    )


def _sha256_file(path: Path) -> str:
    return poster_style_resources.sha256_file(path)


def _open_storage_file(storage_key: str) -> tuple[Path, str, int]:
    return poster_style_resources.open_storage_file(_runtime(), storage_key)


def _stream_file(path: Path, max_bytes: int) -> Iterable[bytes]:
    return poster_style_resources.stream_file(path, max_bytes)


async def _binary_response(storage_key: str, request: Request) -> Response:
    return await poster_style_resources.binary_response(
        _runtime(),
        storage_key,
        request,
    )


async def _validate_owned_image_ids(
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: list[str],
) -> list[str]:
    return await poster_style_resources.validate_owned_image_ids(
        _runtime(),
        db,
        user_id=user_id,
        image_ids=image_ids,
    )


# ----- List and binary routes ----------------------------------------------


@router.get("", response_model=PosterStyleListOut)
async def list_poster_styles(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    category: str = Query(default="all"),
    source: str = Query(default="all"),
    q: str = Query(default=""),
    tags: list[str] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PosterStyleListOut:
    return await poster_style_resources.list_poster_styles(
        _runtime(),
        user=user,
        db=db,
        category=category,
        source=source,
        q=q,
        tags=list(tags or []),
        limit=limit,
        offset=offset,
    )


@router.get("/items/{item_id:path}/binary")
async def get_poster_style_item_binary(
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    return await poster_style_resources.get_preset_binary(
        _runtime(),
        item_id=item_id,
        request=request,
        user=user,
        db=db,
    )


@router.get("/items/{item_id:path}/thumb")
async def get_poster_style_item_thumb(
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    return await poster_style_resources.get_preset_binary(
        _runtime(),
        item_id=item_id,
        request=request,
        user=user,
        db=db,
        thumbnail=True,
    )


@router.get("/items/{item_id:path}/samples/{sample_index}")
async def get_poster_style_sample(
    item_id: str,
    sample_index: int,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    return await poster_style_resources.get_preset_sample(
        _runtime(),
        item_id=item_id,
        sample_index=sample_index,
        request=request,
        user=user,
        db=db,
    )


# ----- CRUD routes ----------------------------------------------------------


@router.post(
    "/items",
    response_model=PosterStyleItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_poster_style_item(
    body: PosterStyleCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> PosterStyleItemOut:
    return await poster_style_resources.create_item(
        _runtime(),
        body=body,
        user=user,
        db=db,
        background_tasks=background_tasks,
    )


@router.patch(
    "/items/{item_id:path}",
    response_model=PosterStyleItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_poster_style_item(
    item_id: str,
    body: PosterStylePatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleItemOut:
    return await poster_style_resources.patch_item(
        _runtime(),
        item_id=item_id,
        body=body,
        user=user,
        db=db,
    )


async def _delete_poster_style_item_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    item_id: str,
) -> bool:
    return await poster_style_resources.delete_item_for_user(
        _runtime(),
        db,
        user_id=user_id,
        item_id=item_id,
    )


@router.delete(
    "/items/{item_id:path}",
    dependencies=[Depends(verify_csrf)],
)
async def delete_poster_style_item(
    item_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    return await poster_style_resources.delete_item(
        _runtime(),
        item_id=item_id,
        user=user,
        db=db,
    )


@router.post(
    "/items/batch-delete",
    response_model=PosterStyleBatchDeleteOut,
    dependencies=[Depends(verify_csrf)],
)
async def batch_delete_poster_style_items(
    body: PosterStyleBatchDeleteIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleBatchDeleteOut:
    return await poster_style_resources.batch_delete_items(
        _runtime(),
        body=body,
        user=user,
        db=db,
    )


@router.post(
    "/sync-presets",
    response_model=PosterStyleSyncOut,
    dependencies=[Depends(verify_csrf)],
)
async def sync_poster_style_presets(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleSyncOut:
    if not await _can_sync_library(db, user):
        raise _http("forbidden", "poster style preset sync is not allowed", 403)
    _, proxy_url = await _resolve_sync_proxy(db)
    return await _sync_library_presets_from_github_folder(
        _github_contents_url(),
        proxy_url=proxy_url,
    )


# ----- Generation workflow facade ------------------------------------------


def _poster_style_generate_image_params(aspect_ratio: str) -> ImageParamsIn:
    return poster_style_generation.generate_image_params(aspect_ratio)


def _poster_style_generate_prompt(
    *,
    body: PosterStyleGenerateIn,
    candidate_index: int,
) -> str:
    return poster_style_serialization.generate_prompt(
        body,
        candidate_index=candidate_index,
    )


async def _get_or_create_workflow_conversation(
    db: AsyncSession,
    *,
    user: Any,
    title: str,
    workflow_type: str,
) -> Any:
    return await poster_style_generation.get_or_create_workflow_conversation(
        _runtime(),
        db,
        user=user,
        title=title,
        workflow_type=workflow_type,
    )


async def _create_user_message(
    db: AsyncSession,
    *,
    conv: Any,
    text: str,
    attachment_ids: list[str],
    workflow_run_id: str,
    workflow_step_key: str,
) -> Any:
    return await poster_style_generation.create_user_message(
        _runtime(),
        db,
        conv=conv,
        text=text,
        attachment_ids=attachment_ids,
        workflow_run_id=workflow_run_id,
        workflow_step_key=workflow_step_key,
    )


async def _poster_style_create_assistant_task(**kwargs: Any) -> Any:
    # Keep the route-specific billing/credential adapters used by messages.
    from .messages import _create_assistant_task

    return await _create_assistant_task(**kwargs)


async def _poster_style_publish_assistant_task(**kwargs: Any) -> None:
    from .messages import _publish_assistant_task

    await _publish_assistant_task(**kwargs)


async def _enqueue_poster_style_generate_tasks(
    *,
    db: AsyncSession,
    user: Any,
    conv: Any,
    run: WorkflowRun,
    step: WorkflowStep,
    body: PosterStyleGenerateIn,
) -> tuple[list[str], list[dict[str, Any]]]:
    return await poster_style_generation.enqueue_generate_tasks(
        _runtime(),
        db=db,
        user=user,
        conv=conv,
        run=run,
        step=step,
        body=body,
        create_task_fn=_poster_style_create_assistant_task,
    )


@router.post(
    "/generate",
    response_model=PosterStyleGenerateOut,
    dependencies=[Depends(verify_csrf)],
)
async def generate_poster_style_samples(
    body: PosterStyleGenerateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleGenerateOut:
    """用户提交 prompt + 元数据，后端创建隐藏 workflow 并入队 N 个生成任务。"""
    return await poster_style_generation.generate_poster_style_samples(
        _runtime(),
        body=body,
        user=user,
        db=db,
        enqueue_fn=_enqueue_poster_style_generate_tasks,
        publish_fn=_poster_style_publish_assistant_task,
    )


def _poster_style_job_status(
    *,
    step_status: str,
    requested_count: int,
    finished_count: int,
) -> str:
    return poster_style_generation.job_status(
        step_status=step_status,
        requested_count=requested_count,
        finished_count=finished_count,
    )


async def _job_from_run(
    db: AsyncSession,
    *,
    run: WorkflowRun,
) -> PosterStyleJobOut:
    return await poster_style_generation.job_from_run(
        _runtime(),
        db,
        run=run,
    )


@router.get(
    "/jobs",
    response_model=PosterStyleJobsOut,
)
async def list_poster_style_jobs(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PosterStyleJobsOut:
    return await poster_style_generation.list_poster_style_jobs(
        _runtime(),
        user=user,
        db=db,
        limit=limit,
        offset=offset,
    )


# ----- Vision auto-tag facade ----------------------------------------------


async def _run_auto_tag_in_background(user_id: str, item_id: str) -> None:
    await poster_style_tagging.run_auto_tag_in_background(
        _runtime(),
        user_id,
        item_id,
    )


async def _api_call_poster_style_tagging_upstream(
    db: AsyncSession,
    *,
    image_id: str,
    user_id: str,
) -> dict[str, Any]:
    return await poster_style_tagging.call_tagging_upstream(
        _runtime(),
        db,
        image_id=image_id,
        user_id=user_id,
    )


def _parse_poster_style_tagging_text(text: str) -> dict[str, Any]:
    return poster_style_serialization.parse_tagging_text(text)


async def _auto_tag_poster_style_item(
    *,
    db: AsyncSession,
    user_id: str,
    item_id: str,
) -> PosterStyleAutoTagOut:
    return await poster_style_tagging.auto_tag_item(
        _runtime(),
        db=db,
        user_id=user_id,
        item_id=item_id,
    )


@router.post(
    "/items/{item_id:path}/auto-tag",
    response_model=PosterStyleAutoTagOut,
    dependencies=[Depends(verify_csrf)],
)
async def auto_tag_poster_style_item(
    item_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleAutoTagOut:
    return await _auto_tag_poster_style_item(
        db=db,
        user_id=user.id,
        item_id=item_id,
    )


# ----- Detail catch-all -----------------------------------------------------


@router.get("/{item_id:path}", response_model=PosterStyleItemOut)
async def get_poster_style_item(
    item_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterStyleItemOut:
    if item_id in {"sync-presets", "jobs", "items", "generate"}:
        raise _http("not_found", "poster style item not found", 404)
    if item_id.startswith("user:"):
        row = await _find_user_item(db, user_id=user.id, item_id=item_id)
        if row is None:
            raise _http("not_found", "poster style item not found", 404)
        return _item_out_from_row(row)
    raw = await _find_preset_item(db, user_id=user.id, item_id=item_id)
    if raw is None:
        raise _http("not_found", "poster style item not found", 404)
    return _item_out_from_preset(raw)
