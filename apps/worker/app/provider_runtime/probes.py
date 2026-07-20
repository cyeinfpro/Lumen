"""Provider endpoint health, probe, and metric projection mixin."""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from .contracts import EndpointStat, ProviderConfig, ProviderHealth
from .errors import UpstreamError


def _facade() -> Any:
    """Resolve the compatibility facade so legacy monkeypatches remain live."""
    return importlib.import_module("app.provider_pool")


class ProviderProbeMixin:
    def _image_candidate_adaptive_score(
        self,
        *,
        health: ProviderHealth,
        endpoint_kind: str | None,
        size_bucket: str | None = None,
        cost_class: str | None = None,
    ) -> float:
        if endpoint_kind:
            stat = health.endpoint_stats.get(endpoint_kind)
            return self._endpoint_adaptive_score(
                stat,
                size_bucket=size_bucket,
                cost_class=cost_class,
            )
        stats = [
            health.endpoint_stats.get(endpoint)
            for endpoint in ("generations", "responses", "")
            if health.endpoint_stats.get(endpoint) is not None
        ]
        if not stats:
            return 0.0
        return min(
            self._endpoint_adaptive_score(
                stat,
                size_bucket=size_bucket,
                cost_class=cost_class,
            )
            for stat in stats
        )

    def _endpoint_adaptive_score(
        self,
        stat: EndpointStat | None,
        *,
        size_bucket: str | None = None,
        cost_class: str | None = None,
    ) -> float:
        latency = (
            stat.latency_ewma_ms
            if stat is not None and stat.latency_ewma_ms is not None
            else stat.success_mean_ms
            if stat is not None and stat.success_count > 0
            else 0.0
        )
        failure_ewma = stat.failure_ewma if stat is not None else 0.0
        consecutive_failures = stat.consecutive_failures if stat is not None else 0
        score = (
            latency
            + failure_ewma * _facade()._IMAGE_ROUTING_FAILURE_PENALTY_MS
            + consecutive_failures
            * _facade()._IMAGE_ROUTING_CONSECUTIVE_FAILURE_PENALTY_MS
        )
        if (
            stat is not None
            and stat.last_failure_at is not None
            and _facade().time.monotonic() - stat.last_failure_at
            < _facade()._ENDPOINT_RECENT_FAILURE_WINDOW_S
        ):
            score += _facade()._IMAGE_ROUTING_CONSECUTIVE_FAILURE_PENALTY_MS
        # Large/dual-race work occupies expensive slots longer, so amplify the
        # health signal when all hard routing keys tie.
        if size_bucket == "large" or cost_class in {"large", "dual_race"}:
            score *= 1.25
        return score

    def report_success(self, provider_name: str, *, is_probe: bool = False) -> None:
        h = self._health.get(provider_name)
        if h is None:
            return
        with self._stats_lock:
            was_open = h.consecutive_failures >= _facade()._CB_FAILURE_THRESHOLD
            h.consecutive_failures = 0
            h.last_success_at = _facade().time.monotonic()
            h.cooldown_until = None
            h.half_open_probe_inflight = False
            h.half_open_probe_token = None
            if not is_probe:
                h.total_requests += 1
                h.successful_requests += 1
        if was_open:
            _facade().logger.info(
                "circuit_closed: provider=%s recovered", provider_name
            )

    def report_failure(
        self,
        provider_name: str,
        *,
        is_probe: bool = False,
        selected_circuit_state: str | None = None,
        half_open_probe_token: str | None = None,
    ) -> None:
        h = self._health.get(provider_name)
        if h is None:
            return
        now = _facade().time.monotonic()
        if is_probe:
            # probe 不毒化熔断（不动 consecutive_failures / cooldown_until），但仍
            # 上报一次 fail 让监控可见——_record_request_stats 只递 total/fail
            # 计数器，不会触发熔断逻辑（已 verify）。
            with self._stats_lock:
                h.last_failure_at = now
                h.total_requests += 1
                h.failed_requests += 1
            return
        with self._stats_lock:
            h.last_failure_at = now
            h.total_requests += 1
            h.failed_requests += 1
            if selected_circuit_state is None:
                was_half_open_probe = h.half_open_probe_inflight
                was_open_fallback = (
                    h.consecutive_failures >= _facade()._CB_FAILURE_THRESHOLD
                    and not was_half_open_probe
                )
                h.half_open_probe_inflight = False
                h.half_open_probe_token = None
            else:
                owns_half_open_probe = (
                    half_open_probe_token is not None
                    and h.half_open_probe_token == half_open_probe_token
                )
                was_half_open_probe = (
                    selected_circuit_state == "half_open" and owns_half_open_probe
                )
                was_open_fallback = (
                    selected_circuit_state == "open"
                    and h.consecutive_failures >= _facade()._CB_FAILURE_THRESHOLD
                )
                if owns_half_open_probe:
                    h.half_open_probe_inflight = False
                    h.half_open_probe_token = None
            duration = 0.0
            if was_open_fallback:
                failures = h.consecutive_failures
            else:
                h.consecutive_failures += 1
                failures = h.consecutive_failures
            if not was_open_fallback and failures >= _facade()._CB_FAILURE_THRESHOLD:
                multiplier = min(failures - _facade()._CB_FAILURE_THRESHOLD + 1, 10)
                duration = min(
                    _facade()._CB_COOLDOWN_BASE_S * multiplier,
                    _facade()._CB_COOLDOWN_MAX_S,
                )
                h.cooldown_until = now + duration
        if not was_open_fallback and failures >= _facade()._CB_FAILURE_THRESHOLD:
            _facade().logger.warning(
                "circuit_open: provider=%s failures=%d cooldown=%.0fs",
                provider_name,
                failures,
                duration,
            )

    # ---- image route 专用上报 -------------------------------------------

    def acquire_image_inflight(
        self, provider_name: str, endpoint_kind: str | None
    ) -> None:
        """记一次"开始用某 provider 跑 image 请求"，给 _select_for_image 排序看。

        endpoint_kind=None 时落到 "" 这个聚合 key——dual_race reserve 等不区分
        endpoint 的路径会用到。caller 必须保证最终调一次 release_image_inflight。
        """
        k = endpoint_kind or ""
        now = _facade().time.monotonic()
        with self._stats_lock:
            h = self._health.setdefault(provider_name, ProviderHealth())
            h.image_inflight[k] = h.image_inflight.get(k, 0) + 1
            h.image_last_attempted_at = now
            h.image_last_attempted_at_per_ek[k] = now

    def release_image_inflight(
        self, provider_name: str, endpoint_kind: str | None
    ) -> None:
        """配 acquire_image_inflight 用，无论请求成功 / 失败 / cancel 都要在 finally
        段里调一次。下界保护：减到 0 就 pop key，不允许出现负数。
        """
        k = endpoint_kind or ""
        with self._stats_lock:
            h = self._health.get(provider_name)
            if h is None:
                return
            cur = h.image_inflight.get(k, 0)
            if cur <= 1:
                h.image_inflight.pop(k, None)
            else:
                h.image_inflight[k] = cur - 1

    def report_image_success(
        self,
        provider_name: str,
        *,
        endpoint_kind: str | None = None,
        record_endpoint: bool = True,
    ) -> None:
        """成功一次 image_generation：清空 image 失败计数 + 记 last_used_at。

        endpoint_kind 给定时，按维度更新 image_last_used_at_per_ek，避免一个号
        在 responses lane 成功后污染 generations lane 排序。同时保留全局
        image_last_used_at 双写（observability/旧调用方仍读它）。

        不会重置 image_rate_limited_until——那是上游 quota 强约束，必须等到
        retry-after 过去；此处只动 health 维度。
        """
        h = self._health.setdefault(provider_name, ProviderHealth())
        now = _facade().time.monotonic()
        ek_key = endpoint_kind or ""
        with self._stats_lock:
            was_image_open = (
                h.image_consecutive_failures >= _facade()._IMAGE_CB_FAILURE_THRESHOLD
            )
            h.image_consecutive_failures = 0
            h.image_cooldown_until = None
            h.image_last_used_at = now
            h.image_last_attempted_at = now
            h.image_last_attempted_at_per_ek[ek_key] = now
            if endpoint_kind:
                h.image_last_used_at_per_ek[endpoint_kind] = now
            h.last_success_at = now  # 同时更新全局 last_success_at（探活逻辑会用）
        if endpoint_kind and record_endpoint:
            self.record_endpoint_success(provider_name, endpoint_kind)
        self._record_request_stats(h, total=1, success=1)
        try:
            from ..observability import account_image_calls_total

            account_image_calls_total.labels(
                account=provider_name, outcome="success"
            ).inc()
        except Exception:  # noqa: BLE001
            pass
        if was_image_open:
            _facade().logger.info(
                "image_circuit_closed: provider=%s recovered", provider_name
            )

    def report_image_failure(
        self, provider_name: str, *, endpoint_kind: str | None = None
    ) -> None:
        """失败一次 image_generation（普通 retriable，比如 SSE response.failed / 5xx）。

        累计 _IMAGE_CB_FAILURE_THRESHOLD 次 → image cooldown _IMAGE_CB_COOLDOWN_S。
        text route 不受影响（让"该号生图差但文本健康"的情况能继续跑文本）。
        """
        h = self._health.setdefault(provider_name, ProviderHealth())
        now = _facade().time.monotonic()
        ek_key = endpoint_kind or ""
        with self._stats_lock:
            h.image_consecutive_failures += 1
            h.last_failure_at = now
            h.image_last_attempted_at = now
            h.image_last_attempted_at_per_ek[ek_key] = now
            image_failures = h.image_consecutive_failures
            if image_failures >= _facade()._IMAGE_CB_FAILURE_THRESHOLD:
                h.image_cooldown_until = now + _facade()._IMAGE_CB_COOLDOWN_S
        if endpoint_kind:
            self.record_endpoint_failure(provider_name, endpoint_kind)
        self._record_request_stats(h, total=1, fail=1)
        try:
            from ..observability import account_image_calls_total

            account_image_calls_total.labels(
                account=provider_name, outcome="failure"
            ).inc()
        except Exception:  # noqa: BLE001
            pass
        if image_failures >= _facade()._IMAGE_CB_FAILURE_THRESHOLD:
            _facade().logger.warning(
                "image_circuit_open: provider=%s image_failures=%d cooldown=%.0fs",
                provider_name,
                image_failures,
                _facade()._IMAGE_CB_COOLDOWN_S,
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
        stat.last_success_at = _facade().time.monotonic()
        stat.consecutive_failures = 0
        stat.successes += 1
        stat.failure_ewma = _facade()._ewma(
            stat.failure_ewma,
            0.0,
            _facade()._ENDPOINT_FAILURE_ALPHA,
        )
        if latency_ms is not None and latency_ms > 0:
            stat.success_count += 1
            # Welford running mean — O(1), no list of samples to bound.
            stat.success_mean_ms += (
                latency_ms - stat.success_mean_ms
            ) / stat.success_count
            stat.latency_ewma_ms = _facade()._ewma(
                stat.latency_ewma_ms,
                latency_ms,
                _facade()._ENDPOINT_EWMA_ALPHA,
            )

    def record_endpoint_failure(self, provider_name: str, endpoint: str) -> None:
        stat = self._endpoint_stat(provider_name, endpoint)
        stat.last_failure_at = _facade().time.monotonic()
        stat.consecutive_failures += 1
        stat.failures += 1
        stat.failure_ewma = _facade()._ewma(
            stat.failure_ewma,
            1.0,
            _facade()._ENDPOINT_FAILURE_ALPHA,
        )

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
        now = _facade().time.monotonic()

        def _score(endpoint: str) -> tuple[float, float, float, int]:
            # Lower is better. Tuple ordering: EWMA failure/recent-failure
            # penalty, EWMA latency, recency, configured default order.
            if h is None:
                stat = EndpointStat()
            else:
                stat = h.endpoint_stats.get(endpoint, EndpointStat())
            penalty = (
                stat.failure_ewma * _facade()._IMAGE_ROUTING_FAILURE_PENALTY_MS
                + stat.consecutive_failures
                * _facade()._IMAGE_ROUTING_CONSECUTIVE_FAILURE_PENALTY_MS
            )
            # If this endpoint failed in the last 60s prefer the alternative
            # even if its mean latency is higher.
            if (
                stat.last_failure_at is not None
                and now - stat.last_failure_at
                < _facade()._ENDPOINT_RECENT_FAILURE_WINDOW_S
            ):
                penalty += _facade()._IMAGE_ROUTING_FAILURE_PENALTY_MS
            latency = (
                stat.latency_ewma_ms
                if stat.latency_ewma_ms is not None
                else stat.success_mean_ms
                if stat.success_count > 0
                else float("inf")
            )
            # Default tie-break: generations historically faster than responses,
            # so when no data exists prefer generations for generate, edits-style
            # for edit. Action-specific bias is folded in by the caller via
            # the action parameter.
            recency = -(stat.last_success_at or 0.0)
            default_order = 0 if endpoint == "generations" else 1
            return (penalty, latency, recency, default_order)

        ranked = sorted(candidates, key=_score)
        # Action-aware default tweak: edit jobs tend to be more reliable on
        # generations/edits than responses (responses' input_image base64 path
        # is heavier); push responses lower for edit unless it's clearly better.
        if action == "edit" and ranked == ["responses", "generations"]:
            stat_g = (
                h.endpoint_stats.get("generations") if h else None
            ) or EndpointStat()
            stat_r = (
                h.endpoint_stats.get("responses") if h else None
            ) or EndpointStat()
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
        now = _facade().time.monotonic()
        wait = (
            float(retry_after_s)
            if retry_after_s is not None and retry_after_s > 0
            else _facade()._IMAGE_RATE_LIMITED_DEFAULT_S
        )
        with self._stats_lock:
            h.image_rate_limited_until = now + wait
            h.last_failure_at = now
            h.image_last_attempted_at = now
        self._record_request_stats(h, total=1, fail=1)
        try:
            from ..observability import account_image_calls_total

            account_image_calls_total.labels(
                account=provider_name, outcome="rate_limited"
            ).inc()
        except Exception:  # noqa: BLE001
            pass
        _facade().logger.warning(
            "image_rate_limited: provider=%s wait=%.0fs", provider_name, wait
        )

    # ---- 探活 ------------------------------------------------------------

    @staticmethod
    def _extract_response_output_text(payload: Any) -> str:
        return _facade().extract_response_output_text(payload)

    @staticmethod
    def _extract_sse_output_text(raw: str) -> str:
        return _facade().extract_sse_output_text(raw)

    async def _probe_one(self, provider: ProviderConfig) -> bool:
        """文本算术探活：让 gpt-5.4-mini 算 99*99，必须答出 9801 才算真活。

        相比"HTTP <500 就算活"的轻量探测，这种"语义探活"能识别：
        - 上游网关返回 200 但模型其实没工作（账号 OAuth 失效但还能 200）
        - 上游强制改写成空响应 / 错误响应但 status=200
        - sub2api 把请求 sticky 到一个坏号但仍返回 200
        """
        if not _facade().endpoint_kind_allowed(provider, "responses"):
            _facade().logger.debug(
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
        body = _facade().build_provider_probe_request()
        try:
            proxy_url = await _facade().resolve_provider_proxy_url(provider.proxy)
            async with _facade().httpx.AsyncClient(
                timeout=_facade().httpx.Timeout(_facade()._PROBE_TIMEOUT_S),
                proxy=proxy_url,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code >= 500:
                _facade().logger.warning(
                    "probe_result: provider=%s status=fail http=%d",
                    provider.name,
                    resp.status_code,
                )
                return False
            if resp.status_code >= 400:
                # 4xx 通常是 auth / 配额问题——不是"上游网关挂了"，但也不是"真活"
                _facade().logger.warning(
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
                    _facade().logger.warning(
                        "probe_result: provider=%s status=fail bad_json",
                        provider.name,
                    )
                    return False
            if "9801" in text:
                return True
            _facade().logger.warning(
                "probe_result: provider=%s status=fail wrong_answer text=%.200s",
                provider.name,
                text,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            _facade().logger.warning(
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
            if p.enabled and _facade().endpoint_kind_allowed(p, "responses")
        ]
        if not providers:
            return {}

        probe_sem = asyncio.Semaphore(max(1, _facade()._PROBE_MAX_CONCURRENCY))

        async def run_probe(provider: ProviderConfig) -> bool:
            async with probe_sem:
                return await self._probe_one(provider)

        results = await asyncio.gather(
            *(run_probe(p) for p in providers),
            return_exceptions=True,
        )

        outcome: dict[str, bool] = {}
        for provider, result in zip(providers, results):
            healthy = bool(result) if not isinstance(result, BaseException) else False
            outcome[provider.name] = healthy

            h = self._health.get(provider.name)
            if h is not None:
                h.last_probe_at = _facade().time.monotonic()

            if healthy:
                self.report_success(provider.name, is_probe=True)
                _facade().logger.debug(
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
        from .probe_hooks import get_image_probe

        if not _facade().endpoint_kind_allowed(provider, "responses"):
            _facade().logger.debug(
                "image_probe_result: provider=%s status=skipped reason=endpoint_locked_to_%s",
                provider.name,
                provider.image_jobs_endpoint,
            )
            return True

        try:
            kwargs: dict[str, Any] = {
                "prompt": _facade()._IMAGE_PROBE_PROMPT,
                "size": _facade()._IMAGE_PROBE_SIZE,
                "action": "generate",
                "quality": _facade()._IMAGE_PROBE_QUALITY,
                "base_url_override": provider.base_url,
                "api_key_override": provider.api_key,
            }
            if provider.proxy is not None:
                kwargs["proxy_override"] = provider.proxy
            b64, _ = await get_image_probe()(**kwargs)
        except UpstreamError as exc:
            _facade().logger.warning(
                "image_probe_result: provider=%s status=fail err_code=%s msg=%.200s",
                provider.name,
                exc.error_code,
                str(exc),
            )
            return False
        except Exception as exc:  # noqa: BLE001
            _facade().logger.warning(
                "image_probe_result: provider=%s status=fail err=%s",
                provider.name,
                type(exc).__name__,
            )
            return False
        if not b64 or len(b64) < _facade()._IMAGE_PROBE_MIN_B64_LEN:
            _facade().logger.warning(
                "image_probe_result: provider=%s status=fail b64_len=%d (min=%d)",
                provider.name,
                len(b64) if b64 else 0,
                _facade()._IMAGE_PROBE_MIN_B64_LEN,
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
            if p.enabled and _facade().endpoint_kind_allowed(p, "responses")
        ]
        if not providers:
            return {}
        probe_sem = asyncio.Semaphore(max(1, _facade()._PROBE_MAX_CONCURRENCY))

        async def run_probe(provider: ProviderConfig) -> bool:
            async with probe_sem:
                return await self._probe_image_one(provider)

        results = await asyncio.gather(
            *(run_probe(p) for p in providers),
            return_exceptions=True,
        )
        outcome: dict[str, bool] = {}
        for provider, result in zip(providers, results):
            healthy = bool(result) if not isinstance(result, BaseException) else False
            outcome[provider.name] = healthy
            if healthy:
                self.report_image_probe_success(provider.name)
                _facade().logger.debug(
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
        with self._stats_lock:
            was_image_open = (
                h.image_consecutive_failures >= _facade()._IMAGE_CB_FAILURE_THRESHOLD
            )
            h.image_consecutive_failures = 0
            h.image_cooldown_until = None
            h.last_probe_at = _facade().time.monotonic()
        if was_image_open:
            _facade().logger.info(
                "image_circuit_closed: provider=%s recovered via probe",
                provider_name,
            )

    def report_image_probe_failure(self, provider_name: str) -> None:
        """image probe 失败：累加 image_consecutive_failures，达到阈值熔断 image cooldown。

        不动 total_requests / failed_requests——这是探针失败，不是真实请求失败。
        但累加 image_consecutive_failures 让坏号能从 image route 候选里被排除。
        """
        h = self._health.setdefault(provider_name, ProviderHealth())
        now = _facade().time.monotonic()
        with self._stats_lock:
            h.image_consecutive_failures += 1
            h.last_probe_at = now
            image_failures = h.image_consecutive_failures
            if image_failures >= _facade()._IMAGE_CB_FAILURE_THRESHOLD:
                h.image_cooldown_until = now + _facade()._IMAGE_CB_COOLDOWN_S
        if image_failures >= _facade()._IMAGE_CB_FAILURE_THRESHOLD:
            _facade().logger.warning(
                "image_circuit_open: provider=%s probe_failures=%d cooldown=%.0fs",
                provider_name,
                image_failures,
                _facade()._IMAGE_CB_COOLDOWN_S,
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
            from .. import account_limiter
            from ..observability import (
                _ALLOWED_IMAGE_STATES,
                account_image_quota_used,
                account_image_state,
            )
        except Exception:  # noqa: BLE001
            return

        wall_now = _facade().time.time()
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
                    await redis.zremrangebyscore(ts_key, 0, wall_now - window_s)
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
            _facade().logger.warning("flush_stats_to_redis failed", exc_info=True)

    def get_status(self) -> list[dict[str, Any]]:
        """返回所有 provider 的当前状态（调试 / admin API 用）。"""
        now = _facade().time.monotonic()
        result: list[dict[str, Any]] = []
        for p in self._providers:
            h = self._health.get(p.name, ProviderHealth())
            if h.consecutive_failures >= _facade()._CB_FAILURE_THRESHOLD:
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
            elif h.image_cooldown_until is not None and now < h.image_cooldown_until:
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


__all__ = ["ProviderProbeMixin"]
