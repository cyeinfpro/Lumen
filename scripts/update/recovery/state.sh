#!/usr/bin/env bash
# Transactional updater snapshots, rollback, and error recovery.

lumen_update_original_service_state_file() {
    [ -n "${UPDATE_ENV_SNAPSHOT:-}" ] || return 1
    printf '%s\n' "${UPDATE_ENV_SNAPSHOT}.services"
}

lumen_update_load_original_service_state() {
    if [ "${UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN:-0}" -eq 1 ]; then
        case "${UPDATE_ORIGINAL_TGBOT_ACTIVE:-}" in
            0|1) return 0 ;;
            *) return 1 ;;
        esac
    fi

    local state_file=""
    local key="" value="" schema="" tgbot_active=""
    local seen_schema=0 seen_tgbot=0
    state_file="$(lumen_update_original_service_state_file)" || return 1
    if [ -L "${state_file}" ] || [ ! -f "${state_file}" ]; then
        return 1
    fi
    while IFS='=' read -r key value; do
        case "${key}" in
            schema)
                [ "${seen_schema}" -eq 0 ] || return 1
                schema="${value}"
                seen_schema=1
                ;;
            tgbot_active)
                [ "${seen_tgbot}" -eq 0 ] || return 1
                tgbot_active="${value}"
                seen_tgbot=1
                ;;
            "")
                [ -z "${value}" ] || return 1
                ;;
            *)
                return 1
                ;;
        esac
    done < "${state_file}"
    if [ "${schema}" != "1" ] \
            || { [ "${tgbot_active}" != "0" ] && [ "${tgbot_active}" != "1" ]; }; then
        return 1
    fi
    UPDATE_ORIGINAL_TGBOT_ACTIVE="${tgbot_active}"
    UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN=1
    return 0
}

lumen_update_restore_original_services() {
    local compose_dir="$1"
    shift
    local services=("$@")
    if [ -f "${compose_dir}/docker-compose.yml" ] \
            && grep -Eq '^[[:space:]]{2}agent-runtime:[[:space:]]*$' \
                "${compose_dir}/docker-compose.yml"; then
        services=(agent-runtime "${services[@]}")
    else
        lumen_docker stop lumen-agent-runtime >/dev/null 2>&1 || true
    fi
    if ! lumen_update_load_original_service_state; then
        log_error "无法证明更新前 tgbot 是否 active，拒绝猜测恢复服务集合。"
        return 1
    fi
    if [ "${UPDATE_ORIGINAL_TGBOT_ACTIVE}" -eq 1 ]; then
        if ! lumen_compose_in "${compose_dir}" --profile tgbot \
                up --pull missing -d --wait --force-recreate "${services[@]}" tgbot; then
            return 1
        fi
    else
        if ! lumen_compose_in "${compose_dir}" \
                up --pull missing -d --wait --force-recreate "${services[@]}"; then
            return 1
        fi
    fi
    lumen_update_wait_for_core_ready "${compose_dir}"
}

snapshot_update_state() {
    if [ "${UPDATE_STATE_SNAPSHOT_READY}" -eq 1 ]; then
        return 0
    fi
    if [ ! -f "${SHARED_ENV}" ]; then
        log_error "无法快照不存在的 shared env：${SHARED_ENV}"
        return 1
    fi
    if { [ -e "${ROOT}/current" ] && [ ! -L "${ROOT}/current" ]; } \
            || { [ -e "${ROOT}/previous" ] && [ ! -L "${ROOT}/previous" ]; }; then
        log_error "current/previous 存在非符号链接对象，拒绝创建可恢复快照。"
        return 1
    fi
    UPDATE_ENV_SNAPSHOT="$(mktemp "${SHARED_DIR}/.env.update.XXXXXX")" \
        || return 1
    if ! lumen_update_copy_file_durable \
            "${SHARED_ENV}" "${UPDATE_ENV_SNAPSHOT}"; then
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
    if ! UPDATE_SNAPSHOT_ENV_SHA256="$(
        lumen_update_file_sha256 "${UPDATE_ENV_SNAPSHOT}"
    )"; then
        log_error "无法计算 shared env 快照摘要，拒绝继续。"
        lumen_discard_host_artifact_snapshot "${UPDATE_HOST_ARTIFACT_SNAPSHOT}"
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        rm -f "${UPDATE_ENV_SNAPSHOT}" 2>/dev/null || true
        UPDATE_ENV_SNAPSHOT=""
        UPDATE_SNAPSHOT_ENV_SHA256=""
        return 1
    fi
    UPDATE_SNAPSHOT_LINKS_KNOWN=1
    UPDATE_STATE_SNAPSHOT_READY=1
    if ! lumen_update_journal_snapshot_state; then
        log_error "无法持久化 update journal 快照，拒绝继续。"
        lumen_discard_host_artifact_snapshot "${UPDATE_HOST_ARTIFACT_SNAPSHOT}"
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        rm -f "${UPDATE_ENV_SNAPSHOT}" 2>/dev/null || true
        UPDATE_ENV_SNAPSHOT=""
        UPDATE_SNAPSHOT_ENV_SHA256=""
        UPDATE_SNAPSHOT_LINKS_KNOWN=0
        UPDATE_STATE_SNAPSHOT_READY=0
        return 1
    fi
    return 0
}

