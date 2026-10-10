"""Immutable, tenant-scoped Canvas execution plans and pure scheduling decisions.

Compilation has no side effects. Reuse is explicit: matching prompts or hashes
alone never skip a generation. Workers must persist claims before side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from .canvas import canonical_hash, topological_node_ids
from .canvas_schemas import CanvasGraph, EXECUTABLE_NODE_TYPES


@dataclass(frozen=True, slots=True)
class OutputReference:
    node_id: str
    execution_id: str
    output_index: int
    asset_kind: Literal["image", "video"]
    asset_id: str
    source_sha256: str

    def __post_init__(self) -> None:
        if not self.node_id or not self.execution_id or not self.asset_id:
            raise ValueError("output reference requires stable IDs")
        if type(self.output_index) is not int or self.output_index < 0:
            raise ValueError("output index must be nonnegative")
        if self.asset_kind not in {"image", "video"}:
            raise ValueError("invalid output asset kind")
        if len(self.source_sha256) != 64 or any(
            value not in "0123456789abcdef" for value in self.source_sha256
        ):
            raise ValueError("output reference requires a source SHA-256")


@dataclass(frozen=True, slots=True)
class PlanPolicy:
    budget_micro: int | None = None
    failure_policy: Literal["continue_independent", "fail_fast"] = (
        "continue_independent"
    )


@dataclass(frozen=True, slots=True)
class PlanStepOptions:
    effective_model: str | None = None
    capability_version: str | None = None
    output_index: int = 0


@dataclass(frozen=True, slots=True)
class PlanStep:
    node_id: str
    dependencies: tuple[str, ...]
    reuse: OutputReference | None = None
    estimated_cost_micro: int | None = None
    effective_model: str | None = None
    capability_version: str | None = None
    output_index: int = 0


@dataclass(frozen=True, slots=True)
class RunPlan:
    schema_version: int
    tenant_id: str
    canvas_id: str
    document_revision: int
    kind: str
    target_node_ids: tuple[str, ...]
    graph_hash: str
    steps: tuple[PlanStep, ...]
    bindings: tuple[tuple[str, OutputReference], ...]
    failure_policy: str
    budget_micro: int | None
    plan_hash: str

    @property
    def estimated_cost_micro(self) -> int | None:
        costs = [step.estimated_cost_micro for step in self.steps if step.reuse is None]
        return None if any(cost is None for cost in costs) else sum(costs)

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def _walk_upstream(
    seeds: set[str],
    incoming: Mapping[str, list],
    reused: set[str],
) -> set[str]:
    included = set(seeds)
    pending = list(seeds)
    while pending:
        node_id = pending.pop()
        if node_id in reused:
            continue
        for edge in incoming.get(node_id, []):
            if edge.binding_mode == "pinned":
                continue
            if edge.source_node_id not in included:
                included.add(edge.source_node_id)
                pending.append(edge.source_node_id)
    return included


def _resolve_plan_bindings(
    *,
    ordered: tuple[str, ...],
    scheduled: set[str],
    executable: set[str],
    incoming: Mapping[str, list],
    selected: Mapping[str, OutputReference],
    pinned: Mapping[str, OutputReference],
    reused: Mapping[str, OutputReference],
) -> tuple[dict[str, set[str]], dict[str, OutputReference]]:
    dependencies: dict[str, set[str]] = {node_id: set() for node_id in ordered}
    bindings: dict[str, OutputReference] = {}
    for node_id in ordered:
        if node_id in reused:
            continue
        pending = list(incoming.get(node_id, []))
        visited: set[str] = set()
        while pending:
            edge = pending.pop()
            if edge.id in visited:
                continue
            visited.add(edge.id)
            source_id = edge.source_node_id
            if edge.binding_mode == "pinned":
                reference = pinned.get(edge.id)
                if (
                    reference is None
                    or reference.node_id != source_id
                    or reference.execution_id != edge.pinned_execution_id
                    or reference.output_index != edge.pinned_output_index
                ):
                    raise ValueError(f"missing or mismatched pinned output: {edge.id}")
                bindings[edge.id] = reference
            elif source_id in scheduled:
                dependencies[node_id].add(source_id)
            elif source_id in executable:
                reference = selected.get(source_id)
                if reference is None or reference.node_id != source_id:
                    raise ValueError(f"missing boundary output: {source_id}")
                bindings[edge.id] = reference
            else:
                pending.extend(incoming.get(source_id, []))
    return dependencies, bindings


def runnable_executable_node_ids(graph: CanvasGraph) -> set[str]:
    parents = {node.id: node.parent_group_id for node in graph.nodes}
    parents.update({frame.id: frame.parent_frame_id for frame in graph.frames})
    hidden = {frame.id for frame in graph.frames if frame.hidden_in_run}
    hidden.update(
        node.id
        for node in graph.nodes
        if node.type == "frame" and node.config.hidden_in_run
    )
    runnable = set()
    for node in graph.nodes:
        if node.type not in EXECUTABLE_NODE_TYPES:
            continue
        parent = parents.get(node.id)
        excluded = False
        while parent is not None:
            if parent in hidden:
                excluded = True
                break
            parent = parents.get(parent)
        if not excluded:
            runnable.add(node.id)
    return runnable


def compile_run_plan(
    graph: CanvasGraph | Mapping,
    *,
    tenant_id: str,
    canvas_id: str,
    document_revision: int,
    kind: Literal["single", "upstream", "selection", "all"],
    target_node_ids: tuple[str, ...],
    # The caller must resolve and authorize these references from database truth.
    selected_outputs: Mapping[str, OutputReference] | None = None,
    pinned_outputs: Mapping[str, OutputReference] | None = None,
    reuse_outputs: Mapping[str, OutputReference] | None = None,
    estimates: Mapping[str, int | None] | None = None,
    step_options: Mapping[str, PlanStepOptions] | None = None,
    policy: PlanPolicy | None = None,
) -> RunPlan:
    policy = policy or PlanPolicy()
    budget_micro, failure_policy = policy.budget_micro, policy.failure_policy
    if not tenant_id or not canvas_id or document_revision < 1:
        raise ValueError("plan requires tenant, canvas and document revision")
    if kind not in {"single", "upstream", "selection", "all"}:
        raise ValueError("unknown run kind")
    if failure_policy not in {"continue_independent", "fail_fast"}:
        raise ValueError("unknown failure policy")
    if budget_micro is not None and (type(budget_micro) is not int or budget_micro < 0):
        raise ValueError("budget must be a nonnegative integer or unknown")
    parsed = (
        graph if isinstance(graph, CanvasGraph) else CanvasGraph.model_validate(graph)
    )
    executable = {
        node.id for node in parsed.nodes if node.type in EXECUTABLE_NODE_TYPES
    }
    runnable = runnable_executable_node_ids(parsed)
    targets = set(target_node_ids) if kind != "all" else runnable
    if not targets or not targets <= runnable:
        raise ValueError("targets must be existing executable nodes")
    if kind == "single" and len(targets) != 1:
        raise ValueError("single runs require exactly one target")
    selected = dict(selected_outputs or {})
    pinned = dict(pinned_outputs or {})
    reused = dict(reuse_outputs or {})
    if any(
        node_id != ref.node_id or node_id not in executable
        for node_id, ref in reused.items()
    ):
        raise ValueError("reuse reference does not match an executable node")
    incoming: dict[str, list] = {}
    for edge in parsed.edges:
        incoming.setdefault(edge.target_node_id, []).append(edge)
    included = _walk_upstream(targets, incoming, set(reused))
    scheduled = included & runnable if kind in {"upstream", "all"} else targets
    if not set(reused) <= scheduled:
        raise ValueError("reuse refers to a node outside this plan")
    ordered = tuple(
        node_id for node_id in topological_node_ids(parsed) if node_id in scheduled
    )
    dependencies, bindings = _resolve_plan_bindings(
        ordered=ordered,
        scheduled=scheduled,
        executable=executable,
        incoming=incoming,
        selected=selected,
        pinned=pinned,
        reused=reused,
    )
    prices = estimates or {}
    for value in prices.values():
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("estimated costs must be nonnegative integers or unknown")
    options = step_options or {}
    if any(
        type(item.output_index) is not int or not 0 <= item.output_index <= 9
        for item in options.values()
    ):
        raise ValueError("invalid plan output index")
    order_index = {node_id: index for index, node_id in enumerate(ordered)}
    steps = tuple(
        PlanStep(
            node_id,
            tuple(sorted(dependencies[node_id], key=order_index.__getitem__)),
            reused.get(node_id),
            0 if node_id in reused else prices.get(node_id),
            options.get(node_id, PlanStepOptions()).effective_model,
            options.get(node_id, PlanStepOptions()).capability_version,
            options.get(node_id, PlanStepOptions()).output_index,
        )
        for node_id in ordered
    )
    payload = {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "canvas_id": canvas_id,
        "document_revision": document_revision,
        "kind": kind,
        "target_node_ids": tuple(node_id for node_id in ordered if node_id in targets),
        "graph_hash": canonical_hash(parsed.model_dump(mode="json")),
        "steps": steps,
        "bindings": tuple(sorted(bindings.items())),
        "failure_policy": failure_policy,
        "budget_micro": budget_micro,
    }
    from dataclasses import asdict

    # canonical_hash accepts plain JSON values, not dataclass objects.
    hash_payload = {
        **payload,
        "steps": [asdict(step) for step in steps],
        "bindings": [(edge_id, asdict(ref)) for edge_id, ref in payload["bindings"]],
    }
    return RunPlan(**payload, plan_hash=canonical_hash(hash_payload))


@dataclass(frozen=True, slots=True)
class PlanDecision:
    ready: tuple[str, ...]
    blocked: tuple[str, ...]
    waiting: tuple[str, ...]
    finished: bool


def plan_ready_nodes(plan: RunPlan, states: Mapping[str, str]) -> PlanDecision:
    """Re-derive readiness after restart; never reissue a started/unknown task."""
    succeeded = {"succeeded", "reused"}
    failed = {"failed", "partial_failed", "canceled", "expired", "blocked", "skipped"}
    active = {
        "queued",
        "running",
        "submitting",
        "submitted",
        "submit_unknown",
        "reconciling",
        "canceling",
    }
    known = succeeded | failed | active | {"pending", "ready"}
    if any(state not in known for state in states.values()):
        raise ValueError("unknown execution state")
    if not set(states) <= {step.node_id for step in plan.steps}:
        raise ValueError("state belongs to a different plan")
    effective = dict(states)
    effective.update({step.node_id: "reused" for step in plan.steps if step.reuse})
    ready, blocked, waiting = [], [], []
    # Blocked steps are consequences, not new failures. They can remain outside
    # an explicit repair closure and must not immediately re-block repaired work.
    fail_fast = plan.failure_policy == "fail_fast" and any(
        state in {"failed", "partial_failed", "canceled", "expired"}
        for state in effective.values()
    )
    for step in plan.steps:
        state = effective.get(step.node_id, "pending")
        if state in succeeded | failed:
            continue
        if state in active:
            waiting.append(step.node_id)
        elif fail_fast or any(
            effective.get(dep) in failed for dep in step.dependencies
        ):
            blocked.append(step.node_id)
            effective[step.node_id] = "blocked"
        elif all(effective.get(dep) in succeeded for dep in step.dependencies):
            ready.append(step.node_id)
        else:
            waiting.append(step.node_id)
    return PlanDecision(
        tuple(ready), tuple(blocked), tuple(waiting), not ready and not waiting
    )


def retryable_plan_nodes(plan: RunPlan, states: Mapping[str, str]) -> tuple[str, ...]:
    """Retry only confirmed failures. Unknown submits and saved successes stay put."""
    return tuple(
        step.node_id
        for step in plan.steps
        if step.reuse is None and states.get(step.node_id) in {"failed", "expired"}
    )


def restore_run_plan(
    payload: Mapping,
    *,
    tenant_id: str,
    canvas_id: str,
) -> RunPlan:
    """Restore the same persisted plan with ownership and integrity fences."""
    raw = dict(payload)
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported run plan version")
    if raw.get("tenant_id") != tenant_id or raw.get("canvas_id") != canvas_id:
        raise ValueError("run plan belongs to a different owner or canvas")
    expected_hash = raw.pop("plan_hash", None)
    if canonical_hash(raw) != expected_hash:
        raise ValueError("run plan integrity mismatch")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, (list, tuple)) or not 0 < len(raw_steps) <= 1000:
        raise ValueError("invalid run plan steps")
    steps = tuple(
        PlanStep(
            node_id=item["node_id"],
            dependencies=tuple(item["dependencies"]),
            reuse=OutputReference(**item["reuse"]) if item.get("reuse") else None,
            estimated_cost_micro=item.get("estimated_cost_micro"),
            effective_model=item.get("effective_model"),
            capability_version=item.get("capability_version"),
            output_index=item.get("output_index", 0),
        )
        for item in raw_steps
    )
    visited: set[str] = set()
    for step in steps:
        if step.node_id in visited or not set(step.dependencies) <= visited:
            raise ValueError("run plan dependency ordering is invalid")
        visited.add(step.node_id)
    return RunPlan(
        **{
            **raw,
            "steps": steps,
            "target_node_ids": tuple(raw["target_node_ids"]),
            "bindings": tuple(
                (edge_id, OutputReference(**ref)) for edge_id, ref in raw["bindings"]
            ),
            "plan_hash": expected_hash,
        }
    )
