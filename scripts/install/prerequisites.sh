#!/usr/bin/env bash
# Host prerequisite detection and dependency installation.
# Sourced by scripts/install.sh after raw bootstrap has completed.

# 是否在 systemd 下运行（PID 1 是 systemd）。容器/WSL1/Alpine OpenRC/某些精简系统
# 会返回 1，调用者据此决定是否跳过 systemctl 调用。
_has_systemd() {
    raw_have_cmd systemctl || return 1
    [ -d /run/systemd/system ] || return 1
    return 0
}

# 等 docker daemon 起来。systemctl enable --now 后 daemon 启动需要几秒；
# 当前用户可能没在 docker 组，先尝试直接 docker info，再用 sudo -n 探测。
_wait_docker_daemon_ready() {
    local timeout=60 i=0
    log_info "等待 Docker daemon 就绪（最多 ${timeout}s）..."
    while [ "${i}" -lt "${timeout}" ]; do
        if docker info >/dev/null 2>&1; then
            log_info "Docker daemon 已就绪。"
            return 0
        fi
        if raw_have_cmd sudo && sudo -n docker info >/dev/null 2>&1; then
            log_info "Docker daemon 已就绪（通过 sudo 访问）。"
            return 0
        fi
        sleep 2
        i=$((i + 2))
    done
    return 1
}

# Linux 的 apt 路径单独处理：apt-get update 失败仅 warn 不致命（cache 可能足以
# 装老版本包；某个 PPA 烂不应阻塞核心 install）。其他发行版直接走原 raw_install_packages。
_install_packages_linux_resilient() {
    local pkgs=("$@")
    [ "${#pkgs[@]}" -gt 0 ] || return 0
    if raw_have_cmd apt-get; then
        local update_ok=0 i
        for i in 1 2; do
            if raw_run_as_root env DEBIAN_FRONTEND=noninteractive apt-get update; then
                update_ok=1
                break
            fi
            if [ "${i}" -lt 2 ]; then
                log_warn "apt-get update 第 ${i} 次失败，5s 后重试。"
                sleep 5
            fi
        done
        if [ "${update_ok}" -eq 0 ]; then
            log_warn "apt-get update 反复失败；继续尝试 install（用现有 cache）。"
        fi
        raw_run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}" || return 1
        return 0
    fi
    raw_install_packages "${pkgs[@]}" || return 1
    return 0
}

# 检测 root 权限可用性：脚本要做 raw_run_as_root，如果当前不是 root 又没有 sudo
# 那么后续包安装一定 fail，提前 fail-fast 给清晰提示。
_ensure_root_or_sudo() {
    if [ "${EUID:-$(id -u 2>/dev/null || echo 1)}" -eq 0 ]; then
        return 0
    fi
    if raw_have_cmd sudo; then
        return 0
    fi
    log_error "当前用户不是 root，且未发现 sudo；无法安装系统包。请用 root 用户重跑或先装 sudo。"
    return 1
}

# 自动安装 openssl / curl 这类轻量基础包。Linux 走 _install_packages_linux_resilient
# （apt update 容错 + apt/dnf/yum/pacman/zypper/apk fallback），macOS 走 brew。
# 任一步失败返回 1，由调用者打印手动安装提示并退出。
_auto_install_basics() {
    local pkgs=("$@")
    [ "${#pkgs[@]}" -gt 0 ] || return 0
    log_info "尝试自动安装基础依赖：${pkgs[*]}"
    case "${OS}" in
        linux)
            _ensure_root_or_sudo || return 1
            _install_packages_linux_resilient ca-certificates "${pkgs[@]}" || return 1
            ;;
        macos)
            if ! raw_have_cmd brew; then
                log_warn "macOS 未发现 Homebrew，无法自动安装 ${pkgs[*]}。请先按 https://brew.sh 装 brew 再重跑。"
                return 1
            fi
            brew install "${pkgs[@]}" || return 1
            ;;
        *)
            return 1
            ;;
    esac
    raw_refresh_tool_paths
    return 0
}

