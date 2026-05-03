#!/usr/bin/env bash
# Lumen 统一运维入口。
# 用法：
#   bash scripts/lumenctl.sh
#   bash scripts/lumenctl.sh install-lumen
#   bash scripts/lumenctl.sh install-image-job
#   bash scripts/lumenctl.sh nginx-optimize

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

ROOT="$(lumen_resolve_repo_root "${SCRIPT_DIR}")"
NGINX_FILES=()
LUMEN_USE_SUDO="${LUMEN_USE_SUDO:-0}"

trap 'log_error "lumenctl 失败：第 ${LINENO} 行返回非零状态。请查看上方输出修正后重试。"' ERR

usage() {
    cat <<EOF
Lumen 一键运维菜单

用法：
  bash scripts/lumenctl.sh [command]

commands:
  menu                 打开交互菜单（默认）
  install-lumen        安装 Lumen（调用 scripts/install.sh）
  update-lumen         更新 Lumen（调用 scripts/update.sh）
  uninstall-lumen      卸载 Lumen（调用 scripts/uninstall.sh）
  install-image-job    安装 image-job sidecar、systemd 服务
  uninstall-image-job  卸载 image-job sidecar
  nginx-scan           扫描 nginx 配置
  nginx-optimize       nginx 反代优化向导（Lumen / sub2api / image-job）
  nginx-lumen          生成/更新 Lumen 反代配置
  nginx-sub2api        生成/更新 sub2api 单机公网反代配置
  nginx-sub2api-inner  生成/更新 sub2api 内层反代配置
  nginx-sub2api-outer  生成/更新 sub2api 外层公网反代配置
  nginx-image-job      给已有站点注入 image-job 路由
  help                 显示帮助

EOF
}

require_sudo() {
    if [ "${EUID:-$(id -u)}" -eq 0 ]; then
        LUMEN_USE_SUDO=0
        return 0
    fi
    ensure_cmd sudo "请安装 sudo，或切换到 root 后重试"
    LUMEN_USE_SUDO=1
}

as_sudo() {
    if [ "${LUMEN_USE_SUDO:-0}" = "1" ]; then
        sudo "$@"
    else
        "$@"
    fi
}

ensure_linux_systemd() {
    if [ "$(detect_os)" != "linux" ]; then
        log_error "image-job systemd/nginx 自动部署仅支持 Linux 服务器。"
        exit 1
    fi
    ensure_cmd systemctl "请使用带 systemd 的 Linux 服务器"
}

strip_trailing_slash() {
    local value="$1"
    while [[ "${value}" == */ ]]; do
        value="${value%/}"
    done
    printf '%s' "${value}"
}

sanitize_name() {
    local value="$1"
    value="${value#http://}"
    value="${value#https://}"
    value="${value%%/*}"
    value="${value%%:*}"
    value="$(printf '%s' "${value}" | tr -c 'A-Za-z0-9_.-' '-')"
    value="${value##-}"
    value="${value%%-}"
    printf '%s' "${value:-site}"
}

validate_no_control_chars() {
    local name="$1"
    local value="$2"
    if printf '%s' "${value}" | LC_ALL=C grep -q '[[:cntrl:]]'; then
        log_error "${name} 不能包含控制字符。"
        return 1
    fi
    return 0
}

validate_no_whitespace() {
    local name="$1"
    local value="$2"
    validate_no_control_chars "${name}" "${value}" || return 1
    if [[ "${value}" =~ [[:space:]] ]]; then
        log_error "${name} 不能包含空白字符。"
        return 1
    fi
    return 0
}

