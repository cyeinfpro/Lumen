#!/usr/bin/env bash
# 一次性迁移脚本：把 in-place 部署的 /opt/lumen/<apps,scripts,...>
# 转换为 Capistrano 风格 release + symlink 布局。
#
# 用法：
#   sudo bash scripts/migrate_to_releases.sh
#
# 行为：
#   1. 幂等：检测 current 是否已经是 symlink，是则直接退出 0
#   2. systemctl stop lumen-tgbot lumen-web lumen-worker lumen-api
#   3. 用 /opt/lumen.tmp 作为中转，把 /opt/lumen 当前内容（除 .env）平移到
#      /opt/lumen.tmp/releases/initial/ 下，再 mv 回 /opt/lumen
#   4. 在 /opt/lumen/current 建立指向 releases/initial 的软链
#   5. 把 .env.local / worker var / .next/cache 移入 shared/ 并回链
#   6. 复制最新 systemd unit 到 /etc/systemd/system/，daemon-reload
#   7. systemctl start lumen-api lumen-worker lumen-web lumen-tgbot
#
# 输出普通 echo 信息，不使用 ::lumen-step:: 协议（迁移由人工执行，不进 SSE）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

ROOT="${LUMEN_ROOT:-/opt/lumen}"
LUMEN_DEPLOY_ROOT="${ROOT}"
export LUMEN_DEPLOY_ROOT
TMP_ROOT="${ROOT}.tmp"
ROLLBACK_STAGE="${TMP_ROOT}/.rollback-original"
INITIAL_ID="${LUMEN_MIGRATION_INITIAL_ID:-initial}"
MIGRATION_LOCK_DIR="${ROOT}.migrate-to-releases.lock.d"
MIGRATION_LOCK_OWNER_TOKEN=""
MIGRATION_STATE_DIR=""
MIGRATION_PHASE="preflight"
MIGRATION_COMMITTED=0
MIGRATION_RECOVERY_DONE=0
MIGRATION_COMPLETE_FINALIZED=0
PHASE_FILE=""
MOVE_INTENT_MANIFEST=""
MOVED_MANIFEST=""
ACTION_MANIFEST=""
ACTIVE_SERVICES_MANIFEST=""

MIGRATION_UNITS=(
    lumen-tgbot.service
    lumen-web.service
    lumen-worker.service
    lumen-api.service
)
MIGRATION_START_UNITS=(
    lumen-api.service
    lumen-worker.service
    lumen-web.service
    lumen-tgbot.service
)

sed_replacement_escape() {
    printf '%s' "$1" | sed 's/[\/&#]/\\&/g'
}

render_update_runner_unit() {
    local src="$1"
    local dst="$2"
    local data_root="$3"
    local backup_root="$4"
    local deploy_root="$5"
    local data_root_esc backup_root_esc deploy_root_esc
    data_root_esc="$(sed_replacement_escape "${data_root}")"
    backup_root_esc="$(sed_replacement_escape "${backup_root}")"
    deploy_root_esc="$(sed_replacement_escape "${deploy_root}")"

    sed \
        -e 's#/opt/lumendata/backup#__LUMEN_BACKUP_ROOT__#g' \
        -e 's#/opt/lumendata#__LUMEN_DATA_ROOT__#g' \
        -e 's#/opt/lumen#__LUMEN_DEPLOY_ROOT__#g' \
        "${src}" \
        | sed \
            -e "s#__LUMEN_BACKUP_ROOT__#${backup_root_esc}#g" \
            -e "s#__LUMEN_DATA_ROOT__#${data_root_esc}#g" \
            -e "s#__LUMEN_DEPLOY_ROOT__#${deploy_root_esc}#g" \
        > "${dst}"
}

migration_fsync_file_and_parent() {
    python3 - "$1" <<'PY'
import os
import stat
import sys

path = os.path.abspath(sys.argv[1])
flags = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
descriptor = os.open(path, flags)
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"not a regular file: {path}")
    os.fsync(descriptor)
finally:
    os.close(descriptor)

parent = os.path.dirname(path) or os.curdir
directory_flags = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
directory_fd = os.open(parent, directory_flags)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

migration_fsync_directories() {
    python3 - "$@" <<'PY'
import os
import stat
import sys

flags = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
seen = set()
for raw_path in sys.argv[1:]:
    path = os.path.abspath(raw_path)
    if path in seen:
        continue
    seen.add(path)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"not a directory: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
}

migration_sync_rename_boundaries() {
    local source_parent="$1"
    local destination_parent="$2"
    migration_fsync_directories \
        "${source_parent}" "${destination_parent}" \
        "$(dirname "${source_parent}")" \
        "$(dirname "${destination_parent}")"
}

migration_path_exists() {
    [ -e "$1" ] || [ -L "$1" ]
}

migration_rename_with_barriers() {
    local source="$1"
    local destination="$2"
    local source_parent destination_parent
    if ! migration_path_exists "${source}"; then
        log_error "迁移 rename 源不存在：${source}"
        return 1
    fi
    if migration_path_exists "${destination}"; then
        log_error "迁移 rename 目标已存在，拒绝覆盖：${destination}"
        return 1
    fi
    source_parent="$(dirname "${source}")"
    destination_parent="$(dirname "${destination}")"
    migration_sync_rename_boundaries \
        "${source_parent}" "${destination_parent}" || return 1
    mv "${source}" "${destination}" || return 1
    migration_sync_rename_boundaries \
        "${source_parent}" "${destination_parent}" || return 1
}

migration_failpoint() {
    local point="$1"
    local kind="${2:-}"
    local name="${3:-}"
    local action="${LUMEN_MIGRATION_FAILPOINT_ACTION:-fail}"
    local ready="${LUMEN_MIGRATION_FAILPOINT_READY:-}"
    local go="${LUMEN_MIGRATION_FAILPOINT_GO:-}"
    [ "${LUMEN_MIGRATION_FAILPOINT:-}" = "${point}" ] || return 0
    if [ -n "${LUMEN_MIGRATION_FAILPOINT_KIND:-}" ] \
            && [ "${LUMEN_MIGRATION_FAILPOINT_KIND}" != "${kind}" ]; then
        return 0
    fi
    if [ -n "${LUMEN_MIGRATION_FAILPOINT_NAME:-}" ] \
            && [ "${LUMEN_MIGRATION_FAILPOINT_NAME}" != "${name}" ]; then
        return 0
    fi
    if [ -n "${ready}" ]; then
        : > "${ready}"
    fi
    case "${action}" in
        pause)
            while [ -z "${go}" ] || [ ! -e "${go}" ]; do
                sleep 0.02
            done
            ;;
        fail)
            log_error "migration failpoint triggered: ${point}:${kind}:${name}"
            return 97
            ;;
        *)
            log_error "未知 migration failpoint action：${action}"
            return 2
            ;;
    esac
    return 0
}

migration_durable_move() {
    local kind="$1"
    local name="$2"
    local source="$3"
    local destination="$4"
    record_move_intent "${kind}" "${name}" || return 1
    migration_failpoint \
        "after_move_intent" "${kind}" "${name}" || return 1
    migration_rename_with_barriers \
        "${source}" "${destination}" || return 1
    migration_failpoint \
        "after_move_before_ack" "${kind}" "${name}" || return 1
    record_moved "${kind}" "${name}" || return 1
    migration_failpoint "after_move_ack" "${kind}" "${name}" || return 1
}

migration_remove_empty_directory() {
    local directory="$1"
    local parent
    if [ ! -e "${directory}" ] && [ ! -L "${directory}" ]; then
        return 0
    fi
    if [ ! -d "${directory}" ] || [ -L "${directory}" ]; then
        log_error "拒绝删除非普通 staging 目录：${directory}"
        return 1
    fi
    parent="$(dirname "${directory}")"
    if ! rmdir "${directory}"; then
        log_error "staging 目录非空或无法安全删除，保留现场：${directory}"
        return 1
    fi
    migration_fsync_directories "${parent}"
}

migration_remove_empty_staging() {
    migration_remove_empty_directory \
        "${TMP_ROOT}/releases/${INITIAL_ID}" || return 1
    migration_remove_empty_directory "${TMP_ROOT}/releases" || return 1
    migration_remove_empty_directory "${TMP_ROOT}/shared" || return 1
    migration_remove_empty_directory "${ROLLBACK_STAGE}" || return 1
    migration_remove_empty_directory "${TMP_ROOT}" || return 1
}

# 必须 root（或 ROOT 已经是当前用户写得动的）。
require_root_or_writable() {
    if [ "${EUID:-$(id -u)}" -eq 0 ]; then
        return 0
    fi
    if [ -w "${ROOT}" ] && [ -w "$(dirname "${ROOT}")" ]; then
        return 0
    fi
    log_error "需要 root 权限或对 ${ROOT} 及其父目录的写入权限。请用 sudo 重跑。"
    exit 1
}

