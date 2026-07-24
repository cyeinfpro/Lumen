from __future__ import annotations

import gzip
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LUMENCTL = ROOT / "scripts" / "lumenctl.sh"
LIB = ROOT / "scripts" / "lib.sh"
LIB_MODULE_DIR = ROOT / "scripts" / "lib"
LIB_MODULES = sorted(LIB_MODULE_DIR.glob("*.sh"))
SCRIPT_FILES = [
    LIB,
    *LIB_MODULES,
    LUMENCTL,
    ROOT / "scripts" / "install.sh",
    ROOT / "scripts" / "update.sh",
    ROOT / "scripts" / "uninstall.sh",
    ROOT / "scripts" / "backup.sh",
    ROOT / "scripts" / "restore.sh",
]
INSTALL = ROOT / "scripts" / "install.sh"
UPDATE = ROOT / "scripts" / "update.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"
RESTORE = ROOT / "scripts" / "restore.sh"
ADMIN_RELEASE = ROOT / "apps" / "api" / "app" / "routes" / "admin_release.py"


def lib_source_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in (LIB, *LIB_MODULES))


def bash_function_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text)
    assert match is not None, f"{name} not found in {path}"
    return match.group(0)


def script_env() -> dict[str, str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    # Tests must exercise the working tree under test. The real lumenctl entry
    # point self-updates scripts/ from GitHub raw; leaving that enabled lets a
    # test mutate this checkout underneath later assertions.
    env.setdefault("LUMEN_SELF_UPDATE", "0")
    env.setdefault("LUMEN_LUMENCTL_SELF_UPDATE", "0")
    return env


def run_bash(
    script: str, *, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", script],
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        env=script_env(),
        check=False,
    )


def assert_bash_ok(script: str) -> subprocess.CompletedProcess[str]:
    result = run_bash(script)
    assert result.returncode == 0, result.stderr + result.stdout
    return result


def test_operations_scripts_parse_with_bash_n() -> None:
    result = subprocess.run(
        ["bash", "-n", *map(str, SCRIPT_FILES)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_operations_scripts_parse_with_zsh_n() -> None:
    result = subprocess.run(
        ["zsh", "-n", *map(str, SCRIPT_FILES)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_lib_facade_loads_structured_modules_and_stays_below_2000_lines() -> None:
    facade = LIB.read_text(encoding="utf-8")

    assert len(facade.splitlines()) < 2000
    assert {"container_release.sh", "locking.sh", "runtime.sh"} <= {
        path.name for path in LIB_MODULES
    }
    assert "lib/runtime.sh" in facade
    assert "lib/locking.sh" in facade
    assert "lib/container_release.sh" in facade
    assert '. "${_LUMEN_LIB_SCRIPTS_DIR}/${_LUMEN_LIB_MODULE}"' in facade


def test_lib_facade_handles_space_path_set_u_and_post_source_monkeypatch(
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "release with spaces" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(LIB, scripts_dir / "lib.sh")
    shutil.copytree(LIB_MODULE_DIR, scripts_dir / "lib")
    digest_lock = tmp_path / "digest locks" / "images.lock"
    digest = f"sha256:{'b' * 64}"

    result = assert_bash_ok(
        f"""
        set -u
        unset SCRIPT_DIR
        . {shlex.quote(str(scripts_dir / "lib.sh"))}
        type lumen_start_local_runtime >/dev/null
        type lumen_with_lock >/dev/null
        type lumen_verify_release_manifest_images >/dev/null
        lumen_docker() {{
            test "$1" = image
            printf '%s\\n' ghcr.io/example/lumen-api@{digest}
        }}
        LUMEN_IMAGE_DIGEST_LOCK_FILE={shlex.quote(str(digest_lock))}
        lumen_record_image_digest ghcr.io/example/lumen-api:v1.2.3
        grep -Fq 'ghcr.io/example/lumen-api@{digest}' \
            {shlex.quote(str(digest_lock))}
        """
    )

    assert "镜像 digest" in result.stdout


def test_self_update_supports_nested_lib_modules(tmp_path: Path) -> None:
    remote = tmp_path / "remote scripts"
    target = tmp_path / "target scripts"
    fakebin = tmp_path / "fake bin"
    (remote / "lib").mkdir(parents=True)
    target.mkdir()
    fakebin.mkdir()
    files = {
        "lib.sh": "#!/usr/bin/env bash\nREMOTE_FACADE=1\n",
        "lib/runtime.sh": "#!/usr/bin/env bash\nREMOTE_RUNTIME=1\n",
        "lib/locking.sh": "#!/usr/bin/env bash\nREMOTE_LOCKING=1\n",
        "lib/container_release.sh": (
            "#!/usr/bin/env bash\nREMOTE_CONTAINER_RELEASE=1\n"
        ),
        "lib/release_layout.sh": ("#!/usr/bin/env bash\nREMOTE_RELEASE_LAYOUT=1\n"),
        "release_manifest_guard.py": (
            "#!/usr/bin/env python3\nREMOTE_RELEASE_GUARD = 1\n"
        ),
        "update_runner.py": "#!/usr/bin/env python3\nREMOTE_UPDATE_RUNNER = 1\n",
        "restore_runner.py": "#!/usr/bin/env python3\nREMOTE_RESTORE_RUNNER = 1\n",
    }
    for relative, content in files.items():
        path = remote / relative
        path.write_text(content, encoding="utf-8")

    curl = fakebin / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
set -u
url=""
output=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o)
            output="$2"
            shift 2
            ;;
        http*)
            url="$1"
            shift
            ;;
        *)
            shift
            ;;
    esac
done
relative="${url#*/scripts/}"
cp "${TEST_REMOTE_ROOT:?}/${relative}" "${output:?}"
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    (target / ".lumen-self-update.last").write_text(
        f"{int(time.time())}\n",
        encoding="utf-8",
    )

    result = assert_bash_ok(
        f"""
        . {shlex.quote(str(LIB))}
        PATH={shlex.quote(str(fakebin))}:$PATH
        TEST_REMOTE_ROOT={shlex.quote(str(remote))}
        export PATH TEST_REMOTE_ROOT
        LUMEN_SELF_UPDATE=1
        LUMEN_SELF_UPDATE_COMMIT={"a" * 40}
        LUMEN_REPO_URL=https://github.com/example/Lumen.git
        lumen_self_update_scripts {shlex.quote(str(target))} {"a" * 40} 0 lib.sh
        printf 'result=%s\\n' "$LUMEN_SELF_UPDATE_RESULT"
        printf 'changed=%s\\n' "$LUMEN_SELF_UPDATE_CHANGED"
        """
    )

    assert "result=ok" in result.stdout
    for relative, content in files.items():
        assert (target / relative).read_text(encoding="utf-8") == content
        assert relative in result.stdout
    coverage = (target / ".lumen-self-update.files").read_text(encoding="utf-8")
    for helper in (
        "release_manifest_guard.py",
        "update_runner.py",
        "restore_runner.py",
    ):
        assert helper in coverage

    updated_runtime = "#!/usr/bin/env bash\nREMOTE_RUNTIME=2\n"
    (remote / "lib" / "runtime.sh").write_text(updated_runtime, encoding="utf-8")
    update_script = "#!/usr/bin/env bash\nREMOTE_UPDATE=1\n"
    (remote / "update.sh").write_text(update_script, encoding="utf-8")
    (target / "update.sh").write_text(update_script, encoding="utf-8")

    module_update = assert_bash_ok(
        f"""
        . {shlex.quote(str(LIB))}
        PATH={shlex.quote(str(fakebin))}:$PATH
        TEST_REMOTE_ROOT={shlex.quote(str(remote))}
        export PATH TEST_REMOTE_ROOT
        LUMEN_SELF_UPDATE=1
        LUMEN_SELF_UPDATE_COMMIT={"a" * 40}
        LUMEN_REPO_URL=https://github.com/example/Lumen.git
        lumen_self_update_scripts \
            {shlex.quote(str(target))} {"a" * 40} 0 lib.sh update.sh
        printf 'result=%s\\n' "$LUMEN_SELF_UPDATE_RESULT"
        printf 'changed=%s\\n' "$LUMEN_SELF_UPDATE_CHANGED"
        """
    )

    assert "result=ok" in module_update.stdout
    changed_line = next(
        line
        for line in module_update.stdout.splitlines()
        if line.startswith("changed=")
    )
    assert "lib/runtime.sh" in changed_line
    assert "lib.sh" in changed_line
    assert "update.sh" in changed_line
    assert (target / "lib" / "runtime.sh").read_text(
        encoding="utf-8"
    ) == updated_runtime

    bootstrap_scripts = tmp_path / "bootstrap only facade" / "scripts"
    bootstrap_scripts.mkdir(parents=True)
    shutil.copy2(LIB, bootstrap_scripts / "lib.sh")
    bootstrap = assert_bash_ok(
        f"""
        PATH={shlex.quote(str(fakebin))}:$PATH
        TEST_REMOTE_ROOT={shlex.quote(str(remote))}
        export PATH TEST_REMOTE_ROOT
        LUMEN_SELF_UPDATE=1
        LUMEN_SELF_UPDATE_COMMIT={"a" * 40}
        LUMEN_REPO_URL=https://github.com/example/Lumen.git
        . {shlex.quote(str(bootstrap_scripts / "lib.sh"))}
        printf 'runtime=%s\\n' "$REMOTE_RUNTIME"
        printf 'locking=%s\\n' "$REMOTE_LOCKING"
        printf 'container=%s\\n' "$REMOTE_CONTAINER_RELEASE"
        printf 'release_layout=%s\\n' "$REMOTE_RELEASE_LAYOUT"
        """
    )

    assert "runtime=2" in bootstrap.stdout
    assert "locking=1" in bootstrap.stdout
    assert "container=1" in bootstrap.stdout
    assert "release_layout=1" in bootstrap.stdout
    assert (bootstrap_scripts / "lib" / "runtime.sh").is_file()
    assert (bootstrap_scripts / "lib" / "locking.sh").is_file()
    assert (bootstrap_scripts / "lib" / "container_release.sh").is_file()
    assert (bootstrap_scripts / "lib" / "release_layout.sh").is_file()


def test_self_update_rejects_mutable_branch_without_overwriting(
    tmp_path: Path,
) -> None:
    target = tmp_path / "scripts"
    target.mkdir()
    local_script = target / "update.sh"
    local_script.write_text("#!/usr/bin/env bash\nLOCAL=1\n", encoding="utf-8")

    result = assert_bash_ok(
        f"""
        . {shlex.quote(str(LIB))}
        LUMEN_SELF_UPDATE=1
        lumen_self_update_scripts {shlex.quote(str(target))} main 0 update.sh
        printf 'result=%s\\n' "$LUMEN_SELF_UPDATE_RESULT"
        """
    )

    assert "result=failed" in result.stdout
    assert "拒绝从可变 branch 覆盖脚本" in result.stderr
    assert local_script.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\nLOCAL=1\n"
    )


def test_self_update_validation_is_file_type_specific(tmp_path: Path) -> None:
    good_shell = tmp_path / "good.sh"
    good_python = tmp_path / "good.py"
    bad_shell = tmp_path / "bad.sh"
    bad_python = tmp_path / "bad.py"
    unknown = tmp_path / "unknown.txt"
    good_shell.write_text("#!/usr/bin/env bash\nprintf 'ok\\n'\n", encoding="utf-8")
    good_python.write_text(
        "#!/usr/bin/env python3\nVALUE = {'ok': True}\n",
        encoding="utf-8",
    )
    bad_shell.write_text("#!/usr/bin/env bash\nif then\n", encoding="utf-8")
    bad_python.write_text(
        "#!/usr/bin/env python3\nif True print('bad')\n", encoding="utf-8"
    )
    unknown.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

    assert_bash_ok(
        f"""
        . {shlex.quote(str(LIB))}
        lumen_validate_self_update_file good.sh {shlex.quote(str(good_shell))}
        lumen_validate_self_update_file good.py {shlex.quote(str(good_python))}
        ! lumen_validate_self_update_file bad.sh {shlex.quote(str(bad_shell))}
        ! lumen_validate_self_update_file bad.py {shlex.quote(str(bad_python))}
        ! lumen_validate_self_update_file unknown.txt {shlex.quote(str(unknown))}
        """
    )


def test_operations_host_artifact_snapshot_restores_units_and_sbin(
    tmp_path: Path,
) -> None:
    unit_dir = tmp_path / "systemd"
    sbin_dir = tmp_path / "sbin"
    runtime_dir = tmp_path / "no-systemd-runtime"
    snapshot = tmp_path / "snapshot"
    unit_dir.mkdir()
    sbin_dir.mkdir()
    update_path = unit_dir / "lumen-update.path"
    restore_path = unit_dir / "lumen-restore.path"
    storage_script = sbin_dir / "lumen-storage-mount"
    update_path.write_bytes(b"old-update-unit\n")
    storage_script.write_bytes(b"#!/bin/sh\nold-storage\n")

    assert_bash_ok(
        f"""
        . {shlex.quote(str(LIB))}
        lumen_run_as_root() {{ "$@"; }}
        LUMEN_SYSTEMD_UNIT_DIR={shlex.quote(str(unit_dir))}
        LUMEN_SYSTEMD_RUNTIME_DIR={shlex.quote(str(runtime_dir))}
        LUMEN_LOCAL_SBIN_DIR={shlex.quote(str(sbin_dir))}
        lumen_snapshot_operations_host_artifacts {shlex.quote(str(snapshot))}
        printf 'new-update-unit\\n' > {shlex.quote(str(update_path))}
        printf 'new-restore-unit\\n' > {shlex.quote(str(restore_path))}
        printf '#!/bin/sh\\nnew-storage\\n' > {shlex.quote(str(storage_script))}
        lumen_restore_operations_host_artifacts {shlex.quote(str(snapshot))}
        """
    )

    assert update_path.read_bytes() == b"old-update-unit\n"
    assert storage_script.read_bytes() == b"#!/bin/sh\nold-storage\n"
    assert not restore_path.exists()


def test_install_and_update_transactions_include_host_artifact_snapshots() -> None:
    install = INSTALL.read_text(encoding="utf-8")
    update = UPDATE.read_text(encoding="utf-8")
    runtime = (ROOT / "scripts" / "lib" / "runtime.sh").read_text(encoding="utf-8")

    assert "lumen_snapshot_operations_host_artifacts" in runtime
    assert "lumen_restore_operations_host_artifacts" in runtime
    assert "lumen-storage-mount" in runtime
    assert "INSTALL_HOST_ARTIFACT_SNAPSHOT" in install
    assert "lumen_restore_operations_host_artifacts" in install
    assert "UPDATE_HOST_ARTIFACT_SNAPSHOT" in update
    assert "lumen_restore_operations_host_artifacts" in update


def test_image_job_install_copies_all_python_runtime_modules() -> None:
    text = LUMENCTL.read_text(encoding="utf-8")

    for module in (
        "app.py",
        "image_artifacts.py",
        "image_candidates.py",
        "image_url_security.py",
        "job_persistence.py",
        "payload_helpers.py",
        "request_bodies.py",
        "runtime_config.py",
        "upstream_runtime.py",
    ):
        assert f'"${{ROOT}}/image-job/{module}" "${{app_dir}}/{module}"' in text


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_release_bootstrap_updates_only_current_scripts(tmp_path: Path) -> None:
    deploy_root = tmp_path / "deploy"
    release = deploy_root / "releases" / "20260711-010101"
    current_scripts = release / "scripts"
    current_scripts.mkdir(parents=True)
    (release / "apps" / "api").mkdir(parents=True)
    (release / "apps" / "api" / "sentinel.txt").write_text(
        "keep-release-source\n",
        encoding="utf-8",
    )
    (current_scripts / "install.sh").write_text(
        "#!/usr/bin/env bash\nexit 99\n",
        encoding="utf-8",
    )
    (deploy_root / "shared").mkdir()
    (deploy_root / "current").symlink_to("releases/20260711-010101")

    remote = tmp_path / "remote"
    (remote / "scripts" / "lib").mkdir(parents=True)
    (remote / "apps" / "api").mkdir(parents=True)
    exec_log = tmp_path / "exec.log"
    (remote / "scripts" / "install.sh").write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" > "${TEST_EXEC_LOG:?}"\n',
        encoding="utf-8",
    )
    (remote / "scripts" / "lib.sh").write_text(
        "#!/usr/bin/env bash\nREMOTE_LIB=1\n",
        encoding="utf-8",
    )
    (remote / "scripts" / "lib" / "runtime.sh").write_text(
        "#!/usr/bin/env bash\nREMOTE_RUNTIME=1\n",
        encoding="utf-8",
    )
    (remote / "apps" / "api" / "sentinel.txt").write_text(
        "must-not-overlay-release-source\n",
        encoding="utf-8",
    )
    (remote / "REMOTE_ONLY.txt").write_text("no\n", encoding="utf-8")
    for path in (remote / "scripts").rglob("*.sh"):
        path.chmod(0o755)

    bootstrap_dir = tmp_path / "bootstrap"
    bootstrap_dir.mkdir()
    bootstrap_script = bootstrap_dir / "install.sh"
    shutil.copy2(INSTALL, bootstrap_script)

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_git = fakebin / "git"
    fake_git.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" != "clone" ]; then
  exit 2
fi
dest="${@: -1}"
mkdir -p "${dest}"
cp -R "${TEST_REMOTE_ROOT:?}/." "${dest}/"
""",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    env = script_env()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_INSTALL_DIR": str(deploy_root),
            "LUMEN_REPO_URL": "https://github.com/example/Lumen.git",
            "LUMEN_BRANCH": "main",
            "TEST_REMOTE_ROOT": str(remote),
            "TEST_EXEC_LOG": str(exec_log),
        }
    )
    result = subprocess.run(
        ["bash", str(bootstrap_script), "--update"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert exec_log.read_text(encoding="utf-8").strip() == "--update"
    assert (current_scripts / "lib.sh").read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\nREMOTE_LIB=1\n"
    )
    assert (current_scripts / "lib" / "runtime.sh").is_file()
    assert (release / "apps" / "api" / "sentinel.txt").read_text(
        encoding="utf-8"
    ) == "keep-release-source\n"
    assert not (release / "REMOTE_ONLY.txt").exists()


def test_restore_stages_postgres_before_service_stop_and_active_swap() -> None:
    text = RESTORE.read_text(encoding="utf-8")

    validate_idx = text.index("if ! pg_validate_archive_list; then")
    stage_idx = text.index("if ! pg_prepare_staged_restore; then")
    stop_idx = text.index('log "stopping api + worker')
    redis_done_idx = text.index('log "redis restored"')
    promote_idx = text.index("if ! pg_promote_staged_restore; then")

    assert validate_idx < stage_idx < stop_idx < redis_done_idx < promote_idx
    assert "pg_restore --list" in text
    assert 'PG_TEMP_DB="lumen_restore_${TS//-/}_$$"' in text
    assert 'PG_ROLLBACK_DB="lumen_rollback_${TS//-/}_$$"' in text
    assert "ALTER DATABASE $from_ident RENAME TO $to_ident;" in text
    assert "pg_recover_active_from_rollback" in text
    assert "pg_discard_rollback_after_success" in text
    assert "previous active database discarded" in text
    assert "DROP DATABASE IF EXISTS $PG_DB_IDENT" not in text
    assert 'pg_drop_database_if_exists "$PG_DB"' not in text


def test_restore_success_path_drops_postgres_rollback_database() -> None:
    text = RESTORE.read_text(encoding="utf-8")

    promoted_idx = text.index('if ! pg_rename_database "$PG_TEMP_DB" "$PG_DB"; then')
    promote_success_idx = text.index("PG_SWAP_IN_PROGRESS=0", promoted_idx)
    discard_idx = text.index("if ! pg_discard_rollback_after_success; then")
    drop_idx = text.index('pg_drop_database_if_exists "$rollback_db"')
    clear_idx = text.index('PG_ROLLBACK_DB=""', drop_idx)

    assert promote_success_idx < discard_idx
    assert drop_idx < clear_idx
    assert "previous active database retained" not in text


@pytest.mark.parametrize("pg_restore_rc", ["1", "2"])
def test_restore_pg_restore_failure_before_stop_leaves_active_db_unmutated(
    tmp_path: Path,
    pg_restore_rc: str,
) -> None:
    ts = "20260529-010203"
    backup_root = tmp_path / "backup"
    (backup_root / "pg").mkdir(parents=True)
    (backup_root / "redis").mkdir()
    with gzip.open(backup_root / "pg" / f"{ts}.pg.dump.gz", "wb") as fh:
        fh.write(b"fake pg custom archive bytes")

    redis_src = tmp_path / "redis-src"
    redis_src.mkdir()
    (redis_src / "dump.rdb").write_bytes(b"redis")
    with tarfile.open(backup_root / "redis" / f"{ts}.redis.tgz", "w:gz") as tf:
        tf.add(redis_src / "dump.rdb", arcname="dump.rdb")

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    docker_log = tmp_path / "docker.log"
    redis_host = tmp_path / "redis-host"
    redis_host.mkdir()
    fake_docker = fakebin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >> "${TEST_DOCKER_LOG:?}"
if [ "${1:-}" = "exec" ]; then
  shift
  while [ "${1:-}" = "-i" ] || [ "${1:-}" = "-e" ]; do
    if [ "$1" = "-e" ]; then
      shift 2
    else
      shift
    fi
  done
  shift || true
  cmd="${1:-}"
  if [ "$cmd" = "pg_restore" ]; then
    cat >/dev/null
    if printf '%s\\n' "$*" | grep -q -- '--list'; then
      exit 0
    fi
    exit "${TEST_PG_RESTORE_RC:-2}"
  fi
  if [ "$cmd" = "psql" ]; then
    if printf '%s\\n' "$*" | grep -q 'SELECT 1 FROM pg_database'; then
      printf '1\\n'
    fi
    exit 0
  fi
  if [ "$cmd" = "redis-cli" ]; then
    printf 'PONG\\n'
    exit 0
  fi
fi
if [ "${1:-}" = "inspect" ]; then
  printf '%s\\n' "${TEST_REDIS_HOST_DIR:?}"
  exit 0
fi
if [ "${1:-}" = "stop" ] || [ "${1:-}" = "start" ]; then
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = script_env()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "BACKUP_ROOT": str(backup_root),
            "TMPDIR": str(tmp_path / "tmp"),
            "LUMEN_MAINT_ROOT": str(tmp_path / "maint"),
            "LUMEN_BACKUP_RESTORE_LOCKFILE": str(tmp_path / "backup.lock"),
            "DB_USER": "lumen",
            "DB_NAME": "lumen",
            "TEST_DOCKER_LOG": str(docker_log),
            "TEST_REDIS_HOST_DIR": str(redis_host),
            "TEST_PG_RESTORE_RC": pg_restore_rc,
        }
    )
    (tmp_path / "tmp").mkdir()
    (tmp_path / "maint").mkdir()

    result = subprocess.run(
        ["bash", str(RESTORE), ts],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    log = docker_log.read_text(encoding="utf-8")
    assert result.returncode == 7, result.stderr + result.stdout
    assert "pg_restore --list" in log
    assert 'CREATE DATABASE "lumen_restore_20260529010203_' in log
    assert 'DROP DATABASE IF EXISTS "lumen_restore_20260529010203_' in log
    assert "pg_restore -U lumen -d lumen_restore_20260529010203_" in log
    assert "stop lumen-api lumen-worker" not in log
    assert 'DROP DATABASE IF EXISTS "lumen";' not in log
    assert 'ALTER DATABASE "lumen"' not in log
    assert "active database lumen was not modified" in result.stdout


def test_install_script_uses_docker_compose_full_stack() -> None:
    """
    docker cutover: install.sh 不再启动宿主机 uv/npm 运行时，而是 docker compose 全栈。
    断言 docker compose 流程关键字 + 反断言旧的 systemctl restart / uv sync / npm ci。
    """
    text = INSTALL.read_text(encoding="utf-8")
    # Docker compose 全栈关键字
    assert "Docker Compose 全栈版" in text
    assert "docker compose pull" in text
    assert "docker compose v2" in text
    # lib.sh 提供的 compose helper（直接引用或本地降级包装）
    assert "lumen_compose" in text
    # /opt/lumendata 数据根目录在脚本里有显式准备逻辑
    assert "/opt/lumendata" in text
    # 反断言：不再依赖宿主机 uv sync / npm ci / systemctl restart lumen-*
    assert "uv sync" not in text
    assert "npm ci" not in text
    assert "systemctl restart lumen-api" not in text
    assert "systemctl restart lumen-worker" not in text
    assert "systemctl restart lumen-web" not in text


def test_install_bootstrap_defaults_to_menu_not_auto_update() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert 'args=("menu")' in text
    assert "避免脚本一运行就跳过菜单" in text
    assert 'exec bash "${script_path}" "${args[@]}" </dev/tty' in text


def test_install_failure_cleanup_array_length_is_bash_safe() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "${#INSTALL_STARTED_SERVICES[@]:-0}" not in text
    assert "${#INSTALL_STARTED_SERVICES[@]}" in text


def test_install_pull_failure_fallback_to_main_requires_opt_in() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "LUMEN_INSTALL_FALLBACK_MAIN:-0" in text
    assert "回退到 main 后重试一次" in text
    assert 'env_file_set "${shared_env}" LUMEN_IMAGE_TAG "main"' in text
    assert "fallback main 后仍失败" in text
    assert "main 镜像也未发布 → 使用 --build 本地构建" in text
    assert "stable 安装不会自动回退 main" in text


def test_install_bootstrap_failure_does_not_mark_bootstrapped(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    env_file = shared / ".env"
    env_file.write_text("APP_ENV=production\n", encoding="utf-8")
    function = bash_function_source(INSTALL, "run_bootstrap_admin")

    result = assert_bash_ok(
        f"""
        {function}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_NONINTERACTIVE=1
        LUMEN_ADMIN_EMAIL=admin@example.com
        LUMEN_ADMIN_PASSWORD=correct-horse-battery
        emit_step_start() {{ :; }}
        emit_step_done() {{ :; }}
        emit_info() {{ :; }}
        log_info() {{ :; }}
        log_error() {{ printf '%s\\n' "$*" >&2; }}
        _install_compose() {{
            printf 'database unavailable\\n'
            return 42
        }}
        rc=0
        run_bootstrap_admin || rc=$?
        test "$rc" -eq 42
        ! grep -q '^LUMEN_BOOTSTRAPPED=1$' {shlex.quote(str(env_file))}
        """
    )

    assert "未写入 LUMEN_BOOTSTRAPPED" in result.stderr


def test_install_bootstrap_confirmed_existing_user_remains_idempotent(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    env_file = shared / ".env"
    env_file.write_text("APP_ENV=production\n", encoding="utf-8")
    function = bash_function_source(INSTALL, "run_bootstrap_admin")

    assert_bash_ok(
        f"""
        {function}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_NONINTERACTIVE=1
        LUMEN_ADMIN_EMAIL=admin@example.com
        LUMEN_ADMIN_PASSWORD=correct-horse-battery
        emit_step_start() {{ :; }}
        emit_step_done() {{ :; }}
        emit_info() {{ :; }}
        log_info() {{ :; }}
        log_error() {{ printf '%s\\n' "$*" >&2; }}
        _install_compose() {{
            printf 'user_already_exists\\n'
            return 17
        }}
        run_bootstrap_admin
        test "$(grep -c '^LUMEN_BOOTSTRAPPED=1$' {shlex.quote(str(env_file))})" -eq 1
        """
    )


def test_install_ghcr_probe_uses_private_mktemp_and_cleans_up(
    tmp_path: Path,
) -> None:
    temp_dir = tmp_path / "tmp"
    shared = tmp_path / "shared"
    meta = tmp_path / "probe.meta"
    temp_dir.mkdir()
    shared.mkdir()
    (shared / ".env").write_text("", encoding="utf-8")
    function = bash_function_source(INSTALL, "probe_ghcr_image_tag")

    result = assert_bash_ok(
        f"""
        {function}
        SHARED_DIR={shlex.quote(str(shared))}
        TMPDIR={shlex.quote(str(temp_dir))}
        TEST_PROBE_META={shlex.quote(str(meta))}
        INSTALL_IMAGE_TAG_OVERRIDE=""
        INSTALL_BUILD_FLAG=0
        INSTALL_GHCR_PROBE_FILE=""
        env_file_get() {{
            case "$1" in
                LUMEN_IMAGE_REGISTRY) printf 'ghcr.io/cyeinfpro' ;;
                LUMEN_IMAGE_TAG) printf 'latest' ;;
            esac
        }}
        emit_step_start() {{ :; }}
        emit_step_done() {{ :; }}
        log_info() {{ :; }}
        log_warn() {{ :; }}
        env_file_set() {{ :; }}
        curl() {{
            local out=""
            while [ "$#" -gt 0 ]; do
                case "$1" in
                    -o) out="$2"; shift 2 ;;
                    *) shift ;;
                esac
            done
            if mode="$(stat -c '%a' "$out" 2>/dev/null)"; then
                :
            else
                mode="$(stat -f '%Lp' "$out")"
            fi
            printf '%s|%s\\n' "$out" "$mode" > "$TEST_PROBE_META"
            printf '{{"tags":["latest"]}}\\n' > "$out"
            printf '200'
        }}
        probe_ghcr_image_tag
        test -z "$INSTALL_GHCR_PROBE_FILE"
        """
    )

    assert result.returncode == 0
    probe_path, mode = meta.read_text(encoding="utf-8").strip().split("|", 1)
    assert Path(probe_path).parent == temp_dir
    assert mode == "600"
    assert not Path(probe_path).exists()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_install_failure_restores_existing_env_and_release_links(
    tmp_path: Path,
) -> None:
    deploy_root = tmp_path / "deploy"
    data_root = tmp_path / "data"
    current_id = "20260711-030303"
    previous_id = "20260710-030303"
    current = deploy_root / "releases" / current_id
    previous = deploy_root / "releases" / previous_id
    current.mkdir(parents=True)
    previous.mkdir(parents=True)
    (deploy_root / "shared").mkdir()
    (deploy_root / "current").symlink_to(f"releases/{current_id}")
    (deploy_root / "previous").symlink_to(f"releases/{previous_id}")
    env_bytes = (
        f"# existing install env\n"
        f"LUMEN_IMAGE_REGISTRY=example.invalid\n"
        f"LUMEN_IMAGE_TAG=v1.2.44\n"
        f"LUMEN_VERSION=1.2.44\n"
        f"LUMEN_DATA_ROOT={data_root}\n"
        f"LUMEN_DB_ROOT={data_root}\n"
        "DATABASE_URL=postgresql://lumen:secret@postgres:5432/lumen\n"
        "REDIS_URL=redis://redis:6379/0\n"
        "SESSION_SECRET='keep-install-bytes'\n"
    ).encode()
    shared_env = deploy_root / "shared" / ".env"
    shared_env.write_bytes(env_bytes)

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_docker = fakebin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -u
case "$*" in
  "--version")
    printf 'Docker version 26.0.0, build fake\\n'
    exit 0
    ;;
  "compose version"|*"compose version"*|"info")
    exit 0
    ;;
  *"config --images"*)
    printf 'example.invalid/lumen-api:main\\n'
    exit 0
    ;;
esac
if [ "${1:-}" = "pull" ]; then
  exit 42
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_sleep = fakebin / "sleep"
    fake_sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)
    fake_sudo = fakebin / "sudo"
    fake_sudo.write_text(
        """#!/usr/bin/env bash
