#!/usr/bin/env bash
# Lumen modular update phase runner. This file only loads modules and orchestrates phases.

set -euo pipefail

UPDATE_MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "${UPDATE_MODULE_DIR}/.." && pwd)"
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
# shellcheck source=update/release/activate.sh
. "${UPDATE_MODULE_DIR}/release/activate.sh"
# shellcheck source=update/backup/phases.sh
. "${UPDATE_MODULE_DIR}/backup/phases.sh"
# shellcheck source=update/services/switch.sh
. "${UPDATE_MODULE_DIR}/services/switch.sh"
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
        lumen_emit_info "phase=${phase}" "key=resume" "value=already_completed"
        lumen_emit_step "phase=${phase}" "status=done" "rc=0" "resumed=1"
        return 0
    fi
    "${implementation}" "$@"
}

do_update() {
    update_run_phase lock update_phase_lock
    update_run_phase self_update_scripts update_phase_self_update_scripts "$@"
    update_run_phase check update_phase_check
    if [ "${SKIP_TO_CLEANUP:-0}" -eq 1 ]; then
        run_update_cleanup "noop"
        lumen_update_journal_status complete
        return 0
    fi
    update_run_phase preflight update_phase_preflight
    update_run_phase backup_preflight update_phase_backup_preflight
    update_run_phase fetch_release update_phase_fetch_release
    update_run_phase set_image_tag update_phase_set_image_tag
    update_run_phase pull_images update_phase_pull_images
    update_run_phase check_storage update_phase_check_storage
    update_run_phase start_infra update_phase_start_infra
    update_run_phase migrate_db update_phase_migrate_db
    update_run_phase switch update_phase_switch
    update_run_phase restart_services update_phase_restart_services
    update_run_phase health_check update_phase_health_check
    run_update_cleanup "updated"
    update_finish_success
}

lumen_acquire_lock "${ROOT}" "update.sh"
lumen_update_journal_init
if [ "${LUMEN_UPDATE_JOURNAL_RESUMED}" = "1" ]; then
    log_info "恢复 update journal：operation_id=${OPERATION_ID}"
fi
trap 'rc=$?; [ "$rc" -ne 0 ] && on_err "$rc" || true; lumen_release_lock' EXIT

if lumen_with_lock "update" 1830 do_update "$@"; then
    lumen_update_journal_status complete
    discard_update_state_snapshot
    trap - ERR
    trap 'lumen_release_lock' EXIT
    exit 0
fi

exit 1
