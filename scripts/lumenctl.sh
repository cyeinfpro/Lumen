#!/usr/bin/env bash
# Lumen 统一运维入口（Docker compose 全栈版）。
# 用法：
#   bash scripts/lumenctl.sh                  # 交互菜单
#   bash scripts/lumenctl.sh install-lumen    # 安装（透传给 install.sh）
#   bash scripts/lumenctl.sh update-lumen     # 更新（透传给 update.sh）
#   bash scripts/lumenctl.sh status           # docker compose ps + healthz
#   bash scripts/lumenctl.sh logs api         # 跟随 api 日志
#   bash scripts/lumenctl.sh nginx-optimize   # nginx 反代向导

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

ROOT="$(lumen_resolve_repo_root "${SCRIPT_DIR}")"
NGINX_FILES=()
LUMEN_USE_SUDO="${LUMEN_USE_SUDO:-0}"
LUMEN_DEPLOY_ROOT="${LUMEN_DEPLOY_ROOT:-/opt/lumen}"
LUMEN_COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-lumen}"
export COMPOSE_PROJECT_NAME="${LUMEN_COMPOSE_PROJECT}"

trap 'log_error "lumenctl 失败：第 ${LINENO} 行返回非零状态。请查看上方输出修正后重试。"' ERR

usage() {
    cat <<EOF
Lumen 一键运维菜单

用法：
  bash scripts/lumenctl.sh [command] [args...]

Lifecycle commands:
  menu                 打开交互菜单（默认）
  install-lumen        安装 Lumen（调用 scripts/install.sh）
  update-lumen         更新 Lumen（调用 scripts/update.sh）
  uninstall-lumen      卸载 Lumen（调用 scripts/uninstall.sh）
  rollback             回滚到 previous release（pull 旧 tag + compose up）
  version              输出 VERSION + 镜像 tag + git sha
  bootstrap-scripts    应急：从 GitHub main 强制热替换 shell 脚本与 Python runners/guard
                       （平时入口处自动 self-update，TTL=600s；本命令突破 TTL）

Docker compose runtime:
  status               docker compose ps + 健康检查
  logs [service]       跟随 service 日志（默认 api，等价 docker compose logs -f）
  start                up -d --wait api worker web
  stop                 stop api worker web tgbot
  restart              up -d --force-recreate api worker web
  migrate              compose --profile migrate run --rm migrate
  bootstrap            创建初始 admin（需 LUMEN_ADMIN_EMAIL / LUMEN_ADMIN_PASSWORD）
  migrate-env          dry-run 检查旧 .env 的容器内 URL
  migrate-env-apply    按白名单迁移旧 .env 的容器内 URL，并写 .bak
  backup               调用 scripts/backup.sh
  restore <ts>         调用 scripts/restore.sh <timestamp>

Auxiliary:
  install-storage-units 安装存储后端组件（lumen-storage-mount + 4 个 systemd unit）
                       对应管理后台「存储后端」页（local / smb 切换）
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
    if ! lumen_require_python_min_version \
            "${python_bin}" "${min_major}" "${min_minor}"; then
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

lumenctl_resolve_script() {
    local script_name="$1"
    if [ -f "${SCRIPT_DIR}/${script_name}" ]; then
        printf '%s' "${SCRIPT_DIR}/${script_name}"
        return 0
    fi
    if [ -f "${ROOT}/current/scripts/${script_name}" ]; then
        printf '%s' "${ROOT}/current/scripts/${script_name}"
        return 0
    fi
    if [ -f "${LUMEN_DEPLOY_ROOT}/current/scripts/${script_name}" ]; then
        printf '%s' "${LUMEN_DEPLOY_ROOT}/current/scripts/${script_name}"
        return 0
    fi
    if [ -f "${ROOT}/scripts/${script_name}" ]; then
        printf '%s' "${ROOT}/scripts/${script_name}"
        return 0
    fi
    return 1
}

lumenctl_raw_script_url() {
    local script_name="$1"
    local branch="${LUMEN_BRANCH:-main}"
    local raw_base="${LUMEN_RAW_BASE:-https://raw.githubusercontent.com/cyeinfpro/Lumen/${branch}}"
    printf '%s/scripts/%s' "${raw_base%/}" "${script_name}"
}

lumenctl_bootstrap_install_from_github() {
    local raw_url tmp_script install_dir
    raw_url="$(lumenctl_raw_script_url install.sh)"
    install_dir="${LUMEN_INSTALL_DIR:-${ROOT}}"

    ensure_cmd curl "当前目录不是完整 Lumen 仓库，且缺少 curl，无法从 GitHub 拉取 install.sh"
    tmp_script="$(mktemp)" || {
        log_error "无法创建临时文件，不能从 GitHub bootstrap 安装。"
        exit 1
    }

    log_warn "当前目录缺少 scripts/install.sh，将从 GitHub bootstrap 完整仓库。"
    log_info "GitHub raw：${raw_url}"
    log_info "目标目录：${install_dir}"
    if ! curl -fsSL "${raw_url}" -o "${tmp_script}"; then
        rm -f "${tmp_script}"
        log_error "无法从 GitHub 下载 install.sh：${raw_url}"
        log_error "可手动执行：git clone ${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git} ${install_dir}"
        exit 1
    fi

    export LUMEN_INSTALL_DIR="${install_dir}"
    export LUMEN_REPO_URL="${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git}"
    export LUMEN_BRANCH="${LUMEN_BRANCH:-main}"

    local rc=0
    bash "${tmp_script}" --install "$@" || rc=$?
    rm -f "${tmp_script}"
    return "${rc}"
}

run_lumen_script() {
    local script_name="$1"
    shift || true
    local script_path=""
    log_step "执行 ${script_name}"
    if ! script_path="$(lumenctl_resolve_script "${script_name}")"; then
        if [ "${script_name}" = "install.sh" ]; then
            lumenctl_bootstrap_install_from_github "$@"
            return $?
        fi
        log_error "找不到脚本：${ROOT}/current/scripts/${script_name} 或 ${ROOT}/scripts/${script_name}"
        log_error "如果这是新机器，请先从 GitHub 拉完整仓库：git clone ${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git} ${ROOT}"
        exit 1
    fi
    # 全栈 docker 化后 install.sh / update.sh / uninstall.sh 都接受透传 flag。
    # 不再强制塞 --install；让上游传什么就传什么。
    case "${script_name}" in
        install.sh|update.sh|uninstall.sh|backup.sh|restore.sh)
            if [ "$(detect_os)" = "linux" ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
                ensure_cmd sudo "请安装 sudo，或切换到 root 后重试"
                # sudo 默认 env_reset 会把 LUMEN_UPDATE_GIT_PULL 等 inline env vars 全部 strip。
                # 用 env KEY=val 显式重建，确保 update.sh / install.sh 能读到调用方的 LUMEN_*。
                local env_args=()
                local _v
                while IFS= read -r _v; do
                    [ -n "${_v}" ] || continue
                    env_args+=("${_v}=${!_v}")
                done < <(compgen -e 2>/dev/null | grep '^LUMEN_' || true)
                if [ "${#env_args[@]}" -gt 0 ]; then
                    lumen_sudo env "${env_args[@]}" bash "${script_path}" "$@"
                else
                    lumen_sudo bash "${script_path}" "$@"
                fi
            else
                bash "${script_path}" "$@"
            fi
            ;;
        *)
            bash "${script_path}" "$@"
            ;;
    esac
}

