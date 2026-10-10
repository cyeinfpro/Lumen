"""Select only explicitly repaired failures and their recoverable descendants."""


def repair_plan_closure(plan, statuses, selected_node_ids):
    selected = set(selected_node_ids)
    if plan.failure_policy == "fail_fast":
        remaining = sorted(
            node_id
            for node_id, status in statuses.items()
            if status in {"failed", "partial_failed", "canceled", "expired"}
            and node_id not in selected
        )
        if remaining:
            return (), tuple(remaining)
    restart = set(selected)
    for step in plan.steps:
        if (
            statuses.get(step.node_id) != "blocked"
            or not set(step.dependencies) & restart
        ):
            continue
        if all(
            dependency in restart or statuses.get(dependency) in {"succeeded", "reused"}
            for dependency in step.dependencies
        ):
            restart.add(step.node_id)
    return tuple(step.node_id for step in plan.steps if step.node_id in restart), ()