validate_nginx_token() {
    local name="$1"
    local value="$2"
    validate_no_control_chars "${name}" "${value}" || return 1
    if [[ "${value}" =~ [\;\{\}\'\"\\] ]]; then
        log_error "${name} 不能包含 ; { } 引号或反斜杠。"
        return 1
    fi
    return 0
}

validate_domain_list() {
    local name="$1"
    local value="$2"
    validate_nginx_token "${name}" "${value}" || return 1
    if [ -z "${value}" ]; then
        log_error "${name} 不能为空。"
        return 1
    fi
    local token
    local tokens=()
    IFS=' ' read -r -a tokens <<< "${value}"
    for token in "${tokens[@]}"; do
        [ -n "${token}" ] || continue
        if [[ ! "${token}" =~ ^(\*\.[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+|_)$ ]]; then
            log_error "${name} 包含无效 server_name：${token}"
            return 1
        fi
    done
    return 0
}

validate_url_like() {
    local name="$1"
    local value="$2"
    validate_no_whitespace "${name}" "${value}" || return 1
    validate_nginx_token "${name}" "${value}" || return 1
    if [[ ! "${value}" =~ ^https?:// ]]; then
        log_error "${name} 必须以 http:// 或 https:// 开头。"
        return 1
    fi
    return 0
}

validate_host_port_target() {
    local name="$1"
    local value="$2"
    validate_no_whitespace "${name}" "${value}" || return 1
    validate_nginx_token "${name}" "${value}" || return 1
    if [ -z "${value}" ]; then
        log_error "${name} 不能为空。"
        return 1
    fi
    return 0
}

validate_tcp_port() {
    local name="$1"
    local value="$2"
    validate_no_control_chars "${name}" "${value}" || return 1
    if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
        log_error "${name} 必须是数字。"
        return 1
    fi
    if [ "${value}" -lt 1 ] || [ "${value}" -gt 65535 ]; then
        log_error "${name} 必须在 1-65535 之间。"
        return 1
    fi
    return 0
}

validate_positive_int() {
    local name="$1"
    local value="$2"
    validate_no_control_chars "${name}" "${value}" || return 1
    if [[ ! "${value}" =~ ^[0-9]+$ ]] || [ "${value}" -lt 1 ]; then
        log_error "${name} 必须是 >= 1 的整数。"
        return 1
    fi
    return 0
}

validate_path_value() {
    local name="$1"
    local value="$2"
    validate_no_whitespace "${name}" "${value}" || return 1
    if [ -z "${value}" ]; then
        log_error "${name} 不能为空。"
        return 1
    fi
    if [[ "${value}" =~ [[:cntrl:]\;\{\}\'\"\\] ]]; then
        log_error "${name} 不能包含控制字符、;、{ }、引号或反斜杠。"
        return 1
    fi
    if [ "${value}" = "/" ]; then
        log_error "${name} 不能是根目录 /。"
        return 1
    fi
    return 0
}

validate_absolute_path() {
    local name="$1"
    local value="$2"
    validate_path_value "${name}" "${value}" || return 1
    if [[ "${value}" != /* ]]; then
        log_error "${name} 必须是绝对路径。"
        return 1
    fi
    return 0
}

validate_service_user_name() {
    local name="$1"
    local value="$2"
    validate_no_whitespace "${name}" "${value}" || return 1
    if [[ ! "${value}" =~ ^[A-Za-z_][A-Za-z0-9_.-]*\$?$ ]] && [ "${value}" != "root" ]; then
        log_error "${name} 不是有效的 Linux 用户名：${value}"
        return 1
    fi
    return 0
}

validate_python_command() {
    local name="$1"
    local value="$2"
    validate_no_whitespace "${name}" "${value}" || return 1
    if [[ "${value}" = */* ]]; then
        if [[ "${value}" != /* ]]; then
            log_error "${name} 如包含 /，必须是绝对路径。"
            return 1
        fi
    elif [[ ! "${value}" =~ ^[A-Za-z0-9_.+-]+$ ]]; then
        log_error "${name} 不是有效命令名：${value}"
        return 1
    fi
    return 0
}

ensure_python_min_version() {
    local python_bin="$1"
    local min_major="$2"
    local min_minor="$3"
    if ! command -v "${python_bin}" >/dev/null 2>&1; then
        log_error "未找到 Python：${python_bin}"
        exit 1
    fi
    if ! "${python_bin}" - "${min_major}" "${min_minor}" <<'PY' >/dev/null 2>&1
import sys

major = int(sys.argv[1])
minor = int(sys.argv[2])
raise SystemExit(0 if sys.version_info >= (major, minor) else 1)
PY
    then
        log_error "${python_bin} 版本过低：$("${python_bin}" --version 2>&1)，需要 >= ${min_major}.${min_minor}"
        exit 1
    fi
}

probe_sub2api_upstream() {
    local upstream_base="$1"
    local probe_path probe_url status

    ensure_cmd curl "请安装 curl，用于安装 image-job 前探测 sub2api 上游"
    log_step "检查 sub2api/OpenAI 兼容上游是否可访问"
    log_info "上游地址（按实际部署填写，不固定端口）：${upstream_base}"

    for probe_path in /v1/models /v1/images/generations /v1/responses; do
        probe_url="${upstream_base}${probe_path}"
        status="$(curl -k -sS -o /dev/null -w '%{http_code}' \
            --connect-timeout 3 \
            --max-time 8 \
            "${probe_url}" 2>/dev/null || true)"
        case "${status}" in
            2??|3??|400|401|403|404|405|422)
                log_info "sub2api/OpenAI 兼容端点探测通过：${probe_url} -> HTTP ${status}"
                return 0
                ;;
        esac
    done

    for probe_path in /health /healthz /; do
        probe_url="${upstream_base}${probe_path}"
        status="$(curl -k -sS -o /dev/null -w '%{http_code}' \
            --connect-timeout 3 \
            --max-time 8 \
            "${probe_url}" 2>/dev/null || true)"
        case "${status}" in
            2??|3??|401|403)
                log_warn "上游地址可达：${probe_url} -> HTTP ${status}；请确认它是 sub2api/OpenAI 兼容服务。"
                return 0
                ;;
        esac
    done

    log_error "无法连接 sub2api/OpenAI 兼容上游：${upstream_base}"
    log_error "image-job 必须绑定一个已运行的 sub2api/OpenAI 兼容上游。"
    log_error "请先启动 sub2api，并确认当前机器可访问你填写的地址，例如：curl -i ${upstream_base}/v1/models"
    exit 1
}

run_lumen_script() {
    local script_name="$1"
    local script_path="${ROOT}/scripts/${script_name}"
    log_step "执行 ${script_name}"
    if [ ! -f "${script_path}" ]; then
        log_error "找不到脚本：${script_path}"
        exit 1
    fi
    case "${script_name}" in
        install.sh)
            if [ "$(detect_os)" = "linux" ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
                ensure_cmd sudo "请安装 sudo，或切换到 root 后重试"
                lumen_sudo bash "${script_path}" --install
            else
                bash "${script_path}" --install
            fi
            ;;
        update.sh|uninstall.sh)
            if [ "$(detect_os)" = "linux" ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
                ensure_cmd sudo "请安装 sudo，或切换到 root 后重试"
                lumen_sudo bash "${script_path}"
            else
                bash "${script_path}"
            fi
            ;;
        *)
            bash "${script_path}"
            ;;
    esac
}

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

install_image_job() {
    ensure_linux_systemd
    require_sudo
    ensure_cmd python3 "请安装 Python 3.11+"

    local app_dir data_dir state_dir db_path upstream_base public_base listen_host listen_port
    local concurrency python_bin service_user service_group

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
    probe_sub2api_upstream "${upstream_base}"

    ensure_service_user "${service_user}" "${app_dir}"
    service_group="$(id -gn "${service_user}" 2>/dev/null || printf '%s' "${service_user}")"
    db_path="${state_dir}/image_jobs.sqlite3"

    log_step "复制 image-job 文件"
    as_sudo install -d "${app_dir}" "${data_dir}/images/temp" "${data_dir}/refs" "${state_dir}"
    as_sudo install -m 0644 "${ROOT}/image-job/app.py" "${app_dir}/app.py"
    as_sudo install -m 0644 "${ROOT}/image-job/requirements.txt" "${app_dir}/requirements.txt"
    as_sudo install -m 0644 "${ROOT}/image-job/README.md" "${app_dir}/README.md"
    as_sudo install -m 0644 "${ROOT}/image-job/image-job.md" "${app_dir}/image-job.md"

    log_step "创建 Python 虚拟环境并安装依赖"
    as_sudo "${python_bin}" -m venv "${app_dir}/.venv"
    as_sudo "${app_dir}/.venv/bin/pip" install -r "${app_dir}/requirements.txt"

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

    as_sudo systemctl daemon-reload
    as_sudo systemctl enable --now image-job

    log_step "image-job 健康检查"
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS "http://${listen_host}:${listen_port}/health" >/dev/null; then
            log_info "image-job 本机健康检查通过：http://${listen_host}:${listen_port}/health"
        else
            log_warn "本机健康检查未通过，请查看：journalctl -u image-job -n 160 --no-pager"
        fi
    else
        log_info "未安装 curl，跳过 HTTP 健康检查。"
    fi

    cat <<EOF

  image-job 已安装：
    service:      image-job
    local health: http://${listen_host}:${listen_port}/health
    public base:  ${public_base}

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

collect_nginx_files() {
    NGINX_FILES=()
    local dirs=(
        /etc/nginx/sites-available
        /etc/nginx/sites-enabled
        /etc/nginx/conf.d
        /www/server/panel/vhost/nginx
        /usr/local/etc/nginx/servers
        /usr/local/etc/nginx/conf.d
    )
    local dir
    for dir in "${dirs[@]}"; do
        if [ -d "${dir}" ]; then
            while IFS= read -r file; do
                case "${file}" in
                    *.bak|*.bak.*|*.remote|*~|*.swp) continue ;;
                esac
                NGINX_FILES+=("${file}")
            done < <(as_sudo find "${dir}" -maxdepth 2 -type f -print 2>/dev/null | sort)
        fi
    done
}

print_nginx_file_summary() {
    local index="$1"
    local file="$2"
    local server_names listens has_jobs has_refs has_temp has_ref_static proxy_targets has_lumen_paths

    server_names="$(as_sudo sed -n 's/^[[:space:]]*server_name[[:space:]]\+\([^;]*\);.*/\1/p' "${file}" 2>/dev/null | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
    listens="$(as_sudo sed -n 's/^[[:space:]]*listen[[:space:]]\+\([^;]*\);.*/\1/p' "${file}" 2>/dev/null | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
    proxy_targets="$(as_sudo sed -n 's/^[[:space:]]*proxy_pass[[:space:]]\+\([^;]*\);.*/\1/p' "${file}" 2>/dev/null | sort -u | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
    has_jobs="no"
    has_refs="no"
    has_temp="no"
    has_ref_static="no"
    has_lumen_paths="no"
    as_sudo grep -Eq 'location[[:space:]]+[^;{]*[[:space:]]/v1/image-jobs' "${file}" 2>/dev/null && has_jobs="yes"
    as_sudo grep -Eq 'location[[:space:]]+[^;{]*[[:space:]]/v1/refs' "${file}" 2>/dev/null && has_refs="yes"
    as_sudo grep -Eq 'location[[:space:]]+[^;{]*[[:space:]]/images/temp/' "${file}" 2>/dev/null && has_temp="yes"
    as_sudo grep -Eq 'location[[:space:]]+[^;{]*[[:space:]]/refs/' "${file}" 2>/dev/null && has_ref_static="yes"
    as_sudo grep -Eq 'location[[:space:]]+[^;{]*[[:space:]]/(api/|events)' "${file}" 2>/dev/null && has_lumen_paths="yes"

    printf '  [%s] %s\n' "${index}" "${file}"
    printf '      listen:      %s\n' "${listens:-unknown}"
    printf '      server_name: %s\n' "${server_names:-unknown}"
    printf '      proxy_pass:  %s\n' "${proxy_targets:-none}"
    printf '      lumen:       /api or /events=%s\n' "${has_lumen_paths}"
    printf '      image-job:   /v1/image-jobs=%s /v1/refs=%s /images/temp=%s /refs=%s\n' \
        "${has_jobs}" "${has_refs}" "${has_temp}" "${has_ref_static}"
}

nginx_scan() {
    require_sudo
    ensure_cmd nginx "请先安装 nginx"

    log_step "扫描 nginx 配置"
    collect_nginx_files
    if [ "${#NGINX_FILES[@]}" -eq 0 ]; then
        log_warn "未在常见目录找到 nginx 站点配置。"
        log_warn "已扫描：/etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/conf.d /www/server/panel/vhost/nginx /usr/local/etc/nginx/servers /usr/local/etc/nginx/conf.d"
        return 0
    fi

    local i=1 file
    for file in "${NGINX_FILES[@]}"; do
        print_nginx_file_summary "${i}" "${file}"
        i=$((i + 1))
    done
}

default_nginx_output_file() {
    local role="$1"
    local server_names="$2"
    local primary safe
    primary="$(printf '%s' "${server_names}" | awk '{print $1}')"
    safe="$(sanitize_name "${primary}")"
    if [ -d /etc/nginx/sites-available ]; then
        printf '/etc/nginx/sites-available/%s-%s.conf' "${safe}" "${role}"
    elif [ -d /etc/nginx/conf.d ]; then
        printf '/etc/nginx/conf.d/%s-%s.conf' "${safe}" "${role}"
    elif [ -d /www/server/panel/vhost/nginx ]; then
        printf '/www/server/panel/vhost/nginx/%s-%s.conf' "${safe}" "${role}"
    elif [ -d /usr/local/etc/nginx/servers ]; then
        printf '/usr/local/etc/nginx/servers/%s-%s.conf' "${safe}" "${role}"
    else
        printf '/etc/nginx/conf.d/%s-%s.conf' "${safe}" "${role}"
    fi
}

nginx_backup_path() {
    local target_file="$1"
    local timestamp="$2"
    local backup_dir safe
    backup_dir="${LUMEN_NGINX_BACKUP_DIR:-/var/backups/lumenctl/nginx}"
    safe="$(printf '%s' "${target_file}" | tr '/' '_' | tr -c 'A-Za-z0-9_.-' '-')"
    safe="${safe##-}"
    safe="${safe%%-}"
    as_sudo install -d -m 0750 "${backup_dir}"
    printf '%s/%s.lumenctl.%s.bak' "${backup_dir}" "${safe:-nginx}" "${timestamp}"
}

install_nginx_config_file() {
    local tmp_file="$1"
    local target_file="$2"
    local timestamp backup target_dir link_file created_target created_link

    target_dir="$(dirname "${target_file}")"
    timestamp="$(date '+%Y%m%d%H%M%S')"
    backup=""
    created_target=0
    created_link=0
    as_sudo install -d "${target_dir}"

    if [ -f "${target_file}" ]; then
        backup="$(nginx_backup_path "${target_file}" "${timestamp}")"
        as_sudo cp -p "${target_file}" "${backup}"
        log_info "已备份：${backup}"
    else
        created_target=1
    fi

    as_sudo install -m 0644 "${tmp_file}" "${target_file}"
    rm -f "${tmp_file}"

    if [[ "${target_file}" == /etc/nginx/sites-available/* ]] && [ -d /etc/nginx/sites-enabled ]; then
        link_file="/etc/nginx/sites-enabled/$(basename "${target_file}")"
        if [ ! -e "${link_file}" ]; then
            as_sudo ln -s "${target_file}" "${link_file}"
            created_link=1
            log_info "已启用站点：${link_file}"
        fi
    fi

    log_step "验证 nginx 配置"
    if ! as_sudo nginx -t; then
        log_error "nginx -t 未通过，正在回滚 ${target_file}。"
        if [ "${created_link}" = "1" ] && [ -n "${link_file:-}" ]; then
            as_sudo rm -f "${link_file}"
        fi
        if [ -n "${backup}" ]; then
            as_sudo cp -p "${backup}" "${target_file}"
        elif [ "${created_target}" = "1" ]; then
            as_sudo rm -f "${target_file}"
        fi
        as_sudo nginx -t || true
        exit 1
    fi

    if confirm "nginx -t 已通过，是否 reload nginx？"; then
        if command -v systemctl >/dev/null 2>&1; then
            as_sudo systemctl reload nginx
        else
            as_sudo nginx -s reload
        fi
        log_info "nginx 已 reload。"
    else
        log_info "已写入配置但未 reload。需要生效时执行：sudo systemctl reload nginx"
    fi
}

write_lumen_nginx_config() {
    local out="$1"
    local server_names="$2"
    local web_upstream="$3"
    local http_redirect="$4"
    local tls_mode="$5"
    local cert_file="$6"
    local key_file="$7"
    local primary zone_suffix zone_api zone_events upstream_name

    primary="$(printf '%s' "${server_names}" | awk '{print $1}')"
    zone_suffix="$(sanitize_name "${primary}" | tr '.-' '__')"
    zone_api="lumen_api_${zone_suffix}"
    zone_events="lumen_events_${zone_suffix}"
    upstream_name="lumen_web_${zone_suffix}"

    {
        cat <<EOF
# Managed by scripts/lumenctl.sh.
# Lumen reverse proxy: Next.js web, /api rewrite, and /events SSE.

limit_req_zone \$binary_remote_addr zone=${zone_api}:10m rate=10r/s;
limit_req_zone \$binary_remote_addr zone=${zone_events}:10m rate=30r/m;

upstream ${upstream_name} {
EOF
        printf '  server %s;\n' "${web_upstream}"
        cat <<'EOF'
  keepalive 32;
}

EOF
        if [ "${http_redirect}" = "1" ]; then
            cat <<EOF
server {
  listen 80;
  listen [::]:80;
  server_name ${server_names};
  return 301 https://\$host\$request_uri;
}

EOF
        fi
        if [ "${tls_mode}" = "1" ]; then
            cat <<EOF
server {
  listen 443 ssl;
  listen [::]:443 ssl;
  http2 on;
  server_name ${server_names};

  ssl_certificate ${cert_file};
  ssl_certificate_key ${key_file};
  ssl_protocols TLSv1.2 TLSv1.3;
  ssl_session_cache shared:SSL:10m;
  ssl_session_timeout 1d;
  add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
EOF
        else
            cat <<EOF
server {
  listen 80;
  listen [::]:80;
  server_name ${server_names};
EOF
        fi
        cat <<EOF
  add_header X-Content-Type-Options "nosniff" always;
  add_header Referer-Policy "strict-origin-when-cross-origin" always;
  limit_req_status 429;

  client_max_body_size 60m;
  client_body_buffer_size 1m;
  gzip off;

  proxy_http_version 1.1;
  proxy_set_header Connection "";
  proxy_set_header Host \$host;
  proxy_set_header X-Real-IP \$remote_addr;
  proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto \$scheme;
  proxy_set_header X-Forwarded-Host \$host;
  proxy_connect_timeout 30s;
  proxy_send_timeout 600s;
  proxy_read_timeout 600s;

  location /events {
    limit_req zone=${zone_events} burst=10 nodelay;
    proxy_pass http://${upstream_name};
    proxy_buffering off;
    proxy_cache off;
    proxy_request_buffering off;
    proxy_read_timeout 600s;
    add_header X-Accel-Buffering no always;
    chunked_transfer_encoding on;
  }

  location /api/ {
    limit_req zone=${zone_api} burst=30 nodelay;
    proxy_pass http://${upstream_name};
    proxy_buffering off;
    proxy_cache off;
    proxy_request_buffering off;
    proxy_read_timeout 600s;
  }

  location / {
    proxy_pass http://${upstream_name};
    proxy_buffering on;
  }
}
EOF
    } > "${out}"
}

write_sub2api_nginx_config() {
    local out="$1"
    local server_names="$2"
    local upstream="$3"
    local tls_mode="$4"
    local cert_file="$5"
    local key_file="$6"
    local listen_port="$7"
    local include_http_redirect="$8"

    {
        cat <<'EOF'
# Managed by scripts/lumenctl.sh.
# sub2api reverse proxy. Supports long image requests and streaming endpoints.

EOF
        if [ "${include_http_redirect}" = "1" ] && [ "${tls_mode}" = "1" ]; then
            cat <<EOF
server {
  listen 80;
  listen [::]:80;
  server_name ${server_names};
  return 301 https://\$host\$request_uri;
}

EOF
        fi
        if [ "${tls_mode}" = "1" ]; then
            cat <<EOF
server {
  listen ${listen_port} ssl;
  listen [::]:${listen_port} ssl;
  http2 on;
  server_name ${server_names};

  ssl_certificate ${cert_file};
  ssl_certificate_key ${key_file};
  ssl_protocols TLSv1.2 TLSv1.3;
EOF
        else
            cat <<EOF
server {
  listen ${listen_port};
  listen [::]:${listen_port};
  server_name ${server_names};
EOF
        fi
        cat <<EOF
  client_max_body_size 100M;
  client_body_buffer_size 1m;
  gzip off;

  proxy_http_version 1.1;
  proxy_set_header Connection "";
  proxy_set_header Host \$host;
  proxy_set_header X-Real-IP \$remote_addr;
  proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto \$scheme;
  proxy_set_header X-Forwarded-Host \$host;
  proxy_connect_timeout 30s;
  proxy_send_timeout 1800s;
  proxy_read_timeout 1800s;

  location / {
    proxy_pass ${upstream};
    proxy_buffering off;
    proxy_cache off;
    proxy_request_buffering off;
    add_header X-Accel-Buffering no always;
  }
}
EOF
    } > "${out}"
}

write_sub2api_outer_nginx_config() {
    local out="$1"
    local server_names="$2"
    local inner_base="$3"
    local tls_mode="$4"
    local cert_file="$5"
    local key_file="$6"

    {
        cat <<'EOF'
# Managed by scripts/lumenctl.sh.
# External/public sub2api reverse proxy. Upstream can be another machine's nginx.

EOF
        if [ "${tls_mode}" = "1" ]; then
            cat <<EOF
server {
  listen 80;
  listen [::]:80;
  server_name ${server_names};
  return 301 https://\$host\$request_uri;
}

server {
  listen 443 ssl;
  listen [::]:443 ssl;
  http2 on;
  server_name ${server_names};

  ssl_certificate ${cert_file};
  ssl_certificate_key ${key_file};
  ssl_protocols TLSv1.2 TLSv1.3;
EOF
        else
            cat <<EOF
server {
  listen 80;
  listen [::]:80;
  server_name ${server_names};
EOF
        fi
        cat <<EOF
  client_max_body_size 100M;
  client_body_buffer_size 1m;
  gzip off;

  proxy_http_version 1.1;
  proxy_set_header Connection "";
  proxy_set_header Host \$host;
  proxy_set_header X-Real-IP \$remote_addr;
  proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto \$scheme;
  proxy_set_header X-Forwarded-Host \$host;
  proxy_connect_timeout 30s;
  proxy_send_timeout 1800s;
  proxy_read_timeout 1800s;

  location / {
    proxy_pass ${inner_base};
    proxy_buffering off;
    proxy_cache off;
    proxy_request_buffering off;
    add_header X-Accel-Buffering no always;
  }
}
EOF
    } > "${out}"
}

ask_tls_mode() {
    local reply
    reply="$(read_or_default '这个域名是否由本机 nginx 直接终止 HTTPS？(Y/n)' 'y')"
    case "${reply}" in
        y|Y|yes|YES|Yes) printf '1' ;;
        *) printf '0' ;;
    esac
}

nginx_lumen_proxy() {
    require_sudo
    ensure_cmd nginx "请先安装 nginx"

    local server_names web_upstream tls_mode cert_file key_file target_file tmp_file http_redirect
    log_step "生成/更新 Lumen 反代配置"
    server_names="$(read_or_default 'Lumen 域名 server_name（可多个，空格分隔）' 'lumen.example.com')"
    web_upstream="$(read_or_default 'Lumen Web 上游（Next.js）' '127.0.0.1:3000')"
    validate_domain_list "Lumen 域名" "${server_names}" || exit 1
    validate_host_port_target "Lumen Web 上游" "${web_upstream}" || exit 1

    tls_mode="$(ask_tls_mode)"
    cert_file=""
    key_file=""
    http_redirect=0
    if [ "${tls_mode}" = "1" ]; then
        http_redirect=1
        cert_file="$(read_or_default 'ssl_certificate' "/etc/letsencrypt/live/$(printf '%s' "${server_names}" | awk '{print $1}')/fullchain.pem")"
        key_file="$(read_or_default 'ssl_certificate_key' "/etc/letsencrypt/live/$(printf '%s' "${server_names}" | awk '{print $1}')/privkey.pem")"
        validate_absolute_path "ssl_certificate" "${cert_file}" || exit 1
        validate_absolute_path "ssl_certificate_key" "${key_file}" || exit 1
    fi

    target_file="$(read_or_default '写入 nginx 配置文件' "$(default_nginx_output_file lumen "${server_names}")")"
    validate_absolute_path "nginx 配置文件" "${target_file}" || exit 1

    tmp_file="$(mktemp)"
    write_lumen_nginx_config "${tmp_file}" "${server_names}" "${web_upstream}" "${http_redirect}" "${tls_mode}" "${cert_file}" "${key_file}"
    install_nginx_config_file "${tmp_file}" "${target_file}"
}

nginx_sub2api_proxy() {
    require_sudo
    ensure_cmd nginx "请先安装 nginx"

    local server_names upstream tls_mode cert_file key_file target_file tmp_file listen_port
    log_step "生成/更新 sub2api 单机公网反代配置"
    server_names="$(read_or_default 'sub2api 公网域名 server_name' 'api.example.com')"
    upstream="$(strip_trailing_slash "$(read_or_default 'sub2api 上游地址' 'http://127.0.0.1:8081')")"
    validate_domain_list "sub2api 公网域名" "${server_names}" || exit 1
    validate_url_like "sub2api 上游地址" "${upstream}" || exit 1
    tls_mode="$(ask_tls_mode)"
    listen_port=80
    cert_file=""
    key_file=""
    if [ "${tls_mode}" = "1" ]; then
        listen_port=443
        cert_file="$(read_or_default 'ssl_certificate' "/etc/letsencrypt/live/$(printf '%s' "${server_names}" | awk '{print $1}')/fullchain.pem")"
        key_file="$(read_or_default 'ssl_certificate_key' "/etc/letsencrypt/live/$(printf '%s' "${server_names}" | awk '{print $1}')/privkey.pem")"
        validate_absolute_path "ssl_certificate" "${cert_file}" || exit 1
        validate_absolute_path "ssl_certificate_key" "${key_file}" || exit 1
    fi
    target_file="$(read_or_default '写入 nginx 配置文件' "$(default_nginx_output_file sub2api "${server_names}")")"
    validate_absolute_path "nginx 配置文件" "${target_file}" || exit 1

    tmp_file="$(mktemp)"
    write_sub2api_nginx_config "${tmp_file}" "${server_names}" "${upstream}" "${tls_mode}" "${cert_file}" "${key_file}" "${listen_port}" "1"
    install_nginx_config_file "${tmp_file}" "${target_file}"
}

nginx_sub2api_inner_proxy() {
    require_sudo
    ensure_cmd nginx "请先安装 nginx"

    local server_names upstream listen_port target_file tmp_file
    log_step "生成/更新 sub2api 内层反代配置"
    log_info "适用于 sub2api 所在机器本身也有 nginx，外部机器/公网域名再转发到这里的场景。"
    server_names="$(read_or_default '内层 server_name（可用 _）' '_')"
    listen_port="$(read_or_default '内层监听端口' '18081')"
    upstream="$(strip_trailing_slash "$(read_or_default '本机 sub2api 上游地址' 'http://127.0.0.1:8081')")"
    validate_domain_list "内层 server_name" "${server_names}" || exit 1
    validate_tcp_port "内层监听端口" "${listen_port}" || exit 1
    validate_url_like "本机 sub2api 上游地址" "${upstream}" || exit 1
    target_file="$(read_or_default '写入 nginx 配置文件' "$(default_nginx_output_file sub2api-inner "${server_names}")")"
    validate_absolute_path "nginx 配置文件" "${target_file}" || exit 1

    tmp_file="$(mktemp)"
    write_sub2api_nginx_config "${tmp_file}" "${server_names}" "${upstream}" "0" "" "" "${listen_port}" "0"
    install_nginx_config_file "${tmp_file}" "${target_file}"
}

nginx_sub2api_outer_proxy() {
    require_sudo
    ensure_cmd nginx "请先安装 nginx"

    local server_names inner_base tls_mode cert_file key_file target_file tmp_file
    log_step "生成/更新 sub2api 外层公网反代配置"
    log_info "适用于公网域名在另一台机器上，该机器再 proxy 到 sub2api 内层 nginx。"
    server_names="$(read_or_default 'sub2api 公网域名 server_name' 'api.example.com')"
    inner_base="$(strip_trailing_slash "$(read_or_default '内层 nginx 地址' 'http://10.0.0.2:18081')")"
    validate_domain_list "sub2api 公网域名" "${server_names}" || exit 1
    validate_url_like "内层 nginx 地址" "${inner_base}" || exit 1
    tls_mode="$(ask_tls_mode)"
    cert_file=""
    key_file=""
    if [ "${tls_mode}" = "1" ]; then
        cert_file="$(read_or_default 'ssl_certificate' "/etc/letsencrypt/live/$(printf '%s' "${server_names}" | awk '{print $1}')/fullchain.pem")"
        key_file="$(read_or_default 'ssl_certificate_key' "/etc/letsencrypt/live/$(printf '%s' "${server_names}" | awk '{print $1}')/privkey.pem")"
        validate_absolute_path "ssl_certificate" "${cert_file}" || exit 1
        validate_absolute_path "ssl_certificate_key" "${key_file}" || exit 1
    fi
    target_file="$(read_or_default '写入 nginx 配置文件' "$(default_nginx_output_file sub2api-outer "${server_names}")")"
    validate_absolute_path "nginx 配置文件" "${target_file}" || exit 1

    tmp_file="$(mktemp)"
    write_sub2api_outer_nginx_config "${tmp_file}" "${server_names}" "${inner_base}" "${tls_mode}" "${cert_file}" "${key_file}"
    install_nginx_config_file "${tmp_file}" "${target_file}"
}

show_nginx_server_blocks() {
    local file="$1"
    as_sudo python3 - "${file}" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8", errors="replace")

def matching_brace(source: str, start: int) -> int:
    depth = 0
    quote = None
    comment = False
    escaped = False
    for i in range(start, len(source)):
        ch = source[i]
        if comment:
            if ch == "\n":
                comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch == "#":
            comment = True
        elif ch in {"'", '"'}:
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1

blocks = []
for m in re.finditer(r"(?m)^[ \t]*server[ \t]*\{", text):
    brace = text.find("{", m.start())
    end = matching_brace(text, brace)
    if end == -1:
        continue
    body = text[brace + 1 : end]
    listens = " ".join(re.findall(r"(?m)^\s*listen\s+([^;]+);", body)) or "unknown"
    names = " ".join(re.findall(r"(?m)^\s*server_name\s+([^;]+);", body)) or "unknown"
    flags = []
    for route in ("/v1/image-jobs", "/v1/refs", "/images/temp/", "/refs/"):
        flags.append(f"{route}={'yes' if route in body else 'no'}")
    blocks.append((listens, names, " ".join(flags)))

if not blocks:
    print("      未识别到 server { } block。")
else:
    print("      server blocks:")
    for idx, (listens, names, flags) in enumerate(blocks, 1):
        print(f"        [{idx}] listen={listens} server_name={names} {flags}")
PY
}

optimize_nginx_file() {
    local target_file="$1"
    local upstream_base="$2"
    local data_dir="$3"
    local server_filter="$4"
    local tmp_file
    tmp_file="$(mktemp)"
    as_sudo cat "${target_file}" > "${tmp_file}"

    python3 - "${tmp_file}" "${upstream_base}" "${data_dir}" "${server_filter}" <<'PY' >&2
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
upstream_base = sys.argv[2].rstrip("/")
data_dir = sys.argv[3].rstrip("/")
server_filter = sys.argv[4].strip()
text = path.read_text(encoding="utf-8", errors="replace")

def matching_brace(source: str, start: int) -> int:
    depth = 0
    quote = None
    comment = False
    escaped = False
    for i in range(start, len(source)):
        ch = source[i]
        if comment:
            if ch == "\n":
                comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch == "#":
            comment = True
        elif ch in {"'", '"'}:
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1

def location_exists(body: str, route: str) -> bool:
    pattern = r"location\s+(?:=|\^~|~\*?|@)?\s*" + re.escape(route)
    return re.search(pattern, body) is not None

sections = [
    (
        "/v1/image-jobs",
        f"""
    location ^~ /v1/image-jobs {{
      client_max_body_size 100M;
      proxy_buffering off;
      proxy_request_buffering off;
      proxy_pass {upstream_base};
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_connect_timeout 30s;
      proxy_send_timeout 1800s;
      proxy_read_timeout 1800s;
    }}
""",
    ),
    (
        "/v1/refs",
        f"""
    location ^~ /v1/refs {{
      client_max_body_size 100M;
      proxy_buffering off;
      proxy_request_buffering off;
      proxy_pass {upstream_base};
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_connect_timeout 30s;
      proxy_send_timeout 600s;
      proxy_read_timeout 600s;
    }}
""",
    ),
    (
        "/images/temp/",
        f"""
    location ^~ /images/temp/ {{
      alias {data_dir}/images/temp/;
      try_files $uri =404;
      expires 1d;
      add_header Cache-Control "public, max-age=86400" always;
    }}
""",
    ),
    (
        "/refs/",
        f"""
    location ^~ /refs/ {{
      alias {data_dir}/refs/;
      try_files $uri =404;
      expires 1d;
      add_header Cache-Control "public, max-age=86400" always;
    }}
""",
    ),
]

blocks = []
for m in re.finditer(r"(?m)^[ \t]*server[ \t]*\{", text):
    brace = text.find("{", m.start())
    end = matching_brace(text, brace)
    if end == -1:
        continue
    body = text[brace + 1 : end]
    listens = " ".join(re.findall(r"(?m)^\s*listen\s+([^;]+);", body))
    names = " ".join(re.findall(r"(?m)^\s*server_name\s+([^;]+);", body))
    blocks.append(
        {
            "start": m.start(),
            "brace": brace,
            "end": end,
            "body": body,
            "listens": listens,
            "names": names,
        }
    )

if not blocks:
    raise SystemExit("未识别到 server { } block，无法自动优化。")

filtered = [
    b
    for b in blocks
    if not server_filter or server_filter in b["names"] or server_filter in b["body"]
]
if not filtered:
    raise SystemExit(f"未找到匹配过滤条件的 server block：{server_filter}")

https_blocks = [
    b
    for b in filtered
    if re.search(r"(^|\s)(443|ssl|http2)(\s|$)", b["listens"], re.IGNORECASE)
]
targets = https_blocks or filtered

changes = []
new_text = text
for block in reversed(targets):
    body = new_text[block["brace"] + 1 : block["end"]]
    missing = [section for route, section in sections if not location_exists(body, route)]
    if not missing:
        continue
    insert = (
        "\n"
        "    # Lumen image-job locations (managed by scripts/lumenctl.sh)\n"
        + "".join(missing)
        + "    # End Lumen image-job locations\n"
    )
    new_text = new_text[: block["end"]] + insert + new_text[block["end"] :]
    names = block["names"] or "unknown"
    listens = block["listens"] or "unknown"
    changes.append(f"server_name={names} listen={listens} added={len(missing)}")

path.write_text(new_text, encoding="utf-8")
if changes:
    print("\n".join(reversed(changes)))
else:
    print("已包含 image-job 所需 location，无需修改。")
PY

    printf '%s' "${tmp_file}"
}

nginx_image_job_locations() {
    require_sudo
    ensure_cmd nginx "请先安装 nginx"
    ensure_cmd python3 "请安装 Python 3"

    log_step "检查当前 nginx 配置"
    if ! as_sudo nginx -t; then
        log_error "当前 nginx 配置未通过 nginx -t。请先修复现有配置，再执行自动优化。"
        exit 1
    fi

    nginx_scan
    if [ "${#NGINX_FILES[@]}" -eq 0 ]; then
        log_warn "没有可优化的 nginx 配置文件，已跳过 image-job 路由注入。"
        return 0
    fi

    local choice target_file upstream_base data_dir server_filter tmp_file backup timestamp
    choice="$(read_or_default '选择要优化的配置编号' '1')"
    if [[ ! "${choice}" =~ ^[0-9]+$ ]] || [ "${choice}" -lt 1 ] || [ "${choice}" -gt "${#NGINX_FILES[@]}" ]; then
        log_error "无效配置编号：${choice}"
        exit 1
    fi
    target_file="${NGINX_FILES[$((choice - 1))]}"

    log_step "目标 nginx 配置：${target_file}"
    show_nginx_server_blocks "${target_file}"

    server_filter="$(read_or_default 'server_name 过滤（留空=该文件内 HTTPS server）' '')"
    upstream_base="$(strip_trailing_slash "$(read_or_default 'image-job 本机代理地址' 'http://127.0.0.1:8091')")"
    data_dir="$(strip_trailing_slash "$(read_or_default 'image-job 数据目录' '/opt/image-job/data')")"
    validate_nginx_token "server_name 过滤" "${server_filter}" || exit 1
    validate_url_like "image-job 本机代理地址" "${upstream_base}" || exit 1
    validate_absolute_path "image-job 数据目录" "${data_dir}" || exit 1

    timestamp="$(date '+%Y%m%d%H%M%S')"
    backup="$(nginx_backup_path "${target_file}" "${timestamp}")"
    as_sudo cp -p "${target_file}" "${backup}"
    log_info "已备份：${backup}"

    tmp_file="$(optimize_nginx_file "${target_file}" "${upstream_base}" "${data_dir}" "${server_filter}")"
    if as_sudo cmp -s "${target_file}" "${tmp_file}"; then
        rm -f "${tmp_file}"
        log_info "目标配置已经包含 image-job 路由，无需修改。"
        return 0
    fi

    as_sudo cp "${tmp_file}" "${target_file}"
    rm -f "${tmp_file}"

    log_step "验证优化后的 nginx 配置"
    if ! as_sudo nginx -t; then
        log_error "nginx -t 未通过，正在回滚 ${target_file}"
        as_sudo cp -p "${backup}" "${target_file}"
        as_sudo nginx -t || true
        exit 1
    fi

    if confirm "nginx -t 已通过，是否 reload nginx？"; then
        if command -v systemctl >/dev/null 2>&1; then
            as_sudo systemctl reload nginx
        else
            as_sudo nginx -s reload
        fi
        log_info "nginx 已 reload。"
    else
        log_info "已修改配置但未 reload。需要生效时执行：sudo systemctl reload nginx"
    fi
}

nginx_optimize() {
    while :; do
        cat <<EOF

nginx 反代优化向导

  1) Lumen 反代（Web / /api / /events）
  2) sub2api 单机公网反代（域名直接到本机 sub2api）
  3) sub2api 内层反代（sub2api 机器本机 nginx -> 127.0.0.1:8081）
  4) sub2api 外层公网反代（公网机器 nginx -> 另一台机器内层 nginx）
  5) 给已有站点注入 image-job 路由
  6) 扫描 nginx 配置
  0) 返回

EOF
        local choice
        choice="$(read_or_default '请选择 nginx 优化项' '0')"
        case "${choice}" in
            1) nginx_lumen_proxy ;;
            2) nginx_sub2api_proxy ;;
            3) nginx_sub2api_inner_proxy ;;
            4) nginx_sub2api_outer_proxy ;;
            5) nginx_image_job_locations ;;
            6) nginx_scan || true ;;
            0) return 0 ;;
            *) log_warn "无效选项：${choice}" ;;
        esac
    done
}

show_menu() {
    while :; do
        cat <<EOF

Lumen 一键运维菜单

  1) 安装 Lumen
  2) 更新 Lumen
  3) 卸载 Lumen
  4) 安装 image-job
  5) 卸载 image-job
  6) 扫描 nginx 配置
  7) nginx 反代优化向导
  0) 退出

EOF
        local choice
        choice="$(read_or_default '请选择' '0')"
        case "${choice}" in
            1) run_lumen_script install.sh ;;
            2) run_lumen_script update.sh ;;
            3) run_lumen_script uninstall.sh ;;
            4) install_image_job ;;
            5) uninstall_image_job ;;
            6) nginx_scan || true ;;
            7) nginx_optimize ;;
            0) exit 0 ;;
            *) log_warn "无效选项：${choice}" ;;
        esac
    done
}

main() {
    local command="${1:-menu}"
    case "${command}" in
        menu) show_menu ;;
        install-lumen) run_lumen_script install.sh ;;
        update-lumen) run_lumen_script update.sh ;;
        uninstall-lumen) run_lumen_script uninstall.sh ;;
        install-image-job) install_image_job ;;
        uninstall-image-job) uninstall_image_job ;;
        nginx-scan) nginx_scan ;;
        nginx-optimize) nginx_optimize ;;
        nginx-lumen) nginx_lumen_proxy ;;
        nginx-sub2api) nginx_sub2api_proxy ;;
        nginx-sub2api-inner) nginx_sub2api_inner_proxy ;;
        nginx-sub2api-outer) nginx_sub2api_outer_proxy ;;
        nginx-image-job) nginx_image_job_locations ;;
        help|-h|--help) usage ;;
        *)
            usage
            log_error "未知命令：${command}"
            exit 1
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