validate_completed_layout() {
    local release_dir="${ROOT}/releases/${INITIAL_ID}"
    [ -L "${ROOT}/current" ] || return 1
    [ "$(readlink "${ROOT}/current" 2>/dev/null || true)" = \
        "releases/${INITIAL_ID}" ] || return 1
    [ -d "${release_dir}" ] || return 1
    [ -d "${ROOT}/shared" ] || return 1
    [ -f "${release_dir}/.lumen_release.json" ] || return 1
    [ -L "${release_dir}/apps/worker/var" ] || return 1
    [ "$(readlink "${release_dir}/apps/worker/var" 2>/dev/null || true)" = \
        "${ROOT}/shared/worker-var" ] || return 1
    [ -L "${release_dir}/apps/web/.next/cache" ] || return 1
    [ "$(readlink "${release_dir}/apps/web/.next/cache" 2>/dev/null || true)" = \
        "${ROOT}/shared/web-next-cache" ] || return 1
    if [ -f "${ROOT}/shared/.env" ]; then
        [ -L "${ROOT}/.env" ] || return 1
        [ "$(readlink "${ROOT}/.env" 2>/dev/null || true)" = "shared/.env" ] \
            || return 1
    fi
    python3 - "${release_dir}/.lumen_release.json" "${INITIAL_ID}" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
expected_id = sys.argv[2]
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    raise SystemExit(1)
if not isinstance(payload, dict) or payload.get("id") != expected_id:
    raise SystemExit(1)
PY
}

inspect_existing_layout() {
    if [ ! -d "${ROOT}" ]; then
        log_error "${ROOT} 不存在或不是目录，无法迁移。"
        return 1
    fi
    if [ -L "${ROOT}/current" ]; then
        if validate_completed_layout \
                && [ ! -e "${TMP_ROOT}" ] \
                && [ ! -e "${MIGRATION_LOCK_DIR}" ]; then
            log_info "${ROOT}/current 指向完整且已验证的 release 布局；幂等退出。"
            return 0
        fi
        log_error "${ROOT}/current 已存在，但完整 release 状态验证失败；拒绝误报迁移完成。"
        return 1
    fi
    if [ -e "${TMP_ROOT}" ]; then
        log_error "中转目录 ${TMP_ROOT} 已存在；上次迁移可能未完成。请手动检查后删除该目录再重跑。"
        return 1
    fi
    return 10
}

write_phase() {
    local phase="$1"
    local phase_tmp=""
    MIGRATION_PHASE="${phase}"
    [ -n "${PHASE_FILE}" ] || return 0
    phase_tmp="${PHASE_FILE}.tmp.$$"
    printf '%s\n' "${phase}" > "${phase_tmp}" || return 1
    migration_fsync_file_and_parent "${phase_tmp}" || return 1
    mv -f -- "${phase_tmp}" "${PHASE_FILE}" || return 1
    migration_fsync_file_and_parent "${PHASE_FILE}" || return 1
}

record_move_intent() {
    printf '%s\t%s\0' "$1" "$2" >> "${MOVE_INTENT_MANIFEST}" || return 1
    migration_fsync_file_and_parent "${MOVE_INTENT_MANIFEST}" || return 1
}

record_moved() {
    printf '%s\t%s\0' "$1" "$2" >> "${MOVED_MANIFEST}" || return 1
    migration_fsync_file_and_parent "${MOVED_MANIFEST}" || return 1
}

record_action() {
    printf '%s\n' "$1" >> "${ACTION_MANIFEST}" || return 1
    migration_fsync_file_and_parent "${ACTION_MANIFEST}"
}

action_recorded() {
    grep -Fxq "$1" "${ACTION_MANIFEST}" 2>/dev/null
}

acquire_migration_lock() {
    if ! lumen_try_create_owned_lock_dir \
            "${MIGRATION_LOCK_DIR}" script "migrate_to_releases.sh"; then
        local owner_pid=""
        owner_pid="$(lumen_lock_owner_pid "${MIGRATION_LOCK_DIR}")"
        if [ "${LUMEN_LAST_LOCK_STALE:-0}" = "1" ]; then
            log_error "检测到 stale release 迁移锁（owner pid=${owner_pid:-未知}）；为避免删除后来 owner，不自动回收。"
            log_error "确认没有迁移脚本运行后，请人工删除：${MIGRATION_LOCK_DIR}"
        else
            log_error "已有 release 迁移脚本在运行（owner pid=${owner_pid:-未知}）。"
        fi
        return 1
    fi
    MIGRATION_LOCK_OWNER_TOKEN="${LUMEN_LAST_LOCK_OWNER_TOKEN}"
    MIGRATION_STATE_DIR="${MIGRATION_LOCK_DIR}/${MIGRATION_LOCK_OWNER_TOKEN}"
    PHASE_FILE="${MIGRATION_STATE_DIR}/phase"
    MOVE_INTENT_MANIFEST="${MIGRATION_STATE_DIR}/move-intent.manifest"
    MOVED_MANIFEST="${MIGRATION_STATE_DIR}/moved.manifest"
    ACTION_MANIFEST="${MIGRATION_STATE_DIR}/actions.manifest"
    ACTIVE_SERVICES_MANIFEST="${MIGRATION_STATE_DIR}/active-services.manifest"
    (
        umask 077
        : > "${MOVE_INTENT_MANIFEST}"
        : > "${MOVED_MANIFEST}"
        : > "${ACTION_MANIFEST}"
        : > "${ACTIVE_SERVICES_MANIFEST}"
    )
    if ! migration_fsync_file_and_parent "${MIGRATION_STATE_DIR}/owner" \
            || ! migration_fsync_file_and_parent "${MOVE_INTENT_MANIFEST}" \
            || ! migration_fsync_file_and_parent "${MOVED_MANIFEST}" \
            || ! migration_fsync_file_and_parent "${ACTION_MANIFEST}" \
            || ! migration_fsync_file_and_parent \
                "${ACTIVE_SERVICES_MANIFEST}" \
            || ! migration_fsync_directories \
                "${MIGRATION_STATE_DIR}" "${MIGRATION_LOCK_DIR}" \
                "$(dirname "${MIGRATION_LOCK_DIR}")" \
            || ! write_phase "locked"; then
        log_error "无法持久化 release 迁移 journal；拒绝开始移动目录。"
        cleanup_migration_lock
        return 1
    fi
}

cleanup_migration_lock() {
    if [ -n "${MIGRATION_STATE_DIR}" ] && [ -d "${MIGRATION_STATE_DIR}" ]; then
        rm -rf -- "${MIGRATION_STATE_DIR}/systemd-backup" \
            "${MIGRATION_STATE_DIR}/systemd-rendered" 2>/dev/null || true
        rm -f -- "${PHASE_FILE}" "${PHASE_FILE}.tmp.$$" \
            "${MOVE_INTENT_MANIFEST}" "${MOVED_MANIFEST}" \
            "${ACTION_MANIFEST}" "${ACTIVE_SERVICES_MANIFEST}" \
            2>/dev/null || true
    fi
    if [ -n "${MIGRATION_LOCK_OWNER_TOKEN}" ]; then
        lumen_with_lock_release_owner \
            "${MIGRATION_LOCK_DIR}" "${MIGRATION_LOCK_OWNER_TOKEN}" || true
    fi
}

reset_migration_state_context() {
    MIGRATION_LOCK_OWNER_TOKEN=""
    MIGRATION_STATE_DIR=""
    MIGRATION_PHASE="preflight"
    MIGRATION_COMMITTED=0
    MIGRATION_RECOVERY_DONE=0
    MIGRATION_COMPLETE_FINALIZED=0
    PHASE_FILE=""
    MOVE_INTENT_MANIFEST=""
    MOVED_MANIFEST=""
    ACTION_MANIFEST=""
    ACTIVE_SERVICES_MANIFEST=""
}