run_lumen_install_script() {
    case "${1:-}" in
        install|--install)
            run_lumen_script install.sh "$@"
            ;;
        *)
            run_lumen_script install.sh --install "$@"
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Docker compose helpers（cutover plan §17 / §24）
# lib.sh 已提供 lumen_compose / lumen_compose_in（注入 COMPOSE_PROJECT_NAME=lumen，§11.4）。
# 这里只负责定位 docker-compose.yml 所在工作目录：
#   优先 ${ROOT}/current（release 布局），其次 ${LUMEN_DEPLOY_ROOT}/current，最后 ROOT 本身。
# ---------------------------------------------------------------------------
lumenctl_compose_workdir() {
    if [ -d "${ROOT}/current" ]; then
        printf '%s' "${ROOT}/current"
        return 0
    fi
    if [ -d "${LUMEN_DEPLOY_ROOT}/current" ]; then
        printf '%s' "${LUMEN_DEPLOY_ROOT}/current"
        return 0
    fi
    if [ -f "${ROOT}/docker-compose.yml" ]; then
        printf '%s' "${ROOT}"
        return 0
    fi
    if [ -f "${LUMEN_DEPLOY_ROOT}/docker-compose.yml" ]; then
        printf '%s' "${LUMEN_DEPLOY_ROOT}"
        return 0
    fi
    return 1
}

lumenctl_compose() {
    local workdir
    if ! workdir="$(lumenctl_compose_workdir)"; then
        log_error "找不到 docker-compose.yml；预期位置：${ROOT}/current 或 ${LUMEN_DEPLOY_ROOT}/current"
        return 1
    fi
    lumen_compose_in "${workdir}" "$@"
}

