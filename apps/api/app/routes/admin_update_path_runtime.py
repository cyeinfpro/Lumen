"""Filesystem paths used by the admin update runtime."""

from __future__ import annotations

import os
from pathlib import Path

from ..config import settings


_DEFAULT_LUMEN_ROOT = os.environ.get("LUMEN_ROOT", "/opt/lumen")


def _backup_artifact(name: str) -> Path:
    return Path(settings.backup_root).expanduser() / name


def update_log_path() -> Path:
    return _backup_artifact(".update.log")


def update_marker_path() -> Path:
    return _backup_artifact(".update.running")


def update_trigger_path() -> Path:
    return _backup_artifact(".update.trigger")


def update_runner_request_path() -> Path:
    return _backup_artifact(".update.request.json")


def update_adoption_receipt_path() -> Path:
    return _backup_artifact(".update.adoption.json")


def lumen_root() -> Path:
    """Resolve the install root per call so tests can override the environment."""
    return Path(os.environ.get("LUMEN_ROOT", _DEFAULT_LUMEN_ROOT)).expanduser()
