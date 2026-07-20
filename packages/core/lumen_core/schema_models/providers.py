"""Providers Pydantic contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from .common import BaseOut

# ---------- Providers（管理员 Provider Pool CRUD + 探活） ----------


ProviderPurpose = Literal["chat", "image", "embedding"]


def _default_provider_purposes() -> list[ProviderPurpose]:
    return ["chat", "image"]


class ProviderItemOut(BaseModel):
    name: str
    base_url: str
    api_key_hint: str
    priority: int
    weight: int
    enabled: bool
    purposes: list[ProviderPurpose] = Field(default_factory=_default_provider_purposes)
    proxy: str | None = None
    image_jobs_enabled: bool = False
    image_jobs_endpoint: str = "auto"
    image_jobs_endpoint_lock: bool = False
    image_jobs_base_url: str = ""
    image_edit_input_transport: Literal["url", "file"] = "url"
    image_concurrency: int = 1
    # Capability tri-state (image-stability-hardening §P2). null = 未知（默认）。
    responses_supported: bool | None = None
    image_generations_supported: bool | None = None
    image_responses_supported: bool | None = None


class ProviderProxyOut(BaseModel):
    name: str
    type: str
    host: str
    port: int
    username: str | None = None
    password_hint: str | None = None
    private_key_path: str | None = None
    enabled: bool = True


class ProvidersOut(BaseModel):
    items: list[ProviderItemOut]
    proxies: list[ProviderProxyOut] = []
    source: str  # "db" | "env" | "none"


class AdminModelOut(BaseModel):
    id: str
    providers: list[str]
    object: str = "model"


class AdminModelsErrorOut(BaseModel):
    provider: str
    message: str


class AdminModelsOut(BaseModel):
    models: list[AdminModelOut]
    fetched_at: datetime
    errors: list[AdminModelsErrorOut] = []


class ProviderItemIn(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    priority: int = 0
    weight: int = 1
    enabled: bool = True
    purposes: list[ProviderPurpose] = Field(
        default_factory=_default_provider_purposes, min_length=1
    )
    proxy: str | None = None
    image_jobs_enabled: bool = False
    image_jobs_endpoint: str = "auto"
    image_jobs_endpoint_lock: bool = False
    image_jobs_base_url: str = ""
    image_edit_input_transport: Literal["url", "file"] = "url"
    image_concurrency: int = 1
    # Capability tri-state（详见 ProviderItemOut 注释）
    responses_supported: bool | None = None
    image_generations_supported: bool | None = None
    image_responses_supported: bool | None = None


class ProviderProxyIn(BaseModel):
    name: str
    type: str = "socks5"
    host: str
    port: int = 1080
    username: str | None = None
    password: str = ""
    private_key_path: str | None = None
    enabled: bool = True


class ProvidersUpdateIn(BaseModel):
    items: list[ProviderItemIn]
    proxies: list[ProviderProxyIn] = []


VideoProviderKind = Literal[
    "volcano",
    "volcano_third_party",
    "volcano_newapi",
    "dashscope",
    "veo",
    "omni_flash",
    "fake",
]


class VideoProviderItemOut(BaseModel):
    name: str
    kind: VideoProviderKind = "volcano"
    base_url: str
    api_key_hint: str
    access_key_id_hint: str | None = None
    secret_access_key_hint: str | None = None
    project_name: str | None = None
    region: str | None = None
    asset_management_ready: bool = False
    enabled: bool = True
    priority: int = 0
    weight: int = 1
    concurrency: int = 1
    supports_idempotency: bool = False
    proxy: str | None = None
    models: dict[str, str] = Field(default_factory=dict)


class VideoProvidersOut(BaseModel):
    enabled: bool = False
    items: list[VideoProviderItemOut]
    proxies: list[ProviderProxyOut] = Field(default_factory=list)
    source: str  # "db" | "env" | "none"


class VideoProviderItemIn(BaseModel):
    name: str
    kind: VideoProviderKind = "volcano"
    base_url: str
    api_key: str = Field(default="", repr=False)
    access_key_id: str = Field(default="", repr=False)
    secret_access_key: str = Field(default="", repr=False)
    project_name: str = "default"
    region: str = "cn-beijing"
    enabled: bool = True
    priority: int = 0
    weight: int = 1
    concurrency: int = 1
    supports_idempotency: bool = False
    proxy: str | None = None
    models: dict[str, str] = Field(default_factory=dict)


class VideoProvidersUpdateIn(BaseModel):
    enabled: bool = False
    items: list[VideoProviderItemIn]


class ProvidersProbeIn(BaseModel):
    names: list[str] | None = None


class ProviderProbeResult(BaseModel):
    name: str
    ok: bool
    latency_ms: int | None = None
    error: str | None = None
    status: str = "unknown"  # "healthy" | "unhealthy" | "disabled" | "unknown"
    # image-stability-hardening §P2 capability 探测信号。
    # 取值：
    #   None           — 探测未给出明确能力信号（默认）
    #   "supported"    — 端点确认支持
    #   "unsupported"  — 端点确认不支持（HTTP 404/405），可写 capability=False
    #   "auth"         — 鉴权问题（401/403），不能据此判定能力
    #   "transient"    — 临时不健康（429/5xx），不能据此判定能力
    capability_signal: str | None = None
    # 上游 HTTP status，便于 UI 直接显示
    http_status: int | None = None


class ProvidersProbeOut(BaseModel):
    items: list[ProviderProbeResult]
    probed_at: str | None = None


class ProviderStatsItem(BaseModel):
    name: str
    total: int = 0
    success: int = 0
    fail: int = 0
    success_rate: float = 0.0  # 0.0 ~ 1.0
    traffic_pct: float = 0.0  # 该 provider 占总请求的比例


class ProviderStatsOut(BaseModel):
    items: list[ProviderStatsItem]
    auto_probe_interval: int = 120  # 文本算术 probe 间隔（0=关闭，默认 120）
    auto_image_probe_interval: int = 0  # Image probe 间隔（0=关闭，默认 0）


# ---------- BYOK 用户自带 API Key ----------

ByokPurpose = Literal["chat", "image", "embedding"]


def _default_byok_purposes() -> list[ByokPurpose]:
    return ["chat", "image"]


class ByokSettingsOut(BaseModel):
    mode_enabled: bool = False
    byok_signup_enabled: bool = False
    byok_signup_bypasses_allowlist: bool = False
    fallback_to_admin_provider: bool = False
    validation_model: str = "gpt-5.4"
    validation_timeout_ms: int = 15000
    pending_token_ttl_seconds: int = 900
    retention_hide_enabled: bool = True
    retention_delete_enabled: bool = False
    retention_hide_days: int = 3
    retention_delete_days: int = 7


class ByokSettingsPatchIn(BaseModel):
    mode_enabled: bool | None = None
    byok_signup_enabled: bool | None = None
    byok_signup_bypasses_allowlist: bool | None = None
    fallback_to_admin_provider: bool | None = None
    validation_model: str | None = Field(default=None, max_length=64)
    validation_timeout_ms: int | None = Field(default=None, ge=1000, le=120000)
    pending_token_ttl_seconds: int | None = Field(default=None, ge=60, le=3600)
    retention_hide_enabled: bool | None = None
    retention_delete_enabled: bool | None = None
    retention_hide_days: int | None = Field(default=None, ge=1, le=3650)
    retention_delete_days: int | None = Field(default=None, ge=1, le=3650)


class ApiSupplierTemplateOut(BaseOut):
    id: str
    name: str
    slug: str
    base_url: str
    enabled: bool
    public_signup_enabled: bool
    user_bind_enabled: bool
    purposes: list[ByokPurpose]
    validation_model: str
    default_chat_model: str
    # review #12：image 任务必须用独立的 default_image_model；nullable
    # 表示该 supplier 不支持图片或 admin 未配置。
    default_image_model: str | None = None
    fast_chat_model: str | None = None
    validation_timeout_ms: int
    proxy_name: str | None = None
    text_concurrency_per_key: int
    image_concurrency_per_key: int
    capabilities_jsonb: dict[str, Any] = Field(default_factory=dict)
    active_credentials: int = 0
    recent_success_rate: float | None = None
    recent_error_counts: dict[str, int] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ApiSupplierTemplatePublicOut(BaseModel):
    id: str
    name: str
    purposes: list[ByokPurpose]
    validation_model: str


class ApiSupplierTemplateListOut(BaseModel):
    items: list[ApiSupplierTemplateOut]


class ApiSupplierTemplatePublicListOut(BaseModel):
    items: list[ApiSupplierTemplatePublicOut]


def _validate_supplier_base_url(v: str) -> str:
    """review #18：BYOK supplier 的 base_url 必须是 http(s) URL，禁止内嵌凭证。

    与 system_settings.providers 校验行为对齐：清理空白/末尾斜杠、
    要求 scheme + hostname、拒绝 user:pass@host 这种历史漏洞。
    """
    from urllib.parse import urlsplit

    v = v.strip().rstrip("/")
    parsed = urlsplit(v)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base_url must be http or https")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain credentials")
    if not parsed.hostname:
        raise ValueError("base_url must include a hostname")
    return v


def _validate_byok_api_key(v: str) -> str:
    """review #18：BYOK 用户 API Key shape 校验；strip 后判空+长度上限。"""
    v = v.strip()
    if not v:
        raise ValueError("api_key is required")
    if len(v) > 512:
        raise ValueError("api_key exceeds 512 characters")
    return v


class ApiSupplierTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # 设计 §5.2 / review #8：slug 未传时由后端从 name 派生（去空白 + lower-case +
    # 限制字符集），便于公开列表里直接走 GET /byok/suppliers/<slug>。
    slug: str | None = Field(default=None, max_length=80)
    base_url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    public_signup_enabled: bool = False
    user_bind_enabled: bool = True
    purposes: list[ByokPurpose] = Field(
        default_factory=_default_byok_purposes, min_length=1
    )
    validation_model: str = Field(default="gpt-5.4", min_length=1, max_length=64)
    default_chat_model: str = Field(default="gpt-5.4", min_length=1, max_length=64)
    # review #12：默认 None，由 admin 显式选填；不在 schema 写默认 image 模型，
    # 避免误把 chat-only supplier 当成支持 image 任务。
    default_image_model: str | None = Field(default=None, max_length=128)
    fast_chat_model: str | None = Field(default="gpt-5.4-mini", max_length=64)
    validation_timeout_ms: int = Field(default=15000, ge=1000, le=120000)
    proxy_name: str | None = Field(default=None, max_length=120)
    text_concurrency_per_key: int = Field(default=4, ge=1, le=100)
    image_concurrency_per_key: int = Field(default=1, ge=1, le=32)
    capabilities_jsonb: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str) -> str:
        return _validate_supplier_base_url(v)


class ApiSupplierTemplatePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=80)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    enabled: bool | None = None
    public_signup_enabled: bool | None = None
    user_bind_enabled: bool | None = None
    purposes: list[ByokPurpose] | None = Field(default=None, min_length=1)
    validation_model: str | None = Field(default=None, min_length=1, max_length=64)
    default_chat_model: str | None = Field(default=None, min_length=1, max_length=64)
    # review #12：patch 时 None 表示不变；显式传 "" 或具体 model id 才更新。
    default_image_model: str | None = Field(default=None, max_length=128)
    fast_chat_model: str | None = Field(default=None, max_length=64)
    validation_timeout_ms: int | None = Field(default=None, ge=1000, le=120000)
    proxy_name: str | None = Field(default=None, max_length=120)
    text_concurrency_per_key: int | None = Field(default=None, ge=1, le=100)
    image_concurrency_per_key: int | None = Field(default=None, ge=1, le=32)
    capabilities_jsonb: dict[str, Any] | None = None

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_supplier_base_url(v)


class ApiSupplierProbeIn(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, v: str) -> str:
        return _validate_byok_api_key(v)


class ApiKeyVerifyIn(BaseModel):
    supplier_id: str
    api_key: str = Field(min_length=1, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, v: str) -> str:
        return _validate_byok_api_key(v)


