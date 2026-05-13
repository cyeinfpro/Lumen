"""Poster Style Library 路由（V1.1 海报工作流）。

风格库的"DB 表 + GitHub 同步 + 用户生成"完整 API 表面。蓝本是
``workflows.py`` 里的 ``apparel-model-library`` 一套实现，差异主要在：

* 元数据从 ``meta.json`` 解析（而非文件名）。
* 每条 PosterStyleItem 有一个 cover + 0~N 张 sample。
* 没有 age_segment / gender / appearance_direction，改用 category / palette / mood。

权限 / 冷却 / 锁与 apparel 完全一致：登录态 + CSRF，5min 成功 cooldown +
30s 失败 cooldown，asyncio.Lock 同进程串行 GitHub sync。

[NOTE] worker 端的 "poster_style_library_generate" workflow_action 由
``apps/worker/app/tasks/poster_style_tagging.py`` + worker generation hook
处理；本文件只负责入队（API 层）。后续 worker hook 接入时不需要改本文件。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Iterable

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
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    GenerationStatus,
    Intent,
    POSTER_STYLE_CATEGORIES,
    POSTER_STYLE_IMAGE_SUFFIXES,
    POSTER_STYLE_MAX_BINARY_BYTES,
    POSTER_STYLE_MAX_SAMPLES,
    POSTER_STYLE_SCHEMA_VERSION,
    POSTER_STYLE_SOURCES,
    POSTER_STYLE_SYNC_COOLDOWN_S,
    POSTER_STYLE_SYNC_FAILURE_COOLDOWN_S,
    Role,
)
from lumen_core.models import (
    Conversation,
    Generation,
    Image,
    Message,
    PosterStyleHiddenPreset,
    PosterStyleItem,
    WorkflowRun,
    WorkflowStep,
    new_uuid7,
)
from lumen_core.providers import (
    ProviderProxyDefinition,
    parse_proxy_json,
    resolve_provider_proxy_url,
)
from lumen_core.runtime_settings import get_spec
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
    PosterStyleSampleOut,
    PosterStyleSyncOut,
    PosterStyleSyncStateOut,
)

from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..runtime_settings import get_setting
from ._poster_library import (
    POSTER_STYLE_GENERATE_STEP_KEY,
    POSTER_STYLE_GENERATE_WORKER_ACTION,
    POSTER_STYLE_ROOT_KEY,
    POSTER_STYLE_SYNC_MODE_KEY,
    POSTER_STYLE_SYNC_PROXY_NAME_KEY,
    POSTER_STYLE_SYNC_USE_PROXY_POOL_KEY,
    WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
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
    _preset_item_id,
    _preset_storage_key,
    _preset_thumb_storage_key,
    _scan_local_presets,
)


router = APIRouter(prefix="/poster-styles", tags=["poster-styles"])
logger = logging.getLogger(__name__)


# ----- 基础 helper（部分对齐 workflows.py，避免循环 import 拷贝过来） -----


def _http(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        err["details"] = extra
    return HTTPException(status_code=http, detail={"error": err})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _now().isoformat().replace("+00:00", "Z")


def _safe_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dedupe_nonempty(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        v = value.strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _clean_string_list(
    values: Iterable[Any], *, max_items: int, max_len: int
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        if not isinstance(raw, (str, int, float)):
            continue
        item = str(raw).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item[:max_len])
        if len(out) >= max_items:
            break
    return out


def _storage_root() -> Path:
    return Path(settings.storage_root).resolve()


def _storage_path(storage_key: str) -> Path:
    root = _storage_root()
    if not storage_key or "\x00" in storage_key:
        raise _http("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise _http("invalid_path", "absolute storage paths are not allowed", 400)
    path = (root / key_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise _http("invalid_path", "storage path escapes root", 400) from exc
    return path


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bytes_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        data, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(default)
    except (OSError, json.JSONDecodeError) as exc:
        raise _http(
            "invalid_index", f"invalid poster style index: {path.name}", 500
        ) from exc
    if not isinstance(data, dict):
        raise _http(
            "invalid_index", f"invalid poster style index: {path.name}", 500
        )
    return data


def _guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"


# ----- 路径 / index helpers ------------------------------------------------


def _preset_storage_root() -> Path:
    """``<storage_root>/poster-style-library/``：preset 二进制 + index.json 都挂这。"""
    return _storage_path(POSTER_STYLE_ROOT_KEY)


def _global_preset_index_path() -> Path:
    """全局 preset index 的本地落盘路径（同步成功后写入）。"""
    return _preset_storage_root() / "index.json"


def _library_sync_state_path() -> Path:
    return _preset_storage_root() / "sync-state.json"


def _local_presets_root() -> Path | None:
    """仓库内 ``assets/poster-style-presets/`` 路径（开发树 / 部署都对齐）。

    若部署不带 assets/ 目录（容器化最小镜像）返回 None。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "assets" / "poster-style-presets"
        if candidate.is_dir():
            return candidate
    return None


def _default_global_index() -> dict[str, Any]:
    return {
        "schema_version": POSTER_STYLE_SCHEMA_VERSION,
        "updated_at": None,
        "preset_items": [],
    }


def _default_sync_state() -> dict[str, Any]:
    return {
        "schema_version": POSTER_STYLE_SCHEMA_VERSION,
        "last_success_at": None,
        "last_error": None,
        "last_attempt_at": None,
        "last_result": None,
    }


def _load_global_preset_index() -> dict[str, Any]:
    return _read_json_file(_global_preset_index_path(), _default_global_index())


def _save_global_preset_index(index: dict[str, Any]) -> None:
    index["schema_version"] = POSTER_STYLE_SCHEMA_VERSION
    index["updated_at"] = _iso_now()
    _write_json_atomic(_global_preset_index_path(), index)


def _save_sync_state(state: dict[str, Any]) -> None:
    state["schema_version"] = POSTER_STYLE_SCHEMA_VERSION
    _write_json_atomic(_library_sync_state_path(), state)


# ----- 权限 / 同步配置 ----------------------------------------------------


async def _sync_mode(db: AsyncSession) -> str:
    """读 system_setting；缺省返回 admin_only。"""
    spec = get_spec(POSTER_STYLE_SYNC_MODE_KEY)
    raw = await get_setting(db, spec) if spec is not None else None
    mode = str(raw or _DEFAULT_SYNC_MODE).strip().lower()
    if mode not in {"admin_only", "any_authenticated", "disabled"}:
        return _DEFAULT_SYNC_MODE
    return mode


async def _can_sync_library(db: AsyncSession, user: Any) -> bool:
    mode = await _sync_mode(db)
    if mode == "disabled":
        return False
    if mode == "any_authenticated":
        return True
    return getattr(user, "role", "") == "admin"


async def _resolve_sync_proxy(
    db: AsyncSession,
) -> tuple[ProviderProxyDefinition | None, str | None]:
    """与模特库 _resolve_model_library_sync_proxy 完全同构。"""
    use_spec = get_spec(POSTER_STYLE_SYNC_USE_PROXY_POOL_KEY)
    use_raw = await get_setting(db, use_spec) if use_spec is not None else None
    if str(use_raw or "0").strip() != "1":
        return None, None

    providers_spec = get_spec("providers")
    raw_providers = (
        await get_setting(db, providers_spec) if providers_spec is not None else None
    )
    proxies, errors = parse_proxy_json(raw_providers)
    for err in errors:
        logger.warning("poster style sync proxy config warning: %s", err)
    enabled = [proxy for proxy in proxies if proxy.enabled]
    if not enabled:
        raise _http(
            "proxy_unavailable",
            "poster style sync proxy pool is enabled but has no enabled proxies",
            409,
        )

    name_spec = get_spec(POSTER_STYLE_SYNC_PROXY_NAME_KEY)
    name_raw = await get_setting(db, name_spec) if name_spec is not None else None
    target_name = str(name_raw or "").strip()
    if target_name:
        proxy = next((p for p in enabled if p.name == target_name), None)
        if proxy is None:
            raise _http(
                "proxy_not_found",
                f"poster style sync proxy '{target_name}' not found or disabled",
                409,
            )
    else:
        proxy = enabled[0]
    proxy_url = await resolve_provider_proxy_url(proxy)
    if not proxy_url:
        raise _http(
            "proxy_resolve_failed",
            f"poster style sync proxy '{proxy.name}' could not be resolved",
            409,
        )
    return proxy, proxy_url


