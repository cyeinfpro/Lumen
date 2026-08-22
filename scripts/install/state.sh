#!/usr/bin/env bash
# Install transaction snapshot, rollback, and signal handling.
# Sourced by scripts/install.sh after raw bootstrap has completed.

install_transaction_dir() {
    printf '%s\n' \
        "${INSTALL_JOURNAL_DIR:-${DEPLOY_ROOT:?}/.install-transaction}"
}

install_transaction_run_python() {
    local directory="$1"
    shift
    if [ -w "${directory}" ]; then
        python3 "$@"
    else
        lumen_run_as_root python3 "$@"
    fi
}

install_transaction_write_value() {
    local name="$1"
    local value="$2"
    local journal=""
    case "${name}" in
        schema|phase|release-id|snapshot-ready|env-original|current-present|current-target|previous-present|previous-target)
            ;;
        *)
            log_error "未知 install journal 字段：${name}"
            return 1
            ;;
    esac
    journal="$(install_transaction_dir)"
    install_transaction_run_python "${journal}" - \
        "${journal}" "${name}" "${value}" <<'PY'
import errno
import os
from pathlib import Path
import stat
import sys
import tempfile

journal = Path(sys.argv[1])
name = sys.argv[2]
value = sys.argv[3]
metadata = journal.lstat()
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
    raise SystemExit("install journal is not a regular directory")
target = journal / name
try:
    target_metadata = target.lstat()
except FileNotFoundError:
    pass
else:
    if stat.S_ISLNK(target_metadata.st_mode) or not stat.S_ISREG(
        target_metadata.st_mode
    ):
        raise SystemExit(f"unsafe install journal field: {target}")
fd, temporary_raw = tempfile.mkstemp(
    prefix=f".{name}.",
    suffix=".tmp",
    dir=journal,
)
temporary = Path(temporary_raw)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
        handle.write("\n")
        handle.flush()
        os.fchmod(handle.fileno(), 0o600)
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    directory_fd = os.open(
        journal,
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
}

install_transaction_read_value() {
    local name="$1"
    local journal=""
    journal="$(install_transaction_dir)"
    install_transaction_run_python "${journal}" - "${journal}/${name}" <<'PY'
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
metadata = path.lstat()
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
    raise SystemExit(1)
print(path.read_text(encoding="utf-8").rstrip("\r\n"))
PY
}

install_transaction_copy_durable() {
    local source="$1"
    local destination="$2"
    local destination_dir=""
    destination_dir="$(dirname "${destination}")"
    install_transaction_run_python "${destination_dir}" - \
        "${source}" "${destination}" <<'PY'
import errno
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
source_metadata = source.lstat()
if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISREG(source_metadata.st_mode):
    raise SystemExit("install snapshot source is not a regular file")
try:
    destination_metadata = destination.lstat()
except FileNotFoundError:
    pass
else:
    if stat.S_ISLNK(destination_metadata.st_mode) or not stat.S_ISREG(
        destination_metadata.st_mode
    ):
        raise SystemExit("install snapshot destination is unsafe")
fd, temporary_raw = tempfile.mkstemp(
    prefix=f".{destination.name}.",
    suffix=".tmp",
    dir=destination.parent,
)
temporary = Path(temporary_raw)
try:
    with source.open("rb") as source_handle, os.fdopen(fd, "wb") as target_handle:
        shutil.copyfileobj(source_handle, target_handle)
        target_handle.flush()
        os.fchmod(target_handle.fileno(), stat.S_IMODE(source_metadata.st_mode))
        os.fsync(target_handle.fileno())
    os.replace(temporary, destination)
    directory_fd = os.open(
        destination.parent,
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
}

install_transaction_fsync_tree() {
    local tree="$1"
    install_transaction_run_python "${tree}" - "${tree}" <<'PY'
import errno
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
for directory, names, files in os.walk(root, topdown=False, followlinks=False):
    base = Path(directory)
    for name in files:
        path = base / name
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(f"unsafe install snapshot entry: {path}")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for name in names:
        metadata = (base / name).lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit(f"unsafe install snapshot directory: {base / name}")
    descriptor = os.open(
        base,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
                raise
    finally:
        os.close(descriptor)
PY
}

install_transaction_begin() {
    local journal=""
    journal="$(install_transaction_dir)"
    if [ -e "${journal}" ] || [ -L "${journal}" ]; then
        log_error "install journal 已存在，必须先恢复：${journal}"
        return 1
    fi
    if ! mkdir -m 0700 "${journal}" 2>/dev/null; then
        lumen_run_as_root install -d -m 0700 \
            -o "$(id -u)" -g "$(id -g)" "${journal}" || return 1
    fi
    INSTALL_JOURNAL_DIR="${journal}"
    install_transaction_write_value schema 1 \
        && install_transaction_write_value release-id "${RELEASE_ID}" \
        && install_transaction_write_value phase layout
}

install_transaction_set_phase() {
    local phase="$1"
    INSTALL_PHASE="${phase}"
    if [ -d "$(install_transaction_dir)" ]; then
        install_transaction_write_value phase "${phase}"
    fi
}

install_transaction_failpoint() {
    local phase="$1"
    local configured="${LUMEN_INSTALL_FAILPOINT:-}"
    local ready="${LUMEN_INSTALL_FAILPOINT_READY:-}"
    local go="${LUMEN_INSTALL_FAILPOINT_GO:-}"
    [ -n "${configured}" ] || return 0
    case "${configured}" in
        "sigkill:${phase}")
            [ -z "${ready}" ] || : > "${ready}"
            kill -KILL "$$"
            ;;
        "pause:${phase}")
            [ -z "${ready}" ] || : > "${ready}"
            while [ -z "${go}" ] || [ ! -e "${go}" ]; do
                sleep 0.02
            done
            ;;
    esac
}