lumen_compose_status() {
    lumen_require_docker_access
    log_step "docker compose ps（project=${LUMEN_COMPOSE_PROJECT}）"
    lumenctl_compose ps || true
    printf '\n---\n'
    log_step "容器健康状态"
    local cn state
    for cn in lumen-api lumen-worker lumen-web lumen-pg lumen-redis lumen-tgbot; do
        state="$(lumen_docker inspect --format '{{.Name}} {{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "${cn}" 2>/dev/null || true)"
        if [ -n "${state}" ]; then
            printf '  %s\n' "${state#/}"
        fi
    done
    printf '\n---\n'
    log_step "本地健康检查"
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS --noproxy '*' --max-time 8 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
            log_info "API healthz: OK"
        else
            log_warn "API healthz: 失败 (http://127.0.0.1:8000/healthz)"
        fi
        if curl -fsS --noproxy '*' --max-time 8 -o /dev/null http://127.0.0.1:3000/ >/dev/null 2>&1; then
            log_info "Web /: OK"
        else
            log_warn "Web /: 失败 (http://127.0.0.1:3000/)"
        fi
    else
        log_warn "未安装 curl，跳过 HTTP 健康检查。"
    fi
}

_LUMENCTL_VALID_SERVICES="api worker web tgbot postgres redis migrate bootstrap"

lumen_compose_logs() {
    lumen_require_docker_access
    local service="${1:-api}"
    # 校验 service 名，避免用户敲错（例如 lumen-api 而非 api）后看到困惑的
    # docker compose 错误。
    case " ${_LUMENCTL_VALID_SERVICES} " in
        *" ${service} "*) ;;
        *)
            log_error "无效服务名：'${service}'"
            log_error "  可用：${_LUMENCTL_VALID_SERVICES}"
            log_error "  注意：docker 容器名是 'lumen-api' 等，但 logs 命令的参数是 service 名（不带 lumen- 前缀）。"
            exit 1
            ;;
    esac
    log_step "docker compose logs -f --tail=200 ${service}"
    lumenctl_compose logs -f --tail=200 "${service}"
}

lumen_compose_restart() {
    lumen_require_docker_access
    log_step "docker compose up -d --force-recreate api worker web"
    lumenctl_compose up -d --force-recreate api worker web
}

lumen_compose_stop() {
    lumen_require_docker_access
    log_step "docker compose stop api worker web tgbot"
    # tgbot 走 profile，stop 时不在默认范围；显式指定即可，未运行也是 noop。
    lumenctl_compose stop api worker web tgbot || true
}

lumen_compose_start() {
    lumen_require_docker_access
    log_step "docker compose up -d --wait api worker web"
    lumenctl_compose up -d --wait api worker web
}

lumen_compose_migrate() {
    lumen_require_docker_access
    log_step "docker compose --profile migrate run --rm migrate"
    lumenctl_compose --profile migrate run --rm migrate
}

lumen_compose_bootstrap() {
    lumen_require_docker_access
    if [ -z "${LUMEN_ADMIN_EMAIL:-}" ] || [ -z "${LUMEN_ADMIN_PASSWORD:-}" ]; then
        log_error "bootstrap 需要 LUMEN_ADMIN_EMAIL 与 LUMEN_ADMIN_PASSWORD 环境变量。"
        log_error "示例：LUMEN_ADMIN_EMAIL=admin@example.com LUMEN_ADMIN_PASSWORD='...' bash scripts/lumenctl.sh bootstrap"
        exit 1
    fi
    log_step "docker compose --profile bootstrap run --rm bootstrap"
    lumenctl_compose --profile bootstrap run --rm \
        -e LUMEN_ADMIN_EMAIL="${LUMEN_ADMIN_EMAIL}" \
        -e LUMEN_ADMIN_PASSWORD="${LUMEN_ADMIN_PASSWORD}" \
        bootstrap python -m app.scripts.bootstrap "${LUMEN_ADMIN_EMAIL}" --role admin --password "${LUMEN_ADMIN_PASSWORD}"
}

