"""异步 SQLAlchemy session factory。Base 从 lumen_core.models 复用。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings


def affected_rows(result: Any) -> int:
    rowcount = getattr(result, "rowcount", 0)
    return rowcount if isinstance(rowcount, int) else 0


def _build_engine():
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