install_transaction_mark_complete() {
    install_transaction_write_value phase complete
    INSTALL_TRANSACTION_COMMITTED=1
}

install_transaction_harden_journal() {
    local journal=""
    journal="$(install_transaction_dir)"
    [ -d "${journal}" ] || return 0
    [ ! -L "${journal}" ] || return 1
    lumen_run_as_root chown -R root:root "${journal}" \
        && lumen_run_as_root chmod -R go-rwx "${journal}" \
        && lumen_run_as_root chmod 0700 "${journal}" \
        && lumen_fsync_directory "${journal}" \
        && lumen_fsync_directory "${DEPLOY_ROOT}"
}

install_transaction_cleanup() {
    local journal="" phase=""
    journal="$(install_transaction_dir)"
    [ ! -e "${journal}" ] && [ ! -L "${journal}" ] && return 0
    if [ -L "${journal}" ] || [ ! -d "${journal}" ] \
            || [ "${journal}" != "${DEPLOY_ROOT}/.install-transaction" ]; then
        log_error "拒绝删除异常 install journal：${journal}"
        return 1
    fi
    phase="$(install_transaction_read_value phase 2>/dev/null || true)"
    if [ "${phase}" = "complete" ]; then
        if ! _install_core_readiness \
                "${RELEASE_DIR}" \
                "${LUMEN_API_READY_URL:-http://127.0.0.1:8000/readyz}" \
                "${LUMEN_INSTALL_CORE_READINESS_ATTEMPTS:-60}" \
                "${LUMEN_INSTALL_CORE_READINESS_INTERVAL_SECONDS:-2}"; then
            log_error "install complete journal 清理前核心 readiness 失败；保留 journal。"
            return 1
        fi
    fi
    if ! rm -rf -- "${journal}" 2>/dev/null; then
        lumen_run_as_root rm -rf -- "${journal}" || return 1
    fi
    lumen_fsync_directory "${DEPLOY_ROOT}"
}

