"""Worker 配置。上游供应商只通过 Provider Pool (`PROVIDERS`) 生效。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from lumen_core.desktop_runtime import (
    DESKTOP_PROVIDER_FILE_ENV,
    DATA_ROOT_ENV,
    desktop_sqlite_url,
    desktop_storage_root,
    is_desktop_runtime,
)

_ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"
BYOK_DEV_MASTER_SECRET = "lumen-dev-byok-secret-DO-NOT-USE-IN-PROD-aabbccdd"
_DEFAULT_REDIS_PASSWORD = "lumen-redis-dev-password"
_DEFAULT_REDIS_URL = f"redis://:{_DEFAULT_REDIS_PASSWORD}@localhost:6379/0"
_DEFAULT_IMAGE_JOB_BASE_URL = "https://image-job.example.com"
_DEFAULT_DATABASE_URL = "postgresql+asyncpg://lumen:lumen@localhost:5432/lumen"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", _ROOT_ENV), env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = _DEFAULT_DATABASE_URL
    lumen_runtime: str = "docker"
    lumen_data_root: str = Field(default="", alias=DATA_ROOT_ENV)
    lumen_desktop_provider_file: str = Field(
        default="",
        alias=DESKTOP_PROVIDER_FILE_ENV,
    )
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
    upstream_default_model: str = "gpt-5.5"
    # 图像主路径偏好（覆盖 t2i + i2i），可被 system_settings 覆盖。
    image_primary_route: str = "responses"
    image_channel: str = Field(default="auto", alias="IMAGE_CHANNEL")
    image_engine: str = Field(default="responses", alias="IMAGE_ENGINE")
    image_job_base_url: str = _DEFAULT_IMAGE_JOB_BASE_URL

    storage_root: str = "/opt/lumendata/storage"

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

    worker_metrics_host: str = "0.0.0.0"
    worker_metrics_port: int = 9100

    # BYOK 用户 API Key 解密主密钥。必须与 API 服务一致。
    byok_api_key_master_secret: str = ""

    @model_validator(mode="after")
    def validate_runtime(self) -> "Settings":
        if self.edit_race_lanes < 1:
            raise ValueError("EDIT_RACE_LANES must be at least 1")
        desktop_runtime = is_desktop_runtime(self.lumen_runtime)
        if desktop_runtime:
            if self.database_url == _DEFAULT_DATABASE_URL:
                self.database_url = desktop_sqlite_url(self.lumen_data_root)
            if self.storage_root == "/opt/lumendata/storage":
                self.storage_root = desktop_storage_root(self.lumen_data_root)
        env = self.app_env.strip().lower()
        is_dev = desktop_runtime or env in {"dev", "development", "local", "test"}
        image_job_base = self.image_job_base_url.strip().rstrip("/")
        if not is_dev and image_job_base == _DEFAULT_IMAGE_JOB_BASE_URL:
            raise ValueError(
                "IMAGE_JOB_BASE_URL must not use the example.com placeholder outside development"
            )
        secret = (self.byok_api_key_master_secret or "").strip()
        # dev/test：未设时 fallback 到与 API 端一致的 deterministic dummy
        # （prod 严禁使用，必须显式 ≥ 32 chars）。
        if is_dev and len(secret) < 16:
            secret = BYOK_DEV_MASTER_SECRET
            self.byok_api_key_master_secret = secret
        elif not is_dev and secret == BYOK_DEV_MASTER_SECRET:
            raise ValueError(
                "BYOK_API_KEY_MASTER_SECRET must not use the public dev fallback outside development"
            )
        elif not is_dev and len(secret) < 32:
            raise ValueError(
                "BYOK_API_KEY_MASTER_SECRET must be at least 32 characters outside development"
            )
        return self


settings = Settings()