adopt_stale_migration_lock() {
    local owner_file owner_dir owner_id phase
    [ -d "${MIGRATION_LOCK_DIR}" ] || return 1
    if [ "${LUMEN_LOCK_KIND:-}" != "flock" ]; then
        log_error "检测到未完成迁移，但当前平台没有全局 flock 互斥；拒绝非原子接管 stale journal。"
        return 2
    fi
    if ! lumen_lock_dir_stale "${MIGRATION_LOCK_DIR}"; then
        log_error "release 迁移 journal 仍由存活进程持有，拒绝接管。"
        return 2
    fi
    owner_file="$(lumen_lock_owner_file "${MIGRATION_LOCK_DIR}")"
    [ -n "${owner_file}" ] && [ -f "${owner_file}" ] || {
        log_error "stale release 迁移锁缺少唯一 owner journal。"
        return 2
    }
    owner_dir="$(dirname "${owner_file}")"
    owner_id="$(basename "${owner_dir}")"
    [ "$(sed -n 's/^script=//p' "${owner_file}" | head -1)" = \
        "migrate_to_releases.sh" ] || {
        log_error "stale 迁移锁 owner 类型不匹配，拒绝接管。"
        return 2
    }
    for phase in phase move-intent.manifest moved.manifest actions.manifest \
            active-services.manifest; do
        [ -f "${owner_dir}/${phase}" ] && [ ! -L "${owner_dir}/${phase}" ] || {
            log_error "stale 迁移 journal 缺少安全状态文件：${phase}"
            return 2
        }
    done
    phase="$(tr -d '\r\n' < "${owner_dir}/phase")"
    case "${phase}" in
        locked|inventory|stopping|stopped|moving|repacking|linking|sharing|metadata|systemd|starting|health|complete) ;;
        *)
            log_error "stale 迁移 journal phase 未知：${phase:-<empty>}；保留现场。"
            return 2
            ;;
    esac
    if ! lumen_write_lock_owner \
            "${owner_dir}" script "migrate_to_releases.sh"; then
        log_error "无法原子接管 stale 迁移 owner journal。"
        return 2
    fi
    MIGRATION_LOCK_OWNER_TOKEN="${owner_id}"
    MIGRATION_STATE_DIR="${owner_dir}"
    PHASE_FILE="${owner_dir}/phase"
    MOVE_INTENT_MANIFEST="${owner_dir}/move-intent.manifest"
    MOVED_MANIFEST="${owner_dir}/moved.manifest"
    ACTION_MANIFEST="${owner_dir}/actions.manifest"
    ACTIVE_SERVICES_MANIFEST="${owner_dir}/active-services.manifest"
    MIGRATION_PHASE="${phase}"
    log_warn "已在全局 flock 保护下接管 stale release 迁移 journal（phase=${phase}）。"
    return 0
}

restore_systemd_targets() {
    local backup_dir="${MIGRATION_STATE_DIR}/systemd-backup"
    local f=""
    local restored=0
    local restore_failed=0
    command -v systemctl >/dev/null 2>&1 || return 0
    for f in lumen-api.service lumen-web.service lumen-worker.service \
             lumen-tgbot.service lumen-update-runner.service \
             lumen-update.path lumen-update-warm.service lumen-update-warm.path \
             lumen-backup.service lumen-backup.timer lumen-backup.path \
             lumen-restore-runner.service lumen-restore.path \
             lumen-health-watchdog.service lumen-health-watchdog.timer \
             lumen-storage-mount.service \
             lumen-storage-apply.service lumen-storage-apply.path \
             lumen-storage-test.service lumen-storage-test.path; do
        if action_recorded "systemd-present:${f}"; then
            cp -f -- "${backup_dir}/${f}" "/etc/systemd/system/${f}" \
                2>/dev/null || restore_failed=1
            restored=1
        elif action_recorded "systemd-absent:${f}"; then
            rm -f -- "/etc/systemd/system/${f}" 2>/dev/null \
                || restore_failed=1
            restored=1
        fi
    done
    if action_recorded "storage-script-present"; then
        cp -f -- "${backup_dir}/lumen-storage-mount" \
            /usr/local/sbin/lumen-storage-mount 2>/dev/null \
            || restore_failed=1
        chmod 0755 /usr/local/sbin/lumen-storage-mount 2>/dev/null \
            || restore_failed=1
        restored=1
    elif action_recorded "storage-script-absent"; then
        rm -f -- /usr/local/sbin/lumen-storage-mount 2>/dev/null \
            || restore_failed=1
        restored=1
    fi
    if [ "${restored}" -eq 1 ]; then
        systemctl daemon-reload 2>/dev/null || restore_failed=1
    fi
    return "${restore_failed}"
}

remove_expected_current_link() {
    local current="${ROOT}/current"
    [ -L "${current}" ] || return 0
    if [ "$(readlink "${current}" 2>/dev/null || true)" = "releases/${INITIAL_ID}" ]; then
        rm -f -- "${current}"
    else
        log_error "拒绝删除 owner 不明的 current 软链：${current}"
        return 1
    fi
}

release_dir_for_recovery() {
    if [ -d "${ROOT}/releases/${INITIAL_ID}" ]; then
        printf '%s\n' "${ROOT}/releases/${INITIAL_ID}"
    elif [ -d "${TMP_ROOT}/releases/${INITIAL_ID}" ]; then
        printf '%s\n' "${TMP_ROOT}/releases/${INITIAL_ID}"
    fi
}

restore_shared_paths() {
    local rdir=""
    rdir="$(release_dir_for_recovery)"
    [ -n "${rdir}" ] || return 0

    if action_recorded "web-env-moved"; then
        if [ -L "${rdir}/apps/web/.env.local" ]; then
            rm -f -- "${rdir}/apps/web/.env.local"
        fi
        if [ -e "${ROOT}/shared/web-env/.env.local" ] \
                && [ ! -e "${rdir}/apps/web/.env.local" ]; then
            mkdir -p "${rdir}/apps/web"
            mv "${ROOT}/shared/web-env/.env.local" \
                "${rdir}/apps/web/.env.local"
        fi
    fi

    if action_recorded "worker-var-moved" \
            || action_recorded "worker-var-created"; then
        [ ! -L "${rdir}/apps/worker/var" ] \
            || rm -f -- "${rdir}/apps/worker/var"
    fi
    if action_recorded "worker-var-moved" \
            && [ -d "${ROOT}/shared/worker-var" ] \
            && [ ! -e "${rdir}/apps/worker/var" ]; then
        mkdir -p "${rdir}/apps/worker"
        mv "${ROOT}/shared/worker-var" "${rdir}/apps/worker/var"
    fi

    if action_recorded "web-cache-moved" \
            || action_recorded "web-cache-created"; then
        [ ! -L "${rdir}/apps/web/.next/cache" ] \
            || rm -f -- "${rdir}/apps/web/.next/cache"
    fi
    if action_recorded "web-cache-moved" \
            && [ -d "${ROOT}/shared/web-next-cache" ] \
            && [ ! -e "${rdir}/apps/web/.next/cache" ]; then
        mkdir -p "${rdir}/apps/web/.next"
        mv "${ROOT}/shared/web-next-cache" "${rdir}/apps/web/.next/cache"
    fi

    if action_recorded "root-env-shared"; then
        if [ -L "${ROOT}/.env" ] \
                && [ "$(readlink "${ROOT}/.env" 2>/dev/null || true)" = "shared/.env" ]; then
            rm -f -- "${ROOT}/.env"
        fi
        if [ -f "${ROOT}/shared/.env" ] && [ ! -e "${ROOT}/.env" ]; then
            mv "${ROOT}/shared/.env" "${ROOT}/.env"
        fi
    fi
}

migration_verify_staging_accounted() {
    case "${MIGRATION_PHASE}" in
        preflight|locked|inventory|stopping|stopped)
            if ! migration_path_exists "${TMP_ROOT}"; then
                return 0
            fi
            ;;
    esac
    if ! python3 - \
            "${ROOT}" "${TMP_ROOT}" "${ROLLBACK_STAGE}" "${INITIAL_ID}" \
            "${MIGRATION_PHASE}" "${MOVE_INTENT_MANIFEST}" \
            "${MOVED_MANIFEST}" <<'PY'
import os
import stat
import sys

root, tmp_root, rollback_stage, initial_id, phase, intent_path, moved_path = (
    sys.argv[1:]
)


