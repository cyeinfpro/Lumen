"""管理员存储后端配置与孤儿媒体巡检回收端点（V1.0.x / OPT-10）。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import (
    Image, ImageVariant, ModelLibraryItem, PosterStyleItem, StorageApplyOperation,
)
from lumen_core.model_entities.media_workflows import Video
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    StorageApplyResponseOut, StorageConfigOut, StorageConfigUpdateIn,
    StorageLocalConfigOut, StorageMountStatusOut, StorageSmbConfigOut,
    StorageTestIn, StorageTestResultOut,
)

from ..audit import hash_email, request_ip_hash, write_audit
from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ..config import settings
from ..images.application.deleted_media_references import known_live_media_storage_keys
from ..images.application.storage_maintenance import sweep_orphan_image_files
from ..runtime_settings import get_setting, update_settings
from ..services.storage_apply_dispatch import (
    create_storage_apply_lifespan, latest_storage_apply_record, wake_storage_apply_reconciler,
)
from .admin_storage_apply_files import (
    read_host_fence_floor, read_json as _read_json, stage_lock, stage_storage_apply,
    storage_apply_request_path, storage_apply_result_path, write_atomic as _write_atomic,
)


logger = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get("LUMEN_STORAGE_STATE_DIR", "/var/lib/lumen-storage"))
STATUS_FILE = STATE_DIR / "status.json"
APPLY_REQUESTS_DIR = STATE_DIR / "requests"
APPLY_RESULTS_DIR = STATE_DIR / "results"
APPLY_CLAIM_FILE = STATE_DIR / "apply.claim.json"
LAST_APPLY_FILE = STATE_DIR / "last-apply.json"
TEST_TRIGGER = STATE_DIR / "test.trigger"
TEST_CONF = STATE_DIR / "test.conf"
LAST_TEST_FILE = STATE_DIR / "last-test.json"

DEFAULT_LOCAL_ROOT = "/var/lib/lumen-data"
_DEFAULT_ALLOWED_LOCAL_ROOTS = ("/var/lib/lumen-data", "/srv/lumen-data", "/mnt", "/media")
_FORBIDDEN_LOCAL_ROOTS = frozenset({
    "/", "/etc", "/usr", "/var", "/var/lib", "/srv", "/mnt", "/media",
    "/opt", "/opt/lumen", "/opt/lumendata", "/var/lib/lumen-storage",
})

_TEST_TIMEOUT_SEC = 30.0
_POLL_INTERVAL = 0.4


def _http(code: str, msg: str, http: int = 400, **details) -> HTTPException:
    err: dict = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


# ----- Input normalization -----
# 用户在 UI 容易把 //10.10.10.40 整段塞进 host，或在 share 前后加 /。
# 为了避免在 mount 时拼出 //10.10.10.40//Lumen 这种坏路径，统一在写入 conf 前 normalize。

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-_]*$")
_SHARE_RE = re.compile(r"^[^/\\]+$")


def _normalize_smb_host(raw: str) -> str:
    value = (raw or "").strip()
    # 用户可能填 \\10.10.10.40 / //10.10.10.40 / smb://host
    value = value.removeprefix("\\\\").removeprefix("//")
    if value.lower().startswith("smb://"):
        value = value[6:]
    # 去掉末尾斜杠 + 用户可能不小心带的 share name
    if "/" in value:
        value = value.split("/", 1)[0]
    if "\\" in value:
        value = value.split("\\", 1)[0]
    return value


def _normalize_smb_share(raw: str) -> str:
    return (raw or "").strip().strip("/").strip("\\")


def _normalize_smb_subpath(raw: str) -> str:
    """Always start with single /, no trailing /, no .. traversal."""
    value = (raw or "/").strip().replace("\\", "/")
    while "//" in value:
        value = value.replace("//", "/")
    if not value.startswith("/"):
        value = "/" + value
    if value != "/" and value.endswith("/"):
        value = value.rstrip("/")
    # 拒绝 .. 路径穿越（CIFS 通常不会但防御性）
    if any(part == ".." for part in value.split("/")):
        raise _http("invalid_subpath", "subpath 不能包含 .. 路径", 422)
    return value


def _normalize_local_root(raw: str) -> str:
    value = (raw or "").strip()
    if not value.startswith("/"):
        raise _http("invalid_local_root", "local.root 必须是绝对路径（以 / 开头）", 422)
    lexical = os.path.normpath(value)
    normalized = str(Path(lexical).resolve(strict=False))
    if lexical in _FORBIDDEN_LOCAL_ROOTS or normalized in _FORBIDDEN_LOCAL_ROOTS:
        raise _http("unsafe_local_root", f"local.root 不能使用系统目录：{normalized}", 422)
    allowed_roots = _allowed_local_roots()
    candidate = Path(normalized)
    if not any(candidate == root or root in candidate.parents for root in allowed_roots):
        allowed = ", ".join(str(item) for item in allowed_roots)
        raise _http("local_root_not_allowed", f"local.root 必须位于允许目录下：{allowed}", 422)
    return normalized


def _allowed_local_roots() -> tuple[Path, ...]:
    raw = os.environ.get("LUMEN_STORAGE_ALLOWED_LOCAL_ROOTS", "")
    values = [item.strip() for item in raw.split(":") if item.strip()] or list(_DEFAULT_ALLOWED_LOCAL_ROOTS)
    roots = [
        Path(v).resolve(strict=False)
        for v in values
        if v.startswith("/") and str(Path(v).resolve(strict=False)) != "/"
    ]
    return tuple(dict.fromkeys(roots or [Path(DEFAULT_LOCAL_ROOT)]))


def _validate_smb_inputs(host: str, share: str) -> None:
    if not _HOST_RE.fullmatch(host):
        raise _http("invalid_smb_host", f"SMB host 格式不合法：{host!r}（只能含字母数字、点、连字符、下划线，不要带 //）", 422)
    if not _SHARE_RE.fullmatch(share):
        raise _http("invalid_smb_share", f"SMB share 格式不合法：{share!r}（不能含斜杠）", 422)


async def _load_config(db: AsyncSession) -> StorageConfigOut:
    backend = await get_setting(db, _spec("storage.backend")) or ""
    local_root = (
        await get_setting(db, _spec("storage.local.root")) or DEFAULT_LOCAL_ROOT
    )
    smb_host = await get_setting(db, _spec("storage.smb.host")) or ""
    smb_port_raw = await get_setting(db, _spec("storage.smb.port")) or ""
    try:
        smb_port = int(smb_port_raw) if smb_port_raw else 0
    except ValueError:
        smb_port = 0
    smb_share = await get_setting(db, _spec("storage.smb.share")) or ""
    smb_subpath = await get_setting(db, _spec("storage.smb.subpath")) or "/"
    smb_username = await get_setting(db, _spec("storage.smb.username")) or ""
    smb_password = await get_setting(db, _spec("storage.smb.password")) or ""

    status_data = _read_json(STATUS_FILE)
    status: StorageMountStatusOut | None = None
    if status_data:
        try:
            status = StorageMountStatusOut(
                mode=str(status_data.get("mode", "")),
                mounted=bool(status_data.get("mounted", False)),
                source=str(status_data.get("source", "")),
                fstype=str(status_data.get("fstype", "")),
                target=str(status_data.get("target", "/opt/lumendata")),
                disabled=bool(status_data.get("disabled", False)),
                updated_at=int(status_data.get("updated_at") or 0) or None,
            )
        except (ValueError, TypeError):
            status = None

    return StorageConfigOut(
        backend=backend,
        local=StorageLocalConfigOut(root=local_root),
        smb=StorageSmbConfigOut(
            host=smb_host, port=smb_port, share=smb_share, subpath=smb_subpath,
            username=smb_username, has_password=bool(smb_password),
        ),
        status=status,
        last_apply=await latest_storage_apply_record(db, legacy_result=_read_json(LAST_APPLY_FILE)),
        last_test=_read_json(LAST_TEST_FILE),
    )


def _spec(key: str):
    spec = get_spec(key)
    if spec is None:
        raise RuntimeError(f"missing SettingSpec for {key!r}")
    return spec


def _format_kv_file(content: dict[str, str]) -> str:
    return "\n".join(f"{k}='{(v or '').replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for k, v in content.items()) + "\n"


def _ensure_state_dir() -> None:
    if not STATE_DIR.is_dir():
        raise _http(
            "storage_state_unavailable",
            f"state dir {STATE_DIR} is missing; check docker-compose volume and that lumen-storage-mount.service is installed on host",
            500,
        )


def _clear_stale_trigger(path: Path, *, stale_after: float) -> None:
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return
    except OSError:
        raise _http("storage_state_unavailable", f"cannot inspect pending trigger {path}", 500)
    if age <= stale_after:
        raise _http("storage_operation_pending", "another storage operation is still pending", 409)
    path.unlink(missing_ok=True)


def _build_storage_conf(cfg: StorageConfigOut, smb_password: str) -> str:
    return _format_kv_file({
        "MODE": cfg.backend or "local", "LOCAL_ROOT": cfg.local.root, "SMB_HOST": cfg.smb.host,
        "SMB_PORT": str(cfg.smb.port) if cfg.smb.port else "", "SMB_SHARE": cfg.smb.share,
        "SMB_SUBPATH": cfg.smb.subpath or "/", "SMB_USERNAME": cfg.smb.username, "SMB_PASSWORD": smb_password,
    })


async def _wait_for_call(path: Path, call_id: str, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = _read_json(path)
        if data and data.get("call_id") == call_id and data.get("status"):
            return data
        await asyncio.sleep(_POLL_INTERVAL)
    return None


async def _load_storage_conf_text(db: AsyncSession) -> str:
    cfg = await _load_config(db)
    smb_password = await get_setting(db, _spec("storage.smb.password")) or ""
    return _build_storage_conf(cfg, smb_password)


def _storage_apply_request_path(operation_id: str, fence: int) -> Path:
    return storage_apply_request_path(APPLY_REQUESTS_DIR, operation_id, fence)


def _storage_apply_result_path(operation_id: str, fence: int) -> Path:
    return storage_apply_result_path(APPLY_RESULTS_DIR, operation_id, fence)


def _stage_storage_apply(
    operation_id: str,
    fence: int,
    desired_config_sha256: str,
    conf_text: str,
) -> None:
    _ensure_state_dir()
    stage_storage_apply(
        state_dir=STATE_DIR,
        requests_dir=APPLY_REQUESTS_DIR,
        operation_id=operation_id,
        fence=fence,
        desired_config_sha256=desired_config_sha256,
        conf_text=conf_text,
    )


def _read_storage_host_fence() -> int:
    _ensure_state_dir()
    return read_host_fence_floor(
        claim_path=APPLY_CLAIM_FILE,
        results_dir=APPLY_RESULTS_DIR,
        requests_dir=APPLY_REQUESTS_DIR,
        latest_result_path=LAST_APPLY_FILE,
    )


router = APIRouter(
    prefix="/admin/storage",
    tags=["admin-storage"],
    lifespan=create_storage_apply_lifespan(
        load_conf_text=_load_storage_conf_text,
        stage_operation=_stage_storage_apply,
        read_host_result=lambda operation_id, fence: _read_json(
            _storage_apply_result_path(operation_id, fence)
        ),
        read_host_fence=_read_storage_host_fence,
    ),
)


@router.get("", response_model=StorageConfigOut)
async def get_storage_endpoint(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StorageConfigOut:
    return await _load_config(db)


@router.post("/test", response_model=StorageTestResultOut, dependencies=[Depends(verify_csrf)])
async def test_storage_endpoint(
    body: StorageTestIn, request: Request, admin: AdminUser, db: Annotated[AsyncSession, Depends(get_db)],
) -> StorageTestResultOut:
    _ensure_state_dir()
    password = body.password
    if password == "":
        stored = await get_setting(db, _spec("storage.smb.password"))
        if not stored:
            raise _http("missing_password", "password is required (no saved password to reuse)", 422)
        password = stored
    host = _normalize_smb_host(body.host)
    share = _normalize_smb_share(body.share)
    subpath = _normalize_smb_subpath(body.subpath)
    _validate_smb_inputs(host, share)
    call_id = uuid.uuid4().hex
    fields = {
        "SMB_HOST": host, "SMB_PORT": str(body.port) if body.port else "",
        "SMB_SHARE": share, "SMB_SUBPATH": subpath, "SMB_USERNAME": body.username.strip(), "SMB_PASSWORD": password,
    }
    with stage_lock(STATE_DIR, "test"):
        _clear_stale_trigger(TEST_TRIGGER, stale_after=_TEST_TIMEOUT_SEC + 30)
        _write_atomic(TEST_CONF, _format_kv_file(fields), mode=0o600)
        _write_atomic(TEST_TRIGGER, f"{call_id}\n", mode=0o600)
    result = await _wait_for_call(LAST_TEST_FILE, call_id, _TEST_TIMEOUT_SEC)
    await write_audit(
        db, event_type="admin.storage.test", user_id=admin.id,
        actor_email_hash=hash_email(admin.email), actor_ip_hash=request_ip_hash(request),
        details={"host": body.host, "share": body.share, "result_status": (result or {}).get("status", "pending")},
    )
    await db.commit()
    if result is None:
        return StorageTestResultOut(
            status="pending",
            message=f"测试在 {_TEST_TIMEOUT_SEC:.0f} 秒内没有返回结果，请检查 host 上的 lumen-storage-test.{{path,service}} 是否启用",
            call_id=call_id,
        )
    return StorageTestResultOut(
        status=str(result.get("status", "fail")), message=str(result.get("message", "")),
        tested_at=int(result.get("tested_at") or 0) or None, call_id=call_id,
    )


@router.post("/image-orphans", dependencies=[Depends(verify_csrf)])
async def sweep_image_orphans_endpoint(
    request: Request, admin: AdminUser, db: Annotated[AsyncSession, Depends(get_db)],
    dry_run: bool = Query(default=True), cursor: str | None = Query(default=None, max_length=1024),
    max_files: int = Query(default=500, ge=1, le=5_000), max_entries: int = Query(default=5_000, ge=1, le=50_000),
    max_bytes: int = Query(default=10 * 1024 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024 * 1024),
    max_seconds: float = Query(default=2.0, gt=0, le=30), minimum_age_seconds: float = Query(default=3600.0, ge=0, le=604800),
) -> dict:
    if max_entries < max_files:
        raise _http("invalid_sweep_budget", "max_entries must be greater than or equal to max_files", 422)
    result = await sweep_orphan_image_files(
        db, storage_root=settings.storage_root, dry_run=dry_run, cursor=cursor,
        max_files=max_files, max_entries=max_entries, max_bytes=max_bytes,
        max_seconds=max_seconds, minimum_age_seconds=minimum_age_seconds,
    )
    await write_audit(
        db, event_type="admin.storage.image_orphans", user_id=admin.id,
        actor_email_hash=hash_email(admin.email), actor_ip_hash=request_ip_hash(request),
        details={
            "dry_run": dry_run, "scanned": result.get("scanned", 0), "orphan_count": len(result.get("orphans", [])),
            "deleted": result.get("deleted", 0), "budget_exhausted": result.get("budget_exhausted", False),
            "next_cursor": result.get("next_cursor"),
        },
    )
    await db.commit()
    return result


@router.get("/image-reconcile-quarantine")
async def list_image_reconcile_quarantine(
    _admin: AdminUser, db: Annotated[AsyncSession, Depends(get_db)], limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    rows = (await db.execute(
        select(Image).where(Image.quarantined_at.is_not(None)).order_by(Image.quarantined_at.asc(), Image.id.asc()).limit(limit)
    )).scalars()
    return [
        {
            "image_id": row.id, "artifact_status": row.artifact_status, "reconcile_attempts": row.reconcile_attempts,
            "last_reconcile_error_code": row.last_reconcile_error_code, "last_artifact_error": row.last_artifact_error,
            "last_reconcile_error_at": row.last_reconcile_error_at, "quarantined_at": row.quarantined_at,
        }
        for row in rows
    ]


@router.post("/image-reconcile-quarantine/{image_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_image_reconcile_quarantine(
    image_id: str, request: Request, admin: AdminUser, db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = (await db.execute(
        select(Image).where(Image.id == image_id, Image.quarantined_at.is_not(None)).with_for_update()
    )).scalar_one_or_none()
    if row is None:
        raise _http("image_reconcile_quarantine_not_found", "quarantined image artifact was not found", 404)
    if row.artifact_status not in {"publishing", "ready"}:
        raise _http("image_reconcile_status_not_repairable", "only publishing or ready image artifacts can be retried", 409)
    now = datetime.now(timezone.utc)
    row.reconcile_attempts = 0
    row.reconcile_fence = 0
    row.reconcile_after = now
    row.last_reconcile_error_code = None
    row.last_reconcile_error_at = None
    row.quarantined_at = None
    row.updated_at = now
    await write_audit(
        db, event_type="admin.storage.image_reconcile_retry", user_id=admin.id,
        actor_email_hash=hash_email(admin.email), actor_ip_hash=request_ip_hash(request),
        details={"image_id": row.id, "artifact_status": row.artifact_status},
    )
    await db.commit()
    return {"image_id": row.id, "artifact_status": row.artifact_status, "reconcile_after": row.reconcile_after, "quarantined": False}


@router.put(
    "",
    response_model=StorageApplyResponseOut,
    status_code=202,
    dependencies=[Depends(verify_csrf)],
)
async def put_storage_endpoint(
    body: StorageConfigUpdateIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StorageApplyResponseOut:
    _ensure_state_dir()
    if body.backend not in {"local", "smb"}:
        raise _http("invalid_backend", "backend must be local or smb", 422)

    pairs: list[tuple[str, str]] = [("storage.backend", body.backend)]
    if body.backend == "local":
        if body.local is None:
            raise _http(
                "missing_local", "local config is required when backend=local", 422
            )
        root = _normalize_local_root(body.local.root)
        pairs.append(("storage.local.root", root))
    else:
        if body.smb is None:
            raise _http("missing_smb", "smb config is required when backend=smb", 422)
        smb = body.smb
        host = _normalize_smb_host(smb.host)
        share = _normalize_smb_share(smb.share)
        subpath = _normalize_smb_subpath(smb.subpath)
        _validate_smb_inputs(host, share)
        username = smb.username.strip()
        if not username:
            raise _http("invalid_smb_username", "username 不能为空", 422)
        pairs.extend([
            ("storage.smb.host", host), ("storage.smb.port", str(smb.port) if smb.port else ""),
            ("storage.smb.share", share), ("storage.smb.subpath", subpath), ("storage.smb.username", username),
        ])
        if smb.password != "":
            pairs.append(("storage.smb.password", smb.password))
        else:
            stored = await get_setting(db, _spec("storage.smb.password"))
            if not stored:
                raise _http("missing_password", "password is required (no saved password to reuse)", 422)
        if body.local is not None and body.local.root.strip():
            pairs.append(("storage.local.root", _normalize_local_root(body.local.root)))
    try:
        await update_settings(db, pairs)
    except ValueError as exc:
        await db.rollback()
        raise _http("invalid_request", str(exc), 422) from exc
    conf_text = await _load_storage_conf_text(db)
    operation_id = uuid.uuid4().hex
    operation = StorageApplyOperation(
        id=operation_id, requested_by=admin.id,
        desired_config_sha256=hashlib.sha256(conf_text.encode("utf-8")).hexdigest(),
        status="pending", active_slot=1,
    )
    db.add(operation)
    try:
        await db.flush()
        await write_audit(
            db, event_type="admin.storage.update.requested", user_id=admin.id,
            actor_email_hash=hash_email(admin.email), actor_ip_hash=request_ip_hash(request),
            details={"backend": body.backend, "operation_id": operation_id}, autocommit=False,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _http("storage_operation_pending", "another storage apply is active", 409) from exc
    except Exception:
        await db.rollback()
        raise
    wake_storage_apply_reconciler(request)
    cfg = await _load_config(db)
    return StorageApplyResponseOut(
        config=cfg, call_id=operation_id, status="pending", message="配置请求已持久化，等待 host 确认应用结果。",
    )


def _extract_u_keys(payload: Any, *, depth: int = 0) -> set[str]:
    if depth > 6:
        return set()
    if isinstance(payload, str):
        return {payload} if payload.startswith("u/") else set()
    if isinstance(payload, dict):
        return {k for v in payload.values() for k in _extract_u_keys(v, depth=depth + 1)}
    if isinstance(payload, (list, tuple)):
        return {k for v in payload for k in _extract_u_keys(v, depth=depth + 1)}
    return set()


def _walk_safe_u_files(base: Path, *, max_entries: int) -> list[tuple[str, Path, os.stat_result]]:
    try:
        st = base.lstat()
    except OSError:
        return []
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return []
    out: list[tuple[str, Path, os.stat_result]] = []
    stack: list[tuple[Path, str]] = [(base, "u")]
    seen = 0
    while stack and seen < max_entries:
        cur, prefix = stack.pop()
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name, reverse=True)
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > max_entries:
                break
            if entry.name in {".", "..", ".quarantine"} or entry.name.startswith(".tmp"):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            rel = f"{prefix}/{entry.name}"
            if stat.S_ISDIR(info.st_mode):
                stack.append((Path(entry.path), rel))
            elif stat.S_ISREG(info.st_mode):
                out.append((rel, Path(entry.path), info))
    return out


async def _library_protected_storage_keys(
    db: AsyncSession, candidates: set[str], user_ids: list[str],
) -> set[str]:
    if not candidates or not user_ids:
        return set()
    refs: set[str] = set()
    img_ids: set[str] = set()
    for img_id, meta in (await db.execute(
        select(ModelLibraryItem.image_id, ModelLibraryItem.metadata_jsonb)
        .where(ModelLibraryItem.user_id.in_(user_ids))
    )).all():
        if isinstance(img_id, str) and img_id:
            img_ids.add(img_id)
        refs.update(_extract_u_keys(meta) & candidates)
    for cover_id, samples, meta in (await db.execute(
        select(PosterStyleItem.cover_image_id, PosterStyleItem.sample_image_ids, PosterStyleItem.metadata_jsonb)
        .where(PosterStyleItem.user_id.in_(user_ids))
    )).all():
        if isinstance(cover_id, str) and cover_id:
            img_ids.add(cover_id)
        if isinstance(samples, list):
            img_ids.update(s for s in samples if isinstance(s, str) and s)
        refs.update(_extract_u_keys(meta) & candidates)
    if img_ids:
        ids = sorted(img_ids)
        for sk, meta, mf in (await db.execute(
            select(Image.storage_key, Image.metadata_jsonb, Image.artifact_manifest_jsonb).where(Image.id.in_(ids))
        )).all():
            if isinstance(sk, str) and sk in candidates:
                refs.add(sk)
            refs.update((_extract_u_keys(meta) | _extract_u_keys(mf)) & candidates)
        refs.update(
            k for k in (await db.execute(select(ImageVariant.storage_key).where(ImageVariant.image_id.in_(ids)))).scalars().all()
            if isinstance(k, str) and k in candidates
        )
    return refs


async def _referenced_storage_keys_five_tables(db: AsyncSession, candidates: set[str]) -> set[str]:
    if not candidates:
        return set()
    users = sorted({PurePosixPath(k).parts[1] for k in candidates if len(PurePosixPath(k).parts) >= 3})
    refs = await known_live_media_storage_keys(db, candidates)
    refs.update(await _library_protected_storage_keys(db, candidates, users))
    ordered = sorted(candidates)
    refs.update(
        k for k in (await db.execute(
            select(Video.storage_key).where(Video.deleted_at.is_(None), Video.storage_key.in_(ordered))
        )).scalars().all() if isinstance(k, str) and k in candidates
    )
    return refs


def _quarantine_move_one(
    key: str, path: Path, info: os.stat_result, *, q_root: Path, now_ts: float, manifest: dict[str, Any],
) -> bool:
    try:
        cur = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(cur.st_mode) or (cur.st_dev, cur.st_ino, cur.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_mtime_ns):
        return False
    dest = q_root / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(path, dest)
    except OSError:
        return False
    try:
        os.utime(dest, (now_ts, now_ts))
    except OSError as exc:
        if exc.errno != errno.EPERM:
            logger.debug("quarantine utime skipped for %s: %s", dest, exc)
    manifest[key] = {"quarantined_at": now_ts, "original_mtime": float(info.st_mtime), "size_bytes": int(info.st_size)}
    return True


def _purge_due_quarantine_items(
    eligible: list[tuple[str, Path, os.stat_result]], manifest: dict[str, Any],
) -> tuple[list[str], int, bool]:
    purged_keys: list[str] = []
    purged_bytes = 0
    dirty = False
    for k, p, st in eligible:
        try:
            p.unlink()
            purged_keys.append(k)
            purged_bytes += int(st.st_size)
            manifest.pop(k, None)
            dirty = True
        except OSError:
            continue
    return purged_keys, purged_bytes, dirty


async def _execute_grace_period_orphan_gc(
    db: AsyncSession, *, root: Path, dry_run: bool, grace_sec: float, retain_sec: float, max_files: int, max_entries: int,
) -> dict[str, Any]:
    now_ts = time.time()
    cutoff = now_ts - grace_sec
    all_files = _walk_safe_u_files(root / "u", max_entries=max_entries)
    young = sum(1 for _, _, st in all_files if st.st_mtime >= cutoff)
    mature = [(k, p, st) for k, p, st in all_files if st.st_mtime < cutoff][:max_files]
    refs = await _referenced_storage_keys_five_tables(db, {k for k, _, _ in mature})
    orphans = [(k, p, st) for k, p, st in mature if k not in refs]
    q_root = root / ".quarantine"
    manifest = _read_json(q_root / ".manifest.json") or {}
    q_keys: list[str] = []
    q_bytes = 0
    dirty = False
    if not dry_run and orphans:
        recheck = await _referenced_storage_keys_five_tables(db, {k for k, _, _ in orphans})
        for k, p, st in orphans:
            if k not in recheck and _quarantine_move_one(k, p, st, q_root=q_root, now_ts=now_ts, manifest=manifest):
                q_keys.append(k)
                q_bytes += int(st.st_size)
                dirty = True
    q_files = _walk_safe_u_files(q_root / "u", max_entries=max_entries)
    due = [(k, p, st) for k, p, st in q_files if (now_ts - float((manifest.get(k) or {}).get("quarantined_at", st.st_mtime))) >= retain_sec]
    due_refs = await _referenced_storage_keys_five_tables(db, {k for k, _, _ in due})
    eligible = [(k, p, st) for k, p, st in due if k not in due_refs]
    purged_keys, purged_bytes, p_dirty = _purge_due_quarantine_items(eligible, manifest) if not dry_run else ([], 0, False)
    if (dirty or p_dirty) and not dry_run:
        q_root.mkdir(parents=True, exist_ok=True)
        _write_atomic(q_root / ".manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", mode=0o600)
    return {
        "dry_run": dry_run, "grace_period_seconds": grace_sec, "quarantine_retention_seconds": retain_sec,
        "reference_tables": ["images", "image_variants", "videos", "model_library_items", "poster_style_items"],
        "scanned_files": len(all_files), "young_skipped": young, "mature_candidates": len(mature),
        "referenced_count": len(refs), "orphan_count": len(orphans),
        "orphan_bytes": sum(int(st.st_size) for _, _, st in orphans), "orphan_keys": [k for k, _, _ in orphans],
        "quarantined_count": len(q_keys), "quarantined_bytes": q_bytes, "quarantined_keys": q_keys,
        "quarantined_total_files": len(q_files), "purge_eligible_count": len(eligible),
        "purge_eligible_bytes": sum(int(st.st_size) for _, _, st in eligible),
        "purged_count": len(purged_keys), "purged_bytes": purged_bytes, "purged_keys": purged_keys,
    }


@router.get("/orphan-gc")
async def inspect_storage_orphan_gc_endpoint(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    grace_period_hours: float = Query(default=48.0, ge=0, le=720),
    quarantine_days: float = Query(default=7.0, ge=0, le=90),
    max_files: int = Query(default=500, ge=1, le=5_000),
    max_entries: int = Query(default=10_000, ge=1, le=100_000),
) -> dict[str, Any]:
    if max_entries < max_files:
        raise _http("invalid_sweep_budget", "max_entries must be >= max_files", 422)
    return await _execute_grace_period_orphan_gc(
        db, root=Path(settings.storage_root), dry_run=True,
        grace_sec=grace_period_hours * 3600.0, retain_sec=quarantine_days * 86400.0,
        max_files=max_files, max_entries=max_entries,
    )


@router.post("/orphan-gc", dependencies=[Depends(verify_csrf)])
async def run_storage_orphan_gc_endpoint(
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    dry_run: bool = Query(default=True),
    grace_period_hours: float = Query(default=48.0, ge=0, le=720),
    quarantine_days: float = Query(default=7.0, ge=0, le=90),
    max_files: int = Query(default=500, ge=1, le=5_000),
    max_entries: int = Query(default=10_000, ge=1, le=100_000),
) -> dict[str, Any]:
    if max_entries < max_files:
        raise _http("invalid_sweep_budget", "max_entries must be >= max_files", 422)
    result = await _execute_grace_period_orphan_gc(
        db, root=Path(settings.storage_root), dry_run=dry_run,
        grace_sec=grace_period_hours * 3600.0, retain_sec=quarantine_days * 86400.0,
        max_files=max_files, max_entries=max_entries,
    )
    await write_audit(
        db, event_type="admin.storage.orphan_gc", user_id=admin.id,
        actor_email_hash=hash_email(admin.email), actor_ip_hash=request_ip_hash(request),
        details={
            "dry_run": dry_run, "orphan_count": result["orphan_count"],
            "quarantined_count": result["quarantined_count"], "purged_count": result["purged_count"],
        },
        autocommit=False,
    )
    await db.commit()
    return result


__all__ = ["router"]
