from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
LUMENCTL = ROOT / "scripts" / "lumenctl.sh"
RELEASE_SELF_UPDATE = ROOT / "scripts" / "update" / "release" / "self_update.sh"
RUNNER = ROOT / "scripts" / "update" / "runner.sh"
COMMIT = "c" * 40
OLDER_COMMIT = "d" * 40


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    for key in (
        "LUMEN_SELF_UPDATE_COMMIT",
        "LUMEN_SELF_UPDATE_REF",
        "LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT",
        "LUMEN_SELF_UPDATE_MANIFEST_FILE",
    ):
        env.pop(key, None)
    return env


def run_bash(
    script: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env or clean_env(),
        check=False,
    )


def write_remote_scripts(remote: Path) -> tuple[str, ...]:
    files = {
        "backup.sh": "#!/usr/bin/env bash\nREMOTE_BACKUP=1\n",
        "lib/backup_journal.sh": "#!/usr/bin/env bash\nREMOTE_JOURNAL=1\n",
        "restore.sh": "#!/usr/bin/env bash\nREMOTE_RESTORE=1\n",
    }
    for relative, content in files.items():
        path = remote / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    return tuple(files)


def install_copying_curl(fakebin: Path, *, serialized: bool = False) -> None:
    fakebin.mkdir(parents=True, exist_ok=True)
    curl = fakebin / "curl"
    guard = (
        """
guard="${TEST_CURL_GUARD:?}"
if ! mkdir "${guard}" 2>/dev/null; then
    : > "${TEST_CURL_OVERLAP:?}"
    exit 91
fi
sleep 0.05
trap 'rmdir "${guard}" 2>/dev/null || true' EXIT
"""
        if serialized
        else ""
    )
    curl.write_text(
        f"""#!/usr/bin/env bash
set -eu
url=""
output=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o)
            output="$2"
            shift 2
            ;;
        http://*|https://*)
            url="$1"
            shift
            ;;
        *)
            shift
            ;;
    esac
done
{guard}
relative="${{url#*/scripts/}}"
cp "${{TEST_REMOTE_ROOT:?}}/${{relative}}" "${{output:?}}"
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)


def self_update_env(fakebin: Path, remote: Path) -> dict[str, str]:
    env = clean_env()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_REPO_URL": "https://github.com/example/Lumen.git",
            "LUMEN_SELF_UPDATE": "1",
            "LUMEN_SELF_UPDATE_COMMIT": COMMIT,
            "TEST_REMOTE_ROOT": str(remote),
        }
    )
    return env


def invoke_self_update(
    target: Path,
    files: tuple[str, ...],
    *,
    env: dict[str, str],
    ttl: int,
) -> subprocess.CompletedProcess[str]:
    args = " ".join(shlex.quote(item) for item in files)
    return run_bash(
        f"""
        set -uo pipefail
        . {shlex.quote(str(LIB))}
        rc=0
        lumen_self_update_scripts \
            {shlex.quote(str(target))} {COMMIT} {ttl} {args} || rc=$?
        printf 'rc=%s result=%s changed=%s\\n' \
            "$rc" "$LUMEN_SELF_UPDATE_RESULT" "$LUMEN_SELF_UPDATE_CHANGED"
        """,
        env=env,
    )


def test_integrity_manifest_drives_ttl_repair_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "scripts"
    remote = tmp_path / "remote"
    fakebin = tmp_path / "bin"
    target.mkdir()
    files = write_remote_scripts(remote)
    install_copying_curl(fakebin)
    env = self_update_env(fakebin, remote)

    first = invoke_self_update(target, files, env=env, ttl=3600)
    assert first.returncode == 0, first.stderr + first.stdout
    assert "result=ok" in first.stdout

    manifest_path = target / ".lumen-self-update.integrity"
    records = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
    ]
    assert {record["path"] for record in records} == set(files)
    assert all(
        set(record) == {"commit", "path", "type", "mode", "hash"} for record in records
    )
    assert all(record["commit"] == COMMIT for record in records)
    assert all(record["type"] == "file" for record in records)
    assert {record["path"]: record["mode"] for record in records} == {
        "backup.sh": "0755",
        "lib/backup_journal.sh": "0644",
        "restore.sh": "0755",
    }
    assert all(len(record["hash"]) == 64 for record in records)

    (target / "backup.sh").write_text(
        "#!/usr/bin/env bash\nTAMPERED=1\n",
        encoding="utf-8",
    )
    repaired_tamper = invoke_self_update(target, files, env=env, ttl=3600)
    assert repaired_tamper.returncode == 0, repaired_tamper.stderr
    assert "backup.sh" in repaired_tamper.stdout
    assert (target / "backup.sh").read_bytes() == (remote / "backup.sh").read_bytes()

    (target / "restore.sh").unlink()
    repaired_delete = invoke_self_update(target, files, env=env, ttl=3600)
    assert repaired_delete.returncode == 0, repaired_delete.stderr
    assert "restore.sh" in repaired_delete.stdout
    assert (target / "restore.sh").read_bytes() == (remote / "restore.sh").read_bytes()

    (target / "backup.sh").chmod(0o600)
    repaired_mode = invoke_self_update(target, files, env=env, ttl=3600)
    assert repaired_mode.returncode == 0, repaired_mode.stderr
    assert "backup.sh" in repaired_mode.stdout
    assert (target / "backup.sh").stat().st_mode & 0o777 == 0o755

    (target / "backup.sh").unlink()
    (target / "backup.sh").symlink_to(remote / "backup.sh")
    rejected_symlink = invoke_self_update(target, files, env=env, ttl=3600)
    assert rejected_symlink.returncode == 0
    assert "rc=78" in rejected_symlink.stdout
    assert "result=failed" in rejected_symlink.stdout
    assert "目标不是普通文件" in rejected_symlink.stderr
    assert (target / "backup.sh").is_symlink()


def test_cross_process_self_update_transaction_lock_serializes_writers(
    tmp_path: Path,
) -> None:
    target = tmp_path / "scripts"
    remote = tmp_path / "remote"
    fakebin = tmp_path / "bin"
    guard = tmp_path / "curl.guard"
    overlap = tmp_path / "curl.overlap"
    target.mkdir()
    files = write_remote_scripts(remote)
    install_copying_curl(fakebin, serialized=True)
    env = self_update_env(fakebin, remote)
    env.update(
        {
            "TEST_CURL_GUARD": str(guard),
            "TEST_CURL_OVERLAP": str(overlap),
            "LUMEN_SELF_UPDATE_LOCK_TIMEOUT": "10",
        }
    )
    args = " ".join(shlex.quote(item) for item in files)
    command = [
        "bash",
        "-c",
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_self_update_scripts \
            {shlex.quote(str(target))} {COMMIT} 0 {args}
        printf '%s\\n' "$LUMEN_SELF_UPDATE_RESULT"
        """,
    ]

    first = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    time.sleep(0.02)
    second = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    first_stdout, first_stderr = first.communicate(timeout=20)
    second_stdout, second_stderr = second.communicate(timeout=20)

    assert first.returncode == 0, first_stderr + first_stdout
    assert second.returncode == 0, second_stderr + second_stdout
    assert first_stdout.rstrip().endswith("ok")
    assert second_stdout.rstrip().endswith("ok")
    assert not overlap.exists()
    assert not (target / ".lumen-self-update.lock.d").exists()
    lock_path = Path(f"{target.resolve()}.lumen-self-update.lock")
    assert lock_path.is_file()
    assert lock_path.stat().st_mode & 0o777 == 0o600
    for relative in files:
        assert (target / relative).read_bytes() == (remote / relative).read_bytes()


