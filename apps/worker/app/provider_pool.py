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
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from lumen_core.byok import (
    build_provider_probe_request,  # noqa: F401 - late-bound probe facade
    extract_response_output_text,  # noqa: F401 - late-bound probe facade
    extract_sse_output_text,  # noqa: F401 - late-bound probe facade
)
from lumen_core.constants import GenerationErrorCode as EC
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderProxyDefinition,
    build_effective_provider_config,
    endpoint_kind_allowed,
    provider_supports_route,
    route_to_purpose,
    resolve_provider_proxy_url,  # noqa: F401 - late-bound probe facade
)

from .config import settings as _cfg
from .provider_runtime.contracts import (
    EndpointStat,  # noqa: F401 - compatibility export
    ProviderConfig,
    ProviderHealth,
    ResolvedProvider,
)
from .provider_runtime.errors import UpstreamError
from .provider_runtime.probes import ProviderProbeMixin
from .validation import validate_provider_base_url

logger = logging.getLogger(__name__)


def _ewma(previous: float | None, sample: float, alpha: float) -> float:
    if previous is None:
        return float(sample)
    return (alpha * float(sample)) + ((1.0 - alpha) * previous)


# ---------------------------------------------------------------------------
# 断路器常量
# ---------------------------------------------------------------------------
_CB_FAILURE_THRESHOLD = 3
_CB_COOLDOWN_BASE_S = 30.0
_CB_COOLDOWN_MAX_S = 300.0
_PROBE_TIMEOUT_S = 15.0
_PROBE_MAX_CONCURRENCY = 8
_CONFIG_TTL_S = 5.0

# image route 独立熔断（多账号场景下 image 失败 3 次内只熔该账号，不影响 text）
_IMAGE_CB_FAILURE_THRESHOLD = 3
_IMAGE_CB_COOLDOWN_S = 10.0
# 上游显式 429 / quota 没给 retry-after 时的兜底冷却时长
_IMAGE_RATE_LIMITED_DEFAULT_S = 60.0
_ENDPOINT_EWMA_ALPHA = 0.25
_ENDPOINT_FAILURE_ALPHA = 0.35
_ENDPOINT_RECENT_FAILURE_WINDOW_S = 60.0
_IMAGE_ROUTING_FAILURE_PENALTY_MS = 5000.0
_IMAGE_ROUTING_CONSECUTIVE_FAILURE_PENALTY_MS = 1000.0

# image probe：发一张 1024x1024 低质量图，真返回 base64 才算 healthy。
# **默认 0 = 关闭**（生产先关，确认账号配额能扛住后再调高，建议 ≥ 1800s/30min）。
# 每张 probe 都会消耗一次账号 OpenAI 配额。
# 通过 runtime_settings: providers.auto_image_probe_interval 调整（admin API 写 DB
# → 5s TTL 自动生效；或 env PROVIDERS_AUTO_IMAGE_PROBE_INTERVAL fallback）。
_DEFAULT_IMAGE_PROBE_INTERVAL_S = 0
_IMAGE_PROBE_PROMPT = "a small red apple on a white background"
_IMAGE_PROBE_SIZE = "1024x1024"
_IMAGE_PROBE_QUALITY = "low"
# 真图 base64 最少 ~5KB（1024x1024 webp 低质量）；这里取 1000 做下限保护
_IMAGE_PROBE_MIN_B64_LEN = 1000


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class TextProviderAttempt:
    _pool: ProviderPool = field(repr=False)
    provider: ResolvedProvider
    _reported: bool = field(default=False, init=False, repr=False)

    def report_success(self) -> None:
        if self._reported:
            return
        self._pool.report_success(self.provider.name)
        self._reported = True

    def report_failure(self) -> None:
        if self._reported:
            return
        self._pool.report_failure(
            self.provider.name,
            selected_circuit_state=self.provider.text_circuit_state,
            half_open_probe_token=self.provider.half_open_probe_token,
        )
        self._reported = True

    def report_exception(self, exc: BaseException) -> bool:
        if not _is_text_provider_failure(exc):
            return False
        self.report_failure()
        return True

    def release(self) -> None:
        if self._reported:
            return
        self._pool.release_text_attempt(self.provider)
        self._reported = True


_ImageCandidate = tuple[ProviderConfig, tuple[int, float, float]]


