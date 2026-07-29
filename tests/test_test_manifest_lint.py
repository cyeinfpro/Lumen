from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "lumen_test_manifest_lint",
    ROOT / "scripts" / "test_manifest_lint.py",
)
assert SPEC is not None and SPEC.loader is not None
test_manifest_lint = module_from_spec(SPEC)
sys.modules[SPEC.name] = test_manifest_lint
SPEC.loader.exec_module(test_manifest_lint)


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_manifest(path: Path, rules: str, *, full_paths: str = "[]") -> None:
    _write(
        path,
        f"""
[planner]
max_reverse_depth = 2
exclusive_resources = []
full_mandatory_paths = {full_paths}

[[rules]]
name = "api-general"
paths = ["apps/api/app/**"]
commands = ["uv run pytest -q apps/api/tests"]
gates = []
risk = []
resources = []
fallback = true

{rules}
""".strip()
        + "\n",
    )


def test_reports_stale_empty_glob_missing_test_and_uncovered_production(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "apps/api/app/routes/live.py")
    _write(tmp_path / "apps/worker/app/jobs.py")
    _write(tmp_path / "apps/api/tests/test_live.py")
    _write_manifest(
        tmp_path / "manifest.toml",
        """
[[rules]]
name = "realtime"
paths = [
  "apps/api/app/routes/missing.py",
  "apps/api/app/realtime/**",
]
commands = [
  "uv run pytest -q apps/api/tests/test_live.py apps/api/tests/test_missing.py",
]
gates = []
risk = []
resources = []
""".strip(),
    )

    report = test_manifest_lint.audit_manifest(
        tmp_path / "manifest.toml",
        repo_root=tmp_path,
        production_patterns=(
            "apps/api/app/**/*.py",
            "apps/worker/app/**/*.py",
        ),
        critical_domains={},
    )

    assert {(finding.code, finding.subject) for finding in report.stale} == {
        ("missing-literal", "apps/api/app/routes/missing.py"),
        ("missing-test-target", "apps/api/tests/test_missing.py"),
    }
    assert {(finding.code, finding.subject) for finding in report.unmatched} == {
        ("empty-glob", "apps/api/app/realtime/**"),
        ("uncovered-production-file", "apps/worker/app/jobs.py"),
    }
    assert report.ok is False


def test_allow_empty_only_suppresses_empty_glob(tmp_path: Path) -> None:
    _write(tmp_path / "apps/api/app/main.py")
    _write(tmp_path / "apps/api/tests/test_main.py")
    _write_manifest(
        tmp_path / "manifest.toml",
        """
[[rules]]
name = "future-realtime"
paths = ["apps/api/app/realtime/**"]
commands = ["uv run pytest -q apps/api/tests/test_main.py"]
gates = []
risk = []
resources = []
allow_empty = true
""".strip(),
    )

    report = test_manifest_lint.audit_manifest(
        tmp_path / "manifest.toml",
        repo_root=tmp_path,
        production_patterns=("apps/api/app/**/*.py",),
        critical_domains={},
    )

    assert report.unmatched == ()
    assert report.stale == ()
    assert report.ok is True


def test_high_risk_file_requires_non_fallback_rule(tmp_path: Path) -> None:
    _write(tmp_path / "apps/api/app/routes/events.py")
    _write(tmp_path / "apps/api/tests/test_events.py")
    _write_manifest(tmp_path / "manifest.toml", "")

    report = test_manifest_lint.audit_manifest(
        tmp_path / "manifest.toml",
        repo_root=tmp_path,
        production_patterns=("apps/api/app/**/*.py",),
        critical_domains={
            "backend-realtime": ("apps/api/app/routes/events.py",),
        },
    )

    assert [
        (finding.owner, finding.subject) for finding in report.critical_fallback_only
    ] == [
        (
            "critical-domain:backend-realtime",
            "apps/api/app/routes/events.py",
        )
    ]
    assert "api-general" in report.critical_fallback_only[0].reason
    assert report.ok is False


def test_shadowed_fallback_and_always_full_reasons_are_reported(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "apps/api/app/routes/events.py")
    _write(tmp_path / "apps/api/tests/test_events.py")
    _write_manifest(
        tmp_path / "manifest.toml",
        """
[[rules]]
name = "backend-realtime"
paths = ["apps/api/app/**"]
commands = ["uv run pytest -q apps/api/tests/test_events.py"]
gates = []
risk = []
resources = []
""".strip(),
        full_paths='["apps/api/app/routes/events.py"]',
    )

    report = test_manifest_lint.audit_manifest(
        tmp_path / "manifest.toml",
        repo_root=tmp_path,
        production_patterns=("apps/api/app/**/*.py",),
        critical_domains={
            "backend-realtime": ("apps/api/app/routes/events.py",),
        },
    )

    assert [(finding.code, finding.subject) for finding in report.shadowed] == [
        ("shadowed-fallback", "api-general")
    ]
    assert [(finding.subject, finding.reason) for finding in report.always_full] == [
        (
            "apps/api/app/routes/events.py",
            "1 matching path(s): apps/api/app/routes/events.py",
        )
    ]
    assert report.ok is True


def test_cli_json_returns_failure_for_manifest_errors(
    tmp_path: Path,
    capsys,
) -> None:
    _write(tmp_path / "apps/api/app/main.py")
    _write_manifest(
        tmp_path / "manifest.toml",
        """
[[rules]]
name = "broken"
paths = ["apps/api/app/missing.py"]
commands = ["uv run pytest -q apps/api/tests/test_missing.py"]
gates = []
risk = []
resources = []
""".strip(),
    )

    exit_code = test_manifest_lint.main(
        [
            "--repo-root",
            str(tmp_path),
            "--manifest",
            str(tmp_path / "manifest.toml"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["stale"][0]["code"] == "missing-literal"


def test_default_production_patterns_cover_real_image_job_package() -> None:
    assert "image-job/image_job/**/*.py" in (
        test_manifest_lint.PRODUCTION_PATTERNS
    )
    assert "image-job/app/**/*.py" not in (
        test_manifest_lint.PRODUCTION_PATTERNS
    )
