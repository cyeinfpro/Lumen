from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.routes import _apparel_scene_planner as scene_planner
from app.routes import workflows
from lumen_core.constants import MAX_PROMPT_CHARS
from lumen_core.schemas import (
    ApparelModelLibraryBatchDeleteIn,
    ApparelModelLibraryGenerateIn,
    ModelCandidatesCreateIn,
    ShowcaseImagesCreateIn,
)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.rowcount = len(rows)

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self.rows

    def scalar_one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None


class _Db:
    def __init__(
        self, rows: list[Any], responses: list[list[Any]] | None = None
    ) -> None:
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


class _BackgroundTasks:
    def __init__(self) -> None:
        self.tasks: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def add_task(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self.tasks.append((func, args, kwargs))


def test_model_candidates_mvp_requires_three_candidates() -> None:
    body = ModelCandidatesCreateIn(
        candidate_count=3,
        style_prompt="premium",
        accessory_plan={
            "enabled": True,
            "items": ["white sneakers"],
            "strength": "subtle",
        },
    )

    assert body.accessory_plan.items == ["white sneakers"]

    with pytest.raises(ValidationError):
        ModelCandidatesCreateIn(candidate_count=2, style_prompt="premium")


def test_showcase_images_output_count_allows_batch_choices() -> None:
    for count in (1, 2, 4, 8, 16):
        assert ShowcaseImagesCreateIn(output_count=count).output_count == count

    assert (
        ShowcaseImagesCreateIn(template="natural_phone_snapshot").template
        == "natural_phone_snapshot"
    )
    assert ShowcaseImagesCreateIn().scene_planner == "gpt55_preflight"
    assert (
        ShowcaseImagesCreateIn(
            scene_strategy="editorial_campaign",
            scene_variety="wild",
            continuity_anchor="pet",
            allow_pet=True,
        ).continuity_anchor
        == "pet"
    )

    with pytest.raises(ValidationError):
        ShowcaseImagesCreateIn(output_count=3)


def test_model_library_batch_delete_accepts_export_sized_batches() -> None:
    body = ApparelModelLibraryBatchDeleteIn(
        item_ids=[f"user:item-{index}" for index in range(549)]
    )

    assert len(body.item_ids) == 549


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


def test_github_folder_metadata_keeps_fine_grained_appearance() -> None:
    item = workflows._metadata_from_github_file(  # noqa: SLF001
        {
            "type": "file",
            "name": "adult-female-southeast-asian-001.webp",
            "path": (
                "assets/apparel-model-presets/05_adult/female/"
                "adult-female-southeast-asian-001.webp"
            ),
            "download_url": "https://example.invalid/model.webp",
        }
    )

    assert item is not None
    assert item["appearance_direction"] == "southeast_asian"


def test_preset_title_uses_updated_age_labels() -> None:
    assert workflows._title_from_preset_id("adult-female-001").startswith("熟龄 女性")
    assert workflows._title_from_preset_id("middle-aged-male-001").startswith(
        "中年 男性"
    )


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
    assert (
        workflows._model_library_folder_for_age("bad", "female")
        == "00_user_favorites/female"
    )  # noqa: SLF001
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


def test_candidate_reference_image_ids_dedupes_all_known_fields() -> None:
    candidate = SimpleNamespace(
        model_brief_json={"candidate_image_ids": ["brief-1", "sheet", "", 123]},
        contact_sheet_image_id="sheet",
        portrait_image_id="portrait",
        front_image_id=None,
        side_image_id="side",
        back_image_id="brief-1",
    )

    assert workflows._candidate_reference_image_ids(candidate) == [  # noqa: SLF001
        "brief-1",
        "sheet",
        "portrait",
        "side",
    ]


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
async def test_workflow_produced_model_image_ids_pulls_from_owner_generation_subquery() -> (
    None
):
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
    assert (
        "(generations.upstream_request ->> 'parent_generation_id') IN ('task-a', 'task-b')"
        in rendered
    )
    # 注意：as_boolean() 在 PostgreSQL 上编译成 CAST(text AS BOOLEAN)，
    # 这样 worker 不论写 JSON true 还是字符串 "true" 都能被 cast 命中。
    assert (
        "CAST((generations.upstream_request ->> 'is_dual_race_bonus') AS BOOLEAN) IS true"
        in rendered
    )


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


def test_task_error_summary_prefers_messages_and_dedupes() -> None:
    out = workflows._task_error_summary(  # noqa: SLF001
        [
            SimpleNamespace(
                error_code="upstream_error", error_message="provider timeout"
            ),
            SimpleNamespace(
                error_code="upstream_error", error_message="provider timeout"
            ),
            SimpleNamespace(error_code="safety", error_message=None),
        ],
        "生成失败",
    )

    assert out == "upstream_error: provider timeout；safety"


@pytest.mark.asyncio
async def test_soft_delete_workflow_generated_images_uses_explicit_image_ids() -> None:
    from sqlalchemy.dialects import postgresql

    step = SimpleNamespace(
        step_key="showcase_generation",
        image_ids=["img-step"],
        task_ids=[],
    )
    candidate = SimpleNamespace(
        contact_sheet_image_id="img-candidate",
        portrait_image_id="img-portrait",
        front_image_id=None,
        side_image_id=None,
        back_image_id=None,
        task_ids=[],
    )
    run = SimpleNamespace(id="run-1", user_id="user-1")
    db = _Db([], responses=[[step], [candidate], []])

    out = await workflows._soft_delete_workflow_generated_images(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        deleted_at=datetime.now(timezone.utc),
        cancel_message="workflow deleted",
    )

    assert out == {
        "images_deleted": 0,
        "generations_canceled": 0,
        "completions_canceled": 0,
    }
    rendered = str(db.statements[-1].compile(dialect=postgresql.dialect()))
    assert "UPDATE images" in rendered
    assert "images.id IN" in rendered
    assert "model_library_items.image_id IS NOT NULL" in rendered


@pytest.mark.asyncio
async def test_delete_workflow_cleans_generated_outputs_and_backing_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        deleted_at=None,
        conversation_id="conv-1",
    )
    conv = SimpleNamespace(id="conv-1", user_id="user-1", deleted_at=None)
    cleanup_calls: list[dict[str, Any]] = []

    async def fake_get_run(
        db: Any,
        *,
        user_id: str,
        run_id: str,
        lock: bool = False,
    ) -> Any:
        assert user_id == "user-1"
        assert run_id == "run-1"
        assert lock is True
        return run

    async def fake_cleanup(
        db: Any,
        *,
        run: Any,
        deleted_at: datetime,
        cancel_message: str,
    ) -> dict[str, int]:
        cleanup_calls.append(
            {
                "run_id": run.id,
                "deleted_at": deleted_at,
                "cancel_message": cancel_message,
            }
        )
        return {
            "images_deleted": 1,
            "generations_canceled": 0,
            "completions_canceled": 0,
        }

    monkeypatch.setattr(workflows, "_get_run", fake_get_run)
    monkeypatch.setattr(
        workflows, "_soft_delete_workflow_generated_images", fake_cleanup
    )
    db = _Db([], responses=[[conv]])

    out = await workflows.delete_workflow(  # noqa: SLF001
        "run-1",
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
    )

    assert out == {"ok": True}
    assert cleanup_calls == [
        {
            "run_id": "run-1",
            "deleted_at": run.deleted_at,
            "cancel_message": "workflow deleted",
        }
    ]
    assert conv.deleted_at == run.deleted_at
    assert db.committed is True


@pytest.mark.asyncio
async def test_delete_apparel_model_library_job_cleans_generated_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        type=workflows.WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
        deleted_at=None,
        conversation_id=None,
    )
    cleanup_calls: list[str] = []

    async def fake_get_run(
        db: Any,
        *,
        user_id: str,
        run_id: str,
        lock: bool = False,
    ) -> Any:
        assert user_id == "user-1"
        assert run_id == "run-1"
        assert lock is True
        return run

    async def fake_cleanup(
        db: Any,
        *,
        run: Any,
        deleted_at: datetime,
        cancel_message: str,
    ) -> dict[str, int]:
        assert deleted_at.tzinfo is not None
        cleanup_calls.append(f"{run.id}:{cancel_message}")
        return {
            "images_deleted": 2,
            "generations_canceled": 0,
            "completions_canceled": 0,
        }

    monkeypatch.setattr(workflows, "_get_run", fake_get_run)
    monkeypatch.setattr(
        workflows, "_soft_delete_workflow_generated_images", fake_cleanup
    )
    db = _Db([])

    out = await workflows.delete_apparel_model_library_job(  # noqa: SLF001
        "run-1",
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
    )

    assert out == {"ok": True}
    assert cleanup_calls == ["run-1:model library job deleted"]
    assert run.deleted_at is not None
    assert db.committed is True


@pytest.mark.asyncio
async def test_clear_apparel_model_library_jobs_cleans_each_finished_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        SimpleNamespace(
            id="run-1", user_id="user-1", deleted_at=None, conversation_id=None
        ),
        SimpleNamespace(
            id="run-2", user_id="user-1", deleted_at=None, conversation_id=None
        ),
    ]
    cleanup_calls: list[str] = []

    async def fake_cleanup(
        db: Any,
        *,
        run: Any,
        deleted_at: datetime,
        cancel_message: str,
    ) -> dict[str, int]:
        cleanup_calls.append(f"{run.id}:{cancel_message}:{deleted_at.isoformat()}")
        return {
            "images_deleted": 1,
            "generations_canceled": 0,
            "completions_canceled": 0,
        }

    monkeypatch.setattr(
        workflows, "_soft_delete_workflow_generated_images", fake_cleanup
    )
    db = _Db([], responses=[rows])

    out = await workflows.clear_apparel_model_library_jobs(  # noqa: SLF001
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
    )

    assert out.deleted == 2
    assert rows[0].deleted_at is not None
    assert rows[1].deleted_at == rows[0].deleted_at
    assert [call.split(":")[0] for call in cleanup_calls] == ["run-1", "run-2"]
    assert all("model library job cleared" in call for call in cleanup_calls)
    assert db.committed is True


def test_default_github_contents_url_points_to_user_repo_folder() -> None:
    assert workflows._github_contents_url().startswith(  # noqa: SLF001
        "https://api.github.com/repos/cyeinfpro/Lumen/contents/assets/apparel-model-presets"
    )


def test_model_library_http_client_kwargs_includes_proxy_when_configured() -> None:
    kwargs = workflows._model_library_http_client_kwargs("socks5h://127.0.0.1:1080")  # noqa: SLF001

    assert kwargs["proxy"] == "socks5h://127.0.0.1:1080"
    assert "timeout" in kwargs


@pytest.mark.asyncio
async def test_resolve_model_library_sync_proxy_uses_enabled_proxy() -> None:
    provider_config = {
        "proxies": [
            {
                "name": "s5-us",
                "type": "socks5",
                "host": "127.0.0.1",
                "port": 1080,
                "enabled": True,
            }
        ],
        "providers": [
            {
                "name": "default",
                "base_url": "https://api.example.com",
                "api_key": "sk-test",
            }
        ],
    }
    db = _Db(
        [],
        responses=[
            ["1"],
            [json.dumps(provider_config)],
            [""],
        ],
    )

    proxy, proxy_url = await workflows._resolve_model_library_sync_proxy(db)  # noqa: SLF001

    assert proxy is not None
    assert proxy.name == "s5-us"
    assert proxy_url == "socks5h://127.0.0.1:1080"