def test_transaction_lock_releases_on_signal_and_chains_saved_exit_trap(
    tmp_path: Path,
) -> None:
    target = tmp_path / "scripts"
    target.mkdir()
    entered = tmp_path / "entered"
    saved_exit = tmp_path / "saved-exit"
    script = f"""
set -euo pipefail
. {shlex.quote(str(LIB))}
_lumen_self_update_scripts_locked() {{
    : > {shlex.quote(str(entered))}
    sleep 60
}}
trap 'exit 143' TERM
trap 'printf "%s\\n" saved > {shlex.quote(str(saved_exit))}' EXIT
lumen_self_update_scripts {shlex.quote(str(target))} {COMMIT} 0 backup.sh
"""
    process = subprocess.Popen(
        ["bash", "-c", script],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=clean_env(),
        start_new_session=True,
    )

    deadline = time.monotonic() + 5
    while not entered.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert entered.exists()
    lock_path = Path(f"{target.resolve()}.lumen-self-update.lock")
    assert lock_path.is_file()

    os.killpg(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 143, stderr + stdout
    assert saved_exit.read_text(encoding="utf-8") == "saved\n"
    reacquire = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        _lumen_self_update_scripts_locked() {{ return 0; }}
        LUMEN_SELF_UPDATE_LOCK_TIMEOUT=0
        lumen_self_update_scripts \
            {shlex.quote(str(target))} {COMMIT} 0 backup.sh
        """
    )
    assert reacquire.returncode == 0, reacquire.stderr + reacquire.stdout


def test_expected_scripts_commit_survives_reexec_and_resume(
    tmp_path: Path,
) -> None:
    target = tmp_path / "scripts"
    remote = tmp_path / "remote"
    fakebin = tmp_path / "bin"
    update_log = tmp_path / "update-log"
    control_root = tmp_path / "control"
    target.mkdir()
    update_log.mkdir()
    control_root.mkdir()
    files = write_remote_scripts(remote)
    install_copying_curl(fakebin)
    env = self_update_env(fakebin, remote)
    installed = invoke_self_update(target, files, env=env, ttl=0)
    assert installed.returncode == 0, installed.stderr + installed.stdout

    operation_id = "update-immutable-commit"
    state_path = control_root / ".lumen-update-state" / f"scripts-{operation_id}.commit"
    first_hop = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(RELEASE_SELF_UPDATE))}
        ROOT={shlex.quote(str(control_root))}
        UPDATE_LOG_DIR={shlex.quote(str(update_log))}
        OPERATION_ID={operation_id}
        LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT={COMMIT}
        export LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT
        lumen_update_bind_expected_scripts_commit {shlex.quote(str(target))}
        printf '%s\\n' "$LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT"
        """,
        env=clean_env(),
    )
    assert first_hop.returncode == 0, first_hop.stderr + first_hop.stdout
    assert first_hop.stdout.strip() == COMMIT
    assert state_path.read_text(encoding="utf-8").strip() == COMMIT

    reexec = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(RELEASE_SELF_UPDATE))}
        ROOT={shlex.quote(str(control_root))}
        UPDATE_LOG_DIR={shlex.quote(str(update_log))}
        OPERATION_ID={operation_id}
        LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT={COMMIT}
        export LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT
        lumen_update_bind_expected_scripts_commit {shlex.quote(str(target))}
        printf '%s\\n' "$LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT"
        """,
        env=clean_env(),
    )
    assert reexec.returncode == 0, reexec.stderr + reexec.stdout
    assert reexec.stdout.strip() == COMMIT

    resume = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(RELEASE_SELF_UPDATE))}
        ROOT={shlex.quote(str(control_root))}
        UPDATE_LOG_DIR={shlex.quote(str(update_log))}
        OPERATION_ID={operation_id}
        unset LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT
        lumen_update_bind_expected_scripts_commit {shlex.quote(str(target))}
        printf '%s\\n' "$LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT"
        """,
        env=clean_env(),
    )
    assert resume.returncode == 0, resume.stderr + resume.stdout
    assert resume.stdout.strip() == COMMIT

    drift = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(RELEASE_SELF_UPDATE))}
        ROOT={shlex.quote(str(control_root))}
        UPDATE_LOG_DIR={shlex.quote(str(update_log))}
        OPERATION_ID={operation_id}
        LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT={OLDER_COMMIT}
        export LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT
        ! lumen_update_bind_expected_scripts_commit {shlex.quote(str(target))}
        """,
        env=clean_env(),
    )
    assert drift.returncode == 0, drift.stderr + drift.stdout
    assert "发生漂移" in drift.stderr


def test_self_update_refuses_older_release_tag_before_download(
    tmp_path: Path,
) -> None:
    target = tmp_path / "scripts"
    remote = tmp_path / "remote"
    fakebin = tmp_path / "bin"
    first_manifest = tmp_path / "first.json"
    old_manifest = tmp_path / "old.json"
    curl_log = tmp_path / "curl.log"
    target.mkdir()
    files = write_remote_scripts(remote)
    install_copying_curl(fakebin)
    first_manifest.write_text(
        json.dumps({"version": "v2.0.0", "commit_sha": COMMIT}),
        encoding="utf-8",
    )
    old_manifest.write_text(
        json.dumps({"version": "v1.9.9", "commit_sha": OLDER_COMMIT}),
        encoding="utf-8",
    )
    env = self_update_env(fakebin, remote)
    env.pop("LUMEN_SELF_UPDATE_COMMIT")
    env["LUMEN_SELF_UPDATE_MANIFEST_FILE"] = str(first_manifest)
    args = " ".join(shlex.quote(item) for item in files)

    first = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_self_update_scripts \
            {shlex.quote(str(target))} v2.0.0 0 {args}
        printf '%s\\n' "$LUMEN_SELF_UPDATE_RESULT"
        """,
        env=env,
    )
    assert first.returncode == 0, first.stderr + first.stdout
    assert first.stdout.rstrip().endswith("ok")
    before = {relative: (target / relative).read_bytes() for relative in files}

    logging_curl = fakebin / "curl"
    original = logging_curl.read_text(encoding="utf-8")
    logging_curl.write_text(
        original.replace(
            'url=""',
            f'printf "called\\\\n" >> {shlex.quote(str(curl_log))}\nurl=""',
            1,
        ),
        encoding="utf-8",
    )
    logging_curl.chmod(0o755)
    env["LUMEN_SELF_UPDATE_MANIFEST_FILE"] = str(old_manifest)
    rejected = run_bash(
        f"""
        set -uo pipefail
        . {shlex.quote(str(LIB))}
        rc=0
        lumen_self_update_scripts \
            {shlex.quote(str(target))} v1.9.9 0 {args} || rc=$?
        printf 'rc=%s result=%s\\n' "$rc" "$LUMEN_SELF_UPDATE_RESULT"
        """,
        env=env,
    )

    assert rejected.returncode == 0, rejected.stderr + rejected.stdout
    assert "rc=78 result=failed" in rejected.stdout
    assert "拒绝 scripts release tag 降级" in rejected.stderr
    assert not curl_log.exists()
    for relative, content in before.items():
        assert (target / relative).read_bytes() == content


