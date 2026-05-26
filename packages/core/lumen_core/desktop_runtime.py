"""Desktop runtime helpers shared by API, worker, and packaging code.

Docker remains the default runtime.  Desktop mode is opt-in through
``LUMEN_RUNTIME=desktop`` so the existing deployment path does not change.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any


RUNTIME_ENV = "LUMEN_RUNTIME"
DESKTOP_RUNTIME = "desktop"
DOCKER_RUNTIME = "docker"

LOCAL_USER_ID = "local-user"
LOCAL_USER_EMAIL = "local@lumen.desktop"
LOCAL_USER_DISPLAY_NAME = "Lumen Desktop"

DESKTOP_TOKEN_HEADER = "X-Lumen-Local-Token"
DESKTOP_PROVIDER_FILE_ENV = "LUMEN_DESKTOP_PROVIDER_FILE"
DATA_ROOT_ENV = "LUMEN_DATA_ROOT"


def runtime_profile(value: str | None = None) -> str:
    return (value if value is not None else os.environ.get(RUNTIME_ENV, "")).strip().lower()


def is_desktop_runtime(value: str | None = None) -> bool:
    return runtime_profile(value) == DESKTOP_RUNTIME


def default_data_root() -> Path:
    home = Path.home()
    system = platform.system().lower()
    if system == "darwin":
        return home / "Library" / "Application Support" / "Lumen"
    if system == "windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Lumen"
        return home / "AppData" / "Local" / "Lumen"
    return home / ".local" / "share" / "Lumen"


def desktop_data_root(raw: str | None = None) -> Path:
    value = (raw if raw is not None else os.environ.get(DATA_ROOT_ENV, "")).strip()
    return Path(value).expanduser() if value else default_data_root()


def desktop_sqlite_url(data_root: str | None = None) -> str:
    db_path = desktop_data_root(data_root) / "data" / "db" / "lumen.sqlite"
    return f"sqlite+aiosqlite:///{db_path}"


def desktop_storage_root(data_root: str | None = None) -> str:
    return str(desktop_data_root(data_root) / "data" / "storage")


def desktop_backup_root(data_root: str | None = None) -> str:
    return str(desktop_data_root(data_root) / "data" / "backup")


def desktop_logs_root(data_root: str | None = None) -> Path:
    return desktop_data_root(data_root) / "data" / "logs"


def desktop_settings_path(data_root: str | None = None) -> Path:
    return desktop_data_root(data_root) / "data" / "settings.json"


def desktop_bootstrap_marker(data_root: str | None = None) -> Path:
    return desktop_data_root(data_root) / "data" / ".bootstrap-done"


def desktop_provider_metadata_path(data_root: str | None = None) -> Path:
    return desktop_data_root(data_root) / "data" / "providers.json"


def desktop_provider_runtime_file() -> Path | None:
    raw = os.environ.get(DESKTOP_PROVIDER_FILE_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def read_desktop_provider_runtime_json() -> str | None:
    path = desktop_provider_runtime_file()
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, list):
        return json.dumps(parsed, ensure_ascii=False)
    if isinstance(parsed, dict) and isinstance(parsed.get("providers"), list):
        return json.dumps(parsed, ensure_ascii=False)
    return None


__all__ = [
    "DATA_ROOT_ENV",
    "DESKTOP_PROVIDER_FILE_ENV",
    "DESKTOP_RUNTIME",
    "DESKTOP_TOKEN_HEADER",
    "DOCKER_RUNTIME",
    "LOCAL_USER_DISPLAY_NAME",
    "LOCAL_USER_EMAIL",
    "LOCAL_USER_ID",
    "RUNTIME_ENV",
    "default_data_root",
    "desktop_backup_root",
    "desktop_bootstrap_marker",
    "desktop_data_root",
    "desktop_logs_root",
    "desktop_provider_metadata_path",
    "desktop_provider_runtime_file",
    "desktop_settings_path",
    "desktop_sqlite_url",
    "desktop_storage_root",
    "is_desktop_runtime",
    "read_desktop_provider_runtime_json",
    "runtime_profile",
]
