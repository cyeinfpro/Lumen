# ruff: noqa: E402, F401, F403, F405
"""Frozen static exports for the historical ``app.routes.workflows`` surface."""

from __future__ import annotations

import re

from ..routes.workflow_routes import apparel as _apparel_routes
from ..routes.workflow_routes import model_library as _model_library_routes
from ..routes.workflow_routes import poster as _poster_routes
from ..workflow_services import library_sync as _library_sync_service
from ..workflow_services import showcase_preflight as _showcase_preflight_service
from ..workflow_services.workflow_runtime import *  # noqa: F403
from ..workflow_domain.workflow_policy_exports import *  # noqa: F403

# Legacy compatibility aliases for the extracted workflow endpoints.
create_apparel_model_showcase = _apparel_routes.create_apparel_model_showcase
list_apparel_model_library = _apparel_routes.list_apparel_model_library
sync_apparel_model_library_presets = _apparel_routes.sync_apparel_model_library_presets
get_apparel_model_library_item_binary = (
    _apparel_routes.get_apparel_model_library_item_binary
)
get_apparel_model_library_item_thumb = (
    _apparel_routes.get_apparel_model_library_item_thumb
)
create_apparel_model_library_item = _apparel_routes.create_apparel_model_library_item
patch_apparel_model_library_item = _apparel_routes.patch_apparel_model_library_item
_delete_apparel_model_library_item_for_user = (
    _apparel_routes.delete_apparel_model_library_item_for_user
)
delete_apparel_model_library_item = _apparel_routes.delete_apparel_model_library_item
batch_delete_apparel_model_library_items = (
    _apparel_routes.batch_delete_apparel_model_library_items
)
approve_product_analysis = _apparel_routes.approve_product_analysis
create_model_candidates = _apparel_routes.create_model_candidates
select_apparel_model_library_item = _apparel_routes.select_apparel_model_library_item

_MODEL_LIBRARY_TITLE_AGE_LABELS = _model_library_routes.MODEL_LIBRARY_TITLE_AGE_LABELS
_model_library_generate_genders = _model_library_routes.model_library_generate_genders
_model_library_gender_label = _model_library_routes.model_library_gender_label
_model_library_run_title = _model_library_routes.model_library_run_title
_model_library_generate_prompt = _model_library_routes.model_library_generate_prompt
_model_library_generate_image_params = (
    _model_library_routes.model_library_generate_image_params
)
_model_library_run_inputs = _model_library_routes.model_library_run_inputs
_saved_image_id_set = _model_library_routes.saved_image_id_set
_model_library_job_status = _model_library_routes.model_library_job_status
_gather_job_image_outs = _model_library_routes.gather_job_image_outs
_model_library_image_meta_by_id = _model_library_routes.model_library_image_meta_by_id
_job_item_out = _model_library_routes.job_item_out
_extract_bonus_ids = _model_library_routes.extract_bonus_ids
_workflow_produced_model_image_ids = (
    _model_library_routes.workflow_produced_model_image_ids
)
_job_from_library_run = _model_library_routes.job_from_library_run
_job_from_project_candidate_step = _model_library_routes.job_from_project_candidate_step
_enqueue_model_library_generate_tasks = (
    _model_library_routes.enqueue_model_library_generate_tasks
)
_model_library_explicit_genders = _model_library_routes.model_library_explicit_genders
_reference_profile_has_required_text_fields = (
    _model_library_routes.reference_profile_has_required_text_fields
)
_merge_reference_overrides = _model_library_routes.merge_reference_overrides
generate_apparel_model_library_job = (
    _model_library_routes.generate_apparel_model_library_job
)
list_apparel_model_library_jobs = _model_library_routes.list_apparel_model_library_jobs
delete_apparel_model_library_job = (
    _model_library_routes.delete_apparel_model_library_job
)
clear_apparel_model_library_jobs = (
    _model_library_routes.clear_apparel_model_library_jobs
)
save_apparel_model_library_job_item = (
    _model_library_routes.save_apparel_model_library_job_item
)
_api_call_tagging_upstream = _model_library_routes.api_call_tagging_upstream
_AGE_ALIASES_API = _model_library_routes.AGE_ALIASES_API
_normalize_tagged_age = _model_library_routes.normalize_tagged_age
_normalize_tagged_gender = _model_library_routes.normalize_tagged_gender
_auto_tag_library_item = _model_library_routes.auto_tag_library_item
_run_auto_tag_in_background = _model_library_routes.run_auto_tag_in_background
auto_tag_apparel_model_library_item = (
    _model_library_routes.auto_tag_apparel_model_library_item
)

