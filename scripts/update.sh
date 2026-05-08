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
_LUMEN_UPDATE_INPUT_DATA_ROOT="${LUMEN_DATA_ROOT-}"
_LUMEN_UPDATE_INPUT_DB_ROOT="${LUMEN_DB_ROOT-}"
_LUMEN_UPDATE_INPUT_BACKUP_ROOT="${LUMEN_BACKUP_ROOT-}"
_LUMEN_UPDATE_INPUT_APP_UID="${LUMEN_APP_UID-}"
_LUMEN_UPDATE_INPUT_APP_GID="${LUMEN_APP_GID-}"
_LUMEN_UPDATE_INPUT_APP_STORAGE_GID="${LUMEN_APP_STORAGE_GID-}"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

# ---------------------------------------------------------------------------
# lib.sh 提供的 helpers 全部在 source 后可用：lumen_emit_step / lumen_emit_info /
# lumen_compose / lumen_compose_in / lumen_health_http / lumen_health_compose /
# lumen_image_tag_resolve / lumen_image_tag_is_rolling / lumen_set_image_tag_in_env
# / lumen_with_lock。之前这里有 ~200 行 `if ! command -v X` 兜底 shim，已删除：
# update.sh 依赖 line 31 强制 source lib.sh，shim 是 dead code。如未来 lib.sh
# 删除或重命名 helper，update.sh 会立即报 "command not found" 并被 CI 测试拦住，
# 比静默 fallback 到不一致语义的 shim 更可靠。
# ---------------------------------------------------------------------------


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
shared_data_root=""
shared_db_root=""
shared_backup_root=""
shared_app_uid=""
shared_app_gid=""
shared_app_storage_gid=""
if [ -f "${SHARED_ENV}" ]; then
    shared_data_root="$(lumen_env_value LUMEN_DATA_ROOT "${SHARED_ENV}" 2>/dev/null || true)"
    shared_db_root="$(lumen_env_value LUMEN_DB_ROOT "${SHARED_ENV}" 2>/dev/null || true)"
    shared_backup_root="$(lumen_env_value LUMEN_BACKUP_ROOT "${SHARED_ENV}" 2>/dev/null || true)"
    shared_backup_root="${shared_backup_root:-$(lumen_env_value BACKUP_ROOT "${SHARED_ENV}" 2>/dev/null || true)}"
    shared_app_uid="$(lumen_env_value LUMEN_APP_UID "${SHARED_ENV}" 2>/dev/null || true)"
    shared_app_gid="$(lumen_env_value LUMEN_APP_GID "${SHARED_ENV}" 2>/dev/null || true)"
    shared_app_storage_gid="$(lumen_env_value LUMEN_APP_STORAGE_GID "${SHARED_ENV}" 2>/dev/null || true)"
fi
LUMEN_DATA_ROOT="${_LUMEN_UPDATE_INPUT_DATA_ROOT:-${shared_data_root:-/opt/lumendata}}"
LUMEN_DB_ROOT="${_LUMEN_UPDATE_INPUT_DB_ROOT:-${shared_db_root:-${LUMEN_DATA_ROOT}}}"
LUMEN_BACKUP_ROOT="${_LUMEN_UPDATE_INPUT_BACKUP_ROOT:-${shared_backup_root:-${LUMEN_DATA_ROOT}/backup}}"
LUMEN_APP_UID="${_LUMEN_UPDATE_INPUT_APP_UID:-${shared_app_uid:-10001}}"
LUMEN_APP_GID="${_LUMEN_UPDATE_INPUT_APP_GID:-${shared_app_gid:-10001}}"
LUMEN_APP_STORAGE_GID="${_LUMEN_UPDATE_INPUT_APP_STORAGE_GID:-${shared_app_storage_gid:-${LUMEN_APP_GID}}}"
export LUMEN_DATA_ROOT LUMEN_DB_ROOT LUMEN_BACKUP_ROOT LUMEN_APP_UID LUMEN_APP_GID LUMEN_APP_STORAGE_GID
UPDATE_LOG_DIR="${LUMEN_BACKUP_ROOT}"
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
# 用单变量而非 declare -A 关联数组，兼容 macOS bash 3.2（CI smoke runner）。
# update.sh 的 emit_start/done 调用是顺序成对的（不交叉），单 LAST_PHASE
# 足够；emit_fail 顺势清空。
_UPDATE_LAST_PHASE=""
_UPDATE_LAST_PHASE_START_TS=""
emit_start() {
    local _phase="$1"
    _UPDATE_LAST_PHASE="${_phase}"
    _UPDATE_LAST_PHASE_START_TS="$(date +%s 2>/dev/null || echo 0)"
    lumen_emit_step "phase=${_phase}" "status=start"
}
emit_done()  {
    local _phase="$1" _rc="${2:-0}"
    local _dur_arg=""
    if [ "${_UPDATE_LAST_PHASE}" = "${_phase}" ] \
            && [ -n "${_UPDATE_LAST_PHASE_START_TS}" ] \
            && [ "${_UPDATE_LAST_PHASE_START_TS}" -gt 0 ] 2>/dev/null; then
        local _end _dur
        _end="$(date +%s 2>/dev/null || echo 0)"
        _dur=$((_end - _UPDATE_LAST_PHASE_START_TS))
        if [ "${_dur}" -ge 0 ]; then
            log_info "  ✓ ${_phase} 完成（耗时 ${_dur}s）"
            _dur_arg="dur_ms=$((_dur * 1000))"
        fi
        _UPDATE_LAST_PHASE=""
        _UPDATE_LAST_PHASE_START_TS=""
    fi
    lumen_emit_step "phase=${_phase}" "status=done" "rc=${_rc}" ${_dur_arg:+"${_dur_arg}"}
}
emit_fail()  {
    local _phase="$1" _rc="${2:-1}"
    if [ "${_UPDATE_LAST_PHASE}" = "${_phase}" ]; then
        _UPDATE_LAST_PHASE=""
        _UPDATE_LAST_PHASE_START_TS=""
    fi
    lumen_emit_step "phase=${_phase}" "status=fail" "rc=${_rc}"
}
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