lumen_env_migrate_file() {
    local mode="$1"
    local env_file="${2:-}"
    if [ -z "${env_file}" ]; then
        if [ -f "${ROOT}/shared/.env" ]; then
            env_file="${ROOT}/shared/.env"
        elif [ -f "${ROOT}/current/.env" ]; then
            env_file="${ROOT}/current/.env"
        elif [ -f "${LUMEN_DEPLOY_ROOT}/shared/.env" ]; then
            env_file="${LUMEN_DEPLOY_ROOT}/shared/.env"
        elif [ -f "${ROOT}/.env" ]; then
            env_file="${ROOT}/.env"
        else
            log_error "找不到 .env；请显式传入路径：bash scripts/lumenctl.sh migrate-env /path/to/.env"
            exit 1
        fi
    fi
    log_step "迁移容器内 URL (${mode})"
    lumen_migrate_container_urls "${env_file}" "${mode}"
}

lumen_compose_backup() {
    run_lumen_script backup.sh "$@"
}

lumen_compose_restore() {
    if [ "$#" -lt 1 ] || [ -z "${1:-}" ]; then
        log_error "restore 需要一个 timestamp 参数（形如 20260424-123000）。"
        log_error "用法：bash scripts/lumenctl.sh restore <timestamp>"
        exit 1
    fi
    # restore 是不可逆操作：DROP database + 覆盖 redis volume。LUMEN_NONINTERACTIVE=1
    # 或 LUMEN_RESTORE_YES=1 才跳过确认（自动化场景）；其余都要人工确认 timestamp。
    if [ "${LUMEN_NONINTERACTIVE:-}" != "1" ] && [ "${LUMEN_RESTORE_YES:-}" != "1" ]; then
        printf '\n'
        log_warn "restore $1 将："
        log_warn "  1) 停止 lumen-api / lumen-worker"
        log_warn "  2) DROP 现有数据库并从 backup/pg/$1.pg.dump.gz 恢复"
        log_warn "  3) 覆盖 redis volume 数据"
        log_warn "此操作不可逆，请确认 timestamp 正确。"
        if ! confirm "继续 restore $1？"; then
            log_info "已取消。"
            exit 0
        fi
    fi
    run_lumen_script restore.sh "$@"
}

# Rollback：持有维护锁 + update 锁，事务化切回 previous release。
_lumen_compose_rollback_locked() {
    local deploy_root="$1"
    if [ ! -L "${deploy_root}/previous" ]; then
        log_error "${deploy_root}/previous 不存在，无法自动 rollback。"
        return 1
    fi

    local current_target previous_target old_id old_dir old_tag old_version
    local shared_env="${deploy_root}/shared/.env"
    current_target="$(readlink "${deploy_root}/current" 2>/dev/null || true)"
    previous_target="$(readlink "${deploy_root}/previous" 2>/dev/null || true)"
    old_id="$(basename "${previous_target}")"
    old_dir="${deploy_root}/releases/${old_id}"
    if [ -z "${current_target}" ] || [ -z "${old_id}" ] || [ ! -d "${old_dir}" ]; then
        log_error "无法解析 rollback 的 current/previous release。"
        return 1
    fi
    if [ ! -f "${shared_env}" ]; then
        log_error "${shared_env} 不存在，无法保证 rollback 配置一致性。"
        return 1
    fi
    old_tag="$(head -n1 "${old_dir}/.image-tag" 2>/dev/null | tr -d '[:space:]' || true)"
    old_version="$(head -n1 "${old_dir}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
    if [ -z "${old_tag}" ] || [ -z "${old_version}" ]; then
        log_error "rollback 目标缺少 .image-tag 或 VERSION，拒绝产生源码/镜像/版本错位。"
        return 1
    fi

    if [ "${LUMEN_NONINTERACTIVE:-}" != "1" ] \
            && [ "${LUMEN_ROLLBACK_YES:-}" != "1" ]; then
        printf '\n'
        log_warn "rollback 将切换到 release ${old_id}（镜像 tag=${old_tag}, version=${old_version}），并重启 api/worker/web。"
        if ! confirm "继续 rollback？"; then
            log_info "已取消。"
            return 0
        fi
    fi

    local env_snapshot
    env_snapshot="$(mktemp "${deploy_root}/shared/.env.rollback.XXXXXX")" \
        || return 1
    if ! cp -p "${shared_env}" "${env_snapshot}"; then
        rm -f "${env_snapshot}" 2>/dev/null || true
        return 1
    fi

    local switched=0 rollback_rc=0
    log_step "rollback 到 release ${old_id}"
    log_info "rollback 目标镜像 tag：${old_tag}"
    log_info "rollback 目标版本：${old_version}"
    if ! lumen_set_image_tag_in_env "${shared_env}" "${old_tag}" \
            || ! lumen_set_env_value_in_file \
                "${shared_env}" LUMEN_VERSION "${old_version}"; then
        rollback_rc=1
    elif ! lumen_release_atomic_switch "${deploy_root}" "${old_id}"; then
        rollback_rc=1
    else
        switched=1
        if [ -f "${deploy_root}/current/VERSION" ]; then
            ln -sfn current/VERSION "${deploy_root}/VERSION" 2>/dev/null \
                || cp "${deploy_root}/current/VERSION" "${deploy_root}/VERSION"
        fi
        log_info "current 已切回 releases/${old_id}"
        log_step "docker compose pull"
        lumen_compose_in "${deploy_root}/current" pull \
            || log_warn "compose pull 返回非零，将继续 up 使用本地旧镜像兜底"
        log_step "docker compose up -d --wait api worker web"
        if ! lumen_compose_in "${deploy_root}/current" \
                up --pull missing -d --wait api worker web; then
            rollback_rc=1
        fi
    fi

    if [ "${rollback_rc}" -eq 0 ]; then
        rm -f "${env_snapshot}"
        return 0
    fi

    log_error "rollback 失败，恢复执行前的 env 与 symlink 状态。"
    local restore_tmp="${deploy_root}/shared/.env.restore.$$" restore_ok=1
    if ! cp -p "${env_snapshot}" "${restore_tmp}" \
            || ! mv -f "${restore_tmp}" "${shared_env}"; then
        rm -f "${restore_tmp}" 2>/dev/null || true
        restore_ok=0
        log_error "shared/.env 原字节恢复失败；快照保留在 ${env_snapshot}。"
    fi
    if [ "${switched}" -eq 1 ]; then
        lumen_atomic_replace_symlink \
            "${current_target}" "${deploy_root}/current" || restore_ok=0
        lumen_atomic_replace_symlink \
            "${previous_target}" "${deploy_root}/previous" || restore_ok=0
    fi
    if [ -f "${deploy_root}/current/VERSION" ]; then
        ln -sfn current/VERSION "${deploy_root}/VERSION" 2>/dev/null \
            || cp "${deploy_root}/current/VERSION" "${deploy_root}/VERSION"
    fi
    if [ "${restore_ok}" -eq 1 ]; then
        rm -f "${env_snapshot}"
        log_warn "rollback 前状态已恢复，尝试重新拉起原 release 核心服务。"
        lumen_compose_in "${deploy_root}/current" \
            up --pull missing -d --wait api worker web \
            || log_error "原 release 核心服务恢复失败，请人工检查 compose 日志。"
    fi
    return 1
}