POSTER_WORKFLOW_TYPE = _poster_routes.POSTER_WORKFLOW_TYPE
POSTER_WORKFLOW_STEPS = _poster_routes.POSTER_WORKFLOW_STEPS
POSTER_DEFAULT_TARGET_ASPECTS = _poster_routes.POSTER_DEFAULT_TARGET_ASPECTS
POSTER_MASTER_ASPECT = _poster_routes.POSTER_MASTER_ASPECT
_poster_image_params = _poster_routes.poster_image_params
_poster_master_image_params = _poster_routes.poster_master_image_params
_poster_find_preset_item = _poster_routes.poster_find_preset_item
_poster_style_from_preset = _poster_routes.poster_style_from_preset
_poster_load_style = _poster_routes.poster_load_style
_poster_copy_analysis_prompt = _poster_routes.poster_copy_analysis_prompt
_poster_style_summary = _poster_routes.poster_style_summary
_poster_layout_safe_area = _poster_routes.poster_layout_safe_area
_poster_text_fields_block = _poster_routes.poster_text_fields_block
_poster_brand_assets_block = _poster_routes.poster_brand_assets_block
_poster_brand_attachment_ids = _poster_routes.poster_brand_attachment_ids
_poster_master_prompt = _poster_routes.poster_master_prompt
_poster_render_prompt = _poster_routes.poster_render_prompt
_poster_revision_prompt = _poster_routes.poster_revision_prompt
_poster_seed_steps = _poster_routes.poster_seed_steps
_create_poster_workflow_task = _poster_routes.create_poster_workflow_task
_poster_parse_copy_analysis_text = _poster_routes.poster_parse_copy_analysis_text
_poster_merge_copy_corrections = _poster_routes.poster_merge_copy_corrections
_sync_poster_workflow_outputs = _poster_routes.sync_poster_workflow_outputs
create_poster_design_workflow = _poster_routes.create_poster_design_workflow
approve_copy_analysis = _poster_routes.approve_copy_analysis
create_poster_masters = _poster_routes.create_poster_masters
approve_poster_master = _poster_routes.approve_poster_master
_poster_selected_master = _poster_routes.poster_selected_master
create_poster_renders = _poster_routes.create_poster_renders
revise_poster_render = _poster_routes.revise_poster_render
_do_poster_inpaint = _poster_routes.do_poster_inpaint
inpaint_poster_render = _poster_routes.inpaint_poster_render

