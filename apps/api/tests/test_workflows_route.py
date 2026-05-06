from __future__ import annotations

import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.routes import workflows
from lumen_core.schemas import ModelCandidatesCreateIn


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self.rows

    def scalar_one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None


class _Db:
    def __init__(self, rows: list[Any], responses: list[list[Any]] | None = None) -> None:
        self.rows = rows
        self.responses = responses
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.flushed = False
        self.committed = False

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        if self.responses is not None:
            rows = self.responses.pop(0)
        else:
            rows = self.rows
        return _Result(rows)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.committed = True


def test_model_candidates_mvp_requires_three_candidates() -> None:
    body = ModelCandidatesCreateIn(
        candidate_count=3,
        style_prompt="premium",
        accessory_plan={"enabled": True, "items": ["white sneakers"], "strength": "subtle"},
    )

    assert body.accessory_plan.items == ["white sneakers"]

    with pytest.raises(ValidationError):
        ModelCandidatesCreateIn(candidate_count=2, style_prompt="premium")


def test_github_folder_metadata_uses_directory_and_filename() -> None:
    item = workflows._metadata_from_github_file(  # noqa: SLF001
        {
            "type": "file",
            "name": "adult-asian-minimal-studio-001.png",
            "path": "assets/apparel-model-presets/05_adult/female/adult-asian-minimal-studio-001.png",
            "download_url": "https://raw.githubusercontent.com/cyeinfpro/Lumen/main/assets/apparel-model-presets/05_adult/female/adult-asian-minimal-studio-001.png",
            "sha": "abc",
        }
    )

    assert item is not None
    assert item["preset_id"] == "adult-asian-minimal-studio-001"
    assert item["age_segment"] == "adult"
    assert item["library_folder"] == "05_adult/female"
    assert item["gender"] == "female"
    assert item["appearance_direction"] == "asian"
    assert "minimal" in item["style_tags"]


def test_github_folder_metadata_accepts_jpg_and_webp() -> None:
    for suffix in ("jpg", "webp"):
        item = workflows._metadata_from_github_file(  # noqa: SLF001
            {
                "type": "file",
                "name": f"adult-minimal-studio-001.{suffix}",
                "path": f"assets/apparel-model-presets/05_adult/male/adult-minimal-studio-001.{suffix}",
                "download_url": f"https://example.invalid/adult-minimal-studio-001.{suffix}",
            }
        )

        assert item is not None
        assert item["age_segment"] == "adult"
        assert item["gender"] == "male"
        assert item["library_folder"] == "05_adult/male"


def test_preset_title_uses_updated_age_labels() -> None:
    assert workflows._title_from_preset_id("adult-female-001").startswith("熟龄 女性")
    assert workflows._title_from_preset_id("middle-aged-male-001").startswith("中年 男性")


def test_github_folder_metadata_ignores_thumb_files() -> None:
    item = workflows._metadata_from_github_file(  # noqa: SLF001
        {
            "type": "file",
            "name": "adult-female-001.thumb.webp",
            "path": "assets/apparel-model-presets/05_adult/female/adult-female-001.thumb.webp",
            "download_url": "https://example.invalid/thumb.webp",
        }
    )

    assert item is None


def test_model_library_generated_source_round_trips_and_filters() -> None:
    raw = {
        "id": "user:generated-1",
        "source": "generated",
        "title": "Generated model",
        "age_segment": "young_adult",
        "gender": "female",
        "appearance_direction": "asian",
        "style_tags": ["studio"],
        "image_id": "img-1",
        "created_at": "2026-01-01T00:00:00Z",
    }

    out = workflows._model_library_item_out(raw)  # noqa: SLF001
    assert out.source == "generated"

    filtered = workflows._filter_library_items(  # noqa: SLF001
        [raw],
        source="generated",
        age_segment="all",
        appearance="all",
        q="",
    )
    assert filtered == [raw]
    assert "generated" in workflows.MODEL_LIBRARY_SOURCES


def test_model_library_folder_helpers_support_numbered_age_dirs() -> None:
    assert workflows._normalize_age_segment("05_adult") == "adult"  # noqa: SLF001
    assert workflows._normalize_age_segment("04_young_adult") == "young_adult"  # noqa: SLF001
    assert workflows._model_library_folder_for_age("senior", "male") == "07_senior/male"  # noqa: SLF001
    assert workflows._model_library_folder_for_age("bad", "female") == "00_user_favorites/female"  # noqa: SLF001
    assert workflows._model_library_folder_for_age("adult", "bad") == "05_adult/female"  # noqa: SLF001