lumen_compose_rollback() {
    if [ "$(detect_os)" = "linux" ] \
            && [ "${EUID:-$(id -u)}" -ne 0 ] \
            && [ "${LUMEN_ROLLBACK_PRIVILEGED:-0}" != "1" ]; then
        ensure_cmd sudo "rollback 需要 root 权限以持有维护锁并原子切换 release。"
        lumen_sudo env \
            LUMEN_ROLLBACK_PRIVILEGED=1 \
            LUMEN_LUMENCTL_SELF_UPDATE=0 \
            LUMEN_SELF_UPDATE=0 \
            LUMEN_NONINTERACTIVE="${LUMEN_NONINTERACTIVE:-0}" \
            LUMEN_ROLLBACK_YES="${LUMEN_ROLLBACK_YES:-0}" \
            LUMEN_DEPLOY_ROOT="${LUMEN_DEPLOY_ROOT}" \
            LUMEN_BACKUP_ROOT="${LUMEN_BACKUP_ROOT}" \
            COMPOSE_PROJECT_NAME="${LUMEN_COMPOSE_PROJECT}" \
            bash "${SCRIPT_DIR}/lumenctl.sh" rollback "$@"
        return $?
    fi

    lumen_require_docker_access
    local deploy_root
    if [ -L "${ROOT}/current" ]; then
        deploy_root="${ROOT}"
    elif [ -L "${LUMEN_DEPLOY_ROOT}/current" ]; then
        deploy_root="${LUMEN_DEPLOY_ROOT}"
    else
        log_error "找不到 release 布局的 current symlink；rollback 仅适用于 release 布局。"
        return 1
    fi

    lumen_acquire_lock "${deploy_root}" "lumenctl rollback"
    local operation_id rc=0
    operation_id="rollback-$(date -u +%Y%m%d-%H%M%S)-$$"
    lumen_with_lock "${operation_id}" 1830 \
        _lumen_compose_rollback_locked "${deploy_root}" || rc=$?
    lumen_release_lock
    return "${rc}"
}

