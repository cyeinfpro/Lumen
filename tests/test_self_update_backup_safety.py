from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
LUMENCTL = ROOT / "scripts" / "lumenctl.sh"
BACKUP = ROOT / "scripts" / "backup.sh"
DOC = ROOT / "docs" / "docker-full-stack-cutover-plan.md"
COMMIT = "b" * 40
SELF_UPDATE_UNIT = (
    "lib.sh",
    "lib/runtime.sh",
    "lib/locking.sh",
    "lib/container_release.sh",
    "lib/release_layout.sh",
    "release_manifest_guard.py",
    "update_runner.py",
    "restore_runner.py",
)
PROXY_KEYS = (
    "LUMEN_UPDATE_PROXY_URL",
    "LUMEN_HTTP_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
)


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    for key in (*PROXY_KEYS, "LUMEN_SELF_UPDATE_COMMIT", "LUMEN_SELF_UPDATE_REF"):
        env.pop(key, None)
    return env


def run_bash(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env or clean_env(),
        check=False,
    )


def stage_remote_self_update_unit(remote: Path) -> None:
    for relative in SELF_UPDATE_UNIT:
        source = ROOT / "scripts" / relative
        target = remote / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def install_fake_github_curl(fakebin: Path) -> Path:
    fakebin.mkdir(parents=True, exist_ok=True)
    curl = fakebin / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "${TEST_CURL_LOG:?}"
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
case "$url" in
    https://api.github.com/repos/*/commits)
        printf '[{"sha":"%s"}]\\n' "${TEST_COMMIT:?}"
        ;;
    https://raw.githubusercontent.com/*/scripts/*)
        relative="${url#*/scripts/}"
        cp "${TEST_REMOTE_ROOT:?}/${relative}" "${output:?}"
        ;;
    *)
        exit 64
        ;;
esac
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return curl


def github_env(fakebin: Path, remote: Path, curl_log: Path) -> dict[str, str]:
    env = clean_env()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_REPO_URL": "https://github.com/example/Lumen.git",
            "LUMEN_SELF_UPDATE": "1",
            "LUMEN_SELF_UPDATE_BRANCH": "main",
            "TEST_COMMIT": COMMIT,
            "TEST_CURL_LOG": str(curl_log),
            "TEST_REMOTE_ROOT": str(remote),
        }
    )
    return env


def prepare_transaction_case(
    tmp_path: Path,
) -> tuple[Path, Path, Path, dict[str, bytes], dict[str, bytes]]:
    target = tmp_path / "target"
    remote = tmp_path / "remote"
    fakebin = tmp_path / "bin"
    curl_log = tmp_path / "curl.log"
    target.mkdir()
    remote.mkdir()
    originals = {
        "backup.sh": b"#!/usr/bin/env bash\nLOCAL_BACKUP=1\n",
        "restore.sh": b"#!/usr/bin/env bash\nLOCAL_RESTORE=1\n",
    }
    replacements = {
        "backup.sh": b"#!/usr/bin/env bash\nREMOTE_BACKUP=2\n",
        "restore.sh": b"#!/usr/bin/env bash\nREMOTE_RESTORE=2\n",
    }
    for relative, content in originals.items():
        path = target / relative
        path.write_bytes(content)
        path.chmod(0o755)
    for relative, content in replacements.items():
        path = remote / relative
        path.write_bytes(content)
        path.chmod(0o755)
    (target / ".lumen-self-update.files").write_text("old-file\n", encoding="utf-8")
    (target / ".lumen-self-update.source").write_text(f"{'a' * 40}\n", encoding="utf-8")
    (target / ".lumen-self-update.last").write_text("1\n", encoding="utf-8")
    install_fake_github_curl(fakebin)
    env = github_env(fakebin, remote, curl_log)
    env["LUMEN_SELF_UPDATE_COMMIT"] = COMMIT
    return target, remote, fakebin, originals, replacements


def assert_transaction_restored(
    target: Path,
    originals: dict[str, bytes],
) -> None:
    for relative, content in originals.items():
        assert (target / relative).read_bytes() == content
    assert (target / ".lumen-self-update.files").read_text(
        encoding="utf-8"
    ) == "old-file\n"
    assert (target / ".lumen-self-update.source").read_text(
        encoding="utf-8"
    ) == f"{'a' * 40}\n"
    assert (target / ".lumen-self-update.last").read_text(encoding="utf-8") == "1\n"
    assert not list(target.glob(".lumen-self-update.txn.*"))
    assert not list(target.rglob("*.new"))


