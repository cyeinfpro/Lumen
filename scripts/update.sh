#!/usr/bin/env bash
# Lumen 一键更新脚本（Capistrano 风格 release + symlink 原子切换版）。
#
# 行为：
#   1. 在 ${ROOT}/releases/<id>/ 下准备一个全新的 release 目录
#   2. 在该目录里 git clone / uv sync / npm ci / npm run build / alembic upgrade
#   3. 都成功后，原子地把 ${ROOT}/current 指向新 release，再 systemctl restart 服务
#   4. 任何一步失败都不切换 current；构建期失败直接清理新 release 目录
#
# 步骤之间通过 stdout 上的 step 协议（::lumen-step::）让 admin_update.py
# 解析 .update.log 推送实时进度到管理后台 SSE。详见 lib.sh::lumen_step_begin。
#
# 兼容旧 in-place 布局：检测到 ${ROOT}/current 不是 symlink 直接报错并提示先跑
# scripts/migrate_to_releases.sh，本脚本不做就地兼容。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

ROOT="$(lumen_resolve_repo_root "${SCRIPT_DIR}")"

lumen_install_signal_handlers
# Lock 必须落在 ROOT（而不是 release 内）：跨 release 共享同一把锁。
lumen_acquire_lock "${ROOT}" "update.sh"

log_info "项目根目录：${ROOT}"

# ---------------------------------------------------------------------------
# 检测 release 布局：current 必须是 symlink。
# 兼容首次部署 / 旧 in-place 布局：自动调用 migrate_to_releases.sh 完成
# 迁移，再继续后续 update。migrate 脚本本身是幂等的，重复跑只会快速 noop。
# 只在 root 执行时尝试自动迁移（迁移涉及 systemctl + chown，需要 root）。
# ---------------------------------------------------------------------------
need_migration=0
if [ ! -L "${ROOT}/current" ] || [ ! -d "${ROOT}/releases" ] || [ ! -d "${ROOT}/shared" ]; then
    need_migration=1
fi

if [ "${need_migration}" = "1" ]; then
    log_warn "检测到旧版 in-place 布局或首次部署 (current/releases/shared 不全)。"
    if [ "$(id -u)" -ne 0 ]; then
        log_error "迁移到 release 布局需要 root 权限；请改用 sudo 重跑或先手动执行："
        log_error "  sudo LUMEN_ROOT='${ROOT}' bash ${SCRIPT_DIR}/migrate_to_releases.sh"
        exit 1
    fi
    if [ ! -x "${SCRIPT_DIR}/migrate_to_releases.sh" ] && [ ! -f "${SCRIPT_DIR}/migrate_to_releases.sh" ]; then
        log_error "找不到 ${SCRIPT_DIR}/migrate_to_releases.sh，无法自动迁移。"
        exit 1
    fi
    log_info "自动调用 migrate_to_releases.sh 完成布局迁移……"
    if ! LUMEN_ROOT="${ROOT}" bash "${SCRIPT_DIR}/migrate_to_releases.sh"; then
        log_error "migrate_to_releases.sh 失败，停止 update。请手动排查后重跑。"
        exit 1
    fi
    # migrate 之后 SCRIPT_DIR 可能被搬移到 ${ROOT}/releases/initial/scripts/，
    # 但当前进程已经把 lib.sh source 进 shell；ROOT 变量值不变，下面继续走。
    log_info "迁移完成，继续 update。"
fi

if [ ! -L "${ROOT}/current" ]; then
    log_error "${ROOT}/current 仍不是 symlink；迁移可能未完成，请人工检查。"
    exit 1
fi
if [ ! -d "${ROOT}/releases" ] || [ ! -d "${ROOT}/shared" ]; then
    log_error "迁移后 ${ROOT}/releases 或 ${ROOT}/shared 仍不存在；请人工检查。"
    exit 1
fi

CURRENT_RELEASE="$(lumen_release_current_path "${ROOT}" || true)"
if [ -z "${CURRENT_RELEASE}" ] || [ ! -d "${CURRENT_RELEASE}" ]; then
    log_error "${ROOT}/current 解析失败；请检查 symlink 是否完整。"
    exit 1
fi
CURRENT_ID="$(basename "${CURRENT_RELEASE}")"
log_info "当前 release：${CURRENT_ID}"

# ---------------------------------------------------------------------------
# 运行用户解析（沿用旧 update.sh 的逻辑，保持非交互兼容性）
# ---------------------------------------------------------------------------
LUMEN_UPDATE_SYSTEMD_RUNTIME=0
LUMEN_UPDATE_RUN_USER="$(id -un 2>/dev/null || echo "${USER:-root}")"
LUMEN_UPDATE_RUN_GROUP="$(id -gn 2>/dev/null || echo "${LUMEN_UPDATE_RUN_USER}")"
LUMEN_UPDATE_EXEC_USER="${LUMEN_UPDATE_RUN_USER}"
LUMEN_UPDATE_EXEC_GROUP="${LUMEN_UPDATE_RUN_GROUP}"