restore_update_env_snapshot() {
    [ "${UPDATE_STATE_SNAPSHOT_READY}" -eq 1 ] || return 0
    [ -f "${UPDATE_ENV_SNAPSHOT}" ] || return 1
    local restore_tmp="${SHARED_DIR}/.env.restore.$$"
    rm -f "${restore_tmp}" 2>/dev/null || true
    if ! lumen_update_copy_file_durable \
            "${UPDATE_ENV_SNAPSHOT}" "${restore_tmp}" \
            || ! mv -f "${restore_tmp}" "${SHARED_ENV}" \
            || ! lumen_update_fsync_directory "${SHARED_DIR}"; then
        rm -f "${restore_tmp}" 2>/dev/null || true
        return 1
    fi
    return 0
}

restore_update_symlink_snapshot() {
    local rc=0
    if [ "${UPDATE_SNAPSHOT_LINKS_KNOWN:-0}" -ne 1 ]; then
        log_error "rollback：原始 current/previous 快照未知，拒绝破坏性恢复。"
        return 1
    fi
    if [ "${UPDATE_ORIGINAL_CURRENT_PRESENT}" -eq 1 ]; then
        if [ -z "${UPDATE_ORIGINAL_CURRENT_TARGET}" ]; then
            log_error "rollback：原始 current target 缺失，拒绝恢复。"
            return 1
        fi
        lumen_atomic_replace_symlink \
            "${UPDATE_ORIGINAL_CURRENT_TARGET}" "${ROOT}/current" || rc=1
    elif [ -L "${ROOT}/current" ]; then
        rm -f "${ROOT}/current" || rc=1
    elif [ -e "${ROOT}/current" ]; then
        log_error "rollback：current 已被非符号链接对象占用，拒绝删除。"
        rc=1
    fi
    if [ "${UPDATE_ORIGINAL_PREVIOUS_PRESENT}" -eq 1 ]; then
        if [ -z "${UPDATE_ORIGINAL_PREVIOUS_TARGET}" ]; then
            log_error "rollback：原始 previous target 缺失，拒绝恢复。"
            return 1
        fi
        lumen_atomic_replace_symlink \
            "${UPDATE_ORIGINAL_PREVIOUS_TARGET}" "${ROOT}/previous" || rc=1
    elif [ -L "${ROOT}/previous" ]; then
        rm -f "${ROOT}/previous" || rc=1
    elif [ -e "${ROOT}/previous" ]; then
        log_error "rollback：previous 已被非符号链接对象占用，拒绝删除。"
        rc=1
    fi
    if [ "${rc}" -eq 0 ] && ! lumen_update_fsync_directory "${ROOT}"; then
        rc=1
    fi
    return "${rc}"
}

discard_update_state_snapshot() {
    local service_state_file=""
    service_state_file="$(
        lumen_update_original_service_state_file 2>/dev/null || true
    )"
    if [ -n "${service_state_file}" ]; then
        rm -f "${service_state_file}" 2>/dev/null || true
    fi
    if [ -n "${UPDATE_ENV_SNAPSHOT}" ]; then
        rm -f "${UPDATE_ENV_SNAPSHOT}" 2>/dev/null || true
    fi
    UPDATE_ENV_SNAPSHOT=""
    UPDATE_SNAPSHOT_ENV_SHA256=""
    lumen_discard_host_artifact_snapshot "${UPDATE_HOST_ARTIFACT_SNAPSHOT}"
    UPDATE_HOST_ARTIFACT_SNAPSHOT=""
    UPDATE_SNAPSHOT_LINKS_KNOWN=0
    UPDATE_STATE_SNAPSHOT_READY=0
    UPDATE_ORIGINAL_TGBOT_ACTIVE=0
    UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN=0
}

mark_update_committed() {
    if ! lumen_update_journal_mark_committed; then
        UPDATE_STATE_COMMIT_UNKNOWN=1
        export UPDATE_STATE_COMMIT_UNKNOWN
        log_error "核心服务已切换，但 committed marker 持久化失败；拒绝执行 pre-commit rollback。"
        return 1
    fi
    UPDATE_STATE_COMMIT_UNKNOWN=0
    export UPDATE_STATE_COMMIT_UNKNOWN
    return 0
}