lumen_compose_version() {
    log_step "Lumen 版本信息"
    local version_file=""
    if [ -f "${ROOT}/current/VERSION" ]; then
        version_file="${ROOT}/current/VERSION"
    elif [ -f "${ROOT}/VERSION" ]; then
        version_file="${ROOT}/VERSION"
    fi
    if [ -n "${version_file}" ]; then
        printf 'VERSION:        %s\n' "$(head -n1 "${version_file}" | tr -d '[:space:]')"
    else
        printf 'VERSION:        (unknown)\n'
    fi

    local env_file=""
    if [ -f "${ROOT}/current/.env" ]; then
        env_file="${ROOT}/current/.env"
    elif [ -f "${ROOT}/.env" ]; then
        env_file="${ROOT}/.env"
    elif [ -f "${LUMEN_DEPLOY_ROOT}/shared/.env" ]; then
        env_file="${LUMEN_DEPLOY_ROOT}/shared/.env"
    fi
    if [ -n "${env_file}" ]; then
        local tag
        tag="$(lumen_env_value LUMEN_IMAGE_TAG "${env_file}")"
        printf 'IMAGE_TAG:      %s\n' "${tag:-(default)}"
        local registry
        registry="$(lumen_env_value LUMEN_IMAGE_REGISTRY "${env_file}")"
        printf 'IMAGE_REGISTRY: %s\n' "${registry:-ghcr.io/cyeinfpro}"
    fi

    if command -v git >/dev/null 2>&1 && [ -d "${ROOT}/.git" ]; then
        printf 'GIT_SHA:        %s\n' "$(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    elif [ -f "${ROOT}/current/.lumen_release.json" ] && command -v python3 >/dev/null 2>&1; then
        printf 'GIT_SHA:        %s\n' "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("git_sha","unknown"))' "${ROOT}/current/.lumen_release.json" 2>/dev/null || echo unknown)"
    fi

    if [ -L "${ROOT}/current" ]; then
        printf 'CURRENT_LINK:   %s -> %s\n' "${ROOT}/current" "$(readlink "${ROOT}/current" 2>/dev/null || true)"
    fi
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
    as_sudo install -d -m 0755 "${app_dir}" "${data_dir}" "${data_dir}/images"
    as_sudo install -d -m 0755 "${data_dir}/images/temp" "${data_dir}/refs"
    as_sudo install -d -m 0700 "${state_dir}"
    as_sudo install -m 0644 "${ROOT}/image-job/app.py" "${app_dir}/app.py"
    as_sudo install -m 0644 "${ROOT}/image-job/image_artifacts.py" "${app_dir}/image_artifacts.py"
    as_sudo install -m 0644 "${ROOT}/image-job/image_url_security.py" "${app_dir}/image_url_security.py"
    as_sudo install -m 0644 "${ROOT}/image-job/job_persistence.py" "${app_dir}/job_persistence.py"
    as_sudo install -m 0644 "${ROOT}/image-job/payload_helpers.py" "${app_dir}/payload_helpers.py"
    as_sudo install -m 0644 "${ROOT}/image-job/request_bodies.py" "${app_dir}/request_bodies.py"
    as_sudo install -m 0644 "${ROOT}/image-job/runtime_config.py" "${app_dir}/runtime_config.py"
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
    as_sudo chmod 0700 "${state_dir}"

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

  client_max_body_size 80m;
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
  proxy_send_timeout 3600s;
  proxy_read_timeout 1800s;

  location /events {
    limit_req zone=${zone_events} burst=10 nodelay;
    proxy_pass http://${upstream_name};
    proxy_buffering off;
    proxy_cache off;
    proxy_request_buffering off;
    proxy_connect_timeout 30s;
    proxy_send_timeout 3600s;
    proxy_read_timeout 1800s;
    add_header X-Accel-Buffering no always;
    chunked_transfer_encoding on;
  }

  location /api/ {
    limit_req zone=${zone_api} burst=30 nodelay;
    proxy_pass http://${upstream_name};
    proxy_buffering off;
    proxy_cache off;
    proxy_request_buffering off;
    proxy_connect_timeout 30s;
    proxy_send_timeout 3600s;
    proxy_read_timeout 1800s;
  }

  location / {
    proxy_pass http://${upstream_name};
    proxy_connect_timeout 30s;
    proxy_send_timeout 3600s;
    proxy_read_timeout 1800s;
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

# 把任何菜单动作包成"失败也回菜单"的 action：
#   - 失败时 log_warn rc 并暂停等用户按 Enter（让他看清上方错误）
#   - 成功时直接返回，无暂停（避免 annoying）
#   - Ctrl+C / 中断（rc=130/143）等同失败处理
#   - 无论 rc 多少，本函数总返回 0，不让 set -e 把菜单进程也带退出
menu_action() {
    local rc=0
    "$@" || rc=$?
    if [ "${rc}" -ne 0 ]; then
        log_warn "命令以非零状态结束（rc=${rc}），返回主菜单。"
        if [ -r /dev/tty ]; then
            printf '\n按 Enter 返回菜单... ' >&2
            IFS= read -r _ </dev/tty 2>/dev/null || true
            printf '\n' >&2
        fi
    fi
    return 0
}

show_menu() {
    while :; do
        cat <<EOF

Lumen 一键运维菜单
（菜单分组：运行/维护/网络/⚠ 危险）

  ── 运行（read-only）──
  1) 查看运行状态（compose ps + 健康检查）
  2) 跟随 API 日志（compose logs -f api）
  3) 查看 Lumen 版本

  ── 维护（compose 操作）──
  4) 启动 api/worker/web（compose up -d --wait）
  5) 重启 api/worker/web（compose up -d --force-recreate）
  6) 停止 api/worker/web/tgbot（compose stop）
  7) 执行 DB migrate（compose --profile migrate run --rm migrate）
  8) 立即触发一次 backup（pg + redis）

  ── 网络（nginx）──
  9) 扫描 nginx 配置
  10) nginx 反代优化向导

  ── ⚠ 危险（影响数据/在线服务）──
  11) 安装 Lumen
  12) 更新 Lumen
  13) Restore from backup（drop DB + 覆盖 redis）
  14) Rollback 到上一版本
  15) 安装 image-job
  16) 卸载 image-job
  17) 卸载 Lumen

  0) 退出

