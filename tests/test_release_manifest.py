from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_release_manifest.py"
WORKFLOW = ROOT / ".github" / "workflows" / "docker-release.yml"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_release_manifest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_alembic_heads_requires_one_real_head() -> None:
    module = _load_script()

    assert module.parse_alembic_heads("0041_billing_window_ledger (head)\n") == [
        "0041_billing_window_ledger"
    ]
    with pytest.raises(module.ReleaseManifestError, match="exactly one"):
        module.parse_alembic_heads(
            "0041_billing_window_ledger (head)\n0042_other (head)\n"
        )
    with pytest.raises(module.ReleaseManifestError, match="TODO"):
        module.parse_alembic_heads("TODO: populate alembic heads\n")


def test_cli_writes_complete_machine_readable_manifest(tmp_path: Path) -> None:
    heads = tmp_path / "heads.txt"
    manifest_path = tmp_path / "release-manifest.json"
    notes_path = tmp_path / "release-notes.md"
    heads.write_text("0041_billing_window_ledger (head)\n", encoding="utf-8")
    commit = "a" * 40
    digest_args: list[str] = []
    for index, service in enumerate(("api", "worker", "tgbot", "web"), start=1):
        digest_args.extend(
            ["--image-digest", f"{service}=sha256:{index:064x}"]
        )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--version",
            "v1.2.45",
            "--commit",
            commit,
            "--short-sha",
            commit[:7],
            "--registry",
            "ghcr.io/cyeinfpro",
            "--alembic-heads-file",
            str(heads),
            "--output",
            str(manifest_path),
            "--notes-output",
            str(notes_path),
            "--generated-at",
            "2026-07-10T00:00:00Z",
            *digest_args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["alembic_heads"] == ["0041_billing_window_ledger"]
    assert manifest["commit_sha"] == commit
    assert manifest["version"] == "v1.2.45"
    assert set(manifest["images"]) == {"api", "worker", "tgbot", "web"}
    for service, image in manifest["images"].items():
        assert image["tag"].endswith(f"/lumen-{service}:v1.2.45")
        assert image["immutable_ref"].endswith(image["digest"])
    assert "TODO" not in manifest_path.read_text(encoding="utf-8").upper()
    assert "0041_billing_window_ledger" in notes_path.read_text(encoding="utf-8")


def test_docker_release_publishes_verified_release_manifest() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "uv run alembic heads" in workflow
    assert "--resolve-images" in workflow
    assert "release-manifest.json" in workflow
    assert "files: release-manifest.json" in workflow
    assert "packages: read" in workflow
    assert "populate alembic heads" not in workflow.lower()