def test_primary_candidate_image_prefers_contact_sheet_then_candidate_ids() -> None:
    assert (
        workflows._primary_candidate_image_id(  # noqa: SLF001
            SimpleNamespace(contact_sheet_image_id="sheet", model_brief_json={})
        )
        == "sheet"
    )
    assert (
        workflows._primary_candidate_image_id(  # noqa: SLF001
            SimpleNamespace(
                contact_sheet_image_id=None,
                model_brief_json={"candidate_image_ids": ["first", "second"]},
            )
        )
        == "first"
    )
    assert (
        workflows._primary_candidate_image_id(  # noqa: SLF001
            SimpleNamespace(contact_sheet_image_id=None, model_brief_json={})
        )
        is None
    )


@pytest.mark.asyncio
async def test_workflow_produced_model_image_ids_includes_dual_race_bonus_ids() -> None:
    step = SimpleNamespace(
        image_ids=["winner-img"],
        task_ids=[],
        output_json={"dual_race_bonus_image_ids": ["bonus-img", "winner-img"]},
    )

    produced = await workflows._workflow_produced_model_image_ids(  # noqa: SLF001
        _Db([]),  # type: ignore[arg-type]
        user_id="user-1",
        steps=[step],  # type: ignore[list-item]
    )

    assert produced == {"winner-img", "bonus-img"}


@pytest.mark.asyncio
async def test_workflow_produced_model_image_ids_pulls_from_owner_generation_subquery() -> None:
    # task_ids 非空 → 触发反向 SQL 查询：把 worker 还没回写到 step.image_ids
    # 但 owner_generation_id 已经指向 task_ids 或 dual_race bonus generation 的图也算「produced」。
    from sqlalchemy.dialects import postgresql

    step = SimpleNamespace(
        image_ids=[],
        task_ids=["task-a", "task-b"],
        output_json={},
    )
    db = _Db(["bonus-img-from-sql", "winner-img-from-sql"])

    produced = await workflows._workflow_produced_model_image_ids(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        user_id="user-1",
        steps=[step],  # type: ignore[list-item]
    )

    assert produced == {"bonus-img-from-sql", "winner-img-from-sql"}
    assert len(db.statements) == 1
    rendered = str(
        db.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    # 主查询：images 受 user_id / deleted_at / owner_generation_id 限定。
    assert "images.user_id = 'user-1'" in rendered
    assert "images.deleted_at IS NULL" in rendered
    assert "images.owner_generation_id IN ('task-a', 'task-b')" in rendered
    # 子查询：通过 generations.upstream_request 反查 dual_race bonus generation 产出的图。
    assert "FROM generations" in rendered
    assert "generations.user_id = 'user-1'" in rendered
    assert "(generations.upstream_request ->> 'parent_generation_id') IN ('task-a', 'task-b')" in rendered
    # 注意：as_boolean() 在 PostgreSQL 上编译成 CAST(text AS BOOLEAN)，
    # 这样 worker 不论写 JSON true 还是字符串 "true" 都能被 cast 命中。
    assert "CAST((generations.upstream_request ->> 'is_dual_race_bonus') AS BOOLEAN) IS true" in rendered


@pytest.mark.asyncio
async def test_workflow_produced_model_image_ids_skips_sql_when_no_task_ids() -> None:
    step = SimpleNamespace(image_ids=["only-img"], task_ids=[], output_json={})
    db = _Db(["should-not-appear"])

    produced = await workflows._workflow_produced_model_image_ids(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        user_id="user-1",
        steps=[step],  # type: ignore[list-item]
    )

    assert produced == {"only-img"}
    assert db.statements == []


def test_default_github_contents_url_points_to_user_repo_folder() -> None:
    assert workflows._github_contents_url().startswith(  # noqa: SLF001
        "https://api.github.com/repos/cyeinfpro/Lumen/contents/assets/apparel-model-presets"
    )


@pytest.mark.asyncio
async def test_apparel_model_library_jobs_respects_offset_and_has_more(monkeypatch) -> None:
    async def fake_ensure_legacy_user_library_migrated(_db, _user_id):
        return False

    async def fake_saved_image_id_set(_db, _user_id):
        return {}

    monkeypatch.setattr(
        workflows, "_ensure_legacy_user_library_migrated", fake_ensure_legacy_user_library_migrated
    )
    monkeypatch.setattr(workflows, "_saved_image_id_set", fake_saved_image_id_set)

    async def fake_library_job(_db, *, run, saved_map):
        ts_map = {
            "library-1": datetime(2026, 1, 3, tzinfo=timezone.utc),
            "library-2": datetime(2026, 1, 2, tzinfo=timezone.utc),
        }
        return workflows.ApparelModelLibraryJobOut(  # noqa: SLF001
            job_id=run.id,
            origin="library_generate",
            workflow_run_id=run.id,
            project_title=None,
            status="succeeded",
            requested_count=1,
            finished_count=1,
            age_segment=None,
            gender=None,
            appearance_direction=None,
            extra_requirements=None,
            items=[],
            candidates=[],
            error_message=None,
            created_at=ts_map[run.id],
            updated_at=ts_map[run.id],
        )

    async def fake_project_job(_db, *, run, step, saved_map):
        return workflows.ApparelModelLibraryJobOut(  # noqa: SLF001
            job_id=f"{run.id}:model_candidates",
            origin="project_candidate",
            workflow_run_id=run.id,
            project_title=run.title,
            status="succeeded",
            requested_count=1,
            finished_count=1,
            age_segment=None,
            gender=None,
            appearance_direction=None,
            extra_requirements=None,
            items=[],
            candidates=[],
            error_message=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(workflows, "_job_from_library_run", fake_library_job)
    monkeypatch.setattr(workflows, "_job_from_project_candidate_step", fake_project_job)

    library_runs = [
        SimpleNamespace(id="library-1"),
        SimpleNamespace(id="library-2"),
    ]
    candidate_rows = [
        (SimpleNamespace(id="project-1", title="Project 1"), SimpleNamespace(id="step-1")),
    ]
    expected_db = _Db([], responses=[library_runs, candidate_rows])
    expected_db_second = _Db([], responses=[library_runs, candidate_rows])

    async def run_page(db: _Db, offset: int) -> workflows.ApparelModelLibraryJobsOut:  # noqa: SLF001
        return await workflows.list_apparel_model_library_jobs(  # noqa: SLF001
            user=SimpleNamespace(id="user-1"),
            db=db,  # type: ignore[arg-type]
            limit=2,
            offset=offset,
        )

    first = await run_page(expected_db, 0)
    assert first.limit == 2
    assert first.offset == 0
    assert first.has_more is True
    assert [item.job_id for item in first.items] == ["library-1", "library-2"]

    second = await run_page(expected_db_second, 2)
    assert second.limit == 2
    assert second.offset == 2
    assert second.has_more is False
    assert [item.job_id for item in second.items] == ["project-1:model_candidates"]


@pytest.mark.asyncio
async def test_create_user_image_from_preset_copies_to_user_private_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(workflows.settings, "storage_root", str(tmp_path))
    source_key = "apparel-model-library/presets/adult-female/v1.png"
    source_path = tmp_path / source_key
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        )
    )
    db = _Db([])

    img = await workflows._create_user_image_from_preset(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        user_id="user-1",
        item={
            "id": "preset:adult-female:v1",
            "preset_id": "adult-female",
            "version": 1,
            "image_storage_key": source_key,
        },
    )

    assert db.flushed is True
    assert img.storage_key.startswith("u/user-1/apparel-model-library/")
    assert img.storage_key.endswith(".png")
    assert img.storage_key != source_key
    assert (tmp_path / img.storage_key).read_bytes() == source_path.read_bytes()
    assert img.metadata_jsonb["cached_from_storage_key"] == source_key
    assert img.metadata_jsonb["shared_storage"] is False


