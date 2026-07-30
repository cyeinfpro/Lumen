from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "check_complexity",
    ROOT / "scripts" / "check_complexity.py",
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ComplexityBudget = MODULE.ComplexityBudget
MetricBudget = MODULE.MetricBudget
RoleFileBudget = MODULE.RoleFileBudget
compare_budgets = MODULE.compare_budgets
compare_file_budgets = MODULE.compare_file_budgets
compare_metric_budgets = MODULE.compare_metric_budgets
collect_python_metrics = MODULE.collect_python_metrics
collect_violations = MODULE.collect_violations
function_identities = MODULE.function_identities
role_ceiling_errors = MODULE.role_ceiling_errors
source_role_budget = MODULE.source_role_budget


def test_complexity_gate_requires_improvements_to_tighten_baseline() -> None:
    baseline = {
        "module.py::large": ComplexityBudget(max_complexity=40, count=1),
        "module.py::removed": ComplexityBudget(max_complexity=20, count=1),
    }
    current = {
        "module.py::large": ComplexityBudget(max_complexity=25, count=1),
    }

    assert compare_budgets(current, baseline) == [
        "complexity baseline is stale: module.py::large 40 -> 25",
        "complexity baseline is stale: module.py::removed is no longer a violation",
    ]


def test_complexity_gate_rejects_new_or_growing_violations() -> None:
    baseline = {
        "module.py::large": ComplexityBudget(max_complexity=20, count=1),
        "module.py::duplicate": ComplexityBudget(max_complexity=18, count=1),
    }
    current = {
        "module.py::large": ComplexityBudget(max_complexity=21, count=1),
        "module.py::duplicate": ComplexityBudget(max_complexity=18, count=2),
        "module.py::new": ComplexityBudget(max_complexity=16, count=1),
    }

    assert compare_budgets(current, baseline) == [
        "complexity grew: module.py::large 20 -> 21",
        "violation count grew: module.py::duplicate 1 -> 2",
        "new complexity violation: module.py::new (complexity=16, count=1)",
    ]


def test_complexity_gate_keeps_same_name_functions_separate(
    monkeypatch,
    tmp_path,
) -> None:
    source = tmp_path / "module.py"
    source.write_text(
        "class First:\n"
        "    def dispatch(self):\n"
        "        return 1\n"
        "\n"
        "class Second:\n"
        "    def dispatch(self):\n"
        "        return 2\n",
        encoding="utf-8",
    )
    findings = [
        {
            "filename": str(source),
            "message": "`dispatch` is too complex (16 > 15)",
            "location": {"row": 2, "column": 9},
        },
        {
            "filename": str(source),
            "message": "`dispatch` is too complex (18 > 15)",
            "location": {"row": 6, "column": 9},
        },
    ]

    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps(findings),
            stderr="",
        ),
    )

    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        result = collect_violations(("module.py",))
    finally:
        MODULE.ROOT = original_root

    assert result == {
        "module.py::First.dispatch": ComplexityBudget(max_complexity=16, count=1),
        "module.py::Second.dispatch": ComplexityBudget(max_complexity=18, count=1),
    }


def test_complexity_identity_is_stable_when_lines_are_inserted(tmp_path) -> None:
    source = tmp_path / "module.py"
    source.write_text(
        "def stable():\n    return 1\n",
        encoding="utf-8",
    )
    before = function_identities(source)
    source.write_text(
        "# inserted above\n\ndef stable():\n    return 1\n",
        encoding="utf-8",
    )
    after = function_identities(source)

    assert list(before.values()) == ["stable"]
    assert list(after.values()) == ["stable"]


def test_function_identity_scan_fails_closed_on_syntax_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "module.py"
    source.write_text("def broken(:\n", encoding="utf-8")

    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        with pytest.raises(RuntimeError) as error:
            function_identities(source)
    finally:
        MODULE.ROOT = original_root

    assert error.value.unscanned_files == ("module.py",)
    assert "SyntaxError" in str(error.value)