if lumen_systemd_has_any_units lumen-api.service lumen-worker.service lumen-web.service; then
    LUMEN_UPDATE_SYSTEMD_RUNTIME=1
    LUMEN_UPDATE_RUN_USER="$(lumen_runtime_service_user)"
    LUMEN_UPDATE_RUN_GROUP="$(lumen_runtime_service_group "${LUMEN_UPDATE_RUN_USER}")"

    # systemd unit 里写了 User=lumen，但系统里可能根本没建过这个用户。
    # 这种状态在"systemd unit 已 deploy 但 install 中途中断 / 旧版 migrate 没建用户"
    # 时会出现。如果当前是 root 且有 useradd，就自动建一个 system 用户。
    if [ -n "${LUMEN_UPDATE_RUN_USER}" ] \
            && [ "${LUMEN_UPDATE_RUN_USER}" != "root" ] \
            && ! id "${LUMEN_UPDATE_RUN_USER}" >/dev/null 2>&1; then
        log_warn "systemd unit 要求运行用户 ${LUMEN_UPDATE_RUN_USER}，但系统中不存在；尝试自动创建。"
        if [ "$(id -u)" -ne 0 ]; then
            log_error "需要 root 权限创建 ${LUMEN_UPDATE_RUN_USER}。请用 sudo 重跑，或手动执行："
            log_error "  sudo useradd --system --home-dir ${ROOT} --shell /usr/sbin/nologin ${LUMEN_UPDATE_RUN_USER}"
            exit 1
        fi
        if ! command -v useradd >/dev/null 2>&1; then
            log_error "缺少 useradd（shadow-utils），无法自动创建用户。"
            log_error "请安装 shadow-utils 后重跑，或手动 useradd ${LUMEN_UPDATE_RUN_USER}。"
            exit 1
        fi
        LUMEN_NOLOGIN_SHELL=/usr/sbin/nologin
        [ -x "${LUMEN_NOLOGIN_SHELL}" ] || LUMEN_NOLOGIN_SHELL=/sbin/nologin
        [ -x "${LUMEN_NOLOGIN_SHELL}" ] || LUMEN_NOLOGIN_SHELL=/bin/false
        if ! useradd --system --home-dir "${ROOT}" --shell "${LUMEN_NOLOGIN_SHELL}" --create-home \
                "${LUMEN_UPDATE_RUN_USER}" 2>/dev/null; then
            # --create-home 在 home-dir 已存在时报错；重试不带 --create-home
            useradd --system --home-dir "${ROOT}" --shell "${LUMEN_NOLOGIN_SHELL}" \
                "${LUMEN_UPDATE_RUN_USER}"
        fi
        # 同步 group 名（useradd 默认会建同名 primary group）。
        LUMEN_UPDATE_RUN_GROUP="$(id -gn "${LUMEN_UPDATE_RUN_USER}" 2>/dev/null || echo "${LUMEN_UPDATE_RUN_USER}")"
        log_info "已创建 system 用户：${LUMEN_UPDATE_RUN_USER}:${LUMEN_UPDATE_RUN_GROUP}（home=${ROOT}, shell=${LUMEN_NOLOGIN_SHELL}）"

        # 把 ROOT 的几个关键运行时目录归还给新建的用户，让后续 fetch / build 能写。
        # 只动 release / shared / current / .env 这几个明确属于运行时的路径，避免误改宿主侧文件。
        for path in "${ROOT}/releases" "${ROOT}/shared" "${ROOT}/.env"; do
            [ -e "${path}" ] && chown -R "${LUMEN_UPDATE_RUN_USER}:${LUMEN_UPDATE_RUN_GROUP}" "${path}" 2>/dev/null || true
        done
        [ -L "${ROOT}/current" ] && chown -h "${LUMEN_UPDATE_RUN_USER}:${LUMEN_UPDATE_RUN_GROUP}" "${ROOT}/current" 2>/dev/null || true
    fi

    LUMEN_UPDATE_EXEC_USER="${LUMEN_UPDATE_RUN_USER}"
    LUMEN_UPDATE_EXEC_GROUP="${LUMEN_UPDATE_RUN_GROUP}"
    case "${ROOT}" in
        /root|/root/*)
            if [ "${EUID:-$(id -u)}" -eq 0 ]; then
                LUMEN_UPDATE_EXEC_USER="root"
                LUMEN_UPDATE_EXEC_GROUP="root"
                log_warn "检测到部署目录位于 ${ROOT}；依赖/迁移/构建将以 root 执行，避免 ${LUMEN_UPDATE_RUN_USER} 无法遍历 /root。"
            fi
            ;;
    esac
    log_info "检测到 systemd 部署，服务用户：${LUMEN_UPDATE_RUN_USER}:${LUMEN_UPDATE_RUN_GROUP}；构建用户：${LUMEN_UPDATE_EXEC_USER}:${LUMEN_UPDATE_EXEC_GROUP}"
fi

lumen_update_as_runtime_user() {
    if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" = "1" ] && [ "${LUMEN_UPDATE_EXEC_USER}" != "$(id -un 2>/dev/null || true)" ]; then
        lumen_run_as_user "${LUMEN_UPDATE_EXEC_USER}" "$@"
    else
        "$@"
    fi
}

lumen_update_runtime_command_path() {
    local cmd="$1"
    local path=""
    if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" = "1" ] && [ "${LUMEN_UPDATE_EXEC_USER}" != "$(id -un 2>/dev/null || true)" ]; then
        path="$(lumen_run_as_user "${LUMEN_UPDATE_EXEC_USER}" sh -lc "command -v ${cmd}" 2>/dev/null || true)"
    else
        path="$(command -v "${cmd}" 2>/dev/null || true)"
    fi
    [ -n "${path}" ] || return 1
    printf '%s' "${path}"
}

lumen_update_ensure_runtime_can_access_path() {
    local path="$1"
    local label="${2:-路径}"
    local dir
    dir="$(dirname "${path}")"

    if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" != "1" ] || [ "${LUMEN_UPDATE_EXEC_USER}" = "root" ]; then
        return 0
    fi
    if lumen_run_as_user "${LUMEN_UPDATE_EXEC_USER}" test -r "${path}" >/dev/null 2>&1; then
        return 0
    fi
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then
        log_error "${label} 对构建用户 ${LUMEN_UPDATE_EXEC_USER} 不可读：${path}"
        log_error "请用 root 运行 update，或修复部署目录祖先权限。"
        return 1
    fi

    log_warn "${label} 对构建用户 ${LUMEN_UPDATE_EXEC_USER} 不可读，尝试修复部署目录遍历权限：${path}"
    chown -R "${LUMEN_UPDATE_EXEC_USER}:${LUMEN_UPDATE_EXEC_GROUP}" "${dir}" 2>/dev/null || true

    if command -v setfacl >/dev/null 2>&1; then
        local walk="${dir}"
        while [ -n "${walk}" ] && [ "${walk}" != "/" ]; do
            setfacl -m "u:${LUMEN_UPDATE_EXEC_USER}:x" "${walk}" 2>/dev/null || true
            walk="$(dirname "${walk}")"
        done
    fi

    if ! lumen_run_as_user "${LUMEN_UPDATE_EXEC_USER}" test -r "${path}" >/dev/null 2>&1; then
        local walk="${dir}"
        while [ -n "${walk}" ] && [ "${walk}" != "/" ]; do
            chmod o+x "${walk}" 2>/dev/null || true
            walk="$(dirname "${walk}")"
        done
    fi

    if ! lumen_run_as_user "${LUMEN_UPDATE_EXEC_USER}" test -r "${path}" >/dev/null 2>&1; then
        log_error "${label} 对构建用户 ${LUMEN_UPDATE_EXEC_USER} 仍不可读：${path}"
        log_error "建议把 Lumen 部署到 /opt/lumen，或手动允许 ${LUMEN_UPDATE_EXEC_USER} 遍历部署目录。"
        return 1
    fi
}

lumen_update_render_systemd_unit() {
    local src="$1"
    local dst="$2"
    local service_user="$3"
    local service_group="$4"
    python3 - "$src" "$dst" "$ROOT" "$service_user" "$service_group" <<'PY'
from pathlib import Path
import sys

src, dst, root, user, group = sys.argv[1:6]
text = Path(src).read_text(encoding="utf-8")
text = text.replace("/opt/lumen", root)
text = text.replace("User=lumen", f"User={user}")
text = text.replace("Group=lumen", f"Group={group}")
text = text.replace("id -u lumen", f"id -u {user}")
text = text.replace("id -g lumen", f"id -g {group}")
if root == "/root" or root.startswith("/root/"):
    text = text.replace("ProtectHome=true", "ProtectHome=false")
Path(dst).write_text(text, encoding="utf-8")
PY
}

lumen_update_dump_failed_unit_logs() {
    command -v systemctl >/dev/null 2>&1 || return 0
    command -v journalctl >/dev/null 2>&1 || return 0
    local unit state
    for unit in "$@"; do
        state="$(systemctl is-active "${unit}" 2>/dev/null || true)"
        if [ "${state}" = "active" ]; then
            continue
        fi
        log_error "${unit} 当前状态：${state:-unknown}"
        systemctl status "${unit}" --no-pager -l 2>/dev/null | tail -n 40 >&2 || true
        journalctl -u "${unit}" -n "${LUMEN_SYSTEMD_LOG_TAIL_LINES:-80}" --no-pager 2>/dev/null >&2 || true
    done
}

lumen_update_sync_systemd_units() {
    if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" != "1" ]; then
        return 0
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        return 0
    fi
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then
        log_warn "非 root 运行，跳过 systemd unit 同步。"
        return 0
    fi
    local src_dir="${NEW_RELEASE}/deploy/systemd"
    if [ ! -d "${src_dir}" ]; then
        log_warn "找不到 ${src_dir}，跳过 systemd unit 同步。"
        return 0
    fi
    local service_user="${LUMEN_UPDATE_RUN_USER}"
    local service_group="${LUMEN_UPDATE_RUN_GROUP}"
    case "${ROOT}" in
        /root|/root/*)
            service_user="root"
            service_group="root"
            ;;
    esac
    log_info "同步 systemd unit（root=${ROOT}, user=${service_user}:${service_group}）"
    local f tmp
    for f in lumen-api.service lumen-web.service lumen-worker.service \
             lumen-tgbot.service lumen-update-runner.service \
             lumen-update.path lumen-backup.service lumen-backup.timer \
             lumen-health-watchdog.service lumen-health-watchdog.timer; do
        [ -f "${src_dir}/${f}" ] || continue
        tmp="$(mktemp)"
        lumen_update_render_systemd_unit "${src_dir}/${f}" "${tmp}" "${service_user}" "${service_group}"
        install -m 0644 "${tmp}" "/etc/systemd/system/${f}"
        rm -f "${tmp}"
    done
    systemctl daemon-reload
}

lumen_update_require_runtime_cmd() {
    local cmd="$1"
    local hint="$2"
    local path=""
    if path="$(lumen_update_runtime_command_path "${cmd}")"; then
        printf '%s' "${path}"
        return 0
    fi

    # uv 缺失时自动安装。systemd 部署经常是 root 触发更新、服务以 lumen
    # 用户运行；如果部署目录在 /root/Lumen，直接以 lumen 跑官方安装脚本会尝试写
    # /root/Lumen/.local/bin 并因 /root 权限失败。root 触发时优先安装到
    # /usr/local/bin 这类系统级 PATH，再让 runtime 用户重新 probe。
    if [ "${cmd}" = "uv" ] && [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" = "1" ]; then
        log_warn "uv 不在构建用户 ${LUMEN_UPDATE_EXEC_USER} 的 PATH，尝试通过官方脚本自动安装……"
        local installed_uv=0
        if [ "${EUID:-$(id -u)}" -eq 0 ]; then
            for uv_install_dir in /usr/local/bin /usr/bin; do
                if [ -d "${uv_install_dir}" ] && [ -w "${uv_install_dir}" ]; then
                    if env UV_INSTALL_DIR="${uv_install_dir}" sh -lc \
                            'curl -LsSf https://astral.sh/uv/install.sh | sh' >&2; then
                        installed_uv=1
                        break
                    fi
                fi
            done
        fi
        if [ "${installed_uv}" -ne 1 ]; then
            if lumen_run_as_user "${LUMEN_UPDATE_EXEC_USER}" sh -lc \
                    'curl -LsSf https://astral.sh/uv/install.sh | sh' >&2; then
                installed_uv=1
            fi
        fi

        if [ "${installed_uv}" -eq 1 ]; then
            if path="$(lumen_update_runtime_command_path "${cmd}")"; then
                log_info "uv 自动安装完成：${path}"
                printf '%s' "${path}"
                return 0
            fi
            # uv installer 默认装到 ~/.local/bin，但有些环境 .profile 还没更新；
            # 直接尝试常见路径作为兜底。
            local home_dir
            home_dir="$(lumen_run_as_user "${LUMEN_UPDATE_EXEC_USER}" sh -lc 'printf %s "${HOME}"' 2>/dev/null || true)"
            if [ -n "${home_dir}" ] && [ -x "${home_dir}/.local/bin/uv" ]; then
                log_info "uv 自动安装完成：${home_dir}/.local/bin/uv"
                printf '%s' "${home_dir}/.local/bin/uv"
                return 0
            fi
        fi
        log_error "uv 自动安装失败或安装后仍不可达。"
    fi

    log_error "缺少 ${cmd}，或构建用户 ${LUMEN_UPDATE_EXEC_USER} 无法访问。"
    log_error "${hint}"
    return 1
}

# ---------------------------------------------------------------------------
# 工具检查（更新阶段假设 install 已经做过完整检查）
# ---------------------------------------------------------------------------
lumen_require_docker_access
UV_BIN="$(lumen_update_require_runtime_cmd uv "curl -LsSf https://astral.sh/uv/install.sh | sh")"
NPM_BIN="$(lumen_update_require_runtime_cmd npm "请安装 Node.js >= 20")"
GIT_BIN="$(lumen_update_runtime_command_path git || true)"

if [ -z "${GIT_BIN}" ]; then
    log_error "未找到 git；release 模式必须用 git 拉取代码。"
    exit 1
fi

# ---------------------------------------------------------------------------
# 状态变量（trap 也会用）
# ---------------------------------------------------------------------------
NEW_ID=""
NEW_RELEASE=""
SWITCHED=0           # 1 = current 已切到 NEW_ID，rollback 需要切回去
RESTART_OK=0         # 1 = systemctl restart 已成功
ROLLBACK_DONE=0      # 防止 trap 重复 rollback

# 触发 rollback：根据 SWITCHED 选择策略：
#   - SWITCHED=0：直接删除 NEW_RELEASE（如有）；不动 current。
#   - SWITCHED=1：把 current 切回 CURRENT_ID，重启服务；DB 不回滚（提示人工干预）。
lumen_update_rollback() {
    if [ "${ROLLBACK_DONE}" -eq 1 ]; then
        return 0
    fi
    ROLLBACK_DONE=1

    lumen_step_begin rollback

    if [ "${SWITCHED}" -eq 0 ]; then
        if [ -n "${NEW_RELEASE}" ] && [ -d "${NEW_RELEASE}" ]; then
            log_warn "rollback：删除未启用的 release ${NEW_ID}"
            lumen_step_info rollback action "remove_unmounted_release"
            lumen_step_info rollback release_id "${NEW_ID}"
            rm -rf "${NEW_RELEASE}" 2>/dev/null || true
        else
            lumen_step_info rollback action "noop"
        fi
        lumen_step_end rollback 0
        return 0
    fi

    # SWITCHED=1：current 已经被切到 NEW_ID。把它切回去，并重启服务。
    log_warn "rollback：把 current 切回到 ${CURRENT_ID}"
    lumen_step_info rollback action "switch_back"
    lumen_step_info rollback target_release "${CURRENT_ID}"
    if lumen_release_atomic_switch "${ROOT}" "${CURRENT_ID}"; then
        lumen_step_info rollback switch_back "ok"
    else
        log_error "rollback：切换回 ${CURRENT_ID} 失败，需人工介入！"
        lumen_step_info rollback switch_back "failed"
    fi

    # DB 已经被 alembic 推进过，回切应用版本可能与新 schema 不兼容。
    lumen_step_info rollback note "DB schema advanced; manual intervention may be required"
    log_warn "rollback：DB schema 已被 migrate 步骤推进；如新旧版本 schema 不兼容，请人工干预。"

    # 重启服务，让旧版应用代码起来（schema 是新的，但旧代码读新 schema 通常向后兼容）。
    if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" = "1" ]; then
        local -a units=()
        local u
        for u in lumen-worker.service lumen-web.service lumen-tgbot.service lumen-api.service; do
            if lumen_systemd_has_unit "${u}"; then
                units+=("${u}")
            fi
        done
        if [ "${#units[@]}" -gt 0 ]; then
            if lumen_restart_systemd_units "${units[@]}"; then
                lumen_step_info rollback restart "ok"
            else
                lumen_step_info rollback restart "failed"
            fi
        fi
    fi
    lumen_step_end rollback 1
    return 0
}

# trap：脚本因 set -e / 显式 exit 非零退出时跑。
lumen_update_on_err() {
    local rc="$?"
    [ "${rc}" -eq 0 ] && return 0
    # 把当前未结束的 phase 收口为失败 done 行
    lumen_step_finalize_failure "${rc}"
    log_error "更新失败：返回码 ${rc}"
    lumen_update_rollback
    exit "${rc}"
}
trap 'lumen_update_on_err' ERR
# EXIT 兜底：bash 3.2 的 set -e + ERR 在某些路径不会触发 ERR（比如显式 exit）。
trap 'rc=$?; [ "$rc" -ne 0 ] && lumen_update_on_err || true; lumen_release_lock' EXIT

# ---------------------------------------------------------------------------
# Phase 1: prepare
# ---------------------------------------------------------------------------
lumen_step_begin prepare
log_step "[prepare] 计算新 release id 并准备目录"

# 先用 current 的 sha 作为占位（如果 git 不能 fetch，整个 release 仍能基于现有代码继续）。
PREP_SHA="$(cd "${CURRENT_RELEASE}" && lumen_update_as_runtime_user "${GIT_BIN}" rev-parse HEAD 2>/dev/null || echo unknown)"
NEW_ID="$(lumen_release_id "${PREP_SHA}")"
NEW_RELEASE="${ROOT}/releases/${NEW_ID}"

if [ -e "${NEW_RELEASE}" ]; then
    log_error "目标 release 目录已存在：${NEW_RELEASE}"
    lumen_step_end prepare 1
    exit 1
fi

mkdir -p "${NEW_RELEASE}"
# 必要的子目录：保证 link_shared 时父目录已就绪
mkdir -p "${ROOT}/shared/web-env" "${ROOT}/shared/worker-var" "${ROOT}/shared/web-next-cache"

# 如果是 systemd 部署，确保 release 目录归运行用户。
if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" = "1" ]; then
    lumen_run_as_root chown -R "${LUMEN_UPDATE_RUN_USER}:${LUMEN_UPDATE_RUN_GROUP}" \
        "${NEW_RELEASE}" "${ROOT}/shared" 2>/dev/null || true
fi

lumen_step_info prepare release_id "${NEW_ID}"
lumen_step_info prepare release_path "${NEW_RELEASE}"
lumen_step_end prepare 0

# ---------------------------------------------------------------------------
# Phase 2: fetch
# 优先在 release 目录里 git clone --reference current（更快）。
# 拉不到（离线 / 远端无更新）则从 current rsync 后再 git pull。
# 如果新 sha == current sha，直接退出 0：已是最新版本，无需创建 release。
# ---------------------------------------------------------------------------
lumen_step_begin fetch
log_step "[fetch] 在新 release 目录里同步代码"

CURRENT_SHA="$(cd "${CURRENT_RELEASE}" && lumen_update_as_runtime_user "${GIT_BIN}" rev-parse HEAD 2>/dev/null || echo unknown)"
CURRENT_BRANCH="$(cd "${CURRENT_RELEASE}" && lumen_update_as_runtime_user "${GIT_BIN}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
# release 在 fetch 阶段 git checkout 到具体 sha 后变成 detached HEAD，
# rev-parse --abbrev-ref HEAD 会返回字面 "HEAD"。这会让后面 fetch + rev-parse
# origin/${CURRENT_BRANCH} 解析成 origin/HEAD（指向 clone 那一刻的 sha），
# 与 current_sha 永远相等，从而错误地走 noop_already_latest 分支，永远拉不到
# 远端新 commit。fallback 顺序：.lumen_release.json 里记录的 branch → main。
if [ "${CURRENT_BRANCH}" = "HEAD" ] || [ -z "${CURRENT_BRANCH}" ]; then
    CURRENT_BRANCH=""
    if [ -f "${CURRENT_RELEASE}/.lumen_release.json" ]; then
        CURRENT_BRANCH="$(sed -nE 's/.*"branch"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' \
            "${CURRENT_RELEASE}/.lumen_release.json" 2>/dev/null | head -n1)"
    fi
    if [ -z "${CURRENT_BRANCH}" ] || [ "${CURRENT_BRANCH}" = "HEAD" ]; then
        CURRENT_BRANCH="main"
    fi
    log_warn "[fetch] release 处于 detached HEAD，使用 fallback 分支：${CURRENT_BRANCH}"
fi
GIT_REMOTE_URL="$(cd "${CURRENT_RELEASE}" && lumen_update_as_runtime_user "${GIT_BIN}" config --get remote.origin.url 2>/dev/null || echo "")"

lumen_step_info fetch current_sha "${CURRENT_SHA}"
lumen_step_info fetch branch "${CURRENT_BRANCH}"

# 优先尝试 fetch 远端，看是否有更新。
NEW_SHA="${CURRENT_SHA}"
FETCH_OK=0
if [ -n "${GIT_REMOTE_URL}" ]; then
    if (cd "${CURRENT_RELEASE}" && lumen_update_as_runtime_user "${GIT_BIN}" fetch --quiet origin "${CURRENT_BRANCH}" 2>/dev/null); then
        FETCH_OK=1
        NEW_SHA="$(cd "${CURRENT_RELEASE}" && lumen_update_as_runtime_user "${GIT_BIN}" rev-parse "origin/${CURRENT_BRANCH}" 2>/dev/null || echo "${CURRENT_SHA}")"
    else
        log_warn "[fetch] git fetch 失败（离线或网络问题），将复用 current sha"
        lumen_step_info fetch fetch_status "failed_use_current"
    fi
fi
lumen_step_info fetch new_sha "${NEW_SHA}"

# 若没有更新可拉，且代码已是最新，则退出 0（不创建 release）。
if [ "${NEW_SHA}" = "${CURRENT_SHA}" ] && [ "${FETCH_OK}" -eq 1 ]; then
    log_info "已是最新版本（${CURRENT_SHA}），无需创建新 release。"
    lumen_step_info fetch action "noop_already_latest"
    lumen_step_end fetch 0
    # 清理已创建但未使用的 release 目录。
    rm -rf "${NEW_RELEASE}" 2>/dev/null || true
    NEW_RELEASE=""
    NEW_ID=""
    # 清掉 trap 防止误判。
    trap - ERR
    trap 'lumen_release_lock' EXIT
    exit 0
fi

# 选择拉取方式：clone --shared / 离线时 rsync。
if [ "${FETCH_OK}" -eq 1 ] && [ -n "${GIT_REMOTE_URL}" ]; then
    log_info "[fetch] 从 ${GIT_REMOTE_URL} clone 到 ${NEW_RELEASE}"
    # 清空 NEW_RELEASE 让 git clone 接管（mkdir 已建好）。
    rmdir "${NEW_RELEASE}" 2>/dev/null || true
    # --reference-if-able 在 git ≥ 2.11 才支持；老版本回退到 --reference（强依赖路径存在）
    # 或直接不用 reference 优化（最坏情况只是慢一点）。
    GIT_CLONE_REFERENCE_FLAGS=()
    if [ -d "${CURRENT_RELEASE}/.git" ]; then
        GIT_VERSION_RAW="$(lumen_update_as_runtime_user "${GIT_BIN}" --version 2>/dev/null | awk '{print $3}' || true)"
        if printf '%s\n2.11.0\n' "${GIT_VERSION_RAW}" | sort -V -C 2>/dev/null; then
            GIT_CLONE_REFERENCE_FLAGS+=(--reference-if-able "${CURRENT_RELEASE}/.git")
        else
            log_warn "[fetch] git ${GIT_VERSION_RAW} < 2.11，跳过 --reference-if-able 优化（首次 clone 会更慢）。"
        fi
    fi
    if ! lumen_update_as_runtime_user "${GIT_BIN}" clone --quiet \
            "${GIT_CLONE_REFERENCE_FLAGS[@]}" \
            --branch "${CURRENT_BRANCH}" \
            "${GIT_REMOTE_URL}" "${NEW_RELEASE}"; then
        log_error "[fetch] git clone 失败"
        lumen_step_end fetch 1
        exit 1
    fi
    # checkout 到目标 sha（确保内容确定性）。
    if ! (cd "${NEW_RELEASE}" && lumen_update_as_runtime_user "${GIT_BIN}" checkout --quiet "${NEW_SHA}"); then
        log_error "[fetch] checkout ${NEW_SHA} 失败"
        lumen_step_end fetch 1
        exit 1
    fi
else
    # 离线 fallback：rsync from current（保留 .git）然后 git pull autostash。
    log_info "[fetch] 从 current 复制代码（离线 fallback）"
    if ! rsync -a --delete \
            --exclude='/.venv/' \
            --exclude='/node_modules/' \
            --exclude='/apps/web/.next/' \
            --exclude='/apps/web/node_modules/' \
            "${CURRENT_RELEASE}/" "${NEW_RELEASE}/"; then
        log_error "[fetch] rsync 失败"
        lumen_step_end fetch 1
        exit 1
    fi
fi

# 写 .lumen_release.json
ACTUAL_SHA="$(cd "${NEW_RELEASE}" && lumen_update_as_runtime_user "${GIT_BIN}" rev-parse HEAD 2>/dev/null || echo "${NEW_SHA}")"
ALEMBIC_HEAD_EXPECTED=""
if [ -d "${NEW_RELEASE}/apps/api" ]; then
    ALEMBIC_HEAD_EXPECTED="$(grep -hRE '^[[:space:]]*revision[[:space:]]*[:=]' "${NEW_RELEASE}/apps/api/migrations" 2>/dev/null \
        | head -n1 \
        | sed -E 's/.*[\"\x27]([a-f0-9]+)[\"\x27].*/\1/' || true)"