WORKFLOW_TYPE = "apparel_model_showcase"
WORKFLOW_STEPS = _showcase_preflight_service.WORKFLOW_STEPS
SHOT_POOL_BY_BAND = _showcase_preflight_service.SHOT_POOL_BY_BAND
ShotClass = _showcase_preflight_service.ShotClass
ShotPool = _showcase_preflight_service.ShotPool
ShotVariant = _showcase_preflight_service.ShotVariant
Template = _showcase_preflight_service.Template
CHILD_POOL = _showcase_preflight_service.CHILD_POOL
TODDLER_POOL = _showcase_preflight_service.TODDLER_POOL
_ShowcasePreflightProgressHook = (
    _showcase_preflight_service.ShowcasePreflightProgressHook
)
_STATIC_REWRITE_REPLACEMENTS = _showcase_preflight_service.STATIC_REWRITE_REPLACEMENTS
MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY = (
    _library_sync_service.MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY
)
MODEL_LIBRARY_SYNC_PROXY_NAME_KEY = (
    _library_sync_service.MODEL_LIBRARY_SYNC_PROXY_NAME_KEY
)
MODEL_LIBRARY_ROOT_KEY = _library_sync_service.MODEL_LIBRARY_ROOT_KEY
_GITHUB_API_HOST = _library_sync_service.GITHUB_API_HOST
_GITHUB_RAW_HOSTS = _library_sync_service.GITHUB_RAW_HOSTS
# apparel-model-library 常量 + 纯 helper 全部从 _apparel_library 导入。
# 这里 re-export 是为了让既有测试（apps/api/tests/test_workflows_route.py）
# 仍能通过 `workflows._normalize_age_segment` 等私有路径访问。
from ..workflow_domain.apparel_library import (
    MODEL_LIBRARY_AGE_SEGMENTS,
    MODEL_LIBRARY_APPEARANCES,
    MODEL_LIBRARY_FETCH_TIMEOUT_SECONDS,
    MODEL_LIBRARY_FOLDER_BY_AGE,
    MODEL_LIBRARY_GENDER_SEGMENTS,
    MODEL_LIBRARY_GENERATE_COUNTS,
    MODEL_LIBRARY_GENERATE_STEP_KEY,
    MODEL_LIBRARY_GENERATE_WORKER_ACTION,
    MODEL_LIBRARY_IMAGE_SUFFIXES,
    MODEL_LIBRARY_MAX_BINARY_BYTES,
    MODEL_LIBRARY_MAX_GITHUB_DEPTH,
    MODEL_LIBRARY_MAX_GITHUB_DIRECTORIES,
    MODEL_LIBRARY_MAX_GITHUB_FILES,
    MODEL_LIBRARY_MAX_GITHUB_METADATA_BYTES,
    MODEL_LIBRARY_MAX_GITHUB_RESPONSE_BYTES,
    MODEL_LIBRARY_MAX_INDEX_BYTES,
    MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES,
    MODEL_LIBRARY_SCHEMA_VERSION,
    MODEL_LIBRARY_SOURCES,
    MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS,
    MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS,
    MODEL_LIBRARY_SYNC_LEASE_SECONDS,
    MODEL_LIBRARY_SYNC_MODES,
    MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS,
    WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
)
from ..workflow_domain.apparel_library import SYNC_LOCK as _SYNC_LOCK  # noqa: F401
from ..workflow_domain.apparel_library import (
    age_segment_from_folder_name as _age_segment_from_folder_name,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    gender_from_folder_name as _gender_from_folder_name,
)  # noqa: F401
from ..workflow_domain.apparel_library import library_item_url as _library_item_url  # noqa: F401
from ..workflow_domain.apparel_library import (
    model_library_folder_for_age as _model_library_folder_for_age,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    model_library_sync_file_lock as _model_library_sync_file_lock,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    normalize_age_segment as _normalize_age_segment,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    normalize_appearance as _normalize_appearance,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    normalize_model_gender as _normalize_model_gender,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    preset_id_from_path as _preset_id_from_path,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    title_from_preset_id as _title_from_preset_id,
)  # noqa: F401


def _static_export(value):  # type: ignore[no-untyped-def]
    return value


_bind_library_service = _static_export
_bind_showcase_service = _static_export

