"""Alembic env for the desktop SQLite migration chain."""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_db_url = make_url(config.get_main_option("sqlalchemy.url") or settings.database_url)
if _db_url.drivername == "sqlite+aiosqlite":
    _db_url = _db_url.set(drivername="sqlite")
sync_url = _db_url.render_as_string(hide_password=False)
config.set_main_option("sqlalchemy.url", sync_url)


def run_migrations_offline() -> None:
    context.configure(
        url=sync_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
