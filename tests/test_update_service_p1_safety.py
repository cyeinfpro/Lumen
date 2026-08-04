from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "scripts" / "update" / "journal.sh"
PHASES = ROOT / "scripts" / "update" / "backup" / "phases.sh"
MIGRATION_HELPERS = ROOT / "scripts" / "update" / "backup" / "migration_helpers.sh"
RECOVERY = ROOT / "scripts" / "update" / "recovery" / "state.sh"
INSTALL_SERVICES = ROOT / "scripts" / "install" / "services.sh"
HEALTH = ROOT / "scripts" / "update" / "services" / "health.sh"
COMPOSE_HELPERS = ROOT / "scripts" / "update" / "services" / "compose.sh"
RESTART = ROOT / "scripts" / "update" / "services" / "restart.sh"
CONTAINER_RELEASE = ROOT / "scripts" / "lib" / "container_release.sh"


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_storage_mount_mocks(fakebin: Path) -> tuple[Path, Path]:
    mountpoint = fakebin / "mountpoint"
    mountpoint.write_text(
        """#!/usr/bin/env bash
path="${!#}"
if [ "${TEST_DATA_EXACT:-0}" = "1" ] && [ "$path" = "${TEST_DATA_ROOT:?}" ]; then
    exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    mountpoint.chmod(0o755)

    findmnt = fakebin / "findmnt"
    findmnt.write_text(
        """#!/usr/bin/env bash
path=""
field=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -T) path="$2"; shift 2 ;;
        -no) field="$2"; shift 2 ;;
        *) shift ;;
    esac
done
if [ "$path" = "${TEST_DATA_ROOT:?}" ]; then
    if [ "${TEST_DATA_EXACT:-0}" = "1" ]; then
        target="$TEST_DATA_ROOT"
        source="//nas.example/lumen"
        fstype="cifs"
        mount_id="data-mount"
    else
        target="/"
        source="/dev/root"
        fstype="ext4"
        mount_id="root-mount"
    fi
elif [ "$path" = "${TEST_DB_ROOT:?}" ]; then
    target="${TEST_DB_MOUNT_TARGET:-/}"
    source="${TEST_DB_SOURCE:-/dev/root}"
    fstype="${TEST_DB_FSTYPE:-ext4}"
    mount_id="db-mount"
    if [ "$field" = "SOURCE" ] && [ -n "${TEST_DB_SOURCE_AFTER_FIRST:-}" ]; then
        count=0
        [ ! -f "${TEST_DB_SOURCE_COUNT:?}" ] \
            || count="$(cat "${TEST_DB_SOURCE_COUNT}")"
        count=$((count + 1))
        printf '%s\n' "$count" > "${TEST_DB_SOURCE_COUNT}"
        if [ "$count" -gt 1 ]; then
            source="$TEST_DB_SOURCE_AFTER_FIRST"
        fi
    fi
else
    exit 1
fi
case "$field" in
    TARGET) printf '%s\n' "$target" ;;
    SOURCE) printf '%s\n' "$source" ;;
    FSTYPE) printf '%s\n' "$fstype" ;;
    ID) printf '%s\n' "$mount_id" ;;
    *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    findmnt.chmod(0o755)

    controller = fakebin / "lumen-storage-mount"
    controller.write_text(
        """#!/usr/bin/env bash
printf '%s|%s|%s\n' \
    "${LUMEN_STORAGE_TARGET:?}" "${LUMEN_DB_ROOT:?}" "$1" \
    >> "${TEST_CONTROLLER_LOG:?}"
case "$1" in
    up|bind-identity)
        printf 'DATASET_IDENTITY=%064d\n' 0 \
            > "${LUMEN_STORAGE_STATE_DIR:?}/last-good.conf"
        ;;
    verify)
        exit "${TEST_CONTROLLER_VERIFY_RC:-0}"
        ;;
    *)
        exit 2
        ;;
esac
""",
        encoding="utf-8",
    )
    controller.chmod(0o755)
    return findmnt, controller


