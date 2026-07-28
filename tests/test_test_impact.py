from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "lumen_test_impact",
    ROOT / "scripts" / "test_impact.py",
)
assert SPEC is not None and SPEC.loader is not None
test_impact = module_from_spec(SPEC)
sys.modules[SPEC.name] = test_impact
SPEC.loader.exec_module(test_impact)


def _write_manifest(path: Path) -> None:
    path.write_text(
        """
[planner]
max_reverse_depth = 2
exclusive_resources = ["postgres", "redis", "filesystem", "web-build"]
full_mandatory_paths = [
  "packages/core/**",
  "uv.lock",
  ".github/workflows/**",
]

[gates.backend-architecture]
command = "uv run python scripts/check_architecture.py"

[gates.web-typecheck]
command = "cd apps/web && npm run type-check"
resources = ["web-build"]

[[rules]]
name = "api-images"
paths = ["apps/api/app/images/**"]
commands = [
  "uv run pytest -q apps/api/tests/images/test_artifact_saga.py",
]
gates = ["backend-architecture"]
risk = ["filesystem"]
resources = ["filesystem"]

[[rules]]
name = "web-realtime"
paths = ["apps/web/src/lib/sse/**"]
commands = [
  "cd apps/web && npm test -- src/lib/sse/runtime.test.ts",
]
gates = ["web-typecheck"]
risk = []
resources = []

[[rules]]
name = "api-callers"
paths = ["apps/api/app/routes/**"]
commands = ["uv run pytest -q apps/api/tests/test_routes.py"]
gates = ["backend-architecture"]
risk = []
resources = []

[[rules]]
name = "shared-core"
paths = ["packages/core/**"]
commands = [
  "uv run pytest -q packages/core/tests",
  "uv run pytest -q apps/api/tests/test_core_security_infra.py",
  "uv run pytest -q apps/worker/tests/test_billing_idempotency.py",
]
gates = ["backend-architecture"]
risk = ["shared-core"]
resources = []

[[rules]]
name = "api-general"
paths = ["apps/api/app/**"]
commands = ["uv run pytest -q apps/api/tests"]
gates = ["backend-architecture"]
risk = []
resources = []
fallback = true
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_plan_records_matches_reverse_imports_and_unselected_suites(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "test-manifest.toml"
    _write_manifest(manifest_path)
    manifest = test_impact.load_manifest(manifest_path)

    plan = test_impact.build_plan(
        manifest,
        changed_files=["apps/api/app/images/service.py"],
        base="base",
        head="head",
        reverse_imports=[
            {
                "changed_file": "apps/api/app/images/service.py",
                "changed_module": "app.images.service",
                "callers": [
                    {
                        "file": "apps/api/app/routes/images.py",
                        "module": "app.routes.images",
                        "depth": 1,
                    }
                ],
            }
        ],
    )

    assert plan["changed_files"] == ["apps/api/app/images/service.py"]
    assert [rule["name"] for rule in plan["matched_rules"]] == [
        "api-callers",
        "api-images",
    ]
    assert {command["kind"] for command in plan["commands"]} == {"gate", "test"}
    assert (
        sum(
            command["command"] == "uv run python scripts/check_architecture.py"
            for command in plan["commands"]
        )
        == 1
    )
    assert all(
        command["command"] != "uv run pytest -q apps/api/tests"
        for command in plan["commands"]
    )
    assert plan["full_mandatory"] is False
    assert plan["not_run_suites"] == [
        {
            "name": "api-general",
            "reason": "no changed or reverse-dependent path matched",
        },
        {
            "name": "shared-core",
            "reason": "no changed or reverse-dependent path matched",
        },
        {
            "name": "web-realtime",
            "reason": "no changed or reverse-dependent path matched",
        },
    ]


def test_shared_core_lock_and_ci_changes_require_final_full(tmp_path: Path) -> None:
    manifest_path = tmp_path / "test-manifest.toml"
    _write_manifest(manifest_path)
    manifest = test_impact.load_manifest(manifest_path)

    plan = test_impact.build_plan(
        manifest,
        changed_files=[
            ".github/workflows/ci.yml",
            "packages/core/lumen_core/models.py",
            "uv.lock",
        ],
        base="base",
        head="head",
        reverse_imports=[],
    )

    assert plan["full_mandatory"] is True
    assert plan["full_mandatory_reasons"] == [
        {
            "file": ".github/workflows/ci.yml",
            "pattern": ".github/workflows/**",
        },
        {
            "file": "packages/core/lumen_core/models.py",
            "pattern": "packages/core/**",
        },
        {"file": "uv.lock", "pattern": "uv.lock"},
    ]
    commands = {entry["command"] for entry in plan["commands"]}
    assert "uv run pytest -q packages/core/tests" in commands
    assert "uv run pytest -q apps/api/tests/test_core_security_infra.py" in commands
    assert "uv run pytest -q apps/worker/tests/test_billing_idempotency.py" in commands


def test_reverse_imports_stop_after_second_level(tmp_path: Path) -> None:
    package_root = tmp_path / "apps/demo/app"
    package_root.mkdir(parents=True)
    for name, source in {
        "__init__.py": "",
        "changed.py": "VALUE = 1\n",
        "direct.py": "import app.changed\n",
        "second.py": "import app.direct\n",
        "third.py": "import app.second\n",
    }.items():
        (package_root / name).write_text(source, encoding="utf-8")

    impacts = test_impact.collect_reverse_imports(
        ["apps/demo/app/changed.py"],
        repo_root=tmp_path,
        specs=(
            test_impact.PackageSpec(
                name="demo",
                root=package_root,
                package="app",
            ),
        ),
        max_depth=2,
    )

    assert impacts == [
        {
            "changed_file": "apps/demo/app/changed.py",
            "changed_module": "app.changed",
            "callers": [
                {
                    "file": "apps/demo/app/direct.py",
                    "module": "app.direct",
                    "depth": 1,
                },
                {
                    "file": "apps/demo/app/second.py",
                    "module": "app.second",
                    "depth": 2,
                },
            ],
        }
    ]


@pytest.mark.parametrize(
    ("changed_file", "expected_rule", "forbidden_command"),
    [
        (
            "apps/api/app/routes/events.py",
            "backend-realtime",
            "uv run pytest -q apps/api/tests",
        ),
        (
            "apps/worker/app/tasks/generation_parts/queue.py",
            "worker-generation-queue",
            "uv run pytest -q apps/worker/tests",
        ),
        (
            "apps/worker/app/upstream_parts/direct_images.py",
            "worker-upstream-images",
            "uv run pytest -q apps/worker/tests",
        ),
        (
            "apps/api/app/routes/generations.py",
            "api-stream-assets",
            "uv run pytest -q apps/api/tests",
        ),
        (
            "apps/web/src/lib/imagePreload.ts",
            "web-stream-assets",
            "cd apps/web && npm test",
        ),
    ],
)
def test_current_manifest_routes_critical_domains_without_generic_fallback(
    changed_file: str,
    expected_rule: str,
    forbidden_command: str,
) -> None:
    manifest = test_impact.load_manifest(ROOT / "scripts" / "test-manifest.toml")

    plan = test_impact.build_plan(
        manifest,
        changed_files=[changed_file],
        base="base",
        head="head",
        reverse_imports=[],
    )

    assert expected_rule in {rule["name"] for rule in plan["matched_rules"]}
    assert forbidden_command not in {command["command"] for command in plan["commands"]}