def fail(message):
    print(f"migration staging accounting failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_manifest(path, label):
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        fail(f"{label} is not a regular file")
    with open(path, "rb") as handle:
        payload = handle.read()
    if payload and not payload.endswith(b"\0"):
        fail(f"{label} has a partial trailing record")
    records = []
    seen = set()
    for raw_record in payload.split(b"\0")[:-1]:
        if not raw_record or b"\t" not in raw_record:
            fail(f"{label} contains a malformed record")
        raw_kind, raw_name = raw_record.split(b"\t", 1)
        try:
            kind = raw_kind.decode("ascii")
        except UnicodeDecodeError:
            fail(f"{label} contains a non-ASCII record kind")
        name = os.fsdecode(raw_name)
        if kind == "top":
            if not name or name in {".", ".."} or "/" in name:
                fail(f"{label} contains an unsafe top-level name")
        elif kind == "env":
            if name != ".env":
                fail(f"{label} contains an invalid env record")
        elif kind == "repack":
            if name not in {"releases", "shared", ".env"}:
                fail(f"{label} contains an invalid repack record")
        else:
            fail(f"{label} contains an unknown record kind: {kind}")
        record = (kind, name)
        if record in seen:
            fail(f"{label} contains a duplicate record: {kind}:{name}")
        seen.add(record)
        records.append(record)
    return records, seen


def lexists(path):
    return os.path.lexists(path)


def directory_names(path, label):
    if not lexists(path):
        return set()
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        fail(f"{label} is not a regular directory")
    return {entry.name for entry in os.scandir(path)}


def generated_apps_skeleton(path):
    if not lexists(path):
        return False
    allowed = {
        "worker": "directory",
        "worker/var": "symlink",
        "web": "directory",
        "web/.next": "directory",
        "web/.next/cache": "symlink",
    }
    expected_links = {
        "worker/var": os.path.join(root, "shared", "worker-var"),
        "web/.next/cache": os.path.join(root, "shared", "web-next-cache"),
    }
    pending = [("", path)]
    while pending:
        relative_parent, current = pending.pop()
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return False
        for entry in os.scandir(current):
            relative = (
                entry.name
                if not relative_parent
                else f"{relative_parent}/{entry.name}"
            )
            expected_kind = allowed.get(relative)
            if expected_kind is None:
                return False
            entry_metadata = entry.stat(follow_symlinks=False)
            if expected_kind == "directory":
                if stat.S_ISLNK(entry_metadata.st_mode) \
                        or not stat.S_ISDIR(entry_metadata.st_mode):
                    return False
                pending.append((relative, entry.path))
            else:
                if not stat.S_ISLNK(entry_metadata.st_mode):
                    return False
                if os.readlink(entry.path) != expected_links[relative]:
                    return False
    return True


intent_records, intent_set = parse_manifest(intent_path, "move intent")
_, moved_set = parse_manifest(moved_path, "moved acknowledgement")
orphaned_acks = moved_set - intent_set
if orphaned_acks:
    kind, name = sorted(orphaned_acks)[0]
    fail(f"moved acknowledgement has no intent: {kind}:{name}")

top_names = {name for kind, name in intent_records if kind == "top"}
has_env_intent = ("env", ".env") in intent_set
repack_names = {name for kind, name in intent_records if kind == "repack"}

tmp_entries = directory_names(tmp_root, "temporary staging root")
unknown_tmp_entries = tmp_entries - {
    "releases",
    "shared",
    ".env",
    os.path.basename(rollback_stage),
}
if unknown_tmp_entries:
    fail(f"temporary staging root contains unknown entry: {sorted(unknown_tmp_entries)[0]}")

tmp_releases = os.path.join(tmp_root, "releases")
tmp_release_entries = directory_names(tmp_releases, "temporary releases directory")
if tmp_release_entries - {initial_id}:
    fail("temporary releases directory contains an unknown release")

tmp_shared = os.path.join(tmp_root, "shared")
if directory_names(tmp_shared, "temporary shared directory"):
    fail("temporary shared directory is not empty")

tmp_initial = os.path.join(tmp_releases, initial_id)
root_initial = os.path.join(root, "releases", initial_id)
stage_entries = directory_names(rollback_stage, "rollback staging directory")
unknown_stage_entries = stage_entries - top_names - ({".env"} if has_env_intent else set())
if unknown_stage_entries:
    fail(f"rollback staging contains unknown entry: {sorted(unknown_stage_entries)[0]}")

for release_dir, label in (
    (tmp_initial, "temporary initial release"),
    (root_initial, "root initial release"),
):
    release_entries = directory_names(release_dir, label)
    unknown_release_entries = release_entries - top_names - {".lumen_release.json"}
    if "apps" in unknown_release_entries \
            and generated_apps_skeleton(os.path.join(release_dir, "apps")):
        unknown_release_entries.remove("apps")
    if unknown_release_entries:
        fail(f"{label} contains entry without durable intent: {sorted(unknown_release_entries)[0]}")

post_move_phases = {
    "repacking",
    "linking",
    "sharing",
    "metadata",
    "systemd",
    "starting",
    "health",
    "complete",
}
if phase in post_move_phases:
    if lexists(os.path.join(root, "releases")) and "releases" not in repack_names:
        fail("root releases rename has no durable intent")
    if lexists(os.path.join(root, "shared")) and "shared" not in repack_names:
        fail("root shared rename has no durable intent")

for name in sorted(top_names):
    locations = [
        os.path.join(root, name),
        os.path.join(tmp_initial, name),
        os.path.join(root_initial, name),
        os.path.join(rollback_stage, name),
    ]
    present = [path for path in locations if lexists(path)]
    if len(present) != 1:
        fail(f"top-level entry {name!r} has {len(present)} recoverable locations")

env_locations = [
    os.path.join(root, ".env"),
    os.path.join(tmp_root, ".env"),
    os.path.join(root, "shared", ".env"),
    os.path.join(rollback_stage, ".env"),
]
env_present = [
    path
    for path in env_locations
    if lexists(path) and not os.path.islink(path)
]
if has_env_intent:
    if len(env_present) != 1:
        fail(f".env has {len(env_present)} recoverable locations")
elif any(
    lexists(path)
    for path in (
        os.path.join(tmp_root, ".env"),
        os.path.join(root, "shared", ".env"),
        os.path.join(rollback_stage, ".env"),
    )
):
    fail("staged .env has no durable intent")
PY
    then
        log_error "迁移 staging 无法由 durable move journal 完整解释；保留现场，拒绝递归删除。"
        return 1
    fi
}

rollback_before_repack() {
    local record="" kind="" name="" source=""
    migration_verify_staging_accounted || return 1
    if [ -f "${MOVE_INTENT_MANIFEST}" ]; then
        while IFS= read -r -d '' record; do
            kind="${record%%$'\t'*}"
            name="${record#*$'\t'}"
            case "${kind}" in
                top)
                    source="${TMP_ROOT}/releases/${INITIAL_ID}/${name}"
                    if { [ -e "${source}" ] || [ -L "${source}" ]; } \
                            && [ ! -e "${ROOT}/${name}" ] \
                            && [ ! -L "${ROOT}/${name}" ]; then
                        migration_rename_with_barriers \
                            "${source}" "${ROOT}/${name}" || return 1
                    fi
                    ;;
                env)
                    if [ -f "${TMP_ROOT}/.env" ] && [ ! -e "${ROOT}/.env" ]; then
                        migration_rename_with_barriers \
                            "${TMP_ROOT}/.env" "${ROOT}/.env" || return 1
                    fi
                    ;;
            esac
        done < "${MOVE_INTENT_MANIFEST}"
    fi
    migration_verify_staging_accounted || return 1
    migration_remove_empty_staging || return 1
    return 0
}

rollback_after_repack() {
    local stage="${ROLLBACK_STAGE}"
    local record="" kind="" name="" source=""
    local rollback_failed=0
    migration_verify_staging_accounted || return 1
    restore_shared_paths || rollback_failed=1
    remove_expected_current_link || rollback_failed=1
    mkdir -p "${stage}" || return 1
    migration_fsync_directories \
        "${stage}" "${TMP_ROOT}" "$(dirname "${TMP_ROOT}")" || return 1

    if [ -f "${MOVE_INTENT_MANIFEST}" ]; then
        while IFS= read -r -d '' record; do
            kind="${record%%$'\t'*}"
            name="${record#*$'\t'}"
            case "${kind}" in
                top)
                    source=""
                    if [ -e "${ROOT}/releases/${INITIAL_ID}/${name}" ] \
                            || [ -L "${ROOT}/releases/${INITIAL_ID}/${name}" ]; then
                        source="${ROOT}/releases/${INITIAL_ID}/${name}"
                    elif [ -e "${TMP_ROOT}/releases/${INITIAL_ID}/${name}" ] \
                            || [ -L "${TMP_ROOT}/releases/${INITIAL_ID}/${name}" ]; then
                        source="${TMP_ROOT}/releases/${INITIAL_ID}/${name}"
                    elif [ -e "${stage}/${name}" ] \
                            || [ -L "${stage}/${name}" ]; then
                        source=""
                    fi
                    if [ -n "${source}" ]; then
                        migration_rename_with_barriers \
                            "${source}" "${stage}/${name}" || rollback_failed=1
                    elif [ ! -e "${ROOT}/${name}" ] && [ ! -L "${ROOT}/${name}" ]; then
                        if [ ! -e "${stage}/${name}" ] \
                                && [ ! -L "${stage}/${name}" ]; then
                            log_error "回滚找不到原始顶层条目：${name}"
                            rollback_failed=1
                        fi
                    fi
                    ;;
                env)
                    source=""
                    if [ -f "${ROOT}/.env" ] && [ ! -L "${ROOT}/.env" ]; then
                        source="${ROOT}/.env"
                    elif [ -f "${ROOT}/shared/.env" ]; then
                        source="${ROOT}/shared/.env"
                    elif [ -f "${TMP_ROOT}/.env" ]; then
                        source="${TMP_ROOT}/.env"
                    fi
                    if [ -n "${source}" ]; then
                        migration_rename_with_barriers \
                            "${source}" "${stage}/.env" || rollback_failed=1
                    elif [ ! -f "${stage}/.env" ]; then
                        log_error "回滚找不到原始 .env"
                        rollback_failed=1
                    fi
                    ;;
            esac
    done < "${MOVE_INTENT_MANIFEST}"
    fi

    if [ "${rollback_failed}" -eq 0 ]; then
        migration_verify_staging_accounted || rollback_failed=1
    fi
    if [ "${rollback_failed}" -eq 0 ]; then
        [ ! -e "${ROOT}/releases" ] \
            || lumen_safe_rm_rf "${ROOT}/releases" || rollback_failed=1
        [ ! -e "${ROOT}/shared" ] \
            || lumen_safe_rm_rf "${ROOT}/shared" || rollback_failed=1
        migration_fsync_directories "${ROOT}" || rollback_failed=1
        if [ -d "${stage}" ]; then
            while IFS= read -r -d '' source; do
                name="$(basename "${source}")"
                if [ -e "${ROOT}/${name}" ] || [ -L "${ROOT}/${name}" ]; then
                    log_error "回滚目标已存在，拒绝覆盖：${ROOT}/${name}"
                    rollback_failed=1
                    continue
                fi
                migration_rename_with_barriers \
                    "${source}" "${ROOT}/${name}" || rollback_failed=1
            done < <(find "${stage}" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)
        fi
    fi

    if [ "${rollback_failed}" -eq 0 ]; then
        migration_verify_staging_accounted || rollback_failed=1
    fi
    if [ "${rollback_failed}" -eq 0 ]; then
        migration_remove_empty_staging || rollback_failed=1
    fi
    return "${rollback_failed}"
}