@pytest.mark.asyncio
async def test_library_job_derives_failed_status_from_failed_generation() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        created_at=now,
        updated_at=now,
    )
    step = SimpleNamespace(
        workflow_run_id="run-1",
        step_key=workflows.MODEL_LIBRARY_GENERATE_STEP_KEY,
        status="running",
        input_json={
            "age_segment": "adult",
            "gender": "female",
            "appearance_direction": "asian",
            "count": 1,
        },
        output_json={},
        task_ids=["gen-1"],
        image_ids=[],
    )
    failed_generation = SimpleNamespace(
        id="gen-1",
        status=workflows.GenerationStatus.FAILED.value,
        error_code="upstream_error",
        error_message="provider timeout",
    )
    db = _Db([], responses=[[step], [failed_generation]])

    job = await workflows._job_from_library_run(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        saved_map={},
    )

    assert job.status == "failed"
    assert job.error_message == "upstream_error: provider timeout"


@pytest.mark.asyncio
async def test_apparel_model_library_jobs_respects_offset_and_has_more(
    monkeypatch,
) -> None:
    async def fake_ensure_legacy_user_library_migrated(_db, _user_id):
        return False

    async def fake_saved_image_id_set(_db, _user_id):
        return {}

    monkeypatch.setattr(
        workflows,
        "_ensure_legacy_user_library_migrated",
        fake_ensure_legacy_user_library_migrated,
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
        (
            SimpleNamespace(id="project-1", title="Project 1"),
            SimpleNamespace(id="step-1"),
        ),
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
async def test_create_user_image_from_preset_copies_to_user_private_storage(
    tmp_path, monkeypatch
) -> None:
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


def test_showcase_gpt55_reference_data_url_downsamples_and_skips_bad_raw() -> None:
    import io

    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGBA", (1800, 1200), (255, 0, 0, 128)).save(buf, format="PNG")
    image = SimpleNamespace(id="img-1", mime="image/png")

    data_url = workflows._showcase_gpt55_reference_data_url(  # noqa: SLF001
        image, buf.getvalue()
    )

    assert data_url is not None
    assert data_url.startswith("data:image/jpeg;base64,")
    payload = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    assert len(payload) <= workflows._SHOWCASE_GPT55_REFERENCE_MAX_BYTES  # noqa: SLF001
    assert (
        workflows._showcase_gpt55_reference_data_url(image, b"not an image")  # noqa: SLF001
        is None
    )


@pytest.mark.asyncio
async def test_showcase_gpt55_reference_images_returns_local_skip_reasons(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from PIL import Image as PILImage

    monkeypatch.setattr(workflows.settings, "storage_root", str(tmp_path))
    PILImage.new("RGB", (64, 64), (12, 34, 56)).save(tmp_path / "good.png")
    db = _Db(
        [
            SimpleNamespace(id="prod-ok", storage_key="good.png", mime="image/png"),
            SimpleNamespace(
                id="model-missing-storage", storage_key="", mime="image/png"
            ),
        ]
    )

    result = await workflows._showcase_gpt55_reference_images(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        user_id="user-1",
        product_image_ids=["prod-ok", "prod-missing"],
        model_image_id="model-missing-storage",
    )

    assert [item["label"] for item in result["images"]] == ["商品图 1"]
    assert result["skips"] == [
        {"image_id": "prod-missing", "label": "商品图 2", "reason": "image_not_found"},
        {
            "image_id": "model-missing-storage",
            "label": "已确认模特图",
            "reason": "missing_storage_key",
        },
    ]


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


@pytest.mark.asyncio
async def test_create_showcase_images_queues_gpt55_preflight_in_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        conversation_id="conv-1",
        user_prompt="自然街拍",
        product_image_ids=["product-1"],
        current_step="model_approval",
        status="needs_review",
        metadata_jsonb={},
    )
    product_step = SimpleNamespace(
        step_key="product_analysis",
        status="approved",
        output_json={"category": "衬衫"},
    )
    candidate = SimpleNamespace(
        id="cand-1",
        contact_sheet_image_id="model-1",
        model_brief_json={},
    )
    showcase = SimpleNamespace(
        step_key="showcase_generation",
        status="failed",
        input_json={},
        output_json={},
        task_ids=["old-failed-task"],
        image_ids=["old-image"],
    )
    approval = SimpleNamespace(
        step_key="model_approval",
        input_json={
            "accessory_plan": {
                "enabled": False,
                "items": [],
                "strength": "subtle",
            }
        },
    )
    quality = SimpleNamespace(
        step_key="quality_review",
        status="waiting_input",
        input_json={},
        output_json={},
        task_ids=[],
        image_ids=[],
    )
    conv = SimpleNamespace(id="conv-1", last_activity_at=None)
    steps = {
        "product_analysis": product_step,
        "showcase_generation": showcase,
        "model_approval": approval,
        "quality_review": quality,
    }

    async def fake_get_run(
        db: Any, *, user_id: str, run_id: str, lock: bool = False
    ) -> Any:
        assert user_id == "user-1"
        assert run_id == "run-1"
        return run

    async def fake_sync(db: Any, current_run: Any) -> None:
        assert current_run is run

    async def fake_step(db: Any, run_id: str, step_key: str) -> Any:
        assert run_id == "run-1"
        return steps[step_key]

    async def fake_selected_candidate(db: Any, run_id: str) -> Any:
        assert run_id == "run-1"
        return candidate

    async def fake_conversation(db: Any, *, user_id: str, conversation_id: str) -> Any:
        assert user_id == "user-1"
        assert conversation_id == "conv-1"
        return conv

    async def fake_build_run_out(db: Any, current_run: Any) -> Any:
        return current_run

    async def fail_if_called(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("preflight must run in the background task")

    monkeypatch.setattr(workflows, "_get_run", fake_get_run)
    monkeypatch.setattr(workflows, "_sync_workflow_outputs", fake_sync)
    monkeypatch.setattr(workflows, "_step", fake_step)
    monkeypatch.setattr(workflows, "_selected_candidate", fake_selected_candidate)
    monkeypatch.setattr(workflows, "_get_owned_conversation", fake_conversation)
    monkeypatch.setattr(workflows, "_build_run_out", fake_build_run_out)
    monkeypatch.setattr(workflows, "_prepare_showcase_preflight", fail_if_called)

    background = _BackgroundTasks()
    body = ShowcaseImagesCreateIn(output_count=2, template="urban_commute")
    db = _Db([])

    out = await workflows.create_showcase_images(
        "run-1",
        body,
        background,  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
    )

    assert out is run
    assert run.current_step == "showcase_generation"
    assert run.status == "running"
    assert showcase.status == "running"
    assert showcase.task_ids == []
    assert showcase.output_json == {}
    assert showcase.input_json["preflight_status"] == "queued"
    assert showcase.input_json["active_task_ids"] == []
    assert showcase.input_json["active_output_count"] == 2
    assert showcase.input_json["baseline_image_count"] == 1
    assert showcase.input_json["target_image_count"] == 3
    assert showcase.input_json["reference_image_ids"] == ["product-1", "model-1"]
    assert quality.status == "waiting_input"
    assert conv.last_activity_at is not None
    assert db.committed is True
    assert len(background.tasks) == 1
    func, args, kwargs = background.tasks[0]
    assert func is workflows._run_showcase_images_generation_in_background  # noqa: SLF001
    assert kwargs == {}
    assert args[0] == "user-1"
    assert args[1] == "run-1"
    assert args[2]["output_count"] == 2
    assert args[3] == showcase.input_json["generation_request_id"]


def test_showcase_preflight_timeout_scales_for_large_gpt55_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LUMEN_SHOWCASE_PREFLIGHT_TIMEOUT_SEC", raising=False)

    assert (
        workflows._showcase_preflight_timeout_seconds(  # noqa: SLF001
            scene_planner="gpt55_preflight",
            shot_count=5,
        )
        == 840.0
    )
    assert (
        workflows._showcase_preflight_timeout_seconds(  # noqa: SLF001
            scene_planner="gpt55_preflight",
            shot_count=16,
        )
        == 1680.0
    )
    assert (
        workflows._showcase_preflight_timeout_seconds(  # noqa: SLF001
            scene_planner="rules_fallback",
            shot_count=16,
        )
        == 240.0
    )


def test_showcase_gpt_provider_limit_caps_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUMEN_SHOWCASE_GPT_PROVIDER_LIMIT", "2")

    assert (
        [p.name for p in scene_planner._limit_gpt55_providers([  # noqa: SLF001
            SimpleNamespace(name="p1"),
            SimpleNamespace(name="p2"),
            SimpleNamespace(name="p3"),
        ])]
        == ["p1", "p2"]
    )


@pytest.mark.asyncio
async def test_prepare_showcase_preflight_marks_timeout_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_wait_for(awaitable: Any, timeout: float) -> Any:
        assert timeout == 1.0
        awaitable.close()
        raise asyncio.TimeoutError

    async def fake_impl(**kwargs: Any) -> dict[str, Any]:
        planner = str(kwargs["scene_planner"])
        calls.append(planner)
        return {
            "planning": {
                "planner": "rules_fallback",
                "fallback_reason": "rules_fallback_requested",
            },
            "scene_cards": [],
            "final_prompts": [],
        }

    monkeypatch.setenv("LUMEN_SHOWCASE_PREFLIGHT_TIMEOUT_SEC", "0.001")
    monkeypatch.setattr(workflows.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(workflows, "_prepare_showcase_preflight_impl", fake_impl)

    result = await workflows._prepare_showcase_preflight(  # noqa: SLF001
        scene_planner="gpt55_preflight",
        shot_picks=[("front_full_body", {"label": "正面", "framing": "product_first"})],
    )

    assert calls == ["rules_fallback"]
    assert result["preflight_timed_out"] is True
    assert result["planning"]["planner"] == "rules_fallback"
    assert result["planning"]["requested_planner"] == "gpt55_preflight"
    assert result["planning"]["fallback_reason"] == "preflight_timeout_after_1s"
    assert result["planning"]["timeout_seconds"] == 1.0


@pytest.mark.asyncio
async def test_mark_showcase_generation_failed_records_background_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        current_step="showcase_generation",
        status="running",
    )
    showcase = SimpleNamespace(
        step_key="showcase_generation",
        status="running",
        input_json={"generation_request_id": "req-1", "preflight_status": "running"},
        output_json={},
    )

    async def fake_get_run(
        db: Any, *, user_id: str, run_id: str, lock: bool = False
    ) -> Any:
        assert user_id == "user-1"
        assert run_id == "run-1"
        assert lock is True
        return run

    async def fake_step(db: Any, run_id: str, step_key: str) -> Any:
        assert run_id == "run-1"
        assert step_key == "showcase_generation"
        return showcase

    monkeypatch.setattr(workflows, "_get_run", fake_get_run)
    monkeypatch.setattr(workflows, "_step", fake_step)

    db = _Db([])
    await workflows._mark_showcase_generation_failed(  # noqa: SLF001
        db=db,  # type: ignore[arg-type]
        user_id="user-1",
        workflow_run_id="run-1",
        request_id="req-1",
        exc=RuntimeError("gpt55 preflight timeout"),
    )

    assert showcase.status == "failed"
    assert showcase.input_json["preflight_status"] == "failed"
    assert showcase.output_json["error_code"] == "showcase_generation_failed"
    assert showcase.output_json["error_message"] == "gpt55 preflight timeout"
    assert run.status == "failed"
    assert db.committed is True


@pytest.mark.asyncio
async def test_sync_showcase_completion_advances_to_quality_review() -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        current_step="showcase_generation",
        status="running",
    )
    showcase_step = SimpleNamespace(
        step_key="showcase_generation",
        status="running",
        input_json={"target_image_count": 1, "output_count": 1},
        output_json={},
        task_ids=["gen-1"],
        image_ids=[],
    )
    quality_step = SimpleNamespace(
        step_key="quality_review",
        status="waiting_input",
        input_json={},
        output_json={},
        task_ids=[],
        image_ids=[],
    )
    steps = [
        SimpleNamespace(step_key="model_approval", task_ids=[]),
        showcase_step,
        quality_step,
    ]
    db = _Db(
        [],
        responses=[
            steps,  # _load_steps
            [],  # model candidates
            [
                SimpleNamespace(
                    id="gen-1", status=workflows.GenerationStatus.SUCCEEDED.value
                )
            ],
            [],  # dual-race bonus generations
            [SimpleNamespace(id="image-1", owner_generation_id="gen-1")],
            [],  # quality reports
        ],
    )

    await workflows._sync_workflow_outputs(db, run)  # noqa: SLF001

    assert showcase_step.status == "completed"
    assert showcase_step.image_ids == ["image-1"]
    assert quality_step.status == "needs_review"
    assert quality_step.image_ids == ["image-1"]
    assert quality_step.output_json["overall"] == "pending"
    assert run.current_step == "quality_review"
    assert run.status == "needs_review"


@pytest.mark.asyncio
async def test_build_run_out_includes_model_library_reference_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id="run-1",
        conversation_id=None,
        user_id="user-1",
        type="apparel_model_showcase",
        status="needs_review",
        title="服饰模特展示图",
        user_prompt="clean studio",
        product_image_ids=["product-1"],
        current_step="model_candidates",
        quality_mode="premium",
        metadata_jsonb={},
        created_at=now,
        updated_at=now,
    )
    step = SimpleNamespace(
        id="step-1",
        workflow_run_id="run-1",
        step_key="model_candidates",
        status="needs_review",
        input_json={},
        output_json={},
        task_ids=[],
        image_ids=[],
        approved_at=None,
        approved_by=None,
        created_at=now,
        updated_at=now,
    )
    candidate = SimpleNamespace(
        id="cand-1",
        workflow_run_id="run-1",
        candidate_index=1,
        portrait_image_id="lib-img",
        front_image_id=None,
        side_image_id=None,
        back_image_id=None,
        contact_sheet_image_id="lib-img",
        model_brief_json={"candidate_image_ids": ["lib-img"]},
        task_ids=[],
        status="ready",
        selected_at=None,
        created_at=now,
        updated_at=now,
    )
    product_image = SimpleNamespace(id="product-1", source="uploaded")
    library_image = SimpleNamespace(id="lib-img", source="uploaded")

    async def fake_sync(_db: Any, _run: Any) -> None:
        return None

    async def fake_load_steps(_db: Any, _run_id: str) -> list[Any]:
        return [step]

    async def fake_load_quality_reports(_db: Any, _run_id: str) -> list[Any]:
        return []

    async def fake_image_out_map(_db: Any, images: list[Any]) -> dict[str, Any]:
        return {
            image.id: workflows.ImageOut(
                id=image.id,
                source=image.source,
                parent_image_id=None,
                owner_generation_id=None,
                width=1024,
                height=1280,
                mime="image/jpeg",
                blurhash=None,
                url=f"/api/images/{image.id}/binary",
                display_url=f"/api/images/{image.id}/variants/display2048",
                preview_url=None,
                thumb_url=None,
                metadata_jsonb={},
            )
            for image in images
        }

    monkeypatch.setattr(workflows, "_sync_workflow_outputs", fake_sync)
    monkeypatch.setattr(workflows, "_load_steps", fake_load_steps)
    monkeypatch.setattr(workflows, "_load_quality_reports", fake_load_quality_reports)
    monkeypatch.setattr(workflows, "_image_out_map", fake_image_out_map)

    db = _Db([], responses=[[candidate], [product_image, library_image]])

    async def fake_refresh(_row: Any) -> None:
        return None

    db.refresh = fake_refresh  # type: ignore[attr-defined]

    out = await workflows._build_run_out(db, run)  # noqa: SLF001

    assert [image.id for image in out.product_images] == ["product-1"]
    assert [image.id for image in out.generated_images] == ["lib-img"]


