from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "architecture_audit",
    ROOT / "scripts" / "architecture_audit.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

RuntimeCouplingFinding = MODULE.RuntimeCouplingFinding
collect_runtime_findings = MODULE.collect_runtime_findings
compare_inventory = MODULE.compare_inventory


def test_runtime_coupling_audit_detects_hidden_runtime_edges(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    source = package / "runtime.py"
    source.write_text(
        "import importlib\n"
        "import sys\n"
        "from .contracts import _private_hook\n"
        "CACHE = {}\n"
        "sys.modules['alias'] = importlib.import_module('target.module')\n"
        "def reset():\n"
        "    global CACHE\n"
        "    sys.modules.pop('alias', None)\n",
        encoding="utf-8",
    )

    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        findings = collect_runtime_findings((package,))
    finally:
        MODULE.ROOT = original_root

    categories = [finding.category for finding in findings.values()]
    assert categories.count("dynamic-import") == 1
    assert categories.count("global-statement") == 1
    assert categories.count("module-mutable-state") == 1
    assert categories.count("private-cross-module-import") == 1
    assert categories.count("sys-modules-mutation") == 2


def test_runtime_inventory_is_a_one_way_ratchet() -> None:
    finding = RuntimeCouplingFinding(
        "dynamic-import",
        "module.py",
        10,
        "import_module",
        "target",
    )
    baseline = {finding.key, "removed"}
    public_api = {"facade.py": ["run"]}

    assert (
        compare_inventory(
            {finding.key},
            baseline,
            public_api,
            public_api,
        )
        == []
    )
    assert compare_inventory(
        {finding.key, "new"},
        baseline,
        public_api,
        public_api,
    ) == ["new runtime coupling: new"]


def test_public_api_manifest_rejects_unreviewed_changes() -> None:
    assert compare_inventory(
        set(),
        set(),
        {"facade.py": ["new", "run"]},
        {"facade.py": ["run"]},
    ) == ["public API changed: facade.py expected=['run'] actual=['new', 'run']"]


def test_tgbot_runtime_coupling_inventory_is_zero() -> None:
    findings = collect_runtime_findings((ROOT / "apps" / "tgbot" / "app",))

    assert findings == {}


def test_architecture_scripts_do_not_hide_runtime_coupling() -> None:
    findings = collect_runtime_findings(
        (
            ROOT / "scripts" / "architecture_audit.py",
            ROOT / "scripts" / "check_architecture.py",
            ROOT / "scripts" / "check_complexity.py",
        )
    )

    assert findings == {}
