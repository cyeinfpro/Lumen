#!/usr/bin/env bash
# Install transaction snapshot, rollback, and signal handling.
# Sourced by scripts/install.sh after raw bootstrap has completed.

snapshot_install_state() {
    local shared_env="${SHARED_DIR}/.env"
    if [ -L "${DEPLOY_ROOT}/current" ]; then
        INSTALL_ORIGINAL_CURRENT_PRESENT=1
        INSTALL_ORIGINAL_CURRENT_TARGET="$(readlink "${DEPLOY_ROOT}/current")"
    fi
    if [ -L "${DEPLOY_ROOT}/previous" ]; then
        INSTALL_ORIGINAL_PREVIOUS_PRESENT=1
        INSTALL_ORIGINAL_PREVIOUS_TARGET="$(readlink "${DEPLOY_ROOT}/previous")"
    fi
    if [ "${INSTALL_ORIGINAL_CURRENT_PRESENT}" -eq 1 ] \
            && [ -f "${DEPLOY_ROOT}/current/docker-compose.yml" ]; then
        local running service
        running="$(lumen_compose_in "${DEPLOY_ROOT}/current" \
            ps --status running --services 2>/dev/null || true)"
        while IFS= read -r service; do
            case "${service}" in
                postgres|redis|api|worker|web|tgbot)
                    INSTALL_ORIGINAL_RUNNING_SERVICES="${INSTALL_ORIGINAL_RUNNING_SERVICES}${service}"$'\n'
                    ;;
            esac
        done <<< "${running}"
    fi
    if [ -f "${shared_env}" ]; then
        INSTALL_ENV_SNAPSHOT="$(mktemp "${SHARED_DIR}/.env.install.XXXXXX")" \
            || return 1
        if ! cp -p "${shared_env}" "${INSTALL_ENV_SNAPSHOT}"; then
            rm -f "${INSTALL_ENV_SNAPSHOT}" 2>/dev/null || true
            INSTALL_ENV_SNAPSHOT=""
            return 1
        fi
    fi
    if lumen_systemd_runtime_available; then
        if ! INSTALL_HOST_ARTIFACT_SNAPSHOT="$(mktemp -d "${SHARED_DIR}/.host.install.XXXXXX")"; then
            rm -f "${INSTALL_ENV_SNAPSHOT}" 2>/dev/null || true
            INSTALL_ENV_SNAPSHOT=""
            return 1
        fi
        if ! lumen_snapshot_operations_host_artifacts \
                "${INSTALL_HOST_ARTIFACT_SNAPSHOT}"; then
            lumen_discard_host_artifact_snapshot "${INSTALL_HOST_ARTIFACT_SNAPSHOT}"
            INSTALL_HOST_ARTIFACT_SNAPSHOT=""
            rm -f "${INSTALL_ENV_SNAPSHOT}" 2>/dev/null || true
            INSTALL_ENV_SNAPSHOT=""
            return 1
        fi
    fi
    INSTALL_STATE_SNAPSHOT_READY=1
    return 0
}

restore_install_original_services() {
    [ "${INSTALL_ORIGINAL_CURRENT_PRESENT}" -eq 1 ] || return 0
    [ -d "${DEPLOY_ROOT}/current" ] || return 1
    local services=()
    local service
    while IFS= read -r service; do
        [ -n "${service}" ] && services+=("${service}")
    done <<< "${INSTALL_ORIGINAL_RUNNING_SERVICES}"
    if [ "${#services[@]}" -eq 0 ]; then
        return 0
    fi
    log_warn "  重新拉起安装前运行中的旧 release 服务：${services[*]}"
    lumen_compose_in "${DEPLOY_ROOT}/current" --profile tgbot up \
        --pull missing -d --wait --force-recreate "${services[@]}"
}

