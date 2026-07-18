from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_release_manifest.py"
WORKFLOW = ROOT / ".github" / "workflows" / "docker-release.yml"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_release_manifest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_workflow() -> dict[str, object]:
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict)
    return workflow


def _step(job: dict[str, object], name: str) -> dict[str, object]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    for step in steps:
        if isinstance(step, dict) and step.get("name") == name:
            return step
    raise AssertionError(f"workflow step not found: {name}")


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
        digest_args.extend(["--image-digest", f"{service}=sha256:{index:064x}"])

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
    for index, service in enumerate(("api", "worker", "tgbot", "web"), start=1):
        image = manifest["images"][service]
        expected_digest = f"sha256:{index:064x}"
        assert image["tag"].endswith(f"/lumen-{service}:v1.2.45")
        assert image["digest"] == expected_digest
        assert image["immutable_ref"] == (
            f"ghcr.io/cyeinfpro/lumen-{service}@{expected_digest}"
        )
    assert "TODO" not in manifest_path.read_text(encoding="utf-8").upper()
    assert "0041_billing_window_ledger" in notes_path.read_text(encoding="utf-8")


def test_docker_release_publishes_verified_release_manifest() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "uv run alembic heads" in workflow
    assert '--image-digest "api=${API_DIGEST}"' in workflow
    assert '--image-digest "worker=${WORKER_DIGEST}"' in workflow
    assert '--image-digest "tgbot=${TGBOT_DIGEST}"' in workflow
    assert '--image-digest "web=${WEB_DIGEST}"' in workflow
    assert "--resolve-images" not in workflow
    assert "release-manifest.json" in workflow
    assert "files: release-manifest.json" in workflow
    assert "packages: read" in workflow
    assert "populate alembic heads" not in workflow.lower()


def test_docker_release_binds_dispatch_builds_to_resolved_commit() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "resolve-ref:" in workflow
    assert "ref: ${{ github.event.inputs.ref || github.sha }}" in workflow
    assert "startsWith(github.event.inputs.ref, 'refs/tags/')" in workflow
    assert "commit=\"$(git rev-parse --verify 'HEAD^{commit}')\"" in workflow
    assert "ref: ${{ needs.resolve-ref.outputs.commit }}" in workflow
    assert (
        'build_tag="sha-${commit}-run-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"'
        in workflow
    )
    assert "type=raw,value=${{ needs.resolve-ref.outputs.build_tag }}" in workflow
    assert "scripts/promote_release_images.py" in workflow
    assert (
        "org.opencontainers.image.revision=${{ needs.resolve-ref.outputs.commit }}"
        in workflow
    )
    assert "LUMEN_GIT_SHA=${{ needs.resolve-ref.outputs.commit }}" in workflow
    assert "LUMEN_REVISION=${{ needs.resolve-ref.outputs.commit }}" in workflow
    assert "type=raw,value=sha-{{sha}}" not in workflow
    assert "type=raw,value=sha-${{ steps.meta-vars.outputs.short_sha }}" not in workflow
    assert "org.opencontainers.image.revision=${{ github.sha }}" not in workflow
    assert "LUMEN_GIT_SHA=${{ github.sha }}" not in workflow


def test_docker_release_never_preexpands_actions_values_in_shell() -> None:
    workflow = _load_workflow()
    jobs = workflow["jobs"]

    for job_name, job in jobs.items():
        steps = job.get("steps")
        assert isinstance(steps, list)
        for step in steps:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if isinstance(run, str):
                assert "${{" not in run, (
                    f"{job_name}/{step.get('name')} must receive Actions values "
                    "through env or action parameters"
                )

    version_step = _step(jobs["version-check"], "Assert tag matches VERSION")
    assert version_step["env"]["RELEASE_TAG"] == (
        "${{ needs.resolve-ref.outputs.release_tag }}"
    )
    manifest_step = _step(jobs["release"], "Build release manifest and notes")
    assert manifest_step["env"]["RELEASE_TAG"] == (
        "${{ needs.resolve-ref.outputs.release_tag }}"
    )
    assert manifest_step["env"]["COMMIT_SHA"] == (
        "${{ needs.resolve-ref.outputs.commit }}"
    )


