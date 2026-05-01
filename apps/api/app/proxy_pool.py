"""共享代理池：providers + telegram bot 都从这里挑 proxy。

Redis 存活：
  lumen:proxy:health:{name}  HASH  last_latency_ms / last_tested_at_iso / last_target
  lumen:proxy:fail:{name}    string INCR with TTL=300  连续失败计数
  lumen:proxy:cooldown:{name} string TTL=N  冷却中（被踢出 pool）
  lumen:proxy:rr:idx          INCR        round_robin 计数器

策略：
  - random       从 healthy 集随机一个
  - latency      取 last_latency_ms 最低的 1/3 候选随机一个；缺测的视为 +inf
  - failover     按入参 candidates 顺序，第一个 healthy 的就用
  - round_robin  全局 INCR % len(healthy)

healthy = enabled 且 不在 cooldown 且 不在 caller 的 avoid set。
若全部不可用则降级到 enabled+不在 avoid 的全集（cooldown 不阻塞），
再不行返回 None。
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from typing import Any, Iterable

from lumen_core.providers import ProviderProxyDefinition, resolve_provider_proxy_url

logger = logging.getLogger(__name__)

DEFAULT_STRATEGY = "random"
ALLOWED_STRATEGIES = ("random", "latency", "failover", "round_robin")
DEFAULT_TEST_TARGET = "https://api.telegram.org"

_HEALTH_PREFIX = "lumen:proxy:health:"
_FAIL_PREFIX = "lumen:proxy:fail:"
_COOLDOWN_PREFIX = "lumen:proxy:cooldown:"
_RR_KEY = "lumen:proxy:rr:idx"
_FAIL_TTL_SECONDS = 300


def health_key(name: str) -> str:
    return f"{_HEALTH_PREFIX}{name}"


def cooldown_key(name: str) -> str:
    return f"{_COOLDOWN_PREFIX}{name}"


def fail_key(name: str) -> str:
    return f"{_FAIL_PREFIX}{name}"


async def _is_in_cooldown(redis: Any, name: str) -> bool:
    try:
        return bool(await redis.exists(cooldown_key(name)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("proxy cooldown check failed name=%s err=%s", name, exc)
        return False


async def get_health(redis: Any, name: str) -> dict[str, Any]:
    try:
        raw = await redis.hgetall(health_key(name))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        ks = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
        vs = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        out[ks] = vs
    if "last_latency_ms" in out:
        try:
            out["last_latency_ms"] = float(out["last_latency_ms"])
        except ValueError:
            out["last_latency_ms"] = None
    return out


async def set_health(redis: Any, name: str, *, latency_ms: float, target: str) -> None:
    try:
        await redis.hset(
            health_key(name),
            mapping={
                "last_latency_ms": f"{latency_ms:.1f}",
                "last_tested_at": datetime.now(timezone.utc).isoformat(),
                "last_target": target,
            },
        )
        # 24h 过期，避免长期不再测的旧数据干扰 strategy=latency 选择
        await redis.expire(health_key(name), 86400)
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_health failed name=%s err=%s", name, exc)


async def report_failure(
    redis: Any,
    name: str,
    *,
    failure_threshold: int,
    cooldown_seconds: int,
) -> bool:
    """记录一次失败；返回 True 代表本次触发了冷却（达到阈值）。"""
    if not name:
        return False
    try:
        n = int(await redis.incr(fail_key(name)))
        await redis.expire(fail_key(name), _FAIL_TTL_SECONDS)
        if n >= failure_threshold:
            await redis.set(cooldown_key(name), b"1", ex=cooldown_seconds)
            await redis.delete(fail_key(name))
            logger.warning(
                "proxy %s into cooldown after %d failures (cooldown=%ds)",
                name, n, cooldown_seconds,
            )
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("report_failure failed name=%s err=%s", name, exc)
    return False


async def report_success(redis: Any, name: str) -> None:
    if not name:
        return
    try:
        await redis.delete(fail_key(name))
    except Exception:  # noqa: BLE001
        pass


async def pick_proxy(
    redis: Any,
    candidates: list[ProviderProxyDefinition],
    *,
    strategy: str = DEFAULT_STRATEGY,
    avoid: Iterable[str] = (),
) -> ProviderProxyDefinition | None:
    """从 candidates（已是按入参顺序的有序列表）中挑一个。失败返回 None。"""
    avoid_set = {a for a in avoid if a}
    enabled = [p for p in candidates if p.enabled and p.name not in avoid_set]
    if not enabled:
        return None

    healthy: list[ProviderProxyDefinition] = []
    for p in enabled:
        if not await _is_in_cooldown(redis, p.name):
            healthy.append(p)
    pool = healthy or enabled  # 全冷却时降级到 enabled，避免完全不可用

    if strategy not in ALLOWED_STRATEGIES:
        strategy = DEFAULT_STRATEGY

    if strategy == "failover":
        return pool[0]
    if strategy == "random":
        return random.choice(pool)
    if strategy == "round_robin":
        try:
            idx = int(await redis.incr(_RR_KEY))
        except Exception:  # noqa: BLE001
            idx = random.randint(0, len(pool) - 1)
        return pool[idx % len(pool)]
    if strategy == "latency":
        # 取最低延迟的 1/3 候选；未测过的延迟视为 inf
        latencies: list[tuple[ProviderProxyDefinition, float]] = []
        for p in pool:
            h = await get_health(redis, p.name)
            ms = h.get("last_latency_ms")
            latencies.append((p, ms if isinstance(ms, (int, float)) else float("inf")))
        latencies.sort(key=lambda x: x[1])
        keep = max(1, len(latencies) // 3)
        top = [p for p, _ in latencies[:keep]]
        return random.choice(top)

    return pool[0]


async def measure_latency(
    proxy: ProviderProxyDefinition,
    *,
    target: str,
    timeout_s: float = 10.0,
) -> tuple[float, str | None]:
    """通过指定 proxy 发 HEAD 到 target，返回 (latency_ms, error_msg|None)。

    SOCKS5 走 httpx[socks]；SSH 通过 resolve_provider_proxy_url 临时起本地隧道。
    """
    import time

    import httpx

    proxy_url = await resolve_provider_proxy_url(proxy)
    if not proxy_url:
        return (-1.0, "proxy resolve failed")
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 5.0)),
            follow_redirects=False,
        ) as client:
            resp = await client.head(target)
            # 任何 HTTP 响应（含 4xx/5xx）都说明 SOCKS 通路完整建立，记为成功
            return ((time.monotonic() - start) * 1000.0, None)
    except Exception as exc:  # noqa: BLE001
        return ((time.monotonic() - start) * 1000.0, f"{type(exc).__name__}: {exc}")


__all__ = [
    "ALLOWED_STRATEGIES",
    "DEFAULT_STRATEGY",
    "DEFAULT_TEST_TARGET",
    "get_health",
    "measure_latency",
    "pick_proxy",
    "report_failure",
    "report_success",
    "set_health",
]
