#!/usr/bin/env bash
# Service compose startup policy helpers.

compose_up_service_fast() {
    local compose_dir="$1"
    local svc="$2"
    # --no-deps 是快速更新的关键：否则重建 worker/web 时 Compose 会顺带 stop/recreate
    # api/redis 等依赖，用户正在看的项目页会撞到 ECONNREFUSED。
    lumen_compose_in "${compose_dir}" up \
        --pull missing \
        --no-deps \
        --timeout "${LUMEN_UPDATE_RECREATE_TIMEOUT:-30}" \
        -d --wait --force-recreate \
        "${svc}"
}

compose_up_service_standard() {
    local compose_dir="$1"
    local svc="$2"
    lumen_compose_in "${compose_dir}" up \
        --pull missing \
        --timeout "${LUMEN_UPDATE_RECREATE_TIMEOUT:-30}" \
        -d --wait --force-recreate \
        "${svc}"
}

compose_up_service() {
    local compose_dir="$1"
    local svc="$2"
    if [ "${LUMEN_UPDATE_MODE}" = "fast" ]; then
        compose_up_service_fast "${compose_dir}" "${svc}"
    else
        compose_up_service_standard "${compose_dir}" "${svc}"
    fi
}

lumen_update_wait_for_core_ready() {
    local compose_dir="${1:-${ROOT:-${LUMEN_DEPLOY_ROOT:-/opt/lumen}}/current}"
    local ready_url="${LUMEN_API_READY_URL:-http://127.0.0.1:${API_BIND_PORT:-8000}/readyz}"
    local attempts="${LUMEN_ROLLBACK_READY_TIMEOUT_SECONDS:-60}"
    local interval="${LUMEN_CORE_READINESS_INTERVAL_SECONDS:-1}"
    if command -v lumen_require_compose_core_readiness >/dev/null 2>&1; then
        lumen_require_compose_core_readiness \
            "${compose_dir}" "${ready_url}" "${attempts}" "${interval}"
        return $?
    fi
    if ! lumen_wait_for_http_ok "${ready_url}" "${attempts}"; then
        return 1
    fi
    lumen_compose_in "${compose_dir}" exec -T worker \
        python -m app.worker_health check >/dev/null 2>&1
}

lumen_update_wait_for_api_ready() {
    lumen_update_wait_for_core_ready "$@"
}
