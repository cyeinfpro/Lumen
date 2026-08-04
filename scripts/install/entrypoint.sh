#!/usr/bin/env bash
# Install command parsing and fresh-install boundary.

usage() {
    cat <<EOF
Lumen 安装入口（Docker Compose 全栈版）

用法：
  bash scripts/install.sh                    打开运维菜单
  bash scripts/install.sh --auto             自动：有部署走 update，新机器走 install
  bash scripts/install.sh --install [opts]   直接安装 Lumen（docker compose）
  bash scripts/install.sh --update           更新 Lumen
  bash scripts/install.sh --uninstall        卸载 Lumen

--install 可选参数：
  --image-tag=vX.Y.Z      钉死镜像 tag（默认探测 GHCR latest，不自动回退 main）
  --data-root=/path       LUMEN_DATA_ROOT 文件/备份根目录（默认 /opt/lumendata）
  --db-root=/path         LUMEN_DB_ROOT 数据库根目录（默认跟随 LUMEN_DATA_ROOT）
  --build                 用本地 Dockerfile 构建而不是 pull GHCR（等价 LUMEN_INSTALL_BUILD=1）

环境变量：
  LUMEN_DEPLOY_ROOT       部署根目录（默认 /opt/lumen 或脚本所在父目录）
  LUMEN_NONINTERACTIVE=1  非交互模式：从 LUMEN_ADMIN_EMAIL / LUMEN_ADMIN_PASSWORD 读管理员
  LUMEN_IMAGE_REGISTRY    镜像 registry 前缀（默认 ghcr.io/cyeinfpro）
  LUMEN_INSTALL_BUILD=1   等价 --build

EOF
}

dispatch_auto() {
    local has_release=0 has_inplace=0 has_systemd=0
    [ -L "${ROOT}/current" ] && has_release=1
    [ -d "${ROOT}/apps/api" ] && has_inplace=1
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet lumen-api.service 2>/dev/null \
           || systemctl is-active --quiet lumen-worker.service 2>/dev/null \
           || systemctl is-active --quiet lumen-web.service 2>/dev/null; then
            has_systemd=1
        fi
    fi
    if [ "${has_release}" = "1" ] || [ "${has_inplace}" = "1" ] \
            || [ "${has_systemd}" = "1" ]; then
        log_info "[auto] 检测到已有 Lumen 部署 (release=${has_release} inplace=${has_inplace} systemd=${has_systemd})，转入 update 流程。"
        exec bash "${SCRIPT_DIR}/update.sh"
    fi
    log_info "[auto] 未检测到已有部署，进入全新安装流程。"
    if [ ! -r /dev/tty ] && [ -t 0 ]; then
        :
    elif [ ! -r /dev/tty ] && [ "${LUMEN_NONINTERACTIVE:-}" != "1" ]; then
        log_warn "[auto] 当前没有 tty，全新安装会卡在交互输入。"
        log_warn "[auto] 请改用：LUMEN_NONINTERACTIVE=1 bash ${SCRIPT_DIR}/install.sh --install   或在 SSH 终端里重跑。"
        exit 2
    fi
}

INSTALL_IMAGE_TAG_OVERRIDE=""
INSTALL_DATA_ROOT_OVERRIDE=""
INSTALL_DB_ROOT_OVERRIDE=""
INSTALL_BUILD_FLAG="${LUMEN_INSTALL_BUILD:-0}"

parse_install_args() {
    local arg
    for arg in "$@"; do
        case "${arg}" in
            --image-tag=*) INSTALL_IMAGE_TAG_OVERRIDE="${arg#*=}" ;;
            --data-root=*) INSTALL_DATA_ROOT_OVERRIDE="${arg#*=}" ;;
            --db-root=*) INSTALL_DB_ROOT_OVERRIDE="${arg#*=}" ;;
            --build) INSTALL_BUILD_FLAG=1 ;;
            *)
                usage
                log_error "未知 install 参数：${arg}"
                exit 1
                ;;
        esac
    done
}

dispatch_entrypoint() {
    local command="${1:-menu}"
    case "${command}" in
        menu|--menu)
            exec bash "${SCRIPT_DIR}/lumenctl.sh" menu
            ;;
        auto|--auto)
            shift || true
            dispatch_auto
            ;;
        install|--install)
            shift || true
            parse_install_args "$@"
            ;;
        update|--update)
            exec bash "${SCRIPT_DIR}/update.sh"
            ;;
        uninstall|--uninstall)
            exec bash "${SCRIPT_DIR}/uninstall.sh"
            ;;
        repair|--repair|repair-compose-project|--repair-compose-project)
            if ! command -v lumen_compose_project_unify >/dev/null 2>&1; then
                log_error "lib.sh 未提供 lumen_compose_project_unify；请确认 install.sh 与 lib.sh 同版本。"
                exit 1
            fi
            log_step "[repair] 检查并修复 lumen-* 容器 compose project 名漂移"
            lumen_compose_project_unify
            local root="${LUMEN_DEPLOY_ROOT:-/opt/lumen}/current"
            if [ ! -f "${root}/docker-compose.yml" ]; then
                log_error "未找到 ${root}/docker-compose.yml；无法重新启动 stack。"
                exit 1
            fi
            log_step "[repair] 重新启动 stack 到 project=${LUMEN_COMPOSE_PROJECT:-lumen}"
            if ! lumen_compose_in "${root}" \
                    up --pull missing -d --wait --force-recreate; then
                log_error "[repair] docker compose up 失败；请检查 docker / compose 状态。"
                exit 1
            fi
            log_info "[repair] 完成。当前 stack:"
            lumen_compose_in "${root}" ps
            exit 0
            ;;
        help|-h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            log_error "未知命令：${command}"
            exit 1
            ;;
    esac
}

guard_install_target_is_fresh() {
    if [ -L "${DEPLOY_ROOT}/current" ]; then
        log_error "检测到已有 release 部署，拒绝通过 install 路径执行数据库迁移。"
        log_error "请改用：bash ${DEPLOY_ROOT}/current/scripts/update.sh"
        return 1
    fi
    if [ -d "${DEPLOY_ROOT}/releases" ] \
            && find "${DEPLOY_ROOT}/releases" -mindepth 1 -maxdepth 1 \
                -print -quit 2>/dev/null | grep -q .; then
        log_error "检测到无 current 但仍含 release 内容的部署目录，拒绝猜测其状态。"
        log_error "请先检查并恢复 ${DEPLOY_ROOT}/current，或确认后清理残留 release。"
        return 1
    fi
    if [ -d "${DEPLOY_ROOT}/releases" ] \
            && [ -f "${DEPLOY_ROOT}/shared/.env" ]; then
        log_warn "检测到 fresh install 失败后保留的空 releases 与 shared/.env；允许安全重跑。"
    fi
    if [ ! -d "${DEPLOY_ROOT}/.git" ] \
            && [ -d "${DEPLOY_ROOT}/apps/api" ] \
            && [ -f "${DEPLOY_ROOT}/docker-compose.yml" ]; then
        log_error "检测到旧 in-place 部署，拒绝通过 install 路径覆盖并迁移数据库。"
        log_error "请先运行：bash ${SCRIPT_DIR}/update.sh"
        return 1
    fi
    return 0
}
