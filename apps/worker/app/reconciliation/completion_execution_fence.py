"""Completion execution fencing adapters."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from lumen_core.constants import CompletionStatus


def bind_completion_execution_fence(
    state: Any,
    execution_epoch: int,
) -> None:
    epoch = max(0, int(execution_epoch))
    ports = state.ports
    persistence = ports.persistence
    completion_model = persistence.Completion
    original_update = persistence.update
    original_context_record = ports.context._record_completion_context_metadata
    original_event_payload = ports.events._completion_event_payload
    original_upstream = ports.upstream

    def fenced_update(model: Any) -> Any:
        statement = original_update(model)
        if model is completion_model:
            statement = statement.where(completion_model.execution_epoch == epoch)
        return statement

    async def fenced_flush(
        task_id: str,
        text: str,
        *,
        attempt_epoch: int,
        retries: int = 3,
    ) -> None:
        last_exc: BaseException | None = None
        for index in range(max(1, int(retries))):
            try:
                async with persistence.SessionLocal() as session:
                    result = await session.execute(
                        fenced_update(completion_model)
                        .where(
                            completion_model.id == task_id,
                            completion_model.attempt == attempt_epoch,
                            completion_model.status == CompletionStatus.STREAMING.value,
                        )
                        .values(text=text)
                    )
                    if persistence.affected_rows(result) == 0:
                        raise ports.retry._CompletionEpochSuperseded(
                            f"completion execution superseded task={task_id} "
                            f"execution_epoch={epoch} attempt={attempt_epoch}"
                        )
                    await session.commit()
                    return
            except ports.retry._CompletionEpochSuperseded:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                ports.events.logger.warning(
                    "completion text flush failed task=%s epoch=%s "
                    "attempt=%s try=%d/%d err=%s",
                    task_id,
                    epoch,
                    attempt_epoch,
                    index + 1,
                    retries,
                    exc,
                )
                if index + 1 < retries:
                    await asyncio.sleep(0.2 * (2**index))
        raise original_upstream.UpstreamError(
            "completion text flush failed after retries",
            error_code="upstream_error",
            status_code=None,
        ) from last_exc

    async def fenced_context_record(
        session: Any,
        *,
        task_id: str,
        attempt_epoch: int,
        packed: Any,
    ) -> None:
        current_epoch = (
            await session.execute(
                persistence.select(completion_model.execution_epoch)
                .where(
                    completion_model.id == task_id,
                    completion_model.attempt == attempt_epoch,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current_epoch != epoch:
            raise ports.retry._CompletionEpochSuperseded(
                f"completion context superseded task={task_id} "
                f"execution_epoch={epoch} current={current_epoch}"
            )
        await original_context_record(
            session,
            task_id=task_id,
            attempt_epoch=attempt_epoch,
            packed=packed,
        )

    async def fenced_upstream_metadata(
        *,
        task_id: str,
        attempt_epoch: int,
        provider_event: dict[str, str],
        fast_mode: bool,
    ) -> None:
        if not provider_event:
            return
        async with persistence.SessionLocal() as session:
            completion = (
                await session.execute(
                    persistence.select(completion_model)
                    .where(
                        completion_model.id == task_id,
                        completion_model.attempt == attempt_epoch,
                        completion_model.execution_epoch == epoch,
                        completion_model.status.in_(
                            ports.retry._RUNNING_COMPLETION_STATUSES
                        ),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if completion is None:
                raise ports.retry._CompletionEpochSuperseded(
                    f"completion metadata superseded task={task_id} "
                    f"execution_epoch={epoch} attempt={attempt_epoch}"
                )
            completion.upstream_request = (
                original_upstream._merge_completion_upstream_metadata(
                    dict(completion.upstream_request or {}),
                    provider_event=provider_event,
                    fast_mode=fast_mode,
                )
                or None
            )
            await session.commit()

    def fenced_event_payload(*args: Any, **extra: Any) -> dict[str, Any]:
        extra.setdefault("execution_epoch", epoch)
        return original_event_payload(*args, **extra)

    state.ports = replace(
        ports,
        persistence=replace(
            persistence,
            update=fenced_update,
            _flush_completion_text=fenced_flush,
        ),
        context=replace(
            ports.context,
            _record_completion_context_metadata=fenced_context_record,
        ),
        upstream=replace(
            original_upstream,
            _record_completion_upstream_metadata=fenced_upstream_metadata,
        ),
        events=replace(
            ports.events,
            _completion_event_payload=fenced_event_payload,
        ),
    )
