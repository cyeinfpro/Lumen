#!/usr/bin/env bash
# Host systemd, storage, and image-job helpers for lumenctl.sh.

detect_nologin_shell() {
    if [ -x /usr/sbin/nologin ]; then
        printf '/usr/sbin/nologin'
    elif [ -x /sbin/nologin ]; then
        printf '/sbin/nologin'
    else
        printf '/bin/false'
    fi
}
ensure_service_user() {
    local service_user="$1"
    local app_dir="$2"
    if [ "${service_user}" = "root" ]; then
        return 0
    fi
    if id "${service_user}" >/dev/null 2>&1; then
        return 0
    fi
    if ! command -v useradd >/dev/null 2>&1 \
        && ! as_sudo sh -c 'command -v useradd >/dev/null 2>&1'; then
        log_error "缺少命令 \"useradd\"。请先安装 shadow-utils/passwd，或手动创建 ${service_user} 用户。"
        exit 1
    fi
    local shell_path
    shell_path="$(detect_nologin_shell)"
    log_info "创建 system 用户：${service_user}"
    as_sudo useradd --system --home-dir "${app_dir}" --shell "${shell_path}" "${service_user}"
}

install_storage_units() {
    # 安装 lumen-storage-mount + 4 个 systemd unit（local/smb 切换的 host 端实现）。
    # 幂等：重复跑不会重启正在运行的 mount。
    ensure_linux_systemd
    require_sudo

    log_step "安装 Lumen 存储后端组件"

    local deploy_scripts="${ROOT}/deploy/scripts"
    local deploy_systemd="${ROOT}/deploy/systemd"
    local mount_script="${deploy_scripts}/lumen_storage_mount.sh"

    if [ ! -f "${mount_script}" ]; then
        log_error "找不到 ${mount_script}（请在 Lumen 仓库根目录或 release current 下运行）"
        exit 1
    fi

    # 1) 共享通信目录：/var/lib/lumen-storage（host ↔ lumen-api 容器双向 bind）
    local storage_gid="${LUMEN_APP_STORAGE_GID:-${LUMEN_APP_GID:-10001}}"
    log_info "创建 /var/lib/lumen-storage（root:${storage_gid} 0770）"
    as_sudo install -d -m 0770 -o root -g "${storage_gid}" /var/lib/lumen-storage

    # 2) 主脚本到 /usr/local/sbin
    log_info "安装 mount 脚本：/usr/local/sbin/lumen-storage-mount"
    as_sudo install -m 0755 "${mount_script}" /usr/local/sbin/lumen-storage-mount

    # 3) systemd 单元
    local unit
    for unit in lumen-storage-mount.service \
                lumen-storage-apply.service lumen-storage-apply.path \
                lumen-storage-test.service lumen-storage-test.path; do
        if [ -f "${deploy_systemd}/${unit}" ]; then
            as_sudo install -m 0644 "${deploy_systemd}/${unit}" "/etc/systemd/system/${unit}"
            log_info "  ${unit}"
        else
            log_warn "  ${deploy_systemd}/${unit} 不存在，跳过"
        fi
    done

    as_sudo systemctl daemon-reload

    # 4) 启用 path-watcher（用于 admin UI 触发 apply / test）
    log_info "启用 path watchers"
    as_sudo systemctl enable --now lumen-storage-apply.path lumen-storage-test.path

    # mount.service 视情况启用：默认无配置时回退到本地路径
    if as_sudo systemctl enable --now lumen-storage-mount.service 2>/dev/null; then
        log_info "lumen-storage-mount.service 已启用并启动"
    else
        log_warn "lumen-storage-mount.service 启动失败（默认会回退到本地路径，admin UI 配好后再 systemctl restart 即可）"
    fi

    log_info "完成。下一步：在管理后台「存储后端」页面配置 local 或 smb。"
}

stage_image_job_package() {
    local app_dir="$1"
    local stage_dir="${app_dir}/.image_job.stage.$$"
    as_sudo rm -rf "${stage_dir}"
    as_sudo mkdir -p "${stage_dir}"
    if ! as_sudo cp -R "${ROOT}/image-job/image_job/." "${stage_dir}/"; then
        as_sudo rm -rf "${stage_dir}"
        return 1
    fi
    if ! as_sudo find "${stage_dir}" -type d -name __pycache__ \
            -prune -exec rm -rf {} + \
            || ! as_sudo find "${stage_dir}" -type f -name '*.pyc' -delete \
            || ! as_sudo find "${stage_dir}" -type d -exec chmod 0755 {} + \
            || ! as_sudo find "${stage_dir}" -type f -name '*.py' \
                -exec chmod 0644 {} +; then
        as_sudo rm -rf "${stage_dir}"
        return 1
    fi
    printf '%s\n' "${stage_dir}"
}

