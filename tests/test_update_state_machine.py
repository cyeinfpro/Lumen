from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
JOURNAL = ROOT / "scripts" / "update" / "journal.sh"
CONTRACT = ROOT / "scripts" / "update" / "phase_contract.sh"
COMMON = ROOT / "scripts" / "update" / "common.sh"
RUNNER = ROOT / "scripts" / "update" / "runner.sh"
RECOVERY_STATE = ROOT / "scripts" / "update" / "recovery" / "state.sh"
CHECK_PHASE = ROOT / "scripts" / "update" / "release" / "check.sh"
FETCH_PHASE = ROOT / "scripts" / "update" / "release" / "fetch.sh"
MANIFEST_PHASE = ROOT / "scripts" / "update" / "release" / "manifest.sh"
DIGEST_PHASE = ROOT / "scripts" / "update" / "release" / "digest.sh"
ACTIVATE_PHASE = ROOT / "scripts" / "update" / "release" / "activate.sh"
SWITCH_PHASE = ROOT / "scripts" / "update" / "services" / "switch.sh"
RESTART_PHASE = ROOT / "scripts" / "update" / "services" / "restart.sh"
SOURCE_COMMIT = "a" * 40
SOURCE_PROOF = "test-proof"
IMAGE_IDS = {
    "api": "sha256:" + ("b" * 64),
    "worker": "sha256:" + ("c" * 64),
    "web": "sha256:" + ("d" * 64),
    "tgbot": "sha256:" + ("e" * 64),
}


def _fake_image_inspect_cases(
    *,
    tag: str,
    build: bool = False,
    include_tgbot: bool = False,
    revision_overrides: dict[str, str] | None = None,
) -> str:
    services = ["api", "worker", "web"]
    if include_tgbot:
        services.append("tgbot")
    revision_overrides = revision_overrides or {}
    cases = []
    for index, service in enumerate(services, start=1):
        repository = f"ghcr.io/cyeinfpro/lumen-{service}"
        revision = revision_overrides.get(service, SOURCE_COMMIT)
        labels = {} if build else {"org.opencontainers.image.revision": revision}
        repo_digests = [] if build else [f"{repository}@sha256:{str(index) * 64}"]
        payload = json.dumps(
            [
                {
                    "Config": {"Labels": labels},
                    "Id": IMAGE_IDS[service],
                    "RepoDigests": repo_digests,
                }
            ],
            separators=(",", ":"),
        )
        cases.append(
            f"{shlex.quote(f'{repository}:{tag}')}) "
            f"printf '%s\\n' {shlex.quote(payload)}; return 0 ;;"
        )
    return "\n".join(cases)