@dataclass(frozen=True)
class _ImageHealthSnapshot:
    text_circuit_open: bool
    image_cooldown_until: float | None
    image_rate_limited_until: float | None
    inflight: int
    last_attempted: float | None
    last_used: float | None


@dataclass
class _ImageCandidateBuckets:
    candidates: list[_ImageCandidate] = field(default_factory=list)
    mask_file_candidates: list[_ImageCandidate] = field(default_factory=list)
    mask_url_candidates: list[_ImageCandidate] = field(default_factory=list)

    def add(
        self,
        candidate: _ImageCandidate,
        *,
        requires_mask: bool,
        mask_transport_required: bool,
    ) -> None:
        provider, _sort_key = candidate
        if not requires_mask or not mask_transport_required:
            self.candidates.append(candidate)
        elif provider.image_edit_input_transport == "file":
            self.mask_file_candidates.append(candidate)
        else:
            self.mask_url_candidates.append(candidate)

    def select(
        self,
        *,
        requires_mask: bool,
        mask_transport_required: bool,
        task_id: str | None,
    ) -> list[_ImageCandidate]:
        if not requires_mask or not mask_transport_required:
            return self.candidates
        if self.mask_file_candidates:
            return self.mask_file_candidates
        if self.mask_url_candidates:
            logger.info(
                "image mask file-mode exhausted; falling back to url transport "
                "task=%s candidates=%d",
                task_id,
                len(self.mask_url_candidates),
            )
        return self.mask_url_candidates


class _UntrackedTextProviderAttempt:
    """Compatibility attempt for lightweight pools used by late-bound tests."""

    def report_success(self) -> None:
        return None

    def report_failure(self) -> None:
        return None

    def report_exception(self, exc: BaseException) -> bool:
        return _is_text_provider_failure(exc)

    def release(self) -> None:
        return None


def _is_text_provider_failure(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPError):
        return True
    if getattr(exc, "status_code", None) is not None:
        return True
    error_code = getattr(exc, "error_code", None)
    if isinstance(error_code, str) and error_code.strip():
        return True
    if isinstance(exc, RuntimeError):
        message = str(exc).lower()
        return (
            message.startswith("ssh proxy ") or "unsupported proxy protocol" in message
        )
    return False


def _image_endpoint_skip_reason(
    provider: ProviderConfig,
    endpoint_kind: str | None,
) -> str | None:
    if not endpoint_kind_allowed(provider, endpoint_kind):
        return f"endpoint_locked_to_{provider.image_jobs_endpoint}"
    if not provider_supports_route(
        provider,
        route="image",
        endpoint_kind=endpoint_kind,
    ):
        return "capability_unsupported"
    return None


def _image_health_skip_reason(
    snapshot: _ImageHealthSnapshot,
    *,
    ignore_cooldown: bool,
    now: float,
) -> str | None:
    if snapshot.text_circuit_open:
        return "text_circuit_open"
    if (
        not ignore_cooldown
        and snapshot.image_cooldown_until is not None
        and now < snapshot.image_cooldown_until
    ):
        return "image_cooldown"
    if (
        not ignore_cooldown
        and snapshot.image_rate_limited_until is not None
        and now < snapshot.image_rate_limited_until
    ):
        return "image_rate_limited"
    return None


def _image_last_attempt_key(snapshot: _ImageHealthSnapshot) -> float:
    attempted_or_used = (
        snapshot.last_attempted
        if snapshot.last_attempted is not None
        else snapshot.last_used
    )
    return attempted_or_used if attempted_or_used is not None else float("-inf")


def _image_candidate_sort_key(
    candidate: _ImageCandidate,
) -> tuple[int, float, float, int]:
    provider, (inflight, last_used, adaptive_score) = candidate
    return inflight, adaptive_score, last_used, -provider.priority


def _resolved_image_provider(provider: ProviderConfig) -> ResolvedProvider:
    return ResolvedProvider(
        name=provider.name,
        base_url=provider.base_url,
        api_key=provider.api_key,
        proxy=provider.proxy,
        image_jobs_enabled=provider.image_jobs_enabled,
        image_jobs_endpoint=provider.image_jobs_endpoint,
        image_jobs_endpoint_lock=provider.image_jobs_endpoint_lock,
        image_jobs_base_url=provider.image_jobs_base_url,
        image_edit_input_transport=provider.image_edit_input_transport,
        image_concurrency=provider.image_concurrency,
        image_rate_limit=provider.image_rate_limit,
        image_daily_quota=provider.image_daily_quota,
        responses_supported=provider.responses_supported,
        purposes=provider.purposes,
        image_generations_supported=provider.image_generations_supported,
        image_responses_supported=provider.image_responses_supported,
    )