def test_missing_module_bootstrap_resolves_branch_and_installs_one_commit(
    tmp_path: Path,
) -> None:
    target = tmp_path / "installed" / "scripts"
    remote = tmp_path / "remote"
    fakebin = tmp_path / "bin"
    curl_log = tmp_path / "curl.log"
    target.mkdir(parents=True)
    shutil.copy2(LIB, target / "lib.sh")
    stage_remote_self_update_unit(remote)
    install_fake_github_curl(fakebin)

    result = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(target / "lib.sh"))}
        printf 'result=%s\\n' "$LUMEN_SELF_UPDATE_RESULT"
        printf 'commit=%s\\n' "$LUMEN_SELF_UPDATE_SOURCE_COMMIT"
        type lumen_with_lock >/dev/null
        """,
        env=github_env(fakebin, remote, curl_log),
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "result=ok" in result.stdout
    assert f"commit={COMMIT}" in result.stdout
    for relative in SELF_UPDATE_UNIT:
        assert (target / relative).read_bytes() == (remote / relative).read_bytes()
    assert (target / ".lumen-self-update.source").read_text(
        encoding="utf-8"
    ).strip() == COMMIT

    calls = curl_log.read_text(encoding="utf-8").splitlines()
    assert any(
        "api.github.com/repos/example/Lumen/commits" in call and "sha=main" in call
        for call in calls
    )
    raw_calls = [call for call in calls if "raw.githubusercontent.com" in call]
    assert raw_calls
    assert all(f"/{COMMIT}/scripts/" in call for call in raw_calls)
    assert not any("/main/scripts/" in call for call in raw_calls)


def test_branch_bootstrap_download_failure_leaves_target_unit_untouched(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    remote = tmp_path / "remote"
    fakebin = tmp_path / "bin"
    curl_log = tmp_path / "curl.log"
    target.mkdir()
    local_lib = target / "lib.sh"
    local_bytes = b"#!/usr/bin/env bash\nLOCAL_LIB=1\n"
    local_lib.write_bytes(local_bytes)
    stage_remote_self_update_unit(remote)
    (remote / "lib" / "locking.sh").unlink()
    install_fake_github_curl(fakebin)

    result = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_self_update_scripts_from_github_branch \
            {shlex.quote(str(target))} main 0 lib.sh
        printf 'result=%s\\n' "$LUMEN_SELF_UPDATE_RESULT"
        """,
        env=github_env(fakebin, remote, curl_log),
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "result=failed" in result.stdout
    assert local_lib.read_bytes() == local_bytes
    assert not (target / "lib").exists()
    assert not list(target.glob(".lumen-self-update.*"))