fi
cat > "${NEW_RELEASE}/.lumen_release.json" <<JSON
{
  "id": "${NEW_ID}",
  "sha": "${ACTUAL_SHA}",
  "branch": "${CURRENT_BRANCH}",
  "created_at": "$(lumen_iso_now)",
  "alembic_head_expected": "${ALEMBIC_HEAD_EXPECTED}",
  "alembic_head_applied": ""
}
JSON
lumen_step_info fetch sha "${ACTUAL_SHA}"
lumen_step_end fetch 0

# ---------------------------------------------------------------------------
# Phase 3: link_shared
# 把 shared 目录里的 .env.local / worker-var / .next/cache 软链进 release。
# ---------------------------------------------------------------------------
lumen_step_begin link_shared
log_step "[link_shared] 把 shared 目录软链到 release 内"

if ! lumen_release_ensure_shared_env "${ROOT}"; then
    lumen_step_end link_shared 1
    exit 1
fi
if ! lumen_release_link_shared "${NEW_RELEASE}" "${ROOT}/shared"; then
    lumen_step_end link_shared 1
    exit 1
fi
if ! lumen_ensure_compose_db_env_vars "${NEW_RELEASE}/.env"; then
    lumen_step_end link_shared 1
    exit 1
fi
lumen_step_end link_shared 0