# 自动安装 Docker：Linux 走官方 https://get.docker.com 一键脚本。
# 鲁棒性增强：先下载到 tmp 文件再执行（避免 curl|sh 半途断流）、加 timeout/retry、
# 把脚本输出 tee 到日志文件供排错、装完显式等 daemon ready。
# macOS 上不支持自动装 Docker Desktop（GUI），需用户手动安装。
_auto_install_docker() {
    case "${OS}" in
        linux)
            _ensure_root_or_sudo || return 1
            if ! raw_have_cmd curl; then
                log_error "缺 curl，无法下载 Docker 安装脚本；请先安装 curl 后重跑。"
                return 1
            fi

            local installer log_file rc
            installer="$(mktemp -t lumen-get-docker.XXXXXX 2>/dev/null || mktemp /tmp/lumen-get-docker.XXXXXX)"
            log_file="/tmp/lumen-docker-install.log"
            : > "${log_file}" 2>/dev/null || true
            # 关闭 ERR trap & errexit 临时，避免 trap 噪音；本函数自己管 rc。
            log_info "下载 Docker 安装脚本（https://get.docker.com → ${installer}）"
            if ! curl -fsSL \
                    --connect-timeout 30 \
                    --max-time 300 \
                    --retry 3 \
                    --retry-delay 5 \
                    --retry-connrefused \
                    -o "${installer}" \
                    "https://get.docker.com"; then
                log_error "下载 https://get.docker.com 失败（网络/代理/防火墙）；请检查后重跑或手动安装。"
                rm -f "${installer}" 2>/dev/null || true
                return 1
            fi
            if [ ! -s "${installer}" ]; then
                log_error "下载到的 Docker 安装脚本为空；请重试或手动安装。"
                rm -f "${installer}" 2>/dev/null || true
                return 1
            fi

            log_info "执行 Docker 安装脚本（详细输出 → ${log_file}，可能需要 1~3 分钟）"
            # tee 到日志文件 + 标准输出。PIPESTATUS[0] 是 sh 的 rc。
            set +e
            if [ "${EUID:-$(id -u)}" -eq 0 ]; then
                sh "${installer}" 2>&1 | tee "${log_file}"
                rc="${PIPESTATUS[0]}"
            else
                sudo sh "${installer}" 2>&1 | tee "${log_file}"
                rc="${PIPESTATUS[0]}"
            fi
            set -e
            rm -f "${installer}" 2>/dev/null || true

            if [ "${rc}" -ne 0 ]; then
                log_error "Docker 安装脚本失败（rc=${rc}）；详细日志：${log_file}"
                log_error "  常见原因：网络超时、apt/dnf 仓库被封、内核太旧、SELinux/AppArmor 拦截。"
                log_error "  排错：tail -n 80 ${log_file}"
                return 1
            fi
            raw_refresh_tool_paths

            # 启动 docker daemon：systemd 走 systemctl；OpenRC/其他走 service；都没有就靠 dockerd 自启。
            if _has_systemd; then
                if ! raw_run_as_root systemctl enable --now docker; then
                    log_warn "systemctl enable --now docker 失败；尝试 systemctl start docker。"
                    raw_run_as_root systemctl start docker || \
                        log_warn "systemctl start docker 也失败；下面的 daemon 等待会兜底报错。"
                fi
            elif raw_have_cmd service; then
                raw_run_as_root service docker start || \
                    log_warn "service docker start 失败；下面的 daemon 等待会兜底报错。"
            else
                log_warn "未检测到 systemd 或 service 命令（容器/WSL1/Alpine？）；如 daemon 未起请手动 dockerd & 或重启 shell。"
            fi

            # 把当前用户加入 docker 组（root 直接跳过）。
            local target_user
            target_user="${SUDO_USER:-${USER:-}}"
            [ -n "${target_user}" ] || target_user="$(id -un 2>/dev/null || echo)"
            if [ -n "${target_user}" ] && [ "${target_user}" != "root" ]; then
                if ! getent group docker >/dev/null 2>&1; then
                    log_warn "docker 组未发现（dockerd 安装异常？）；docker 命令将通过 sudo 调用。"
                elif id -nG "${target_user}" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
                    log_info "${target_user} 已在 docker 组中。"
                elif raw_run_as_root usermod -aG docker "${target_user}"; then
                    log_warn "已把 ${target_user} 加入 docker 组；当前 shell 仍未拿到组权限，docker 命令由 sudo 兜底，重登录后免 sudo。"
                else
                    log_warn "把 ${target_user} 加入 docker 组失败；docker 命令将通过 sudo 调用。"
                fi
            fi

            # 等 daemon 起来；起不来就 fail-fast。
            if ! _wait_docker_daemon_ready; then
                log_error "Docker 装好但 daemon 60s 内未就绪；请检查 systemctl status docker / journalctl -u docker。"
                return 1
            fi
            return 0
            ;;
        macos)
            log_error "macOS 上 Docker 需手动安装 Docker Desktop（https://www.docker.com/products/docker-desktop）；脚本无法自动安装 GUI 应用。"
            return 1
            ;;
        *)
            return 1
            ;;
    esac
}

