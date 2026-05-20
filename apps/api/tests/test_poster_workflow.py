"""Poster Design Workflow 后端测试。

参照 test_workflows_route.py 的纯单元 / monkeypatch 风格，避免拉真实 DB / Redis。
覆盖：
1. PosterDesignWorkflowCreateIn schema 校验（copy_text 非空 / style_id 必填）
2. PosterReviseIn 校验（inpaint 必须带 mask_image_id）
3. _poster_seed_steps 初始化 7 个 step + 文案分析进入 running
4. _poster_master_prompt prompt cache friendly 前缀稳定
5. _sync_poster_workflow_outputs 文案分析完成 → needs_review 推进
6. _sync_poster_workflow_outputs 母版全部 ready → needs_review + 选定流程
7. _sync_poster_workflow_outputs render ready → multi_size_generation needs_review
8. _do_poster_inpaint mask_image_id 必填校验（通过 schema 触发）
9. create_poster_design_workflow + masters + render 主流程 monkeypatch 串通
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.routes import workflows
from lumen_core.schemas import (
    CopyAnalysisApproveIn,
    PosterDesignWorkflowCreateIn,
    PosterInpaintIn,
    PosterMasterApproveIn,
    PosterMastersCreateIn,
    PosterRendersCreateIn,
    PosterReviseIn,
)


# ---------- minimal DB stub --------------------------------------------------


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
    def __init__(self, responses: list[list[Any]] | None = None) -> None:
        self.responses = responses if responses is not None else []
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.flushed = False
        self.committed = False

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        if self.responses:
            return _Result(self.responses.pop(0))
        return _Result([])

    def add(self, row: Any) -> None:
        self.added.append(row)
        # 模拟 SA 在 add 时立刻分配 default 主键（new_uuid7）。真实 SA 是 flush 时分配，
        # 但 _create_assistant_task / _create_workflow_task 链路里多次依赖 row.id 不为 None。
        if getattr(row, "id", None) is None:
            try:
                row.id = f"row-{len(self.added)}"
            except Exception:  # noqa: BLE001
                pass

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, row: Any) -> None:
        return None

    async def get(self, model: Any, key: Any) -> Any:
        return None


# ---------- schema-level tests ----------------------------------------------


def test_poster_create_in_requires_copy_text_and_style_id() -> None:
    body = PosterDesignWorkflowCreateIn(copy_text="限时五折", style_id="style_promo_01")
    assert body.target_aspects == ["1:1", "9:16", "16:9", "3:4"]
    assert body.quality_mode == "premium"

    with pytest.raises(ValidationError):
        PosterDesignWorkflowCreateIn(copy_text="", style_id="style_promo_01")
    with pytest.raises(ValidationError):
        PosterDesignWorkflowCreateIn(copy_text="ok", style_id="")


def test_poster_revise_in_inpaint_requires_mask() -> None:
    # scope=background 不需要 mask
    b1 = PosterReviseIn(scope="background", instruction="色调暖一点")
    assert b1.mask_image_id is None
    # scope=inpaint 必须带 mask
    with pytest.raises(ValidationError):
        PosterReviseIn(scope="inpaint", instruction="替换为红苹果")
    b2 = PosterReviseIn(
        scope="inpaint", instruction="替换为红苹果", mask_image_id="mask-1"
    )
    assert b2.mask_image_id == "mask-1"


def test_poster_inpaint_in_mask_required() -> None:
    # 直接 schema 强制 mask_image_id min_length=1
    with pytest.raises(ValidationError):
        PosterInpaintIn(instruction="hello", mask_image_id="")
    b = PosterInpaintIn(instruction="hello", mask_image_id="mask-1")
    assert b.mask_image_id == "mask-1"


def test_poster_masters_create_in_default_count() -> None:
    body = PosterMastersCreateIn()
    assert body.candidate_count == 4
    assert body.size_mode == "fixed"


def test_poster_master_approve_in_optional_adjustments() -> None:
    body = PosterMasterApproveIn()
    assert body.adjustments == ""
    body = PosterMasterApproveIn(adjustments="向暖色微调")
    assert body.adjustments == "向暖色微调"


def test_poster_renders_create_in_aspects_default() -> None:
    body = PosterRendersCreateIn()
    assert body.aspects == ["1:1", "9:16", "16:9", "3:4"]
    assert body.use_master_as_reference is True


def test_copy_analysis_approve_in_default_corrections() -> None:
    body = CopyAnalysisApproveIn()
    assert body.corrections == {}


# ---------- internal helper tests -------------------------------------------


def test_poster_workflow_steps_match_design_doc() -> None:
    assert workflows.POSTER_WORKFLOW_STEPS == [
        "copy_input",
        "style_selection",
        "copy_analysis",
        "master_generation",
        "master_approval",
        "multi_size_generation",
        "delivery",
    ]


def test_poster_seed_steps_initial_status() -> None:
    run = SimpleNamespace(
        id="run-1",
        user_prompt="限时五折，全场夏季新品，立即抢购",
        metadata_jsonb={
            "style_id": "style_promo_01",
            "target_aspects": ["1:1", "9:16"],
        },
    )
    seeded = workflows._poster_seed_steps(run)  # noqa: SLF001
    by_key = {s.step_key: s for s in seeded}
    assert set(by_key.keys()) == set(workflows.POSTER_WORKFLOW_STEPS)
    assert by_key["copy_input"].status == "approved"
    assert by_key["style_selection"].status == "approved"
    assert by_key["copy_analysis"].status == "running"
    assert by_key["master_generation"].status == "waiting_input"
    assert by_key["delivery"].status == "waiting_input"
    # copy_input.input_json 记录原文案
    assert by_key["copy_input"].input_json["copy_text"] == run.user_prompt
    # style_selection.input_json 记录风格 id 和目标比例
    assert by_key["style_selection"].input_json["style_id"] == "style_promo_01"


def test_poster_parse_copy_analysis_text_handles_valid_and_garbage() -> None:
    valid = '{"main_title":"限时五折","subtitle":"全场夏季新品","selling_points":["满200减50"],"cta":"立即抢购","price":null,"tone":"促销","info_density":"high"}'
    parsed = workflows._poster_parse_copy_analysis_text(valid)  # noqa: SLF001
    assert parsed["main_title"] == "限时五折"
    assert parsed["subtitle"] == "全场夏季新品"
    assert parsed["selling_points"] == ["满200减50"]
    assert parsed["cta"] == "立即抢购"
    assert parsed["price"] is None
    assert parsed["info_density"] == "high"
    # garbage 也能 graceful 降级
    bad = workflows._poster_parse_copy_analysis_text("not json")  # noqa: SLF001
    assert bad["info_density"] == "medium"
    assert bad["main_title"] is None
    # info_density 非法值 → fallback medium
    bad2 = workflows._poster_parse_copy_analysis_text('{"info_density":"crazy"}')  # noqa: SLF001
    assert bad2["info_density"] == "medium"


def test_poster_merge_copy_corrections_overrides_with_non_null() -> None:
    base = {
        "main_title": "ai_title",
        "subtitle": "ai_sub",
        "cta": "立即抢购",
        "info_density": "medium",
    }
    corrections = {"main_title": "user_title", "subtitle": None, "price": "¥99"}
    merged = workflows._poster_merge_copy_corrections(base, corrections)  # noqa: SLF001
    # main_title 用户改了 → 覆盖；subtitle 是 None → 保留 AI 输出；price 新增
    assert merged["main_title"] == "user_title"
    assert merged["subtitle"] == "ai_sub"
    assert merged["cta"] == "立即抢购"
    assert merged["price"] == "¥99"
    # 留有审计信息
    assert merged["user_corrections"] == corrections
    assert "confirmed_at" in merged


def test_poster_master_prompt_cache_prefix_stable() -> None:
    """prompt cache 友好：风格 + 信息密度 + 母版指令前缀必须稳定；
    只有 candidate_index 和具体文案在末尾变化。"""
    style_summary = {
        "style_id": "style_promo_01",
        "title": "促销扁平插画",
        "mood": "热闹欢快",
        "prompt_template": "flat illustration with bold typography",
        "palette": ["#ff6b35", "#1a1a1a"],
        "recommended_aspects": ["1:1"],
        "style_tags": ["flat", "promo"],
        "category": "promo",
    }
    copy_analysis = {
        "main_title": "限时五折",
        "subtitle": "全场新品",
        "selling_points": ["满200减50"],
        "cta": "立即抢购",
        "price": None,
        "info_density": "high",
    }
    p1 = workflows._poster_master_prompt(  # noqa: SLF001
        style_summary=style_summary,
        copy_analysis=copy_analysis,
        brand_assets={},
        candidate_index=1,
    )
    p2 = workflows._poster_master_prompt(  # noqa: SLF001
        style_summary=style_summary,
        copy_analysis=copy_analysis,
        brand_assets={},
        candidate_index=2,
    )
    # 前缀（指令 + 风格 + 信息密度段）应该完全一致，只有末尾候选编号不同
    common_len = 0
    for a, b in zip(p1, p2):
        if a != b:
            break
        common_len += 1
    # 公共前缀至少占总长度 80%
    assert common_len > len(p1) * 0.8, (
        f"prompt cache prefix instability: common={common_len}/{len(p1)}"
    )
    # 末尾必带 candidate index
    assert "Candidate variation number: 1" in p1
    assert "Candidate variation number: 2" in p2


def test_poster_render_prompt_includes_target_aspect_and_text_fields() -> None:
    style_summary = {"palette": ["#fff", "#000"]}
    copy_analysis = {
        "main_title": "限时五折",
        "cta": "立即抢购",
        "info_density": "medium",
    }
    p = workflows._poster_render_prompt(  # noqa: SLF001
        style_summary=style_summary,
        copy_analysis=copy_analysis,
        target_aspect="9:16",
    )
    assert "9:16" in p
    assert "限时五折" in p
    assert "立即抢购" in p
    # adjustments 可选段
    p2 = workflows._poster_render_prompt(  # noqa: SLF001
        style_summary=style_summary,
        copy_analysis=copy_analysis,
        target_aspect="9:16",
        adjustments="背景再暖一点",
    )
    assert "背景再暖一点" in p2


def test_poster_revision_prompt_inpaint_unused() -> None:
    """revision prompt 只服务 scope=background/style；inpaint 走 worker invariant 模板。"""
    p = workflows._poster_revision_prompt(  # noqa: SLF001
        style_summary={"palette": ["#fff"]},
        copy_analysis={"info_density": "low"},
        target_aspect="1:1",
        instruction="去掉左下角元素",
        scope="background",
    )
    assert "去掉左下角元素" in p
    assert "1:1" in p
    p_style = workflows._poster_revision_prompt(  # noqa: SLF001
        style_summary={"palette": ["#fff"]},
        copy_analysis={"info_density": "low"},
        target_aspect="1:1",
        instruction="换成冷色调",
        scope="style",
    )
    assert "换成冷色调" in p_style


# ---------- _sync_poster_workflow_outputs state-machine tests ---------------


@pytest.mark.asyncio
async def test_sync_promotes_copy_analysis_to_needs_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文案分析 completion 完成 → step.status=needs_review, run.current_step=copy_analysis。"""
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        type="poster_design",
        status="running",
        current_step="copy_analysis",
    )
    copy_step = SimpleNamespace(
        step_key="copy_analysis",
        status="running",
        task_ids=["c-1"],
        input_json={},
        output_json={},
    )
    completion = SimpleNamespace(
        id="c-1",
        status="succeeded",
        text='{"main_title":"hello","info_density":"low"}',
        error_code=None,
        error_message=None,
    )

    # 模拟 _load_steps 返回我们的 steps
    async def fake_load_steps(db: Any, run_id: str) -> list[Any]:
        return [copy_step]

    monkeypatch.setattr(workflows, "_load_steps", fake_load_steps)
    db = _Db(responses=[[completion], [], []])
    await workflows._sync_poster_workflow_outputs(db, run)  # type: ignore[arg-type]  # noqa: SLF001

    assert copy_step.status == "needs_review"
    assert copy_step.output_json["main_title"] == "hello"
    assert run.status == "needs_review"


