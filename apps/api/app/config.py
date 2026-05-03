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
    # 旧字段 image_text_to_image_primary_route 保留为 fallback，确保旧 env / 旧 DB 行平滑切换。
    image_primary_route: str = "responses"
    image_channel: str = Field(default="auto", alias="IMAGE_CHANNEL")
    image_engine: str = Field(default="responses", alias="IMAGE_ENGINE")
    image_text_to_image_primary_route: str = ""  # DEPRECATED；空字符串表示"未显式设置"
    image_job_base_url: str = "https://image-job.example.com"

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

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.session_ttl_min < 5:
            raise ValueError("SESSION_TTL_MIN must be at least 5 minutes")
        if self.session_ttl_min > 60 * 24 * 30:
            raise ValueError("SESSION_TTL_MIN must not exceed 30 days")
        for item in self.trusted_proxies.split(","):
            cidr = item.strip()
            if not cidr:
                continue
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid TRUSTED_PROXIES CIDR: {cidr}") from exc
        env = self.app_env.strip().lower()
        if env not in {"dev", "development", "local", "test"}:
            secret = self.session_secret.strip()
            if not secret:
                raise ValueError("SESSION_SECRET must be set outside development")
            if secret in {
                "change-me",
                "change-me-to-a-long-random-string",
            }:
                raise ValueError("SESSION_SECRET must be changed outside development")
            if len(secret) < 32:
                raise ValueError("SESSION_SECRET must be at least 32 characters outside development")
            img_secret = self.image_proxy_secret.strip()
            if img_secret and len(img_secret) < 32:
                raise ValueError(
                    "IMAGE_PROXY_SECRET must be at least 32 characters outside development"
                )
            tg_secret = self.telegram_bot_shared_secret.strip()
            if tg_secret and len(tg_secret) < 32:
                raise ValueError(
                    "TELEGRAM_BOT_SHARED_SECRET must be at least 32 characters outside development"
                )
            db_url = urlsplit(self.database_url)
            db_user = unquote(db_url.username or "")
            db_password = unquote(db_url.password or "")
            if db_user == _DEFAULT_DB_USER and db_password == _DEFAULT_DB_PASSWORD:
                raise ValueError(
                    "DATABASE_URL must not use default Postgres credentials outside development"
                )
            redis_url = urlsplit(self.redis_url)
            redis_password = unquote(redis_url.password or "")
            if redis_password == _DEFAULT_REDIS_PASSWORD:
                raise ValueError(
                    "REDIS_URL must not use the default Redis password outside development"
                )
        return self


settings = Settings()