@pytest.mark.asyncio
async def test_validate_owned_images_accepts_one_to_three_owned_images() -> None:
    db = _Db(["img-1", "img-2"])

    out = await workflows._validate_owned_images(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        user_id="user-1",
        image_ids=["img-1", "img-2", "img-1"],
        min_count=1,
        max_count=3,
    )

    assert out == ["img-1", "img-2"]
    rendered = str(db.statements[0])
    assert "images.user_id" in rendered
    assert "images.deleted_at IS NULL" in rendered


@pytest.mark.asyncio
async def test_validate_owned_images_rejects_missing_or_foreign_image() -> None:
    db = _Db(["img-1"])

    with pytest.raises(Exception) as excinfo:
        await workflows._validate_owned_images(  # noqa: SLF001
            db,  # type: ignore[arg-type]
            user_id="user-1",
            image_ids=["img-1", "img-foreign"],
            min_count=1,
            max_count=3,
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "invalid_image"


def test_product_analysis_json_fallback_keeps_reviewable_constraints() -> None:
    out = workflows._try_parse_json_text("This looks like an ivory blazer.")  # noqa: SLF001

    assert out["category"] == "需人工复核"
    assert "This looks like an ivory blazer." in out["key_details"]
    assert "可见商品细节" in out["must_preserve"]
    assert out["summary_text"] == "This looks like an ivory blazer."


def test_product_analysis_json_parser_unwraps_content_envelope() -> None:
    out = workflows._try_parse_json_text(
        '{"content":{"text":"{\\"category\\":\\"衬衫\\",\\"color\\":\\"蓝色\\",'
        '\\"material\\":\\"棉\\",\\"silhouette\\":\\"宽松\\",'
        '\\"details\\":[\\"翻领\\"],\\"preserve\\":[\\"蓝色\\",\\"翻领\\"],'
        '\\"background\\":\\"明亮自然的日常随拍氛围\\"}"}}'
    )  # noqa: SLF001

    assert out["category"] == "衬衫"
    assert out["material_guess"] == "棉"
    assert out["key_details"] == ["翻领"]
    assert out["must_preserve"] == ["蓝色", "翻领"]
    assert out["background_recommendation"] == "明亮自然的日常随拍氛围"


def test_product_analysis_prompt_requests_styling_recommendations() -> None:
    prompt = workflows._product_analysis_prompt("8岁童装")  # noqa: SLF001

    assert "styling_recommendations" in prompt
    assert "background_recommendation" in prompt
    assert "只服务后续生成真人模特穿搭图" in prompt
    assert "1-3 个" in prompt
    assert "开放式背景氛围建议" in prompt
    assert "不要列具体地点或具体空间名" in prompt
    assert "低存在感" in prompt
    assert "must_preserve" in prompt
    assert "3-8 个" in prompt
    assert "必须只返回一个 JSON object" in prompt


def test_workflow_image_params_use_high_quality_jpeg() -> None:
    params = workflows._image_params(aspect_ratio="4:5", count=1, render_quality="high")  # noqa: SLF001

    assert params.output_format == "jpeg"
    assert params.output_compression == 100
    assert params.fast is False


def test_workflow_image_params_default_to_non_fast_high_quality_for_showcase() -> None:
    params = workflows._image_params(  # noqa: SLF001
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        final_quality="high",
    )

    assert params.fast is False
    assert params.render_quality == "high"
    assert params.fixed_size == "1600x2000"


def test_showcase_refs_use_product_images_when_no_accessory_preview() -> None:
    refs = workflows._showcase_reference_image_ids(  # noqa: SLF001
        product_image_ids=["product-1", "product-2"],
        model_image_id="model-1",
        selected_accessory_image_id=None,
    )

    assert refs == ["product-1", "product-2", "model-1"]


def test_showcase_refs_use_accessory_preview_instead_of_product_images() -> None:
    refs = workflows._showcase_reference_image_ids(  # noqa: SLF001
        product_image_ids=["product-1", "product-2"],
        model_image_id="model-1",
        selected_accessory_image_id="accessory-preview-1",
    )

    assert refs == ["product-1", "product-2", "accessory-preview-1"]
    assert "model-1" not in refs


def test_showcase_regeneration_target_keeps_existing_outputs() -> None:
    assert (
        workflows._showcase_target_image_count(  # noqa: SLF001
            existing_image_ids=["old-1", "old-2", "old-2"],
            output_count=4,
        )
        == 6
    )
    assert (
        workflows._showcase_expected_image_count(  # noqa: SLF001
            showcase_input={"target_image_count": 8, "output_count": 4},
            fallback_task_count=12,
        )
        == 8
    )


def test_candidate_prompt_uses_clean_four_view_reference_without_text_labels() -> None:
    prompt = workflows._candidate_prompt(  # noqa: SLF001
        style_prompt="premium natural model",
        product_analysis={"category": "连衣裙"},
        candidate_index=2,
        avoid=[],
    )

    assert "warm ivory sleeveless top" in prompt
    assert "warm ivory shorts" in prompt
    assert "Every candidate must wear this exact same outfit" in prompt
    assert "2x2 ecommerce model reference contact sheet" in prompt
    assert "exactly four panels" in prompt
    assert "front full body" in prompt
    assert "left 90-degree profile full body" in prompt
    assert "straight back full body" in prompt
    assert "close-up headshot" in prompt
    assert "same camera height and distance" in prompt
    assert "only one eye visible" in prompt
    assert "not a three-quarter pose" in prompt
    assert "Back panel must hide the face" in prompt
    assert "Plain seamless white or light gray studio background" in prompt
    assert "Real commercially photographed person" in prompt
    assert "No text labels" in prompt
    assert "no height labels" in prompt
    # diversity anchor 注入：不同 candidate_index 拿到不同 archetype
    assert "Look anchor for this candidate" in prompt


def test_candidate_image_params_use_lossless_png_reference() -> None:
    params = workflows._candidate_image_params()  # noqa: SLF001

    assert params.aspect_ratio == "4:5"
    assert params.size_mode == "fixed"
    assert params.fixed_size == "1600x2000"
    assert params.count == 1
    assert params.render_quality == "high"
    assert params.fast is False
    assert params.output_format == "png"
    assert params.output_compression is None
    assert params.background == "opaque"
    assert params.moderation == "low"


def test_age_direction_adapts_child_model_pose_and_expression() -> None:
    candidate_prompt = workflows._candidate_prompt(  # noqa: SLF001
        style_prompt="8岁儿童，活泼自然",
        product_analysis={"category": "童装"},
        candidate_index=1,
        avoid=[],
    )
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "8岁儿童，活泼自然"},
    )
    showcase_prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["颜色", "版型"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="lifestyle",
        shot_type="natural_pose",
        final_quality="high",
    )

    assert "around 8 years old" in candidate_prompt
    assert "around 128cm" in candidate_prompt
    assert "age-appropriate" in candidate_prompt
    assert "non-adultized" in candidate_prompt
    assert "模特图" in showcase_prompt
    assert "同一张脸" in showcase_prompt
    assert "身材比例" in showcase_prompt


