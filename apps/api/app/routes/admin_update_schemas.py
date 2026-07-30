"""Request and response schemas for admin updates."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class UpdateTriggerOut(BaseModel):
    accepted: bool
    pid: int | None = None
    unit: str | None = None
    started_at: datetime
    proxy_name: str | None = None
    log_path: str
    note: str
    target_tag: str | None = None
    idempotency_key: str | None = None
    replayed: bool = False


class UpdateTriggerIn(BaseModel):
    target_tag: str | None = None
    force_redeploy: bool = False
    channel: str | None = None
    confirm_update: bool = False
    confirmed_target_tag: str | None = None