@pytest.mark.asyncio
async def test_sync_marks_master_step_needs_review_when_all_masters_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        type="poster_design",
        status="running",
        current_step="master_generation",
    )
    master_step = SimpleNamespace(
        step_key="master_generation",
        status="running",
        task_ids=["g-1", "g-2"],
        input_json={"candidate_count": 2},
        output_json={},
        image_ids=[],
    )
    approval_step = SimpleNamespace(
        step_key="master_approval",
        status="waiting_input",
        task_ids=[],
        input_json={},
        output_json={},
    )
    masters = [
        SimpleNamespace(
            id="m-1",
            candidate_index=1,
            status="ready",
            image_id="img-1",
            task_ids=["g-1"],
            style_summary_json={},
            selected_at=None,
        ),
        SimpleNamespace(
            id="m-2",
            candidate_index=2,
            status="ready",
            image_id="img-2",
            task_ids=["g-2"],
            style_summary_json={},
            selected_at=None,
        ),
    ]

    async def fake_load_steps(db: Any, run_id: str) -> list[Any]:
        return [master_step, approval_step]

    monkeypatch.setattr(workflows, "_load_steps", fake_load_steps)
    # responses 顺序：master 行 + (per master image lookup + per master gens) → 简化为空
    db = _Db(responses=[masters, [], [], []])
    await workflows._sync_poster_workflow_outputs(db, run)  # type: ignore[arg-type]  # noqa: SLF001

    assert master_step.status == "needs_review"
    assert approval_step.status == "needs_review"
    assert run.current_step == "master_approval"
    assert run.status == "needs_review"


