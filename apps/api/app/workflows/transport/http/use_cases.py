"""Named compatibility use cases owned by the HTTP transport."""

from __future__ import annotations

from dataclasses import dataclass

from .operation_ports import (
    ApparelWorkflowOperations,
    ModelLibraryWorkflowOperations,
    PosterWorkflowOperations,
    ProjectWorkflowOperations,
)


@dataclass(frozen=True, slots=True)
class WorkflowHttpUseCases:
    projects: ProjectWorkflowOperations
    apparel: ApparelWorkflowOperations
    model_library: ModelLibraryWorkflowOperations
    poster: PosterWorkflowOperations

    async def get_workflow(
        self, *, workflow_run_id: str, user: object, db: object
    ) -> object:
        return await self.projects.get_workflow(
            workflow_run_id=workflow_run_id, user=user, db=db
        )

    async def reconcile_workflow(
        self, *, workflow_run_id: str, user: object, db: object
    ) -> object:
        return await self.projects.reconcile_workflow(
            workflow_run_id=workflow_run_id, user=user, db=db
        )

    async def add_workflow_assets(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.projects.add_workflow_assets(
            workflow_run_id=workflow_run_id, body=body, user=user, db=db
        )

    async def save_model_candidate_to_library(
        self,
        *,
        workflow_run_id: str,
        candidate_id: str,
        body: object,
        user: object,
        db: object,
        background_tasks: object,
    ) -> object:
        return await self.projects.save_model_candidate_to_library(
            workflow_run_id=workflow_run_id,
            candidate_id=candidate_id,
            body=body,
            user=user,
            db=db,
            background_tasks=background_tasks,
        )

    async def approve_model_candidate(
        self,
        *,
        workflow_run_id: str,
        candidate_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.projects.approve_model_candidate(
            workflow_run_id=workflow_run_id,
            candidate_id=candidate_id,
            body=body,
            user=user,
            db=db,
        )

    async def reopen_model_selection(
        self, *, workflow_run_id: str, user: object, db: object
    ) -> object:
        return await self.projects.reopen_model_selection(
            workflow_run_id=workflow_run_id, user=user, db=db
        )

    async def create_accessory_previews(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.projects.create_accessory_previews(
            workflow_run_id=workflow_run_id, body=body, user=user, db=db
        )

    async def save_accessory_selection(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.projects.save_accessory_selection(
            workflow_run_id=workflow_run_id, body=body, user=user, db=db
        )

    async def create_showcase_images(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.projects.create_showcase_images(
            workflow_run_id=workflow_run_id, body=body, user=user, db=db
        )

    async def revise_showcase_image(
        self,
        *,
        workflow_run_id: str,
        image_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.projects.revise_showcase_image(
            workflow_run_id=workflow_run_id,
            image_id=image_id,
            body=body,
            user=user,
            db=db,
        )

    async def complete_delivery(
        self, *, workflow_run_id: str, user: object, db: object
    ) -> object:
        return await self.projects.complete_delivery(
            workflow_run_id=workflow_run_id, user=user, db=db
        )

    async def list_apparel_model_library(
        self,
        *,
        user: object,
        db: object,
        age_segment: object,
        source: str,
        appearance: str,
        q: str,
    ) -> object:
        return await self.apparel.list_apparel_model_library(
            user=user,
            db=db,
            age_segment=age_segment,
            source=source,
            appearance=appearance,
            q=q,
        )

    async def sync_apparel_model_library_presets(
        self, *, user: object, db: object
    ) -> object:
        return await self.apparel.sync_apparel_model_library_presets(user=user, db=db)

    async def get_apparel_model_library_item_binary(
        self,
        *,
        item_id: str,
        request: object,
        user: object,
        db: object,
    ) -> object:
        return await self.apparel.get_apparel_model_library_item_binary(
            item_id=item_id, request=request, user=user, db=db
        )

    async def get_apparel_model_library_item_thumb(
        self,
        *,
        item_id: str,
        request: object,
        user: object,
        db: object,
    ) -> object:
        return await self.apparel.get_apparel_model_library_item_thumb(
            item_id=item_id, request=request, user=user, db=db
        )

    async def create_apparel_model_library_item(
        self,
        *,
        body: object,
        user: object,
        db: object,
        background_tasks: object,
    ) -> object:
        return await self.apparel.create_apparel_model_library_item(
            body=body,
            user=user,
            db=db,
            background_tasks=background_tasks,
        )

    async def patch_apparel_model_library_item(
        self,
        *,
        item_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.apparel.patch_apparel_model_library_item(
            item_id=item_id, body=body, user=user, db=db
        )

    async def delete_apparel_model_library_item(
        self, *, item_id: str, user: object, db: object
    ) -> object:
        return await self.apparel.delete_apparel_model_library_item(
            item_id=item_id, user=user, db=db
        )

    async def batch_delete_apparel_model_library_items(
        self, *, body: object, user: object, db: object
    ) -> object:
        return await self.apparel.batch_delete_apparel_model_library_items(
            body=body, user=user, db=db
        )

    async def approve_product_analysis(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.apparel.approve_product_analysis(
            workflow_run_id=workflow_run_id, body=body, user=user, db=db
        )

    async def create_model_candidates(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.apparel.create_model_candidates(
            workflow_run_id=workflow_run_id, body=body, user=user, db=db
        )

    async def select_apparel_model_library_item(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.apparel.select_apparel_model_library_item(
            workflow_run_id=workflow_run_id, body=body, user=user, db=db
        )

    async def generate_apparel_model_library_job(
        self, *, body: object, user: object, db: object
    ) -> object:
        return await self.model_library.generate_apparel_model_library_job(
            body=body, user=user, db=db
        )

    async def list_apparel_model_library_jobs(
        self,
        *,
        user: object,
        db: object,
        limit: int,
        offset: int,
    ) -> object:
        return await self.model_library.list_apparel_model_library_jobs(
            user=user, db=db, limit=limit, offset=offset
        )

    async def delete_apparel_model_library_job(
        self, *, workflow_run_id: str, user: object, db: object
    ) -> object:
        return await self.model_library.delete_apparel_model_library_job(
            workflow_run_id=workflow_run_id, user=user, db=db
        )

    async def clear_apparel_model_library_jobs(
        self, *, user: object, db: object
    ) -> object:
        return await self.model_library.clear_apparel_model_library_jobs(
            user=user, db=db
        )

    async def save_apparel_model_library_job_item(
        self,
        *,
        workflow_run_id: str,
        image_id: str,
        body: object,
        user: object,
        db: object,
        background_tasks: object,
    ) -> object:
        return await self.model_library.save_apparel_model_library_job_item(
            workflow_run_id=workflow_run_id,
            image_id=image_id,
            body=body,
            user=user,
            db=db,
            background_tasks=background_tasks,
        )

    async def auto_tag_apparel_model_library_item(
        self, *, item_id: str, user: object, db: object
    ) -> object:
        return await self.model_library.auto_tag_apparel_model_library_item(
            item_id=item_id, user=user, db=db
        )

    async def approve_copy_analysis(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.poster.approve_copy_analysis(
            workflow_run_id=workflow_run_id, body=body, user=user, db=db
        )

    async def create_poster_masters(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.poster.create_poster_masters(
            workflow_run_id=workflow_run_id, body=body, user=user, db=db
        )

    async def approve_poster_master(
        self,
        *,
        workflow_run_id: str,
        master_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.poster.approve_poster_master(
            workflow_run_id=workflow_run_id,
            master_id=master_id,
            body=body,
            user=user,
            db=db,
        )

    async def create_poster_renders(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.poster.create_poster_renders(
            workflow_run_id=workflow_run_id, body=body, user=user, db=db
        )

    async def revise_poster_render(
        self,
        *,
        workflow_run_id: str,
        render_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.poster.revise_poster_render(
            workflow_run_id=workflow_run_id,
            render_id=render_id,
            body=body,
            user=user,
            db=db,
        )

    async def inpaint_poster_render(
        self,
        *,
        workflow_run_id: str,
        render_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object:
        return await self.poster.inpaint_poster_render(
            workflow_run_id=workflow_run_id,
            render_id=render_id,
            body=body,
            user=user,
            db=db,
        )


__all__ = ["WorkflowHttpUseCases"]