IMAGE_JOB_PACKAGE_BACKUP=""

replace_image_job_package() {
    local app_dir="$1"
    local stage_dir="$2"
    local old_dir="${app_dir}/.image_job.previous.$$"

    as_sudo rm -rf "${old_dir}"
    IMAGE_JOB_PACKAGE_BACKUP=""
    if as_sudo test -e "${app_dir}/image_job" \
            || as_sudo test -L "${app_dir}/image_job"; then
        if ! as_sudo mv "${app_dir}/image_job" "${old_dir}"; then
            as_sudo rm -rf "${stage_dir}"
            return 1
        fi
        IMAGE_JOB_PACKAGE_BACKUP="${old_dir}"
    fi
    if ! as_sudo mv "${stage_dir}" "${app_dir}/image_job"; then
        if as_sudo test -e "${old_dir}"; then
            as_sudo mv "${old_dir}" "${app_dir}/image_job" || true
        fi
        IMAGE_JOB_PACKAGE_BACKUP=""
        as_sudo rm -rf "${stage_dir}"
        return 1
    fi
}

rollback_image_job_package() {
    local app_dir="$1"
    as_sudo rm -rf "${app_dir}/image_job"
    if [ -n "${IMAGE_JOB_PACKAGE_BACKUP}" ] \
            && as_sudo test -e "${IMAGE_JOB_PACKAGE_BACKUP}"; then
        as_sudo mv "${IMAGE_JOB_PACKAGE_BACKUP}" "${app_dir}/image_job" \
            || return 1
    fi
    IMAGE_JOB_PACKAGE_BACKUP=""
}

finalize_image_job_package() {
    if [ -n "${IMAGE_JOB_PACKAGE_BACKUP}" ]; then
        as_sudo rm -rf "${IMAGE_JOB_PACKAGE_BACKUP}" || return 1
    fi
    IMAGE_JOB_PACKAGE_BACKUP=""
}

image_job_wait_healthy() {
    local url="$1"
    local attempts="${LUMEN_IMAGE_JOB_HEALTH_ATTEMPTS:-30}"
    local interval="${LUMEN_IMAGE_JOB_HEALTH_INTERVAL_SECONDS:-1}"
    local attempt=0
    command -v curl >/dev/null 2>&1 || return 1
    while [ "${attempt}" -lt "${attempts}" ]; do
        attempt=$((attempt + 1))
        if curl --noproxy '*' -fsS --max-time 5 "${url}" >/dev/null 2>&1; then
            return 0
        fi
        [ "${attempt}" -ge "${attempts}" ] || sleep "${interval}"
    done
    return 1
}

image_job_env_value() {
    local env_file="$1"
    local key="$2"
    awk -F= -v target="${key}" '
        $1 == target {
            sub(/^[^=]*=/, "")
            print
            exit
        }
    ' "${env_file}"
}

image_job_upsert_env_value() {
    local env_file="$1"
    local key="$2"
    local value="$3"
    local next_file value_file
    next_file="$(mktemp "${env_file}.new.XXXXXX")" || return 1
    value_file="$(mktemp "${env_file}.value.XXXXXX")" || {
        rm -f "${next_file}"
        return 1
    }
    chmod 0600 "${next_file}" "${value_file}"
    if ! printf '%s' "${value}" > "${value_file}"; then
        rm -f "${next_file}" "${value_file}"
        return 1
    fi
    if ! awk -v target="${key}" -v value_file="${value_file}" '
        BEGIN {
            if ((getline replacement < value_file) < 0) exit 2
            close(value_file)
            replaced = 0
        }
        index($0, target "=") == 1 {
            if (!replaced) {
                print target "=" replacement
                replaced = 1
            }
            next
        }
        { print }
        END {
            if (!replaced) print target "=" replacement
        }
    ' "${env_file}" > "${next_file}"; then
        rm -f "${next_file}" "${value_file}"
        return 1
    fi
    rm -f "${value_file}"
    chmod 0600 "${next_file}"
    mv "${next_file}" "${env_file}"
}