@pytest.mark.asyncio
async def test_build_run_out_includes_dual_race_bonus_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id="run-1",
        conversation_id=None,
        user_id="user-1",
        type="apparel_model_showcase",
        status="running",
        title="服饰模特展示图",
        user_prompt="clean studio",
        product_image_ids=[],
        current_step="showcase_generation",
        quality_mode="premium",
        metadata_jsonb={},
        created_at=now,
        updated_at=now,
    )
    step = SimpleNamespace(
        id="step-1",
        workflow_run_id="run-1",
        step_key="showcase_generation",
        status="running",
        input_json={"active_task_ids": ["gen-1"], "output_count": 1},
        output_json={},
        task_ids=["gen-1"],
        image_ids=[],
        approved_at=None,
        approved_by=None,
        created_at=now,
        updated_at=now,
    )
    base = _generation_row(
        id="gen-1",
        status=workflows.GenerationStatus.FAILED.value,
        parent_generation_id=None,
        is_dual_race_bonus=False,
        now=now,
    )
    bonus = _generation_row(
        id="bonus-1",
        status=workflows.GenerationStatus.SUCCEEDED.value,
        parent_generation_id="gen-1",
        is_dual_race_bonus=True,
        now=now,
    )

    async def fake_sync(_db: Any, _run: Any) -> None:
        return None

    async def fake_load_steps(_db: Any, _run_id: str) -> list[Any]:
        return [step]

    async def fake_load_quality_reports(_db: Any, _run_id: str) -> list[Any]:
        return []

    async def fake_generation_rows(
        _db: Any,
        *,
        user_id: str,
        task_ids: list[str],
        include_dual_bonus: bool,
    ) -> list[Any]:
        assert user_id == "user-1"
        assert task_ids == ["gen-1"]
        assert include_dual_bonus is True
        return [base, bonus]

    async def fake_image_out_map(_db: Any, images: list[Any]) -> dict[str, Any]:
        assert images == []
        return {}

    monkeypatch.setattr(workflows, "_sync_workflow_outputs", fake_sync)
    monkeypatch.setattr(workflows, "_load_steps", fake_load_steps)
    monkeypatch.setattr(workflows, "_load_quality_reports", fake_load_quality_reports)
    monkeypatch.setattr(
        workflows,
        "_workflow_generation_rows_from_task_ids",
        fake_generation_rows,
    )
    monkeypatch.setattr(workflows, "_image_out_map", fake_image_out_map)

    db = _Db([], responses=[[], []])

    async def fake_refresh(_row: Any) -> None:
        return None

    db.refresh = fake_refresh  # type: ignore[attr-defined]

    out = await workflows._build_run_out(db, run)  # noqa: SLF001

    assert [generation.id for generation in out.generations] == ["gen-1", "bonus-1"]
    assert out.generations[1].parent_generation_id == "gen-1"
    assert out.generations[1].is_dual_race_bonus is True


def _generation_row(
    *,
    id: str,
    status: str,
    parent_generation_id: str | None,
    is_dual_race_bonus: bool,
    now: datetime,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        message_id="msg-1",
        user_api_credential_id=None,
        upstream_supplier_id=None,
        parent_generation_id=parent_generation_id,
        action="edit",
        prompt="prompt",
        size_requested="auto",
        aspect_ratio="4:5",
        input_image_ids=[],
        primary_input_image_id=None,
        mask_image_id=None,
        status=status,
        progress_stage="finalizing",
        attempt=1,
        error_code=None,
        error_message=None,
        started_at=now,
        finished_at=now,
        is_dual_race_bonus=is_dual_race_bonus,
        billing_free=is_dual_race_bonus,
        billing_label="free" if is_dual_race_bonus else None,
        billing_exempt_reason="dual_race_loser" if is_dual_race_bonus else None,
    )


@pytest.mark.asyncio
async def test_sync_showcase_failure_records_generation_error_message() -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        current_step="showcase_generation",
        status="running",
    )
    showcase_step = SimpleNamespace(
        step_key="showcase_generation",
        status="running",
        input_json={"target_image_count": 1, "output_count": 1},
        output_json={},
        task_ids=["gen-1"],
        image_ids=[],
    )
    steps = [
        SimpleNamespace(step_key="model_approval", task_ids=[]),
        showcase_step,
    ]
    db = _Db(
        [],
        responses=[
            steps,  # _load_steps
            [],  # model candidates
            [
                SimpleNamespace(
                    id="gen-1",
                    status=workflows.GenerationStatus.FAILED.value,
                    error_code="upstream_error",
                    error_message="provider timeout",
                )
            ],
            [],  # dual-race bonus generations
            [],  # images
        ],
    )

    await workflows._sync_workflow_outputs(db, run)  # noqa: SLF001

    assert showcase_step.status == "failed"
    assert showcase_step.output_json["failed_generation_ids"] == ["gen-1"]
    assert showcase_step.output_json["error_message"] == (
        "upstream_error: provider timeout"
    )
    assert run.status == "failed"


@pytest.mark.asyncio
async def test_sync_showcase_canceled_generation_marks_terminal_failure() -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        current_step="showcase_generation",
        status="running",
    )
    showcase_step = SimpleNamespace(
        step_key="showcase_generation",
        status="running",
        input_json={"target_image_count": 1, "output_count": 1},
        output_json={},
        task_ids=["gen-1"],
        image_ids=[],
    )
    steps = [
        SimpleNamespace(step_key="model_approval", task_ids=[]),
        showcase_step,
    ]
    db = _Db(
        [],
        responses=[
            steps,  # _load_steps
            [],  # model candidates
            [
                SimpleNamespace(
                    id="gen-1",
                    status=workflows.GenerationStatus.CANCELED.value,
                    error_code=None,
                    error_message=None,
                )
            ],
            [],  # dual-race bonus generations
            [],  # images
        ],
    )

    await workflows._sync_workflow_outputs(db, run)  # noqa: SLF001

    assert showcase_step.status == "failed"
    assert showcase_step.output_json["canceled_generation_ids"] == ["gen-1"]
    assert "failed_generation_ids" not in showcase_step.output_json
    assert showcase_step.output_json["error_message"] == "展示图生成失败或取消"
    assert run.status == "failed"


