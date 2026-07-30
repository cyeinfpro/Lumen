"""Runtime-setting coercion for Telegram configuration routes."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.providers import parse_provider_bool
from lumen_core.runtime_settings import get_spec

from ..runtime_settings import get_setting


def bool_option(value: object, default: bool = False) -> bool:
    try:
        return parse_provider_bool(value, default=default)
    except ValueError:
        return default


async def get_setting_str(
    db: AsyncSession,
    key: str,
    default: str = "",
) -> str:
    spec = get_spec(key)
    if spec is None:
        return default
    raw = await get_setting(db, spec)
    if raw is None:
        return default
    return str(raw).strip()


async def get_setting_int(db: AsyncSession, key: str, default: int) -> int:
    spec = get_spec(key)
    if spec is None:
        return default
    raw = await get_setting(db, spec)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default
