#!/usr/bin/env bash
# Release activation, host control planes, health checks, and summary.
# Sourced by scripts/install.sh after raw bootstrap has completed.

install_alembic_revision_from_output() {
    awk 'NF && !/^INFO/ {print $1; exit}'
}

install_release_declared_alembic_head() {
    local manifest="${RELEASE_DIR}/release-manifest.json"
    if [ -f "${manifest}" ] && [ ! -L "${manifest}" ]; then
        python3 - "${manifest}" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
heads = payload.get("alembic_heads") if isinstance(payload, dict) else None
if (
    not isinstance(heads, list)
    or len(heads) != 1
    or not isinstance(heads[0], str)
    or not re.fullmatch(r"[0-9A-Za-z_]+", heads[0])
):
    raise SystemExit(1)
print(heads[0])
PY
        return $?
    fi
    _install_compose --profile migrate run --rm migrate alembic heads \
        2>/dev/null | install_alembic_revision_from_output
}

write_install_release_metadata() {
    local expected_head="" applied_head="" sha="" branch="" meta=""
    expected_head="$(install_release_declared_alembic_head 2>/dev/null || true)"
    applied_head="$(
        _install_compose --profile migrate run --rm migrate alembic current \
            2>/dev/null | install_alembic_revision_from_output
    )"
    if [[ ! "${expected_head}" =~ ^[0-9A-Za-z_]+$ ]] \
            || [[ ! "${applied_head}" =~ ^[0-9A-Za-z_]+$ ]]; then
        log_error "无法证明 fresh install 的 Alembic capability：expected=${expected_head:-<unknown>} applied=${applied_head:-<unknown>}。"
        return 1
    fi
    if [ "${expected_head}" != "${applied_head}" ]; then
        log_error "fresh install Alembic head 不一致：release=${expected_head} database=${applied_head}。"
        return 1
    fi
    sha="${INSTALL_SOURCE_COMMIT:-}"
    if [ -z "${sha}" ] && [ -f "${RELEASE_DIR}/.source-commit" ]; then
        sha="$(head -n1 "${RELEASE_DIR}/.source-commit" 2>/dev/null || true)"
    fi
    if [ -d "${ROOT}/.git" ] && command -v git >/dev/null 2>&1; then
        branch="$(git -C "${ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    fi
    meta="${RELEASE_DIR}/.lumen_release.json"
    python3 - "${meta}" "${RELEASE_ID}" "${sha}" "${branch}" \
        "${expected_head}" "${applied_head}" <<'PY'
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone

path = Path(sys.argv[1])
payload = {
    "id": sys.argv[2],
    "sha": sys.argv[3],
    "branch": sys.argv[4],
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "alembic_head_expected": sys.argv[5],
    "alembic_head_applied": sys.argv[6],
}
fd, temporary_raw = tempfile.mkstemp(
    prefix=".lumen-release.",
    suffix=".tmp",
    dir=path.parent,
)
temporary = Path(temporary_raw)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fchmod(handle.fileno(), 0o644)
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
                raise
    finally:
        os.close(directory_fd)
finally:
    temporary.unlink(missing_ok=True)
PY
    log_info "fresh install release 已声明 Alembic capability：${expected_head}"
}

install_config_read_group() {
    local operator_user="${LUMEN_INSTALL_OPERATOR_USER:-${SUDO_USER:-}}"
    local config_group="${LUMEN_INSTALL_CONFIG_GROUP:-${LUMEN_CONFIG_READ_GROUP:-}}"
    if [ -z "${operator_user}" ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
        operator_user="$(id -un 2>/dev/null || true)"
    fi
    if [ -z "${config_group}" ] && [ -n "${operator_user}" ] \
            && [ "${operator_user}" != "root" ]; then
        config_group="$(id -gn "${operator_user}" 2>/dev/null || true)"
    fi
    config_group="${config_group:-${LUMEN_BACKUP_SERVICE_GROUP:-lumen-backup}}"
    case "${config_group}" in
        ''|*[!A-Za-z0-9_.-]*)
            log_error "非法的 shared/.env 读取组：${config_group:-<empty>}。"
            return 1
            ;;
    esac
    if [ -n "${operator_user}" ] && [ "${operator_user}" != "root" ] \
            && ! id -Gn "${operator_user}" 2>/dev/null \
                | tr ' ' '\n' | grep -Fxq "${config_group}"; then
        log_error "安装账户 ${operator_user} 当前不属于配置读取组 ${config_group}。"
        log_error "请使用该账户已有的私有组设置 LUMEN_INSTALL_CONFIG_GROUP 后重跑。"
        return 1
    fi
    printf '%s\n' "${config_group}"
}