# link_shared 的 mkdir -p 是以 root 身份创建父目录（如 apps/web/.next）。
# 后续 build_web / deps_python 由构建用户执行；把 release 先交给构建用户，
# 发布切换前再按服务用户需要调整。
if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" = "1" ]; then
    chown -R "${LUMEN_UPDATE_EXEC_USER}:${LUMEN_UPDATE_EXEC_GROUP}" "${NEW_RELEASE}" 2>/dev/null || true
fi
if ! lumen_update_ensure_runtime_can_access_path "${NEW_RELEASE}/uv.toml" "uv 配置文件"; then
    lumen_step_end link_shared 1
    exit 1
fi

# ---------------------------------------------------------------------------
# Phase 4: containers
# 在 release 目录里跑 docker compose up -d --wait。
# 注：compose 文件里 service name 全局唯一，不会启第二个 postgres/redis；
# 这一步主要是确保数据库容器已经在跑（旧 current 重启过容器也算）。
# ---------------------------------------------------------------------------
lumen_step_begin containers
log_step "[containers] 确保 PostgreSQL / Redis 容器在运行（docker compose up -d --wait）"

(
    cd "${NEW_RELEASE}"
    # release 目录每次 update 都换名字，但容器 / volume 必须固定在 "lumen"
    # project，否则 compose up 会以为现有容器属于别的 project 而尝试重建，
    # 引发 container_name conflict + volume 错位丢数据。
    export COMPOSE_PROJECT_NAME=lumen
    if ! lumen_docker compose up -d --wait; then
        log_error "[containers] 容器启动或健康检查失败"
        exit 1
    fi
)
CONTAINERS_RC=$?
if [ "${CONTAINERS_RC}" -ne 0 ]; then
    lumen_step_end containers 1
    exit "${CONTAINERS_RC}"
