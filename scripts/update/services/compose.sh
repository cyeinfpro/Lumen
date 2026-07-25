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
