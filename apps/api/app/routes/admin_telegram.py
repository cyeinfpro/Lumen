"""管理后台「Telegram 机器人」专用路由。

设置数据走 system_settings（telegram.* 系列 key）和已有的 /admin/settings；
本文件只负责「触发 bot 重启」。

机制：bot 进程订阅 Redis pubsub `admin:tgbot:control`，收到 `restart` 后 clean exit；
systemd unit 用 Restart=always 自动拉起。无需 systemctl 权限。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ..redis_client import get_redis
from ._admin_common import write_admin_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/telegram", tags=["admin-telegram"])

_CONTROL_CHANNEL = "admin:tgbot:control"


class RestartOut(BaseModel):
    ok: bool
    receivers: int  # Redis publish 返回的 subscriber 数；0 表示 bot 进程不在线


@router.post("/restart", response_model=RestartOut, dependencies=[Depends(verify_csrf)])
async def restart_bot(
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RestartOut:
    redis = get_redis()
    try:
        receivers = int(await redis.publish(_CONTROL_CHANNEL, b"restart"))
    except Exception as exc:  # noqa: BLE001
        logger.error("publish restart failed: %s", exc)
        receivers = 0
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.telegram.restart",
        details={"receivers": receivers},
    )
    await db.commit()
    logger.info("admin restart tgbot by user=%s receivers=%d", admin.id, receivers)
    return RestartOut(ok=True, receivers=receivers)