def test_showcase_prompt_uses_user_direction_for_scene_and_action() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean cold commute model"},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={
            "must_preserve": ["lapel shape", "button position", "pocket placement"],
            "background_recommendation": "明亮松弛的日常随拍氛围",
        },
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={
            "enabled": True,
            "items": ["small earrings"],
            "strength": "subtle",
        },
        template="premium_studio",
        shot_type="front_full_body",
        final_quality="high",
        user_prompt="明亮松弛，自然走动回头",
    )

    assert "请根据这张白底产品图和模特图" in prompt
    assert "生成真实自然的真人模特穿搭图" in prompt
    assert "要求：" in prompt
    assert "明亮松弛" in prompt
    assert "走动" not in prompt
    assert "回头" not in prompt
    assert "明亮松弛的日常随拍氛围" in prompt
    assert "重点保留：lapel shape、button position、pocket placement" in prompt
    assert "少量自然搭配" in prompt
    assert "small earrings" not in prompt
    assert "优先参考它" in prompt
    assert "不要抢衣服主体" in prompt
    assert "超写实" in prompt
    assert "商业摄影" in prompt
    assert "适合亚马逊电商主图" in prompt
    assert "全身照" in prompt
    assert "正面全身" in prompt
    assert "欧美风格" in prompt
    assert "无文字水印" in prompt
    assert len(prompt) < 650


