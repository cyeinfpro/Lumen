#!/usr/bin/env bash
# Transactional updater snapshots, rollback, and error recovery.

snapshot_update_state() {
    if [ "${UPDATE_STATE_SNAPSHOT_READY}" -eq 1 ]; then
        return 0
    fi
    if [ ! -f "${SHARED_ENV}" ]; then
        log_error "无法快照不存在的 shared env：${SHARED_ENV}"
        return 1
    fi
    UPDATE_ENV_SNAPSHOT="$(mktemp "${SHARED_DIR}/.env.update.XXXXXX")" \
        || return 1
    if ! cp -p "${SHARED_ENV}" "${UPDATE_ENV_SNAPSHOT}" \
            || ! chmod 0600 "${UPDATE_ENV_SNAPSHOT}"; then
        rm -f "${UPDATE_ENV_SNAPSHOT}" 2>/dev/null || true
        UPDATE_ENV_SNAPSHOT=""
        return 1
    fi
    if [ -L "${ROOT}/current" ]; then
        UPDATE_ORIGINAL_CURRENT_PRESENT=1
        UPDATE_ORIGINAL_CURRENT_TARGET="$(readlink "${ROOT}/current")"
    fi
    if [ -L "${ROOT}/previous" ]; then
        UPDATE_ORIGINAL_PREVIOUS_PRESENT=1
        UPDATE_ORIGINAL_PREVIOUS_TARGET="$(readlink "${ROOT}/previous")"
    fi
    if lumen_systemd_runtime_available; then
        if ! UPDATE_HOST_ARTIFACT_SNAPSHOT="$(mktemp -d "${SHARED_DIR}/.host.update.XXXXXX")"; then
            rm -f "${UPDATE_ENV_SNAPSHOT}" 2>/dev/null || true
            UPDATE_ENV_SNAPSHOT=""
            return 1
        fi
        if ! lumen_snapshot_operations_host_artifacts \
                "${UPDATE_HOST_ARTIFACT_SNAPSHOT}"; then
            lumen_discard_host_artifact_snapshot "${UPDATE_HOST_ARTIFACT_SNAPSHOT}"
            UPDATE_HOST_ARTIFACT_SNAPSHOT=""
            rm -f "${UPDATE_ENV_SNAPSHOT}" 2>/dev/null || true
            UPDATE_ENV_SNAPSHOT=""
            return 1
        fi
    fi
    UPDATE_STATE_SNAPSHOT_READY=1
    return 0
}

restore_update_env_snapshot() {
    [ "${UPDATE_STATE_SNAPSHOT_READY}" -eq 1 ] || return 0
    [ -f "${UPDATE_ENV_SNAPSHOT}" ] || return 1
    local restore_tmp="${SHARED_DIR}/.env.restore.$$"
    rm -f "${restore_tmp}" 2>/dev/null || true
    if ! cp -p "${UPDATE_ENV_SNAPSHOT}" "${restore_tmp}" \
            || ! mv -f "${restore_tmp}" "${SHARED_ENV}"; then
        rm -f "${restore_tmp}" 2>/dev/null || true
        return 1
    fi
    return 0
}

restore_update_symlink_snapshot() {
    local rc=0
    if [ "${UPDATE_ORIGINAL_CURRENT_PRESENT}" -eq 1 ]; then
        lumen_atomic_replace_symlink \
            "${UPDATE_ORIGINAL_CURRENT_TARGET}" "${ROOT}/current" || rc=1
    elif [ -L "${ROOT}/current" ]; then
        rm -f "${ROOT}/current" || rc=1
    fi
    if [ "${UPDATE_ORIGINAL_PREVIOUS_PRESENT}" -eq 1 ]; then
        lumen_atomic_replace_symlink \
            "${UPDATE_ORIGINAL_PREVIOUS_TARGET}" "${ROOT}/previous" || rc=1
    elif [ -L "${ROOT}/previous" ]; then
        rm -f "${ROOT}/previous" || rc=1
    fi
    return "${rc}"
}