EOF
        local choice
        choice="$(read_or_default '请选择' '0')"
        case "${choice}" in
            1)  menu_action lumen_compose_status ;;
            2)  menu_action lumen_compose_logs api ;;
            3)  menu_action lumen_compose_version ;;
            4)  menu_action lumen_compose_start ;;
            5)  menu_action lumen_compose_restart ;;
            6)  menu_action lumen_compose_stop ;;
            7)  menu_action lumen_compose_migrate ;;
            8)  menu_action run_lumen_script backup.sh ;;
            9)  menu_action nginx_scan ;;
            10) menu_action nginx_optimize ;;
            11) menu_action run_lumen_install_script ;;
            12) menu_action run_lumen_script update.sh ;;
            13)
                local ts
                ts="$(read_or_default '请输入 backup timestamp（YYYYMMDD-HHMMSS）' '')"
                if [ -n "${ts}" ]; then
                    menu_action lumen_compose_restore "${ts}"
                else
                    log_warn "未输入 timestamp，已取消。"
                fi
                ;;
            14) menu_action lumen_compose_rollback ;;
            15) menu_action install_image_job ;;
            16) menu_action uninstall_image_job ;;
            17) menu_action run_lumen_script uninstall.sh ;;
            0)  exit 0 ;;
            *)  log_warn "无效选项：${choice}" ;;
        esac
    done
}

# 哪些子命令值得在执行前 self-update：
#   - 写命令 / 维护命令 / 菜单：是
#   - 纯查询（status/logs/version/help）：否，避免完全无副作用的查询都打外网
lumenctl_command_needs_self_update() {
    # 触发 self-update 的命令：实际会调用本地 scripts/* 或会写入持久数据的命令。
    # migrate / start / stop / restart / status / logs 是纯 docker compose 操作，
    # 跟 scripts 无关，不触发避免无意义打外网。
    case "$1" in
        menu|install-lumen|update-lumen|uninstall-lumen|rollback|backup|restore|bootstrap|migrate-env|migrate-env-apply|bootstrap-scripts)
            return 0
            ;;
    esac
    return 1
}

