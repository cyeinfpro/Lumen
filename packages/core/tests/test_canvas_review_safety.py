from types import SimpleNamespace
import pytest
from lumen_core.canvas_task_state import canvas_task_recovery
from lumen_core.task_outcome_knowledge import task_outcome_unknown
from lumen_core.canvas_plan_repair import repair_plan_closure
from lumen_core.canvas_run_plan import plan_ready_nodes
from lumen_core.upstream_billing import (
    mark_upstream_dispatch_started,
    mark_upstream_dispatch_proven_undelivered,
)
from lumen_core.capacity_leases import CapacityLeaseGuard, CapacityLeaseLost


@pytest.mark.parametrize(
    "kind,status,stage,code",
    [
        ("video_generation", "expired", "finished", "submit_unknown"),
        ("generation", "failed", "finalizing", "direct_image_result_unknown"),
        ("generation", "failed", "finalizing", "image_job_result_unknown"),
        ("generation", "failed", "finished", "no_image_returned"),
    ],
)
def test_terminal_presentation_does_not_convert_unknown_to_new_generation(
    kind, status, stage, code
):
    owner = SimpleNamespace(status=status, progress_stage=stage, error_code=code)
    assert task_outcome_unknown(owner, task_kind=kind)
    result = canvas_task_recovery(owner, task_kind=kind)
    assert result["state"] == "submission_unknown"
    assert result["can_query"] and not result["can_generate_new"]


def test_cancelled_image_dispatch_receipt_is_epoch_fenced():
    owner = SimpleNamespace(status="canceled", error_code="canceled", execution_epoch=2)
    owner.upstream_request = mark_upstream_dispatch_started(
        {}, at="now", attempt=1, execution_epoch=2
    )
    assert task_outcome_unknown(owner, task_kind="generation")
    owner.execution_epoch = 3
    assert not task_outcome_unknown(owner, task_kind="generation")
    owner.upstream_request = mark_upstream_dispatch_proven_undelivered(
        {}, at="now", attempt=1, execution_epoch=3
    )
    assert not task_outcome_unknown(owner, task_kind="generation")


def test_cancelled_video_preserves_unknown_delivery_but_accepts_proven_absent():
    owner = SimpleNamespace(
        status="canceled",
        progress_stage="finished",
        error_code="canceled",
        diagnostics={"submit_delivery_state": "unknown"},
        attempt=1,
    )
    assert (
        canvas_task_recovery(owner, task_kind="video_generation")["state"]
        == "submission_unknown"
    )
    owner.diagnostics = {"submit_delivery_state": "proven_absent"}
    assert not task_outcome_unknown(owner, task_kind="video_generation")


def repair_plan(policy="continue_independent"):
    return SimpleNamespace(
        failure_policy=policy,
        steps=[
            SimpleNamespace(node_id=name, dependencies=dependencies)
            for name, dependencies in (
                ("a", ()),
                ("d", ()),
                ("b", ("a",)),
                ("e", ("d",)),
                ("shared", ("a", "d")),
                ("tail", ("b",)),
            )
        ],
    )


def test_repair_scope_ignores_unselected_branch_and_blocked_shared_dependency():
    statuses = {
        "a": "failed",
        "d": "failed",
        "b": "blocked",
        "e": "blocked",
        "shared": "blocked",
        "tail": "blocked",
    }
    assert repair_plan_closure(repair_plan(), statuses, {"a"}) == (
        ("a", "b", "tail"),
        (),
    )
    assert repair_plan_closure(repair_plan("fail_fast"), statuses, {"a"}) == (
        (),
        ("d",),
    )
    assert repair_plan_closure(repair_plan("fail_fast"), statuses, {"a", "d"})[0] == (
        "a",
        "d",
        "b",
        "e",
        "shared",
        "tail",
    )
    statuses["d"] = "partial_failed"
    assert repair_plan_closure(repair_plan("fail_fast"), statuses, {"a"}) == (
        (),
        ("d",),
    )


def test_fail_fast_repaired_root_runs_with_unselected_blocked_branch():
    plan = SimpleNamespace(
        failure_policy="fail_fast",
        steps=[
            SimpleNamespace(node_id=name, dependencies=dependencies, reuse=None)
            for name, dependencies in (
                ("a", ()),
                ("b", ()),
                ("a_tail", ("a",)),
                ("b_tail", ("b",)),
            )
        ],
    )
    statuses = {"a": "failed", "b": "blocked", "a_tail": "blocked", "b_tail": "blocked"}
    restart, missing = repair_plan_closure(plan, statuses, {"a"})
    assert restart == ("a", "a_tail") and not missing
    statuses.update(dict.fromkeys(restart, "pending"))
    decision = plan_ready_nodes(plan, statuses)
    assert decision.ready == ("a",)
    assert decision.waiting == ("a_tail",)
    assert not decision.blocked and not decision.finished
    assert statuses["b"] == statuses["b_tail"] == "blocked"
    statuses["a"] = "succeeded"
    assert plan_ready_nodes(plan, statuses).ready == ("a_tail",)
    # A new real failure still enforces the original fail-fast policy.
    statuses["a"] = "failed"
    assert plan_ready_nodes(plan, statuses).blocked == ("a_tail",)


@pytest.mark.asyncio
async def test_slow_renewal_response_never_extends_server_ttl_estimate():
    clock = [0.0]

    class Lease:
        async def renew(self):
            clock[0] += 5
            return True

    guard = CapacityLeaseGuard.create(
        Lease(), ttl_seconds=10, monotonic=lambda: clock[0]
    )
    await guard.assert_owned()
    clock[0] = 8
    with pytest.raises(CapacityLeaseLost):
        await guard.assert_owned()