set -u
[ "${1:-}" = "-n" ] && shift
case "${1:-}" in
  chown) exit 0 ;;
  *) command "$@" ;;
esac
""",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)
    fake_df = fakebin / "df"
    fake_df.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
        "printf '/dev/fake 99999999 1 90000000 1%% /\\n'\n",
        encoding="utf-8",
    )
    fake_df.chmod(0o755)

    env = script_env()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_DEPLOY_ROOT": str(deploy_root),
            "LUMEN_DATA_ROOT": str(data_root),
            "LUMEN_DB_ROOT": str(data_root),
            "LUMEN_NONINTERACTIVE": "1",
            "LUMEN_SELF_UPDATE": "0",
        }
    )
    result = subprocess.run(
        ["bash", str(INSTALL), "--install", "--image-tag=main"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert shared_env.read_bytes() == env_bytes
    assert os.readlink(deploy_root / "current") == f"releases/{current_id}"
    assert os.readlink(deploy_root / "previous") == f"releases/{previous_id}"
    assert sorted(path.name for path in (deploy_root / "releases").iterdir()) == [
        previous_id,
        current_id,
    ]
    assert "shared/.env 已按安装前快照原字节恢复" in result.stderr


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
def test_rerun_install_late_failure_restarts_previously_running_old_services(
    tmp_path: Path,
) -> None:
    deploy_root = tmp_path / "deploy"
    data_root = tmp_path / "data"
    current_id = "20260711-040404"
    previous_id = "20260710-040404"
    current = deploy_root / "releases" / current_id
    previous = deploy_root / "releases" / previous_id
    current.mkdir(parents=True)
    previous.mkdir(parents=True)
    (current / "docker-compose.yml").write_text(
        "services:\n  api:\n    image: example.invalid/lumen-api:${LUMEN_IMAGE_TAG}\n",
        encoding="utf-8",
    )
    (current / "VERSION").write_text("1.2.44\n", encoding="utf-8")
    (current / ".image-tag").write_text("main\n", encoding="utf-8")
    (previous / "VERSION").write_text("1.2.43\n", encoding="utf-8")
    (deploy_root / "shared").mkdir()
    (deploy_root / "current").symlink_to(f"releases/{current_id}")
    (deploy_root / "previous").symlink_to(f"releases/{previous_id}")
    for path in (
        data_root / "postgres",
        data_root / "redis",
        data_root / "storage",
        data_root / "backup",
    ):
        path.mkdir(parents=True)

    env_bytes = (
        f"# existing healthy install\n"
        f"LUMEN_IMAGE_REGISTRY=example.invalid\n"
        f"LUMEN_IMAGE_TAG=main\n"
        f"LUMEN_VERSION=1.2.44\n"
        f"LUMEN_DATA_ROOT={data_root}\n"
        f"LUMEN_DB_ROOT={data_root}\n"
        "APP_ENV=development\n"
        "DATABASE_URL=postgresql+asyncpg://lumen:secret@postgres:5432/lumen\n"
        "REDIS_URL=redis://:redis-secret@redis:6379/0\n"
        "SESSION_SECRET='keep-late-failure-bytes'\n"
        "IMAGE_PROXY_SECRET='keep-image-proxy-secret'\n"
        "BYOK_API_KEY_MASTER_SECRET='keep-byok-secret'\n"
        "TELEGRAM_BOT_SHARED_SECRET='keep-telegram-shared-secret'\n"
        "DB_USER=lumen\n"
        "DB_PASSWORD=secret\n"
        "DB_NAME=lumen\n"
        "REDIS_PASSWORD=redis-secret\n"
        "LUMEN_BOOTSTRAPPED=1\n"
        "WEB_BIND_HOST=127.0.0.1\n"
    ).encode()
    shared_env = deploy_root / "shared" / ".env"
    shared_env.write_bytes(env_bytes)

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = fakebin / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
printf '%s|%s\n' "$PWD" "$*" >> {shlex.quote(str(docker_log))}
case "$*" in
  "--version")
    printf 'Docker version 26.0.0, build fake\n'
    exit 0
    ;;
  "compose version"|*"compose version"*|"info")
    exit 0
    ;;
  *"ps --status running --services"*)
    printf 'postgres\nredis\napi\nworker\nweb\n'
    exit 0
    ;;
  *"config --images"*)
    printf 'example.invalid/lumen-api:main\n'
    exit 0
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_curl = fakebin / "curl"
    fake_curl.write_text("#!/usr/bin/env bash\nexit 22\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    fake_sleep = fakebin / "sleep"
    fake_sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)
    fake_sudo = fakebin / "sudo"
    fake_sudo.write_text(
        """#!/usr/bin/env bash
set -u
[ "${1:-}" = "-n" ] && shift
case "${1:-}" in
  chown) exit 0 ;;
  *) command "$@" ;;
