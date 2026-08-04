"""HTTP error mapping for workflow application actions."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from ....deps import durable_session_id_from_db
from ....services.active_user import (
    ActiveUserFenceError,
    account_mode_from_user,
    active_user_fence_http_error,
    lock_active_user_snapshot,
)
from ...application.errors import WorkflowRequestError
from ...application.paid_idempotency import (
    execute_paid_operation,
    paid_operation_request,
)
from ...composition import build_paid_operation_port


async def execute_workflow_action(
    action: Callable[..., Awaitable[Any]],
    **kwargs: Any,
) -> Any:
    try:
        return await action(**kwargs)
    except WorkflowRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc


async def execute_durable_workflow_action(
    action: Callable[..., Awaitable[Any]],
    *,
    identity_user: Any | None = None,
    identity_db: Any | None = None,
    **kwargs: Any,
) -> Any:
    user = identity_user if identity_user is not None else kwargs.get("user")
    db = identity_db if identity_db is not None else kwargs.get("db")
    if user is None or db is None:
        raise RuntimeError("durable workflow action requires user and db identity")
    expected_account_mode = account_mode_from_user(user)

    async def execute() -> Any:
        try:
            snapshot = await lock_active_user_snapshot(
                db,
                str(user.id),
                expected_account_mode,
                session_id=durable_session_id_from_db(db),
            )
        except ActiveUserFenceError as exc:
            raise active_user_fence_http_error(exc) from exc
        locked_kwargs = dict(kwargs)
        if "user" in locked_kwargs:
            locked_kwargs["user"] = snapshot.user
        if "account_mode" in locked_kwargs:
            locked_kwargs["account_mode"] = snapshot.account_mode
        return await action(**locked_kwargs)

    return await execute_workflow_action(execute)


async def execute_paid_workflow_action(
    action: Callable[..., Awaitable[Any]],
    *,
    operation_namespace: str,
    request_payload: Any,
    idempotency_key: str | None,
    idempotency_user: Any,
    idempotency_db: Any,
    idempotency_session_id: str | None = None,
    replay: Callable[[Any], Awaitable[Any]],
    **kwargs: Any,
) -> Any:
    expected_account_mode = account_mode_from_user(idempotency_user)

    async def guarded_action(**action_kwargs: Any) -> Any:
        try:
            snapshot = await lock_active_user_snapshot(
                idempotency_db,
                str(idempotency_user.id),
                expected_account_mode,
                session_id=idempotency_session_id,
            )
        except ActiveUserFenceError as exc:
            raise active_user_fence_http_error(exc) from exc
        locked_kwargs = dict(action_kwargs)
        if "user" in locked_kwargs:
            locked_kwargs["user"] = snapshot.user
        return await action(**locked_kwargs)

    async def execute() -> Any:
        request = paid_operation_request(
            user_id=str(idempotency_user.id),
            idempotency_key=idempotency_key,
            operation_namespace=operation_namespace,
            payload=request_payload,
        )
        return await execute_paid_operation(
            guarded_action,
            request=request,
            port=build_paid_operation_port(idempotency_db),
            replay=replay,
            action_kwargs=kwargs,
        )

    return await execute_workflow_action(execute)


__all__ = [
    "execute_durable_workflow_action",
    "execute_paid_workflow_action",
    "execute_workflow_action",
]
