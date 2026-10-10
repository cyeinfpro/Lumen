from dataclasses import FrozenInstanceError

import pytest

from lumen_core.canvas_run_plan import (
    OutputReference,
    PlanPolicy,
    compile_run_plan,
    plan_ready_nodes,
    retryable_plan_nodes,
)


def graph():
    def node(identifier, kind):
        return {
            "id": identifier,
            "type": kind,
            "position": {"x": 0, "y": 0},
            "config": {"text": "draw"} if kind == "prompt" else {},
        }

    def edge(identifier, source, target, handle, kind):
        return {
            "id": identifier,
            "source_node_id": source,
            "target_node_id": target,
            "source_handle": "text" if kind == "text" else "image",
            "target_handle": handle,
            "data_type": kind,
        }

    return {
        "nodes": [
            node("prompt", "prompt"),
            node("a", "image_generate"),
            node("b", "image_generate"),
            node("c", "image_edit"),
        ],
        "edges": [
            edge("pa", "prompt", "a", "prompt", "text"),
            edge("pb", "prompt", "b", "prompt", "text"),
            edge("pc", "prompt", "c", "prompt", "text"),
            edge("ac", "a", "c", "source", "image"),
        ],
    }


def reference(node_id="a"):
    return OutputReference(node_id, "execution-1", 0, "image", "image-1", "a" * 64)


def compile_(**kwargs):
    defaults = dict(
        tenant_id="user-1",
        canvas_id="canvas-1",
        document_revision=1,
        kind="upstream",
        target_node_ids=("c",),
    )
    defaults["policy"] = PlanPolicy(
        kwargs.pop("budget_micro", None),
        kwargs.pop("failure_policy", "continue_independent"),
    )
    defaults.update(kwargs)
    return compile_run_plan(graph(), **defaults)


def test_upstream_plan_excludes_unrelated_branch_and_orders_dependencies():
    plan = compile_()
    assert [step.node_id for step in plan.steps] == ["a", "c"]
    assert plan.steps[1].dependencies == ("a",)
    assert plan.estimated_cost_micro is None


def test_equal_prompt_does_not_reuse_a_generation():
    plan = compile_(kind="all")
    assert [step.node_id for step in plan.steps] == ["a", "b", "c"]
    assert all(step.reuse is None for step in plan.steps)
    assert plan_ready_nodes(plan, {}).ready == ("a", "b")


def test_explicit_reuse_is_bound_to_exact_asset_and_ready_downstream():
    plan = compile_(reuse_outputs={"a": reference()})
    assert plan.steps[0].reuse == reference()
    assert plan_ready_nodes(plan, {}).ready == ("c",)


def test_single_and_selection_use_existing_boundary_outputs():
    with pytest.raises(ValueError, match="missing boundary"):
        compile_(kind="single")
    plan = compile_(kind="single", selected_outputs={"a": reference()})
    assert [step.node_id for step in plan.steps] == ["c"]
    assert plan.bindings == (("ac", reference()),)
    assert plan_ready_nodes(plan, {}).ready == ("c",)


def test_pin_excludes_source_execution_and_is_validated():
    value = graph()
    value["edges"][-1].update(
        binding_mode="pinned", pinned_execution_id="execution-1", pinned_output_index=0
    )
    with pytest.raises(ValueError, match="pinned"):
        compile_run_plan(
            value,
            tenant_id="u",
            canvas_id="c",
            document_revision=1,
            kind="upstream",
            target_node_ids=("c",),
        )
    plan = compile_run_plan(
        value,
        tenant_id="u",
        canvas_id="c",
        document_revision=1,
        kind="upstream",
        target_node_ids=("c",),
        pinned_outputs={"ac": reference()},
    )
    assert [step.node_id for step in plan.steps] == ["c"]


def test_failure_blocks_only_descendants_and_independent_branch_continues():
    plan = compile_(kind="all")
    decision = plan_ready_nodes(plan, {"a": "failed"})
    assert decision.ready == ("b",)
    assert decision.blocked == ("c",)
    assert decision.finished is False
    assert plan_ready_nodes(plan, {"a": "failed", "b": "succeeded"}).finished