esac
""",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)
    fake_df = fakebin / "df"
    fake_df.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
        "printf '/dev/fake 99999999 1 90000000 1%% /\\n'\n",
        encoding="utf-8",
    )
    fake_df.chmod(0o755)

    env = script_env()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_DEPLOY_ROOT": str(deploy_root),
            "LUMEN_DATA_ROOT": str(data_root),
            "LUMEN_DB_ROOT": str(data_root),
            "LUMEN_NONINTERACTIVE": "1",
            "LUMEN_SELF_UPDATE": "0",
            "LUMEN_POSTGRES_UID": str(os.getuid()),
            "LUMEN_POSTGRES_GID": str(os.getgid()),
            "LUMEN_REDIS_UID": str(os.getuid()),
            "LUMEN_REDIS_GID": str(os.getgid()),
            "LUMEN_APP_UID": str(os.getuid()),
            "LUMEN_APP_GID": str(os.getgid()),
            "LUMEN_APP_STORAGE_GID": str(os.getgid()),
        }
    )
    result = subprocess.run(
        ["bash", str(INSTALL), "--install", "--image-tag=main"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert shared_env.read_bytes() == env_bytes
    assert os.readlink(deploy_root / "current") == f"releases/{current_id}"
    assert os.readlink(deploy_root / "previous") == f"releases/{previous_id}"
    assert sorted(path.name for path in (deploy_root / "releases").iterdir()) == [
        previous_id,
        current_id,
    ]
    log_lines = docker_log.read_text(encoding="utf-8").splitlines()
    restored = [
        line
        for line in log_lines
        if line.startswith(f"{deploy_root / 'current'}|")
        and " up " in f" {line.split('|', 1)[1]} "
        and "--force-recreate" in line
    ]
    assert restored, "\n".join(log_lines)
    assert all(
        service in restored[-1]
        for service in ("postgres", "redis", "api", "worker", "web")
    )
    assert "重新拉起安装前运行中的旧 release 服务" in result.stderr


def test_install_generates_all_required_compose_secrets() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    for key in (
        "DB_PASSWORD",
        "REDIS_PASSWORD",
        "SESSION_SECRET",
        "IMAGE_PROXY_SECRET",
        "BYOK_API_KEY_MASTER_SECRET",
        "TELEGRAM_BOT_SHARED_SECRET",
    ):
        assert f'ensure_env_secret "${{file}}" {key}' in text


def test_update_preflight_matches_byok_dev_fallback_policy() -> None:
    text = UPDATE.read_text(encoding="utf-8")
    assert "shared_app_env_is_development" in text
    assert "for k in DATABASE_URL REDIS_URL SESSION_SECRET; do" in text
    assert 'lumen_env_value BYOK_API_KEY_MASTER_SECRET "${SHARED_ENV}"' in text
    assert 'emit_info preflight byok_secret "dev_fallback_backfilled"' in text
    assert (
        'lumen_set_env_value_in_file "${SHARED_ENV}" BYOK_API_KEY_MASTER_SECRET' in text
    )
    assert 'lumen_env_value IMAGE_PROXY_SECRET "${SHARED_ENV}"' in text
    assert 'lumen_set_env_value_in_file "${SHARED_ENV}" IMAGE_PROXY_SECRET' in text
    assert 'emit_info preflight image_proxy_secret "generated"' in text
    assert "dev|development|local|test)" in text


def test_image_proxy_secret_templates_match_prod_requirement() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert (
        'IMAGE_PROXY_SECRET: "${IMAGE_PROXY_SECRET:?Set IMAGE_PROXY_SECRET in .env}"'
        in compose
    )
    assert 'IMAGE_PROXY_SECRET: "${IMAGE_PROXY_SECRET:-}"' not in compose
    assert "IMAGE_PROXY_SECRET=" in env_example
    assert "# 生成命令：openssl rand -hex 32" in env_example
    assert "| `IMAGE_PROXY_SECRET` | 必填 |" in readme


def test_web_port_defaults_to_loopback_and_public_bind_is_explicit_opt_in() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    public_dns_compose = (ROOT / "docker-compose.public-dns.yml").read_text(
        encoding="utf-8"
    )
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    install = INSTALL.read_text(encoding="utf-8")
    assert '"${WEB_BIND_HOST:-127.0.0.1}:3000:3000"' in compose
    assert "WEB_BIND_HOST=127.0.0.1" in env_example
    assert '"${LUMEN_WORKER_DNS_PRIMARY:-1.1.1.1}"' not in compose
    assert '"${LUMEN_WORKER_DNS_PRIMARY:-1.1.1.1}"' in public_dns_compose
    assert "LUMEN_WEB_BIND_HOST" in install
    assert "LUMEN_EXPOSE_WEB_DIRECTLY" in install
    assert 'env_file_set "${shared_env}" WEB_BIND_HOST "127.0.0.1"' in install
    assert "WEB_BIND_HOST 是旧公开默认值 0.0.0.0" in install


def test_workflow_actions_are_sha_pinned() -> None:
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        for line in text.splitlines():
            match = re.search(r"uses:\s*[^@\s]+@([^\s#]+)", line)
            if match:
                assert re.fullmatch(r"[0-9a-f]{40}", match.group(1)), (
                    f"{workflow} has unpinned action line: {line}"
                )
                assert "#" in line, (
                    f"{workflow} action pin missing upstream tag comment: {line}"
                )


def test_api_service_mounts_release_scripts_for_admin_update() -> None:
    """admin_update preflight uses _discover_scripts_dir() to locate update.sh.

    Containerised lumen-api can't see the release directory unless we mount it,
    so the trigger button reports `missing /opt/lumen/scripts/update.sh`. The
    mount must point at the release's scripts/ (which always travels with each
    release via rsync), and admin_update must run in path-unit trigger mode.
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./scripts:/app/scripts:ro" in compose, (
        "lumen-api container must mount the release scripts directory or "
        "admin_update preflight will fail with `missing update.sh`"
    )
    assert "${LUMEN_UPDATE_ROOT:-/opt/lumen}:/opt/lumen:ro" in compose, (
        "lumen-api must see host release metadata so admin_update reports the "
        "real current version/build instead of 0.0.0/unknown"
    )
    assert 'LUMEN_UPDATE_VIA_TRIGGER: "1"' in compose, (
        "lumen-api needs LUMEN_UPDATE_VIA_TRIGGER=1 to skip systemctl probes "
        "(systemctl is not present inside the api container)"
    )
    assert 'LUMEN_BACKUP_VIA_TRIGGER: "1"' in compose, (
        "lumen-api must trigger host-side backup.service from the container; "
        "running backup.sh inside api lacks Docker CLI/socket access"
    )


def test_update_runner_docs_match_path_unit_contract() -> None:
    path_unit = (ROOT / "deploy" / "systemd" / "lumen-update.path").read_text(
        encoding="utf-8"
    )
    deploy_readme = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")

    assert "PathChanged=/opt/lumendata/backup/.update.trigger" in path_unit
    assert "Unit=lumen-update-runner.service" in path_unit
    assert "/opt/lumendata/backup/.update.trigger" in deploy_readme
    assert "lumen-update.path" in deploy_readme
    assert "lumen-update-trigger.path" not in deploy_readme
    assert ".update-trigger" not in deploy_readme


def test_backup_units_use_release_layout_and_path_trigger() -> None:
    service = (ROOT / "deploy" / "systemd" / "lumen-backup.service").read_text(
        encoding="utf-8"
    )
    path_unit = (ROOT / "deploy" / "systemd" / "lumen-backup.path").read_text(
        encoding="utf-8"
    )

    assert "EnvironmentFile=-/opt/lumen/shared/.env" in service
    assert "ExecStart=/usr/bin/env bash /opt/lumen/current/scripts/backup.sh" in service
    assert "Environment=BACKUP_ROOT=/opt/lumendata/backup" in service
    assert (
        "Environment=LUMEN_BACKUP_RESTORE_LOCKFILE=/opt/lumendata/backup/.backup-restore.lock"
        in service
    )
    assert "Environment=LUMEN_BACKUP_SERVICE_MODE=1" in service
    assert "ExecStartPre=+/bin/sh -c" in service
    assert "LUMEN_BACKUP_LOG_MAX_BYTES" in service
    assert "chgrp lumen-backup /opt/lumen/.lumen-maintenance.lock" in service
    assert "chmod g+rw /opt/lumen/.lumen-maintenance.lock" in service
    assert "StandardOutput=append:/opt/lumendata/backup/.backup.log" in service
    assert ".backup.pending" in service
    assert ".backup.running" in service
    assert "touch /opt/lumendata/backup/.backup.trigger" in service
    assert "NoNewPrivileges=true" in service
    assert "PrivateTmp=true" in service
    assert "ProtectSystem=strict" in service
    assert "ReadWritePaths=/opt/lumendata/backup" in service
    assert "/opt/lumen/.lumen-maintenance.lock" in service
    assert "-/run/docker.sock" in service
    assert "-/var/run/docker.sock" in service
    assert "PathChanged=/opt/lumendata/backup/.backup.trigger" in path_unit
    assert "Unit=lumen-backup.service" in path_unit
    assert "TriggerLimitIntervalSec=30s" in path_unit
    assert "TriggerLimitBurst=3" in path_unit


def test_backup_script_records_service_marker_and_queues_retrigger() -> None:
    text = (ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")

    assert "BACKUP_RUNNING_FILE" in text
    assert "BACKUP_PENDING_FILE" in text
    assert "trigger_fingerprint()" in text
    assert 'LUMEN_BACKUP_SERVICE_MODE:-0}" = "1"' in text
    assert "mark_backup_running" in text
    assert "mark_backup_pending_if_retriggered" in text
    assert "detected another backup trigger while running" in text
    assert '"pg_size":%s' in text
    assert '"${PG_SIZE:-0}"' in text
    assert '"${REDIS_SIZE:-0}"' in text
    assert 'exec 7>"$LOCKFILE"' in text
    assert "redis_bgsave_start()" in text
    assert "wait_for_redis_bgsave()" in text
    assert "rdb_bgsave_in_progress" in text
    assert "Background save already in progress" in text
    assert 'docker_cp_redis "/data/dump.rdb"' in text
    assert "ERROR: redis $label missing" in text
    assert "lumen_try_create_owned_lock_dir" in text
    assert "BACKUP_LOCK_OWNER_TOKEN" in text
    assert "lumen_release_owned_lock_dir" in text


@pytest.mark.parametrize("max_keep", ["-1", "invalid", "1001"])
def test_backup_rejects_unsafe_max_keep_before_docker_or_deletion(
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
        f"#!/usr/bin/env bash\ntouch {shlex.quote(str(docker_called))}\nexit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    env = script_env()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "BACKUP_ROOT": str(backup_root),
            "MAX_KEEP": max_keep,
        }
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "backup.sh")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 2
    assert pg.read_bytes() == b"keep-pg"
    assert redis.read_bytes() == b"keep-redis"
    assert not docker_called.exists()
    assert "MAX_KEEP" in result.stdout


def test_systemd_unit_rendering_uses_ordered_placeholders_for_overlapping_roots() -> (
    None
):
    install = INSTALL.read_text(encoding="utf-8")
    update = UPDATE.read_text(encoding="utf-8")
    migrate = (ROOT / "scripts" / "migrate_to_releases.sh").read_text(encoding="utf-8")

    for text in (install, update, migrate):
        assert "s#/opt/lumendata/backup#__LUMEN_BACKUP_ROOT__#g" in text
        assert "s#/opt/lumendata#__LUMEN_DATA_ROOT__#g" in text
        assert "s#/opt/lumen#__LUMEN_DEPLOY_ROOT__#g" in text
        assert "s#__LUMEN_BACKUP_ROOT__#" in text
        assert "s#__LUMEN_DATA_ROOT__#" in text
        assert "s#__LUMEN_DEPLOY_ROOT__#" in text

    render_section = install[
        install.index("_render_update_runner_units()") : install.index(
            "# ---------------------------------------------------------------------------\n# C."
        )
    ]
    assert "_render_systemd_unit_template" in render_section
    assert "s#/opt/lumen#" not in render_section


def test_install_refreshes_update_runner_units_for_admin_button() -> None:
    """Fresh Docker installs must enable the host watcher used by the panel button."""
    install = INSTALL.read_text(encoding="utf-8")
    update = UPDATE.read_text(encoding="utf-8")
    migrate = (ROOT / "scripts" / "migrate_to_releases.sh").read_text(encoding="utf-8")

    assert "install_update_runner_units" in install
    assert "systemctl enable --now lumen-update.path" in install
    assert "systemctl enable --now lumen-backup.timer" in install
    assert "systemctl enable --now lumen-backup.path" in install
    assert "lumen-restore-runner.service" in install
    assert "systemctl enable --now lumen-restore.path" in install
    assert "LUMEN_BACKUP_ROOT" in install
    assert "__LUMEN_BACKUP_ROOT__" in install
    assert "__LUMEN_DEPLOY_ROOT__" in install
    assert (
        "install_update_runner_units"
        in install[install.rindex("switch_current_symlink") :]
    )

    assert "refresh_update_runner_units" in update
    assert "systemctl enable --now lumen-update.path" in update
    assert "systemctl enable --now lumen-backup.timer" in update
    assert "systemctl enable --now lumen-backup.path" in update
    assert "lumen-restore-runner.service" in update
    assert "systemctl enable --now lumen-restore.path" in update
    assert "__LUMEN_BACKUP_ROOT__" in update
    assert "__LUMEN_DEPLOY_ROOT__" in update
    assert "lumen_install_optional_systemd_unit" in install
    assert "lumen_enable_optional_systemd_unit" in install
    assert "lumen_install_optional_systemd_unit" in update
    assert "lumen_enable_optional_systemd_unit" in update

    assert "systemctl enable --now lumen-update.path" in migrate
    assert "systemctl enable --now lumen-backup.timer" in migrate
    assert "systemctl enable --now lumen-backup.path" in migrate
    assert "lumen-restore-runner.service" in migrate
    assert "systemctl enable --now lumen-restore.path" in migrate
    assert "__LUMEN_BACKUP_ROOT__" in migrate
    assert "__LUMEN_DEPLOY_ROOT__" in migrate


def test_fresh_install_provisions_storage_control_plane_for_container_gid() -> None:
    install = INSTALL.read_text(encoding="utf-8")
    update = UPDATE.read_text(encoding="utf-8")
    migrate = (ROOT / "scripts" / "migrate_to_releases.sh").read_text(encoding="utf-8")
    lumenctl = LUMENCTL.read_text(encoding="utf-8")

    assert "install_storage_control_plane" in install
    assert "LUMEN_LOCAL_SBIN_DIR%/}/lumen-storage-mount" in install
    assert (
        "systemctl enable --now lumen-storage-apply.path lumen-storage-test.path"
        in install
    )
    assert 'storage_gid="${LUMEN_APP_STORAGE_GID:-${LUMEN_APP_GID:-10001}}"' in install
    assert "install -d -m 0770 -o root -g" in install

    for text in (update, migrate, lumenctl):
        assert "LUMEN_APP_STORAGE_GID" in text
        assert "/var/lib/lumen-storage" in text
        assert "0770" in text

    for unit in (
        "lumen-storage-mount.service",
        "lumen-storage-apply.service",
        "lumen-storage-test.service",
    ):
        text = (ROOT / "deploy/systemd" / unit).read_text(encoding="utf-8")
        assert "Environment=LUMEN_STORAGE_TARGET=/opt/lumendata" in text


def test_storage_apply_restarts_all_backup_mount_path_watchers() -> None:
    unit = (ROOT / "deploy/systemd/lumen-storage-apply.service").read_text(
        encoding="utf-8"
    )

    for watcher in (
        "lumen-update.path",
        "lumen-update-warm.path",
        "lumen-backup.path",
        "lumen-restore.path",
    ):
        assert watcher in unit
    assert 'systemctl try-restart "$$unit"' in unit


def test_admin_update_panel_arms_stream_after_trigger_success() -> None:
    """The panel should live-update after a trigger without requiring Refresh status."""
    panel_sources = "\n".join(
        (
            (ROOT / "apps/web/src/app/admin/_panels/AdminUpdatePanel.tsx").read_text(
                encoding="utf-8"
            ),
            (
                ROOT / "apps/web/src/app/admin/_panels/AdminUpdatePanel.helpers.ts"
            ).read_text(encoding="utf-8"),
            (
                ROOT / "apps/web/src/app/admin/_panels/AdminUpdatePanel.hooks.ts"
            ).read_text(encoding="utf-8"),
        )
    )

    assert "updateStreamArmed" in panel_sources
    assert "setUpdateStreamArmed(true)" in panel_sources
    assert "queryClient.setQueryData<AdminUpdateStatusOut | undefined>" in panel_sources
    assert "running: true" in panel_sources
    assert "anyPending(updateRunning, updateStreamArmed, triggering)" in panel_sources


