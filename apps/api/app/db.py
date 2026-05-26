"""异步 SQLAlchemy session factory。Base 从 lumen_core.models 复用。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from lumen_core.desktop_runtime import is_desktop_runtime


def _build_engine():
    if is_desktop_runtime(settings.lumen_runtime):
        db_path = settings.database_url.removeprefix("sqlite+aiosqlite:///")
        if db_path and not db_path.startswith(":memory:"):
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            connect_args={"timeout": 5},
            future=True,
        )
        from .db_desktop_ext import configure_sqlite_engine

        configure_sqlite_engine(engine)
        return engine
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        future=True,
    )


engine = _build_engine()
# Why: expire_on_commit=False keeps attribute access cheap after commit but
# means handlers must NOT trust column values they didn't set themselves to be
# refreshed — call `await db.refresh(obj)` if you need post-commit DB-side
# defaults / triggers, otherwise return responses built from in-memory values.
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
