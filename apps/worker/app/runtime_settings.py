"""Worker 侧 system_settings 解析（带 5s 内存缓存）。

Worker 调上游前用 `await resolve('providers')` 等取最终值；DB 没有则 fallback
到 config.py / env 值。

缓存粒度按 spec_key；TTL=5s 既保证响应足够快，也能让站长后台改完几秒就生效。
"""

from __future__ import annotations

import asyncio
import os
import time

from sqlalchemy import select

from lumen_core.models import SystemSetting
from lumen_core.runtime_settings import SettingSpec, get_spec

from .config import settings as _config_settings
from .db import SessionLocal


_TTL_S = 5.0
# key -> (expires_at, raw_str_or_None)
_CACHE: dict[str, tuple[float, str | None]] = {}
_DB_ONLY_CACHE: dict[str, tuple[float, str | None]] = {}
_CACHE_LOCK = asyncio.Lock()


def _config_fallback(spec: SettingSpec) -> str | None:
    """先看 env（与 SettingSpec.env_fallback 同名），再看 config.py 的 settings 属性。"""
    env_val = os.environ.get(spec.env_fallback)
    if env_val is not None and env_val != "":
        return env_val
    # 把 'upstream.pixel_budget' 转成 'upstream_pixel_budget' 去 config.py 取
    attr = spec.key.replace(".", "_")
    val = getattr(_config_settings, attr, None)
    if val is None:
        return None
    s = str(val)
    return s if s != "" else None


async def _read_db(spec_key: str) -> str | None:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(SystemSetting.value).where(SystemSetting.key == spec_key)
            )
        ).scalar_one_or_none()
    if row is not None and row != "":
        return row
    return None


async def resolve(spec_key: str) -> str | None:
    """检查缓存；过期则查 DB；DB 无则 env / config fallback。"""
    spec = get_spec(spec_key)
    if spec is None:
        return None

    now = time.monotonic()
    cached = _CACHE.get(spec_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    async with _CACHE_LOCK:
        # 双重检查
        cached = _CACHE.get(spec_key)
        if cached is not None and cached[0] > now:
            return cached[1]

        db_val = await _read_db(spec_key)
        if db_val is not None:
            value: str | None = db_val
        else:
            value = _config_fallback(spec)

        _CACHE[spec_key] = (now + _TTL_S, value)
        return value


async def resolve_db(spec_key: str) -> str | None:
    """Return the raw DB value only, bypassing env/config fallback."""
    if get_spec(spec_key) is None:
        return None
    now = time.monotonic()
    cached = _DB_ONLY_CACHE.get(spec_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    async with _CACHE_LOCK:
        cached = _DB_ONLY_CACHE.get(spec_key)
        if cached is not None and cached[0] > now:
            return cached[1]
        value = await _read_db(spec_key)
        _DB_ONLY_CACHE[spec_key] = (now + _TTL_S, value)
        return value


async def resolve_int(spec_key: str, default: int) -> int:
    raw = await resolve(spec_key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def invalidate_cache() -> None:
    """主动清缓存（用于测试）。"""
    _CACHE.clear()
    _DB_ONLY_CACHE.clear()


def warmup_supported() -> list[str]:
    """返回当前缓存里所有命中的 keys（调试用）。"""
    return list(_CACHE.keys())


__all__ = [
    "resolve",
    "resolve_db",
    "resolve_int",
    "invalidate_cache",
    "warmup_supported",
]
