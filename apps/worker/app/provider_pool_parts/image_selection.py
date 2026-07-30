"""Image-route candidate qualification and quota-aware selection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
from typing import Any

from lumen_core.constants import GenerationErrorCode as EC
from lumen_core.providers_parts.selection import (
    endpoint_kind_allowed,
    provider_supports_route,
)

from ..provider_runtime.contracts import (
    ProviderConfig,
    ProviderHealth,
    ResolvedProvider,
)
from ..provider_runtime.errors import UpstreamError

logger = logging.getLogger("app.provider_pool")


ImageCandidate = tuple[ProviderConfig, tuple[int, float, float]]


@dataclass(frozen=True)
class ImageHealthSnapshot:
    text_circuit_open: bool
    image_cooldown_until: float | None
    image_rate_limited_until: float | None
    inflight: int
    last_attempted: float | None
    last_used: float | None


@dataclass
class ImageCandidateBuckets:
    candidates: list[ImageCandidate] = field(default_factory=list)
    mask_file_candidates: list[ImageCandidate] = field(default_factory=list)
    mask_url_candidates: list[ImageCandidate] = field(default_factory=list)

    def add(
        self,
        candidate: ImageCandidate,
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
    ) -> list[ImageCandidate]:
        return self.select_with_logger(
            requires_mask=requires_mask,
            mask_transport_required=mask_transport_required,
            task_id=task_id,
            logger=logger,
        )

    def select_with_logger(
        self,
        *,
        requires_mask: bool,
        mask_transport_required: bool,
        task_id: str | None,
        logger: Any,
    ) -> list[ImageCandidate]:
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


@dataclass(frozen=True)
class ImageCandidateQuery:
    avoided: set[str]
    endpoint_kind: str | None
    ignore_cooldown: bool
    redis: Any
    account_limiter: Any
    wall_now: float
    mono_now: float
    requires_mask: bool
    mask_transport_required: bool
    task_id: str | None
    size_bucket: str | None
    cost_class: str | None


_ImageCandidate = ImageCandidate
_ImageHealthSnapshot = ImageHealthSnapshot
_ImageCandidateQuery = ImageCandidateQuery


@dataclass(frozen=True)
class ImageSelectionDependencies:
    logger: Any
    monotonic: Callable[[], float]
    wall_time: Callable[[], float]
    account_limiter: Any
    health_type: type[ProviderHealth]
    health_snapshot_type: type[ImageHealthSnapshot]
    candidate_buckets_type: type[ImageCandidateBuckets]
    candidate_query_type: type[ImageCandidateQuery]
    endpoint_skip_reason: Callable[[ProviderConfig, str | None], str | None]
    health_skip_reason: Callable[..., str | None]
    last_attempt_key: Callable[[ImageHealthSnapshot], float]
    load_avoided_providers: Callable[[Any, str | None], Awaitable[set[str]]]
    only_avoided_providers: Callable[[set[str], list[tuple[str, str]]], bool]
    all_accounts_failed: Callable[[list[tuple[str, str]]], UpstreamError]
    candidate_sort_key: Callable[[ImageCandidate], tuple[int, float, float, int]]
    resolved_provider: Callable[[ProviderConfig], ResolvedProvider]


def image_endpoint_skip_reason(
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


def image_health_skip_reason(
    snapshot: ImageHealthSnapshot,
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


def image_last_attempt_key(snapshot: ImageHealthSnapshot) -> float:
    attempted_or_used = (
        snapshot.last_attempted
        if snapshot.last_attempted is not None
        else snapshot.last_used
    )
    return attempted_or_used if attempted_or_used is not None else float("-inf")


def image_candidate_sort_key(
    candidate: ImageCandidate,
) -> tuple[int, float, float, int]:
    provider, (inflight, last_used, adaptive_score) = candidate
    return inflight, adaptive_score, last_used, -provider.priority


def resolved_image_provider(provider: ProviderConfig) -> ResolvedProvider:
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


def decode_avoided_provider(value: Any) -> str | None:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None
    return value if isinstance(value, str) else None


async def load_avoided_image_providers(
    redis: Any,
    task_id: str | None,
    *,
    decoder: Callable[[Any], str | None] = decode_avoided_provider,
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
        if (decoded := decoder(item)) is not None
    }


def only_avoided_image_providers(
    avoided: set[str],
    skipped: list[tuple[str, str]],
) -> bool:
    return bool(avoided) and all(
        reason == "avoided_from_previous_attempt" for _, reason in skipped
    )


def all_image_accounts_failed(
    skipped: list[tuple[str, str]],
) -> UpstreamError:
    detail = ", ".join(f"{name}({reason})" for name, reason in skipped) or "none"
    return UpstreamError(
        f"all accounts unavailable for image: {detail}",
        error_code=EC.ALL_ACCOUNTS_FAILED.value,
        status_code=503,
        payload={"skipped": skipped},
    )


class ProviderPoolImageSelectionMixin:
    def _image_health_snapshot(
        self,
        health: ProviderHealth,
        *,
        endpoint_kind: str | None,
        now: float,
    ) -> _ImageHealthSnapshot:
        deps = self._image_selection_dependencies()
        endpoint_key = endpoint_kind or ""
        with self._stats_lock:
            return deps.health_snapshot_type(
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
        deps = self._image_selection_dependencies()
        try:
            allowed, retry_after = await account_limiter.check_quota(
                redis,
                provider.name,
                provider.image_rate_limit,
                provider.image_daily_quota,
                now=wall_now,
            )
        except Exception as exc:  # noqa: BLE001
            deps.logger.warning(
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
        deps = self._image_selection_dependencies()
        reason = deps.endpoint_skip_reason(provider, endpoint_kind)
        if reason is not None:
            return None, reason
        health = self._health.setdefault(provider.name, deps.health_type())
        if provider.name in avoided:
            return None, "avoided_from_previous_attempt"
        snapshot = self._image_health_snapshot(
            health,
            endpoint_kind=endpoint_kind,
            now=mono_now,
        )
        reason = deps.health_skip_reason(
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
            (snapshot.inflight, deps.last_attempt_key(snapshot), adaptive_score),
        ), None

    async def _collect_image_candidates(
        self,
        enabled: list[ProviderConfig],
        *,
        query: _ImageCandidateQuery,
    ) -> tuple[list[_ImageCandidate], list[tuple[str, str]]]:
        deps = self._image_selection_dependencies()
        buckets = deps.candidate_buckets_type()
        skipped: list[tuple[str, str]] = []
        for provider in enabled:
            candidate, reason = await self._qualify_image_candidate(
                provider,
                avoided=query.avoided,
                endpoint_kind=query.endpoint_kind,
                ignore_cooldown=query.ignore_cooldown,
                redis=query.redis,
                account_limiter=query.account_limiter,
                wall_now=query.wall_now,
                mono_now=query.mono_now,
                size_bucket=query.size_bucket,
                cost_class=query.cost_class,
            )
            if candidate is None:
                skipped.append((provider.name, reason or "unavailable"))
                continue
            buckets.add(
                candidate,
                requires_mask=query.requires_mask,
                mask_transport_required=query.mask_transport_required,
            )
        return buckets.select(
            requires_mask=query.requires_mask,
            mask_transport_required=query.mask_transport_required,
            task_id=query.task_id,
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
        """Select image accounts by health, quota, inflight load, and EWMA."""
        deps = self._image_selection_dependencies()
        enabled = [p for p in self._providers if p.enabled and purpose in p.purposes]
        if not enabled:
            raise UpstreamError(
                "no upstream providers configured or all disabled",
                error_code=EC.NO_PROVIDERS.value,
                status_code=503,
            )

        now = deps.monotonic()
        wall_now = deps.wall_time()
        redis = self.get_redis()
        avoided = await deps.load_avoided_providers(redis, task_id)
        candidates, skipped = await self._collect_image_candidates(
            enabled,
            query=deps.candidate_query_type(
                avoided=avoided,
                endpoint_kind=endpoint_kind,
                ignore_cooldown=ignore_cooldown,
                redis=redis,
                account_limiter=deps.account_limiter,
                wall_now=wall_now,
                mono_now=now,
                requires_mask=requires_mask,
                mask_transport_required=mask_transport_required,
                task_id=task_id,
                size_bucket=size_bucket,
                cost_class=cost_class or queue_lane,
            ),
        )
        if not candidates:
            if deps.only_avoided_providers(avoided, skipped):
                deps.logger.info(
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
            raise deps.all_accounts_failed(skipped)

        candidates.sort(key=deps.candidate_sort_key)
        if acquire_inflight and task_id:
            candidates = await self._reserve_first_quota_candidate(
                candidates,
                redis=redis,
                account_limiter=deps.account_limiter,
                task_id=task_id,
                wall_now=wall_now,
                mono_now=now,
                skipped=skipped,
            )
            if not candidates:
                raise deps.all_accounts_failed(skipped)
        result = [deps.resolved_provider(provider) for provider, _ in candidates]
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
        """Reserve quota for the provider whose inflight slot is claimed."""
        deps = self._image_selection_dependencies()
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
                deps.logger.warning(
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
            health = self._health.setdefault(provider.name, deps.health_type())
            with self._stats_lock:
                health.image_rate_limited_until = mono_now + max(1.0, retry_after)
            skipped.append(
                (provider.name, f"quota_exhausted retry_after={retry_after:.0f}s")
            )
        return []