service_was_originally_active() {
    grep -Fxq "$1" "${ACTIVE_SERVICES_MANIFEST}" 2>/dev/null
}

verify_migration_core_readiness() {
    local check_api=0
    local check_worker=0
    local api_url="${LUMEN_API_READY_URL:-${LUMEN_API_HEALTH_URL:-http://127.0.0.1:8000/readyz}}"
    local attempts="${LUMEN_MIGRATION_CORE_READINESS_ATTEMPTS:-${LUMEN_MIGRATION_API_HEALTH_ATTEMPTS:-${LUMEN_API_HEALTH_ATTEMPTS:-60}}}"
    local interval="${LUMEN_MIGRATION_CORE_READINESS_INTERVAL_SECONDS:-${LUMEN_MIGRATION_ACTIVE_INTERVAL_SECONDS:-1}}"
    service_was_originally_active lumen-api.service && check_api=1
    service_was_originally_active lumen-worker.service && check_worker=1
    if [ "${check_api}" -eq 0 ] && [ "${check_worker}" -eq 0 ]; then
        return 0
    fi
    case "${attempts}:${interval}" in
        *[!0-9:]*|0:*|0[0-9]*:*)
            log_error "迁移核心 readiness 参数无效：attempts=${attempts}, interval=${interval}。"
            return 1
            ;;
    esac
    lumen_require_systemd_core_readiness \
        "${ROOT}" "${api_url}" "${attempts}" "${interval}" \
        "${check_api}" "${check_worker}"
}

restart_originally_active_services() {
    local restart_failed=0
    local u=""
    command -v systemctl >/dev/null 2>&1 || return 0
    for u in "${MIGRATION_START_UNITS[@]}"; do
        service_was_originally_active "${u}" || continue
        if systemctl start "${u}" \
                && systemctl is-active --quiet "${u}" 2>/dev/null; then
            log_warn "已恢复原先 active 的 ${u}"
        else
            log_error "恢复 ${u} 失败，请立即检查 journalctl -u ${u}"
            restart_failed=1
        fi
    done
    if [ "${restart_failed}" -eq 0 ] \
            && ! verify_migration_core_readiness; then
        log_error "恢复后的 API/Worker readiness 未通过，保留 release-layout journal。"
        restart_failed=1
    fi
    return "${restart_failed}"
}

stop_originally_active_services_for_recovery() {
    local stop_failed=0
    local u=""
    command -v systemctl >/dev/null 2>&1 || return 0
    for u in "${MIGRATION_UNITS[@]}"; do
        service_was_originally_active "${u}" || continue
        if ! systemctl stop "${u}" 2>/dev/null; then
            log_error "恢复前无法停止 ${u}。"
            stop_failed=1
        fi
    done
    return "${stop_failed}"
}

recover_migration() {
    local reason="$1"
    local recovery_failed=0
    if [ "${MIGRATION_RECOVERY_DONE}" -eq 1 ]; then
        return 0
    fi
    MIGRATION_RECOVERY_DONE=1
    if [ "${MIGRATION_PHASE}" = "complete" ]; then
        if ! validate_completed_layout; then
            log_error "phase=complete 的 stale migration journal 与当前布局不一致；绝不 rollback，保留现场。"
            return 1
        fi
        if ! verify_migration_core_readiness; then
            log_error "phase=complete journal 清理前 API/Worker readiness 失败；保留现场。"
            return 1
        fi
        if [ -e "${TMP_ROOT}" ]; then
            migration_verify_staging_accounted || return 1
            migration_remove_empty_staging || return 1
        fi
        cleanup_migration_lock
        MIGRATION_COMMITTED=1
        MIGRATION_COMPLETE_FINALIZED=1
        log_warn "phase=complete 的 stale migration journal 已 finalize cleanup；未执行 rollback。"
        return 0
    fi
    log_error "release 迁移未完成（phase=${MIGRATION_PHASE}, reason=${reason}），开始恢复。"
    case "${MIGRATION_PHASE}" in
        starting|health)
            stop_originally_active_services_for_recovery || recovery_failed=1
            ;;
    esac
    case "${MIGRATION_PHASE}" in
        preflight|locked|inventory|stopping|stopped|moving)
            rollback_before_repack || recovery_failed=1
            ;;
        *)
            rollback_after_repack || recovery_failed=1
            ;;
    esac
    restore_systemd_targets || recovery_failed=1
    restart_originally_active_services || recovery_failed=1
    if [ "${recovery_failed}" -ne 0 ]; then
        log_error "恢复证据与 owner 锁已保留：${MIGRATION_STATE_DIR}"
        log_error "迁移恢复不完整，请立即人工检查 ${ROOT} 和 lumen systemd units。"
    else
        cleanup_migration_lock
        log_warn "迁移恢复完成，原始目录布局和原先 active 的服务已恢复。"
    fi
    return "${recovery_failed}"
}

migration_exit_handler() {
    local rc="$1"
    local recovery_rc=0
    trap - EXIT
    trap '' INT TERM HUP
    set +e
    if [ "${MIGRATION_COMMITTED}" -eq 1 ]; then
        cleanup_migration_lock
    else
        recover_migration "exit:${rc}" || recovery_rc=$?
    fi
    if [ "${recovery_rc}" -ne 0 ]; then
        lumen_release_lock 2>/dev/null || true
        exit 70
    fi
    lumen_release_lock 2>/dev/null || true
    exit "${rc}"
}

migration_signal_handler() {
    local signal_name="$1"
    local signal_rc="$2"
    local recovery_rc=0
    trap - EXIT
    trap '' INT TERM HUP
    set +e
    if [ "${MIGRATION_COMMITTED}" -eq 1 ]; then
        cleanup_migration_lock
    else
        recover_migration "signal:${signal_name}" || recovery_rc=$?
    fi
    if [ "${recovery_rc}" -ne 0 ]; then
        lumen_release_lock 2>/dev/null || true
        exit 70
    fi
    lumen_release_lock 2>/dev/null || true
    exit "${signal_rc}"
}

install_recovery_traps() {
    trap 'migration_exit_handler "$?"' EXIT
    trap 'migration_signal_handler INT 130' INT
    trap 'migration_signal_handler TERM 143' TERM
    trap 'migration_signal_handler HUP 129' HUP
}

inventory_active_services() {
    local u=""
    write_phase "inventory"
    : > "${ACTIVE_SERVICES_MANIFEST}"
    migration_fsync_file_and_parent "${ACTIVE_SERVICES_MANIFEST}"
    if ! command -v systemctl >/dev/null 2>&1; then
        log_warn "未发现 systemctl，跳过服务状态记录（请确认服务未在运行）。"
        return 0
    fi
    for u in "${MIGRATION_UNITS[@]}"; do
        if systemctl list-unit-files "${u}" --no-legend 2>/dev/null \
                | awk '{print $1}' | grep -Fxq "${u}" \
                && systemctl is-active --quiet "${u}" 2>/dev/null; then
            printf '%s\n' "${u}" >> "${ACTIVE_SERVICES_MANIFEST}"
            migration_fsync_file_and_parent "${ACTIVE_SERVICES_MANIFEST}"
        fi
    done
}