def test_showcase_prompt_preserves_model_identity_height_and_limb_proportions() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "8岁儿童，活泼自然", "height_cm": 128},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["颜色", "版型"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="lifestyle",
        shot_type="natural_pose",
        final_quality="high",
    )

    assert "同一张脸" in prompt
    assert "发型" in prompt
    assert "身材比例" in prompt
    assert "不要换人" in prompt
    assert "身高约" not in prompt


def test_showcase_prompt_uses_quality_mode_variable() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean ecommerce model", "height_cm": 168},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="white_ecommerce",
        shot_type="front_full_body",
        final_quality="4k",
    )

    assert "画质：4K 终稿" in prompt
    assert "少量自然搭配" in prompt
    assert "不要抢衣服主体" in prompt


def test_showcase_prompt_respects_explicit_non_european_style_region() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "亚洲女性，自然电商模特", "height_cm": 168},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": []},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="premium_studio",
        shot_type="front_full_body",
        final_quality="high",
    )

    assert "亚洲风格" in prompt
    assert "重点保留：颜色、版型、款式" in prompt


def test_showcase_prompts_assign_distinct_actions_per_shot() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model", "height_cm": 168},
    )
    prompts = {
        shot: workflows._showcase_prompt(  # noqa: SLF001
            product_analysis={},
            selected_candidate=candidate,  # type: ignore[arg-type]
            accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
            template="premium_studio",
            shot_type=shot,
            final_quality="high",
        )
        for shot in workflows.DEFAULT_SHOT_PLAN
    }

    assert "正面全身" in prompts["front_full_body"]
    assert "姿势生动活泼有活力" in prompts["natural_pose"]
    assert "姿态自由不死板" in prompts["natural_pose"]
    assert "自然全身展示" in prompts["detail_half_body"]
    assert "姿态自然不重复" in prompts["detail_half_body"]
    assert "另一张" not in prompts["detail_half_body"]
    assert "侧面" in prompts["side_or_back"]
    assert "背面" not in prompts["side_or_back"]
    for prompt in prompts.values():
        assert "走动" not in prompt
        assert "回头" not in prompt
        assert "扶袖口" not in prompt
    assert len(set(prompts.values())) == len(workflows.DEFAULT_SHOT_PLAN)


def test_accessory_preview_prompt_is_model_quad_with_accessories_only() -> None:
    prompt = workflows._accessory_preview_prompt(  # noqa: SLF001
        accessory_plan={"items": ["small earrings", "white sneakers"], "strength": "subtle"},
        style_prompt="natural clean styling",
    )

    assert "已确认模特四宫格参考图" in prompt
    assert "白底模特配饰四宫格参考图" in prompt
    assert "2x2 四宫格参考图" in prompt
    assert "正面全身、侧面全身、背面全身、近景头像" in prompt
    assert "左上正面全身、右上侧面全身、左下背面全身、右下近景头像" in prompt
    assert "不要穿商品图中的衣服" in prompt
    assert "不要出现任何商品服饰" in prompt
    assert "只添加这些配饰：small earrings、white sneakers" in prompt
    assert "不要自动新增未列出的包、帽子、腰带、眼镜、首饰、鞋子或道具" in prompt
    assert "耳饰在耳垂位置" in prompt
    assert "不能漂浮、变形、穿模" in prompt
    assert "不要让配饰遮挡未来商品展示区域" in prompt
    assert "natural clean styling" in prompt


