#!/usr/bin/env bash
# Blue-green rollback and recovery helpers.

blue_green_restore_blue_traffic() {
    local blue_ready_url="${LUMEN_BLUE_GREEN_ROLLBACK_READY_URL:-http://127.0.0.1:${_blue_port:-8000}/readyz}"
    if ! lumen_wait_for_http_ok \
            "${blue_ready_url}" \
            "${LUMEN_BLUE_GREEN_ROLLBACK_HEALTH_TIMEOUT:-60}"; then
        log_error "[restart_services] blue API 依赖尚未就绪，保留 green 承载流量。"
        return 1
    fi
    if ! lumen_run_as_root env \
            LUMEN_BLUE_UPSTREAM="${_blue_upstream:-127.0.0.1:8000}" \
            LUMEN_GREEN_UPSTREAM="${_green_upstream:-127.0.0.1:18001}" \
            bash "${_shift_script}" blue 100; then
        log_error "[restart_services] 切回 blue 100% 失败，保留 green 承载流量。"
        return 1
    fi
}

blue_green_stop_green() {
    lumen_compose_in "${CURRENT_LINK}" \
        -f docker-compose.yml \
        -f docker-compose.bluegreen.yml \
        stop api-green >/dev/null 2>&1 \
        || log_warn "[restart_services] green 停止失败，已保留供人工检查。"
}