# 校验数据目录属主：postgres=999, redis=999, storage/backup 对齐应用 storage gid。
# v1.0.48 起 postgres 容器换到 pgvector/pgvector:pg16（Debian, uid=999）。
# 老老 alpine 镜像 postgres uid=70 的数据目录会在 migrate_postgres_uid 阶段
# 一次性 chown 70 → 999；这里仍仅 warn 兜底。
check_data_owners() {
    local missing=0
    local path
    for path in \
        "${LUMEN_DB_ROOT}/postgres" \
        "${LUMEN_DB_ROOT}/redis" \
        "${LUMEN_DATA_ROOT}/storage" \
        "${LUMEN_DATA_ROOT}/backup"; do
        if [ ! -d "${path}" ]; then
            log_error "缺少数据目录：${path}"
            missing=1
        fi
    done
    if [ "${missing}" -eq 1 ]; then
        return 1
    fi
    # 仅做 warn，不阻断（install.sh 是 single source of truth）
    local uid gid
    if command -v stat >/dev/null 2>&1; then
        uid="$(stat -c '%u' "${LUMEN_DB_ROOT}/postgres" 2>/dev/null || stat -f '%u' "${LUMEN_DB_ROOT}/postgres" 2>/dev/null || echo "")"
        [ -n "${uid}" ] && [ "${uid}" != "999" ] && log_warn "${LUMEN_DB_ROOT}/postgres 属主非 999（实际 ${uid}），postgres 容器可能起不来。"
        uid="$(stat -c '%u' "${LUMEN_DB_ROOT}/redis" 2>/dev/null || stat -f '%u' "${LUMEN_DB_ROOT}/redis" 2>/dev/null || echo "")"
        [ -n "${uid}" ] && [ "${uid}" != "999" ] && log_warn "${LUMEN_DB_ROOT}/redis 属主非 999（实际 ${uid}），redis 容器可能起不来。"
        gid="$(stat -c '%g' "${LUMEN_DATA_ROOT}/storage" 2>/dev/null || stat -f '%g' "${LUMEN_DATA_ROOT}/storage" 2>/dev/null || echo "")"
        [ -n "${gid}" ] && [ "${gid}" != "${LUMEN_APP_STORAGE_GID}" ] && log_warn "${LUMEN_DATA_ROOT}/storage 属组非 ${LUMEN_APP_STORAGE_GID}（实际 ${gid}），api/worker 可能写不进去。"
        gid="$(stat -c '%g' "${LUMEN_DATA_ROOT}/backup" 2>/dev/null || stat -f '%g' "${LUMEN_DATA_ROOT}/backup" 2>/dev/null || echo "")"
        [ -n "${gid}" ] && [ "${gid}" != "${LUMEN_APP_STORAGE_GID}" ] && log_warn "${LUMEN_DATA_ROOT}/backup 属组非 ${LUMEN_APP_STORAGE_GID}（实际 ${gid}），备份/日志可能写不进去。"
    fi
    return 0
}

# v1.0.48: postgres 镜像从 alpine (uid=70) 切到 pgvector/pgvector:pg16 (uid=999).
# 老 install 在数据目录写过 owner=70 的文件,新容器 uid=999 启动会 EACCES.
# 这个 helper 检测属主, 仅在 ≠ 999 时 chown 一次, idempotent.
migrate_postgres_uid() {
    local pg_dir="${LUMEN_DB_ROOT}/postgres"
    if [ ! -d "${pg_dir}" ]; then
        return 0
    fi
    local current_uid=""
    if command -v stat >/dev/null 2>&1; then
        current_uid="$(stat -c '%u' "${pg_dir}" 2>/dev/null || stat -f '%u' "${pg_dir}" 2>/dev/null || echo "")"
    fi
    if [ -z "${current_uid}" ] || [ "${current_uid}" = "999" ]; then
        return 0
    fi
    log_info "[migrate_postgres_uid] ${pg_dir} 属主 ${current_uid} → 999 (pgvector 镜像 postgres uid)"
    if lumen_run_as_root chown -R 999:999 "${pg_dir}"; then
        log_info "[migrate_postgres_uid] chown 完成"
        return 0
    fi
    log_error "[migrate_postgres_uid] chown 失败,postgres 容器可能起不来"
    return 1
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
    local token http_code
    token="$(curl -fsSL "https://ghcr.io/token?scope=repository:${image#ghcr.io/}:pull" 2>/dev/null \
        | sed -nE 's/.*"token"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' | head -n1)"
    if [ -z "${token}" ]; then
        return 0
    fi
    # 多架构 buildx 推上来的 tag 在 GHCR 是 OCI Image Index / manifest list，
    # 不带 list/index 的 mediaType 会被 registry 当成 unknown manifest 返回 404，
    # 导致 update.sh 误判镜像不存在并回退到本地 build。所以四种 mediaType 全列上。
    http_code="$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${token}" \
        -H "Accept: application/vnd.oci.image.index.v1+json" \
        -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
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
UPDATE_ERROR_HANDLED=0
on_err() {
    local rc="${1:-1}"
    [ "${rc}" -eq 0 ] && return 0
    if [ "${UPDATE_ERROR_HANDLED}" -eq 1 ]; then
        return 0
    fi
    UPDATE_ERROR_HANDLED=1
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
    return 0
}
# ERR trap：只调 on_err 标记/回滚，不再 exit —— bash 默认会让出错命令的非零 rc
# 透传到 EXIT trap，避免双 exit 与 EXIT 内 lumen_release_lock 的执行顺序竞态。
trap 'rc=$?; on_err "$rc"' ERR
trap 'rc=$?; [ "$rc" -ne 0 ] && on_err "$rc" || true; lumen_release_lock' EXIT

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