restore_install_state_snapshot() {
    [ "${INSTALL_STATE_SNAPSHOT_READY}" -eq 1 ] || return 0
    local rc=0 shared_env="${SHARED_DIR}/.env"
    if [ "${INSTALL_ORIGINAL_CURRENT_PRESENT}" -eq 1 ]; then
        lumen_atomic_replace_symlink \
            "${INSTALL_ORIGINAL_CURRENT_TARGET}" "${DEPLOY_ROOT}/current" || rc=1
    elif [ -L "${DEPLOY_ROOT}/current" ]; then
        rm -f "${DEPLOY_ROOT}/current" || rc=1
    fi
    if [ "${INSTALL_ORIGINAL_PREVIOUS_PRESENT}" -eq 1 ]; then
        lumen_atomic_replace_symlink \
            "${INSTALL_ORIGINAL_PREVIOUS_TARGET}" "${DEPLOY_ROOT}/previous" || rc=1
    elif [ -L "${DEPLOY_ROOT}/previous" ]; then
        rm -f "${DEPLOY_ROOT}/previous" || rc=1
    fi
    if [ -n "${INSTALL_ENV_SNAPSHOT}" ] \
            && [ -f "${INSTALL_ENV_SNAPSHOT}" ]; then
        local restore_tmp="${SHARED_DIR}/.env.restore.$$"
        if ! cp -p "${INSTALL_ENV_SNAPSHOT}" "${restore_tmp}" \
                || ! mv -f "${restore_tmp}" "${shared_env}"; then
            rm -f "${restore_tmp}" 2>/dev/null || true
            log_error "  shared/.env 原字节恢复失败；快照保留在 ${INSTALL_ENV_SNAPSHOT}"
            rc=1
        else
            log_warn "  shared/.env 已按安装前快照原字节恢复。"
        fi
    fi
    if [ -n "${INSTALL_HOST_ARTIFACT_SNAPSHOT}" ]; then
        if ! lumen_restore_operations_host_artifacts \
                "${INSTALL_HOST_ARTIFACT_SNAPSHOT}"; then
            log_error "  systemd units 或 host 脚本未能完整恢复。"
            rc=1
        else
            log_warn "  systemd units 与 host 脚本已恢复到安装前快照。"
        fi
    fi
    if ! restore_install_original_services; then
        log_error "  安装前旧 release 服务恢复失败。"
        rc=1
    fi
    return "${rc}"
}

discard_install_state_snapshot() {
    if [ -n "${INSTALL_ENV_SNAPSHOT}" ]; then
        rm -f "${INSTALL_ENV_SNAPSHOT}" 2>/dev/null || true
    fi
    INSTALL_ENV_SNAPSHOT=""
    lumen_discard_host_artifact_snapshot "${INSTALL_HOST_ARTIFACT_SNAPSHOT}"
    INSTALL_HOST_ARTIFACT_SNAPSHOT=""
    INSTALL_ORIGINAL_RUNNING_SERVICES=""
    INSTALL_STATE_SNAPSHOT_READY=0
}

on_error() {
    local line="$1"
    log_error "安装失败：第 ${line} 行返回非零状态（阶段=${INSTALL_PHASE:-unknown}）。"
}

