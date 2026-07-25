"""Thin poster workflow route compatibility module."""

from __future__ import annotations

from ...workflow_services import poster_endpoints as _impl


router = _impl.router
FACADE_EXPORTS = _impl.FACADE_EXPORTS
POSTER_DEFAULT_TARGET_ASPECTS = _impl.POSTER_DEFAULT_TARGET_ASPECTS
POSTER_MASTER_ASPECT = _impl.POSTER_MASTER_ASPECT
POSTER_WORKFLOW_STEPS = _impl.POSTER_WORKFLOW_STEPS
POSTER_WORKFLOW_TYPE = _impl.POSTER_WORKFLOW_TYPE

_poster_image_params = _impl.poster_image_params
_poster_master_image_params = _impl.poster_master_image_params
_poster_find_preset_item = _impl.poster_find_preset_item
_poster_style_from_preset = _impl.poster_style_from_preset
_poster_load_style = _impl.poster_load_style
_poster_copy_analysis_prompt = _impl.poster_copy_analysis_prompt
_poster_style_summary = _impl.poster_style_summary
_poster_layout_safe_area = _impl.poster_layout_safe_area
_poster_text_fields_block = _impl.poster_text_fields_block
_poster_brand_assets_block = _impl.poster_brand_assets_block
_poster_brand_attachment_ids = _impl.poster_brand_attachment_ids
_poster_master_prompt = _impl.poster_master_prompt
_poster_render_prompt = _impl.poster_render_prompt
_poster_revision_prompt = _impl.poster_revision_prompt
_poster_seed_steps = _impl.poster_seed_steps
_create_poster_workflow_task = _impl.create_poster_workflow_task
_poster_parse_copy_analysis_text = _impl.poster_parse_copy_analysis_text
_poster_merge_copy_corrections = _impl.poster_merge_copy_corrections
_sync_poster_workflow_outputs = _impl.sync_poster_workflow_outputs
create_poster_design_workflow = _impl.create_poster_design_workflow
approve_copy_analysis = _impl.approve_copy_analysis
create_poster_masters = _impl.create_poster_masters
approve_poster_master = _impl.approve_poster_master
_poster_selected_master = _impl.poster_selected_master
create_poster_renders = _impl.create_poster_renders
revise_poster_render = _impl.revise_poster_render
_do_poster_inpaint = _impl.do_poster_inpaint
inpaint_poster_render = _impl.inpaint_poster_render


__all__ = [
    *FACADE_EXPORTS,
    "FACADE_EXPORTS",
    "POSTER_DEFAULT_TARGET_ASPECTS",
    "POSTER_MASTER_ASPECT",
    "POSTER_WORKFLOW_STEPS",
    "POSTER_WORKFLOW_TYPE",
    "router",
]


# Public compatibility contracts.
create_poster_workflow_task = _create_poster_workflow_task
do_poster_inpaint = _do_poster_inpaint
poster_brand_assets_block = _poster_brand_assets_block
poster_brand_attachment_ids = _poster_brand_attachment_ids
poster_copy_analysis_prompt = _poster_copy_analysis_prompt
poster_find_preset_item = _poster_find_preset_item
poster_image_params = _poster_image_params
poster_layout_safe_area = _poster_layout_safe_area
poster_load_style = _poster_load_style
poster_master_image_params = _poster_master_image_params
poster_master_prompt = _poster_master_prompt
poster_merge_copy_corrections = _poster_merge_copy_corrections
poster_parse_copy_analysis_text = _poster_parse_copy_analysis_text
poster_render_prompt = _poster_render_prompt
poster_revision_prompt = _poster_revision_prompt
poster_seed_steps = _poster_seed_steps
poster_selected_master = _poster_selected_master
poster_style_from_preset = _poster_style_from_preset
poster_style_summary = _poster_style_summary
poster_text_fields_block = _poster_text_fields_block
sync_poster_workflow_outputs = _sync_poster_workflow_outputs