stop_services() {
    local u=""
    local stop_failed=0
    write_phase "stopping"
    log_info "停止迁移前 active 的 lumen 服务"
    if ! command -v systemctl >/dev/null 2>&1; then
        write_phase "stopped"
        return 0
    fi
    for u in "${MIGRATION_UNITS[@]}"; do
        service_was_originally_active "${u}" || continue
        if ! systemctl stop "${u}"; then
            log_error "stop ${u} 失败；拒绝移动部署目录。"
            stop_failed=1
            break
        fi
    done
    if [ "${stop_failed}" -ne 0 ]; then
        return 1
    fi
    write_phase "stopped"
    return 0
}

move_to_release() {
    write_phase "moving"
    log_info "创建中转目录：${TMP_ROOT}"
    mkdir -p "${TMP_ROOT}/releases/${INITIAL_ID}" "${TMP_ROOT}/shared"
    migration_fsync_directories \
        "${TMP_ROOT}/releases/${INITIAL_ID}" \
        "${TMP_ROOT}/releases" "${TMP_ROOT}/shared" "${TMP_ROOT}" \
        "${ROOT}" "$(dirname "${TMP_ROOT}")"

    log_info "把 ${ROOT} 当前内容（除 .env）移到 ${TMP_ROOT}/releases/${INITIAL_ID}/"
    # 用 find -mindepth 1 -maxdepth 1 列出所有顶层条目（含点开头），逐个移动。
    # 跳过 .env（共享配置，迁移后挂在 ${ROOT}/.env）。
    local entry name
    while IFS= read -r -d '' entry; do
        name="$(basename "${entry}")"
        case "${name}" in
            ''|'.'|'..'|'.env'|'.lumen-maintenance.lock'|'.lumen-maintenance.lock.d')
                continue
                ;;
        esac
        migration_durable_move \
            "top" "${name}" "${entry}" \
            "${TMP_ROOT}/releases/${INITIAL_ID}/${name}"
    done < <(find "${ROOT}" -mindepth 1 -maxdepth 1 \
        \( -type d -o -type f -o -type l \) -print0 2>/dev/null)

    # 把 .env 单独搬到 ${TMP_ROOT}/.env
    if [ -f "${ROOT}/.env" ]; then
        migration_durable_move \
            "env" ".env" "${ROOT}/.env" "${TMP_ROOT}/.env"
    fi

    write_phase "repacking"
    log_info "把中转目录内容回填到 ${ROOT}"
    # 此时 ${ROOT} 已空。把 ${TMP_ROOT} 下的 releases / shared / .env 移过来。
    migration_durable_move \
        "repack" "releases" "${TMP_ROOT}/releases" "${ROOT}/releases"
    migration_durable_move \
        "repack" "shared" "${TMP_ROOT}/shared" "${ROOT}/shared"
    if [ -f "${TMP_ROOT}/.env" ]; then
        migration_durable_move \
            "repack" ".env" "${TMP_ROOT}/.env" "${ROOT}/.env"
    fi
    migration_remove_empty_staging
}

create_current_symlink() {
    write_phase "linking"
    log_info "创建 ${ROOT}/current -> releases/${INITIAL_ID}"
    record_action "current-link"
    lumen_atomic_replace_symlink \
        "releases/${INITIAL_ID}" "${ROOT}/current"
}

# 把 release 内的 .env.local / worker/var / web/.next/cache 搬到 shared/，再在 release 内回链。
# 三条独立处理，缺失的源直接跳过。
extract_to_shared() {
    local rdir="${ROOT}/releases/${INITIAL_ID}"
    write_phase "sharing"

    # apps/web/.env.local -> shared/web-env/.env.local
    if [ -f "${rdir}/apps/web/.env.local" ]; then
        log_info "把 apps/web/.env.local 移入 shared/web-env/"
        record_action "web-env-moved"
        mkdir -p "${ROOT}/shared/web-env"
        mv "${rdir}/apps/web/.env.local" "${ROOT}/shared/web-env/.env.local"
        ln -s "${ROOT}/shared/web-env/.env.local" "${rdir}/apps/web/.env.local"
    else
        log_warn "${rdir}/apps/web/.env.local 不存在，跳过（部署时再写入 shared/web-env/）"
        mkdir -p "${ROOT}/shared/web-env"
    fi

    # apps/worker/var -> shared/worker-var
    if [ -d "${rdir}/apps/worker/var" ]; then
        log_info "把 apps/worker/var 移入 shared/worker-var/"
        record_action "worker-var-moved"
        # shared/worker-var 必须先不存在或为空，否则 mv 不会原子合并目录
        if [ -d "${ROOT}/shared/worker-var" ]; then
            # 已存在（理论上不应该）：保留现有 shared，把 release 内的备份起来。
            log_warn "shared/worker-var 已存在，备份 release 内的目录到 .pre-migrate"
            mv "${rdir}/apps/worker/var" "${rdir}/apps/worker/var.pre-migrate.$(date -u +%Y%m%d%H%M%S)" || true
        else
            mv "${rdir}/apps/worker/var" "${ROOT}/shared/worker-var"
        fi
        ln -s "${ROOT}/shared/worker-var" "${rdir}/apps/worker/var"
    else
        log_warn "${rdir}/apps/worker/var 不存在，创建空 shared/worker-var/ 并回链"
        record_action "worker-var-created"
        mkdir -p "${ROOT}/shared/worker-var"
        mkdir -p "${rdir}/apps/worker"
        ln -s "${ROOT}/shared/worker-var" "${rdir}/apps/worker/var"
    fi

    # apps/web/.next/cache -> shared/web-next-cache
    if [ -d "${rdir}/apps/web/.next/cache" ]; then
        log_info "把 apps/web/.next/cache 移入 shared/web-next-cache/"
        record_action "web-cache-moved"
        if [ -d "${ROOT}/shared/web-next-cache" ]; then
            log_warn "shared/web-next-cache 已存在，备份 release 内的目录到 .pre-migrate"
            mv "${rdir}/apps/web/.next/cache" "${rdir}/apps/web/.next/cache.pre-migrate.$(date -u +%Y%m%d%H%M%S)" || true
        else
            mv "${rdir}/apps/web/.next/cache" "${ROOT}/shared/web-next-cache"
        fi
        ln -s "${ROOT}/shared/web-next-cache" "${rdir}/apps/web/.next/cache"
    else
        log_warn "${rdir}/apps/web/.next/cache 不存在，创建空 shared/web-next-cache/ 并回链"
        record_action "web-cache-created"
        mkdir -p "${ROOT}/shared/web-next-cache"
        mkdir -p "${rdir}/apps/web/.next"
        ln -s "${ROOT}/shared/web-next-cache" "${rdir}/apps/web/.next/cache"
    fi

    # docker compose 用的根 .env：搬到 shared/.env，并在 ROOT 留软链兜底。
    # 这样 update.sh 的 link_shared 能把它链入新 release（compose 在 release
    # 目录下找 .env 时不会再缺 DB_USER/REDIS_PASSWORD）。
    if [ -f "${ROOT}/.env" ] && [ ! -L "${ROOT}/.env" ]; then
        log_info "把 ${ROOT}/.env 移入 shared/.env 并在 ROOT 保留软链"
        record_action "root-env-shared"
        if [ -f "${ROOT}/shared/.env" ]; then
            log_warn "shared/.env 已存在，备份 ROOT/.env 到 .pre-migrate"
            mv "${ROOT}/.env" "${ROOT}/.env.pre-migrate.$(date -u +%Y%m%d%H%M%S)" || true
        else
            mv "${ROOT}/.env" "${ROOT}/shared/.env"
        fi
        ln -s "shared/.env" "${ROOT}/.env"
    elif [ ! -e "${ROOT}/shared/.env" ] && [ -L "${ROOT}/.env" ]; then
        log_warn "${ROOT}/.env 是软链但 shared/.env 不存在；请人工检查 .env 配置。"
    fi
}