def test_accessory_preview_prompt_adapts_child_accessory_styling() -> None:
    prompt = workflows._accessory_preview_prompt(  # noqa: SLF001
        accessory_plan={"items": ["canvas shoes"], "strength": "strong"},
        style_prompt="8岁儿童，活泼自然",
        age_context="童装",
    )

    assert "child-appropriate" in prompt
    assert "no adult jewelry styling" in prompt
    assert "更明显但仍克制" in prompt


def test_accessory_preview_image_params_use_png_reference_quality() -> None:
    params = workflows._accessory_preview_image_params()  # noqa: SLF001

    assert params.aspect_ratio == "4:5"
    assert params.size_mode == "fixed"
    assert params.fixed_size == "1600x2000"
    assert params.count == 1
    assert params.render_quality == "high"
    assert params.fast is False
    assert params.output_format == "png"
    assert params.output_compression is None
    assert params.background == "opaque"
    assert params.moderation == "low"


def test_lifestyle_template_uses_product_matched_scene_and_integration() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model"},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={
            "category": "西装外套",
            "color": "深灰色",
            "material_guess": "羊毛混纺",
            "silhouette": "修身通勤",
            "must_preserve": ["深灰色", "西装驳领"],
            "background_recommendation": "克制高级的精品空间氛围",
        },
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="lifestyle",
        shot_type="front_full_body",
        final_quality="high",
        user_prompt="克制高级，轻松侧身",
    )

    assert "克制高级" in prompt
    assert "精品空间氛围" in prompt
    assert "画廊" not in prompt
    assert "酒店" not in prompt
    assert "咖啡馆" not in prompt
    assert "买手店" not in prompt
    assert "西装外套" in prompt
    assert "羊毛混纺" not in prompt
    assert "boutique hotel lobby" not in prompt


def test_daily_snapshot_template_uses_phone_realistic_scene() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "熟龄女性，欧美，自然日常"},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"category": "针织上衣", "must_preserve": ["浅灰色", "短款版型"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": True, "items": ["帆布包"], "strength": "subtle"},
        template="daily_snapshot",
        shot_type="natural_pose",
        final_quality="high",
    )

    assert "日常随拍质感" in prompt
    assert "手机拍摄感" in prompt
    assert "超真实、超自然" in prompt
    assert "不像棚拍" in prompt
    assert "帆布包" not in prompt