def test_file_size_scan_fails_closed_on_os_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_source_read(path: Path, *args, **kwargs) -> str:
        if path == source:
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_source_read)
    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        with pytest.raises(RuntimeError) as error:
            MODULE.collect_oversized_files(("module.py",))
    finally:
        MODULE.ROOT = original_root

    assert error.value.unscanned_files == ("module.py",)
    assert "OSError: permission denied" in str(error.value)


def test_python_metric_scan_lists_every_unscanned_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    malformed = source_dir / "malformed.py"
    unreadable = source_dir / "unreadable.py"
    malformed.write_text("def broken(:\n", encoding="utf-8")
    unreadable.write_text("value = 1\n", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_source_read(path: Path, *args, **kwargs) -> str:
        if path == unreadable:
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_source_read)
    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        with pytest.raises(RuntimeError) as error:
            collect_python_metrics(("source",))
    finally:
        MODULE.ROOT = original_root

    assert error.value.unscanned_files == (
        "source/malformed.py",
        "source/unreadable.py",
    )
    assert "SyntaxError" in str(error.value)
    assert "OSError: permission denied" in str(error.value)


def test_complexity_gate_main_fails_closed_and_reports_unscanned_files(
    monkeypatch,
    capsys,
) -> None:
    scan_error = MODULE.ComplexityScanError(
        {
            "source/malformed.py": "SyntaxError: invalid syntax",
            "source/unreadable.py": "OSError: permission denied",
        }
    )

    def fail_scan(_paths: tuple[str, ...]) -> dict:
        raise scan_error

    monkeypatch.setattr(MODULE, "collect_violations", fail_scan)
    monkeypatch.setattr(MODULE, "collect_oversized_files", lambda _paths: {})
    monkeypatch.setattr(
        MODULE,
        "collect_python_metrics",
        lambda _paths: {
            "function_lines": {},
            "function_parameters": {},
            "nesting_depth": {},
        },
    )
    monkeypatch.setattr(sys, "argv", ["check_complexity.py"])

    assert MODULE.main() == 1
    stderr = capsys.readouterr().err
    assert "Complexity scan failed closed; unscanned files:" in stderr
    assert "- source/malformed.py: SyntaxError: invalid syntax" in stderr
    assert "- source/unreadable.py: OSError: permission denied" in stderr


def test_file_size_gate_requires_improvements_to_tighten_baseline() -> None:
    baseline = {
        "existing.py": 2000,
        "removed.py": 1800,
    }
    current = {
        "existing.py": 1900,
        "new.ts": 1600,
    }

    assert compare_file_budgets(current, baseline) == [
        "oversized-file baseline is stale: existing.py 2000 -> 1900",
        "new oversized source file: new.ts (1600 > 1500 lines)",
        "oversized-file baseline is stale: removed.py is no longer oversized",
    ]

    assert compare_file_budgets({"existing.py": 2001}, baseline) == [
        "oversized source file grew: existing.py 2000 -> 2001",
        "oversized-file baseline is stale: removed.py is no longer oversized",
    ]


def test_update_shell_modules_use_strict_400_line_limit(tmp_path: Path) -> None:
    update_dir = tmp_path / "scripts" / "update"
    update_dir.mkdir(parents=True)
    acceptable = update_dir / "acceptable.sh"
    oversized = update_dir / "oversized.sh"
    acceptable.write_text("line\n" * 400, encoding="utf-8")
    oversized.write_text("line\n" * 401, encoding="utf-8")

    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        findings = MODULE.collect_oversized_files(("scripts/update",))
    finally:
        MODULE.ROOT = original_root

    assert findings == {"scripts/update/oversized.sh": 401}
    assert compare_file_budgets(findings, {}) == [
        "new oversized source file: scripts/update/oversized.sh (401 > 400 lines)"
    ]


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (
            "apps/api/app/routes/auth.py",
            ("Python route/controller", 800),
        ),
        (
            "apps/api/app/services/export.py",
            ("Python service/adapter", 1000),
        ),
        (
            "apps/web/src/app/page.tsx",
            ("React page/component", 800),
        ),
        (
            "apps/web/src/app/use-upload-controller.ts",
            ("React hook/controller", 600),
        ),
        (
            "scripts/install.sh",
            ("shell entrypoint", 600),
        ),
        (
            "apps/web/src/lib/domain.ts",
            ("general module", 1000),
        ),
    ],
)
def test_role_file_ceiling_classification(
    tmp_path: Path,
    relative: str,
    expected: tuple[str, int],
) -> None:
    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        assert source_role_budget(tmp_path / relative) == expected
    finally:
        MODULE.ROOT = original_root


