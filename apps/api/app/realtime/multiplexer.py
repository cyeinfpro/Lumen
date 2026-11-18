"""Process-scoped single-connection Redis Pub/Sub multiplexer for SSE."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
import inspect
import logging
import time
from typing import Any

from fastapi import FastAPI


logger = logging.getLogger("app.routes.events")

REALTIME_MULTIPLEXER_STATE_ATTR = "realtime_multiplexer"
DEFAULT_CLIENT_QUEUE_MAXSIZE = 1024
_READER_POLL_TIMEOUT_SECONDS = 1.0
_MOCK_SPIN_GUARD_SECONDS = 0.005
_RECONNECT_BACKOFF_SECONDS = 0.2

ClientEventQueue = asyncio.Queue[dict[str, Any] | None]
RealtimeRouteLifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def _decode_channel(value: object) -> str | None:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return None


async def _close_pubsub_handle(pubsub: Any) -> None:
    close_fn = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
    if not callable(close_fn):
        return
    result = close_fn()
    if inspect.isawaitable(result):
        await result


class ProcessRealtimeMultiplexer:
    """Maintain at most one Redis Pub/Sub connection per API worker process.

    Channels are dynamically subscribed/unsubscribed by active client reference
    count, and incoming frames are fanned out in memory via
    ``dict[str, set[asyncio.Queue]]``.
    """

    def __init__(
        self,
        redis_or_provider: Any,
        *,
        queue_maxsize: int = DEFAULT_CLIENT_QUEUE_MAXSIZE,
        close_pubsub_when_idle: bool = False,
    ) -> None:
        self._redis_provider: Callable[[], Any] = (
            redis_or_provider
            if callable(redis_or_provider)
            else (lambda: redis_or_provider)
        )
        self._explicit_redis = (
            None if callable(redis_or_provider) else redis_or_provider
        )
        self._bound_redis: Any | None = None
        self._pubsub: Any | None = None
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._live_consumers: set[ClientEventQueue] = set()
        self._lock = asyncio.Lock()
        self._live_ready = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._queue_maxsize = max(1, int(queue_maxsize))
        self._close_pubsub_when_idle = close_pubsub_when_idle
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscribers(self) -> dict[str, set[asyncio.Queue]]:
        return self._subscribers

    @property
    def active_channel_count(self) -> int:
        return len(self._subscribers)

    @property
    def active_subscriber_count(self) -> int:
        seen: set[int] = set()
        for queues in self._subscribers.values():
            for queue in queues:
                seen.add(id(queue))
        return len(seen)

    def matches_redis(self, redis: Any) -> bool:
        if self._explicit_redis is not None:
            return self._explicit_redis is redis
        if self._bound_redis is not None:
            return self._bound_redis is redis
        return True

    def _resolve_redis(self) -> Any:
        return self._redis_provider()

    async def _ensure_pubsub_locked(self) -> Any:
        redis = self._resolve_redis()
        if (
            self._pubsub is not None
            and self._bound_redis is not redis
            and not self._subscribers
        ):
            try:
                await _close_pubsub_handle(self._pubsub)
            except Exception:  # noqa: BLE001
                logger.warning("sse pubsub close failed", exc_info=True)
            self._pubsub = None
            self._bound_redis = None
        if self._pubsub is None:
            self._pubsub = redis.pubsub()
            self._bound_redis = redis
        return self._pubsub

    def _ensure_reader_started_locked(self) -> None:
        if self._closed:
            return
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(
                self._reader_loop(),
                name="sse-realtime-multiplexer",
            )

    def _update_live_ready_locked(self) -> None:
        if (
            not self._closed
            and self._pubsub is not None
            and bool(self._subscribers)
            and bool(self._live_consumers)
        ):
            self._live_ready.set()
            self._ensure_reader_started_locked()
        else:
            self._live_ready.clear()

    async def subscribe_client(
        self,
        channels: Sequence[str],
        *,
        user_id: str,
    ) -> tuple[ClientEventQueue, list[str]]:
        unique_channels = list(dict.fromkeys(ch for ch in channels if ch))
        queue: ClientEventQueue = asyncio.Queue(maxsize=self._queue_maxsize)
        if not unique_channels:
            return queue, []

        async with self._lock:
            if self._closed:
                raise RuntimeError("realtime multiplexer is closed")
            pubsub = await self._ensure_pubsub_locked()
            newly_active: list[str] = []
            for channel in unique_channels:
                bucket = self._subscribers.get(channel)
                if bucket is None:
                    bucket = set()
                    self._subscribers[channel] = bucket
                    newly_active.append(channel)
                bucket.add(queue)

            if newly_active:
                try:
                    await pubsub.subscribe(*newly_active)
                except Exception:
                    for channel in unique_channels:
                        bucket = self._subscribers.get(channel)
                        if bucket is not None:
                            bucket.discard(queue)
                            if not bucket:
                                self._subscribers.pop(channel, None)
                    self._update_live_ready_locked()
                    logger.warning(
                        "sse pubsub subscribe failed user_id=%s channels=%d",
                        user_id,
                        len(newly_active),
                        exc_info=True,
                    )
                    raise
            self._update_live_ready_locked()
        return queue, unique_channels

    @asynccontextmanager
    async def live_consumer(self, queue: ClientEventQueue) -> AsyncIterator[None]:
        async with self._lock:
            if not self._closed:
                self._live_consumers.add(queue)
                self._update_live_ready_locked()
        try:
            yield
        finally:
            async with self._lock:
                self._live_consumers.discard(queue)
                self._update_live_ready_locked()

    async def unsubscribe_client(
        self,
        queue: ClientEventQueue,
        channels: Sequence[str],
    ) -> None:
        unique_channels = list(dict.fromkeys(ch for ch in channels if ch))
        async with self._lock:
            self._live_consumers.discard(queue)
            now_empty: list[str] = []
            for channel in unique_channels:
                bucket = self._subscribers.get(channel)
                if bucket is None:
                    continue
                bucket.discard(queue)
                if not bucket:
                    self._subscribers.pop(channel, None)
                    now_empty.append(channel)
            self._update_live_ready_locked()

            pubsub = self._pubsub
            if now_empty and pubsub is not None:
                try:
                    await pubsub.unsubscribe(*now_empty)
                except Exception:  # noqa: BLE001
                    logger.warning("sse pubsub unsubscribe failed", exc_info=True)

            if (
                not self._subscribers
                and self._close_pubsub_when_idle
                and self._pubsub is not None
            ):
                idle_pubsub = self._pubsub
                self._pubsub = None
                self._bound_redis = None
                try:
                    await _close_pubsub_handle(idle_pubsub)
                except Exception:  # noqa: BLE001
                    logger.warning("sse pubsub close failed", exc_info=True)

    def _enqueue_for_channel(self, channel: str, message: dict[str, Any]) -> None:
        queues = tuple(self._subscribers.get(channel, ()))
        for queue in queues:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                try:
                    _ = queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    logger.warning(
                        "sse client queue overflow dropped frame channel=%s",
                        channel,
                    )

    def _fanout_message(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        raw_type = message.get("type")
        if raw_type is not None:
            msg_type = _decode_channel(raw_type)
            if msg_type not in {"message", "pmessage"}:
                return
        channel = _decode_channel(message.get("channel"))
        if not channel:
            return
        self._enqueue_for_channel(channel, message)

    async def _recover_pubsub_after_error(self) -> None:
        async with self._lock:
            if self._closed or not self._subscribers:
                return
            broken = self._pubsub
            self._pubsub = None
            self._bound_redis = None
            if broken is not None:
                try:
                    await _close_pubsub_handle(broken)
                except Exception:  # noqa: BLE001
                    logger.debug("sse broken pubsub close ignored", exc_info=True)
            channels = list(self._subscribers.keys())
            if not channels:
                return
            pubsub = await self._ensure_pubsub_locked()
            await pubsub.subscribe(*channels)

    async def _reader_loop(self) -> None:
        while not self._closed:
            await self._live_ready.wait()
            if self._closed:
                break
            pubsub = self._pubsub
            if pubsub is None or not self._subscribers or not self._live_consumers:
                self._live_ready.clear()
                continue
            started = time.monotonic()
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=_READER_POLL_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                if self._closed:
                    break
                logger.warning(
                    "sse multiplexer pubsub read failed; attempting reconnect",
                    exc_info=True,
                )
                await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
                try:
                    await self._recover_pubsub_after_error()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "sse multiplexer pubsub reconnect failed",
                        exc_info=True,
                    )
                continue

            if message is not None:
                self._fanout_message(message)
            elif time.monotonic() - started < _MOCK_SPIN_GUARD_SECONDS:
                await asyncio.sleep(_MOCK_SPIN_GUARD_SECONDS)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._live_ready.set()
        reader = self._reader_task
        self._reader_task = None
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

        async with self._lock:
            active_channels = list(self._subscribers.keys())
            queues: list[ClientEventQueue] = []
            seen: set[int] = set()
            for bucket in self._subscribers.values():
                for queue in bucket:
                    if id(queue) not in seen:
                        seen.add(id(queue))
                        queues.append(queue)
            self._subscribers.clear()
            self._live_consumers.clear()
            self._live_ready.clear()

            pubsub = self._pubsub
            self._pubsub = None
            self._bound_redis = None
            if pubsub is not None:
                if active_channels:
                    try:
                        await pubsub.unsubscribe(*active_channels)
                    except Exception:  # noqa: BLE001
                        logger.warning("sse pubsub unsubscribe failed", exc_info=True)
                try:
                    await _close_pubsub_handle(pubsub)
                except Exception:  # noqa: BLE001
                    logger.warning("sse pubsub close failed", exc_info=True)

            for queue in queues:
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    try:
                        _ = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        queue.put_nowait(None)
                    except asyncio.QueueFull:
                        pass


def _extract_app_from_target(target: Any) -> Any | None:
    if target is None:
        return None
    if isinstance(target, FastAPI):
        return target
    scope = getattr(target, "scope", None)
    if isinstance(scope, Mapping):
        return scope.get("app")
    if hasattr(target, "state") and not hasattr(target, "headers"):
        return target
    return getattr(target, "app", None)


def get_app_realtime_multiplexer(target: Any) -> ProcessRealtimeMultiplexer | None:
    app = _extract_app_from_target(target)
    state = getattr(app, "state", None) if app is not None else None
    if state is None:
        return None
    mux = getattr(state, REALTIME_MULTIPLEXER_STATE_ATTR, None)
    return mux if isinstance(mux, ProcessRealtimeMultiplexer) else None


async def close_app_realtime_multiplexer(target: Any) -> None:
    app = _extract_app_from_target(target)
    state = getattr(app, "state", None) if app is not None else None
    if state is None:
        return
    mux = getattr(state, REALTIME_MULTIPLEXER_STATE_ATTR, None)
    if isinstance(mux, ProcessRealtimeMultiplexer):
        try:
            await mux.aclose()
        finally:
            if getattr(state, REALTIME_MULTIPLEXER_STATE_ATTR, None) is mux:
                delattr(state, REALTIME_MULTIPLEXER_STATE_ATTR)


def resolve_realtime_multiplexer(
    request: Any,
    redis: Any,
) -> tuple[ProcessRealtimeMultiplexer, bool]:
    """Return ``(multiplexer, ephemeral)`` for an incoming SSE request.

    When ``request`` is bound to an application ``state`` container, a single
    process-scoped ``ProcessRealtimeMultiplexer`` is stored on
    ``request.app.state.realtime_multiplexer`` and reused across all concurrent
    SSE connections in the worker process. Bare unit-test request stubs without
    ``app`` receive an isolated ``close_pubsub_when_idle=True`` multiplexer.
    """

    app = _extract_app_from_target(request)
    state = getattr(app, "state", None) if app is not None else None
    if state is None:
        return ProcessRealtimeMultiplexer(redis, close_pubsub_when_idle=True), True

    existing = getattr(state, REALTIME_MULTIPLEXER_STATE_ATTR, None)
    if isinstance(existing, ProcessRealtimeMultiplexer) and not existing.closed:
        if existing.matches_redis(redis):
            return existing, False
        if existing.active_subscriber_count > 0:
            return ProcessRealtimeMultiplexer(redis, close_pubsub_when_idle=True), True

    mux = ProcessRealtimeMultiplexer(redis)
    setattr(state, REALTIME_MULTIPLEXER_STATE_ATTR, mux)
    return mux, False


def create_realtime_route_lifespan(
    redis_provider: Callable[[], Any],
) -> RealtimeRouteLifespan:
    """Own a process-scoped ``ProcessRealtimeMultiplexer`` on ``app.state``."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        existing = getattr(app.state, REALTIME_MULTIPLEXER_STATE_ATTR, None)
        if isinstance(existing, ProcessRealtimeMultiplexer) and not existing.closed:
            yield
            return
        mux = ProcessRealtimeMultiplexer(redis_provider)
        setattr(app.state, REALTIME_MULTIPLEXER_STATE_ATTR, mux)
        try:
            yield
        finally:
            await close_app_realtime_multiplexer(app)

    return lifespan
