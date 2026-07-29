"""Manual context-compaction arq task implementation."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from lumen_core.constants import GenerationErrorCode as EC, Role
from lumen_core.models import Conversation, Message

from ...provider_runtime.errors import UpstreamError
from ...provider_runtime.upstream_services import ImageUpstreamRuntime


@dataclass(frozen=True, slots=True)
class ManualCompactDependencies:
    session_factory: Callable[[], Any]
    ensure_summary: Callable[..., Awaitable[dict[str, Any] | None]]
    job_key: Callable[..., str]
    set_job_status: Callable[..., Awaitable[None]]
    release_active: Callable[..., Awaitable[None]]
    compact_payload: Callable[..., dict[str, Any]]
    utc_now: Callable[[], Any]
    logger: logging.Logger


async def manual_compact_conversation(
    ctx: dict[str, Any],
    user_id: str,
    conv_id: str,
    boundary_id: str,
    job_id: str,
    extra_instruction: str | None,
    target_tokens: int,
    input_budget: int,
    summary_timeout_s: float,
    model: str,
    *,
    deps: ManualCompactDependencies,
) -> dict[str, Any]:
    redis = ctx.get("redis")
    job_key = deps.job_key(
        user_id=user_id,
        conv_id=conv_id,
        job_id=job_id,
    )
    now = deps.utc_now().isoformat()
    await deps.set_job_status(
        redis,
        job_key,
        {
            "status": "running",
            "job_id": job_id,
            "user_id": user_id,
            "conv_id": conv_id,
            "boundary_id": boundary_id,
            "created_at": now,
            "updated_at": now,
        },
    )

    try:
        image_upstream_runtime = ctx["image_upstream_runtime"]
        if not isinstance(image_upstream_runtime, ImageUpstreamRuntime):
            raise TypeError(
                "ctx['image_upstream_runtime'] must be ImageUpstreamRuntime"
            )

        async with deps.session_factory() as session:
            conv = (
                await session.execute(
                    select(Conversation).where(
                        Conversation.id == conv_id,
                        Conversation.user_id == user_id,
                        Conversation.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if conv is None:
                raise ValueError("conversation not found")

            boundary = await session.get(Message, boundary_id)
            if boundary is None or boundary.conversation_id != conv_id:
                boundary = (
                    await session.execute(
                        select(Message)
                        .where(
                            Message.conversation_id == conv_id,
                            Message.deleted_at.is_(None),
                            Message.role.in_((Role.USER.value, Role.ASSISTANT.value)),
                        )
                        .order_by(Message.created_at.desc(), Message.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if boundary is None:
                raise ValueError("no messages to compact")

            result = await deps.ensure_summary(
                session,
                conv,
                boundary,
                {
                    "context.summary_target_tokens": target_tokens,
                    "context.summary_input_budget": input_budget,
                    "context.summary_http_timeout_s": summary_timeout_s,
                    "context.summary_model": model,
                    "redis": redis,
                },
                force=True,
                extra_instruction=extra_instruction,
                trigger="manual",
                image_upstream_runtime=image_upstream_runtime,
            )
            if (
                result is None
                or not isinstance(result, dict)
                or str(result.get("status") or "") in {"summary_failed", "failed"}
            ):
                raise UpstreamError(
                    "manual context summary failed",
                    error_code=EC.UPSTREAM_ERROR.value,
                    status_code=503,
                )

            await session.refresh(conv)
            response = {
                "status": "ok",
                "compacted": True,
                "summary": deps.compact_payload(result=result, conv=conv),
            }
            completed = deps.utc_now().isoformat()
            await deps.set_job_status(
                redis,
                job_key,
                {
                    "status": "succeeded",
                    "job_id": job_id,
                    "user_id": user_id,
                    "conv_id": conv_id,
                    "boundary_id": getattr(boundary, "id", boundary_id),
                    "created_at": now,
                    "updated_at": completed,
                    "completed_at": completed,
                    "response": response,
                },
            )
            return response
    except Exception as exc:  # noqa: BLE001
        deps.logger.exception(
            "manual_compact.worker_failed user=%s conv=%s job=%s",
            user_id,
            conv_id,
            job_id,
        )
        completed = deps.utc_now().isoformat()
        await deps.set_job_status(
            redis,
            job_key,
            {
                "status": "failed",
                "job_id": job_id,
                "user_id": user_id,
                "conv_id": conv_id,
                "boundary_id": boundary_id,
                "created_at": now,
                "updated_at": completed,
                "completed_at": completed,
                "reason": "upstream_error",
                "error": str(exc)[:500],
            },
        )
        raise
    finally:
        await deps.release_active(
            redis,
            user_id=user_id,
            conv_id=conv_id,
            job_id=job_id,
        )
