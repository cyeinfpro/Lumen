#!/usr/bin/env bash
# Release activation, host control planes, health checks, and summary.
# Sourced by scripts/install.sh after raw bootstrap has completed.

# ---------------------------------------------------------------------------
# G. 切换 current symlink
# ---------------------------------------------------------------------------
switch_current_symlink() {
    emit_step_start switch "切换 current symlink → releases/${RELEASE_ID}"
    local cur="${DEPLOY_ROOT}/current"
    if [ -L "${cur}" ]; then
        local prev_target
        prev_target="$(readlink "${cur}" 2>/dev/null || true)"
        if [ -n "${prev_target}" ] && [ "${prev_target}" != "releases/${RELEASE_ID}" ]; then
            if ! lumen_atomic_replace_symlink "${prev_target}" "${DEPLOY_ROOT}/previous" 2>/dev/null; then
                log_warn "无法更新 previous symlink → ${prev_target}（已忽略，不阻断 switch）"
            fi
        fi
    fi
    if ! lumen_atomic_replace_symlink "releases/${RELEASE_ID}" "${cur}"; then
        log_error "切换 current → releases/${RELEASE_ID} 失败。"
        exit 1
    fi
    log_info "${cur} → releases/${RELEASE_ID}"
    emit_step_done
}

# ---------------------------------------------------------------------------
# H. 安装/刷新一键更新 systemd runner
# ---------------------------------------------------------------------------
install_update_runner_units() {
    emit_step_start prepare "安装一键更新 runner（systemd path）"
    if [ "${OS}" != "linux" ] || ! lumen_systemd_runtime_available; then
        log_warn "未检测到 Linux systemd，跳过一键更新 runner 安装；命令行 update-lumen 不受影响。"
        emit_step_done
        return 0
    fi

    local src_dir="${RELEASE_DIR}/deploy/systemd"
    local src_path="${src_dir}/lumen-update.path"
    local src_runner="${src_dir}/lumen-update-runner.service"
    if [ ! -f "${src_path}" ] || [ ! -f "${src_runner}" ]; then
        log_warn "找不到 update runner unit 模板（${src_dir}），跳过一键更新 runner 安装。"
        emit_step_done
        return 0
    fi

    local data_root deploy_root backup_root tmp_dir
    data_root="${LUMEN_DATA_ROOT%/}"
    deploy_root="${DEPLOY_ROOT%/}"
    backup_root="${LUMEN_BACKUP_ROOT:-${data_root}/backup}"
    backup_root="${backup_root%/}"
    tmp_dir="$(mktemp -d)"

    _render_update_runner_units \
        "${src_path}" \
        "${src_runner}" \
        "${tmp_dir}" \
        "${data_root}" \
        "${backup_root}" \
        "${deploy_root}"

    lumen_ensure_backup_service_user "${backup_root}"

    if ! lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update.path" "${LUMEN_SYSTEMD_UNIT_DIR%/}/lumen-update.path"; then
        log_warn "安装 lumen-update.path 失败，面板一键更新将不可用。"
        rm -rf "${tmp_dir}"
        emit_step_done
        return 0
    fi
    if ! lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update-runner.service" "${LUMEN_SYSTEMD_UNIT_DIR%/}/lumen-update-runner.service"; then
        log_warn "安装 lumen-update-runner.service 失败，面板一键更新将不可用。"
        rm -rf "${tmp_dir}"
        emit_step_done
        return 0
    fi
    if [ -f "${tmp_dir}/lumen-update-warm.path" ] && [ -f "${tmp_dir}/lumen-update-warm.service" ]; then
        lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update-warm.path" "${LUMEN_SYSTEMD_UNIT_DIR%/}/lumen-update-warm.path" \
            || log_warn "安装 lumen-update-warm.path 失败，镜像预热将不可用。"
        lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update-warm.service" "${LUMEN_SYSTEMD_UNIT_DIR%/}/lumen-update-warm.service" \
            || log_warn "安装 lumen-update-warm.service 失败，镜像预热将不可用。"
    fi
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-backup.service "安装 lumen-backup.service 失败，自动/手动触发备份将不可用。"
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-backup.timer "安装 lumen-backup.timer 失败，自动备份将不可用。"
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-backup.path "安装 lumen-backup.path 失败，管理后台立即备份将无法触发宿主机备份。"
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-restore-runner.service "安装 lumen-restore-runner.service 失败，管理后台恢复将不可用。"
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-restore.path "安装 lumen-restore.path 失败，管理后台恢复将不可用。"
    if ! lumen_run_as_root systemctl daemon-reload; then
        log_warn "systemctl daemon-reload 失败，面板一键更新可能不可用。"
        rm -rf "${tmp_dir}"
        emit_step_done
        return 0
    fi
    if ! lumen_run_as_root systemctl enable --now lumen-update.path; then
        log_warn "启用 lumen-update.path 失败，面板一键更新将不可用；可稍后手动执行 systemctl enable --now lumen-update.path。"
        rm -rf "${tmp_dir}"
        emit_step_done
        return 0
    fi
    if [ -f "${tmp_dir}/lumen-update-warm.path" ]; then
        lumen_run_as_root systemctl enable --now lumen-update-warm.path \
            || log_warn "启用 lumen-update-warm.path 失败，镜像预热将不可用；可稍后手动执行 systemctl enable --now lumen-update-warm.path。"
    fi
    lumen_enable_optional_systemd_unit "${tmp_dir}" lumen-backup.timer "启用 lumen-backup.timer 失败，自动备份将不可用；可稍后手动执行 systemctl enable --now lumen-backup.timer。"
    lumen_enable_optional_systemd_unit "${tmp_dir}" lumen-backup.path "启用 lumen-backup.path 失败，管理后台立即备份将不可用；可稍后手动执行 systemctl enable --now lumen-backup.path。"
    lumen_enable_optional_systemd_unit "${tmp_dir}" lumen-restore.path "启用 lumen-restore.path 失败，管理后台恢复将不可用；可稍后手动执行 systemctl enable --now lumen-restore.path。"
    rm -rf "${tmp_dir}"

    log_info "一键更新 runner 已启用：监听 ${backup_root}/.update.trigger"
    emit_info "key=update_trigger" "value=${backup_root}/.update.trigger"
    emit_info "key=warm_trigger" "value=${backup_root}/.warm.trigger"
    emit_info "key=backup_trigger" "value=${backup_root}/.backup.trigger"
    emit_info "key=restore_trigger" "value=${backup_root}/.restore.trigger"
    emit_step_done
}


