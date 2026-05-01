"""Worker 侧 async session factory（与 API 同样复用 lumen_core.models.Base）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as s:
        yield s
