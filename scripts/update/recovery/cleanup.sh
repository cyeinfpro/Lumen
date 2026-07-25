#!/usr/bin/env bash
# Update cleanup and successful completion helpers.

run_update_cleanup() {
    local cleanup_action="${1:-updated}"
    local cleanup_dangling_default="0"
    local cleanup_images_default="0"
    local cleanup_cache_default="0"

    if [ "${LUMEN_UPDATE_MODE}" = "fast" ]; then
        # Fast mode is the normal admin-update path. Keep a short rollback buffer
        # for freshly pulled images, but still reclaim the old tags that would
        # otherwise accumulate across frequent releases.
        cleanup_images_default="48"
        cleanup_cache_default="48"
    fi

    local CLEANUP_DANGLING_H CLEANUP_IMAGES_H CLEANUP_CACHE_H
    local filter_args=()
    CLEANUP_DANGLING_H="${LUMEN_CLEANUP_DANGLING_HOURS:-${cleanup_dangling_default}}"
    CLEANUP_IMAGES_H="${LUMEN_CLEANUP_IMAGES_HOURS:-${cleanup_images_default}}"
    CLEANUP_CACHE_H="${LUMEN_CLEANUP_CACHE_HOURS:-${cleanup_cache_default}}"

    _cleanup_filter_args() {
        local hours="$1"
        if [ "${hours}" -gt 0 ] 2>/dev/null; then
            printf -- '--filter\nuntil=%sh\n' "${hours}"
        fi
    }

    emit_start cleanup
    emit_info cleanup action "${cleanup_action}"

    if lumen_env_truthy "${LUMEN_UPDATE_SKIP_DOCKER_CLEANUP:-0}"; then
        log_info "[cleanup] LUMEN_UPDATE_SKIP_DOCKER_CLEANUP=1：跳过 docker image/buildx prune。"
        emit_info cleanup docker_prune "skipped_by_env"
    elif ! lumen_detect_docker_access; then
        log_warn "[cleanup] Docker 不可用或当前用户无权访问，跳过 docker image/buildx prune。"
        emit_info cleanup docker_prune "skipped_no_docker_access"
    else
        # 1. dangling layers — 几乎 0 风险。
        filter_args=()
        while IFS= read -r line; do filter_args+=("${line}"); done < <(_cleanup_filter_args "${CLEANUP_DANGLING_H}")
        if ! lumen_docker image prune -f \
                ${filter_args[@]+"${filter_args[@]}"} >/dev/null 2>&1; then
            log_warn "[cleanup] docker image prune (dangling) 失败（已忽略）。"
        else
            emit_info cleanup dangling_pruned "hours=${CLEANUP_DANGLING_H}"
        fi

        # 2. unused images — docker protects images still referenced by running
        # containers, so this does not remove the current deployment.
        filter_args=()
        while IFS= read -r line; do filter_args+=("${line}"); done < <(_cleanup_filter_args "${CLEANUP_IMAGES_H}")
        if ! lumen_docker image prune -a -f \
                ${filter_args[@]+"${filter_args[@]}"} >/dev/null 2>&1; then
            log_warn "[cleanup] docker image prune -a 失败（已忽略）。"
        else
            emit_info cleanup unused_images_pruned "hours=${CLEANUP_IMAGES_H}"
        fi

        # 3. buildx build cache — local build paths can grow unbounded.
        if lumen_docker buildx version >/dev/null 2>&1; then
            filter_args=()
            while IFS= read -r line; do filter_args+=("${line}"); done < <(_cleanup_filter_args "${CLEANUP_CACHE_H}")
            if ! lumen_docker buildx prune -f \
                    ${filter_args[@]+"${filter_args[@]}"} >/dev/null 2>&1; then
                log_warn "[cleanup] docker buildx prune 失败（已忽略）。"
            else
                emit_info cleanup buildx_cache_pruned "hours=${CLEANUP_CACHE_H}"
            fi
        fi
    fi

    # 4. 旧 release 目录 — keep 最近 N 个（含 current）。
    if ! lumen_release_cleanup_old "${ROOT}" "${LUMEN_RELEASE_KEEP:-3}"; then
        log_warn "[cleanup] 旧 release 清理失败（已忽略）。"
    fi

    emit_done cleanup 0
}

# Trap：cleanup 之后的失败由 recovery/state.sh 统一收口。
update_finish_success() {
log_step "更新完成"
log_info "release ${NEW_ID} 已上线（previous: ${CURRENT_ID:-<none>}, tag: ${TARGET_TAG}）"
log_info "  API:    ${API_HEALTH_URL}"
log_info "  Web:    ${WEB_HEALTH_URL}"
lumen_update_journal_status complete
}
