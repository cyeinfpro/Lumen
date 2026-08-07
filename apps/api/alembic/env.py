"""Alembic env —— 读 app.config.settings.database_url；metadata 取自 lumen_core.models.Base。

注意 asyncpg 的同步/异步处理：alembic 自身是同步的，
因此 DATABASE_URL 里 `postgresql+asyncpg://` 在 migration 时改成同步驱动。"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

# 让 alembic 能 import app.* 与 lumen_core.*
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ALEMBIC_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ALEMBIC_ROOT))

from app.config import settings  # noqa: E402
from concurrent_index_retry import (  # noqa: E402
    prepare_concurrent_index_retry_boundary,
)
from lumen_core.models import Base  # noqa: E402
from telegram_downgrade_guard import (  # noqa: E402
    TELEGRAM_CONTROL_REVISION,
    guard_telegram_downgrade,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# migration 用同步驱动；用 URL parser 避免误改 username/password/path 里的字符串。
_db_url = make_url(settings.database_url)
if _db_url.drivername in {"postgresql+asyncpg", "postgresql"}:
    _db_url = _db_url.set(drivername="postgresql+psycopg2")
sync_url = _db_url.render_as_string(hide_password=False)
# Alembic stores options in ConfigParser, where percent-encoded socket paths
# such as host=%2Ftmp must escape "%" to survive interpolation.
config.set_main_option("sqlalchemy.url", sync_url.replace("%", "%%"))

target_metadata = Base.metadata


def _configured_revision_steps(
    migration_context: Any,
    current_heads: tuple[str, ...],
) -> tuple[Any, ...] | None:
    migration_fn = getattr(migration_context, "_migrations_fn", None)
    if migration_fn is None:
        return None

    steps = tuple(migration_fn(current_heads, migration_context))

    def cached_migration_fn(
        heads: tuple[str, ...],
        active_context: Any,
    ) -> tuple[Any, ...]:
        if tuple(heads) == current_heads and active_context is migration_context:
            return steps
        return tuple(migration_fn(heads, active_context))

    # Alembic's documented command API installs this work function for both
    # CLI and programmatic invocations. Cache the evaluated plan so the normal
    # run does not resolve or import the revision sequence a second time.
    migration_context._migrations_fn = cached_migration_fn
    return steps


def _revision_plan_from_steps(
    steps: tuple[Any, ...],
) -> tuple[str, set[str]]:
    commands: set[str] = set()
    revision_ids: set[str] = set()
    for step in steps:
        info = getattr(step, "info", None)
        if info is None or getattr(info, "is_stamp", False):
            return "", set()
        commands.add("upgrade" if info.is_upgrade else "downgrade")
        revision_ids.update(info.up_revision_ids)
    if len(commands) != 1:
        return "", set()
    return commands.pop(), revision_ids


def _cli_revision_plan(
    current_revision: str,
) -> tuple[str, set[str]]:
    options = getattr(config, "cmd_opts", None)
    command_spec = getattr(options, "cmd", None)
    target_revision = getattr(options, "revision", None)
    if (
        not isinstance(command_spec, tuple)
        or not command_spec
        or not isinstance(target_revision, str)
    ):
        return "", set()
    command = getattr(command_spec[0], "__name__", "")
    scripts = ScriptDirectory.from_config(config)
    if command == "upgrade":
        steps = scripts._upgrade_revs(target_revision, current_revision)
    elif command == "downgrade":
        steps = scripts._downgrade_revs(target_revision, current_revision)
    else:
        return "", set()
    return command, {
        step.revision.revision
        for step in steps
        if getattr(step, "revision", None) is not None
    }


def _pending_revision_ids(
    migration_context: Any,
    current_heads: tuple[str, ...],
) -> tuple[str, set[str]]:
    steps = _configured_revision_steps(migration_context, current_heads)
    if steps is not None:
        return _revision_plan_from_steps(steps)
    return _cli_revision_plan(current_heads[0])


def _prepare_concurrent_index_retry() -> None:
    migration_context = context.get_context()
    current_heads = tuple(migration_context.get_current_heads())
    if len(current_heads) != 1:
        return
    current_revision = current_heads[0]
    command, pending_revision_ids = _pending_revision_ids(
        migration_context,
        current_heads,
    )
    if not command or not pending_revision_ids:
        return
    prepare_concurrent_index_retry_boundary(
        Operations(migration_context),
        command=command,
        current_revision=current_revision,
        pending_revision_ids=pending_revision_ids,
    )


def _prepare_downgrade_guards(*, online: bool) -> None:
    migration_context = context.get_context()
    current_heads = tuple(migration_context.get_current_heads())
    if len(current_heads) != 1:
        return
    command, pending_revision_ids = _pending_revision_ids(
        migration_context,
        current_heads,
    )
    if command != "downgrade" or not pending_revision_ids:
        return
    if not online:
        if TELEGRAM_CONTROL_REVISION in pending_revision_ids:
            raise RuntimeError(
                "Telegram destructive downgrade requires an online database "
                "and the explicit export command"
            )
        return
    guard_telegram_downgrade(
        context.get_bind(),
        pending_revision_ids=pending_revision_ids,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        _prepare_downgrade_guards(online=False)
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": 10, "application_name": "alembic"},
    )
    with connectable.connect() as connection:
        # Fail-fast 防止活跃事务挡住 ALTER 等元数据操作 → 全局雪崩。
        # PG 默认 lock_timeout=0 (无限等); 一个 idle in transaction 就能让
        # ALTER 死等并把后续所有 query 排在它后面阻塞 (v1.0.51 现场踩过).
        # 5s 拿不到锁立刻 abort migration → update.sh 整体 fail-fast 不切
        # current 不重启服务, 旧 schema 继续跑, 比 hang 几小时强.
        # statement_timeout=120s 防 backfill UPDATE 巨表时全表锁过久.
        connection.exec_driver_sql("SET lock_timeout = '5s'")
        connection.exec_driver_sql("SET statement_timeout = '120s'")
        # SQLAlchemy 2 autobegins on the SET statements above. Commit that
        # setup transaction so Alembic owns and commits the migration DDL
        # transaction instead of rolling it back when the connection closes.
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            _prepare_downgrade_guards(online=True)
            _prepare_concurrent_index_retry()
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
