"""管理员存储后端配置端点（V1.0.x）。

GET /admin/storage          — 当前后端配置 + mount 状态 + 最近一次 apply/test 结果
POST /admin/storage/test    — 测试 SMB 连通性（不切真实挂载，临时挂 /var/lib/lumen-storage/scratch）
PUT /admin/storage          — 应用配置（写 conf + 触发 host 上的 lumen-storage-apply.service）

写入流程：
  1. 校验入参 + 写 storage.* keys 到 SystemSetting（password 留空 = 保留旧值）
  2. 把当前完整配置写到 /var/lib/lumen-storage/storage.conf
     （host 与 lumen-api 通过 docker bind 双向共享这个目录）
  3. 写 /var/lib/lumen-storage/apply.trigger
     （PathChanged 触发 host 上的 lumen-storage-apply.service —— PID 1 启动，绕过容器 sandbox）
  4. 等 ~5 秒看是否能拿到结果；apply 流程会 docker stop lumen-api，所以
     这个连接很可能在等结果前被断开。返回 202 风格的 pending 状态，UI 重连后
     poll GET /admin/storage 比对 last_apply.call_id 知道是否完成。
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

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
from .images import sweep_orphan_image_files
from ..runtime_settings import get_setting, update_settings


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/storage", tags=["admin-storage"])

STATE_DIR = Path(os.environ.get("LUMEN_STORAGE_STATE_DIR", "/var/lib/lumen-storage"))
STORAGE_CONF = STATE_DIR / "storage.conf"
STATUS_FILE = STATE_DIR / "status.json"
APPLY_TRIGGER = STATE_DIR / "apply.trigger"
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
_FORBIDDEN_LOCAL_ROOTS = {
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

# Apply 流程会 docker stop lumen-api 自身，不能等太久；UI 走 polling 模式补全。
_APPLY_INLINE_WAIT_SEC = 5.0
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
    if not any(candidate == root or root in candidate.parents for root in allowed_roots):
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


def _read_json(path: Path) -> dict | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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
        last_apply=_read_json(LAST_APPLY_FILE),
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


def _write_atomic(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            # CIFS forceuid mounts can EPERM on chmod; STATE_DIR isn't on CIFS but
            # tolerate to avoid future surprises.
            pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _ensure_state_dir() -> None:
    if not STATE_DIR.is_dir():
        raise _http(
            "storage_state_unavailable",
            f"state dir {STATE_DIR} is missing; check docker-compose volume "
            f"and that lumen-storage-mount.service is installed on host",
            500,
        )


@contextmanager
def _stage_lock(name: str) -> Iterator[None]:
    lock_path = STATE_DIR / f".{name}.stage.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    with _stage_lock("test"):
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
) -> dict:
    result = await sweep_orphan_image_files(
        db,
        storage_root=settings.storage_root,
        dry_run=dry_run,
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
        },
    )
    await db.commit()
    return result


@router.put(
    "",
    response_model=StorageApplyResponseOut,
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
        raise _http("invalid_request", str(exc), 422)

    cfg = await _load_config(db)
    smb_password = await get_setting(db, _spec("storage.smb.password")) or ""
    conf_text = _build_storage_conf(cfg, smb_password)
    call_id = uuid.uuid4().hex
    try:
        with _stage_lock("apply"):
            _clear_stale_trigger(APPLY_TRIGGER, stale_after=15 * 60)
            _write_atomic(STORAGE_CONF, conf_text, mode=0o660)
            _write_atomic(APPLY_TRIGGER, f"{call_id}\n", mode=0o600)
    except HTTPException:
        await db.rollback()
        raise

    await write_audit(
        db,
        event_type="admin.storage.update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"backend": body.backend, "call_id": call_id},
    )
    await db.commit()

    # Apply 流程会 docker stop lumen-api，所以可能在拿到结果前 connection 就断了。
    # 短等一下：如果在容器被停之前结果回来了（fast path 或失败），返回带 status；
    # 否则返回 pending，UI 自己 poll GET /admin/storage 看 last_apply.call_id。
    result = await _wait_for_call(LAST_APPLY_FILE, call_id, _APPLY_INLINE_WAIT_SEC)
    cfg = await _load_config(db)
    if result is None:
        return StorageApplyResponseOut(
            config=cfg,
            call_id=call_id,
            status="pending",
            message=(
                "配置已写入，挂载切换正在后台执行（约 30 秒）。"
                "API 重启期间页面会短暂无响应，请稍候再刷新。"
            ),
        )
    return StorageApplyResponseOut(
        config=cfg,
        call_id=call_id,
        status=str(result.get("status", "pending")),
        message=str(result.get("message", "")),
    )


__all__ = ["router"]
