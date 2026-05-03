#!/usr/bin/env bash
# Lumen 容器化更新脚本（docker compose pull + migrate + up）。
#
# 行为（详见 docs/docker-full-stack-cutover-plan.md §11.3）：
#   1. lumen_with_lock "update" 把整段流程包在全局锁里
#   2. 解析目标 LUMEN_IMAGE_TAG（channel + GitHub Releases），current==target 直接 noop
#   3. preflight 检查 docker / 磁盘 / .env 关键字段 / /opt/lumendata 目录权限
#   4. backup_preflight 强制 PG dump（除非显式 SKIP），失败 abort
#   5. 准备新 release 目录（rsync 仓库快照），shared/.env 软链回去
#   6. set_image_tag → docker compose pull → start_infra → migrate_db
#   7. migrate 失败 → fail-fast，不切 current、不重启业务容器（§11.3 + §17.6）
#   8. 切 current → restart_services；失败自动用上一已知好 tag pull && up
#   9. health_check / cleanup
#
# 显式禁止（任何形式）：
#   - uv sync / pip install / npm ci / npm run build / systemctl restart lumen-*
#
# 阶段日志通过 lumen_emit_step / lumen_emit_info 输出 ::lumen-step:: 协议，
# 由管理后台 SSE 解析。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

# ---------------------------------------------------------------------------
# 兜底 shim：如果 lib.sh 还没加 wave 新 helper，定义最小可用版本，避免脚本崩。
# 真正版本由 lib.sh 提供时直接覆盖（function 定义后定义优先生效，不会冲突——
# 因为 source 顺序固定为 lib.sh 在前，shim 在后；shim 才是兜底）。
# 使用 `command -v xxx >/dev/null` 判断是否已存在，存在则不覆盖。
# ---------------------------------------------------------------------------

if ! command -v lumen_emit_step >/dev/null 2>&1; then
    lumen_emit_step() {
        local phase="" status="" rc="" dur_ms=""
        local kv key val
        for kv in "$@"; do
            key="${kv%%=*}"
            val="${kv#*=}"
            case "${key}" in
                phase) phase="${val}" ;;
                status) status="${val}" ;;
                rc) rc="${val}" ;;
                dur_ms) dur_ms="${val}" ;;
            esac
        done
        case "${status}" in
            start)
                lumen_step_begin "${phase}"
                ;;
            done|fail)
                lumen_step_end "${phase}" "${rc:-0}"
                ;;
            *)
                printf '::lumen-step:: phase=%s status=%s ts=%s\n' \
                    "${phase}" "${status}" "$(lumen_iso_now)"
                ;;
        esac
    }
fi

if ! command -v lumen_emit_info >/dev/null 2>&1; then
    lumen_emit_info() {
        local phase="" key="" value=""
        local kv k v
        for kv in "$@"; do
            k="${kv%%=*}"
            v="${kv#*=}"
            case "${k}" in
                phase) phase="${v}" ;;
                key) key="${v}" ;;
                value) value="${v}" ;;
            esac
        done
        # 屏蔽已知敏感 key
        case "${key}" in
            DATABASE_URL|REDIS_URL|SESSION_SECRET|PROVIDERS|*TOKEN*|*SECRET*|*PASSWORD*|*API_KEY*)
                value="***"
                ;;
        esac
        lumen_step_info "${phase}" "${key}" "${value}"
    }
fi

if ! command -v lumen_compose >/dev/null 2>&1; then
    lumen_compose() {
        COMPOSE_PROJECT_NAME="lumen" lumen_docker compose "$@"
    }
fi

if ! command -v lumen_compose_in >/dev/null 2>&1; then
    lumen_compose_in() {
        local dir="$1"; shift
        ( cd "${dir}" && COMPOSE_PROJECT_NAME="lumen" lumen_docker compose "$@" )
    }
fi

if ! command -v lumen_health_http >/dev/null 2>&1; then
    lumen_health_http() {
        local url="$1"
        local attempts="${2:-60}"
        local interval="${3:-2}"
        local _i status=""
        for _i in $(seq 1 "${attempts}"); do
            status="$(lumen_http_status "${url}" || true)"
            case "${status}" in
                2??|3??) return 0 ;;
            esac
            sleep "${interval}"
        done
        return 1
    }
fi

if ! command -v lumen_health_compose >/dev/null 2>&1; then
    lumen_health_compose() {
        local svc state
        for svc in "$@"; do
            state="$(lumen_docker inspect --format '{{.State.Status}}' "lumen-${svc}" 2>/dev/null || echo "missing")"
            if [ "${state}" != "running" ]; then
                log_error "服务 ${svc} 状态异常：${state}"
                return 1
            fi
        done
        return 0
    }
fi

if ! command -v lumen_image_tag_resolve >/dev/null 2>&1; then
    # 极简兜底：channel=stable→latest；channel=main→main；channel=v*→该字面 tag。
    # 真正实现应该查 GitHub Releases；这里只让脚本能跑起来。
    lumen_image_tag_resolve() {
        local channel="${1:-stable}"
        case "${channel}" in
            stable|"") printf 'latest' ;;
            main) printf 'main' ;;
            *) printf '%s' "${channel}" ;;
        esac
    }
fi

if ! command -v lumen_set_image_tag_in_env >/dev/null 2>&1; then
    lumen_set_image_tag_in_env() {
        local env_file="$1"
        local new_tag="$2"
        if [ -z "${env_file}" ] || [ -z "${new_tag}" ]; then
            log_error "lumen_set_image_tag_in_env: 参数缺失。"
            return 1
        fi
        if [ ! -f "${env_file}" ]; then
            log_error "lumen_set_image_tag_in_env: ${env_file} 不存在。"
            return 1
        fi
        local tmp="${env_file}.tag.tmp.$$"
        if grep -qE '^LUMEN_IMAGE_TAG=' "${env_file}"; then
            # 替换唯一一行
            awk -v tag="${new_tag}" '
                BEGIN { done = 0 }
                /^LUMEN_IMAGE_TAG=/ {
                    if (done == 0) { print "LUMEN_IMAGE_TAG=" tag; done = 1 }
                    next
                }
                { print }
            ' "${env_file}" > "${tmp}"
        else
            cp "${env_file}" "${tmp}"
            printf 'LUMEN_IMAGE_TAG=%s\n' "${new_tag}" >> "${tmp}"
        fi
        mv "${tmp}" "${env_file}"
        local cnt
        cnt="$(grep -cE '^LUMEN_IMAGE_TAG=' "${env_file}" 2>/dev/null || echo 0)"
        if [ "${cnt}" != "1" ]; then
            log_error "lumen_set_image_tag_in_env: 写入后 ${env_file} 中 LUMEN_IMAGE_TAG 行数=${cnt}（期望 1）。"
            return 1
        fi
        return 0
    }
