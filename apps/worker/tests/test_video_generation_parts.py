from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from pathlib import Path
from typing import Any

from app.tasks.video_generation_parts import default_runtime as video_generation
from app.tasks.video_generation_parts import (
    entrypoints,
    lifecycle,
    persistence,
    polling,
    providers,
    reconciliation,
    submission,
)
from app.tasks.video_generation_parts.runtime import VideoGenerationPorts


_TASKS_DIR = Path(__file__).parents[1] / "app" / "tasks"
_PARTS_DIR = _TASKS_DIR / "video_generation_parts"
_PORT_GROUPS = {
    "policy",
    "store",
    "lease_queue",
    "provider",
    "billing_events",
    "operations",
}


def _video_port_reference(node: ast.AST) -> tuple[str, str] | None:
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Attribute):
        return None
    group_node = node.value
    root = group_node.value
    if (
        group_node.attr not in _PORT_GROUPS
        or not isinstance(root, ast.Call)
        or root.args
        or root.keywords
        or not isinstance(root.func, ast.Name)
        or root.func.id != "video_ports"
    ):
        return None
    return group_node.attr, node.attr


def _bind_ast_call(
    callable_port: object,
    args: list[ast.AST],
    keywords: list[ast.keyword],
) -> None:
    if any(isinstance(arg, ast.Starred) for arg in args):
        raise AssertionError("starred arguments cannot verify a video port contract")
    if any(keyword.arg is None for keyword in keywords):
        raise AssertionError("expanded keyword arguments cannot verify a video port contract")
    inspect.signature(callable_port).bind(
        *([object()] * len(args)),
        **{str(keyword.arg): object() for keyword in keywords},
    )


def test_video_generation_production_modules_stay_below_line_budget() -> None:
    paths = list(_PARTS_DIR.glob("*.py"))
    oversized = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in paths
        if len(path.read_text(encoding="utf-8").splitlines()) > 1500
    }

    assert oversized == {}
    assert not (_TASKS_DIR / "video_generation.py").exists()


def test_video_generation_parts_do_not_import_compatibility_facade() -> None:
    forbidden: list[str] = []
    for path in _PARTS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
                if "app.tasks.video_generation" in names:
                    forbidden.append(path.name)
            if isinstance(node, ast.ImportFrom) and node.module in {
                "app.tasks",
                "app.tasks.video_generation",
            }:
                forbidden.append(path.name)
            if (
                isinstance(node, ast.ImportFrom)
                and node.level > 0
                and any(alias.name == "video_generation" for alias in node.names)
            ):
                forbidden.append(path.name)

    assert forbidden == []


def test_leaf_modules_expose_decomposed_task_entrypoints() -> None:
    assert video_generation.run_video_generation is submission.run_video_generation
    assert video_generation.run_video_poll is polling.run_video_poll
    assert (
        video_generation.reconcile_video_tasks is reconciliation.reconcile_video_tasks
    )
    assert video_generation._acquire_lease is lifecycle.acquire_lease
    assert (
        video_generation._provider_for_generation is providers.provider_for_generation
    )
    assert video_generation._finish_success is persistence.finish_success
    assert entrypoints.run_video_generation.__module__.endswith(".entrypoints")
    assert entrypoints.run_video_poll.__module__.endswith(".entrypoints")
    assert entrypoints.reconcile_video_tasks.__module__.endswith(".entrypoints")


def test_video_runtime_ports_are_grouped_by_domain() -> None:
    ports = video_generation.DEFAULT_VIDEO_GENERATION_RUNTIME.ports

    assert isinstance(ports, VideoGenerationPorts)
    assert tuple(field.name for field in fields(ports)) == (
        "policy",
        "store",
        "lease_queue",
        "provider",
        "billing_events",
        "operations",
    )
    for group in (
        ports.policy,
        ports.store,
        ports.lease_queue,
        ports.provider,
        ports.billing_events,
        ports.operations,
    ):
        assert all(field.type is not Any for field in fields(group))
    assert "__getattr__" not in VideoGenerationPorts.__dict__
    assert "from_flat" not in VideoGenerationPorts.__dict__
    flattened: list[str] = []
    for path in _PARTS_DIR.glob("*.py"):
        if path.name in {"runtime.py", "default_runtime.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            if "video_ports()." not in line:
                continue
            if not any(
                f"video_ports().{group}." in line
                for group in (
                    "policy",
                    "store",
                    "lease_queue",
                    "provider",
                    "billing_events",
                    "operations",
                )
            ):
                flattened.append(f"{path.name}:{line.strip()}")
    assert flattened == []


def test_video_port_call_sites_match_bound_production_signatures() -> None:
    ports = video_generation.DEFAULT_VIDEO_GENERATION_RUNTIME.ports
    referenced: set[tuple[str, str]] = set()
    checked_calls: set[tuple[str, str]] = set()
    failures: list[str] = []
    for path in _PARTS_DIR.glob("*.py"):
        if path.name in {"runtime.py", "default_runtime.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            reference = _video_port_reference(node)
            if reference is not None:
                referenced.add(reference)
            if not isinstance(node, ast.Call):
                continue
            target = _video_port_reference(node.func)
            call_args = node.args
            call_keywords = node.keywords
            if (
                target is None
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "to_thread"
                and node.args
            ):
                target = _video_port_reference(node.args[0])
                call_args = node.args[1:]
            if target is None:
                continue
            group_name, field_name = target
            if group_name == "policy":
                continue
            callable_port = getattr(getattr(ports, group_name), field_name)
            try:
                _bind_ast_call(callable_port, call_args, call_keywords)
            except (TypeError, ValueError, AssertionError) as exc:
                failures.append(
                    f"{path.name}:{node.lineno} {group_name}.{field_name}: {exc}"
                )
            checked_calls.add(target)

    declared = {
        (group_name, field.name)
        for group_name in _PORT_GROUPS
        if group_name != "policy"
        for field in fields(getattr(ports, group_name))
        if callable(getattr(getattr(ports, group_name), field.name))
    }
    assert failures == []
    assert declared - referenced == set()
    assert checked_calls == declared


def test_reference_mime_falls_back_when_upstream_override_is_invalid() -> None:
    assert (
        providers._reference_mime(  # noqa: SLF001
            {
                "mime": "video/mp4",
                "upstream_reference_mime": 123,
            }
        )
        == "video/mp4"
    )