# CURRENT_RELEASE 提前到 self_update_scripts 前解析（check phase 内仍会重赋值，幂等）；
# 不放进 check phase 是为了 self_update_scripts 在 noop 判断之前就能拿到 release scripts 目录。
CURRENT_RELEASE=""
CURRENT_ID=""
if [ -L "${ROOT}/current" ]; then
    CURRENT_RELEASE="$(lumen_release_current_path "${ROOT}" || true)"
    [ -n "${CURRENT_RELEASE}" ] && CURRENT_ID="$(basename "${CURRENT_RELEASE}")"
fi

# ---------------------------------------------------------------------------
# Phase: self_update_scripts
# 从 GitHub 拉最新 scripts/ 替换 current release 里的对应文件，让 backup_preflight
# 等后续阶段直接用上仓库 main 的 bash 修复，避免"修一个 scripts/ bug 必须等下次
# update 才生效"的鸡蛋问题。
#
# 位置：必须在 check phase 之前 —— 否则当 current_tag == target_tag（pinned tag noop）
# 时 check 会 SKIP_TO_CLEANUP return，self_update_scripts 永远跑不到。
#
# 实现委托给 lib.sh 的 lumen_self_update_scripts（lumenctl 入口处也用同一个函数）。
# update 阶段用短 TTL（60s）：lumenctl 入口刚拉过会命中 marker 跳过，避免一次 update 调用
# 拉两次 GitHub；冷启动（admin systemd-run 直接跑 update.sh）会突破 TTL 拉一次。
# 只取 lib.sh / backup.sh / restore.sh / update.sh 四个（lumenctl.sh 已在 lumenctl 入口处更新过）。
# ---------------------------------------------------------------------------
emit_start self_update_scripts

if [ "${LUMEN_UPDATE_SELF_UPDATED:-0}" = "1" ]; then
    log_info "[self_update_scripts] 已通过 self-update re-exec 重入，跳过自身。"
    emit_done self_update_scripts 0
elif [ "${LUMEN_UPDATE_SELF_UPDATE_SCRIPTS:-1}" = "0" ]; then
    log_info "[self_update_scripts] 关闭（LUMEN_UPDATE_SELF_UPDATE_SCRIPTS=0）。"
    emit_done self_update_scripts 0
elif [ -z "${CURRENT_RELEASE}" ] || [ ! -d "${CURRENT_RELEASE}/scripts" ]; then
    log_info "[self_update_scripts] 不是 release 布局（CURRENT_RELEASE 为空），跳过。"
    emit_done self_update_scripts 0
else
    lumen_self_update_scripts \
        "${CURRENT_RELEASE}/scripts" \
        "${LUMEN_UPDATE_SCRIPTS_BRANCH:-main}" \
        60 \
        lib.sh backup.sh restore.sh update.sh
    case "${LUMEN_SELF_UPDATE_RESULT:-}" in
        ok)
            if [ -n "${LUMEN_SELF_UPDATE_CHANGED:-}" ]; then
                emit_info self_update_scripts source "${LUMEN_SELF_UPDATE_SOURCE}"
                emit_info self_update_scripts changed "${LUMEN_SELF_UPDATE_CHANGED}"
                emit_info self_update_scripts backup_suffix ".bak.${LUMEN_SELF_UPDATE_BACKUP_TS}"
                # update.sh 自己变化 → re-exec 新版
                case " ${LUMEN_SELF_UPDATE_CHANGED} " in
                    *" update.sh "*)
                        log_info "[self_update_scripts] update.sh 已变更，re-exec 新版（保留 OPERATION_ID）。"
                        emit_done self_update_scripts 0
                        export LUMEN_UPDATE_SELF_UPDATED=1
                        export OPERATION_ID
                        exec bash "${CURRENT_RELEASE}/scripts/update.sh" "$@"
                        ;;
                esac
            fi
            emit_done self_update_scripts 0
            ;;
        failed)
            emit_warn self_update_scripts "fetch_or_validate_failed_continue_with_local"
            emit_done self_update_scripts 0
            ;;
        disabled|skipped|*)
            emit_done self_update_scripts 0
            ;;
    esac
fi

# ---------------------------------------------------------------------------
# Phase: check
# 解析当前 / 目标 LUMEN_IMAGE_TAG，相同则直接跳到 cleanup。
# ---------------------------------------------------------------------------
emit_start check

