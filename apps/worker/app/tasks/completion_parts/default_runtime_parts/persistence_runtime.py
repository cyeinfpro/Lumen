"""Completion persistence, retry classification, and attempt fencing."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from sqlalchemy import text as sa_text

from lumen_core.upstream_billing import decide_dispatch_evidence_billing

from ....completion_checkpoint import completion_has_trustworthy_persisted_usage


class CompletionEpochSuperseded(RuntimeError):
    """Raised when another worker has advanced the completion attempt epoch."""


@dataclass(frozen=True, slots=True)
class FlushDependencies:
    session_factory: Callable[..., Any]
    completion_model: Any
    streaming_status: str
    update: Callable[..., Any]
    affected_rows: Callable[[Any], int]
    logger: Any
    backoff_s: float
    upstream_error_type: type[BaseException]
    upstream_error_code: str


def completion_lock_key(completion_id: str) -> int:
    digest = hashlib.sha256(completion_id.encode("utf-8", errors="replace")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False) & ((1 << 63) - 1)


async def acquire_completion_xact_lock(
    session: Any,
    completion_id: str,
    *,
    logger: Any,
) -> None:
    try:
        key = completion_lock_key(completion_id)
        await session.execute(
            sa_text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=key)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("pg_advisory_xact_lock unavailable: %s", exc)


async def record_upstream_metadata(
    *,
    task_id: str,
    attempt_epoch: int,
    provider_event: dict[str, str],
    fast_mode: bool,
    session_factory: Callable[..., Any],
    completion_model: Any,
    running_statuses: tuple[str, ...],
    merge_metadata: Callable[..., dict[str, Any]],
    logger: Any,
) -> None:
    if not provider_event:
        return
    try:
        async with session_factory() as session:
            completion = await session.get(completion_model, task_id)
            if completion is None or completion.attempt != attempt_epoch:
                return
            if completion.status not in running_statuses:
                return
            completion.upstream_request = merge_metadata(
                dict(completion.upstream_request or {}),
                provider_event=provider_event,
                fast_mode=fast_mode,
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "completion upstream metadata write failed task=%s attempt=%s",
            task_id,
            attempt_epoch,
            exc_info=True,
        )


async def settle_failed_billing(
    session: Any,
    completion: Any,
    *,
    usage_values: tuple[Any, ...],
    reason: str,
    worker_billing: Any,
) -> None:
    if any(
        int(value or 0) > 0 for value in usage_values
    ) or completion_has_trustworthy_persisted_usage(completion):
        await worker_billing.charge_completion(session, completion)
        return
    decision = decide_dispatch_evidence_billing(
        completion,
        actual_cost_known=False,
    )
    if decision.released:
        await worker_billing.release_completion(session, completion, reason=reason)
        return
    await worker_billing.settle_completion_unknown_upstream(
        session,
        completion,
        reason=reason,
        knowledge=decision.knowledge.value,
    )


def classify_exception(
    exc: BaseException,
    has_partial: bool,
    *,
    upstream_error_type: type[BaseException],
    billing_error_type: type[BaseException],
    is_retriable: Callable[..., Any],
    retry_decision_type: Callable[..., Any],
) -> Any:
    if isinstance(exc, upstream_error_type):
        return is_retriable(
            exc.error_code,
            exc.status_code,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(exc, billing_error_type):
        return is_retriable(
            exc.code,
            exc.status_code,
            has_partial,
            error_message=exc.message,
        )
    if isinstance(
        exc,
        (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError),
    ):
        return is_retriable(
            "stream_interrupted" if has_partial else "upstream_error",
            None,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(exc, httpx.HTTPError):
        return is_retriable(
            "upstream_error",
            None,
            has_partial,
            error_message=str(exc),
        )
    return retry_decision_type(False, f"unhandled {type(exc).__name__}")


def bounded_next_attempt(
    current_attempt: int | None,
    *,
    max_attempts: int,
) -> tuple[int, bool]:
    next_attempt = min((current_attempt or 0) + 1, max_attempts + 1)
    return next_attempt, next_attempt <= max_attempts


async def flush_completion_text(
    task_id: str,
    text: str,
    *,
    attempt_epoch: int,
    retries: int,
    dependencies: FlushDependencies,
) -> None:
    last_exc: BaseException | None = None
    for idx in range(retries):
        try:
            async with dependencies.session_factory() as session:
                result = await session.execute(
                    dependencies.update(dependencies.completion_model)
                    .where(
                        dependencies.completion_model.id == task_id,
                        dependencies.completion_model.attempt == attempt_epoch,
                        dependencies.completion_model.status
                        == dependencies.streaming_status,
                    )
                    .values(text=text)
                )
                if dependencies.affected_rows(result) == 0:
                    raise CompletionEpochSuperseded(
                        f"completion epoch superseded task={task_id} "
                        f"attempt_epoch={attempt_epoch}"
                    )
                await session.commit()
                return
        except CompletionEpochSuperseded:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            dependencies.logger.warning(
                "completion text flush failed task=%s attempt_epoch=%s "
                "try=%d/%d err=%s",
                task_id,
                attempt_epoch,
                idx + 1,
                retries,
                exc,
            )
            if idx + 1 < retries:
                await asyncio.sleep(dependencies.backoff_s * (2**idx))
    raise dependencies.upstream_error_type(
        "completion text flush failed after retries",
        error_code=dependencies.upstream_error_code,
        status_code=None,
    ) from last_exc


async def completion_preflight_failure(
    session: Any,
    completion: Any,
    *,
    worker_billing: Any,
    max_attempts: int,
) -> tuple[int, tuple[str, str] | None]:
    window_failure = await worker_billing.completion_window_rate_limit_failure(
        session,
        completion,
    )
    if window_failure is not None:
        return int(completion.attempt or 0), window_failure
    attempt, may_run = bounded_next_attempt(
        completion.attempt,
        max_attempts=max_attempts,
    )
    if may_run:
        return attempt, None
    return (
        attempt,
        (
            "max_attempts_exceeded",
            f"completion exceeded max attempts ({max_attempts})",
        ),
    )