# 失败清理：停止已启动的容器、回滚 current symlink、删除半完成的 release。
# 数据卷与 shared/.env 永远保留，让用户重跑 install 时复用。
cleanup_on_failure() {
    local rc=$?
    trap - EXIT INT TERM ERR
    if [ -n "${INSTALL_GHCR_PROBE_FILE:-}" ]; then
        rm -f "${INSTALL_GHCR_PROBE_FILE}" 2>/dev/null || true
        INSTALL_GHCR_PROBE_FILE=""
    fi
    if [ "${rc}" -ne 0 ]; then
        log_error "安装在阶段 [${INSTALL_PHASE:-unknown}] 失败，正在清理已启动的容器（数据卷与 shared/.env 保留）。"
        if [ "${#INSTALL_STARTED_SERVICES[@]}" -gt 0 ]; then
            local svc
            for svc in "${INSTALL_STARTED_SERVICES[@]}"; do
                log_warn "  最近 40 行 ${svc} 日志："
                _install_compose logs --tail=40 "${svc}" 2>/dev/null || log_warn "    （取日志失败，已忽略）"
            done
            log_warn "停止已启动的服务（数据卷保留）：${INSTALL_STARTED_SERVICES[*]}"
            if ! _install_compose stop "${INSTALL_STARTED_SERVICES[@]}" 2>/dev/null; then
                log_warn "  docker compose stop 返回非零（已忽略，请手动 docker compose ps 检查）"
            fi
        fi

        # 恢复安装开始时的 current / previous / shared env 状态。已有 .env
        # 按原字节恢复；首次安装生成的新 .env 仍保留，方便修复后幂等重跑。
        local _deploy_root="${DEPLOY_ROOT:-}"
        if [ -n "${_deploy_root}" ] \
                && [ "${INSTALL_STATE_SNAPSHOT_READY}" -eq 1 ]; then
            if restore_install_state_snapshot; then
                discard_install_state_snapshot
            else
                log_error "  安装前状态未能完整恢复，请检查 current/previous 与 ${SHARED_DIR:-<shared>}/.env。"
            fi
        fi

        # 半完成的 release 目录：rsync 已落地但 current 从未切到它（或已切回 previous），删除。
        if [ -n "${RELEASE_DIR:-}" ] && [ -d "${RELEASE_DIR}" ]; then
            local cur_target=""
            if [ -n "${_deploy_root}" ] && [ -L "${_deploy_root}/current" ]; then
                cur_target="$(readlink "${_deploy_root}/current" 2>/dev/null || true)"
            fi
            if [ "${cur_target}" != "releases/${RELEASE_ID:-}" ]; then
                log_warn "清理半完成的 release：${RELEASE_DIR}"
                if ! lumen_safe_rm_rf "${RELEASE_DIR}" 2>/dev/null; then
                    if ! lumen_safe_rm_rf_as_root "${RELEASE_DIR}" 2>/dev/null; then
                        log_warn "  release 删除失败，请手动：sudo rm -rf '${RELEASE_DIR}'"
                    fi
                fi
            fi
        fi

        # 只在新流程触发的 step protocol 上下文里写 fail；emit_step 函数在 lib.sh
        if command -v lumen_emit_step >/dev/null 2>&1 && [ -n "${INSTALL_PHASE:-}" ]; then
            lumen_emit_step "phase=${INSTALL_PHASE}" "status=fail" "rc=${rc}" "dur_ms=0" 2>/dev/null \
                || log_warn "lumen_emit_step 写入失败（已忽略）"
        fi
        log_error ""
        log_error "可恢复命令："
        log_error "  cd ${_deploy_root:-${ROOT}}/current 2>/dev/null || cd ${ROOT}"
        log_error "  COMPOSE_PROJECT_NAME=lumen docker compose ps"
        log_error "  COMPOSE_PROJECT_NAME=lumen docker compose logs --tail=200 api worker web"
        log_error "  bash ${SCRIPT_DIR}/install.sh --install   # 修复后重跑（幂等）"
    fi
    if [ "${rc}" -eq 0 ]; then
        discard_install_state_snapshot
    fi
    # lumen_release_lock 由 lumen_acquire_lock 安装的 EXIT trap 处理；这里手动也调一次幂等
    if command -v lumen_release_lock >/dev/null 2>&1; then
        lumen_release_lock 2>/dev/null || true
    fi
    return "${rc}"
}

on_signal() {
    local signal_name="$1"
    local rc="$2"
    log_error "安装被 ${signal_name} 中断（rc=${rc}），将走完整失败清理流程。"
    # exit 触发 EXIT trap (cleanup_on_failure)：清理已起容器、回滚 current
    # symlink、删半成品 release，最后释放锁。比裸 exit 更彻底。
    exit "${rc}"
}