# CURRENT_RELEASE / CURRENT_ID 已在 self_update_scripts phase 之前解析（line ~531）。

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
emit_info check data_root     "${LUMEN_DATA_ROOT}"
emit_info check db_root       "${LUMEN_DB_ROOT}"
emit_info check web_bind_host "${CURRENT_WEB_BIND_HOST:-<default>}"
if [ -n "${LUMEN_PROXY_URL}" ]; then
    emit_info check proxy "configured"
fi

# rolling image tag（main / latest / vMAJOR / vMAJOR.MINOR）即使 tag 名不变，
# GHCR 上的 digest 仍会随 CI 推送变化；用 tag 名做 noop 比较等于永远拉不到
# 新镜像。识别最终 TARGET_TAG 并跳过 noop，让 pull/migrate/restart 完整跑一遍
# ——`docker compose pull` 自带 layer-level 去重，digest 没变时也只是 HEAD 几个
# manifest 即返。
NOOP_BY_TAG_NAME=1
if lumen_image_tag_is_rolling "${TARGET_TAG}"; then
    NOOP_BY_TAG_NAME=0
fi

if [ -n "${CURRENT_TAG}" ] \
        && [ "${CURRENT_TAG}" = "${TARGET_TAG}" ] \
        && [ "${CONFIG_CHANGED}" -eq 0 ] \
        && [ "${NOOP_BY_TAG_NAME}" -eq 1 ]; then
    log_info "[check] 当前 tag ${CURRENT_TAG} 已是目标版本，跳过中间阶段，仅做 cleanup。"
    emit_info check action "noop_already_latest"
    emit_done  check 0
    SKIP_TO_CLEANUP=1
else
    if [ "${NOOP_BY_TAG_NAME}" -eq 0 ] \
            && [ -n "${CURRENT_TAG}" ] \
            && [ "${CURRENT_TAG}" = "${TARGET_TAG}" ]; then
        log_info "[check] target_tag=${TARGET_TAG} 是 rolling tag，跳过 tag 名 noop 检查，强制 pull 拉新 digest。"
        emit_info check action "rolling_force_redeploy"
    elif [ -n "${CURRENT_TAG}" ] && [ "${CURRENT_TAG}" = "${TARGET_TAG}" ] && [ "${CONFIG_CHANGED}" -eq 1 ]; then
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

# 数据目录与权限。PG/Redis 可通过 LUMEN_DB_ROOT 放本机盘；
# storage/backup 继续跟随 LUMEN_DATA_ROOT（可为 CIFS/NAS）。
if ! check_data_owners; then
    log_error "[preflight] 数据目录不齐全，请先跑 install.sh 或手动准备 LUMEN_DB_ROOT / LUMEN_DATA_ROOT。"
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

# Image-extract fallback: host 不是 git repo 时,从 lumen-api image 里 docker cp
# 出 release-time files (docker-compose.yml + deploy/ + scripts/ + VERSION),让
# update 流程不再依赖 host 上是否有 git clone / 公网 GitHub 可达。
# 见 Dockerfile.python 的 "Release-time files" COPY 块。
try_image_extract_release() {
    local tag="${1:-main}"
    local out_dir="$2"
    if ! command -v docker >/dev/null 2>&1; then
        return 1
    fi
    local registry="${LUMEN_IMAGE_REGISTRY:-ghcr.io/cyeinfpro}"
    local image="${registry}/lumen-api:${tag}"
    log_info "[fetch_release] 尝试 docker pull ${image}"
    if ! docker pull "${image}" >/dev/null 2>&1; then
        log_warn "[fetch_release] docker pull 失败 (image=${image})"
        return 1
    fi
    rm -rf "${out_dir}"
    mkdir -p "${out_dir}"
    local cid
    cid="$(docker create "${image}" /bin/true 2>/dev/null)" || return 1
    local rc=0
    # docker cp 不支持通配符;逐个 cp 完整 release-time 内容
    # （host 仅严格需要 docker-compose.yml + scripts + deploy；apps/packages/pyproject/
    # uv.lock 主要让 host ssh 调试时能看到完整代码树，不影响 runtime — 容器从 image 起。）
    local required_paths=(docker-compose.yml VERSION deploy scripts)
    local optional_paths=(apps packages pyproject.toml uv.lock)
    local path
    for path in "${required_paths[@]}"; do
        if ! docker cp "${cid}:/app/${path}" "${out_dir}/${path}" 2>/dev/null; then
            log_warn "[fetch_release] image 内缺少必须的 /app/${path}（image 可能是旧版本）"
            rc=1
        fi
    done
    for path in "${optional_paths[@]}"; do
        docker cp "${cid}:/app/${path}" "${out_dir}/${path}" 2>/dev/null || true
    done
    docker rm "${cid}" >/dev/null 2>&1 || true
    [ "${rc}" = "0" ] || return 1
    test -f "${out_dir}/docker-compose.yml" || return 1
    test -d "${out_dir}/scripts" || return 1
    return 0
}