install_transaction_load() {
    local journal="" schema="" release_id="" phase=""
    journal="$(install_transaction_dir)"
    if [ -L "${journal}" ] || [ ! -d "${journal}" ]; then
        log_error "install journal 不是普通目录：${journal}"
        return 1
    fi
    schema="$(install_transaction_read_value schema 2>/dev/null || true)"
    release_id="$(install_transaction_read_value release-id 2>/dev/null || true)"
    phase="$(install_transaction_read_value phase 2>/dev/null || true)"
    if [ "${schema}" != "1" ] \
            || [[ ! "${release_id}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        log_error "install journal schema 或 release id 无效，保留现场。"
        return 1
    fi
    case "${phase}" in
        layout|snapshot|prepare_env|probe_images|pull_images|start_infrastructure|migrate_db|bootstrap_admin|metadata|ownership|start_services|switch|host_operations|health|summary|complete)
            ;;
        *)
            log_error "install journal phase 未知：${phase:-<empty>}"
            return 1
            ;;
    esac
    INSTALL_JOURNAL_DIR="${journal}"
    RELEASE_ID="${release_id}"
    RELEASE_DIR="${DEPLOY_ROOT}/releases/${release_id}"
    SHARED_DIR="${DEPLOY_ROOT}/shared"
    INSTALL_PHASE="${phase}"
    INSTALL_ENV_SNAPSHOT="${journal}/env.before"
    INSTALL_HOST_ARTIFACT_SNAPSHOT="${journal}/host-artifacts"
    INSTALL_STATE_SNAPSHOT_READY=0
    if [ "$(install_transaction_read_value snapshot-ready 2>/dev/null || true)" = "1" ]; then
        INSTALL_STATE_SNAPSHOT_READY=1
    fi
    INSTALL_ORIGINAL_CURRENT_PRESENT="$(
        install_transaction_read_value current-present 2>/dev/null || printf '0'
    )"
    INSTALL_ORIGINAL_CURRENT_TARGET="$(
        install_transaction_read_value current-target 2>/dev/null || true
    )"
    INSTALL_ORIGINAL_PREVIOUS_PRESENT="$(
        install_transaction_read_value previous-present 2>/dev/null || printf '0'
    )"
    INSTALL_ORIGINAL_PREVIOUS_TARGET="$(
        install_transaction_read_value previous-target 2>/dev/null || true
    )"
}

install_transaction_phase_started_services() {
    case "$1" in
        start_infrastructure|migrate_db|bootstrap_admin|metadata|ownership|start_services|switch|host_operations|health|summary)
            return 0
            ;;
        *) return 1 ;;
    esac
}

install_transaction_restore_links() {
    local link="" present="" target="" current_target=""
    for link in current previous; do
        if [ "${link}" = "current" ]; then
            present="${INSTALL_ORIGINAL_CURRENT_PRESENT:-0}"
            target="${INSTALL_ORIGINAL_CURRENT_TARGET:-}"
        else
            present="${INSTALL_ORIGINAL_PREVIOUS_PRESENT:-0}"
            target="${INSTALL_ORIGINAL_PREVIOUS_TARGET:-}"
        fi
        if [ "${present}" = "1" ]; then
            lumen_atomic_replace_symlink "${target}" "${DEPLOY_ROOT}/${link}" \
                || return 1
            continue
        fi
        if [ -L "${DEPLOY_ROOT}/${link}" ]; then
            current_target="$(
                readlink "${DEPLOY_ROOT}/${link}" 2>/dev/null || true
            )"
            if [ "${current_target}" != "releases/${RELEASE_ID}" ]; then
                log_error "拒绝删除 owner 不明的 ${link} symlink：${current_target}"
                return 1
            fi
            if ! rm -f "${DEPLOY_ROOT}/${link}" 2>/dev/null; then
                lumen_run_as_root rm -f "${DEPLOY_ROOT}/${link}" || return 1
            fi
            lumen_fsync_directory "${DEPLOY_ROOT}" || return 1
        elif [ -e "${DEPLOY_ROOT}/${link}" ]; then
            log_error "拒绝覆盖非 symlink：${DEPLOY_ROOT}/${link}"
            return 1
        fi
    done
}

