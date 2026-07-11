"""Worker 侧 async session factory（与 API 同样复用 lumen_core.models.Base）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings


def _build_engine():
    return create_async_engine(settings.database_url, pool_pre_ping=True, future=True)


def affected_rows(result: Any) -> int:
    rowcount = getattr(result, "rowcount", 0)
    return rowcount if isinstance(rowcount, int) else 0


engine = _build_engine()
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
