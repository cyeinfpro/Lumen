#!/usr/bin/env bash
# Post-switch API, web, and compose health phase.

if ! command -v lumen_update_tgbot_expected >/dev/null 2>&1; then
    lumen_update_tgbot_expected() {
        [ -n "${SHARED_ENV:-}" ] \
            && env_key_present "${SHARED_ENV}" "TELEGRAM_BOT_TOKEN"
    }
fi

if ! command -v lumen_update_image_job_expected >/dev/null 2>&1; then
    lumen_update_image_job_expected() {
        local channel=""
        [ "${UPDATE_IMAGE_JOB_READINESS_REQUIRED:-0}" = "1" ] && return 0
        if [ -n "${SHARED_ENV:-}" ] && [ -f "${SHARED_ENV}" ]; then
            channel="$(
                lumen_env_value IMAGE_CHANNEL "${SHARED_ENV}" 2>/dev/null \
                    | tr '[:upper:]' '[:lower:]' \
                    || true
            )"
            [ "${channel}" = "image_jobs_only" ] && return 0
        fi
        command -v systemctl >/dev/null 2>&1 \
            && systemctl is-active --quiet image-job.service 2>/dev/null
    }
fi

lumen_update_image_job_health_url() {
    local channel="" base_url=""
    if [ -n "${LUMEN_IMAGE_JOB_HEALTH_URL:-}" ]; then
        printf '%s\n' "${LUMEN_IMAGE_JOB_HEALTH_URL}"
        return 0
    fi
    if [ -n "${SHARED_ENV:-}" ] && [ -f "${SHARED_ENV}" ]; then
        channel="$(
            lumen_env_value IMAGE_CHANNEL "${SHARED_ENV}" 2>/dev/null \
                | tr '[:upper:]' '[:lower:]' \
                || true
        )"
        base_url="$(
            lumen_env_value IMAGE_JOB_BASE_URL "${SHARED_ENV}" 2>/dev/null || true
        )"
    fi
    if [ "${channel}" = "image_jobs_only" ]; then
        [ -n "${base_url}" ] || return 1
        printf '%s/health\n' "${base_url%/}"
        return 0
    fi
    printf '%s\n' "http://127.0.0.1:8091/health"
}

# Phase: health_check
update_phase_health_check() {
emit_start health_check

if ! lumen_update_require_storage_identity health_check; then
    log_error "[health_check] 数据根 identity 校验失败，拒绝提交更新状态。"
    emit_fail health_check 1
    exit 1
fi

API_READY_URL="${LUMEN_API_READY_URL:-http://127.0.0.1:8000/readyz}"
WEB_HEALTH_URL="${LUMEN_WEB_HEALTH_URL:-http://127.0.0.1:3000/}"

# 4K 长任务场景下 worker warm-up 可能持续数分钟（layered timeout：nginx 1800
# → arq 1800 → task 1500 → upstream 660），原 60s 探测窗口对 cold-start 严重
# 不够。默认 300s（5 min），可通过 LUMEN_HEALTH_TIMEOUT_SECONDS 覆盖。
HEALTH_TIMEOUT="${LUMEN_HEALTH_TIMEOUT_SECONDS:-300}"
HEALTH_FAIL=0
HEALTH_SERVICES=(api worker web)
if [ -f "${CURRENT_LINK:-${ROOT}/current}/docker-compose.yml" ] \
        && grep -Eq '^[[:space:]]{2}agent-runtime:[[:space:]]*$' \
            "${CURRENT_LINK:-${ROOT}/current}/docker-compose.yml"; then
    HEALTH_SERVICES=(agent-runtime api worker web)
fi
if [ "${UPDATE_TGBOT_READINESS_REQUIRED:-0}" = "1" ] \
        || lumen_update_tgbot_expected; then
    HEALTH_SERVICES+=(tgbot)
fi
LUMEN_ROLLBACK_READY_TIMEOUT_SECONDS="${HEALTH_TIMEOUT}"
if ! lumen_update_wait_for_core_ready "${CURRENT_LINK:-${ROOT}/current}"; then
    log_error "[health_check] API /readyz 或 Worker health 在 ${HEALTH_TIMEOUT}s 内未通过。"
    HEALTH_FAIL=1
fi
if ! lumen_health_http "${WEB_HEALTH_URL}" "${HEALTH_TIMEOUT}" 2; then
    log_error "[health_check] Web ${WEB_HEALTH_URL} 在 ${HEALTH_TIMEOUT}s 内不可达。"
    HEALTH_FAIL=1
fi
if lumen_update_image_job_expected; then
    IMAGE_JOB_HEALTH_URL="$(lumen_update_image_job_health_url || true)"
    if [ -z "${IMAGE_JOB_HEALTH_URL}" ] \
            || ! lumen_health_http "${IMAGE_JOB_HEALTH_URL}" "${HEALTH_TIMEOUT}" 2; then
        log_error "[health_check] image-job readiness 在 ${HEALTH_TIMEOUT}s 内未通过。"
        HEALTH_FAIL=1
    fi
fi
if ! lumen_health_compose "${HEALTH_SERVICES[@]}"; then
    log_error "[health_check] docker compose readiness 失败：${HEALTH_SERVICES[*]}。"
    HEALTH_FAIL=1
fi

if [ "${HEALTH_FAIL}" -eq 1 ]; then
    log_error "[health_check] 健康检查失败；新代码已上线但状态异常。"
    if [ "${UPDATE_MIGRATION_VERIFIED}" -eq 1 ]; then
        log_update_restore_boundary health_check
        log_error "  数据库迁移已应用；恢复策略将按已记录 restore boundary 执行。"
    else
        log_warn "  本轮未执行数据库迁移，将按更新前快照自动恢复旧 release。"
        log_error "  如自动恢复失败，请执行："
    fi
    log_error "    cd ${CURRENT_LINK}"
    log_error "    COMPOSE_PROJECT_NAME=lumen docker compose logs --tail=120 ${HEALTH_SERVICES[*]}"
    log_error "    COMPOSE_PROJECT_NAME=lumen docker compose ps"
    log_error "  状态快照：release_id=${NEW_ID}  image_tag=${TARGET_TAG}  current → $(readlink "${ROOT}/current" 2>/dev/null || echo unknown)"
    log_error "  如需回滚，参考 docs/.. §18 或调高 LUMEN_HEALTH_TIMEOUT_SECONDS 重跑健康。"
    emit_fail health_check 1
    exit 1
fi
if ! mark_update_committed; then
    emit_fail health_check 1
    exit 1
fi
emit_done health_check 0
}
