from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install.sh"
RAW_BOOTSTRAP = ROOT / "scripts" / "install" / "raw_bootstrap.sh"
INSTALL_ENTRYPOINT = ROOT / "scripts" / "install" / "entrypoint.sh"
INSTALL_LAYOUT = ROOT / "scripts" / "install" / "layout.sh"
INSTALL_SERVICES = ROOT / "scripts" / "install" / "services.sh"
RELEASE_TAG = "v9.8.7"
RELEASE_COMMIT = "a" * 40


def _snapshot_tree(root: Path) -> dict[str, tuple[str, int, bytes | str]]:
    snapshot: dict[str, tuple[str, int, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode & 0o7777
        if path.is_symlink():
            snapshot[relative] = ("symlink", mode, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("dir", mode, b"")
        else:
            snapshot[relative] = ("file", mode, path.read_bytes())
    return snapshot


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _prepare_bootstrap(
    tmp_path: Path,
    *,
    remote_install: str = (
        "#!/usr/bin/env bash\n"
        'python3 "$(dirname "${BASH_SOURCE[0]}")/install/'
        'bootstrap_transaction.py" accept '
        '"${LUMEN_RAW_BOOTSTRAP_TRANSACTION:?}"\n'
        'printf "%s\\n" "$*" >> "${TEST_EXEC_LOG:?}"\n'
    ),
) -> tuple[Path, Path, Path, Path, dict[str, str]]:
    deploy_root = tmp_path / "deploy"
    release = deploy_root / "releases" / "20260802-010101"
    scripts = release / "scripts"
    (scripts / "lib").mkdir(parents=True)
    (release / "apps" / "api").mkdir(parents=True)
    _write_executable(
        scripts / "install.sh",
        "#!/usr/bin/env bash\nprintf 'old installer\\n'\n",
    )
    _write_executable(
        scripts / "lib.sh",
        "#!/usr/bin/env bash\nOLD_LIB=1\n",
    )
    _write_executable(
        scripts / "lib" / "runtime.sh",
        "#!/usr/bin/env bash\nOLD_RUNTIME=1\n",
    )
    _write_executable(
        scripts / "obsolete.sh",
        "#!/usr/bin/env bash\nOBSOLETE=1\n",
    )
    (release / "apps" / "api" / "sentinel.txt").write_text(
        "keep-release-source\n",
        encoding="utf-8",
    )
    (deploy_root / "shared").mkdir()
    (deploy_root / "current").symlink_to("releases/20260802-010101")

    remote = tmp_path / "remote"
    (remote / "scripts" / "lib").mkdir(parents=True)
    _write_executable(remote / "scripts" / "install.sh", remote_install)
    _write_executable(
        remote / "scripts" / "lib.sh",
        "#!/usr/bin/env bash\nREMOTE_LIB=1\n",
    )
    _write_executable(
        remote / "scripts" / "lib" / "runtime.sh",
        "#!/usr/bin/env bash\nREMOTE_RUNTIME=1\n",
    )
    (remote / "scripts" / "install").mkdir()
    shutil.copy2(
        ROOT / "scripts" / "install" / "bootstrap_transaction.py",
        remote / "scripts" / "install" / "bootstrap_transaction.py",
    )
    (remote / "scripts" / "update").mkdir()
    shutil.copy2(
        ROOT / "scripts" / "update" / "entry_lock.py",
        remote / "scripts" / "update" / "entry_lock.py",
    )
    (remote / "apps").mkdir()
    (remote / "apps" / "must-not-copy.txt").write_text("no\n", encoding="utf-8")

    bootstrap = tmp_path / "bootstrap" / "install.sh"
    bootstrap.parent.mkdir()
    shutil.copy2(INSTALL, bootstrap)
    (bootstrap.parent / "install").mkdir()
    shutil.copy2(RAW_BOOTSTRAP, bootstrap.parent / "install" / "raw_bootstrap.sh")

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _write_executable(
        fakebin / "git",
        """#!/usr/bin/env bash
set -euo pipefail
test "${1:-}" = "clone"
dest="${@: -1}"
mkdir -p "${dest}"
cp -a "${TEST_REMOTE_ROOT:?}/." "${dest}/"
""",
    )

    exec_log = tmp_path / "exec.log"
    env = os.environ.copy()
    env.update(
        {
            "LC_ALL": "C",
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_INSTALL_DIR": str(deploy_root),
            "LUMEN_REPO_URL": "https://github.com/example/Lumen.git",
            "LUMEN_BRANCH": "main",
            "TEST_REMOTE_ROOT": str(remote),
            "TEST_EXEC_LOG": str(exec_log),
        }
    )
    return deploy_root, release, remote, bootstrap, env


def _run_bootstrap(
    bootstrap: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(bootstrap), "--update"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _prepare_fresh_bootstrap(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, str], Path]:
    install_dir = tmp_path / "fresh"
    remote = tmp_path / "remote"
    (remote / "scripts").mkdir(parents=True)
    _write_executable(
        remote / "scripts" / "install.sh",
        """#!/usr/bin/env bash
printf '%s|%s|%s|%s\n' \
    "${LUMEN_INSTALL_RESOLVED_TAG:-}" \
    "${LUMEN_INSTALL_RESOLVED_COMMIT:-}" \
    "${LUMEN_IMAGE_TAG:-}" \
    "$*" > "${TEST_EXEC_LOG:?}"
""",
    )
    _write_executable(remote / "scripts" / "lib.sh", "#!/usr/bin/env bash\n")

    bootstrap = tmp_path / "bootstrap" / "install.sh"
    bootstrap.parent.mkdir()
    shutil.copy2(INSTALL, bootstrap)
    (bootstrap.parent / "install").mkdir()
    shutil.copy2(RAW_BOOTSTRAP, bootstrap.parent / "install" / "raw_bootstrap.sh")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    event_log = tmp_path / "events.log"
    _write_executable(
        fakebin / "curl",
        f"""#!/usr/bin/env bash
set -euo pipefail
url=""
output=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) output="$2"; shift 2 ;;
        https://*) url="$1"; shift ;;
        *) shift ;;
    esac
done
printf 'curl:%s\n' "$url" >> "${{TEST_EVENT_LOG:?}}"
case "$url" in
    */releases/latest)
        printf '%s\n' '{{"tag_name":"{RELEASE_TAG}"}}' > "$output"
        ;;
    */release-manifest.json)
        printf '%s\n' \
            '{{"schema_version":1,"version":"{RELEASE_TAG}","commit_sha":"{RELEASE_COMMIT}","images":{{}}}}' \
            > "$output"
        ;;
    *) exit 91 ;;
esac
""",
    )
    _write_executable(
        fakebin / "git",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'git:%s\n' "$*" >> "${{TEST_EVENT_LOG:?}}"
case "${{1:-}}" in
    clone)
        dest="${{@: -1}}"
        mkdir -p "$dest/.git"
        cp -a "${{TEST_REMOTE_ROOT:?}}/." "$dest/"
        ;;
    init)
        dest="${{@: -1}}"
        mkdir -p "$dest/.git"
        ;;
    -C)
        root="$2"
        shift 2
        case "${{1:-}}" in
            remote|checkout|reset) ;;
            fetch) cp -a "${{TEST_REMOTE_ROOT:?}}/." "$root/" ;;
            rev-parse) printf '%s\n' '{RELEASE_COMMIT}' ;;
            *) exit 92 ;;
        esac
        ;;
    *) exit 93 ;;