def test_revision_prompt_is_short_repair_brief() -> None:
    candidate = SimpleNamespace(id="cand-1")

    prompt = workflows._revision_prompt(  # noqa: SLF001
        instruction="衣服颜色更接近商品图",
        product_analysis={"must_preserve": ["米白色", "宽松版型"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
    )

    assert "返修" in prompt
    assert "保持已确认模特" in prompt
    assert "不要改款" in prompt
    assert "米白色" in prompt
    assert "衣服颜色更接近商品图" in prompt


def test_quality_review_prompt_focuses_on_core_ecommerce_checks() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model"},
    )

    prompt = workflows._quality_review_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["领口", "纽扣"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        shot_type="front_full_body",
    )

    assert "自动质检" in prompt
    assert "是否还是同一件商品" in prompt
    assert "模特人脸" in prompt
    assert "电商主图" in prompt
    assert "只返回严格 JSON" in prompt
    assert "approve 或 revise" in prompt


@pytest.mark.asyncio
async def test_quality_review_tasks_are_not_auto_created() -> None:
    bundles = await workflows._ensure_quality_review_tasks(  # noqa: SLF001
        None,  # type: ignore[arg-type]
        user=None,  # type: ignore[arg-type]
        conv=None,  # type: ignore[arg-type]
        run=None,  # type: ignore[arg-type]
        showcase_step=SimpleNamespace(image_ids=["image-1"]),
        quality_step=SimpleNamespace(output_json={}, task_ids=[]),
    )

    assert bundles == []


def test_quality_summary_merge_preserves_review_task_map() -> None:
    report = SimpleNamespace(overall_score=88, recommendation="approve")

    payload = workflows._merge_quality_summary_payload(  # noqa: SLF001
        {
            "review_tasks": {"image-1": "completion-1"},
            "review_task_count": 1,
        },
        [report],  # type: ignore[list-item]
    )

    assert payload["overall"] == "approve"
    assert payload["image_count"] == 1
    assert payload["average_score"] == 88.0
    assert payload["review_tasks"] == {"image-1": "completion-1"}
    assert payload["review_task_count"] == 1


# ---------------------------------------------------------------------------
# 模特库独立生成 + 任务中心 + auto-tag helper 单测
# ---------------------------------------------------------------------------


def test_model_library_run_title_includes_age_gender_and_appearance() -> None:
    title = workflows._model_library_run_title(  # noqa: SLF001
        age_segment="young_adult",
        gender="female",
        appearance_direction="asian",
    )
    assert "模特库生成" in title
    assert "青年女性" in title
    assert "asian" in title


def test_model_library_run_title_labels_multi_gender() -> None:
    title = workflows._model_library_run_title(  # noqa: SLF001
        age_segment="young_adult",
        genders=["female", "male"],
        appearance_direction="east_asian",
    )
    assert "青年男女" in title


def test_model_library_run_title_handles_missing_appearance() -> None:
    title = workflows._model_library_run_title(  # noqa: SLF001
        age_segment="adult",
        gender="male",
        appearance_direction=None,
    )
    assert "模特库生成" in title
    assert "熟龄男性" in title
    # 没有 appearance 不应留尾随 ·
    assert not title.endswith("·")


def test_model_library_generate_prompt_embeds_age_gender_appearance() -> None:
    prompt = workflows._model_library_generate_prompt(  # noqa: SLF001
        age_segment="young_adult",
        gender="female",
        appearance_direction="asian",
        extra_requirements="natural studio",
        style_tags=["minimal", "soft light"],
        candidate_index=2,
    )
    assert "Gender: female" in prompt
    assert "young adult proportions" in prompt
    assert "Appearance direction: asian." in prompt
    assert "natural studio" in prompt
    assert "minimal" in prompt
    assert "Variation index: 2." in prompt
    assert "Look anchor for this candidate" in prompt
    assert "warm ivory sleeveless top" in prompt
    assert "Every candidate must wear this exact same outfit" in prompt


def test_model_diversity_anchor_rotates_by_candidate_index_and_gender() -> None:
    anchor = workflows._model_diversity_anchor  # noqa: SLF001

    # 同 gender 不同 index → 不同 archetype
    a1 = anchor(candidate_index=1, gender="female")
    a2 = anchor(candidate_index=2, gender="female")
    assert a1 != a2
    assert "Look anchor for this candidate" in a1
    assert "Look anchor for this candidate" in a2

    # 池子大小 8：第 9 个绕回第 1 个
    assert anchor(candidate_index=9, gender="female") == anchor(candidate_index=1, gender="female")

    # gender 切换 → 不同池子
    male_a1 = anchor(candidate_index=1, gender="male")
    assert male_a1 != a1
    assert "short side-part hair" in male_a1  # 男性池子第 1 条

    # toddler/child 走引导句，不套成人 archetype
    child_anchor = anchor(candidate_index=1, gender="female", age_segment="child")
    assert "Look anchor for this candidate" not in child_anchor
    assert "visibly different" in child_anchor


def test_model_library_generate_image_params_use_lossless_png_with_fast_off() -> None:
    params = workflows._model_library_generate_image_params()  # noqa: SLF001

    assert params.aspect_ratio == "4:5"
    assert params.count == 1
    assert params.render_quality == "high"
    assert params.fast is False
    assert params.output_format == "png"
    assert params.output_compression is None


def test_infer_candidate_gender_detects_male_signal_and_defaults_to_female() -> None:
    f = workflows._infer_candidate_gender  # noqa: SLF001

    assert f("男装通勤", {"category": "衬衫"}) == "male"
    assert f("menswear smart casual", {"category": "shirt"}) == "male"
    assert f("男童运动套装", {"category": "童装"}) == "male"
    assert f("male casual wear", {"category": "shirt"}) == "male"
    assert f("female premium ecommerce model", {"category": "apparel"}) == "female"
    assert f("womenswear studio", {"category": "shirt"}) == "female"
    assert f("女装连衣裙", {"category": "连衣裙"}) == "female"
    # 没有性别信号时默认 female
    assert f("clean premium ecommerce model", {"category": "apparel"}) == "female"


def test_model_library_job_status_combines_step_status_and_count() -> None:
    f = workflows._model_library_job_status  # noqa: SLF001
    # running with no images -> running
    assert f(step_status="running", requested_count=4, finished_count=0) == "running"
    # running with partial -> still running
    assert f(step_status="running", requested_count=4, finished_count=2) == "running"
    # succeeded with full -> succeeded
    assert f(step_status="succeeded", requested_count=4, finished_count=4) == "succeeded"
    # succeeded with partial -> partial
    assert f(step_status="succeeded", requested_count=4, finished_count=2) == "partial"
    # failed with no images -> failed
    assert f(step_status="failed", requested_count=4, finished_count=0) == "failed"
    # failed with some succeeded -> partial
    assert f(step_status="failed", requested_count=4, finished_count=2) == "partial"


def test_model_library_run_inputs_normalizes_input_json() -> None:
    step = SimpleNamespace(
        input_json={
            "age_segment": "young_adult",
            "gender": "F",
            "appearance_direction": "  asian  ",
            "style_tags": ["minimal", "minimal", "studio"],
            "count": 4,
            "auto_tag": True,
        },
        task_ids=["t1", "t2", "t3", "t4"],
    )
    out = workflows._model_library_run_inputs(step)  # noqa: SLF001
    assert out["age_segment"] == "young_adult"
    assert out["gender"] == "female"
    assert out["appearance_direction"] == "asian"
    assert out["style_tags"] == ["minimal", "studio"]
    assert out["count"] == 4
    assert out["auto_tag"] is True


def test_model_library_run_inputs_accepts_multi_gender_snapshot() -> None:
    step = SimpleNamespace(
        input_json={
            "age_segment": "young_adult",
            "genders": ["female", "male"],
            "count": 4,
        },
        task_ids=["t1"] * 8,
    )
    out = workflows._model_library_run_inputs(step)  # noqa: SLF001
    assert out["gender"] == "female/male"
    assert out["genders"] == ["female", "male"]


def test_job_item_out_uses_image_gender_and_download_filename() -> None:
    item = workflows._job_item_out(  # noqa: SLF001
        image_id="image-abcdef123456",
        image_out=SimpleNamespace(
            url="/api/images/image-abcdef123456/binary",
            display_url="/api/images/image-abcdef123456/variants/display2048",
            thumb_url="/api/images/image-abcdef123456/variants/thumb256",
            mime="image/png",
        ),
        saved_item_id=None,
        age_segment="adult",
        gender="female/male",
        style_tags=["温柔亲和"],
        appearance_direction="east_asian",
        image_meta={"gender": "male"},
    )
    assert item.gender == "male"
    assert item.download_filename is not None
    assert "male" in item.download_filename


def test_merge_library_item_fields_appends_style_tags_only() -> None:
    existing = {
        "id": "user:1",
        "title": "preset",
        "age_segment": "user_favorites",
        "gender": "",
        "appearance_direction": None,
        "style_tags": ["old"],
    }
    merged = workflows._merge_library_item_fields(  # noqa: SLF001
        existing=existing,
        style_tags=["new", "tag"],
        appearance_direction="european",
        age_segment="young_adult",
        gender="female",
        notes="auto tagged",
    )
    # style_tags appends without losing tags the user selected in advance.
    assert merged["style_tags"] == ["old", "new", "tag"]
    # appearance_direction empty before -> filled
    assert merged["appearance_direction"] == "european"
    # age_segment user_favorites -> upgraded
    assert merged["age_segment"] == "young_adult"
    # gender empty -> filled
    assert merged["gender"] == "female"
    assert merged["auto_tag_notes"] == "auto tagged"


def test_merge_library_item_fields_preserves_user_filled_appearance() -> None:
    existing = {
        "id": "user:1",
        "title": "preset",
        "age_segment": "adult",
        "gender": "male",
        "appearance_direction": "european",
        "style_tags": [],
    }
    merged = workflows._merge_library_item_fields(  # noqa: SLF001
        existing=existing,
        style_tags=["minimal"],
        appearance_direction="asian",
        age_segment="senior",
        gender="female",
        notes=None,
    )
    # 用户已填的字段保守不被覆盖
    assert merged["appearance_direction"] == "european"
    assert merged["age_segment"] == "adult"
    assert merged["gender"] == "male"
    # style_tags 会追加；其他用户已填字段保守不被覆盖
    assert merged["style_tags"] == ["minimal"]


def test_normalize_tagged_age_recognizes_aliases() -> None:
    f = workflows._normalize_tagged_age  # noqa: SLF001
    assert f("young_adult") == "young_adult"
    assert f("YOUNG") == "young_adult"
    assert f("kids") == "child"
    assert f("middleaged") == "middle_aged"
    assert f("garbage") is None


def test_normalize_tagged_gender_normalizes_aliases() -> None:
    f = workflows._normalize_tagged_gender  # noqa: SLF001
    assert f("female") == "female"
    assert f("Woman") == "female"
    assert f("M") == "male"
    assert f("unknown") is None


def test_parse_tagging_text_strips_markdown_fences() -> None:
    payload = workflows._parse_tagging_text(  # noqa: SLF001
        '```json\n{"style_tags": ["a", "b"], "gender": "female"}\n```'
    )
    assert payload["style_tags"] == ["a", "b"]
    assert payload["gender"] == "female"


def test_parse_tagging_text_extracts_json_from_noisy_text() -> None:
    payload = workflows._parse_tagging_text(  # noqa: SLF001
        'Here is the JSON: {"style_tags": ["x"]}\nthank you'
    )
    assert payload == {"style_tags": ["x"]}


def test_parse_tagging_text_returns_empty_on_invalid_json() -> None:
    assert workflows._parse_tagging_text("not json at all") == {}  # noqa: SLF001
    assert workflows._parse_tagging_text("") == {}  # noqa: SLF001