def test_unknown_submit_is_not_retryable_after_restart():
    plan = compile_(kind="all")
    decision = plan_ready_nodes(plan, {"a": "submit_unknown", "b": "failed"})
    assert decision.ready == ()
    assert decision.waiting == ("a", "c")
    assert retryable_plan_nodes(plan, {"a": "submit_unknown", "b": "failed"}) == ("b",)


def test_repair_does_not_redo_succeeded_nodes():
    plan = compile_(kind="all")
    assert retryable_plan_nodes(
        plan, {"a": "succeeded", "b": "failed", "c": "expired"}
    ) == ("b", "c")


def test_fail_fast_does_not_cancel_already_submitted_provider_work():
    plan = compile_(kind="all", failure_policy="fail_fast")
    decision = plan_ready_nodes(plan, {"a": "failed", "b": "submitted"})
    assert decision.waiting == ("b",)
    assert decision.blocked == ("c",)


def test_plan_is_immutable_and_tenant_scoped():
    plan = compile_()
    assert plan.plan_hash == compile_().plan_hash
    assert plan.plan_hash != compile_(tenant_id="another-user").plan_hash
    with pytest.raises(FrozenInstanceError):
        plan.kind = "all"
    assert len(plan.plan_hash) == 64


def test_costs_do_not_turn_unknown_or_reused_outputs_into_new_charges():
    assert compile_(estimates={"a": 5, "c": 8}).estimated_cost_micro == 13
    assert compile_(estimates={"a": 5}).estimated_cost_micro is None
    assert (
        compile_(
            estimates={"a": 5, "c": 8}, reuse_outputs={"a": reference()}
        ).estimated_cost_micro
        == 8
    )
    with pytest.raises(ValueError, match="costs"):
        compile_(estimates={"a": -1})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_node_ids": ("missing",)},
        {"target_node_ids": ("prompt",)},
        {"kind": "single", "target_node_ids": ("a", "b")},
        {"budget_micro": -1},
        {"reuse_outputs": {"b": reference("b")}},
    ],
)
def test_invalid_scope_is_rejected(kwargs):
    with pytest.raises(ValueError):
        compile_(**kwargs)


def test_plan_roundtrip_preserves_hash_and_ownership():
    import json
    from lumen_core.canvas_run_plan import restore_run_plan

    plan = compile_(reuse_outputs={"a": reference()})
    payload = json.loads(json.dumps(plan.to_dict()))
    restored = restore_run_plan(payload, tenant_id="user-1", canvas_id="canvas-1")
    assert restored == plan
    assert plan_ready_nodes(restored, {}).ready == ("c",)
    with pytest.raises(ValueError, match="different owner"):
        restore_run_plan(payload, tenant_id="other", canvas_id="canvas-1")
    payload["steps"][0]["reuse"]["asset_id"] = "tampered"
    with pytest.raises(ValueError, match="integrity"):
        restore_run_plan(payload, tenant_id="user-1", canvas_id="canvas-1")


def test_hidden_frame_nodes_are_not_implicitly_billed_by_run_all():
    value = graph()
    value["nodes"].append(
        {
            "id": "hidden",
            "type": "frame",
            "position": {"x": 0, "y": 0},
            "config": {"hidden_in_run": True},
        }
    )
    value["nodes"][1]["parent_group_id"] = "hidden"
    plan = compile_run_plan(
        value,
        tenant_id="u",
        canvas_id="c",
        document_revision=1,
        kind="all",
        target_node_ids=(),
        selected_outputs={"a": reference()},
    )
    assert [step.node_id for step in plan.steps] == ["b", "c"]
    assert plan.bindings == (("ac", reference()),)


def test_partial_candidate_failure_never_silently_shifts_downstream_choice():
    decision = plan_ready_nodes(compile_(kind="all"), {"a": "partial_failed"})
    assert decision.ready == ("b",)
    assert decision.blocked == ("c",)