harden_install_release_ownership() {
    local config_group=""
    config_group="$(install_config_read_group)" || return 1
    LUMEN_CONFIG_READ_GROUP="${config_group}"
    export LUMEN_CONFIG_READ_GROUP
    if ! lumen_ensure_backup_service_user \
            "${LUMEN_BACKUP_ROOT:-${LUMEN_DATA_ROOT%/}/backup}"; then
        log_error "无法安全配置备份服务用户与私有 recovery journal。"
        return 1
    fi
    if ! lumen_release_harden_ownership \
            "${DEPLOY_ROOT}" "${RELEASE_DIR}" "${SHARED_DIR}" \
            "${LUMEN_APP_UID}" "${LUMEN_APP_GID}" "${config_group}"; then
        log_error "无法收紧 release/shared ownership，拒绝启动应用服务。"
        return 1
    fi
    if [ -f "${SHARED_DIR}/.env" ] && [ ! -r "${SHARED_DIR}/.env" ]; then
        log_error "shared/.env 已收紧为 0640，但当前安装账户失去读取权限。"
        return 1
    fi
    if ! install_transaction_harden_journal; then
        log_error "无法把 install journal 收紧为 root-owned。"
        return 1
    fi
    log_info "release/shared 顶层与运维脚本已 root-owned；仅 runtime 子目录授予应用 UID。"
}

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
    if ! lumen_release_harden_ownership \
            "${DEPLOY_ROOT}" "${RELEASE_DIR}" "${SHARED_DIR}" \
            "${LUMEN_APP_UID}" "${LUMEN_APP_GID}" \
            "${LUMEN_CONFIG_READ_GROUP:-${LUMEN_BACKUP_SERVICE_GROUP:-lumen-backup}}"; then
        log_error "current 切换后 ownership 收口失败。"
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

    if ! lumen_ensure_backup_service_user "${backup_root}"; then
        log_error "备份目录权限迁移失败，拒绝安装 host operations units。"
        rm -rf "${tmp_dir}"
        return 1
    fi

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

    local tmp_dir storage_gid unit boot_mount_safe=1
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
    if lumen_run_as_root test -L /var/lib/lumen-storage/last-good.conf \
            || { lumen_run_as_root test -e /var/lib/lumen-storage/last-good.conf \
                && ! lumen_run_as_root test -f \
                    /var/lib/lumen-storage/last-good.conf; }; then
        boot_mount_safe=0
        log_warn "last-good.conf 类型不安全，拒绝启用 boot-time storage mount。"
    elif ! lumen_run_as_root test -e /var/lib/lumen-storage/last-good.conf; then
        if lumen_run_as_root test -L /var/lib/lumen-storage/unmanaged-direct; then
            boot_mount_safe=0
            log_warn "unmanaged-direct marker 是符号链接，拒绝覆盖。"
        elif ! printf 'schema=1\nmode=unmanaged-direct\n' \
                | lumen_run_as_root tee \
                    /var/lib/lumen-storage/unmanaged-direct >/dev/null \
                || ! lumen_run_as_root chmod 0640 \
                    /var/lib/lumen-storage/unmanaged-direct \
                || ! lumen_run_as_root chown \
                    "root:${storage_gid}" \
                    /var/lib/lumen-storage/unmanaged-direct \
                || ! lumen_run_as_root sync -f \
                    /var/lib/lumen-storage/unmanaged-direct; then
            boot_mount_safe=0
            log_warn "初始化直连存储保护标记失败；重启前必须先完成存储 apply。"
        fi
    fi
    lumen_run_as_root systemctl daemon-reload \
        || log_warn "刷新 storage systemd units 失败。"
    lumen_run_as_root systemctl enable --now lumen-storage-apply.path lumen-storage-test.path \
        || log_warn "启用 storage path watchers 失败，管理后台存储切换不可用。"
    # Boot-time mount is enabled for future reboots, but deliberately not
    # started during install. The first admin apply performs the controlled
    # stop/remount/start cycle after explicit configuration.
    if [ "${boot_mount_safe}" -eq 1 ]; then
        lumen_run_as_root systemctl enable lumen-storage-mount.service \
            || log_warn "启用 lumen-storage-mount.service 失败，重启后需手工恢复挂载。"
    else
        lumen_run_as_root systemctl disable lumen-storage-mount.service \
            >/dev/null 2>&1 || true
        log_warn "boot-time storage mount 未启用；修复 last-good/marker 后再启用。"
    fi
    rm -rf "${tmp_dir}"
    log_info "存储控制面已安装，共享目录 gid=${storage_gid}。"
}


