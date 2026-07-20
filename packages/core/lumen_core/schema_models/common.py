"""Common Pydantic contracts."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

_ASSET_URL_PREFIX_RE = re.compile(r"^asset\s*:\s*/\s*/", re.IGNORECASE)
_ASSET_ID_RE = re.compile(r"^asset[-_][A-Za-z0-9_-]+$", re.IGNORECASE)
_ASSET_ID_MAX_LENGTH = 256
_ASSET_URL_WRAPPER_CHARS = "\"'`“”‘’"


def normalize_asset_reference_url(raw_url: str) -> str | None:
    value = raw_url.strip().strip(_ASSET_URL_WRAPPER_CHARS).strip()
    if not value:
        return None
    without_prefix = _ASSET_URL_PREFIX_RE.sub("", value, count=1)
    has_asset_prefix = without_prefix != value
    asset_id = without_prefix.strip() if has_asset_prefix else value
    if not has_asset_prefix and not _ASSET_ID_RE.fullmatch(asset_id):
        return None
    if (
        not asset_id
        or len(asset_id) > _ASSET_ID_MAX_LENGTH
        or _ASSET_ID_RE.fullmatch(asset_id) is None
    ):
        return ""
    return f"asset://{asset_id.lower()}"


class BaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)


__all__ = [
    "normalize_asset_reference_url",
    "BaseOut",
]