def test_self_update_semantic_failures_return_nonzero(tmp_path: Path) -> None:
    target = tmp_path / "scripts"
    remote = tmp_path / "remote"
    fakebin = tmp_path / "bin"
    target.mkdir()
    files = write_remote_scripts(remote)
    install_copying_curl(fakebin)
    env = self_update_env(fakebin, remote)
    args = " ".join(shlex.quote(item) for item in files)

    cases = {
        "manifest": f"""
            lumen_fetch_release_manifest() {{ return 1; }}
            lumen_self_update_scripts \
                {shlex.quote(str(target))} v9.9.9 0 {args}
        """,
        "commit_mismatch": f"""
            LUMEN_SELF_UPDATE_COMMIT={OLDER_COMMIT}
            lumen_self_update_scripts \
                {shlex.quote(str(target))} {COMMIT} 0 {args}
        """,
        "download": f"""
            curl() {{ return 22; }}
            lumen_self_update_scripts \
                {shlex.quote(str(target))} {COMMIT} 0 {args}
        """,
        "validation": f"""
            lumen_validate_self_update_file() {{ return 1; }}
            lumen_self_update_scripts \
                {shlex.quote(str(target))} {COMMIT} 0 {args}
        """,
        "install_transaction": f"""
            lumen_self_update_install_transaction() {{ return 71; }}
            lumen_self_update_scripts \
                {shlex.quote(str(target))} {COMMIT} 0 {args}
        """,
    }

    for failure, command in cases.items():
        result = run_bash(
            f"""
            set -uo pipefail
            . {shlex.quote(str(LIB))}
            rc=0
            {{
            {command}
            }} || rc=$?
            printf 'failure=%s rc=%s result=%s\\n' \
                {shlex.quote(failure)} "$rc" "$LUMEN_SELF_UPDATE_RESULT"
            """,
            env=env,
        )

        assert result.returncode == 0, failure + ": " + result.stderr + result.stdout
        assert f"failure={failure} rc=78 result=failed" in result.stdout