def _run(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _prepare_target_artifacts(
    release: Path,
    *,
    tag: str,
    manifest: bool = False,
) -> None:
    release.mkdir(parents=True, exist_ok=True)
    (release / "docker-compose.yml").write_text(
        "services:\n  api:\n    image: example.invalid/lumen-api\n",
        encoding="utf-8",
    )
    (release / ".release-source-proof").write_text(
        f"{SOURCE_COMMIT}\n{SOURCE_PROOF}\n",
        encoding="utf-8",
    )
    (release / ".image-tag").write_text(f"{tag}\n", encoding="utf-8")
    if manifest:
        (release / "release-manifest.json").write_text(
            '{"schema_version":1,"version":"v1.2.3"}\n',
            encoding="utf-8",
        )


def _request_contract_script(
    tag: str = "v1.2.3",
    *,
    idempotency_key: str = "",
) -> str:
    return f"""
        LUMEN_UPDATE_CHANNEL=stable
        LUMEN_UPDATE_FORCE_REDEPLOY=0
        LUMEN_UPDATE_IDEMPOTENCY_KEY={shlex.quote(idempotency_key)}
        TARGET_TAG={shlex.quote(tag)}
        lumen_update_journal_bind_request
    """


def _target_contract_script(
    release: Path,
    *,
    tag: str = "v1.2.3",
    release_tag: str = "",
    manifest: bool = False,
    rolling_digest: str = "",
) -> str:
    manifest_path = release / "release-manifest.json"
    manifest_file = shlex.quote(str(manifest_path)) if manifest else '""'
    manifest_sha = (
        f"$(lumen_update_file_sha256 {shlex.quote(str(manifest_path))})"
        if manifest
        else '""'
    )
    return f"""
        TARGET_TAG={shlex.quote(tag)}
        TARGET_RELEASE_TAG={shlex.quote(release_tag)}
        NEW_ID={shlex.quote(release.name)}
        NEW_RELEASE={shlex.quote(str(release))}
        RELEASE_SOURCE_COMMIT={SOURCE_COMMIT}
        RELEASE_SOURCE_COMMIT_PROOF={SOURCE_PROOF}
        RELEASE_SOURCE_PROOF_FILE={shlex.quote(str(release / ".release-source-proof"))}
        RELEASE_SOURCE_PROOF_SHA256="$(
            lumen_update_file_sha256 "$RELEASE_SOURCE_PROOF_FILE"
        )"
        RELEASE_SOURCE_TREE_SHA256="$(
            lumen_update_release_source_sha256 "$NEW_RELEASE"
        )"
        RELEASE_SOURCE_MANIFEST_CACHE=""
        RELEASE_MANIFEST_FILE={manifest_file}
        RELEASE_MANIFEST_SHA256={manifest_sha}
        RELEASE_IMAGE_TAG_FILE={shlex.quote(str(release / ".image-tag"))}
        RELEASE_IMAGE_TAG_SHA256="$(
            lumen_update_file_sha256 "$RELEASE_IMAGE_TAG_FILE"
        )"
        TARGET_ROLLING_DIGEST={shlex.quote(rolling_digest)}
        lumen_update_journal_bind_target
    """


def test_update_journal_records_phase_failure_and_explicit_resume(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    journal = shared / "journal.json"
    first = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-original
        LUMEN_UPDATE_RESUME=0
        CURRENT_ID=release-old
        TARGET_TAG=v9.9.9
        lumen_update_journal_init
        lumen_update_journal_phase_start lock
        lumen_update_journal_phase_done lock
        lumen_update_journal_phase_start self_update_scripts
        lumen_update_journal_failed self_update_scripts 41
        """
    )
    assert first.returncode == 0, first.stderr + first.stdout

    failed = json.loads(journal.read_text(encoding="utf-8"))
    assert failed["schema"] == 2
    assert failed["operation_id"] == "update-original"
    assert failed["status"] == "failed"
    assert failed["completed_phases"] == ["lock"]
    assert failed["last_error"]["phase"] == "self_update_scripts"
    assert failed["last_error"]["return_code"] == 41
    assert failed["context"]["TARGET_TAG"] == "v9.9.9"

    resumed = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-new
        LUMEN_UPDATE_RESUME=1
        lumen_update_journal_init
        printf 'operation=%s resumed=%s target=%s\\n' \
            "$OPERATION_ID" "$LUMEN_UPDATE_JOURNAL_RESUMED" "$TARGET_TAG"
        """
    )
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "operation=update-original resumed=1 target=v9.9.9" in resumed.stdout
    resumed_payload = json.loads(journal.read_text(encoding="utf-8"))
    assert resumed_payload["status"] == "running"
    assert resumed_payload["resume_count"] == 1


def test_update_journal_records_rollback_terminal_state(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-rollback
        lumen_update_journal_init
        lumen_update_journal_phase_start lock
        lumen_update_journal_failed lock 1
        lumen_update_journal_status rolled_back
        """
    )
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["status"] == "rolled_back"
    assert payload["current_phase"] is None


def test_schema_v1_journal_is_never_auto_resumed(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "schema": 1,
                "operation_id": "legacy-update",
                "status": "failed",
                "completed_phases": ["switch"],
                "context": {},
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-new
        LUMEN_UPDATE_RESUME=1
        lumen_update_journal_init
        """
    )

    assert result.returncode != 0
    assert "schema 1" in result.stderr
    assert json.loads(journal.read_text(encoding="utf-8"))["schema"] == 1


def test_active_journal_requires_explicit_resume(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    first = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-active
        lumen_update_journal_init
        """
    )
    assert first.returncode == 0, first.stderr + first.stdout

    second = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-overwrite
        LUMEN_UPDATE_RESUME=0
        lumen_update_journal_init
        """
    )

    assert second.returncode != 0
    assert "active update journal already exists" in second.stderr
    assert json.loads(journal.read_text(encoding="utf-8"))["operation_id"] == (
        "update-active"
    )


def test_request_contract_is_set_once_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal.json"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-request-contract
        lumen_update_journal_init
        {_request_contract_script(idempotency_key="request-secret")}
        lumen_update_journal_bind_request
        LUMEN_UPDATE_IDEMPOTENCY_KEY=changed-secret
        rc=0
        lumen_update_journal_bind_request || rc=$?
        printf 'conflict_rc=%s\\n' "$rc"
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "conflict_rc=1" in result.stdout
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["request"]["channel"] == "stable"
    assert payload["request"]["resolved_tag"] == "v1.2.3"
    assert payload["request"]["force_redeploy"] is False
    assert len(payload["request"]["idempotency_key_sha256"]) == 64
    assert "request-secret" not in journal.read_text(encoding="utf-8")
    assert "idempotency_key_sha256" in result.stderr


def test_request_idempotency_key_is_not_exposed_in_python_argv(
    tmp_path: Path,
) -> None:
    real_python = shutil.which("python3")
    assert real_python is not None
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    argv_log = tmp_path / "python-argv.log"
    wrapper = wrapper_dir / "python3"
    wrapper.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$ARGV_LOG"\n'
        f'exec {shlex.quote(real_python)} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    journal = tmp_path / "journal.json"
    secret = "request-secret-never-in-argv"

    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-request-argv
        lumen_update_journal_init
        {_request_contract_script(idempotency_key=secret)}
        """,
        env={
            **os.environ,
            "ARGV_LOG": str(argv_log),
            "PATH": f"{wrapper_dir}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert secret not in argv_log.read_text(encoding="utf-8")


def test_durable_copy_replaces_regular_file_and_rejects_destination_symlink(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_text("new-bytes\n", encoding="utf-8")
    target.write_text("old-bytes\n", encoding="utf-8")
    old_inode = target.stat().st_ino

    replaced = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        lumen_update_copy_file_durable \
            {shlex.quote(str(source))} {shlex.quote(str(target))}
        """
    )

    assert replaced.returncode == 0, replaced.stderr + replaced.stdout
    assert target.read_text(encoding="utf-8") == "new-bytes\n"
    assert target.stat().st_ino != old_inode
    assert target.stat().st_mode & 0o777 == 0o600

    victim = tmp_path / "victim"
    destination_link = tmp_path / "destination-link"
    victim.write_text("trusted\n", encoding="utf-8")
    destination_link.symlink_to(victim)
    rejected = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        lumen_update_copy_file_durable \
            {shlex.quote(str(source))} {shlex.quote(str(destination_link))}
        """
    )

    assert rejected.returncode != 0
    assert "destination symlink" in rejected.stderr
    assert destination_link.is_symlink()
    assert victim.read_text(encoding="utf-8") == "trusted\n"


def test_target_contract_replay_is_idempotent_and_conflict_is_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    release = root / "releases" / "new"
    shared.mkdir(parents=True)
    _prepare_target_artifacts(release, tag="v1.2.3", manifest=True)
    journal = shared / "journal.json"

    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-target-contract
        lumen_update_journal_init
        {
            _target_contract_script(
                release,
                release_tag="v1.2.3",
                manifest=True,
            )
        }
        lumen_update_journal_bind_target
        TARGET_TAG=v1.2.4
        rc=0
        lumen_update_journal_bind_target || rc=$?
        printf 'conflict_rc=%s\\n' "$rc"
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "conflict_rc=1" in result.stdout
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["target"]["effective_tag"] == "v1.2.3"
    assert payload["target"]["release_tag"] == "v1.2.3"
    assert payload["target"]["release_path"] == str(release)
    assert payload["target"]["release_id"] == "new"
    assert payload["target"]["source_commit"] == SOURCE_COMMIT
    assert len(payload["target"]["manifest_sha256"]) == 64
    assert payload["target"]["rolling_digest"] is None
    assert "effective_tag" in result.stderr


def test_target_contract_allows_only_monotonic_proof_completion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    release = root / "releases" / "new"
    shared.mkdir(parents=True)
    _prepare_target_artifacts(release, tag="main")
    journal = shared / "journal.json"
    manifest = release / "release-manifest.json"
    rolling_digest = "sha256:" + ("b" * 64)

    initial = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-target-completion
        lumen_update_journal_init
        {_target_contract_script(release, tag="main")}
        """
    )
    assert initial.returncode == 0, initial.stderr + initial.stdout

    manifest.write_text('{"rolling":"proof"}\n', encoding="utf-8")
    completed = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        LUMEN_UPDATE_RESUME=1
        OPERATION_ID=ignored
        lumen_update_journal_init
        TARGET_TAG=main
        TARGET_RELEASE_TAG=""
        NEW_ID=new
        NEW_RELEASE={shlex.quote(str(release))}
        RELEASE_SOURCE_COMMIT={SOURCE_COMMIT}
        RELEASE_SOURCE_COMMIT_PROOF={SOURCE_PROOF}
        RELEASE_SOURCE_PROOF_FILE={shlex.quote(str(release / ".release-source-proof"))}
        RELEASE_SOURCE_PROOF_SHA256="$(
            lumen_update_file_sha256 "$RELEASE_SOURCE_PROOF_FILE"
        )"
        RELEASE_SOURCE_TREE_SHA256="$(
            lumen_update_release_source_sha256 "$NEW_RELEASE"
        )"
        RELEASE_SOURCE_MANIFEST_CACHE=""
        RELEASE_MANIFEST_FILE={shlex.quote(str(manifest))}
        RELEASE_MANIFEST_SHA256="$(
            lumen_update_file_sha256 "$RELEASE_MANIFEST_FILE"
        )"
        RELEASE_IMAGE_TAG_FILE={shlex.quote(str(release / ".image-tag"))}
        RELEASE_IMAGE_TAG_SHA256="$(
            lumen_update_file_sha256 "$RELEASE_IMAGE_TAG_FILE"
        )"
        TARGET_ROLLING_DIGEST={rolling_digest}
        lumen_update_journal_bind_target
        lumen_update_journal_bind_target
        """
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    target = json.loads(journal.read_text(encoding="utf-8"))["target"]
    assert target["manifest_sha256"]
    assert target["manifest_path"] == str(manifest)
    assert target["rolling_digest"] == rolling_digest


@pytest.mark.parametrize("fake_digest", ["main", SOURCE_COMMIT])
def test_rolling_target_rejects_tag_or_source_commit_as_digest(
    tmp_path: Path,
    fake_digest: str,
) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    release = root / "releases" / "new"
    shared.mkdir(parents=True)
    _prepare_target_artifacts(release, tag="main")
    journal = shared / "journal.json"

    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-fake-rolling-digest
        lumen_update_journal_init
        {
            _target_contract_script(
                release,
                tag="main",
                rolling_digest=fake_digest,
            )
        }
        """
    )

    assert result.returncode != 0
    assert "rolling_digest is invalid" in result.stderr
    assert json.loads(journal.read_text(encoding="utf-8"))["target"] is None