validate_resumed_update_state() {
    UPDATE_RUNTIME_MIGRATION_HEAD=""
    if [ "${UPDATE_MIGRATION_VERIFIED:-0}" -eq 1 ] \
            && [ -n "${UPDATE_MIGRATION_HEAD:-}" ]; then
        if [ -z "${NEW_RELEASE:-}" ] || [ ! -d "${NEW_RELEASE}" ]; then
            log_error "resume 校验失败：目标 release 不存在。"
            return 1
        fi
        UPDATE_RUNTIME_MIGRATION_HEAD="$(
            current_alembic_revision "${NEW_RELEASE}" 2>/dev/null || true
        )"
    fi
    if ! lumen_update_journal_validate_resume; then
        log_error "resume invariant 校验失败，拒绝自动续跑且不执行 rollback。"
        return 1
    fi
    if [ -n "${UPDATE_RESTORE_POINT_TIMESTAMP:-}" ] \
            && ! validate_bound_update_restore_point; then
        log_error "resume 校验失败：已绑定恢复点缺失、被替换或归档无效。"
        return 1
    fi

    # Proxy credentials are intentionally excluded from the journal. Rebuild
    # the ephemeral proxy environment from shared/.env after a safe resume.
    LUMEN_PROXY_URL=""
    if lumen_configure_proxy_env "${SHARED_ENV}" >/dev/null 2>&1; then
        LUMEN_PROXY_URL="${LUMEN_UPDATE_PROXY_URL:-${LUMEN_HTTP_PROXY:-}}"
    fi
    return 0
}

restore_uncommitted_update_state() {
    local rc=0
    if ! guard_automatic_app_rollback_compatibility \
            "${ROOT}/releases/${CURRENT_ID:-}" "${NEW_RELEASE:-}"; then
        log_error "rollback：schema capability guard 拒绝恢复旧应用状态；保留现场等待前向恢复或整库回滚。"
        return 1
    fi
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
        if lumen_update_load_original_service_state; then
            if [ "${UPDATE_ORIGINAL_TGBOT_ACTIVE}" -eq 1 ]; then
                log_warn "rollback：重新拉起更新前 release 的核心服务与 tgbot。"
            else
                log_warn "rollback：重新拉起更新前 release 的核心服务；tgbot 原本未运行。"
            fi
        else
            log_error "rollback：更新前 tgbot active 状态未知；仅恢复核心服务并要求人工确认 bot。"
            rc=1
        fi
        if [ "${UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN:-0}" -eq 1 ]; then
            if ! lumen_update_restore_original_services \
                    "${ROOT}/current" api worker web; then
                log_error "rollback：更新前 release 服务恢复失败。"
                rc=1
            else
                UPDATE_RELEASE_SWITCHED=0
                UPDATE_OLD_SERVICES_STOPPED=0
            fi
        else
            local fallback_services=(api worker web)
            if grep -Eq '^[[:space:]]{2}agent-runtime:[[:space:]]*$' \
                    "${ROOT}/current/docker-compose.yml" 2>/dev/null; then
                fallback_services=(agent-runtime api worker web)
            fi
            if ! lumen_compose_in "${ROOT}/current" \
                    up --pull missing -d --wait --force-recreate \
                    "${fallback_services[@]}" \
                    || ! lumen_update_wait_for_core_ready "${ROOT}/current"; then
                log_error "rollback：更新前 release 核心服务未通过 API/Worker readiness。"
                rc=1
            fi
        fi
    fi
    if [ "${rc}" -eq 0 ] \
            && [ -n "${NEW_RELEASE}" ] && [ -d "${NEW_RELEASE}" ]; then
        if ! lumen_release_remove_unused "${ROOT}" "${NEW_ID}"; then
            log_warn "rollback：未能删除未启用 release ${NEW_ID}。"
            rc=1
        fi
    fi
    if [ "${rc}" -eq 0 ]; then
        discard_update_state_snapshot
    else
        log_error "rollback：readiness 或恢复步骤失败，保留 update snapshot/journal 与 release 证据。"
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
    trap '' INT TERM HUP
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
                && [ "${UPDATE_STATE_COMMIT_UNKNOWN:-0}" -eq 0 ] \
                && [ "${UPDATE_STATE_SNAPSHOT_READY}" -eq 1 ] \
                && [ "${UPDATE_SNAPSHOT_LINKS_KNOWN:-0}" -eq 1 ]; then
            if restore_uncommitted_update_state; then
                lumen_update_journal_status rolled_back || true
            else
                lumen_update_journal_failed \
                    "${_UPDATE_LAST_PHASE:-update}" "${rc}" || true
            fi
        elif [ "${UPDATE_STATE_COMMITTED}" -eq 1 ] \
                || [ "${UPDATE_STATE_COMMIT_UNKNOWN:-0}" -eq 1 ]; then
            discard_update_state_snapshot
            lumen_update_journal_status manual_required || true
        else
            log_error "rollback 快照不完整或未知，保留现场并拒绝破坏性恢复。"
            lumen_update_journal_failed \
                "${_UPDATE_LAST_PHASE:-update}" "${rc}" || true
        fi
    fi
    return 0
}
