from __future__ import annotations

import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "lumen_run_test_plan",
    ROOT / "scripts" / "run_test_plan.py",
)
assert SPEC is not None and SPEC.loader is not None
run_test_plan = module_from_spec(SPEC)
sys.modules[SPEC.name] = run_test_plan
SPEC.loader.exec_module(run_test_plan)


def test_extract_failures_supports_pytest_and_node_test_output() -> None:
    output = "\n".join(
        [
            "FAILED tests/test_example.py::test_broken - AssertionError",
            "not ok 3 - reconnects after a cursor gap",
            "FAILED tests/test_example.py::test_broken - repeated",
        ]
    )

    assert run_test_plan.extract_failures(output) == [
        "tests/test_example.py::test_broken",
        "reconnects after a cursor gap",
    ]


def test_exclusive_resource_tags_are_serialized_without_blocking_other_work() -> None:
    active_resources: set[str] = set()
    overlap_detected = False
    peak_running = 0
    running = 0

    async def fake_runner(command: dict[str, object]) -> run_test_plan.CommandResult:
        nonlocal overlap_detected, peak_running, running
        resources = set(command.get("resource_tags", []))
        if active_resources & resources:
            overlap_detected = True
        active_resources.update(resources)
        running += 1
        peak_running = max(peak_running, running)
        await asyncio.sleep(0.03)
        running -= 1
        active_resources.difference_update(resources)
        return run_test_plan.CommandResult(
            id=str(command["id"]),
            command=str(command["command"]),
            duration_seconds=0.03,
            exit_code=0,
            failures=[],
            output="",
        )

    commands = [
        {
            "id": "postgres-a",
            "command": "a",
            "resource_tags": ["postgres"],
        },
        {
            "id": "postgres-b",
            "command": "b",
            "resource_tags": ["postgres"],
        },
        {"id": "free", "command": "c", "resource_tags": []},
    ]

    results = asyncio.run(
        run_test_plan.execute_commands(
            commands,
            max_jobs=4,
            exclusive_resources={"postgres"},
            runner=fake_runner,
        )
    )

    assert [result.id for result in results] == [
        "postgres-a",
        "postgres-b",
        "free",
    ]
    assert overlap_detected is False
    assert peak_running >= 2


def test_rerun_failed_selects_only_previous_failures() -> None:
    commands = [
        {"id": "passed", "command": "true", "resource_tags": []},
        {"id": "failed", "command": "false", "resource_tags": []},
        {"id": "not-run", "command": "echo new", "resource_tags": []},
    ]
    previous_results = {
        "results": [
            {"id": "passed", "exit_code": 0},
            {"id": "failed", "exit_code": 1},
        ]
    }

    selected = run_test_plan.select_commands(
        commands,
        rerun_failed=True,
        previous_results=previous_results,
    )

    assert [command["id"] for command in selected] == ["failed"]


def test_plan_identity_binds_base_head_and_command_set() -> None:
    commands = [
        {"id": "one", "command": "true", "resource_tags": ["postgres"]},
    ]
    identity = run_test_plan.build_plan_identity(
        {
            "schema_version": 1,
            "base": "base-sha",
            "head": "head-sha",
        },
        commands,
    )

    assert identity["base"] == "base-sha"
    assert identity["head"] == "head-sha"
    assert identity["commands"] == [
        {
            "command": "true",
            "id": "one",
            "resource_tags": ["postgres"],
        }
    ]
    assert len(identity["digest"]) == 64


def test_rerun_failed_rejects_stale_plan_identity() -> None:
    commands = [{"id": "one", "command": "true", "resource_tags": []}]
    current_identity = run_test_plan.build_plan_identity(
        {"schema_version": 1, "base": "base", "head": "new-head"},
        commands,
    )
    previous_identity = run_test_plan.build_plan_identity(
        {"schema_version": 1, "base": "base", "head": "old-head"},
        commands,
    )

    with pytest.raises(ValueError, match="do not match the current plan"):
        run_test_plan._validate_previous_results(
            {
                "plan_identity": previous_identity,
                "results": [{"id": "one", "exit_code": 0}],
            },
            plan_identity=current_identity,
            commands=commands,
        )


def test_rerun_failed_rejects_commands_that_were_never_executed() -> None:
    commands = [
        {"id": "old", "command": "true", "resource_tags": []},
        {"id": "new", "command": "echo new", "resource_tags": []},
    ]
    identity = run_test_plan.build_plan_identity(
        {"schema_version": 1, "base": "base", "head": "head"},
        commands,
    )

    with pytest.raises(ValueError, match="never executed: new"):
        run_test_plan._validate_previous_results(
            {
                "plan_identity": identity,
                "results": [{"id": "old", "exit_code": 0}],
            },
            plan_identity=identity,
            commands=commands,
        )