def test_fixed_release_target_requires_manifest_sha256(tmp_path: Path) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    release = root / "releases" / "new"
    shared.mkdir(parents=True)
    _prepare_target_artifacts(release, tag="v1.2.3")
    journal = shared / "journal.json"

    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-fixed-no-manifest
        lumen_update_journal_init
        {
            _target_contract_script(
                release,
                release_tag="v1.2.3",
            )
        }
        """
    )

    assert result.returncode != 0
    assert "manifest SHA-256 is missing" in result.stderr
    assert json.loads(journal.read_text(encoding="utf-8"))["target"] is None


def test_committed_marker_does_not_flip_memory_when_persist_fails(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal.json"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-commit-failure
        lumen_update_journal_init
        lumen_update_journal_exec() {{ return 1; }}
        UPDATE_STATE_COMMITTED=0
        rc=0
        lumen_update_journal_mark_committed || rc=$?
        printf 'rc=%s committed=%s\\n' "$rc" "$UPDATE_STATE_COMMITTED"
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "rc=1 committed=0" in result.stdout
    assert (
        json.loads(journal.read_text(encoding="utf-8"))["state"]["committed"] is False
    )


def test_commit_persist_failure_enters_manual_recovery_without_rollback(
    tmp_path: Path,
) -> None:
    status_file = tmp_path / "status"
    rollback_file = tmp_path / "rollback"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(RECOVERY_STATE))}
        UPDATE_STATE_COMMITTED=0
        UPDATE_STATE_COMMIT_UNKNOWN=0
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        UPDATE_ERROR_HANDLED=0
        UPDATE_RESTORE_POINT_TIMESTAMP=""
        UPDATE_MIGRATION_STARTED=0
        ROLLBACK_DONE=0
        lumen_update_journal_mark_committed() {{ return 1; }}
        log_error() {{ :; }}
        log_warn() {{ :; }}
        discard_release_source_manifest_cache() {{ :; }}
        lumen_step_finalize_failure() {{ :; }}
        discard_update_state_snapshot() {{ :; }}
        lumen_update_journal_status() {{ printf '%s\\n' "$1" > {shlex.quote(str(status_file))}; }}
        restore_uncommitted_update_state() {{
            : > {shlex.quote(str(rollback_file))}
            return 0
        }}
        rc=0
        mark_update_committed || rc=$?
        test "$rc" -eq 1
        test "$UPDATE_STATE_COMMITTED" -eq 0
        test "$UPDATE_STATE_COMMIT_UNKNOWN" -eq 1
        on_err 1
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert status_file.read_text(encoding="utf-8").strip() == "manual_required"
    assert not rollback_file.exists()


def test_incomplete_v2_journal_cannot_skip_snapshot_bearing_phase(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "schema": 2,
                "operation_id": "malformed-v2",
                "status": "failed",
                "completed_phases": ["switch"],
                "current_phase": None,
                "current_subphase": None,
                "snapshot": {
                    "ready": False,
                    "root": str(tmp_path),
                    "shared_env": str(tmp_path / ".env"),
                    "shared_env_sha256": None,
                    "env_snapshot": None,
                    "host_artifact_snapshot": None,
                    "current": {"known": False, "present": False, "target": None},
                    "previous": {"known": False, "present": False, "target": None},
                },
                "state": {
                    "committed": False,
                    "runtime": {
                        "current": {
                            "kind": "unknown",
                            "present": False,
                            "target": None,
                        },
                        "previous": {
                            "kind": "unknown",
                            "present": False,
                            "target": None,
                        },
                        "shared_env_sha256": None,
                        "migration_head": None,
                    },
                },
                "invariants": {},
                "context": {},
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        SHARED_ENV={shlex.quote(str(tmp_path / ".env"))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=ignored
        LUMEN_UPDATE_RESUME=1
        lumen_update_journal_init
        lumen_update_journal_validate_resume
        """
    )

    assert result.returncode != 0
    assert "completed_phases" in result.stderr


def test_check_completion_rejects_missing_request_contract_without_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    old_release = root / "releases" / "old"
    shared.mkdir(parents=True)
    old_release.mkdir(parents=True)
    shared_env = shared / ".env"
    env_snapshot = shared / ".env.update.snapshot"
    journal = shared / "journal.json"
    shared_env.write_text("LUMEN_IMAGE_TAG=v1.0.0\n", encoding="utf-8")
    env_snapshot.write_bytes(shared_env.read_bytes())
    (root / "current").symlink_to("releases/old")

    prepared = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-missing-request
        lumen_update_journal_init
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        UPDATE_ENV_SNAPSHOT={shlex.quote(str(env_snapshot))}
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_ORIGINAL_CURRENT_PRESENT=1
        UPDATE_ORIGINAL_CURRENT_TARGET=releases/old
        UPDATE_ORIGINAL_PREVIOUS_PRESENT=0
        UPDATE_ORIGINAL_PREVIOUS_TARGET=""
        UPDATE_SNAPSHOT_ENV_SHA256="$(lumen_update_file_sha256 "$SHARED_ENV")"
        lumen_update_journal_snapshot_state
        for phase in lock self_update_scripts; do
            lumen_update_journal_phase_start "$phase"
            lumen_update_journal_phase_done "$phase"
        done
        lumen_update_journal_phase_start check
        rc=0
        lumen_update_journal_phase_done check || rc=$?
        test "$rc" -ne 0
        """
    )
    assert prepared.returncode == 0, prepared.stderr + prepared.stdout
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["completed_phases"] == ["lock", "self_update_scripts"]
    assert payload["current_phase"] == "check"
    assert payload["request"] is None
    assert os.readlink(root / "current") == "releases/old"


def test_fetch_completion_rejects_missing_target_contract_without_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    old_release = root / "releases" / "old"
    shared.mkdir(parents=True)
    old_release.mkdir(parents=True)
    shared_env = shared / ".env"
    env_snapshot = shared / ".env.update.snapshot"
    journal = shared / "journal.json"
    shared_env.write_text("LUMEN_IMAGE_TAG=v1.0.0\n", encoding="utf-8")
    env_snapshot.write_bytes(shared_env.read_bytes())
    (root / "current").symlink_to("releases/old")

    prepared = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-missing-target
        lumen_update_journal_init
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        UPDATE_ENV_SNAPSHOT={shlex.quote(str(env_snapshot))}
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_ORIGINAL_CURRENT_PRESENT=1
        UPDATE_ORIGINAL_CURRENT_TARGET=releases/old
        UPDATE_ORIGINAL_PREVIOUS_PRESENT=0
        UPDATE_ORIGINAL_PREVIOUS_TARGET=""
        UPDATE_SNAPSHOT_ENV_SHA256="$(lumen_update_file_sha256 "$SHARED_ENV")"
        lumen_update_journal_snapshot_state
        {_request_contract_script()}
        for phase in lock self_update_scripts check preflight backup_preflight; do
            lumen_update_journal_phase_start "$phase"
            lumen_update_journal_phase_done "$phase"
        done
        lumen_update_journal_phase_start fetch_release
        rc=0
        lumen_update_journal_phase_done fetch_release || rc=$?
        test "$rc" -ne 0
        """
    )
    assert prepared.returncode == 0, prepared.stderr + prepared.stdout
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["completed_phases"][-1] == "backup_preflight"
    assert payload["current_phase"] == "fetch_release"
    assert payload["target"] is None
    assert os.readlink(root / "current") == "releases/old"