if [ "${LUMEN_UPDATE_GIT_PULL:-0}" = "1" ]; then
    if ! command -v git >/dev/null 2>&1; then
        log_error "[fetch_release] LUMEN_UPDATE_GIT_PULL=1 但缺少 git。"
        emit_fail fetch_release 1
        exit 1
    fi
    if [ ! -d "${REPO_DIR}/.git" ]; then
        IMAGE_EXTRACT_DIR="${ROOT}/.update-image-extract"
        # CI smoke tests set LUMEN_UPDATE_DISABLE_IMAGE_EXTRACT=1 to skip the
        # network-y docker pull and exercise the legacy snapshot-only branch.
        if [ "${LUMEN_UPDATE_DISABLE_IMAGE_EXTRACT:-0}" != "1" ] && \
           try_image_extract_release "${TARGET_TAG:-main}" "${IMAGE_EXTRACT_DIR}"; then
            REPO_DIR="${IMAGE_EXTRACT_DIR}"
            emit_info fetch_release source "image_extract"
            log_info "[fetch_release] 已从 image 提取代码到 ${REPO_DIR}"
        else
            log_warn "[fetch_release] LUMEN_UPDATE_GIT_PULL=1 但 ${REPO_DIR} 不是 git 仓库；使用当前发布物快照继续。"
        fi
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
enable_local_build_fallback() {
    if [ "${LUMEN_UPDATE_BUILD:-0}" != "1" ]; then
        log_warn "[fetch_release] GHCR 镜像不可用，自动启用本地 build 继续。"
    fi
    LUMEN_UPDATE_BUILD=1
    export LUMEN_UPDATE_BUILD
    emit_info fetch_release build_fallback "local"
}
if ! probe_ghcr_tag "${LUMEN_IMAGE_REGISTRY}/lumen-api" "${TARGET_TAG}"; then
    if [ "${TARGET_TAG}" != "main" ] && [ "${LUMEN_UPDATE_FALLBACK_MAIN:-1}" = "1" ]; then
        log_warn "[fetch_release] 目标镜像 tag=${TARGET_TAG} 不存在，自动回退到 main。"
        emit_info fetch_release target_tag_fallback "main"
        TARGET_TAG="main"
        if ! probe_ghcr_tag "${LUMEN_IMAGE_REGISTRY}/lumen-api" "${TARGET_TAG}"; then
            enable_local_build_fallback
        fi
    else
        enable_local_build_fallback
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
    # tgbot 在 docker-compose.yml 里走 profile=tgbot，bare `docker compose pull`
    # 会跳过它。如果 .env 启用了 telegram，单独拉一次让 tgbot 镜像也跟到目标
    # tag 对应的 GHCR digest——否则 restart_services 阶段的
    # `--profile tgbot up -d tgbot` 会复用本地旧 image。失败仅 warn，不阻断
    # 业务 API 升级。
    if env_key_present "${SHARED_ENV}" "TELEGRAM_BOT_TOKEN"; then
        if ! lumen_retry 2 5 "docker compose pull tgbot" \
                lumen_compose_in "${NEW_RELEASE}" --profile tgbot pull tgbot; then
            log_warn "[pull_images] tgbot pull 失败，已忽略（业务 API 不受影响）。"
            emit_info pull_images tgbot_pull "warn_skipped"
        else
            emit_info pull_images tgbot_pull "ok"
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

# self-heal: 如果历史上有人手工 `cd current && docker compose up` 起过容器
# (project=current 而非 lumen)，先 down 掉，避免新 project=lumen 撞容器名。
# idempotent — 无 stale 直接返回。
lumen_compose_project_unify

# v1.0.48 镜像切到 pgvector/pgvector:pg16, postgres uid 70 → 999.
# 老老 install 留下的 alpine 数据目录 uid=70, 必须先 chown 否则 PG 起不来.
if ! migrate_postgres_uid; then
    log_error "[start_infra] postgres 数据目录 chown 999 失败,中止升级."
    emit_fail start_infra 1
    exit 1
fi

# --force-recreate：避免容器名已存在但配置签名不一致（caller 历史 cwd 不同
# 或人工 docker compose up 留下来的孤儿容器）时报 conflict 直接 fail。
if ! lumen_compose_in "${NEW_RELEASE}" up -d --wait --force-recreate postgres redis; then
    log_error "[start_infra] postgres / redis 启动或健康检查失败。"
    log_error "  当前 API/Worker/Web 服务保持不变。"
    emit_fail start_infra 1
    exit 1
fi
emit_done start_infra 0

# ---------------------------------------------------------------------------
# Phase: migrate_db
# 死规则：失败 → abort，不切 current、不重启业务容器。
# v1.0.51 升级踩过的设计漏洞: 老 api/worker 在 migrate 期间继续跑, 任何一个
# idle in transaction (read-only SELECT 也算) 持 share lock, 让 ALTER 等
# ACCESS EXCLUSIVE 死锁; ALTER 排在 lock queue 头部又把所有后续 query 一起
# 堵住 → 全局 401/CONNECTION_RESET 雪崩, 直到 systemd 7200s 超时. 修法:
# migrate 前 stop api/worker/tgbot 让 PG 没活跃业务事务; alembic 自己
# 也设 lock_timeout=5s fail-fast (env.py). PG/Redis 保持 up.
# ---------------------------------------------------------------------------
emit_start migrate_db

log_info "[migrate_db] stop api/worker/tgbot 让出活跃事务,避免 schema lock 死锁"
# stop 失败 (容器本来没起 / 无该 service 之类) 不阻塞 migrate.
lumen_compose_in "${NEW_RELEASE}" stop api worker tgbot >/dev/null 2>&1 || true