fi

if ! command -v lumen_with_lock >/dev/null 2>&1; then
    lumen_with_lock() {
        local label="$1"
        local _ttl="${2:-1830}"  # 兜底：ttl 不强制
        shift 2 || true
        # 用现有 lumen_acquire_lock 套一把维护锁
        lumen_acquire_lock "${ROOT}" "${label}"
        "$@"
    }
fi

# ---------------------------------------------------------------------------
# ROOT / 状态变量
# ---------------------------------------------------------------------------
SCRIPT_ROOT="$(lumen_resolve_repo_root "${SCRIPT_DIR}")"
ROOT="${LUMEN_UPDATE_ROOT:-${SCRIPT_ROOT}}"
ROOT_SOURCE="script"
if [ -z "${LUMEN_UPDATE_ROOT:-}" ] \
        && [ "${ROOT}" != "${LUMEN_DEPLOY_ROOT}" ] \
        && [ ! -f "${ROOT}/shared/.env" ] \
        && [ ! -L "${ROOT}/current" ] \
        && [ -f "${LUMEN_DEPLOY_ROOT}/shared/.env" ]; then
    ROOT="${LUMEN_DEPLOY_ROOT}"
    ROOT_SOURCE="deploy_root"
    if [ -z "${LUMEN_REPO_DIR:-}" ] && [ -f "${SCRIPT_ROOT}/docker-compose.yml" ]; then
        LUMEN_REPO_DIR="${SCRIPT_ROOT}"
        export LUMEN_REPO_DIR
    fi
fi
SHARED_DIR="${ROOT}/shared"
SHARED_ENV="${SHARED_DIR}/.env"
LUMEN_DATA_ROOT="${LUMEN_DATA_ROOT:-/opt/lumendata}"
UPDATE_LOG_DIR="${LUMEN_DATA_ROOT}/backup"
OPERATION_ID="update-$(date -u +%Y%m%d-%H%M%S)-$$"

NEW_ID=""
NEW_RELEASE=""
PREVIOUS_TAG=""
TARGET_TAG=""
SWITCHED=0
ROLLBACK_DONE=0

lumen_install_signal_handlers

log_info "项目根目录：${ROOT}"
if [ "${ROOT_SOURCE}" = "deploy_root" ]; then
    log_info "检测到已安装部署目录：${ROOT}；发布物来源：${LUMEN_REPO_DIR:-${SCRIPT_ROOT}}"
fi
log_info "operation_id：${OPERATION_ID}"

# ---------------------------------------------------------------------------
# 工具函数：emit 包装 + 安全 mask
# ---------------------------------------------------------------------------
emit_start() { lumen_emit_step "phase=$1" "status=start"; }
emit_done()  { lumen_emit_step "phase=$1" "status=done" "rc=${2:-0}"; }
emit_fail()  { lumen_emit_step "phase=$1" "status=fail" "rc=${2:-1}"; }
emit_info()  { lumen_emit_info "phase=$1" "key=$2" "value=$3"; }
emit_warn()  { lumen_emit_info "phase=$1" "key=warn" "value=$2"; }

# 检查 .env 是否存在指定 key 且非空，不输出 value。
env_key_present() {
    local file="$1"
    local key="$2"
    [ -f "${file}" ] || return 1
    grep -qE "^${key}=.+" "${file}"
}

# 计算磁盘可用 GB（取 /opt 所在 fs）。失败回 -1。
disk_free_gb_opt() {
    local out
    if command -v df >/dev/null 2>&1; then
        # df -P -k 输出 1024-blocks，第 4 列是 available
        out="$(df -P -k /opt 2>/dev/null | awk 'NR==2 {print int($4/1024/1024)}')"
        if [ -n "${out}" ]; then
            printf '%s' "${out}"
            return 0
        fi
    fi
    printf '%s' "-1"
}

# 校验 /opt/lumendata 子目录属主：postgres=70, redis=999, storage/backup=10001。
# 不严格 chown，只 warn——install.sh 才负责强制 chown。
check_data_owners() {
    local missing=0
    local d
    for d in postgres redis storage backup; do
        if [ ! -d "${LUMEN_DATA_ROOT}/${d}" ]; then
            log_error "缺少数据目录：${LUMEN_DATA_ROOT}/${d}"
            missing=1
        fi
    done
    if [ "${missing}" -eq 1 ]; then
        return 1
    fi
    # 仅做 warn，不阻断（install.sh 是 single source of truth）
    local uid
    if command -v stat >/dev/null 2>&1; then
        uid="$(stat -c '%u' "${LUMEN_DATA_ROOT}/postgres" 2>/dev/null || stat -f '%u' "${LUMEN_DATA_ROOT}/postgres" 2>/dev/null || echo "")"
        [ -n "${uid}" ] && [ "${uid}" != "70" ] && log_warn "${LUMEN_DATA_ROOT}/postgres 属主非 70（实际 ${uid}），postgres 容器可能起不来。"
        uid="$(stat -c '%u' "${LUMEN_DATA_ROOT}/redis" 2>/dev/null || stat -f '%u' "${LUMEN_DATA_ROOT}/redis" 2>/dev/null || echo "")"
        [ -n "${uid}" ] && [ "${uid}" != "999" ] && log_warn "${LUMEN_DATA_ROOT}/redis 属主非 999（实际 ${uid}），redis 容器可能起不来。"
        uid="$(stat -c '%u' "${LUMEN_DATA_ROOT}/storage" 2>/dev/null || stat -f '%u' "${LUMEN_DATA_ROOT}/storage" 2>/dev/null || echo "")"
        [ -n "${uid}" ] && [ "${uid}" != "10001" ] && log_warn "${LUMEN_DATA_ROOT}/storage 属主非 10001（实际 ${uid}），api/worker 可能写不进去。"
        uid="$(stat -c '%u' "${LUMEN_DATA_ROOT}/backup" 2>/dev/null || stat -f '%u' "${LUMEN_DATA_ROOT}/backup" 2>/dev/null || echo "")"
        [ -n "${uid}" ] && [ "${uid}" != "10001" ] && log_warn "${LUMEN_DATA_ROOT}/backup 属主非 10001（实际 ${uid}），备份/日志可能写不进去。"
    fi
    return 0
}

