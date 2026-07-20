"""pydantic-settings 读 .env。api / worker 各自有 config，但字段共享一致。"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlsplit

from pydantic import Field
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_HOST = "localhost"
_DEFAULT_API_PORT = 8000
_DEFAULT_WEB_PORT = 3000
_DEFAULT_POSTGRES_PORT = 5432
_DEFAULT_REDIS_PORT = 6379
_DEFAULT_DB_USER = "lumen"
_DEFAULT_DB_PASSWORD = "lumen"
_DEFAULT_DB_NAME = "lumen"
_DEFAULT_REDIS_PASSWORD = "lumen-redis-dev-password"

_DEFAULT_DATABASE_URL = (
    f"postgresql+asyncpg://{_DEFAULT_DB_USER}:{_DEFAULT_DB_PASSWORD}"
    f"@{_LOCAL_HOST}:{_DEFAULT_POSTGRES_PORT}/{_DEFAULT_DB_NAME}"
)
_DEFAULT_REDIS_URL = (
    f"redis://:{_DEFAULT_REDIS_PASSWORD}@{_LOCAL_HOST}:{_DEFAULT_REDIS_PORT}/0"
)
_DEFAULT_PUBLIC_BASE_URL = f"http://{_LOCAL_HOST}:{_DEFAULT_WEB_PORT}"
_DEFAULT_CORS_ALLOW_ORIGINS = f"http://{_LOCAL_HOST}:{_DEFAULT_WEB_PORT}"
_DEFAULT_IMAGE_JOB_BASE_URL = "https://image-job.example.com"
BYOK_DEV_MASTER_SECRET = "lumen-dev-byok-secret-DO-NOT-USE-IN-PROD-aabbccdd"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def _workspace_env_file() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if data.get("tool", {}).get("uv", {}).get("workspace", {}).get("members"):
            return parent / ".env"
    return None


def _settings_env_files() -> tuple[str | Path, ...]:
    env_files: list[str | Path] = [".env"]
    workspace_env = _workspace_env_file()
    if workspace_env is not None:
        env_files.append(workspace_env)
    explicit_env = os.environ.get("LUMEN_ENV_FILE", "").strip()
    if explicit_env:
        env_files.append(Path(explicit_env).expanduser())
    return tuple(env_files)


def _host_is_localish(host: str) -> bool:
    value = host.strip().strip("[]").lower()
    if not value:
        return False
    if value == "localhost" or value.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
    )


def _origin_looks_public(origin: str) -> bool:
    parsed = urlsplit(origin.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname or ""
    return bool(host and not _host_is_localish(host))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_settings_env_files(), env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = _DEFAULT_DATABASE_URL
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout: float = Field(default=30.0, gt=0)
    db_pool_recycle: int = Field(default=1800, ge=-1)
    redis_url: str = _DEFAULT_REDIS_URL

    # V1.0 收尾：可被 system_settings 覆盖；这里是 env fallback
    # 4K 升级后语义为"默认像素预算"（仅用于 size_mode=auto 的 preset 推导），
    # 显式 fixed_size 走 lumen_core.sizing.validate_explicit_size 独立校验。
    upstream_pixel_budget: int = 1572864
    upstream_global_concurrency: int = 4
    upstream_default_model: str = "gpt-5.5"
    # 图像主路径偏好（覆盖 t2i + i2i），可被 system_settings 覆盖。
    image_primary_route: str = "responses"
    image_channel: str = Field(default="auto", alias="IMAGE_CHANNEL")
    image_engine: str = Field(default="responses", alias="IMAGE_ENGINE")
    image_job_base_url: str = _DEFAULT_IMAGE_JOB_BASE_URL
    # 与 worker 同名；默认前端/API 只消费脱敏后的 generation diagnostics。
    expose_provider_diagnostics: bool = False

    session_secret: str = ""
    session_ttl_min: int = 60 * 24 * 7  # 7 天

    # 图片签名 URL 的对称密钥（HMAC-SHA256）。`/api/images/_/sig/...` 端点用它验签。
    # dev/test 留空时 verify 立即返回 False，签名通道实际不可用——保持 owner-check 路由可用即可。
    # 生产必须显式配置 ≥32 字符的随机串；轮转 key 会立即作废所有未过期的签名 URL。
    image_proxy_secret: str = ""

    app_env: str = "dev"
    app_port: int = _DEFAULT_API_PORT
    storage_root: str = "/opt/lumendata/storage"
    backup_root: str = "/opt/lumendata/backup"
    lumen_scripts_dir: str = ""
    public_base_url: str = _DEFAULT_PUBLIC_BASE_URL
    cors_allow_origins: str = _DEFAULT_CORS_ALLOW_ORIGINS
    trusted_proxies: str = ""

    # Password reset email delivery. Production must be wired to a real SMTP
    # server so reset links are not silently generated and dropped.
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = False
    smtp_starttls: bool = True
    smtp_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # Apparel model library presets live in this repo under
    # assets/apparel-model-presets/. After pushing that folder to GitHub, the
    # sync endpoint enumerates the folder through GitHub Contents API and
    # caches binaries into storage_root/apparel-model-library/.
    apparel_model_library_github_contents_url: str = (
        "https://api.github.com/repos/cyeinfpro/Lumen/contents/"
        "assets/apparel-model-presets?ref=main"
    )
    apparel_model_library_sync_mode: str = "admin_only"

    # ---------- 观测层（DESIGN §10 / V1.0 收尾） ----------
    # Sentry：dsn 为空则 init_sentry 静默 no-op
    sentry_dsn: str = ""
    sentry_environment: str = ""  # 留空时 init_sentry 内部会 fallback 到 app_env
    sentry_traces_sample_rate: float = 0.1

    # OpenTelemetry：endpoint 为空则 init_otel 完全跳过
    otel_exporter_endpoint: str = ""
    otel_service_name: str = "lumen-api"

    # Prometheus /metrics 开关
    metrics_enabled: bool = True

    # 登录用户的限流（发消息 / 上传图）开关。默认关闭；
    # 公开端点的限流（invite / share 预览）恒开，不受此开关影响。
    user_rate_limit_enabled: bool = False

    # Telegram bot ↔ API 之间的共享密钥（X-Bot-Token 头）。
    # 留空则 /telegram/* 路由全部 401，等价于关闭 bot 集成。
    # 生产必须 ≥32 字符；和 SESSION_SECRET 一样的强度要求。
    telegram_bot_shared_secret: str = ""

    # Bot 的 TG username（不带 @），仅用于 /me/telegram/link-code 拼 deep_link。
    # 留空则返回 deep_link=None，前端自拼。
    telegram_bot_username: str = ""

    # BYOK 用户 API Key 加密主密钥。明文 key 只在请求生命周期中出现；
    # 数据库持久化前使用 AES-GCM 加密，HMAC-SHA256 计算 key_hash。
    byok_api_key_master_secret: str = ""

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        _validate_session_ttl(self.session_ttl_min)
        _validate_trusted_proxies(self.trusted_proxies)
        _normalize_and_validate_smtp(self)
        is_dev = _is_development_environment(self.app_env)
        _validate_public_development_origin(self, is_dev=is_dev)
        _configure_byok_secret(self, is_dev=is_dev)
        if not is_dev:
            _validate_production_settings(self)
        return self


def _validate_session_ttl(session_ttl_min: int) -> None:
    if session_ttl_min < 5:
        raise ValueError("SESSION_TTL_MIN must be at least 5 minutes")
    if session_ttl_min > 60 * 24 * 30:
        raise ValueError("SESSION_TTL_MIN must not exceed 30 days")


def _validate_trusted_proxies(trusted_proxies: str) -> None:
    for item in trusted_proxies.split(","):
        cidr = item.strip()
        if not cidr:
            continue
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid TRUSTED_PROXIES CIDR: {cidr}") from exc


def _normalize_and_validate_smtp(settings: Settings) -> None:
    settings.smtp_host = settings.smtp_host.strip()
    settings.smtp_from_email = settings.smtp_from_email.strip()
    settings.smtp_username = settings.smtp_username.strip()
    settings.smtp_password = settings.smtp_password.strip()
    if settings.smtp_use_tls and settings.smtp_starttls:
        raise ValueError("SMTP_USE_TLS and SMTP_STARTTLS cannot both be enabled")
    if settings.smtp_from_email and "@" not in settings.smtp_from_email:
        raise ValueError("SMTP_FROM_EMAIL must be a valid email address")
    if settings.smtp_password and not settings.smtp_username:
        raise ValueError("SMTP_USERNAME must be set when SMTP_PASSWORD is set")


def _is_development_environment(app_env: str) -> bool:
    return app_env.strip().lower() in {"dev", "development", "local", "test"}


def _validate_public_development_origin(
    settings: Settings,
    *,
    is_dev: bool,
) -> None:
    allow_public_dev = (
        os.environ.get("LUMEN_ALLOW_PUBLIC_DEV", "").strip().lower() in _TRUE_ENV_VALUES
    )
    if (
        is_dev
        and _origin_looks_public(settings.public_base_url)
        and not allow_public_dev
    ):
        raise ValueError(
            "APP_ENV=dev cannot be used with a public PUBLIC_BASE_URL; "
            "set APP_ENV=prod for deployments or set LUMEN_ALLOW_PUBLIC_DEV=1 "
            "only for an intentional temporary test"
        )


def _configure_byok_secret(settings: Settings, *, is_dev: bool) -> None:
    # Production needs a stable encryption root; development gets a deterministic
    # fallback so fresh checkouts and smoke tests do not need secret provisioning.
    byok_secret = settings.byok_api_key_master_secret.strip()
    if is_dev and len(byok_secret) < 16:
        settings.byok_api_key_master_secret = BYOK_DEV_MASTER_SECRET
        return
    if not is_dev and byok_secret == BYOK_DEV_MASTER_SECRET:
        raise ValueError(
            "BYOK_API_KEY_MASTER_SECRET must not use the public dev fallback outside development"
        )
    if not is_dev and len(byok_secret) < 32:
        raise ValueError(
            "BYOK_API_KEY_MASTER_SECRET must be at least 32 characters in production; "
            "generate with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )


def _validate_production_smtp(settings: Settings) -> None:
    if not settings.smtp_host:
        raise ValueError(
            "SMTP_HOST must be set outside development for password reset email"
        )
    if not settings.smtp_from_email:
        raise ValueError(
            "SMTP_FROM_EMAIL must be set outside development for password reset email"
        )


def _validate_production_session_secret(secret: str) -> None:
    secret = secret.strip()
    if not secret:
        raise ValueError("SESSION_SECRET must be set outside development")
    if secret in {"change-me", "change-me-to-a-long-random-string"}:
        raise ValueError("SESSION_SECRET must be changed outside development")
    if len(secret) < 32:
        raise ValueError(
            "SESSION_SECRET must be at least 32 characters outside development"
        )


def _validate_production_image_proxy_secret(secret: str) -> None:
    secret = secret.strip()
    if not secret:
        raise ValueError("IMAGE_PROXY_SECRET must be set outside development")
    if len(secret) < 32:
        raise ValueError(
            "IMAGE_PROXY_SECRET must be at least 32 characters outside development"
        )


def _validate_production_image_job_url(image_job_base_url: str) -> None:
    image_job_url = image_job_base_url.strip().rstrip("/")
    image_job_host = urlsplit(image_job_url).hostname or ""
    if (
        not image_job_url
        or image_job_url == _DEFAULT_IMAGE_JOB_BASE_URL
        or image_job_host == "image-job.example.com"
    ):
        raise ValueError("IMAGE_JOB_BASE_URL must be configured outside development")


def _validate_production_telegram_secret(secret: str) -> None:
    secret = secret.strip()
    if secret and len(secret) < 32:
        raise ValueError(
            "TELEGRAM_BOT_SHARED_SECRET must be at least 32 characters outside development"
        )


def _validate_service_password(url: str, default_password: str, label: str) -> None:
    password = unquote(urlsplit(url).password or "")
    if not password or password == default_password:
        raise ValueError(
            f"{label} must not use the default password outside development"
        )


def _validate_production_settings(settings: Settings) -> None:
    _validate_production_smtp(settings)
    _validate_production_session_secret(settings.session_secret)
    _validate_production_image_proxy_secret(settings.image_proxy_secret)
    _validate_production_image_job_url(settings.image_job_base_url)
    _validate_production_telegram_secret(settings.telegram_bot_shared_secret)
    _validate_service_password(
        settings.database_url,
        _DEFAULT_DB_PASSWORD,
        "DATABASE_URL",
    )
    _validate_service_password(
        settings.redis_url,
        _DEFAULT_REDIS_PASSWORD,
        "REDIS_URL",
    )


settings = Settings()