def test_tgbot_service_points_at_api_via_docker_network() -> None:
    """tgbot's lumen_api_base default is 127.0.0.1:8000 — that's the tgbot

    container's own loopback, not lumen-api. The compose file must override
    LUMEN_API_BASE so tgbot resolves the api service through the docker
    network instead of dying with httpx.ConnectError on startup.
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'LUMEN_API_BASE: "http://api:8000"' in compose, (
        "tgbot needs LUMEN_API_BASE pointing at the api service hostname; "
        "config.py default 127.0.0.1:8000 only works for in-process dev"
    )


def test_compose_supports_split_db_root_for_cifs_data_root() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    install = INSTALL.read_text(encoding="utf-8")
    update = UPDATE.read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert (
        "${LUMEN_DB_ROOT:-/opt/lumendata}/postgres:/var/lib/postgresql/data" in compose
    )
    assert "${LUMEN_DB_ROOT:-/opt/lumendata}/redis:/data" in compose
    assert (
        "${LUMEN_DATA_ROOT:-/opt/lumendata}/storage:/opt/lumendata/storage" in compose
    )
    assert "${LUMEN_DATA_ROOT:-/opt/lumendata}/backup:/opt/lumendata/backup" in compose
    assert "LUMEN_DB_ROOT=/opt/lumendata" in env_example
    assert 'user: "${LUMEN_APP_UID:-10001}:${LUMEN_APP_GID:-10001}"' in compose
    assert '- "${LUMEN_APP_STORAGE_GID:-10001}"' in compose
    assert "LUMEN_APP_STORAGE_GID=10001" in env_example
    assert (
        'env_file_set "${shared_env}" LUMEN_DB_ROOT        "${LUMEN_DB_ROOT}"'
        in install
    )
    assert (
        'env_file_set "${shared_env}" LUMEN_APP_STORAGE_GID "${LUMEN_APP_STORAGE_GID}"'
        in install
    )
    assert (
        'LUMEN_DB_ROOT="${INSTALL_DB_ROOT_OVERRIDE:-${LUMEN_DB_ROOT:-${LUMEN_DATA_ROOT}}}"'
        in install
    )
    assert (
        'LUMEN_DB_ROOT="${_LUMEN_UPDATE_INPUT_DB_ROOT:-${shared_db_root:-${LUMEN_DATA_ROOT}}}"'
        in update
    )
    assert 'shared_db_root="$(lumen_env_value LUMEN_DB_ROOT "${SHARED_ENV}"' in update
    assert '"${LUMEN_DB_ROOT}/postgres"' in update
    assert '"${LUMEN_DATA_ROOT}/storage"' in update
    assert "enable_local_build_fallback()" in update
    assert "GHCR 镜像不可用，自动启用本地 build 继续" in update


def test_update_preserves_web_bind_and_proxy_env() -> None:
    update = UPDATE.read_text(encoding="utf-8")
    lib = lib_source_text()

    assert "SCRIPT_ROOT=" in update
    assert 'ROOT="${LUMEN_DEPLOY_ROOT}"' in update
    assert 'LUMEN_REPO_DIR="${SCRIPT_ROOT}"' in update
    assert '[ -d "${candidate}/.git" ]' in update
    assert "detect_repo_source_dir()" in update
    assert '"/root/Lumen"' in update
    assert "sync_repo_to_release" in update
    assert "git archive" in update
    assert "target_tag_fallback" in update
    assert "LUMEN_UPDATE_FALLBACK_MAIN:-0" in update
    assert "stable 通道不会自动回退 main" in update
    assert "fallback main 后 docker compose pull 仍失败" in update
    assert 'if lumen_configure_proxy_env "${SHARED_ENV}"' in update
    assert "config_changed_redeploy" in update
    assert 'emit_info check reason "missing_shared_env"' in update
    assert 'emit_info check reason "target_tag_empty"' in update
    assert (
        'lumen_set_env_value_in_file "${SHARED_ENV}" WEB_BIND_HOST "127.0.0.1"'
        in update
    )
    assert "Web 仅监听本机回环" in update
    assert "WEB_BIND_HOST 是旧公开默认值 0.0.0.0" in update
    assert "LUMEN_EXPOSE_WEB_DIRECTLY" in update
    assert (
        'emit_info check web_bind_host "${CURRENT_WEB_BIND_HOST:-<default>}"' in update
    )
    assert "LUMEN_HTTP_PROXY HTTPS_PROXY HTTP_PROXY" in lib
    assert 'export HTTP_PROXY="${proxy_url}"' in lib
    assert 'export HTTPS_PROXY="${proxy_url}"' in lib
    assert "lumen_verify_image_signature_if_required" in lib
    assert "LUMEN_VERIFY_IMAGE_SIGNATURES=1" in lib
    assert "cosign verify" in lib
    assert "lumen_record_image_digest" in lib
    assert "LUMEN_IMAGE_DIGEST_LOCK_FILE" in lib
    assert "拒绝执行未校验的 docker compose pull" in lib


def test_update_failure_restores_env_bytes_and_removes_staged_release(
    tmp_path: Path,
) -> None:
    deploy_root = tmp_path / "deploy"
    data_root = tmp_path / "data"
    current_id = "20260711-010101"
    previous_id = "20260710-010101"
    current = deploy_root / "releases" / current_id
    previous = deploy_root / "releases" / previous_id
    shutil.copytree(ROOT / "scripts", current / "scripts")
    previous.mkdir(parents=True)
    (current / "docker-compose.yml").write_text(
        "services:\n  api:\n    image: example.invalid/lumen-api:${LUMEN_IMAGE_TAG}\n",
        encoding="utf-8",
    )
    (current / "VERSION").write_text("1.2.44\n", encoding="utf-8")
    (current / ".image-tag").write_text("v1.2.44\n", encoding="utf-8")
    (previous / "VERSION").write_text("1.2.43\n", encoding="utf-8")
    (previous / ".image-tag").write_text("v1.2.43\n", encoding="utf-8")
    (deploy_root / "shared").mkdir(parents=True)
    (deploy_root / "current").symlink_to(f"releases/{current_id}")
    (deploy_root / "previous").symlink_to(f"releases/{previous_id}")
    (current / ".env").symlink_to(deploy_root / "shared" / ".env")
    main_image_root = tmp_path / "main-image"
    shutil.copytree(ROOT / "scripts", main_image_root / "scripts")
    (main_image_root / "deploy").mkdir()
    (main_image_root / "docker-compose.yml").write_text(
        "services:\n  api:\n    image: example.invalid/lumen-api:${LUMEN_IMAGE_TAG}\n",
        encoding="utf-8",
    )
    (main_image_root / "VERSION").write_text("1.2.99\n", encoding="utf-8")
    (main_image_root / "scripts" / "fallback-main-marker").write_text(
        "main-source\n",
        encoding="utf-8",
    )
    for path in (
        data_root / "postgres",
        data_root / "redis",
        data_root / "storage",
        data_root / "backup",
    ):
        path.mkdir(parents=True)

    env_bytes = (
        f"# preserve exact bytes\n"
        f"LUMEN_IMAGE_REGISTRY=example.invalid\n"
        f"LUMEN_IMAGE_TAG=v1.2.44\n"
        f"LUMEN_VERSION=1.2.44\n"
        f"LUMEN_UPDATE_CHANNEL=stable\n"
        f"LUMEN_DATA_ROOT={data_root}\n"
        f"LUMEN_DB_ROOT={data_root}\n"
        "APP_ENV=development\n"
        "DATABASE_URL=postgresql://lumen:secret@postgres:5432/lumen\n"
        "REDIS_URL=redis://redis:6379/0\n"
        "SESSION_SECRET='keep-this-format'\n"
        "DB_USER=lumen\n"
        "DB_PASSWORD=secret\n"
        "DB_NAME=lumen\n"
        "REDIS_PASSWORD=\n"
    ).encode()
    shared_env = deploy_root / "shared" / ".env"
    shared_env.write_bytes(env_bytes)

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_docker = fakebin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -u
args=" $* "
if [ "${1:-}" = "pull" ]; then
  case "${2:-}" in
    *:v1.2.45) exit 42 ;;
    *) exit 0 ;;
  esac
fi
if [ "${1:-}" = "create" ]; then
  printf 'cid-main\\n'
  exit 0
fi
if [ "${1:-}" = "cp" ]; then
  relative="${2#*:/app/}"
  source_path="${TEST_MAIN_IMAGE_ROOT:?}/${relative}"
  if [ ! -e "${source_path}" ]; then
    exit 1
  fi
  cp -R "${source_path}" "$3"
  exit 0
fi
if [ "${1:-}" = "rm" ]; then
  exit 0
fi
case "$*" in
  "info"|*"compose version"*)
    exit 0
    ;;
  *"inspect lumen-api"*)
    exit 1
    ;;
  *"config --images"*)
    printf 'example.invalid/lumen-api:%s\\n' "${LUMEN_IMAGE_TAG:?}"
    exit 0
    ;;
  *"alembic heads"*|*"alembic current"*)
    printf '0043_test_head\\n'
    exit 0
    ;;
esac
if [[ "${args}" == *" up "* && "${args}" == *" worker"* ]]; then
  if [ ! -f "${TEST_DOCKER_FAIL_STATE:?}" ]; then
    : > "${TEST_DOCKER_FAIL_STATE}"
    marker="missing"
    [ -f "${LUMEN_UPDATE_ROOT:?}/current/scripts/fallback-main-marker" ] \
      && marker="$(cat "${LUMEN_UPDATE_ROOT}/current/scripts/fallback-main-marker")"
    version="$(sed -n 's/^LUMEN_VERSION=//p' \
      "${LUMEN_UPDATE_ROOT}/shared/.env" | head -n1)"
    printf '%s|%s\\n' "${marker}" "${version}" \
      > "${TEST_FALLBACK_SOURCE_STATE:?}"
    exit 42
  fi
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_sleep = fakebin / "sleep"
    fake_sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)
    fake_curl = fakebin / "curl"
    fake_curl.write_text("#!/usr/bin/env bash\nexit 22\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    fake_df = fakebin / "df"
    fake_df.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
        "printf '/dev/fake 99999999 1 90000000 1%% /\\n'\n",
        encoding="utf-8",
    )
    fake_df.chmod(0o755)

    env = script_env()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_UPDATE_ROOT": str(deploy_root),
            "LUMEN_DEPLOY_ROOT": str(deploy_root),
            "LUMEN_DATA_ROOT": str(data_root),
            "LUMEN_DB_ROOT": str(data_root),
            "LUMEN_BACKUP_ROOT": str(data_root / "backup"),
            "LUMEN_POSTGRES_UID": str(os.getuid()),
            "LUMEN_POSTGRES_GID": str(os.getgid()),
            "LUMEN_REDIS_UID": str(os.getuid()),
            "LUMEN_REDIS_GID": str(os.getgid()),
            "LUMEN_APP_UID": str(os.getuid()),
            "LUMEN_APP_GID": str(os.getgid()),
            "LUMEN_APP_STORAGE_GID": str(os.getgid()),
            "LUMEN_UPDATE_RESOLVED_TAG": "v1.2.45",
            "LUMEN_UPDATE_FALLBACK_MAIN": "1",
            "LUMEN_UPDATE_FAST_EXPLICIT_PULL": "1",
            "LUMEN_ALLOW_UNVERIFIED_CUSTOM_REGISTRY": "1",
            "LUMEN_UPDATE_SKIP_BACKUP": "1",
            "LUMEN_UPDATE_SELF_UPDATE_SCRIPTS": "0",
            "SKIP_STORAGE_CHECK": "1",
            "TEST_DOCKER_FAIL_STATE": str(tmp_path / "docker-failed-once"),
            "TEST_FALLBACK_SOURCE_STATE": str(tmp_path / "fallback-source-state"),
            "TEST_MAIN_IMAGE_ROOT": str(main_image_root),
        }
    )
    result = subprocess.run(
        ["bash", str(current / "scripts" / "update.sh")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert shared_env.read_bytes() == env_bytes
    assert os.readlink(deploy_root / "current") == f"releases/{current_id}"
    assert os.readlink(deploy_root / "previous") == f"releases/{previous_id}"
    release_ids = sorted(path.name for path in (deploy_root / "releases").iterdir())
    assert release_ids == sorted((current_id, previous_id))
    assert (tmp_path / "docker-failed-once").is_file()
    assert (tmp_path / "fallback-source-state").read_text(
        encoding="utf-8"
    ).strip() == "main-source|1.2.99"
    assert "已用 v1.2.44 回滚成功" in result.stderr
    assert "shared/.env 已按更新前快照原字节恢复" in result.stderr


def test_main_fallback_resyncs_release_source_and_skips_failed_tgbot_manifest() -> None:
    update = UPDATE.read_text(encoding="utf-8")
    install = INSTALL.read_text(encoding="utf-8")

    assert update.count("sync_main_fallback_release") >= 3
    assert "fallback 镜像一致" in update
    assert 'if [ "${TGBOT_IMAGE_READY}" -eq 1 ]; then' in update
    assert 'if [ "${tgbot_image_ready}" -eq 1 ]; then' in install
    assert "跳过 tgbot manifest 校验" in install


def test_release_manifest_python_prerequisite_is_consistent() -> None:
    lib = LIB.read_text(encoding="utf-8")
    install = INSTALL.read_text(encoding="utf-8")
    update = UPDATE.read_text(encoding="utf-8")
    guard = (ROOT / "scripts" / "release_manifest_guard.py").read_text(encoding="utf-8")

    assert "lumen_require_python_min_version()" in lib
    assert "lumen_require_python_min_version python3 3 8" in install
    assert "lumen_require_python_min_version python3 3 8" in update
    assert "Optional[urllib.request.Request]" in guard
    assert "urllib.request.Request | None" not in guard


def test_signature_verification_fails_closed_when_compose_images_cannot_be_enumerated(
    tmp_path: Path,
) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "docker.log"
    fake_docker = fakebin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${TEST_DOCKER_LOG:?}"
has_compose=0
has_config=0
has_images=0
for arg in "$@"; do
  [ "$arg" = "compose" ] && has_compose=1
  [ "$arg" = "config" ] && has_config=1
  [ "$arg" = "--images" ] && has_images=1
done
if [ "$has_compose" = "1" ] && [ "$has_config" = "1" ] && [ "$has_images" = "1" ]; then
  exit 2
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = script_env()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "TEST_DOCKER_LOG": str(log),
            "LUMEN_VERIFY_IMAGE_SIGNATURES": "1",
        }
    )
    result = subprocess.run(
        [
            "bash",
            "-lc",
            f". {shlex.quote(str(LIB))}; lumen_compose_pull_per_image {shlex.quote(str(tmp_path))}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "拒绝执行未校验的 docker compose pull" in result.stderr
    docker_log = log.read_text(encoding="utf-8")
    assert "config --images" in docker_log
    assert "pull" not in docker_log


def test_lumen_configure_proxy_env_reads_shared_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LUMEN_HTTP_PROXY=http://127.0.0.1:7890\n"
        "NO_PROXY=localhost,127.0.0.1,::1,10.0.0.0/8\n",
        encoding="utf-8",
    )

    result = assert_bash_ok(
        f"""
        . {LIB}
        unset LUMEN_UPDATE_PROXY_URL LUMEN_HTTP_PROXY HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NO_PROXY no_proxy
        lumen_configure_proxy_env {shlex.quote(str(env_file))} >/tmp/lumen-proxy-test.out
        printf 'proxy=%s\\n' "$(cat /tmp/lumen-proxy-test.out)"
        printf 'http=%s\\n' "$HTTP_PROXY"
        printf 'https=%s\\n' "$HTTPS_PROXY"
        printf 'update=%s\\n' "$LUMEN_UPDATE_PROXY_URL"
        printf 'no_proxy=%s\\n' "$NO_PROXY"
        """
    )

    assert "proxy=http://127.0.0.1:7890" in result.stdout
    assert "http=http://127.0.0.1:7890" in result.stdout
    assert "https=http://127.0.0.1:7890" in result.stdout
    assert "update=http://127.0.0.1:7890" in result.stdout
    assert "no_proxy=localhost,127.0.0.1,::1,10.0.0.0/8" in result.stdout


def test_update_script_requires_release_layout_and_prepares_new_release() -> None:
    """
    docker cutover: release 布局保留，但 prepare 流程从 git clone 改为 rsync 仓库快照
    + shared/.env symlink，不再走 uv.toml 校验。
    """
    text = (ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    # 仍然要求 release 布局 + shared/.env 复用
    assert 'NEW_RELEASE="${ROOT}/releases/${NEW_ID}"' in text
    assert 'lumen_release_ensure_shared_env "${ROOT}"' in text
    # docker cutover：fetch_release 阶段同步 REPO_DIR -> NEW_RELEASE
    assert "rsync_repo_to_release" in text
    assert "sync_repo_to_release" in text
    assert "git archive" in text
    assert 'git archive --format=tar "${archive_ref}"' in text
    assert (
        'sync_repo_to_release "${REPO_DIR}" "${NEW_RELEASE}" "${RELEASE_SOURCE_REF}"'
        in text
    )
    assert "git checkout --quiet" not in text
    assert (
        'elif [ -n "${CURRENT_RELEASE}" ] && [ -d "${CURRENT_RELEASE}" ]; then' in text
    )
    assert 'REPO_DIR="${CURRENT_RELEASE}"' in text
    assert "非正式/rolling 更新未取得 image source；按显式兼容语义使用当前快照" in text
    assert "正式 release 在无 .git 主机上不能禁用 immutable image source" in text
    assert "--exclude='/releases/'" in text
    assert "--exclude='/shared/'" in text
    # release/.env 是 -> shared/.env 的 symlink，docker compose 自动识别
    assert 'ln -sfn "${SHARED_ENV}" "${NEW_RELEASE}/.env"' in text
    # 切换走 atomic switch helper
    assert 'lumen_release_atomic_switch "${ROOT}" "${NEW_ID}"' in text
    # 不再依赖宿主机 uv 配置 / git clone 流程
    assert "uv.toml" not in text
    assert "lumen_update_ensure_runtime_can_access_path" not in text


def test_update_sources_are_bound_to_release_tags_or_commits() -> None:
    text = UPDATE.read_text(encoding="utf-8")

    assert "LUMEN_UPDATE_SCRIPTS_BRANCH" not in text
    assert "LUMEN_SELF_UPDATE_SOURCE_COMMIT" in text
    assert "LUMEN_UPDATE_GIT_REF 必须是具体 release tag 或 40 位 commit" in text
    assert 'git rev-parse --verify "${GIT_REF}^{commit}"' in text
    assert "git pull --ff-only" not in text
    assert "拒绝从 branch 自更新" in text


def test_release_migration_fails_closed_when_systemd_stop_fails() -> None:
    text = (ROOT / "scripts" / "migrate_to_releases.sh").read_text(encoding="utf-8")

    assert 'if ! systemctl stop "${u}"; then' in text
    assert "stop_failed=1" in text
    assert "拒绝移动部署目录" in text
    assert "失败（忽略，继续迁移）" not in text


def test_env_example_documents_ssh_host_key_trust() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert '"known_hosts_path":"/run/secrets/lumen_known_hosts"' in text
    assert '"host_key_fingerprint":"SHA256:' in text
    assert "建议 0600" in text


def test_compose_db_env_vars_backfilled_from_database_url(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL='postgresql+asyncpg://alice:pa%24%24@localhost:5432/lumen_prod'\n",
        encoding="utf-8",
    )

    result = assert_bash_ok(
        f"""
        . {LIB}
        lumen_ensure_compose_db_env_vars {env_file}
        """
    )

    assert "已从 DATABASE_URL 补全" in result.stderr
    text = env_file.read_text(encoding="utf-8")
    assert "DB_USER=alice" in text
    assert "DB_PASSWORD='pa$$'" in text
    assert "DB_NAME=lumen_prod" in text


def test_compose_db_env_vars_fail_without_database_url(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PUBLIC_BASE_URL=http://localhost:3000\n", encoding="utf-8")

    result = run_bash(
        f"""
        . {LIB}
        lumen_ensure_compose_db_env_vars {env_file}
        """
    )

    assert result.returncode == 1
    assert "缺少 DB_USER/DB_PASSWORD/DB_NAME" in result.stderr
    assert "无法从 DATABASE_URL 推导" in result.stderr


def test_container_url_migration_dry_run_and_apply_are_allowlisted(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql+asyncpg://alice:secret@localhost:5432/lumen",
                "REDIS_URL=redis://:redis-secret@127.0.0.1:6379/0",
                "LUMEN_BACKEND_URL=http://127.0.0.1:8000",
                "LUMEN_API_BASE=http://localhost:8000",
                "PUBLIC_BASE_URL=http://localhost:8000",
                "CORS_ALLOW_ORIGINS=http://localhost:3000",
                "NEXT_PUBLIC_API_BASE=/api",
                "WORKER_METRICS_BIND=127.0.0.1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    dry_run = assert_bash_ok(
        f"""
        . {LIB}
        lumen_migrate_container_urls {env_file} --dry-run
        """
    )
    before = env_file.read_text(encoding="utf-8")
    assert "DATABASE_URL:" in dry_run.stdout
    assert "@postgres:5432/lumen" in dry_run.stdout
    assert before.startswith("DATABASE_URL=postgresql+asyncpg://alice:secret@localhost")

    applied = assert_bash_ok(
        f"""
        . {LIB}
        lumen_migrate_container_urls {env_file} --apply
        """
    )
    after = env_file.read_text(encoding="utf-8")
    assert "backup=" in applied.stdout
    assert "DATABASE_URL=postgresql+asyncpg://alice:secret@postgres:5432/lumen" in after
    assert "REDIS_URL=redis://:redis-secret@redis:6379/0" in after
    assert "LUMEN_BACKEND_URL=http://api:8000" in after
    assert "LUMEN_API_BASE=http://api:8000" in after
    # Browser/CORS fields are intentionally not touched by the migration helper.
    assert "PUBLIC_BASE_URL=http://localhost:8000" in after
    assert "CORS_ALLOW_ORIGINS=http://localhost:3000" in after
    assert "WORKER_METRICS_BIND=127.0.0.1" in after
    backups = list(tmp_path.glob(".env.bak.*"))
    assert backups
    assert backups[0].stat().st_mode & 0o777 == 0o600


def test_container_url_migration_rejects_unclassified_localhost_keys(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://alice:secret@localhost:5432/lumen\n"
        "INTERNAL_CALLBACK_URL=http://127.0.0.1:9000\n",
        encoding="utf-8",
    )

    result = run_bash(
        f"""
        . {LIB}
        lumen_migrate_container_urls {env_file} --dry-run
        """
    )

    assert result.returncode == 1
    assert "INTERNAL_CALLBACK_URL still contains localhost/127.0.0.1" in result.stderr


def test_install_existing_env_container_url_check_defaults_to_dry_run() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "LUMEN_ENV_MIGRATE_CONTAINER_URLS:-dry-run" in text
    assert "apply|--apply)" in text
    assert "migrate-env-apply" in text
    assert "检测到旧 .env 仍需要容器地址迁移" in text
    assert "LUMEN_ENV_MIGRATE_CONTAINER_URLS=apply" in text


def test_release_shared_env_recovers_from_root_env(tmp_path: Path) -> None:
    deploy_root = tmp_path / "lumen"
    (deploy_root / "shared").mkdir(parents=True)
    root_env = deploy_root / ".env"
    root_env.write_text(
        "DB_USER=lumen_app\nDB_PASSWORD='secret'\nDB_NAME=lumen\n", encoding="utf-8"
    )

    result = assert_bash_ok(
        f"""
        . {LIB}
        lumen_release_ensure_shared_env {deploy_root}
        test -f {deploy_root / "shared" / ".env"}
        test -L {root_env}
        """
    )

    assert "shared/.env 缺失，检测到 ROOT/.env" in result.stderr
    assert (deploy_root / "shared" / ".env").read_text(encoding="utf-8") == (
        "DB_USER=lumen_app\nDB_PASSWORD='secret'\nDB_NAME=lumen\n"
    )


def test_release_shared_env_fails_when_no_env_source(tmp_path: Path) -> None:
    deploy_root = tmp_path / "lumen"
    (deploy_root / "shared").mkdir(parents=True)

    result = run_bash(
        f"""
        . {LIB}
        lumen_release_ensure_shared_env {deploy_root}
        """
    )

    assert result.returncode == 1
    assert "shared/.env 缺失" in result.stderr
    assert "未找到可恢复的 ROOT/.env 或 current/.env" in result.stderr


def test_rollback_script_validates_compose_env_before_compose_up() -> None:
    text = ADMIN_RELEASE.read_text(encoding="utf-8")
    assert '. "$ROOT/current/scripts/lib.sh"' in text
    assert 'SHARED_ENV="$ROOT/shared/.env"' in text
    assert 'TARGET_IMAGE_TAG="$(head -n1 "$ROOT/current/.image-tag"' in text
    assert 'lumen_set_image_tag_in_env "$SHARED_ENV" "$TARGET_IMAGE_TAG"' in text
    assert (
        'lumen_set_env_value_in_file "$SHARED_ENV" LUMEN_VERSION "$TARGET_VERSION"'
        in text
    )
    assert 'lumen_ensure_compose_db_env_vars "$ROOT/current/.env"' in text
    assert (
        "compose env validation failed; rollback continues but containers may be stale"
        in text
    )
    assert 'cd "$ROOT/current" && docker compose up -d --wait' in text
    assert "SystemOperationLockService(" in text
    assert "_maintenance_marker_busy()" in text
    assert "await asyncio.to_thread(_start_rollback_subprocess" not in text
    assert "asyncio.to_thread(_list_releases, limit=None)" in text


def _prepare_lumenctl_rollback_layout(
    tmp_path: Path,
) -> tuple[Path, bytes, str, str]:
    deploy_root = tmp_path / "deploy"
    current_id = "20260711-020202"
    previous_id = "20260710-020202"
    current = deploy_root / "releases" / current_id
    previous = deploy_root / "releases" / previous_id
    current.mkdir(parents=True)
    previous.mkdir(parents=True)
    for release, version, tag in (
        (current, "1.2.44", "v1.2.44"),
        (previous, "1.2.43", "v1.2.43"),
    ):
        (release / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        (release / ".image-tag").write_text(f"{tag}\n", encoding="utf-8")
        (release / "docker-compose.yml").write_text(
            "services:\n  api:\n    image: example.invalid/lumen-api:${LUMEN_IMAGE_TAG}\n",
            encoding="utf-8",
        )
    (deploy_root / "shared").mkdir()
    env_bytes = (
        b"# keep formatting\n"
        b"LUMEN_IMAGE_TAG=v1.2.44\n"
        b"LUMEN_VERSION=1.2.44\n"
        b"DATABASE_URL=postgresql://lumen:secret@postgres/lumen\n"
    )
    (deploy_root / "shared" / ".env").write_bytes(env_bytes)
    (deploy_root / "current").symlink_to(f"releases/{current_id}")
    (deploy_root / "previous").symlink_to(f"releases/{previous_id}")
    (deploy_root / "VERSION").symlink_to("current/VERSION")
    return deploy_root, env_bytes, current_id, previous_id


def test_lumenctl_rollback_failure_restores_env_links_and_releases_lock(
    tmp_path: Path,
) -> None:
    deploy_root, env_bytes, current_id, previous_id = _prepare_lumenctl_rollback_layout(
        tmp_path
    )
    result = assert_bash_ok(
        f"""
        . {shlex.quote(str(LUMENCTL))}
        ROOT={shlex.quote(str(deploy_root))}
        LUMEN_DEPLOY_ROOT=$ROOT
        LUMEN_BACKUP_ROOT={shlex.quote(str(tmp_path / "backup"))}
        LUMEN_NONINTERACTIVE=1
        LUMEN_ROLLBACK_PRIVILEGED=1
        detect_os() {{ printf 'macos\\n'; }}
        lumen_require_docker_access() {{ :; }}
        rollback_up_count=0
        lumen_compose_in() {{
            case " $* " in
                *" up "*)
                    rollback_up_count=$((rollback_up_count + 1))
                    [ "$rollback_up_count" -gt 1 ]
                    ;;
                *) return 0 ;;
            esac
        }}
        rollback_rc=0
        lumen_compose_rollback || rollback_rc=$?
        test "$rollback_rc" -ne 0
        test -z "${{LUMEN_LOCK_KIND:-}}"
        test ! -d "$ROOT/.lumen-maintenance.lock.d"
        """
    )

    assert "rollback 失败" in result.stderr
    assert (deploy_root / "shared" / ".env").read_bytes() == env_bytes
    assert os.readlink(deploy_root / "current") == f"releases/{current_id}"
    assert os.readlink(deploy_root / "previous") == f"releases/{previous_id}"


def test_lumenctl_rollback_success_updates_tag_version_and_previous(
    tmp_path: Path,
) -> None:
    deploy_root, _, current_id, previous_id = _prepare_lumenctl_rollback_layout(
        tmp_path
    )
    result = assert_bash_ok(
        f"""
        . {shlex.quote(str(LUMENCTL))}
        ROOT={shlex.quote(str(deploy_root))}
        LUMEN_DEPLOY_ROOT=$ROOT
        LUMEN_BACKUP_ROOT={shlex.quote(str(tmp_path / "backup"))}
        LUMEN_NONINTERACTIVE=1
        LUMEN_ROLLBACK_PRIVILEGED=1
        detect_os() {{ printf 'macos\\n'; }}
        lumen_require_docker_access() {{ :; }}
        lumen_compose_in() {{ return 0; }}
        lumen_compose_rollback
        test -z "${{LUMEN_LOCK_KIND:-}}"
        """
    )

    assert "rollback 目标版本：1.2.43" in result.stdout
    env_file = deploy_root / "shared" / ".env"
    assert subprocess.run(
        [
            "bash",
            "-lc",
            f". {shlex.quote(str(LIB))}; "
            f"lumen_env_value LUMEN_IMAGE_TAG {shlex.quote(str(env_file))}; "
            "printf '\\n'; "
            f"lumen_env_value LUMEN_VERSION {shlex.quote(str(env_file))}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=script_env(),
        check=True,
    ).stdout.splitlines() == ["v1.2.43", "1.2.43"]
    assert os.readlink(deploy_root / "current") == f"releases/{previous_id}"
    assert os.readlink(deploy_root / "previous") == f"releases/{current_id}"


def _strip_shell_comments(text: str) -> str:
    """
    去掉 bash 行内注释（# 开头或行末），用于反断言时只看实际可执行命令。
    简化版：一行内首个未在引号里的 # 之后视为注释；不做 here-doc / 复杂引号解析，
    对脚本顶部 banner 和 inline 注释足够。
    """
    out_lines: list[str] = []
    for line in text.splitlines():
        in_single = False
        in_double = False
        i = 0
        cut_at = len(line)
        while i < len(line):
            ch = line[i]
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                if i == 0 or line[i - 1] in (" ", "\t"):
                    cut_at = i
                    break
            i += 1
        out_lines.append(line[:cut_at].rstrip())
    return "\n".join(out_lines)


def test_update_script_runs_docker_compose_pull_migrate_up_phases() -> None:
    """
    docker cutover: update.sh 改为 docker compose pull -> start_infra -> migrate_db
    -> switch -> restart_services。不再 systemctl restart lumen-* /
    uv sync / npm ci；systemd 只允许刷新一键更新 path runner，不负责业务进程。
    """
    text = UPDATE.read_text(encoding="utf-8")
    # 关键阶段（::lumen-step:: phase=...）必须存在
    assert "emit_start set_image_tag" in text
    assert "emit_start pull_images" in text
    assert "emit_start start_infra" in text
    assert "emit_start migrate_db" in text
    assert "emit_start switch" in text
    assert "emit_start restart_services" in text
    # 阶段输出协议：emit_start/done wrapper 走 lumen_emit_step。原来用
    # "phase=$1"，现在 wrapper 内 local _phase=$1 后用 "phase=${_phase}"
    # （加了 phase 耗时计时逻辑，见 install.sh 同款 wrapper）。
    assert 'lumen_emit_step "phase=${_phase}"' in text
    # docker compose 关键命令
    assert "lumen_compose_in" in text
    assert "--profile migrate run --rm migrate" in text
    assert "up --pull missing -d --wait --force-recreate postgres redis" in text
    assert 'export LUMEN_IMAGE_TAG="${TARGET_TAG}"' in text
    assert 'tag_version="$(semver_from_image_tag "${target_tag}"' in text
    assert 'printf \'%s\\n\' "${TARGET_VERSION}" > "${NEW_RELEASE}/VERSION"' in text
    assert (
        'lumen_set_env_value_in_file "${SHARED_ENV}" LUMEN_VERSION "${TARGET_VERSION}"'
        in text
    )
    assert "image_tag_drift_redeploy" in text
    assert 'ln -sfn current/VERSION "${ROOT}/VERSION"' in text
    assert 'stop -t "${LUMEN_UPDATE_STOP_TIMEOUT:-30}" api worker tgbot' in text
    # restart_services: api 必须最后启动（lumen-api 在跑 update.sh 自身的进度
    # SSE，先重 api 会让前端断流）。形态：for _svc in worker web api; do up -d
    # --pull missing --wait --force-recreate "${_svc}"; done。fast 模式通过
    # compose_up_service helper 加 --no-deps，standard 保留原重建语义。
    assert "for _svc in worker web api" in text
    assert 'compose_up_service "${CURRENT_LINK}" "${_svc}"' in text
    assert "compose_up_service_fast()" in text
    assert "--no-deps" in text
    # release 切换走 atomic switch
    assert 'lumen_release_atomic_switch "${ROOT}" "${NEW_ID}"' in text
    # 反断言：脚本注释里可以提"不再 uv sync / npm ci"，但实际可执行命令必须不包含。
    code = _strip_shell_comments(text)
    assert "systemctl restart lumen-api" not in code
    assert "systemctl restart lumen-worker" not in code
    assert "systemctl restart lumen-web" not in code
    assert "lumen_restart_systemd_units" not in code
    assert "uv sync" not in code
    assert "npm ci" not in code
    assert "npm run build" not in code


def test_update_script_checks_storage_before_mutating_running_release() -> None:
    text = UPDATE.read_text(encoding="utf-8")

    pull_done_idx = text.index("emit_done pull_images 0")
    check_idx = text.index("emit_start check_storage")
    start_infra_idx = text.index("emit_start start_infra")
    migrate_idx = text.index("emit_start migrate_db")
    switch_idx = text.index("emit_start switch")
    stop_idx = text.index('stop -t "${LUMEN_UPDATE_STOP_TIMEOUT:-30}"')

    assert pull_done_idx < check_idx < start_infra_idx < migrate_idx < stop_idx
    assert check_idx < switch_idx
    assert '_storage_target="${LUMEN_DATA_ROOT:-/opt/lumendata}"' in text
    assert 'findmnt -T "${_storage_target}"' in text
    assert "findmnt -T /opt/lumendata" not in _strip_shell_comments(text)


def test_update_script_cleanup_prunes_images_buildx_and_releases() -> None:
    """Each successful update must reclaim disk that builds up over time:

    - dangling layers (existed before, just renamed in v1.0.9 to take an env)
    - **untagged unused images** — old ``:main`` digests left behind by the
      rolling tag move, plus retired ``:v1.0.x`` versions
    - **buildx build cache** — local build paths leave 4 GB+ around if
      never pruned; must clean periodically
    - old release directories (existing keep=3 logic untouched)

    All four are non-blocking — a cleanup hiccup must not flip a successful
    update to fail.
    """
    text = UPDATE.read_text(encoding="utf-8")
    code = _strip_shell_comments(text)
    # Three prune passes; --filter is conditional (helper builds it from env).
    assert "image prune -f" in code
    assert "image prune -a -f" in code, (
        "cleanup must prune untagged unused images so old :main digests "
        "and retired :v1.0.x layers don't accumulate on disk"
    )
    assert "buildx prune -f" in code, (
        "cleanup must prune buildx build cache; it grows unbounded if "
        "LUMEN_UPDATE_BUILD=1 ever runs (or just from compose builds)"
    )
    # Env overlay so operators can lengthen or shorten the buffer if they need
    # different rollback windows on a particular host. Fast mode keeps a 48h
    # rollback buffer by default while still reclaiming older release images.
    assert 'cleanup_images_default="48"' in code
    assert 'cleanup_cache_default="48"' in code
    assert "LUMEN_CLEANUP_DANGLING_HOURS:-${cleanup_dangling_default}" in code
    assert "LUMEN_CLEANUP_IMAGES_HOURS:-${cleanup_images_default}" in code
    assert "LUMEN_CLEANUP_CACHE_HOURS:-${cleanup_cache_default}" in code
    assert "LUMEN_UPDATE_SKIP_DOCKER_CLEANUP" in code
    # Helper that omits --filter when hours==0 so prune actually runs
    # against everything, not "everything older than 0 hours".
    assert "_cleanup_filter_args" in code
    assert 'run_update_cleanup "noop"' in code
    assert 'run_update_cleanup "updated"' in code
    # Each prune failure must warn-not-fail, otherwise a stale CIFS or
    # docker daemon hiccup would mark a perfectly applied update as failed.
    assert code.count("已忽略") >= 4
    # Existing release directory cleanup is preserved.
    assert 'lumen_release_cleanup_old "${ROOT}" "${LUMEN_RELEASE_KEEP:-3}"' in code


def test_update_cleanup_empty_filters_are_bash32_set_u_safe(
    tmp_path: Path,
) -> None:
    source = UPDATE.read_text(encoding="utf-8")
    start = source.index("run_update_cleanup() {")
    end = source.index("\n}\n\n# Trap：", start) + len("\n}\n")
    function = source[start:end]
    docker_log = tmp_path / "docker.log"

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
        set -u
        {function}
        ROOT={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_MODE=standard
        emit_start() {{ :; }}
        emit_info() {{ :; }}
        emit_done() {{ :; }}
        log_info() {{ :; }}
        log_warn() {{ :; }}
        lumen_env_truthy() {{ return 1; }}
        lumen_detect_docker_access() {{ return 0; }}
        lumen_release_cleanup_old() {{ return 0; }}
        lumen_docker() {{
            printf '%s\\n' "$*" >> {shlex.quote(str(docker_log))}
            return 0
        }}
        run_update_cleanup test
        """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert docker_log.read_text(encoding="utf-8").splitlines() == [
        "image prune -f",
        "image prune -a -f",
        "buildx version",
        "buildx prune -f",
    ]