install_storage_control_plane() {
    if [ "${OS}" != "linux" ] || ! lumen_systemd_runtime_available; then
        log_warn "未检测到 Linux systemd，跳过存储控制面安装。"
        return 0
    fi

    local src_systemd="${RELEASE_DIR}/deploy/systemd"
    local src_script="${RELEASE_DIR}/deploy/scripts/lumen_storage_mount.sh"
    if [ ! -f "${src_script}" ]; then
        log_warn "找不到 ${src_script}，管理后台存储切换将不可用。"
        return 0
    fi

    local tmp_dir storage_gid unit
    tmp_dir="$(mktemp -d)"
    storage_gid="${LUMEN_APP_STORAGE_GID:-${LUMEN_APP_GID:-10001}}"
    for unit in lumen-storage-mount.service \
        lumen-storage-apply.service lumen-storage-apply.path \
        lumen-storage-test.service lumen-storage-test.path; do
        [ -f "${src_systemd}/${unit}" ] || continue
        _render_systemd_unit_template \
            "${src_systemd}/${unit}" \
            "${tmp_dir}/${unit}" \
            "${LUMEN_DATA_ROOT%/}" \
            "${LUMEN_BACKUP_ROOT:-${LUMEN_DATA_ROOT%/}/backup}" \
            "${DEPLOY_ROOT%/}"
        lumen_run_as_root install -m 0644 "${tmp_dir}/${unit}" "${LUMEN_SYSTEMD_UNIT_DIR%/}/${unit}" \
            || log_warn "安装 ${unit} 失败，管理后台存储切换可能不可用。"
    done
    lumen_run_as_root install -m 0755 "${src_script}" "${LUMEN_LOCAL_SBIN_DIR%/}/lumen-storage-mount" \
        || log_warn "安装 lumen-storage-mount 失败，管理后台存储切换不可用。"
    lumen_run_as_root install -d -m 0770 -o root -g "${storage_gid}" /var/lib/lumen-storage \
        || log_warn "创建 /var/lib/lumen-storage 失败，API 可能无法写入存储触发文件。"
    lumen_run_as_root systemctl daemon-reload \
        || log_warn "刷新 storage systemd units 失败。"
    lumen_run_as_root systemctl enable --now lumen-storage-apply.path lumen-storage-test.path \
        || log_warn "启用 storage path watchers 失败，管理后台存储切换不可用。"
    # Boot-time mount is enabled for future reboots, but deliberately not
    # started during install. The first admin apply performs the controlled
    # stop/remount/start cycle after explicit configuration.
    lumen_run_as_root systemctl enable lumen-storage-mount.service \
        || log_warn "启用 lumen-storage-mount.service 失败，重启后需手工恢复挂载。"
    rm -rf "${tmp_dir}"
    log_info "存储控制面已安装，共享目录 gid=${storage_gid}。"
}


