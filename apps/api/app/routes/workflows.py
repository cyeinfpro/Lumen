"""Public workflow HTTP router and stable endpoint exports."""

from __future__ import annotations

from ..workflow_domain.apparel_library import (
    WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
)
from ..workflow_services import apparel_endpoints as _apparel
from ..workflow_services import model_library_endpoints as _model_library
from ..workflow_services import poster_endpoints as _poster
from ..workflow_services import project_endpoints as _project

router = _project.router

list_workflows = _project.list_workflows
get_workflow = _project.get_workflow
reconcile_workflow = _project.reconcile_workflow
patch_workflow = _project.patch_workflow
delete_workflow = _project.delete_workflow
add_workflow_assets = _project.add_workflow_assets
save_model_candidate_to_library = _project.save_model_candidate_to_library
approve_model_candidate = _project.approve_model_candidate
reopen_model_selection = _project.reopen_model_selection
create_accessory_previews = _project.create_accessory_previews
save_accessory_selection = _project.save_accessory_selection
create_showcase_images = _project.create_showcase_images
revise_showcase_image = _project.revise_showcase_image
complete_delivery = _project.complete_delivery

create_apparel_model_showcase = _apparel.create_apparel_model_showcase
list_apparel_model_library = _apparel.list_apparel_model_library
sync_apparel_model_library_presets = _apparel.sync_apparel_model_library_presets
get_apparel_model_library_item_binary = _apparel.get_apparel_model_library_item_binary
get_apparel_model_library_item_thumb = _apparel.get_apparel_model_library_item_thumb
create_apparel_model_library_item = _apparel.create_apparel_model_library_item
patch_apparel_model_library_item = _apparel.patch_apparel_model_library_item
delete_apparel_model_library_item = _apparel.delete_apparel_model_library_item
batch_delete_apparel_model_library_items = (
    _apparel.batch_delete_apparel_model_library_items
)
approve_product_analysis = _apparel.approve_product_analysis
create_model_candidates = _apparel.create_model_candidates
select_apparel_model_library_item = _apparel.select_apparel_model_library_item

generate_apparel_model_library_job = _model_library.generate_apparel_model_library_job
list_apparel_model_library_jobs = _model_library.list_apparel_model_library_jobs
delete_apparel_model_library_job = _model_library.delete_apparel_model_library_job
clear_apparel_model_library_jobs = _model_library.clear_apparel_model_library_jobs
save_apparel_model_library_job_item = _model_library.save_apparel_model_library_job_item
auto_tag_apparel_model_library_item = _model_library.auto_tag_apparel_model_library_item

POSTER_WORKFLOW_TYPE = _poster.POSTER_WORKFLOW_TYPE
create_poster_design_workflow = _poster.create_poster_design_workflow
approve_copy_analysis = _poster.approve_copy_analysis
create_poster_masters = _poster.create_poster_masters
approve_poster_master = _poster.approve_poster_master
create_poster_renders = _poster.create_poster_renders
revise_poster_render = _poster.revise_poster_render
inpaint_poster_render = _poster.inpaint_poster_render

__all__ = [
    "POSTER_WORKFLOW_TYPE",
    "WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE",
    "add_workflow_assets",
    "approve_copy_analysis",
    "approve_model_candidate",
    "approve_poster_master",
    "approve_product_analysis",
    "auto_tag_apparel_model_library_item",
    "batch_delete_apparel_model_library_items",
    "clear_apparel_model_library_jobs",
    "complete_delivery",
    "create_accessory_previews",
    "create_apparel_model_library_item",
    "create_apparel_model_showcase",
    "create_model_candidates",
    "create_poster_design_workflow",
    "create_poster_masters",
    "create_poster_renders",
    "create_showcase_images",
    "delete_apparel_model_library_item",
    "delete_apparel_model_library_job",
    "delete_workflow",
    "generate_apparel_model_library_job",
    "get_apparel_model_library_item_binary",
    "get_apparel_model_library_item_thumb",
    "get_workflow",
    "inpaint_poster_render",
    "list_apparel_model_library",
    "list_apparel_model_library_jobs",
    "list_workflows",
    "patch_apparel_model_library_item",
    "patch_workflow",
    "reconcile_workflow",
    "reopen_model_selection",
    "revise_poster_render",
    "revise_showcase_image",
    "router",
    "save_accessory_selection",
    "save_apparel_model_library_job_item",
    "save_model_candidate_to_library",
    "select_apparel_model_library_item",
    "sync_apparel_model_library_presets",
]