esac
""",
    )
    exec_log = tmp_path / "exec.log"
    env = os.environ.copy()
    env.update(
        {
            "LC_ALL": "C",
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_INSTALL_DIR": str(install_dir),
            "LUMEN_REPO_URL": "https://github.com/example/Lumen.git",
            "TEST_REMOTE_ROOT": str(remote),
            "TEST_EVENT_LOG": str(event_log),
            "TEST_EXEC_LOG": str(exec_log),
        }
    )
    return install_dir, bootstrap, env, event_log


def _prepare_inplace_bootstrap(
    tmp_path: Path,
    *,
    remote_install: str,
) -> tuple[Path, Path, dict[str, str], dict[str, tuple[str, int, bytes | str]]]:
    install_dir = tmp_path / "legacy"
    (install_dir / "scripts").mkdir(parents=True)
    (install_dir / "apps" / "api").mkdir(parents=True)
    (install_dir / "var").mkdir()
    _write_executable(
        install_dir / "scripts" / "install.sh",
        "#!/usr/bin/env bash\nprintf 'old installer\n'\n",
    )
    _write_executable(
        install_dir / "scripts" / "lib.sh",
        "#!/usr/bin/env bash\nOLD_LIB=1\n",
    )
    (install_dir / "apps" / "api" / "old.txt").write_text(
        "old\n",
        encoding="utf-8",
    )
    (install_dir / "obsolete.txt").write_text("obsolete\n", encoding="utf-8")
    (install_dir / ".env").write_text("KEEP_ENV=1\n", encoding="utf-8")
    (install_dir / "var" / "runtime.txt").write_text(
        "keep-runtime\n",
        encoding="utf-8",
    )
    before = _snapshot_tree(install_dir)

    remote = tmp_path / "remote"
    (remote / "scripts" / "install").mkdir(parents=True)
    (remote / "scripts" / "update").mkdir()
    (remote / "apps" / "api").mkdir(parents=True)
    _write_executable(remote / "scripts" / "install.sh", remote_install)
    _write_executable(
        remote / "scripts" / "lib.sh",
        "#!/usr/bin/env bash\nNEW_LIB=1\n",
    )
    shutil.copy2(
        ROOT / "scripts" / "install" / "bootstrap_transaction.py",
        remote / "scripts" / "install" / "bootstrap_transaction.py",
    )
    shutil.copy2(
        ROOT / "scripts" / "update" / "entry_lock.py",
        remote / "scripts" / "update" / "entry_lock.py",
    )
    (remote / "apps" / "api" / "new.txt").write_text("new\n", encoding="utf-8")

    bootstrap = tmp_path / "bootstrap" / "install.sh"
    bootstrap.parent.mkdir()
    shutil.copy2(INSTALL, bootstrap)
    (bootstrap.parent / "install").mkdir()
    shutil.copy2(RAW_BOOTSTRAP, bootstrap.parent / "install" / "raw_bootstrap.sh")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _write_executable(
        fakebin / "git",
        """#!/usr/bin/env bash