image_job_db_has_encrypted_credentials() {
    local python_bin="$1"
    local db_path="$2"
    as_sudo "${python_bin}" -c '
import pathlib
import sqlite3
import sys

path = pathlib.Path(sys.argv[1])
if not path.is_file() or path.stat().st_size == 0:
    raise SystemExit(1)
try:
    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "auth_ciphertext" not in columns:
        raise SystemExit(1)
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE auth_ciphertext IS NOT NULL LIMIT 1"
    ).fetchone()
except sqlite3.DatabaseError:
    raise SystemExit(2)
finally:
    try:
        conn.close()
    except NameError:
        pass
raise SystemExit(0 if row is not None else 1)
' "${db_path}"
}

prepare_image_job_credential_env() {
    local env_file="$1"
    local db_path="$2"
    local python_bin="$3"
    local key_id master_secret encrypted_state=0

    key_id="$(image_job_env_value "${env_file}" IMAGE_JOB_CREDENTIAL_ACTIVE_KEY_ID)"
    master_secret="$(
        image_job_env_value "${env_file}" IMAGE_JOB_CREDENTIAL_MASTER_SECRET
    )"

    if [ "${#master_secret}" -lt 32 ] \
            || [[ "${master_secret}" =~ [[:space:]] ]]; then
        image_job_db_has_encrypted_credentials "${python_bin}" "${db_path}" \
            || encrypted_state=$?
        if [ "${encrypted_state}" -eq 0 ]; then
            log_error "image-job 数据库已有加密凭据，但 master secret 缺失或无效。"
            log_error "请从 /etc/image-job/image-job.env 的备份恢复原始密钥。"
            return 1
        fi
        if [ "${encrypted_state}" -ne 1 ]; then
            log_error "无法检查 image-job 数据库中的加密凭据，拒绝改写密钥。"
            return 1
        fi
        master_secret="$(
            "${python_bin}" -c 'import secrets; print(secrets.token_urlsafe(48))'
        )"
    fi

    if [[ ! "${key_id}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]]; then
        key_id="v1"
    fi

    image_job_upsert_env_value \
        "${env_file}" IMAGE_JOB_CREDENTIAL_ACTIVE_KEY_ID "${key_id}"
    image_job_upsert_env_value \
        "${env_file}" IMAGE_JOB_CREDENTIAL_MASTER_SECRET "${master_secret}"
}

install_image_job() {
    ensure_linux_systemd
    require_sudo
    ensure_cmd python3 "请安装 Python 3.11+"

    local app_dir data_dir state_dir db_path upstream_base public_base listen_host listen_port
    local concurrency python_bin service_user service_group
    local config_dir env_file sidecar_token tmp_env package_stage local_health_url
    local service_was_active=0

    log_step "安装 image-job sidecar"
    app_dir="$(read_or_default '应用目录' '/opt/image-job')"
    data_dir="$(read_or_default '数据目录' "${app_dir}/data")"
    state_dir="$(read_or_default '状态目录' '/var/lib/image-job/state')"
    upstream_base="$(strip_trailing_slash "$(read_or_default 'sub2api/OpenAI 兼容上游 base URL（按实际地址填写）' 'http://127.0.0.1:8081')")"
    public_base="$(strip_trailing_slash "$(read_or_default 'image-job 公网 base URL' 'https://example.com')")"
    listen_host="$(read_or_default '监听地址' '127.0.0.1')"
    listen_port="$(read_or_default '监听端口' '8091')"
    concurrency="$(read_or_default '图片任务并发' '2')"
    python_bin="$(read_or_default 'Python 命令' 'python3')"
    service_user="$(read_or_default 'systemd 运行用户' 'image-job')"

    validate_absolute_path "应用目录" "${app_dir}" || exit 1
    validate_absolute_path "数据目录" "${data_dir}" || exit 1
    validate_absolute_path "状态目录" "${state_dir}" || exit 1
    validate_url_like "sub2api/OpenAI 兼容上游 base URL" "${upstream_base}" || exit 1
    validate_url_like "image-job 公网 base URL" "${public_base}" || exit 1
    validate_host_port_target "监听地址" "${listen_host}" || exit 1
    validate_tcp_port "监听端口" "${listen_port}" || exit 1
    validate_positive_int "图片任务并发" "${concurrency}" || exit 1
    validate_python_command "Python 命令" "${python_bin}" || exit 1
    validate_service_user_name "systemd 运行用户" "${service_user}" || exit 1
    ensure_python_min_version "${python_bin}" 3 11
    if as_sudo systemctl is-active --quiet image-job; then
        service_was_active=1
    fi
    probe_sub2api_upstream "${upstream_base}"

    ensure_service_user "${service_user}" "${app_dir}"
    service_group="$(id -gn "${service_user}" 2>/dev/null || printf '%s' "${service_user}")"
    db_path="${state_dir}/image_jobs.sqlite3"
    config_dir="/etc/image-job"
    env_file="${config_dir}/image-job.env"

    log_step "复制 image-job 文件"
    as_sudo install -d -m 0755 "${app_dir}" "${data_dir}" "${data_dir}/images"
    as_sudo install -d -m 0755 "${data_dir}/images/temp" "${data_dir}/refs"
    as_sudo install -d -m 0700 "${state_dir}"
    package_stage="$(stage_image_job_package "${app_dir}")" || {
        log_error "image-job 包 staging 失败，保留现有安装。"
        exit 1
    }

    log_step "预检 image-job 服务凭证"
    tmp_env="$(mktemp)"
    chmod 0600 "${tmp_env}"
    if as_sudo test -f "${env_file}"; then
        as_sudo cat "${env_file}" > "${tmp_env}"
    fi
    sidecar_token="$(
        image_job_env_value "${tmp_env}" IMAGE_JOB_SIDECAR_TOKEN
    )"
    if [ "${#sidecar_token}" -lt 32 ] \
            || [[ "${sidecar_token}" =~ [[:space:]] ]]; then
        sidecar_token="$(
            "${python_bin}" -c 'import secrets; print(secrets.token_urlsafe(48))'
        )"
    fi
    if ! image_job_upsert_env_value \
            "${tmp_env}" IMAGE_JOB_SIDECAR_TOKEN "${sidecar_token}" \
            || ! prepare_image_job_credential_env \
                "${tmp_env}" "${db_path}" "${python_bin}"; then
        rm -f "${tmp_env}"
        as_sudo rm -rf "${package_stage}"
        exit 1
    fi

    if ! replace_image_job_package "${app_dir}" "${package_stage}"; then
        rm -f "${tmp_env}"
        log_error "image-job 包切换失败，保留现有安装。"
        exit 1
    fi
    as_sudo install -m 0644 "${ROOT}/image-job/app.py" "${app_dir}/app.py"
    # 旧版把这些实现装在 app.py 旁边；现在实现只存在于 image_job 包内。
    # 只有新包成功接管后才删除旧副本，失败时保留完整的旧安装。
    as_sudo rm -f \
        "${app_dir}/image_artifacts.py" \
        "${app_dir}/image_candidates.py" \
        "${app_dir}/image_url_security.py" \
        "${app_dir}/job_persistence.py" \
        "${app_dir}/payload_helpers.py" \
        "${app_dir}/request_bodies.py" \
        "${app_dir}/upstream_runtime.py"
    # 旧版本装过 runtime_config.py，现已删除（死代码，配置由 image_job/config.py
    # 提供）。升级时清掉残件，免得留一份没人读的环境变量副本误导排障。
    as_sudo rm -f "${app_dir}/runtime_config.py"
    as_sudo install -m 0644 "${ROOT}/image-job/requirements.txt" "${app_dir}/requirements.txt"
    as_sudo install -m 0644 "${ROOT}/image-job/README.md" "${app_dir}/README.md"
    as_sudo install -m 0644 "${ROOT}/image-job/image-job.md" "${app_dir}/image-job.md"

    log_step "创建 Python 虚拟环境并安装依赖"
    as_sudo "${python_bin}" -m venv "${app_dir}/.venv"
    as_sudo "${app_dir}/.venv/bin/pip" install -r "${app_dir}/requirements.txt"

    log_step "安装 image-job 服务凭证"
    as_sudo install -d -m 0755 "${config_dir}"
    as_sudo install -m 0600 "${tmp_env}" "${env_file}"
    rm -f "${tmp_env}"

    log_step "写入 systemd 服务"
    local tmp_unit
    tmp_unit="$(mktemp)"
    cat > "${tmp_unit}" <<EOF