@pytest.mark.asyncio
async def test_sync_master_generation_ignores_ready_masters_from_previous_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        type="poster_design",
        status="running",
        current_step="master_generation",
    )
    master_step = SimpleNamespace(
        step_key="master_generation",
        status="running",
        task_ids=["g-new"],
        input_json={"candidate_count": 1},
        output_json={},
        image_ids=[],
    )
    approval_step = SimpleNamespace(
        step_key="master_approval",
        status="waiting_input",
        task_ids=[],
        input_json={},
        output_json={},
    )
    old_ready = SimpleNamespace(
        id="m-old",
        candidate_index=1,
        status="ready",
        image_id="img-old",
        task_ids=["g-old"],
        style_summary_json={},
        selected_at=None,
    )
    new_pending = SimpleNamespace(
        id="m-new",
        candidate_index=2,
        status="generating",
        image_id=None,
        task_ids=["g-new"],
        style_summary_json={},
        selected_at=None,
    )
    generations = [
        SimpleNamespace(id="g-old", status="succeeded"),
        SimpleNamespace(id="g-new", status="running"),
    ]

    async def fake_load_steps(db: Any, run_id: str) -> list[Any]:
        return [master_step, approval_step]

    monkeypatch.setattr(workflows, "_load_steps", fake_load_steps)
    db = _Db(responses=[[old_ready, new_pending], generations, [], []])

    await workflows._sync_poster_workflow_outputs(db, run)  # type: ignore[arg-type]  # noqa: SLF001

    assert master_step.status == "running"
    assert master_step.image_ids == []
    assert approval_step.status == "waiting_input"
    assert run.current_step == "master_generation"
    assert run.status == "running"


