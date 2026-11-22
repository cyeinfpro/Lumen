"""Admin-only, write-only credentials for the private voice workbench."""

from __future__ import annotations

import asyncio
import json
from types import MappingProxyType
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.voice_config import (
    VOICE_WORKER_READY_KEY,
    VoiceProviderConfig,
    VoiceProviderConfigurationError,
    change_voice_provider_config,
    get_voice_provider_state,
)

from ..arq_pool import get_arq_pool
from ..audit import hash_email, request_ip_hash, write_audit
from ..config import settings
from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ..redis_client import get_redis

router = APIRouter(prefix="/admin/voice-provider", tags=["admin-voice-provider"])
DB = Annotated[AsyncSession, Depends(get_db)]
_NO_STORE = MappingProxyType({"Cache-Control": "no-store"})
_MAX_SETTINGS_REQUEST_BYTES = 4096
_ERRORS = MappingProxyType({
    "VOICE_PROVIDER_INVALID_REQUEST": (422, "请检查声音服务设置"),
    "VOICE_PROVIDER_INVALID_REGION": (422, "请选择有效的百炼地域"),
    "VOICE_PROVIDER_INVALID_WORKSPACE": (422, "工作空间 ID 格式不正确"),
    "VOICE_PROVIDER_INVALID_KEY": (422, "请输入有效的百炼 API Key"),
    "VOICE_PROVIDER_KEY_REQUIRED": (422, "修改地域或工作空间时，请同时输入匹配的百炼 API Key"),
    "VOICE_PROVIDER_CONFIG_INVALID": (503, "声音服务设置暂不可用"),
    "VOICE_PROVIDER_CRYPTO_UNAVAILABLE": (503, "声音服务密钥暂无法安全保存"),
})


def _http(exc: VoiceProviderConfigurationError) -> HTTPException:
    status, message = _ERRORS.get(exc.code, (503, "声音服务设置暂不可用"))
    return HTTPException(status, detail={"error": {"code": exc.code, "message": message}}, headers=_NO_STORE)


async def _present(config: VoiceProviderConfig):
    ready = False
    if config.configured:
        try:
            marker = await asyncio.wait_for(get_redis().get(VOICE_WORKER_READY_KEY), timeout=1)
            ready = marker in (config.ready_marker, config.ready_marker.encode("ascii"))
        except Exception:
            pass
    return {
        "configured": config.configured, "region": config.region,
        "workspace_id": config.workspace_id, "revision": config.revision, "ready": ready,
    }


async def _input(request: Request) -> dict[str, str | None]:
    # Avoid FastAPI/Pydantic validation payloads that echo the offending key,
    # including for wrong JSON types, unsupported fields and overlong values.
    try:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > _MAX_SETTINGS_REQUEST_BYTES:
                raise ValueError
            raw.extend(chunk)
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) - {"api_key", "region", "workspace_id"}:
            raise ValueError
        if not value or any(type(v) is not str for v in value.values()):
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise _http(VoiceProviderConfigurationError("VOICE_PROVIDER_INVALID_REQUEST")) from None
    return value


@router.get("")
async def get_config(admin: AdminUser, db: DB, response: Response):
    response.headers.update(_NO_STORE)
    try:
        state = await get_voice_provider_state(db)
    except VoiceProviderConfigurationError as exc:
        raise _http(exc) from None
    return await _present(state)


async def _change(request: Request, response: Response, admin: AdminUser, db: DB, **kwargs):
    response.headers.update(_NO_STORE)
    try:
        config = await change_voice_provider_config(
            db, master_secret=settings.byok_api_key_master_secret, **kwargs,
        )
    except VoiceProviderConfigurationError as exc:
        raise _http(exc) from None
    await write_audit(
        db, event_type="admin.voice_provider.changed", user_id=str(admin.id),
        actor_email_hash=hash_email(admin.email), actor_ip_hash=request_ip_hash(request),
        details={"configured": config.configured, "region": config.region,
                 "revision": config.revision},
        autocommit=False,
    )
    await db.commit()
    async def wake() -> None:
        pool = await get_arq_pool()
        await pool.enqueue_job("process_voice_work")

    try:
        await asyncio.wait_for(wake(), timeout=1)
    except Exception:
        pass  # periodic worker heartbeat still observes the committed revision
    return await _present(config)


@router.put("", dependencies=[Depends(verify_csrf)])
async def save_config(request: Request, response: Response, admin: AdminUser, db: DB):
    return await _change(request, response, admin, db, **await _input(request))


@router.delete("", dependencies=[Depends(verify_csrf)])
async def delete_config(request: Request, response: Response, admin: AdminUser, db: DB):
    return await _change(request, response, admin, db, remove_key=True)