@pytest.mark.asyncio
async def test_reopen_model_selection_resets_downstream_and_clears_quality_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        current_step="delivery",
        status="completed",
    )
    selected = SimpleNamespace(
        id="cand-1",
        status="selected",
        contact_sheet_image_id="model-1",
        selected_at=datetime.now(timezone.utc),
    )
    rejected = SimpleNamespace(
        id="cand-2",
        status="rejected",
        contact_sheet_image_id="model-2",
        selected_at=None,
    )
    steps = {
        "model_approval": SimpleNamespace(
            status="approved",
            input_json={
                "accessory_plan": {
                    "enabled": True,
                    "items": ["bag"],
                    "strength": "subtle",
                },
                "style_prompt": "clean studio",
            },
            output_json={"selected_candidate_id": "cand-1"},
            task_ids=["acc-task"],
            image_ids=["acc-image"],
            approved_at=datetime.now(timezone.utc),
            approved_by="user-1",
        ),
        "model_candidates": SimpleNamespace(
            status="needs_review",
            input_json={},
            output_json={},
            task_ids=[],
            image_ids=[],
        ),
        "model_settings": SimpleNamespace(output_json={}),
        "product_analysis": SimpleNamespace(output_json={}),
        "showcase_generation": SimpleNamespace(
            status="completed",
            input_json={"template": "premium_studio"},
            output_json={"some": "result"},
            task_ids=["showcase-task"],
            image_ids=["showcase-image"],
        ),
        "quality_review": SimpleNamespace(
            status="approved",
            input_json={"latest_revision": {}},
            output_json={"overall": "approve"},
            task_ids=["quality-task"],
            image_ids=["showcase-image"],
        ),
        "delivery": SimpleNamespace(
            status="completed",
            input_json={"final_image_ids": ["showcase-image"]},
            output_json={"download_image_ids": ["showcase-image"]},
            task_ids=[],
            image_ids=["showcase-image"],
        ),
    }

    async def fake_get_run(
        db: Any, *, user_id: str, run_id: str, lock: bool = False
    ) -> Any:
        assert user_id == "user-1"
        assert run_id == "run-1"
        assert lock is True
        return run

    async def fake_sync(db: Any, current_run: Any) -> None:
        assert current_run is run

    async def fake_step(db: Any, run_id: str, step_key: str) -> Any:
        assert run_id == "run-1"
        return steps[step_key]

    async def fake_build_run_out(db: Any, current_run: Any) -> Any:
        return current_run

    monkeypatch.setattr(workflows, "_get_run", fake_get_run)
    monkeypatch.setattr(workflows, "_sync_workflow_outputs", fake_sync)
    monkeypatch.setattr(workflows, "_step", fake_step)
    monkeypatch.setattr(workflows, "_build_run_out", fake_build_run_out)
    db = _Db([], responses=[[selected, rejected], []])

    out = await workflows.reopen_model_selection(
        "run-1",
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
    )

    assert out is run
    assert selected.status == "ready"
    assert selected.selected_at is None
    assert rejected.status == "ready"
    assert steps["model_approval"].status == "needs_review"
    assert steps["model_approval"].input_json == {
        "accessory_plan": {"enabled": True, "items": ["bag"], "strength": "subtle"},
        "style_prompt": "clean studio",
    }
    for step_key in ("showcase_generation", "quality_review", "delivery"):
        step = steps[step_key]
        assert step.status == "waiting_input"
        assert step.input_json == {}
        assert step.output_json == {}
        assert step.task_ids == []
        assert step.image_ids == []
    assert run.current_step == "model_candidates"
    assert run.status == "needs_review"
    assert db.committed is True
    assert any(
        "DELETE FROM quality_reports" in str(statement) for statement in db.statements
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
    assert "明亮松弛的日常随拍氛围" in prompt
    assert "本张重点清楚：lapel shape、button position、pocket placement" in prompt
    assert "少量自然搭配" in prompt
    assert "small earrings" not in prompt
    assert "优先参考它" in prompt
    assert "不要抢衣服主体" in prompt
    assert "杂志大片质感" in prompt
    assert "皮肤真实有毛孔和细纹" in prompt
    assert "不要塑料感、过度磨皮、AI网红脸" in prompt
    assert "正面全身" in prompt
    assert "自然商业摄影风格" in prompt
    assert "服装主体清晰可见" in prompt
    assert "全身完整入镜" in prompt
    assert "顶满" in prompt
    assert len(prompt) < 900


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
    assert "身高 128cm" in prompt
    assert "头身比" in prompt
    assert "肢体长度" in prompt


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
    assert "本张重点清楚：颜色、版型、款式" in prompt


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
    assert "上身近景" in prompts["detail_half_body"]
    assert "另一张" not in prompts["detail_half_body"]
    for prompt in prompts.values():
        assert "戏剧化" in prompt or "时装大片" in prompt
        assert "扶袖口" not in prompt
    assert len(set(prompts.values())) == len(workflows.DEFAULT_SHOT_PLAN)


def test_showcase_prompt_includes_scene_card_direction_and_garment_lock() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model", "height_cm": 168},
    )
    scene_card = {
        "id": "street-01",
        "scene_family": "urban_street",
        "location": "城市斑马线",
        "micro_event": "牵狗过马路时回头",
        "camera": {
            "distance": "full_body",
            "angle": "high_angle",
            "lens_feel": "phone",
        },
        "pose": "小步向前",
        "motion": "自然走动",
        "props": ["狗", "牵引绳"],
        "lighting": "晴天侧光",
        "composition": "衣服胸前清楚",
        "product_visibility": "front_full_body",
        "negative": ["牵引绳不要遮挡胸前"],
    }

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["蓝色格纹", "胸袋", "纽扣"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="urban_commute",
        shot_type="front_full_body",
        final_quality="high",
        scene_card=scene_card,
    )
    composed = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["蓝色格纹", "胸袋", "纽扣"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="urban_commute",
        shot_type="front_full_body",
        final_quality="high",
        scene_card=scene_card,
        garment_lock={
            "core_identity": "蓝色格纹衬衫",
            "must_preserve": ["蓝色格纹", "胸袋", "纽扣"],
            "visibility_priority": ["正面胸口"],
            "mutation_bans": ["改颜色"],
            "occlusion_policy": "不要遮挡胸前。",
        },
        composed_prompt="城市街拍，模特牵狗过马路，衣服主体清楚。",
    )
    safe = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["蓝色格纹"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="urban_commute",
        shot_type="front_full_body",
        final_quality="high",
        scene_card=scene_card,
        garment_lock={
            "core_identity": "蓝色格纹衬衫",
            "must_preserve": ["蓝色格纹"],
            "visibility_priority": ["正面胸口"],
            "mutation_bans": ["改颜色"],
            "occlusion_policy": "不要遮挡胸前。",
        },
        allow_pet=False,
        allow_background_people=False,
    )
    sparse_scene = workflows._showcase_scene_card_direction(  # noqa: SLF001
        {"camera": {"angle": "eye_level"}}
    )

    assert "城市斑马线" in prompt
    assert "牵狗过马路" in prompt
    assert "轻微俯拍" in prompt
    assert "严格按本张拍摄方案" in prompt
    assert "50mm 标准焦段" not in prompt
    assert "低存在感宠物" in prompt
    assert "【本张拍摄方案】" in composed
    assert "蓝色格纹衬衫" in composed
    assert "本张拍摄方案必须执行" in composed
    assert "城市斑马线" in composed
    assert "轻微俯拍" in composed
    assert "牵狗过马路" in composed
    assert "商品主体清楚，不遮挡" in composed
    assert "【商品 1:1 锁定】" in safe
    assert "蓝色格纹衬衫" in safe
    assert "宠物" not in safe
    assert "路人" not in safe
    assert "None" not in sparse_scene


def test_showcase_prompt_scene_card_overrides_conflicting_template_scene() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "独立生成 · 儿童", "height_cm": 128},
    )
    scene_card = {
        "id": "detail-half-1",
        "scene_family": "designed_lifestyle",
        "location": "酒店大堂边缘的安静区域",
        "micro_event": "手指轻触衣摆边缘展示面料垂感",
        "camera": {
            "distance": "half_body",
            "angle": "eye_level",
            "lens_feel": "natural_standard",
        },
        "pose": "半身微侧，手部动作避开胸前主体",
        "motion": "手指轻整理细节，衣服纹理和结构清楚",
        "props": ["白色短袜", "浅色低帮童鞋"],
        "lighting": "酒店大堂窗边柔和侧光",
        "composition": "半身到大腿上方，胸前和衣摆都入镜",
        "product_visibility": "upper_body_detail",
        "negative": ["不要让手遮挡胸前贴布和扣饰"],
    }

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={
            "category": "女童短袖假两件背带连衣裙",
            "must_preserve": [
                "白色圆领短袖上衣",
                "浅蓝色牛仔A字裙身",
                "一红一浅黄的异色背带",
                "前胸雏菊刺绣和白色花形扣饰",
                "前片立体毛绒小熊贴布与小口袋",
                "背后交叉背带和牛仔蝴蝶结",
                "裙摆彩色波浪缝线",
                "牛仔布纹理与明线缝合感",
            ],
        },
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="urban_commute",
        shot_type="detail_half_body",
        final_quality="high",
        scene_card=scene_card,
        garment_lock={
            "core_identity": "女童短袖假两件背带连衣裙",
            "must_preserve": [
                "白色圆领短袖上衣",
                "浅蓝色牛仔A字裙身",
                "一红一浅黄的异色背带",
                "前胸雏菊刺绣和白色花形扣饰",
                "前片立体毛绒小熊贴布与小口袋",
                "背后交叉背带和牛仔蝴蝶结",
                "裙摆彩色波浪缝线",
                "牛仔布纹理与明线缝合感",
            ],
            "visibility_priority": ["正面胸口", "领口", "口袋", "袖口和袖型"],
            "mutation_bans": ["改颜色", "改廓形", "新增图案/logo"],
            "occlusion_policy": "手、头发和道具不得遮挡商品主体。",
        },
    )

    assert "酒店大堂边缘的安静区域" in prompt
    assert "手指轻触衣摆边缘展示面料垂感" in prompt
    assert "本张重点清楚" in prompt
    assert "一红一浅黄的异色背带" in prompt
    assert "前胸雏菊刺绣和白色花形扣饰" in prompt
    assert "前片立体毛绒小熊贴布与小口袋" in prompt
    assert "其它角度细节" in prompt
    assert "本张不要强求" not in prompt
    assert "街边花坛" not in prompt
    assert "户外日光带明确方向" not in prompt
    assert "真实街头摄影质感" not in prompt
    assert "半身到大腿上方构图" in prompt
    assert "真实自然儿童摄影质感" in prompt
    assert "低存在感宠物" not in prompt
    assert "远处路人" not in prompt