@pytest.mark.asyncio
async def test_sync_render_generation_ignores_ready_renders_from_previous_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """追加生成新比例时，旧 ready render 不能让当前批次提前完成。"""
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        type="poster_design",
        status="running",
        current_step="multi_size_generation",
    )
    multi_step = SimpleNamespace(
        step_key="multi_size_generation",
        status="running",
        task_ids=["g-old", "g-new"],
        input_json={"expected_render_count": 1, "active_task_ids": ["g-new"]},
        output_json={},
        image_ids=[],
    )
    old_ready = SimpleNamespace(
        id="r-old",
        aspect_ratio="1:1",
        status="ready",
        image_id="img-old",
        task_ids=["g-old"],
    )
    new_pending = SimpleNamespace(
        id="r-new",
        aspect_ratio="4:3",
        status="generating",
        image_id=None,
        task_ids=["g-new"],
    )
    generations = [
        SimpleNamespace(id="g-old", status="succeeded"),
        SimpleNamespace(id="g-new", status="running"),
    ]
    images = [SimpleNamespace(id="img-old", owner_generation_id="g-old")]

    async def fake_load_steps(db: Any, run_id: str) -> list[Any]:
        return [multi_step]

    monkeypatch.setattr(workflows, "_load_steps", fake_load_steps)
    db = _Db(responses=[[], [old_ready, new_pending], generations, images])

    await workflows._sync_poster_workflow_outputs(db, run)  # type: ignore[arg-type]  # noqa: SLF001

    assert new_pending.status == "generating"
    assert multi_step.status == "running"
    assert run.status == "running"