_migrate_run_failed=0
if ! lumen_compose_in "${NEW_RELEASE}" --profile migrate run --rm migrate; then
    _migrate_run_failed=1
fi

# Verify alembic 真到 head — 已观察到 alembic upgrade 在某些情况下 silent
# exit=0 但 transaction rollback（lock_timeout / FK 验证 abort 时异常被
# SA 内部吞掉）。仅看 docker compose run 的 exit code 不可靠：必须二次
# query alembic_version 与 heads 比对。否则 update.sh 误以为 success 后切
# current → api 用新代码查旧 schema → 全站 500（v1.1.0 prod 已踩过）。
_alembic_heads="$(lumen_compose_in "${NEW_RELEASE}" --profile migrate run --rm migrate alembic heads 2>/dev/null \
    | awk 'NF && !/^INFO/ {print $1; exit}')"
_alembic_current="$(lumen_compose_in "${NEW_RELEASE}" --profile migrate run --rm migrate alembic current 2>/dev/null \
    | awk 'NF && !/^INFO/ {print $1; exit}')"

if [ "${_migrate_run_failed}" = "1" ] \
        || [ -z "${_alembic_heads}" ] \
        || [ "${_alembic_current}" != "${_alembic_heads}" ]; then
    log_error "[migrate_db] alembic upgrade 失败或未真正落地 → fail-fast。"
    log_error "  observed alembic current=${_alembic_current:-<空>}"
    log_error "  expected head=${_alembic_heads:-<空>}"
    log_error "  原始 docker compose run rc：${_migrate_run_failed}（0=看起来 success，但 verify 不通过仍 fail-fast）"
    log_error "  根据 §11.3 / §17.6：不切 current、不重启新版本业务容器。"
    # 关键修复：之前 stop 了旧 api/worker/tgbot，migrate 失败后必须把它们用旧
    # release 起回来，否则业务停摆 — 旧 schema 与旧代码兼容，仍可正常服务。
    if [ -n "${CURRENT_ID:-}" ] && [ -d "${ROOT}/releases/${CURRENT_ID}" ]; then
        log_warn "[migrate_db] 用旧 release ${CURRENT_ID} 重启 api/worker，让业务恢复旧 schema 服务..."
        # 旧 release 的 compose 文件指向旧镜像 tag（PREVIOUS_TAG），SHARED_ENV
        # 还没被 set_image_tag 改写之前已经被改过；如果已改，先恢复成旧 tag。
        if [ -n "${PREVIOUS_TAG:-}" ] && [ -n "${TARGET_TAG:-}" ] && [ "${PREVIOUS_TAG}" != "${TARGET_TAG}" ]; then
            lumen_set_image_tag_in_env "${SHARED_ENV}" "${PREVIOUS_TAG}" 2>/dev/null \
                || log_warn "  恢复 SHARED_ENV 到 ${PREVIOUS_TAG} 失败，旧服务可能拉错镜像 tag。"
        fi
        if lumen_compose_in "${ROOT}/releases/${CURRENT_ID}" up -d worker api 2>/dev/null; then
            log_info "[migrate_db] 旧服务 (${CURRENT_ID}) 已重启，业务可用旧 schema 继续。"
        else
            log_error "[migrate_db] 旧服务重启失败！业务此时停摆，请人工处理："
            log_error "    cd ${ROOT}/releases/${CURRENT_ID}"
            log_error "    COMPOSE_PROJECT_NAME=lumen docker compose up -d worker api"
        fi
    else
        log_error "[migrate_db] 无可用的旧 release（CURRENT_ID=${CURRENT_ID:-<none>}），业务停摆。"
    fi
    log_error "  请人工查 migrate 日志：docker compose logs --tail=120 migrate"
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
# Phase: check_storage
# 验证 /opt/lumendata 已挂载且可写。这里只校验，不重挂；真正的切换由 admin UI →
# lumen-storage-apply.service 单独负责。如果用户跑 update 之前 SMB 挂不上，
# 业务容器起来也会失败，所以早 abort 比 restart_services 失败再回滚更省事。
# 跳过条件：SKIP_STORAGE_CHECK=1（适用于 host 还没装 lumen-storage-* 的过渡期）。
# ---------------------------------------------------------------------------
if [ "${SKIP_STORAGE_CHECK:-0}" = "1" ]; then
    emit_info check_storage status "skipped_via_env"
else
    emit_start check_storage
    if ! findmnt -T /opt/lumendata >/dev/null 2>&1; then
        log_error "[check_storage] /opt/lumendata 未挂载。"
        log_error "  在管理后台「存储后端」页面配置 local 或 smb 后即可生效；"
        log_error "  紧急绕过：SKIP_STORAGE_CHECK=1 ./update.sh"
        emit_fail check_storage 1
        exit 1
    fi
    _storage_probe="/opt/lumendata/.update_probe_$$"
    if ! touch "${_storage_probe}" 2>/dev/null; then
        log_error "[check_storage] /opt/lumendata 不可写（host 端挂载源可能不可达）。"
        emit_fail check_storage 1
        exit 1
    fi
    rm -f "${_storage_probe}"
    _storage_fstype="$(findmnt -T /opt/lumendata -no FSTYPE 2>/dev/null || true)"
    emit_info check_storage fstype "${_storage_fstype:-unknown}"
    emit_done check_storage 0
fi

