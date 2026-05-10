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
import json
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
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
from ..runtime_settings import get_setting, update_settings


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/storage", tags=["admin-storage"])

STATE_DIR = Path(os.environ.get("LUMEN_STORAGE_STATE_DIR", "/var/lib/lumen-storage"))
STORAGE_CONF = STATE_DIR / "storage.conf"
STATUS_FILE = STATE_DIR / "status.json"
APPLY_TRIGGER = STATE_DIR / "apply.trigger"
APPLY_ENV = STATE_DIR / "apply.env"
LAST_APPLY_FILE = STATE_DIR / "last-apply.json"
TEST_TRIGGER = STATE_DIR / "test.trigger"
TEST_ENV = STATE_DIR / "test.env"
TEST_CONF = STATE_DIR / "test.conf"
LAST_TEST_FILE = STATE_DIR / "last-test.json"

DEFAULT_LOCAL_ROOT = "/var/lib/lumen-data"

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
    # collapse //
    while "//" in value:
        value = value.replace("//", "/")
    # 不允许末尾 /，除非根
    if len(value) > 1 and value.endswith("/"):
        value = value.rstrip("/")
    return value


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
    local_root = await get_setting(db, _spec("storage.local.root")) or DEFAULT_LOCAL_ROOT
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


def _build_storage_conf(cfg: StorageConfigOut, smb_password: str) -> str:
    return _format_kv_file({
        "MODE": cfg.backend or "local",
        "LOCAL_ROOT": cfg.local.root,
        "SMB_HOST": cfg.smb.host,
        # 0 / 空 → 让 mount.cifs 走默认 445；其余值由脚本拼到 -o port=
        "SMB_PORT": str(cfg.smb.port) if cfg.smb.port else "",
        "SMB_SHARE": cfg.smb.share,
        "SMB_SUBPATH": cfg.smb.subpath or "/",
        "SMB_USERNAME": cfg.smb.username,
        "SMB_PASSWORD": smb_password,
    })


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
    _write_atomic(TEST_CONF, _format_kv_file(fields), mode=0o600)
    _write_atomic(TEST_ENV, f"LUMEN_STORAGE_TEST_CALL_ID={call_id}\n", mode=0o600)
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
            raise _http("missing_local", "local config is required when backend=local", 422)
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
            ("storage.smb.host", host),
            # smb.port == 0 表示用默认 445，存空字符串
            ("storage.smb.port", str(smb.port) if smb.port else ""),
            ("storage.smb.share", share),
            ("storage.smb.subpath", subpath),
            ("storage.smb.username", username),
        ])
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
    _write_atomic(STORAGE_CONF, conf_text, mode=0o660)

    call_id = uuid.uuid4().hex
    _write_atomic(APPLY_ENV, f"LUMEN_STORAGE_APPLY_CALL_ID={call_id}\n", mode=0o600)
    _write_atomic(APPLY_TRIGGER, f"{call_id}\n", mode=0o600)

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