# 复制最新 systemd unit 到 /etc/systemd/system/ 并 daemon-reload。
# 注意：unit 路径已经改为 /opt/lumen/current/...，这是迁移完成后才生效的路径。
deploy_systemd_units() {
    if ! command -v systemctl >/dev/null 2>&1; then
        log_warn "未检测到 systemctl，跳过 systemd unit 安装。请手动复制 deploy/systemd/*.service。"
        return 0
    fi
    write_phase "systemd"
    local src_dir="${ROOT}/current/deploy/systemd"
    if [ ! -d "${src_dir}" ]; then
        log_warn "找不到 ${src_dir}，无法复制 systemd unit。请手工部署。"
        return 0
    fi
    log_info "复制最新 systemd unit 到 /etc/systemd/system/"
    local data_root backup_root tmp_dir backup_dir boot_mount_safe=1
    data_root="${LUMEN_DATA_ROOT%/}"
    backup_root="${LUMEN_BACKUP_ROOT:-${data_root}/backup}"
    backup_root="${backup_root%/}"
    tmp_dir="${MIGRATION_STATE_DIR}/systemd-rendered"
    backup_dir="${MIGRATION_STATE_DIR}/systemd-backup"
    mkdir -p "${tmp_dir}" "${backup_dir}"
    if ! lumen_ensure_backup_service_user "${backup_root}"; then
        log_error "备份目录权限迁移失败，拒绝安装 systemd units。"
        return 1
    fi
    local f src dst
    for f in lumen-api.service lumen-web.service lumen-worker.service \
             lumen-tgbot.service lumen-update-runner.service \
             lumen-update.path lumen-update-warm.service lumen-update-warm.path \
             lumen-backup.service lumen-backup.timer lumen-backup.path \
             lumen-restore-runner.service lumen-restore.path \
             lumen-health-watchdog.service lumen-health-watchdog.timer \
             lumen-storage-mount.service \
             lumen-storage-apply.service lumen-storage-apply.path \
             lumen-storage-test.service lumen-storage-test.path; do
        if [ -f "${src_dir}/${f}" ]; then
            src="${src_dir}/${f}"
            dst="${tmp_dir}/${f}"
            if [ -f "/etc/systemd/system/${f}" ]; then
                cp -f -- "/etc/systemd/system/${f}" "${backup_dir}/${f}"
                record_action "systemd-present:${f}"
            else
                record_action "systemd-absent:${f}"
            fi
            case "${f}" in
                lumen-update.path|lumen-update-runner.service|lumen-update-warm.path|lumen-update-warm.service|lumen-backup.service|lumen-backup.timer|lumen-backup.path|lumen-restore-runner.service|lumen-restore.path|lumen-storage-mount.service|lumen-storage-apply.service|lumen-storage-apply.path|lumen-storage-test.service|lumen-storage-test.path)
                    render_update_runner_unit "${src}" "${dst}" "${data_root}" "${backup_root}" "${ROOT%/}"
                    ;;
                *)
                    cp -f "${src}" "${dst}"
                    ;;
            esac
            cp -f "${dst}" "/etc/systemd/system/${f}"
        fi
    done
    rm -rf "${tmp_dir}"
    # storage mount 控制脚本部署到 /usr/local/sbin（unit 通过绝对路径调用）
    local storage_script="${ROOT}/current/deploy/scripts/lumen_storage_mount.sh"
    if [ -f "${storage_script}" ]; then
        if [ -f /usr/local/sbin/lumen-storage-mount ]; then
            cp -f -- /usr/local/sbin/lumen-storage-mount \
                "${backup_dir}/lumen-storage-mount"
            record_action "storage-script-present"
        else
            record_action "storage-script-absent"
        fi
        install -m 0755 "${storage_script}" /usr/local/sbin/lumen-storage-mount
        log_info "  /usr/local/sbin/lumen-storage-mount"
    fi
    # storage 共享目录（host ↔ lumen-api 容器双向 bind）
    install -d -m 0770 -o root -g "${LUMEN_APP_STORAGE_GID:-${LUMEN_APP_GID:-10001}}" /var/lib/lumen-storage
    if [ -L /var/lib/lumen-storage/last-good.conf ] \
            || { [ -e /var/lib/lumen-storage/last-good.conf ] \
                && [ ! -f /var/lib/lumen-storage/last-good.conf ]; }; then
        log_error "last-good.conf 类型不安全，拒绝启用 boot-time storage mount"
        boot_mount_safe=0
    elif [ ! -e /var/lib/lumen-storage/last-good.conf ]; then
        if [ -L /var/lib/lumen-storage/unmanaged-direct ]; then
            log_error "unmanaged-direct marker 是符号链接，拒绝覆盖"
            boot_mount_safe=0
        else
            printf 'schema=1\nmode=unmanaged-direct\n' \
                > /var/lib/lumen-storage/unmanaged-direct
            chmod 0640 /var/lib/lumen-storage/unmanaged-direct
            chown "root:${LUMEN_APP_STORAGE_GID:-${LUMEN_APP_GID:-10001}}" \
                /var/lib/lumen-storage/unmanaged-direct
            sync -f /var/lib/lumen-storage/unmanaged-direct
        fi
    elif ! grep -Eq '^DATASET_IDENTITY=[0-9a-f]{64}$' \
            /var/lib/lumen-storage/last-good.conf; then
        if ! env \
                LUMEN_STORAGE_TARGET="${data_root}" \
                LUMEN_DB_ROOT="${LUMEN_DB_ROOT:-${data_root}}" \
                LUMEN_STORAGE_STATE_DIR=/var/lib/lumen-storage \
                LUMEN_DOCKER_COMPOSE_DIR="${ROOT}/current" \
                bash /usr/local/sbin/lumen-storage-mount bind-identity; then
            log_error "legacy storage dataset identity 升级失败，拒绝启用 boot mount"
            boot_mount_safe=0
        fi
    fi
    systemctl daemon-reload
    log_info "systemctl daemon-reload 完成"
    # 启用 storage path-watcher（admin UI 通过 trigger 文件触发 apply/test）
    systemctl enable --now lumen-storage-apply.path lumen-storage-test.path 2>/dev/null \
        || log_warn "启用 lumen-storage-{apply,test}.path 失败（继续）"
    if [ "${boot_mount_safe}" -eq 1 ]; then
        systemctl enable lumen-storage-mount.service 2>/dev/null \
            || log_warn "启用 lumen-storage-mount.service 失败（继续；重启后需手工恢复挂载）"
    else
        systemctl disable lumen-storage-mount.service >/dev/null 2>&1 || true
        log_error "boot-time storage mount 未启用；修复 storage identity 后重试迁移"
        return 1
    fi
    systemctl enable --now lumen-update.path 2>/dev/null \
        || log_warn "启用 lumen-update.path 失败（继续；面板一键更新可能不可用）"
    systemctl enable --now lumen-update-warm.path 2>/dev/null \
        || log_warn "启用 lumen-update-warm.path 失败（继续；镜像预热不可用）"
    systemctl enable --now lumen-backup.timer 2>/dev/null \
        || log_warn "启用 lumen-backup.timer 失败（继续；自动备份可能不可用）"
    systemctl enable --now lumen-backup.path 2>/dev/null \
        || log_warn "启用 lumen-backup.path 失败（继续；管理后台立即备份可能不可用）"
    systemctl enable --now lumen-restore.path 2>/dev/null \
        || log_warn "启用 lumen-restore.path 失败（继续；管理后台恢复可能不可用）"
}

# Release code, deployment roots, config, and root-executed scripts must remain
# root-owned. Only explicitly mutable runtime subtrees are delegated.
fix_ownership() {
    local runtime_owner="${LUMEN_APP_UID:-10001}"
    local runtime_group="${LUMEN_APP_GID:-10001}"
    local config_group="root"
    local backup_root="${LUMEN_BACKUP_ROOT:-${LUMEN_DATA_ROOT%/}/backup}"
    if ! lumen_ensure_backup_service_user "${backup_root}"; then
        log_error "备份目录权限迁移失败，拒绝完成 release ownership 收口。"
        return 1
    fi
    if id lumen >/dev/null 2>&1; then
        runtime_owner="lumen"
        runtime_group="$(id -gn lumen 2>/dev/null || printf 'lumen')"
        if command -v getent >/dev/null 2>&1 \
                && getent group "${LUMEN_BACKUP_SERVICE_GROUP:-lumen-backup}" \
                    >/dev/null 2>&1; then
            config_group="${LUMEN_BACKUP_SERVICE_GROUP:-lumen-backup}"
        fi
    fi
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then
        if [ "$(id -un)" = "lumen" ] \
                || [ "$(id -u)" = "${runtime_owner}" ]; then
            log_error "拒绝让运行服务账户拥有 release 代码；请以 root 或独立部署账户重跑。"
            return 1
        fi
        chmod -R go-w "${ROOT}/releases/${INITIAL_ID}" "${ROOT}/shared" \
            || return 1
        if [ -f "${ROOT}/shared/.env" ] \
                && [ ! -L "${ROOT}/shared/.env" ]; then
            chmod 0600 "${ROOT}/shared/.env" || return 1
        fi
        if [ -f "${ROOT}/shared/web-env/.env.local" ] \
                && [ ! -L "${ROOT}/shared/web-env/.env.local" ]; then
            chmod 0600 "${ROOT}/shared/web-env/.env.local" || return 1
        fi
        lumen_fsync_directory "${ROOT}/releases/${INITIAL_ID}" \
            && lumen_fsync_directory "${ROOT}/shared" \
            && lumen_fsync_directory "${ROOT}" || return 1
        log_info "release/shared 已由独立部署账户 $(id -un) 持有并移除 group/other 写权限。"
        return 0
    fi
    lumen_release_harden_ownership \
        "${ROOT}" "${ROOT}/releases/${INITIAL_ID}" "${ROOT}/shared" \
        "${runtime_owner}" "${runtime_group}" "${config_group}" || return 1
    log_info "release/shared 顶层、配置与运维脚本已 root-owned；runtime owner=${runtime_owner}:${runtime_group}。"
}