def test_docker_release_validates_external_tag_before_git_substitution() -> None:
    workflow = _load_workflow()
    source = _step(workflow["jobs"]["resolve-ref"], "Resolve source commit")
    source_run = source["run"]
    assert isinstance(source_run, str)

    validation = 'if [[ ! "${release_tag}" =~ ${release_pattern} ]]; then'
    commit_resolution = "commit=\"$(git rev-parse --verify 'HEAD^{commit}')\""
    tag_resolution = 'tag_commit="$(git rev-parse --verify "${event_ref}^{commit}")"'
    assert source_run.index(validation) < source_run.index(commit_resolution)
    assert source_run.index(commit_resolution) < source_run.index(tag_resolution)
    assert "$(" not in source_run[: source_run.index(commit_resolution)]
    assert 'release_tag="${event_ref#refs/tags/}"' in source_run
    assert 'git rev-parse "${release_tag}' not in source_run


def test_docker_release_uses_run_scoped_build_tags_and_serialized_sha_aliases() -> None:
    workflow = _load_workflow()
    jobs = workflow["jobs"]
    resolve = jobs["resolve-ref"]
    source_run = _step(resolve, "Resolve source commit")["run"]

    assert set(resolve["outputs"]) >= {"commit", "short_sha", "build_tag", "sha_tag"}
    assert (
        'build_tag="sha-${commit}-run-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"'
        in source_run
    )
    assert 'sha_tag="sha-${short_sha}"' in source_run

    build_metadata = _step(jobs["build"], "Extract run-scoped Docker metadata")
    assert build_metadata["with"]["tags"].strip() == (
        "type=raw,value=${{ needs.resolve-ref.outputs.build_tag }}"
    )
    merge_step = _step(jobs["merge-web"], "Create run-scoped immutable web manifest")
    assert merge_step["env"]["BUILD_TAG"] == (
        "${{ needs.resolve-ref.outputs.build_tag }}"
    )
    assert '--tag "${build_ref}"' in merge_step["run"]
    assert 'sha_ref="${IMAGE}:sha-${SHORT_SHA}"' not in merge_step["run"]

    promote = jobs["promote"]
    assert promote["concurrency"] == {
        "group": (
            "${{ github.workflow }}-sha-${{ needs.resolve-ref.outputs.short_sha }}"
        ),
        "cancel-in-progress": "false",
    }
    sha_step = _step(promote, "Publish serialized compatibility SHA aliases")
    assert sha_step["env"]["SHA_TAG"] == "${{ needs.resolve-ref.outputs.sha_tag }}"
    assert '--tag "${image}:${SHA_TAG}"' in sha_step["run"]
    assert "compatibility SHA alias verification failed" in sha_step["run"]


def test_docker_release_promotes_aliases_only_after_all_signed_builds() -> None:
    workflow = _load_workflow()
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)

    build_job_names = ("build", "build-web", "merge-web")
    formal_alias_markers = ("value=latest", "value=main", "type=semver")
    for job_name in build_job_names:
        job = jobs[job_name]
        encoded = json.dumps(job, sort_keys=True)
        for marker in formal_alias_markers:
            assert marker not in encoded, (
                f"{job_name} must not publish mutable/formal alias marker {marker!r}"
            )
        assert "cosign sign --yes" in encoded
        assert "@${DIGEST}" in encoded
        permissions = job["permissions"]
        assert permissions["packages"] == "write"
        assert permissions["id-token"] == "write"

    promote = jobs["promote"]
    assert set(promote["needs"]) == {"resolve-ref", "build", "merge-web"}
    assert promote["permissions"]["packages"] == "write"
    assert set(promote["outputs"]) == {
        "api_digest",
        "worker_digest",
        "tgbot_digest",
        "web_digest",
        "is_prerelease",
    }
    promote_encoded = json.dumps(promote, sort_keys=True)
    assert "scripts/promote_release_images.py" in promote_encoded
    assert "Publish exact release tags or main alias" in promote_encoded
    assert "--github-repository" in promote_encoded
    assert "--github-output" in promote_encoded
    assert "docker/metadata-action@" not in promote_encoded
    publication = _step(promote, "Publish exact release tags or main alias")
    assert "imagetools create" not in publication["run"]
    assert "--metadata-file" in json.dumps(jobs["merge-web"], sort_keys=True)

    release = jobs["release"]
    assert set(release["needs"]) == {"resolve-ref", "promote"}
    release_encoded = json.dumps(release, sort_keys=True)
    assert "softprops/action-gh-release@" in release_encoded
    assert "needs.promote.outputs.is_prerelease" in release_encoded