# ---------------------------------------------------------------------------
# I. 健康检查（HTTP + Compose 状态）
# ---------------------------------------------------------------------------
run_health_checks() {
    emit_step_start health_post "健康检查（HTTP + compose service 状态）"

    local ready_url="${LUMEN_API_READY_URL:-http://127.0.0.1:8000/readyz}"
    local ready_attempts="${LUMEN_INSTALL_CORE_READINESS_ATTEMPTS:-60}"
    local ready_interval="${LUMEN_INSTALL_CORE_READINESS_INTERVAL_SECONDS:-2}"
    local readiness_compose_dir="${RELEASE_DIR:-${ROOT:-.}}"
    if ! _install_core_readiness \
            "${readiness_compose_dir}" "${ready_url}" \
            "${ready_attempts}" "${ready_interval}"; then
        log_error "核心 readiness 失败：API ${ready_url} 或 Worker health 未通过。"
        log_error "  排查：${COMPOSE_LABEL} logs --tail=200 api worker"
        exit 1
    fi
    log_info "API /readyz 与 Worker health 通过。"

    if ! _install_health_http "http://127.0.0.1:3000/" 60 2; then
        log_error "Web 健康检查失败：http://127.0.0.1:3000/ 在 60s 内未返回 2xx/3xx。"
        log_error "  排查：${COMPOSE_LABEL} logs --tail=200 web"
        exit 1
    fi
    log_info "Web 首页通过。"

    local health_services=("api" "worker" "web")
    local shared_env="${SHARED_DIR}/.env"
    if [ -n "$(env_file_get TELEGRAM_BOT_TOKEN "${shared_env}")" ]; then
        health_services+=("tgbot")
    fi
    if ! _install_health_compose "${health_services[@]}"; then
        log_error "compose service 健康状态异常。"
        exit 1
    fi
    log_info "已启用的 compose service 全部健康：${health_services[*]}"
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
  API liveness ...... http://127.0.0.1:8000/healthz
  API readiness ..... http://127.0.0.1:8000/readyz
  Worker readiness .. python -m app.worker_health check
  管理员邮箱 ....... ${INSTALL_ADMIN_EMAIL:-（已存在或非交互模式未设置）}
  Provider 配置 .... 登录后 → 右上角「管理 → 上游 Provider」
                     默认 PROVIDERS=[]，需添加 1 条才能调图像 API

  部署目录 ......... ${DEPLOY_ROOT}/current → releases/${RELEASE_ID}
  数据目录 ......... storage/backup=${LUMEN_DATA_ROOT}，postgres/redis=${LUMEN_DB_ROOT}
  共享 .env ........ ${SHARED_DIR}/.env
  镜像 tag ......... ${image_tag}
  tgbot ............ ${INSTALL_TGBOT_STATUS:-unknown}（started=已通过 healthcheck / skipped=未配置）

  ${LUMEN_C_BOLD}日常运维${LUMEN_C_RESET}

    状态：    cd ${DEPLOY_ROOT}/current && COMPOSE_PROJECT_NAME=lumen docker compose ps
    日志：    cd ${DEPLOY_ROOT}/current && COMPOSE_PROJECT_NAME=lumen docker compose logs -f api
    更新：    bash ${DEPLOY_ROOT}/current/scripts/lumenctl.sh update-lumen
    备份：    bash ${DEPLOY_ROOT}/current/scripts/backup.sh   （输出到 ${LUMEN_DATA_ROOT}/backup）
    卸载：    bash ${DEPLOY_ROOT}/current/scripts/uninstall.sh

EOF
    emit_step_done
}
