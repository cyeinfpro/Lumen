"""Response, snapshot, and durable-producer helpers for prompt enhancement."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...task_billing import EnhanceBillingContext, EnhanceUsageCapture
from .upstream import has_nonempty_text, terminal_chunk_kind

if TYPE_CHECKING:
    from . import idempotency


_QUEUE_DONE = object()
_HEARTBEAT_SECONDS = 10.0
_TERMINAL_PERSIST_ATTEMPTS = 3
TERMINAL_PERSIST_UNKNOWN_CODE = "idempotency_terminal_persist_unknown"


class TerminalPersistenceUnknown(RuntimeError):
    """The terminal outcome may exist, but could not be confirmed durably."""


def _idempotency_module() -> Any:
    from . import idempotency

    return idempotency


def replay_stream(chunks: tuple[str, ...]) -> AsyncIterator[str]:
    async def iterate() -> AsyncIterator[str]:
        for chunk in chunks:
            yield chunk

    return iterate()


async def keepalive_stream(
    source: AsyncIterator[str], **kwargs: Any
) -> AsyncIterator[str]:
    implementation = kwargs.pop("implementation")
    async for chunk in implementation(source, **kwargs):
        yield chunk


def billing_snapshot(
    billing: EnhanceBillingContext | None,
    *,
    request_id: str,
) -> dict[str, Any]:
    if billing is None:
        return {"version": 1, "mode": "none", "request_id": request_id}
    if billing.request_id != request_id:
        raise ValueError("prompt enhancement billing request id changed")
    return {
        "version": 1,
        "mode": "wallet",
        "request_id": billing.request_id,
        "user_id": billing.user_id,
        "rate_multiplier_x10000": billing.rate_multiplier_x10000,
        "cache_aware": billing.cache_aware,
        "allow_negative": billing.allow_negative,
        "hold_amount_micro": billing.hold_amount_micro,
        "pricing_snapshots": dict(billing.pricing_snapshots),
    }


def billing_from_snapshot(
    db: AsyncSession,
    user: Any,
    snapshot: dict[str, Any],
) -> EnhanceBillingContext | None:
    if snapshot.get("version") != 1:
        raise ValueError("unsupported prompt enhancement billing snapshot")
    mode = snapshot.get("mode")
    request_id = snapshot.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("prompt enhancement billing snapshot has no request id")
    if mode == "none":
        return None
    if mode != "wallet" or snapshot.get("user_id") != user.id:
        raise ValueError("prompt enhancement billing snapshot identity changed")
    pricing_snapshots = snapshot.get("pricing_snapshots")
    rate_multiplier = snapshot.get("rate_multiplier_x10000")
    hold_amount = snapshot.get("hold_amount_micro")
    cache_aware = snapshot.get("cache_aware")
    allow_negative = snapshot.get("allow_negative")
    if (
        not isinstance(pricing_snapshots, dict)
        or not isinstance(rate_multiplier, int)
        or rate_multiplier < 0
        or not isinstance(hold_amount, int)
        or hold_amount < 0
        or not isinstance(cache_aware, bool)
        or not isinstance(allow_negative, bool)
    ):
        raise ValueError("prompt enhancement billing snapshot is invalid")
    return EnhanceBillingContext(
        db=db,
        user_id=user.id,
        user_email=getattr(user, "email", None),
        request_id=request_id,
        rate_multiplier_x10000=rate_multiplier,
        cache_aware=cache_aware,
        allow_negative=allow_negative,
        hold_amount_micro=hold_amount,
        pricing_snapshots=dict(pricing_snapshots),
    )


def usage_capture_snapshot(capture: EnhanceUsageCapture) -> dict[str, Any]:
    return {
        "provider_name": capture.provider_name,
        "model": capture.model,
        "service_tier": capture.service_tier,
        "pricing_snapshot_key": capture.pricing_snapshot_key,
        "response_id": capture.response_id,
        "usage": dict(capture.usage) if isinstance(capture.usage, dict) else None,
    }


def usage_capture_from_snapshot(
    snapshot: dict[str, Any] | None,
) -> EnhanceUsageCapture:
    if not isinstance(snapshot, dict):
        raise ValueError("prompt enhancement usage checkpoint is unavailable")
    usage = snapshot.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ValueError("prompt enhancement usage checkpoint is invalid")
    return EnhanceUsageCapture(
        provider_name=(
            snapshot["provider_name"]
            if isinstance(snapshot.get("provider_name"), str)
            else None
        ),
        model=snapshot["model"] if isinstance(snapshot.get("model"), str) else None,
        service_tier=(
            snapshot["service_tier"]
            if isinstance(snapshot.get("service_tier"), str)
            else "standard"
        ),
        pricing_snapshot_key=(
            snapshot["pricing_snapshot_key"]
            if isinstance(snapshot.get("pricing_snapshot_key"), str)
            else None
        ),
        response_id=(
            snapshot["response_id"]
            if isinstance(snapshot.get("response_id"), str)
            else None
        ),
        usage=dict(usage) if isinstance(usage, dict) else None,
    )


def response_headers(idempotency_key: str) -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Idempotency-Key": idempotency_key,
    }


def replay_response(
    reservation: idempotency.PromptEnhanceReservation,
    *,
    idempotency_key: str,
    with_keepalive: Callable[[AsyncIterator[str]], AsyncIterator[str]],
) -> StreamingResponse:
    chunks = reservation.replay_chunks
    if chunks is None:
        raise RuntimeError("prompt enhancement replay requested without chunks")
    return StreamingResponse(
        with_keepalive(replay_stream(chunks)),
        media_type="text/event-stream",
        headers=response_headers(idempotency_key),
    )


@dataclass(frozen=True, slots=True)
class _DurableStreamContext:
    operation: idempotency.PromptEnhanceOperation
    attempt: idempotency.PromptEnhanceAttempt | None
    recovery: idempotency.PromptEnhanceRecovery | None
    session_factory: Callable[..., Any]
    source_factory: Callable[[AsyncSession], AsyncIterator[str]]
    recovery_handler: (
        Callable[[AsyncSession, idempotency.PromptEnhanceRecovery], Awaitable[None]]
        | None
    )
    logger: Any
    queue: asyncio.Queue[str | object]
    heartbeat_interval_seconds: float


async def _persist_terminal_with_retry(
    db: AsyncSession,
    context: _DurableStreamContext,
    chunks: list[str],
    terminal_state: str,
) -> None:
    idem = _idempotency_module()
    last_error: Exception | None = None
    for retry in range(_TERMINAL_PERSIST_ATTEMPTS):
        try:
            kwargs: dict[str, Any] = {
                "chunks": chunks,
                "terminal_state": terminal_state,
            }
            if context.attempt is not None:
                kwargs["attempt"] = context.attempt
            await idem.persist_terminal_response(db, context.operation, **kwargs)
            return
        except idem.AttemptOwnershipLost:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await db.rollback()
            if retry < _TERMINAL_PERSIST_ATTEMPTS - 1:
                await asyncio.sleep(0.05 * (2**retry))
    assert last_error is not None
    raise TerminalPersistenceUnknown(
        "prompt enhancement terminal persistence outcome is unknown"
    ) from last_error


async def _close_durable_source(
    source: AsyncIterator[str],
    context: _DurableStreamContext,
) -> None:
    try:
        await source.aclose()
    except Exception:  # noqa: BLE001
        context.logger.exception(
            "prompt enhancement durable source close failed record_id=%s",
            context.operation.record_id,
        )


async def _checkpoint_text_chunk(
    db: AsyncSession,
    context: _DurableStreamContext,
    chunks: list[str],
    chunk: str,
) -> None:
    if context.attempt is None:
        return
    await _idempotency_module().checkpoint_response_chunk(
        db,
        context.operation,
        context.attempt,
        sequence=len(chunks),
        chunk=chunk,
    )


async def _produce_new_from_session(
    db: AsyncSession,
    context: _DurableStreamContext,
) -> None:
    chunks: list[str] = []
    source = context.source_factory(db)
    try:
        async for source_chunk in source:
            chunk = source_chunk
            terminal_state = terminal_chunk_kind(chunk)
            if terminal_state == "succeeded" and not has_nonempty_text(chunks):
                chunk = 'data: {"error": "empty_response"}\n\n'
                terminal_state = "failed"
                if context.attempt is not None:
                    await _idempotency_module().checkpoint_finalization(
                        db,
                        context.operation,
                        context.attempt,
                        terminal_state="failed",
                        terminal_chunk=chunk,
                        billing_action="none",
                        reason="empty_response",
                    )
            if terminal_state is None:
                await _checkpoint_text_chunk(db, context, chunks, chunk)
                chunks.append(chunk)
                await context.queue.put(chunk)
                continue
            chunks.append(chunk)
            await _persist_terminal_with_retry(db, context, chunks, terminal_state)
            await context.queue.put(chunk)
            return
        raise RuntimeError("prompt enhancement stream ended without a terminal event")
    finally:
        await _close_durable_source(source, context)


async def _recover_from_session(
    db: AsyncSession,
    context: _DurableStreamContext,
) -> None:
    recovery = context.recovery
    if recovery is None:
        raise RuntimeError("prompt enhancement recovery is unavailable")
    if context.recovery_handler is not None:
        await context.recovery_handler(db, recovery)
    elif recovery.billing_action not in {"none", "preserve_hold"}:
        raise RuntimeError("prompt enhancement billing recovery handler is unavailable")
    chunks = [*recovery.response_chunks, recovery.terminal_chunk]
    await _persist_terminal_with_retry(db, context, chunks, recovery.terminal_state)
    for chunk in chunks:
        await context.queue.put(chunk)


async def _produce_from_session(
    db: AsyncSession,
    context: _DurableStreamContext,
) -> None:
    if context.recovery is not None:
        await _recover_from_session(db, context)
        return
    await _produce_new_from_session(db, context)


async def _heartbeat_attempt(
    context: _DurableStreamContext,
    stop: asyncio.Event,
) -> None:
    if context.attempt is None:
        return
    while True:
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=context.heartbeat_interval_seconds,
            )
            return
        except TimeoutError:
            async with context.session_factory() as db:
                await _idempotency_module().renew_attempt_lease(
                    db,
                    context.operation,
                    context.attempt,
                )


async def _run_durable_producer(context: _DurableStreamContext) -> None:
    async def produce() -> None:
        async with context.session_factory() as db:
            await _produce_from_session(db, context)

    if context.attempt is None or context.heartbeat_interval_seconds <= 0:
        await produce()
        return
    stop = asyncio.Event()
    producer = asyncio.create_task(produce())
    heartbeat = asyncio.create_task(_heartbeat_attempt(context, stop))
    try:
        done, _pending = await asyncio.wait(
            {producer, heartbeat},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if producer in done:
            await producer
        else:
            await heartbeat
            raise RuntimeError("prompt enhancement heartbeat stopped unexpectedly")
    finally:
        stop.set()
        for task in (producer, heartbeat):
            if not task.done():
                task.cancel()
        await asyncio.gather(producer, heartbeat, return_exceptions=True)


async def _produce_durable_stream(context: _DurableStreamContext) -> None:
    idem = _idempotency_module()
    try:
        await _run_durable_producer(context)
    except asyncio.CancelledError:
        raise
    except idem.AttemptOwnershipLost:
        context.logger.warning(
            "prompt enhancement stale producer fenced record_id=%s attempt=%s",
            context.operation.record_id,
            context.attempt.number if context.attempt is not None else None,
        )
    except TerminalPersistenceUnknown:
        context.logger.exception(
            "prompt enhancement terminal persistence unknown record_id=%s",
            context.operation.record_id,
        )
        await context.queue.put(
            f'data: {{"error": "{TERMINAL_PERSIST_UNKNOWN_CODE}"}}\n\n'
        )
    except Exception:  # noqa: BLE001
        context.logger.exception(
            "prompt enhancement durable producer failed record_id=%s",
            context.operation.record_id,
        )
        await context.queue.put('data: {"error": "internal"}\n\n')
    finally:
        await context.queue.put(_QUEUE_DONE)


async def _consume_durable_stream(
    queue: asyncio.Queue[str | object],
) -> AsyncIterator[str]:
    while True:
        item = await queue.get()
        if item is _QUEUE_DONE:
            return
        if isinstance(item, str):
            yield item


def durable_stream(
    *,
    operation: idempotency.PromptEnhanceOperation,
    session_factory: Callable[..., Any],
    source_factory: Callable[[AsyncSession], AsyncIterator[str]],
    logger: Any,
    attempt: idempotency.PromptEnhanceAttempt | None = None,
    recovery: idempotency.PromptEnhanceRecovery | None = None,
    recovery_handler: (
        Callable[[AsyncSession, idempotency.PromptEnhanceRecovery], Awaitable[None]]
        | None
    ) = None,
    heartbeat_interval_seconds: float | None = None,
) -> tuple[AsyncIterator[str], asyncio.Task[None]]:
    queue: asyncio.Queue[str | object] = asyncio.Queue()
    context = _DurableStreamContext(
        operation=operation,
        attempt=attempt,
        recovery=recovery,
        session_factory=session_factory,
        source_factory=source_factory,
        recovery_handler=recovery_handler,
        logger=logger,
        queue=queue,
        heartbeat_interval_seconds=(
            _HEARTBEAT_SECONDS
            if heartbeat_interval_seconds is None
            else heartbeat_interval_seconds
        ),
    )
    task = asyncio.create_task(_produce_durable_stream(context))
    return _consume_durable_stream(queue), task


@dataclass(frozen=True, slots=True)
class PromptDurabilityRuntime:
    session_factory: Callable[..., Any]
    logger: Any
    prepare_billing: Callable[..., Awaitable[EnhanceBillingContext | None]]
    charge: Callable[[EnhanceBillingContext, EnhanceUsageCapture], Awaitable[bool]]
    settle_default: Callable[..., Awaitable[bool]]
    release: Callable[..., Awaitable[bool]]
    stream_enhance: Callable[..., AsyncIterator[str]]
    track_operation_task: Callable[[asyncio.Task[None]], None]


def required_operation_attempt(
    reservation: idempotency.PromptEnhanceReservation,
) -> idempotency.PromptEnhanceAttempt:
    attempt = reservation.attempt
    if attempt is None:
        raise RuntimeError("prompt enhancement reservation has no attempt owner")
    return attempt


async def prepare_reserved_billing(
    db: AsyncSession,
    user: Any,
    operation: idempotency.PromptEnhanceOperation,
    reservation: idempotency.PromptEnhanceReservation,
    *,
    runtime: PromptDurabilityRuntime,
) -> tuple[EnhanceBillingContext | None, bool]:
    snapshot = reservation.billing_snapshot
    if snapshot is not None:
        return billing_from_snapshot(db, user, snapshot), False
    attempt = required_operation_attempt(reservation)
    billing = await runtime.prepare_billing(
        db,
        user,
        request_id=attempt.billing_request_id,
        commit=False,
    )
    await _idempotency_module().bind_billing_snapshot(
        db,
        operation,
        attempt,
        billing_snapshot(billing, request_id=attempt.billing_request_id),
    )
    return billing, billing is not None and billing.hold_amount_micro > 0


async def _recover_checkpointed_billing(
    db: AsyncSession,
    operation: idempotency.PromptEnhanceOperation,
    attempt: idempotency.PromptEnhanceAttempt,
    recovery: idempotency.PromptEnhanceRecovery,
    billing: EnhanceBillingContext | None,
    *,
    runtime: PromptDurabilityRuntime,
) -> None:
    await _idempotency_module().assert_attempt_owner(db, operation, attempt)
    action = recovery.billing_action
    result: bool | None = True
    if action == "charge":
        if billing is None:
            raise RuntimeError("prompt enhancement charge recovery has no billing")
        result = await runtime.charge(
            billing,
            usage_capture_from_snapshot(recovery.billing_capture),
        )
    elif action == "settle_default":
        result = await runtime.settle_default(
            billing,
            reason=recovery.reason or "stale_attempt_recovery",
        )
    elif action == "release":
        result = await runtime.release(
            billing,
            reason=recovery.reason or "stale_attempt_recovery",
        )
    elif action not in {"none", "preserve_hold"}:
        raise RuntimeError("prompt enhancement recovery billing action is invalid")
    if result is False:
        raise RuntimeError(f"prompt enhancement recovery billing failed: {action}")


def durable_prompt_stream(
    operation: idempotency.PromptEnhanceOperation,
    reservation: idempotency.PromptEnhanceReservation,
    *,
    text: str,
    providers: list[Any],
    billing: EnhanceBillingContext | None,
    prompt_runtime: Any,
    runtime: PromptDurabilityRuntime,
    system_prompt: str,
    content: list[dict[str, Any]] | None = None,
) -> tuple[AsyncIterator[str], asyncio.Task[None]]:
    attempt = required_operation_attempt(reservation)

    def source_factory(stream_db: AsyncSession) -> AsyncIterator[str]:
        detached = replace(billing, db=stream_db) if billing is not None else None

        async def dispatch_intent() -> None:
            try:
                await _idempotency_module().record_dispatch_intent(
                    stream_db, operation, attempt
                )
            except BaseException:
                await stream_db.rollback()
                raise

        async def candidate_outcome(upstream_cost_possible: bool) -> None:
            await _idempotency_module().record_candidate_outcome(
                stream_db,
                operation,
                attempt,
                upstream_cost_possible=upstream_cost_possible,
            )

        async def finalization_checkpoint(
            *,
            terminal_state: str,
            terminal_chunk: str,
            billing_action: str,
            capture: EnhanceUsageCapture | None = None,
            reason: str | None = None,
        ) -> None:
            await _idempotency_module().checkpoint_finalization(
                stream_db,
                operation,
                attempt,
                terminal_state=terminal_state,
                terminal_chunk=terminal_chunk,
                billing_action=billing_action,
                billing_capture=(
                    usage_capture_snapshot(capture) if capture is not None else None
                ),
                reason=reason,
            )

        return runtime.stream_enhance(
            text,
            providers,
            detached,
            runtime=prompt_runtime,
            system_prompt=system_prompt,
            content=content,
            record_dispatch_intent=dispatch_intent,
            record_candidate_outcome=candidate_outcome,
            checkpoint_finalization=finalization_checkpoint,
            require_billing_confirmation=True,
        )

    async def recovery_handler(
        stream_db: AsyncSession,
        recovery: idempotency.PromptEnhanceRecovery,
    ) -> None:
        detached = replace(billing, db=stream_db) if billing is not None else None
        await _recover_checkpointed_billing(
            stream_db,
            operation,
            attempt,
            recovery,
            detached,
            runtime=runtime,
        )

    source, task = durable_stream(
        operation=operation,
        attempt=attempt,
        recovery=reservation.recovery,
        session_factory=runtime.session_factory,
        source_factory=source_factory,
        recovery_handler=recovery_handler,
        logger=runtime.logger,
    )
    runtime.track_operation_task(task)
    return source, task


def durable_prompt_response(
    operation: idempotency.PromptEnhanceOperation,
    reservation: idempotency.PromptEnhanceReservation,
    *,
    text: str,
    providers: list[Any],
    billing: EnhanceBillingContext | None,
    prompt_runtime: Any,
    runtime: PromptDurabilityRuntime,
    system_prompt: str,
    content: list[dict[str, Any]] | None,
    with_keepalive: Callable[[AsyncIterator[str]], AsyncIterator[str]],
) -> StreamingResponse:
    source, _task = durable_prompt_stream(
        operation,
        reservation,
        text=text,
        providers=providers,
        billing=billing,
        prompt_runtime=prompt_runtime,
        runtime=runtime,
        system_prompt=system_prompt,
        content=content,
    )
    return StreamingResponse(
        with_keepalive(source),
        media_type="text/event-stream",
        headers=response_headers(operation.idempotency_key),
    )


async def release_hold_detached(
    billing: EnhanceBillingContext | None,
    *,
    reason: str,
    session_factory: Callable[..., Any],
    release: Callable[..., Awaitable[bool]],
) -> None:
    if billing is None or billing.hold_amount_micro <= 0:
        return
    async with session_factory() as db:
        await release(replace(billing, db=db), reason=reason)


def schedule_hold_release(
    billing: EnhanceBillingContext | None,
    *,
    reason: str,
    release_detached: Callable[..., Awaitable[None]],
    track_task: Callable[[asyncio.Task[None]], None],
) -> asyncio.Task[None] | None:
    if billing is None or billing.hold_amount_micro <= 0:
        return None
    task = asyncio.create_task(release_detached(billing, reason=reason))
    track_task(task)
    return task


async def wait_for_hold_release(
    billing: EnhanceBillingContext | None,
    *,
    reason: str,
    schedule_release: Callable[..., asyncio.Task[None] | None],
    logger: Any,
) -> None:
    task = schedule_release(billing, reason=reason)
    if task is None:
        return
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        logger.info(
            "prompt enhance hold release continues after stream cancellation "
            "request_id=%s reason=%s",
            billing.request_id if billing is not None else None,
            reason,
        )
        raise


def orphan_hold_release_callback(
    billing: EnhanceBillingContext | None,
    *,
    logger: Any,
    schedule_release: Callable[..., Any],
) -> Callable[[], None]:
    def release_orphan_hold() -> None:
        if billing is not None and billing.settle_outcome.attempted:
            logger.warning(
                "prompt enhance orphan release skipped settle_attempted "
                "request_id=%s hold_micro=%d",
                billing.request_id,
                billing.hold_amount_micro,
            )
            return
        schedule_release(billing, reason="stream_orphaned")

    return release_orphan_hold


class TrackedStreamIterator:
    """Track whether a streaming body reached normal exhaustion."""

    def __init__(self, source: AsyncIterator[str]) -> None:
        self._source = source
        self.exhausted = False

    def __aiter__(self) -> TrackedStreamIterator:
        return self

    async def __anext__(self) -> str:
        try:
            return await self._source.__anext__()
        except StopAsyncIteration:
            self.exhausted = True
            raise

    async def aclose(self) -> None:
        await self._source.aclose()


class GuardedEnhanceStreamingResponse(StreamingResponse):
    """Run a teardown callback when the body does not exhaust normally."""

    def __init__(
        self,
        content: AsyncIterator[str],
        *,
        on_teardown: Callable[[], None],
        **kwargs: Any,
    ) -> None:
        self._body = TrackedStreamIterator(content)
        super().__init__(self._body, **kwargs)
        self._on_teardown = on_teardown

    async def stream_response(self, send: Any) -> None:
        try:
            await super().stream_response(send)
        finally:
            if not self._body.exhausted:
                self._on_teardown()
