from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "facade_inventory",
    ROOT / "scripts" / "facade_inventory.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_reexport_facade_is_discovered_and_business_module_is_ignored(
    tmp_path: Path,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "facade.py").write_text(
        'from .target import VALUE\n__all__ = ["VALUE"]\n',
        encoding="utf-8",
    )
    (package / "business.py").write_text(
        'def value():\n    return 1\n__all__ = ["value"]\n',
        encoding="utf-8",
    )

    findings = MODULE.discover_facades(((package, "pkg"),))

    assert list(findings) == [(package / "facade.py").as_posix()]
    finding = next(iter(findings.values()))
    assert finding.reason == "re-export"
    assert finding.public_api == ("VALUE",)


def test_unregistered_and_stale_facades_fail() -> None:
    finding = MODULE.FacadeFinding(
        path="pkg/facade.py",
        module="pkg.facade",
        public_api=("VALUE",),
        reason="re-export",
        caller_count=2,
    )

    assert MODULE.audit_facades({finding.path: finding}, {}) == [
        "unregistered compatibility facade: pkg/facade.py"
    ]
    assert MODULE.audit_facades(
        {finding.path: finding},
        {
            finding.path: {
                "caller_count": 1,
                "owner": "core",
                "path": finding.path,
                "retirement_condition": "remove",
                "status": "active",
            },
            "pkg/stale.py": {
                "caller_count": 0,
                "owner": "core",
                "path": "pkg/stale.py",
                "retirement_condition": "remove",
                "status": "active",
            },
        },
    ) == [
        "facade caller count is stale: pkg/facade.py ledger=1 current=2",
        "stale compatibility facade entry: pkg/stale.py",
    ]
