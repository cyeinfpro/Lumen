#!/usr/bin/env python3
"""Run, verify, or import a durable Alembic downgrade export."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "apps" / "api"
ALEMBIC_ROOT = API_ROOT / "alembic"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage durable exports for destructive Alembic downgrades."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    downgrade = commands.add_parser(
        "downgrade",
        help="run an Alembic downgrade and commit its export on success",
    )
    downgrade.add_argument(
        "revision",
        help="Alembic target revision, for example 0059_reference_token_expiry",
    )
    downgrade.add_argument(
        "--export-path",
        type=Path,
        required=True,
        help="new manifest path; its parent must be owned by the caller and 0700",
    )

    verify = commands.add_parser(
        "verify",
        help="verify file security, typed rows, row counts, and digests",
    )
    verify.add_argument("export_path", type=Path)

    restore = commands.add_parser(
        "import",
        help="merge a committed export into upgraded tables by primary key",
    )
    restore.add_argument("export_path", type=Path)
    restore.add_argument(
        "--verify-only",
        action="store_true",
        help="verify database compatibility without inserting rows",
    )
    restore.add_argument(
        "--allow-pending",
        action="store_true",
        help=(
            "accept a failure-free pending export only while the database is "
            "at its recorded target revision"
        ),
    )
    return parser


def _arguments(argv: Sequence[str] | None) -> list[str]:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] not in {"downgrade", "verify", "import"}:
        arguments.insert(0, "downgrade")
    return arguments


def _load_export_helpers():
    sys.path.insert(0, str(ALEMBIC_ROOT))
    from telegram_downgrade_guard import (  # noqa: PLC0415
        import_migration_export,
        verify_migration_export,
    )

    return import_migration_export, verify_migration_export


def _sync_database_url() -> str:
    sys.path.insert(0, str(API_ROOT))
    from app.config import settings  # noqa: PLC0415

    url = make_url(settings.database_url)
    if url.drivername in {"postgresql+asyncpg", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg2")
    return url.render_as_string(hide_password=False)


def _run_downgrade(args: argparse.Namespace) -> int:
    export_path = args.export_path.expanduser().resolve()
    if export_path.exists() or export_path.is_symlink():
        raise RuntimeError(f"--export-path already exists: {export_path}")

    sys.path.insert(0, str(API_ROOT))
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ALEMBIC_ROOT))
    config.set_main_option("prepend_sys_path", str(API_ROOT))
    os.environ["LUMEN_MIGRATION_EXPORT_PATH"] = str(export_path)
    command.downgrade(config, args.revision)
    return 0


def _run_verify(args: argparse.Namespace) -> int:
    _, verify_migration_export = _load_export_helpers()
    manifest = verify_migration_export(args.export_path.expanduser().resolve())
    print(
        json.dumps(
            {
                "request_id": manifest["request_id"],
                "source_revision": manifest["source_revision"],
                "target_revision": manifest["target_revision"],
                "status": manifest["status"],
                "tables": {
                    name: table["row_count"]
                    for name, table in manifest["tables"].items()
                },
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


def _run_import(args: argparse.Namespace) -> int:
    import_migration_export, _ = _load_export_helpers()
    url = make_url(_sync_database_url())
    connect_args: dict[str, object] = {}
    if url.get_backend_name() == "postgresql":
        connect_args = {
            "connect_timeout": 10,
            "application_name": "lumen-migration-import",
        }
    engine = create_engine(
        url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    try:
        with engine.begin() as connection:
            imported = import_migration_export(
                connection,
                args.export_path.expanduser().resolve(),
                verify_only=args.verify_only,
                allow_pending=args.allow_pending,
            )
    finally:
        engine.dispose()
    print(json.dumps(imported, ensure_ascii=True, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(_arguments(argv))
    if args.command == "downgrade":
        return _run_downgrade(args)
    if args.command == "verify":
        return _run_verify(args)
    return _run_import(args)


if __name__ == "__main__":
    raise SystemExit(main())
