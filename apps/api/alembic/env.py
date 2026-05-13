"""Alembic env —— 读 app.config.settings.database_url；metadata 取自 lumen_core.models.Base。

注意 asyncpg 的同步/异步处理：alembic 自身是同步的，
因此 DATABASE_URL 里 `postgresql+asyncpg://` 在 migration 时改成同步驱动。"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

# 让 alembic 能 import app.* 与 lumen_core.*
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from lumen_core.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# migration 用同步驱动；用 URL parser 避免误改 username/password/path 里的字符串。
_db_url = make_url(settings.database_url)
if _db_url.drivername in {"postgresql+asyncpg", "postgresql"}:
    _db_url = _db_url.set(drivername="postgresql+psycopg2")
sync_url = _db_url.render_as_string(hide_password=False)
config.set_main_option("sqlalchemy.url", sync_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=sync_url,
        target_metadata=target_metadata,
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
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