def test_resume_replays_bound_target_before_completing_fetch_phase(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    old_release = root / "releases" / "old"
    new_release = root / "releases" / "new"
    shared.mkdir(parents=True)
    old_release.mkdir(parents=True)
    _prepare_target_artifacts(new_release, tag="v1.2.3")
    shared_env = shared / ".env"
    env_snapshot = shared / ".env.update.snapshot"
    journal = shared / "journal.json"
    shared_env.write_text("LUMEN_IMAGE_TAG=v1.0.0\n", encoding="utf-8")
    env_snapshot.write_bytes(shared_env.read_bytes())
    (root / "current").symlink_to("releases/old")

    prepared = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-fetch-replay
        lumen_update_journal_init
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        UPDATE_ENV_SNAPSHOT={shlex.quote(str(env_snapshot))}
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_ORIGINAL_CURRENT_PRESENT=1
        UPDATE_ORIGINAL_CURRENT_TARGET=releases/old
        UPDATE_ORIGINAL_PREVIOUS_PRESENT=0
        UPDATE_ORIGINAL_PREVIOUS_TARGET=""
        UPDATE_SNAPSHOT_ENV_SHA256="$(lumen_update_file_sha256 "$SHARED_ENV")"
        lumen_update_journal_snapshot_state
        {_request_contract_script()}
        {_target_contract_script(new_release)}
        for phase in lock self_update_scripts check preflight backup_preflight; do
            lumen_update_journal_phase_start "$phase"
            lumen_update_journal_phase_done "$phase"
        done
        lumen_update_journal_phase_start fetch_release
        lumen_update_journal_failed fetch_release 97
        """
    )
    assert prepared.returncode == 0, prepared.stderr + prepared.stdout

    resumed = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        . {shlex.quote(str(COMMON))}
        . {shlex.quote(str(FETCH_PHASE))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=ignored
        LUMEN_UPDATE_RESUME=1
        lumen_emit_step() {{ :; }}
        lumen_emit_info() {{ :; }}
        log_info() {{ :; }}
        log_error() {{ printf '%s\\n' "$*" >&2; }}
        lumen_update_journal_init
        lumen_update_journal_validate_resume
        update_phase_fetch_release
        """
    )

    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["completed_phases"][-1] == "fetch_release"
    assert payload["target"]["release_path"] == str(new_release)


@pytest.mark.parametrize(
    ("artifact", "expected_error"),
    [
        ("manifest", "release manifest proof hash mismatch"),
        ("image_tag", ".image-tag proof hash mismatch"),
        ("source_tree", "source tree hash mismatch"),
        ("source_proof", "source proof hash mismatch"),
    ],
)
def test_resume_rejects_target_proof_drift_without_moving_links(
    tmp_path: Path,
    artifact: str,
    expected_error: str,
) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    old_release = root / "releases" / "old"
    new_release = root / "releases" / "new"
    shared.mkdir(parents=True)
    old_release.mkdir(parents=True)
    _prepare_target_artifacts(new_release, tag="v1.2.3", manifest=True)
    shared_env = shared / ".env"
    env_snapshot = shared / ".env.update.snapshot"
    journal = shared / "journal.json"
    shared_env.write_text("LUMEN_IMAGE_TAG=v1.0.0\n", encoding="utf-8")
    env_snapshot.write_bytes(shared_env.read_bytes())
    (root / "current").symlink_to("releases/old")

    prepared = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-proof-drift
        lumen_update_journal_init
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        UPDATE_ENV_SNAPSHOT={shlex.quote(str(env_snapshot))}
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_ORIGINAL_CURRENT_PRESENT=1
        UPDATE_ORIGINAL_CURRENT_TARGET=releases/old
        UPDATE_ORIGINAL_PREVIOUS_PRESENT=0
        UPDATE_ORIGINAL_PREVIOUS_TARGET=""
        UPDATE_SNAPSHOT_ENV_SHA256="$(lumen_update_file_sha256 "$SHARED_ENV")"
        lumen_update_journal_snapshot_state
        {_request_contract_script()}
        {
            _target_contract_script(
                new_release,
                release_tag="v1.2.3",
                manifest=True,
            )
        }
        for phase in lock self_update_scripts check preflight backup_preflight \
                fetch_release set_image_tag; do
            lumen_update_journal_phase_start "$phase"
            lumen_update_journal_phase_done "$phase"
        done
        lumen_update_journal_failed pull_images 97
        """
    )
    assert prepared.returncode == 0, prepared.stderr + prepared.stdout

    if artifact == "manifest":
        (new_release / "release-manifest.json").write_text(
            '{"changed":true}\n',
            encoding="utf-8",
        )
    elif artifact == "image_tag":
        (new_release / ".image-tag").write_text("main\n", encoding="utf-8")
    elif artifact == "source_tree":
        (new_release / "docker-compose.yml").write_text(
            "services:\n  api:\n    image: changed.invalid/api\n",
            encoding="utf-8",
        )
    else:
        (new_release / ".release-source-proof").write_text(
            f"{'b' * 40}\n{SOURCE_PROOF}\n",
            encoding="utf-8",
        )

    rejected = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=ignored
        LUMEN_UPDATE_RESUME=1
        lumen_update_journal_init
        lumen_update_journal_validate_resume
        """
    )

    assert rejected.returncode != 0
    assert expected_error in rejected.stderr
    assert os.readlink(root / "current") == "releases/old"


