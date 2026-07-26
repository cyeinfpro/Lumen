from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "module_runtime_state_audit",
    ROOT / "scripts" / "module_runtime_state_audit.py",
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ModuleRuntimeFinding = MODULE.ModuleRuntimeFinding
audit_runtime_state = MODULE.audit_runtime_state
collect_module_runtime_findings = MODULE.collect_module_runtime_findings
load_ledger = MODULE.load_ledger


def _ledger(tmp_path: Path, modules: list[dict], *, max_total: int = 10) -> dict:
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {"version": 1, "max_total": max_total, "modules": modules},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return load_ledger(path)


def test_detects_mutable_dataclass_instance_and_ignores_frozen(
    tmp_path: Path,
) -> None:
    package = tmp_path / "app"
    package.mkdir()
    source = package / "runtime.py"
    source.write_text(
        """
from dataclasses import dataclass, field

@dataclass
class RuntimeState:
    values: dict[str, int] = field(default_factory=dict)

@dataclass(frozen=True)
class ImmutableConfig:
    value: int = 1

_runtime = RuntimeState()
_config = ImmutableConfig()
""".lstrip(),
        encoding="utf-8",
    )

    findings = collect_module_runtime_findings((package,), root=tmp_path)

    assert list(findings.values()) == [
        ModuleRuntimeFinding(
            path="app/runtime.py",
            line=11,
            symbol="_runtime",
            class_name="RuntimeState",
        )
    ]


def test_unowned_module_runtime_state_fails(tmp_path: Path) -> None:
    finding = ModuleRuntimeFinding(
        path="app/new_runtime.py",
        line=10,
        symbol="_runtime",
        class_name="RuntimeState",
    )
    ledger = _ledger(tmp_path, [])

    assert audit_runtime_state({finding.key: finding}, ledger) == [
        "unowned module runtime state: app/new_runtime.py|_runtime|RuntimeState"
    ]


def test_owned_runtime_state_may_only_shrink(tmp_path: Path) -> None:
    known = ModuleRuntimeFinding(
        path="app/runtime.py",
        line=10,
        symbol="_runtime",
        class_name="RuntimeState",
    )
    extra = ModuleRuntimeFinding(
        path="app/runtime.py",
        line=11,
        symbol="_other",
        class_name="RuntimeState",
    )
    ledger = _ledger(
        tmp_path,
        [
            {
                "path": "app/runtime.py",
                "owner": "api",
                "max_instances": 1,
                "symbols": ["_runtime"],
                "retirement_condition": "move to lifespan",
            }
        ],
    )

    assert audit_runtime_state({known.key: known}, ledger) == []
    assert audit_runtime_state(
        {known.key: known, extra.key: extra},
        ledger,
    ) == [
        "module runtime state budget grew: app/runtime.py current=2 budget=1",
        "unexpected module runtime symbol: app/runtime.py|_other",
    ]


def test_total_budget_prevents_cross_module_growth(tmp_path: Path) -> None:
    first = ModuleRuntimeFinding("app/a.py", 1, "_a", "State")
    second = ModuleRuntimeFinding("app/b.py", 1, "_b", "State")
    ledger = _ledger(
        tmp_path,
        [
            {
                "path": "app/a.py",
                "owner": "api",
                "max_instances": 1,
                "symbols": ["_a"],
                "retirement_condition": "remove",
            },
            {
                "path": "app/b.py",
                "owner": "api",
                "max_instances": 1,
                "symbols": ["_b"],
                "retirement_condition": "remove",
            },
        ],
        max_total=1,
    )

    assert audit_runtime_state(
        {first.key: first, second.key: second},
        ledger,
    ) == ["module runtime state total grew: current=2 budget=1"]
