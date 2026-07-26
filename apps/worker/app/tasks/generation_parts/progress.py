from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .runtime import GenerationRunState


class ImageProgressPublisher:
    """Translate upstream progress callbacks into stable generation events."""

    def __init__(self, state: GenerationRunState, ports: Any) -> None:
        self.state = state
        self.ports = ports

    def pop_provider_used_event(self) -> dict[str, str]:
        if self.state.provider_used_events:
            return self.state.provider_used_events.pop(0)
        return {}

    async def __call__(self, event: dict[str, Any]) -> None:
        await self._raise_if_interrupted()
        handler = self._handlers().get(str(event.get("type") or ""))
        if handler is not None:
            await handler(event)

    def _handlers(
        self,
    ) -> dict[str, Callable[[dict[str, Any]], Awaitable[None]]]:
        return {
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

    async def _raise_if_interrupted(self) -> None:
        state = self.state
        if state.lease_lost.is_set():
            raise self.ports.lease._LeaseLost("generation lease renewer failed")
        if await self.ports.lease._is_cancelled(state.redis, state.task_id):
            raise self.ports.lease._TaskCancelled("cancelled during upstream call")

    async def _record_image_job(self, event: dict[str, Any]) -> None:
        metadata = self.state.image_job_meta
        url = self.ports.queue._redis_text(event.get("image_job_url"))
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
            self.ports.events._provider_attempt_from_progress(
                event,
                status="failover",
                attempt_epoch=self.state.attempt,
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
        provider = self.ports.queue._redis_text(
            event.get("provider") or event.get("actual_provider")
        )
        if not provider:
            return
        metadata = self._provider_metadata(event, provider)
        self.state.provider_used_events.append(metadata)
        self.state.provider_attempt_log.append(
            {
                **self.ports.events._provider_attempt_from_progress(
                    event,
                    status="used",
                    attempt_epoch=self.state.attempt,
                ),
                **metadata,
            }
        )
        await self.ports.queue._inflight_set_fields(
            self.state.redis,
            self.state.task_id,
            self._provider_inflight_update(metadata),
        )

    def _provider_metadata(
        self,
        event: dict[str, Any],
        provider: str,
    ) -> dict[str, str]:
        metadata = {"provider": provider}
        for source_key in ("route", "source", "endpoint"):
            value = self.ports.queue._redis_text(event.get(source_key))
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
            lane = self.ports.queue._classify_inflight_lane(route, endpoint)
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
        await self.ports.events.publish_event(
            state.redis,
            state.user_id,
            state.channel,
            self.ports.events.EV_GEN_PARTIAL_IMAGE,
            {
                **self._event_identity(),
                "stage": self.ports.domain.GenerationStage.RENDERING.value,
                "substage": self.ports.domain.GenerationStage.PARTIAL_RECEIVED.value,
                "index": event.get("index"),
                "count": event.get("count"),
            },
        )

    async def _publish_lifecycle_progress(self, event: dict[str, Any]) -> None:
        is_final = event.get("type") in {"final_image", "completed"}
        stage = (
            self.ports.domain.GenerationStage.FINALIZING.value
            if is_final
            else self.ports.domain.GenerationStage.RENDERING.value
        )
        substage = (
            self.ports.domain.GenerationStage.FINAL_RECEIVED.value
            if is_final
            else self.ports.domain.GenerationStage.STREAM_STARTED.value
        )
        await self.ports.events.publish_event(
            self.state.redis,
            self.state.user_id,
            self.state.channel,
            self.ports.events.EV_GEN_PROGRESS,
            {
                **self._event_identity(),
                "stage": stage,
                "substage": substage,
                "source": event.get("source") or "responses_fallback",
            },
        )

    async def _publish_provider_failover(self, event: dict[str, Any]) -> None:
        state = self.state
        from_provider = self.ports.queue._redis_text(event.get("from_provider"))
        route = self.ports.queue._redis_text(event.get("route")) or ""
        state.provider_attempt_log.append(
            self.ports.events._provider_attempt_from_progress(
                event,
                status="failover",
                attempt_epoch=state.attempt,
                provider_key="from_provider",
                route_default=route or None,
            )
        )
        await self.ports.queue._inflight_set_fields(
            state.redis,
            state.task_id,
            self._failover_inflight_update(from_provider, route),
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
            lane = self.ports.queue._classify_inflight_lane(route, "")
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
        await self.ports.events.publish_event(
            self.state.redis,
            self.state.user_id,
            self.state.channel,
            self.ports.events.EV_GEN_PROGRESS,
            self.ports.events._sanitize_provider_progress_payload(
                {
                    **self._event_identity(),
                    "stage": self.ports.domain.GenerationStage.RENDERING.value,
                    "substage": self.ports.domain.GenerationStage.PROVIDER_SELECTED.value,
                    **payload,
                },
                expose_provider_diagnostics=(
                    self.ports.queue.settings.expose_provider_diagnostics
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