@pytest.mark.asyncio
async def test_sync_render_revision_waits_for_active_revision_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """返修中旧图仍在 render.image_id 时，必须等待新 task 成功后才 ready。"""
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        type="poster_design",
        status="running",
        current_step="multi_size_generation",
    )
    multi_step = SimpleNamespace(
        step_key="multi_size_generation",
        status="running",
        task_ids=["g-old", "g-new"],
        input_json={"expected_render_count": 1, "active_task_ids": ["g-new"]},
        output_json={},
        image_ids=[],
    )
    render = SimpleNamespace(
        id="r-1",
        aspect_ratio="1:1",
        status="revising",
        image_id="img-old",
        task_ids=["g-old", "g-new"],
    )
    generations = [
        SimpleNamespace(id="g-old", status="succeeded"),
        SimpleNamespace(id="g-new", status="running"),
    ]
    images = [SimpleNamespace(id="img-old", owner_generation_id="g-old")]

    async def fake_load_steps(db: Any, run_id: str) -> list[Any]:
        return [multi_step]

    monkeypatch.setattr(workflows, "_load_steps", fake_load_steps)
    db = _Db(responses=[[], [render], generations, images])

    await workflows._sync_poster_workflow_outputs(db, run)  # type: ignore[arg-type]  # noqa: SLF001

    assert render.image_id == "img-old"
    assert render.status == "revising"
    assert multi_step.status == "running"
    assert run.status == "running"


# ---------- endpoint-level monkeypatched integration ------------------------


@pytest.mark.asyncio
async def test_list_workflows_hides_poster_style_generation_jobs_by_default() -> None:
    db = _Db(responses=[[]])
    user = SimpleNamespace(id="user-1")

    out = await workflows.list_workflows(user, db, type=None, limit=50)  # type: ignore[arg-type]

    assert out.items == []
    rendered = str(
        db.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "apparel_model_library_generate" in rendered
    assert "poster_style_library_generate" in rendered


@pytest.mark.asyncio
async def test_list_workflows_counts_poster_multi_size_outputs() -> None:
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    run = SimpleNamespace(
        id="run-poster",
        conversation_id=None,
        type="poster_design",
        status="needs_review",
        title="海报",
        user_prompt="限时五折",
        product_image_ids=[],
        current_step="multi_size_generation",
        quality_mode="premium",
        metadata_jsonb={},
        created_at=now,
        updated_at=now,
    )
    db = _Db(responses=[[run], [("run-poster", ["img-1", "img-2"])]])
    user = SimpleNamespace(id="user-1")

    out = await workflows.list_workflows(user, db, type=None, limit=50)  # type: ignore[arg-type]

    assert len(out.items) == 1
    assert out.items[0].output_count == 2


def test_next_action_for_poster_project_steps() -> None:
    run = SimpleNamespace(
        type="poster_design",
        status="needs_review",
        current_step="multi_size_generation",
    )

    assert workflows._next_action_for(run) == "生成/确认多尺寸"  # noqa: SLF001


@pytest.mark.asyncio
async def test_attach_workflow_assets_updates_run_step_and_image_metadata() -> None:
    now = datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc)
    run = SimpleNamespace(
        id="run-poster",
        type="poster_design",
        title="春季海报",
        metadata_jsonb={},
    )
    step = SimpleNamespace(
        image_ids=["img-old"],
        output_json={"asset_image_ids": ["img-old"]},
    )
    image = SimpleNamespace(id="img-1", metadata_jsonb={})
    db = _Db(responses=[[image], [step]])

    records = await workflows._attach_workflow_assets(  # noqa: SLF001
        db,
        run=run,  # type: ignore[arg-type]
        user_id="user-1",
        image_ids=["img-1", "img-1"],
        asset_type="poster_delivery",
        source_step_key="delivery",
        label="海报交付",
        added_at=now,
    )

    assert len(records) == 1
    assert run.metadata_jsonb["asset_image_ids"] == ["img-1"]
    assert step.image_ids == ["img-old", "img-1"]
    assert step.output_json["asset_image_ids"] == ["img-old", "img-1"]
    assert (
        image.metadata_jsonb["latest_workflow_asset"]["workflow_run_id"] == "run-poster"
    )
    assert (
        image.metadata_jsonb["latest_workflow_asset"]["asset_type"] == "poster_delivery"
    )