install_transaction_restore_env() {
    local original=""
    original="$(install_transaction_read_value env-original 2>/dev/null || printf '0')"
    if [ "${original}" = "1" ]; then
        if [ ! -f "${INSTALL_ENV_SNAPSHOT}" ] \
                || [ -L "${INSTALL_ENV_SNAPSHOT}" ]; then
            log_error "install journal 缺少原始 .env 快照。"
            return 1
        fi
        install_transaction_copy_durable \
            "${INSTALL_ENV_SNAPSHOT}" "${SHARED_DIR}/.env" || return 1
    fi
    if [ -f "${SHARED_DIR}/.env" ] && [ ! -L "${SHARED_DIR}/.env" ]; then
        lumen_run_as_root chown root:root "${SHARED_DIR}/.env" || return 1
        lumen_run_as_root chmod 0600 "${SHARED_DIR}/.env" || return 1
    fi
    lumen_run_as_root chown root:root "${DEPLOY_ROOT}" "${SHARED_DIR}" \
        "${DEPLOY_ROOT}/releases" 2>/dev/null || true
    lumen_run_as_root chmod 0755 "${DEPLOY_ROOT}" "${SHARED_DIR}" \
        "${DEPLOY_ROOT}/releases" 2>/dev/null || true
    lumen_fsync_directory "${SHARED_DIR}"
}

install_transaction_finalize_complete() {
    if [ ! -L "${DEPLOY_ROOT}/current" ] \
            || [ "$(readlink "${DEPLOY_ROOT}/current" 2>/dev/null || true)" \
                != "releases/${RELEASE_ID}" ] \
            || [ ! -d "${RELEASE_DIR}" ]; then
        log_error "install journal 标记 complete，但 current/release 验证失败；拒绝回滚并保留 journal。"
        return 1
    fi
    install_transaction_cleanup
    INSTALL_RECOVERED_COMPLETE=1
    log_warn "检测到 phase=complete 的 stale install journal；仅完成清理，保留已激活 release。"
}

recover_stale_install_transaction() {
    local journal="" recovery_failed=0
    journal="$(install_transaction_dir)"
    [ -e "${journal}" ] || [ -L "${journal}" ] || return 0
    install_transaction_load || return 1
    if [ "${INSTALL_PHASE}" = "complete" ]; then
        install_transaction_finalize_complete
        return $?
    fi

    log_warn "检测到 SIGKILL/掉电后遗留的 install journal（phase=${INSTALL_PHASE}），开始恢复。"
    if install_transaction_phase_started_services "${INSTALL_PHASE}" \
            && [ -f "${RELEASE_DIR}/docker-compose.yml" ]; then
        if ! lumen_compose_in "${RELEASE_DIR}" --profile tgbot stop \
                tgbot web worker api agent-runtime postgres redis >/dev/null 2>&1; then
            log_error "无法停止上次 fresh install 启动的 compose 服务，保留 journal。"
            return 1
        fi
    fi
    install_transaction_restore_links || recovery_failed=1
    if [ "${INSTALL_STATE_SNAPSHOT_READY}" -eq 1 ]; then
        install_transaction_restore_env || recovery_failed=1
        if [ -d "${INSTALL_HOST_ARTIFACT_SNAPSHOT}" ]; then
            lumen_restore_operations_host_artifacts \
                "${INSTALL_HOST_ARTIFACT_SNAPSHOT}" || recovery_failed=1
        fi
    fi
    if [ "${recovery_failed}" -eq 0 ] && [ -d "${RELEASE_DIR}" ]; then
        if ! lumen_safe_rm_rf "${RELEASE_DIR}" 2>/dev/null \
                && ! lumen_safe_rm_rf_as_root "${RELEASE_DIR}" 2>/dev/null; then
            log_error "无法删除半完成 release：${RELEASE_DIR}"
            recovery_failed=1
        fi
    fi
    if [ "${recovery_failed}" -ne 0 ]; then
        log_error "fresh install 恢复不完整，证据保留在 ${journal}。"
        return 1
    fi
    install_transaction_cleanup || return 1
    log_warn "上次 fresh install 已恢复；保留数据卷与已生成 shared/.env，可安全重跑。"
}