def test_docker_release_serializes_sha_and_stable_shared_aliases() -> None:
    workflow = _load_workflow()
    concurrency = workflow["concurrency"]

    group = concurrency["group"]
    assert "github.event_name" in group
    assert "github.ref" in group
    assert "stable-publication" not in group
    assert concurrency["cancel-in-progress"] == "false"
    assert workflow["permissions"]["contents"] == "read"

    shared = workflow["jobs"]["promote-shared"]
    assert shared["concurrency"] == {
        "group": "${{ github.workflow }}-stable-mutable-aliases",
        "cancel-in-progress": "false",
    }
    promote = workflow["jobs"]["promote"]
    assert promote["concurrency"] == {
        "group": (
            "${{ github.workflow }}-sha-${{ needs.resolve-ref.outputs.short_sha }}"
        ),
        "cancel-in-progress": "false",
    }
    for job_name, job in workflow["jobs"].items():
        if job_name not in {"promote", "promote-shared"}:
            assert "concurrency" not in job


def test_docker_release_main_and_release_promotion_are_explicit() -> None:
    workflow = _load_workflow()
    jobs = workflow["jobs"]
    promote = jobs["promote"]
    publication = _step(promote, "Publish exact release tags or main alias")
    publication_run = publication["run"]
    assert isinstance(publication_run, str)

    assert "if" not in promote
    assert (
        publication["if"] == "needs.resolve-ref.outputs.is_release == 'true' || "
        "needs.resolve-ref.outputs.is_main_push == 'true'"
    )
    assert "mode=main" in publication_run
    assert "mode=release" in publication_run
    assert "phase=mutable" in publication_run
    assert "phase=exact" in publication_run
    assert '--phase "${phase}"' in publication_run
    assert 'release_args=(--release-tag "${RELEASE_TAG}")' in publication_run
    assert "GH_TOKEN" in publication["env"]
    assert promote["permissions"] == {"contents": "read", "packages": "write"}
    assert jobs["release"]["if"] == "needs.resolve-ref.outputs.is_release == 'true'"
    assert jobs["release"]["permissions"] == {
        "contents": "write",
        "packages": "read",
    }


def test_release_source_of_truth_precedes_stable_shared_aliases() -> None:
    workflow = _load_workflow()
    jobs = workflow["jobs"]
    release = jobs["release"]
    manifest_step = _step(release, "Build release manifest and notes")
    manifest_run = manifest_step["run"]
    assert isinstance(manifest_run, str)

    assert set(release["needs"]) == {"resolve-ref", "promote"}
    manifest_env = manifest_step["env"]
    assert manifest_env["API_DIGEST"] == "${{ needs.promote.outputs.api_digest }}"
    assert manifest_env["WORKER_DIGEST"] == (
        "${{ needs.promote.outputs.worker_digest }}"
    )
    assert manifest_env["TGBOT_DIGEST"] == ("${{ needs.promote.outputs.tgbot_digest }}")
    assert manifest_env["WEB_DIGEST"] == "${{ needs.promote.outputs.web_digest }}"
    assert '--image-digest "api=${API_DIGEST}"' in manifest_run
    assert '--image-digest "worker=${WORKER_DIGEST}"' in manifest_run
    assert '--image-digest "tgbot=${TGBOT_DIGEST}"' in manifest_run
    assert '--image-digest "web=${WEB_DIGEST}"' in manifest_run
    assert "--resolve-images" not in manifest_run
    assert (
        release["steps"][-1]["with"]["prerelease"]
        == "${{ needs.promote.outputs.is_prerelease }}"
    )

    shared = jobs["promote-shared"]
    assert set(shared["needs"]) == {"resolve-ref", "promote", "release"}
    assert (
        shared["if"] == "needs.resolve-ref.outputs.is_release == 'true' && "
        "needs.promote.outputs.is_prerelease == 'false'"
    )
    assert shared["permissions"] == {"contents": "read", "packages": "write"}
    shared_step = _step(shared, "Publish stable shared aliases")
    shared_run = shared_step["run"]
    assert isinstance(shared_run, str)
    assert "--phase mutable" in shared_run
    assert "--release-tag" in shared_run
    assert "always()" not in json.dumps(shared, sort_keys=True)