def test_showcase_prompt_composed_scene_card_appends_conflict_guardrails() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "独立生成 · 儿童", "height_cm": 128},
    )
    scene_card = {
        "id": "detail-half-1",
        "scene_family": "designed_lifestyle",
        "location": "酒店大堂边缘的安静区域",
        "micro_event": "轻触衣摆边缘",
        "camera": {"distance": "half_body", "angle": "eye_level"},
        "pose": "半身微侧",
        "motion": "手指轻整理细节",
        "lighting": "窗边柔和侧光",
        "composition": "半身到大腿上方",
        "product_visibility": "upper_body_detail",
        "negative": ["不要遮挡胸前"],
    }

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["正面刺绣", "背后蝴蝶结", "裙摆彩色线"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="urban_commute",
        shot_type="detail_half_body",
        final_quality="high",
        scene_card=scene_card,
        garment_lock={
            "core_identity": "女童背带裙",
            "must_preserve": ["正面刺绣", "背后蝴蝶结", "裙摆彩色线"],
            "visibility_priority": ["正面胸口"],
            "mutation_bans": ["改颜色"],
            "occlusion_policy": "不要遮挡胸前。",
        },
        composed_prompt="街边花坛旁轻扶肩带，户外日光街拍。",
    )

    assert "【本张拍摄方案】" in prompt
    assert "街边花坛旁轻扶肩带" in prompt
    assert "最终画面只采用上方短摄影方案" in prompt
    assert "不得混入其它地点" in prompt
    assert "商品主体清楚，不遮挡" in prompt
    assert "其它角度细节" in prompt
    assert "本张不要强求" not in prompt
    assert "本张画面范围" not in prompt
    assert "真实自然儿童摄影质感" not in prompt


def test_showcase_prompt_expands_gpt55_scene_details_without_internal_terms() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "独立生成 · 儿童", "height_cm": 128},
    )
    scene_card = {
        "id": "side-back-1",
        "scene_family": "premium_studio",
        "location": "灰白墙面和木地板的高级棚拍空间",
        "micro_event": "背向前走半步后自然回望",
        "camera": {
            "distance": "full_body",
            "angle": "side_or_back",
            "lens_feel": "natural_standard",
        },
        "pose": "侧身站位，肩背轮廓完整",
        "motion": "轻微转身带出侧面或背面廓形",
        "props": ["白色短袜", "浅色低帮童鞋"],
        "lighting": "自然窗光或柔和室内暖光，方向明确不过曝",
        "composition": "侧面或背面廓形清楚，人物完整不切断",
        "product_visibility": "side_or_back_silhouette",
        "environment_detail": "灰白墙面、木地板和远处柔和墙角形成真实空间深度",
        "lighting_detail": "主光从左前方侧窗落下，肩背和裙摆有柔和明暗层次",
        "camera_detail": "平视全身机位，镜头与人物保持真实距离，头脚完整不切断",
        "composition_detail": "转身方向一侧留出空间，背面结构不被头发遮挡",
        "creative_intent": "用转身回望的瞬间制造侧背面廓形张力",
        "natural_detail": "回望幅度很小，像走动中自然被叫住，不做舞台式扭身",
        "negative": ["画面里不要出现镜子或镜面反射"],
    }

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={
            "category": "女童短袖假两件背带连衣裙",
            "must_preserve": [
                "白色圆领短袖上衣",
                "浅蓝色牛仔A字裙身",
                "一红一浅黄的异色背带",
                "前胸雏菊刺绣和白色花形扣饰",
                "前片立体毛绒小熊贴布与小口袋",
                "背后交叉背带和牛仔蝴蝶结",
                "裙摆彩色波浪缝线",
                "牛仔布纹理与明线缝合感",
            ],
        },
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="premium_studio",
        shot_type="side_or_back",
        final_quality="high",
        scene_card=scene_card,
        garment_lock={
            "core_identity": "女童短袖假两件背带连衣裙",
            "must_preserve": [
                "白色圆领短袖上衣",
                "浅蓝色牛仔A字裙身",
                "一红一浅黄的异色背带",
                "前胸雏菊刺绣和白色花形扣饰",
                "前片立体毛绒小熊贴布与小口袋",
                "背后交叉背带和牛仔蝴蝶结",
                "裙摆彩色波浪缝线",
                "牛仔布纹理与明线缝合感",
            ],
            "visibility_priority": ["正面胸口", "领口", "口袋", "袖口和袖型"],
            "mutation_bans": ["改颜色", "改廓形", "新增图案/logo"],
            "occlusion_policy": "手、头发和道具不得遮挡商品主体。",
        },
    )

    assert "SceneCard" not in prompt
    assert prompt.count("【商品 1:1 还原") == 0
    assert prompt.count("【商品 1:1 锁定】") == 1
    assert "灰白墙面、木地板和远处柔和墙角形成真实空间深度" in prompt
    assert "主光从左前方侧窗落下" in prompt
    assert "平视全身机位，镜头与人物保持真实距离" in prompt
    assert "转身方向一侧留出空间" in prompt
    assert "摄影意图" in prompt
    assert "用转身回望的瞬间制造侧背面廓形张力" in prompt
    assert "回望幅度很小" in prompt
    assert "背后交叉背带和牛仔蝴蝶结" in prompt
    assert "其它角度细节" in prompt
    assert "本张不要强求" not in prompt


def test_showcase_prompt_clamps_oversized_garment_lock() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model", "height_cm": 168},
    )
    long_item = "蓝色格纹" * 80
    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": [long_item] * 32},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="urban_commute",
        shot_type="front_full_body",
        final_quality="high",
        garment_lock={
            "core_identity": "蓝色格纹衬衫",
            "must_preserve": [long_item] * 32,
            "visibility_priority": [long_item] * 32,
            "mutation_bans": [long_item] * 32,
            "occlusion_policy": long_item,
        },
        composed_prompt="自然街拍，衣服主体清楚。" * 2000,
    )

    assert len(prompt) <= MAX_PROMPT_CHARS


@pytest.mark.asyncio
async def test_prepare_showcase_preflight_runs_gpt55_merged_director(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model", "height_cm": 168},
    )
    shot_picks = workflows._showcase_pick_shot_variants(  # noqa: SLF001
        template="urban_commute",
        age_segment="young_adult",
        output_count=2,
        seed_key="preflight-test",
    )

    async def fake_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        cards = [
            {
                "id": f"scene-{idx}",
                "scene_family": "urban_street",
                "location": "街角",
                "micro_event": f"自然事件 {idx}",
                "camera": {"distance": "full_body", "angle": "eye_level"},
                "pose": "自然站定",
                "motion": "小步",
                "props": [],
                "lighting": "侧光",
                "composition": "衣服清楚",
                "product_visibility": "front_full_body",
                "shooting_brief": (
                    f"窗边侧光拍摄方案 scene-{idx}，按照自然事件展开，"
                    "人物在真实空间里小步停住，衣服主体清楚。"
                ),
                "negative": ["不要遮挡商品"],
                "fingerprint": f"fp-{idx}",
            }
            for idx in range(1, 3)
        ]
        return {
            "planner": "gpt55_preflight",
            "planner_status": "ok",
            "series_concept": "城市街拍",
            "continuity_anchors": [],
            "scene_cards": cards,
            "scene_fingerprints": ["fp-1", "fp-2"],
            "risk_notes": [],
            "fallback_reason": None,
        }

    async def fake_review(*args: Any, **kwargs: Any) -> dict[str, Any]:
        scene_card = kwargs["scene_card"]
        return {
            "scene_card_id": scene_card["id"],
            "status": "ok",
            "risk_level": "low",
            "risks": [],
            "must_rewrite": False,
            "rewrite_instruction": "",
            "fallback_reason": None,
        }

    monkeypatch.setattr(workflows, "_plan_scene_cards_with_gpt55", fake_plan)
    monkeypatch.setattr(workflows, "_review_prompt_risk_with_gpt55", fake_review)
    preflight = await workflows._prepare_showcase_preflight(  # noqa: SLF001
        db=SimpleNamespace(),  # type: ignore[arg-type]
        product_analysis={"category": "衬衫", "must_preserve": ["蓝色格纹", "胸袋"]},
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
        scene_variety="rich",
        scene_planner="gpt55_preflight",
        continuity_anchor="accessory",
        allow_pet=False,
        allow_background_people=True,
    )

    assert preflight["planning"]["planner"] == "gpt55_preflight"
    assert len(preflight["scene_cards"]) == 2
    assert len(preflight["per_image_prompts"]) == 2
    assert len(preflight["prompt_reviews"]) == 2
    assert preflight["prompt_reviews"][0]["status"] == "ok"
    assert preflight["prompt_reviews"][0]["risk_level"] == "low"
    assert preflight["per_image_prompts"][0]["status"] == "director_batch"
    assert "【本张拍摄方案】" in preflight["final_prompts"][0]
    assert "窗边侧光拍摄方案 scene-1" in preflight["final_prompts"][0]
    assert "蓝色格纹" in preflight["final_prompts"][0]
    assert "本张拍摄方案必须执行" in preflight["final_prompts"][0]
    assert "自然事件 1" in preflight["final_prompts"][0]


@pytest.mark.asyncio
async def test_prepare_showcase_preflight_retries_provider_resolution_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model", "height_cm": 168},
    )
    shot_picks = workflows._showcase_pick_shot_variants(  # noqa: SLF001
        template="urban_commute",
        age_segment="young_adult",
        output_count=1,
        seed_key="provider-retry-test",
    )
    seen_provider_orders: list[Any] = []

    async def fake_resolve(*args: Any, **kwargs: Any) -> list[Any]:
        raise RuntimeError("db temporarily unavailable")

    async def fake_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen_provider_orders.append(kwargs.get("provider_order"))
        return workflows._rules_fallback_scene_planning(  # noqa: SLF001
            product_analysis={"category": "衬衫"},
            template="urban_commute",
            scene_environment="outdoor",
            shot_picks=[(cls, dict(variant)) for cls, variant in shot_picks],
            aspect_ratio="4:5",
            user_prompt="自然街拍",
            accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
            allow_pet=False,
            continuity_anchor="accessory",
        )

    monkeypatch.setattr(workflows, "_resolve_scene_provider_order", fake_resolve)
    monkeypatch.setattr(workflows, "_plan_scene_cards_with_gpt55", fake_plan)

    await workflows._prepare_showcase_preflight(  # noqa: SLF001
        db=SimpleNamespace(),  # type: ignore[arg-type]
        product_analysis={"category": "衬衫", "must_preserve": ["蓝色格纹"]},
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
        scene_variety="rich",
        scene_planner="gpt55_batch_only",
        continuity_anchor="accessory",
        allow_pet=False,
        allow_background_people=True,
    )

    assert seen_provider_orders == [None]