# rsync 仓库内容到 release 目录；与 install 的发布物布局对齐。
# 排除 .git / node_modules / .venv / .next 等大目录。
rsync_repo_to_release() {
    local src="$1"
    local dst="$2"
    local err_file rc
    if ! command -v rsync >/dev/null 2>&1; then
        log_error "缺少 rsync，请先安装。"
        return 1
    fi
    err_file="$(mktemp "${UPDATE_LOG_DIR:-/tmp}/lumen-rsync.XXXXXX.err" 2>/dev/null || mktemp)"
    rc=0
    rsync -a \
        --exclude='/.git/' \
        --exclude='/.env' \
        --exclude='/.env.local' \
        --exclude='/shared/' \
        --exclude='/releases/' \
        --exclude='/current' \
        --exclude='/previous' \
        --exclude='/node_modules/' \
        --exclude='/.venv/' \
        --exclude='/.pytest_cache/' \
        --exclude='/.mypy_cache/' \
        --exclude='/.ruff_cache/' \
        --exclude='/apps/web/.next/' \
        --exclude='/apps/web/node_modules/' \
        --exclude='/apps/worker/var/' \
        --exclude='/var/' \
        --exclude='/.lumen-script.lock/' \
        --exclude='/.update.log' \
        --exclude='/.install-logs/' \
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        --exclude='.DS_Store' \
        "${src}/" "${dst}/" 2>"${err_file}" || rc=$?
    if [ "${rc}" -ne 0 ]; then
        log_error "rsync 失败（rc=${rc}）：${src} -> ${dst}"
        sed -n '1,30p' "${err_file}" 2>/dev/null | while IFS= read -r line; do
            [ -n "${line}" ] && log_error "rsync stderr: ${line}"
        done
        rm -f "${err_file}"
        return "${rc}"
    fi
    rm -f "${err_file}"
    return 0
}

sync_repo_to_release() {
    local src="$1"
    local dst="$2"
    local err_file rc
    if [ -d "${src}/.git" ] && command -v git >/dev/null 2>&1 && command -v tar >/dev/null 2>&1; then
        err_file="$(mktemp "${UPDATE_LOG_DIR:-/tmp}/lumen-git-archive.XXXXXX.err" 2>/dev/null || mktemp)"
        log_info "[fetch_release] git archive ${src} -> ${dst}"
        rc=0
        ( cd "${src}" && git archive --format=tar HEAD ) 2>"${err_file}" \
            | tar -xf - -C "${dst}" 2>>"${err_file}" || rc=$?
        if [ "${rc}" -eq 0 ]; then
            rm -f "${err_file}"
            return 0
        fi
        log_warn "[fetch_release] git archive 失败（rc=${rc}），回退 rsync。"
        sed -n '1,20p' "${err_file}" 2>/dev/null | while IFS= read -r line; do
            [ -n "${line}" ] && log_warn "git archive stderr: ${line}"
        done
        rm -f "${err_file}"
    fi
    rsync_repo_to_release "${src}" "${dst}"
}

detect_repo_source_dir() {
    local candidate
    for candidate in \
        "${LUMEN_REPO_DIR:-}" \
        "${LUMEN_SOURCE_ROOT:-}" \
        "${SCRIPT_ROOT}" \
        "/root/Lumen" \
        "/opt/Lumen"; do
        [ -n "${candidate}" ] || continue
        [ -f "${candidate}/docker-compose.yml" ] || continue
        if [ -n "${LUMEN_REPO_DIR:-}" ] && [ "${candidate}" = "${LUMEN_REPO_DIR}" ]; then
            printf '%s' "${candidate}"
            return 0
        fi
        if [ -d "${candidate}/.git" ]; then
            printf '%s' "${candidate}"
            return 0
        fi
    done
    return 1
}

# 探测 GHCR 上 tag 是否存在。在没有 token 的情况下只能尽力 HEAD：
# 失败时 warn 但不 abort（pull_images 阶段会真实暴露问题）。
probe_ghcr_tag() {
    local image="$1"  # e.g. ghcr.io/cyeinfpro/lumen-api
    local tag="$2"
    local manifest_url="https://ghcr.io/v2/${image#ghcr.io/}/manifests/${tag}"
    if ! command -v curl >/dev/null 2>&1; then
        return 0
    fi
    # 先拿匿名 token（GHCR 公开包流程：/token?scope=repository:<image>:pull）
    local token resp http_code
    token="$(curl -fsSL "https://ghcr.io/token?scope=repository:${image#ghcr.io/}:pull" 2>/dev/null \
        | sed -nE 's/.*"token"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' | head -n1)"
    if [ -z "${token}" ]; then
        return 0
    fi
    http_code="$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${token}" \
        -H "Accept: application/vnd.oci.image.manifest.v1+json" \
        -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
        "${manifest_url}" 2>/dev/null || echo "000")"
    case "${http_code}" in
        2??) return 0 ;;
        404)
            log_error "GHCR 上未找到镜像：${image}:${tag}"
            return 1
            ;;
        *)
            log_warn "GHCR 探测 ${image}:${tag} 返回 HTTP ${http_code}，跳过严格校验，由 pull 阶段兜底。"
            return 0
            ;;
    esac
}

