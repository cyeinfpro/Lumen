from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "baseline_monotonic",
    ROOT / "scripts" / "baseline_monotonic.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_architecture_and_complexity_baselines_cannot_expand() -> None:
    assert MODULE.compare_architecture(
        {"violations": ["known", "new"], "cycles": []},
        {"violations": ["known"], "cycles": []},
    ) == ["architecture baseline grew: violations:new"]

    errors = MODULE.compare_complexity(
        {
            "max_complexity": 15,
            "max_file_lines": 1500,
            "max_shell_file_lines": 400,
            "metric_thresholds": {"function_lines": 200},
            "oversized_files": {"existing.py": 1200, "new.py": 1600},
            "violations": {},
            "metrics": {},
        },
        {
            "max_complexity": 15,
            "max_file_lines": 1500,
            "max_shell_file_lines": 400,
            "metric_thresholds": {"function_lines": 200},
            "oversized_files": {"existing.py": 1400},
            "violations": {},
            "metrics": {},
        },
    )
    assert errors == ["oversized file baseline added entry: new.py"]


def test_runtime_inventory_only_shrinks() -> None:
    errors = MODULE.compare_runtime_inventory(
        {
            "findings": [
                {
                    "category": "dynamic-import",
                    "path": "a.py",
                    "symbol": "load",
                    "target": "x",
                }
            ],
            "public_api": {"facade.py": ["existing", "new"]},
        },
        {
            "findings": [],
            "public_api": {"facade.py": ["existing"]},
        },
    )
    assert errors == [
        "runtime coupling baseline grew: dynamic-import|a.py|load|x",
        "facade public API grew: facade.py:new",
    ]


def test_runtime_scope_expansion_only_registers_preexisting_symbols(
    tmp_path: Path,
) -> None:
    source = "_RUNTIME = Runtime()\n"

    def runner(args, _cwd):
        import subprocess

        if args[0] == "show":
            return subprocess.CompletedProcess(args, 0, source, "")
        raise AssertionError(args)

    current = {
        "max_total": 2,
        "modules": [
            {
                "path": "known.py",
                "max_instances": 1,
                "symbols": ["_KNOWN"],
            },
            {
                "path": "hidden.py",
                "max_instances": 1,
                "symbols": ["_RUNTIME"],
            },
        ],
    }
    base = {
        "max_total": 1,
        "modules": [
            {
                "path": "known.py",
                "max_instances": 1,
                "symbols": ["_KNOWN"],
            }
        ],
    }

    assert (
        MODULE.compare_runtime_ledger(
            current,
            base,
            merge_base="abc",
            root=tmp_path,
            runner=runner,
        )
        == []
    )

    current["modules"][1]["symbols"] = ["_NEW_RUNTIME"]
    assert MODULE.compare_runtime_ledger(
        current,
        base,
        merge_base="abc",
        root=tmp_path,
        runner=runner,
    ) == [
        "runtime ledger added new symbol: hidden.py|_NEW_RUNTIME",
        "runtime ledger total grew beyond pre-existing hidden state: allowed=1 current=2",
    ]
