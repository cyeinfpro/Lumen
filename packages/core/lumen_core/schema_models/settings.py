"""Settings Pydantic contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# ---------- Invite Links（V1.0 收尾） ----------


class InviteLinkOut(BaseModel):
    id: str
    token: str
    url: str
    email: str | None
    role: str
    expires_at: datetime | None
    used_at: datetime | None
    used_by_email: str | None
    revoked_at: datetime | None
    created_at: datetime


class InviteLinkPublicOut(BaseModel):
    token: str
    email: str | None
    role: str
    expires_at: datetime | None
    used: bool
    valid: bool
    invalid_reason: str | None = None  # expired/used/revoked/not_found


# ---------- System Settings（V1.0 收尾） ----------


class SystemSettingItem(BaseModel):
    key: str
    value: str | None
    has_value: bool
    is_sensitive: bool
    description: str


class SystemSettingsOut(BaseModel):
    items: list[SystemSettingItem]


class SystemSettingsUpdateItem(BaseModel):
    key: str
    value: str  # 空字符串 = 删除该 key（fallback 到 env）


class SystemSettingsUpdateIn(BaseModel):
    items: list[SystemSettingsUpdateItem]


# ---------- Storage 后端（local / smb 切换） ----------


class StorageMountStatusOut(BaseModel):
    mode: str = ""  # "local" | "smb" | ""
    mounted: bool = False
    source: str = ""
    fstype: str = ""
    target: str = "/opt/lumendata"
    disabled: bool = False
    updated_at: int | None = None


class StorageLocalConfigOut(BaseModel):
    root: str = "/var/lib/lumen-data"


class StorageSmbConfigOut(BaseModel):
    host: str = ""
    port: int = 0  # 0 = 留空走默认 445；非零时 mount.cifs 用 -o port=N
    share: str = ""
    subpath: str = "/"
    username: str = ""
    has_password: bool = False


class StorageConfigOut(BaseModel):
    backend: str = ""  # "local" | "smb" | ""
    local: StorageLocalConfigOut
    smb: StorageSmbConfigOut
    status: StorageMountStatusOut | None = None
    last_apply: dict | None = None
    last_test: dict | None = None


class StorageLocalConfigIn(BaseModel):
    root: str = Field(..., min_length=1, max_length=512)


class StorageSmbConfigIn(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    # 0 / 不传 = 走 mount.cifs 默认 445；其他值 1-65535 写入 -o port=
    port: int = Field(0, ge=0, le=65535)
    share: str = Field(..., min_length=1, max_length=255)
    subpath: str = Field("/", max_length=512)
    username: str = Field(..., min_length=1, max_length=255)
    # 空字符串 = 保留旧值
    password: str = Field("", max_length=512)


class StorageConfigUpdateIn(BaseModel):
    backend: str  # "local" | "smb"
    local: StorageLocalConfigIn | None = None
    smb: StorageSmbConfigIn | None = None


class StorageTestIn(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(0, ge=0, le=65535)
    share: str = Field(..., min_length=1, max_length=255)
    subpath: str = Field("/", max_length=512)
    username: str = Field(..., min_length=1, max_length=255)
    # 空字符串 = 用已存的密码（必须 has_password）
    password: str = Field("", max_length=512)


class StorageTestResultOut(BaseModel):
    status: str  # "ok" | "fail" | "pending"
    message: str = ""
    tested_at: int | None = None
    call_id: str | None = None


class StorageApplyResponseOut(BaseModel):
    config: StorageConfigOut
    call_id: str
    status: str  # "pending" | "ok" | "fail"
    message: str = ""


__all__ = [
    "InviteLinkOut",
    "InviteLinkPublicOut",
    "SystemSettingItem",
    "SystemSettingsOut",
    "SystemSettingsUpdateItem",
    "SystemSettingsUpdateIn",
    "StorageMountStatusOut",
    "StorageLocalConfigOut",
    "StorageSmbConfigOut",
    "StorageConfigOut",
    "StorageLocalConfigIn",
    "StorageSmbConfigIn",
    "StorageConfigUpdateIn",
    "StorageTestIn",
    "StorageTestResultOut",
    "StorageApplyResponseOut",
]