# ---------------------------------------------------------------------------
# A. 前置检查
# 必装：docker / docker compose v2 / openssl / curl / python3 >= 3.8；
# Linux 额外要求 util-linux 提供 flock。
# 可选：systemd（仅 update-runner 路径用）
# 磁盘：/opt 至少 10GB
# ---------------------------------------------------------------------------
check_prerequisites() {
    emit_step_start prepare "前置检查（docker / compose v2 / openssl / curl / python3）"
    case "${OS}" in
        macos|linux) ;;
        *)
            log_error "暂不支持当前操作系统（uname -s = $(uname -s)）。仅支持 macOS 与 Linux（含 WSL2）。"
            exit 1
            ;;
    esac

    # 1) openssl / curl / rsync 缺则自动装（轻量、安全副作用低）。
    # rsync 在 prepare_release_layout 阶段必需；提前装避免后面才 fail-fast。
    local basics_missing=()
    command -v openssl >/dev/null 2>&1 || basics_missing+=("openssl")
    command -v curl    >/dev/null 2>&1 || basics_missing+=("curl")
    command -v rsync   >/dev/null 2>&1 || basics_missing+=("rsync")
    command -v python3 >/dev/null 2>&1 || basics_missing+=("python3")
    if [ "${OS}" = "linux" ] && ! command -v flock >/dev/null 2>&1; then
        basics_missing+=("util-linux")
    fi
    if [ "${#basics_missing[@]}" -gt 0 ]; then
        if ! _auto_install_basics "${basics_missing[@]}"; then
            log_error "缺少必备命令：${basics_missing[*]}（自动安装失败）"
            log_error "  请通过系统包管理器手动安装（apt/dnf/brew）后重跑。"
            exit 1
        fi
        # 装完 re-check（PATH 可能没刷新）。
        for cmd in openssl curl rsync python3; do
            if ! command -v "${cmd}" >/dev/null 2>&1; then
                log_error "${cmd} 自动安装后仍未在 PATH 中，请重新登录或手动安装后重跑。"
                exit 1
            fi
        done
        if [ "${OS}" = "linux" ] && ! command -v flock >/dev/null 2>&1; then
            log_error "util-linux 安装后仍未检测到 flock；请检查 PATH 或手动安装后重跑。"
            exit 1
        fi
    fi

    if ! lumen_require_python_min_version python3 3 8; then
        log_error "release manifest 校验器需要 Python >= 3.8。"
        exit 1
    fi

    # 2) docker 缺则走官方一键脚本（Linux）；macOS 仍 fail-fast 让用户装 Desktop。
    if ! command -v docker >/dev/null 2>&1; then
        case "${OS}" in
            linux)
                # 非交互模式直接装（脚本本来就是无人值守目标）；交互模式问一次，
                # 默认 No 防生产机上误触发系统级改动。
                local do_install=0
                if [ "${LUMEN_NONINTERACTIVE:-}" = "1" ]; then
                    do_install=1
                elif confirm "未检测到 Docker，是否调用官方一键脚本（https://get.docker.com）自动安装？"; then
                    do_install=1
                fi
                if [ "${do_install}" -eq 1 ]; then
                    if ! _auto_install_docker; then
                        exit 1
                    fi
                else
                    log_error "缺少 Docker；请按 https://docs.docker.com/engine/install/ 手动安装后重跑。"
                    exit 1
                fi
                ;;
            macos)
                log_error "缺少 Docker；macOS 请安装 Docker Desktop（https://www.docker.com/products/docker-desktop）后重跑。"
                exit 1
                ;;
        esac
        if ! command -v docker >/dev/null 2>&1; then
            log_error "Docker 安装后仍未检测到 docker 命令；请检查 PATH 或重新登录后重跑。"
            exit 1
        fi
    fi

    # docker compose v2 子命令检测
    if ! docker compose version >/dev/null 2>&1; then
        log_error "未检测到 docker compose v2 子命令。请安装 docker-compose-plugin（Linux）"
        log_error "或升级 Docker Desktop（macOS）。"
        exit 1
    fi

    # docker daemon 可达 + 是否需要 sudo
    if command -v lumen_require_docker_access >/dev/null 2>&1; then
        lumen_require_docker_access
    elif ! docker info >/dev/null 2>&1; then
        log_error "Docker daemon 未运行，或当前用户无权访问 Docker。"
        log_error "  Linux：sudo systemctl start docker；将用户加入 docker 组后重新登录"
        log_error "  macOS：启动 Docker Desktop 等待初始化"
        exit 1
    fi

    # 磁盘空间：分别探测 LUMEN_DATA_ROOT (storage/backup) 与 LUMEN_DB_ROOT
    # (postgres/redis)。--data-root=/data 时 /opt 容量充足无意义，必须探数据卷。
    # 没存在的路径退化到上级 / 根分区。
    local _disk_check_paths
    _disk_check_paths="$(printf '%s\n%s\n' \
        "${LUMEN_DATA_ROOT:-/opt/lumendata}" "${LUMEN_DB_ROOT:-/opt/lumendata}" | sort -u)"
    while IFS= read -r _path; do
        [ -z "${_path}" ] && continue
        local probe="${_path}"
        # 找最近存在的祖先目录
        while [ -n "${probe}" ] && [ "${probe}" != "/" ] && [ ! -d "${probe}" ]; do
            probe="$(dirname "${probe}")"
        done
        [ -d "${probe}" ] || probe="/"
        if command -v df >/dev/null 2>&1; then
            local free_kb
            free_kb="$(df -Pk "${probe}" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
            if [ -n "${free_kb}" ] && [ "${free_kb}" -lt $((10 * 1024 * 1024)) ] 2>/dev/null; then
                log_warn "${probe} 空闲约 $((free_kb / 1024)) MB（< 10 GB 建议值；目标路径 ${_path}）"
                if [ "${LUMEN_NONINTERACTIVE:-}" != "1" ] && ! confirm "仍要继续？"; then
                    exit 0
                fi
            fi
        fi
    done <<< "${_disk_check_paths}"

    log_info "前置检查通过：docker $(docker --version 2>&1 | awk '{print $3}' | tr -d ',') / compose v2 / openssl / curl"
    emit_step_done
}
