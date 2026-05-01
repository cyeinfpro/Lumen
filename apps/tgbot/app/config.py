"""Telegram bot 配置。

env 来源：
- apps/tgbot/.env（开发优先）
- workspace 根 .env（fallback）
- LUMEN_ENV_FILE 环境变量指向的文件（最高优先）
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    files: list[str | Path] = [".env"]
    workspace_env = _workspace_env_file()
    if workspace_env is not None:
        files.append(workspace_env)
    explicit = os.environ.get("LUMEN_ENV_FILE", "").strip()
    if explicit:
        files.append(Path(explicit).expanduser())
    return tuple(files)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_settings_env_files(), env_file_encoding="utf-8", extra="ignore"
    )

    telegram_bot_token: str = ""
    telegram_bot_username: str = "LumenBot"  # 拼 deep_link 用，不带 @
    telegram_bot_shared_secret: str = ""

    # 出站代理：bot 调 api.telegram.org 必须能 outbound。中国境内服务器 GFW 阻断
    # SNI，必须走代理。例：socks5://user:pass@host:port
    telegram_proxy_url: str = ""

    # API endpoint。bot 与 api 通常同机或同 LAN，loopback / 内网走 http 即可。
    lumen_api_base: str = "http://127.0.0.1:8000"

    # Redis：bot 直连订阅 task:{gen_id} pubsub。和 api 共用同一实例。
    redis_url: str = "redis://localhost:6379/0"

    # 图片下载临时目录（可选；默认走内存）。
    download_tmp_dir: str = ""

    # 部署模式：polling 或 webhook。先用 polling，后切 webhook。
    bot_mode: str = Field(default="polling", pattern="^(polling|webhook)$")
    webhook_url: str = ""  # https://your-domain.example.com/tgbot/<secret>/webhook
    webhook_secret_path: str = ""  # 路径段，加随机性

    log_level: str = "INFO"

    # 仅允许这些 TG user_id（数字）使用 bot；逗号分隔。空 = 不限制（仅靠 chat_id 绑定）。
    # 与 telegram_bindings 形成双因子：拿到 X-Bot-Token 又知道 chat_id 也没用，因为
    # 进 bot 这层会先按 from_user.id 拒掉。
    telegram_allowed_user_ids: str = ""

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if not self.telegram_bot_token.strip():
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if not self.telegram_bot_shared_secret.strip():
            raise ValueError("TELEGRAM_BOT_SHARED_SECRET is required")
        if self.bot_mode == "webhook" and not self.webhook_url:
            raise ValueError("WEBHOOK_URL is required when BOT_MODE=webhook")
        return self


settings = Settings()