def test_journal_resume_skip_requires_complete_manifest_backed_unit() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    function = runner.split("update_run_phase() {", 1)[1].split("\n}\n\n", 1)[0]
    validation = function.index("lumen_update_script_unit_complete")
    resumed_emit = function.index("value=already_completed")
    implementation = function.index('"${implementation}" "$@"')

    assert validation < resumed_emit < implementation
    assert "return 78" in function[validation:resumed_emit]


def test_lumenctl_branch_first_hop_exports_immutable_expected_commit() -> None:
    core = (ROOT / "scripts" / "lib" / "self_update.sh").read_text(encoding="utf-8")
    branch = core.split("lumen_self_update_scripts_from_github_branch() {", 1)[1]
    branch = branch.split("\n}", 1)[0]
    lumenctl = LUMENCTL.read_text(encoding="utf-8")

    assert 'LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT="${commit_sha}"' in branch
    assert "export LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT" in branch
    assert "lumen_self_update_scripts_from_github_branch" in lumenctl


def test_expected_scripts_commit_state_is_outside_app_writable_backup_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    scripts = root / "current" / "scripts"
    backup_root = tmp_path / "app-backup"
    root.mkdir()
    scripts.mkdir(parents=True)
    backup_root.mkdir()
    backup_root.chmod(0o777)
    operation_id = "update-root-trust"

    result = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(RELEASE_SELF_UPDATE))}
        ROOT={shlex.quote(str(root))}
        UPDATE_LOG_DIR={shlex.quote(str(backup_root))}
        OPERATION_ID={operation_id}
        LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT={COMMIT}
        export LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT
        lumen_update_bind_expected_scripts_commit {shlex.quote(str(scripts))}
        state_path="$(lumen_update_expected_scripts_state_path)"
        printf 'state=%s\\n' "$state_path"
        """,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    state_path = root / ".lumen-update-state" / f"scripts-{operation_id}.commit"
    assert f"state={state_path}" in result.stdout
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert not list(backup_root.glob(".lumen-update-scripts-*.commit"))


def test_expected_scripts_commit_state_rejects_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    scripts = root / "current" / "scripts"
    control = root / ".lumen-update-state"
    victim = tmp_path / "victim"
    scripts.mkdir(parents=True)
    control.mkdir(mode=0o700)
    victim.write_text("do-not-touch\n", encoding="utf-8")
    operation_id = "update-symlink-state"
    state_path = control / f"scripts-{operation_id}.commit"
    state_path.symlink_to(victim)

    result = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        . {shlex.quote(str(RELEASE_SELF_UPDATE))}
        ROOT={shlex.quote(str(root))}
        OPERATION_ID={operation_id}
        LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT={COMMIT}
        export LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT
        rc=0
        lumen_update_bind_expected_scripts_commit \
            {shlex.quote(str(scripts))} || rc=$?
        test "$rc" -ne 0
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert state_path.is_symlink()
    assert victim.read_text(encoding="utf-8") == "do-not-touch\n"


def test_update_orchestrator_propagates_nonzero_phase_status() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    do_update = runner.split("do_update() {", 1)[1].split("\n}\n\n", 1)[0]

    for phase in (
        "lock",
        "self_update_scripts",
        "check",
        "preflight",
        "backup_preflight",
        "fetch_release",
        "set_image_tag",
        "pull_images",
        "check_storage",
        "start_infra",
        "migrate_db",
        "switch",
        "restart_services",
        "health_check",
    ):
        call = f"update_run_phase {phase} "
        line = next(line for line in do_update.splitlines() if call in line)
        assert "|| return" in line


def test_active_script_unit_never_exposes_a_per_file_partial_commit(
    tmp_path: Path,
) -> None:
    target = tmp_path / "scripts"
    remote = tmp_path / "remote"
    fakebin = tmp_path / "bin"
    ready = tmp_path / "first-target-replaced"
    go = tmp_path / "continue"
    target.mkdir()
    files = write_remote_scripts(remote)
    originals = {
        relative: f"#!/usr/bin/env bash\nLOCAL_{index}=1\n".encode()
        for index, relative in enumerate(files, start=1)
    }
    for relative, content in originals.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o755 if relative.endswith(".sh") and "/" not in relative else 0o644)
    install_copying_curl(fakebin)
    real_mv = shutil.which("mv") or "/bin/mv"
    mv = fakebin / "mv"
    mv.write_text(
        f"""#!/usr/bin/env bash