fi
lumen_step_end containers 0

# ---------------------------------------------------------------------------
# Phase 5: deps_python
# release 目录里 uv sync。.venv 落在 release 内，跟整个 release 一起切换。
# ---------------------------------------------------------------------------
lumen_step_begin deps_python
log_step "[deps_python] 同步 Python 依赖（uv sync --frozen --all-packages）"

if ! (cd "${NEW_RELEASE}" && lumen_update_as_runtime_user "${UV_BIN}" sync --frozen --all-packages); then
    log_error "[deps_python] uv sync 失败。如果是 lock 已过期，请改跑 'uv sync --all-packages'。"
    lumen_step_end deps_python 1
    exit 1
fi
lumen_step_end deps_python 0

# ---------------------------------------------------------------------------
# Phase 6: migrate_db
# release 目录里 alembic upgrade head。
# 成功后把实际 head 写回 .lumen_release.json。
# ---------------------------------------------------------------------------
lumen_step_begin migrate_db
log_step "[migrate_db] 应用数据库迁移（alembic upgrade head）"

(
    cd "${NEW_RELEASE}/apps/api"
    if ! lumen_update_as_runtime_user "${UV_BIN}" run alembic upgrade head; then
        log_error "[migrate_db] alembic upgrade 失败"
        exit 1
    fi
)
MIGRATE_RC=$?
if [ "${MIGRATE_RC}" -ne 0 ]; then
    lumen_step_end migrate_db 1
    exit "${MIGRATE_RC}"