# Trap：任何未结束 phase 收口为 fail，并尝试切回旧 release。
on_err() {
    local rc="$?"
    [ "${rc}" -eq 0 ] && return 0
    lumen_step_finalize_failure "${rc}"
    log_error "更新失败：返回码 ${rc}"
    if [ "${ROLLBACK_DONE}" -eq 0 ]; then
        ROLLBACK_DONE=1
        if [ "${SWITCHED}" -eq 1 ] && [ -n "${CURRENT_ID:-}" ]; then
            # 切回前先验证 previous release 还在；不在的话拒绝盲切（手动恢复）
            if [ ! -d "${ROOT}/releases/${CURRENT_ID}" ]; then
                log_error "rollback：previous release ${CURRENT_ID} 目录不存在，拒绝盲切。请手动恢复："
                log_error "  ls ${ROOT}/releases/  # 找到合法 release id"
                log_error "  ln -sfn releases/<id> ${ROOT}/current"
            elif lumen_release_atomic_switch "${ROOT}" "${CURRENT_ID}"; then
                log_warn "rollback：current 已切回 ${CURRENT_ID}（业务容器仍是新版本，建议人工 docker compose up -d）"
            else
                log_error "rollback：current 切回 ${CURRENT_ID} 失败，请手动："
                log_error "  ln -sfn releases/${CURRENT_ID} ${ROOT}/current"
            fi
        elif [ -n "${NEW_RELEASE}" ] && [ -d "${NEW_RELEASE}" ]; then
            log_warn "rollback：删除未启用的 release ${NEW_ID}"
            if ! lumen_release_remove_unused "${ROOT}" "${NEW_ID}"; then
                log_warn "  release 删除失败，请手动 sudo rm -rf '${NEW_RELEASE}'"
            fi
        fi
    fi
    exit "${rc}"
}
trap 'on_err' ERR
trap 'rc=$?; [ "$rc" -ne 0 ] && on_err || true; lumen_release_lock' EXIT