@pytest.mark.asyncio
async def test_complete_delivery_supports_poster_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        id="run-poster",
        user_id="user-1",
        type="poster_design",
        status="needs_review",
        current_step="multi_size_generation",
        metadata_jsonb={},
    )
    multi_step = SimpleNamespace(
        status="needs_review",
        image_ids=["img-1", "img-2"],
        input_json={},
        output_json={},
        approved_at=None,
        approved_by=None,
    )
    delivery_step = SimpleNamespace(
        status="waiting_input",
        image_ids=[],
        input_json={},
        output_json={},
        approved_at=None,
        approved_by=None,
    )
    attached: dict[str, Any] = {}

    async def fake_get_run(
        db: Any,
        *,
        user_id: str,
        run_id: str,
        lock: bool = False,
    ) -> Any:
        assert user_id == "user-1"
        assert run_id == "run-poster"
        assert lock is True
        return run

    async def fake_sync_poster(db: Any, run_arg: Any) -> None:
        assert run_arg is run

    async def fake_step(db: Any, run_id: str, step_key: str) -> Any:
        return {
            "multi_size_generation": multi_step,
            "delivery": delivery_step,
        }[step_key]

    async def fake_attach_assets(db_arg: Any, **kwargs: Any) -> list[dict[str, Any]]:
        assert db_arg is not None
        attached.update(kwargs)
        delivery_step.image_ids = kwargs["image_ids"]
        return []

    async def fake_build_run_out(db: Any, run_arg: Any) -> Any:
        return SimpleNamespace(status=run_arg.status, current_step=run_arg.current_step)

    monkeypatch.setattr(workflows, "_get_run", fake_get_run)
    monkeypatch.setattr(workflows, "_sync_poster_workflow_outputs", fake_sync_poster)
    monkeypatch.setattr(workflows, "_step", fake_step)
    monkeypatch.setattr(workflows, "_attach_workflow_assets", fake_attach_assets)
    monkeypatch.setattr(workflows, "_build_run_out", fake_build_run_out)

    db = _Db()
    user = SimpleNamespace(id="user-1")
    out = await workflows.complete_delivery("run-poster", user, db)  # type: ignore[arg-type]

    assert out.status == "completed"
    assert out.current_step == "delivery"
    assert run.status == "completed"
    assert multi_step.status == "completed"
    assert delivery_step.status == "completed"
    assert delivery_step.output_json["download_image_ids"] == ["img-1", "img-2"]
    assert attached["asset_type"] == "poster_delivery"
    assert attached["source_step_key"] == "delivery"
    assert attached["image_ids"] == ["img-1", "img-2"]
    assert db.committed is True


