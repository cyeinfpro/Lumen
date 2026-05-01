"""Pure task state helpers shared by Worker tasks.

GEN-P0-10 合同（全部 mark_task_* 函数均遵守）：
- 若调用方传入 `session`：只在该 session 上改字段并 commit。调用方若已在事务边界内，
  session.commit() 会提交该事务；若已 begin() 的 savepoint，commit 会 release 保存点。
- 若调用方未传 session：函数内部自建独立 async session（`async with SessionLocal()
  as s, s.begin():`），重新 get(type, id) 再写并 commit。
- 纯内存对象（没挂到 ORM 的 SimpleNamespace / mock）依然只就地改字段，保持 unit test 兼容。

提交是这几个函数的契约。返回前如果没 commit，调用方没有后续责任——由此前的
`mark_task_failed` 未 commit 导致"任务永久 pending" 的 bug 必须根除。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

from lumen_core.constants import CompletionStatus, GenerationStatus

from ..db import SessionLocal

logger = logging.getLogger(__name__)


def is_generation_terminal(status: str | None) -> bool:
    return status in {
        GenerationStatus.SUCCEEDED.value,
        GenerationStatus.FAILED.value,
        GenerationStatus.CANCELED.value,
    }


def is_completion_terminal(status: str | None) -> bool:
    return status in {
        CompletionStatus.SUCCEEDED.value,
        CompletionStatus.FAILED.value,
        CompletionStatus.CANCELED.value,
    }


# ---------------------------------------------------------------------------
# Field appliers — 就地改字段，不触碰 session
# ---------------------------------------------------------------------------


def _apply_failed_fields(task: Any, *, error_code: str, error_message: str) -> None:
    task.status = "failed"
    task.error_code = error_code
    task.error_message = error_message
    task.finished_at = datetime.now(timezone.utc)


def _apply_succeeded_fields(task: Any) -> None:
    task.status = "succeeded"
    task.error_code = None
    task.error_message = None
    task.finished_at = datetime.now(timezone.utc)


def _apply_cancelled_fields(task: Any, *, error_code: str, error_message: str) -> None:
    task.status = "canceled"
    task.error_code = error_code
    task.error_message = error_message
    task.finished_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Shared commit wrapper — 统一 commit/rollback 语义
# ---------------------------------------------------------------------------


async def _commit_or_rollback(session: Any) -> None:
    try:
        await session.commit()
    except Exception:
        rollback = getattr(session, "rollback", None)
        if rollback is not None:
            try:
                await rollback()
            except Exception:  # noqa: BLE001
                logger.exception("mark_task_*: rollback failed")
        raise


async def _run_self_managed(
    task: Any,
    apply_fn: Callable[[Any], None],
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None,
) -> None:
    """GEN-P0-10: 没有外部 session 时，独立事务 get→apply→commit。"""
    task_id = getattr(task, "id", None)
    task_type = type(task)
    if task_id is None or not hasattr(task_type, "__table__"):
        # 纯 SimpleNamespace / mock 单测路径：只就地改字段不落库，维持向后兼容。
        return

    factory = session_factory or SessionLocal
    async with factory() as db_session:
        try:
            db_task = await db_session.get(task_type, task_id)
            if db_task is not None:
                apply_fn(db_task)
            await db_session.commit()
        except Exception:
            rollback = getattr(db_session, "rollback", None)
            if rollback is not None:
                try:
                    await rollback()
                except Exception:  # noqa: BLE001
                    logger.exception("mark_task_*: self-managed rollback failed")
            raise


# ---------------------------------------------------------------------------
# Public API — GEN-P0-10 契约：返回前必 commit（除非纯内存对象）
# ---------------------------------------------------------------------------


async def mark_task_failed(
    task: Any,
    *,
    error_code: str,
    error_message: str,
    session: Any | None = None,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
) -> None:
    """Mark a task failed. **Commit is part of the contract.**

    GEN-P0-10: 之前该函数只改内存对象不 commit，导致失败状态永远写不到 PG，
    任务永久 pending。重写保证：
    - 传入 session：写完 commit。调用方若已 begin()，commit 会把 outer tx flush。
    - 不传 session：自建 `async with SessionLocal()` 独立事务。
    - 纯内存对象（没有 __table__）：只改字段，兼容原单测。
    """
    _apply_failed_fields(task, error_code=error_code, error_message=error_message)

    if session is not None:
        await _commit_or_rollback(session)
        return

    await _run_self_managed(
        task,
        lambda t: _apply_failed_fields(t, error_code=error_code, error_message=error_message),
        session_factory=session_factory,
    )


async def mark_task_succeeded(
    task: Any,
    *,
    session: Any | None = None,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
) -> None:
    """Mark a task succeeded. **Commit is part of the contract.** (GEN-P0-10)"""
    _apply_succeeded_fields(task)

    if session is not None:
        await _commit_or_rollback(session)
        return

    await _run_self_managed(
        task,
        _apply_succeeded_fields,
        session_factory=session_factory,
    )


async def mark_task_cancelled(
    task: Any,
    *,
    error_code: str = "cancelled",
    error_message: str = "task cancelled by user",
    session: Any | None = None,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
) -> None:
    """Mark a task cancelled. **Commit is part of the contract.** (GEN-P0-10)"""
    _apply_cancelled_fields(task, error_code=error_code, error_message=error_message)

    if session is not None:
        await _commit_or_rollback(session)
        return

    await _run_self_managed(
        task,
        lambda t: _apply_cancelled_fields(
            t, error_code=error_code, error_message=error_message
        ),
        session_factory=session_factory,
    )


__all__ = [
    "is_generation_terminal",
    "is_completion_terminal",
    "mark_task_failed",
    "mark_task_succeeded",
    "mark_task_cancelled",
]