# ---------------------------------------------------------------------------
# Phase: restart_services
# 启动 api / worker / web；如启用 Telegram，则起 tgbot。
# 失败 → 自动用 PREVIOUS_TAG 回滚（pull && up）。
# ---------------------------------------------------------------------------
emit_start restart_services

CURRENT_LINK="${ROOT}/current"
# --force-recreate：同 start_infra 理由，避免容器名冲突 fail。
# 服务启动顺序：worker → web → api。lumen-api **必须最后重启**——
# update.sh 自身就是被 admin_update 通过 lumen-update-runner 触发的，
# 如果 api 先重启，正在等 update 进度 SSE 的前端会立刻断流；
# 把 api 放到最后还能用旧 api 把进度写完，再无缝切到新版本。
# (per project_lumen_update_button.md)
_restart_ok=1
for _svc in worker web api; do
    if ! lumen_compose_in "${CURRENT_LINK}" up -d --wait --force-recreate "${_svc}"; then
        _restart_ok=0
        break
    fi
done
if [ "${_restart_ok}" = "1" ]; then
    :
else
    log_error "[restart_services] api/worker/web 启动失败，尝试自动回滚到上一已知好 tag：${PREVIOUS_TAG:-<none>}"
    emit_warn restart_services "starting_auto_rollback"
    # 事务化回滚：先备份新 tag、改 .env，pull/up 任一步失败就把 .env 恢复成新 tag，
    # 确保 SHARED_ENV 与 current symlink 状态一致（不会出现 .env 是旧 tag 但 current
    # 仍是新 release 的中间态）。
    ROLLBACK_OK=0
    # 优先用 releases/<CURRENT_ID>/.image-tag 锚定回滚 tag（之前 set_image_tag
    # 阶段写入），fallback 到 PREVIOUS_TAG（update 开始前 SHARED_ENV 中的值）。
    # 前者抗"update 中途用户手动改过 SHARED_ENV"的边界情况，避免回滚拉到错误
    # 镜像导致 release 代码与镜像版本不匹配。
    ROLLBACK_TAG="${PREVIOUS_TAG}"
    if [ -n "${CURRENT_ID:-}" ] && [ -f "${ROOT}/releases/${CURRENT_ID}/.image-tag" ]; then
        _anchored="$(head -n1 "${ROOT}/releases/${CURRENT_ID}/.image-tag" 2>/dev/null | tr -d '[:space:]')"
        if [ -n "${_anchored}" ]; then
            ROLLBACK_TAG="${_anchored}"
        fi
    fi
    if [ -n "${ROLLBACK_TAG}" ] && [ "${ROLLBACK_TAG}" != "${TARGET_TAG}" ]; then
        # 还要验证 PREVIOUS release 目录还在；缺失时回滚没意义，直接走手动恢复路径
        if [ -z "${CURRENT_ID:-}" ] || [ ! -d "${ROOT}/releases/${CURRENT_ID}" ]; then
            log_error "[restart_services] previous release 目录不存在（${ROOT}/releases/${CURRENT_ID:-<none>}），跳过自动回滚。"
        else
            if lumen_set_image_tag_in_env "${SHARED_ENV}" "${ROLLBACK_TAG}"; then
                _rollback_started=1
                if lumen_release_atomic_switch "${ROOT}" "${CURRENT_ID}" \
                    && lumen_compose_in "${CURRENT_LINK}" pull; then
                    # 回滚同样按 worker → web → api 顺序逐个 up，保留 api 最后启动的偏好。
                    for _svc in worker web api; do
                        if ! lumen_compose_in "${CURRENT_LINK}" up -d --wait --force-recreate "${_svc}"; then
                            _rollback_started=0
                            break
                        fi
                    done
                else
                    _rollback_started=0
                fi
                if [ "${_rollback_started}" = "1" ]; then
                    SWITCHED=0  # current 已切回旧 release，on_err 不再重复切
                    log_warn "[restart_services] 已用 ${ROLLBACK_TAG} 回滚成功（current → ${CURRENT_ID}）；本次 update 视为失败。"
                    emit_info restart_services rolled_back_to "${ROLLBACK_TAG}"
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
                log_error "[restart_services] 改写 SHARED_ENV 到 ${ROLLBACK_TAG} 失败，跳过自动回滚。"
            fi
        fi
    fi
    if [ "${ROLLBACK_OK}" = "1" ]; then
        emit_fail restart_services 1
        exit 1
    fi
    log_error "[restart_services] 自动回滚失败 → 请按 §18 手动回滚："
    log_error "  ln -sfn releases/${CURRENT_ID:-<id>} ${ROOT}/current"
    log_error "  sed -i 's|^LUMEN_IMAGE_TAG=.*|LUMEN_IMAGE_TAG=${ROLLBACK_TAG:-${PREVIOUS_TAG:-<old-tag>}}|' ${SHARED_ENV}"
    log_error "  cd ${ROOT}/current && COMPOSE_PROJECT_NAME=lumen docker compose pull && up -d --wait api worker web"
    emit_fail restart_services 1
    exit 1
fi

# tgbot：如果 .env 有 TELEGRAM_BOT_TOKEN 非空才起
if env_key_present "${SHARED_ENV}" "TELEGRAM_BOT_TOKEN"; then
    if ! lumen_compose_in "${CURRENT_LINK}" --profile tgbot up -d --force-recreate tgbot; then
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