def test_update_script_defaults_to_fast_update_path() -> None:
    text = UPDATE.read_text(encoding="utf-8")
    code = _strip_shell_comments(text)

    assert "LUMEN_UPDATE_MODE:-fast" in code
    assert "LUMEN_UPDATE_SELF_UPDATE_SCRIPTS=0" in code
    assert "LUMEN_UPDATE_FAST_EXPLICIT_PULL:-0" in code
    assert '! lumen_image_tag_is_rolling "${TARGET_TAG}"' in code
    assert "skipped_by_fast_mode" in text
    assert "reuse_healthy" in code
    assert "recreated_for_release_bind_mount" in code
    assert "--no-deps" in code
    assert 'image_prune "skipped_by_fast_mode"' not in code
    assert 'cleanup_images_default="48"' in code


def test_update_script_pulls_tgbot_image_when_telegram_configured() -> None:
    """tgbot in docker-compose.yml lives under profile=tgbot. A bare

    ``docker compose pull`` skips profile-gated services, so the
    ``--profile tgbot up -d tgbot`` in restart_services would re-use the
    locally cached (pre-update) tgbot image and never advance to the new
    GHCR digest. The pull_images phase must explicitly pull tgbot when
    the deployment has TELEGRAM_BOT_TOKEN configured.
    """
    text = UPDATE.read_text(encoding="utf-8")
    code = _strip_shell_comments(text)
    # Guarded by the same env check used by restart_services so tgbot-less
    # deployments don't waste a pull.
    assert 'env_key_present "${SHARED_ENV}" "TELEGRAM_BOT_TOKEN"' in code
    # The actual pull command must include --profile tgbot pull tgbot.
    assert "--profile tgbot pull tgbot" in code, (
        "pull_images must explicitly pull the profile=tgbot image so "
        "restart_services doesn't reuse the cached pre-update digest"
    )
    # Failure is warn-only by default so api/worker/web updates are not blocked
    # by a non-core profile image.
    assert "tgbot pull 失败" in text
    assert "LUMEN_UPDATE_REQUIRE_TGBOT" in code
    assert "跳过 tgbot 更新" in text
    assert 'tgbot_pull "warn_skipped"' not in code