# Preserve the historical names as direct references to their service owners.
# There is no runtime facade or module rebinding behind these aliases.
_write_json_atomic = _bind_library_service(_library_sync_service.write_json_atomic)
_fsync_dir = _bind_library_service(_library_sync_service.fsync_dir)
_read_file_bytes_bounded = _bind_library_service(
    _library_sync_service.read_file_bytes_bounded
)
_read_json_file = _bind_library_service(_library_sync_service.read_json_file)
_library_root = _bind_library_service(_library_sync_service.library_root)
_library_index_path = _bind_library_service(_library_sync_service.library_index_path)
_library_sync_state_path = _bind_library_service(
    _library_sync_service.library_sync_state_path
)
_library_sync_lock_path = _bind_library_service(
    _library_sync_service.library_sync_lock_path
)
_library_user_index_path = _bind_library_service(
    _library_sync_service.library_user_index_path
)
_default_library_index = _bind_library_service(
    _library_sync_service.default_library_index
)
_default_user_library_index = _bind_library_service(
    _library_sync_service.default_user_library_index
)
_default_sync_state = _bind_library_service(_library_sync_service.default_sync_state)
_github_contents_url = _bind_library_service(_library_sync_service.github_contents_url)
_sync_mode = _bind_library_service(_library_sync_service.sync_mode)
_model_library_http_client_kwargs = _bind_library_service(
    _library_sync_service.model_library_http_client_kwargs
)
_resolve_model_library_sync_proxy = _bind_library_service(
    _library_sync_service.resolve_model_library_sync_proxy
)
_can_sync_library = _bind_library_service(_library_sync_service.can_sync_library)
_sync_state_out = _bind_library_service(_library_sync_service.sync_state_out)
_model_library_item_out = _bind_library_service(
    _library_sync_service.model_library_item_out
)
_load_global_library_index = _bind_library_service(
    _library_sync_service.load_global_library_index
)
_load_user_library_index = _bind_library_service(
    _library_sync_service.load_user_library_index
)
_save_global_library_index = _bind_library_service(
    _library_sync_service.save_global_library_index
)
_save_user_library_index = _bind_library_service(
    _library_sync_service.save_user_library_index
)
_remove_user_library_item_from_legacy_index = _bind_library_service(
    _library_sync_service.remove_user_library_item_from_legacy_index
)
_hide_preset_in_legacy_user_library_index = _bind_library_service(
    _library_sync_service.hide_preset_in_legacy_user_library_index
)
_save_sync_state = _bind_library_service(_library_sync_service.save_sync_state)
_model_library_row_to_dict = _bind_library_service(
    _library_sync_service.model_library_row_to_dict
)
_legacy_library_item_insert_values = _bind_library_service(
    _library_sync_service.legacy_library_item_insert_values
)
_ensure_legacy_user_library_migrated = _bind_library_service(
    _library_sync_service.ensure_legacy_user_library_migrated
)
_load_user_library_items = _bind_library_service(
    _library_sync_service.load_user_library_items
)
_load_user_hidden_preset_ids = _bind_library_service(
    _library_sync_service.load_user_hidden_preset_ids
)
_combined_library_items = _bind_library_service(
    _library_sync_service.combined_library_items
)
_filter_library_items = _bind_library_service(
    _library_sync_service.filter_library_items
)
_find_library_item = _bind_library_service(_library_sync_service.find_library_item)
_guess_mime = _bind_library_service(_library_sync_service.guess_mime)
_sha256_file_bounded = _bind_library_service(_library_sync_service.sha256_file_bounded)
_open_library_storage_file = _bind_library_service(
    _library_sync_service.open_library_storage_file
)
_stream_file = _bind_library_service(_library_sync_service.stream_file)
_library_binary_response = _bind_library_service(
    _library_sync_service.library_binary_response
)
_preset_storage_key = _bind_library_service(_library_sync_service.preset_storage_key)
_preset_thumb_storage_key = _bind_library_service(
    _library_sync_service.preset_thumb_storage_key
)
_write_bytes_replace = _bind_library_service(_library_sync_service.write_bytes_replace)
_ModelLibrarySyncLimitExceeded = _library_sync_service.ModelLibrarySyncLimitExceeded
_ModelLibrarySyncLeaseLost = _library_sync_service.ModelLibrarySyncLeaseLost
_fetch_bytes = _bind_library_service(_library_sync_service.fetch_bytes)
_fetch_github_download_bytes = _bind_library_service(
    _library_sync_service.fetch_github_download_bytes
)
_github_api_child_url = _bind_library_service(
    _library_sync_service.github_api_child_url
)
_decoded_url_path_segments = _bind_library_service(
    _library_sync_service.decoded_url_path_segments
)
_validate_github_contents_url = _bind_library_service(
    _library_sync_service.validate_github_contents_url
)
_validate_github_download_url = _bind_library_service(
    _library_sync_service.validate_github_download_url
)
_walk_github_contents = _bind_library_service(
    _library_sync_service.walk_github_contents
)
_metadata_from_github_file = _bind_library_service(
    _library_sync_service.metadata_from_github_file
)
_github_entry_size = _bind_library_service(_library_sync_service.github_entry_size)
_sync_lease_owner = _bind_library_service(_library_sync_service.sync_lease_owner)
_claim_library_sync_lease_sync = _bind_library_service(
    _library_sync_service.claim_library_sync_lease_sync
)
_claim_library_sync_lease = _bind_library_service(
    _library_sync_service.claim_library_sync_lease
)
_renew_library_sync_lease_sync = _bind_library_service(
    _library_sync_service.renew_library_sync_lease_sync
)
_renew_library_sync_lease = _bind_library_service(
    _library_sync_service.renew_library_sync_lease
)
_complete_library_sync_lease_sync = _bind_library_service(
    _library_sync_service.complete_library_sync_lease_sync
)
_complete_library_sync_lease = _bind_library_service(
    _library_sync_service.complete_library_sync_lease
)
_fail_library_sync_lease_sync = _bind_library_service(
    _library_sync_service.fail_library_sync_lease_sync
)
_fail_library_sync_lease = _bind_library_service(
    _library_sync_service.fail_library_sync_lease
)
_cached_sync_response = _bind_library_service(
    _library_sync_service.cached_sync_response
)
_sync_library_presets_from_github_folder = _bind_library_service(
    _library_sync_service.sync_library_presets_from_github_folder
)
_do_sync_library_presets = _bind_library_service(
    _library_sync_service.do_sync_library_presets
)
_owned_image = _bind_library_service(_library_sync_service.owned_image)
_image_url = _bind_library_service(_library_sync_service.image_url)
_model_library_download_filename = _bind_library_service(
    _library_sync_service.model_library_download_filename
)
_model_library_image_metadata_from_fields = _bind_library_service(
    _library_sync_service.model_library_image_metadata_from_fields
)
_create_user_image_from_preset = _bind_library_service(
    _library_sync_service.create_user_image_from_preset
)
_add_user_library_item = _bind_library_service(
    _library_sync_service.add_user_library_item
)