def test_killed_switch_resume_restores_snapshot_and_commit_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    releases = root / "releases"
    old_release = releases / "old"
    new_release = releases / "new"
    shared.mkdir(parents=True)
    old_release.mkdir(parents=True)
    new_release.mkdir(parents=True)
    shared_env = shared / ".env"
    env_snapshot = shared / ".env.update.snapshot"
    journal = shared / "journal.json"
    shared_env.write_text("LUMEN_IMAGE_TAG=v1.0.0\n", encoding="utf-8")
    env_snapshot.write_bytes(shared_env.read_bytes())
    (root / "current").symlink_to("releases/old")
    _prepare_target_artifacts(new_release, tag="v1.0.1")

    killed = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-killed-switch
        lumen_update_journal_init
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        UPDATE_ENV_SNAPSHOT={shlex.quote(str(env_snapshot))}
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_ORIGINAL_CURRENT_PRESENT=1
        UPDATE_ORIGINAL_CURRENT_TARGET=releases/old
        UPDATE_ORIGINAL_PREVIOUS_PRESENT=0
        UPDATE_ORIGINAL_PREVIOUS_TARGET=""
        UPDATE_SNAPSHOT_ENV_SHA256="$(lumen_update_file_sha256 "$SHARED_ENV")"
        lumen_update_journal_snapshot_state
        {_request_contract_script("v1.0.1")}
        {_target_contract_script(new_release, tag="v1.0.1")}
        for phase in lock self_update_scripts check preflight backup_preflight \
                fetch_release set_image_tag pull_images check_storage start_infra \
                migrate_db; do
            lumen_update_journal_phase_start "$phase"
            lumen_update_journal_phase_done "$phase"
        done
        lumen_update_journal_phase_start switch
        rm -f "$ROOT/current"
        ln -s releases/new "$ROOT/current"
        ln -s releases/old "$ROOT/previous"
        kill -9 $$
        """
    )
    assert killed.returncode != 0

    resumed = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=ignored-on-resume
        LUMEN_UPDATE_RESUME=1
        UPDATE_RUNTIME_MIGRATION_HEAD=""
        lumen_update_journal_init
        lumen_update_journal_validate_resume
        printf 'snapshot=%s committed=%s current=%s previous=%s\\n' \
            "$UPDATE_SNAPSHOT_LINKS_KNOWN" \
            "$UPDATE_STATE_COMMITTED" \
            "$UPDATE_ORIGINAL_CURRENT_TARGET" \
            "$UPDATE_ORIGINAL_PREVIOUS_PRESENT"
        lumen_update_journal_mark_committed
        """
    )
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "snapshot=1 committed=0 current=releases/old previous=0" in resumed.stdout

    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["state"]["committed"] is True
    assert payload["snapshot"]["current"] == {
        "known": True,
        "present": True,
        "target": "releases/old",
    }

    restored = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=ignored-again
        LUMEN_UPDATE_RESUME=1
        UPDATE_RUNTIME_MIGRATION_HEAD=""
        lumen_update_journal_init
        lumen_update_journal_validate_resume
        test "$UPDATE_STATE_COMMITTED" -eq 1
        """
    )
    assert restored.returncode == 0, restored.stderr + restored.stdout


def test_resume_rejects_unexpected_link_without_changing_it(tmp_path: Path) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    releases = root / "releases"
    for release in ("old", "new", "intruder"):
        (releases / release).mkdir(parents=True, exist_ok=True)
    shared.mkdir(parents=True)
    shared_env = shared / ".env"
    env_snapshot = shared / ".env.update.snapshot"
    journal = shared / "journal.json"
    shared_env.write_text("LUMEN_IMAGE_TAG=v1.0.0\n", encoding="utf-8")
    env_snapshot.write_bytes(shared_env.read_bytes())
    (root / "current").symlink_to("releases/old")
    new_release = releases / "new"
    _prepare_target_artifacts(new_release, tag="v1.0.1")

    prepared = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-invariant
        lumen_update_journal_init
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        UPDATE_ENV_SNAPSHOT={shlex.quote(str(env_snapshot))}
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_ORIGINAL_CURRENT_PRESENT=1
        UPDATE_ORIGINAL_CURRENT_TARGET=releases/old
        UPDATE_ORIGINAL_PREVIOUS_PRESENT=0
        UPDATE_ORIGINAL_PREVIOUS_TARGET=""
        UPDATE_SNAPSHOT_ENV_SHA256="$(lumen_update_file_sha256 "$SHARED_ENV")"
        lumen_update_journal_snapshot_state
        {_request_contract_script("v1.0.1")}
        {_target_contract_script(new_release, tag="v1.0.1")}
        for phase in lock self_update_scripts check preflight backup_preflight \
                fetch_release set_image_tag pull_images check_storage start_infra \
                migrate_db; do
            lumen_update_journal_phase_start "$phase"
            lumen_update_journal_phase_done "$phase"
        done
        lumen_update_journal_phase_start switch
        lumen_update_journal_failed switch 137
        """
    )
    assert prepared.returncode == 0, prepared.stderr + prepared.stdout
    (root / "current").unlink()
    (root / "current").symlink_to("releases/intruder")

    first_rejected = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=ignored
        LUMEN_UPDATE_RESUME=1
        UPDATE_RUNTIME_MIGRATION_HEAD=""
        lumen_update_journal_init
        if lumen_update_journal_validate_resume; then
            exit 99
        fi
        lumen_update_journal_failed resume_validation 78
        exit 78
        """
    )

    assert first_rejected.returncode == 78
    assert "current link invariant mismatch" in first_rejected.stderr
    after_first_rejection = json.loads(journal.read_text(encoding="utf-8"))
    assert after_first_rejection["state"]["runtime"]["current"]["target"] == (
        "releases/old"
    )

    second_rejected = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=ignored-again
        LUMEN_UPDATE_RESUME=1
        UPDATE_RUNTIME_MIGRATION_HEAD=""
        lumen_update_journal_init
        lumen_update_journal_validate_resume
        """
    )

    assert second_rejected.returncode != 0
    assert "current link invariant mismatch" in second_rejected.stderr
    assert os.readlink(root / "current") == "releases/intruder"


def test_rolling_target_binds_image_ids_and_resume_ignores_tag_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    old_release = root / "releases" / "old"
    new_release = root / "releases" / "new"
    shared.mkdir(parents=True)
    old_release.mkdir(parents=True)
    _prepare_target_artifacts(new_release, tag="main")
    shared_env = shared / ".env"
    env_snapshot = shared / ".env.update.snapshot"
    journal = shared / "journal.json"
    shared_env.write_text("LUMEN_IMAGE_TAG=v1.0.0\n", encoding="utf-8")
    env_snapshot.write_bytes(shared_env.read_bytes())
    (root / "current").symlink_to("releases/old")
    tag_inspects = tmp_path / "tag-inspects.log"
    inspect_cases = _fake_image_inspect_cases(tag="main")

    prepared = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        . {shlex.quote(str(MANIFEST_PHASE))}
        . {shlex.quote(str(DIGEST_PHASE))}
        env_key_present() {{ grep -qE "^$2=.+" "$1"; }}
        emit_info() {{ :; }}
        lumen_compose_in() {{
            printf '%s\\n' \
                'ghcr.io/cyeinfpro/lumen-api:main' \
                'ghcr.io/cyeinfpro/lumen-worker:main' \
                'ghcr.io/cyeinfpro/lumen-web:main'
        }}
        lumen_docker() {{
            local ref="${{@: -1}}"
            if [ "$1" = image ] && [ "$2" = inspect ]; then
                if [ "${{3:-}}" = "--format" ]; then
                    printf '%s\\n' "$ref"
                    return 0
                fi
                printf '%s\\n' "$ref" >> {shlex.quote(str(tag_inspects))}
                case "$ref" in
                    {inspect_cases}
                    *) return 91 ;;
                esac
            fi
            return 92
        }}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        LUMEN_IMAGE_REGISTRY=ghcr.io/cyeinfpro
        LUMEN_UPDATE_BUILD=0
        TGBOT_IMAGE_READY=0
        OPERATION_ID=update-rolling-digest
        lumen_update_journal_init
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        UPDATE_ENV_SNAPSHOT={shlex.quote(str(env_snapshot))}
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_ORIGINAL_CURRENT_PRESENT=1
        UPDATE_ORIGINAL_CURRENT_TARGET=releases/old
        UPDATE_ORIGINAL_PREVIOUS_PRESENT=0
        UPDATE_ORIGINAL_PREVIOUS_TARGET=""
        UPDATE_SNAPSHOT_ENV_SHA256="$(lumen_update_file_sha256 "$SHARED_ENV")"
        lumen_update_journal_snapshot_state
        {_request_contract_script("main")}
        {_target_contract_script(new_release, tag="main")}
        for phase in lock self_update_scripts check preflight backup_preflight \
                fetch_release set_image_tag; do
            lumen_update_journal_phase_start "$phase"
            lumen_update_journal_phase_done "$phase"
        done
        lumen_update_journal_phase_start pull_images
        missing_rc=0
        lumen_update_journal_phase_done pull_images || missing_rc=$?
        test "$missing_rc" -ne 0
        lumen_update_bind_immutable_images
        lumen_update_journal_phase_done pull_images
        lumen_update_journal_failed check_storage 97
        """
    )
    assert prepared.returncode == 0, prepared.stderr + prepared.stdout
    target = json.loads(journal.read_text(encoding="utf-8"))["target"]
    assert target["rolling_digest"] == target["image_set_digest"]
    assert target["image_set_digest"] == f"sha256:{target['image_proof_sha256']}"
    assert len(tag_inspects.read_text(encoding="utf-8").splitlines()) == 3

    resumed = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        . {shlex.quote(str(MANIFEST_PHASE))}
        . {shlex.quote(str(DIGEST_PHASE))}
        lumen_docker() {{
            local ref="${{@: -1}}"
            if [ "$1" = image ] && [ "$2" = inspect ] \
                    && [ "${{3:-}}" = "--format" ] \
                    && [[ "$ref" == sha256:* ]]; then
                printf '%s\\n' "$ref"
                return 0
            fi
            printf '%s\\n' "$ref" >> {shlex.quote(str(tag_inspects))}
            return 93
        }}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=ignored
        LUMEN_UPDATE_RESUME=1
        lumen_update_journal_init
        lumen_update_journal_validate_resume
        """
    )

    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert len(tag_inspects.read_text(encoding="utf-8").splitlines()) == 3