def test_strict_self_update_rejects_main_even_with_expected_commit(
    tmp_path: Path,
) -> None:
    target = tmp_path / "scripts"
    fakebin = tmp_path / "bin"
    curl_called = tmp_path / "curl-called"
    target.mkdir()
    fakebin.mkdir()
    local_script = target / "update.sh"
    local_bytes = b"#!/usr/bin/env bash\nLOCAL_UPDATE=1\n"
    local_script.write_bytes(local_bytes)
    curl = fakebin / "curl"
    curl.write_text(
        f"#!/usr/bin/env bash\ntouch {shlex.quote(str(curl_called))}\nexit 90\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    env = clean_env()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_SELF_UPDATE": "1",
            "LUMEN_SELF_UPDATE_COMMIT": COMMIT,
        }
    )

    result = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_self_update_scripts {shlex.quote(str(target))} main 0 update.sh
        printf 'result=%s\\n' "$LUMEN_SELF_UPDATE_RESULT"
        """,
        env=env,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "result=failed" in result.stdout
    assert local_script.read_bytes() == local_bytes
    assert not curl_called.exists()


def test_self_update_backup_failure_never_starts_replacement(
    tmp_path: Path,
) -> None:
    target, _, _, originals, _ = prepare_transaction_case(tmp_path)
    env = github_env(tmp_path / "bin", tmp_path / "remote", tmp_path / "curl.log")
    env["LUMEN_SELF_UPDATE_COMMIT"] = COMMIT

    result = run_bash(
        f"""
        set -u
        . {shlex.quote(str(LIB))}
        cp() {{
            local last=""
            for last in "$@"; do :; done
            case "$last" in
                *.bak.*) return 71 ;;
            esac
            command cp "$@"
        }}
        lumen_self_update_scripts \
            {shlex.quote(str(target))} {COMMIT} 0 backup.sh restore.sh
        printf 'result=%s\\n' "$LUMEN_SELF_UPDATE_RESULT"
        """,
        env=env,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "result=failed" in result.stdout
    assert "备份失败" in result.stderr
    assert_transaction_restored(target, originals)
    assert not list(target.rglob("*.bak.*"))


def test_self_update_marker_move_failure_rolls_back_entire_unit(
    tmp_path: Path,
) -> None:
    target, _, _, originals, _ = prepare_transaction_case(tmp_path)
    env = github_env(tmp_path / "bin", tmp_path / "remote", tmp_path / "curl.log")
    env["LUMEN_SELF_UPDATE_COMMIT"] = COMMIT

    result = run_bash(
        f"""
        set -u
        . {shlex.quote(str(LIB))}
        failed=0
        mv() {{
            local last=""
            for last in "$@"; do :; done
            case "$last" in
                */.lumen-self-update.source)
                    if [ "$failed" -eq 0 ]; then
                        failed=1
                        return 72
                    fi
                    ;;
            esac
            command mv "$@"
        }}
        lumen_self_update_scripts \
            {shlex.quote(str(target))} {COMMIT} 0 backup.sh restore.sh
        printf 'result=%s\\n' "$LUMEN_SELF_UPDATE_RESULT"
        """,
        env=env,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "result=failed" in result.stdout
    assert "正在恢复全部 scripts 文件" in result.stderr
    assert_transaction_restored(target, originals)


def test_self_update_permission_failure_rolls_back_before_commit(
    tmp_path: Path,
) -> None:
    target, _, _, originals, _ = prepare_transaction_case(tmp_path)
    env = github_env(tmp_path / "bin", tmp_path / "remote", tmp_path / "curl.log")
    env["LUMEN_SELF_UPDATE_COMMIT"] = COMMIT

    result = run_bash(
        f"""
        set -u
        . {shlex.quote(str(LIB))}
        chmod() {{
            case "${{1:-}}|${{2:-}}" in
                0755\\|*/staged/0002) return 73 ;;
            esac
            command chmod "$@"
        }}
        lumen_self_update_scripts \
            {shlex.quote(str(target))} {COMMIT} 0 backup.sh restore.sh
        printf 'result=%s\\n' "$LUMEN_SELF_UPDATE_RESULT"
        """,
        env=env,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "result=failed" in result.stdout
    assert "staging/权限校验失败" in result.stderr
    assert_transaction_restored(target, originals)
    assert not list(target.rglob("*.bak.*"))


def test_self_update_signal_rolls_back_all_replaced_files(
    tmp_path: Path,
) -> None:
    target, _, _, originals, _ = prepare_transaction_case(tmp_path)
    env = github_env(tmp_path / "bin", tmp_path / "remote", tmp_path / "curl.log")
    env["LUMEN_SELF_UPDATE_COMMIT"] = COMMIT

    result = run_bash(
        f"""
        set -u
        . {shlex.quote(str(LIB))}
        moved=0
        mv() {{
            local arg source="" last=""
            for arg in "$@"; do
                source="$last"
                last="$arg"
            done
            case "$source" in
                */staged/*)
                    moved=$((moved + 1))
                    if [ "$moved" -eq 2 ]; then
                        kill -TERM "${{LUMEN_SELF_UPDATE_TRANSACTION_PID:?}}"
                        return 143
                    fi
                    ;;
            esac
            command mv "$@"
        }}
        rc=0
        lumen_self_update_scripts \
            {shlex.quote(str(target))} {COMMIT} 0 backup.sh restore.sh || rc=$?
        printf 'rc=%s result=%s\\n' "$rc" "$LUMEN_SELF_UPDATE_RESULT"
        exit "$rc"
        """,
        env=env,
    )

    assert result.returncode in {-signal.SIGTERM, 143}, result.stderr + result.stdout
    assert "正在恢复全部 scripts 文件" in result.stderr
    assert_transaction_restored(target, originals)


@pytest.mark.parametrize("max_keep", ["0", "000"])
def test_backup_rejects_zero_retention_before_docker_or_deletion(
    tmp_path: Path,
    max_keep: str,
) -> None:
    backup_root = tmp_path / "backup"
    pg = backup_root / "pg" / "20260101-000000.pg.dump.gz"
    redis = backup_root / "redis" / "20260101-000000.redis.tgz"
    pg.parent.mkdir(parents=True)
    redis.parent.mkdir(parents=True)
    pg.write_bytes(b"keep-pg")
    redis.write_bytes(b"keep-redis")
    fakebin = tmp_path / "bin"
    docker_called = tmp_path / "docker-called"
    fakebin.mkdir()
    docker = fakebin / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
touch "${TEST_DOCKER_CALLED:?}"
exit 99
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = clean_env()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "BACKUP_ROOT": str(backup_root),
            "LUMEN_SELF_UPDATE": "0",
            "MAX_KEEP": max_keep,
            "TEST_DOCKER_CALLED": str(docker_called),
        }
    )

    result = subprocess.run(
        ["bash", str(BACKUP)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 2
    assert "MAX_KEEP must be at least 1" in result.stdout
    assert pg.read_bytes() == b"keep-pg"
    assert redis.read_bytes() == b"keep-redis"
    assert not docker_called.exists()


def test_lumenctl_update_defaults_git_pull_and_preserves_explicit_values(
    tmp_path: Path,
) -> None:
    fake_update = tmp_path / "update.sh"
    fake_update.write_text(
        """#!/usr/bin/env bash
printf 'git_pull=<%s> arg=<%s>\\n' "${LUMEN_UPDATE_GIT_PULL-__unset__}" "${1:-}"
""",
        encoding="utf-8",
    )
    fake_update.chmod(0o755)

    result = run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LUMENCTL))}
        lumenctl_resolve_script() {{ printf '%s' {shlex.quote(str(fake_update))}; }}
        detect_os() {{ printf 'macos\\n'; }}

        unset LUMEN_UPDATE_GIT_PULL
        main update-lumen default
        test "${{LUMEN_UPDATE_GIT_PULL+x}}" != x
        LUMEN_UPDATE_GIT_PULL=0 main update-lumen zero
        LUMEN_UPDATE_GIT_PULL=1 main update-lumen one
        LUMEN_UPDATE_GIT_PULL= main update-lumen empty
        """,
        env=clean_env(),
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "git_pull=<1> arg=<default>" in result.stdout
    assert "git_pull=<0> arg=<zero>" in result.stdout
    assert "git_pull=<1> arg=<one>" in result.stdout
    assert "git_pull=<> arg=<empty>" in result.stdout


def test_lumenctl_branch_calls_use_bootstrap_wrapper() -> None:
    text = LUMENCTL.read_text(encoding="utf-8")
    default_start = text.index("lumenctl_maybe_self_update()")
    default_end = text.index("\nmain()", default_start)
    default_section = text[default_start:default_end]
    bootstrap_start = text.index("        bootstrap-scripts)")
    bootstrap_end = text.index("        # Docker compose runtime", bootstrap_start)
    bootstrap_section = text[bootstrap_start:bootstrap_end]

    assert "lumen_self_update_scripts_from_github_branch" in default_section
    assert "lumen_self_update_scripts_from_github_branch" in bootstrap_section
    assert 'lumen_self_update_scripts "${SCRIPT_DIR}"' not in default_section
    assert 'lumen_self_update_scripts "${SCRIPT_DIR}"' not in bootstrap_section


def test_cutover_rollback_edits_root_shared_env() -> None:
    text = DOC.read_text(encoding="utf-8")
    section = text.split("### 18.1", 1)[1].split("### 18.2", 1)[0]

    assert "ROOT=/opt/lumen" in section
    assert 'cd "${ROOT}/current"' in section
    assert '"${ROOT}/shared/.env"' in section
    assert "\ncd current\n" not in section
    assert "' shared/.env" not in section
