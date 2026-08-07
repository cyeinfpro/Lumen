#!/usr/bin/env python3
"""Run an explicit, exported Alembic downgrade."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from alembic import command
from alembic.config import Config


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "apps" / "api"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run an Alembic downgrade with the required durable export for "
            "destructive Telegram revisions."
        )
    )
    parser.add_argument(
        "revision",
        help="Alembic target revision, for example 0059_reference_token_expiry",
    )
    parser.add_argument(
        "--export-path",
        type=Path,
        required=True,
        help="new JSON path for pre-downgrade Telegram data export",
    )
    args = parser.parse_args()

    if args.export_path.exists():
        parser.error(f"--export-path already exists: {args.export_path}")

    sys.path.insert(0, str(API_ROOT))
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(API_ROOT))
    os.environ["LUMEN_MIGRATION_EXPORT_PATH"] = str(
        args.export_path.expanduser().resolve()
    )
    command.downgrade(config, args.revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