def test_update_script_skips_tag_name_noop_for_rolling_channels() -> None:
    """For rolling GHCR tags, tag-name equality is not enough to noop.

    Every CI push to main re-publishes ``:main`` with a new digest under the
    same tag string; semver aliases like ``v1`` / ``v1.2`` also move when a
    newer release in that range is published. Comparing tag names would always
    declare noop and production would never receive the newer digest. The check
    phase must detect rolling TARGET_TAG values and force the full
    pull/migrate/restart path instead.
    """
    text = UPDATE.read_text(encoding="utf-8")
    code = _strip_shell_comments(text)
    assert "NOOP_BY_TAG_NAME" in code, (
        "expected the check phase to expose a NOOP_BY_TAG_NAME flag so rolling "
        "image tags can override the tag-equality noop"
    )
    assert 'lumen_image_tag_is_rolling "${TARGET_TAG}"' in code
    assert "NOOP_BY_TAG_NAME=0" in code
    assert "target_tag=${TARGET_TAG}" in code
    # Operator-visible action key used by the SSE dashboard to explain why a
    # repeat click on a rolling tag still ran the full update.
    assert "rolling_force_redeploy" in code


def test_update_script_supports_optional_local_build_when_env_set() -> None:
    """
    docker cutover §11.3.2: 默认 pull 优先；LUMEN_UPDATE_BUILD=1 才本地构建。
    """
    text = UPDATE.read_text(encoding="utf-8")
    # build 路径必须由 env 显式开启
    assert "LUMEN_UPDATE_BUILD:-0" in text
    assert "build api worker web" in text
    # build 路径仍走 lumen_compose_in（不直接 systemctl）
    assert 'lumen_compose_in "${NEW_RELEASE}" build api worker web' in text
    assert "LUMEN_UPDATE_BUILD=1 已完成本地 build，跳过远程 pull" in text


@pytest.mark.skip(
    reason="docker cutover: 宿主机 uv 自动安装路径已删除（API/Worker 由 lumen-api / lumen-worker 镜像提供）"
)
def test_update_installs_missing_uv_to_system_path_before_runtime_home() -> None:
    pass


def test_shared_runtime_health_helpers_cover_api_web_worker() -> None:
    text = lib_source_text()
    assert "lumen_check_runtime_health()" in text
    assert "http://127.0.0.1:8000/healthz" in text
    assert "http://127.0.0.1:3000/" in text
    assert "lumen_systemd_unit_active lumen-worker.service" in text
    assert "lumen_start_local_runtime()" in text
    assert 'lumen_tail_runtime_log "Worker"' in text


def test_local_runtime_stops_persisted_pids_before_port_scan() -> None:
    text = lib_source_text()
    start = text.index("lumen_start_local_runtime()")
    persisted = text.index('lumen_stop_persisted_runtime "${root}"', start)
    api_port_scan = text.index('lumen_prepare_port_for_runtime 8000 "API"', start)
    assert persisted < api_port_scan
    assert "LUMEN_RUNTIME_STOP_WAIT_SECONDS:-15" in text[persisted:api_port_scan]


def test_shared_root_resolver_handles_release_current_symlink(tmp_path: Path) -> None:
    deploy_root = tmp_path / "lumen"
    release = deploy_root / "releases" / "20260503010101-abcdef0"
    scripts_dir = release / "scripts"
    scripts_dir.mkdir(parents=True)
    (deploy_root / "current").symlink_to(release)

    result = assert_bash_ok(
        f"""
        . {LIB}
        lumen_resolve_repo_root {deploy_root / "current" / "scripts"}
        """
    )
    assert Path(result.stdout.strip()) == deploy_root


def test_lumenctl_runs_lumen_updates_from_resolved_root_with_sudo_on_linux() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        detect_os() {{ printf 'linux\\n'; }}
        ensure_cmd() {{ :; }}
        bash() {{ printf 'bash:%s\\n' "$*"; }}
        lumen_sudo() {{ printf 'sudo:%s\\n' "$*"; }}
        run_lumen_script update.sh
        """
    )
    if os.geteuid() == 0:
        assert "sudo:" not in result.stdout
        assert f"bash:{ROOT / 'scripts' / 'update.sh'}" in result.stdout
    else:
        # run_lumen_script 通过 lumen_sudo 透传 LUMEN_* 环境变量，命令形如：
        #   sudo:env LUMEN_FOO=... LUMEN_BAR=... bash /path/to/update.sh
        # 旧形式 "sudo:bash <path>" 也接受（无 LUMEN_* env 时）。
        update_path = ROOT / "scripts" / "update.sh"
        assert "sudo:" in result.stdout
        assert (
            f"bash {update_path}" in result.stdout
            or f"bash:{update_path}" in result.stdout
        )


def test_lumenctl_resolves_scripts_from_current_release(tmp_path: Path) -> None:
    deploy_root = tmp_path / "Lumen"
    release = deploy_root / "releases" / "20260503010101-abcdef0"
    scripts_dir = release / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "update.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (deploy_root / "current").symlink_to(release)

    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        ROOT={deploy_root}
        SCRIPT_DIR={deploy_root / "current" / "scripts"}
        detect_os() {{ printf 'linux\\n'; }}
        bash() {{ printf 'bash:%s\\n' "$*"; }}
        lumen_sudo() {{ printf 'sudo:%s\\n' "$*"; }}
        run_lumen_script update.sh
        """
    )

    assert f"{deploy_root / 'current' / 'scripts' / 'update.sh'}" in result.stdout
    assert f"{deploy_root / 'scripts' / 'update.sh'}" not in result.stderr


def test_lumenctl_install_bootstraps_from_github_when_install_script_missing(
    tmp_path: Path,
) -> None:
    deploy_root = tmp_path / "Lumen"
    downloaded = tmp_path / "downloaded-install.sh"
    deploy_root.mkdir()

    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        ROOT={deploy_root}
        SCRIPT_DIR={deploy_root / "scripts"}
        LUMEN_BRANCH=main
        mktemp() {{ printf '%s\\n' {downloaded}; }}
        ensure_cmd() {{ :; }}
        curl() {{
          printf 'curl:%s\\n' "$*"
          printf '#!/usr/bin/env bash\\nexit 0\\n' > "$4"
        }}
        bash() {{ printf 'bash:%s\\n' "$*"; }}
        run_lumen_script install.sh --image-tag=main
        printf 'install_dir:%s\\n' "${{LUMEN_INSTALL_DIR}}"
        """
    )

    assert (
        "raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/install.sh"
        in result.stdout
    )
    assert f"bash:{downloaded} --install --image-tag=main" in result.stdout
    assert f"install_dir:{deploy_root}" in result.stdout


def test_runtime_health_check_fails_when_api_unhealthy() -> None:
    result = run_bash(
        f"""
        . {LIB}
        log_step() {{ :; }}
        sleep() {{ :; }}
        curl() {{
          case "$*" in
            *'127.0.0.1:8000/healthz'*) printf '500' ;;
            *'127.0.0.1:3000/'*) printf '200' ;;
            *) printf '000' ;;
          esac
        }}
        systemctl() {{
          case "$1 $2" in
            "list-unit-files lumen-worker.service") printf 'lumen-worker.service enabled\\n' ;;
            "is-active --quiet") return 0 ;;
            *) return 0 ;;
          esac
        }}
        LUMEN_API_HEALTH_ATTEMPTS=1 LUMEN_WEB_HEALTH_ATTEMPTS=1 lumen_check_runtime_health
        """
    )
    assert result.returncode == 1
    assert "API 健康检查失败" in result.stderr


def test_runtime_health_check_passes_for_api_web_and_worker() -> None:
    result = assert_bash_ok(
        f"""
        . {LIB}
        log_step() {{ :; }}
        sleep() {{ :; }}
        curl() {{
          case "$*" in
            *'127.0.0.1:8000/healthz'*) printf '200' ;;
            *'127.0.0.1:3000/'*) printf '200' ;;
            *) printf '000' ;;
          esac
        }}
        systemctl() {{
          case "$1 $2" in
            "list-unit-files lumen-worker.service") printf 'lumen-worker.service enabled\\n' ;;
            "is-active --quiet") return 0 ;;
            *) return 0 ;;
          esac
        }}
        LUMEN_API_HEALTH_ATTEMPTS=1 LUMEN_WEB_HEALTH_ATTEMPTS=1 lumen_check_runtime_health
        """
    )
    assert "API 健康检查通过" in result.stdout
    assert "Web 健康检查通过" in result.stdout


def test_runtime_dir_check_uses_systemd_service_user(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    storage = tmp_path / "storage"
    backup = tmp_path / "backup"
    env_file.write_text(
        f"STORAGE_ROOT={storage}\nBACKUP_ROOT={backup}\n",
        encoding="utf-8",
    )

    result = assert_bash_ok(
        f"""
        . {LIB}
        systemctl() {{
          case "$1 $2" in
            "list-unit-files lumen-api.service") printf 'lumen-api.service enabled\\n' ;;
            "show -p") printf 'lumen\\n' ;;
            *) return 1 ;;
          esac
        }}
        id() {{
          case "$1 $2" in
            "-gn lumen") printf 'lumen\\n' ;;
            "-un ") printf 'tester\\n' ;;
            *) command id "$@" ;;
          esac
        }}
        lumen_run_as_user() {{ shift; "$@"; }}
        lumen_run_as_root() {{ "$@"; }}
        lumen_ensure_runtime_dirs {env_file}
        test -d {storage}
        test -d {backup / "pg"}
        test -d {backup / "redis"}
        """
    )
    assert "运行用户：lumen:lumen" in result.stdout


def test_lumen_with_lock_falls_back_to_mkdir_when_flock_missing(tmp_path: Path) -> None:
    lock_root = tmp_path / "backup"
    result = assert_bash_ok(
        f"""
        . {LIB}
        command() {{
          if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
            return 1
          fi
          builtin command "$@"
        }}
        LUMEN_BACKUP_ROOT={lock_root}
        lumen_with_lock update-test 30 bash -c \
          'owner="$(find {lock_root / ".lumen-update.lock.d"} -path "*/.owner.*/owner" -type f)" &&
           grep -q "^pid=" "$owner" &&
           grep -q "^start_token=" "$owner" &&
           printf locked'
        test ! -d {lock_root / ".lumen-update.lock.d"}
        """
    )
    assert result.stdout == "locked"


def test_mkdir_lock_detects_reused_pid_without_unsafe_reclamation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    lock_dir = root / ".lumen-maintenance.lock.d"
    root.mkdir()

    result = assert_bash_ok(
        f"""
        . {LIB}
        command() {{
          if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
            return 1
          fi
          builtin command "$@"
        }}
        lumen_pid_start_token() {{ printf 'token-%s\\n' "$1"; }}
        mkdir {lock_dir}
        printf 'pid=%s\\nstart_token=old-token\\nscript=old.sh\\n' "$$" \
          > {lock_dir / "owner"}
        ! lumen_try_acquire_lock {root} new.sh
        test "${{LUMEN_LAST_LOCK_STALE:-0}}" = 1
        test "${{LUMEN_LAST_LOCK_RECLAIMED:-0}}" = 0
        grep -q "^pid=$$" {lock_dir / "owner"}
        grep -q "^start_token=old-token" {lock_dir / "owner"}
        grep -q "^script=old.sh" {lock_dir / "owner"}
        test -d {lock_dir}
        rm -rf {lock_dir}
        """
    )

    assert result.returncode == 0


def test_mkdir_lock_preserves_live_owner_with_matching_start_token(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    lock_dir = root / ".lumen-maintenance.lock.d"
    root.mkdir()

    assert_bash_ok(
        f"""
        . {LIB}
        command() {{
          if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
            return 1
          fi
          builtin command "$@"
        }}
        lumen_pid_start_token() {{ printf 'token-%s\\n' "$1"; }}
        mkdir {lock_dir}
        printf 'pid=%s\\nstart_token=token-%s\\nscript=active.sh\\n' "$$" "$$" \
          > {lock_dir / "owner"}
        ! lumen_try_acquire_lock {root} contender.sh
        grep -q "^script=active.sh" {lock_dir / "owner"}
        rm -rf {lock_dir}
        """
    )


def test_lumen_with_lock_mkdir_busy_returns_operation_busy(tmp_path: Path) -> None:
    lock_root = tmp_path / "backup"
    (lock_root / ".lumen-update.lock.d").mkdir(parents=True)

    result = run_bash(
        f"""
        . {LIB}
        command() {{
          if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
            return 1
          fi
          builtin command "$@"
        }}
        LUMEN_BACKUP_ROOT={lock_root}
        lumen_with_lock update-test 30 true
        """
    )

    assert result.returncode == 75
    assert '"code":"system_operation_busy"' in result.stdout


def test_lumenctl_help_lists_every_documented_command() -> None:
    result = subprocess.run(
        ["bash", str(LUMENCTL), "help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=script_env(),
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    output = result.stdout
    for command in (
        # 旧命令必须保留
        "menu",
        "install-lumen",
        "update-lumen",
        "uninstall-lumen",
        "install-image-job",
        "uninstall-image-job",
        "nginx-scan",
        "nginx-optimize",
        "nginx-lumen",
        "nginx-sub2api",
        "nginx-sub2api-inner",
        "nginx-sub2api-outer",
        "nginx-image-job",
        "help",
        # docker cutover §24 新增的 lifecycle / compose runtime 命令
        "rollback",
        "version",
        "status",
        "logs",
        "start",
        "stop",
        "restart",
        "migrate",
        "bootstrap",
        "backup",
        "restore",
    ):
        assert f"  {command}" in output, f"lumenctl.sh help 缺少子命令：{command}"


def test_install_image_job_persists_required_sidecar_token() -> None:
    source = bash_function_source(LUMENCTL, "install_image_job")

    assert 'env_file="${config_dir}/image-job.env"' in source
    assert "secrets.token_urlsafe(48)" in source
    assert 'as_sudo install -m 0600 "${tmp_env}" "${env_file}"' in source
    assert "EnvironmentFile=${env_file}" in source
    assert "IMAGE_JOB_ALLOW_LEGACY_BEARER_AUTH=1" not in source


def test_lumenctl_menu_accepts_default_exit_without_error() -> None:
    result = subprocess.run(
        ["bash", str(LUMENCTL), "menu"],
        cwd=ROOT,
        input="\n",
        text=True,
        capture_output=True,
        env=script_env(),
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Lumen 一键运维菜单" in result.stdout


def test_lumenctl_direct_commands_dispatch_to_expected_handlers() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        run_lumen_script() {{ printf 'run_lumen_script:%s %s\\n' "$1" "${{*:2}}"; }}
        install_image_job() {{ printf 'install_image_job\\n'; }}
        uninstall_image_job() {{ printf 'uninstall_image_job\\n'; }}
        nginx_scan() {{ printf 'nginx_scan\\n'; }}
        nginx_optimize() {{ printf 'nginx_optimize\\n'; }}
        nginx_lumen_proxy() {{ printf 'nginx_lumen_proxy\\n'; }}
        nginx_sub2api_proxy() {{ printf 'nginx_sub2api_proxy\\n'; }}
        nginx_sub2api_inner_proxy() {{ printf 'nginx_sub2api_inner_proxy\\n'; }}
        nginx_sub2api_outer_proxy() {{ printf 'nginx_sub2api_outer_proxy\\n'; }}
        nginx_image_job_locations() {{ printf 'nginx_image_job_locations\\n'; }}

        main install-lumen
        main update-lumen
        main uninstall-lumen
        main install-image-job
        main uninstall-image-job
        main nginx-scan
        main nginx-optimize
        main nginx-lumen
        main nginx-sub2api
        main nginx-sub2api-inner
        main nginx-sub2api-outer
        main nginx-image-job
        """
    )
    assert result.stdout.splitlines() == [
        "run_lumen_script:install.sh --install",
        "run_lumen_script:update.sh ",
        "run_lumen_script:uninstall.sh ",
        "install_image_job",
        "uninstall_image_job",
        "nginx_scan",
        "nginx_optimize",
        "nginx_lumen_proxy",
        "nginx_sub2api_proxy",
        "nginx_sub2api_inner_proxy",
        "nginx_sub2api_outer_proxy",
        "nginx_image_job_locations",
    ]