fi
ALEMBIC_HEAD_APPLIED="$(cd "${NEW_RELEASE}/apps/api" && lumen_update_as_runtime_user "${UV_BIN}" run alembic current 2>/dev/null \
    | awk '{print $1}' \
    | head -n1 || true)"
# 更新 .lumen_release.json 的 applied 字段（用 sed 替换 alembic_head_applied 行）
if [ -n "${ALEMBIC_HEAD_APPLIED}" ]; then
    sed -i.bak -E "s|\"alembic_head_applied\": \"[^\"]*\"|\"alembic_head_applied\": \"${ALEMBIC_HEAD_APPLIED}\"|" \
        "${NEW_RELEASE}/.lumen_release.json" 2>/dev/null || true
    rm -f "${NEW_RELEASE}/.lumen_release.json.bak" 2>/dev/null || true
fi
lumen_step_info migrate_db alembic_head "${ALEMBIC_HEAD_APPLIED}"
lumen_step_end migrate_db 0

# ---------------------------------------------------------------------------
# Phase 7: deps_node
# ---------------------------------------------------------------------------
lumen_step_begin deps_node
log_step "[deps_node] 同步前端依赖（npm ci）"

if ! (cd "${NEW_RELEASE}/apps/web" && lumen_update_as_runtime_user "${NPM_BIN}" ci); then
    log_error "[deps_node] npm ci 失败"
    lumen_step_end deps_node 1
    exit 1