snapshot_install_state() {
    local shared_env="${SHARED_DIR}/.env"
    if [ -L "${DEPLOY_ROOT}/current" ]; then
        INSTALL_ORIGINAL_CURRENT_PRESENT=1
        INSTALL_ORIGINAL_CURRENT_TARGET="$(readlink "${DEPLOY_ROOT}/current")"
    fi
    if [ -L "${DEPLOY_ROOT}/previous" ]; then
        INSTALL_ORIGINAL_PREVIOUS_PRESENT=1
        INSTALL_ORIGINAL_PREVIOUS_TARGET="$(readlink "${DEPLOY_ROOT}/previous")"
    fi
    if [ "${INSTALL_ORIGINAL_CURRENT_PRESENT}" -eq 1 ] \
            && [ -f "${DEPLOY_ROOT}/current/docker-compose.yml" ]; then
        local running service
        running="$(lumen_compose_in "${DEPLOY_ROOT}/current" \
            ps --status running --services 2>/dev/null || true)"
        while IFS= read -r service; do
            case "${service}" in
                postgres|redis|agent-runtime|api|worker|web|tgbot)
                    INSTALL_ORIGINAL_RUNNING_SERVICES="${INSTALL_ORIGINAL_RUNNING_SERVICES}${service}"$'\n'
                    ;;
            esac
        done <<< "${running}"
    fi
    INSTALL_ENV_SNAPSHOT="$(install_transaction_dir)/env.before"
    INSTALL_HOST_ARTIFACT_SNAPSHOT="$(install_transaction_dir)/host-artifacts"
    install_transaction_write_value current-present \
        "${INSTALL_ORIGINAL_CURRENT_PRESENT}" || return 1
    install_transaction_write_value current-target \
        "${INSTALL_ORIGINAL_CURRENT_TARGET}" || return 1
    install_transaction_write_value previous-present \
        "${INSTALL_ORIGINAL_PREVIOUS_PRESENT}" || return 1
    install_transaction_write_value previous-target \
        "${INSTALL_ORIGINAL_PREVIOUS_TARGET}" || return 1
    if [ -f "${shared_env}" ] && [ ! -L "${shared_env}" ]; then
        install_transaction_copy_durable \
            "${shared_env}" "${INSTALL_ENV_SNAPSHOT}" || return 1
        install_transaction_write_value env-original 1 || return 1
    else
        INSTALL_ENV_SNAPSHOT=""
        install_transaction_write_value env-original 0 || return 1
    fi
    if lumen_systemd_runtime_available; then
        mkdir -m 0700 "${INSTALL_HOST_ARTIFACT_SNAPSHOT}" || return 1
        if ! lumen_snapshot_operations_host_artifacts \
                "${INSTALL_HOST_ARTIFACT_SNAPSHOT}"; then
            lumen_discard_host_artifact_snapshot "${INSTALL_HOST_ARTIFACT_SNAPSHOT}"
            INSTALL_HOST_ARTIFACT_SNAPSHOT=""
            return 1
        fi
        install_transaction_fsync_tree \
            "${INSTALL_HOST_ARTIFACT_SNAPSHOT}" || return 1
    else
        INSTALL_HOST_ARTIFACT_SNAPSHOT=""
    fi
    install_transaction_write_value snapshot-ready 1 || return 1
    INSTALL_STATE_SNAPSHOT_READY=1
    return 0
}

restore_install_original_services() {
    [ "${INSTALL_ORIGINAL_CURRENT_PRESENT}" -eq 1 ] || return 0
    [ -d "${DEPLOY_ROOT}/current" ] || return 1
    local services=()
    local service
    while IFS= read -r service; do
        [ -n "${service}" ] && services+=("${service}")
    done <<< "${INSTALL_ORIGINAL_RUNNING_SERVICES}"
    if [ "${#services[@]}" -eq 0 ]; then
        return 0
    fi
    log_warn "  重新拉起安装前运行中的旧 release 服务：${services[*]}"
    if ! lumen_compose_in "${DEPLOY_ROOT}/current" --profile tgbot up \
            --pull missing -d --wait --force-recreate "${services[@]}"; then
        return 1
    fi
    local needs_core=0
    for service in "${services[@]}"; do
        case "${service}" in
            api|worker) needs_core=1 ;;
        esac
    done
    if [ "${needs_core}" -eq 1 ]; then
        _install_core_readiness \
            "${DEPLOY_ROOT}/current" \
            "${LUMEN_API_READY_URL:-http://127.0.0.1:8000/readyz}" \
            "${LUMEN_INSTALL_CORE_READINESS_ATTEMPTS:-60}" \
            "${LUMEN_INSTALL_CORE_READINESS_INTERVAL_SECONDS:-2}"
    fi
}

