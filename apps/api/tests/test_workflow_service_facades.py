from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from lumen_core.schemas import ApparelModelLibrarySyncOut

from app.routes import workflows
from app.workflow_services import (
    library_github,
    library_items,
    library_lease,
    library_materialization,
    library_runtime,
    library_storage,
    library_sync,
    library_sync_operation,
    showcase_context,
    showcase_inputs,
    showcase_orchestration,
    showcase_preflight,
    showcase_prompts,
    showcase_runtime,
    showcase_scene_policy,
    showcase_shots,
)


def test_library_service_facade_exports_remain_compatible() -> None:
    bound_names = (
        "_write_json_atomic",
        "_fsync_dir",
        "_read_file_bytes_bounded",
        "_read_json_file",
        "_library_root",
        "_library_index_path",
        "_library_sync_state_path",
        "_library_sync_lock_path",
        "_library_user_index_path",
        "_default_library_index",
        "_default_user_library_index",
        "_default_sync_state",
        "_github_contents_url",
        "_sync_mode",
        "_model_library_http_client_kwargs",
        "_resolve_model_library_sync_proxy",
        "_can_sync_library",
        "_sync_state_out",
        "_metadata_from_github_file",
        "_model_library_item_out",
        "_load_global_library_index",
        "_load_user_library_index",
        "_save_global_library_index",
        "_save_user_library_index",
        "_remove_user_library_item_from_legacy_index",
        "_hide_preset_in_legacy_user_library_index",
        "_save_sync_state",
        "_model_library_row_to_dict",
        "_legacy_library_item_insert_values",
        "_load_user_library_items",
        "_load_user_hidden_preset_ids",
        "_combined_library_items",
        "_filter_library_items",
        "_guess_mime",
        "_sha256_file_bounded",
        "_open_library_storage_file",
        "_stream_file",
        "_library_binary_response",
        "_preset_storage_key",
        "_preset_thumb_storage_key",
        "_write_bytes_replace",
        "_fetch_bytes",
        "_fetch_github_download_bytes",
        "_github_api_child_url",
        "_decoded_url_path_segments",
        "_validate_github_contents_url",
        "_validate_github_download_url",
        "_github_entry_size",
        "_sync_lease_owner",
        "_claim_library_sync_lease_sync",
        "_claim_library_sync_lease",
        "_renew_library_sync_lease_sync",
        "_renew_library_sync_lease",
        "_complete_library_sync_lease_sync",
        "_complete_library_sync_lease",
        "_fail_library_sync_lease_sync",
        "_fail_library_sync_lease",
        "_cached_sync_response",
        "_do_sync_library_presets",
        "_create_user_image_from_preset",
        "_walk_github_contents",
        "_sync_library_presets_from_github_folder",
        "_ensure_legacy_user_library_migrated",
        "_find_library_item",
        "_owned_image",
        "_image_url",
        "_model_library_download_filename",
        "_model_library_image_metadata_from_fields",
        "_add_user_library_item",
    )
    for name in bound_names:
        facade = getattr(workflows, name)
        service = getattr(library_sync, name)
        assert inspect.unwrap(facade) is service
        assert inspect.signature(facade) == inspect.signature(service)

    assert (
        workflows._ModelLibrarySyncLimitExceeded
        is library_sync._ModelLibrarySyncLimitExceeded
    )
    assert (
        workflows._ModelLibrarySyncLeaseLost is library_sync._ModelLibrarySyncLeaseLost
    )


def test_showcase_service_facade_exports_remain_compatible() -> None:
    bound_names = (
        "_showcase_prompt_brief",
        "_showcase_reference_image_ids",
        "_validate_accessory_preview_image",
        "_showcase_target_image_count",
        "_validate_owned_images",
        "_seed_steps",
        "_product_analysis_prompt",
        "_candidate_prompt",
        "_showcase_scene_label",
        "_showcase_scene_card_direction",
        "_showcase_scene_card_scene_direction",
        "_showcase_scene_card_action_direction",
        "_showcase_scene_card_camera_direction",
        "_showcase_scene_card_text",
        "_text_has_any",
        "_is_child_showcase",
        "_showcase_scene_render_direction",
        "_showcase_scene_framing_direction",
        "_showcase_visibility_policy",
        "_truncate_prompt_text",
        "_join_lock_items",
        "_compact_lock_text",
        "_compact_product_identity",
        "_showcase_garment_lock_prefix",
        "_showcase_prompt",
        "_showcase_default_variant",
        "_showcase_pick_shot_variants",
        "_composition_shooting_brief",
        "_guarded_shooting_brief",
        "_preserve_safe_motion_rewrite_instruction",
        "_rewrite_instruction_replaces_scene_or_composition",
        "_showcase_request_input_json",
        "_prepare_durable_showcase_preflight",
        "_prepare_showcase_preflight_impl",
        "_showcase_generation_context",
    )
    for name in bound_names:
        facade = getattr(workflows, name)
        service = getattr(showcase_preflight, name)
        assert inspect.unwrap(facade) is service
        assert inspect.signature(facade) == inspect.signature(service)

    assert workflows.ShotVariant is showcase_preflight.ShotVariant
    assert workflows.CHILD_POOL is showcase_preflight.CHILD_POOL
    assert workflows.TODDLER_POOL is showcase_preflight.TODDLER_POOL