# ---------------------------------------------------------------------------
# I. 健康检查（HTTP + Compose 状态）
# ---------------------------------------------------------------------------
run_health_checks() {
    emit_step_start health_post "健康检查（HTTP + compose service 状态）"

    if ! _install_health_http "http://127.0.0.1:8000/healthz" 60 2; then
        log_error "API 健康检查失败：http://127.0.0.1:8000/healthz 在 60s 内未返回 2xx/3xx。"
        log_error "  排查：${COMPOSE_LABEL} logs --tail=200 api"
        exit 1
    fi
    log_info "API /healthz 通过。"

    if ! _install_health_http "http://127.0.0.1:3000/" 60 2; then
        log_error "Web 健康检查失败：http://127.0.0.1:3000/ 在 60s 内未返回 2xx/3xx。"
        log_error "  排查：${COMPOSE_LABEL} logs --tail=200 web"
        exit 1
    fi
    log_info "Web 首页通过。"

    local health_services=("api" "worker" "web")
    local shared_env="${SHARED_DIR}/.env"
    if [ -n "$(env_file_get TELEGRAM_BOT_TOKEN "${shared_env}")" ]; then
        # tgbot 没有 healthcheck（compose 里没声明），降级到 service started
        :
    fi
    if ! _install_health_compose "${health_services[@]}"; then
        log_error "compose service 健康状态异常。"
        exit 1
    fi
    log_info "所有 compose service 健康。"
    emit_step_done
}

# ---------------------------------------------------------------------------
# J. systemd 处理（不自动 disable，仅提示）
# ---------------------------------------------------------------------------
warn_about_legacy_systemd() {
    if ! command -v systemctl >/dev/null 2>&1; then
        return 0
    fi
    local has_active=0 unit
    for unit in lumen-api.service lumen-worker.service lumen-web.service lumen-tgbot.service; do
        if systemctl is-active --quiet "${unit}" 2>/dev/null; then
            has_active=1
            break
        fi
    done
    if [ "${has_active}" -eq 1 ]; then
        log_warn ""
        log_warn "检测到旧版本的 systemd 服务仍在运行（可能与 docker 容器抢端口）："
        log_warn "  Docker 栈已启动并健康。建议手动禁用旧 systemd 服务以避免冲突："
        log_warn "    sudo systemctl disable --now lumen-api lumen-worker lumen-web lumen-tgbot"
        log_warn "  确认后再访问 Web，避免请求被旧 systemd 进程截获。"
    fi
}

# ---------------------------------------------------------------------------
# K. 输出汇总
# ---------------------------------------------------------------------------
print_summary() {
    emit_step_start cleanup "安装完成汇总"
    local shared_env="${SHARED_DIR}/.env"
    local image_tag web_bind_host
    image_tag="$(env_file_get LUMEN_IMAGE_TAG "${shared_env}")"
    web_bind_host="$(env_file_get WEB_BIND_HOST "${shared_env}")"
    web_bind_host="${web_bind_host:-127.0.0.1}"
    local browser_url browser_note
    if [ "${web_bind_host}" = "0.0.0.0" ]; then
        browser_url="http://<服务器IP>:3000/"
        browser_note="云安全组需放行 TCP 3000；建议生产使用反代"
    else
        browser_url="http://127.0.0.1:3000/"
        browser_note="默认仅本机可访问；公网访问请配置 nginx/Caddy 反代"
    fi
    cat <<EOF

  ${LUMEN_C_BOLD}Lumen 安装完成（Docker Compose 全栈）${LUMEN_C_RESET}

  Web 监听 ......... ${web_bind_host}:3000
  浏览器访问 ....... ${browser_url}（${browser_note}）
  API 健康检查 ..... http://127.0.0.1:8000/healthz
  管理员邮箱 ....... ${INSTALL_ADMIN_EMAIL:-（已存在或非交互模式未设置）}
  Provider 配置 .... 登录后 → 右上角「管理 → 上游 Provider」
                     默认 PROVIDERS=[]，需添加 1 条才能调图像 API

  部署目录 ......... ${DEPLOY_ROOT}/current → releases/${RELEASE_ID}
  数据目录 ......... storage/backup=${LUMEN_DATA_ROOT}，postgres/redis=${LUMEN_DB_ROOT}
  共享 .env ........ ${SHARED_DIR}/.env
  镜像 tag ......... ${image_tag}
  tgbot ............ ${INSTALL_TGBOT_STATUS:-unknown}（started=正常 / failed=token 或网络问题 / skipped=未配置）

  ${LUMEN_C_BOLD}日常运维${LUMEN_C_RESET}

    状态：    cd ${DEPLOY_ROOT}/current && COMPOSE_PROJECT_NAME=lumen docker compose ps
    日志：    cd ${DEPLOY_ROOT}/current && COMPOSE_PROJECT_NAME=lumen docker compose logs -f api
    更新：    bash ${DEPLOY_ROOT}/current/scripts/lumenctl.sh update-lumen
    备份：    bash ${DEPLOY_ROOT}/current/scripts/backup.sh   （输出到 ${LUMEN_DATA_ROOT}/backup）
    卸载：    bash ${DEPLOY_ROOT}/current/scripts/uninstall.sh

EOF
    emit_step_done
}
