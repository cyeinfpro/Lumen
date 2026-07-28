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
        output_counts = await self._load_output_counts(page)
        return WorkflowRunReadPage(
            items=tuple(
                self._record_from_run(run, output_counts.get(run.id, 0))
                for run in page
            ),
            has_more=len(runs) > limit,
        )

    async def _load_output_counts(
        self,
        runs: list[WorkflowRun],
    ) -> dict[str, int]:
        if not runs:
            return {}
        rows = (
            await self._session.execute(
                select(WorkflowStep.workflow_run_id, WorkflowStep.image_ids).where(
                    WorkflowStep.workflow_run_id.in_([run.id for run in runs]),
                    WorkflowStep.step_key.in_(_OUTPUT_STEP_KEYS),
                )
            )
        ).all()
        output_counts: dict[str, int] = {}
        for run_id, image_ids in rows:
            output_counts[run_id] = output_counts.get(run_id, 0) + len(image_ids or [])
        return output_counts

    @staticmethod
    def _record_from_run(
        run: WorkflowRun,
        output_count: int,
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
        )


__all__ = ["SQLAlchemyWorkflowRunReadAdapter"]