@pytest.mark.asyncio
async def test_prepare_showcase_preflight_reviews_director_brief_without_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model", "height_cm": 168},
    )
    shot_picks = workflows._showcase_pick_shot_variants(  # noqa: SLF001
        template="urban_commute",
        age_segment="young_adult",
        output_count=1,
        seed_key="rewrite-test",
    )
    scene_card = {
        "id": "scene-risky",
        "scene_family": "urban_street",
        "location": "街角",
        "micro_event": "手拿饮料靠近胸前",
        "camera": {"distance": "full_body", "angle": "eye_level"},
        "pose": "自然站定",
        "motion": "小步",
        "props": ["饮料"],
        "lighting": "侧光",
        "composition": "衣服清楚",
        "product_visibility": "front_full_body",
        "shooting_brief": "初稿拍摄方案，饮料靠近胸前，可能遮挡商品。",
        "negative": ["不要遮挡商品"],
        "fingerprint": "fp-risky",
    }

    async def fake_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "planner": "gpt55_preflight",
            "planner_status": "ok",
            "series_concept": "城市街拍",
            "continuity_anchors": [],
            "scene_cards": [scene_card],
            "scene_fingerprints": ["fp-risky"],
            "risk_notes": [],
            "fallback_reason": None,
        }

    async def fake_review(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "scene_card_id": scene_card["id"],
            "status": "ok",
            "risk_level": "low",
            "risks": [],
            "must_rewrite": False,
            "rewrite_instruction": "",
            "fallback_reason": None,
        }

    monkeypatch.setattr(workflows, "_plan_scene_cards_with_gpt55", fake_plan)
    monkeypatch.setattr(workflows, "_review_prompt_risk_with_gpt55", fake_review)

    preflight = await workflows._prepare_showcase_preflight(  # noqa: SLF001
        db=SimpleNamespace(),  # type: ignore[arg-type]
        product_analysis={"category": "衬衫", "must_preserve": ["蓝色格纹", "胸袋"]},
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
        scene_variety="rich",
        scene_planner="gpt55_preflight",
        continuity_anchor="accessory",
        allow_pet=False,
        allow_background_people=True,
    )

    assert preflight["prompt_reviews"][0]["status"] == "ok"
    assert preflight["prompt_reviews"][0]["risk_level"] == "low"
    assert preflight["per_image_prompts"][0]["status"] == "director_batch"
    assert "初稿拍摄方案" in preflight["final_prompts"][0]
    assert "改写后的安全拍摄方案" not in preflight["final_prompts"][0]


@pytest.mark.asyncio
async def test_prepare_showcase_preflight_reports_composer_review_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model", "height_cm": 168},
    )
    shot_picks = workflows._showcase_pick_shot_variants(  # noqa: SLF001
        template="urban_commute",
        age_segment="young_adult",
        output_count=1,
        seed_key="progress-hook-test",
    )
    scene_card = {
        "id": "scene-progress",
        "scene_family": "urban_street",
        "location": "街角",
        "micro_event": "整理袖口",
        "camera": {"distance": "full_body", "angle": "eye_level"},
        "pose": "自然站定",
        "motion": "小步",
        "props": [],
        "lighting": "侧光",
        "composition": "衣服清楚",
        "product_visibility": "front_full_body",
        "shooting_brief": "街角侧光下自然整理袖口，商品主体清楚。",
        "negative": ["不要遮挡商品"],
        "fingerprint": "fp-progress",
    }
    progress_events: list[tuple[str, str, int | None, int | None]] = []

    async def fake_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "planner": "gpt55_preflight",
            "planner_status": "ok",
            "series_concept": "城市街拍",
            "continuity_anchors": [],
            "scene_cards": [scene_card],
            "scene_fingerprints": ["fp-progress"],
            "risk_notes": [],
            "fallback_reason": None,
        }

    async def fake_review(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "scene_card_id": scene_card["id"],
            "status": "ok",
            "risk_level": "low",
            "risks": [],
            "must_rewrite": False,
            "rewrite_instruction": "",
            "fallback_reason": None,
        }

    async def progress_hook(
        phase: str,
        detail: str,
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        progress_events.append((phase, detail, current, total))

    monkeypatch.setattr(workflows, "_plan_scene_cards_with_gpt55", fake_plan)
    monkeypatch.setattr(workflows, "_review_prompt_risk_with_gpt55", fake_review)
    await workflows._prepare_showcase_preflight(  # noqa: SLF001
        db=SimpleNamespace(),  # type: ignore[arg-type]
        product_analysis={"category": "衬衫", "must_preserve": ["蓝色格纹"]},
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
        scene_variety="rich",
        scene_planner="gpt55_preflight",
        continuity_anchor="accessory",
        allow_pet=False,
        allow_background_people=True,
        progress_hook=progress_hook,
    )

    phases = [event[0] for event in progress_events]
    assert "director" in phases
    assert "composer" in phases
    assert "review" in phases
    assert phases[-1] == "dispatching"


def test_guarded_shooting_brief_allows_scene_rewrite_without_old_scene() -> None:
    guarded = workflows._guarded_shooting_brief(  # noqa: SLF001
        "咖啡店窗边拍摄，模特拿咖啡杯靠近胸前，窗边侧光。",
        rewrite_instruction=(
            "更换场景和构图以避免重复，改为图书馆过道自然整理衣袖。"
        ),
    )

    assert "咖啡店窗边" not in guarded
    assert "拿咖啡杯靠近胸前" not in guarded
    assert "保留上方摄影方案里的场景" not in guarded
    assert "图书馆过道" in guarded


def test_guarded_shooting_brief_preserves_safe_motion_energy() -> None:
    guarded = workflows._guarded_shooting_brief(  # noqa: SLF001
        "模特向镜头走近，脚步刚落地，衣摆和发丝有自然摆动。",
        rewrite_instruction=(
            "改为稳定的正面或三分之二正面站定展示，只保留轻微落步感；"
            "双手远离胸口和裙身主体。"
        ),
    )

    assert "稳定的正面或三分之二正面站定展示" not in guarded
    assert "只保留轻微落步感" not in guarded
    assert "安全动态抓拍" in guarded
    assert "保留安全动态能量" in guarded
    assert "不要退回僵硬静态站姿" in guarded
    assert "双手保持低位或打开在身体两侧" in guarded


@pytest.mark.asyncio
async def test_prepare_showcase_preflight_uses_director_brief_for_risky_scene(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "clean commute model", "height_cm": 168},
    )
    shot_picks = workflows._showcase_pick_shot_variants(  # noqa: SLF001
        template="urban_commute",
        age_segment="young_adult",
        output_count=1,
        seed_key="fallback-test",
    )
    scene_card = {
        "id": "scene-risky",
        "scene_family": "urban_street",
        "location": "街角",
        "micro_event": "手拿饮料靠近胸前",
        "camera": {"distance": "full_body", "angle": "eye_level"},
        "pose": "自然站定",
        "motion": "小步",
        "props": ["饮料"],
        "lighting": "侧光",
        "composition": "衣服清楚",
        "product_visibility": "front_full_body",
        "shooting_brief": "高风险拍摄方案，手和饮料仍在胸前。",
        "negative": ["不要遮挡商品"],
        "fingerprint": "fp-risky",
    }

    async def fake_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "planner": "gpt55_preflight",
            "planner_status": "ok",
            "series_concept": "城市街拍",
            "continuity_anchors": [],
            "scene_cards": [scene_card],
            "scene_fingerprints": ["fp-risky"],
            "risk_notes": [],
            "fallback_reason": None,
        }

    async def fake_compose(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "scene_card_id": scene_card["id"],
            "status": "ok",
            "shooting_brief": "高风险拍摄方案，手和饮料仍在胸前。",
            "final_prompt": "高风险拍摄方案，手和饮料仍在胸前。",
            "candidate_briefs": ["高风险拍摄方案，手和饮料仍在胸前。"],
            "selected_candidate_index": None,
            "selection_scores": [],
            "scene_keywords": [],
            "composition_keywords": [],
            "lighting_keywords": [],
            "action_keywords": [],
            "photographic_idea_keywords": [],
            "product_visibility_checklist": [],
            "negative_prompt_notes": [],
            "regenerate_if": [],
            "fallback_reason": None,
        }

    async def fake_review(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "scene_card_id": scene_card["id"],
            "status": "ok",
            "risk_level": "high",
            "risks": ["仍可能遮挡商品"],
            "must_rewrite": True,
            "rewrite_instruction": "移开饮料",
            "fallback_reason": None,
        }

    monkeypatch.setattr(workflows, "_plan_scene_cards_with_gpt55", fake_plan)
    monkeypatch.setattr(workflows, "_compose_image_prompt_with_gpt55", fake_compose)
    monkeypatch.setattr(workflows, "_review_prompt_risk_with_gpt55", fake_review)

    preflight = await workflows._prepare_showcase_preflight(  # noqa: SLF001
        db=SimpleNamespace(),  # type: ignore[arg-type]
        product_analysis={"category": "衬衫", "must_preserve": ["蓝色格纹", "胸袋"]},
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
        scene_variety="rich",
        scene_planner="gpt55_preflight",
        continuity_anchor="accessory",
        allow_pet=False,
        allow_background_people=True,
    )

    assert preflight["prompt_reviews"][0]["guarded_composer"] is True
    assert preflight["per_image_prompts"][0]["status"] == "guarded"
    assert preflight["per_image_prompts"][0]["risk_guard_applied"] is True
    assert "高风险拍摄方案" in preflight["final_prompts"][0]
    assert "安全覆盖" in preflight["final_prompts"][0]
    assert "移开饮料" in preflight["final_prompts"][0]
    assert "本张场景种子" not in preflight["final_prompts"][0]
    assert "【商品 1:1 锁定】" in preflight["final_prompts"][0]


@pytest.mark.asyncio
async def test_prepare_showcase_preflight_timeout_falls_back_to_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_wait_for(awaitable: Any, timeout: float) -> Any:
        assert timeout == 840.0
        awaitable.close()
        raise asyncio.TimeoutError

    async def fake_impl(**kwargs: Any) -> dict[str, Any]:
        return {
            "garment_lock": {},
            "planning": {"planner": kwargs["scene_planner"]},
            "scene_cards": [],
            "per_image_prompts": [],
            "prompt_reviews": [],
            "final_prompts": [],
        }

    monkeypatch.setattr(workflows.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(workflows, "_prepare_showcase_preflight_impl", fake_impl)

    preflight = await workflows._prepare_showcase_preflight(  # noqa: SLF001
        db=SimpleNamespace(),  # type: ignore[arg-type]
        product_analysis={},
        selected_candidate=SimpleNamespace(model_brief_json={}),  # type: ignore[arg-type]
        accessory_plan={},
        template="urban_commute",
        shot_picks=[],
        age_segment=None,
        final_quality="high",
        user_prompt="",
        aspect_ratio="4:5",
        scene_environment="outdoor",
        scene_strategy="natural_series",
        scene_variety="rich",
        scene_planner="gpt55_preflight",
        continuity_anchor="accessory",
        allow_pet=False,
        allow_background_people=True,
    )

    assert preflight["planning"]["planner"] == "rules_fallback"
    assert preflight["planning"]["requested_planner"] == "gpt55_preflight"
    assert preflight["planning"]["fallback_reason"] == "preflight_timeout_after_840s"
    assert preflight["preflight_timed_out"] is True


@pytest.mark.asyncio
async def test_prompt_risk_review_treats_string_false_as_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "risk_level": "low",
            "risks": [],
            "must_rewrite": "false",
            "rewrite_instruction": "",
        }

    monkeypatch.setattr(scene_planner, "_call_gpt55_json", fake_call)

    review = await scene_planner.review_prompt_risk_with_gpt55(
        SimpleNamespace(),  # type: ignore[arg-type]
        final_prompt="商品主体清楚，动作简单。",
        garment_lock={"must_preserve": ["蓝色格纹"]},
        scene_card={"id": "scene-1"},
        batch_context={},
    )

    assert review["risk_level"] == "low"
    assert review["must_rewrite"] is False


