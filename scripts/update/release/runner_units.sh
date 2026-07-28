#!/usr/bin/env bash
# Release-bound update runner unit rendering and refresh helpers.

render_update_runner_unit() {
    local src="$1"
    local dst="$2"
    local data_root="$3"
    local backup_root="$4"
    local deploy_root="$5"
    local data_root_esc backup_root_esc deploy_root_esc
    data_root_esc="$(sed_replacement_escape "${data_root}")"
    backup_root_esc="$(sed_replacement_escape "${backup_root}")"
    deploy_root_esc="$(sed_replacement_escape "${deploy_root}")"

    sed \
        -e 's#/opt/lumendata/backup#__LUMEN_BACKUP_ROOT__#g' \
        -e 's#/opt/lumendata#__LUMEN_DATA_ROOT__#g' \
        -e 's#/opt/lumen#__LUMEN_DEPLOY_ROOT__#g' \
        "${src}" \
        | sed \
            -e "s#__LUMEN_BACKUP_ROOT__#${backup_root_esc}#g" \
            -e "s#__LUMEN_DATA_ROOT__#${data_root_esc}#g" \
            -e "s#__LUMEN_DEPLOY_ROOT__#${deploy_root_esc}#g" \
        > "${dst}"
}

refresh_update_runner_units() {
    if ! lumen_systemd_runtime_available; then
        log_info "[refresh_update_runner] 未检测到 Linux systemd，跳过。"
        emit_info refresh_update_runner status "skipped_no_systemd"
        return 0
    fi

    local src_dir="${ROOT}/current/deploy/systemd"
    if [ ! -d "${src_dir}" ] && [ -n "${NEW_RELEASE:-}" ] && [ -d "${NEW_RELEASE}/deploy/systemd" ]; then
        src_dir="${NEW_RELEASE}/deploy/systemd"
    fi
    local src_path="${src_dir}/lumen-update.path"
    local src_runner="${src_dir}/lumen-update-runner.service"
    local src_warm_path="${src_dir}/lumen-update-warm.path"
    local src_warm_service="${src_dir}/lumen-update-warm.service"
    local src_backup_service="${src_dir}/lumen-backup.service"
    local src_backup_timer="${src_dir}/lumen-backup.timer"
    local src_backup_path="${src_dir}/lumen-backup.path"
    local src_restore_service="${src_dir}/lumen-restore-runner.service"
    local src_restore_path="${src_dir}/lumen-restore.path"
    local src_storage_script="${src_dir%/systemd}/scripts/lumen_storage_mount.sh"
    if [ ! -f "${src_path}" ] || [ ! -f "${src_runner}" ]; then
        log_warn "[refresh_update_runner] 找不到 update runner unit 模板（${src_dir}），跳过。"
        emit_warn refresh_update_runner "unit_templates_missing"
        return 0
    fi

    local data_root backup_root deploy_root tmp_dir
    data_root="${LUMEN_DATA_ROOT%/}"
    backup_root="${LUMEN_BACKUP_ROOT:-${data_root}/backup}"
    backup_root="${backup_root%/}"
    deploy_root="${ROOT%/}"
    tmp_dir="$(mktemp -d "${UPDATE_LOG_DIR:-/tmp}/lumen-update-runner.XXXXXX" 2>/dev/null || mktemp -d)"

    render_update_runner_unit "${src_path}" "${tmp_dir}/lumen-update.path" "${data_root}" "${backup_root}" "${deploy_root}"
    render_update_runner_unit "${src_runner}" "${tmp_dir}/lumen-update-runner.service" "${data_root}" "${backup_root}" "${deploy_root}"
    if [ -f "${src_warm_path}" ] && [ -f "${src_warm_service}" ]; then
        render_update_runner_unit "${src_warm_path}" "${tmp_dir}/lumen-update-warm.path" "${data_root}" "${backup_root}" "${deploy_root}"
        render_update_runner_unit "${src_warm_service}" "${tmp_dir}/lumen-update-warm.service" "${data_root}" "${backup_root}" "${deploy_root}"
    fi
    if [ -f "${src_backup_service}" ]; then
        render_update_runner_unit "${src_backup_service}" "${tmp_dir}/lumen-backup.service" "${data_root}" "${backup_root}" "${deploy_root}"
    fi
    if [ -f "${src_backup_timer}" ]; then
        render_update_runner_unit "${src_backup_timer}" "${tmp_dir}/lumen-backup.timer" "${data_root}" "${backup_root}" "${deploy_root}"
    fi
    if [ -f "${src_backup_path}" ]; then
        render_update_runner_unit "${src_backup_path}" "${tmp_dir}/lumen-backup.path" "${data_root}" "${backup_root}" "${deploy_root}"
    fi
    if [ -f "${src_restore_service}" ]; then
        render_update_runner_unit "${src_restore_service}" "${tmp_dir}/lumen-restore-runner.service" "${data_root}" "${backup_root}" "${deploy_root}"
    fi
    if [ -f "${src_restore_path}" ]; then
        render_update_runner_unit "${src_restore_path}" "${tmp_dir}/lumen-restore.path" "${data_root}" "${backup_root}" "${deploy_root}"
    fi
    local storage_unit
    for storage_unit in lumen-storage-mount.service \
        lumen-storage-apply.service lumen-storage-apply.path \
        lumen-storage-test.service lumen-storage-test.path; do
        if [ -f "${src_dir}/${storage_unit}" ]; then
            render_update_runner_unit "${src_dir}/${storage_unit}" "${tmp_dir}/${storage_unit}" "${data_root}" "${backup_root}" "${deploy_root}"
        fi
    done

    lumen_ensure_backup_service_user "${backup_root}"

    if ! lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update.path" "${LUMEN_SYSTEMD_UNIT_DIR%/}/lumen-update.path"; then
        log_error "[refresh_update_runner] 安装 lumen-update.path 失败，拒绝完成 switch。"
        rm -rf "${tmp_dir}"
        return 1
    fi
    if ! lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update-runner.service" "${LUMEN_SYSTEMD_UNIT_DIR%/}/lumen-update-runner.service"; then
        log_error "[refresh_update_runner] 安装 lumen-update-runner.service 失败，拒绝完成 switch。"
        rm -rf "${tmp_dir}"
        return 1
    fi
    if [ -f "${tmp_dir}/lumen-update-warm.path" ] && [ -f "${tmp_dir}/lumen-update-warm.service" ]; then
        if ! lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update-warm.path" "${LUMEN_SYSTEMD_UNIT_DIR%/}/lumen-update-warm.path"; then
            log_warn "[refresh_update_runner] 安装 lumen-update-warm.path 失败，镜像预热将不可用。"
        elif ! lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update-warm.service" "${LUMEN_SYSTEMD_UNIT_DIR%/}/lumen-update-warm.service"; then
            log_warn "[refresh_update_runner] 安装 lumen-update-warm.service 失败，镜像预热将不可用。"
        fi
    fi
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-backup.service "[refresh_update_runner] 安装 lumen-backup.service 失败，自动/手动触发备份将不可用。"
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-backup.timer "[refresh_update_runner] 安装 lumen-backup.timer 失败，自动备份将不可用。"
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-backup.path "[refresh_update_runner] 安装 lumen-backup.path 失败，管理后台立即备份将不可用。"
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-restore-runner.service "[refresh_update_runner] 安装 lumen-restore-runner.service 失败，管理后台恢复将不可用。"
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-restore.path "[refresh_update_runner] 安装 lumen-restore.path 失败，管理后台恢复将不可用。"
    for storage_unit in lumen-storage-mount.service \
        lumen-storage-apply.service lumen-storage-apply.path \
        lumen-storage-test.service lumen-storage-test.path; do
        lumen_install_optional_systemd_unit "${tmp_dir}" "${storage_unit}" "[refresh_update_runner] 安装 ${storage_unit} 失败，管理后台存储切换可能不可用。"
    done
    if [ -f "${src_storage_script}" ]; then
        lumen_run_as_root install -m 0755 "${src_storage_script}" "${LUMEN_LOCAL_SBIN_DIR%/}/lumen-storage-mount" \
            || log_warn "[refresh_update_runner] 安装 lumen-storage-mount 失败，管理后台存储切换不可用。"
    fi
    local storage_gid="${LUMEN_APP_STORAGE_GID:-${LUMEN_APP_GID:-10001}}"
    lumen_run_as_root install -d -m 0770 -o root -g "${storage_gid}" /var/lib/lumen-storage \
        || log_warn "[refresh_update_runner] 创建 /var/lib/lumen-storage 失败，API 可能无法写入触发文件。"
    if ! lumen_run_as_root systemctl daemon-reload; then
        log_error "[refresh_update_runner] systemctl daemon-reload 失败，拒绝完成 switch。"
        rm -rf "${tmp_dir}"
        return 1
    fi
    if ! lumen_run_as_root systemctl enable --now lumen-update.path; then
        log_error "[refresh_update_runner] 启用 lumen-update.path 失败，拒绝完成 switch。"
        rm -rf "${tmp_dir}"
        return 1
    fi
    if [ -f "${tmp_dir}/lumen-update-warm.path" ]; then
        if ! lumen_run_as_root systemctl enable --now lumen-update-warm.path; then
            log_warn "[refresh_update_runner] 启用 lumen-update-warm.path 失败，镜像预热将不可用；可稍后手动执行 systemctl enable --now lumen-update-warm.path。"
        fi
    fi
    lumen_enable_optional_systemd_unit "${tmp_dir}" lumen-backup.timer "[refresh_update_runner] 启用 lumen-backup.timer 失败，自动备份将不可用；可稍后手动执行 systemctl enable --now lumen-backup.timer。"
    lumen_enable_optional_systemd_unit "${tmp_dir}" lumen-backup.path "[refresh_update_runner] 启用 lumen-backup.path 失败，管理后台立即备份将不可用；可稍后手动执行 systemctl enable --now lumen-backup.path。"
    lumen_enable_optional_systemd_unit "${tmp_dir}" lumen-restore.path "[refresh_update_runner] 启用 lumen-restore.path 失败，管理后台恢复将不可用；可稍后手动执行 systemctl enable --now lumen-restore.path。"
    if [ -f "${tmp_dir}/lumen-storage-apply.path" ] && [ -f "${tmp_dir}/lumen-storage-test.path" ]; then
        lumen_run_as_root systemctl enable --now lumen-storage-apply.path lumen-storage-test.path \
            || log_warn "[refresh_update_runner] 启用 storage path watchers 失败，管理后台存储切换不可用。"
    fi
    if [ -f "${tmp_dir}/lumen-storage-mount.service" ]; then
        lumen_run_as_root systemctl enable lumen-storage-mount.service \
            || log_warn "[refresh_update_runner] 启用 lumen-storage-mount.service 失败，重启后需手工恢复挂载。"
    fi
    rm -rf "${tmp_dir}"

    log_info "一键更新 runner 已刷新：监听 ${backup_root}/.update.trigger"
    emit_info refresh_update_runner update_trigger "${backup_root}/.update.trigger"
    emit_info refresh_update_runner warm_trigger "${backup_root}/.warm.trigger"
    emit_info refresh_update_runner backup_trigger "${backup_root}/.backup.trigger"
    emit_info refresh_update_runner restore_trigger "${backup_root}/.restore.trigger"
    return 0
}

# 计算磁盘可用 GB（取 /opt 所在 fs）。失败回 -1。