def test_lumenctl_interactive_menu_dispatches_all_numbered_options() -> None:
    """菜单分组重构后的项映射（commit P：运行/维护/网络/⚠ 危险）：
    9=nginx_scan, 10=nginx_optimize, 11=install, 12=update,
    15=install_image_job, 16=uninstall_image_job, 17=uninstall。
    本测试只验证这 7 个 dispatch 正确，不覆盖 1-8/13-14（compose/restore/rollback
    需更多 mock）。
    """
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        read_or_default() {{
          local _prompt="$1"
          local default="${{2:-}}"
          local reply=""
          if ! IFS= read -r reply; then
            reply=""
          fi
          printf '%s' "${{reply:-$default}}"
        }}
        run_lumen_script() {{ printf 'run_lumen_script:%s %s\\n' "$1" "${{*:2}}"; }}
        install_image_job() {{ printf 'install_image_job\\n'; }}
        uninstall_image_job() {{ printf 'uninstall_image_job\\n'; }}
        nginx_scan() {{ printf 'nginx_scan\\n'; }}
        nginx_optimize() {{ printf 'nginx_optimize\\n'; }}

        printf '11\\n12\\n17\\n15\\n16\\n9\\n10\\n0\\n' | show_menu
        """
    )
    dispatch_lines = [
        line
        for line in result.stdout.splitlines()
        if line.startswith(
            ("run_lumen_script:", "install_image_job", "uninstall_image_job", "nginx_")
        )
    ]
    assert dispatch_lines == [
        "run_lumen_script:install.sh --install",
        "run_lumen_script:update.sh ",
        "run_lumen_script:uninstall.sh ",
        "install_image_job",
        "uninstall_image_job",
        "nginx_scan",
        "nginx_optimize",
    ]


def test_lumenctl_install_lumen_preserves_explicit_install_flag() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        run_lumen_script() {{ printf 'run_lumen_script:%s %s\\n' "$1" "${{*:2}}"; }}

        main install-lumen --image-tag=main
        main install-lumen --install --image-tag=main
        """
    )

    assert result.stdout.splitlines() == [
        "run_lumen_script:install.sh --install --image-tag=main",
        "run_lumen_script:install.sh --install --image-tag=main",
    ]


def test_lumenctl_install_update_uninstall_smoke_with_fake_docker(
    tmp_path: Path,
) -> None:
    """
    Runs the real lumenctl -> install.sh / update.sh / uninstall.sh entrypoints
    against a temp release layout with fake docker/curl/sudo functions. This
    verifies the one-click control flow without touching the host daemon or /opt.
    """
    sim_root = tmp_path / "sim"
    deploy_root = sim_root / "deploy"
    data_root = sim_root / "data"
    log_dir = sim_root / "logs"
    fakebin = sim_root / "fakebin"
    log_dir.mkdir(parents=True)
    fakebin.mkdir(parents=True)

    (fakebin / "docker").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\\n' "$*" >> "${LOG_DIR}/docker.log"
if [ "$#" -ge 1 ] && [ "$1" = "--version" ]; then
  printf 'Docker version 26.0.0, build fake\\n'
  exit 0
fi
if [ "$#" -ge 2 ] && [ "$1" = "compose" ] && [ "$2" = "version" ]; then
  printf 'Docker Compose version v2.27.0\\n'
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "info" ]; then
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "compose" ]; then
  shift
  [ "${1:-}" = "--ansi=never" ] && shift
  if [ "${1:-}" = "ps" ]; then
    svc="${*: -1}"
    printf 'cid-%s\\n' "${svc}"
    exit 0
  fi
  # Mock alembic heads / current（update.sh migrate_db 阶段会比对）。
  # 真 alembic 会输出 "<rev_id> (head)"；mock 给同 rev_id 让 verify 通过。
  rest="$*"
  case "${rest}" in
    *"alembic heads"*)
      printf '0021_test_head\\n'
      exit 0
      ;;
    *"alembic current"*)
      printf '0021_test_head\\n'
      exit 0
      ;;
  esac
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "inspect" ]; then
  printf 'healthy\\n'
  exit 0
fi
if [ "$#" -ge 2 ] && [ "$1" = "image" ] && [ "$2" = "prune" ]; then
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "ps" ]; then
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "rm" ]; then
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "cp" ]; then
  dest="${*: -1}"
  mkdir -p "$(dirname "${dest}")"
  printf 'redis-dump\\n' > "${dest}"
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "exec" ]; then
  args="$*"
  case "${args}" in
    *redis-cli*LASTSAVE*)
      sed -n 's/^lastsave=//p' "${TEST_DOCKER_STATE}" | tail -n1
      exit 0
      ;;
    *redis-cli*BGSAVE*)
      printf 'lastsave=2\\n' > "${TEST_DOCKER_STATE}"
      printf 'Background saving started\\n'
      exit 0
      ;;
    *redis-cli*PING*)
      # backup.sh 在 BGSAVE 前会做 PING 预检（识别 AUTH 错误）；mock 必须返回 PONG
      printf 'PONG\\n'
      exit 0
      ;;
    *redis-cli*)
      printf 'OK\\n'
      exit 0
      ;;
    *pg_dump*)
      printf 'fake pg dump\\n'
      exit 0
      ;;
  esac
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    (fakebin / "curl").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
original="$*"
printf 'curl %s\\n' "${original}" >> "${LOG_DIR}/curl.log"
case "${original}" in
  *api.github.com/repos/cyeinfpro/Lumen/releases/latest*)
    printf '{"tag_name":"main"}\\n'
    exit 0
    ;;
  *ghcr.io/token*)
    printf '{"token":"fake"}\\n'
    exit 0
    ;;
  *raw.githubusercontent.com/*)
    # self_update_scripts 的拉取：在测试环境用 404 让 self_update softfail，
    # 走"继续用本地脚本"路径（避免把 mock 默认的 GHCR JSON 写进 update.sh 等）
    exit 22
    ;;
esac
out=""
write_code=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o)
      out="$2"
      shift 2
      ;;
    -w)
      write_code="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
if [ -n "${out}" ] && [ "${out}" != "/dev/null" ]; then
  printf '{"tags":["latest","main","old"]}\\n' > "${out}"
fi
if [ -n "${write_code}" ]; then
  printf '200'
fi
exit 0
""",
        encoding="utf-8",
    )
    (fakebin / "sudo").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
[ "${1:-}" = "-n" ] && shift
unset LUMEN_UPDATE_GIT_PULL
if [ "${1:-}" = "-u" ]; then
  shift 2
fi
case "${1:-}" in
  chown)
    exit 0
    ;;
  chmod)
    shift
    command chmod "$@" 2>/dev/null || true
    exit 0
    ;;
  mkdir|rm|ln|mv|cp)
    command "$@"
    ;;
  docker)
    shift
    docker "$@"
    ;;
  *)
    command "$@"
    ;;
esac
""",
        encoding="utf-8",
    )
    (fakebin / "systemctl").write_text(
        "#!/usr/bin/env bash\nexit 1\n", encoding="utf-8"
    )
    (fakebin / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (fakebin / "uname").write_text(
        "#!/usr/bin/env bash\nprintf 'Linux\\n'\n", encoding="utf-8"
    )
    for path in fakebin.iterdir():
        path.chmod(0o755)

    script = f"""
    set -euo pipefail
    SIM_ROOT={shlex.quote(str(sim_root))}
    DEPLOY_ROOT={shlex.quote(str(deploy_root))}
    DATA_ROOT={shlex.quote(str(data_root))}
    LOG_DIR={shlex.quote(str(log_dir))}
    export LOG_DIR
    : > "${{LOG_DIR}}/docker.log"
    : > "${{LOG_DIR}}/curl.log"
    export TEST_DOCKER_STATE="${{SIM_ROOT}}/docker-state"
    mkdir -p "${{SIM_ROOT}}"
    printf 'lastsave=1\\n' > "${{TEST_DOCKER_STATE}}"
    export PATH={shlex.quote(str(fakebin))}:$PATH

    export LUMEN_DEPLOY_ROOT="${{DEPLOY_ROOT}}"
    export LUMEN_DATA_ROOT="${{DATA_ROOT}}"
    export LUMEN_NONINTERACTIVE=1
    export LUMEN_ADMIN_EMAIL=admin@example.com
    export LUMEN_ADMIN_PASSWORD=password123456
    export LUMEN_HEALTH_COMPOSE_ATTEMPTS=1
    export LUMEN_HEALTH_COMPOSE_INTERVAL=1
    export LUMEN_RELEASE_KEEP=3
    export LUMEN_ALLOW_BYOK_KEY_GEN=1
    export LUMEN_BACKUP_RESTORE_LOCKFILE="${{DATA_ROOT}}/backup/backup-restore.lock"
    export LUMEN_BACKUP_ROOT="${{DATA_ROOT}}/backup"
    # update.sh 的 check_storage phase 检查 LUMEN_DATA_ROOT 是否挂载；CI 临时目录
    # 跟测试无关，跳过避免 false fail。
    export SKIP_STORAGE_CHECK=1
    # update.sh 的 image-extract fallback (try_image_extract_release) 会真去
    # docker pull GHCR；CI runner 里那个 image 真的能拉到，会让本测试本意的
    # "host 不是 git repo → fallback 到当前快照" 路径走不到。这里强制禁用，
    # 让测试只验证 legacy snapshot-only 行为。
    export LUMEN_UPDATE_DISABLE_IMAGE_EXTRACT=1
    # lumenctl 入口会触发 lumen_self_update_scripts 从 GitHub raw 拉最新 scripts。
    # 紧贴 release 之后跑（< 5 分钟）会撞上 raw.githubusercontent 缓存，把 install
    # 阶段 rsync 进去的当前 commit 的 update.sh 替换成上一个 commit 的版本，导致
    # log/grep 断言失败。CI 测试本来就只想验证当前 working tree 的脚本，禁用 self-update。
    export LUMEN_SELF_UPDATE=0

    bash scripts/lumenctl.sh install-lumen --image-tag=old > "${{LOG_DIR}}/install.out" 2> "${{LOG_DIR}}/install.err"
    test -L "${{DEPLOY_ROOT}}/current"
    test -f "${{DEPLOY_ROOT}}/current/docker-compose.yml"
    test -f "${{DEPLOY_ROOT}}/shared/.env"
    grep -q '^LUMEN_IMAGE_TAG=old$' "${{DEPLOY_ROOT}}/shared/.env"

    if grep -q '^LUMEN_UPDATE_CHANNEL=' "${{DEPLOY_ROOT}}/shared/.env"; then
      sed -i.bak 's/^LUMEN_UPDATE_CHANNEL=.*/LUMEN_UPDATE_CHANNEL=main/' "${{DEPLOY_ROOT}}/shared/.env"
      rm -f "${{DEPLOY_ROOT}}/shared/.env.bak"
    else
      printf 'LUMEN_UPDATE_CHANNEL=main\\n' >> "${{DEPLOY_ROOT}}/shared/.env"
    fi

    LUMEN_UPDATE_GIT_PULL=1 bash "${{DEPLOY_ROOT}}/current/scripts/lumenctl.sh" update-lumen > "${{LOG_DIR}}/update.out" 2> "${{LOG_DIR}}/update.err"
    test -L "${{DEPLOY_ROOT}}/current"
    test -f "${{DEPLOY_ROOT}}/current/docker-compose.yml"
    grep -q '^LUMEN_IMAGE_TAG=main$' "${{DEPLOY_ROOT}}/shared/.env"
    test "$(find "${{DEPLOY_ROOT}}/releases" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" -ge 2
    grep -q '非正式/rolling 更新未取得 image source；按显式兼容语义使用当前快照' "${{LOG_DIR}}/update.err"
    grep -q 'phase=migrate_db status=done' "${{LOG_DIR}}/update.out"
    grep -q 'phase=restart_services status=done' "${{LOG_DIR}}/update.out"
    grep -q 'phase=health_check status=done' "${{LOG_DIR}}/update.out"

    LUMEN_UNINSTALL_NONINTERACTIVE=1 bash "${{DEPLOY_ROOT}}/current/scripts/lumenctl.sh" uninstall-lumen > "${{LOG_DIR}}/uninstall.out" 2> "${{LOG_DIR}}/uninstall.err"
    grep -q '卸载总结' "${{LOG_DIR}}/uninstall.out"
    grep -q '已 docker compose down 主栈' "${{LOG_DIR}}/uninstall.out"
    """

    result = run_bash(script)

    def _maybe_log(name: str) -> str:
        path = log_dir / name
        if not path.exists():
            return f"\n{name}: <missing>\n"
        return f"\n{name}:\n" + path.read_text(encoding="utf-8", errors="replace")

    assert result.returncode == 0, (
        result.stderr
        + result.stdout
        + _maybe_log("install.out")
        + _maybe_log("install.err")
        + _maybe_log("update.out")
        + _maybe_log("update.err")
        + _maybe_log("uninstall.out")
        + _maybe_log("uninstall.err")
        + _maybe_log("docker.log")
    )


def test_lumenctl_validators_reject_unsafe_values() -> None:
    assert_bash_ok(
        f"""
        . {LUMENCTL}
        validate_domain_list domain 'example.com *.example.com _'
        ! validate_domain_list domain 'example.com;'
        ! validate_domain_list domain 'bad$name'
        ! validate_domain_list domain '*'
        validate_tcp_port port 1
        validate_tcp_port port 65535
        ! validate_tcp_port port 0
        ! validate_tcp_port port 65536
        validate_url_like url http://127.0.0.1:8081
        ! validate_url_like url ftp://127.0.0.1
        validate_absolute_path path /opt/image-job
        ! validate_absolute_path path relative/path
        validate_service_user_name user image-job
        validate_service_user_name user root
        ! validate_service_user_name user 'bad user'
        validate_python_command py python3
        validate_python_command py /usr/bin/python3
        ! validate_python_command py ../python3
        """
    )


def test_probe_sub2api_upstream_accepts_openai_compatible_http_status() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        ensure_cmd() {{ :; }}
        curl() {{
          printf '401'
        }}
        probe_sub2api_upstream http://10.0.0.8:18080
        """
    )
    assert "http://10.0.0.8:18080/v1/models" in result.stdout
    assert "sub2api/OpenAI 兼容端点探测通过" in result.stdout


def test_probe_sub2api_upstream_uses_health_as_reachability_fallback() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        ensure_cmd() {{ :; }}
        curl() {{
          case "$*" in
            *'/health'*) printf '200' ;;
            *) printf '000' ;;
          esac
        }}
        probe_sub2api_upstream https://sub2api.example.com
        """
    )
    assert "https://sub2api.example.com/health" in result.stderr
    assert "请确认它是 sub2api/OpenAI 兼容服务" in result.stderr


def test_probe_sub2api_upstream_fails_when_unreachable() -> None:
    result = run_bash(
        f"""
        . {LUMENCTL}
        ensure_cmd() {{ :; }}
        curl() {{
          printf '000'
        }}
        probe_sub2api_upstream http://127.0.0.1:19091
        """
    )
    assert result.returncode == 1
    assert "无法连接 sub2api/OpenAI 兼容上游" in result.stderr


def test_lumen_nginx_config_contains_sse_api_and_security_defaults(
    tmp_path: Path,
) -> None:
    out = tmp_path / "lumen.conf"
    assert_bash_ok(
        f"""
        . {LUMENCTL}
        LUMEN_HSTS_ENABLED=true
        LUMEN_HSTS_INCLUDE_SUBDOMAINS=false
        write_lumen_nginx_config {out} 'lumen.example.com www.example.com' '127.0.0.1:3000' 1 1 /etc/ssl/fullchain.pem /etc/ssl/privkey.pem
        """
    )
    config = out.read_text(encoding="utf-8")
    assert "return 301 https://$host$request_uri;" in config
    assert "location /events" in config
    assert "proxy_buffering off;" in config
    assert "location /api/" in config
    assert (
        "limit_req_zone $binary_remote_addr zone=lumen_api_lumen_example_com" in config
    )
    assert "add_header X-Content-Type-Options" in config
    assert (
        'add_header Strict-Transport-Security "max-age=31536000" always;' in config
    )
    assert "includeSubDomains" not in config
    assert "client_max_body_size 80m;" in config
    assert config.count("proxy_send_timeout 3600s;") == 4
    assert config.count("proxy_read_timeout 1800s;") == 4


def test_lumen_nginx_config_honors_hsts_policy_switches(tmp_path: Path) -> None:
    disabled = tmp_path / "disabled.conf"
    subdomains = tmp_path / "subdomains.conf"
    assert_bash_ok(
        f"""
        . {LUMENCTL}
        LUMEN_HSTS_ENABLED=false
        LUMEN_HSTS_INCLUDE_SUBDOMAINS=true
        write_lumen_nginx_config {disabled} lumen.example.com 127.0.0.1:3000 1 1 /cert /key
        LUMEN_HSTS_ENABLED=true
        LUMEN_HSTS_INCLUDE_SUBDOMAINS=true
        write_lumen_nginx_config {subdomains} lumen.example.com 127.0.0.1:3000 1 1 /cert /key
        """
    )

    assert "Strict-Transport-Security" not in disabled.read_text(encoding="utf-8")
    assert (
        'Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'
        in subdomains.read_text(encoding="utf-8")
    )


def test_ci_upload_body_size_guard_tracks_80mb_limit() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert 'if "client_max_body_size 80m;" not in nginx:' in workflow
    assert "if 'proxyClientMaxBodySize: \"80mb\"' not in next_config:" in workflow
    assert "client_max_body_size 60m" not in workflow
    assert 'proxyClientMaxBodySize: "60mb"' not in workflow
    assert '"_MAX_REQUEST_BYTES = 66 * 1024 * 1024" not in api_main' in workflow
    assert (
        '"_VIDEO_REFERENCE_UPLOAD_MAX_BYTES = 64 * 1024 * 1024" not in video_routes'
    ) in workflow


def test_update_blue_green_starts_target_worker_before_green_api_traffic() -> None:
    text = UPDATE.read_text(encoding="utf-8")

    blue_green_start = text.index(
        'if [ "${LUMEN_UPDATE_BLUE_GREEN:-0}" = "1" ] '
        '&& [ -f "${CURRENT_LINK}/docker-compose.bluegreen.yml" ]; then'
    )
    start_green = text.index("emit_start start_green", blue_green_start)
    start_green_api = text.index(
        "up --pull missing -d --wait --force-recreate api-green",
        start_green,
    )
    shift_traffic = text.index("emit_start shift_traffic_50", start_green_api)
    start_target_worker = text.index(
        'compose_up_service "${CURRENT_LINK}" worker',
        blue_green_start,
    )

    assert start_target_worker < start_green_api < shift_traffic


def test_update_blue_green_failure_keeps_green_until_blue_is_healthy() -> None:
    text = UPDATE.read_text(encoding="utf-8")
    failure_start = text.index('if [ "${_restart_ok}" = "1" ]; then')
    rollback_start = text.index("ROLLBACK_OK=0", failure_start)
    rollback_success = text.index(
        'if [ "${_rollback_started}" = "1" ]; then',
        rollback_start,
    )

    failure_block = text[failure_start:rollback_start]
    failure_recovery = failure_block[
        failure_block.index('log_warn "[restart_services] 蓝绿路径失败') :
    ]
    rollback_block = text[rollback_start:rollback_success]
    assert "blue_green_restore_blue_traffic()" in failure_block
    assert "lumen_wait_for_http_ok" in failure_block
    assert failure_recovery.index("blue_green_restore_blue_traffic; then") < (
        failure_recovery.index("blue_green_stop_green")
    )
    assert "blue_green_restore_blue_traffic; then" in rollback_block
    assert rollback_block.index("blue_green_restore_blue_traffic; then") < (
        rollback_block.index("blue_green_stop_green")
    )
    assert (
        'bash "${_shift_script}" blue 100 >/dev/null 2>&1 || true'
        not in failure_recovery
    )


def test_nginx_example_security_headers_are_not_duplicated() -> None:
    config = (ROOT / "deploy" / "nginx.conf.example").read_text(encoding="utf-8")

    for header in (
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "X-Frame-Options",
        "Cross-Origin-Opener-Policy",
        "Cross-Origin-Resource-Policy",
    ):
        assert config.count(f"add_header {header}") == 1
    assert config.count("add_header Content-Security-Policy") == 1
    assert (
        "add_header Content-Security-Policy \"default-src 'self'; frame-ancestors 'none';\" always;"
        in config
    )
    assert "Content-Security-Policy \"default-src 'none'" not in config
    assert "${LUMEN_HSTS_ENABLED}:${LUMEN_HSTS_INCLUDE_SUBDOMAINS}" in config
    assert '"true:false" "max-age=31536000";' in config
    assert '"true:true"  "max-age=31536000; includeSubDomains";' in config
    assert "add_header Strict-Transport-Security $lumen_hsts_header always;" in config


def test_sub2api_nginx_configs_have_long_timeouts_and_buffering_off(
    tmp_path: Path,
) -> None:
    sub2api = tmp_path / "sub2api.conf"
    outer = tmp_path / "outer.conf"
    assert_bash_ok(
        f"""
        . {LUMENCTL}
        write_sub2api_nginx_config {sub2api} api.example.com http://127.0.0.1:8081 1 /etc/ssl/fullchain.pem /etc/ssl/privkey.pem 443 1
        write_sub2api_outer_nginx_config {outer} api.example.com http://10.0.0.2:18081 0 '' ''
        """
    )
    for path in (sub2api, outer):
        config = path.read_text(encoding="utf-8")
        assert "client_max_body_size 100M;" in config
        assert "proxy_send_timeout 1800s;" in config
        assert "proxy_read_timeout 1800s;" in config
        assert "proxy_buffering off;" in config
        assert "proxy_request_buffering off;" in config


def test_nginx_backup_path_is_outside_enabled_config_tree(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        LUMEN_NGINX_BACKUP_DIR={backup_dir}
        nginx_backup_path /etc/nginx/sites-enabled/lumen.conf 20260502010101
        """
    )
    backup_path = Path(result.stdout.strip())
    assert backup_path.parent == backup_dir
    assert backup_path.name.endswith(".lumenctl.20260502010101.bak")
    assert "sites-enabled" in backup_path.name
    assert not backup_path.is_relative_to(Path("/etc/nginx"))