@pytest.mark.asyncio
async def test_prompt_risk_review_preserves_safe_dynamic_motion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["instructions"] = kwargs["instructions"]
        return {
            "risk_level": "medium",
            "risks": ["手部接近商品主体"],
            "must_rewrite": True,
            "rewrite_instruction": "双手保持低位，保留落步动态。",
        }

    monkeypatch.setattr(scene_planner, "_call_gpt55_json", fake_call)

    review = await scene_planner.review_prompt_risk_with_gpt55(
        SimpleNamespace(),  # type: ignore[arg-type]
        final_prompt="向镜头走近，脚步刚落地，衣摆有自然摆动。",
        garment_lock={"must_preserve": ["胸前图案"]},
        scene_card={"id": "scene-1"},
        batch_context={},
    )

    assert review["must_rewrite"] is True
    assert "中等动态本身不是风险" in captured["instructions"]
    assert "禁止要求改成“稳定站定”" in captured["instructions"]
    assert "安全动态抓拍" in captured["instructions"]


def test_accessory_preview_prompt_is_model_quad_with_accessories_only() -> None:
    prompt = workflows._accessory_preview_prompt(  # noqa: SLF001
        accessory_plan={
            "items": ["small earrings", "white sneakers"],
            "strength": "subtle",
        },
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
    assert "西装外套" in prompt
    assert "羊毛混纺" not in prompt
    assert "boutique hotel lobby" not in prompt
    assert "从容" in prompt or "空间感" in prompt


def test_daily_snapshot_template_uses_phone_realistic_scene() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "熟龄女性，欧美，自然日常"},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={
            "category": "针织上衣",
            "must_preserve": ["浅灰色", "短款版型"],
        },
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


def test_natural_phone_snapshot_template_uses_real_phone_constraints() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "8岁儿童，亚洲，自然童装模特"},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={
            "category": "童装连衣裙",
            "must_preserve": ["蓝色薄纱", "蓬蓬裙摆"],
            "background_recommendation": "明亮温馨的儿童房或木质客厅",
        },
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": True, "items": ["帆布包"], "strength": "subtle"},
        template="natural_phone_snapshot",
        shot_type="natural_pose",
        final_quality="high",
    )

    assert "真实手机竖屏随手拍" in prompt
    assert "平视或自然手持视角" in prompt
    assert "明亮温馨的儿童房或木质客厅" in prompt
    assert "氛围跟童装连衣裙搭配" in prompt
    assert "姿态自然松弛" in prompt
    assert "姿势生动活泼有活力" not in prompt
    assert "俯拍" not in prompt
    assert "高机位" not in prompt
    assert "自然窗光或柔和室内暖光" in prompt
    assert "自然碎发" in prompt
    assert "衣服真实褶皱" in prompt
    assert "社交媒体截图界面" in prompt
    assert "本张重点清楚：蓝色薄纱、蓬蓬裙摆" in prompt
    assert "帆布包" not in prompt
    assert "超写实商业摄影" not in prompt
    assert "适合亚马逊电商主图" not in prompt


def test_natural_phone_snapshot_falls_back_to_category_scene() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "都市轻熟女，自然通勤"},
    )

    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"category": "针织开衫", "must_preserve": ["米白色"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="natural_phone_snapshot",
        shot_type="front_full_body",
        final_quality="high",
    )

    assert "与针织开衫风格搭配的真实生活空间" in prompt
    assert "氛围跟针织开衫搭配" in prompt
    assert "姿态自然松弛" in prompt
    assert "俯拍" not in prompt
    assert "不要棚拍" in prompt


def test_showcase_prompt_scene_environment_indoor_keeps_default_scene() -> None:
    """3 个生活化模板 indoor 时保持原场景 prompt（向后兼容）。"""
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "natural model", "height_cm": 168},
    )
    common = dict(
        product_analysis={"category": "针织开衫", "must_preserve": ["米白色"]},
        selected_candidate=candidate,
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        shot_type="front_full_body",
        final_quality="high",
    )
    for template in ("daily_snapshot", "natural_phone_snapshot", "social_seed"):
        default_prompt = workflows._showcase_prompt(template=template, **common)  # noqa: SLF001
        indoor_prompt = workflows._showcase_prompt(  # noqa: SLF001
            template=template, scene_environment="indoor", **common
        )
        assert default_prompt == indoor_prompt
        assert "户外" not in indoor_prompt


def test_showcase_prompt_scene_environment_outdoor_branches_for_lifestyle_templates() -> (
    None
):
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "natural model", "height_cm": 168},
    )
    common = dict(
        product_analysis={"category": "针织开衫", "must_preserve": ["米白色"]},
        selected_candidate=candidate,
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        shot_type="front_full_body",
        final_quality="high",
    )
    for template, outdoor_keyword in (
        ("daily_snapshot", "户外随拍"),
        ("natural_phone_snapshot", "户外随手拍"),
        ("social_seed", "户外种草"),
    ):
        outdoor_prompt = workflows._showcase_prompt(  # noqa: SLF001
            template=template, scene_environment="outdoor", **common
        )
        indoor_prompt = workflows._showcase_prompt(  # noqa: SLF001
            template=template, scene_environment="indoor", **common
        )
        assert outdoor_keyword in outdoor_prompt
        assert outdoor_keyword not in indoor_prompt
        assert "自然日光" in outdoor_prompt
        assert outdoor_prompt != indoor_prompt


def test_showcase_prompt_scene_environment_ignored_for_other_templates() -> None:
    """非 3 个生活化模板时，indoor/outdoor 输出相同。"""
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "natural model", "height_cm": 168},
    )
    common = dict(
        product_analysis={"category": "针织开衫", "must_preserve": ["米白色"]},
        selected_candidate=candidate,
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        shot_type="front_full_body",
        final_quality="high",
    )
    for template in ("white_ecommerce", "premium_studio", "urban_commute", "lifestyle"):
        indoor = workflows._showcase_prompt(  # noqa: SLF001
            template=template, scene_environment="indoor", **common
        )
        outdoor = workflows._showcase_prompt(  # noqa: SLF001
            template=template, scene_environment="outdoor", **common
        )
        assert indoor == outdoor


def test_showcase_pose_direction_per_template() -> None:
    pairs = {
        "white_ecommerce": "舒展自然",
        "premium_studio": "戏剧化",
        "urban_commute": "街头抓拍感",
        "lifestyle": "空间感",
        "daily_snapshot": "朋友视角",
        "natural_phone_snapshot": "平视手持",
        "social_seed": "互动展示",
    }
    for template, keyword in pairs.items():
        assert keyword in workflows._showcase_pose_direction(template)  # noqa: SLF001


def test_age_soft_constraint_applied_to_pose_direction() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "60岁银发女性"},
    )
    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["米白色"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="premium_studio",
        shot_type="natural_pose",
        final_quality="high",
        age_segment="senior",
    )
    assert "姿态温和稳重" in prompt


def test_kids_pool_routes_to_child_band_for_child_segment() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "8岁儿童，活泼自然"},
    )
    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"category": "童装连衣裙", "must_preserve": ["蓝色薄纱"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="premium_studio",
        shot_type="natural_pose",
        final_quality="high",
        age_segment="child",
    )
    # 童版棚拍不应出现成人池里的戏剧化成人 pose
    assert "腾空跳跃" not in prompt
    assert "单腿后踢" not in prompt
    assert "坐地半躺" not in prompt
    assert "插袋收腰" not in prompt


def test_toddler_pool_routes_to_toddler_band() -> None:
    candidate = SimpleNamespace(
        id="cand-1",
        model_brief_json={"summary": "2岁幼儿"},
    )
    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"category": "幼童 T 恤", "must_preserve": ["纯棉"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="natural_phone_snapshot",
        shot_type="front_full_body",
        final_quality="high",
        age_segment="toddler",
    )
    # 幼儿池不允许出现需要他人配合的元素
    assert "牵手" not in prompt
    assert "陪伴" not in prompt
    assert "妈妈" not in prompt


def test_pick_shot_variants_counts_match_output_count() -> None:
    for n in (1, 2, 4, 8, 16):
        picks = workflows._showcase_pick_shot_variants(  # noqa: SLF001
            template="premium_studio",
            age_segment="young_adult",
            output_count=n,
            seed_key=f"test-seed-{n}",
        )
        assert len(picks) == n, f"expected {n} picks for output_count={n}"
        product_count = sum(1 for _, v in picks if v["framing"] == "product_first")
        if n >= 2:
            assert product_count >= 2, (
                f"need >= 2 product_first for N={n}, got {product_count}"
            )
        else:
            assert product_count >= 1


def test_default_showcase_pools_avoid_high_risk_motion_terms() -> None:
    """默认展示图库不应把自然感写成高风险动作。

    跳跃、转圈、跪趴、半躺等动作会让服装物理和摄影构图同时变差；
    这类动作以后应该进显式 action/lifestyle 模式，而不是默认 showcase pool。
    """
    risky_terms = (
        "跳",
        "蹦",
        "腾空",
        "转圈",
        "盘腿",
        "蹲",
        "跪",
        "趴",
        "半躺",
        "后踢",
        "甩头",
        "双手举起",
        "伸懒腰",
        "坐地",
    )
    pools = {
        "adult": workflows.ADULT_POOL,
        "child": workflows.CHILD_POOL,
        "toddler": workflows.TODDLER_POOL,
    }
    offenders: list[str] = []
    for pool_name, pool in pools.items():
        for template, classes in pool.items():
            for shot_class, variants in classes.items():
                for variant in variants:
                    label = variant["label"]
                    if any(term in label for term in risky_terms):
                        offenders.append(
                            f"{pool_name}/{template}/{shot_class}: {label}"
                        )

    assert offenders == []


def test_small_showcase_outputs_keep_product_first_composition() -> None:
    """1-4 张通常是商品展示交付，不应抽到环境主体构图。"""
    for age_segment in ("young_adult", "child", "toddler"):
        for template in workflows.TEMPLATE_LABELS:
            for count in (1, 2, 4):
                picks = workflows._showcase_pick_shot_variants(  # noqa: SLF001
                    template=template,
                    age_segment=age_segment,
                    output_count=count,
                    seed_key=f"small-product-first:{age_segment}:{template}:{count}",
                )
                assert len(picks) == count
                assert all(v["framing"] == "product_first" for _, v in picks)


def test_pick_shot_variants_is_deterministic_for_same_seed() -> None:
    a = workflows._showcase_pick_shot_variants(  # noqa: SLF001
        template="urban_commute",
        age_segment="young_adult",
        output_count=8,
        seed_key="seed-A",
    )
    b = workflows._showcase_pick_shot_variants(  # noqa: SLF001
        template="urban_commute",
        age_segment="young_adult",
        output_count=8,
        seed_key="seed-A",
    )
    c = workflows._showcase_pick_shot_variants(  # noqa: SLF001
        template="urban_commute",
        age_segment="young_adult",
        output_count=8,
        seed_key="seed-B",
    )
    assert [v["label"] for _, v in a] == [v["label"] for _, v in b]
    assert [v["label"] for _, v in a] != [v["label"] for _, v in c]