_showcase_prompt_brief = _bind_showcase_service(
    _showcase_preflight_service.showcase_prompt_brief
)
_showcase_reference_image_ids = _bind_showcase_service(
    _showcase_preflight_service.showcase_reference_image_ids
)
_validate_accessory_preview_image = _bind_showcase_service(
    _showcase_preflight_service.validate_accessory_preview_image
)
_showcase_target_image_count = _bind_showcase_service(
    _showcase_preflight_service.showcase_target_image_count
)
_validate_owned_images = _bind_showcase_service(
    _showcase_preflight_service.validate_owned_images
)
_seed_steps = _bind_showcase_service(_showcase_preflight_service.seed_steps)
_product_analysis_prompt = _bind_showcase_service(
    _showcase_preflight_service.product_analysis_prompt
)
_candidate_prompt = _bind_showcase_service(_showcase_preflight_service.candidate_prompt)
_showcase_scene_label = _bind_showcase_service(
    _showcase_preflight_service.showcase_scene_label
)
_showcase_scene_card_direction = _bind_showcase_service(
    _showcase_preflight_service.showcase_scene_card_direction
)
_showcase_scene_card_scene_direction = _bind_showcase_service(
    _showcase_preflight_service.showcase_scene_card_scene_direction
)
_showcase_scene_card_action_direction = _bind_showcase_service(
    _showcase_preflight_service.showcase_scene_card_action_direction
)
_showcase_scene_card_camera_direction = _bind_showcase_service(
    _showcase_preflight_service.showcase_scene_card_camera_direction
)
_showcase_scene_card_text = _bind_showcase_service(
    _showcase_preflight_service.showcase_scene_card_text
)
_text_has_any = _bind_showcase_service(_showcase_preflight_service.text_has_any)
_is_child_showcase = _bind_showcase_service(
    _showcase_preflight_service.is_child_showcase
)
_showcase_scene_render_direction = _bind_showcase_service(
    _showcase_preflight_service.showcase_scene_render_direction
)
_showcase_scene_framing_direction = _bind_showcase_service(
    _showcase_preflight_service.showcase_scene_framing_direction
)
_showcase_visibility_policy = _bind_showcase_service(
    _showcase_preflight_service.showcase_visibility_policy
)
_truncate_prompt_text = _bind_showcase_service(
    _showcase_preflight_service.truncate_prompt_text
)
_join_lock_items = _bind_showcase_service(_showcase_preflight_service.join_lock_items)
_compact_lock_text = _bind_showcase_service(
    _showcase_preflight_service.compact_lock_text
)
_compact_product_identity = _bind_showcase_service(
    _showcase_preflight_service.compact_product_identity
)
_showcase_garment_lock_prefix = _bind_showcase_service(
    _showcase_preflight_service.showcase_garment_lock_prefix
)
_showcase_prompt = _bind_showcase_service(_showcase_preflight_service.showcase_prompt)
_showcase_default_variant = _bind_showcase_service(
    _showcase_preflight_service.showcase_default_variant
)
_showcase_pick_shot_variants = _bind_showcase_service(
    _showcase_preflight_service.showcase_pick_shot_variants
)
_composition_shooting_brief = _bind_showcase_service(
    _showcase_preflight_service.composition_shooting_brief
)
_guarded_shooting_brief = _bind_showcase_service(
    _showcase_preflight_service.guarded_shooting_brief
)
_preserve_safe_motion_rewrite_instruction = _bind_showcase_service(
    _showcase_preflight_service.preserve_safe_motion_rewrite_instruction
)
_rewrite_instruction_replaces_scene_or_composition = _bind_showcase_service(
    _showcase_preflight_service.rewrite_instruction_replaces_scene_or_composition
)
_prepare_showcase_preflight_impl = _bind_showcase_service(
    _showcase_preflight_service.prepare_showcase_preflight_impl
)
_showcase_request_input_json = _bind_showcase_service(
    _showcase_preflight_service.showcase_request_input_json
)
_showcase_generation_context = _bind_showcase_service(
    _showcase_preflight_service.showcase_generation_context
)
_prepare_durable_showcase_preflight = _bind_showcase_service(
    _showcase_preflight_service.prepare_durable_showcase_preflight
)