class ApiKeyVerifyOut(BaseModel):
    ok: bool
    verification_token: str
    supplier_id: str
    key_hint: str
    verified_at: datetime


class SignupByokIn(BaseModel):
    email: EmailStr
    # review #18：与 SignupIn.password 一致的强度上下限；上限避免 bcrypt 撞 72-byte
    # 截断 + 阻挡明显恶意大体积 payload。下限 8 是登录类系统最低基线。
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""
    verification_token: str = Field(min_length=1)
    invite_token: str | None = None


class UserApiCredentialOut(BaseOut):
    id: str
    supplier_id: str
    supplier_name: str
    key_hint: str
    status: str
    last_verified_at: datetime | None = None
    last_failed_at: datetime | None = None
    last_error_code: str | None = None
    rate_limited_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class UserApiCredentialListOut(BaseModel):
    items: list[UserApiCredentialOut]


class UserApiCredentialUpdateIn(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, v: str) -> str:
        return _validate_byok_api_key(v)


class ApiSupplierStatsOut(BaseModel):
    supplier_id: str
    active_credentials: int = 0
    recent_success_rate: float | None = None
    recent_error_counts: dict[str, int] = Field(default_factory=dict)


# ---------- Sessions（V1.0 收尾：/me/sessions） ----------


class SessionOut(BaseModel):
    id: str
    ua: str | None
    ip: str | None
    created_at: datetime
    expires_at: datetime
    is_current: bool


class SessionsOut(BaseModel):
    items: list[SessionOut]


# ---------- Regenerate（V1.0 收尾） ----------


class RegenerateIn(BaseModel):
    intent: Literal["chat", "vision_qa", "text_to_image", "image_to_image"]
    idempotency_key: str


class RegenerateOut(BaseModel):
    assistant_message_id: str
    completion_id: str | None = None
    generation_ids: list[str] = []


__all__ = [
    "ProviderPurpose",
    "ProviderItemOut",
    "ProviderProxyOut",
    "ProvidersOut",
    "AdminModelOut",
    "AdminModelsErrorOut",
    "AdminModelsOut",
    "ProviderItemIn",
    "ProviderProxyIn",
    "ProvidersUpdateIn",
    "VideoProviderKind",
    "VideoProviderItemOut",
    "VideoProvidersOut",
    "VideoProviderItemIn",
    "VideoProvidersUpdateIn",
    "ProvidersProbeIn",
    "ProviderProbeResult",
    "ProvidersProbeOut",
    "ProviderStatsItem",
    "ProviderStatsOut",
    "ByokPurpose",
    "ByokSettingsOut",
    "ByokSettingsPatchIn",
    "ApiSupplierTemplateOut",
    "ApiSupplierTemplatePublicOut",
    "ApiSupplierTemplateListOut",
    "ApiSupplierTemplatePublicListOut",
    "ApiSupplierTemplateIn",
    "ApiSupplierTemplatePatchIn",
    "ApiSupplierProbeIn",
    "ApiKeyVerifyIn",
    "ApiKeyVerifyOut",
    "SignupByokIn",
    "UserApiCredentialOut",
    "UserApiCredentialListOut",
    "UserApiCredentialUpdateIn",
    "ApiSupplierStatsOut",
    "SessionOut",
    "SessionsOut",
    "RegenerateIn",
    "RegenerateOut",
]
