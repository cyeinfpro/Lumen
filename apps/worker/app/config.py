"""Worker 配置。上游供应商只通过 Provider Pool (`PROVIDERS`) 生效。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .upstream_clients.url_validation import validate_image_job_control_url


_ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"
BYOK_DEV_MASTER_SECRET = "lumen-dev-byok-secret-DO-NOT-USE-IN-PROD-aabbccdd"
_DEFAULT_REDIS_PASSWORD = "lumen-redis-dev-password"
_DEFAULT_REDIS_URL = f"redis://:{_DEFAULT_REDIS_PASSWORD}@localhost:6379/0"
_DEFAULT_IMAGE_JOB_BASE_URL = "https://image-job.example.com"
_DEFAULT_DATABASE_URL = "postgresql+asyncpg://lumen:lumen@localhost:5432/lumen"
_DEV_ENVIRONMENTS = frozenset({"dev", "development", "local", "test"})


def _internal_service_url(raw: str, *, field: str) -> str:
    value = (raw or "").strip().rstrip("/")
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError(f"{field} must be an HTTP(S) service URL without credentials")
    return value


def _agent_proxy_host(raw: str, *, field: str) -> str:
    value = (raw or "").strip()
    if (
        not value
        or any(char.isspace() or ord(char) < 32 for char in value)
        or any(char in value for char in "/?#@")
    ):
        raise ValueError(f"{field} must be a host name or address")
    return value


def validate_image_job_base_url(raw_base: str) -> str:
    """Apply the same transport policy at startup and at request time.

    Plain HTTP is accepted only for loopback/private/container-network hosts;
    any public control-plane host must use HTTPS because service and upstream
    credentials are sent on this channel.
    """

    return validate_image_job_control_url(
        raw_base,
        allow_private_http=True,
    )


def validate_image_job_sidecar_token(raw_token: str) -> str:
    """Validate the service credential required by the image-job path."""
    token = (raw_token or "").strip()
    if len(token) < 32 or any(char.isspace() for char in token):
        raise ValueError(
            "IMAGE_JOB_SIDECAR_TOKEN must be a whitespace-free token "
            "with at least 32 characters"
        )
    return token


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", _ROOT_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = _DEFAULT_DATABASE_URL
    db_pool_size: int = Field(default=32, ge=1)
    db_max_overflow: int = Field(default=16, ge=0)
    db_pool_timeout: float = Field(default=10.0, gt=0)
    db_pool_recycle: int = Field(default=1800, ge=-1)
    redis_url: str = _DEFAULT_REDIS_URL

    providers: str = ""
    # 探活默认值（runtime_settings DB 优先，这里是 env / 启动 fallback）
    # 文本算术 probe：120s 一次，让 gpt-5.4-mini 算 99×99 验答案
    providers_auto_probe_interval: int = 120
    # Image probe：默认 0 = 关闭（每张 probe 烧一次账号配额，生产先关）
    providers_auto_image_probe_interval: int = 0
    # V1.0 收尾：可被 system_settings 覆盖；这里是 env fallback
    # 4K 升级后语义为"默认像素预算"（仅用于 size_mode=auto 的 preset 推导），
    # 显式 fixed_size 走 lumen_core.sizing.validate_explicit_size 独立校验。
    upstream_pixel_budget: int = 1572864
    upstream_global_concurrency: int = 4
    upstream_default_model: str = "gpt-5.6-sol"
    # 图像主路径偏好（覆盖 t2i + i2i），可被 system_settings 覆盖。
    image_primary_route: str = "responses"
    image_channel: str = Field(default="auto", alias="IMAGE_CHANNEL")
    image_engine: str = Field(default="responses", alias="IMAGE_ENGINE")
    image_job_base_url: str = _DEFAULT_IMAGE_JOB_BASE_URL
    image_job_sidecar_token: str = Field(
        default="",
        repr=False,
    )

    storage_root: str = "/opt/lumendata/storage"
    minimum_storage_free_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=0,
    )
    image_upload_lease_ttl_seconds: int = Field(
        default=120,
        ge=10,
        le=3600,
    )
    image_upload_capacity_degraded_policy: str = Field(
        default="",
        pattern=r"^(|fail_closed|scaled_local)$",
    )

    # 并发 / 超时（DESIGN §6.5 / §6.7）
    # P2-10 timeout 分层（用户明确约束）：
    #   nginx 3600 / 1800  >  arq job_timeout 1800
    #     >  task _RUN_GENERATION_TIMEOUT_S 1500
    #       >  upstream_read_timeout_s 660
    # 不要颠倒任何一层；缩小 upstream 会让 4K 长任务在 OpenAI 排队 + 推理 + 下载
    # 8min 高峰被误杀。task 1500 - upstream 660 = 840s 缓冲，足够任务在被 arq
    # 强杀前优雅释放 lease / avoid set / image_queue slot。
    upstream_connect_timeout_s: float = 10.0
    # 4K 升级后单次上游调用（OpenAI 排队 + 推理 + 下载）最坏 ~8 min；180s 会误杀。
    upstream_read_timeout_s: float = 660.0
    upstream_write_timeout_s: float = 30.0
    # 图片任务统一 FIFO 队列并发：所有 1K/2K/4K 共用，且 worker 会确保并发任务
    # 使用不同 provider（dual_race 模式不锁 provider，但每 task 内部 image2/responses
    # 两路自己 failover 全 N 个号）。
    image_generation_concurrency: int = 4
    # 默认只向前端暴露脱敏诊断；排查自托管/内部问题时可显式打开 provider/proxy/endpoint 细节。
    expose_provider_diagnostics: bool = False

    edit_race_lanes: int = 2

    # ---------- 观测层 ----------
    app_env: str = "dev"
    sentry_dsn: str = ""
    sentry_environment: str = ""  # 空时回退 app_env
    sentry_traces_sample_rate: float = 0.1

    otel_exporter_endpoint: str = ""
    otel_service_name: str = "lumen-worker"

    # Prometheus scrapes this port over the private container network.
    worker_metrics_host: str = "0.0.0.0"  # nosec B104
    worker_metrics_port: int = 9100

    # BYOK 用户 API Key 解密主密钥。必须与 API 服务一致。
    byok_api_key_master_secret: str = ""

    # ---------- Pi Agent Runtime (backend-only) ----------
    agent_runtime_url: str = "http://agent-runtime:8090"
    agent_runtime_shared_secret: str = Field(default="", repr=False)
    agent_tool_capability_secret: str = Field(default="", repr=False)
    agent_tool_gateway_url: str = "http://api:8000/internal/agent"
    agent_runtime_proxy_bind_host: str = "0.0.0.0"
    agent_runtime_proxy_advertise_host: str = "worker"
    agent_runtime_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    agent_runtime_heartbeat_interval_seconds: float = Field(default=15.0, ge=1, le=60)
    agent_runtime_event_idle_timeout_seconds: float = Field(default=90.0, gt=0, le=300)
    agent_runtime_max_request_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=64 * 1024,
        le=64 * 1024 * 1024,
    )
    agent_runtime_max_line_bytes: int = Field(
        default=64 * 1024,
        ge=1024,
        le=1024 * 1024,
    )
    agent_text_flush_chars: int = Field(default=256, ge=32, le=8192)
    agent_text_flush_seconds: float = Field(default=0.5, ge=0.1, le=10)
    agent_reference_preview_max_bytes: int = Field(
        default=128 * 1024, ge=64 * 1024, le=512 * 1024
    )

    @model_validator(mode="after")
    def validate_runtime(self) -> "Settings":
        if self.edit_race_lanes < 1:
            raise ValueError("EDIT_RACE_LANES must be at least 1")
        if (
            self.agent_runtime_event_idle_timeout_seconds
            <= self.agent_runtime_heartbeat_interval_seconds * 2
        ):
            raise ValueError(
                "AGENT_RUNTIME_EVENT_IDLE_TIMEOUT_SECONDS must exceed twice "
                "AGENT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS"
            )
        env = self.app_env.strip().lower()
        is_dev = env in _DEV_ENVIRONMENTS
        self.image_job_base_url = self.image_job_base_url.strip().rstrip("/")
        self.image_job_sidecar_token = self.image_job_sidecar_token.strip()
        if self.image_channel.strip().lower() == "image_jobs_only":
            self.image_job_base_url = validate_image_job_base_url(
                self.image_job_base_url
            )
            self.image_job_sidecar_token = validate_image_job_sidecar_token(
                self.image_job_sidecar_token
            )
        if not is_dev and self.redis_url.strip() == _DEFAULT_REDIS_URL:
            raise ValueError(
                "REDIS_URL must be explicitly configured outside development"
            )
        if not is_dev and self.database_url.strip() == _DEFAULT_DATABASE_URL:
            raise ValueError(
                "DATABASE_URL must be explicitly configured outside development"
            )
        secret = (self.byok_api_key_master_secret or "").strip()
        # dev/test：未设时 fallback 到与 API 端一致的 deterministic dummy
        # （prod 严禁使用，必须显式 ≥ 32 chars）。
        if is_dev and len(secret) < 16:
            secret = BYOK_DEV_MASTER_SECRET
            self.byok_api_key_master_secret = secret
        elif not is_dev and secret == BYOK_DEV_MASTER_SECRET:
            raise ValueError(
                "BYOK_API_KEY_MASTER_SECRET must not use the public dev "
                "fallback outside development"
            )
        elif not is_dev and len(secret) < 32:
            raise ValueError(
                "BYOK_API_KEY_MASTER_SECRET must be at least 32 characters "
                "outside development"
            )
        self.agent_runtime_url = _internal_service_url(
            self.agent_runtime_url,
            field="AGENT_RUNTIME_URL",
        )
        self.agent_tool_gateway_url = _internal_service_url(
            self.agent_tool_gateway_url,
            field="AGENT_TOOL_GATEWAY_URL",
        )
        self.agent_runtime_proxy_bind_host = _agent_proxy_host(
            self.agent_runtime_proxy_bind_host,
            field="AGENT_RUNTIME_PROXY_BIND_HOST",
        )
        self.agent_runtime_proxy_advertise_host = _agent_proxy_host(
            self.agent_runtime_proxy_advertise_host,
            field="AGENT_RUNTIME_PROXY_ADVERTISE_HOST",
        )
        for field_name in (
            "agent_runtime_shared_secret",
            "agent_tool_capability_secret",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if value and len(value.encode("utf-8")) < 32:
                env_name = field_name.upper()
                raise ValueError(f"{env_name} must contain at least 32 UTF-8 bytes")
            setattr(self, field_name, value)
        return self


settings = Settings()
