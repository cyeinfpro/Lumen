#!/usr/bin/env python3
"""Compatibility wrapper for the shared Docker-to-desktop importer."""

from __future__ import annotations

from lumen_core.desktop_import import main


if __name__ == "__main__":
    raise SystemExit(main())
