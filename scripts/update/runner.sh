#!/usr/bin/env bash
# Lumen modular update phase runner. This file only loads modules and orchestrates phases.

set -euo pipefail

UPDATE_MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "${UPDATE_MODULE_DIR}/.." && pwd)"
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    _LUMEN_ENTRY_LOCK_HELPER="${UPDATE_MODULE_DIR}/entry_lock.py"
    _LUMEN_ENTRY_LOCK_SCRIPTS_DIR="$(cd "${SCRIPT_DIR}" && pwd -P)"
    _LUMEN_ENTRY_LOCK_PATH="${_LUMEN_ENTRY_LOCK_SCRIPTS_DIR}.lumen-self-update.lock"
    if [ ! -f "${_LUMEN_ENTRY_LOCK_HELPER}" ] \
            || [ -L "${_LUMEN_ENTRY_LOCK_HELPER}" ]; then
        printf '[ERROR] updater 脚本单元缺少安全入口锁 helper。\n' >&2
        exit 78
    fi
    if ! python3 "${_LUMEN_ENTRY_LOCK_HELPER}" verify \
            "${LUMEN_SCRIPT_UNIT_LOCK_FD:-}" \
            "${_LUMEN_ENTRY_LOCK_PATH}" >/dev/null 2>&1; then
        exec python3 "${_LUMEN_ENTRY_LOCK_HELPER}" exec \
            "${_LUMEN_ENTRY_LOCK_PATH}" \
            "${LUMEN_SELF_UPDATE_LOCK_TIMEOUT:-60}" \
            -- bash "${BASH_SOURCE[0]}" "$@"
    fi
    unset _LUMEN_ENTRY_LOCK_HELPER _LUMEN_ENTRY_LOCK_SCRIPTS_DIR \
        _LUMEN_ENTRY_LOCK_PATH
fi
_LUMEN_UPDATE_INPUT_DEPLOY_ROOT="${LUMEN_DEPLOY_ROOT-}"
_LUMEN_UPDATE_INPUT_UPDATE_ROOT="${LUMEN_UPDATE_ROOT-}"
_LUMEN_UPDATE_INPUT_DATA_ROOT="${LUMEN_DATA_ROOT-}"
_LUMEN_UPDATE_INPUT_DB_ROOT="${LUMEN_DB_ROOT-}"
_LUMEN_UPDATE_INPUT_BACKUP_ROOT="${LUMEN_BACKUP_ROOT-}"
_LUMEN_UPDATE_INPUT_POSTGRES_UID="${LUMEN_POSTGRES_UID-}"
_LUMEN_UPDATE_INPUT_POSTGRES_GID="${LUMEN_POSTGRES_GID-}"
_LUMEN_UPDATE_INPUT_REDIS_UID="${LUMEN_REDIS_UID-}"
_LUMEN_UPDATE_INPUT_REDIS_GID="${LUMEN_REDIS_GID-}"
_LUMEN_UPDATE_INPUT_APP_UID="${LUMEN_APP_UID-}"
_LUMEN_UPDATE_INPUT_APP_GID="${LUMEN_APP_GID-}"
_LUMEN_UPDATE_INPUT_APP_STORAGE_GID="${LUMEN_APP_STORAGE_GID-}"

# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"
# shellcheck source=update/phase_contract.sh
. "${UPDATE_MODULE_DIR}/phase_contract.sh"
# shellcheck source=update/journal.sh
. "${UPDATE_MODULE_DIR}/journal.sh"
# shellcheck source=update/bootstrap.sh
. "${UPDATE_MODULE_DIR}/bootstrap.sh"
# shellcheck source=update/common.sh
. "${UPDATE_MODULE_DIR}/common.sh"
# shellcheck source=update/backup/restore_points.sh
. "${UPDATE_MODULE_DIR}/backup/restore_points.sh"
# shellcheck source=update/release/manifest.sh
. "${UPDATE_MODULE_DIR}/release/manifest.sh"
# shellcheck source=update/services/compose.sh
. "${UPDATE_MODULE_DIR}/services/compose.sh"
# shellcheck source=update/backup/migration_helpers.sh
. "${UPDATE_MODULE_DIR}/backup/migration_helpers.sh"
# shellcheck source=update/release/runner_units.sh
. "${UPDATE_MODULE_DIR}/release/runner_units.sh"
# shellcheck source=update/release/source_helpers.sh
. "${UPDATE_MODULE_DIR}/release/source_helpers.sh"
# shellcheck source=update/recovery/cleanup.sh
. "${UPDATE_MODULE_DIR}/recovery/cleanup.sh"
# shellcheck source=update/recovery/state.sh
. "${UPDATE_MODULE_DIR}/recovery/state.sh"
# shellcheck source=update/recovery/blue_green.sh
. "${UPDATE_MODULE_DIR}/recovery/blue_green.sh"
# shellcheck source=update/release/self_update.sh
. "${UPDATE_MODULE_DIR}/release/self_update.sh"
# shellcheck source=update/release/check.sh
. "${UPDATE_MODULE_DIR}/release/check.sh"
# shellcheck source=update/backup/preflight.sh
. "${UPDATE_MODULE_DIR}/backup/preflight.sh"
# shellcheck source=update/release/fetch.sh
. "${UPDATE_MODULE_DIR}/release/fetch.sh"
# shellcheck source=update/release/digest.sh
. "${UPDATE_MODULE_DIR}/release/digest.sh"
# shellcheck source=update/release/activate.sh
. "${UPDATE_MODULE_DIR}/release/activate.sh"
# shellcheck source=update/backup/phases.sh
. "${UPDATE_MODULE_DIR}/backup/phases.sh"
# shellcheck source=update/services/switch.sh
. "${UPDATE_MODULE_DIR}/services/switch.sh"
# shellcheck source=update/services/release_activation.sh
. "${UPDATE_MODULE_DIR}/services/release_activation.sh"
# shellcheck source=update/services/restart.sh
. "${UPDATE_MODULE_DIR}/services/restart.sh"
# shellcheck source=update/services/health.sh
. "${UPDATE_MODULE_DIR}/services/health.sh"