[Unit]
Description=sub2api image async job sidecar
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=${service_user}
Group=${service_group}
WorkingDirectory=${app_dir}
EnvironmentFile=${env_file}
Environment=IMAGE_JOB_UPSTREAM_BASE_URL=${upstream_base}
Environment=IMAGE_JOB_PUBLIC_BASE_URL=${public_base}
Environment=IMAGE_JOB_ROOT_DIR=${app_dir}
Environment=IMAGE_JOB_DATA_DIR=${data_dir}
Environment=IMAGE_JOB_STATE_DIR=${state_dir}
Environment=IMAGE_JOB_DB_PATH=${db_path}
Environment=IMAGE_JOB_CONCURRENCY=${concurrency}
Environment=IMAGE_JOB_UPSTREAM_TIMEOUT_S=1800
Environment=IMAGE_JOB_RETENTION_DAYS=1
Environment=IMAGE_JOB_MAX_RETENTION_DAYS=1
Environment=IMAGE_JOB_JOB_TTL_DAYS=1
ExecStart=${app_dir}/.venv/bin/uvicorn app:app --host ${listen_host} --port ${listen_port}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    as_sudo install -m 0644 "${tmp_unit}" /etc/systemd/system/image-job.service
    rm -f "${tmp_unit}"
    as_sudo chown -R "${service_user}:${service_group}" "${app_dir}" "${data_dir}" "${state_dir}"
    as_sudo chmod 0700 "${state_dir}"

    as_sudo systemctl daemon-reload
    if [ "${service_was_active}" -eq 1 ]; then
        as_sudo systemctl restart image-job
    else
        as_sudo systemctl enable --now image-job
    fi

    log_step "image-job 健康检查"
    local_health_url="http://${listen_host}:${listen_port}/health"
    if ! image_job_wait_healthy "${local_health_url}"; then
        log_error "image-job 启动或健康检查失败，正在恢复上一版包。"
        as_sudo systemctl stop image-job >/dev/null 2>&1 || true
        if ! rollback_image_job_package "${app_dir}"; then
            log_error "image-job 包回滚失败，请立即人工恢复 ${app_dir}/image_job。"
            exit 1
        fi
        if [ "${service_was_active}" -eq 1 ]; then
            as_sudo systemctl restart image-job || true
            if ! image_job_wait_healthy "${local_health_url}"; then
                log_error "上一版 image-job 包已恢复，但服务 readiness 仍失败。"
            fi
        else
            as_sudo systemctl disable --now image-job >/dev/null 2>&1 || true
        fi
        exit 1
    fi
    finalize_image_job_package || {
        log_error "image-job 新包已健康，但清理上一版包失败。"
        exit 1
    }
    log_info "image-job 本机健康检查通过：${local_health_url}"

    cat <<EOF

  image-job 已安装：
    service:      image-job
    local health: http://${listen_host}:${listen_port}/health
    public base:  ${public_base}
    service auth: ${env_file}（仅 root 可读）

  如需暴露公网路由，请继续执行：
    bash scripts/lumenctl.sh nginx-optimize

