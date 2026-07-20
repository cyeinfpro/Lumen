from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.tasks import (
    volcano_asset_actions,
    volcano_asset_create,
    volcano_asset_dispatch,
    volcano_assets,
)
from app.tasks.volcano_asset_runtime import (
    VolcanoAssetRuntimeContext,
    VolcanoAssetRuntimeSlot,
)


@pytest.mark.parametrize(
    "module",
    (
        volcano_asset_actions,
        volcano_asset_create,
        volcano_asset_dispatch,
    ),
)
def test_volcano_parts_do_not_import_task_facade(module: object) -> None:
    path = Path(str(getattr(module, "__file__")))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    back_imports: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "app.tasks.volcano_assets" for alias in node.names):
                back_imports.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            source = node.module or ""
            imports_facade_module = source == "app.tasks.volcano_assets" or (
                node.level > 0 and source == "volcano_assets"
            )
            imports_facade_name = any(
                alias.name == "volcano_assets" for alias in node.names
            ) and (node.level > 0 or source == "app.tasks")
            if imports_facade_module or imports_facade_name:
                back_imports.append(node.lineno)
    assert back_imports == []


@pytest.mark.parametrize(
    "module",
    (
        volcano_asset_actions,
        volcano_asset_create,
        volcano_asset_dispatch,
    ),
)
def test_volcano_parts_declare_their_runtime_dependencies(module: object) -> None:
    path = Path(str(getattr(module, "__file__")))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    accessed: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        direct_runtime = isinstance(node.value, ast.Name) and node.value.id == "runtime"
        called_runtime = (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_runtime"
        )
        if direct_runtime or called_runtime:
            accessed.add(node.attr)

    runtime_slot = getattr(module, "_RUNTIME")
    assert accessed == runtime_slot.dependencies


def test_runtime_slot_is_late_bound_and_restricted() -> None:
    namespace = {"allowed": object()}
    slot = VolcanoAssetRuntimeSlot(
        owner="test.runtime",
        dependencies=frozenset({"allowed"}),
    )
    slot.install(VolcanoAssetRuntimeContext(namespace.__getitem__))
    runtime = slot.get()

    first = runtime.allowed
    namespace["allowed"] = object()

    assert runtime.allowed is namespace["allowed"]
    assert runtime.allowed is not first
    with pytest.raises(AttributeError, match="does not declare runtime dependency"):
        _ = runtime.undeclared


def test_facade_monkeypatch_is_visible_to_installed_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = object()

    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", replacement)

    assert volcano_asset_actions._runtime().VolcanoAssetClient is replacement
    assert volcano_asset_create._runtime().VolcanoAssetClient is replacement