# lumenctl 入口处的 self-update：拉 GitHub 最新 scripts/，更新到 SCRIPT_DIR；
# 走 TTL 缓存（默认 600s）避免菜单反复打开就反复拉；网络/校验失败 → WARN 继续。
# lumenctl.sh 或 lib.sh 自己变了 → re-exec lumenctl 让函数定义/逻辑生效。
lumenctl_maybe_self_update() {
    # source 模式（测试 / interactive shell 用 `. lumenctl.sh; main ...` 调用）跳过：
    # 否则 source 后调 main 会真去拉 GitHub + re-exec 替换当前 bash 进程，破坏测试。
    # 直接执行时 BASH_SOURCE[0] == "$0"（同为 lumenctl.sh 路径），source 时 $0 是 caller。
    if [ "${BASH_SOURCE[0]}" != "${0}" ]; then
        return 0
    fi
    if [ "${LUMEN_LUMENCTL_SELF_UPDATED:-0}" = "1" ]; then
        return 0
    fi
    if [ "${LUMEN_LUMENCTL_SELF_UPDATE:-1}" = "0" ]; then
        return 0
    fi

    # 让用户看到自更新行为（之前完全静默，本地未提交改动被覆盖时用户感知是
    # "我刚改的代码神奇消失"）。需禁用：LUMEN_LUMENCTL_SELF_UPDATE=0。
    log_info "[self-update] 检查远端 scripts/ 更新（branch=${LUMEN_SELF_UPDATE_BRANCH:-main}, TTL=${LUMEN_SELF_UPDATE_TTL:-600}s）..."
    log_info "[self-update] 跳过本次更新：LUMEN_LUMENCTL_SELF_UPDATE=0 bash scripts/lumenctl.sh ..."

    lumen_self_update_scripts "${SCRIPT_DIR}" \
        "${LUMEN_SELF_UPDATE_BRANCH:-main}" \
        "${LUMEN_SELF_UPDATE_TTL:-600}"

    case " ${LUMEN_SELF_UPDATE_CHANGED:-} " in
        *" lumenctl.sh "*|*" lib.sh "*)
            log_info "[lumenctl] 核心脚本已更新（${LUMEN_SELF_UPDATE_CHANGED}），re-exec 新版。"
            export LUMEN_LUMENCTL_SELF_UPDATED=1
            exec bash "${SCRIPT_DIR}/lumenctl.sh" "$@"
            ;;
    esac
}

main() {
    local command="${1:-menu}"

    # 在 dispatch 之前拉一次最新脚本（带 TTL）。命令路由用原始 args，不要 shift。
    if lumenctl_command_needs_self_update "${command}"; then
        lumenctl_maybe_self_update "$@"
    fi

    shift || true
    case "${command}" in
        menu) show_menu ;;
        # Lifecycle：透传额外 args 给底层脚本，install.sh / update.sh 可识别 --flag
        install-lumen) run_lumen_install_script "$@" ;;
        update-lumen) run_lumen_script update.sh "$@" ;;
        uninstall-lumen) run_lumen_script uninstall.sh "$@" ;;
        rollback) lumen_compose_rollback "$@" ;;
        version) lumen_compose_version ;;
        # 应急：突破 TTL 强拉 scripts/（"我刚改了 scripts，想立刻生效"）
        bootstrap-scripts)
            LUMEN_SELF_UPDATE_FORCE=1 \
                lumen_self_update_scripts "${SCRIPT_DIR}" \
                    "${LUMEN_SELF_UPDATE_BRANCH:-main}" 0
            case "${LUMEN_SELF_UPDATE_RESULT:-}" in
                ok)
                    if [ -n "${LUMEN_SELF_UPDATE_CHANGED:-}" ]; then
                        log_info "[bootstrap-scripts] 已更新：${LUMEN_SELF_UPDATE_CHANGED}（备份 *.bak.${LUMEN_SELF_UPDATE_BACKUP_TS}）。"
                    else
                        log_info "[bootstrap-scripts] 远端与本地一致，无需替换。"
                    fi
                    ;;
                failed)   log_error "[bootstrap-scripts] 拉取失败，详见上方 WARN。"; exit 1 ;;
                disabled) log_warn "[bootstrap-scripts] 已通过 LUMEN_SELF_UPDATE=0 全局关闭。" ;;
                *)        log_info "[bootstrap-scripts] 跳过（${LUMEN_SELF_UPDATE_RESULT:-unknown}）。" ;;
            esac
            ;;
        # Docker compose runtime
        status) lumen_compose_status ;;
        logs) lumen_compose_logs "${1:-api}" ;;
        start) lumen_compose_start ;;
        stop) lumen_compose_stop ;;
        restart) lumen_compose_restart ;;
        migrate) lumen_compose_migrate ;;
        bootstrap) lumen_compose_bootstrap ;;
        migrate-env) lumen_env_migrate_file --dry-run "$@" ;;
        migrate-env-apply) lumen_env_migrate_file --apply "$@" ;;
        backup) lumen_compose_backup "$@" ;;
        restore) lumen_compose_restore "$@" ;;
        # Auxiliary（保留，不再适用 docker 时由内部函数自行报错）
        install-storage-units) install_storage_units ;;
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
