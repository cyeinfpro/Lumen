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


def _compare_added_facade(source, exports, path="core/__init__.py"):
    import subprocess

    def runner(args, _cwd):
        assert args == ("show", f"original:{path}")
        return subprocess.CompletedProcess(args, 1 if source is None else 0, source or "", "")

    return MODULE.compare_runtime_inventory(
        {"findings": [], "public_api": {path: exports}},
        {"findings": [], "public_api": {}},
        merge_base="original",
        runner=runner,
    )


def test_facade_inventory_accepts_only_preexisting_eager_package_module_exports():
    source = "from . import models, providers as provider_api\n"
    assert _compare_added_facade(source, ["models", "provider_api"]) == []
    assert _compare_added_facade(source, ["models", "new_api"]) == [
        "facade baseline added path: core/__init__.py",
    ]


def test_package_export_proof_respects_explicit_all_and_private_bindings():
    sources = (
        "from . import models\n__all__ = []\n",
        "from . import models\n__all__ = build_exports()\n",
        "from . import models as _private\n",
        "from other import models\n",
        "from .models import models\n",
        "if condition:\n    from . import models\n",
    )
    for source in sources:
        assert _compare_added_facade(source, ["models"]) == [
            "facade baseline added path: core/__init__.py",
        ], source


def test_implicit_package_export_exception_does_not_apply_to_ordinary_modules():
    assert _compare_added_facade("from . import models\n", ["models"], "core/facade.py") == [
        "facade baseline added path: core/facade.py",
    ]


def test_inventory_proof_still_fails_for_missing_base_and_accepts_static_all():
    assert _compare_added_facade(None, ["models"]) == [
        "facade baseline added path: core/__init__.py",
    ]
    assert _compare_added_facade('__all__ = ["models"]\n', ["models"]) == []
