"""SQLAlchemy workflow read adapter."""

from __future__ import annotations

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.media_workflows import WorkflowRun, WorkflowStep

from ..ports.run_reads import (
    WorkflowRunCursor,
    WorkflowRunListRecord,
    WorkflowRunReadPage,
)


_OUTPUT_STEP_KEYS = ("showcase_generation", "multi_size_generation")
_COMPLETED_STEP_STATUSES = frozenset(
    {"approved", "completed", "succeeded", "done", "selected"}
)


def _completion_percent(run: WorkflowRun, step_statuses: list[str]) -> int:
    if run.status == "completed":
        return 100
    if not step_statuses:
        return 0
    completed = sum(1 for status in step_statuses if status in _COMPLETED_STEP_STATUSES)
    return max(0, min(99, round(completed * 100 / len(step_statuses))))


class SQLAlchemyWorkflowRunReadAdapter:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_runs(
        self,
        *,
        user_id: str,
        workflow_type: str | None,
        excluded_types: tuple[str, ...],
        after: WorkflowRunCursor | None,
        limit: int,
    ) -> WorkflowRunReadPage:
        statement = select(WorkflowRun).where(
            WorkflowRun.user_id == user_id,
            WorkflowRun.deleted_at.is_(None),
        )
        if workflow_type:
            statement = statement.where(WorkflowRun.type == workflow_type)
        elif excluded_types:
            statement = statement.where(WorkflowRun.type.notin_(excluded_types))
        if after is not None:
            statement = statement.where(
                or_(
                    WorkflowRun.updated_at < after.updated_at,
                    and_(
                        WorkflowRun.updated_at == after.updated_at,
                        WorkflowRun.id < after.run_id,
                    ),
                )
            )

        runs = list(
            (
                await self._session.execute(
                    statement.order_by(
                        desc(WorkflowRun.updated_at),
                        desc(WorkflowRun.id),
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        page = runs[:limit]
        output_counts, completion_percentages = await self._load_run_metrics(page)
        return WorkflowRunReadPage(
            items=tuple(
                self._record_from_run(
                    run,
                    output_counts.get(run.id, 0),
                    completion_percentages.get(run.id, 0),
                )
                for run in page
            ),
            has_more=len(runs) > limit,
        )

    async def _load_run_metrics(
        self,
        runs: list[WorkflowRun],
    ) -> tuple[dict[str, int], dict[str, int]]:
        if not runs:
            return {}, {}
        rows = (
            await self._session.execute(
                select(
                    WorkflowStep.workflow_run_id,
                    WorkflowStep.step_key,
                    WorkflowStep.status,
                    WorkflowStep.image_ids,
                ).where(WorkflowStep.workflow_run_id.in_([run.id for run in runs]))
            )
        ).all()
        output_counts: dict[str, int] = {}
        statuses_by_run: dict[str, list[str]] = {}
        for run_id, step_key, status, image_ids in rows:
            statuses_by_run.setdefault(run_id, []).append(status)
            if step_key in _OUTPUT_STEP_KEYS:
                output_counts[run_id] = output_counts.get(run_id, 0) + len(
                    image_ids or []
                )
        completion_percentages = {
            run.id: _completion_percent(run, statuses_by_run.get(run.id, []))
            for run in runs
        }
        return output_counts, completion_percentages

    @staticmethod
    def _record_from_run(
        run: WorkflowRun,
        output_count: int,
        completion_percent: int,
    ) -> WorkflowRunListRecord:
        return WorkflowRunListRecord(
            id=run.id,
            conversation_id=run.conversation_id,
            type=run.type,
            status=run.status,
            title=run.title,
            user_prompt=run.user_prompt,
            product_image_ids=tuple(run.product_image_ids or ()),
            current_step=run.current_step,
            quality_mode=run.quality_mode,
            metadata_jsonb=dict(run.metadata_jsonb or {}),
            created_at=run.created_at,
            updated_at=run.updated_at,
            output_count=output_count,
            completion_percent=completion_percent,
        )


__all__ = ["SQLAlchemyWorkflowRunReadAdapter"]
