"""Worker 侧 async session factory（与 API 同样复用 lumen_core.models.Base）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from lumen_core.desktop_runtime import is_desktop_runtime

from .config import settings


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
    return create_async_engine(settings.database_url, pool_pre_ping=True, future=True)


engine = _build_engine()
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as s:
        yield s