# ---------------------------------------------------------------------------
# 主流程封装：用 lumen_with_lock 套一层
# ---------------------------------------------------------------------------
do_update() {

# ---------------------------------------------------------------------------
# Phase: lock
# ---------------------------------------------------------------------------
emit_start lock
emit_info lock operation_id "${OPERATION_ID}"
emit_done  lock 0

# ---------------------------------------------------------------------------
# Phase: check
# 解析当前 / 目标 LUMEN_IMAGE_TAG，相同则直接跳到 cleanup。
# ---------------------------------------------------------------------------
emit_start check

# 解析 current release（容忍首次部署时 current 不存在）
CURRENT_RELEASE=""
CURRENT_ID=""
if [ -L "${ROOT}/current" ]; then
    CURRENT_RELEASE="$(lumen_release_current_path "${ROOT}" || true)"
    [ -n "${CURRENT_RELEASE}" ] && CURRENT_ID="$(basename "${CURRENT_RELEASE}")"
fi

# 确保 shared/.env 至少存在（用 lib.sh 的 helper）
if ! lumen_release_ensure_shared_env "${ROOT}"; then
    emit_info check reason "missing_shared_env"
    log_error "[check] shared/.env 不可用，无法继续。"
    log_error "[check] 当前检查目录：${ROOT}"
    if [ "${ROOT}" != "${LUMEN_DEPLOY_ROOT}" ] && [ -f "${LUMEN_DEPLOY_ROOT}/shared/.env" ]; then
        log_error "[check] 发现 ${LUMEN_DEPLOY_ROOT}/shared/.env；请执行：LUMEN_UPDATE_ROOT=${LUMEN_DEPLOY_ROOT} bash ${SCRIPT_DIR}/update.sh"
    else
        log_error "[check] 如果还没完整安装，请先执行安装；已安装实例请从部署目录的 current/scripts/lumenctl.sh 执行更新。"
    fi
    emit_fail check 1
    exit 1
fi

# 当前 tag 与 channel
CURRENT_TAG="$(lumen_env_value LUMEN_IMAGE_TAG "${SHARED_ENV}" 2>/dev/null || echo "")"
PREVIOUS_TAG="${CURRENT_TAG}"
LUMEN_UPDATE_CHANNEL="$(lumen_env_value LUMEN_UPDATE_CHANNEL "${SHARED_ENV}" 2>/dev/null || echo "")"
[ -n "${LUMEN_UPDATE_CHANNEL}" ] || LUMEN_UPDATE_CHANNEL="stable"

# 统一代理来源：支持 shared/.env 里的 LUMEN_UPDATE_PROXY_URL / LUMEN_HTTP_PROXY，
# 也兼容面板触发时透传进来的 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。
LUMEN_PROXY_URL=""
if lumen_configure_proxy_env "${SHARED_ENV}" >/dev/null 2>&1; then
    LUMEN_PROXY_URL="${LUMEN_UPDATE_PROXY_URL:-${LUMEN_HTTP_PROXY:-}}"
fi

CONFIG_CHANGED=0
CURRENT_WEB_BIND_HOST="$(lumen_env_value WEB_BIND_HOST "${SHARED_ENV}" 2>/dev/null || echo "")"
if [ -n "${LUMEN_WEB_BIND_HOST:-}" ]; then
    if [ "${CURRENT_WEB_BIND_HOST}" != "${LUMEN_WEB_BIND_HOST}" ]; then
        lumen_set_env_value_in_file "${SHARED_ENV}" WEB_BIND_HOST "${LUMEN_WEB_BIND_HOST}"
        CURRENT_WEB_BIND_HOST="${LUMEN_WEB_BIND_HOST}"
        CONFIG_CHANGED=1
    fi
elif [ -z "${CURRENT_WEB_BIND_HOST}" ] || [ "${CURRENT_WEB_BIND_HOST}" = "127.0.0.1" ]; then
    log_info "[check] WEB_BIND_HOST 仍是旧默认 ${CURRENT_WEB_BIND_HOST:-<unset>}，自动改为 0.0.0.0 暴露宿主机 3000。"
    lumen_set_env_value_in_file "${SHARED_ENV}" WEB_BIND_HOST "0.0.0.0"
    CURRENT_WEB_BIND_HOST="0.0.0.0"
    CONFIG_CHANGED=1
fi

# 解析目标 tag
TARGET_TAG="$(lumen_image_tag_resolve "${LUMEN_UPDATE_CHANNEL}" "${SHARED_ENV}" 2>/dev/null || echo "")"
if [ -z "${TARGET_TAG}" ]; then
    emit_info check reason "target_tag_empty"
    log_error "[check] 无法解析目标 tag（channel=${LUMEN_UPDATE_CHANNEL}）。"
    log_error "[check] 可临时执行：LUMEN_UPDATE_CHANNEL=main bash ${SCRIPT_DIR}/update.sh"
    emit_fail check 1
    exit 1
fi

emit_info check channel       "${LUMEN_UPDATE_CHANNEL}"
emit_info check current_tag   "${CURRENT_TAG:-<none>}"
emit_info check target_tag    "${TARGET_TAG}"
emit_info check current_id    "${CURRENT_ID:-<none>}"
emit_info check web_bind_host "${CURRENT_WEB_BIND_HOST:-<default>}"
if [ -n "${LUMEN_PROXY_URL}" ]; then
    emit_info check proxy "configured"
fi

if [ -n "${CURRENT_TAG}" ] && [ "${CURRENT_TAG}" = "${TARGET_TAG}" ] && [ "${CONFIG_CHANGED}" -eq 0 ]; then
    log_info "[check] 当前 tag ${CURRENT_TAG} 已是目标版本，跳过中间阶段，仅做 cleanup。"
    emit_info check action "noop_already_latest"
    emit_done  check 0
    SKIP_TO_CLEANUP=1
else
    if [ -n "${CURRENT_TAG}" ] && [ "${CURRENT_TAG}" = "${TARGET_TAG}" ] && [ "${CONFIG_CHANGED}" -eq 1 ]; then
        log_info "[check] 当前 tag ${CURRENT_TAG} 已是目标版本，但配置已变更，继续重建 release 并重启服务。"
        emit_info check action "config_changed_redeploy"
    fi
    SKIP_TO_CLEANUP=0
    emit_done check 0
fi

if [ "${SKIP_TO_CLEANUP}" -eq 1 ]; then
    # 直接跳到 cleanup
    emit_start cleanup
    emit_info cleanup action "noop"
    emit_done  cleanup 0
    return 0
fi

# ---------------------------------------------------------------------------
# Phase: preflight
# ---------------------------------------------------------------------------
emit_start preflight

# Docker / docker compose 可用
lumen_require_docker_access

# 磁盘 ≥ 5GB
DISK_FREE_GB="$(disk_free_gb_opt)"
emit_info preflight disk_free_gb "${DISK_FREE_GB}"
if [ "${DISK_FREE_GB}" != "-1" ] && [ "${DISK_FREE_GB}" -lt 5 ]; then
    log_error "[preflight] /opt 可用磁盘 ${DISK_FREE_GB}GB < 5GB，请先清理。"
    emit_fail preflight 1
    exit 1
fi

# .env 关键字段
ENV_MISSING=0
for k in DATABASE_URL REDIS_URL SESSION_SECRET; do
    if ! env_key_present "${SHARED_ENV}" "${k}"; then
        log_error "[preflight] shared/.env 缺少 ${k} 或为空。"
        ENV_MISSING=1
    fi
done
if [ "${ENV_MISSING}" -eq 1 ]; then
    emit_fail preflight 1
    exit 1
fi

# /opt/lumendata 目录与权限
if ! check_data_owners; then
    log_error "[preflight] ${LUMEN_DATA_ROOT} 子目录不齐全，请先跑 install.sh。"
    emit_fail preflight 1
    exit 1
fi

emit_done preflight 0

# ---------------------------------------------------------------------------
# Phase: backup_preflight
# 默认强制；只有 LUMEN_UPDATE_SKIP_BACKUP=1 才跳过。失败 abort。
# ---------------------------------------------------------------------------
emit_start backup_preflight

if [ "${LUMEN_UPDATE_SKIP_BACKUP:-0}" = "1" ]; then
    log_warn "[backup_preflight] LUMEN_UPDATE_SKIP_BACKUP=1，跳过备份（强烈不推荐）。"
    emit_warn backup_preflight "skipped_by_env"
    emit_done backup_preflight 0
else
    BACKUP_SCRIPT=""
    if [ -x "${SCRIPT_DIR}/backup.sh" ]; then
        BACKUP_SCRIPT="${SCRIPT_DIR}/backup.sh"
    elif [ -n "${CURRENT_RELEASE}" ] && [ -x "${CURRENT_RELEASE}/scripts/backup.sh" ]; then
        BACKUP_SCRIPT="${CURRENT_RELEASE}/scripts/backup.sh"
    fi
    if [ -z "${BACKUP_SCRIPT}" ]; then
        log_error "[backup_preflight] 找不到 backup.sh，无法生成备份；如需跳过，显式 export LUMEN_UPDATE_SKIP_BACKUP=1（不推荐）。"
        emit_fail backup_preflight 1
        exit 1
    fi
    log_info "[backup_preflight] 调用 ${BACKUP_SCRIPT}（BACKUP_ROOT=${UPDATE_LOG_DIR}）"
    # LUMEN_BACKUP_FORCE=1：跳过 backup.sh 内的维护锁 try-acquire（本进程已持有同一把维护锁）。
    if ! LUMEN_ENV_FILE="${SHARED_ENV}" LUMEN_BACKUP_ROOT="${UPDATE_LOG_DIR}" BACKUP_ROOT="${UPDATE_LOG_DIR}" \
            LUMEN_BACKUP_FORCE=1 \
            DB_USER="$(lumen_env_value DB_USER "${SHARED_ENV}")" \
            DB_NAME="$(lumen_env_value DB_NAME "${SHARED_ENV}")" \
            REDIS_PASSWORD="$(lumen_env_value REDIS_PASSWORD "${SHARED_ENV}")" \
            bash "${BACKUP_SCRIPT}"; then
        emit_info backup_preflight backup_script "${BACKUP_SCRIPT}"
        log_error "[backup_preflight] 备份失败 → abort（不允许无备份继续，4K 任务环境死规则）。"
        log_error "[backup_preflight] 已使用 env 文件：${SHARED_ENV}"
        log_error "[backup_preflight] 请查看上方 backup 日志中的 pg_dump/redis 具体错误。"
        emit_fail backup_preflight 1
        exit 1
    fi
    emit_done backup_preflight 0
fi

# ---------------------------------------------------------------------------
# Phase: fetch_release
# 可选 git pull（LUMEN_UPDATE_GIT_PULL=1）。新建 release 目录 + rsync 仓库快照。
# ---------------------------------------------------------------------------
emit_start fetch_release

# 发布物来源目录：
#   - LUMEN_REPO_DIR / LUMEN_SOURCE_ROOT 显式指定时优先采用；
#   - 当前脚本或 /root/Lumen 来自完整 git 仓库时，优先从该仓库复制（让脚本/compose 修复进入新 release）；
#   - 标准 release 布局下从 current release 复制，确保新 release 根部有 docker-compose.yml；
#   - 旧 in-place / 开发仓库下才从 ROOT 复制。
if REPO_SOURCE="$(detect_repo_source_dir 2>/dev/null || true)" && [ -n "${REPO_SOURCE}" ]; then
    REPO_DIR="${REPO_SOURCE}"
elif [ -n "${CURRENT_RELEASE}" ] && [ -d "${CURRENT_RELEASE}" ]; then
    REPO_DIR="${CURRENT_RELEASE}"
else
    REPO_DIR="${ROOT}"
fi
emit_info fetch_release repo_dir "${REPO_DIR}"

if [ "${LUMEN_UPDATE_GIT_PULL:-0}" = "1" ]; then
    if ! command -v git >/dev/null 2>&1; then
        log_error "[fetch_release] LUMEN_UPDATE_GIT_PULL=1 但缺少 git。"
        emit_fail fetch_release 1
        exit 1
    fi
    if [ ! -d "${REPO_DIR}/.git" ]; then
        log_warn "[fetch_release] LUMEN_UPDATE_GIT_PULL=1 但 ${REPO_DIR} 不是 git 仓库；使用当前发布物快照继续。"
    else
        GIT_REF="${LUMEN_UPDATE_GIT_REF:-}"
        log_info "[fetch_release] git fetch in ${REPO_DIR}"
        if ! ( cd "${REPO_DIR}" && git fetch --quiet --all --prune ); then
            log_error "[fetch_release] git fetch 失败。"
            emit_fail fetch_release 1
            exit 1
        fi
        if [ -n "${GIT_REF}" ]; then
            if ! ( cd "${REPO_DIR}" && git checkout --quiet "${GIT_REF}" ); then
                log_error "[fetch_release] git checkout ${GIT_REF} 失败。"
                emit_fail fetch_release 1
                exit 1
            fi
        else
            # 默认 fast-forward 当前分支
            local local_branch
            local_branch="$(cd "${REPO_DIR}" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
            if [ -n "${local_branch}" ] && [ "${local_branch}" != "HEAD" ]; then
                ( cd "${REPO_DIR}" && git pull --ff-only --quiet ) || \
                    log_warn "[fetch_release] git pull --ff-only 失败（可能已 detached），忽略。"
            fi
        fi
    fi
fi

# 新 release id + 目录
NEW_ID="releases-$(date -u +%Y%m%d-%H%M%S)"
NEW_RELEASE="${ROOT}/releases/${NEW_ID}"
mkdir -p "${ROOT}/releases" "${ROOT}/shared"
if [ -e "${NEW_RELEASE}" ]; then
    log_error "[fetch_release] 目标 release 目录已存在：${NEW_RELEASE}"
    emit_fail fetch_release 1
    exit 1
fi
mkdir -p "${NEW_RELEASE}"
emit_info fetch_release release_id   "${NEW_ID}"
emit_info fetch_release release_path "${NEW_RELEASE}"

log_info "[fetch_release] 同步发布物 ${REPO_DIR} -> ${NEW_RELEASE}"
if ! sync_repo_to_release "${REPO_DIR}" "${NEW_RELEASE}"; then
    log_error "[fetch_release] 同步仓库到 release 失败。"
    emit_fail fetch_release 1
    exit 1
fi

# 把 shared/.env 软链回 release 根（让 docker compose --env-file 默认行为生效）
mkdir -p "${SHARED_DIR}"
if [ -e "${NEW_RELEASE}/.env" ] && [ ! -L "${NEW_RELEASE}/.env" ]; then
    mv "${NEW_RELEASE}/.env" "${NEW_RELEASE}/.env.pre-link.$(date -u +%Y%m%d%H%M%S)" 2>/dev/null || \
        rm -f "${NEW_RELEASE}/.env" 2>/dev/null || true
fi
ln -sfn "${SHARED_ENV}" "${NEW_RELEASE}/.env"

# 探测 GHCR 上 tag 是否真的存在（lumen-api 作为代表）
LUMEN_IMAGE_REGISTRY="$(lumen_env_value LUMEN_IMAGE_REGISTRY "${SHARED_ENV}" 2>/dev/null || echo "")"
[ -n "${LUMEN_IMAGE_REGISTRY}" ] || LUMEN_IMAGE_REGISTRY="ghcr.io/cyeinfpro"
if ! probe_ghcr_tag "${LUMEN_IMAGE_REGISTRY}/lumen-api" "${TARGET_TAG}"; then
    if [ "${TARGET_TAG}" != "main" ] && [ "${LUMEN_UPDATE_FALLBACK_MAIN:-1}" = "1" ]; then
        log_warn "[fetch_release] 目标镜像 tag=${TARGET_TAG} 不存在，自动回退到 main。"
        emit_info fetch_release target_tag_fallback "main"
        TARGET_TAG="main"
        if ! probe_ghcr_tag "${LUMEN_IMAGE_REGISTRY}/lumen-api" "${TARGET_TAG}"; then
            log_error "[fetch_release] fallback main 镜像也不存在：${LUMEN_IMAGE_REGISTRY}/lumen-api:${TARGET_TAG}"
            emit_fail fetch_release 1
            exit 1
        fi
    else
        log_error "[fetch_release] 目标镜像不存在：${LUMEN_IMAGE_REGISTRY}/lumen-api:${TARGET_TAG}"
        log_error "[fetch_release] 可临时执行：LUMEN_UPDATE_CHANNEL=main bash ${SCRIPT_DIR}/update.sh"
        emit_fail fetch_release 1
        exit 1
    fi
fi

emit_done fetch_release 0

# ---------------------------------------------------------------------------
# Phase: set_image_tag
# 把 TARGET_TAG 写入 shared/.env 的 LUMEN_IMAGE_TAG 行（唯一）。
# 同时把 tag 写入 releases/<id>/.image-tag 作为回滚锚点（§18.1）。
# ---------------------------------------------------------------------------
emit_start set_image_tag

if ! lumen_set_image_tag_in_env "${SHARED_ENV}" "${TARGET_TAG}"; then
    log_error "[set_image_tag] 写入 shared/.env 失败。"
    emit_fail set_image_tag 1
    exit 1
fi

# 防御：再次 grep 校验 ==1
TAG_LINE_CNT="$(grep -cE '^LUMEN_IMAGE_TAG=' "${SHARED_ENV}" 2>/dev/null || echo 0)"
if [ "${TAG_LINE_CNT}" != "1" ]; then
    log_error "[set_image_tag] 校验失败：shared/.env 中 LUMEN_IMAGE_TAG 行数=${TAG_LINE_CNT}（期望 1）。"
    emit_fail set_image_tag 1
    exit 1
fi

# 把 target tag 落到 release 目录的 .image-tag（回滚定位）
printf '%s\n' "${TARGET_TAG}" > "${NEW_RELEASE}/.image-tag" 2>/dev/null || true

emit_info set_image_tag tag "${TARGET_TAG}"
emit_done set_image_tag 0

# ---------------------------------------------------------------------------
# 兜底：LUMEN_UPDATE_BUILD=1 → 在 pull 之前先 build（不与 pull 互斥）。
# ---------------------------------------------------------------------------
if [ "${LUMEN_UPDATE_BUILD:-0}" = "1" ]; then
    emit_start pull_images   # build 兜底复用 pull_images 阶段，便于后台进度兼容。
    # 为清晰起见，单独发一个 info 行
    emit_info pull_images action "build_images"
    log_info "[build_images] LUMEN_UPDATE_BUILD=1 → docker compose build api worker web"
    if ! lumen_compose_in "${NEW_RELEASE}" build api worker web; then
        log_error "[build_images] docker compose build 失败。"
        emit_fail pull_images 1
        exit 1
    fi
    # tgbot 可选
    if env_key_present "${SHARED_ENV}" "TELEGRAM_BOT_TOKEN"; then
        lumen_compose_in "${NEW_RELEASE}" build tgbot 2>/dev/null || \
            log_warn "[build_images] tgbot build 失败，已忽略。"
    fi
    emit_done pull_images 0
fi

if [ "${LUMEN_UPDATE_BUILD:-0}" != "1" ]; then
    # -----------------------------------------------------------------------
    # Phase: pull_images
    # -----------------------------------------------------------------------
    emit_start pull_images

    if [ -n "${LUMEN_PROXY_URL}" ]; then
        emit_info pull_images proxy "configured"
    fi

    # 网络抖动是 pull 失败最常见原因，先重试 3 次（指数退避 5/10/20s），仍失败再走 fallback。
    if ! lumen_retry 3 5 "docker compose pull tag=${TARGET_TAG}" \
            lumen_compose_in "${NEW_RELEASE}" pull; then
        if [ "${TARGET_TAG}" != "main" ] && [ "${LUMEN_UPDATE_FALLBACK_MAIN:-1}" = "1" ]; then
            log_warn "[pull_images] docker compose pull tag=${TARGET_TAG} 失败，自动回退到 main 后重试。"
            emit_info pull_images target_tag_fallback "main"
            TARGET_TAG="main"
            if ! lumen_set_image_tag_in_env "${SHARED_ENV}" "${TARGET_TAG}"; then
                log_error "[pull_images] 回退 main 时写入 shared/.env 失败。"
                emit_fail pull_images 1
                exit 1
            fi
            printf '%s\n' "${TARGET_TAG}" > "${NEW_RELEASE}/.image-tag" 2>/dev/null \
                || log_warn "[pull_images] .image-tag 写入失败（已忽略，仅影响事后定位）"
            if ! lumen_retry 2 5 "docker compose pull (main fallback)" \
                    lumen_compose_in "${NEW_RELEASE}" pull; then
                log_error "[pull_images] fallback main 后 docker compose pull 仍失败。"
                log_error "  请检查 GHCR 可达性或代理配置。"
                log_error "  当前服务保持不变。"
                emit_fail pull_images 1
                exit 1
            fi
        else
            log_error "[pull_images] docker compose pull 失败。"
            log_error "  请检查 GHCR 可达性或代理配置。"
            log_error "  当前服务保持不变。"
            emit_fail pull_images 1
            exit 1
        fi
    fi
    emit_info pull_images tag "${TARGET_TAG}"
    emit_done pull_images 0
else
    log_info "[pull_images] LUMEN_UPDATE_BUILD=1 已完成本地 build，跳过远程 pull。"
fi

# ---------------------------------------------------------------------------
# Phase: start_infra
# ---------------------------------------------------------------------------
emit_start start_infra

if ! lumen_compose_in "${NEW_RELEASE}" up -d --wait postgres redis; then
    log_error "[start_infra] postgres / redis 启动或健康检查失败。"
    log_error "  当前 API/Worker/Web 服务保持不变。"
    emit_fail start_infra 1
    exit 1
fi
emit_done start_infra 0

# ---------------------------------------------------------------------------
# Phase: migrate_db
# 死规则：失败 → abort，不切 current、不重启业务容器。
# ---------------------------------------------------------------------------
emit_start migrate_db

if ! lumen_compose_in "${NEW_RELEASE}" --profile migrate run --rm migrate; then
    log_error "[migrate_db] alembic upgrade 失败 → fail-fast。"
    log_error "  根据 §11.3 / §17.6：不切 current、不重启业务容器。"
    log_error "  旧服务继续跑旧 schema；请人工查 logs：docker compose logs --tail=120"
    emit_fail migrate_db 1
    exit 1
fi
emit_done migrate_db 0

# ---------------------------------------------------------------------------
# Phase: switch
# 把 current 软链切到 NEW_ID；旧 current 自动写入 previous。
# ---------------------------------------------------------------------------
emit_start switch

if ! lumen_release_atomic_switch "${ROOT}" "${NEW_ID}"; then
    log_error "[switch] symlink 切换失败。"
    emit_fail switch 1
    exit 1
fi
SWITCHED=1
emit_info switch from "${CURRENT_ID:-<none>}"
emit_info switch to   "${NEW_ID}"
emit_done switch 0

# ---------------------------------------------------------------------------
# Phase: restart_services
# 启动 api / worker / web；如启用 Telegram，则起 tgbot。
# 失败 → 自动用 PREVIOUS_TAG 回滚（pull && up）。
# ---------------------------------------------------------------------------
emit_start restart_services

CURRENT_LINK="${ROOT}/current"
RESTART_OK=0
if lumen_compose_in "${CURRENT_LINK}" up -d --wait api worker web; then
    RESTART_OK=1
else
    log_error "[restart_services] api/worker/web 启动失败，尝试自动回滚到上一已知好 tag：${PREVIOUS_TAG:-<none>}"
    emit_warn restart_services "starting_auto_rollback"
    # 事务化回滚：先备份新 tag、改 .env，pull/up 任一步失败就把 .env 恢复成新 tag，
    # 确保 SHARED_ENV 与 current symlink 状态一致（不会出现 .env 是旧 tag 但 current
    # 仍是新 release 的中间态）。
    ROLLBACK_OK=0
    if [ -n "${PREVIOUS_TAG}" ] && [ "${PREVIOUS_TAG}" != "${TARGET_TAG}" ]; then
        # 还要验证 PREVIOUS release 目录还在；缺失时回滚没意义，直接走手动恢复路径
        if [ -z "${CURRENT_ID:-}" ] || [ ! -d "${ROOT}/releases/${CURRENT_ID}" ]; then
            log_error "[restart_services] previous release 目录不存在（${ROOT}/releases/${CURRENT_ID:-<none>}），跳过自动回滚。"
        else
            if lumen_set_image_tag_in_env "${SHARED_ENV}" "${PREVIOUS_TAG}"; then
                if lumen_release_atomic_switch "${ROOT}" "${CURRENT_ID}" \
                    && lumen_compose_in "${CURRENT_LINK}" pull \
                    && lumen_compose_in "${CURRENT_LINK}" up -d --wait api worker web; then
                    SWITCHED=0  # current 已切回旧 release，on_err 不再重复切
                    log_warn "[restart_services] 已用 ${PREVIOUS_TAG} 回滚成功（current → ${CURRENT_ID}）；本次 update 视为失败。"
                    emit_info restart_services rolled_back_to "${PREVIOUS_TAG}"
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
                log_error "[restart_services] 改写 SHARED_ENV 到 ${PREVIOUS_TAG} 失败，跳过自动回滚。"
            fi
        fi
    fi
    if [ "${ROLLBACK_OK}" = "1" ]; then
        emit_fail restart_services 1
        exit 1
    fi
    log_error "[restart_services] 自动回滚失败 → 请按 §18 手动回滚："
    log_error "  ln -sfn releases/${CURRENT_ID:-<id>} ${ROOT}/current"
    log_error "  sed -i 's|^LUMEN_IMAGE_TAG=.*|LUMEN_IMAGE_TAG=${PREVIOUS_TAG:-<old-tag>}|' ${SHARED_ENV}"
    log_error "  COMPOSE_PROJECT_NAME=lumen docker compose pull && up -d --wait api worker web"
    emit_fail restart_services 1
    exit 1
fi

# tgbot：如果 .env 有 TELEGRAM_BOT_TOKEN 非空才起
if env_key_present "${SHARED_ENV}" "TELEGRAM_BOT_TOKEN"; then
    if ! lumen_compose_in "${CURRENT_LINK}" --profile tgbot up -d tgbot; then
        log_warn "[restart_services] tgbot 启动失败，已忽略（业务 API 不受影响）。"
        emit_warn restart_services "tgbot_failed_ignored"
    else
        emit_info restart_services tgbot "started"
    fi
fi

emit_done restart_services 0

# ---------------------------------------------------------------------------
# Phase: health_check
# HTTP healthz + Compose 状态。失败 → emit fail，不自动回滚（DB 已 migrate）。
# ---------------------------------------------------------------------------
emit_start health_check

API_HEALTH_URL="${LUMEN_API_HEALTH_URL:-http://127.0.0.1:8000/healthz}"
WEB_HEALTH_URL="${LUMEN_WEB_HEALTH_URL:-http://127.0.0.1:3000/}"

HEALTH_FAIL=0
if ! lumen_health_http "${API_HEALTH_URL}" 60 2; then
    log_error "[health_check] API ${API_HEALTH_URL} 不可达。"
    HEALTH_FAIL=1
fi
if ! lumen_health_http "${WEB_HEALTH_URL}" 60 2; then
    log_error "[health_check] Web ${WEB_HEALTH_URL} 不可达。"
    HEALTH_FAIL=1
fi
if ! lumen_health_compose api worker web; then
    log_error "[health_check] docker compose 状态检查失败。"
    HEALTH_FAIL=1
fi

if [ "${HEALTH_FAIL}" -eq 1 ]; then
    log_error "[health_check] 健康检查失败；新代码已上线但状态异常。"
    log_error "  数据库迁移已应用，**不自动回滚**——请执行："
    log_error "    docker compose logs --tail=120 api"
    log_error "    docker compose ps"
    log_error "  如需回滚，参考 docs/.. §18。"
    emit_fail health_check 1
    exit 1
fi
emit_done health_check 0

# ---------------------------------------------------------------------------
# Phase: cleanup
# docker image prune（仅 dangling）+ 旧 release 清理（保留 N=3）。
# 失败不阻断成功。
# ---------------------------------------------------------------------------
emit_start cleanup

# dangling-only prune；--filter "until=24h" 限制 24h 前的，避免误删本次 untag 的旧版
if ! lumen_docker image prune -f --filter "until=24h" >/dev/null 2>&1; then
    log_warn "[cleanup] docker image prune 失败（已忽略）。"
fi

if ! lumen_release_cleanup_old "${ROOT}" "${LUMEN_RELEASE_KEEP:-3}"; then
    log_warn "[cleanup] 旧 release 清理失败（已忽略）。"
fi

emit_done cleanup 0

# ---------------------------------------------------------------------------
# 收尾
# ---------------------------------------------------------------------------
log_step "更新完成"
log_info "release ${NEW_ID} 已上线（previous: ${CURRENT_ID:-<none>}, tag: ${TARGET_TAG}）"
log_info "  API:    ${API_HEALTH_URL}"
log_info "  Web:    ${WEB_HEALTH_URL}"

return 0
}  # end do_update

# ---------------------------------------------------------------------------
# 入口：双层锁
#   - 维护锁（${ROOT}/.lumen-maintenance.lock）：与 install.sh / uninstall.sh 互斥
#   - 全局更新锁（${LUMEN_BACKUP_ROOT}/.lumen-update.lock）：与并发 update 互斥
# ---------------------------------------------------------------------------
lumen_acquire_lock "${ROOT}" "update.sh"

if lumen_with_lock "update" 1830 do_update; then
    # 解 trap，让 EXIT 只做 lock 释放
    trap - ERR
    trap 'lumen_release_lock' EXIT
    exit 0
fi

exit 1