# 写 ${ROOT}/releases/${INITIAL_ID}/.lumen_release.json，让 admin_release 列表与回滚 UI
# 看到 sha / branch / alembic head。失败的字段留空，后端按 None 渲染。
write_initial_release_metadata() {
    local rdir="${ROOT}/releases/${INITIAL_ID}"
    local meta="${rdir}/.lumen_release.json"
    if [ -f "${meta}" ]; then
        log_info ".lumen_release.json 已存在，跳过覆盖"
        return 0
    fi

    write_phase "metadata"
    record_action "metadata-created"
    local sha="" branch="" head="" created_at
    created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    if [ -d "${rdir}/.git" ] && command -v git >/dev/null 2>&1; then
        sha="$(cd "${rdir}" && git rev-parse HEAD 2>/dev/null || true)"
        branch="$(cd "${rdir}" && git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    fi

    # alembic head best-effort：用 release 内的 .venv 跑 `alembic heads`，
    # 失败（venv 不存在 / 命令缺失 / 模块路径不对）就留空。
    if [ -x "${rdir}/.venv/bin/alembic" ] && [ -d "${rdir}/apps/api" ]; then
        head="$(cd "${rdir}/apps/api" \
            && "${rdir}/.venv/bin/alembic" heads 2>/dev/null \
            | awk 'NR==1{print $1}' || true)"
    fi

    log_info "写入 ${meta} (sha=${sha:-<unknown>} branch=${branch:-<unknown>} head=${head:-<unknown>})"
    cat > "${meta}" <<JSON
{
  "id": "${INITIAL_ID}",
  "sha": "${sha}",
  "branch": "${branch}",
  "created_at": "${created_at}",
  "alembic_head_expected": "${head}",
  "alembic_head_applied": "${head}"
}
JSON
    migration_fsync_file_and_parent "${meta}"
}

start_services() {
    if ! command -v systemctl >/dev/null 2>&1; then
        log_warn "未发现 systemctl，跳过启动服务。请手动启动。"
        return 0
    fi
    write_phase "starting"
    log_info "仅启动迁移前 active 的 lumen 服务"
    local start_failed=0
    local u=""
    for u in "${MIGRATION_START_UNITS[@]}"; do
        service_was_originally_active "${u}" || continue
        if ! systemctl start "${u}"; then
            log_error "启动 ${u} 失败，请检查 journalctl -u ${u}"
            start_failed=1
        fi
    done
    if [ "${start_failed}" -ne 0 ]; then
        log_error "至少一个 lumen systemd unit 启动失败。"
        return 1
    fi

    write_phase "health"
    local active_polls="${LUMEN_MIGRATION_ACTIVE_ATTEMPTS:-30}"
    local stable_polls="${LUMEN_MIGRATION_ACTIVE_STABLE_POLLS:-3}"
    local poll_interval="${LUMEN_MIGRATION_ACTIVE_INTERVAL_SECONDS:-1}"
    local web_url="${LUMEN_WEB_HEALTH_URL:-http://127.0.0.1:3000/}"
    local web_attempts="${LUMEN_MIGRATION_WEB_HEALTH_ATTEMPTS:-${LUMEN_WEB_HEALTH_ATTEMPTS:-60}}"
    local health_failed=0

    case "${active_polls}" in
        ''|*[!0-9]*|0|0[0-9]*)
            log_error "LUMEN_MIGRATION_ACTIVE_ATTEMPTS 必须是正整数。"
            return 1
            ;;
    esac
    case "${stable_polls}" in
        ''|*[!0-9]*|0|0[0-9]*)
            log_error "LUMEN_MIGRATION_ACTIVE_STABLE_POLLS 必须是正整数。"
            return 1
            ;;
    esac
    case "${poll_interval}" in
        ''|*[!0-9]*|0[0-9]*)
            log_error "LUMEN_MIGRATION_ACTIVE_INTERVAL_SECONDS 必须是非负整数。"
            return 1
            ;;
    esac
    case "${web_attempts}" in
        ''|*[!0-9]*|0|0[0-9]*)
            log_error "Web HTTP 健康检查次数必须是正整数。"
            return 1
            ;;
    esac
    if [ "${active_polls}" -lt 1 ] || [ "${stable_polls}" -lt 1 ] \
            || [ "${stable_polls}" -gt "${active_polls}" ]; then
        log_error "迁移 active 健康参数无效：attempts=${active_polls}, stable=${stable_polls}。"
        return 1
    fi

    wait_for_migration_unit_active() {
        local unit="$1"
        local poll=0
        local consecutive=0
        for ((poll = 1; poll <= active_polls; poll++)); do
            if systemctl is-active --quiet "${unit}" 2>/dev/null; then
                consecutive=$((consecutive + 1))
                if [ "${consecutive}" -ge "${stable_polls}" ]; then
                    return 0
                fi
            else
                consecutive=0
            fi
            if [ "${poll}" -lt "${active_polls}" ] && [ "${poll_interval}" -gt 0 ]; then
                sleep "${poll_interval}"
            fi
        done
        log_error "${unit} 启动后未能持续 active（${stable_polls}/${active_polls} 次检查）。"
        return 1
    }

    for u in "${MIGRATION_START_UNITS[@]}"; do
        service_was_originally_active "${u}" || continue
        if ! wait_for_migration_unit_active "${u}"; then
            health_failed=1
        fi
    done

    if ! verify_migration_core_readiness; then
        log_error "迁移后 API/Worker readiness 检查失败。"
        health_failed=1
    fi
    if service_was_originally_active lumen-web.service; then
        if lumen_wait_for_http_ok "${web_url}" "${web_attempts}"; then
            log_info "Web 健康检查通过：${web_url}"
        else
            log_error "Web 健康检查失败：${web_url}"
            health_failed=1
        fi
    fi

    if [ "${health_failed}" -ne 0 ]; then
        log_error "至少一个迁移后服务健康检查失败。"
        return 1
    fi
    return 0
}

main() {
    local existing_rc=0 adopt_rc=0
    require_root_or_writable
    lumen_acquire_lock "${ROOT}" "migrate_to_releases.sh"
    if [ -d "${MIGRATION_LOCK_DIR}" ]; then
        adopt_stale_migration_lock || adopt_rc=$?
        if [ "${adopt_rc}" -ne 0 ]; then
            return 1
        fi
        trap '' INT TERM HUP
        if ! recover_migration "stale-journal-resume"; then
            return 70
        fi
        if [ "${MIGRATION_COMPLETE_FINALIZED}" -eq 1 ]; then
            return 0
        fi
        reset_migration_state_context
        trap - INT TERM HUP
        log_warn "上次强制中断的迁移已恢复；本次重跑将从原始布局重新执行。"
    fi
    inspect_existing_layout || existing_rc=$?
    case "${existing_rc}" in
        0) return 0 ;;
        10) ;;
        *) return 1 ;;
    esac
    trap '' INT TERM HUP
    acquire_migration_lock
    install_recovery_traps
    # 锁在 ROOT 外，取得锁后重查，避免两个迁移进程通过首次布局检查。
    if [ -e "${TMP_ROOT}" ]; then
        log_error "中转目录 ${TMP_ROOT} 已存在；拒绝开始新的迁移。"
        return 1
    fi
    log_info "开始把 ${ROOT} 迁移为 release + symlink 布局"
    inventory_active_services
    if ! stop_services; then
        log_error "服务停止阶段失败；部署目录保持原状，迁移结果为失败。"
        return 1
    fi
    move_to_release
    create_current_symlink
    extract_to_shared
    write_initial_release_metadata
    fix_ownership
    deploy_systemd_units
    if ! lumen_verify_backup_service_layout_binding; then
        log_error "backup/maintenance root binding 在迁移服务激活前失效。"
        return 70
    fi
    if ! start_services; then
        log_error "服务启动不完整；将回滚 release 布局并恢复原 active 服务。"
        return 1
    fi
    write_phase "complete"
    migration_failpoint "after_complete"
    MIGRATION_COMMITTED=1
    log_info "迁移完成"
    log_info "  ROOT:        ${ROOT}"
    log_info "  current ->   releases/${INITIAL_ID}"
    log_info "  shared:      ${ROOT}/shared"
    log_info "可以通过 scripts/update.sh 触发后续 release 切换。"
}

main "$@"
