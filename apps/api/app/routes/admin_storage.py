"""管理员存储后端配置端点（V1.0.x）。

GET /admin/storage          — desired 配置 + mount 状态 + durable apply/test 结果
POST /admin/storage/test    — 测试 SMB 连通性（不切真实挂载，临时挂 /var/lib/lumen-storage/scratch）
PUT /admin/storage          — 持久化 desired operation，后台触发 host apply

写入流程：
  1. 同一事务写 storage.* desired settings、operation row 和 audit。
  2. durable commit 后只唤醒 router-owned reconciler。
  3. reconciler 用 owner + lease + fence claim operation，再写不可变 request。
  4. host 单调 claim fence、激活配置并写同 identity/fence 的 terminal result。
  5. reconciler 只接收当前 operation/fence 的 result。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, StorageApplyOperation
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    StorageApplyResponseOut,
    StorageConfigOut,
    StorageConfigUpdateIn,
    StorageLocalConfigOut,
    StorageMountStatusOut,
    StorageSmbConfigOut,
    StorageTestIn,
    StorageTestResultOut,
)

from ..audit import hash_email, request_ip_hash, write_audit
from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ..config import settings
from ..images.application.storage_maintenance import sweep_orphan_image_files
from ..runtime_settings import get_setting, update_settings
from ..services.storage_apply_dispatch import (
    create_storage_apply_lifespan,
    latest_storage_apply_record,
    wake_storage_apply_reconciler,
)
from .admin_storage_apply_files import (
    read_host_fence_floor,
    read_json as _read_json,
    stage_lock,
    stage_storage_apply,
    storage_apply_request_path,
    storage_apply_result_path,
    write_atomic as _write_atomic,
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
_DEFAULT_ALLOWED_LOCAL_ROOTS = (
    "/var/lib/lumen-data",
    "/srv/lumen-data",
    "/mnt",
    "/media",
)
_FORBIDDEN_LOCAL_ROOTS = frozenset(
    {
        "/",
        "/etc",
        "/usr",
        "/var",
        "/var/lib",
        "/srv",
        "/mnt",
        "/media",
        "/opt",
        "/opt/lumen",
        "/opt/lumendata",
        "/var/lib/lumen-storage",
    }
)

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
        raise _http(
            "invalid_local_root",
            "local.root 必须是绝对路径（以 / 开头）",
            422,
        )
    lexical = os.path.normpath(value)
    normalized = str(Path(lexical).resolve(strict=False))
    if lexical in _FORBIDDEN_LOCAL_ROOTS or normalized in _FORBIDDEN_LOCAL_ROOTS:
        raise _http(
            "unsafe_local_root",
            f"local.root 不能使用系统目录：{normalized}",
            422,
        )
    allowed_roots = _allowed_local_roots()
    candidate = Path(normalized)
    if not any(
        candidate == root or root in candidate.parents for root in allowed_roots
    ):
        allowed = ", ".join(str(item) for item in allowed_roots)
        raise _http(
            "local_root_not_allowed",
            f"local.root 必须位于允许目录下：{allowed}",
            422,
        )
    return normalized


def _allowed_local_roots() -> tuple[Path, ...]:
    raw = os.environ.get("LUMEN_STORAGE_ALLOWED_LOCAL_ROOTS", "")
    values = [item.strip() for item in raw.split(":") if item.strip()]
    if not values:
        values = list(_DEFAULT_ALLOWED_LOCAL_ROOTS)
    roots: list[Path] = []
    for value in values:
        if not value.startswith("/"):
            continue
        root = Path(value).resolve(strict=False)
        if str(root) == "/":
            continue
        roots.append(root)
    if not roots:
        roots.append(Path(DEFAULT_LOCAL_ROOT))
    return tuple(dict.fromkeys(roots))


def _validate_smb_inputs(host: str, share: str) -> None:
    if not _HOST_RE.fullmatch(host):
        raise _http(
            "invalid_smb_host",
            f"SMB host 格式不合法：{host!r}（只能含字母数字、点、连字符、下划线，不要带 //）",
            422,
        )
    if not _SHARE_RE.fullmatch(share):
        raise _http(
            "invalid_smb_share",
            f"SMB share 格式不合法：{share!r}（不能含斜杠）",
            422,
        )


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
            host=smb_host,
            port=smb_port,
            share=smb_share,
            subpath=smb_subpath,
            username=smb_username,
            has_password=bool(smb_password),
        ),
        status=status,
        last_apply=await latest_storage_apply_record(
            db,
            legacy_result=_read_json(LAST_APPLY_FILE),
        ),
        last_test=_read_json(LAST_TEST_FILE),
    )


def _spec(key: str):
    spec = get_spec(key)
    if spec is None:
        # Defensive: someone removed the key from runtime_settings.SUPPORTED_SETTINGS
        # while admin_storage still references it.
        raise RuntimeError(f"missing SettingSpec for {key!r}")
    return spec


def _format_kv_file(content: dict[str, str]) -> str:
    """KEY='value' lines, single-quoted with escaping so bash `source` round-trips.

    `'` inside the value becomes `'\\''` (close quote, escaped single, reopen).
    """
    lines = []
    for k, v in content.items():
        escaped = (v or "").replace("'", "'\\''")
        lines.append(f"{k}='{escaped}'")
    return "\n".join(lines) + "\n"


def _ensure_state_dir() -> None:
    if not STATE_DIR.is_dir():
        raise _http(
            "storage_state_unavailable",
            f"state dir {STATE_DIR} is missing; check docker-compose volume "
            f"and that lumen-storage-mount.service is installed on host",
            500,
        )


def _clear_stale_trigger(path: Path, *, stale_after: float) -> None:
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return
    except OSError:
        raise _http(
            "storage_state_unavailable",
            f"cannot inspect pending trigger {path}",
            500,
        )
    if age <= stale_after:
        raise _http(
            "storage_operation_pending",
            "another storage operation is still pending",
            409,
        )
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _build_storage_conf(cfg: StorageConfigOut, smb_password: str) -> str:
    return _format_kv_file(
        {
            "MODE": cfg.backend or "local",
            "LOCAL_ROOT": cfg.local.root,
            "SMB_HOST": cfg.smb.host,
            # 0 / 空 → 让 mount.cifs 走默认 445；其余值由脚本拼到 -o port=
            "SMB_PORT": str(cfg.smb.port) if cfg.smb.port else "",
            "SMB_SHARE": cfg.smb.share,
            "SMB_SUBPATH": cfg.smb.subpath or "/",
            "SMB_USERNAME": cfg.smb.username,
            "SMB_PASSWORD": smb_password,
        }
    )


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


@router.post(
    "/test",
    response_model=StorageTestResultOut,
    dependencies=[Depends(verify_csrf)],
)
async def test_storage_endpoint(
    body: StorageTestIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StorageTestResultOut:
    _ensure_state_dir()
    password = body.password
    if password == "":
        stored = await get_setting(db, _spec("storage.smb.password"))
        if not stored:
            raise _http(
                "missing_password",
                "password is required (no saved password to reuse)",
                422,
            )
        password = stored

    host = _normalize_smb_host(body.host)
    share = _normalize_smb_share(body.share)
    subpath = _normalize_smb_subpath(body.subpath)
    _validate_smb_inputs(host, share)

    call_id = uuid.uuid4().hex
    fields = {
        "SMB_HOST": host,
        # 0 → 走默认 445；脚本检测空字符串就不加 -o port=
        "SMB_PORT": str(body.port) if body.port else "",
        "SMB_SHARE": share,
        "SMB_SUBPATH": subpath,
        "SMB_USERNAME": body.username.strip(),
        "SMB_PASSWORD": password,
    }
    with stage_lock(STATE_DIR, "test"):
        _clear_stale_trigger(TEST_TRIGGER, stale_after=_TEST_TIMEOUT_SEC + 30)
        _write_atomic(TEST_CONF, _format_kv_file(fields), mode=0o600)
        _write_atomic(TEST_TRIGGER, f"{call_id}\n", mode=0o600)

    result = await _wait_for_call(LAST_TEST_FILE, call_id, _TEST_TIMEOUT_SEC)

    await write_audit(
        db,
        event_type="admin.storage.test",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "host": body.host,
            "share": body.share,
            "result_status": (result or {}).get("status", "pending"),
        },
    )
    await db.commit()

    if result is None:
        return StorageTestResultOut(
            status="pending",
            message=(
                f"测试在 {_TEST_TIMEOUT_SEC:.0f} 秒内没有返回结果，"
                "请检查 host 上的 lumen-storage-test.{path,service} 是否启用"
            ),
            call_id=call_id,
        )
    return StorageTestResultOut(
        status=str(result.get("status", "fail")),
        message=str(result.get("message", "")),
        tested_at=int(result.get("tested_at") or 0) or None,
        call_id=call_id,
    )


@router.post("/image-orphans", dependencies=[Depends(verify_csrf)])
async def sweep_image_orphans_endpoint(
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    dry_run: bool = Query(default=True),
    cursor: str | None = Query(default=None, max_length=1024),
    max_files: int = Query(default=500, ge=1, le=5_000),
    max_entries: int = Query(default=5_000, ge=1, le=50_000),
    max_bytes: int = Query(
        default=10 * 1024 * 1024 * 1024,
        ge=1,
        le=1024 * 1024 * 1024 * 1024,
    ),
    max_seconds: float = Query(default=2.0, gt=0, le=30),
    minimum_age_seconds: float = Query(default=3600.0, ge=0, le=604800),
) -> dict:
    if max_entries < max_files:
        raise _http(
            "invalid_sweep_budget",
            "max_entries must be greater than or equal to max_files",
            422,
        )
    result = await sweep_orphan_image_files(
        db,
        storage_root=settings.storage_root,
        dry_run=dry_run,
        cursor=cursor,
        max_files=max_files,
        max_entries=max_entries,
        max_bytes=max_bytes,
        max_seconds=max_seconds,
        minimum_age_seconds=minimum_age_seconds,
    )
    await write_audit(
        db,
        event_type="admin.storage.image_orphans",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "dry_run": dry_run,
            "scanned": result.get("scanned", 0),
            "orphan_count": len(result.get("orphans", [])),
            "deleted": result.get("deleted", 0),
            "budget_exhausted": result.get("budget_exhausted", False),
            "next_cursor": result.get("next_cursor"),
        },
    )
    await db.commit()
    return result


@router.get("/image-reconcile-quarantine")
async def list_image_reconcile_quarantine(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    rows = (
        await db.execute(
            select(Image)
            .where(Image.quarantined_at.is_not(None))
            .order_by(Image.quarantined_at.asc(), Image.id.asc())
            .limit(limit)
        )
    ).scalars()
    return [
        {
            "image_id": row.id,
            "artifact_status": row.artifact_status,
            "reconcile_attempts": row.reconcile_attempts,
            "last_reconcile_error_code": row.last_reconcile_error_code,
            "last_artifact_error": row.last_artifact_error,
            "last_reconcile_error_at": row.last_reconcile_error_at,
            "quarantined_at": row.quarantined_at,
        }
        for row in rows
    ]


@router.post(
    "/image-reconcile-quarantine/{image_id}/retry",
    dependencies=[Depends(verify_csrf)],
)
async def retry_image_reconcile_quarantine(
    image_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = (
        await db.execute(
            select(Image)
            .where(
                Image.id == image_id,
                Image.quarantined_at.is_not(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http(
            "image_reconcile_quarantine_not_found",
            "quarantined image artifact was not found",
            404,
        )
    if row.artifact_status not in {"publishing", "ready"}:
        raise _http(
            "image_reconcile_status_not_repairable",
            "only publishing or ready image artifacts can be retried",
            409,
        )
    now = datetime.now(timezone.utc)
    row.reconcile_attempts = 0
    row.reconcile_fence = 0
    row.reconcile_after = now
    row.last_reconcile_error_code = None
    row.last_reconcile_error_at = None
    row.quarantined_at = None
    row.updated_at = now
    await write_audit(
        db,
        event_type="admin.storage.image_reconcile_retry",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "image_id": row.id,
            "artifact_status": row.artifact_status,
        },
    )
    await db.commit()
    return {
        "image_id": row.id,
        "artifact_status": row.artifact_status,
        "reconcile_after": row.reconcile_after,
        "quarantined": False,
    }


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
        pairs.extend(
            [
                ("storage.smb.host", host),
                # smb.port == 0 表示用默认 445，存空字符串
                ("storage.smb.port", str(smb.port) if smb.port else ""),
                ("storage.smb.share", share),
                ("storage.smb.subpath", subpath),
                ("storage.smb.username", username),
            ]
        )
        if smb.password != "":
            pairs.append(("storage.smb.password", smb.password))
        else:
            stored = await get_setting(db, _spec("storage.smb.password"))
            if not stored:
                raise _http(
                    "missing_password",
                    "password is required (no saved password to reuse)",
                    422,
                )
        # local.root 在切到 SMB 时也可一并保存（让用户切回时能用回先前的本地路径）
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
        id=operation_id,
        requested_by=admin.id,
        desired_config_sha256=hashlib.sha256(conf_text.encode("utf-8")).hexdigest(),
        status="pending",
        active_slot=1,
    )
    db.add(operation)
    try:
        # Flush before audit so a concurrent active operation maps cleanly to
        # 409 instead of being wrapped as an audit persistence failure.
        await db.flush()
        await write_audit(
            db,
            event_type="admin.storage.update.requested",
            user_id=admin.id,
            actor_email_hash=hash_email(admin.email),
            actor_ip_hash=request_ip_hash(request),
            details={
                "backend": body.backend,
                "operation_id": operation_id,
            },
            autocommit=False,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _http(
            "storage_operation_pending",
            "another storage apply is active",
            409,
        ) from exc
    except Exception:
        await db.rollback()
        raise

    # This call never performs host I/O. If the process exits here, startup and
    # periodic scans still find the committed pending operation.
    wake_storage_apply_reconciler(request)
    cfg = await _load_config(db)
    return StorageApplyResponseOut(
        config=cfg,
        call_id=operation_id,
        status="pending",
        message="配置请求已持久化，等待 host 确认应用结果。",
    )


__all__ = ["router"]
