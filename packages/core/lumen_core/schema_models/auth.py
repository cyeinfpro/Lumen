"""Auth Pydantic contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

from .common import BaseOut

# ---------- Auth / User ----------


class SignupIn(BaseModel):
    # EmailStr 依赖 pydantic[email]（apps/api 已声明）；触发 422 而非 500，比手写正则更严格。
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""
    invite_token: str | None = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class NavigationVisibilityOut(BaseModel):
    studio: bool = True
    video: bool = True
    projects: bool = True
    assets: bool = True


class RuntimeDefaultsOut(BaseModel):
    fast: bool = True
    upload_max_source_bytes: int = 50 * 1024 * 1024
    canvas_enabled: bool = False
    nav_visibility: NavigationVisibilityOut = Field(
        default_factory=NavigationVisibilityOut
    )


class UserOut(BaseOut):
    id: str
    email: str
    display_name: str
    role: str
    account_mode: Literal["wallet", "byok"] = "wallet"
    notification_email: bool
    default_system_prompt_id: str | None = None
    runtime_defaults: RuntimeDefaultsOut = Field(default_factory=RuntimeDefaultsOut)


# ---------- System Prompts ----------


class SystemPromptOut(BaseOut):
    id: str
    name: str
    content: str
    is_default: bool = False
    created_at: datetime
    updated_at: datetime


class SystemPromptCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=10000)
    make_default: bool = False


class SystemPromptPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    make_default: bool | None = None


class SystemPromptListOut(BaseModel):
    items: list[SystemPromptOut]
    default_id: str | None = None


# ---------- Conversations ----------


class ConversationOut(BaseOut):
    id: str
    title: str
    pinned: bool
    archived: bool
    memory_disabled: bool = False
    active_scope_id: str | None = None
    last_activity_at: datetime
    default_params: dict[str, Any]
    default_system: str | None = None
    default_system_prompt_id: str | None = None
    created_at: datetime


class ConversationPatchIn(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    archived: bool | None = None
    default_params: dict[str, Any] | None = None
    default_system: str | None = None
    default_system_prompt_id: str | None = None


__all__ = [
    "SignupIn",
    "LoginIn",
    "NavigationVisibilityOut",
    "RuntimeDefaultsOut",
    "UserOut",
    "SystemPromptOut",
    "SystemPromptCreateIn",
    "SystemPromptPatchIn",
    "SystemPromptListOut",
    "ConversationOut",
    "ConversationPatchIn",
]