EOF
}

uninstall_image_job() {
    ensure_linux_systemd
    require_sudo

    local app_dir state_root service_user
    log_step "卸载 image-job sidecar"
    app_dir="$(read_or_default '应用目录' '/opt/image-job')"
    state_root="$(read_or_default '状态根目录' '/var/lib/image-job')"
    service_user="$(read_or_default 'systemd 运行用户' 'image-job')"
    validate_absolute_path "应用目录" "${app_dir}" || exit 1
    validate_absolute_path "状态根目录" "${state_root}" || exit 1
    validate_service_user_name "systemd 运行用户" "${service_user}" || exit 1

    if systemctl list-unit-files image-job.service >/dev/null 2>&1; then
        as_sudo systemctl disable --now image-job || true
    else
        log_info "未发现 image-job.service，跳过停服务。"
    fi

    if [ -f /etc/systemd/system/image-job.service ]; then
        as_sudo rm -f /etc/systemd/system/image-job.service
        as_sudo systemctl daemon-reload
        log_info "已删除 /etc/systemd/system/image-job.service"
    fi

    if [ -d "${app_dir}" ]; then
        log_warn "应用目录包含源码、虚拟环境和临时图片：${app_dir}"
        if confirm "删除应用目录 ${app_dir}？"; then
            as_sudo rm -rf "${app_dir}"
            log_info "已删除 ${app_dir}"
        else
            log_info "保留 ${app_dir}"
        fi
    fi

    if [ -d "${state_root}" ]; then
        log_warn "状态目录包含 SQLite 任务库：${state_root}"
        if confirm "删除状态目录 ${state_root}？"; then
            as_sudo rm -rf "${state_root}"
            log_info "已删除 ${state_root}"
        else
            log_info "保留 ${state_root}"
        fi
    fi

    if [ "${service_user}" != "root" ] && id "${service_user}" >/dev/null 2>&1; then
        if confirm "删除 system 用户 ${service_user}？"; then
            as_sudo userdel "${service_user}" || log_warn "userdel ${service_user} 未成功，请手动检查。"
        fi
    fi

    log_step "image-job 卸载完成"
}