def _decode_avoided_provider(value: Any) -> str | None:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None
    return value if isinstance(value, str) else None


async def _load_avoided_image_providers(
    redis: Any,
    task_id: str | None,
) -> set[str]:
    if not task_id or redis is None:
        return set()
    try:
        raw = await redis.smembers(f"generation:image_queue:avoid:{task_id}")
    except Exception:  # noqa: BLE001
        return set()
    return {
        decoded
        for item in raw or []
        if (decoded := _decode_avoided_provider(item)) is not None
    }


def _only_avoided_image_providers(
    avoided: set[str],
    skipped: list[tuple[str, str]],
) -> bool:
    return bool(avoided) and all(
        reason == "avoided_from_previous_attempt" for _, reason in skipped
    )


def _all_image_accounts_failed(
    skipped: list[tuple[str, str]],
) -> UpstreamError:
    detail = ", ".join(f"{name}({reason})" for name, reason in skipped) or "none"
    return UpstreamError(
        f"all accounts unavailable for image: {detail}",
        error_code=EC.ALL_ACCOUNTS_FAILED.value,
        status_code=503,
        payload={"skipped": skipped},
    )


@contextmanager
def text_provider_attempt(
    pool: Any,
    provider: Any,
) -> Iterator[TextProviderAttempt | _UntrackedTextProviderAttempt]:
    """Track a real ProviderPool attempt while preserving simple test doubles."""
    attempt_factory = getattr(pool, "text_attempt", None)
    if not callable(attempt_factory):
        attempt = _UntrackedTextProviderAttempt()
        try:
            yield attempt
        finally:
            attempt.release()
        return
    with attempt_factory(provider) as attempt:
        yield attempt


# ---------------------------------------------------------------------------
# Provider Base URL 格式校验（独立模块，避免 provider_pool 运行时导入 upstream）
# ---------------------------------------------------------------------------
async def _validate_provider_base_url(raw_base: str) -> str:
    return await validate_provider_base_url(raw_base)


