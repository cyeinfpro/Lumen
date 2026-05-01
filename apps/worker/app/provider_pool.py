"""上游供应商池：多 provider 加权轮询 + 断路器 + 主动探活 + 请求统计。

单 provider 时退化为当前行为（零开销）。多 provider 时：
- priority 降序分组，组内 weight 加权轮询
- 断路器：连续 3 次 retriable 失败 → 熔断，冷却后 half-open 探测
- failover：retriable 错误立即切下一个 provider（零延迟）
- 探活 cron：间隔可配置（默认 120s，可关闭），cron 每 30s 执行统计刷入
- 请求统计：per-provider total/success/fail 计数，定期刷入 Redis
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import InitVar, dataclass, field
from typing import Any

import httpx
from lumen_core.constants import GenerationErrorCode as EC
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderProxyDefinition,
    build_effective_provider_config,
    endpoint_kind_allowed,
    resolve_provider_proxy_url,
)

from .config import settings as _cfg
from .validation import validate_provider_base_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 断路器常量
# ---------------------------------------------------------------------------
_CB_FAILURE_THRESHOLD = 3
_CB_COOLDOWN_BASE_S = 30.0
_CB_COOLDOWN_MAX_S = 300.0
_PROBE_TIMEOUT_S = 15.0
_CONFIG_TTL_S = 5.0

# image route 独立熔断（多账号场景下 image 失败 3 次内只熔该账号，不影响 text）
_IMAGE_CB_FAILURE_THRESHOLD = 3
_IMAGE_CB_COOLDOWN_S = 10.0
# 上游显式 429 / quota 没给 retry-after 时的兜底冷却时长
_IMAGE_RATE_LIMITED_DEFAULT_S = 60.0

# image probe：发一张 1024x1024 低质量图，真返回 base64 才算 healthy。
# **默认 0 = 关闭**（生产先关，确认账号配额能扛住后再调高，建议 ≥ 1800s/30min）。
# 每张 probe 都会消耗一次账号 OpenAI 配额。
# 通过 runtime_settings: providers.auto_image_probe_interval 调整（admin API 写 DB
# → 5s TTL 自动生效；或 env PROVIDERS_AUTO_IMAGE_PROBE_INTERVAL fallback）。
_DEFAULT_IMAGE_PROBE_INTERVAL_S = 0
_PROBE_INSTRUCTIONS = "You are a precise calculator. Return only the final integer."
_IMAGE_PROBE_PROMPT = "a small red apple on a white background"
_IMAGE_PROBE_SIZE = "1024x1024"
_IMAGE_PROBE_QUALITY = "low"
# 真图 base64 最少 ~5KB（1024x1024 webp 低质量）；这里取 1000 做下限保护
_IMAGE_PROBE_MIN_B64_LEN = 1000


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: InitVar[str]
    priority: int = 0
    weight: int = 1
    enabled: bool = True
    proxy_name: str | None = None
    proxy: ProviderProxyDefinition | None = field(
        default=None, repr=False, compare=False
    )
    image_rate_limit: str | None = None    # "5/min" / "50/h" / "200/d"
    image_daily_quota: int | None = None
    image_jobs_enabled: bool = False
    image_jobs_endpoint: str = "auto"      # "auto" | "generations" | "responses"
    # 锁定模式：当 endpoint != auto 且 lock=True，本号只服务对应 endpoint，
    # 选号阶段过滤掉对端请求，failover 也不再回退到对端。
    image_jobs_endpoint_lock: bool = False
    image_jobs_base_url: str = ""          # empty → fall back to global image.job_base_url
    image_concurrency: int = 1             # 每 provider 同时进行的图片任务上限
    _api_key: str = field(init=False, repr=False, compare=False)

    def __post_init__(self, api_key: str) -> None:
        object.__setattr__(self, "_api_key", api_key)

    @property
    def api_key(self) -> str:
        return self._api_key


@dataclass
class EndpointStat:
    """Per (provider, image-job endpoint) health used by `auto` route selection.

    Tracked in-memory only (lost on worker restart). Reflects how a provider
    has behaved on a specific upstream endpoint — generations vs responses —
    so auto mode can prefer the historically-faster path before falling back.
    """
    last_success_at: float | None = None
    last_failure_at: float | None = None
    consecutive_failures: int = 0
    successes: int = 0
    failures: int = 0
    # Welford-style running mean (ms) over successful requests; cheap, robust
    # to outliers compared to a moving window.
    success_count: int = 0
    success_mean_ms: float = 0.0


@dataclass
class ProviderHealth:
    consecutive_failures: int = 0
    last_failure_at: float | None = None
    last_success_at: float | None = None
    last_probe_at: float | None = None
    cooldown_until: float | None = None
    # 真实请求统计（内存计数，定期刷入 Redis）
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    # image route 独立健康（让文本健康不污染生图选号）
    image_consecutive_failures: int = 0
    image_cooldown_until: float | None = None
    image_last_used_at: float | None = None
    # OpenAI 上游 429 / quota 触发的硬冷却（按 retry-after 头设置）
    image_rate_limited_until: float | None = None
    # image-job per-endpoint 统计（仅 image_jobs 路由用到；其他路径不影响）
    endpoint_stats: dict[str, EndpointStat] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedProvider:
    name: str
    base_url: str
    api_key: InitVar[str]
    proxy: ProviderProxyDefinition | None = field(
        default=None, repr=False, compare=False
    )
    image_jobs_enabled: bool = False
    image_jobs_endpoint: str = "auto"
    image_jobs_endpoint_lock: bool = False
    image_jobs_base_url: str = ""
    image_concurrency: int = 1
    _api_key: str = field(init=False, repr=False, compare=False)

    def __post_init__(self, api_key: str) -> None:
        object.__setattr__(self, "_api_key", api_key)

    @property
    def api_key(self) -> str:
        return self._api_key


# ---------------------------------------------------------------------------
# Provider Base URL 格式校验（独立模块，避免 provider_pool 运行时导入 upstream）
# ---------------------------------------------------------------------------
async def _validate_provider_base_url(raw_base: str) -> str:
    return await validate_provider_base_url(raw_base)


# ---------------------------------------------------------------------------
# ProviderPool
# ---------------------------------------------------------------------------
class ProviderPool:

    def __init__(self) -> None:
        self._providers: list[ProviderConfig] = []
        self._proxies: dict[str, ProviderProxyDefinition] = {}
        self._health: dict[str, ProviderHealth] = {}
        self._config_loaded_at: float = 0.0
        self._config_lock = asyncio.Lock()
        self._stats_lock = threading.Lock()
        self._rr_counters: dict[int, int] = {}
        # arq ctx['redis']；由 generation 任务在执行前 attach_redis() 注入。
        # 没注入时 image route 的 quota 检查会短路（让 limiter 不阻塞主路径）。
        self._redis: Any = None

    def attach_redis(self, redis: Any) -> None:
        """注入 redis 客户端供 image route quota 检查 / 入账使用。

        多次调用幂等——只缓存最后一个引用（arq 的 ctx['redis'] 在 worker 生命周期内
        是稳定的）。
        """
        self._redis = redis

    def get_redis(self) -> Any:
        """Public accessor for the attached Redis client; absent returns None."""
        return self._redis

    # ---- 配置加载 --------------------------------------------------------

    async def _load_provider_config(
        self,
    ) -> tuple[list[ProviderConfig], dict[str, ProviderProxyDefinition]]:
        from .runtime_settings import resolve

        raw = await resolve("providers")
        if not raw:
            raw = os.environ.get("PROVIDERS") or _cfg.providers
        provider_defs, proxy_defs, errors = build_effective_provider_config(
            raw_providers=raw,
            legacy_base_url=(
                os.environ.get("UPSTREAM_BASE_URL")
                or DEFAULT_LEGACY_PROVIDER_BASE_URL
            ),
            legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
        )
        for err in errors:
            logger.warning("%s", err)
        return [
            ProviderConfig(
                name=p.name,
                base_url=p.base_url,
                api_key=p.api_key,
                priority=p.priority,
                weight=p.weight,
                enabled=p.enabled,
                proxy_name=p.proxy_name,
                proxy=p.proxy,
                image_rate_limit=p.image_rate_limit,
                image_daily_quota=p.image_daily_quota,
                image_jobs_enabled=p.image_jobs_enabled,
                image_jobs_endpoint=p.image_jobs_endpoint,
                image_jobs_endpoint_lock=p.image_jobs_endpoint_lock,
                image_jobs_base_url=p.image_jobs_base_url,
                image_concurrency=p.image_concurrency,
            )
            for p in provider_defs
        ], {p.name: p for p in proxy_defs}

    async def _load_config(self) -> list[ProviderConfig]:
        providers, _proxies = await self._load_provider_config()
        return providers

    async def _maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self._config_loaded_at < _CONFIG_TTL_S:
            return
        async with self._config_lock:
            if now - self._config_loaded_at < _CONFIG_TTL_S:
                return
            new_providers, new_proxies = await self._load_provider_config()
            validated: list[ProviderConfig] = []
            for p in new_providers:
                try:
                    url = await _validate_provider_base_url(p.base_url)
                    validated.append(
                        ProviderConfig(
                            name=p.name,
                            base_url=url,
                            api_key=p.api_key,
                            priority=p.priority,
                            weight=p.weight,
                            enabled=p.enabled,
                            proxy_name=p.proxy_name,
                            proxy=p.proxy,
                            image_rate_limit=p.image_rate_limit,
                            image_daily_quota=p.image_daily_quota,
                            image_jobs_enabled=p.image_jobs_enabled,
                            image_jobs_endpoint=p.image_jobs_endpoint,
                            image_jobs_endpoint_lock=p.image_jobs_endpoint_lock,
                            image_jobs_base_url=p.image_jobs_base_url,
                            image_concurrency=p.image_concurrency,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "provider %s base URL validation failed, skipping: %s",
                        p.name,
                        exc,
                    )
            if not validated:
                if self._providers:
                    logger.error(
                        "all providers failed validation, keeping previous config"
                    )
                    self._config_loaded_at = now
                    return
                from .upstream import UpstreamError

                raise UpstreamError(
                    "no valid upstream providers",
                    error_code=EC.NO_PROVIDERS.value,
                    status_code=503,
                )

            old_names = set(self._health.keys())
            new_names = {p.name for p in validated}
            for removed in old_names - new_names:
                del self._health[removed]
            for name in new_names - old_names:
                self._health[name] = ProviderHealth()

            changed = (
                [p.name for p in self._providers] != [p.name for p in validated]
            )
            self._providers = validated
            self._proxies = new_proxies
            self._config_loaded_at = now
            if changed:
                desc = ", ".join(
                    f"{p.name}(p={p.priority},w={p.weight},proxy={p.proxy_name or 'none'})"
                    for p in validated
                )
                logger.info("provider_pool reloaded: providers=[%s]", desc)

    # ---- 选择算法 --------------------------------------------------------

    def _select_ordered(
        self,
        *,
        endpoint_kind: str | None = None,
    ) -> list[ResolvedProvider]:
        from .upstream import UpstreamError

        enabled = [p for p in self._providers if p.enabled]
        if not enabled:
            raise UpstreamError(
                "no upstream providers configured or all disabled",
                error_code=EC.NO_PROVIDERS.value,
                status_code=503,
            )

        by_priority: dict[int, list[ProviderConfig]] = {}
        for p in enabled:
            if not endpoint_kind_allowed(p, endpoint_kind):
                continue
            by_priority.setdefault(p.priority, []).append(p)
        if not by_priority:
            raise UpstreamError(
                f"no upstream providers available for endpoint kind={endpoint_kind}",
                error_code=EC.NO_PROVIDERS.value,
                status_code=503,
            )
        priority_levels = sorted(by_priority.keys(), reverse=True)

        now = time.monotonic()
        result: list[ResolvedProvider] = []

        for prio in priority_levels:
            group = by_priority[prio]
            ordered = self._weighted_round_robin(group)

            healthy: list[ProviderConfig] = []
            half_open: list[ProviderConfig] = []
            circuit_open: list[ProviderConfig] = []

            for p in ordered:
                h = self._health.get(p.name)
                if h is None or not self._is_open(h, now):
                    healthy.append(p)
                elif h.cooldown_until is not None and now >= h.cooldown_until:
                    half_open.append(p)
                else:
                    circuit_open.append(p)

            # 按冷却开始时间排 circuit_open（最早的最可能已恢复）
            circuit_open.sort(
                key=lambda p: self._health[p.name].cooldown_until or float("inf")
            )

            for p in healthy + half_open + circuit_open:
                result.append(
                    ResolvedProvider(
                        name=p.name,
                        base_url=p.base_url,
                        api_key=p.api_key,
                        proxy=p.proxy,
                        image_jobs_endpoint=p.image_jobs_endpoint,
                        image_jobs_endpoint_lock=p.image_jobs_endpoint_lock,
                        image_jobs_base_url=p.image_jobs_base_url,
                        image_concurrency=p.image_concurrency,
                    )
                )

        if not result:
            raise UpstreamError(
                "no upstream providers available",
                error_code=EC.NO_PROVIDERS.value,
                status_code=503,
            )
        return result

    def _weighted_round_robin(
        self, group: list[ProviderConfig]
    ) -> list[ProviderConfig]:
        if len(group) <= 1:
            return list(group)
        prio = group[0].priority
        total_weight = sum(p.weight for p in group)
        counter = self._rr_counters.get(prio, 0)

        expanded: list[ProviderConfig] = []
        for p in group:
            expanded.extend([p] * p.weight)

        offset = counter % max(total_weight, 1)
        rotated = expanded[offset:] + expanded[:offset]

        seen: set[str] = set()
        result: list[ProviderConfig] = []
        for p in rotated:
            if p.name not in seen:
                seen.add(p.name)
                result.append(p)

        self._rr_counters[prio] = counter + 1
        return result

    @staticmethod
    def _is_open(h: ProviderHealth, now: float) -> bool:
        if h.consecutive_failures < _CB_FAILURE_THRESHOLD:
            return False
        # P2-7: failures 已达阈值但 cooldown_until 还未赋值——这是 report_failure
        # 中"先 incr 后赋值"的窗口期；并发请求若此时进入会绕过断路器。视为 open
        # 拒绝请求，等同时刻的 report_failure 完成赋值后转入正常 cooldown。
        if h.cooldown_until is None:
            return True
        return now < h.cooldown_until

    # ---- 公开 API --------------------------------------------------------

    def has_image_jobs_provider(self) -> bool:
        """True iff at least one enabled provider opted into image_jobs.

        Kept for admin/status callers. Dispatch no longer uses this as a
        global gate; it selects a provider first, then decides image_jobs vs
        stream from that provider's own ``image_jobs_enabled`` flag.
        """
        return any(p.enabled and p.image_jobs_enabled for p in self._providers)

    def enabled_provider_names(self) -> list[str]:
        """所有 enabled provider 的 name 列表，按配置顺序保留。

        供 task 层计算"还有多少未试号"——避免外部直接访问 _providers。
        """
        return [p.name for p in self._providers if p.enabled]

    async def select(
        self,
        *,
        route: str = "text",
        ignore_cooldown: bool = False,
        task_id: str | None = None,
        endpoint_kind: str | None = None,
    ) -> list[ResolvedProvider]:
        await self._maybe_reload()
        if route == "image":
            return await self._select_for_image(
                ignore_cooldown=ignore_cooldown,
                task_id=task_id,
                endpoint_kind=endpoint_kind,
            )
        if route == "image_jobs":
            return await self._select_for_image(
                ignore_cooldown=ignore_cooldown,
                task_id=task_id,
                endpoint_kind=endpoint_kind,
            )
        if route == "text":
            endpoint_kind = endpoint_kind or "responses"
        return self._select_ordered(endpoint_kind=endpoint_kind)

    async def select_one(self, *, route: str = "text") -> ResolvedProvider:
        providers = await self.select(route=route)
        return providers[0]

    def _record_request_stats(
        self,
        h: ProviderHealth,
        *,
        total: int = 0,
        success: int = 0,
        fail: int = 0,
    ) -> None:
        with self._stats_lock:
            h.total_requests += total
            h.successful_requests += success
            h.failed_requests += fail

    async def _select_for_image(
        self,
        *,
        ignore_cooldown: bool = False,
        task_id: str | None = None,
        endpoint_kind: str | None = None,
    ) -> list[ResolvedProvider]:
        """按账号视角选号：跳过熔断 / image cooldown / 配额耗尽的账号，
        剩余候选按 `image_last_used_at` 升序（最久未用优先）轮询。

        ignore_cooldown=True 时，单任务遍历场景下跳过 image_cooldown / image_rate_limited
        过滤——上次失败不代表下次失败，让任务把所有 enabled 账号都试一遍。
        text 熔断（auth 类硬故障）和 quota 耗尽仍然过滤。

        这一层是 sub2api"一号一 key"部署形态下的核心调度：让多次生图请求自然
        分散到不同账号，避免单号被打到 OpenAI quota 上限。配额检查走
        account_limiter（rate_limit/daily_quota 都为空时短路，让"先放开跑"的
        策略不查 Redis）。
        """
        from .upstream import UpstreamError

        enabled = [p for p in self._providers if p.enabled]
        if not enabled:
            raise UpstreamError(
                "no upstream providers configured or all disabled",
                error_code=EC.NO_PROVIDERS.value,
                status_code=503,
            )

        now = time.monotonic()
        # account_limiter 用 wall clock；ProviderHealth 时间戳用 monotonic。两套时间不
        # 互相参与计算，此处分别 cache。
        wall_now = time.time()
        redis = self.get_redis()

        from . import account_limiter

        # P1-8: 读取本 task 的 avoid set——上次失败的 provider 不应在同一 task
        # 里被反复重选。avoid set 由 generation.py 在 retry 前写入；这里用
        # set 包裹做 O(1) 查找。Redis 抖动时 fail-open（空 set，按原逻辑选号）。
        avoided: set[str] = set()
        if task_id and redis is not None:
            try:
                raw = await redis.smembers(
                    f"generation:image_queue:avoid:{task_id}"
                )
                for item in raw or []:
                    if isinstance(item, bytes):
                        try:
                            avoided.add(item.decode("utf-8", "replace"))
                        except Exception:  # noqa: BLE001
                            pass
                    elif isinstance(item, str):
                        avoided.add(item)
            except Exception:  # noqa: BLE001
                avoided = set()

        candidates: list[tuple[ProviderConfig, float]] = []
        skipped: list[tuple[str, str]] = []

        for p in enabled:
            # lock 检查放最前：被锁的号在本 endpoint kind 下连 ProviderHealth
            # 都不必创建，避免内存里堆积永远用不到的 health 计数。
            if not endpoint_kind_allowed(p, endpoint_kind):
                skipped.append((p.name, f"endpoint_locked_to_{p.image_jobs_endpoint}"))
                continue
            h = self._health.setdefault(p.name, ProviderHealth())
            if p.name in avoided:
                skipped.append((p.name, "avoided_from_previous_attempt"))
                continue
            if self._is_open(h, now):
                skipped.append((p.name, "text_circuit_open"))
                continue
            if (
                not ignore_cooldown
                and h.image_cooldown_until is not None
                and now < h.image_cooldown_until
            ):
                skipped.append((p.name, "image_cooldown"))
                continue
            if (
                not ignore_cooldown
                and h.image_rate_limited_until is not None
                and now < h.image_rate_limited_until
            ):
                skipped.append((p.name, "image_rate_limited"))
                continue
            # 配额检查：rate_limit / daily_quota 都为空时 limiter 短路返回 (True, 0)
            try:
                allowed, retry_after = await account_limiter.check_quota(
                    redis,
                    p.name,
                    p.image_rate_limit,
                    p.image_daily_quota,
                    now=wall_now,
                )
            except Exception as exc:  # noqa: BLE001
                # limiter 异常按"放开"处理（fail-open）：quota 是软约束，不应让
                # Redis 短暂抖动阻塞 image 主路径；记 warning 让运维看到。
                logger.warning(
                    "account_limiter.check_quota raised provider=%s err=%s — "
                    "treating as allowed",
                    p.name,
                    exc,
                )
                allowed, retry_after = True, 0.0
            if not allowed:
                # 缓存到 image_rate_limited_until，避免下次选号再查 Redis
                h.image_rate_limited_until = now + max(1.0, retry_after)
                skipped.append((p.name, f"quota_exhausted retry_after={retry_after:.0f}s"))
                continue
            # None（从未用过）排最前；不能用 `or 0.0`，因为 monotonic() 在
            # 进程启动初期可能 < 0 之外的任意正小值，会把"从未用过"误排到中间。
            sort_key = (
                h.image_last_used_at
                if h.image_last_used_at is not None
                else float("-inf")
            )
            candidates.append((p, sort_key))

        if not candidates:
            # P1-8: 若所有 enabled provider 都在 avoid set 里，退化为忽略 avoid
            # 重新选号——避免单 task 因为把所有号都试过一遍后再也无号可用而永久卡住。
            # 调用方（generation.py）已有同样的 fallback；这里是双保险。
            avoided_only = avoided and all(
                reason == "avoided_from_previous_attempt" for _, reason in skipped
            )
            if avoided_only:
                logger.info(
                    "image avoid set fully overlaps providers task=%s avoided=%s — "
                    "ignoring avoid",
                    task_id,
                    sorted(avoided),
                )
                return await self._select_for_image(
                    ignore_cooldown=ignore_cooldown,
                    task_id=None,
                    endpoint_kind=endpoint_kind,
                )
            detail = ", ".join(f"{name}({reason})" for name, reason in skipped) or "none"
            raise UpstreamError(
                f"all accounts unavailable for image: {detail}",
                error_code=EC.ALL_ACCOUNTS_FAILED.value,
                status_code=503,
                payload={"skipped": skipped},
            )

        # 最久未用优先；并列再按 priority(降序)/weight 加权 RR 维持长跑公平。
        # 注：endpoint_kind lock 过滤已在 candidate gather 阶段（上方循环里
        # endpoint_kind_allowed）完成，到这里 candidates 必然只剩可用号；不再做
        # 二次过滤，避免维护双份失败信息。
        candidates.sort(key=lambda x: (x[1], -x[0].priority))
        return [
            ResolvedProvider(
                name=p.name,
                base_url=p.base_url,
                api_key=p.api_key,
                proxy=p.proxy,
                image_jobs_enabled=p.image_jobs_enabled,
                image_jobs_endpoint=p.image_jobs_endpoint,
                image_jobs_endpoint_lock=p.image_jobs_endpoint_lock,
                image_jobs_base_url=p.image_jobs_base_url,
                image_concurrency=p.image_concurrency,
            )
            for p, _ in candidates
        ]

    def report_success(self, provider_name: str, *, is_probe: bool = False) -> None:
        h = self._health.get(provider_name)
        if h is None:
            return
        was_open = h.consecutive_failures >= _CB_FAILURE_THRESHOLD
        h.consecutive_failures = 0
        h.last_success_at = time.monotonic()
        h.cooldown_until = None
        if not is_probe:
            self._record_request_stats(h, total=1, success=1)
        if was_open:
            logger.info("circuit_closed: provider=%s recovered", provider_name)

    def report_failure(self, provider_name: str, *, is_probe: bool = False) -> None:
        h = self._health.get(provider_name)
        if h is None:
            return
        now = time.monotonic()
        h.consecutive_failures += 1
        h.last_failure_at = now
        if not is_probe:
            self._record_request_stats(h, total=1, fail=1)
        if h.consecutive_failures >= _CB_FAILURE_THRESHOLD:
            multiplier = min(h.consecutive_failures - _CB_FAILURE_THRESHOLD + 1, 10)
            duration = min(_CB_COOLDOWN_BASE_S * multiplier, _CB_COOLDOWN_MAX_S)
            h.cooldown_until = now + duration
            logger.warning(
                "circuit_open: provider=%s failures=%d cooldown=%.0fs",
                provider_name,
                h.consecutive_failures,
                duration,
            )

    # ---- image route 专用上报 -------------------------------------------

    def report_image_success(self, provider_name: str) -> None:
        """成功一次 image_generation：清空 image 失败计数 + 记 last_used_at。

        不会重置 image_rate_limited_until——那是上游 quota 强约束，必须等到
        retry-after 过去；此处只动 health 维度。
        """
        h = self._health.setdefault(provider_name, ProviderHealth())
        was_image_open = h.image_consecutive_failures >= _IMAGE_CB_FAILURE_THRESHOLD
        now = time.monotonic()
        h.image_consecutive_failures = 0
        h.image_cooldown_until = None
        h.image_last_used_at = now
        h.last_success_at = now  # 同时更新全局 last_success_at（探活逻辑会用）
        self._record_request_stats(h, total=1, success=1)
        try:
            from .observability import account_image_calls_total

            account_image_calls_total.labels(
                account=provider_name, outcome="success"
            ).inc()
        except Exception:  # noqa: BLE001
            pass
        if was_image_open:
            logger.info(
                "image_circuit_closed: provider=%s recovered", provider_name
            )

    def report_image_failure(self, provider_name: str) -> None:
        """失败一次 image_generation（普通 retriable，比如 SSE response.failed / 5xx）。

        累计 _IMAGE_CB_FAILURE_THRESHOLD 次 → image cooldown _IMAGE_CB_COOLDOWN_S。
        text route 不受影响（让"该号生图差但文本健康"的情况能继续跑文本）。
        """
        h = self._health.setdefault(provider_name, ProviderHealth())
        now = time.monotonic()
        h.image_consecutive_failures += 1
        h.last_failure_at = now
        self._record_request_stats(h, total=1, fail=1)
        try:
            from .observability import account_image_calls_total

            account_image_calls_total.labels(
                account=provider_name, outcome="failure"
            ).inc()
        except Exception:  # noqa: BLE001
            pass
        if h.image_consecutive_failures >= _IMAGE_CB_FAILURE_THRESHOLD:
            h.image_cooldown_until = now + _IMAGE_CB_COOLDOWN_S
            logger.warning(
                "image_circuit_open: provider=%s image_failures=%d cooldown=%.0fs",
                provider_name,
                h.image_consecutive_failures,
                _IMAGE_CB_COOLDOWN_S,
            )

    # ---- image-job per-endpoint stats ------------------------------------
    #
    # auto-mode endpoint selection wants to know how a provider has historically
    # behaved on each upstream endpoint (generations vs responses), so it can
    # prefer the faster / more reliable one and fall back fast otherwise.

    def _endpoint_stat(self, provider_name: str, endpoint: str) -> EndpointStat:
        h = self._health.setdefault(provider_name, ProviderHealth())
        return h.endpoint_stats.setdefault(endpoint, EndpointStat())

    def record_endpoint_success(
        self, provider_name: str, endpoint: str, *, latency_ms: float | None = None
    ) -> None:
        stat = self._endpoint_stat(provider_name, endpoint)
        stat.last_success_at = time.monotonic()
        stat.consecutive_failures = 0
        stat.successes += 1
        if latency_ms is not None and latency_ms > 0:
            stat.success_count += 1
            # Welford running mean — O(1), no list of samples to bound.
            stat.success_mean_ms += (latency_ms - stat.success_mean_ms) / stat.success_count

    def record_endpoint_failure(self, provider_name: str, endpoint: str) -> None:
        stat = self._endpoint_stat(provider_name, endpoint)
        stat.last_failure_at = time.monotonic()
        stat.consecutive_failures += 1
        stat.failures += 1

    def endpoint_chain(
        self,
        provider_name: str,
        action: str,
        configured: str,
    ) -> list[str]:
        """Return the ordered endpoints the failover layer should attempt.

        ``configured`` is the per-provider preference (auto / generations /
        responses). For ``auto`` we score each candidate against historical
        stats and put the more promising one first. For explicit values we
        return only that endpoint — the caller asked us not to second-guess.

        Endpoints returned are the high-level kind ("generations" / "responses").
        Translation to the actual sidecar URL path (``/v1/images/edits`` etc.)
        happens in the upstream layer because it depends on action.
        """
        if configured == "generations":
            return ["generations"]
        if configured == "responses":
            return ["responses"]
        # auto: rank both, with sane defaults when we have no history yet.
        candidates = ["generations", "responses"]
        h = self._health.get(provider_name)
        now = time.monotonic()

        def _score(endpoint: str) -> tuple[int, float, float]:
            # Lower is better. Tuple ordering: (consecutive_failure_penalty,
            # mean_latency_ms_with_default, recency_penalty).
            if h is None:
                stat = EndpointStat()
            else:
                stat = h.endpoint_stats.get(endpoint, EndpointStat())
            penalty = stat.consecutive_failures
            # If this endpoint failed in the last 60s prefer the alternative
            # even if its mean latency is higher.
            if stat.last_failure_at is not None and now - stat.last_failure_at < 60:
                penalty += 5
            latency = stat.success_mean_ms if stat.success_count > 0 else float("inf")
            # Default tie-break: generations historically faster than responses,
            # so when no data exists prefer generations for generate, edits-style
            # for edit. Action-specific bias is folded in by the caller via
            # the action parameter.
            recency = -(stat.last_success_at or 0.0)
            return (penalty, latency, recency)

        ranked = sorted(candidates, key=_score)
        # Action-aware default tweak: edit jobs tend to be more reliable on
        # generations/edits than responses (responses' input_image base64 path
        # is heavier); push responses lower for edit unless it's clearly better.
        if action == "edit" and ranked == ["responses", "generations"]:
            stat_g = (h.endpoint_stats.get("generations") if h else None) or EndpointStat()
            stat_r = (h.endpoint_stats.get("responses") if h else None) or EndpointStat()
            if stat_g.consecutive_failures <= stat_r.consecutive_failures:
                ranked = ["generations", "responses"]
        return ranked

    def report_image_rate_limited(
        self, provider_name: str, *, retry_after_s: float | None = None
    ) -> None:
        """上游显式 429 / quota：按 retry-after 头设 image_rate_limited_until。

        retry_after_s=None 时用 _IMAGE_RATE_LIMITED_DEFAULT_S（60s）兜底。
        不计入 image_consecutive_failures——这不是"号坏了"，而是"号当前没额度"。
        """
        h = self._health.setdefault(provider_name, ProviderHealth())
        now = time.monotonic()
        wait = (
            float(retry_after_s)
            if retry_after_s is not None and retry_after_s > 0
            else _IMAGE_RATE_LIMITED_DEFAULT_S
        )
        h.image_rate_limited_until = now + wait
        h.last_failure_at = now
        self._record_request_stats(h, total=1, fail=1)
        try:
            from .observability import account_image_calls_total

            account_image_calls_total.labels(
                account=provider_name, outcome="rate_limited"
            ).inc()
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "image_rate_limited: provider=%s wait=%.0fs", provider_name, wait
        )

    # ---- 探活 ------------------------------------------------------------

    @staticmethod
    def _extract_response_output_text(payload: Any) -> str:
        """从 /v1/responses 非流式响应里抽取模型输出文本。

        兼容三种形态：
        1) `payload["output_text"]`（OpenAI Responses API 顶层简化字段）
        2) `payload["output"][*]["content"][*]["text"]`（标准结构）
        3) 兜底：把 payload 整个 json.dumps 后返回，用关键词搜兜底验证

        返回拼接后的字符串（可能为空）。
        """
        if not isinstance(payload, dict):
            return ""
        # 1) 顶层 output_text
        ot = payload.get("output_text")
        if isinstance(ot, str) and ot:
            return ot
        # 2) 遍历 output[].content[].text
        chunks: list[str] = []
        out = payload.get("output")
        if isinstance(out, list):
            for item in out:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    t = c.get("text") or c.get("output_text")
                    if isinstance(t, str) and t:
                        chunks.append(t)
        if chunks:
            return "".join(chunks)
        # 3) 兜底：用整个 payload 字符串（让后面的 "9801" 匹配能 work）
        try:
            import json

            return json.dumps(payload, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _extract_sse_output_text(raw: str) -> str:
        chunks: list[str] = []
        buffer = raw.replace("\r\n", "\n")
        for raw_event in buffer.split("\n\n"):
            data_lines: list[str] = []
            for line in raw_event.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            if data == "[DONE]":
                continue
            try:
                import json

                obj = json.loads(data)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(obj, dict):
                continue

            delta = obj.get("delta")
            if isinstance(delta, str) and delta:
                chunks.append(delta)
                continue

            text = obj.get("text") or obj.get("output_text")
            if isinstance(text, str) and text:
                chunks.append(text)
                continue

            for key in ("response", "item", "part"):
                nested_text = ProviderPool._extract_response_output_text(obj.get(key))
                if nested_text:
                    chunks.append(nested_text)
                    break

        return "".join(chunks)

    async def _probe_one(self, provider: ProviderConfig) -> bool:
        """文本算术探活：让 gpt-5.4-mini 算 99*99，必须答出 9801 才算真活。

        相比"HTTP <500 就算活"的轻量探测，这种"语义探活"能识别：
        - 上游网关返回 200 但模型其实没工作（账号 OAuth 失效但还能 200）
        - 上游强制改写成空响应 / 错误响应但 status=200
        - sub2api 把请求 sticky 到一个坏号但仍返回 200
        """
        if not endpoint_kind_allowed(provider, "responses"):
            logger.debug(
                "probe_result: provider=%s status=skipped reason=endpoint_locked_to_%s",
                provider.name,
                provider.image_jobs_endpoint,
            )
            return True
        url = provider.base_url.rstrip("/")
        if not url.endswith("/v1"):
            url += "/v1"
        url += "/responses"
        headers = {
            "authorization": f"Bearer {provider.api_key}",
            "content-type": "application/json",
        }
        body = {
            "model": "gpt-5.4-mini",
            "instructions": _PROBE_INSTRUCTIONS,
            # 严格指令：模型必须只返回整数；任何额外文字都不影响 .find("9801") 命中
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "What is 99 times 99? Reply with only the integer "
                                "result, no words, no explanation."
                            ),
                        }
                    ],
                }
            ],
            "stream": False,
            "store": False,
        }
        try:
            proxy_url = await resolve_provider_proxy_url(provider.proxy)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_PROBE_TIMEOUT_S),
                proxy=proxy_url,
            ) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code >= 500:
                logger.warning(
                    "probe_result: provider=%s status=fail http=%d",
                    provider.name,
                    resp.status_code,
                )
                return False
            if resp.status_code >= 400:
                # 4xx 通常是 auth / 配额问题——不是"上游网关挂了"，但也不是"真活"
                logger.warning(
                    "probe_result: provider=%s status=fail http=%d (auth/quota)",
                    provider.name,
                    resp.status_code,
                )
                return False
            try:
                payload = resp.json()
                text = self._extract_response_output_text(payload)
            except Exception:  # noqa: BLE001
                text = self._extract_sse_output_text(resp.text)
                if not text:
                    logger.warning(
                        "probe_result: provider=%s status=fail bad_json",
                        provider.name,
                    )
                    return False
            if "9801" in text:
                return True
            logger.warning(
                "probe_result: provider=%s status=fail wrong_answer text=%.200s",
                provider.name,
                text,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "probe_result: provider=%s status=fail err=%s",
                provider.name,
                type(exc).__name__,
            )
            return False

    async def probe_all(self) -> dict[str, bool]:
        try:
            await self._maybe_reload()
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "error_code", None) == "no_providers":
                return {}
            raise
        providers = [
            p
            for p in self._providers
            if p.enabled and endpoint_kind_allowed(p, "responses")
        ]
        if not providers:
            return {}

        results = await asyncio.gather(
            *(self._probe_one(p) for p in providers),
            return_exceptions=True,
        )

        outcome: dict[str, bool] = {}
        for provider, result in zip(providers, results):
            healthy = bool(result) if not isinstance(result, BaseException) else False
            outcome[provider.name] = healthy

            h = self._health.get(provider.name)
            if h is not None:
                h.last_probe_at = time.monotonic()

            if healthy:
                self.report_success(provider.name, is_probe=True)
                logger.debug(
                    "probe_result: provider=%s status=ok", provider.name
                )
            else:
                self.report_failure(provider.name, is_probe=True)

        return outcome

    async def _probe_image_one(self, provider: ProviderConfig) -> bool:
        """1024x1024 低质量生图探活：必须真拿回 base64 才算 healthy。

        和文本 probe 隔离：
        - 不动 image_last_used_at（probe 不应影响调度顺序）
        - 不动 total_requests / failed_requests（不污染真实请求统计）
        - 失败累加 image_consecutive_failures，3 次熔 image cooldown 60s
        - 不调 record_image_call（不入账 Redis quota，sub2api 那边的 OAuth 配额会消耗，
          但 Lumen 的滑动窗口不计 probe 次数）
        """
        from .upstream import UpstreamError, _responses_image_stream

        if not endpoint_kind_allowed(provider, "responses"):
            logger.debug(
                "image_probe_result: provider=%s status=skipped reason=endpoint_locked_to_%s",
                provider.name,
                provider.image_jobs_endpoint,
            )
            return True

        try:
            kwargs: dict[str, Any] = {
                "prompt": _IMAGE_PROBE_PROMPT,
                "size": _IMAGE_PROBE_SIZE,
                "action": "generate",
                "quality": _IMAGE_PROBE_QUALITY,
                "base_url_override": provider.base_url,
                "api_key_override": provider.api_key,
            }
            if provider.proxy is not None:
                kwargs["proxy_override"] = provider.proxy
            b64, _ = await _responses_image_stream(**kwargs)
        except UpstreamError as exc:
            logger.warning(
                "image_probe_result: provider=%s status=fail err_code=%s msg=%.200s",
                provider.name,
                exc.error_code,
                str(exc),
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "image_probe_result: provider=%s status=fail err=%s",
                provider.name,
                type(exc).__name__,
            )
            return False
        if not b64 or len(b64) < _IMAGE_PROBE_MIN_B64_LEN:
            logger.warning(
                "image_probe_result: provider=%s status=fail b64_len=%d (min=%d)",
                provider.name,
                len(b64) if b64 else 0,
                _IMAGE_PROBE_MIN_B64_LEN,
            )
            return False
        return True

    async def probe_image_all(self) -> dict[str, bool]:
        """对所有 enabled provider 跑 image probe。"""
        try:
            await self._maybe_reload()
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "error_code", None) == "no_providers":
                return {}
            raise
        providers = [
            p
            for p in self._providers
            if p.enabled and endpoint_kind_allowed(p, "responses")
        ]
        if not providers:
            return {}
        results = await asyncio.gather(
            *(self._probe_image_one(p) for p in providers),
            return_exceptions=True,
        )
        outcome: dict[str, bool] = {}
        for provider, result in zip(providers, results):
            healthy = bool(result) if not isinstance(result, BaseException) else False
            outcome[provider.name] = healthy
            if healthy:
                self.report_image_probe_success(provider.name)
                logger.debug(
                    "image_probe_result: provider=%s status=ok", provider.name
                )
            else:
                self.report_image_probe_failure(provider.name)
        return outcome

    def report_image_probe_success(self, provider_name: str) -> None:
        """image probe 成功：清空 image 失败计数，关闭 image cooldown。

        和真实请求 report_image_success 的关键差别：**不动 image_last_used_at**——
        probe 不算用户请求，不该影响"最久未用优先"的调度排序。也不动 total_requests
        计数（probe 不计入真实请求统计）。
        """
        h = self._health.setdefault(provider_name, ProviderHealth())
        was_image_open = h.image_consecutive_failures >= _IMAGE_CB_FAILURE_THRESHOLD
        h.image_consecutive_failures = 0
        h.image_cooldown_until = None
        h.last_probe_at = time.monotonic()
        if was_image_open:
            logger.info(
                "image_circuit_closed: provider=%s recovered via probe",
                provider_name,
            )

    def report_image_probe_failure(self, provider_name: str) -> None:
        """image probe 失败：累加 image_consecutive_failures，达到阈值熔断 image cooldown。

        不动 total_requests / failed_requests——这是探针失败，不是真实请求失败。
        但累加 image_consecutive_failures 让坏号能从 image route 候选里被排除。
        """
        h = self._health.setdefault(provider_name, ProviderHealth())
        now = time.monotonic()
        h.image_consecutive_failures += 1
        h.last_probe_at = now
        if h.image_consecutive_failures >= _IMAGE_CB_FAILURE_THRESHOLD:
            h.image_cooldown_until = now + _IMAGE_CB_COOLDOWN_S
            logger.warning(
                "image_circuit_open: provider=%s probe_failures=%d cooldown=%.0fs",
                provider_name,
                h.image_consecutive_failures,
                _IMAGE_CB_COOLDOWN_S,
            )

    async def flush_image_metrics(self) -> None:
        """把每个 provider 的 image route state + quota 已用次数推到 Prometheus gauge。

        由 probe_providers cron 周期调用（30s 一次足够，gauge 不需要逐请求精确）。
        失败静默——指标缺失不应影响主路径。

        流程：
        - state：每个号每个 state 维度都 set，避免老 gauge 残留误读
        - quota_used：从 Redis ZCARD（滑动窗口当前已用）+ daily counter 拉
        """
        try:
            from . import account_limiter
            from .observability import (
                _ALLOWED_IMAGE_STATES,
                account_image_quota_used,
                account_image_state,
            )
        except Exception:  # noqa: BLE001
            return

        now = time.monotonic()
        wall_now = time.time()
        redis = self.get_redis()
        statuses = self.get_status()

        for s in statuses:
            name = s["name"]
            cur_state = s.get("image", {}).get("state", "closed")
            # 每个 state 维度都明确设 1/0：避免 cooldown→closed 切换时旧 gauge 残留
            for state_label in _ALLOWED_IMAGE_STATES:
                value = 1.0 if state_label == cur_state else 0.0
                try:
                    account_image_state.labels(account=name, state=state_label).set(
                        value
                    )
                except Exception:  # noqa: BLE001
                    pass

            if redis is None:
                continue
            # 滑动窗口当前已用：先清掉过期戳，再 ZCARD
            cfg_rate = s.get("image", {}).get("rate_limit")
            parsed = account_limiter.parse_rate_limit(cfg_rate)
            if parsed is not None:
                _, window_s = parsed
                ts_key = f"lumen:acct:{name}:image:ts"
                try:
                    await redis.zremrangebyscore(
                        ts_key, 0, wall_now - window_s
                    )
                    used_raw = await redis.zcard(ts_key)
                    used_window = int(used_raw or 0)
                    account_image_quota_used.labels(
                        account=name, window="current_window"
                    ).set(float(used_window))
                except Exception:  # noqa: BLE001
                    pass

            # 当日已用
            day_key = (
                f"lumen:acct:{name}:image:daily:"
                f"{account_limiter._today_utc_key(wall_now)}"
            )
            try:
                raw = await redis.get(day_key)
            except Exception:  # noqa: BLE001
                raw = None
            try:
                used_daily = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                used_daily = 0
            try:
                account_image_quota_used.labels(account=name, window="daily").set(
                    float(used_daily)
                )
            except Exception:  # noqa: BLE001
                pass

        # 防止 unused 警告
        _ = now

    async def flush_stats_to_redis(self, redis: Any) -> None:
        """将内存中的请求计数刷入 Redis Hash，供 API 进程读取。"""
        snapshots: list[tuple[str, ProviderHealth, int, int, int]] = []
        try:
            with self._stats_lock:
                for p in self._providers:
                    h = self._health.get(p.name)
                    if h is None:
                        continue

                    total = h.total_requests
                    success = h.successful_requests
                    fail = h.failed_requests
                    if total == 0 and success == 0 and fail == 0:
                        continue

                    h.total_requests = 0
                    h.successful_requests = 0
                    h.failed_requests = 0
                    snapshots.append((p.name, h, total, success, fail))

            pipe = redis.pipeline(transaction=False)
            for name, _h, total, success, fail in snapshots:
                key = f"lumen:provider_stats:{name}"
                pipe.hincrby(key, "total", total)
                pipe.hincrby(key, "success", success)
                pipe.hincrby(key, "fail", fail)
            await pipe.execute()
        except Exception:  # noqa: BLE001
            if snapshots:
                with self._stats_lock:
                    for _name, h, total, success, fail in snapshots:
                        h.total_requests += total
                        h.successful_requests += success
                        h.failed_requests += fail
            logger.warning("flush_stats_to_redis failed", exc_info=True)

    def get_status(self) -> list[dict[str, Any]]:
        """返回所有 provider 的当前状态（调试 / admin API 用）。"""
        now = time.monotonic()
        result: list[dict[str, Any]] = []
        for p in self._providers:
            h = self._health.get(p.name, ProviderHealth())
            if h.consecutive_failures >= _CB_FAILURE_THRESHOLD:
                if h.cooldown_until and now >= h.cooldown_until:
                    state = "half_open"
                else:
                    state = "open"
            else:
                state = "closed"
            # image route 状态
            image_state = "closed"
            if (
                h.image_rate_limited_until is not None
                and now < h.image_rate_limited_until
            ):
                image_state = "rate_limited"
            elif (
                h.image_cooldown_until is not None and now < h.image_cooldown_until
            ):
                image_state = "cooldown"
            with self._stats_lock:
                total_requests = h.total_requests
                successful_requests = h.successful_requests
                failed_requests = h.failed_requests
            result.append(
                {
                    "name": p.name,
                    "priority": p.priority,
                    "weight": p.weight,
                    "enabled": p.enabled,
                    "circuit": state,
                    "consecutive_failures": h.consecutive_failures,
                    "total_requests": total_requests,
                    "successful_requests": successful_requests,
                    "failed_requests": failed_requests,
                    "image": {
                        "state": image_state,
                        "consecutive_failures": h.image_consecutive_failures,
                        "cooldown_remaining_s": (
                            max(0.0, h.image_cooldown_until - now)
                            if h.image_cooldown_until is not None
                            else 0.0
                        ),
                        "rate_limited_remaining_s": (
                            max(0.0, h.image_rate_limited_until - now)
                            if h.image_rate_limited_until is not None
                            else 0.0
                        ),
                        "rate_limit": p.image_rate_limit,
                        "daily_quota": p.image_daily_quota,
                    },
                }
            )
        return result


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
_pool: ProviderPool | None = None
_pool_lock = asyncio.Lock()


async def get_pool() -> ProviderPool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = ProviderPool()
    return _pool


# ---------------------------------------------------------------------------
# arq cron 探活入口
# ---------------------------------------------------------------------------
_DEFAULT_PROBE_INTERVAL_S = 120
_last_probe_at: float = 0.0
_last_image_probe_at: float = 0.0


async def probe_providers(ctx: dict[str, Any]) -> int:
    """cron 入口：按 runtime setting 决定是否探活，同时刷统计到 Redis。

    两类探活独立调度：
    - 文本算术 probe：providers.auto_probe_interval（默认 120s）
    - image probe：providers.auto_image_probe_interval（默认 3600s）
    """
    global _last_probe_at, _last_image_probe_at

    pool = await get_pool()
    redis = ctx.get("redis")
    # cron 第一次跑就把 redis 注入 pool（image route 配额检查依赖此引用）。
    # generation 任务也会 attach，但 worker 启动后 cron 比第一个生图任务先跑。
    if redis is not None:
        pool.attach_redis(redis)

    # 无论是否探活，都刷统计
    if redis is not None:
        await pool.flush_stats_to_redis(redis)
    # image route gauge 每轮都刷一次（state + quota_used），不依赖探活
    try:
        await pool.flush_image_metrics()
    except Exception:  # noqa: BLE001
        logger.warning("flush_image_metrics failed", exc_info=True)

    # 读取自动探活间隔设置
    from .runtime_settings import resolve_int

    text_interval = await resolve_int(
        "providers.auto_probe_interval", _DEFAULT_PROBE_INTERVAL_S
    )
    image_interval = await resolve_int(
        "providers.auto_image_probe_interval", _DEFAULT_IMAGE_PROBE_INTERVAL_S
    )

    now = time.monotonic()
    healthy = -1  # -1 表示本轮跳过文本 probe；保持函数返回语义不变

    # ---- 文本算术 probe ----
    if text_interval > 0 and now - _last_probe_at >= text_interval:
        _last_probe_at = now
        results = await pool.probe_all()
        healthy = sum(1 for v in results.values() if v)
        total = len(results)
        if total > 0 and healthy < total:
            summary = ", ".join(
                f"{k}={'ok' if v else 'FAIL'}" for k, v in results.items()
            )
            logger.warning(
                "probe_providers: %d/%d healthy (%s)", healthy, total, summary
            )

    # ---- image probe（独立间隔，默认 1h）----
    if image_interval > 0 and now - _last_image_probe_at >= image_interval:
        _last_image_probe_at = now
        try:
            image_results = await pool.probe_image_all()
        except Exception:  # noqa: BLE001
            logger.warning("probe_image_all failed", exc_info=True)
            image_results = {}
        if image_results:
            i_total = len(image_results)
            i_healthy = sum(1 for v in image_results.values() if v)
            if i_healthy < i_total:
                summary = ", ".join(
                    f"{k}={'ok' if v else 'FAIL'}" for k, v in image_results.items()
                )
                logger.warning(
                    "probe_image_providers: %d/%d healthy (%s)",
                    i_healthy,
                    i_total,
                    summary,
                )
            else:
                logger.info(
                    "probe_image_providers: %d/%d healthy", i_healthy, i_total
                )

    return healthy


__all__ = [
    "ProviderConfig",
    "ProviderHealth",
    "ProviderPool",
    "ResolvedProvider",
    "get_pool",
    "probe_providers",
]