set -eu
{shlex.quote(real_mv)} "$@"
last="${{@: -1}}"
case "$last" in
    {shlex.quote(str(target.resolve()))}.txn.*/active/.lumen-self-update.source)
        : > {shlex.quote(str(ready))}
        while [ ! -e {shlex.quote(str(go))} ]; do sleep 0.02; done
        ;;
esac
""",
        encoding="utf-8",
    )
    mv.chmod(0o755)
    env = self_update_env(fakebin, remote)
    args = " ".join(shlex.quote(item) for item in files)
    process = subprocess.Popen(
        [
            "bash",
            "-c",
            f"""
            set -euo pipefail
            . {shlex.quote(str(LIB))}
            lumen_self_update_scripts \
                {shlex.quote(str(target))} {COMMIT} 0 {args}
            """,
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while (
            process.poll() is None
            and not ready.exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if ready.exists():
            assert {
                relative: (target / relative).read_bytes() for relative in files
            } == originals
        go.touch()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            go.touch()
            process.kill()
            process.wait()

    assert process.returncode == 0, stderr + stdout
    for relative in files:
        assert (target / relative).read_bytes() == (remote / relative).read_bytes()


def test_self_update_fsync_order_and_sigkill_worker_rollback(
    tmp_path: Path,
) -> None:
    target = tmp_path / "scripts"
    remote = tmp_path / "remote"
    fakebin = tmp_path / "bin"
    trace = tmp_path / "trace.log"
    target.mkdir()
    files = write_remote_scripts(remote)
    install_copying_curl(fakebin)
    env = self_update_env(fakebin, remote)
    env["LUMEN_SELF_UPDATE_TRACE_FILE"] = str(trace)

    installed = invoke_self_update(target, files, env=env, ttl=0)
    assert installed.returncode == 0, installed.stderr + installed.stdout
    assert "result=ok" in installed.stdout
    assert trace.read_text(encoding="utf-8").splitlines() == [
        "stage_fsync_complete",
        "intent_durable",
        "exchange_complete",
        "validation_durable",
        "old_tree_removed",
    ]

    before = {relative: (target / relative).read_bytes() for relative in files}
    (remote / "backup.sh").write_text(
        "#!/usr/bin/env bash\nREMOTE_BACKUP=2\n",
        encoding="utf-8",
    )
    trace.write_text("", encoding="utf-8")
    env["LUMEN_SELF_UPDATE_FAILPOINT"] = "sigkill:after_exchange"
    interrupted = invoke_self_update(target, files, env=env, ttl=0)

    assert interrupted.returncode == 0, interrupted.stderr + interrupted.stdout
    assert "result=failed" in interrupted.stdout
    assert trace.read_text(encoding="utf-8").splitlines() == [
        "stage_fsync_complete",
        "intent_durable",
        "exchange_complete",
        "rollback_exchange_complete",
    ]
    for relative, content in before.items():
        assert (target / relative).read_bytes() == content
    assert not list(target.parent.glob(f"{target.name}.txn.*"))


def test_self_update_lock_timeout_is_nonzero_and_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "scripts"
    target.mkdir()
    lock_path = Path(f"{target.resolve()}.lumen-self-update.lock")
    ready = tmp_path / "lock-ready"
    holder = subprocess.Popen(
        [
            "python3",
            "-c",
            (
                "import fcntl, os, signal, sys; "
                "fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600); "
                "fcntl.flock(fd, fcntl.LOCK_EX); "
                "open(sys.argv[2], 'w').close(); "
                "signal.pause()"
            ),
            str(lock_path),
            str(ready),
        ],
        cwd=ROOT,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        result = run_bash(
            f"""
            set -euo pipefail
            . {shlex.quote(str(LIB))}
            LUMEN_SELF_UPDATE_LOCK_TIMEOUT=0
            rc=0
            lumen_self_update_scripts \
                {shlex.quote(str(target))} {COMMIT} 0 backup.sh || rc=$?
            printf 'rc=%s result=%s\\n' "$rc" "$LUMEN_SELF_UPDATE_RESULT"
            test "$rc" -eq 75
            test "$LUMEN_SELF_UPDATE_RESULT" = failed
            """,
        )
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    assert result.returncode == 0, result.stderr + result.stdout
    assert "rc=75 result=failed" in result.stdout
