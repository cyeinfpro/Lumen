"""Executable entrypoint for the desktop API sidecar."""

from __future__ import annotations

import os
import sys


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "desktop-import":
        from lumen_core.desktop_import import main as import_main

        raise SystemExit(import_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "desktop-migrate":
        import asyncio

        from app.desktop_migrations import run_desktop_migrations

        asyncio.run(run_desktop_migrations())
        return

    import uvicorn

    from app.main import app

    host = os.environ.get("APP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = _int_env("APP_PORT", _int_env("PORT", 8000))
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"),
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