def test_role_ceiling_scan_excludes_tests_and_reports_hard_limits(
    tmp_path: Path,
) -> None:
    route = tmp_path / "apps" / "api" / "app" / "routes" / "auth.py"
    component = tmp_path / "apps" / "web" / "src" / "Feature.tsx"
    hook = tmp_path / "apps" / "web" / "src" / "useFeature.ts"
    test_file = tmp_path / "apps" / "web" / "src" / "Feature.test.tsx"
    for path, lines in (
        (route, 801),
        (component, 801),
        (hook, 601),
        (test_file, 1200),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("line\n" * lines, encoding="utf-8")

    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        findings = MODULE.collect_role_ceiling_violations(("apps",))
    finally:
        MODULE.ROOT = original_root

    assert findings == {
        "apps/api/app/routes/auth.py": RoleFileBudget(
            role="Python route/controller",
            line_count=801,
            limit=800,
        ),
        "apps/web/src/Feature.tsx": RoleFileBudget(
            role="React page/component",
            line_count=801,
            limit=800,
        ),
        "apps/web/src/useFeature.ts": RoleFileBudget(
            role="React hook/controller",
            line_count=601,
            limit=600,
        ),
    }
    assert role_ceiling_errors(findings) == [
        "role ceiling exceeded: apps/api/app/routes/auth.py "
        "(Python route/controller, 801 > 800 lines)",
        "role ceiling exceeded: apps/web/src/Feature.tsx "
        "(React page/component, 801 > 800 lines)",
        "role ceiling exceeded: apps/web/src/useFeature.ts "
        "(React hook/controller, 601 > 600 lines)",
    ]


def test_python_metrics_report_multiple_complexity_dimensions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "module.py"
    nested = "\n".join(
        [
            "def large(a, b, c, d, e, f, g, h, i, j, k, l, m):",
            "    if a:",
            "        if b:",
            "            if c:",
            "                if d:",
            "                    if e:",
            "                        if f:",
            "                            if g:",
            *["                                value = 1" for _ in range(195)],
        ]
    )
    source.write_text(nested + "\n", encoding="utf-8")

    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        metrics = collect_python_metrics(("module.py",))
    finally:
        MODULE.ROOT = original_root

    key = "module.py::large"
    assert metrics["function_lines"][key].value > MODULE.MAX_FUNCTION_LINES
    assert metrics["function_parameters"][key].value == 13
    assert metrics["nesting_depth"][key].value == 7


def test_multi_dimensional_metrics_require_improvements_to_tighten_baseline() -> None:
    baseline = {
        "function_lines": {
            "module.py::large": MetricBudget(250),
            "module.py::removed": MetricBudget(210),
        },
        "function_parameters": {},
        "nesting_depth": {},
    }
    current = {
        "function_lines": {"module.py::large": MetricBudget(220)},
        "function_parameters": {"module.py::wide": MetricBudget(14)},
        "nesting_depth": {},
    }

    assert compare_metric_budgets(current, baseline) == [
        "function_lines baseline is stale: module.py::large 250 -> 220",
        "new function_parameters violation: module.py::wide (value=14)",
        "function_lines baseline is stale: module.py::removed is no longer a violation",
    ]
    current["function_lines"]["module.py::large"] = MetricBudget(251)
    assert compare_metric_budgets(current, baseline) == [
        "function_lines grew: module.py::large 250 -> 251",
        "new function_parameters violation: module.py::wide (value=14)",
        "function_lines baseline is stale: module.py::removed is no longer a violation",
    ]