def _http_client_kwargs(proxy_url: str | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(30.0),
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


async def _sync_state_out(db: AsyncSession, user: Any) -> PosterStyleSyncStateOut:
    state = _read_json_file(_library_sync_state_path(), _default_sync_state())
    return PosterStyleSyncStateOut(
        last_success_at=_safe_datetime(state.get("last_success_at")),
        last_error=_clean_optional_text(state.get("last_error"), max_len=1000),
        can_sync=await _can_sync_library(db, user),
        github_contents_url=_github_contents_url() or None,
    )


# ----- DB row → API out -----------------------------------------------------


def _item_out_from_row(row: PosterStyleItem) -> PosterStyleItemOut:
    """user item 走 image API（cover_image_id + sample_image_ids 都指向真实 Image）。"""
    cover_id = row.cover_image_id
    sample_ids = list(row.sample_image_ids or [])
    if cover_id and cover_id not in sample_ids:
        sample_ids = [cover_id, *sample_ids]
    if cover_id:
        cover_url = f"/api/images/{cover_id}/binary"
        display_url = f"/api/images/{cover_id}/variants/display2048"
        thumb_url = display_url
    else:
        cover_url = _library_item_url(row.id, "binary")
        display_url = cover_url
        thumb_url = _library_item_url(row.id, "thumb")
    samples_out = [
        PosterStyleSampleOut(
            index=idx,
            image_id=image_id,
            image_url=f"/api/images/{image_id}/binary",
            display_url=f"/api/images/{image_id}/variants/display2048",
            thumb_url=f"/api/images/{image_id}/variants/display2048",
        )
        for idx, image_id in enumerate(sample_ids)
    ]
    return PosterStyleItemOut(
        id=row.id,
        source=row.source,  # type: ignore[arg-type]
        visibility_scope="user_private",
        title=str(row.title or "").strip()[:120] or "未命名风格",
        category=_normalize_category(row.category),  # type: ignore[arg-type]
        mood=_clean_optional_text(row.mood, max_len=120),
        prompt_template=_clean_optional_text(row.prompt_template, max_len=2000),
        palette=list(row.palette or []),
        recommended_aspects=list(row.recommended_aspects or []),
        style_tags=list(row.style_tags or []),
        cover_image_url=cover_url,
        display_url=display_url,
        thumb_url=thumb_url,
        cover_image_id=cover_id,
        sample_image_ids=sample_ids,
        samples=samples_out,
        preset_id=None,
        version=None,
        library_folder=_clean_optional_text(row.library_folder, max_len=64),
        download_filename=None,
        auto_tagged_at=row.auto_tagged_at,
        auto_tag_notes=_clean_optional_text(row.auto_tag_notes, max_len=400),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _item_out_from_preset(raw: dict[str, Any]) -> PosterStyleItemOut:
    """preset 走 ``/poster-styles/items/{id}/binary`` 或 samples/{i}。"""
    item_id = str(raw.get("id") or "")
    samples = raw.get("samples")
    if not isinstance(samples, list):
        samples = []
    samples_out: list[PosterStyleSampleOut] = []
    for idx, sample in enumerate(samples):
        if not isinstance(sample, dict):
            continue
        sample_url = _library_sample_url(item_id, idx)
        samples_out.append(
            PosterStyleSampleOut(
                index=idx,
                image_id=None,
                image_url=sample_url,
                display_url=sample_url,
                thumb_url=sample_url,
            )
        )
    has_samples = bool(samples_out)
    cover_url = _library_item_url(item_id, "binary") if has_samples else ""
    thumb_url = _library_item_url(item_id, "thumb") if has_samples else None
    created_at = _safe_datetime(raw.get("created_at")) or _now()
    updated_at = _safe_datetime(raw.get("updated_at"))
    return PosterStyleItemOut(
        id=item_id,
        source="preset",
        visibility_scope="global_preset",
        title=str(raw.get("title") or "").strip()[:120] or "未命名风格",
        category=_normalize_category(raw.get("category")),  # type: ignore[arg-type]
        mood=_clean_optional_text(raw.get("mood"), max_len=120),
        prompt_template=_clean_optional_text(
            raw.get("prompt_template"), max_len=2000
        ),
        palette=_normalize_palette(raw.get("palette") or []),
        recommended_aspects=_normalize_recommended_aspects(
            raw.get("recommended_aspects") or []
        ),
        style_tags=_normalize_style_tags(raw.get("style_tags") or []),
        cover_image_url=cover_url,
        display_url=cover_url or None,
        thumb_url=thumb_url,
        cover_image_id=None,
        sample_image_ids=[],
        samples=samples_out,
        preset_id=_clean_optional_text(raw.get("preset_id"), max_len=120),
        version=int(raw.get("version") or 1),
        library_folder=_clean_optional_text(
            raw.get("library_folder")
            or _poster_style_folder_for_category(raw.get("category")),
            max_len=64,
        ),
        download_filename=None,
        auto_tagged_at=None,
        auto_tag_notes=None,
        created_at=created_at,
        updated_at=updated_at,
    )


# ----- 列表 / 过滤 helpers --------------------------------------------------


async def _load_user_hidden_preset_ids(
    db: AsyncSession, user_id: str
) -> set[str]:
    rows = (
        await db.execute(
            select(PosterStyleHiddenPreset.preset_id).where(
                PosterStyleHiddenPreset.user_id == user_id
            )
        )
    ).scalars().all()
    return {pid for pid in rows if isinstance(pid, str)}


def _filter_preset_items(
    items: Iterable[dict[str, Any]],
    *,
    category: str,
    q: str,
    tags: Iterable[str],
) -> list[dict[str, Any]]:
    query = q.strip().lower()
    tag_filter = {t.strip().lower() for t in tags if isinstance(t, str) and t.strip()}
    out: list[dict[str, Any]] = []
    for item in items:
        item_category = _normalize_category(item.get("category"))
        if category != "all" and item_category != category:
            continue
        item_tags = {
            str(t).strip().lower()
            for t in (item.get("style_tags") or [])
            if isinstance(t, (str, int, float))
        }
        if tag_filter and not (tag_filter & item_tags):
            continue
        if query:
            haystack = " ".join(
                [
                    str(item.get("title") or ""),
                    str(item.get("mood") or ""),
                    str(item.get("prompt_template") or ""),
                    " ".join(item.get("style_tags") or []),
                ]
            ).lower()
            if query not in haystack:
                continue
        out.append(item)
    category_rank = {
        cat: idx
        for idx, cat in enumerate(
            [
                "illustration",
                "3d",
                "minimal",
                "retro",
                "traditional",
                "photo",
                "other",
                "user_favorites",
            ]
        )
    }
    return sorted(
        out,
        key=lambda item: (
            category_rank.get(_normalize_category(item.get("category")), 9),
            str(item.get("preset_id") or ""),
            int(item.get("version") or 0),
        ),
    )


async def _load_user_items(
    db: AsyncSession,
    *,
    user_id: str,
    category: str,
    q: str,
    tags: list[str],
) -> list[PosterStyleItem]:
    stmt = select(PosterStyleItem).where(PosterStyleItem.user_id == user_id)
    if category != "all":
        stmt = stmt.where(PosterStyleItem.category == category)
    rows = list(
        (
            await db.execute(stmt.order_by(desc(PosterStyleItem.created_at)))
        ).scalars().all()
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
        tag_set = {t.strip().lower() for t in tags if t and t.strip()}
        rows = [
            row
            for row in rows
            if tag_set
            & {str(t).strip().lower() for t in (row.style_tags or [])}
        ]
    return rows


# ----- GitHub sync ----------------------------------------------------------


def _github_api_child_url(base_url: str, child_name: str) -> str:
    prefix, _, query = base_url.partition("?")
    return (
        f"{prefix.rstrip('/')}/{child_name}?{query}"
        if query
        else f"{prefix.rstrip('/')}/{child_name}"
    )


async def _walk_github_contents(
    client: httpx.AsyncClient,
    contents_url: str,
) -> list[dict[str, Any]]:
    resp = await client.get(
        contents_url,
        headers={"Accept": "application/vnd.github+json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("type") == "file":
        return [data]
    if not isinstance(data, list):
        raise ValueError("GitHub contents response must be an array")
    files: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        name = str(entry.get("name") or "")
        if entry_type == "dir":
            child_url = str(entry.get("url") or "") or _github_api_child_url(
                contents_url, name
            )
            files.extend(await _walk_github_contents(client, child_url))
        elif entry_type == "file":
            files.append(entry)
    return files


async def _fetch_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content


async def _fetch_meta_json(client: httpx.AsyncClient, entry: dict[str, Any]) -> dict[str, Any] | None:
    """从 GitHub Contents API 拿到 meta.json 文件实体，下载 raw 并解析为 dict。"""
    download_url = str(entry.get("download_url") or "").strip()
    if not download_url:
        return None
    try:
        raw = await _fetch_bytes(client, download_url)
    except Exception as exc:  # noqa: BLE001
        logger.info("poster style: meta.json download failed url=%s err=%s", download_url, exc)
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.info("poster style: meta.json decode failed err=%s", exc)
        return None
    return data if isinstance(data, dict) else None


def _cached_sync_response(state: dict[str, Any]) -> PosterStyleSyncOut:
    raw_result = state.get("last_result")
    result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    return PosterStyleSyncOut(
        status="skipped",
        added=int(result.get("added") or 0),
        updated=int(result.get("updated") or 0),
        skipped=int(result.get("skipped") or 0),
        errors=_clean_string_list(
            result.get("errors") or [], max_items=20, max_len=300
        ),
        last_success_at=_safe_datetime(state.get("last_success_at")),
        last_error=_clean_optional_text(state.get("last_error"), max_len=1000),
    )


async def _sync_library_presets_from_github_folder(
    contents_url: str,
    *,
    proxy_url: str | None = None,
) -> PosterStyleSyncOut:
    """触发 GitHub 同步。冷却 + 锁与 apparel 同构。

    成功 5min cooldown；失败 30s 短重试 cooldown，避免临时网络故障锁死。
    单进程内 _SYNC_LOCK 串行执行；多副本部署仍要靠 cooldown 防互打。
    """
    if not contents_url:
        raise _http(
            "sync_not_configured",
            "preset GitHub folder url is not configured",
            503,
        )
    async with _SYNC_LOCK:
        state = _read_json_file(_library_sync_state_path(), _default_sync_state())
        last_success = _safe_datetime(state.get("last_success_at"))
        if last_success is not None:
            age = (_now() - last_success).total_seconds()
            if age < POSTER_STYLE_SYNC_COOLDOWN_S:
                return _cached_sync_response(state)
        last_attempt = _safe_datetime(state.get("last_attempt_at"))
        if last_attempt is not None:
            age = (_now() - last_attempt).total_seconds()
            if age < POSTER_STYLE_SYNC_FAILURE_COOLDOWN_S:
                return _cached_sync_response(state)
        return await _do_sync_library_presets(
            contents_url, state, proxy_url=proxy_url
        )


def _build_preset_entry(
    *,
    parsed_meta: dict[str, Any],
    samples_for_storage: list[dict[str, Any]],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    now_iso = _iso_now()
    return {
        "id": _preset_item_id(parsed_meta["preset_id"], parsed_meta["version"]),
        "source": "preset",
        "preset_id": parsed_meta["preset_id"],
        "version": parsed_meta["version"],
        "title": parsed_meta["title"],
        "category": parsed_meta["category"],
        "library_folder": parsed_meta["library_folder"],
        "mood": parsed_meta["mood"],
        "prompt_template": parsed_meta["prompt_template"],
        "palette": parsed_meta["palette"],
        "recommended_aspects": parsed_meta["recommended_aspects"],
        "style_tags": parsed_meta["style_tags"],
        "samples": samples_for_storage,
        "created_at": (previous or {}).get("created_at") or now_iso,
        "updated_at": now_iso,
    }


def _preset_changed(prev: dict[str, Any], cur: dict[str, Any]) -> bool:
    fields = (
        "title",
        "category",
        "mood",
        "prompt_template",
        "palette",
        "recommended_aspects",
        "style_tags",
    )
    if any(prev.get(f) != cur.get(f) for f in fields):
        return True
    raw_prev = prev.get("samples")
    raw_cur = cur.get("samples")
    prev_samples: list[dict[str, Any]] = (
        raw_prev if isinstance(raw_prev, list) else []
    )
    cur_samples: list[dict[str, Any]] = (
        raw_cur if isinstance(raw_cur, list) else []
    )
    if len(prev_samples) != len(cur_samples):
        return True
    for a, b in zip(prev_samples, cur_samples):
        if not isinstance(a, dict) or not isinstance(b, dict):
            continue
        if a.get("sha256") != b.get("sha256"):
            return True
    return False


async def _do_sync_library_presets(
    contents_url: str,
    state: dict[str, Any],
    *,
    proxy_url: str | None = None,
) -> PosterStyleSyncOut:
    now = _now()
    state["last_attempt_at"] = now.isoformat().replace("+00:00", "Z")
    _save_sync_state(state)

    added = 0
    updated = 0
    skipped = 0
    errors: list[str] = []
    try:
        async with httpx.AsyncClient(**_http_client_kwargs(proxy_url)) as client:
            files = await _walk_github_contents(client, contents_url)
            # 按目录分组 GitHub 文件实体。dir 信息靠 entry["path"] 的父目录。
            by_dir: dict[str, dict[str, Any]] = {}
            for entry in files:
                path_value = str(entry.get("path") or entry.get("name") or "")
                if not path_value:
                    continue
                path = Path(path_value)
                parts = [
                    p for p in path.parts if p not in {"assets", "poster-style-presets"}
                ]
                if len(parts) < 2:
                    continue
                dir_name = parts[-2]
                bucket = by_dir.setdefault(
                    dir_name,
                    {"meta": None, "samples": [], "thumbs": {}},
                )
                file_name = path.name.lower()
                suffix = path.suffix.lower()
                if file_name == "meta.json":
                    bucket["meta"] = entry
                elif suffix in POSTER_STYLE_IMAGE_SUFFIXES:
                    stem = path.stem
                    if stem.endswith(".thumb"):
                        base = stem[: -len(".thumb")]
                        bucket["thumbs"][f"{base}{suffix}"] = entry
                    else:
                        bucket["samples"].append(entry)

            index = _load_global_preset_index()
            existing_by_id = {
                str(item.get("id") or ""): dict(item)
                for item in index.get("preset_items", [])
                if isinstance(item, dict)
            }
            next_items: dict[str, dict[str, Any]] = dict(existing_by_id)

            for dir_name, bucket in by_dir.items():
                meta_entry = bucket["meta"]
                if not isinstance(meta_entry, dict):
                    continue
                meta = await _fetch_meta_json(client, meta_entry)
                if meta is None:
                    skipped += 1
                    errors.append(f"{dir_name}: meta.json missing or invalid")
                    continue
                category_hint = _category_from_folder_name(dir_name)
                parsed = _metadata_from_meta_json(
                    meta, category_hint=category_hint
                )
                if parsed is None:
                    skipped += 1
                    errors.append(f"{dir_name}: meta.json has no preset_id")
                    continue
                preset_id = parsed["preset_id"]
                version = int(parsed["version"])
                item_id = _preset_item_id(preset_id, version)
                previous = next_items.get(item_id)

                # 下载样图 + thumb，落盘到 storage。
                sample_entries: list[dict[str, Any]] = sorted(
                    bucket["samples"],
                    key=lambda e: str(e.get("name") or ""),
                )
                samples_for_storage: list[dict[str, Any]] = []
                for sample_entry in sample_entries[:POSTER_STYLE_MAX_SAMPLES]:
                    sample_name = str(sample_entry.get("name") or "")
                    sample_url = str(sample_entry.get("download_url") or "")
                    if not sample_url:
                        continue
                    try:
                        data = await _fetch_bytes(client, sample_url)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(
                            f"{preset_id}: sample {sample_name} download failed: {exc!r}"
                        )
                        continue
                    sample_sha = hashlib.sha256(data).hexdigest()
                    image_key = _preset_storage_key(preset_id, version, sample_name)
                    image_path = _storage_path(image_key)
                    needs_write = not image_path.is_file()
                    if previous:
                        prev_samples = previous.get("samples") or []
                        prev_match = next(
                            (
                                s
                                for s in prev_samples
                                if isinstance(s, dict) and s.get("name") == sample_name
                            ),
                            None,
                        )
                        if prev_match and prev_match.get("sha256") != sample_sha:
                            needs_write = True
                    else:
                        needs_write = True
                    if needs_write:
                        _write_bytes_replace(image_path, data)

                    # thumb：与 image key 同目录下 stem.thumb.*
                    thumb_entry = bucket["thumbs"].get(sample_name)
                    thumb_key = image_key
                    thumb_sha = sample_sha
                    if isinstance(thumb_entry, dict):
                        thumb_url = str(thumb_entry.get("download_url") or "")
                        suffix = Path(sample_name).suffix.lower() or ".webp"
                        base_stem = Path(sample_name).stem
                        thumb_key_candidate = _preset_thumb_storage_key(
                            preset_id, version, base_stem, suffix
                        )
                        if thumb_url:
                            try:
                                thumb_data = await _fetch_bytes(client, thumb_url)
                                thumb_sha = hashlib.sha256(thumb_data).hexdigest()
                                thumb_path = _storage_path(thumb_key_candidate)
                                if not thumb_path.is_file():
                                    _write_bytes_replace(thumb_path, thumb_data)
                                thumb_key = thumb_key_candidate
                            except Exception as exc:  # noqa: BLE001
                                errors.append(
                                    f"{preset_id}: thumb fallback to original: {exc!r}"
                                )
                    samples_for_storage.append(
                        {
                            "name": sample_name,
                            "image_storage_key": image_key,
                            "thumb_storage_key": thumb_key,
                            "sha256": sample_sha,
                            "thumb_sha256": thumb_sha,
                        }
                    )

                item = _build_preset_entry(
                    parsed_meta=parsed,
                    samples_for_storage=samples_for_storage,
                    previous=previous,
                )
                if previous is None:
                    added += 1
                elif _preset_changed(previous, item):
                    updated += 1
                else:
                    skipped += 1
                next_items[item_id] = item

            index["preset_items"] = sorted(
                next_items.values(),
                key=lambda item: (
                    _normalize_category(item.get("category")),
                    str(item.get("preset_id") or ""),
                    int(item.get("version") or 0),
                ),
            )
            _save_global_preset_index(index)
        state = _read_json_file(_library_sync_state_path(), _default_sync_state())
        state["last_success_at"] = now.isoformat().replace("+00:00", "Z")
        state["last_error"] = None
        state["last_result"] = {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "errors": errors[:20],
        }
        _save_sync_state(state)
        return PosterStyleSyncOut(
            status="ok",
            added=added,
            updated=updated,
            skipped=skipped,
            errors=errors[:20],
            last_success_at=now,
            last_error=None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        state = _read_json_file(_library_sync_state_path(), _default_sync_state())
        msg = str(exc)
        state["last_error"] = msg[:1000]
        state["last_result"] = {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "errors": [*errors[:19], msg[:300]],
        }
        _save_sync_state(state)
        raise _http("preset_sync_failed", msg or "preset sync failed", 502) from exc


async def _bootstrap_local_presets_if_empty() -> None:
    """启动后 cold start：本地 index 为空且仓库内 assets 目录存在时一次性 bootstrap。

    与 apparel "GitHub 同步即可建立 index" 不同——风格库样图可能没全推到
    GitHub，只是 meta.json 有；先把本地能扫到的 preset 元数据填到 index 里，
    sync 时再下样图覆盖。如果 sample 仓库内已经有，也一并尝试拷到 storage。
    """
    index = _load_global_preset_index()
    if index.get("preset_items"):
        return
    local_root = _local_presets_root()
    if local_root is None:
        return
    scanned = _scan_local_presets(local_root)
    if not scanned:
        return
    items: list[dict[str, Any]] = []
    for parsed in scanned:
        # bootstrap 阶段不下样图（仓库内可能没真图，避免引入 placeholder 0-byte）。
        # sync-presets 跑过一次后样图会从 GitHub 下载并覆盖。
        items.append(
            _build_preset_entry(
                parsed_meta=parsed,
                samples_for_storage=[],
                previous=None,
            )
        )
    index["preset_items"] = items
    _save_global_preset_index(index)
    logger.info("poster style: bootstrapped %d presets from local assets", len(items))


# ----- Item lookup ---------------------------------------------------------


async def _find_user_item(
    db: AsyncSession, *, user_id: str, item_id: str
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


async def _find_preset_item(
    db: AsyncSession, *, user_id: str, item_id: str
) -> dict[str, Any] | None:
    """preset:<id>:v<n> 形式 id：从全局 index 找；hidden 的对该用户视为不存在。"""
    if not item_id.startswith("preset:"):
        return None
    hidden = await _load_user_hidden_preset_ids(db, user_id)
    if item_id in hidden:
        return None
    index = _load_global_preset_index()
    for item in index.get("preset_items") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == item_id:
            return dict(item)
    return None


def _open_storage_file(storage_key: str) -> tuple[Path, str, str]:
    path = _storage_path(storage_key)
    if not path.is_file():
        raise _http("not_found", "library binary missing", 404)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, _guess_mime(path), sha


def _stream_file(path: Path) -> Iterable[bytes]:
    with path.open("rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            yield chunk


def _binary_response(storage_key: str, request: Request) -> Response:
    path, media_type, sha = _open_storage_file(storage_key)
    size = path.stat().st_size
    if size > POSTER_STYLE_MAX_BINARY_BYTES:
        raise _http(
            "library_binary_too_large",
            f"library binary exceeds {POSTER_STYLE_MAX_BINARY_BYTES} bytes",
            413,
        )
    etag = f'"{sha}"'
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, max-age=86400"},
        )
    return StreamingResponse(
        _stream_file(path),
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "ETag": etag,
            "Content-Length": str(size),
        },
    )


# ----- Owned image helper --------------------------------------------------


async def _validate_owned_image_ids(
    db: AsyncSession, *, user_id: str, image_ids: list[str]
) -> list[str]:
    cleaned = _dedupe_nonempty(image_ids)
    if not cleaned:
        return []
    rows = (
        await db.execute(
            select(Image.id).where(
                Image.id.in_(cleaned),
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    owned = {iid for iid in rows if isinstance(iid, str)}
    missing = [iid for iid in cleaned if iid not in owned]
    if missing:
        raise _http(
            "invalid_image",
            "one or more images are not owned by the current user or were deleted",
            400,
            missing=missing,
        )
    return cleaned


# ----- 列表 + 详情 端点 ----------------------------------------------------


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
    category = category.strip() or "all"
    if category not in POSTER_STYLE_CATEGORIES:
        raise _http("invalid_category", "invalid poster style category", 422)
    source = source.strip() or "all"
    if source not in POSTER_STYLE_SOURCES:
        raise _http("invalid_source", "invalid poster style source", 422)
    tag_list = list(tags or [])

    await _bootstrap_local_presets_if_empty()
    items_out: list[PosterStyleItemOut] = []

    preset_total = 0
    if source in {"all", "preset"}:
        index = _load_global_preset_index()
        hidden = await _load_user_hidden_preset_ids(db, user.id)
        preset_items = [
            item
            for item in index.get("preset_items") or []
            if isinstance(item, dict) and str(item.get("id") or "") not in hidden
        ]
        preset_items = _filter_preset_items(
            preset_items, category=category, q=q, tags=tag_list
        )
        preset_total = len(preset_items)
        items_out.extend(_item_out_from_preset(item) for item in preset_items)

    user_total = 0
    if source in {"all", "favorite", "user_upload", "generated"}:
        user_items = await _load_user_items(
            db,
            user_id=user.id,
            category=category if category != "user_favorites" else "user_favorites",
            q=q,
            tags=tag_list,
        )
        if source != "all":
            user_items = [row for row in user_items if row.source == source]
        user_total = len(user_items)
        items_out.extend(_item_out_from_row(row) for row in user_items)

    total = preset_total + user_total
    page = items_out[offset : offset + limit]
    return PosterStyleListOut(
        items=page,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
        sync=await _sync_state_out(db, user),
    )


# ----- 二进制下载（preset cover / thumb / sample） -------------------------


def _preset_cover_storage_key(preset: dict[str, Any]) -> str:
    samples = preset.get("samples") or []
    if isinstance(samples, list) and samples and isinstance(samples[0], dict):
        return str(samples[0].get("image_storage_key") or "")
    return ""


def _preset_thumb_for_cover(preset: dict[str, Any]) -> str:
    samples = preset.get("samples") or []
    if isinstance(samples, list) and samples and isinstance(samples[0], dict):
        return str(
            samples[0].get("thumb_storage_key")
            or samples[0].get("image_storage_key")
            or ""
        )
    return ""


@router.get("/items/{item_id:path}/binary")
async def get_poster_style_item_binary(
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    if item_id.startswith("user:"):
        raise _http(
            "use_image_api",
            "user library image is served by image API",
            400,
        )
    raw = await _find_preset_item(db, user_id=user.id, item_id=item_id)
    if raw is None:
        raise _http("not_found", "poster style item not found", 404)
    storage_key = _preset_cover_storage_key(raw)
    if not storage_key:
        raise _http("no_cover", "preset has no synced sample image yet", 404)
    return _binary_response(storage_key, request)


@router.get("/items/{item_id:path}/thumb")
async def get_poster_style_item_thumb(
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    if item_id.startswith("user:"):
        raise _http(
            "use_image_api",
            "user library image is served by image API",
            400,
        )
    raw = await _find_preset_item(db, user_id=user.id, item_id=item_id)
    if raw is None:
        raise _http("not_found", "poster style item not found", 404)
    storage_key = _preset_thumb_for_cover(raw) or _preset_cover_storage_key(raw)
    if not storage_key:
        raise _http("no_cover", "preset has no synced sample image yet", 404)
    return _binary_response(storage_key, request)


@router.get("/items/{item_id:path}/samples/{sample_index}")
async def get_poster_style_sample(
    item_id: str,
    sample_index: int,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    if item_id.startswith("user:"):
        # 用户的 sample 走 image API；让前端别走错路。
        raise _http(
            "use_image_api",
            "user library samples are served by image API",
            400,
        )
    raw = await _find_preset_item(db, user_id=user.id, item_id=item_id)
    if raw is None:
        raise _http("not_found", "poster style item not found", 404)
    samples = raw.get("samples") or []
    if (
        not isinstance(samples, list)
        or sample_index < 0
        or sample_index >= len(samples)
    ):
        raise _http("invalid_sample", "sample index out of range", 404)
    sample = samples[sample_index]
    if not isinstance(sample, dict):
        raise _http("invalid_sample", "sample entry invalid", 500)
    storage_key = str(sample.get("image_storage_key") or "")
    if not storage_key:
        raise _http("no_sample", "preset sample has no synced binary yet", 404)
    return _binary_response(storage_key, request)


# ----- 创建 / 编辑 / 删除 -------------------------------------------------


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
    # cover_image_id 必须是当前用户拥有的图；sample_image_ids 同样校验。
    extra_samples = [sid for sid in body.sample_image_ids if sid != body.cover_image_id]
    all_ids = [body.cover_image_id, *extra_samples]
    await _validate_owned_image_ids(db, user_id=user.id, image_ids=all_ids)

    category = _normalize_category(body.category)
    style_tags = _normalize_style_tags(body.style_tags)
    palette = _normalize_palette(body.palette)
    aspects = _normalize_recommended_aspects(body.recommended_aspects)
    item_id = f"user:{new_uuid7()}"
    row = PosterStyleItem(
        id=item_id,
        user_id=user.id,
        source=body.source,
        cover_image_id=body.cover_image_id,
        sample_image_ids=extra_samples,
        title=body.title.strip()[:120],
        category=category,
        mood=_clean_optional_text(body.mood, max_len=120),
        prompt_template=_clean_optional_text(body.prompt_template, max_len=2000),
        palette=palette,
        recommended_aspects=aspects,
        style_tags=style_tags,
        library_folder=_poster_style_folder_for_category(category),
        metadata_jsonb={},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    if body.auto_tag:
        background_tasks.add_task(_run_auto_tag_in_background, user.id, item_id)
    return _item_out_from_row(row)


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
    if not item_id.startswith("user:"):
        raise _http(
            "preset_readonly",
            "preset items are read-only; delete to hide",
            400,
        )
    row = await _find_user_item(db, user_id=user.id, item_id=item_id)
    if row is None:
        raise _http("not_found", "poster style item not found", 404)
    if body.title is not None:
        row.title = body.title.strip()[:120]
    if body.category is not None:
        row.category = _normalize_category(body.category)
        row.library_folder = _poster_style_folder_for_category(row.category)
    if body.mood is not None:
        row.mood = _clean_optional_text(body.mood, max_len=120)
    if body.prompt_template is not None:
        row.prompt_template = _clean_optional_text(body.prompt_template, max_len=2000)
    if body.palette is not None:
        row.palette = _normalize_palette(body.palette)
    if body.recommended_aspects is not None:
        row.recommended_aspects = _normalize_recommended_aspects(body.recommended_aspects)
    if body.style_tags is not None:
        row.style_tags = _normalize_style_tags(body.style_tags)
    await db.commit()
    await db.refresh(row)
    return _item_out_from_row(row)


async def _delete_poster_style_item_for_user(
    db: AsyncSession, *, user_id: str, item_id: str
) -> bool:
    """User: 真删；preset: 当前用户范围 hide。返回是否删除成功。"""
    if item_id.startswith("user:"):
        row = await _find_user_item(db, user_id=user_id, item_id=item_id)
        if row is None:
            return False
        await db.delete(row)
        return True
    raw = await _find_preset_item(db, user_id=user_id, item_id=item_id)
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


@router.delete(
    "/items/{item_id:path}",
    dependencies=[Depends(verify_csrf)],
)
async def delete_poster_style_item(
    item_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    deleted = await _delete_poster_style_item_for_user(
        db, user_id=user.id, item_id=item_id
    )
    if not deleted:
        raise _http("not_found", "poster style item not found", 404)
    await db.commit()
    return {"ok": True}


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
    item_ids = _dedupe_nonempty(body.item_ids)
    deleted = 0
    not_found: list[str] = []
    for item_id in item_ids:
        if await _delete_poster_style_item_for_user(
            db, user_id=user.id, item_id=item_id
        ):
            deleted += 1
        else:
            not_found.append(item_id)
    await db.commit()
    return PosterStyleBatchDeleteOut(deleted=deleted, not_found=not_found)


# ----- 同步预设 -----------------------------------------------------------


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


# ----- 用户生成样图入库 ---------------------------------------------------


def _poster_style_generate_image_params(aspect_ratio: str) -> ImageParamsIn:
    """风格库样图：1 张 / batch；与海报 master 一档质量。"""
    return ImageParamsIn(
        aspect_ratio=aspect_ratio,  # type: ignore[arg-type]
        size_mode="auto",
        count=1,
        fast=False,
        render_quality="high",
        output_format="jpeg",
        output_compression=100,
        background="opaque",
        moderation="low",
    )


def _poster_style_generate_prompt(
    *,
    body: PosterStyleGenerateIn,
    candidate_index: int,
) -> str:
    """生成一张风格样图的 prompt。prompt cache friendly：稳定前缀 + 末尾 user_intent。

    Note: 与 apparel 模特库生成不同——风格库样图允许文字 + 排版表达，无需 4-panel
    contact sheet 约束；只需要呈现"这是一张代表此风格的海报样图"。
    """
    extras: list[str] = []
    if body.prompt_template:
        extras.append(body.prompt_template.strip())
    palette_text = ", ".join(body.palette[:6]) if body.palette else ""
    mood_text = (body.mood or "").strip()
    tag_text = ", ".join(body.style_tags[:6]) if body.style_tags else ""
    return " ".join(
        part
        for part in [
            "Create one stylish poster sample illustrating a single visual style.",
            "The poster should be a self-contained composition representative of the style,",
            "no real product mockups required.",
            "Use plain, generic placeholder shapes or motifs to demonstrate the style,",
            "not specific brand names or logos.",
            f"Style direction: {extras[0]}" if extras else "",
            f"Palette: {palette_text}." if palette_text else "",
            f"Mood: {mood_text}." if mood_text else "",
            f"Style tags: {tag_text}." if tag_text else "",
            f"Variation index: {candidate_index}.",
            f"User intent: {body.prompt.strip()}",
        ]
        if part
    ).strip()


async def _get_or_create_workflow_conversation(
    db: AsyncSession,
    *,
    user: Any,
    title: str,
    workflow_type: str,
) -> Conversation:
    """与 workflows._get_or_create_workflow_conversation 同语义的本地版本。

    [DECISION] 不直接 import workflows.* 以避免循环依赖（workflows.py 体积巨大）。
    """
    conv = Conversation(
        user_id=user.id,
        title=title,
        archived=True,
        default_params={
            "workflow_type": workflow_type,
            "hidden_from_conversations": True,
        },
    )
    db.add(conv)
    await db.flush()
    return conv


async def _create_user_message(
    db: AsyncSession,
    *,
    conv: Conversation,
    text: str,
    attachment_ids: list[str],
    workflow_run_id: str,
    workflow_step_key: str,
) -> Message:
    msg = Message(
        conversation_id=conv.id,
        role=Role.USER.value,
        content={
            "text": text,
            "attachments": [{"image_id": iid} for iid in attachment_ids],
            "workflow_run_id": workflow_run_id,
            "workflow_step_key": workflow_step_key,
        },
        intent=None,
        status=None,
    )
    db.add(msg)
    await db.flush()
    return msg


async def _enqueue_poster_style_generate_tasks(
    *,
    db: AsyncSession,
    user: Any,
    conv: Conversation,
    run: WorkflowRun,
    step: WorkflowStep,
    body: PosterStyleGenerateIn,
) -> tuple[list[str], list[dict[str, Any]]]:
    """入队 N 个 generation task。worker 端按 workflow_meta 识别并写回 step。

    [DECISION] 为避免与 workflows.py 大量耦合，这里复用 messages._create_assistant_task
    来创建 assistant message + Completion/Generation 行。
    """
    from .messages import _create_assistant_task

    task_ids: list[str] = []
    publish_jobs: list[dict[str, Any]] = []
    for idx in range(1, int(body.count) + 1):
        prompt = _poster_style_generate_prompt(body=body, candidate_index=idx)
        user_msg = await _create_user_message(
            db,
            conv=conv,
            text=prompt,
            attachment_ids=[],
            workflow_run_id=run.id,
            workflow_step_key=POSTER_STYLE_GENERATE_STEP_KEY,
        )
        result = await _create_assistant_task(
            db=db,
            user_id=user.id,
            conv=conv,
            user_msg=user_msg,
            intent=Intent.TEXT_TO_IMAGE,
            idempotency_key=f"pstyle:{run.id[:24]}:{idx}"[:64],
            image_params=_poster_style_generate_image_params(body.aspect_ratio),
            chat_params=ChatParamsIn(),
            system_prompt=None,
            attachment_ids=[],
            text=prompt,
        )
        # 把 workflow_meta 写回 Generation.upstream_request 让 worker 识别
        meta = {
            "workflow_run_id": run.id,
            "workflow_type": WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
            "workflow_step_key": POSTER_STYLE_GENERATE_STEP_KEY,
            "workflow_action": POSTER_STYLE_GENERATE_WORKER_ACTION,
            "workflow_candidate_index": idx,
            "workflow_poster_style_title": body.title,
            "workflow_poster_style_category": _normalize_category(body.category),
            "workflow_poster_style_tags": _normalize_style_tags(body.style_tags),
            "workflow_poster_style_palette": _normalize_palette(body.palette),
            "workflow_poster_style_auto_tag": bool(body.auto_tag),
        }
        for generation_id in result.generation_ids:
            gen = await db.get(Generation, generation_id)
            if gen is not None:
                req = dict(gen.upstream_request or {})
                req.update(meta)
                gen.upstream_request = req
        task_ids.extend(result.generation_ids)
        publish_jobs.append(
            {
                "assistant_msg_id": result.assistant_msg.id,
                "outbox_payloads": result.outbox_payloads,
                "outbox_rows": result.outbox_rows,
            }
        )
    step.task_ids = task_ids
    return task_ids, publish_jobs


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
    category = _normalize_category(body.category)
    style_tags = _normalize_style_tags(body.style_tags)
    palette = _normalize_palette(body.palette)
    aspects = _normalize_recommended_aspects(body.recommended_aspects)
    title = body.title.strip()[:120] or "未命名风格"

    conv = await _get_or_create_workflow_conversation(
        db,
        user=user,
        title=title,
        workflow_type=WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
    )
    run = WorkflowRun(
        conversation_id=conv.id,
        user_id=user.id,
        type=WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
        status="running",
        title=title,
        user_prompt=body.prompt[:4000],
        product_image_ids=[],
        current_step=POSTER_STYLE_GENERATE_STEP_KEY,
        quality_mode="standard",
        metadata_jsonb={
            "template": WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
            "poster_style_profile": {
                "title": title,
                "category": category,
                "style_tags": style_tags,
                "palette": palette,
                "recommended_aspects": aspects,
                "mood": _clean_optional_text(body.mood, max_len=120),
                "prompt": body.prompt,
            },
        },
    )
    db.add(run)
    await db.flush()
    step = WorkflowStep(
        workflow_run_id=run.id,
        step_key=POSTER_STYLE_GENERATE_STEP_KEY,
        status="running",
        input_json={
            "title": title,
            "category": category,
            "style_tags": style_tags,
            "palette": palette,
            "recommended_aspects": aspects,
            "mood": _clean_optional_text(body.mood, max_len=120),
            "prompt": body.prompt,
            "prompt_template": _clean_optional_text(body.prompt_template, max_len=2000),
            "aspect_ratio": body.aspect_ratio,
            "count": int(body.count),
            "auto_tag": bool(body.auto_tag),
        },
        output_json={},
    )
    db.add(step)
    await db.flush()
    task_ids, publish_jobs = await _enqueue_poster_style_generate_tasks(
        db=db, user=user, conv=conv, run=run, step=step, body=body
    )
    conv.last_activity_at = _now()
    await db.commit()
    if publish_jobs:
        from ..redis_client import get_redis
        from .messages import _publish_assistant_task

        redis = get_redis()
        for job in publish_jobs:
            await _publish_assistant_task(
                db=db,
                redis=redis,
                user_id=user.id,
                conv_id=conv.id,
                assistant_msg_id=str(job["assistant_msg_id"]),
                outbox_payloads=list(job["outbox_payloads"]),
                outbox_rows=list(job["outbox_rows"]),
            )
    return PosterStyleGenerateOut(
        job_id=run.id,
        workflow_run_id=run.id,
        status="running",
        requested_count=int(body.count),
        task_ids=task_ids,
        created_at=run.created_at,
    )


# ----- 任务列表 -----------------------------------------------------------


def _poster_style_job_status(
    *, step_status: str, requested_count: int, finished_count: int
) -> str:
    if step_status == "failed":
        return "partial" if finished_count > 0 else "failed"
    if step_status in {"succeeded", "completed", "approved", "needs_review"}:
        if requested_count > 0 and finished_count >= requested_count:
            return "succeeded"
        if finished_count > 0:
            return "partial"
        return "succeeded" if step_status == "succeeded" else "failed"
    if step_status == "running":
        return "running"
    return "queued"


async def _job_from_run(
    db: AsyncSession, *, run: WorkflowRun
) -> PosterStyleJobOut:
    step = (
        await db.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.step_key == POSTER_STYLE_GENERATE_STEP_KEY,
            )
        )
    ).scalar_one_or_none()
    inputs: dict[str, Any] = {}
    image_ids: list[str] = []
    requested = 0
    step_status = "queued"
    if step is not None:
        inputs = step.input_json if isinstance(step.input_json, dict) else {}
        image_ids = [iid for iid in (step.image_ids or []) if isinstance(iid, str)]
        requested = max(
            int(inputs.get("count") or 0),
            len(step.task_ids or []),
            len(image_ids),
        )
        step_status = step.status
    finished = len(image_ids)

    error_message: str | None = None
    if step is not None:
        out_json = step.output_json if isinstance(step.output_json, dict) else {}
        error_message = _clean_optional_text(out_json.get("error_message"), max_len=400)
        # 推断失败：所有 task 都失败且没产出图
        if step.task_ids:
            generations = list(
                (
                    await db.execute(
                        select(Generation).where(
                            Generation.id.in_(list(step.task_ids)),
                            Generation.user_id == run.user_id,
                        )
                    )
                ).scalars().all()
            )
            active = [
                g
                for g in generations
                if g.status
                in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
            ]
            failed = [g for g in generations if g.status == GenerationStatus.FAILED.value]
            if failed and not active and finished < requested:
                if step_status == "running":
                    step_status = "failed"
                if error_message is None:
                    msgs = [
                        str(getattr(g, "error_message", "") or "").strip()
                        for g in failed
                    ]
                    error_message = "；".join([m for m in msgs if m])[:400] or "生成失败"

    job_status = _poster_style_job_status(
        step_status=step_status,
        requested_count=requested,
        finished_count=finished,
    )
    saved_item_id: str | None = None
    if image_ids:
        row = (
            await db.execute(
                select(PosterStyleItem.id).where(
                    PosterStyleItem.user_id == run.user_id,
                    PosterStyleItem.cover_image_id.in_(image_ids),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if isinstance(row, str):
            saved_item_id = row
    return PosterStyleJobOut(
        job_id=run.id,
        workflow_run_id=run.id,
        title=str(run.title or "")[:120],
        category=_normalize_category(inputs.get("category")),  # type: ignore[arg-type]
        status=job_status,  # type: ignore[arg-type]
        requested_count=requested,
        finished_count=finished,
        prompt=_clean_optional_text(inputs.get("prompt"), max_len=2000),
        style_tags=_normalize_style_tags(inputs.get("style_tags") or []),
        image_ids=image_ids,
        saved_item_id=saved_item_id,
        error_message=error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
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
    fetch_limit = offset + limit + 1
    runs = list(
        (
            await db.execute(
                select(WorkflowRun)
                .where(
                    WorkflowRun.user_id == user.id,
                    WorkflowRun.deleted_at.is_(None),
                    WorkflowRun.type == WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
                )
                .order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id))
                .limit(fetch_limit)
            )
        ).scalars().all()
    )
    jobs: list[PosterStyleJobOut] = []
    for run in runs:
        jobs.append(await _job_from_run(db, run=run))
    page = jobs[offset : offset + limit]
    return PosterStyleJobsOut(
        items=page,
        limit=limit,
        offset=offset,
        has_more=len(jobs) > offset + limit,
    )


# ----- Vision auto-tag 后台触发 -------------------------------------------


async def _run_auto_tag_in_background(user_id: str, item_id: str) -> None:
    """Background trigger for vision auto-tag.

    [NOTE] 实际 vision 调用在 worker 端的 poster_style_tagging 模块；这里只是把
    item_id 推到一个后台 task，让 worker 拉起 vision provider chain。
    """
    try:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            await _auto_tag_poster_style_item(
                db=session, user_id=user_id, item_id=item_id
            )
    except HTTPException as exc:
        logger.info(
            "poster_style auto_tag background skipped user=%s item=%s status=%s",
            user_id,
            item_id,
            exc.status_code,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "poster_style auto_tag background failed user=%s item=%s err=%s",
            user_id,
            item_id,
            exc,
        )


async def _api_call_poster_style_tagging_upstream(
    db: AsyncSession,
    *,
    image_id: str,
    user_id: str,
) -> dict[str, Any]:
    """API 进程内同步调 vision provider 做风格库自动打标签。

    [DECISION] worker 进程和 api 进程的 sys.path 隔离，api 不能直接 import
    apps.worker.* 的模块（与模特库 ``_api_call_tagging_upstream`` 同样的做法）。
    这里把"读图字节 + provider failover + httpx + JSON 解析"的精简版搬过来，
    失败 graceful（返回 {}），让调用方留默认空字段。
    """
    import base64

    from lumen_core.providers import (
        DEFAULT_LEGACY_PROVIDER_BASE_URL,
        build_effective_provider_config,
        endpoint_kind_allowed,
        resolve_provider_proxy_url,
        weighted_priority_order,
    )

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
        return {}
    storage_key = (image.storage_key or "").strip()
    if not storage_key:
        return {}
    try:
        path = _storage_path(storage_key)
        raw = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "poster_style auto_tag api: read image failed key=%s err=%s",
            storage_key,
            exc,
        )
        return {}
    if not raw:
        return {}
    mime = (
        image.mime
        if isinstance(image.mime, str) and image.mime.startswith("image/")
        else "image/png"
    )
    image_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    spec_providers = get_spec("providers")
    raw_providers = (
        await get_setting(db, spec_providers) if spec_providers else None
    )
    providers, _proxies, _errors = build_effective_provider_config(
        raw_providers=raw_providers,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    providers = [p for p in providers if endpoint_kind_allowed(p, "responses")]
    counters: dict[int, int] = {}
    ordered = weighted_priority_order(providers, counters)
    if not ordered:
        return {}

    instructions = (
        "你是海报风格库自动打标签助手。仔细分析这张海报样图的视觉风格，输出严格 JSON。\n\n"
        "字段（全部必填，无法判断填空串/空数组）：\n"
        "- category：英文小写之一：illustration / 3d / minimal / retro / traditional / photo / other。\n"
        "- style_tags：3-6 个中文短词，每个 ≤ 8 字，聚焦视觉风格特征。\n"
        "    禁止描述具体商品 / 模特 / 文字内容；禁止英文。\n"
        "- mood：≤ 20 字中文，整体情绪关键词。\n"
        "- palette：3-6 个 #RRGGBB 十六进制色彩值。\n"
        "- notes：≤ 60 字中文一句话点评。\n\n"
        "只输出 JSON 对象，不要 Markdown / 代码块 / 解释。字段必须用上述英文 key。"
    )
    body = {
        "model": "gpt-5.4-mini",
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": instructions},
                    {"type": "input_image", "image_url": image_url},
                ],
            }
        ],
        "metadata": {"image_id": image_id, "purpose": "poster_style_tagging"},
        "stream": False,
        "store": False,
        "max_output_tokens": 600,
    }
    last_err: str | None = None
    for provider in ordered:
        try:
            proxy_url = await resolve_provider_proxy_url(provider.proxy)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=25.0, write=25.0, pool=10.0),
                proxy=proxy_url,
            ) as client:
                base = provider.base_url.rstrip("/")
                url = f"{base}/v1/responses" if not base.endswith("/v1") else f"{base}/responses"
                resp = await client.post(
                    url,
                    json=body,
                    headers={
                        "authorization": f"Bearer {provider.api_key}",
                        "content-type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            last_err = f"network: {exc}"
            continue
        if resp.status_code >= 400:
            last_err = f"http {resp.status_code}"
            continue
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError):
            last_err = "bad_json"
            continue
        text_chunks: list[str] = []
        output = payload.get("output") if isinstance(payload, dict) else None
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    t = part.get("text") or part.get("output_text")
                    if isinstance(t, str) and t:
                        text_chunks.append(t)
        ot = payload.get("output_text") if isinstance(payload, dict) else None
        if isinstance(ot, str) and ot:
            text_chunks.append(ot)
        text = "".join(text_chunks).strip()
        return _parse_poster_style_tagging_text(text)
    if last_err is not None:
        logger.info("poster_style auto_tag api: all providers failed err=%s", last_err)
    return {}


def _parse_poster_style_tagging_text(text: str) -> dict[str, Any]:
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        nl = cleaned.find("\n")
        if nl != -1:
            cleaned = cleaned[nl + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    payload: Any = None
    try:
        payload = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        import re as _re

        match = _re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                payload = json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                payload = None
    if not isinstance(payload, dict):
        return {}
    return payload


async def _auto_tag_poster_style_item(
    *,
    db: AsyncSession,
    user_id: str,
    item_id: str,
) -> PosterStyleAutoTagOut:
    """Run vision tagging against one ``poster_style_items`` row.

    Single-row UPDATE under transaction; concurrent auto-tag calls for
    different items don't trample each other. When vision returns nothing
    usable we deliberately leave ``auto_tagged_at`` NULL so the UI can
    distinguish "not yet identified" from "identified but empty".
    """
    row = await _find_user_item(db, user_id=user_id, item_id=item_id)
    if row is None:
        raise _http("not_found", "poster style item not found", 404)
    cover_id = (row.cover_image_id or "").strip()
    if not cover_id:
        raise _http("invalid_item", "poster style item has no cover image", 422)

    raw_payload = await _api_call_poster_style_tagging_upstream(
        db, image_id=cover_id, user_id=user_id
    )

    # 解析字段（容忍多种 key 命名）
    style_tags_raw = (
        raw_payload.get("style_tags")
        or raw_payload.get("tags")
        or raw_payload.get("styleTags")
        or []
    )
    if isinstance(style_tags_raw, str):
        style_tags_iter: list[str] = [style_tags_raw]
    elif isinstance(style_tags_raw, list):
        style_tags_iter = [
            str(t) for t in style_tags_raw if isinstance(t, (str, int, float))
        ]
    else:
        style_tags_iter = []
    style_tags = _normalize_style_tags(style_tags_iter)
    category_raw = raw_payload.get("category")
    category = _normalize_category(category_raw) if isinstance(category_raw, str) else "user_favorites"
    mood = _clean_optional_text(raw_payload.get("mood"), max_len=120)
    palette = _normalize_palette(raw_payload.get("palette") or [])
    notes = _clean_optional_text(raw_payload.get("notes"), max_len=400)

    upstream_signal = bool(
        raw_payload
        and (
            style_tags
            or mood
            or palette
            or notes
            or (category and category != "user_favorites")
        )
    )
    if upstream_signal:
        if style_tags:
            row.style_tags = _normalize_style_tags(
                [*(row.style_tags or []), *style_tags]
            )
        if mood and not row.mood:
            row.mood = mood
        if palette and not (row.palette or []):
            row.palette = palette
        if (
            category
            and category != "user_favorites"
            and _normalize_category(row.category) == "user_favorites"
        ):
            row.category = category
            row.library_folder = _poster_style_folder_for_category(category)
        if notes:
            row.auto_tag_notes = notes
        row.auto_tagged_at = _now()
        await db.commit()
        await db.refresh(row)
    return PosterStyleAutoTagOut(
        item_id=item_id,
        style_tags=style_tags,
        category=category if category != "user_favorites" else None,  # type: ignore[arg-type]
        mood=mood,
        palette=palette,
        notes=notes,
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
        db=db, user_id=user.id, item_id=item_id
    )


# ----- 详情（catch-all：放在最后避免吃掉 /sync-presets / /jobs / /items 路径） -----
#
# 路由顺序至关重要：FastAPI 按注册顺序匹配，``{item_id:path}`` 是贪婪的，
# 任何放在它后面的 ``/items/...`` / ``/jobs`` / ``/sync-presets`` 都会被它先吞掉。
# 必须把它注册在所有更具体路径之后。


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
