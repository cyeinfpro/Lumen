"""SQLAlchemy adapter for workflow run creation."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import User
from lumen_core.schema_models.posters import (
    PosterBrandAssetsIn,
    PosterDesignWorkflowCreateIn,
)
from lumen_core.schema_models.workflows import ApparelWorkflowCreateIn

from ..ports.run_creation import (
    CreateApparelRunCommand,
    CreatePosterRunCommand,
    WorkflowRunCreated,
)
from .operations import apparel, poster


@dataclass(frozen=True, slots=True)
class SQLAlchemyWorkflowRunCreationAdapter:
    session: AsyncSession
    user: User

    async def create_apparel(
        self,
        command: CreateApparelRunCommand,
    ) -> WorkflowRunCreated:
        result = await apparel.create_apparel_model_showcase(
            ApparelWorkflowCreateIn(
                product_image_ids=list(command.product_image_ids),
                user_prompt=command.user_prompt,
                quality_mode=command.quality_mode,
                title=command.title,
            ),
            self.user,
            self.session,
        )
        return WorkflowRunCreated(
            workflow_run_id=result.workflow_run_id,
            status=result.status,
            current_step=result.current_step,
        )

    async def create_poster(
        self,
        command: CreatePosterRunCommand,
    ) -> WorkflowRunCreated:
        result = await poster.create_poster_design_workflow(
            PosterDesignWorkflowCreateIn(
                conversation_id=command.conversation_id,
                copy_text=command.copy_text,
                style_id=command.style_id,
                target_aspects=list(command.target_aspects),
                brand_assets=PosterBrandAssetsIn(
                    logo_image_id=command.brand_assets.logo_image_id,
                    product_image_id=command.brand_assets.product_image_id,
                    primary_color=command.brand_assets.primary_color,
                    font_family=command.brand_assets.font_family,
                ),
                quality_mode=command.quality_mode,
                title=command.title,
            ),
            self.user,
            self.session,
        )
        return WorkflowRunCreated(
            workflow_run_id=result.workflow_run_id,
            status=result.status,
            current_step=result.current_step,
        )


__all__ = ["SQLAlchemyWorkflowRunCreationAdapter"]