__all__ = [
    "ADULT_POOL",
    "CHILD_POOL",
    "MODEL_LIBRARY_AGE_SEGMENTS",
    "MODEL_LIBRARY_APPEARANCES",
    "MODEL_LIBRARY_FETCH_TIMEOUT_SECONDS",
    "MODEL_LIBRARY_FOLDER_BY_AGE",
    "MODEL_LIBRARY_GENDER_SEGMENTS",
    "MODEL_LIBRARY_GENERATE_COUNTS",
    "MODEL_LIBRARY_GENERATE_STEP_KEY",
    "MODEL_LIBRARY_GENERATE_WORKER_ACTION",
    "MODEL_LIBRARY_IMAGE_SUFFIXES",
    "MODEL_LIBRARY_MAX_BINARY_BYTES",
    "MODEL_LIBRARY_MAX_GITHUB_DEPTH",
    "MODEL_LIBRARY_MAX_GITHUB_DIRECTORIES",
    "MODEL_LIBRARY_MAX_GITHUB_FILES",
    "MODEL_LIBRARY_MAX_GITHUB_METADATA_BYTES",
    "MODEL_LIBRARY_MAX_GITHUB_RESPONSE_BYTES",
    "MODEL_LIBRARY_MAX_INDEX_BYTES",
    "MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES",
    "MODEL_LIBRARY_ROOT_KEY",
    "MODEL_LIBRARY_SCHEMA_VERSION",
    "MODEL_LIBRARY_SOURCES",
    "MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS",
    "MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS",
    "MODEL_LIBRARY_SYNC_LEASE_SECONDS",
    "MODEL_LIBRARY_SYNC_MODES",
    "MODEL_LIBRARY_SYNC_PROXY_NAME_KEY",
    "MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS",
    "MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY",
    "POSTER_DEFAULT_TARGET_ASPECTS",
    "POSTER_MASTER_ASPECT",
    "POSTER_WORKFLOW_STEPS",
    "POSTER_WORKFLOW_TYPE",
    "ReferenceProfile",
    "SCENE_ENVIRONMENT_TEMPLATES",
    "SHOT_CLASS_ORDER",
    "SHOT_POOL_BY_BAND",
    "ShotClass",
    "ShotPool",
    "ShotVariant",
    "TEMPLATE_LABELS",
    "TODDLER_POOL",
    "Template",
    "WORKFLOW_STEPS",
    "WORKFLOW_TYPE",
    "WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE",
    "_AGE_ALIASES_API",
    "_FACE_ARCHETYPES_FEMALE",
    "_FACE_ARCHETYPES_MALE",
    "_GITHUB_API_HOST",
    "_GITHUB_RAW_HOSTS",
    "_LIFESTYLE_TEMPLATES",
    "_MODEL_LIBRARY_TITLE_AGE_LABELS",
    "_ModelLibrarySyncLeaseLost",
    "_ModelLibrarySyncLimitExceeded",
    "_POSE_DIRECTIONS",
    "_PublishBundle",
    "_RENDER_DIRECTIONS",
    "_RENDER_DIRECTIONS_OUTDOOR",
    "_SQUARE_OR_LANDSCAPE_RATIOS",
    "_STATIC_REWRITE_REPLACEMENTS",
    "_SYNC_LOCK",
    "_ShowcasePreflightProgressHook",
    "_accessory_age_direction",
    "_accessory_plan_from_product_analysis",
    "_accessory_preview_image_params",
    "_accessory_preview_prompt",
    "_accessory_strength_direction",
    "_add_user_library_item",
    "_age_direction",
    "_age_segment_from_folder_name",
    "_age_soft_constraint",
    "_api_call_tagging_upstream",
    "_attach_workflow_assets",
    "_auto_tag_library_item",
    "_build_garment_lock",
    "_build_run_out",
    "_cached_sync_response",
    "_can_sync_library",
    "_cancel_workflow_generation_rows",
    "_candidate_image_params",
    "_candidate_prompt",
    "_candidate_reference_image_ids",
    "_claim_library_sync_lease",
    "_claim_library_sync_lease_sync",
    "_cleanup_string_list",
    "_coerce_accessory_plan_payload",
    "_combined_library_items",
    "_compact_lock_text",
    "_compact_product_identity",
    "_compact_showcase_user_direction",
    "_complete_library_sync_lease",
    "_complete_library_sync_lease_sync",
    "_compose_image_prompt_with_gpt55",
    "_composition_shooting_brief",
    "_create_poster_workflow_task",
    "_create_user_image_from_preset",
    "_create_workflow_task",
    "_decoded_url_path_segments",
    "_default_library_index",
    "_default_sync_state",
    "_default_user_library_index",
    "_delete_apparel_model_library_item_for_user",
    "_do_poster_inpaint",
    "_do_sync_library_presets",
    "_empty_workflow_generated_cleanup",
    "_enqueue_model_library_generate_tasks",
    "_ensure_legacy_user_library_migrated",
    "_extract_bonus_ids",
    "_fail_library_sync_lease",
    "_fail_library_sync_lease_sync",
    "_fallback_risk_review",
    "_fetch_bytes",
    "_fetch_github_download_bytes",
    "_filter_library_items",
    "_find_library_item",
    "_fixed_size_for_quality",
    "_fsync_dir",
    "_gather_job_image_outs",
    "_gender_from_folder_name",
    "_get_or_create_workflow_conversation",
    "_get_owned_conversation",
    "_get_run",
    "_github_api_child_url",
    "_github_contents_url",
    "_github_entry_size",
    "_guarded_shooting_brief",
    "_guess_mime",
    "_height_requirement",
    "_hide_preset_in_legacy_user_library_index",
    "_image_out_map",
    "_image_params",
    "_image_to_out",
    "_image_url",
    "_infer_age",
    "_infer_age_segment_from_text",
    "_infer_age_segment_from_workflow",
    "_infer_candidate_gender",
    "_infer_model_height_cm",
    "_is_child_showcase",
    "_job_from_library_run",
    "_job_from_project_candidate_step",
    "_job_item_out",
    "_join_lock_items",
    "_legacy_library_item_insert_values",
    "_library_binary_response",
    "_library_index_path",
    "_library_item_url",
    "_library_root",
    "_library_sync_lock_path",
    "_library_sync_state_path",
    "_library_user_index_path",
    "_list_item_from_run",
    "_load_global_library_index",
    "_load_steps",
    "_load_user_hidden_preset_ids",
    "_load_user_library_index",
    "_load_user_library_items",
    "_merge_image_workflow_asset_metadata",
    "_merge_product_corrections",
    "_merge_reference_overrides",
    "_merge_workflow_asset_metadata",
    "_metadata_from_github_file",
    "_metadata_model_profile_from_prompt",
    "_model_diversity_anchor",
    "_model_library_download_filename",
    "_model_library_explicit_genders",
    "_model_library_folder_for_age",
    "_model_library_gender_label",
    "_model_library_generate_genders",
    "_model_library_generate_image_params",
    "_model_library_generate_prompt",
    "_model_library_http_client_kwargs",
    "_model_library_image_meta_by_id",
    "_model_library_image_metadata_from_fields",
    "_model_library_item_out",
    "_model_library_job_status",
    "_model_library_row_to_dict",
    "_model_library_run_inputs",
    "_model_library_run_title",
    "_model_library_sync_file_lock",
    "_next_action_for",
    "_normalize_age_segment",
    "_normalize_appearance",
    "_normalize_model_gender",
    "_normalize_tagged_age",
    "_normalize_tagged_gender",
    "_open_library_storage_file",
    "_owned_image",
    "_plan_scene_cards_with_gpt55",
    "_post_commit_workflow_generated_cleanup",
    "_poster_brand_assets_block",
    "_poster_brand_attachment_ids",
    "_poster_copy_analysis_prompt",
    "_poster_find_preset_item",
    "_poster_image_params",
    "_poster_layout_safe_area",
    "_poster_load_style",
    "_poster_master_image_params",
    "_poster_master_prompt",
    "_poster_merge_copy_corrections",
    "_poster_parse_copy_analysis_text",
    "_poster_render_prompt",
    "_poster_revision_prompt",
    "_poster_seed_steps",
    "_poster_selected_master",
    "_poster_style_from_preset",
    "_poster_style_summary",
    "_poster_text_fields_block",
    "_prepare_durable_showcase_preflight",
    "_prepare_showcase_preflight_impl",
    "_preserve_safe_motion_rewrite_instruction",
    "_preset_id_from_path",
    "_preset_storage_key",
    "_preset_thumb_storage_key",
    "_primary_candidate_image_id",
    "_product_analysis_prompt",
    "_publish_bundles",
    "_read_file_bytes_bounded",
    "_read_json_file",
    "_reference_profile_has_required_text_fields",
    "_release_soft_deleted_task_hold",
    "_release_workflow_generation_queue_state",
    "_remove_user_library_item_from_legacy_index",
    "_renew_library_sync_lease",
    "_renew_library_sync_lease_sync",
    "_resolve_model_library_sync_proxy",
    "_resolve_pool_band",
    "_resolve_scene_provider_order",
    "_review_prompt_risk_with_gpt55",
    "_revision_prompt",
    "_rewrite_instruction_replaces_scene_or_composition",
    "_rules_fallback_scene_planning",
    "_run_auto_tag_in_background",
    "_save_global_library_index",
    "_save_sync_state",
    "_save_user_library_index",
    "_saved_image_id_set",
    "_scene_environment_outdoor_phrase",
    "_scene_fingerprint",
    "_seed_steps",
    "_select_shot_variants",
    "_sha256_file_bounded",
    "_shot_class_distribution",
    "_showcase_composition_direction",
    "_showcase_default_variant",
    "_showcase_framing_direction",
    "_showcase_garment_lock_prefix",
    "_showcase_generation_context",
    "_showcase_pick_shot_variants",
    "_showcase_pose_direction",
    "_showcase_prompt",
    "_showcase_prompt_brief",
    "_showcase_reference_image_ids",
    "_showcase_render_direction",
    "_showcase_request_input_json",
    "_showcase_scene_card_action_direction",
    "_showcase_scene_card_camera_direction",
    "_showcase_scene_card_direction",
    "_showcase_scene_card_scene_direction",
    "_showcase_scene_card_text",
    "_showcase_scene_framing_direction",
    "_showcase_scene_label",
    "_showcase_scene_render_direction",
    "_showcase_target_image_count",
    "_showcase_visibility_policy",
    "_soft_delete_workflow_generated_images",
    "_step",
    "_stream_file",
    "_style_region_from_text",
    "_sync_lease_owner",
    "_sync_library_presets_from_github_folder",
    "_sync_mode",
    "_sync_poster_workflow_outputs",
    "_sync_state_out",
    "_template_requirement",
    "_text_has_any",
    "_title_from_preset_id",
    "_truncate_prompt_text",
    "_validate_accessory_preview_image",
    "_validate_github_contents_url",
    "_validate_github_download_url",
    "_validate_owned_images",
    "_walk_github_contents",
    "_workflow_asset_key",
    "_workflow_asset_records",
    "_workflow_direct_image_ids",
    "_workflow_direct_task_ids",
    "_workflow_generation_rows_from_task_ids",
    "_workflow_produced_model_image_ids",
    "_workflow_steps_and_candidates",
    "_workflow_wallet_exists",
    "_write_bytes_replace",
    "_write_json_atomic",
    "approve_copy_analysis",
    "approve_poster_master",
    "approve_product_analysis",
    "auto_tag_apparel_model_library_item",
    "batch_delete_apparel_model_library_items",
    "clear_apparel_model_library_jobs",
    "create_apparel_model_library_item",
    "create_apparel_model_showcase",
    "create_model_candidates",
    "create_poster_design_workflow",
    "create_poster_masters",
    "create_poster_renders",
    "delete_apparel_model_library_item",
    "delete_apparel_model_library_job",
    "generate_apparel_model_library_job",
    "get_apparel_model_library_item_binary",
    "get_apparel_model_library_item_thumb",
    "inpaint_poster_render",
    "list_apparel_model_library",
    "list_apparel_model_library_jobs",
    "patch_apparel_model_library_item",
    "revise_poster_render",
    "save_apparel_model_library_job_item",
    "select_apparel_model_library_item",
    "sync_apparel_model_library_presets",
]