def test_pick_shot_variants_prefers_front_views_when_output_4() -> None:
    picks = workflows._showcase_pick_shot_variants(  # noqa: SLF001
        template="lifestyle",
        age_segment="young_adult",
        output_count=4,
        seed_key="cover-test",
    )
    classes = [cls for cls, _ in picks]
    assert classes == [
        "front_full_body",
        "natural_pose",
        "detail_half_body",
        "front_full_body",
    ]
    assert "side_or_back" not in classes


@pytest.mark.parametrize(
    "template",
    [
        "urban_commute",
        "lifestyle",
        "daily_snapshot",
        "natural_phone_snapshot",
        "social_seed",
    ],
)
def test_natural_light_templates_describe_light_direction_and_contrast(
    template: str,
) -> None:
    """自然光模板要明确光线方向和明暗反差，否则模型默认渲染均匀照明 → 假感重。

    棚拍类（white/premium）有人造光，本来就工整，不在此约束。
    """
    direction = workflows._showcase_render_direction(template)  # noqa: SLF001
    direction_keywords = ("方向", "侧光", "侧面", "逆光", "斜上光", "顶光", "侧窗光")
    assert any(kw in direction for kw in direction_keywords), (
        f"{template} render direction lacks light direction cue"
    )
    contrast_keywords = ("高光", "阴影", "明暗", "光斑")
    assert any(kw in direction for kw in contrast_keywords), (
        f"{template} render direction lacks light contrast cue"
    )
    assert "全脸均匀照明" in direction


@pytest.mark.parametrize(
    "template",
    [
        "white_ecommerce",
        "premium_studio",
        "urban_commute",
        "lifestyle",
        "daily_snapshot",
        "natural_phone_snapshot",
        "social_seed",
    ],
)
def test_render_direction_includes_skin_texture_for_every_template(
    template: str,
) -> None:
    """每个模板都要给"真实皮肤毛孔/细纹"正面约束 + 反对塑料感/磨皮，避免 AI 假感。"""
    direction = workflows._showcase_render_direction(template)  # noqa: SLF001
    assert "皮肤" in direction
    assert "毛孔" in direction
    assert "塑料感" in direction or "棚拍感" in direction
    assert "磨皮" in direction
    # 每个模板都要给具体瑕疵线索，否则模型会渲染"真实但完美无瑕"的脸
    imperfection_keywords = (
        "痘印",
        "黑头",
        "非完美对称",
        "不对称",
        "色不",  # "皮肤色不均" / "色不完全均匀"
        "深浅不均",
        "细微差异",
        "细纹",
    )
    assert any(kw in direction for kw in imperfection_keywords), (
        f"{template} render direction lacks imperfection cue"
    )


def test_framing_direction_differs_between_full_body_and_detail() -> None:
    candidate = SimpleNamespace(id="c", model_brief_json={"summary": "都市轻熟女"})
    full = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["米白"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="lifestyle",
        shot_type="front_full_body",
        final_quality="high",
    )
    detail = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["米白"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="lifestyle",
        shot_type="detail_half_body",
        final_quality="high",
    )
    assert "全身完整入镜" in full
    assert "脚下完整不切断" in full
    assert "上半身或胸口以上入镜" in detail
    assert "肩部肘部不顶画面边缘" in detail


def test_framing_direction_for_tone_first_emphasizes_environment() -> None:
    """tone_first 变体可以带环境，但不能让背景压过服装主体。"""
    candidate = SimpleNamespace(id="c", model_brief_json={"summary": "都市通勤"})
    # premium_studio side_or_back 有 3 条 tone_first 变体，第一条 product 用 default 取
    # 所以这里直接构造一个 tone_first variant 测试
    tone_variant = workflows.ShotVariant(
        label="远景剪影，街景延伸为画面主体",
        framing="tone_first",
    )
    prompt = workflows._showcase_prompt(  # noqa: SLF001
        product_analysis={"must_preserve": ["深灰"]},
        selected_candidate=candidate,  # type: ignore[arg-type]
        accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
        template="urban_commute",
        shot_type="natural_pose",
        shot_variant=tone_variant,
        final_quality="high",
    )
    assert "人物占画面 55-70% 高度" in prompt
    assert "环境只作为氛围辅助" in prompt
    assert "不要让背景压过服装主体" in prompt


@pytest.mark.parametrize("age_segment", ["child", "toddler"])
def test_pick_shot_variants_returns_full_count_when_pool_smaller_than_plan(
    age_segment: str,
) -> None:
    """child/toddler 池较小，请求 16 张需循环复用变体且只少量补侧背。"""
    picks = workflows._showcase_pick_shot_variants(  # noqa: SLF001
        template="premium_studio",
        age_segment=age_segment,
        output_count=16,
        seed_key=f"kids-16-{age_segment}",
    )
    assert len(picks) == 16
    classes = [cls for cls, _ in picks]
    assert classes.count("front_full_body") == 6
    assert classes.count("natural_pose") == 4
    assert classes.count("detail_half_body") == 4
    assert classes.count("side_or_back") == 2
    # 至少 2 张 product_first（min_product_first 保证）
    product = sum(1 for _, v in picks if v["framing"] == "product_first")
    assert product >= 2


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


def test_model_library_generate_prompt_reference_mode_locks_identity() -> None:
    prompt = workflows._model_library_generate_prompt(  # noqa: SLF001
        age_segment="young_adult",
        gender="female",
        appearance_direction="east_asian",
        extra_requirements=None,
        style_tags=["温柔亲和"],
        candidate_index=1,
        reference_mode=True,
    )

    assert "Use the attached reference image ONLY" in prompt
    assert "SAME PERSON" in prompt
    assert "neutral relaxed expression" in prompt
    assert "Look anchor for this candidate" not in prompt
    assert "Variation index: 1." in prompt


def test_apparel_model_library_generate_schema_validates_modes() -> None:
    assert ApparelModelLibraryGenerateIn(age_segment="young_adult").mode == "text"

    body = ApparelModelLibraryGenerateIn(
        mode="reference_image",
        reference_image_id="img-1",
        count=1,
    )
    assert body.age_segment is None
    assert body.reference_image_id == "img-1"

    with pytest.raises(ValidationError):
        ApparelModelLibraryGenerateIn(mode="reference_image", count=1)

    with pytest.raises(ValidationError):
        ApparelModelLibraryGenerateIn(
            age_segment="young_adult",
            reference_image_id="img-1",
        )


def test_merge_reference_overrides_user_fields_win() -> None:
    body = ApparelModelLibraryGenerateIn(
        mode="reference_image",
        reference_image_id="img-1",
        age_segment="adult",
        genders=["male"],
        appearance_direction="european",
        style_tags=["用户标签"],
        count=1,
    )
    extracted = workflows.ReferenceProfile(  # noqa: SLF001
        age_segment="young_adult",
        gender="female",
        appearance_direction="east_asian",
        style_tags=["温柔亲和"],
        notes="短发",
    )

    merged = workflows._merge_reference_overrides(body, extracted)  # noqa: SLF001

    assert merged.age_segment == "adult"
    assert merged.genders == ["male"]
    assert merged.gender == "male"
    assert merged.appearance_direction == "european"
    assert merged.style_tags == ["用户标签", "温柔亲和"]


def test_reference_profile_required_fields_gate() -> None:
    body = ApparelModelLibraryGenerateIn(
        mode="reference_image",
        reference_image_id="img-1",
        count=1,
    )
    assert (
        workflows._reference_profile_has_required_text_fields(  # noqa: SLF001
            body,
            workflows.ReferenceProfile(notes="not a person"),
        )
        is False
    )
    assert (
        workflows._reference_profile_has_required_text_fields(  # noqa: SLF001
            body,
            workflows.ReferenceProfile(age_segment="young_adult", gender="female"),
        )
        is True
    )


@pytest.mark.asyncio
async def test_enqueue_model_library_reference_tasks_use_i2i_attachment_and_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_create_workflow_task(**kwargs: Any) -> tuple[Any, None, list[str]]:
        calls.append(kwargs)
        return SimpleNamespace(), None, [f"gen-{len(calls)}"]

    monkeypatch.setattr(workflows, "_create_workflow_task", fake_create_workflow_task)
    body = ApparelModelLibraryGenerateIn(
        mode="reference_image",
        reference_image_id="img-ref",
        age_segment="young_adult",
        genders=["female"],
        count=1,
    )
    step = SimpleNamespace(task_ids=[])

    _bundles, task_ids = await workflows._enqueue_model_library_generate_tasks(  # noqa: SLF001
        db=SimpleNamespace(),
        user=SimpleNamespace(id="user-1"),
        conv=SimpleNamespace(id="conv-1"),
        run=SimpleNamespace(id="run-1234567890"),
        step=step,
        body=body,
        reference_image_id="img-ref",
    )

    assert task_ids == ["gen-1"]
    assert step.task_ids == ["gen-1"]
    assert calls[0]["intent"] == workflows.Intent.IMAGE_TO_IMAGE
    assert calls[0]["attachment_ids"] == ["img-ref"]
    assert "Use the attached reference image ONLY" in calls[0]["text"]
    meta = calls[0]["workflow_meta"]
    assert meta["workflow_model_library_mode"] == "reference_image"
    assert meta["workflow_model_library_reference_image_id"] == "img-ref"
    assert meta["workflow_model_library_age_segment"] == "young_adult"


def test_model_diversity_anchor_rotates_by_candidate_index_and_gender() -> None:
    anchor = workflows._model_diversity_anchor  # noqa: SLF001

    # 同 gender 不同 index → 不同 archetype
    a1 = anchor(candidate_index=1, gender="female")
    a2 = anchor(candidate_index=2, gender="female")
    assert a1 != a2
    assert "Look anchor for this candidate" in a1
    assert "Look anchor for this candidate" in a2

    # 池子大小 8：第 9 个绕回第 1 个
    assert anchor(candidate_index=9, gender="female") == anchor(
        candidate_index=1, gender="female"
    )

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
    assert (
        f(step_status="succeeded", requested_count=4, finished_count=4) == "succeeded"
    )
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


def test_job_item_out_marks_dual_race_bonus_as_free() -> None:
    item = workflows._job_item_out(  # noqa: SLF001
        image_id="bonus-image-1",
        image_out=None,
        saved_item_id=None,
        age_segment="adult",
        gender="female",
        style_tags=[],
        appearance_direction="east_asian",
        image_meta={
            "is_dual_race_bonus": True,
            "billing_free": True,
            "billing_label": "free",
            "billing_exempt_reason": "dual_race_loser",
        },
    )
    assert item.is_dual_race_bonus is True
    assert item.billing_free is True
    assert item.billing_label == "free"
    assert item.billing_exempt_reason == "dual_race_loser"


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
