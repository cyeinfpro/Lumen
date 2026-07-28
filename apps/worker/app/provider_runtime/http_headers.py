"""Pure outbound header construction for provider requests."""

from __future__ import annotations

import os
import uuid

try:
    from lumen_core import __version__ as lumen_core_version
except ImportError:  # pragma: no cover - package metadata fallback
    lumen_core_version = "unknown"


def _lumen_version() -> str:
    return os.environ.get("LUMEN_VERSION", "").strip() or lumen_core_version


UPSTREAM_ORIGINATOR = f"lumen-prod-{_lumen_version()}"


def upstream_auth_headers(
    api_key: str,
    *,
    trace_id: str | None = None,
) -> dict[str, str]:
    return {
        "authorization": f"Bearer {api_key}",
        "originator": UPSTREAM_ORIGINATOR,
        "x-trace-id": trace_id or uuid.uuid4().hex,
    }


__all__ = ["UPSTREAM_ORIGINATOR", "upstream_auth_headers"]