restore_install_state_snapshot() {
    [ "${INSTALL_STATE_SNAPSHOT_READY}" -eq 1 ] || return 0
    local rc=0 shared_env="${SHARED_DIR}/.env"
    if [ "${INSTALL_ORIGINAL_CURRENT_PRESENT}" -eq 1 ]; then
        lumen_atomic_replace_symlink \
            "${INSTALL_ORIGINAL_CURRENT_TARGET}" "${DEPLOY_ROOT}/current" || rc=1
    elif [ -L "${DEPLOY_ROOT}/current" ]; then
        rm -f "${DEPLOY_ROOT}/current" || rc=1
    fi
    if [ "${INSTALL_ORIGINAL_PREVIOUS_PRESENT}" -eq 1 ]; then
        lumen_atomic_replace_symlink \
            "${INSTALL_ORIGINAL_PREVIOUS_TARGET}" "${DEPLOY_ROOT}/previous" || rc=1
    elif [ -L "${DEPLOY_ROOT}/previous" ]; then
        rm -f "${DEPLOY_ROOT}/previous" || rc=1
    fi
    if [ -n "${INSTALL_ENV_SNAPSHOT}" ] \
            && [ -f "${INSTALL_ENV_SNAPSHOT}" ]; then
        if ! install_transaction_copy_durable \
                "${INSTALL_ENV_SNAPSHOT}" "${shared_env}"; then
            log_error "  shared/.env 原字节恢复失败；快照保留在 ${INSTALL_ENV_SNAPSHOT}"
            rc=1
        else
            lumen_run_as_root chown root:root "${shared_env}" 2>/dev/null \
                || rc=1
            lumen_run_as_root chmod 0600 "${shared_env}" 2>/dev/null \
                || rc=1
            log_warn "  shared/.env 已按安装前快照原字节恢复。"
        fi
    fi
    if [ -n "${INSTALL_HOST_ARTIFACT_SNAPSHOT}" ]; then
        if ! lumen_restore_operations_host_artifacts \
                "${INSTALL_HOST_ARTIFACT_SNAPSHOT}"; then
            log_error "  systemd units 或 host 脚本未能完整恢复。"
            rc=1
        else
            log_warn "  systemd units 与 host 脚本已恢复到安装前快照。"
        fi
    fi
    if ! restore_install_original_services; then
        log_error "  安装前旧 release 服务恢复失败。"
        rc=1
    fi
    return "${rc}"
}

discard_install_state_snapshot() {
    INSTALL_ENV_SNAPSHOT=""
    INSTALL_HOST_ARTIFACT_SNAPSHOT=""
    INSTALL_ORIGINAL_RUNNING_SERVICES=""
    INSTALL_STATE_SNAPSHOT_READY=0
}

on_error() {
    local line="$1"
    log_error "安装失败：第 ${line} 行返回非零状态（阶段=${INSTALL_PHASE:-unknown}）。"
}

