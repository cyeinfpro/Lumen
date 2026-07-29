from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "lumen_dead_code_audit",
    ROOT / "scripts" / "dead_code_audit.py",
)
assert SPEC is not None and SPEC.loader is not None
dead_code_audit = module_from_spec(SPEC)
sys.modules[SPEC.name] = dead_code_audit
SPEC.loader.exec_module(dead_code_audit)


def _write_web_manifests(root: Path, *, include_retired: bool = False) -> None:
    web = root / "apps/web"
    web.mkdir(parents=True)
    dependencies = {"gsap": "1.0.0"} if include_retired else {}
    (web / "package.json").write_text(
        json.dumps({"dependencies": dependencies}),
        encoding="utf-8",
    )
    packages = {"node_modules/gsap": {}} if include_retired else {}
    (web / "package-lock.json").write_text(
        json.dumps({"packages": packages}),
        encoding="utf-8",
    )


def test_current_retirement_inventory_passes() -> None:
    assert dead_code_audit.audit(ROOT) == []


def test_retired_path_and_dependency_fail_closed(tmp_path: Path) -> None:
    retired = tmp_path / dead_code_audit.RETIRED_PATHS[0]
    retired.parent.mkdir(parents=True)
    retired.write_text("# stale\n", encoding="utf-8")
    _write_web_manifests(tmp_path, include_retired=True)

    errors = dead_code_audit.audit(tmp_path)

    assert any("retired path still exists" in error for error in errors)
    assert any("retired npm dependency remains: gsap" in error for error in errors)
    assert any("retired npm lock entry remains: gsap" in error for error in errors)


def test_retired_import_marker_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "apps/api/app/example.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from app.upstream_parts.errors import UpstreamError\n",
        encoding="utf-8",
    )
    _write_web_manifests(tmp_path)

    errors = dead_code_audit.audit(tmp_path)

    assert any("upstream_parts.errors" in error for error in errors)