set -euo pipefail
test "${1:-}" = clone
dest="${@: -1}"
mkdir -p "$dest/.git"
cp -a "${TEST_REMOTE_ROOT:?}/." "$dest/"
""",
    )
    _write_executable(
        fakebin / "rsync",
        """#!/usr/bin/env bash
touch "${TEST_RSYNC_CALLED:?}"
exit 99
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "LC_ALL": "C",
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_INSTALL_DIR": str(install_dir),
            "LUMEN_REPO_URL": "https://github.com/example/Lumen.git",
            "LUMEN_INSTALL_CHANNEL": "main",
            "TEST_REMOTE_ROOT": str(remote),
            "TEST_EXEC_LOG": str(tmp_path / "exec.log"),
            "TEST_RSYNC_CALLED": str(tmp_path / "rsync-called"),
        }
    )
    return install_dir, bootstrap, env, before


def test_release_bootstrap_stages_validates_and_atomically_replaces_scripts(
    tmp_path: Path,
) -> None:
    deploy_root, release, remote, bootstrap, env = _prepare_bootstrap(tmp_path)
    old_scripts = release / "scripts"
    old_snapshot = _snapshot_tree(old_scripts)

    result = _run_bootstrap(bootstrap, env)

    assert result.returncode == 0, result.stderr + result.stdout
    assert os.readlink(deploy_root / "current") == "releases/20260802-010101"
    assert (old_scripts / "lib.sh").read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\nREMOTE_LIB=1\n"
    )
    assert not (old_scripts / "obsolete.sh").exists()
    assert (release / "apps" / "api" / "sentinel.txt").read_text(
        encoding="utf-8"
    ) == "keep-release-source\n"
    assert not (release / "apps" / "must-not-copy.txt").exists()
    assert (tmp_path / "exec.log").read_text(encoding="utf-8").splitlines() == [
        "--update"
    ]
    assert not list(release.glob(".scripts.bootstrap.*"))
    assert old_snapshot != _snapshot_tree(old_scripts)
    assert _snapshot_tree(old_scripts) == _snapshot_tree(remote / "scripts")