# 失败清理：停止已启动的容器、回滚 current symlink、删除半完成的 release。
# 数据卷与 shared/.env 永远保留，让用户重跑 install 时复用。
cleanup_on_failure() {
    local rc=$?
    local cleanup_complete=1
    trap - EXIT ERR
    trap '' INT TERM HUP
    if [ -n "${INSTALL_GHCR_PROBE_FILE:-}" ]; then
        rm -f "${INSTALL_GHCR_PROBE_FILE}" 2>/dev/null || true
        INSTALL_GHCR_PROBE_FILE=""
    fi
    if [ "${rc}" -ne 0 ]; then
        log_error "安装在阶段 [${INSTALL_PHASE:-unknown}] 失败，正在清理已启动的容器（数据卷与 shared/.env 保留）。"
        if [ "${#INSTALL_STARTED_SERVICES[@]}" -gt 0 ]; then
            local svc
            for svc in "${INSTALL_STARTED_SERVICES[@]}"; do
                log_warn "  最近 40 行 ${svc} 日志："
                _install_compose logs --tail=40 "${svc}" 2>/dev/null || log_warn "    （取日志失败，已忽略）"
            done
            log_warn "停止已启动的服务（数据卷保留）：${INSTALL_STARTED_SERVICES[*]}"
            if ! _install_compose stop "${INSTALL_STARTED_SERVICES[@]}" 2>/dev/null; then
                log_warn "  docker compose stop 返回非零（已忽略，请手动 docker compose ps 检查）"
            fi
        fi

        # 恢复安装开始时的 current / previous / shared env 状态。已有 .env
        # 按原字节恢复；首次安装生成的新 .env 仍保留，方便修复后幂等重跑。
        local _deploy_root="${DEPLOY_ROOT:-}"
        if [ -n "${_deploy_root}" ] \
                && [ "${INSTALL_STATE_SNAPSHOT_READY}" -eq 1 ]; then
            if restore_install_state_snapshot; then
                :
            else
                log_error "  安装前状态未能完整恢复，请检查 current/previous 与 ${SHARED_DIR:-<shared>}/.env。"
                cleanup_complete=0
            fi
        fi

        # 半完成的 release 目录：rsync 已落地但 current 从未切到它（或已切回 previous），删除。
        if [ -n "${RELEASE_DIR:-}" ] && [ -d "${RELEASE_DIR}" ]; then
            local cur_target=""
            if [ -n "${_deploy_root}" ] && [ -L "${_deploy_root}/current" ]; then
                cur_target="$(readlink "${_deploy_root}/current" 2>/dev/null || true)"
            fi
            if [ "${cur_target}" != "releases/${RELEASE_ID:-}" ]; then
                log_warn "清理半完成的 release：${RELEASE_DIR}"
                if ! lumen_safe_rm_rf "${RELEASE_DIR}" 2>/dev/null; then
                    if ! lumen_safe_rm_rf_as_root "${RELEASE_DIR}" 2>/dev/null; then
                        log_warn "  release 删除失败，请手动：sudo rm -rf '${RELEASE_DIR}'"
                        cleanup_complete=0
                    fi
                fi
            fi
        fi
        if [ "${cleanup_complete}" -eq 1 ]; then
            if ! install_transaction_cleanup; then
                cleanup_complete=0
            fi
        fi
        if [ "${cleanup_complete}" -eq 1 ]; then
            discard_install_state_snapshot
        else
            log_error "  install journal 已保留，修复环境后重跑会先尝试恢复。"
        fi

        # 只在新流程触发的 step protocol 上下文里写 fail；emit_step 函数在 lib.sh
        if command -v lumen_emit_step >/dev/null 2>&1 && [ -n "${INSTALL_PHASE:-}" ]; then
            lumen_emit_step "phase=${INSTALL_PHASE}" "status=fail" "rc=${rc}" "dur_ms=0" 2>/dev/null \
                || log_warn "lumen_emit_step 写入失败（已忽略）"
        fi
        log_error ""
        log_error "可恢复命令："
        log_error "  cd ${_deploy_root:-${ROOT}}/current 2>/dev/null || cd ${ROOT}"
        log_error "  COMPOSE_PROJECT_NAME=lumen docker compose ps"
        log_error "  COMPOSE_PROJECT_NAME=lumen docker compose logs --tail=200 api worker web"
        log_error "  bash ${SCRIPT_DIR}/install.sh --install   # 修复后重跑（幂等）"
    fi
    # lumen_release_lock 由 lumen_acquire_lock 安装的 EXIT trap 处理；这里手动也调一次幂等
    if command -v lumen_release_lock >/dev/null 2>&1; then
        lumen_release_lock 2>/dev/null || true
    fi
    return "${rc}"
}

on_signal() {
    local signal_name="$1"
    local rc="$2"
    log_error "安装被 ${signal_name} 中断（rc=${rc}），将走完整失败清理流程。"
    # exit 触发 EXIT trap (cleanup_on_failure)：清理已起容器、回滚 current
    # symlink、删半成品 release，最后释放锁。比裸 exit 更彻底。
    exit "${rc}"
}
