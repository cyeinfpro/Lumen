"""管理员可调系统设置（V1.0 收尾）。

GET /admin/settings 与 PUT /admin/settings；写入仅限 SUPPORTED_SETTINGS 列表里的 key，
type-check int/float 失败即 422。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.runtime_settings import get_spec, parse_value
from lumen_core.schemas import (
    SystemSettingsOut,
    SystemSettingsUpdateIn,
)

from ..audit import hash_email, request_ip_hash, write_audit
from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ..runtime_settings import get_settings_view, update_settings


router = APIRouter(prefix="/admin/settings", tags=["admin-settings"])


def _http(code: str, msg: str, http: int = 400, **details) -> HTTPException:
    err: dict = {"code": code, "message": msg}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


@router.get("", response_model=SystemSettingsOut)
async def get_settings_endpoint(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemSettingsOut:
    items = await get_settings_view(db)
    return SystemSettingsOut(items=items)


@router.put(
    "",
    response_model=SystemSettingsOut,
    dependencies=[Depends(verify_csrf)],
)
async def put_settings_endpoint(
    body: SystemSettingsUpdateIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemSettingsOut:
    # 收 (key, value) 元组先做合法性 + 类型校验
    pairs: list[tuple[str, str]] = []
    invalid: list[dict] = []
    _MAX_VALUE_LEN = 2048
    _MAX_PROVIDERS_LEN = 16384
    for it in body.items:
        spec = get_spec(it.key)
        if spec is None:
            invalid.append({"key": it.key, "reason": "unknown_key"})
            continue
        max_len = _MAX_PROVIDERS_LEN if it.key == "providers" else _MAX_VALUE_LEN
        if len(it.value) > max_len:
            invalid.append(
                {
                    "key": it.key,
                    "reason": "value_too_long",
                    "message": f"value exceeds {max_len} chars",
                }
            )
            continue
        try:
            parse_value(spec, it.value)
        except (TypeError, ValueError) as exc:
            invalid.append(
                {
                    "key": it.key,
                    "reason": f"invalid_{spec.parser.__name__}",
                    "message": str(exc),
                }
            )
            continue
        pairs.append((it.key, it.value))

    if invalid:
        raise _http(
            "invalid_request",
            "one or more setting items are invalid",
            422,
            errors=invalid,
        )

    try:
        await update_settings(db, pairs)
    except ValueError as exc:
        await db.rollback()
        raise _http("invalid_request", str(exc), 422)
    await write_audit(
        db,
        event_type="admin.settings.update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"keys": [k for k, _ in pairs]},
    )
    await db.commit()

    items = await get_settings_view(db)
    return SystemSettingsOut(items=items)


__all__ = ["router"]
