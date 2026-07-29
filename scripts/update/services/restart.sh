#!/usr/bin/env bash
# Service restart, blue-green traffic shift, and automatic rollback phase.

lumen_update_compose_up_bound_service() {
    local compose_dir="$1" service="$2"
    (
        export COMPOSE_FILE="${compose_dir}/docker-compose.yml:${TARGET_IMAGE_OVERRIDE_FILE:?}"
        if [ "${service}" = "tgbot" ]; then
            export COMPOSE_PROFILES=tgbot
        fi
        compose_up_service "${compose_dir}" "${service}"
    )
}

lumen_update_compose_blue_green_bound() {
    local compose_dir="$1"
    shift
    (
        export COMPOSE_FILE="${compose_dir}/docker-compose.yml:${compose_dir}/docker-compose.bluegreen.yml:${TARGET_IMAGE_OVERRIDE_FILE:?}"
        lumen_compose_in "${compose_dir}" "$@"
    )
}

lumen_update_start_bound_service() {
    local compose_dir="$1" service="$2" container="${3:-lumen-$2}"
    lumen_update_compose_up_bound_service "${compose_dir}" "${service}" \
        && lumen_update_verify_running_service_image "${service}" "${container}"
}

# Phase: restart_services
update_phase_restart_services() {
emit_start restart_services

CURRENT_LINK="${ROOT}/current"
if ! lumen_update_activate_bound_image_override; then
    log_error "[restart_services] immutable image binding 不可用，拒绝启动目标服务。"
    emit_fail restart_services 1
    exit 1
fi
# --force-recreate：同 start_infra 理由，避免容器名冲突 fail。
# 服务启动顺序：worker → web → api。lumen-api **必须最后重启**——
# update.sh 自身就是被 admin_update 通过 lumen-update-runner 触发的，
# 如果 api 先重启，正在等 update 进度 SSE 的前端会立刻断流；
# 把 api 放到最后还能用旧 api 把进度写完，再无缝切到新版本。
# (per project_lumen_update_button.md)
_restart_ok=1
if [ "${LUMEN_UPDATE_BLUE_GREEN:-0}" = "1" ] && [ -f "${CURRENT_LINK}/docker-compose.bluegreen.yml" ]; then
    _green_port="${API_GREEN_BIND_PORT:-18001}"
    _blue_port="${API_BIND_PORT:-8000}"
    _blue_upstream="${LUMEN_BLUE_UPSTREAM:-127.0.0.1:${_blue_port}}"
    _green_upstream="${LUMEN_GREEN_UPSTREAM:-127.0.0.1:${_green_port}}"
    _shift_script="${CURRENT_LINK}/scripts/lumen-shift-traffic.sh"

    emit_start start_target_worker
    if lumen_update_start_bound_service "${CURRENT_LINK}" worker; then
        emit_done start_target_worker 0
    else
        _restart_ok=0
        emit_fail start_target_worker 1
    fi

    if [ "${_restart_ok}" = "1" ]; then
        emit_start start_green
        if lumen_update_compose_blue_green_bound "${CURRENT_LINK}" \
                up --pull missing -d --wait --force-recreate api-green \
            && lumen_update_verify_running_service_image \
                api-green lumen-api-green \
            && lumen_wait_for_http_ok \
                "http://127.0.0.1:${_green_port}/healthz" 60; then
            emit_info start_green port "${_green_port}"
            emit_done start_green 0
        else
            _restart_ok=0
            emit_fail start_green 1
        fi
    fi

    if [ "${_restart_ok}" = "1" ]; then
        emit_start shift_traffic_50
        if lumen_run_as_root env LUMEN_BLUE_UPSTREAM="${_blue_upstream}" LUMEN_GREEN_UPSTREAM="${_green_upstream}" bash "${_shift_script}" green 50; then
            sleep "${LUMEN_BLUE_GREEN_SHIFT_PAUSE_SEC:-3}"
            emit_done shift_traffic_50 0
        else
            _restart_ok=0
            emit_fail shift_traffic_50 1
        fi
    fi
    if [ "${_restart_ok}" = "1" ]; then
        emit_start shift_traffic_100
        if lumen_run_as_root env LUMEN_BLUE_UPSTREAM="${_blue_upstream}" LUMEN_GREEN_UPSTREAM="${_green_upstream}" bash "${_shift_script}" green 100; then
            sleep "${LUMEN_BLUE_GREEN_SHIFT_PAUSE_SEC:-3}"
            emit_done shift_traffic_100 0
        else
            _restart_ok=0
            emit_fail shift_traffic_100 1
        fi
    fi
    if [ "${_restart_ok}" = "1" ]; then
        emit_start drain_blue
        sleep "${LUMEN_BLUE_GREEN_DRAIN_SEC:-30}"
        emit_done drain_blue 0
    fi
    if [ "${_restart_ok}" = "1" ]; then
        emit_start stop_blue
        if lumen_compose_in "${CURRENT_LINK}" stop api; then
            emit_done stop_blue 0
        else
            _restart_ok=0
            emit_fail stop_blue 1
        fi
    fi
    if [ "${_restart_ok}" = "1" ]; then
        emit_start start_blue
        for _svc in web api; do
            if ! lumen_update_start_bound_service \
                    "${CURRENT_LINK}" "${_svc}"; then
                _restart_ok=0
                break
            fi
        done
        if [ "${_restart_ok}" = "1" ]; then
            emit_done start_blue 0
        else
            emit_fail start_blue 1
        fi
    fi
    if [ "${_restart_ok}" = "1" ]; then
        emit_start shift_traffic_blue
        if lumen_run_as_root env LUMEN_BLUE_UPSTREAM="${_blue_upstream}" LUMEN_GREEN_UPSTREAM="${_green_upstream}" bash "${_shift_script}" blue 100; then
            emit_done shift_traffic_blue 0
        else
            _restart_ok=0
            emit_fail shift_traffic_blue 1
        fi
    fi
    if [ "${_restart_ok}" = "1" ]; then
        emit_start stop_green
        if lumen_update_compose_blue_green_bound \
                "${CURRENT_LINK}" stop api-green; then
            emit_done stop_green 0
        else
            emit_warn stop_green "stop_green_failed_ignored"
            emit_done stop_green 0
        fi
    fi
else
    for _svc in worker web api; do
        if ! lumen_update_start_bound_service "${CURRENT_LINK}" "${_svc}"; then
            _restart_ok=0
            break
        fi
    done
fi

if [ "${_restart_ok}" = "1" ] \
        && env_key_present "${SHARED_ENV}" "TELEGRAM_BOT_TOKEN"; then
    if [ "${TGBOT_IMAGE_READY:-0}" != "1" ] \
            || ! lumen_update_start_bound_service \
                "${CURRENT_LINK}" tgbot lumen-tgbot; then
        _restart_ok=0
        log_error "[restart_services] tgbot 已启用但 immutable 启动 proof 失败。"
    else
        emit_info restart_services tgbot "started_by_image_id"
    fi
elif [ "${_restart_ok}" = "1" ]; then
    if ! lumen_compose_in "${CURRENT_LINK}" \
            --profile tgbot stop tgbot >/dev/null 2>&1; then
        _restart_ok=0
        log_error "[restart_services] 无法确认禁用的 tgbot 已停止。"
    else
        emit_info restart_services tgbot "disabled_not_started"
    fi
fi

if [ "${_restart_ok}" = "1" ]; then
    :
else
    if [ "${UPDATE_MIGRATION_STARTED}" -eq 1 ]; then
        log_update_restore_boundary restart_services
    fi
    if [ "${LUMEN_UPDATE_BLUE_GREEN:-0}" = "1" ] && [ -n "${_shift_script:-}" ]; then
        log_warn "[restart_services] 蓝绿路径失败，仅在 blue 健康且流量切回成功后停止 green。"
        if blue_green_restore_blue_traffic; then
            blue_green_stop_green
        fi
    fi
    log_error "[restart_services] 目标服务启动/proof 失败，尝试自动回滚到上一已知好 tag：${PREVIOUS_TAG:-<none>}"
    emit_warn restart_services "starting_auto_rollback"
    # 事务化回滚：先备份新 tag、改 .env，pull/up 任一步失败就把 .env 恢复成新 tag，
    # 确保 SHARED_ENV 与 current symlink 状态一致（不会出现 .env 是旧 tag 但 current
    # 仍是新 release 的中间态）。
    ROLLBACK_OK=0
    # 优先用 releases/<CURRENT_ID>/.image-tag 锚定回滚 tag（之前 set_image_tag
    # 阶段写入），fallback 到 PREVIOUS_TAG（update 开始前 SHARED_ENV 中的值）。
    # 前者抗"update 中途用户手动改过 SHARED_ENV"的边界情况，避免回滚拉到错误
    # 镜像导致 release 代码与镜像版本不匹配。
    ROLLBACK_TAG="${PREVIOUS_TAG}"
    ROLLBACK_VERSION=""
    if [ -n "${CURRENT_ID:-}" ] && [ -f "${ROOT}/releases/${CURRENT_ID}/.image-tag" ]; then
        _anchored="$(head -n1 "${ROOT}/releases/${CURRENT_ID}/.image-tag" 2>/dev/null | tr -d '[:space:]')"
        if [ -n "${_anchored}" ]; then
            ROLLBACK_TAG="${_anchored}"
        fi
    fi
    if [ -n "${CURRENT_ID:-}" ] \
            && [ -f "${ROOT}/releases/${CURRENT_ID}/VERSION" ]; then
        ROLLBACK_VERSION="$(head -n1 \
            "${ROOT}/releases/${CURRENT_ID}/VERSION" 2>/dev/null \
            | tr -d '[:space:]')"
    fi
    if [ -n "${ROLLBACK_TAG}" ] && [ "${ROLLBACK_TAG}" != "${TARGET_TAG}" ]; then
        # 还要验证 PREVIOUS release 目录还在；缺失时回滚没意义，直接走手动恢复路径
        if [ -z "${CURRENT_ID:-}" ] || [ ! -d "${ROOT}/releases/${CURRENT_ID}" ]; then
            log_error "[restart_services] previous release 目录不存在（${ROOT}/releases/${CURRENT_ID:-<none>}），跳过自动回滚。"
        else
            if lumen_set_image_tag_in_env "${SHARED_ENV}" "${ROLLBACK_TAG}" \
                    && { [ -z "${ROLLBACK_VERSION}" ] \
                        || lumen_set_env_value_in_file \
                            "${SHARED_ENV}" LUMEN_VERSION "${ROLLBACK_VERSION}"; }; then
                _rollback_started=1
                if lumen_release_atomic_switch "${ROOT}" "${CURRENT_ID}" \
                    && lumen_compose_in "${CURRENT_LINK}" pull; then
                    # 回滚同样按 worker → web → api 顺序逐个 up，保留 api 最后启动的偏好。
                    for _svc in worker web api; do
                        if ! compose_up_service_standard "${CURRENT_LINK}" "${_svc}"; then
                            _rollback_started=0
                            break
                        fi
                    done
                    if [ "${_rollback_started}" = "1" ] \
                            && env_key_present \
                                "${SHARED_ENV}" "TELEGRAM_BOT_TOKEN" \
                            && { ! lumen_compose_in "${CURRENT_LINK}" \
                                --profile tgbot pull tgbot \
                                || ! lumen_compose_in "${CURRENT_LINK}" \
                                    --profile tgbot up --pull missing \
                                    --no-deps -d --force-recreate tgbot; }; then
                        _rollback_started=0
                    fi
                else
                    _rollback_started=0
                fi
                if [ "${_rollback_started}" = "1" ] \
                        && [ "${LUMEN_UPDATE_BLUE_GREEN:-0}" = "1" ] \
                        && [ -n "${_shift_script:-}" ]; then
                    if blue_green_restore_blue_traffic; then
                        blue_green_stop_green
                    else
                        _rollback_started=0
                    fi
                fi
                if [ "${_rollback_started}" = "1" ]; then
                    UPDATE_RELEASE_SWITCHED=0
                    UPDATE_OLD_SERVICES_STOPPED=0
                    log_warn "[restart_services] 已用 ${ROLLBACK_TAG} 回滚成功（current → ${CURRENT_ID}）；本次 update 视为失败。"
                    emit_info restart_services rolled_back_to "${ROLLBACK_TAG}"
                    emit_info restart_services rolled_back_release "${CURRENT_ID}"
                    ROLLBACK_OK=1
                else
                    # pull/up 失败：把 .env 恢复成 TARGET_TAG，避免下次重启拉错镜像
                    log_error "[restart_services] 回滚 pull/up 失败，恢复 SHARED_ENV 到 ${TARGET_TAG} 以避免错位。"
                    if ! lumen_set_image_tag_in_env "${SHARED_ENV}" "${TARGET_TAG}"; then
                        log_error "  恢复 SHARED_ENV 到 ${TARGET_TAG} 也失败！请手动检查 ${SHARED_ENV}"
                    fi
                fi
            else
                log_error "[restart_services] 改写 SHARED_ENV 到 ${ROLLBACK_TAG} 失败，跳过自动回滚。"
            fi
        fi
    fi
    if [ "${ROLLBACK_OK}" = "1" ]; then
        emit_fail restart_services 1
        exit 1
    fi
    log_error "[restart_services] 自动回滚失败 → 请按 §18 手动回滚："
    log_error "  ln -sfn releases/${CURRENT_ID:-<id>} ${ROOT}/current"
    log_error "  sed -i 's|^LUMEN_IMAGE_TAG=.*|LUMEN_IMAGE_TAG=${ROLLBACK_TAG:-${PREVIOUS_TAG:-<old-tag>}}|' ${SHARED_ENV}"
    log_error "  cd ${ROOT}/current && COMPOSE_PROJECT_NAME=lumen docker compose pull && docker compose up --pull missing -d --wait api worker web"
    emit_fail restart_services 1
    exit 1
fi

emit_done restart_services 0
}