# ---------------------------------------------------------------------------
# ProviderPool
# ---------------------------------------------------------------------------
class ProviderPool(ProviderProbeMixin):
    def __init__(self) -> None:
        self._providers: list[ProviderConfig] = []
        self._proxies: dict[str, ProviderProxyDefinition] = {}
        self._health: dict[str, ProviderHealth] = {}
        self._config_loaded_at: float = 0.0
        self._config_lock = asyncio.Lock()
        self._stats_lock = threading.Lock()
        self._rr_state: dict[int, dict[str, int]] = {}
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

    async def _validate_provider_base_url(self, raw_base: str) -> str:
        return await _validate_provider_base_url(raw_base)

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
                os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
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
                purposes=p.purposes,
                proxy_name=p.proxy_name,
                proxy=p.proxy,
                image_rate_limit=p.image_rate_limit,
                image_daily_quota=p.image_daily_quota,
                image_jobs_enabled=p.image_jobs_enabled,
                image_jobs_endpoint=p.image_jobs_endpoint,
                image_jobs_endpoint_lock=p.image_jobs_endpoint_lock,
                image_jobs_base_url=p.image_jobs_base_url,
                image_edit_input_transport=p.image_edit_input_transport,
                image_concurrency=p.image_concurrency,
                responses_supported=getattr(p, "responses_supported", None),
                image_generations_supported=getattr(
                    p, "image_generations_supported", None
                ),
                image_responses_supported=getattr(p, "image_responses_supported", None),
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
                    url = await self._validate_provider_base_url(p.base_url)
                    validated.append(
                        ProviderConfig(
                            name=p.name,
                            base_url=url,
                            api_key=p.api_key,
                            priority=p.priority,
                            weight=p.weight,
                            enabled=p.enabled,
                            purposes=p.purposes,
                            proxy_name=p.proxy_name,
                            proxy=p.proxy,
                            image_rate_limit=p.image_rate_limit,
                            image_daily_quota=p.image_daily_quota,
                            image_jobs_enabled=p.image_jobs_enabled,
                            image_jobs_endpoint=p.image_jobs_endpoint,
                            image_jobs_endpoint_lock=p.image_jobs_endpoint_lock,
                            image_jobs_base_url=p.image_jobs_base_url,
                            image_edit_input_transport=p.image_edit_input_transport,
                            image_concurrency=p.image_concurrency,
                            responses_supported=p.responses_supported,
                            image_generations_supported=p.image_generations_supported,
                            image_responses_supported=p.image_responses_supported,
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
            for prio, state in list(self._rr_state.items()):
                for name in list(state.keys()):
                    if name not in new_names:
                        del state[name]
                if not state:
                    del self._rr_state[prio]

            changed = [p.name for p in self._providers] != [p.name for p in validated]
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

    @staticmethod
    def _eligible_providers_by_priority(
        providers: list[ProviderConfig],
        *,
        endpoint_kind: str | None,
        route: str,
        purpose: str,
    ) -> dict[int, list[ProviderConfig]]:
        by_priority: dict[int, list[ProviderConfig]] = {}
        for provider in providers:
            if purpose not in provider.purposes:
                continue
            if not endpoint_kind_allowed(provider, endpoint_kind):
                continue
            # capability=False explicitly excludes a provider; None preserves
            # health-based failover learning for previously unknown routes.
            if not provider_supports_route(
                provider,
                route=route,
                endpoint_kind=endpoint_kind,
            ):
                continue
            by_priority.setdefault(provider.priority, []).append(provider)
        return by_priority

    def _select_ordered(
        self,
        *,
        endpoint_kind: str | None = None,
        route: str = "text",
        purpose: str | None = None,
        claim_half_open: bool = True,
        advance_round_robin: bool = True,
    ) -> list[ResolvedProvider]:
        enabled = [p for p in self._providers if p.enabled]
        if not enabled:
            raise UpstreamError(
                "no upstream providers configured or all disabled",
                error_code=EC.NO_PROVIDERS.value,
                status_code=503,
            )

        effective_purpose = purpose or route_to_purpose(route)
        by_priority = self._eligible_providers_by_priority(
            enabled,
            endpoint_kind=endpoint_kind,
            route=route,
            purpose=effective_purpose,
        )
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
            ordered = self._weighted_round_robin(
                group,
                advance=advance_round_robin,
            )

            healthy: list[ProviderConfig] = []
            half_open_candidates: list[ProviderConfig] = []
            circuit_open: list[ProviderConfig] = []

            for p in ordered:
                h = self._health.get(p.name)
                with self._stats_lock:
                    circuit_state = (
                        "closed" if h is None else self._circuit_state(h, now)
                    )
                    half_open_busy = bool(h is not None and h.half_open_probe_inflight)
                if circuit_state == "closed":
                    healthy.append(p)
                elif circuit_state == "half_open" and (
                    not claim_half_open or not half_open_busy
                ):
                    half_open_candidates.append(p)
                elif circuit_state == "open":
                    circuit_open.append(p)

            # 按冷却开始时间排 circuit_open（最早的最可能已恢复）
            circuit_open.sort(
                key=lambda p: self._health[p.name].cooldown_until or float("inf")
            )

            half_open_probe: list[tuple[ProviderConfig, str]] = []
            # A half-open candidate must be the first provider this selection
            # will actually attempt. Reserving later fallback candidates would
            # strand their probe slots whenever an earlier healthy provider
            # succeeds. At most one selector owns the probe for a provider.
            if claim_half_open and not result:
                for candidate in half_open_candidates:
                    h = self._health.get(candidate.name)
                    if h is None:
                        continue
                    with self._stats_lock:
                        if (
                            self._circuit_state(h, now) == "half_open"
                            and not h.half_open_probe_inflight
                        ):
                            token = uuid.uuid4().hex
                            h.half_open_probe_inflight = True
                            h.half_open_probe_token = token
                            half_open_probe.append((candidate, token))
                            break

            candidates = (
                [(p, "half_open", token) for p, token in half_open_probe]
                + (
                    []
                    if claim_half_open
                    else [(p, "half_open", None) for p in half_open_candidates]
                )
                + [(p, "closed", None) for p in healthy]
                + [(p, "open", None) for p in circuit_open]
            )
            for p, circuit_state, half_open_token in candidates:
                result.append(
                    ResolvedProvider(
                        name=p.name,
                        base_url=p.base_url,
                        api_key=p.api_key,
                        proxy=p.proxy,
                        image_jobs_endpoint=p.image_jobs_endpoint,
                        image_jobs_endpoint_lock=p.image_jobs_endpoint_lock,
                        image_jobs_base_url=p.image_jobs_base_url,
                        image_edit_input_transport=p.image_edit_input_transport,
                        image_concurrency=p.image_concurrency,
                        purposes=p.purposes,
                        responses_supported=p.responses_supported,
                        image_generations_supported=p.image_generations_supported,
                        image_responses_supported=p.image_responses_supported,
                        text_circuit_state=circuit_state,
                        half_open_probe_token=half_open_token,
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
        self,
        group: list[ProviderConfig],
        *,
        advance: bool = True,
    ) -> list[ProviderConfig]:
        if len(group) <= 1:
            return list(group)
        prio = group[0].priority
        state = (
            self._rr_state.setdefault(prio, {})
            if advance
            else dict(self._rr_state.get(prio, {}))
        )
        for p in group:
            state.setdefault(p.name, 0)

        def weight(p: ProviderConfig) -> int:
            return max(1, int(p.weight or 1))

        total_weight = sum(weight(p) for p in group)

        def pick_next(
            candidates: list[ProviderConfig],
            current: dict[str, int],
            *,
            subtract_total: int,
        ) -> ProviderConfig:
            for p in candidates:
                current[p.name] = current.get(p.name, 0) + weight(p)
            order_index = {p.name: idx for idx, p in enumerate(group)}
            selected = max(
                candidates,
                key=lambda p: (current[p.name], weight(p), -order_index[p.name]),
            )
            current[selected.name] -= subtract_total
            return selected

        first = pick_next(group, state, subtract_total=total_weight)
        ordered = [first]
        remaining = [p for p in group if p.name != first.name]
        local_state = dict(state)
        while remaining:
            remaining_total = sum(weight(p) for p in remaining)
            selected = pick_next(
                remaining,
                local_state,
                subtract_total=remaining_total,
            )
            ordered.append(selected)
            remaining = [p for p in remaining if p.name != selected.name]
        return ordered

    @staticmethod
    def _circuit_state(h: ProviderHealth, now: float) -> str:
        if h.consecutive_failures < _CB_FAILURE_THRESHOLD:
            return "closed"
        # P2-7: failures 已达阈值但 cooldown_until 还未赋值——这是 report_failure
        # 中"先 incr 后赋值"的窗口期；并发请求若此时进入会绕过断路器。视为 open
        # 拒绝请求，等同时刻的 report_failure 完成赋值后转入正常 cooldown。
        if h.cooldown_until is None:
            return "open"
        if now < h.cooldown_until:
            return "open"
        return "half_open"

    @staticmethod
    def _is_open(h: ProviderHealth, now: float) -> bool:
        return ProviderPool._circuit_state(h, now) == "open"

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
        purpose: str | None = None,
        ignore_cooldown: bool = False,
        task_id: str | None = None,
        endpoint_kind: str | None = None,
        acquire_inflight: bool = True,
        requires_mask: bool = False,
        mask_transport_required: bool = True,
        queue_lane: str | None = None,
        size_bucket: str | None = None,
        cost_class: str | None = None,
    ) -> list[ResolvedProvider]:
        """Select 候选 provider。

        ``requires_mask=True`` 标识本次是局部 inpaint 任务。是否真的按
        ``image_edit_input_transport=file`` 过滤候选，由 ``mask_transport_required``
        决定：

        - ``mask_transport_required=True``（默认，sidecar 路径调用方）：优先返回
          ``image_edit_input_transport=file`` 的号；如果 file-mode 候选全部耗尽，
          再退回 url-mode 候选，避免 inpaint 在 file-only 号池清空时直接终态。
        - ``mask_transport_required=False``（direct edits 路径调用方）：跳过
          transport 过滤——direct multipart 自身就是 multipart，不依赖 sidecar
          的 transport 字段；该路径下任意 provider 都能携带 mask。

        把 sidecar 能力和 direct 能力拆开，避免把 sidecar 配置泄漏成普适过滤器。
        """
        await self._maybe_reload()
        effective_purpose = purpose or route_to_purpose(route)
        if route == "image":
            return await self._select_for_image(
                purpose=effective_purpose,
                ignore_cooldown=ignore_cooldown,
                task_id=task_id,
                endpoint_kind=endpoint_kind,
                acquire_inflight=acquire_inflight,
                requires_mask=requires_mask,
                mask_transport_required=mask_transport_required,
                queue_lane=queue_lane,
                size_bucket=size_bucket,
                cost_class=cost_class,
            )
        if route == "image_jobs":
            return await self._select_for_image(
                purpose=effective_purpose,
                ignore_cooldown=ignore_cooldown,
                task_id=task_id,
                endpoint_kind=endpoint_kind,
                acquire_inflight=acquire_inflight,
                requires_mask=requires_mask,
                mask_transport_required=mask_transport_required,
                queue_lane=queue_lane,
                size_bucket=size_bucket,
                cost_class=cost_class,
            )
        if route == "text":
            endpoint_kind = endpoint_kind or "responses"
        if effective_purpose == "embedding":
            endpoint_kind = endpoint_kind or "responses"
        return self._select_ordered(
            endpoint_kind=endpoint_kind,
            route=route,
            purpose=effective_purpose,
        )

    async def select_one(
        self, *, route: str = "text", purpose: str | None = None
    ) -> ResolvedProvider:
        providers = await self.select(route=route, purpose=purpose)
        return providers[0]

    async def peek(
        self,
        *,
        route: str = "text",
        purpose: str | None = None,
        endpoint_kind: str | None = None,
    ) -> list[ResolvedProvider]:
        """Inspect text-capable providers without claiming half-open or RR state."""
        await self._maybe_reload()
        effective_purpose = purpose or route_to_purpose(route)
        if route == "text" or effective_purpose == "embedding":
            endpoint_kind = endpoint_kind or "responses"
        return self._select_ordered(
            endpoint_kind=endpoint_kind,
            route=route,
            purpose=effective_purpose,
            claim_half_open=False,
            advance_round_robin=False,
        )

    async def peek_one(
        self,
        *,
        route: str = "text",
        purpose: str | None = None,
    ) -> ResolvedProvider:
        providers = await self.peek(route=route, purpose=purpose)
        return providers[0]

    @contextmanager
    def text_attempt(
        self,
        provider: ResolvedProvider,
    ) -> Iterator[TextProviderAttempt]:
        """Own a selected text attempt and release an unresolved half-open slot."""
        attempt = TextProviderAttempt(self, provider)
        try:
            yield attempt
        finally:
            attempt.release()

    def release_text_attempt(self, provider: ResolvedProvider) -> None:
        """Release only the half-open slot owned by this selected provider."""
        token = provider.half_open_probe_token
        if token is None:
            return
        with self._stats_lock:
            h = self._health.get(provider.name)
            if h is None or h.half_open_probe_token != token:
                return
            h.half_open_probe_inflight = False
            h.half_open_probe_token = None

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

    def _image_health_snapshot(
        self,
        health: ProviderHealth,
        *,
        endpoint_kind: str | None,
        now: float,
    ) -> _ImageHealthSnapshot:
        endpoint_key = endpoint_kind or ""
        with self._stats_lock:
            return _ImageHealthSnapshot(
                text_circuit_open=self._is_open(health, now),
                image_cooldown_until=health.image_cooldown_until,
                image_rate_limited_until=health.image_rate_limited_until,
                inflight=health.image_inflight.get(endpoint_key, 0),
                last_attempted=health.image_last_attempted_at_per_ek.get(endpoint_key),
                last_used=health.image_last_used_at_per_ek.get(endpoint_key),
            )

    async def _image_quota_skip_reason(
        self,
        provider: ProviderConfig,
        health: ProviderHealth,
        *,
        redis: Any,
        account_limiter: Any,
        wall_now: float,
        mono_now: float,
    ) -> str | None:
        try:
            allowed, retry_after = await account_limiter.check_quota(
                redis,
                provider.name,
                provider.image_rate_limit,
                provider.image_daily_quota,
                now=wall_now,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "account_limiter.check_quota raised provider=%s err=%s — "
                "treating as temporarily unavailable",
                provider.name,
                exc,
            )
            allowed = False
            retry_after = float(account_limiter.REDIS_ERROR_RETRY_AFTER_S)
        if allowed:
            return None
        with self._stats_lock:
            health.image_rate_limited_until = mono_now + max(1.0, retry_after)
        return f"quota_exhausted retry_after={retry_after:.0f}s"

    async def _qualify_image_candidate(
        self,
        provider: ProviderConfig,
        *,
        avoided: set[str],
        endpoint_kind: str | None,
        ignore_cooldown: bool,
        redis: Any,
        account_limiter: Any,
        wall_now: float,
        mono_now: float,
        size_bucket: str | None,
        cost_class: str | None,
    ) -> tuple[_ImageCandidate | None, str | None]:
        reason = _image_endpoint_skip_reason(provider, endpoint_kind)
        if reason is not None:
            return None, reason
        health = self._health.setdefault(provider.name, ProviderHealth())
        if provider.name in avoided:
            return None, "avoided_from_previous_attempt"
        snapshot = self._image_health_snapshot(
            health,
            endpoint_kind=endpoint_kind,
            now=mono_now,
        )
        reason = _image_health_skip_reason(
            snapshot,
            ignore_cooldown=ignore_cooldown,
            now=mono_now,
        )
        if reason is not None:
            return None, reason
        reason = await self._image_quota_skip_reason(
            provider,
            health,
            redis=redis,
            account_limiter=account_limiter,
            wall_now=wall_now,
            mono_now=mono_now,
        )
        if reason is not None:
            return None, reason
        adaptive_score = self._image_candidate_adaptive_score(
            health=health,
            endpoint_kind=endpoint_kind,
            size_bucket=size_bucket,
            cost_class=cost_class,
        )
        return (
            provider,
            (snapshot.inflight, _image_last_attempt_key(snapshot), adaptive_score),
        ), None

    async def _collect_image_candidates(
        self,
        enabled: list[ProviderConfig],
        *,
        avoided: set[str],
        endpoint_kind: str | None,
        ignore_cooldown: bool,
        redis: Any,
        account_limiter: Any,
        wall_now: float,
        mono_now: float,
        requires_mask: bool,
        mask_transport_required: bool,
        task_id: str | None,
        size_bucket: str | None,
        cost_class: str | None,
    ) -> tuple[list[_ImageCandidate], list[tuple[str, str]]]:
        buckets = _ImageCandidateBuckets()
        skipped: list[tuple[str, str]] = []
        for provider in enabled:
            candidate, reason = await self._qualify_image_candidate(
                provider,
                avoided=avoided,
                endpoint_kind=endpoint_kind,
                ignore_cooldown=ignore_cooldown,
                redis=redis,
                account_limiter=account_limiter,
                wall_now=wall_now,
                mono_now=mono_now,
                size_bucket=size_bucket,
                cost_class=cost_class,
            )
            if candidate is None:
                skipped.append((provider.name, reason or "unavailable"))
                continue
            buckets.add(
                candidate,
                requires_mask=requires_mask,
                mask_transport_required=mask_transport_required,
            )
        return buckets.select(
            requires_mask=requires_mask,
            mask_transport_required=mask_transport_required,
            task_id=task_id,
        ), skipped

    async def _select_for_image(
        self,
        *,
        purpose: str = "image",
        ignore_cooldown: bool = False,
        task_id: str | None = None,
        endpoint_kind: str | None = None,
        acquire_inflight: bool = True,
        requires_mask: bool = False,
        mask_transport_required: bool = True,
        queue_lane: str | None = None,
        size_bucket: str | None = None,
        cost_class: str | None = None,
    ) -> list[ResolvedProvider]:
        """按账号视角选号：跳过熔断 / image cooldown / 配额耗尽的账号，
        剩余候选按 inflight/last-used 基线 + EWMA 健康分升序排序。

        ignore_cooldown=True 时，单任务遍历场景下跳过 image_cooldown / image_rate_limited
        过滤——上次失败不代表下次失败，让任务把所有 enabled 账号都试一遍。
        text 熔断（auth 类硬故障）和 quota 耗尽仍然过滤。

        排序最高优先级是当前 endpoint_kind 维度的 in-flight 计数：让并发请求
        自然落到不同号上，避免 image_last_used_at 只在 success 时更新带来的
        "select-then-update"雪崩（多个 task 同时看到同一个候选第一名）。
        次优先级是 image_last_used_at_per_ek（按 endpoint_kind 维度，避免一个号
        在 responses lane 成功后污染 generations lane 的排序），最后是 priority。

        acquire_inflight=True（默认）时，return 前对列表第 0 个候选 incr inflight；
        caller 必须用 try/finally 保证最终 release（无论是否实际使用第 0 个）。
        Reserve 等只为"挑号占 zset"不真正发请求的路径应传 False。

        这一层是 sub2api"一号一 key"部署形态下的核心调度：让多次生图请求自然
        分散到不同账号，避免单号被打到 OpenAI quota 上限。配额检查走
        account_limiter（rate_limit/daily_quota 都为空时短路，让"先放开跑"的
        策略不查 Redis）。
        """
        enabled = [p for p in self._providers if p.enabled and purpose in p.purposes]
        if not enabled:
            raise UpstreamError(
                "no upstream providers configured or all disabled",
                error_code=EC.NO_PROVIDERS.value,
                status_code=503,
            )

        now = time.monotonic()
        wall_now = time.time()
        redis = self.get_redis()

        from . import account_limiter

        avoided = await _load_avoided_image_providers(redis, task_id)
        candidates, skipped = await self._collect_image_candidates(
            enabled,
            avoided=avoided,
            endpoint_kind=endpoint_kind,
            ignore_cooldown=ignore_cooldown,
            redis=redis,
            account_limiter=account_limiter,
            wall_now=wall_now,
            mono_now=now,
            requires_mask=requires_mask,
            mask_transport_required=mask_transport_required,
            task_id=task_id,
            size_bucket=size_bucket,
            cost_class=cost_class or queue_lane,
        )
        if not candidates:
            if _only_avoided_image_providers(avoided, skipped):
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
                    acquire_inflight=acquire_inflight,
                    requires_mask=requires_mask,
                    mask_transport_required=mask_transport_required,
                    queue_lane=queue_lane,
                    size_bucket=size_bucket,
                    cost_class=cost_class,
                )
            raise _all_image_accounts_failed(skipped)

        candidates.sort(key=_image_candidate_sort_key)
        if acquire_inflight and task_id:
            candidates = await self._reserve_first_quota_candidate(
                candidates,
                redis=redis,
                account_limiter=account_limiter,
                task_id=task_id,
                wall_now=wall_now,
                mono_now=now,
                skipped=skipped,
            )
            if not candidates:
                raise _all_image_accounts_failed(skipped)
        result = [_resolved_image_provider(provider) for provider, _ in candidates]
        if acquire_inflight and result:
            self.acquire_image_inflight(result[0].name, endpoint_kind)
        return result

    async def _reserve_first_quota_candidate(
        self,
        candidates: list[_ImageCandidate],
        *,
        redis: Any,
        account_limiter: Any,
        task_id: str,
        wall_now: float,
        mono_now: float,
        skipped: list[tuple[str, str]],
    ) -> list[_ImageCandidate]:
        """Reserve quota for the provider whose inflight slot we are claiming.

        ``check_quota`` is still used while gathering candidates so exhausted
        providers are filtered early. This second pass closes the race for the
        selected provider by re-checking and reserving in one Redis script.
        """
        if redis is None:
            return candidates
        for idx, (provider, sort_key) in enumerate(candidates):
            try:
                allowed, retry_after, _member = await account_limiter.reserve_quota(
                    redis,
                    provider.name,
                    provider.image_rate_limit,
                    provider.image_daily_quota,
                    task_id=task_id,
                    now=wall_now,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "account_limiter.reserve_quota raised provider=%s err=%s — "
                    "treating as temporarily unavailable",
                    provider.name,
                    exc,
                )
                allowed = False
                retry_after = float(account_limiter.REDIS_ERROR_RETRY_AFTER_S)
            if allowed:
                if idx == 0:
                    return candidates
                return [(provider, sort_key)] + candidates[:idx] + candidates[idx + 1 :]
            h = self._health.setdefault(provider.name, ProviderHealth())
            with self._stats_lock:
                h.image_rate_limited_until = mono_now + max(1.0, retry_after)
            skipped.append(
                (provider.name, f"quota_exhausted retry_after={retry_after:.0f}s")
            )
        return []


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


async def probe_providers(ctx: dict[str, Any]) -> int | None:
    """cron 入口：按 runtime setting 决定是否探活，同时刷统计到 Redis。

    两类探活独立调度：
    - 文本算术 probe：providers.auto_probe_interval（默认 120s）
    - image probe：providers.auto_image_probe_interval（默认 3600s）

    返回值：本轮跑了文本 probe 时返回 healthy 数（int）；跳过时返回 None
    （旧版返回 -1 看起来像错误码，arq cron 完成日志里会刷 `● -1` 噪音）。
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
    healthy: int | None = None

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
                logger.info("probe_image_providers: %d/%d healthy", i_healthy, i_total)

    return healthy


__all__ = [
    "ProviderConfig",
    "ProviderHealth",
    "ProviderPool",
    "ResolvedProvider",
    "TextProviderAttempt",
    "get_pool",
    "probe_providers",
    "text_provider_attempt",
]
