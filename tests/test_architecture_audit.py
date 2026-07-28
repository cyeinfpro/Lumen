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


def _collect(tmp_path: Path, *roots: Path) -> dict[str, RuntimeCouplingFinding]:
    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        return collect_runtime_findings(roots)
    finally:
        MODULE.ROOT = original_root


def test_runtime_coupling_audit_detects_hidden_runtime_edges(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    source = package / "runtime.py"
    source.write_text(
        "import importlib\n"
        "import sys\n"
        "from contextvars import ContextVar\n"
        "from .contracts import _private_hook\n"
        "CACHE = {}\n"
        "_request_id = ContextVar('request_id')\n"
        "sys.modules['alias'] = importlib.import_module('target.module')\n"
        "def reset():\n"
        "    global CACHE\n"
        "    sys.modules.pop('alias', None)\n",
        encoding="utf-8",
    )

    findings = _collect(tmp_path, package)

    categories = [finding.category for finding in findings.values()]
    assert categories.count("dynamic-import") == 1
    assert categories.count("global-statement") == 1
    assert categories.count("module-mutable-state") == 1
    assert categories.count("module-contextvar") == 1
    assert categories.count("private-cross-module-import") == 1
    assert categories.count("sys-modules-mutation") == 2


def test_dynamic_architecture_gate_detects_production_bypasses(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "service"
    package = source_root / "package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "target.py").write_text(
        "def original():\n    return None\n",
        encoding="utf-8",
    )
    (source_root / "legacy_adapter.py").write_text(
        "def run():\n    return None\n",
        encoding="utf-8",
    )
    (package / "runtime.py").write_text(
        "from functools import lru_cache\n"
        "from . import target as target_module\n"
        "import legacy_adapter\n"
        "\n"
        "def replacement():\n"
        "    return None\n"
        "\n"
        "target_module.original = replacement\n"
        "setattr(target_module, 'factory', replacement)\n"
        "\n"
        "@lru_cache(maxsize=1)\n"
        "def shared_service():\n"
        "    return Service()\n",
        encoding="utf-8",
    )

    findings = _collect(tmp_path, source_root)
    categories = [finding.category for finding in findings.values()]

    assert categories.count("module-attribute-replacement") == 1
    assert categories.count("module-setattr") == 1
    assert categories.count("lru-cache-service-singleton") == 1
    assert categories.count("top-level-sibling-import") == 1


def test_dynamic_architecture_gate_ignores_non_bypasses(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "target.py").write_text("MAX_ITEMS = 10\n", encoding="utf-8")
    (package / "runtime.py").write_text(
        "from functools import lru_cache\n"
        "from . import target as target_module\n"
        "import json\n"
        "\n"
        "class Holder:\n"
        "    pass\n"
        "\n"
        "holder = Holder()\n"
        "holder.handler = lambda: None\n"
        "setattr(holder, 'handler', lambda: None)\n"
        "target_module.MAX_ITEMS = 20\n"
        "setattr(target_module, 'MAX_ITEMS', 30)\n"
        "target_module.handler: object\n"
        "\n"
        "@lru_cache(maxsize=128)\n"
        "def lookup_service(key):\n"
        "    return Service(key)\n"
        "\n"
        "@lru_cache(maxsize=1)\n"
        "def cached_constant():\n"
        "    return 42\n"
        "\n"
        "payload = json.dumps({'ok': True})\n",
        encoding="utf-8",
    )

    assert _collect(tmp_path, package) == {}


def test_dynamic_architecture_gate_ignores_test_sources(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "test_runtime.py").write_text(
        "import importlib\nimportlib.import_module('hidden.runtime')\n",
        encoding="utf-8",
    )
    tests_dir = package / "tests"
    tests_dir.mkdir()
    (tests_dir / "runtime.py").write_text(
        "import importlib\nimportlib.import_module('hidden.runtime')\n",
        encoding="utf-8",
    )

    assert _collect(tmp_path, package) == {}


def test_runtime_architecture_scan_fails_closed_on_missing_root(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"

    try:
        _collect(tmp_path, missing)
    except FileNotFoundError as error:
        assert "architecture scan root is missing" in str(error)
    else:
        raise AssertionError("missing architecture scan root was silently skipped")


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

    assert compare_inventory(
        {finding.key},
        baseline,
        public_api,
        public_api,
    ) == ["runtime coupling inventory is stale: removed"]
    assert compare_inventory(
        {finding.key, "new"},
        baseline,
        public_api,
        public_api,
    ) == [
        "new runtime coupling: new",
        "runtime coupling inventory is stale: removed",
    ]


def test_public_api_manifest_rejects_unreviewed_changes() -> None:
    assert compare_inventory(
        set(),
        set(),
        {"facade.py": ["new", "run"]},
        {"facade.py": ["run"]},
    ) == ["public API changed: facade.py expected=['run'] actual=['new', 'run']"]


def test_tgbot_contextvar_runtime_coupling_is_explicit() -> None:
    findings = collect_runtime_findings((ROOT / "apps" / "tgbot" / "app",))

    assert {finding.category for finding in findings.values()} == {"module-contextvar"}


def test_architecture_scripts_do_not_hide_runtime_coupling() -> None:
    findings = collect_runtime_findings(
        (
            ROOT / "scripts" / "architecture_audit.py",
            ROOT / "scripts" / "check_architecture.py",
            ROOT / "scripts" / "check_complexity.py",
        )
    )

    assert findings == {}