def test_nginx_image_job_returns_cleanly_when_scan_finds_no_files() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        require_sudo() {{ :; }}
        ensure_cmd() {{ :; }}
        as_sudo() {{
          if [ "$1" = "nginx" ] && [ "$2" = "-t" ]; then
            return 0
          fi
          "$@"
        }}
        collect_nginx_files() {{ NGINX_FILES=(); }}
        nginx_image_job_locations
        """
    )
    assert "没有可优化的 nginx 配置文件" in result.stderr


def test_image_job_location_injection_is_scoped_and_idempotent(tmp_path: Path) -> None:
    nginx_conf = tmp_path / "site.conf"
    nginx_conf.write_text(
        """
server {
  listen 80;
  server_name example.com;
  location / { proxy_pass http://127.0.0.1:3000; }
}

server {
  listen 443 ssl;
  server_name example.com;
  location / { proxy_pass http://127.0.0.1:3000; }
}
""",
        encoding="utf-8",
    )
    tmp1 = (
        assert_bash_ok(
            f"""
        . {LUMENCTL}
        optimize_nginx_file {nginx_conf} http://127.0.0.1:8091 /opt/image-job/data example.com
        """
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    first = Path(tmp1).read_text(encoding="utf-8")
    nginx_conf.write_text(first, encoding="utf-8")
    tmp2 = (
        assert_bash_ok(
            f"""
        . {LUMENCTL}
        optimize_nginx_file {nginx_conf} http://127.0.0.1:8091 /opt/image-job/data example.com
        """
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    second = Path(tmp2).read_text(encoding="utf-8")

    assert first == second
    assert first.count("location ^~ /v1/image-jobs") == 1
    assert first.count("location ^~ /v1/refs") == 1
    assert first.count("location ^~ /images/temp/") == 1
    assert first.count("location ^~ /refs/") == 1
    http_block, https_block = first.split("server {", 2)[1:]
    assert "/v1/image-jobs" not in http_block
    assert "/v1/image-jobs" in https_block


# ---------------------------------------------------------------------------
# Docker 全栈切换：脚本互相之间的契约（install / update / uninstall + lib helper）
# ---------------------------------------------------------------------------


def test_install_script_uses_lumen_compose_helpers_from_lib() -> None:
    """
    docker cutover §6.2 / §10.2: install.sh 走 lib.sh 提供的 lumen_compose helper
    （或本地 _install_compose 包装），不再 systemctl restart lumen-*。
    """
    text = INSTALL.read_text(encoding="utf-8")
    # 显式引用 lib.sh 的 compose helper（lumen_compose 或 lumen_compose_in）
    assert "lumen_compose" in text
    # 流程描述里出现 docker compose pull / migrate / api/worker/web
    assert "docker compose pull" in text
    assert "migrate" in text
    # set -euo pipefail 必须保留
    assert "set -euo pipefail" in text
    # source lib.sh
    assert '. "${SCRIPT_DIR}/lib.sh"' in text


def test_update_script_emits_set_image_tag_and_migrate_db_phases() -> None:
    """
    docker cutover §11.3.1: update.sh 阶段日志里必须包含 set_image_tag 与 migrate_db，
    后台一键更新解析这两个阶段决定 LUMEN_IMAGE_TAG 是否切换 / 数据库迁移是否成功。
    """
    text = UPDATE.read_text(encoding="utf-8")
    assert "set_image_tag" in text
    assert "migrate_db" in text
    # phase=set_image_tag 与 phase=migrate_db 在最终输出里靠 emit_start 拼出，
    # 加耗时计时后 wrapper 用 local _phase=$1 + "phase=${_phase}"。
    assert 'lumen_emit_step "phase=${_phase}"' in text
    assert "emit_start set_image_tag" in text
    assert "emit_start migrate_db" in text
    # 必须 source lib.sh 并 set -euo pipefail
    assert "set -euo pipefail" in text
    assert "lib.sh" in text


def test_update_script_runner_default_pull_not_build() -> None:
    """
    docker cutover §11.3.2 + §12.3.2: build 必须由 LUMEN_UPDATE_BUILD=1 显式开启，
    runner systemd 默认 LUMEN_UPDATE_BUILD=0（pull 优先）。
    """
    text = UPDATE.read_text(encoding="utf-8")
    # build 路径必须 gated by env
    assert "LUMEN_UPDATE_BUILD:-0" in text
    runner_unit = (
        ROOT / "deploy" / "systemd" / "lumen-update-runner.service"
    ).read_text(encoding="utf-8")
    runner = (ROOT / "scripts" / "update_runner.py").read_text(encoding="utf-8")
    assert "update_runner.py" in runner_unit
    assert "EnvironmentFile=" not in runner_unit
    assert '"LUMEN_UPDATE_BUILD": "0"' in runner


def test_uninstall_script_uses_docker_compose_down() -> None:
    """
    docker cutover §17.4 / §17.9: uninstall.sh 走 docker compose down --remove-orphans，
    不再依赖 systemctl stop lumen-* 作为停服主路径。
    """
    text = UNINSTALL.read_text(encoding="utf-8")
    # 主路径必须有 docker compose down
    assert "docker compose down" in text
    # 优先走 lib.sh 的 lumen_compose_in（带 COMPOSE_PROJECT_NAME=lumen）
    assert "lumen_compose_in" in text
    # 含 --profile tgbot 的 down，确保 profile 服务也清理
    assert "--profile tgbot" in text
    # set -euo pipefail + source lib.sh 必须保留
    assert "set -euo pipefail" in text
    assert "lib.sh" in text


def test_uninstall_purge_has_lumen_data_prefix_guard() -> None:
    text = UNINSTALL.read_text(encoding="utf-8")
    assert "lumen_uninstall_data_path_safe_for_purge" in text
    assert "/opt/lumen*|/var/lumen*|/var/lib/lumen*|/srv/lumen*" in text
    assert "purge 路径规范化后脱离 Lumen 数据目录前缀" in text


def test_lib_provides_compose_helpers_required_by_cutover() -> None:
    """
    docker cutover: lib.sh 必须暴露 cutover plan §3.1 / §11 / §13 列出的全部 helper。
    """
    text = lib_source_text()
    for fn in (
        "lumen_compose()",
        "lumen_compose_in()",
        "lumen_health_http()",
        "lumen_health_compose()",
        "lumen_image_tag_resolve()",
        "lumen_resolve_release_alias()",
        "lumen_verify_release_manifest_images()",
        "lumen_set_image_tag_in_env()",
        "lumen_emit_step()",
        "lumen_emit_info()",
        "lumen_with_lock()",
    ):
        assert fn in text, f"lib.sh 缺少 helper：{fn}"


def test_image_tag_resolve_uses_channel_and_env_file_for_pinned(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LUMEN_IMAGE_TAG=v1.2.3\n", encoding="utf-8")

    result = assert_bash_ok(
        f"""
        . {LIB}
        lumen_image_tag_resolve pinned {env_file}
        """
    )

    assert result.stdout.strip() == "v1.2.3"


def test_image_tag_resolve_supports_main_minor_and_major_channels(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LUMEN_IMAGE_TAG=v1.2.3\n", encoding="utf-8")

    result = assert_bash_ok(
        f"""
        . {LIB}
        printf 'main=%s\\n' "$(lumen_image_tag_resolve main {env_file})"
        printf 'minor=%s\\n' "$(lumen_image_tag_resolve minor {env_file})"
        printf 'major=%s\\n' "$(lumen_image_tag_resolve major {env_file})"
        printf 'literal=%s\\n' "$(lumen_image_tag_resolve v9.8.7 {env_file})"
        """
    )

    assert "main=main" in result.stdout
    assert "minor=v1.2" in result.stdout
    assert "major=v1" in result.stdout
    assert "literal=v9.8.7" in result.stdout


def test_image_tag_resolve_stable_fails_closed_when_latest_unavailable(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LUMEN_IMAGE_TAG=v1.1.17\n", encoding="utf-8")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    curl = fakebin / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 28\n", encoding="utf-8")
    curl.chmod(0o755)

    result = run_bash(
        f"""
        . {LIB}
        PATH={shlex.quote(str(fakebin))}:$PATH
        hash -r
        lumen_image_tag_resolve stable {env_file}
        """
    )

    assert result.returncode == 1
    assert result.stdout.strip() == ""
    assert "stable/latest 无法解析" in result.stderr
    assert "显式设置 LUMEN_UPDATE_CHANNEL=main" in result.stderr


def test_image_tag_resolve_pinned_is_only_channel_that_keeps_current_tag(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LUMEN_IMAGE_TAG=v1.1.17\n", encoding="utf-8")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    curl = fakebin / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 28\n", encoding="utf-8")
    curl.chmod(0o755)

    result = assert_bash_ok(
        f"""
        . {LIB}
        PATH={shlex.quote(str(fakebin))}:$PATH
        hash -r
        lumen_image_tag_resolve pinned {env_file}
        """
    )

    assert result.stdout.strip() == "v1.1.17"


def test_image_tag_is_rolling_matches_main_and_semver_aliases() -> None:
    result = assert_bash_ok(
        f"""
        . {LIB}
        for tag in main latest v1 v1.2 v1.2.3 sha-deadbee; do
            if lumen_image_tag_is_rolling "$tag"; then
                printf '%s=rolling\\n' "$tag"
            else
                printf '%s=fixed\\n' "$tag"
            fi
        done
        """
    )

    assert "main=rolling" in result.stdout
    assert "latest=rolling" in result.stdout
    assert "v1=rolling" in result.stdout
    assert "v1.2=rolling" in result.stdout
    assert "v1.2.3=fixed" in result.stdout
    assert "sha-deadbee=fixed" in result.stdout


def test_release_manifest_verifier_checks_alias_image_against_concrete_digest(
    tmp_path: Path,
) -> None:
    digest = f"sha256:{'a' * 64}"
    guard = tmp_path / "guard.py"
    guard.write_text(
        "print("
        f"'api\\tghcr.io/cyeinfpro/lumen-api:v1.2.10\\t{digest}"
        f"\\tghcr.io/cyeinfpro/lumen-api@{digest}'"
        ")\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")

    result = assert_bash_ok(
        f"""
        . {LIB}
        LUMEN_RELEASE_MANIFEST_GUARD={shlex.quote(str(guard))}
        lumen_docker() {{
            test "$1" = image
            test "$2" = inspect
            test "$5" = ghcr.io/cyeinfpro/lumen-api:v1.2
            printf '%s\\n' ghcr.io/cyeinfpro/lumen-api@{digest}
        }}
        lumen_verify_release_manifest_images \
            {shlex.quote(str(manifest))} v1.2.10 v1.2 --service api
        """
    )

    assert "release manifest digest 通过" in result.stdout


def test_docker_release_workflow_builds_amd64_and_arm64() -> None:
    workflow = (ROOT / ".github" / "workflows" / "docker-release.yml").read_text(
        encoding="utf-8"
    )
    # api/worker/tgbot 走 build matrix，QEMU 双架构稳定。
    assert 'platforms: "linux/amd64,linux/arm64"' in workflow, (
        "expected python images (api/worker/tgbot) to build amd64+arm64"
    )
    assert "platforms: ${{ matrix.image.platforms }}" in workflow, (
        "expected build step to read platforms from matrix"
    )
    # lumen-web 拆到 build-web/merge-web：amd64 在 ubuntu-latest、arm64 在
    # native ubuntu-24.04-arm runner，各自 push-by-digest，最后用
    # buildx imagetools create 合并成多架构 manifest list。
    assert "build-web:" in workflow, "expected dedicated build-web job"
    assert "merge-web:" in workflow, "expected merge-web job"
    assert "ubuntu-24.04-arm" in workflow, (
        "expected build-web arm64 to run on native ARM runner"
    )
    assert "push-by-digest=true" in workflow, (
        "expected per-platform web build to push by digest"
    )
    assert "docker buildx imagetools create" in workflow, (
        "expected merge-web to assemble multi-arch manifest list"
    )
    assert "release_tag#v" in workflow, (
        "tag builds should pass the product version without a leading v"
    )
    assert "workflow_dispatch.ref cannot create release semantics" in workflow
    assert "needs: [resolve-ref, build-web]" in workflow
    assert "needs: [resolve-ref, build, merge-web]" in workflow
    assert "needs: [resolve-ref, quality-gate]" in workflow
    assert "type=semver,pattern=v{{version}}" in workflow
    assert "type=semver,pattern=v{{major}}.{{minor}}" in workflow
    assert "type=semver,pattern=v{{major}}" in workflow
    assert "go install github.com/sigstore/cosign/v2/cmd/cosign@v2.6.1" in workflow
    assert "cosign sign --yes" in workflow
    assert "id-token: write" in workflow
    assert "cp .env.example .env" in workflow
    assert 'image_proxy_secret="$(openssl rand -hex 32)"' in workflow
    assert (
        '-e "s|^IMAGE_PROXY_SECRET=.*|IMAGE_PROXY_SECRET=${image_proxy_secret}|"'
        in workflow
    )
    assert "Compose config" in workflow
    assert "Image start smoke" in workflow


def test_deploy_docker_helper_files_exist() -> None:
    assert (ROOT / "deploy" / "docker" / "README.md").is_file()
    override = (ROOT / "deploy" / "docker" / "docker-compose.local.yml").read_text(
        encoding="utf-8"
    )
    assert "lumen-local-api" in override
    assert "18000:8000" in override
    assert "13000:3000" in override


def test_admin_update_checklist_uses_docker_phases() -> None:
    panel = "\n".join(
        (
            (
                ROOT
                / "apps"
                / "web"
                / "src"
                / "app"
                / "admin"
                / "_panels"
                / "AdminUpdatePanel.tsx"
            ).read_text(encoding="utf-8"),
            (
                ROOT
                / "apps"
                / "web"
                / "src"
                / "app"
                / "admin"
                / "_panels"
                / "AdminUpdatePanel.helpers.ts"
            ).read_text(encoding="utf-8"),
        )
    )
    for phase in (
        "lock",
        "check",
        "preflight",
        "backup_preflight",
        "fetch_release",
        "set_image_tag",
        "pull_images",
        "start_infra",
        "migrate_db",
        "switch",
        "restart_services",
        "health_check",
        "cleanup",
    ):
        assert f'"{phase}"' in panel
    for old_phase in ("deps_python", "deps_node", "build_web"):
        assert f'"{old_phase}"' not in panel


def test_lumenctl_help_documents_docker_compose_runtime_block() -> None:
    """
    docker cutover §24: lumenctl.sh help 输出里必须出现 status / logs / migrate / rollback / version
    这些 docker compose 阶段命令的描述（菜单 + CLI 双入口）。
    """
    result = subprocess.run(
        ["bash", str(LUMENCTL), "help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=script_env(),
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    output = result.stdout
    # help 描述里出现 docker compose 关键字（确认 help 在描述中提到 compose 路径）
    assert "docker compose" in output or "compose" in output
    # 关键 lifecycle / runtime 命令名在描述行里
    for keyword in ("rollback", "migrate", "version", "status", "logs"):
        assert keyword in output, f"lumenctl.sh help 描述里缺少 {keyword}"