discard_update_state_snapshot() {
    if [ -n "${UPDATE_ENV_SNAPSHOT}" ]; then
        rm -f "${UPDATE_ENV_SNAPSHOT}" 2>/dev/null || true
    fi
    UPDATE_ENV_SNAPSHOT=""
    lumen_discard_host_artifact_snapshot "${UPDATE_HOST_ARTIFACT_SNAPSHOT}"
    UPDATE_HOST_ARTIFACT_SNAPSHOT=""
    UPDATE_STATE_SNAPSHOT_READY=0
}

restore_uncommitted_update_state() {
    local rc=0
    if ! restore_update_symlink_snapshot; then
        log_error "rollback：current/previous symlink 未能完整恢复。"
        rc=1
    fi
    if ! restore_update_env_snapshot; then
        log_error "rollback：shared/.env 原字节恢复失败；快照保留在 ${UPDATE_ENV_SNAPSHOT:-<missing>}。"
        rc=1
    else
        log_warn "rollback：shared/.env 已按更新前快照原字节恢复。"
    fi
    if [ -n "${UPDATE_HOST_ARTIFACT_SNAPSHOT}" ]; then
        if ! lumen_restore_operations_host_artifacts \
                "${UPDATE_HOST_ARTIFACT_SNAPSHOT}"; then
            log_error "rollback：systemd units 或 host 脚本未能完整恢复。"
            rc=1
        else
            log_warn "rollback：systemd units 与 host 脚本已恢复到更新前快照。"
        fi
    fi
    if [ "${UPDATE_RELEASE_SWITCHED}" -eq 1 ] \
            || [ "${UPDATE_OLD_SERVICES_STOPPED}" -eq 1 ]; then
        log_warn "rollback：重新拉起更新前 release 的 worker/web/api。"
        if ! lumen_compose_in "${ROOT}/current" \
                up --pull missing -d worker web api; then
            log_error "rollback：更新前 release 核心服务恢复失败。"
            rc=1
        else
            UPDATE_RELEASE_SWITCHED=0
            UPDATE_OLD_SERVICES_STOPPED=0
        fi
    fi
    if [ -n "${NEW_RELEASE}" ] && [ -d "${NEW_RELEASE}" ]; then
        if ! lumen_release_remove_unused "${ROOT}" "${NEW_ID}"; then
            log_warn "rollback：未能删除未启用 release ${NEW_ID}。"
            rc=1
        fi
    fi
    if [ "${rc}" -eq 0 ]; then
        discard_update_state_snapshot
    fi
    return "${rc}"
}

UPDATE_ERROR_HANDLED=0
on_err() {
    local rc="${1:-1}"
    [ "${rc}" -eq 0 ] && return 0
    if [ "${UPDATE_ERROR_HANDLED}" -eq 1 ]; then
        return 0
    fi
    UPDATE_ERROR_HANDLED=1
    discard_release_source_manifest_cache
    lumen_step_finalize_failure "${rc}"
    log_error "更新失败：返回码 ${rc}"
    if [ -n "${UPDATE_RESTORE_POINT_TIMESTAMP}" ] \
            || [ "${UPDATE_MIGRATION_STARTED}" -eq 1 ]; then
        log_update_restore_boundary update
    fi
    if [ "${ROLLBACK_DONE}" -eq 0 ]; then
        ROLLBACK_DONE=1
        if [ "${UPDATE_STATE_COMMITTED}" -eq 0 ] \
                && [ "${UPDATE_STATE_SNAPSHOT_READY}" -eq 1 ]; then
            if restore_uncommitted_update_state; then
                lumen_update_journal_status rolled_back || true
            else
                lumen_update_journal_failed \
                    "${_UPDATE_LAST_PHASE:-update}" "${rc}" || true
            fi
        else
            discard_update_state_snapshot
            lumen_update_journal_failed \
                "${_UPDATE_LAST_PHASE:-update}" "${rc}" || true
        fi
    fi
    return 0
}