@pytest.mark.asyncio
async def test_poster_load_style_reads_json_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preset styles live in the JSON index, not poster_style_items DB rows."""

    async def fake_find_preset(
        db: Any, *, user_id: str, style_id: str
    ) -> dict[str, Any] | None:
        assert user_id == "user-1"
        assert style_id == "preset:flat_illustration:v1"
        return {
            "id": style_id,
            "title": "扁平插画",
            "mood": "现代轻快",
            "prompt_template": "flat vector poster",
            "palette": ["#FF6B6B"],
            "recommended_aspects": ["1:1", "9:16"],
            "style_tags": ["扁平", "矢量"],
            "category": "illustration",
        }

    monkeypatch.setattr(workflows, "_poster_find_preset_item", fake_find_preset)

    db = _Db()
    style = await workflows._poster_load_style(  # noqa: SLF001
        db, user_id="user-1", style_id="preset:flat_illustration:v1"
    )

    assert style.id == "preset:flat_illustration:v1"
    assert style.prompt_template == "flat vector poster"
    assert style.palette == ["#FF6B6B"]
    assert db.statements == []


@pytest.mark.asyncio
async def test_poster_load_style_filters_user_private_rows_by_owner() -> None:
    style = SimpleNamespace(
        id="user:style-1",
        user_id="user-1",
        title="私有风格",
        mood="",
        prompt_template="private style",
        palette=[],
        recommended_aspects=[],
        style_tags=[],
        category="minimal",
    )
    db = _Db(responses=[[style]])

    loaded = await workflows._poster_load_style(  # noqa: SLF001
        db, user_id="user-1", style_id="user:style-1"
    )

    assert loaded is style
    assert "poster_style_items.user_id" in str(db.statements[0])


@pytest.mark.asyncio
async def test_create_poster_design_workflow_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主流程 monkeypatch smoke：style 找到 → run 创建 → step 入库 → completion 入队。"""

    style = SimpleNamespace(
        id="style_promo_01",
        title="促销扁平插画",
        mood="热闹",
        prompt_template="flat illustration",
        palette=["#ff6b35"],
        recommended_aspects=["1:1"],
        style_tags=["promo"],
        category="promo",
    )

    async def fake_load_style(db: Any, *, user_id: str, style_id: str) -> Any:
        assert style_id == "style_promo_01"
        return style

    async def fake_validate_owned_images(
        db: Any,
        *,
        user_id: str,
        image_ids: list[str],
        min_count: int = 1,
        max_count: int | None = None,
    ) -> list[str]:
        return image_ids

    async def fake_get_or_create_conv(
        db: Any, *, user: Any, conversation_id: Any, title: str, workflow_type: str
    ) -> Any:
        return SimpleNamespace(
            id="conv-1",
            user_id=user.id,
            archived=False,
            title="",
            last_activity_at=None,
        )

    async def fake_step(db: Any, run_id: str, step_key: str) -> Any:
        return SimpleNamespace(
            step_key=step_key, task_ids=[], input_json={}, output_json={}
        )

    async def fake_create_workflow_task(**kwargs: Any) -> Any:
        bundle = SimpleNamespace(
            assistant_msg_id="msg-a",
            message_ids=["msg-u", "msg-a"],
            outbox_payloads=[],
            outbox_rows=[],
        )
        return bundle, "comp-1", []

    async def fake_publish_bundles(
        db: Any, *, user_id: str, conv_id: str, bundles: list[Any]
    ) -> None:
        return None

    monkeypatch.setattr(workflows, "_poster_load_style", fake_load_style)
    monkeypatch.setattr(workflows, "_validate_owned_images", fake_validate_owned_images)
    monkeypatch.setattr(
        workflows, "_get_or_create_workflow_conversation", fake_get_or_create_conv
    )
    monkeypatch.setattr(workflows, "_step", fake_step)
    monkeypatch.setattr(
        workflows, "_create_poster_workflow_task", fake_create_workflow_task
    )
    monkeypatch.setattr(workflows, "_publish_bundles", fake_publish_bundles)

    body = PosterDesignWorkflowCreateIn(
        copy_text="限时五折，全场夏季新品",
        style_id="style_promo_01",
        target_aspects=["1:1", "9:16"],
        quality_mode="premium",
    )
    db = _Db()
    user = SimpleNamespace(id="user-1")
    out = await workflows.create_poster_design_workflow(body, user, db)  # type: ignore[arg-type]

    assert out.status == "running"
    assert out.current_step == "copy_analysis"
    # workflow_run 已 add 到 db；至少 1 个 run + 7 个 step
    added_types = [type(row).__name__ for row in db.added]
    assert "WorkflowRun" in added_types
    assert added_types.count("WorkflowStep") == len(workflows.POSTER_WORKFLOW_STEPS)
    assert db.committed is True


@pytest.mark.asyncio
async def test_create_poster_design_workflow_rejects_unknown_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    async def fake_load_style(db: Any, *, user_id: str, style_id: str) -> Any:
        raise workflows._http("style_not_found", "poster style not found", 404)  # noqa: SLF001

    monkeypatch.setattr(workflows, "_poster_load_style", fake_load_style)

    body = PosterDesignWorkflowCreateIn(copy_text="测试文案", style_id="missing")
    db = _Db()
    user = SimpleNamespace(id="user-1")
    with pytest.raises(HTTPException) as exc:
        await workflows.create_poster_design_workflow(body, user, db)  # type: ignore[arg-type]
    assert exc.value.status_code == 404