# 4K 长任务场景下 worker warm-up 可能持续数分钟（layered timeout：nginx 1800
# → arq 1800 → task 1500 → upstream 660），原 60s 探测窗口对 cold-start 严重
# 不够。默认 300s（5 min），可通过 LUMEN_HEALTH_TIMEOUT_SECONDS 覆盖。
HEALTH_TIMEOUT="${LUMEN_HEALTH_TIMEOUT_SECONDS:-300}"
HEALTH_FAIL=0
if ! lumen_health_http "${API_HEALTH_URL}" "${HEALTH_TIMEOUT}" 2; then
    log_error "[health_check] API ${API_HEALTH_URL} 在 ${HEALTH_TIMEOUT}s 内不可达。"
    HEALTH_FAIL=1
fi
if ! lumen_health_http "${WEB_HEALTH_URL}" "${HEALTH_TIMEOUT}" 2; then
    log_error "[health_check] Web ${WEB_HEALTH_URL} 在 ${HEALTH_TIMEOUT}s 内不可达。"
    HEALTH_FAIL=1
fi
if ! lumen_health_compose api worker web; then
    log_error "[health_check] docker compose 状态检查失败。"
    HEALTH_FAIL=1
fi

if [ "${HEALTH_FAIL}" -eq 1 ]; then
    log_error "[health_check] 健康检查失败；新代码已上线但状态异常。"
    log_error "  数据库迁移已应用，**不自动回滚**——请执行："
    log_error "    cd ${CURRENT_LINK}"
    log_error "    COMPOSE_PROJECT_NAME=lumen docker compose logs --tail=120 api worker web"
    log_error "    COMPOSE_PROJECT_NAME=lumen docker compose ps"
    log_error "  状态快照：release_id=${NEW_ID}  image_tag=${TARGET_TAG}  current → $(readlink "${ROOT}/current" 2>/dev/null || echo unknown)"
    log_error "  如需回滚，参考 docs/.. §18 或调高 LUMEN_HEALTH_TIMEOUT_SECONDS 重跑健康。"
    emit_fail health_check 1
    exit 1
fi
emit_done health_check 0

# ---------------------------------------------------------------------------
# Phase: cleanup
# 多级 prune：dangling images / 未引用 images / buildx cache / 旧 release。
# 三个时间窗口可通过 env 覆盖；**默认 0 = 不加 until filter，立即清所有**：
#   LUMEN_CLEANUP_DANGLING_HOURS  (default 0)
#   LUMEN_CLEANUP_IMAGES_HOURS    (default 0)
#   LUMEN_CLEANUP_CACHE_HOURS     (default 0)
# 24 小时连发多版本时 until 过滤反而把所有候选都豁免；默认激进清理符合
# "每次更新都打扫一次"的诉求。需要保留旧镜像做回滚的 host 自己设 hours。
# 仍在跑的容器引用的 image 永远不会被 prune（docker 自己保护），所以
# 0-filter 安全：清掉的都是真 unused。
# 任一步失败仅 warn 不阻断，磁盘清理把成功的 update 拉成 fail 不可接受。
# ---------------------------------------------------------------------------
emit_start cleanup

CLEANUP_DANGLING_H="${LUMEN_CLEANUP_DANGLING_HOURS:-0}"
CLEANUP_IMAGES_H="${LUMEN_CLEANUP_IMAGES_HOURS:-0}"
CLEANUP_CACHE_H="${LUMEN_CLEANUP_CACHE_HOURS:-0}"

_cleanup_filter_args() {
    local hours="$1"
    if [ "${hours}" -gt 0 ] 2>/dev/null; then
        printf -- '--filter\nuntil=%sh\n' "${hours}"
    fi
}

# 1. dangling layers — 几乎 0 风险。
filter_args=()
while IFS= read -r line; do filter_args+=("${line}"); done < <(_cleanup_filter_args "${CLEANUP_DANGLING_H}")
if ! lumen_docker image prune -f "${filter_args[@]}" >/dev/null 2>&1; then
    log_warn "[cleanup] docker image prune (dangling) 失败（已忽略）。"
else
    emit_info cleanup dangling_pruned "hours=${CLEANUP_DANGLING_H}"
fi

# 2. untagged unused images — 旧 :main / :v1.0.x。docker 不会 prune 仍被运行容器
#    引用的 image，所以默认无 filter 安全。
filter_args=()
while IFS= read -r line; do filter_args+=("${line}"); done < <(_cleanup_filter_args "${CLEANUP_IMAGES_H}")
if ! lumen_docker image prune -a -f "${filter_args[@]}" >/dev/null 2>&1; then
    log_warn "[cleanup] docker image prune -a 失败（已忽略）。"
else
    emit_info cleanup unused_images_pruned "hours=${CLEANUP_IMAGES_H}"
fi

# 3. buildx build cache — local build 路径会无限增长，必须定期清。
if lumen_docker buildx version >/dev/null 2>&1; then
    filter_args=()
    while IFS= read -r line; do filter_args+=("${line}"); done < <(_cleanup_filter_args "${CLEANUP_CACHE_H}")
    if ! lumen_docker buildx prune -f "${filter_args[@]}" >/dev/null 2>&1; then
        log_warn "[cleanup] docker buildx prune 失败（已忽略）。"
    else
        emit_info cleanup buildx_cache_pruned "hours=${CLEANUP_CACHE_H}"
    fi
fi

# 4. 旧 release 目录 — keep 最近 N 个（含 current）。
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