def test_image_binding_rejects_revision_mismatch_from_single_inspect(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    shared_env = tmp_path / ".env"
    shared_env.write_text("LUMEN_IMAGE_TAG=main\n", encoding="utf-8")
    inspect_cases = _fake_image_inspect_cases(
        tag="main",
        revision_overrides={"api": "b" * 40},
    )
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(JOURNAL))}
        . {shlex.quote(str(MANIFEST_PHASE))}
        . {shlex.quote(str(DIGEST_PHASE))}
        env_key_present() {{ grep -qE "^$2=.+" "$1"; }}
        emit_info() {{ :; }}
        lumen_update_journal_bind_target() {{ :; }}
        lumen_compose_in() {{
            printf '%s\\n' \
                'ghcr.io/cyeinfpro/lumen-api:main' \
                'ghcr.io/cyeinfpro/lumen-worker:main' \
                'ghcr.io/cyeinfpro/lumen-web:main'
        }}
        lumen_docker() {{
            local ref="${{@: -1}}"
            case "$ref" in
                {inspect_cases}
                *) return 91 ;;
            esac
        }}
        NEW_RELEASE={shlex.quote(str(release))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        TARGET_TAG=main
        LUMEN_IMAGE_REGISTRY=ghcr.io/cyeinfpro
        LUMEN_UPDATE_BUILD=0
        TGBOT_IMAGE_READY=0
        RELEASE_SOURCE_COMMIT={SOURCE_COMMIT}
        lumen_update_bind_immutable_images
        """
    )

    assert result.returncode != 0
    assert "rolling image/source commit mismatch" in result.stderr
    assert not (release / ".update-image-proof.json").exists()


def test_enabled_tgbot_enters_the_same_immutable_image_proof(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    shared_env = tmp_path / ".env"
    shared_env.write_text(
        "LUMEN_IMAGE_TAG=main\nTELEGRAM_BOT_TOKEN=enabled\n",
        encoding="utf-8",
    )
    tag_inspects = tmp_path / "tag-inspects.log"
    inspect_cases = _fake_image_inspect_cases(
        tag="main",
        include_tgbot=True,
    )
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(JOURNAL))}
        . {shlex.quote(str(MANIFEST_PHASE))}
        . {shlex.quote(str(DIGEST_PHASE))}
        env_key_present() {{ grep -qE "^$2=.+" "$1"; }}
        emit_info() {{ :; }}
        lumen_update_journal_bind_target() {{ :; }}
        lumen_compose_in() {{
            printf '%s\\n' \
                'ghcr.io/cyeinfpro/lumen-api:main' \
                'ghcr.io/cyeinfpro/lumen-worker:main' \
                'ghcr.io/cyeinfpro/lumen-web:main' \
                'ghcr.io/cyeinfpro/lumen-tgbot:main'
        }}
        lumen_docker() {{
            local ref="${{@: -1}}"
            if [ "${{3:-}}" = "--format" ]; then
                printf '%s\\n' "$ref"
                return 0
            fi
            printf '%s\\n' "$ref" >> {shlex.quote(str(tag_inspects))}
            case "$ref" in
                {inspect_cases}
                *) return 91 ;;
            esac
        }}
        NEW_RELEASE={shlex.quote(str(release))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        TARGET_TAG=main
        LUMEN_IMAGE_REGISTRY=ghcr.io/cyeinfpro
        LUMEN_UPDATE_BUILD=0
        TGBOT_IMAGE_READY=1
        RELEASE_SOURCE_COMMIT={SOURCE_COMMIT}
        lumen_update_bind_immutable_images
        lumen_update_activate_bound_image_override
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    proof = json.loads(
        (release / ".update-image-proof.json").read_text(encoding="utf-8")
    )
    assert set(proof["services"]) == {"api", "worker", "web", "tgbot"}
    assert proof["compose_services"]["tgbot"] == IMAGE_IDS["tgbot"]
    assert len(tag_inspects.read_text(encoding="utf-8").splitlines()) == 4


def test_build_binding_uses_image_ids_without_repo_digests(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    shared_env = tmp_path / ".env"
    shared_env.write_text("LUMEN_IMAGE_TAG=v9.9.9\n", encoding="utf-8")
    inspect_cases = _fake_image_inspect_cases(tag="v9.9.9", build=True)
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(JOURNAL))}
        . {shlex.quote(str(MANIFEST_PHASE))}
        . {shlex.quote(str(DIGEST_PHASE))}
        env_key_present() {{ grep -qE "^$2=.+" "$1"; }}
        emit_info() {{ :; }}
        lumen_update_journal_bind_target() {{ :; }}
        lumen_compose_in() {{
            printf '%s\\n' \
                'ghcr.io/cyeinfpro/lumen-api:v9.9.9' \
                'ghcr.io/cyeinfpro/lumen-worker:v9.9.9' \
                'ghcr.io/cyeinfpro/lumen-web:v9.9.9'
        }}
        lumen_docker() {{
            local ref="${{@: -1}}"
            case "$ref" in
                {inspect_cases}
                *) return 91 ;;
            esac
        }}
        NEW_RELEASE={shlex.quote(str(release))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        TARGET_TAG=v9.9.9
        LUMEN_IMAGE_REGISTRY=ghcr.io/cyeinfpro
        LUMEN_UPDATE_BUILD=1
        TGBOT_IMAGE_READY=0
        RELEASE_SOURCE_COMMIT={SOURCE_COMMIT}
        lumen_update_bind_immutable_images
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    proof = json.loads(
        (release / ".update-image-proof.json").read_text(encoding="utf-8")
    )
    override = (release / ".update-images.override.yml").read_text(encoding="utf-8")
    assert proof["build"] is True
    assert all(not record["repo_digests"] for record in proof["services"].values())
    assert all(
        image_id.startswith("sha256:")
        for image_id in proof["compose_services"].values()
    )
    assert "ghcr.io/" not in override
    assert ":v9.9.9" not in override


def test_runtime_tag_drift_cannot_change_bound_start_reference(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    shared_env = tmp_path / ".env"
    shared_env.write_text("LUMEN_IMAGE_TAG=main\n", encoding="utf-8")
    tag_inspects = tmp_path / "tag-inspects.log"
    compose_log = tmp_path / "compose.log"
    inspect_cases = _fake_image_inspect_cases(tag="main")
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(JOURNAL))}
        . {shlex.quote(str(MANIFEST_PHASE))}
        . {shlex.quote(str(DIGEST_PHASE))}
        . {shlex.quote(str(RESTART_PHASE))}
        env_key_present() {{ grep -qE "^$2=.+" "$1"; }}
        emit_info() {{ :; }}
        lumen_update_journal_bind_target() {{ :; }}
        lumen_compose_in() {{
            printf '%s\\n' \
                'ghcr.io/cyeinfpro/lumen-api:main' \
                'ghcr.io/cyeinfpro/lumen-worker:main' \
                'ghcr.io/cyeinfpro/lumen-web:main'
        }}
        compose_up_service() {{
            printf '%s\\t%s\\t%s\\n' "$1" "$2" "$COMPOSE_FILE" \
                > {shlex.quote(str(compose_log))}
        }}
        lumen_docker() {{
            local ref="${{@: -1}}"
            if [ "$1" = image ] && [ "$2" = inspect ]; then
                if [ "${{3:-}}" = "--format" ]; then
                    printf '%s\\n' "$ref"
                    return 0
                fi
                printf '%s\\n' "$ref" >> {shlex.quote(str(tag_inspects))}
                if [ "${{TAG_DRIFTED:-0}}" = "1" ]; then
                    return 94
                fi
                case "$ref" in
                    {inspect_cases}
                    *) return 91 ;;
                esac
            elif [ "$1" = inspect ] && [ "$ref" = lumen-api ]; then
                printf '%s\\n' {shlex.quote(IMAGE_IDS["api"])}
                return 0
            fi
            return 92
        }}
        NEW_RELEASE={shlex.quote(str(release))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        TARGET_TAG=main
        LUMEN_IMAGE_REGISTRY=ghcr.io/cyeinfpro
        LUMEN_UPDATE_BUILD=0
        TGBOT_IMAGE_READY=0
        RELEASE_SOURCE_COMMIT={SOURCE_COMMIT}
        LUMEN_UPDATE_MODE=fast
        lumen_update_bind_immutable_images
        TAG_DRIFTED=1
        lumen_update_activate_bound_image_override
        lumen_update_start_bound_service \
            {shlex.quote(str(release))} api lumen-api
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert len(tag_inspects.read_text(encoding="utf-8").splitlines()) == 3
    _, service, compose_file = compose_log.read_text(encoding="utf-8").split("\t")
    assert service == "api"
    assert str(release / ".update-images.override.yml") in compose_file
    assert f'image: "{IMAGE_IDS["api"]}"' in (
        release / ".update-images.override.yml"
    ).read_text(encoding="utf-8")


def test_target_compose_uses_bound_override_without_leaking_into_rollback(
    tmp_path: Path,
) -> None:
    target = tmp_path / "releases" / "target"
    rollback = tmp_path / "releases" / "rollback"
    target.mkdir(parents=True)
    rollback.mkdir(parents=True)
    override = target / ".update-images.override.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    compose_log = tmp_path / "compose.log"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(DIGEST_PHASE))}
        lumen_compose() {{
            printf '%s\\t%s\\n' "$PWD" "${{COMPOSE_FILE:-}}" \
                >> {shlex.quote(str(compose_log))}
        }}
        NEW_RELEASE={shlex.quote(str(target))}
        TARGET_IMAGE_OVERRIDE_FILE={shlex.quote(str(override))}
        lumen_compose_in {shlex.quote(str(target))} --profile migrate run migrate
        lumen_compose_in {shlex.quote(str(rollback))} up api
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    target_line, rollback_line = compose_log.read_text(encoding="utf-8").splitlines()
    assert str(target / "docker-compose.yml") in target_line
    assert str(override) in target_line
    assert rollback_line == f"{rollback}\t"


def test_resume_accepts_journaled_env_candidate_after_sigkill(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    releases = root / "releases"
    old_release = releases / "old"
    shared.mkdir(parents=True)
    old_release.mkdir(parents=True)
    shared_env = shared / ".env"
    env_snapshot = shared / ".env.update.snapshot"
    journal = shared / "journal.json"
    shared_env.write_text("LUMEN_IMAGE_TAG=v1.0.0\n", encoding="utf-8")
    env_snapshot.write_bytes(shared_env.read_bytes())
    (root / "current").symlink_to("releases/old")

    killed = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-env-candidate
        lumen_update_journal_init
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        UPDATE_ENV_SNAPSHOT={shlex.quote(str(env_snapshot))}
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_ORIGINAL_CURRENT_PRESENT=1
        UPDATE_ORIGINAL_CURRENT_TARGET=releases/old
        UPDATE_ORIGINAL_PREVIOUS_PRESENT=0
        UPDATE_ORIGINAL_PREVIOUS_TARGET=""
        UPDATE_SNAPSHOT_ENV_SHA256="$(lumen_update_file_sha256 "$SHARED_ENV")"
        lumen_update_journal_snapshot_state
        {_request_contract_script("v1.0.1")}
        for phase in lock self_update_scripts check; do
            lumen_update_journal_phase_start "$phase"
            lumen_update_journal_phase_done "$phase"
        done
        lumen_update_journal_phase_start preflight
        lumen_set_env_value_in_file \
            "$SHARED_ENV" IMAGE_PROXY_SECRET generated-before-kill
        kill -9 $$
        """
    )
    assert killed.returncode != 0

    resumed = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=ignored
        LUMEN_UPDATE_RESUME=1
        lumen_update_journal_init
        lumen_update_journal_validate_resume
        test "$(lumen_env_value IMAGE_PROXY_SECRET "$SHARED_ENV")" = \
            generated-before-kill
        """
    )

    assert resumed.returncode == 0, resumed.stderr + resumed.stdout


def test_unknown_snapshot_never_removes_current_or_previous(tmp_path: Path) -> None:
    root = tmp_path / "deploy"
    root.mkdir()
    (root / "current").symlink_to("releases/live")
    (root / "previous").symlink_to("releases/previous")

    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(RECOVERY_STATE))}
        ROOT={shlex.quote(str(root))}
        UPDATE_SNAPSHOT_LINKS_KNOWN=0
        UPDATE_ORIGINAL_CURRENT_PRESENT=0
        UPDATE_ORIGINAL_CURRENT_TARGET=""
        UPDATE_ORIGINAL_PREVIOUS_PRESENT=0
        UPDATE_ORIGINAL_PREVIOUS_TARGET=""
        rc=0
        restore_update_symlink_snapshot || rc=$?
        test "$rc" -ne 0
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert os.readlink(root / "current") == "releases/live"
    assert os.readlink(root / "previous") == "releases/previous"


def test_update_failpoints_support_before_after_and_alias_forms(
    tmp_path: Path,
) -> None:
    script = f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        LUMEN_UPDATE_FAILPOINT="$1"
        rc=0
        lumen_update_failpoint "$2" "$3" || rc=$?
        printf 'rc=%s\\n' "$rc"
    """
    for configured, timing, phase in (
        ("before:check", "before", "check"),
        ("check:before", "before", "check"),
        ("check", "before", "check"),
        ("after:check", "after", "check"),
    ):
        result = subprocess.run(
            ["/bin/bash", "-c", script, "bash", configured, timing, phase],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ, "SHARED_DIR": str(tmp_path)},
            check=False,
        )
        assert result.returncode == 0
        assert "rc=97" in result.stdout
        assert f"{timing}:{phase}" in result.stderr