trap 'rc=$?; on_err "$rc"' ERR
trap 'rc=$?; [ "$rc" -ne 0 ] && on_err "$rc" || true; lumen_release_lock' EXIT

update_run_phase() {
    local phase="$1"
    local implementation="$2"
    shift 2
    if [ "${LUMEN_UPDATE_JOURNAL_RESUMED}" = "1" ] \
            && lumen_update_journal_phase_completed "${phase}"; then
        if [ "${phase}" = "self_update_scripts" ] \
                && ! lumen_update_script_unit_complete "${SCRIPT_DIR}"; then
            log_error "resume 拒绝跳过 self_update_scripts：完整 updater unit 的 commit/type/mode/hash 校验失败。"
            return 78
        fi
        lumen_emit_info "phase=${phase}" "key=resume" "value=already_completed"
        lumen_emit_step "phase=${phase}" "status=done" "rc=0" "resumed=1"
        return 0
    fi
    "${implementation}" "$@"
}

do_update() {
    update_run_phase lock update_phase_lock || return $?
    update_run_phase self_update_scripts update_phase_self_update_scripts "$@" || return $?
    update_run_phase check update_phase_check || return $?
    if [ "${SKIP_TO_CLEANUP:-0}" -eq 1 ]; then
        run_update_cleanup "noop"
        return 0
    fi
    update_run_phase preflight update_phase_preflight || return $?
    update_run_phase backup_preflight update_phase_backup_preflight || return $?
    update_run_phase fetch_release update_phase_fetch_release || return $?
    update_run_phase set_image_tag update_phase_set_image_tag || return $?
    update_run_phase pull_images update_phase_pull_images || return $?
    update_run_phase check_storage update_phase_check_storage || return $?
    update_run_phase start_infra update_phase_start_infra || return $?
    update_run_phase migrate_db update_phase_migrate_db || return $?
    update_run_phase switch update_phase_switch || return $?
    update_run_phase restart_services update_phase_restart_services || return $?
    update_run_phase health_check update_phase_health_check || return $?
    run_update_cleanup "updated" || return $?
    update_finish_success || return $?
}

lumen_acquire_lock "${ROOT}" "update.sh"
lumen_update_journal_init
if ! lumen_update_bind_expected_scripts_commit "${SCRIPT_DIR}"; then
    lumen_update_journal_failed resume_validation 78 || true
    trap - ERR
    trap 'lumen_release_lock' EXIT
    exit 78
fi
if [ "${LUMEN_UPDATE_JOURNAL_RESUMED}" = "1" ]; then
    log_info "恢复 update journal：operation_id=${OPERATION_ID}"
    if ! validate_resumed_update_state; then
        lumen_update_journal_failed resume_validation 78 || true
        trap - ERR
        trap 'lumen_release_lock' EXIT
        exit 78
    fi
fi
trap 'rc=$?; [ "$rc" -ne 0 ] && on_err "$rc" || true; lumen_release_lock' EXIT

if lumen_with_lock "update" 1830 do_update "$@"; then
    if ! lumen_update_wait_for_core_ready "${ROOT}/current"; then
        log_error "最终 API/Worker readiness 未通过；保留 update journal 与 trigger。"
        on_err 1
        trap - ERR
        trap 'lumen_release_lock' EXIT
        exit 1
    fi
    lumen_update_journal_status complete
    discard_update_state_snapshot
    lumen_update_clear_expected_scripts_commit
    trap - ERR
    trap 'lumen_release_lock' EXIT
    exit 0
fi

exit 1
