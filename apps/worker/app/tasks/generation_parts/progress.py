from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from lumen_core.constants import (
    EV_GEN_PARTIAL_IMAGE,
    EV_GEN_PROGRESS,
    GenerationStage,
)
from lumen_core.model_entities import Generation

from ...upstream_clients.image_job_models import ImageJobExecutionHandle
from .diagnostics import (
    provider_attempt_from_progress,
    sanitize_provider_progress_payload,
)
from .errors import LeaseLost, StaleGenerationAttempt, TaskCancelled
from .execution_boundary import SIDECAR_EXECUTION_KEY
from .lease import is_cancelled
from .queue import classify_inflight_lane, inflight_set_fields, redis_text
from .retry_state import RUNNING_GENERATION_STATUSES
from .run_state import GenerationRunState
from .services import RunGenerationDeps


class ImageProgressPublisher:
    """Translate upstream progress callbacks into stable generation events."""

    def __init__(self, state: GenerationRunState, deps: RunGenerationDeps) -> None:
        self.state = state
        self.deps = deps

    def pop_provider_used_event(self) -> dict[str, str]:
        if self.state.provider_used_events:
            return self.state.provider_used_events.pop(0)
        return {}

    async def __call__(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        handler = self._handlers().get(event_type)
        if event_type == "image_job_execution" and handler is not None:
            await handler(event)
            return
        await self._raise_if_interrupted()
        if handler is not None:
            await handler(event)

    def _handlers(
        self,
    ) -> dict[str, Callable[[dict[str, Any]], Awaitable[None]]]:
        return {
            "image_job_execution": self._persist_image_job_execution,
            "image_job_image": self._record_image_job,
            "route_diagnostic": self._publish_route_diagnostic,
            "endpoint_failover": self._publish_endpoint_failover,
            "provider_used": self._record_provider_used,
            "partial_image": self._publish_partial_image,
            "fallback_started": self._publish_lifecycle_progress,
            "final_image": self._publish_lifecycle_progress,
            "completed": self._publish_lifecycle_progress,
            "provider_failover": self._publish_provider_failover,
        }

    async def _persist_image_job_execution(self, event: dict[str, Any]) -> None:
        execution = ImageJobExecutionHandle.from_mapping(event.get("execution"))
        if execution is None:
            return
        payload = execution.to_dict()
        self.state.sidecar_execution = execution
        async with self.deps.store.session() as session:
            current = (
                await session.execute(
                    select(Generation)
                    .where(
                        Generation.id == self.state.task_id,
                        Generation.attempt == self.state.attempt,
                        Generation.status.in_(RUNNING_GENERATION_STATUSES),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current is None:
                raise StaleGenerationAttempt(
                    "sidecar execution persistence lost generation ownership "
                    f"task={self.state.task_id} attempt={self.state.attempt}"
                )
            current_request = (
                dict(current.upstream_request)
                if isinstance(current.upstream_request, dict)
                else {}
            )
            current_request[SIDECAR_EXECUTION_KEY] = payload
            current.upstream_request = current_request
            await session.commit()
        request = dict(self.state.gen_upstream_request_snapshot or {})
        request[SIDECAR_EXECUTION_KEY] = payload
        self.state.gen_upstream_request_snapshot = request

    async def _raise_if_interrupted(self) -> None:
        state = self.state
        if state.lease_lost.is_set():
            raise LeaseLost("generation lease renewer failed")
        if await is_cancelled(state.redis, state.task_id):
            raise TaskCancelled("cancelled during upstream call")

    async def _record_image_job(self, event: dict[str, Any]) -> None:
        metadata = self.state.image_job_meta
        url = redis_text(event.get("image_job_url"))
        if url:
            metadata["image_job_url"] = url
        for key in ("job_id", "endpoint_used", "expires_at", "format"):
            value = event.get(key)
            if value is not None:
                metadata[f"image_job_{key}"] = value

    async def _publish_route_diagnostic(self, event: dict[str, Any]) -> None:
        diagnostic = {
            "route": event.get("route"),
            "fallback_route": event.get("fallback_route"),
            "reason": event.get("reason"),
            "byok": event.get("byok"),
            "status": event.get("status") or "routed",
        }
        self.state.route_diagnostics.append(
            {key: value for key, value in diagnostic.items() if value is not None}
        )
        await self._publish_provider_progress(
            event,
            {
                "route_diagnostic": True,
                "provider": event.get("provider"),
                "route": event.get("route"),
                "fallback_route": event.get("fallback_route"),
                "reason": event.get("reason"),
                "byok": event.get("byok"),
            },
        )

    async def _publish_endpoint_failover(self, event: dict[str, Any]) -> None:
        self.state.provider_attempt_log.append(
            provider_attempt_from_progress(
                event,
                status="failover",
                attempt_epoch=self.state.attempt,
                redis_text=redis_text,
                route_default="image_jobs",
            )
        )
        await self._publish_provider_progress(
            event,
            {
                "endpoint_failover": True,
                "provider": event.get("provider"),
                "from_endpoint": event.get("from_endpoint"),
                "remaining": event.get("remaining"),
                "reason": event.get("reason"),
                "route": event.get("route") or "image_jobs",
            },
        )

    async def _record_provider_used(self, event: dict[str, Any]) -> None:
        provider = redis_text(
            event.get("provider") or event.get("actual_provider")
        )
        if not provider:
            return
        metadata = self._provider_metadata(event, provider)
        self.state.provider_used_events.append(metadata)
        self.state.provider_attempt_log.append(
            {
                **provider_attempt_from_progress(
                    event,
                    status="used",
                    attempt_epoch=self.state.attempt,
                    redis_text=redis_text,
                ),
                **metadata,
            }
        )
        await inflight_set_fields(
            self.state.redis,
            self.state.task_id,
            self._provider_inflight_update(metadata),
            services=self.deps,
        )

    def _provider_metadata(
        self,
        event: dict[str, Any],
        provider: str,
    ) -> dict[str, str]:
        metadata = {"provider": provider}
        for source_key in ("route", "source", "endpoint"):
            value = redis_text(event.get(source_key))
            if value:
                metadata[source_key] = value
        return metadata

    def _provider_inflight_update(
        self,
        metadata: dict[str, str],
    ) -> dict[str, str]:
        provider = metadata["provider"]
        route = metadata.get("route") or ""
        endpoint = metadata.get("endpoint") or ""
        if self.state.is_dual_race:
            lane = classify_inflight_lane(route, endpoint)
            update = {f"{lane}_provider": provider}
            if route:
                update[f"{lane}_route"] = route
            if endpoint:
                update[f"{lane}_endpoint"] = endpoint
            return update
        update = {"provider": provider}
        if route:
            update["actual_route"] = route
        if endpoint:
            update["endpoint"] = endpoint
        return update

    async def _publish_partial_image(self, event: dict[str, Any]) -> None:
        state = self.state
        state.has_partial = True
        await self.deps.events.publish(
            state.redis,
            state.user_id,
            state.channel,
            EV_GEN_PARTIAL_IMAGE,
            {
                **self._event_identity(),
                "stage": GenerationStage.RENDERING.value,
                "substage": GenerationStage.PARTIAL_RECEIVED.value,
                "index": event.get("index"),
                "count": event.get("count"),
            },
        )

    async def _publish_lifecycle_progress(self, event: dict[str, Any]) -> None:
        is_final = event.get("type") in {"final_image", "completed"}
        stage = (
            GenerationStage.FINALIZING.value
            if is_final
            else GenerationStage.RENDERING.value
        )
        substage = (
            GenerationStage.FINAL_RECEIVED.value
            if is_final
            else GenerationStage.STREAM_STARTED.value
        )
        await self.deps.events.publish(
            self.state.redis,
            self.state.user_id,
            self.state.channel,
            EV_GEN_PROGRESS,
            {
                **self._event_identity(),
                "stage": stage,
                "substage": substage,
                "source": event.get("source") or "responses_fallback",
            },
        )

    async def _publish_provider_failover(self, event: dict[str, Any]) -> None:
        state = self.state
        from_provider = redis_text(event.get("from_provider"))
        route = redis_text(event.get("route")) or ""
        state.provider_attempt_log.append(
            provider_attempt_from_progress(
                event,
                status="failover",
                attempt_epoch=state.attempt,
                redis_text=redis_text,
                provider_key="from_provider",
                route_default=route or None,
            )
        )
        await inflight_set_fields(
            state.redis,
            state.task_id,
            self._failover_inflight_update(from_provider, route),
            services=self.deps,
        )
        await self._publish_provider_progress(
            event,
            {
                "provider_failover": True,
                "from_provider": event.get("from_provider"),
                "remaining": event.get("remaining"),
                "reason": event.get("reason"),
                "route": event.get("route") or "responses",
            },
        )

    def _failover_inflight_update(
        self,
        from_provider: str | None,
        route: str,
    ) -> dict[str, str]:
        if self.state.is_dual_race:
            lane = classify_inflight_lane(route, "")
            update = {f"{lane}_status": "failover"}
            if from_provider:
                update[f"{lane}_last_failed"] = from_provider
            return update
        update = {"status": "failover"}
        if from_provider:
            update["last_failed"] = from_provider
        return update

    async def _publish_provider_progress(
        self,
        _event: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        await self.deps.events.publish(
            self.state.redis,
            self.state.user_id,
            self.state.channel,
            EV_GEN_PROGRESS,
            sanitize_provider_progress_payload(
                {
                    **self._event_identity(),
                    "stage": GenerationStage.RENDERING.value,
                    "substage": GenerationStage.PROVIDER_SELECTED.value,
                    **payload,
                },
                expose_provider_diagnostics=(
                    self.deps.queue.expose_provider_diagnostics
                ),
            ),
        )

    def _event_identity(self) -> dict[str, str]:
        state = self.state
        return {
            "generation_id": state.task_id,
            "message_id": state.message_id,
            "trace_id": state.trace_id,
        }
