"""HTTP-facing workflow operation ports.

The application owns the named use-case API. Adapters implement these ports
without leaking FastAPI or SQLAlchemy imports into the application package.
"""

from __future__ import annotations

from typing import Protocol


class ProjectWorkflowOperations(Protocol):
    async def get_workflow(
        self, *, workflow_run_id: str, user: object, db: object
    ) -> object: ...

    async def reconcile_workflow(
        self, *, workflow_run_id: str, user: object, db: object
    ) -> object: ...

    async def delete_workflow(
        self, *, workflow_run_id: str, user: object, db: object
    ) -> object: ...

    async def add_workflow_assets(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def save_model_candidate_to_library(
        self,
        *,
        workflow_run_id: str,
        candidate_id: str,
        body: object,
        user: object,
        db: object,
        background_tasks: object,
    ) -> object: ...

    async def approve_model_candidate(
        self,
        *,
        workflow_run_id: str,
        candidate_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def reopen_model_selection(
        self, *, workflow_run_id: str, user: object, db: object
    ) -> object: ...

    async def create_accessory_previews(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def save_accessory_selection(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def create_showcase_images(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def revise_showcase_image(
        self,
        *,
        workflow_run_id: str,
        image_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def complete_delivery(
        self, *, workflow_run_id: str, user: object, db: object
    ) -> object: ...


class ApparelWorkflowOperations(Protocol):
    async def list_apparel_model_library(
        self,
        *,
        user: object,
        db: object,
        age_segment: object,
        source: str,
        appearance: str,
        q: str,
    ) -> object: ...

    async def sync_apparel_model_library_presets(
        self, *, user: object, db: object
    ) -> object: ...

    async def get_apparel_model_library_item_binary(
        self,
        *,
        item_id: str,
        request: object,
        user: object,
        db: object,
    ) -> object: ...

    async def get_apparel_model_library_item_thumb(
        self,
        *,
        item_id: str,
        request: object,
        user: object,
        db: object,
    ) -> object: ...

    async def create_apparel_model_library_item(
        self,
        *,
        body: object,
        user: object,
        db: object,
        background_tasks: object,
    ) -> object: ...

    async def patch_apparel_model_library_item(
        self,
        *,
        item_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def delete_apparel_model_library_item(
        self, *, item_id: str, user: object, db: object
    ) -> object: ...

    async def batch_delete_apparel_model_library_items(
        self, *, body: object, user: object, db: object
    ) -> object: ...

    async def approve_product_analysis(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def create_model_candidates(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def select_apparel_model_library_item(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...


class ModelLibraryWorkflowOperations(Protocol):
    async def generate_apparel_model_library_job(
        self, *, body: object, user: object, db: object
    ) -> object: ...

    async def list_apparel_model_library_jobs(
        self,
        *,
        user: object,
        db: object,
        limit: int,
        offset: int,
    ) -> object: ...

    async def delete_apparel_model_library_job(
        self, *, workflow_run_id: str, user: object, db: object
    ) -> object: ...

    async def clear_apparel_model_library_jobs(
        self, *, user: object, db: object
    ) -> object: ...

    async def save_apparel_model_library_job_item(
        self,
        *,
        workflow_run_id: str,
        image_id: str,
        body: object,
        user: object,
        db: object,
        background_tasks: object,
    ) -> object: ...

    async def auto_tag_apparel_model_library_item(
        self, *, item_id: str, user: object, db: object
    ) -> object: ...


class PosterWorkflowOperations(Protocol):
    async def approve_copy_analysis(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def create_poster_masters(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def approve_poster_master(
        self,
        *,
        workflow_run_id: str,
        master_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def create_poster_renders(
        self,
        *,
        workflow_run_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def revise_poster_render(
        self,
        *,
        workflow_run_id: str,
        render_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...

    async def inpaint_poster_render(
        self,
        *,
        workflow_run_id: str,
        render_id: str,
        body: object,
        user: object,
        db: object,
    ) -> object: ...


__all__ = [
    "ApparelWorkflowOperations",
    "ModelLibraryWorkflowOperations",
    "PosterWorkflowOperations",
    "ProjectWorkflowOperations",
]
