from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
from types import SimpleNamespace


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
compare_budgets = MODULE.compare_budgets
compare_file_budgets = MODULE.compare_file_budgets
compare_metric_budgets = MODULE.compare_metric_budgets
collect_python_metrics = MODULE.collect_python_metrics
collect_violations = MODULE.collect_violations
function_identities = MODULE.function_identities


def test_complexity_gate_allows_existing_violations_to_shrink() -> None:
    baseline = {
        "module.py::large": ComplexityBudget(max_complexity=40, count=1),
        "module.py::removed": ComplexityBudget(max_complexity=20, count=1),
    }
    current = {
        "module.py::large": ComplexityBudget(max_complexity=25, count=1),
    }

    assert compare_budgets(current, baseline) == []


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


def test_file_size_gate_only_allows_oversized_files_to_shrink() -> None:
    baseline = {
        "existing.py": 2000,
        "removed.py": 1800,
    }
    current = {
        "existing.py": 1900,
        "new.ts": 1600,
    }

    assert compare_file_budgets(current, baseline) == [
        "new oversized source file: new.ts (1600 > 1500 lines)",
    ]

    assert compare_file_budgets({"existing.py": 2001}, baseline) == [
        "oversized source file grew: existing.py 2000 -> 2001",
    ]


def test_update_shell_modules_use_strict_600_line_limit(tmp_path: Path) -> None:
    update_dir = tmp_path / "scripts" / "update"
    update_dir.mkdir(parents=True)
    acceptable = update_dir / "acceptable.sh"
    oversized = update_dir / "oversized.sh"
    acceptable.write_text("line\n" * 599, encoding="utf-8")
    oversized.write_text("line\n" * 600, encoding="utf-8")

    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        findings = MODULE.collect_oversized_files(("scripts/update",))
    finally:
        MODULE.ROOT = original_root

    assert findings == {"scripts/update/oversized.sh": 600}
    assert compare_file_budgets(findings, {}) == [
        "new oversized source file: scripts/update/oversized.sh (600 > 599 lines)"
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


def test_multi_dimensional_metrics_only_allow_debt_to_shrink() -> None:
    baseline = {
        "function_lines": {"module.py::large": MetricBudget(250)},
        "function_parameters": {},
        "nesting_depth": {},
    }
    current = {
        "function_lines": {"module.py::large": MetricBudget(220)},
        "function_parameters": {"module.py::wide": MetricBudget(14)},
        "nesting_depth": {},
    }

    assert compare_metric_budgets(current, baseline) == [
        "new function_parameters violation: module.py::wide (value=14)"
    ]
    current["function_lines"]["module.py::large"] = MetricBudget(251)
    assert compare_metric_budgets(current, baseline) == [
        "function_lines grew: module.py::large 250 -> 251",
        "new function_parameters violation: module.py::wide (value=14)",
    ]
