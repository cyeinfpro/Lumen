"""Worker 侧 system_settings 解析（带 5s 内存缓存）。

Worker 调上游前用 `await resolve('providers')` 等取最终值；DB 没有则 fallback
到 config.py / env 值。

缓存粒度按 spec_key；TTL=5s 既保证响应足够快，也能让站长后台改完几秒就生效。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select

from lumen_core.models import SystemSetting
from lumen_core.runtime_settings import SettingSpec, get_spec

from .config import settings as _config_settings
from .db import SessionLocal
from .task_runtime import RuntimeSlot


logger = logging.getLogger(__name__)

_TTL_S = 5.0
_UNAVAILABLE_TTL_S = 1.0
# _read_db 是在 cache.lock 里跑的：DB 慢查询 / 锁等待会把同进程所有 settings
# 解析一起堵死（上游调用前几乎每条路径都要 resolve 一次）。PG 侧没有
# statement_timeout 时这个等待没有上限，所以本地加硬上限，超时按「DB 无值」
# 处理 —— resolve() 退回 env / config，行为与该 key 未写入 DB 时完全一致。
_DB_TIMEOUT_S = 5.0


class SettingUnavailable(RuntimeError):
    """The authoritative runtime setting store could not be read."""


@dataclass(frozen=True, slots=True)
class SettingResolution:
    state: Literal["value", "missing", "unavailable"]
    value: str | None = None
    source: Literal["database", "environment", "config", "none"] = "none"


# key -> (expires_at, typed resolution)
@dataclass(slots=True)
class RuntimeSettingsCache:
    values: dict[str, tuple[float, SettingResolution]] = field(default_factory=dict)
    db_only_values: dict[str, tuple[float, SettingResolution]] = field(
        default_factory=dict
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def shutdown(self) -> None:
        async with self.lock:
            self.values.clear()
            self.db_only_values.clear()


_RUNTIME_CACHE_SLOT: RuntimeSlot[RuntimeSettingsCache] = RuntimeSlot(
    "worker-runtime-settings-cache",
    default_factory=RuntimeSettingsCache,
)


def configure_cache(cache: RuntimeSettingsCache | None = None) -> RuntimeSettingsCache:
    runtime = cache or _RUNTIME_CACHE_SLOT.current()
    _RUNTIME_CACHE_SLOT.install_default(runtime)
    return runtime


async def shutdown_cache(cache: RuntimeSettingsCache) -> None:
    await cache.shutdown()
    _RUNTIME_CACHE_SLOT.clear_default(cache)


def _runtime_cache() -> RuntimeSettingsCache:
    return _RUNTIME_CACHE_SLOT.current()


def _config_fallback(spec: SettingSpec) -> SettingResolution:
    """先看 env（与 SettingSpec.env_fallback 同名），再看 config.py 的 settings 属性。"""
    env_val = os.environ.get(spec.env_fallback)
    if env_val is not None and env_val != "":
        return SettingResolution("value", env_val, "environment")
    # 把 'upstream.pixel_budget' 转成 'upstream_pixel_budget' 去 config.py 取
    attr = spec.key.replace(".", "_")
    val = getattr(_config_settings, attr, None)
    if val is None:
        return SettingResolution("missing")
    s = str(val)
    if s == "":
        return SettingResolution("missing")
    return SettingResolution("value", s, "config")


async def _read_db_state(spec_key: str) -> SettingResolution:
    try:
        async with SessionLocal() as session:
            row = (
                await asyncio.wait_for(
                    session.execute(
                        select(SystemSetting.value).where(SystemSetting.key == spec_key)
                    ),
                    _DB_TIMEOUT_S,
                )
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        # SQLAlchemy 把 CancelledError/TimeoutError 当 exit exception，会 invalidate
        # 这条连接而不是把半死的连接放回池子，所以这里超时是安全的。
        logger.error(
            "runtime settings DB read unavailable key=%s timeout=%ss",
            spec_key,
            _DB_TIMEOUT_S,
            exc_info=True,
        )
        return SettingResolution("unavailable")
    if row is None:
        return SettingResolution("missing")
    return SettingResolution("value", str(row), "database")


def _resolution_ttl(resolution: SettingResolution) -> float:
    return _UNAVAILABLE_TTL_S if resolution.state == "unavailable" else _TTL_S


def _resolved_value(
    spec_key: str,
    resolution: SettingResolution,
) -> str | None:
    if resolution.state == "unavailable":
        raise SettingUnavailable(f"runtime setting unavailable: {spec_key}")
    return resolution.value if resolution.state == "value" else None


async def _read_db(spec_key: str) -> str | None:
    """Compatibility wrapper around the typed DB resolution contract."""
    return _resolved_value(spec_key, await _read_db_state(spec_key))


async def resolve_state(spec_key: str) -> SettingResolution:
    """Resolve a setting without conflating missing and unavailable."""
    spec = get_spec(spec_key)
    if spec is None:
        return SettingResolution("missing")

    cache = _runtime_cache()
    now = time.monotonic()
    cached = cache.values.get(spec_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    async with cache.lock:
        # 双重检查
        cached = cache.values.get(spec_key)
        if cached is not None and cached[0] > now:
            return cached[1]

        db_resolution = await _read_db_state(spec_key)
        if db_resolution.state == "missing":
            resolution = _config_fallback(spec)
        else:
            resolution = db_resolution

        cache.values[spec_key] = (
            now + _resolution_ttl(resolution),
            resolution,
        )
        return resolution


async def resolve(spec_key: str) -> str | None:
    """Return the resolved value, raising when the control plane is unavailable."""
    return _resolved_value(spec_key, await resolve_state(spec_key))


async def resolve_db_state(spec_key: str) -> SettingResolution:
    """Return typed raw DB state, bypassing env/config fallback."""
    if get_spec(spec_key) is None:
        return SettingResolution("missing")
    cache = _runtime_cache()
    now = time.monotonic()
    cached = cache.db_only_values.get(spec_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    async with cache.lock:
        cached = cache.db_only_values.get(spec_key)
        if cached is not None and cached[0] > now:
            return cached[1]
        resolution = await _read_db_state(spec_key)
        cache.db_only_values[spec_key] = (
            now + _resolution_ttl(resolution),
            resolution,
        )
        return resolution


async def resolve_db(spec_key: str) -> str | None:
    """Return the raw DB value only, raising when the DB is unavailable."""
    return _resolved_value(spec_key, await resolve_db_state(spec_key))


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
    cache = _runtime_cache()
    cache.values.clear()
    cache.db_only_values.clear()


__all__ = [
    "RuntimeSettingsCache",
    "SettingResolution",
    "SettingUnavailable",
    "configure_cache",
    "resolve",
    "resolve_db",
    "resolve_db_state",
    "resolve_int",
    "resolve_state",
    "invalidate_cache",
    "shutdown_cache",
]