def test_stable_raw_install_resolves_manifest_commit_before_git_checkout(
    tmp_path: Path,
) -> None:
    install_dir, bootstrap, env, event_log = _prepare_fresh_bootstrap(tmp_path)

    result = subprocess.run(
        ["/bin/bash", str(bootstrap), "--install"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert (install_dir / "scripts" / "install.sh").is_file()
    assert (tmp_path / "exec.log").read_text(encoding="utf-8").strip() == (
        f"{RELEASE_TAG}|{RELEASE_COMMIT}||--install"
    )
    events = event_log.read_text(encoding="utf-8").splitlines()
    latest = next(i for i, item in enumerate(events) if item.endswith("/releases/latest"))
    manifest = next(
        i for i, item in enumerate(events) if item.endswith("/release-manifest.json")
    )
    git_init = next(i for i, item in enumerate(events) if item.startswith("git:init "))
    git_fetch = next(i for i, item in enumerate(events) if " fetch " in item)
    assert latest < manifest < git_init < git_fetch
    assert any(f"refs/tags/{RELEASE_TAG}" in item for item in events)
    assert not any(" clone " in f" {item} " for item in events)


def test_explicit_rolling_raw_install_may_clone_main_without_release_lookup(
    tmp_path: Path,
) -> None:
    _, bootstrap, env, event_log = _prepare_fresh_bootstrap(tmp_path)
    env["LUMEN_INSTALL_CHANNEL"] = "main"

    result = subprocess.run(
        ["/bin/bash", str(bootstrap), "--install"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    events = event_log.read_text(encoding="utf-8").splitlines()
    assert not any(item.startswith("curl:") for item in events)
    assert any("git:clone" in item and "--branch main" in item for item in events)
    assert (tmp_path / "exec.log").read_text(encoding="utf-8").strip() == (
        "||main|--install"
    )


def test_release_bootstrap_fsyncs_intent_before_exchange_and_validation(
    tmp_path: Path,
) -> None:
    _, _, _, bootstrap, env = _prepare_bootstrap(tmp_path)
    trace = tmp_path / "bootstrap-trace.log"
    env["LUMEN_BOOTSTRAP_TRACE_FILE"] = str(trace)

    result = _run_bootstrap(bootstrap, env)

    assert result.returncode == 0, result.stderr + result.stdout
    assert trace.read_text(encoding="utf-8").splitlines() == [
        "stage_fsync_complete",
        "intent_durable",
        "exchange_complete",
        "validation_durable",
        "old_tree_removed",
    ]


def test_legacy_inplace_sigkill_rolls_back_without_live_rsync(
    tmp_path: Path,
) -> None:
    install_dir, bootstrap, env, before = _prepare_inplace_bootstrap(
        tmp_path,
        remote_install="#!/usr/bin/env bash\nkill -KILL $$\n",
    )

    result = _run_bootstrap(bootstrap, env)

    assert result.returncode == 137, result.stderr + result.stdout
    assert _snapshot_tree(install_dir) == before
    assert not (tmp_path / "rsync-called").exists()
    assert not list(tmp_path.glob(".legacy.bootstrap.*"))


def test_legacy_inplace_switch_preserves_runtime_and_removes_obsolete_code(
    tmp_path: Path,
) -> None:
    install_dir, bootstrap, env, _ = _prepare_inplace_bootstrap(
        tmp_path,
        remote_install=(
            "#!/usr/bin/env bash\n"
            'python3 "$(dirname "${BASH_SOURCE[0]}")/install/'
            'bootstrap_transaction.py" accept '
            '"${LUMEN_RAW_BOOTSTRAP_TRANSACTION:?}"\n'
            'printf "%s\\n" "$*" > "${TEST_EXEC_LOG:?}"\n'
        ),
    )

    result = _run_bootstrap(bootstrap, env)

    assert result.returncode == 0, result.stderr + result.stdout
    assert (install_dir / "apps" / "api" / "new.txt").read_text(
        encoding="utf-8"
    ) == "new\n"
    assert not (install_dir / "apps" / "api" / "old.txt").exists()
    assert not (install_dir / "obsolete.txt").exists()
    assert (install_dir / ".env").read_text(encoding="utf-8") == "KEEP_ENV=1\n"
    assert (install_dir / "var" / "runtime.txt").read_text(
        encoding="utf-8"
    ) == "keep-runtime\n"
    assert (tmp_path / "exec.log").read_text(encoding="utf-8").strip() == "--update"
    assert not (tmp_path / "rsync-called").exists()
    assert not list(tmp_path.glob(".legacy.bootstrap.*"))


def test_release_bootstrap_staging_failure_leaves_active_scripts_byte_identical(
    tmp_path: Path,
) -> None:
    deploy_root, release, remote, bootstrap, env = _prepare_bootstrap(tmp_path)
    active_scripts = release / "scripts"
    before = _snapshot_tree(active_scripts)
    os.mkfifo(remote / "scripts" / "cannot-copy")

    result = _run_bootstrap(bootstrap, env)

    assert result.returncode != 0
    assert os.readlink(deploy_root / "current") == "releases/20260802-010101"
    assert _snapshot_tree(active_scripts) == before
    assert not list(release.glob(".scripts.bootstrap.*"))


def test_release_bootstrap_validation_failure_leaves_active_scripts_byte_identical(
    tmp_path: Path,
) -> None:
    deploy_root, release, remote, bootstrap, env = _prepare_bootstrap(tmp_path)
    active_scripts = release / "scripts"
    before = _snapshot_tree(active_scripts)
    _write_executable(
        remote / "scripts" / "install.sh",
        "#!/usr/bin/env bash\nif then\n",
    )

    result = _run_bootstrap(bootstrap, env)

    assert result.returncode != 0
    assert os.readlink(deploy_root / "current") == "releases/20260802-010101"
    assert _snapshot_tree(active_scripts) == before
    assert not list(release.glob(".scripts.bootstrap.*"))


def test_release_bootstrap_rejects_symlinks_without_touching_active_scripts(
    tmp_path: Path,
) -> None:
    deploy_root, release, remote, bootstrap, env = _prepare_bootstrap(tmp_path)
    active_scripts = release / "scripts"
    before = _snapshot_tree(active_scripts)
    symlink_target = remote / "outside-helper.py"
    symlink_target.write_text("VALUE = 1\n", encoding="utf-8")
    (remote / "scripts" / "linked-helper.py").symlink_to(symlink_target)

    result = _run_bootstrap(bootstrap, env)

    assert result.returncode != 0
    assert os.readlink(deploy_root / "current") == "releases/20260802-010101"
    assert _snapshot_tree(active_scripts) == before
    assert not list(release.glob(".scripts.bootstrap.*"))


def test_release_bootstrap_rejects_invalid_python_without_touching_active_scripts(
    tmp_path: Path,
) -> None:
    deploy_root, release, remote, bootstrap, env = _prepare_bootstrap(tmp_path)
    active_scripts = release / "scripts"
    before = _snapshot_tree(active_scripts)
    (remote / "scripts" / "broken_helper.py").write_text(
        "def broken(:\n",
        encoding="utf-8",
    )

    result = _run_bootstrap(bootstrap, env)

    assert result.returncode != 0
    assert os.readlink(deploy_root / "current") == "releases/20260802-010101"
    assert _snapshot_tree(active_scripts) == before
    assert not list(release.glob(".scripts.bootstrap.*"))


def test_release_bootstrap_signal_during_staging_preserves_original_bytes(
    tmp_path: Path,
) -> None:
    deploy_root, release, _, bootstrap, env = _prepare_bootstrap(tmp_path)
    active_scripts = release / "scripts"
    before = _snapshot_tree(active_scripts)
    fake_bash = Path(env["PATH"].split(os.pathsep, 1)[0]) / "bash"
    _write_executable(
        fake_bash,
        """#!/bin/bash
set -euo pipefail
if [ "${1:-}" = "-n" ]; then
    kill -TERM "$PPID"
fi
exec /bin/bash "$@"
""",
    )

    result = _run_bootstrap(bootstrap, env)

    assert result.returncode != 0, result.stderr + result.stdout
    assert os.readlink(deploy_root / "current") == "releases/20260802-010101"
    assert _snapshot_tree(active_scripts) == before
    assert not list(release.glob(".scripts.bootstrap.*"))


def test_release_bootstrap_rolls_back_when_new_unit_cannot_load(
    tmp_path: Path,
) -> None:
    deploy_root, release, remote, bootstrap, env = _prepare_bootstrap(tmp_path)
    active_scripts = release / "scripts"
    before = _snapshot_tree(active_scripts)
    shutil.rmtree(remote / "scripts")
    shutil.copytree(ROOT / "scripts", remote / "scripts", symlinks=True)
    (remote / "scripts" / "install" / "state.sh").unlink()

    result = _run_bootstrap(bootstrap, env)

    assert result.returncode != 0
    assert _snapshot_tree(active_scripts) == before
    assert not list(release.glob(".scripts.bootstrap.*"))


def test_explicit_install_refuses_existing_release_layout(tmp_path: Path) -> None:
    root = tmp_path / "deploy"
    release = root / "releases" / "old"
    release.mkdir(parents=True)
    (root / "current").symlink_to("releases/old")
    source = INSTALL.read_text(encoding="utf-8")
    entrypoint = (
        ROOT / "scripts" / "install" / "entrypoint.sh"
    ).read_text(encoding="utf-8")

    assert "guard_install_target_is_fresh() {" in entrypoint
    guard_index = source.index("\nguard_install_target_is_fresh\n")
    migration_index = source.index("run_migration", guard_index)
    assert guard_index < migration_index


def test_fresh_install_cleanup_residue_is_safe_to_rerun(tmp_path: Path) -> None:
    deploy_root = tmp_path / "deploy"
    (deploy_root / "releases").mkdir(parents=True)
    (deploy_root / "shared").mkdir()
    (deploy_root / "shared/.env").write_text(
        "DB_NAME=lumen\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
            log_warn() {{ printf 'WARN:%s\\n' "$*" >&2; }}
            . {shlex.quote(str(INSTALL_ENTRYPOINT))}
            DEPLOY_ROOT={shlex.quote(str(deploy_root))}
            guard_install_target_is_fresh
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "允许安全重跑" in result.stderr

    partial = deploy_root / "releases" / "partial"
    partial.mkdir()
    (partial / "payload").write_text("partial\n", encoding="utf-8")
    rejected = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
            log_warn() {{ :; }}
            . {shlex.quote(str(INSTALL_ENTRYPOINT))}
            DEPLOY_ROOT={shlex.quote(str(deploy_root))}
            guard_install_target_is_fresh
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "仍含 release 内容" in rejected.stderr


def test_formal_install_binds_clean_source_commit_to_release_manifest(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    source_commit = "a" * 40
    matching = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            . {shlex.quote(str(INSTALL_SERVICES))}
            log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
            log_info() {{ :; }}
            lumen_release_manifest_commit() {{ printf '%s\\n' {source_commit}; }}
            RELEASE_DIR={shlex.quote(str(release))}
            INSTALL_SOURCE_COMMIT={source_commit}
            INSTALL_SOURCE_COMMIT_PROOF=git-clean
            verify_install_release_source_commit manifest.json v1.2.3
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert matching.returncode == 0, matching.stderr + matching.stdout
    assert (release / ".manifest-commit").read_text(encoding="utf-8") == (
        f"{source_commit}\n"
    )

    mismatch = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            . {shlex.quote(str(INSTALL_SERVICES))}
            log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
            log_info() {{ :; }}
            lumen_release_manifest_commit() {{ printf '%s\\n' {'b' * 40}; }}
            RELEASE_DIR={shlex.quote(str(release))}
            INSTALL_SOURCE_COMMIT={source_commit}
            INSTALL_SOURCE_COMMIT_PROOF=git-clean
            verify_install_release_source_commit manifest.json v1.2.3
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert mismatch.returncode != 0
    assert "源码 commit 与 release manifest 不一致" in mismatch.stderr


def test_source_commit_proof_ignores_only_the_installer_entry_lock(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Lumen Tests"],
        check=True,
    )
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "initial"],
        check=True,
    )
    (repo / "scripts.lumen-self-update.lock").write_text("", encoding="utf-8")

    shell = f"""
        set -euo pipefail
        . {shlex.quote(str(INSTALL_LAYOUT))}
        ROOT={shlex.quote(str(repo))}
        resolve_install_source_commit
        printf '%s\\n' "$INSTALL_SOURCE_COMMIT_PROOF"
    """
    clean = subprocess.run(
        ["/bin/bash", "-c", shell],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert clean.returncode == 0, clean.stderr + clean.stdout
    assert clean.stdout.strip() == "git-clean"

    (repo / "unexpected.tmp").write_text("dirty\n", encoding="utf-8")
    dirty = subprocess.run(
        ["/bin/bash", "-c", shell],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert dirty.returncode == 0, dirty.stderr + dirty.stdout
    assert dirty.stdout.strip() == "git-dirty"


def test_source_commit_check_precedes_image_digest_acceptance() -> None:
    source = INSTALL_SERVICES.read_text(encoding="utf-8")
    fetch = source.index("lumen_fetch_release_manifest")
    commit = source.index("verify_install_release_source_commit", fetch)
    digest = source.index("lumen_verify_release_manifest_images", commit)

    assert fetch < commit < digest