def test_failpoints_stop_phase_body_even_when_errexit_is_suppressed(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal.json"
    body_marker = tmp_path / "body"
    next_marker = tmp_path / "next"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        . {shlex.quote(str(COMMON))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-before-failpoint
        lumen_emit_step() {{ :; }}
        lumen_emit_info() {{ :; }}
        log_info() {{ :; }}
        lumen_update_journal_init
        LUMEN_UPDATE_FAILPOINT=before:lock
        phase() {{
            emit_start lock
            : > {shlex.quote(str(body_marker))}
            emit_done lock 0
            : > {shlex.quote(str(next_marker))}
        }}
        if phase; then
            :
        fi
        """
    )

    assert result.returncode == 97
    assert not body_marker.exists()
    assert not next_marker.exists()
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["completed_phases"] == []


def test_after_failpoint_stops_next_phase_and_keeps_completion_boundary(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal.json"
    body_marker = tmp_path / "body"
    next_marker = tmp_path / "next"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        . {shlex.quote(str(COMMON))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-after-failpoint
        lumen_emit_step() {{ :; }}
        lumen_emit_info() {{ :; }}
        log_info() {{ :; }}
        lumen_update_journal_init
        LUMEN_UPDATE_FAILPOINT=after:lock
        phase() {{
            emit_start lock
            : > {shlex.quote(str(body_marker))}
            emit_done lock 0
            : > {shlex.quote(str(next_marker))}
        }}
        if phase; then
            :
        fi
        """
    )

    assert result.returncode == 97
    assert body_marker.is_file()
    assert not next_marker.exists()
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["completed_phases"] == ["lock"]


def test_failpoint_records_the_exact_nested_phase(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-nested-failpoint
        lumen_update_journal_init
        LUMEN_UPDATE_FAILPOINT=after:start_green
        rc=0
        lumen_update_failpoint after start_green || rc=$?
        test "$rc" -eq 97
        """
    )
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["last_error"]["phase"] == "start_green"


def test_phase_contract_contains_stable_public_update_protocol() -> None:
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        for phase in lock check fetch_release migrate_db switch \
                restart_services start_green shift_traffic_100 \
                health_check cleanup; do
            lumen_update_phase_is_known "$phase"
        done
        ! lumen_update_phase_is_known arbitrary_new_phase
        """
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_resume_query_reports_completed_phases(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-resume-query
        lumen_update_journal_init
        lumen_update_journal_phase_start lock
        lumen_update_journal_phase_done lock
        lumen_update_journal_phase_completed lock
        ! lumen_update_journal_phase_completed switch
        """
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_phase_and_terminal_transitions_fail_closed_with_noop_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    shared = root / "shared"
    old_release = root / "releases" / "old"
    shared.mkdir(parents=True)
    old_release.mkdir(parents=True)
    shared_env = shared / ".env"
    env_snapshot = shared / ".env.update.snapshot"
    journal = shared / "journal.json"
    shared_env.write_text("LUMEN_IMAGE_TAG=v1.2.3\n", encoding="utf-8")
    env_snapshot.write_bytes(shared_env.read_bytes())
    (root / "current").symlink_to("releases/old")

    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        . {shlex.quote(str(JOURNAL))}
        ROOT={shlex.quote(str(root))}
        SHARED_DIR={shlex.quote(str(shared))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-noop-protocol
        lumen_update_journal_init
        illegal_done=0
        lumen_update_journal_phase_done lock || illegal_done=$?
        test "$illegal_done" -ne 0
        lumen_update_journal_phase_start lock
        wrong_done=0
        lumen_update_journal_phase_done self_update_scripts || wrong_done=$?
        test "$wrong_done" -ne 0
        lumen_update_journal_phase_done lock
        skipped_start=0
        lumen_update_journal_phase_start check || skipped_start=$?
        test "$skipped_start" -ne 0
        lumen_update_journal_phase_start self_update_scripts
        lumen_update_journal_phase_done self_update_scripts
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        UPDATE_ENV_SNAPSHOT={shlex.quote(str(env_snapshot))}
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_ORIGINAL_CURRENT_PRESENT=1
        UPDATE_ORIGINAL_CURRENT_TARGET=releases/old
        UPDATE_ORIGINAL_PREVIOUS_PRESENT=0
        UPDATE_ORIGINAL_PREVIOUS_TARGET=""
        UPDATE_SNAPSHOT_ENV_SHA256="$(lumen_update_file_sha256 "$SHARED_ENV")"
        lumen_update_journal_snapshot_state
        {_request_contract_script("v1.2.3")}
        lumen_update_journal_phase_start check
        SKIP_TO_CLEANUP=1
        lumen_update_journal_phase_done check
        premature_complete=0
        lumen_update_journal_status complete || premature_complete=$?
        test "$premature_complete" -ne 0
        SKIP_TO_CLEANUP=0
        illegal_cleanup=0
        lumen_update_journal_phase_start cleanup || illegal_cleanup=$?
        test "$illegal_cleanup" -ne 0
        SKIP_TO_CLEANUP=1
        lumen_update_journal_phase_start cleanup
        lumen_update_journal_phase_done cleanup
        lumen_update_journal_status complete
        lumen_update_journal_status complete
        """
    )
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["completed_phases"] == [
        "lock",
        "self_update_scripts",
        "check",
        "cleanup",
    ]
    assert payload["completion_mode"] == "noop"
    assert payload["current_subphase"] is None
    assert payload["status"] == "complete"


def test_update_modules_are_domain_split_and_at_most_400_lines() -> None:
    update_dir = ROOT / "scripts" / "update"
    modules = sorted(update_dir.rglob("*.sh"))

    assert {path.parent.name for path in modules} >= {
        "backup",
        "recovery",
        "release",
        "services",
        "update",
    }
    assert all(
        len(path.read_text(encoding="utf-8").splitlines()) <= 400 for path in modules
    )

    runner = RUNNER.read_text(encoding="utf-8")
    assert "update_run_phase" in runner
    for implementation_detail in (
        "docker compose",
        "lumen_compose_in",
        "rsync ",
        "alembic ",
        "curl ",
    ):
        assert implementation_detail not in runner


def test_self_update_unit_contains_every_update_module() -> None:
    update_dir = ROOT / "scripts" / "update"
    source = (update_dir / "release" / "self_update.sh").read_text(encoding="utf-8")
    expected = {
        path.relative_to(ROOT / "scripts").as_posix()
        for path in update_dir.rglob("*")
        if path.suffix in {".py", ".sh"}
    }

    assert expected
    assert all(relative in source for relative in expected)


def test_journal_contract_covers_every_emitted_phase() -> None:
    update_dir = ROOT / "scripts" / "update"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(update_dir.rglob("*.sh"))
    )
    emitted = set(__import__("re").findall(r"emit_start\s+([a-z][a-z0-9_]*)", source))
    emitted.add("cleanup")
    contract = CONTRACT.read_text(encoding="utf-8")

    assert emitted
    assert all(f"\n{phase}\n" in contract for phase in emitted)


def test_phase_done_is_after_required_state_and_side_effects() -> None:
    check = CHECK_PHASE.read_text(encoding="utf-8")
    switch = SWITCH_PHASE.read_text(encoding="utf-8")
    restart = RESTART_PHASE.read_text(encoding="utf-8")
    self_update = (ROOT / "scripts/update/release/self_update.sh").read_text(
        encoding="utf-8"
    )

    noop_done = check.index("emit_done  check 0")
    assert check.index("SKIP_TO_CLEANUP=1") < noop_done
    assert switch.index("refresh_update_runner_units") < switch.index(
        "emit_done switch 0"
    )
    assert restart.index("mark_update_committed") < restart.rindex(
        "emit_done restart_services 0"
    )
    assert self_update.index('CURRENT_RELEASE=""') < self_update.index(
        "emit_done  lock 0"
    )
    assert "export LUMEN_UPDATE_RESUME=1" in self_update


def test_switch_does_not_complete_when_runner_unit_refresh_fails(
    tmp_path: Path,
) -> None:
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(SWITCH_PHASE))}
        ROOT={shlex.quote(str(tmp_path))}
        NEW_ID=new
        CURRENT_ID=old
        emit_start() {{ :; }}
        emit_info() {{ :; }}
        emit_done() {{ : > {shlex.quote(str(tmp_path / "done"))}; }}
        emit_fail() {{ :; }}
        log_error() {{ :; }}
        lumen_release_atomic_switch() {{ return 0; }}
        lumen_update_fsync_directory() {{ return 0; }}
        refresh_update_runner_units() {{ return 1; }}
        update_phase_switch
        """
    )

    assert result.returncode != 0
    assert not (tmp_path / "done").exists()


def test_update_snapshot_restore_and_switch_use_durable_filesystem_commits() -> None:
    journal = JOURNAL.read_text(encoding="utf-8")
    recovery = RECOVERY_STATE.read_text(encoding="utf-8")
    switch = SWITCH_PHASE.read_text(encoding="utf-8")

    assert "lumen_update_copy_file_durable()" in journal
    assert "lumen_update_fsync_directory()" in journal
    assert recovery.count("lumen_update_copy_file_durable") >= 2
    assert 'lumen_update_fsync_directory "${SHARED_DIR}"' in recovery
    assert 'lumen_update_fsync_directory "${ROOT}"' in recovery
    assert 'lumen_update_fsync_directory "${ROOT}"' in switch