fi
lumen_step_end deps_node 0

# ---------------------------------------------------------------------------
# Phase 8: build_web
# ---------------------------------------------------------------------------
lumen_step_begin build_web
log_step "[build_web] 构建前端（npm run build）"

WEB_ENV="${NEW_RELEASE}/apps/web/.env.local"
NEXT_PUBLIC_API_BASE_VALUE=""
if [ -f "${WEB_ENV}" ] && grep -qE "^NEXT_PUBLIC_API_BASE=.+" "${WEB_ENV}"; then
    NEXT_PUBLIC_API_BASE_VALUE="$(sed -n 's/^NEXT_PUBLIC_API_BASE=//p' "${WEB_ENV}" | head -n1)"
fi

(
    cd "${NEW_RELEASE}/apps/web"
    if [ -n "${NEXT_PUBLIC_API_BASE_VALUE}" ]; then
        lumen_update_as_runtime_user env NEXT_PUBLIC_API_BASE="${NEXT_PUBLIC_API_BASE_VALUE}" \
            "${NPM_BIN}" run build
    else
        lumen_update_as_runtime_user "${NPM_BIN}" run build
    fi
)
BUILD_RC=$?
if [ "${BUILD_RC}" -ne 0 ]; then
    log_error "[build_web] npm run build 失败"
    lumen_step_end build_web 1
    exit "${BUILD_RC}"