def _start_infra_storage_run(
    tmp_path: Path,
    *,
    data_exact: bool,
    split_db_root: bool,
    db_source_after_first: str = "",
    controller_verify_rc: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str], Path, Path]:
    data_root = tmp_path / "lumendata"
    db_root = tmp_path / "database" if split_db_root else data_root
    shared = tmp_path / "shared"
    state = tmp_path / "storage-state"
    release = tmp_path / "release"
    fakebin = tmp_path / "bin"
    for path in (data_root, db_root, shared, state, release, fakebin):
        path.mkdir(parents=True, exist_ok=True)
    for path in (
        data_root / "storage",
        data_root / "backup",
        db_root / "postgres",
        db_root / "redis",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (state / "last-good.conf").write_text(
        f"DATASET_IDENTITY={'a' * 64}\n",
        encoding="utf-8",
    )
    _, controller = _write_storage_mount_mocks(fakebin)
    events = tmp_path / "events.log"
    controller_log = tmp_path / "controller.log"
    identity_file = shared / ".db-root.last-good.json"
    result = _run(
        f"""
        set -u
        export PATH={shlex.quote(str(fakebin))}:$PATH
        export TEST_DATA_ROOT={shlex.quote(str(data_root))}
        export TEST_DB_ROOT={shlex.quote(str(db_root))}
        export TEST_DATA_EXACT={1 if data_exact else 0}
        export TEST_DB_MOUNT_TARGET=/
        export TEST_DB_SOURCE=/dev/root
        export TEST_DB_SOURCE_AFTER_FIRST={shlex.quote(db_source_after_first)}
        export TEST_DB_SOURCE_COUNT={shlex.quote(str(tmp_path / "db-source.count"))}
        export TEST_CONTROLLER_LOG={shlex.quote(str(controller_log))}
        export TEST_CONTROLLER_VERIFY_RC={controller_verify_rc}
        . {shlex.quote(str(MIGRATION_HELPERS))}
        . {shlex.quote(str(PHASES))}
        emit_start() {{ :; }}
        emit_done() {{ :; }}
        emit_fail() {{ :; }}
        emit_info() {{ :; }}
        log_info() {{ :; }}
        log_warn() {{ printf 'WARN:%s\\n' "$*" >&2; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        lumen_require_no_active_systemd_fallback_writers() {{ return 0; }}
        lumen_run_as_root() {{ "$@"; }}
        lumen_compose_project_unify() {{
            printf 'unify\\n' >> {shlex.quote(str(events))}
        }}
        migrate_postgres_uid() {{
            printf 'migrate-uid\\n' >> {shlex.quote(str(events))}
            return 0
        }}
        lumen_health_compose() {{ return 1; }}
        lumen_compose_in() {{
            printf 'compose:%s\\n' "$*" >> {shlex.quote(str(events))}
            return 0
        }}
        ROOT={shlex.quote(str(tmp_path))}
        SHARED_DIR={shlex.quote(str(shared))}
        NEW_RELEASE={shlex.quote(str(release))}
        LUMEN_DATA_ROOT={shlex.quote(str(data_root))}
        LUMEN_DB_ROOT={shlex.quote(str(db_root))}
        LUMEN_STORAGE_STATE_DIR={shlex.quote(str(state))}
        LUMEN_UPDATE_DB_ROOT_IDENTITY_FILE={shlex.quote(str(identity_file))}
        LUMEN_UPDATE_STORAGE_CONTROLLER={shlex.quote(str(controller))}
        LUMEN_UPDATE_MODE=standard
        SKIP_STORAGE_CHECK=0
        update_phase_start_infra
        """
    )
    lines = events.read_text(encoding="utf-8").splitlines() if events.exists() else []
    return result, lines, controller_log, identity_file


def _migration_run(
    tmp_path: Path,
    *,
    stop_rc: int = 0,
    running_writer: str = "",
    migration_rc: int = 0,
    current_revision: str = "head",
    tgbot_active: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    events = tmp_path / "events.log"
    old_release = tmp_path / "releases" / "old"
    new_release = tmp_path / "releases" / "new"
    old_release.mkdir(parents=True)
    new_release.mkdir(parents=True)
    running_container = f"lumen-{running_writer}" if running_writer else ""
    result = _run(
        f"""
        set -u
        . {shlex.quote(str(MIGRATION_HELPERS))}
        . {shlex.quote(str(RECOVERY))}
        . {shlex.quote(str(PHASES))}
        emit_start() {{ :; }}
        emit_done() {{ :; }}
        emit_fail() {{ :; }}
        emit_info() {{ :; }}
        log_info() {{ :; }}
        log_warn() {{ :; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        log_update_restore_boundary() {{ :; }}
        lumen_update_require_storage_identity() {{ return 0; }}
        guard_migration_restore_point() {{ return 0; }}
        lumen_update_capture_original_tgbot_state() {{
            UPDATE_ORIGINAL_TGBOT_ACTIVE={tgbot_active}
            UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN=1
            return 0
        }}
        lumen_set_image_tag_in_env() {{ return 0; }}
        target_alembic_head() {{ printf 'head\\n'; }}
        current_alembic_revision() {{
            printf '%s\\n' {shlex.quote(current_revision)}
        }}
        lumen_docker() {{
            printf 'docker:%s\\n' "$*" >> {shlex.quote(str(events))}
            if [ -n {shlex.quote(running_container)} ] \
                    && [[ "$*" == *"name=^/{running_container}$"* ]]; then
                printf 'running-container\\n'
            fi
            return 0
        }}
        lumen_compose_in() {{
            printf 'compose:%s\\n' "$*" >> {shlex.quote(str(events))}
            case " $* " in
                *" stop "*) return {stop_rc} ;;
                *" --profile migrate run --rm migrate "*) return {migration_rc} ;;
                *" up "*) return 0 ;;
                *) return 0 ;;
            esac
        }}
        ROOT={shlex.quote(str(tmp_path))}
        NEW_RELEASE={shlex.quote(str(new_release))}
        CURRENT_ID=old
        PREVIOUS_TAG=v1.2.3
        TARGET_TAG=v1.2.3
        SHARED_ENV={shlex.quote(str(tmp_path / "shared.env"))}
        LUMEN_UPDATE_MODE=standard
        LUMEN_UPDATE_BLUE_GREEN=0
        UPDATE_OLD_SERVICES_STOPPED=0
        UPDATE_MIGRATION_STARTED=0
        UPDATE_MIGRATION_VERIFIED=0
        UPDATE_MIGRATION_HEAD=""
        update_phase_migrate_db
        """
    )
    lines = events.read_text(encoding="utf-8").splitlines() if events.exists() else []
    return result, lines


def test_root_filesystem_findmnt_false_positive_blocks_infra_recreate(
    tmp_path: Path,
) -> None:
    result, events, controller_log, identity_file = _start_infra_storage_run(
        tmp_path,
        data_exact=False,
        split_db_root=False,
    )

    assert result.returncode != 0
    assert "数据根不是独立 mountpoint" in result.stderr
    assert not any(line.startswith("compose:") for line in events)
    assert not controller_log.exists()
    assert not identity_file.exists()


def test_verified_data_mount_allows_split_db_root_on_root_filesystem(
    tmp_path: Path,
) -> None:
    result, events, controller_log, identity_file = _start_infra_storage_run(
        tmp_path,
        data_exact=True,
        split_db_root=True,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert any(
        "up --pull missing -d --wait --force-recreate postgres redis" in line
        for line in events
    )
    identity = json.loads(identity_file.read_text(encoding="utf-8"))
    assert identity["db_root"] == str(tmp_path / "database")
    assert identity["mount_target"] == "/"
    assert identity["mount_source"] == "/dev/root"
    assert controller_log.read_text(encoding="utf-8").count("|verify\n") == 2


def test_split_db_parent_identity_drift_blocks_recreate(tmp_path: Path) -> None:
    result, events, _, identity_file = _start_infra_storage_run(
        tmp_path,
        data_exact=True,
        split_db_root=True,
        db_source_after_first="/dev/replacement",
    )

    assert result.returncode != 0
    assert identity_file.exists()
    assert "database mount identity changed" in result.stderr
    assert not any(line.startswith("compose:") for line in events)


def test_storage_controller_last_good_mismatch_blocks_recreate(
    tmp_path: Path,
) -> None:
    result, events, controller_log, _ = _start_infra_storage_run(
        tmp_path,
        data_exact=True,
        split_db_root=True,
        controller_verify_rc=1,
    )

    assert result.returncode != 0
    assert "controller verify/last-good identity 校验失败" in result.stderr
    assert not any(line.startswith("compose:") for line in events)
    assert controller_log.read_text(encoding="utf-8").endswith("|verify\n")


def test_stop_failure_blocks_alembic_and_marks_possible_partial_stop(
    tmp_path: Path,
) -> None:
    result, events = _migration_run(tmp_path, stop_rc=17)

    assert result.returncode != 0
    assert any("stop -t 30 api worker tgbot" in line for line in events)
    assert not any("--profile migrate run --rm migrate" in line for line in events)
    assert "stop api/worker/tgbot 失败" in result.stderr


def test_all_writer_containers_must_be_confirmed_stopped_before_alembic(
    tmp_path: Path,
) -> None:
    result, events = _migration_run(tmp_path, running_writer="worker")

    assert result.returncode != 0
    assert not any("--profile migrate run --rm migrate" in line for line in events)
    assert "writer 容器仍在运行：worker" in result.stderr
    for container in ("lumen-api", "lumen-worker", "lumen-tgbot"):
        assert any(f"name=^/{container}$" in line for line in events)


def test_verified_writer_shutdown_precedes_alembic(tmp_path: Path) -> None:
    result, events = _migration_run(tmp_path)

    assert result.returncode == 0, result.stderr + result.stdout
    stop_index = next(i for i, line in enumerate(events) if " stop " in f" {line} ")
    migrate_index = next(
        i
        for i, line in enumerate(events)
        if "--profile migrate run --rm migrate" in line
    )
    writer_checks = [
        next(i for i, line in enumerate(events) if f"name=^/{name}$" in line)
        for name in ("lumen-api", "lumen-worker", "lumen-tgbot")
    ]
    assert stop_index < min(writer_checks)
    assert max(writer_checks) < migrate_index


@pytest.mark.parametrize("tgbot_active", [0, 1])
def test_migration_failure_restores_tgbot_only_if_it_was_active(
    tmp_path: Path,
    tgbot_active: int,
) -> None:
    result, events = _migration_run(
        tmp_path,
        migration_rc=9,
        current_revision="head",
        tgbot_active=tgbot_active,
    )

    assert result.returncode != 0
    restore = next(line for line in events if " up " in f" {line} ")
    assert ("--profile tgbot" in restore) is bool(tgbot_active)
    assert (" tgbot" in restore) is bool(tgbot_active)
    assert " worker api" in restore


def test_migration_failure_never_starts_old_writers_on_partial_revision(
    tmp_path: Path,
) -> None:
    result, events = _migration_run(
        tmp_path,
        migration_rc=9,
        current_revision="partial-new-head",
        tgbot_active=1,
    )

    assert result.returncode != 0
    assert not any(" up " in f" {line} " for line in events)
    assert "旧 release old 无法安全写入当前数据库 revision=partial-new-head" in (
        result.stderr
    )
    assert "禁止旧 writer" in result.stderr
    assert "前向升级" in result.stderr
    assert "人工整库回滚" in result.stderr


@pytest.mark.parametrize("tgbot_active", [0, 1])
def test_generic_rollback_restores_tgbot_only_if_it_was_active(
    tmp_path: Path,
    tgbot_active: int,
) -> None:
    compose_log = tmp_path / "compose.log"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(COMPOSE_HELPERS))}
        . {shlex.quote(str(RECOVERY))}
        guard_automatic_app_rollback_compatibility() {{ return 0; }}
        restore_update_symlink_snapshot() {{ return 0; }}
        restore_update_env_snapshot() {{ return 0; }}
        lumen_release_remove_unused() {{ return 0; }}
        discard_update_state_snapshot() {{ return 0; }}
        log_warn() {{ :; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        lumen_compose_in() {{
            printf '%s\\n' "$*" >> {shlex.quote(str(compose_log))}
            return 0
        }}
        lumen_wait_for_http_ok() {{ return 0; }}
        ROOT={shlex.quote(str(tmp_path))}
        CURRENT_ID=old
        NEW_RELEASE=""
        NEW_ID=new
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_RELEASE_SWITCHED=1
        UPDATE_OLD_SERVICES_STOPPED=1
        UPDATE_ORIGINAL_TGBOT_ACTIVE={tgbot_active}
        UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN=1
        restore_uncommitted_update_state
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    restore = compose_log.read_text(encoding="utf-8").strip()
    assert ("--profile tgbot" in restore) is bool(tgbot_active)
    assert (" tgbot" in restore) is bool(tgbot_active)
    assert " worker web api" in restore
    assert "--wait --force-recreate" in restore


@pytest.mark.parametrize("failure", ["compose", "readyz"])
def test_generic_rollback_failure_retains_snapshot_and_release_evidence(
    tmp_path: Path,
    failure: str,
) -> None:
    events = tmp_path / "rollback-events.log"
    new_release = tmp_path / "releases" / "new"
    new_release.mkdir(parents=True)
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(COMPOSE_HELPERS))}
        . {shlex.quote(str(RECOVERY))}
        guard_automatic_app_rollback_compatibility() {{ return 0; }}
        restore_update_symlink_snapshot() {{ return 0; }}
        restore_update_env_snapshot() {{ return 0; }}
        lumen_release_remove_unused() {{
            printf 'remove-release\\n' >> {shlex.quote(str(events))}
        }}
        discard_update_state_snapshot() {{
            printf 'discard-snapshot\\n' >> {shlex.quote(str(events))}
        }}
        log_warn() {{ :; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        lumen_compose_in() {{
            printf 'compose:%s\\n' "$*" >> {shlex.quote(str(events))}
            [ {shlex.quote(failure)} != compose ]
        }}
        lumen_wait_for_http_ok() {{
            printf 'ready:%s\\n' "$1" >> {shlex.quote(str(events))}
            [ {shlex.quote(failure)} != readyz ]
        }}
        ROOT={shlex.quote(str(tmp_path))}
        CURRENT_ID=old
        NEW_RELEASE={shlex.quote(str(new_release))}
        NEW_ID=new
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_RELEASE_SWITCHED=1
        UPDATE_OLD_SERVICES_STOPPED=1
        UPDATE_ORIGINAL_TGBOT_ACTIVE=0
        UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN=1
        rc=0
        restore_uncommitted_update_state || rc=$?
        printf 'rc=%s flags=%s:%s\\n' \
            "$rc" "$UPDATE_RELEASE_SWITCHED" "$UPDATE_OLD_SERVICES_STOPPED"
        test "$rc" -ne 0
        test "$UPDATE_RELEASE_SWITCHED" -eq 1
        test "$UPDATE_OLD_SERVICES_STOPPED" -eq 1
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    lines = events.read_text(encoding="utf-8").splitlines()
    compose = next(line for line in lines if line.startswith("compose:"))
    assert "--wait --force-recreate worker web api" in compose
    if failure == "compose":
        assert not any(line.startswith("ready:") for line in lines)
    else:
        assert "ready:http://127.0.0.1:8000/readyz" in lines
    assert "remove-release" not in lines
    assert "discard-snapshot" not in lines
    assert new_release.is_dir()
    assert "flags=1:1" in result.stdout
    assert "保留 update snapshot/journal" in result.stderr


@pytest.mark.parametrize("ready_rc, expect_rolled_back", [(0, True), (1, False)])
def test_standard_restart_rollback_requires_compose_wait_and_readyz(
    tmp_path: Path,
    ready_rc: int,
    expect_rolled_back: bool,
) -> None:
    events = tmp_path / "restart-rollback.log"
    old_release = tmp_path / "releases" / "old"
    old_release.mkdir(parents=True)
    (old_release / ".image-tag").write_text("v1.2.3\n", encoding="utf-8")
    (old_release / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    shared_env = tmp_path / "shared.env"
    shared_env.write_text("LUMEN_IMAGE_TAG=v1.2.4\n", encoding="utf-8")
    result = _run(
        f"""
        set -u
        . {shlex.quote(str(COMPOSE_HELPERS))}
        . {shlex.quote(str(RESTART))}
        emit_start() {{ :; }}
        emit_done() {{ :; }}
        emit_fail() {{ printf 'emit-fail:%s\\n' "$1" >> {shlex.quote(str(events))}; }}
        emit_warn() {{ :; }}
        emit_info() {{
            printf 'emit-info:%s:%s:%s\\n' "$1" "$2" "$3" \
                >> {shlex.quote(str(events))}
        }}
        log_info() {{ :; }}
        log_warn() {{ :; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        log_update_restore_boundary() {{ :; }}
        lumen_update_activate_bound_image_override() {{ return 0; }}
        lumen_update_write_release_metadata() {{ return 0; }}
        lumen_update_harden_release_ownership() {{ return 0; }}
        lumen_verify_backup_service_layout_binding() {{ return 0; }}
        lumen_update_start_bound_service() {{ return 1; }}
        guard_automatic_app_rollback_compatibility() {{ return 0; }}
        lumen_set_image_tag_in_env() {{ return 0; }}
        lumen_set_env_value_in_file() {{ return 0; }}
        lumen_release_harden_ownership() {{ return 0; }}
        lumen_release_atomic_switch() {{ return 0; }}
        env_key_present() {{ return 1; }}
        lumen_compose_in() {{
            printf 'compose:%s\\n' "$*" >> {shlex.quote(str(events))}
            return 0
        }}
        lumen_wait_for_http_ok() {{
            printf 'ready:%s\\n' "$1" >> {shlex.quote(str(events))}
            return {ready_rc}
        }}
        ROOT={shlex.quote(str(tmp_path))}
        CURRENT_ID=old
        PREVIOUS_TAG=v1.2.3
        TARGET_TAG=v1.2.4
        NEW_RELEASE={shlex.quote(str(tmp_path / "releases" / "new"))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        TARGET_IMAGE_OVERRIDE_FILE={shlex.quote(str(tmp_path / "override.yml"))}
        LUMEN_UPDATE_MODE=standard
        LUMEN_UPDATE_BLUE_GREEN=0
        UPDATE_MIGRATION_STARTED=0
        UPDATE_RELEASE_SWITCHED=1
        UPDATE_OLD_SERVICES_STOPPED=1
        TGBOT_IMAGE_READY=0
        update_phase_restart_services
        """
    )

    assert result.returncode != 0
    lines = events.read_text(encoding="utf-8").splitlines()
    rollback_up = [line for line in lines if "compose:" in line and " up " in line]
    assert len(rollback_up) == 3
    assert all("-d --wait --force-recreate" in line for line in rollback_up)
    assert "ready:http://127.0.0.1:8000/readyz" in lines
    rolled_back = any("rolled_back_to" in line for line in lines)
    assert rolled_back is expect_rolled_back
    if not expect_rolled_back:
        assert "不标记 rolled_back" in result.stderr


def test_original_tgbot_state_survives_process_context_loss(tmp_path: Path) -> None:
    env_snapshot = tmp_path / ".env.update.test"
    env_snapshot.write_text("LUMEN_IMAGE_TAG=v1.2.3\n", encoding="utf-8")
    state_file = Path(f"{env_snapshot}.services")
    result = _run(
        f"""
        set -euo pipefail
        UPDATE_MODULE_DIR={shlex.quote(str(ROOT / "scripts" / "update"))}
        . {shlex.quote(str(JOURNAL))}
        . {shlex.quote(str(RECOVERY))}
        . {shlex.quote(str(PHASES))}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        emit_info() {{ :; }}
        lumen_docker() {{ printf 'tgbot-container\\n'; }}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        UPDATE_ENV_SNAPSHOT={shlex.quote(str(env_snapshot))}
        UPDATE_ORIGINAL_TGBOT_ACTIVE=0
        UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN=0
        lumen_update_capture_original_tgbot_state
        UPDATE_ORIGINAL_TGBOT_ACTIVE=0
        UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN=0
        lumen_update_load_original_service_state
        printf 'active=%s known=%s\\n' \
            "$UPDATE_ORIGINAL_TGBOT_ACTIVE" \
            "$UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN"
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert state_file.read_text(encoding="utf-8") == "schema=1\ntgbot_active=1\n"
    assert "active=1 known=1" in result.stdout


def test_restart_records_tgbot_as_required_when_it_starts_the_service(
    tmp_path: Path,
) -> None:
    shared_env = tmp_path / "shared.env"
    shared_env.write_text("TELEGRAM_BOT_TOKEN=configured\n", encoding="utf-8")
    events = tmp_path / "restart-events.log"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(RESTART))}
        emit_start() {{ :; }}
        emit_done() {{ :; }}
        emit_fail() {{ :; }}
        emit_warn() {{ :; }}
        emit_info() {{ :; }}
        log_info() {{ :; }}
        log_warn() {{ :; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        env_key_present() {{ grep -qE "^$2=.+" "$1"; }}
        lumen_update_activate_bound_image_override() {{ return 0; }}
        lumen_update_write_release_metadata() {{ return 0; }}
        lumen_update_harden_release_ownership() {{ return 0; }}
        lumen_verify_backup_service_layout_binding() {{ return 0; }}
        lumen_update_start_bound_service() {{
            printf 'start:%s\\n' "$2" >> {shlex.quote(str(events))}
            return 0
        }}
        ROOT={shlex.quote(str(tmp_path))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        TARGET_IMAGE_OVERRIDE_FILE={shlex.quote(str(tmp_path / "override.yml"))}
        LUMEN_UPDATE_BLUE_GREEN=0
        LUMEN_UPDATE_MODE=standard
        UPDATE_MIGRATION_STARTED=0
        TGBOT_IMAGE_READY=1
        update_phase_restart_services
        printf 'required=%s\\n' "$UPDATE_TGBOT_READINESS_REQUIRED"
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert events.read_text(encoding="utf-8").splitlines() == [
        "start:worker",
        "start:web",
        "start:api",
        "start:tgbot",
    ]
    assert "required=1" in result.stdout


@pytest.mark.parametrize("failed_dependency", ("postgres", "redis"))
def test_dependency_readiness_failure_rolls_back_before_commit(
    tmp_path: Path,
    failed_dependency: str,
) -> None:
    events = tmp_path / "events.log"
    current = tmp_path / "current"
    current.mkdir()
    shared_env = tmp_path / "shared.env"
    shared_env.write_text("", encoding="utf-8")
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(RECOVERY))}
        . {shlex.quote(str(HEALTH))}
        lumen_update_require_storage_identity() {{ return 0; }}
        env_key_present() {{ grep -qE "^$2=.+" "$1"; }}
        emit_start() {{ printf 'phase:start:%s\\n' "$1" >> {shlex.quote(str(events))}; }}
        emit_done() {{ printf 'phase:done:%s\\n' "$1" >> {shlex.quote(str(events))}; }}
        emit_fail() {{ printf 'phase:fail:%s\\n' "$1" >> {shlex.quote(str(events))}; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        log_warn() {{ printf 'WARN:%s\\n' "$*" >&2; }}
        log_update_restore_boundary() {{ :; }}
        lumen_update_wait_for_core_ready() {{
            printf 'core-ready:%s:{failed_dependency}\\n' "$1" \
                >> {shlex.quote(str(events))}
            return 1
        }}
        lumen_health_http() {{
            printf 'http:%s:{failed_dependency}\\n' "$1" >> {shlex.quote(str(events))}
            return 0
        }}
        lumen_health_compose() {{
            printf 'compose-liveness:%s\\n' "$*" >> {shlex.quote(str(events))}
            return 0
        }}
        mark_update_committed() {{
            printf 'commit\\n' >> {shlex.quote(str(events))}
            return 0
        }}
        discard_release_source_manifest_cache() {{ :; }}
        lumen_step_finalize_failure() {{ :; }}
        guard_automatic_app_rollback_compatibility() {{ return 0; }}
        restore_update_symlink_snapshot() {{
            printf 'rollback:links\\n' >> {shlex.quote(str(events))}
        }}
        restore_update_env_snapshot() {{
            printf 'rollback:env\\n' >> {shlex.quote(str(events))}
        }}
        lumen_update_load_original_service_state() {{
            UPDATE_ORIGINAL_TGBOT_ACTIVE=0
            UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN=1
            return 0
        }}
        lumen_update_restore_original_services() {{
            printf 'rollback:services:%s\\n' "$*" >> {shlex.quote(str(events))}
        }}
        lumen_release_remove_unused() {{ return 0; }}
        discard_update_state_snapshot() {{
            printf 'rollback:discard\\n' >> {shlex.quote(str(events))}
        }}
        lumen_update_journal_status() {{
            printf 'journal:%s\\n' "$1" >> {shlex.quote(str(events))}
        }}
        lumen_update_journal_failed() {{ return 0; }}
        ROOT={shlex.quote(str(tmp_path))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        CURRENT_LINK={shlex.quote(str(current))}
        CURRENT_ID=old
        NEW_ID=new
        NEW_RELEASE=""
        TARGET_TAG=v1.2.3
        UPDATE_MIGRATION_VERIFIED=0
        UPDATE_MIGRATION_STARTED=0
        UPDATE_RESTORE_POINT_TIMESTAMP=""
        UPDATE_RELEASE_SWITCHED=1
        UPDATE_OLD_SERVICES_STOPPED=1
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_STATE_COMMITTED=0
        UPDATE_STATE_COMMIT_UNKNOWN=0
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        UPDATE_ORIGINAL_TGBOT_ACTIVE=0
        UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN=1
        ROLLBACK_DONE=0
        UPDATE_ERROR_HANDLED=0
        _UPDATE_LAST_PHASE=health_check
        set +e
        (update_phase_health_check)
        rc=$?
        set -e
        [ "$rc" -ne 0 ]
        on_err "$rc"
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    lines = events.read_text(encoding="utf-8").splitlines()
    assert f"core-ready:{current}:{failed_dependency}" in lines
    assert "compose-liveness:api worker web" in lines
    assert "commit" not in lines
    assert lines.count("rollback:links") == 1
    assert lines.count("rollback:env") == 1
    assert any(line.startswith("rollback:services:") for line in lines)
    assert "journal:rolled_back" in lines


@pytest.mark.parametrize("decision_source", ("restart_flag", "shared_env"))
def test_tgbot_readiness_failure_blocks_update_commit(
    tmp_path: Path,
    decision_source: str,
) -> None:
    events = tmp_path / "events.log"
    current = tmp_path / "current"
    current.mkdir()
    shared_env = tmp_path / "shared.env"
    shared_env.write_text(
        ("TELEGRAM_BOT_TOKEN=configured\n" if decision_source == "shared_env" else ""),
        encoding="utf-8",
    )
    required = 1 if decision_source == "restart_flag" else 0
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(HEALTH))}
        lumen_update_require_storage_identity() {{ return 0; }}
        env_key_present() {{ grep -qE "^$2=.+" "$1"; }}
        emit_start() {{ :; }}
        emit_done() {{ :; }}
        emit_fail() {{ printf 'fail:%s\\n' "$1" >> {shlex.quote(str(events))}; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        log_warn() {{ :; }}
        log_update_restore_boundary() {{ :; }}
        lumen_update_wait_for_core_ready() {{ return 0; }}
        lumen_health_http() {{ return 0; }}
        lumen_health_compose() {{
            printf 'compose:%s\\n' "$*" >> {shlex.quote(str(events))}
            return 1
        }}
        mark_update_committed() {{
            printf 'commit\\n' >> {shlex.quote(str(events))}
            return 0
        }}
        ROOT={shlex.quote(str(tmp_path))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        CURRENT_LINK={shlex.quote(str(current))}
        NEW_ID=new
        TARGET_TAG=v1.2.3
        UPDATE_MIGRATION_VERIFIED=0
        UPDATE_TGBOT_READINESS_REQUIRED={required}
        set +e
        (update_phase_health_check)
        rc=$?
        set -e
        printf 'rc=%s\\n' "$rc"
        test "$rc" -ne 0
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    lines = events.read_text(encoding="utf-8").splitlines()
    assert "compose:api worker web tgbot" in lines
    assert "commit" not in lines
    assert "fail:health_check" in lines


def test_compose_inspect_error_is_not_treated_as_healthy() -> None:
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTAINER_RELEASE))}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        lumen_compose() {{ printf 'cid-tgbot\\n'; }}
        lumen_docker() {{ return 125; }}
        LUMEN_HEALTH_COMPOSE_ATTEMPTS=2
        LUMEN_HEALTH_COMPOSE_INTERVAL=0
        ! lumen_health_compose tgbot
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "未在 2×0s 内 healthy" in result.stderr


def test_image_jobs_only_readiness_failure_blocks_update_commit(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.log"
    current = tmp_path / "current"
    current.mkdir()
    shared_env = tmp_path / "shared.env"
    shared_env.write_text(
        "IMAGE_CHANNEL=image_jobs_only\nIMAGE_JOB_BASE_URL=https://jobs.example\n",
        encoding="utf-8",
    )
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(HEALTH))}
        lumen_update_require_storage_identity() {{ return 0; }}
        env_key_present() {{ grep -qE "^$2=.+" "$1"; }}
        lumen_env_value() {{
            sed -n "s/^$1=//p" "$2" | tail -n1
        }}
        emit_start() {{ :; }}
        emit_done() {{ :; }}
        emit_fail() {{ printf 'fail:%s\\n' "$1" >> {shlex.quote(str(events))}; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        log_warn() {{ :; }}
        log_update_restore_boundary() {{ :; }}
        lumen_update_wait_for_core_ready() {{ return 0; }}
        lumen_health_http() {{
            printf 'http:%s\\n' "$1" >> {shlex.quote(str(events))}
            [ "$1" != "https://jobs.example/health" ]
        }}
        lumen_health_compose() {{ return 0; }}
        mark_update_committed() {{
            printf 'commit\\n' >> {shlex.quote(str(events))}
            return 0
        }}
        ROOT={shlex.quote(str(tmp_path))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        CURRENT_LINK={shlex.quote(str(current))}
        NEW_ID=new
        TARGET_TAG=v1.2.3
        UPDATE_MIGRATION_VERIFIED=0
        set +e
        (update_phase_health_check)
        rc=$?
        set -e
        test "$rc" -ne 0
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    lines = events.read_text(encoding="utf-8").splitlines()
    assert "http:https://jobs.example/health" in lines
    assert "commit" not in lines
    assert "fail:health_check" in lines


def _install_pull_run(
    tmp_path: Path,
    *,
    tgbot_pull_rc: int,
    current_tag: str = "v1.2.3",
    fallback_main: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    events = tmp_path / "install-events.log"
    pull_state = tmp_path / "core-pull.state"
    release = tmp_path / "release"
    shared = tmp_path / "shared"
    release.mkdir()
    shared.mkdir()
    (shared / ".env").write_text(
        "LUMEN_IMAGE_REGISTRY=ghcr.io/cyeinfpro\n"
        f"LUMEN_IMAGE_TAG={current_tag}\n"
        "TELEGRAM_BOT_TOKEN=configured\n",
        encoding="utf-8",
    )
    result = _run(
        f"""
        set -u
        . {shlex.quote(str(INSTALL_SERVICES))}
        log_info() {{ :; }}
        log_warn() {{ printf 'WARN:%s\\n' "$*" >&2; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        emit_step_start() {{ :; }}
        emit_step_done() {{ :; }}
        env_file_get() {{
            case "$1" in
                LUMEN_IMAGE_REGISTRY) printf 'ghcr.io/cyeinfpro' ;;
                LUMEN_IMAGE_TAG) printf '%s' {shlex.quote(current_tag)} ;;
                TELEGRAM_BOT_TOKEN) printf 'configured' ;;
            esac
        }}
        env_key_present() {{
            [ "$2" = "TELEGRAM_BOT_TOKEN" ]
        }}
        env_file_set() {{ return 0; }}
        lumen_release_manifest_required() {{
            [ "$1" = "v1.2.3" ]
        }}
        lumen_env_truthy() {{ return 1; }}
        lumen_fetch_release_manifest() {{
            printf '{{}}\\n' > "$2"
            return 0
        }}
        verify_install_release_source_commit() {{
            printf 'commit:%s\\n' "$*" >> {shlex.quote(str(events))}
            return 0
        }}
        lumen_retry() {{
            shift 3
            "$@"
        }}
        _install_compose_pull_per_image() {{
            printf 'core-pull\\n' >> {shlex.quote(str(events))}
            if [ {1 if fallback_main else 0} -eq 1 ] \
                    && [ ! -e {shlex.quote(str(pull_state))} ]; then
                : > {shlex.quote(str(pull_state))}
                return 1
            fi
            return 0
        }}
        _install_compose() {{
            printf 'compose:%s\\n' "$*" >> {shlex.quote(str(events))}
            if [[ "$*" == *"--profile tgbot pull tgbot"* ]]; then
                return {tgbot_pull_rc}
            fi
            return 0
        }}
        lumen_verify_release_manifest_images() {{
            printf 'manifest:%s\\n' "$*" >> {shlex.quote(str(events))}
            return 0
        }}
        SHARED_DIR={shlex.quote(str(shared))}
        RELEASE_DIR={shlex.quote(str(release))}
        INSTALL_BUILD_FLAG=0
        INSTALL_IMAGE_TAG_OVERRIDE=""
        LUMEN_INSTALL_FALLBACK_MAIN={1 if fallback_main else 0}
        pull_or_build_images
        """
    )
    lines = events.read_text(encoding="utf-8").splitlines() if events.exists() else []
    return result, lines


def test_stable_install_tgbot_pull_failure_cannot_reuse_local_tag(
    tmp_path: Path,
) -> None:
    result, events = _install_pull_run(tmp_path, tgbot_pull_rc=18)

    assert result.returncode != 0
    assert any("--profile tgbot pull tgbot" in line for line in events)
    assert not any(line.startswith("manifest:") for line in events)
    assert "拒绝复用本地同 tag 未验证镜像" in result.stderr


def test_stable_install_verifies_tgbot_with_core_manifest_images(
    tmp_path: Path,
) -> None:
    result, events = _install_pull_run(tmp_path, tgbot_pull_rc=0)

    assert result.returncode == 0, result.stderr + result.stdout
    manifest = next(line for line in events if line.startswith("manifest:"))
    for service in ("api", "worker", "web", "tgbot"):
        assert f"--service {service}" in manifest
    commit_index = next(
        i for i, line in enumerate(events) if line.startswith("commit:")
    )
    manifest_index = next(
        i for i, line in enumerate(events) if line.startswith("manifest:")
    )
    assert commit_index < manifest_index


def test_non_release_install_can_continue_after_optional_tgbot_pull_failure(
    tmp_path: Path,
) -> None:
    result, events = _install_pull_run(
        tmp_path,
        tgbot_pull_rc=18,
        current_tag="main",
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert any("--profile tgbot pull tgbot" in line for line in events)
    assert not any(line.startswith("manifest:") for line in events)
    assert "主栈安装继续" in result.stderr


def test_main_fallback_clears_formal_tgbot_verification_requirement(
    tmp_path: Path,
) -> None:
    result, events = _install_pull_run(
        tmp_path,
        tgbot_pull_rc=18,
        fallback_main=True,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert events.count("core-pull") == 2
    assert any("--profile tgbot pull tgbot" in line for line in events)
    assert not any(line.startswith("manifest:") for line in events)
    assert "主栈安装继续" in result.stderr
