"""Durable dispatch and reconciliation for host storage apply operations."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.storage_operations import StorageApplyOperation

from ..audit import write_audit
from ..db import SessionLocal, affected_rows


logger = logging.getLogger(__name__)

_RUNTIME_STATE_KEY = "_storage_apply_runtime"
_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DISPATCH_LEASE_SECONDS = 30
_REDISPATCH_AFTER_SECONDS = 60
_RECONCILE_INTERVAL_SECONDS = 5.0
_RECONCILE_BATCH_SIZE = 10
_ERROR_LIMIT = 2000

LoadConfText = Callable[[AsyncSession], Awaitable[str]]
StageOperation = Callable[[str, str], None]
ReadHostResult = Callable[[], dict[str, Any] | None]


@dataclass(frozen=True, slots=True)
class StorageDispatchClaim:
    operation_id: str
    desired_config_sha256: str
    owner: str
    fence: int


@dataclass(slots=True)
class StorageApplyRuntime:
    wakeup: asyncio.Event
    stop: asyncio.Event
    task: asyncio.Task[None] | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_ERROR_LIMIT]


def _host_timestamp(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _epoch_seconds(value: datetime | None) -> int:
    if value is None:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _operation_apply_record(operation: StorageApplyOperation) -> dict[str, Any]:
    status = {
        "succeeded": "ok",
        "failed": "fail",
    }.get(operation.status, "pending")
    if operation.status == "pending":
        message = "配置请求已持久化，等待 host 调度。"
    elif operation.status == "dispatched":
        message = "host 已领取配置，正在执行存储切换。"
    else:
        message = operation.result_message or operation.last_error or ""
    return {
        "call_id": operation.id,
        "status": status,
        "message": message,
        "started_at": operation.host_started_at
        or _epoch_seconds(operation.dispatched_at)
        or _epoch_seconds(operation.created_at),
        "finished_at": operation.host_finished_at
        or _epoch_seconds(operation.completed_at),
        "operation_status": operation.status,
        "desired_config_sha256": operation.desired_config_sha256,
        "dispatch_attempts": operation.dispatch_attempts,
        "last_error": operation.last_error,
    }


async def latest_storage_apply_record(
    session: AsyncSession,
    *,
    legacy_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    operation = (
        await session.execute(
            select(StorageApplyOperation)
            .order_by(
                StorageApplyOperation.created_at.desc(),
                StorageApplyOperation.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if operation is None:
        return legacy_result
    return _operation_apply_record(operation)


async def _ingest_host_result(result: dict[str, Any]) -> bool:
    operation_id = str(result.get("call_id") or "").strip()
    host_status = str(result.get("status") or "").strip()
    if not _OPERATION_ID_RE.fullmatch(operation_id) or host_status not in {
        "ok",
        "fail",
    }:
        logger.error("storage apply host result is invalid: %r", result)
        return False

    async with SessionLocal() as session:
        operation = (
            await session.execute(
                select(StorageApplyOperation)
                .where(StorageApplyOperation.id == operation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is None:
            logger.error(
                "storage apply host result references unknown operation_id=%s",
                operation_id,
            )
            return False
        terminal_status = "succeeded" if host_status == "ok" else "failed"
        if operation.status in {"succeeded", "failed"}:
            if operation.status != terminal_status:
                logger.error(
                    "storage apply terminal result conflicts operation_id=%s "
                    "db_status=%s host_status=%s",
                    operation_id,
                    operation.status,
                    host_status,
                )
            return False

        message = str(result.get("message") or "")[:_ERROR_LIMIT]
        operation.status = terminal_status
        operation.active_slot = None
        operation.dispatch_owner = None
        operation.dispatch_lease_until = None
        operation.completed_at = _now()
        operation.result_message = message
        operation.host_started_at = _host_timestamp(result.get("started_at"))
        operation.host_finished_at = _host_timestamp(result.get("finished_at"))
        operation.last_error = message if terminal_status == "failed" else None
        await write_audit(
            session,
            event_type=f"admin.storage.apply.{terminal_status}",
            user_id=operation.requested_by,
            details={
                "operation_id": operation.id,
                "host_status": host_status,
                "message": message,
            },
            autocommit=False,
        )
        await session.commit()
        return True


async def _due_operation_ids() -> list[str]:
    now = _now()
    retry_before = now - timedelta(seconds=_REDISPATCH_AFTER_SECONDS)
    rows = (
        await _execute_select(
            select(StorageApplyOperation.id)
            .where(
                StorageApplyOperation.active_slot == 1,
                or_(
                    StorageApplyOperation.status == "pending",
                    and_(
                        StorageApplyOperation.status == "dispatched",
                        or_(
                            StorageApplyOperation.dispatched_at.is_(None),
                            StorageApplyOperation.dispatched_at <= retry_before,
                        ),
                    ),
                ),
                or_(
                    StorageApplyOperation.dispatch_lease_until.is_(None),
                    StorageApplyOperation.dispatch_lease_until <= now,
                ),
            )
            .order_by(
                StorageApplyOperation.created_at.asc(),
                StorageApplyOperation.id.asc(),
            )
            .limit(_RECONCILE_BATCH_SIZE)
        )
    ).scalars()
    return list(rows)


async def _execute_select(statement: Any) -> Any:
    async with SessionLocal() as session:
        return await session.execute(statement)


async def _claim_operation(operation_id: str) -> StorageDispatchClaim | None:
    owner = f"api:{os.getpid()}:{uuid.uuid4().hex}"
    now = _now()
    retry_before = now - timedelta(seconds=_REDISPATCH_AFTER_SECONDS)
    async with SessionLocal() as session:
        row = (
            await session.execute(
                update(StorageApplyOperation)
                .where(
                    StorageApplyOperation.id == operation_id,
                    StorageApplyOperation.active_slot == 1,
                    or_(
                        StorageApplyOperation.status == "pending",
                        and_(
                            StorageApplyOperation.status == "dispatched",
                            or_(
                                StorageApplyOperation.dispatched_at.is_(None),
                                StorageApplyOperation.dispatched_at <= retry_before,
                            ),
                        ),
                    ),
                    or_(
                        StorageApplyOperation.dispatch_lease_until.is_(None),
                        StorageApplyOperation.dispatch_lease_until <= now,
                    ),
                )
                .values(
                    dispatch_owner=owner,
                    dispatch_lease_until=now
                    + timedelta(seconds=_DISPATCH_LEASE_SECONDS),
                    dispatch_fence=StorageApplyOperation.dispatch_fence + 1,
                    dispatch_attempts=StorageApplyOperation.dispatch_attempts + 1,
                    last_error=None,
                )
                .returning(
                    StorageApplyOperation.desired_config_sha256,
                    StorageApplyOperation.dispatch_fence,
                )
            )
        ).one_or_none()
        await session.commit()
    if row is None:
        return None
    return StorageDispatchClaim(
        operation_id=operation_id,
        desired_config_sha256=str(row[0]),
        owner=owner,
        fence=int(row[1]),
    )


async def _record_dispatch_failure(
    claim: StorageDispatchClaim,
    error: str,
) -> None:
    async with SessionLocal() as session:
        result = await session.execute(
            update(StorageApplyOperation)
            .where(
                StorageApplyOperation.id == claim.operation_id,
                StorageApplyOperation.active_slot == 1,
                StorageApplyOperation.dispatch_owner == claim.owner,
                StorageApplyOperation.dispatch_fence == claim.fence,
            )
            .values(
                dispatch_owner=None,
                dispatch_lease_until=None,
                last_error=error[:_ERROR_LIMIT],
            )
        )
        if affected_rows(result) != 1:
            await session.rollback()
            raise RuntimeError("storage dispatch fence was lost after failure")
        await session.commit()


async def _mark_digest_mismatch(claim: StorageDispatchClaim) -> None:
    async with SessionLocal() as session:
        operation = (
            await session.execute(
                select(StorageApplyOperation)
                .where(
                    StorageApplyOperation.id == claim.operation_id,
                    StorageApplyOperation.active_slot == 1,
                    StorageApplyOperation.dispatch_owner == claim.owner,
                    StorageApplyOperation.dispatch_fence == claim.fence,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is None:
            raise RuntimeError("storage dispatch fence was lost before digest failure")
        operation.status = "failed"
        operation.active_slot = None
        operation.completed_at = _now()
        operation.result_message = "desired_config_hash_mismatch"
        operation.last_error = "desired_config_hash_mismatch"
        operation.dispatch_owner = None
        operation.dispatch_lease_until = None
        await write_audit(
            session,
            event_type="admin.storage.apply.failed",
            user_id=operation.requested_by,
            details={
                "operation_id": operation.id,
                "reason": "desired_config_hash_mismatch",
            },
            autocommit=False,
        )
        await session.commit()


async def _mark_dispatched(claim: StorageDispatchClaim) -> None:
    async with SessionLocal() as session:
        result = await session.execute(
            update(StorageApplyOperation)
            .where(
                StorageApplyOperation.id == claim.operation_id,
                StorageApplyOperation.active_slot == 1,
                StorageApplyOperation.dispatch_owner == claim.owner,
                StorageApplyOperation.dispatch_fence == claim.fence,
            )
            .values(
                status="dispatched",
                dispatched_at=_now(),
                dispatch_owner=None,
                dispatch_lease_until=None,
                last_error=None,
            )
        )
        if affected_rows(result) != 1:
            await session.rollback()
            raise RuntimeError("storage dispatch fence was lost")
        await session.commit()


async def dispatch_storage_apply_operation(
    operation_id: str,
    *,
    load_conf_text: LoadConfText,
    stage_operation: StageOperation,
) -> bool:
    claim = await _claim_operation(operation_id)
    if claim is None:
        return False

    try:
        async with SessionLocal() as session:
            conf_text = await load_conf_text(session)
        digest = hashlib.sha256(conf_text.encode("utf-8")).hexdigest()
        if digest != claim.desired_config_sha256:
            await _mark_digest_mismatch(claim)
            logger.error(
                "storage desired config digest changed operation_id=%s",
                operation_id,
            )
            return False
        await asyncio.to_thread(stage_operation, operation_id, conf_text)
        await _mark_dispatched(claim)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            await _record_dispatch_failure(claim, _error_text(exc))
        except Exception:
            logger.exception(
                "storage dispatch failure state could not be persisted "
                "operation_id=%s",
                operation_id,
            )
        raise


async def run_storage_apply_reconciler_once(
    *,
    load_conf_text: LoadConfText,
    stage_operation: StageOperation,
    read_host_result: ReadHostResult,
) -> int:
    reconciled = 0
    result = read_host_result()
    if result is not None and await _ingest_host_result(result):
        reconciled += 1
    for operation_id in await _due_operation_ids():
        try:
            dispatched = await dispatch_storage_apply_operation(
                operation_id,
                load_conf_text=load_conf_text,
                stage_operation=stage_operation,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "storage apply dispatch failed operation_id=%s",
                operation_id,
            )
            continue
        reconciled += int(dispatched)
    return reconciled


async def storage_apply_reconciler_loop(
    runtime: StorageApplyRuntime,
    *,
    load_conf_text: LoadConfText,
    stage_operation: StageOperation,
    read_host_result: ReadHostResult,
    interval_seconds: float = _RECONCILE_INTERVAL_SECONDS,
) -> None:
    while not runtime.stop.is_set():
        runtime.wakeup.clear()
        try:
            await run_storage_apply_reconciler_once(
                load_conf_text=load_conf_text,
                stage_operation=stage_operation,
                read_host_result=read_host_result,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("storage apply reconciliation iteration failed")
        if runtime.stop.is_set() or runtime.wakeup.is_set():
            continue
        try:
            await asyncio.wait_for(runtime.wakeup.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def create_storage_apply_lifespan(
    *,
    load_conf_text: LoadConfText,
    stage_operation: StageOperation,
    read_host_result: ReadHostResult,
) -> Callable[[FastAPI], AsyncIterator[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        wakeup = asyncio.Event()
        stop = asyncio.Event()
        runtime = StorageApplyRuntime(
            wakeup=wakeup,
            stop=stop,
        )
        runtime.task = asyncio.create_task(
            storage_apply_reconciler_loop(
                runtime,
                load_conf_text=load_conf_text,
                stage_operation=stage_operation,
                read_host_result=read_host_result,
            ),
            name="storage-apply-reconciler",
        )
        setattr(app.state, _RUNTIME_STATE_KEY, runtime)
        try:
            yield
        finally:
            runtime.stop.set()
            runtime.wakeup.set()
            task = runtime.task
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if getattr(app.state, _RUNTIME_STATE_KEY, None) is runtime:
                delattr(app.state, _RUNTIME_STATE_KEY)

    return lifespan


def wake_storage_apply_reconciler(request: Request) -> bool:
    runtime = getattr(request.app.state, _RUNTIME_STATE_KEY, None)
    if not isinstance(runtime, StorageApplyRuntime):
        logger.error(
            "storage operation persisted but reconciler runtime is unavailable"
        )
        return False
    runtime.wakeup.set()
    return True


__all__ = [
    "create_storage_apply_lifespan",
    "dispatch_storage_apply_operation",
    "latest_storage_apply_record",
    "run_storage_apply_reconciler_once",
    "storage_apply_reconciler_loop",
    "wake_storage_apply_reconciler",
]