fi
lumen_step_end build_web 0

if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" = "1" ]; then
    chown -R "${LUMEN_UPDATE_RUN_USER}:${LUMEN_UPDATE_RUN_GROUP}" "${NEW_RELEASE}" 2>/dev/null || true
fi
lumen_update_sync_systemd_units

# ---------------------------------------------------------------------------
# Phase 9: switch
# 原子地把 current symlink 指向 NEW_ID。失败必须清理新 release。
# 切换成功后任何后续失败都需要 rollback 到旧 release。
# ---------------------------------------------------------------------------
lumen_step_begin switch
log_step "[switch] 原子切换 current -> ${NEW_ID}"

if ! lumen_release_atomic_switch "${ROOT}" "${NEW_ID}"; then
    log_error "[switch] symlink 切换失败"
    lumen_step_end switch 1
    exit 1
fi
SWITCHED=1
lumen_step_info switch from "${CURRENT_ID}"
lumen_step_info switch to "${NEW_ID}"
lumen_step_end switch 0

# ---------------------------------------------------------------------------
# Phase 10: restart
# 顺序：worker -> web -> tgbot -> api（与原 update.sh 一致）。
# 失败时 trap 触发 rollback：current 切回旧 ID，再重启回旧版本。
# ---------------------------------------------------------------------------
lumen_step_begin restart
log_step "[restart] 重启 systemd 服务"

lumen_ensure_runtime_dirs "${ROOT}/.env"

if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" = "1" ]; then
    LUMEN_RESTART_UNITS=()
    for _LUMEN_UNIT in lumen-worker.service lumen-web.service lumen-tgbot.service lumen-api.service; do
        if lumen_systemd_has_unit "${_LUMEN_UNIT}"; then
            LUMEN_RESTART_UNITS+=("${_LUMEN_UNIT}")
        fi
    done
    unset _LUMEN_UNIT
    if [ "${#LUMEN_RESTART_UNITS[@]}" -eq 0 ]; then
        log_error "[restart] 未发现可重启的 Lumen systemd unit"
        lumen_step_end restart 1
        exit 1
    fi
    if ! lumen_restart_systemd_units "${LUMEN_RESTART_UNITS[@]}"; then
        log_error "[restart] systemctl restart 失败"
        lumen_update_dump_failed_unit_logs "${LUMEN_RESTART_UNITS[@]}"
        lumen_step_end restart 1
        exit 1
    fi
else
    log_warn "[restart] 未发现 systemd 部署，跳过 systemctl restart（开发态依赖外部 supervisor）"
fi

RESTART_OK=1
lumen_step_end restart 0

# ---------------------------------------------------------------------------
# Phase 11: health_post
# ---------------------------------------------------------------------------
lumen_step_begin health_post
log_step "[health_post] 运行时健康检查"

if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" = "1" ]; then
    if ! lumen_check_runtime_health; then
        log_error "[health_post] 健康检查失败"
        lumen_step_end health_post 1
        exit 1
    fi
else
    log_warn "[health_post] 非 systemd 部署，跳过健康检查（请确认运行时另行起好）"
fi
lumen_step_end health_post 0

# ---------------------------------------------------------------------------
# Phase 12: cleanup
# 保留最近 5 个 release，其它删除。current/previous 指向的永远保留。
# 失败不致命：磁盘空间问题不应阻止此次更新被认可成功。
# ---------------------------------------------------------------------------
lumen_step_begin cleanup
log_step "[cleanup] 清理旧 release（保留最近 5 个）"

if lumen_release_cleanup_old "${ROOT}" "${LUMEN_RELEASE_KEEP:-5}"; then
    lumen_step_end cleanup 0
else
    # 不阻断更新成功：只记录警告。
    log_warn "[cleanup] 清理旧 release 失败（已忽略）"
    lumen_step_end cleanup 0
fi

# ---------------------------------------------------------------------------
# 收尾
# ---------------------------------------------------------------------------
log_step "更新完成"
log_info "release ${NEW_ID} 已上线（previous: ${CURRENT_ID}）"
log_info "  API:    ${LUMEN_API_HEALTH_URL:-http://127.0.0.1:8000/healthz}"
log_info "  Web:    ${LUMEN_WEB_HEALTH_URL:-http://127.0.0.1:3000/}"

# 解除 ERR/EXIT trap，留 lumen_release_lock 给最终 EXIT 处理。
trap - ERR
trap 'lumen_release_lock' EXIT
exit 0