def test_library_service_exports_are_direct_submodule_aliases() -> None:
    module_exports = {
        library_storage: (
            "_write_json_atomic",
            "_read_json_file",
            "_load_global_library_index",
            "_remove_user_library_item_from_legacy_index",
            "_library_binary_response",
            "_write_bytes_replace",
        ),
        library_items: (
            "_resolve_model_library_sync_proxy",
            "_model_library_item_out",
            "_ensure_legacy_user_library_migrated",
            "_find_library_item",
        ),
        library_github: (
            "_fetch_bytes",
            "_walk_github_contents",
            "_metadata_from_github_file",
        ),
        library_lease: (
            "_claim_library_sync_lease_sync",
            "_complete_library_sync_lease_sync",
            "_fail_library_sync_lease_sync",
        ),
        library_sync_operation: (
            "_sync_library_presets_from_github_folder",
            "_do_sync_library_presets",
        ),
        library_materialization: (
            "_create_user_image_from_preset",
            "_add_user_library_item",
        ),
    }
    for module, names in module_exports.items():
        for name in names:
            assert getattr(library_sync, name) is getattr(module, name)

    assert library_sync.FACADE_RUNTIME is library_runtime.FACADE_RUNTIME
    assert library_sync._runtime is library_runtime.runtime
    assert (
        library_sync._ModelLibrarySyncLimitExceeded
        is library_github._ModelLibrarySyncLimitExceeded
    )
    assert (
        library_sync._ModelLibrarySyncLeaseLost
        is library_lease._ModelLibrarySyncLeaseLost
    )


def test_showcase_service_exports_are_direct_submodule_aliases() -> None:
    module_exports = {
        showcase_inputs: (
            "_showcase_reference_image_ids",
            "_validate_owned_images",
            "_seed_steps",
            "_candidate_prompt",
        ),
        showcase_scene_policy: (
            "_showcase_scene_card_direction",
            "_showcase_visibility_policy",
            "_compact_lock_text",
        ),
        showcase_prompts: (
            "_showcase_prompt_brief",
            "_showcase_prompt",
            "_guarded_shooting_brief",
        ),
        showcase_shots: (
            "_showcase_default_variant",
            "_showcase_pick_shot_variants",
        ),
        showcase_orchestration: ("_prepare_showcase_preflight_impl",),
        showcase_context: (
            "_showcase_request_input_json",
            "_showcase_generation_context",
            "_prepare_durable_showcase_preflight",
        ),
    }
    for module, names in module_exports.items():
        for name in names:
            assert getattr(showcase_preflight, name) is getattr(module, name)

    assert showcase_preflight.FACADE_RUNTIME is showcase_runtime.FACADE_RUNTIME
    assert showcase_preflight._runtime is showcase_runtime.runtime
    assert (
        showcase_preflight._STATIC_REWRITE_REPLACEMENTS
        is showcase_prompts._STATIC_REWRITE_REPLACEMENTS
    )


def test_library_service_direct_runtime_honors_module_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "items": [
                    {"id": "user:keep", "image_id": "img-keep"},
                    {"id": "user:remove", "image_id": "img-remove"},
                ],
                "hidden_preset_ids": [],
            }
        ),
        "utf-8",
    )
    monkeypatch.setattr(
        library_sync,
        "_library_user_index_path",
        lambda _user_id: index_path,
    )

    assert library_sync._remove_user_library_item_from_legacy_index(
        "user-1",
        "user:remove",
    )
    saved = json.loads(index_path.read_text("utf-8"))
    assert [item["id"] for item in saved["items"]] == ["user:keep"]

    monkeypatch.setattr(library_sync, "MODEL_LIBRARY_MAX_INDEX_BYTES", 4)
    with pytest.raises(HTTPException) as excinfo:
        library_sync._read_json_file(index_path, {})
    assert excinfo.value.detail["error"]["code"] == "invalid_index"


@pytest.mark.asyncio
async def test_library_sync_service_direct_entry_uses_patched_collaborators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = library_sync._default_sync_state()

    async def fake_claim() -> tuple[str, dict[str, Any]]:
        return "lease-token", state

    async def fake_sync(
        contents_url: str,
        claimed_state: dict[str, Any],
        *,
        proxy_url: str | None,
        lease_token: str | None,
    ) -> ApparelModelLibrarySyncOut:
        assert contents_url.endswith("apparel-model-presets?ref=main")
        assert claimed_state is state
        assert proxy_url is None
        assert lease_token == "lease-token"
        return ApparelModelLibrarySyncOut(status="ok")

    monkeypatch.setattr(library_sync, "_claim_library_sync_lease", fake_claim)
    monkeypatch.setattr(library_sync, "_do_sync_library_presets", fake_sync)

    result = await library_sync._sync_library_presets_from_github_folder(
        "https://api.github.com/repos/cyeinfpro/Lumen/contents/"
        "assets/apparel-model-presets?ref=main"
    )

    assert result.status == "ok"


@pytest.mark.asyncio
async def test_showcase_preflight_service_runs_directly() -> None:
    candidate = SimpleNamespace(
        id="candidate-1",
        model_brief_json={"summary": "自然通勤模特", "height_cm": 168},
    )
    shot_picks = showcase_preflight._showcase_pick_shot_variants(
        template="urban_commute",
        age_segment="young_adult",
        output_count=1,
        seed_key="direct-service",
    )

    result = await showcase_preflight._prepare_showcase_preflight_impl(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        product_analysis={"category": "衬衫", "must_preserve": ["蓝色", "胸袋"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="urban_commute",
        shot_picks=shot_picks,
        age_segment="young_adult",
        final_quality="high",
        user_prompt="自然街拍",
        aspect_ratio="4:5",
        scene_environment="outdoor",
        scene_strategy="natural_series",
        scene_variety="balanced",
        scene_planner="rules_fallback",
        continuity_anchor="accessory",
        allow_pet=False,
        allow_background_people=True,
    )

    assert result["planning"]["planner"] == "rules_fallback"
    assert len(result["scene_cards"]) == 1
    assert len(result["final_prompts"]) == 1
    assert "【商品 1:1 锁定】" in result["final_prompts"][0]
