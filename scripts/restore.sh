#!/usr/bin/env bash
# Lumen 恢复：成对恢复指定 timestamp 的 PG + Redis 备份。
# 用法：restore.sh <timestamp>  （timestamp 形如 20260424-123000）
#
# 执行顺序：
#   1. 校验 PG 归档并恢复到临时库（不触碰活库）
#   2. 停 lumen-api、lumen-worker（避免切换期间写入）
#   3. 恢复 Redis（需要重启 container）
#   4. 将 PG 临时库切换为活库
#   5. 启 lumen-api、lumen-worker
#
# 失败时：只有在 PG/Redis 已证明回到同一时点后才重启 API/Worker；无法证明一致
# 时保持业务服务停止并 exit 70，避免带着跨存储错配继续处理任务。
set -euo pipefail

_LUMEN_RESTORE_INPUT_DEPLOY_ROOT="${LUMEN_DEPLOY_ROOT-}"
_LUMEN_RESTORE_INPUT_MAINT_ROOT="${LUMEN_MAINT_ROOT-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"

if [ ! -f "${SCRIPT_DIR}/lib.sh" ]; then
    echo "[restore] ERROR: ${SCRIPT_DIR}/lib.sh missing" >&2
    exit 1
fi
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"
# shellcheck source=lib/backup_restore_services.sh
. "${SCRIPT_DIR}/lib/backup_restore_services.sh"

if ! LUMEN_DEPLOY_ROOT="$(
        lumen_resolve_deploy_root \
            "${SCRIPT_DIR}" \
            "${_LUMEN_RESTORE_INPUT_DEPLOY_ROOT}" \
            "${_LUMEN_RESTORE_INPUT_MAINT_ROOT}"
)"; then
    echo "[restore] ERROR: refusing unsafe or ambiguous deployment root" >&2
    exit 78
fi
export LUMEN_DEPLOY_ROOT
unset _LUMEN_RESTORE_INPUT_DEPLOY_ROOT _LUMEN_RESTORE_INPUT_MAINT_ROOT

# 自动从 shared/.env 兜底：lumenctl 调用本脚本时只透传 LUMEN_* 系列 env，
# 不会传 REDIS_URL / REDIS_PASSWORD / DB_*。无 .env 兜底则 redis_cli 拿不到密码。
ENV_FILE="$(lumen_find_shared_env "${LUMEN_DEPLOY_ROOT}" 2>/dev/null || true)"
if [ -n "${ENV_FILE}" ]; then
    export LUMEN_ENV_FILE="${ENV_FILE}"
    for key in DB_USER DB_NAME DB_PASSWORD REDIS_URL REDIS_PASSWORD BACKUP_ROOT PG_CONTAINER REDIS_CONTAINER; do
        lumen_dotenv_export_if_unset "${key}" "${ENV_FILE}"
    done
fi

RESTORE_RECOVERY_ONLY=0
TS="${1:-}"
if [ "$TS" = "--recover-only" ]; then
    RESTORE_RECOVERY_ONLY=1
    TS="$(date -u +%Y%m%d-%H%M%S)"
elif [ -z "$TS" ]; then
    echo "usage: $0 <timestamp>" >&2
    exit 1
elif [[ ! "$TS" =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    echo "invalid timestamp: $TS (expected YYYYMMDD-HHMMSS)" >&2
    exit 1
fi

BACKUP_ROOT="${BACKUP_ROOT:-/opt/lumendata/backup}"
PG_FILE="$BACKUP_ROOT/pg/$TS.pg.dump.gz"
REDIS_FILE="$BACKUP_ROOT/redis/$TS.redis.tgz"
PG_CONTAINER="${PG_CONTAINER:-lumen-pg}"
REDIS_CONTAINER="${REDIS_CONTAINER:-lumen-redis}"
# 优先用 REDIS_URL 嵌入的密码（与 api/worker 共用同一真值）；兜底单独那一行 REDIS_PASSWORD。
REDIS_PASSWORD="$(lumen_redis_resolve_password)"
PG_USER="${DB_USER:-lumen}"
PG_DB="${DB_NAME:-lumen}"
LOCK_BASE="${LUMEN_BACKUP_RESTORE_LOCKDIR:-${XDG_RUNTIME_DIR:-/run/lock}}"
if [ ! -d "$LOCK_BASE" ] || [ ! -w "$LOCK_BASE" ]; then
    LOCK_BASE="${TMPDIR:-/tmp}"
fi
LOCKFILE="${LUMEN_BACKUP_RESTORE_LOCKFILE:-${LOCK_BASE}/lumen-backup-restore.lock}"
LOCKDIR="$LOCKFILE.d"
LOCK_KIND=""
RESTORE_LOCK_OWNER_TOKEN=""
TMP_DIR=""
SERVICES_STOPPED=0
REDIS_NEEDS_START=0
PG_TEMP_DB=""
PG_ROLLBACK_DB=""
PG_SWAP_IN_PROGRESS=0
# 1 = 库已经真的换成恢复后的数据（用于判断失败时该不该把 redis 也退回去）
PG_PROMOTED=0
# 1 = restored PG/Redis pair 的 committed phase 已持久化。
RESTORE_COMMITTED=0
REDIS_HOST_DIR=""
REDIS_BACKUP_DIR=""
REDIS_ORIGINAL_MANIFEST=""
REDIS_RESTORE_STATE="untouched"
REDIS_BOOTSTRAP_CONTAINER=""
REDIS_RESTORED_DB_SIZE=""
RESTORE_BACKUP_OPERATION_ID=""
RESTORE_BACKUP_PAIR_MARKER=""
RESTORE_BACKUP_PG_PATH=""
RESTORE_BACKUP_REDIS_PATH=""
RESTORE_BACKUP_PG_SIZE=0
RESTORE_BACKUP_REDIS_SIZE=0
RESTORE_BACKUP_PG_SHA256=""
RESTORE_BACKUP_REDIS_SHA256=""
ACTIVE_WRITER_SERVICES=()
ACTIVE_SITE_SERVICES=()
RESTORE_RECOVERY_FAILED=0
# redis 旧数据备份目录的前缀；名字里再拼 UTC 时间戳（可排序，用于轮转）+ pid。
REDIS_BACKUP_PREFIX=".lumen-restore-old."
# 最多保留几份中断操作留下的 rollback 目录（含本次临时目录）。正常 restore
# 会在 readiness commit 后删除本次目录；这里的轮转用于约束 crash leftovers。
REDIS_BACKUP_KEEP="${LUMEN_REDIS_RESTORE_BACKUP_KEEP:-2}"
case "$REDIS_BACKUP_KEEP" in
    ''|*[!0-9]*) REDIS_BACKUP_KEEP=2 ;;
    0) REDIS_BACKUP_KEEP=1 ;;
esac

log() { printf '[restore %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

restore_commit_is_durable() {
    [ "${RESTORE_COMMITTED:-0}" -eq 1 ] \
        || {
            [ "${RESTORE_JOURNAL_ACTIVE:-0}" -eq 1 ] \
                && [ "${RESTORE_JOURNAL_PHASE:-}" = "committed" ]
        }
}

restore_failpoint() {
    local phase="$1"
    local configured=",${LUMEN_RESTORE_FAILPOINT:-},${LUMEN_RESTORE_FAILPOINTS:-},"
    case "$configured" in
        *",${phase},"*)
            log "ERROR: restore crash failpoint triggered: ${phase}"
            kill -KILL "$$"
            sleep 1
            exit 137
            ;;
    esac
}

if [ ! -f "${SCRIPT_DIR}/lib/restore_journal.sh" ]; then
    log "ERROR: ${SCRIPT_DIR}/lib/restore_journal.sh missing"
    exit 1
fi
# shellcheck source=lib/restore_journal.sh
. "${SCRIPT_DIR}/lib/restore_journal.sh"

release_lock() {
    if [ "$LOCK_KIND" = "flock" ]; then
        flock -u 7 2>/dev/null || true
        exec 7>&- 2>/dev/null || true
    elif [ "$LOCK_KIND" = "mkdir" ]; then
        if ! lumen_release_owned_lock_dir \
                "$LOCKDIR" "${RESTORE_LOCK_OWNER_TOKEN:-}"; then
            log "WARN backup/restore lock owner changed; refusing removal: $LOCKDIR"
        fi
    fi
}

_restore_compose_start_services() {
    local -a services=(
        "${ACTIVE_WRITER_SERVICES[@]}"
        "${ACTIVE_SITE_SERVICES[@]}"
    )
    [ "${#services[@]}" -gt 0 ] || return 0
    lumen_start_services_verified "${services[@]}"
}

_restore_compose_stop_services() {
    if ! lumen_quiesce_all_writer_services; then
        return 1
    fi
    if [ "${#ACTIVE_SITE_SERVICES[@]}" -gt 0 ] \
            && ! lumen_quiesce_services "${ACTIVE_SITE_SERVICES[@]}"; then
        return 1
    fi
    return 0
}

cleanup() {
    local rc=$?
    local recovery_failed="${RESTORE_RECOVERY_FAILED:-0}"
    local bootstrap_cleanup_failed=0
    local committed=0
    trap - EXIT
    trap '' INT TERM HUP

    if restore_commit_is_durable; then
        committed=1
        RESTORE_COMMITTED=1
    fi
    if [ -n "${REDIS_BOOTSTRAP_CONTAINER:-}" ] \
            && ! redis_remove_bootstrap_container; then
        recovery_failed=1
        bootstrap_cleanup_failed=1
    fi
    if [ "${PG_SWAP_IN_PROGRESS:-0}" = "1" ] \
            || { [ "${PG_PROMOTED:-0}" = "1" ] && [ "$committed" -eq 0 ]; }; then
        if ! pg_recover_active_from_rollback; then
            recovery_failed=1
        fi
    fi
    if [ "$committed" -eq 1 ]; then
        if [ "${PG_PROMOTED:-0}" != "1" ]; then
            log "ERROR: committed restore no longer has a promoted postgres database"
            recovery_failed=1
        fi
        case "${REDIS_RESTORE_STATE:-untouched}" in
            applied|committed) REDIS_RESTORE_STATE="committed" ;;
            *) ;;
        esac
    elif [ "$recovery_failed" -eq 0 ]; then
        case "${REDIS_RESTORE_STATE:-untouched}" in
            stashing|stashed|applying|applied|committed|rolling_back)
                if [ "${bootstrap_cleanup_failed}" -eq 1 ]; then
                    REDIS_RESTORE_STATE="rollback_failed"
                elif ! redis_rollback_after_pg_failure; then
                    recovery_failed=1
                fi
                ;;
            rollback_failed)
                recovery_failed=1
                ;;
        esac
    fi
    if [ "$REDIS_NEEDS_START" -eq 1 ] && [ "$recovery_failed" -eq 0 ]; then
        log "starting redis container"
        if ensure_redis_started; then
            REDIS_NEEDS_START=0
        else
            log "ERROR: failed to start redis container during cleanup"
            recovery_failed=1
        fi
    fi
    if [ -n "${PG_TEMP_DB:-}" ]; then
        pg_drop_database_if_exists "$PG_TEMP_DB" >/dev/null 2>&1 || true
    fi
    if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR" 2>/dev/null || true
    fi
    if [ "$committed" -eq 1 ] && [ "$recovery_failed" -eq 0 ]; then
        if ! pg_discard_rollback_after_success; then
            recovery_failed=1
        fi
    fi
    if [ "$SERVICES_STOPPED" -eq 1 ]; then
        if [ "$recovery_failed" -eq 0 ]; then
            log "starting original services: writers=${ACTIVE_WRITER_SERVICES[*]:-<none>} site=${ACTIVE_SITE_SERVICES[*]:-<none>}"
            if ! _restore_compose_start_services; then
                log "ERROR: failed to restart api + worker during cleanup"
                recovery_failed=1
                _restore_compose_stop_services >/dev/null 2>&1 || true
            else
                SERVICES_STOPPED=0
            fi
        else
            log "ERROR: restore recovery incomplete; refusing to restart writers"
        fi
    fi
    if [ "$recovery_failed" -eq 0 ] \
            && [ "${RESTORE_JOURNAL_ACTIVE:-0}" -eq 1 ]; then
        if ! lumen_restore_journal_clear; then
            recovery_failed=1
        fi
    fi
    release_lock
    if command -v lumen_release_lock >/dev/null 2>&1; then
        lumen_release_lock 2>/dev/null || true
    fi
    if [ "$recovery_failed" -ne 0 ]; then
        log "ERROR: restore cleanup could not prove a consistent service state"
        exit 70
    fi
    return "$rc"
}

on_signal() {
    local sig="$1"
    local rc
    case "$sig" in
        HUP) rc=129 ;;
        INT) rc=130 ;;
        TERM) rc=143 ;;
        *) rc=128 ;;
    esac
    log "ERROR: interrupted by SIG$sig"
    exit "$rc"
}

acquire_lock() {
    local lock_parent
    lock_parent="$(dirname "$LOCKFILE")"
    mkdir -p "$lock_parent"

    if command -v flock >/dev/null 2>&1; then
        if ! { exec 7>"$LOCKFILE"; } 2>/dev/null; then
            log "ERROR: cannot open lock file: $LOCKFILE"
            exit 10
        fi
        if ! flock -n 7; then
            log "ERROR: another backup/restore is already running (lock: $LOCKFILE)"
            exit 10
        fi
        LOCK_KIND="flock"
        return 0
    fi

    if lumen_try_create_owned_lock_dir "$LOCKDIR" script "restore.sh"; then
        RESTORE_LOCK_OWNER_TOKEN="${LUMEN_LAST_LOCK_OWNER_TOKEN}"
        LOCK_KIND="mkdir"
        return 0
    fi

    local _owner_pid=""
    _owner_pid="$(lumen_lock_owner_pid "$LOCKDIR")"
    if [ "${LUMEN_LAST_LOCK_STALE:-0}" = "1" ]; then
        log "ERROR: stale backup/restore lock detected (owner=${LUMEN_LAST_STALE_LOCK_PID:-${_owner_pid:-未知}}); refusing automatic removal"
        log "ERROR: confirm no backup/restore process is running, then remove: $LOCKDIR"
    fi

    log "ERROR: another backup/restore is already running (lock: $LOCKDIR, owner=${_owner_pid:-未知})"
    exit 10
}

make_tmp_dir() {
    local base
    local tmp_dir
    for base in "${TMPDIR:-}" /var/tmp /tmp "$BACKUP_ROOT/.tmp"; do
        [ -n "$base" ] || continue
        mkdir -p "$base" 2>/dev/null || true
        if tmp_dir="$(mktemp -d "$base/lumen-restore.XXXXXXXXXX" 2>/dev/null)"; then
            printf '%s\n' "$tmp_dir"
            return 0
        fi
    done
    log "ERROR: failed to create temporary directory"
    exit 5
}

pg_quote_ident() {
    printf '"'
    printf '%s' "$1" | sed 's/"/""/g'
    printf '"'
}

pg_quote_literal() {
    printf "'"
    printf '%s' "$1" | sed "s/'/''/g"
    printf "'"
}

pg_exec_postgres() {
    local sql="$1"
    docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres -c "$sql" >/dev/null
}

pg_database_exists() {
    local db="$1"
    local db_literal out
    db_literal="$(pg_quote_literal "$db")"
    if ! out="$(docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = $db_literal" 2>/dev/null)"; then
        return 2
    fi
    out="$(printf '%s' "$out" | tr -d '[:space:]')"
    [ "$out" = "1" ]
}

pg_terminate_db_connections() {
    local db="$1"
    local db_literal
    db_literal="$(pg_quote_literal "$db")"
    pg_exec_postgres "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $db_literal AND pid <> pg_backend_pid();"
}

pg_drop_database_if_exists() {
    local db="$1"
    local db_ident
    db_ident="$(pg_quote_ident "$db")"
    pg_terminate_db_connections "$db" || return $?
    pg_exec_postgres "DROP DATABASE IF EXISTS $db_ident;"
}

pg_create_database() {
    local db="$1"
    local db_ident owner_ident
    db_ident="$(pg_quote_ident "$db")"
    owner_ident="$(pg_quote_ident "$PG_USER")"
    pg_exec_postgres "CREATE DATABASE $db_ident OWNER $owner_ident;"
}

pg_rename_database() {
    local from_db="$1"
    local to_db="$2"
    local from_ident to_ident
    from_ident="$(pg_quote_ident "$from_db")"
    to_ident="$(pg_quote_ident "$to_db")"
    pg_terminate_db_connections "$from_db" || return $?
    pg_exec_postgres "ALTER DATABASE $from_ident RENAME TO $to_ident;"
}

pg_validate_archive_list() {
    local gunzip_rc pg_restore_rc
    local -a pipe_status
    log "validating postgres archive catalog with pg_restore --list"
    set +e
    gunzip -c "$PG_FILE" | docker exec -i "$PG_CONTAINER" pg_restore --list >/dev/null
    pipe_status=("${PIPESTATUS[@]}")
    gunzip_rc=${pipe_status[0]}
    pg_restore_rc=${pipe_status[1]}
    set -e
    if [ "$gunzip_rc" -ne 0 ]; then
        log "ERROR: failed to read pg dump during archive catalog validation (gunzip exit $gunzip_rc)"
        return 1
    fi
    if [ "$pg_restore_rc" -ne 0 ]; then
        log "ERROR: pg_restore --list failed with exit $pg_restore_rc"
        return 1
    fi
}

restore_pg_archive_to_db() {
    local target_db="$1"
    local label="$2"
    local gunzip_rc pg_restore_rc
    local -a pipe_status
    set +e
    gunzip -c "$PG_FILE" | docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$target_db" --no-owner --no-acl
    pipe_status=("${PIPESTATUS[@]}")
    gunzip_rc=${pipe_status[0]}
    pg_restore_rc=${pipe_status[1]}
    set -e
    if [ "$pg_restore_rc" -ne 0 ]; then
        log "ERROR: pg_restore into $label failed with exit $pg_restore_rc"
        return 1
    fi
    if [ "$gunzip_rc" -ne 0 ]; then
        log "ERROR: failed to read pg dump during restore into $label (gunzip exit $gunzip_rc)"
        return 1
    fi
}

pg_prepare_staged_restore() {
    PG_TEMP_DB="lumen_restore_${TS//-/}_$$"
    log "staging postgres restore into temporary database $PG_TEMP_DB"

    if ! pg_drop_database_if_exists "$PG_TEMP_DB"; then
        log "ERROR: failed to clear postgres temporary restore database $PG_TEMP_DB"
        return 1
    fi
    if ! pg_create_database "$PG_TEMP_DB"; then
        log "ERROR: failed to create postgres temporary restore database $PG_TEMP_DB"
        return 1
    fi
    if ! restore_pg_archive_to_db "$PG_TEMP_DB" "temporary database $PG_TEMP_DB"; then
        log "ERROR: staged postgres restore failed; active database $PG_DB was not modified"
        pg_drop_database_if_exists "$PG_TEMP_DB" || log "WARN: failed to drop temporary postgres database $PG_TEMP_DB"
        PG_TEMP_DB=""
        return 1
    fi

    log "postgres staged restore ready: $PG_TEMP_DB"
}

_restore_quiesce_for_storage_rollback() {
    log "quiescing application services before restoring the pre-restore data pair"
    if ! _restore_compose_stop_services; then
        log "ERROR: failed to stop writers before storage rollback"
        return 1
    fi
    SERVICES_STOPPED=1
}

pg_prepare_uncommitted_rollback() {
    PG_PROMOTED=0
    PG_SWAP_IN_PROGRESS=1
    if [ "${RESTORE_JOURNAL_ACTIVE:-0}" -eq 1 ] \
            && ! lumen_restore_journal_write "pg_promoting"; then
        log "ERROR: failed to persist postgres rollback intent"
        return 1
    fi
}

pg_mark_uncommitted_rollback_complete() {
    PG_ROLLBACK_DB=""
    PG_SWAP_IN_PROGRESS=0
    PG_PROMOTED=0
    if [ "${RESTORE_JOURNAL_ACTIVE:-0}" -eq 1 ] \
            && ! lumen_restore_journal_write "pg_rolled_back"; then
        log "WARN: postgres rollback completed but pg_rolled_back phase was not persisted"
    fi
    restore_failpoint after_pg_rollback
}

pg_recover_active_from_rollback() {
    local active_exists_rc rollback_exists_rc temp_exists_rc committed=0
    if restore_commit_is_durable; then
        committed=1
    fi
    set +e
    pg_database_exists "$PG_DB"
    active_exists_rc=$?
    if [ -n "${PG_ROLLBACK_DB:-}" ]; then
        pg_database_exists "$PG_ROLLBACK_DB"
        rollback_exists_rc=$?
    else
        rollback_exists_rc=1
    fi
    if [ -n "${PG_TEMP_DB:-}" ]; then
        pg_database_exists "$PG_TEMP_DB"
        temp_exists_rc=$?
    else
        temp_exists_rc=1
    fi
    set -e

    if [ "$active_exists_rc" -gt 1 ] || [ "$rollback_exists_rc" -gt 1 ] \
            || [ "$temp_exists_rc" -gt 1 ]; then
        log "ERROR: failed to inspect postgres databases during swap recovery"
        return 1
    fi

    if [ -z "${PG_ROLLBACK_DB:-}" ]; then
        if [ "$committed" -eq 1 ] \
                && [ "$active_exists_rc" -eq 0 ] \
                && [ "$temp_exists_rc" -eq 1 ]; then
            PG_TEMP_DB=""
            PG_SWAP_IN_PROGRESS=0
            PG_PROMOTED=1
            log "postgres promotion without rollback was already committed before interruption"
            return 0
        fi
        if [ "$committed" -eq 1 ]; then
            log "ERROR: committed postgres promotion without rollback is missing its active database"
            return 1
        fi
        if [ "$active_exists_rc" -eq 1 ]; then
            [ "$temp_exists_rc" -eq 0 ] || PG_TEMP_DB=""
            PG_SWAP_IN_PROGRESS=0
            PG_PROMOTED=0
            log "postgres promotion without a prior active database had not committed"
            return 0
        fi
        if [ "$active_exists_rc" -eq 0 ] && [ "$temp_exists_rc" -eq 1 ]; then
            if ! _restore_quiesce_for_storage_rollback; then
                return 1
            fi
            if ! pg_prepare_uncommitted_rollback; then
                return 1
            fi
            log "dropping uncommitted promoted postgres database $PG_DB"
            if ! pg_drop_database_if_exists "$PG_DB"; then
                log "ERROR: failed to remove uncommitted promoted postgres database $PG_DB"
                return 1
            fi
            PG_TEMP_DB=""
            pg_mark_uncommitted_rollback_complete
            log "postgres restored to its pre-restore state with no active database"
            return 0
        fi
        log "ERROR: ambiguous uncommitted postgres promotion without rollback: active=$active_exists_rc temp=$temp_exists_rc"
        return 1
    fi

    if [ "$committed" -eq 1 ] \
            && [ "$active_exists_rc" -eq 0 ] \
            && [ "$temp_exists_rc" -eq 1 ]; then
        PG_TEMP_DB=""
        [ "$rollback_exists_rc" -eq 0 ] || PG_ROLLBACK_DB=""
        PG_SWAP_IN_PROGRESS=0
        PG_PROMOTED=1
        log "postgres promotion was committed after readiness; keeping restored PG/Redis state"
        return 0
    fi
    if [ "$committed" -eq 1 ]; then
        log "ERROR: committed postgres swap state is invalid: active=$active_exists_rc rollback=$rollback_exists_rc temp=$temp_exists_rc"
        return 1
    fi

    if [ "$active_exists_rc" -eq 0 ] && [ "$rollback_exists_rc" -eq 1 ] \
            && [ "$temp_exists_rc" -eq 0 ]; then
        # 第一次 rename 尚未提交，active 仍是原库。
        PG_ROLLBACK_DB=""
        PG_SWAP_IN_PROGRESS=0
        log "postgres active database $PG_DB was unchanged before interruption"
        return 0
    fi

    if [ "$active_exists_rc" -eq 0 ] && [ "$rollback_exists_rc" -eq 1 ] \
            && [ "$temp_exists_rc" -eq 1 ] && [ "$PG_PROMOTED" -eq 0 ]; then
        PG_TEMP_DB=""
        PG_ROLLBACK_DB=""
        PG_SWAP_IN_PROGRESS=0
        log "postgres rollback had already restored the pre-restore active database"
        return 0
    fi

    if [ "$rollback_exists_rc" -ne 0 ] \
            || { [ "$active_exists_rc" -eq 0 ] && [ "$temp_exists_rc" -eq 0 ]; }; then
        log "ERROR: uncommitted postgres swap has no provable rollback path: active=$active_exists_rc rollback=$rollback_exists_rc temp=$temp_exists_rc"
        return 1
    fi

    if ! _restore_quiesce_for_storage_rollback; then
        return 1
    fi
    if ! pg_prepare_uncommitted_rollback; then
        return 1
    fi
    if [ "$active_exists_rc" -eq 0 ]; then
        log "dropping uncommitted promoted postgres database $PG_DB before rollback"
        if ! pg_drop_database_if_exists "$PG_DB"; then
            log "ERROR: failed to remove uncommitted promoted postgres database $PG_DB"
            return 1
        fi
    fi
    log "attempting postgres rollback swap: $PG_ROLLBACK_DB -> $PG_DB"
    if pg_rename_database "$PG_ROLLBACK_DB" "$PG_DB"; then
        [ "$temp_exists_rc" -eq 0 ] || PG_TEMP_DB=""
        pg_mark_uncommitted_rollback_complete
        log "postgres active database restored from rollback"
        return 0
    fi
    log "ERROR: postgres rollback database $PG_ROLLBACK_DB could not be renamed back to $PG_DB"
    return 1
}

pg_discard_rollback_after_success() {
    local rollback_db="${PG_ROLLBACK_DB:-}"
    if [ -n "$rollback_db" ]; then
        log "dropping postgres rollback database after readiness commit: $rollback_db"
        if ! pg_drop_database_if_exists "$rollback_db"; then
            log "WARN: failed to drop postgres rollback database $rollback_db; retaining the paired Redis rollback"
            return 1
        fi
        PG_ROLLBACK_DB=""
        log "postgres rollback database dropped: $rollback_db"
    fi
    redis_discard_rollback_after_success
}

pg_promote_staged_restore() {
    if [ -z "${PG_TEMP_DB:-}" ]; then
        log "ERROR: no staged postgres restore database is available"
        return 1
    fi

    local active_exists_rc
    set +e
    pg_database_exists "$PG_DB"
    active_exists_rc=$?
    set -e
    if [ "$active_exists_rc" -eq 1 ]; then
        log "WARN: active postgres database $PG_DB does not exist; promoting staged restore without rollback database"
        PG_SWAP_IN_PROGRESS=1
        if ! lumen_restore_journal_write "pg_promoting"; then
            return 1
        fi
        if ! pg_rename_database "$PG_TEMP_DB" "$PG_DB"; then
            log "ERROR: failed to promote postgres temporary restore database $PG_TEMP_DB"
            if ! pg_recover_active_from_rollback; then
                log "ERROR: could not reconcile postgres after promotion without rollback failed"
            fi
            return 1
        fi
        PG_TEMP_DB=""
        PG_PROMOTED=1
        PG_SWAP_IN_PROGRESS=0
        if ! lumen_restore_journal_write "pg_promoted"; then
            return 1
        fi
        log "postgres staged database promoted"
        return 0
    fi
    if [ "$active_exists_rc" -ne 0 ]; then
        log "ERROR: failed to inspect active postgres database $PG_DB"
        return 1
    fi

    PG_ROLLBACK_DB="lumen_rollback_${TS//-/}_$$"
    log "swapping postgres database: active $PG_DB -> rollback $PG_ROLLBACK_DB; staged $PG_TEMP_DB -> active"
    if ! pg_drop_database_if_exists "$PG_ROLLBACK_DB"; then
        log "ERROR: failed to clear postgres rollback database $PG_ROLLBACK_DB"
        return 1
    fi

    PG_SWAP_IN_PROGRESS=1
    if ! lumen_restore_journal_write "pg_promoting"; then
        return 1
    fi
    if ! pg_rename_database "$PG_DB" "$PG_ROLLBACK_DB"; then
        log "ERROR: failed to move active postgres database $PG_DB to rollback database"
        if ! pg_recover_active_from_rollback; then
            log "ERROR: could not reconcile postgres after the failed active-to-rollback rename"
        fi
        return 1
    fi
    if ! pg_rename_database "$PG_TEMP_DB" "$PG_DB"; then
        log "ERROR: failed to promote postgres temporary restore database $PG_TEMP_DB"
        if ! pg_recover_active_from_rollback; then
            log "ERROR: active database $PG_DB is unavailable; staged=$PG_TEMP_DB rollback=$PG_ROLLBACK_DB"
        fi
        return 1
    fi

    PG_TEMP_DB=""
    # 数据库已经切到恢复版本，但 readiness 尚未通过，因此 rollback 数据库必须
    # 保留。只有 durable committed phase 之后才能清理。
    PG_PROMOTED=1
    PG_SWAP_IN_PROGRESS=0
    if ! lumen_restore_journal_write "pg_promoted"; then
        return 1
    fi
    log "postgres staged database promoted; rollback retained pending readiness"
}

redis_cli() {
    # redis-cli 把协议错误（NOAUTH/WRONGPASS/...）当作正常回复打印到 stdout 并 exit 0；
    # 必须 wrapper 里识别协议错误。否则 ping 检查会把 "AUTH failed" 误识别成"未起来"。
    local out rc
    if [ -n "$REDIS_PASSWORD" ]; then
        out="$(REDISCLI_AUTH="$REDIS_PASSWORD" docker exec -e REDISCLI_AUTH "$REDIS_CONTAINER" redis-cli --no-auth-warning "$@" 2>&1)"
    else
        out="$(docker exec "$REDIS_CONTAINER" redis-cli "$@" 2>&1)"
    fi
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log "ERROR: redis-cli $* exit=$rc out=${out}"
        return "$rc"
    fi
    if lumen_redis_is_error_reply "$out"; then
        log "ERROR: redis-cli $* protocol error: ${out}"
        return 1
    fi
    printf '%s' "$out"
}

redis_ping_quiet() {
    local out rc
    if [ -n "$REDIS_PASSWORD" ]; then
        out="$(REDISCLI_AUTH="$REDIS_PASSWORD" docker exec -e REDISCLI_AUTH "$REDIS_CONTAINER" redis-cli --no-auth-warning PING 2>/dev/null)"
        rc=$?
    else
        out="$(docker exec "$REDIS_CONTAINER" redis-cli PING 2>/dev/null)"
        rc=$?
    fi
    [ "$rc" -eq 0 ] && [ "$out" = "PONG" ]
}

redis_remove_bootstrap_container() {
    local container="${REDIS_BOOTSTRAP_CONTAINER:-}"
    [ -n "${container}" ] || return 0
    if ! docker rm -f "${container}" >/dev/null 2>&1; then
        log "ERROR: failed to remove Redis RDB bootstrap container ${container}"
        return 1
    fi
    REDIS_BOOTSTRAP_CONTAINER=""
}

redis_cleanup_stale_bootstrap_containers() {
    local container="" listing=""
    local -a containers=()
    if ! listing="$(
        docker ps -aq \
            --filter "label=com.lumen.restore.redis-bootstrap=1" \
            2>/dev/null
    )"; then
        log "ERROR: cannot enumerate stale Redis RDB bootstrap containers"
        return 1
    fi
    while IFS= read -r container; do
        [ -n "${container}" ] && containers+=("${container}")
    done <<< "${listing}"
    [ "${#containers[@]}" -eq 0 ] && return 0
    if ! docker rm -f "${containers[@]}" >/dev/null 2>&1; then
        log "ERROR: failed to remove stale Redis RDB bootstrap container"
        return 1
    fi
}

redis_bootstrap_cli() {
    local out rc
    out="$(
        docker exec "${REDIS_BOOTSTRAP_CONTAINER}" \
            redis-cli --no-auth-warning \
            -s /tmp/lumen-restore-bootstrap.sock "$@" 2>&1
    )"
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        log "ERROR: Redis bootstrap command failed: $* exit=${rc} out=${out}"
        return "${rc}"
    fi
    if lumen_redis_is_error_reply "${out}"; then
        log "ERROR: Redis bootstrap protocol error for $*: ${out}"
        return 1
    fi
    printf '%s' "${out}"
}

redis_rebuild_aof_from_rdb() {
    local image="" container_user="" ping="" db_size="" response=""
    local info="" enabled="" in_progress="" rewrite_status="" running=""
    local attempt=0
    local -a run_args=()

    image="$(
        docker inspect -f '{{.Config.Image}}' "${REDIS_CONTAINER}" 2>/dev/null
    )" || return 1
    container_user="$(
        docker inspect -f '{{.Config.User}}' "${REDIS_CONTAINER}" 2>/dev/null
    )" || return 1
    [ -n "${image}" ] || {
        log "ERROR: cannot determine Redis container image for AOF rebuild"
        return 1
    }

    REDIS_BOOTSTRAP_CONTAINER="lumen-redis-restore-bootstrap-$$"
    run_args=(
        run -d
        --name "${REDIS_BOOTSTRAP_CONTAINER}"
        --label "com.lumen.restore.redis-bootstrap=1"
        --network none
        --volumes-from "${REDIS_CONTAINER}"
        --entrypoint redis-server
    )
    if [ -n "${container_user}" ]; then
        run_args+=(--user "${container_user}")
    fi
    run_args+=(
        "${image}"
        --port 0
        --unixsocket /tmp/lumen-restore-bootstrap.sock
        --unixsocketperm 700
        --dir /data
        --dbfilename dump.rdb
        --appendonly no
        --appenddirname appendonlydir
        --save ""
        --daemonize no
    )
    if ! docker "${run_args[@]}" >/dev/null; then
        log "ERROR: failed to start isolated Redis RDB bootstrap container"
        REDIS_BOOTSTRAP_CONTAINER=""
        return 1
    fi

    for ((attempt = 1; attempt <= 30; attempt++)); do
        if ping="$(redis_bootstrap_cli PING 2>/dev/null)" \
                && [ "${ping}" = "PONG" ]; then
            break
        fi
        sleep 1
    done
    if [ "${ping}" != "PONG" ]; then
        log "ERROR: isolated Redis bootstrap did not load dump.rdb"
        return 1
    fi
    db_size="$(redis_bootstrap_cli DBSIZE)" || return 1
    db_size="${db_size//$'\r'/}"
    db_size="${db_size//$'\n'/}"
    case "${db_size}" in
        ''|*[!0-9]*)
            log "ERROR: Redis bootstrap returned invalid DBSIZE=${db_size:-<empty>}"
            return 1
            ;;
    esac

    response="$(redis_bootstrap_cli CONFIG SET appendonly yes)" || return 1
    [ "${response}" = "OK" ] || {
        log "ERROR: Redis bootstrap could not enable appendonly mode"
        return 1
    }
    for ((attempt = 1; attempt <= 120; attempt++)); do
        info="$(redis_bootstrap_cli INFO persistence)" || return 1
        enabled="$(
            printf '%s\n' "${info}" | tr -d '\r' \
                | sed -n 's/^aof_enabled://p'
        )"
        in_progress="$(
            printf '%s\n' "${info}" | tr -d '\r' \
                | sed -n 's/^aof_rewrite_in_progress://p'
        )"
        rewrite_status="$(
            printf '%s\n' "${info}" | tr -d '\r' \
                | sed -n 's/^aof_last_bgrewrite_status://p'
        )"
        if [ "${enabled}" = "1" ] \
                && [ "${in_progress}" = "0" ] \
                && [ "${rewrite_status}" = "ok" ]; then
            break
        fi
        sleep 1
    done
    if [ "${enabled}" != "1" ] \
            || [ "${in_progress}" != "0" ] \
            || [ "${rewrite_status}" != "ok" ]; then
        log "ERROR: Redis AOF rewrite did not complete successfully"
        return 1
    fi

    if ! docker exec "${REDIS_BOOTSTRAP_CONTAINER}" sh -eu -c '
        manifest=/data/appendonlydir/appendonly.aof.manifest
        [ -f "$manifest" ] && [ ! -L "$manifest" ]
        count=0
        while IFS=" " read -r marker file rest; do
            [ "$marker" = file ] || continue
            case "$file" in
                ""|*/*|..*) exit 1 ;;
            esac
            [ -f "/data/appendonlydir/$file" ]
            [ ! -L "/data/appendonlydir/$file" ]
            count=$((count + 1))
        done < "$manifest"
        [ "$count" -gt 0 ]
        redis-check-aof "$manifest" >/dev/null
    '; then
        log "ERROR: generated Redis AOF manifest or segment validation failed"
        return 1
    fi

    redis_bootstrap_cli SHUTDOWN NOSAVE >/dev/null 2>&1 || true
    for ((attempt = 1; attempt <= 30; attempt++)); do
        running="$(
            docker inspect -f '{{.State.Running}}' \
                "${REDIS_BOOTSTRAP_CONTAINER}" 2>/dev/null || true
        )"
        [ "${running}" = "false" ] && break
        sleep 1
    done
    if [ "${running}" != "false" ]; then
        log "ERROR: isolated Redis bootstrap did not stop cleanly"
        return 1
    fi
    if ! redis_remove_bootstrap_container; then
        return 1
    fi
    REDIS_RESTORED_DB_SIZE="${db_size}"
    log "Redis RDB loaded and converted to validated multipart AOF (dbsize=${db_size})"
}

ensure_redis_started() {
    local running=""
    running="$(
        docker inspect -f '{{.State.Running}}' "$REDIS_CONTAINER" 2>/dev/null \
            || true
    )"
    if [ "$running" != "true" ]; then
        docker start "$REDIS_CONTAINER" >/dev/null || return 1
    fi
    for _ in $(seq 1 30); do
        if redis_ping_quiet; then
            break
        fi
        sleep 1
    done
    local ping_out=""
    if ! ping_out="$(redis_cli PING)" || [ "$ping_out" != "PONG" ]; then
        return 1
    fi
}

verify_restored_redis_dataset() {
    local actual=""
    [ -n "${REDIS_RESTORED_DB_SIZE:-}" ] || return 0
    actual="$(redis_cli DBSIZE)" || return 1
    actual="${actual//$'\r'/}"
    actual="${actual//$'\n'/}"
    if [ "${actual}" != "${REDIS_RESTORED_DB_SIZE}" ]; then
        log "ERROR: restored Redis DBSIZE mismatch: expected=${REDIS_RESTORED_DB_SIZE} actual=${actual:-<empty>}"
        return 1
    fi
}

redis_host_dir() {
    # 限定 destination=/data，避免容器同时挂了别的 volume 时 docker inspect
    # 输出多行 / 顺序不稳，被 validate_redis_host_dir 当成单值消费——拿错路径
    # 后 find -exec rm -rf 会删错目录。这里显式拒绝多行结果。
    local out
    out="$(docker inspect "$REDIS_CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{println}}{{end}}{{end}}')" || return $?
    # 去尾部空行后还有多行就报错退出
    out="${out%$'\n'}"
    case "$out" in
        *$'\n'*)
            log "ERROR: redis container has multiple /data mounts; refusing to guess: ${out}"
            return 1
            ;;
    esac
    if [ -z "$out" ]; then
        log "ERROR: redis container has no /data mount"
        return 1
    fi
    printf '%s\n' "$out"
}

validate_redis_host_dir() {
    local dir="$1"
    local resolved
    if [ -z "$dir" ] || [ "$dir" = "/" ] || [[ "$dir" == *$'\n'* ]] || [[ "$dir" == *$'\r'* ]]; then
        log "ERROR: unsafe redis volume mountpoint: ${dir:-<empty>}"
        return 1
    fi
    if [ ! -d "$dir" ]; then
        log "ERROR: redis volume mountpoint is not a directory: $dir"
        return 1
    fi
    if ! resolved="$(cd -- "$dir" && pwd -P)"; then
        log "ERROR: cannot resolve redis volume mountpoint: $dir"
        return 1
    fi
    case "$resolved" in
        "/"|"/bin"|"/sbin"|"/usr"|"/usr/local"|"/var"|"/var/lib"|"/var/lib/docker"|"/opt"|"/opt/lumendata"|"/tmp"|"/private"|"/Users")
            log "ERROR: refusing to restore redis into broad system directory: $resolved"
            return 1
            ;;
    esac
    printf '%s\n' "$resolved"
}

prune_redis_restore_backups() {
    # $1 = 已过 validate_redis_host_dir 的 redis 数据目录
    # $2 = 保留份数（含即将创建的那一份），所以这里最多留 $2-1 份历史
    local dir="$1"
    local keep="${2:-2}"
    local keep_existing entry pruned=0
    [ -d "$dir" ] || return 0
    keep_existing=$(( keep > 0 ? keep - 1 : 0 ))
    # 目录名带 UTC 时间戳前缀，按名字倒序 == 按时间从新到旧；跳过前
    # keep_existing 个，剩下的都是过期的。
    while IFS= read -r entry; do
        if [ -z "$entry" ]; then
            continue
        fi
        # 只删自己按前缀造出来的目录，且必须落在目标目录下
        case "$entry" in
            "$dir/$REDIS_BACKUP_PREFIX"*) ;;
            *) continue ;;
        esac
        if rm -rf -- "$entry"; then
            pruned=$(( pruned + 1 ))
        else
            log "WARN 清理过期 redis 备份失败，请人工删除: $entry"
        fi
    done < <(
        find "$dir" -mindepth 1 -maxdepth 1 -type d \
            -name "${REDIS_BACKUP_PREFIX}*" 2>/dev/null \
            | LC_ALL=C sort -r \
            | tail -n "+$(( keep_existing + 1 ))"
    )
    if [ "$pruned" -gt 0 ]; then
        log "已清理 $pruned 份过期 redis 旧数据备份（保留最近 $keep_existing 份 + 本次）"
    fi
    return 0
}

redis_rollback_from_backup() {
    # 把 $REDIS_BACKUP_DIR 里的旧数据搬回 $REDIS_HOST_DIR。
    # 调用方负责保证此刻 redis 容器是停的。全部搬回返回 0，否则返回 1。
    local rc=0 _f original_existed backup_exists target_exists
    if [ -z "${REDIS_BACKUP_DIR:-}" ] || [ ! -d "$REDIS_BACKUP_DIR" ] \
            || [ -z "${REDIS_HOST_DIR:-}" ]; then
        return 1
    fi
    REDIS_ORIGINAL_MANIFEST="${REDIS_ORIGINAL_MANIFEST:-$REDIS_BACKUP_DIR/.original-items}"
    if [ ! -f "$REDIS_ORIGINAL_MANIFEST" ] || [ -L "$REDIS_ORIGINAL_MANIFEST" ]; then
        log "ERROR redis rollback manifest missing or unsafe: $REDIS_ORIGINAL_MANIFEST"
        return 1
    fi
    for _f in dump.rdb appendonly.aof appendonlydir; do
        original_existed=0
        backup_exists=0
        target_exists=0
        grep -Fqx -- "$_f" "$REDIS_ORIGINAL_MANIFEST" && original_existed=1
        { [ -e "$REDIS_BACKUP_DIR/$_f" ] || [ -L "$REDIS_BACKUP_DIR/$_f" ]; } \
            && backup_exists=1
        { [ -e "$REDIS_HOST_DIR/$_f" ] || [ -L "$REDIS_HOST_DIR/$_f" ]; } \
            && target_exists=1

        if [ "$original_existed" -eq 0 ]; then
            if [ "$backup_exists" -eq 1 ]; then
                log "WARN unexpected redis backup item not present in manifest: $_f"
                rc=1
                continue
            fi
            rm -rf -- "${REDIS_HOST_DIR:?}/$_f" || rc=1
            continue
        fi

        if [ "$backup_exists" -eq 1 ]; then
            if ! rm -rf -- "${REDIS_HOST_DIR:?}/$_f"; then
                log "WARN cannot clear restored redis/$_f before rollback"
                rc=1
                continue
            fi
            if ! mv "$REDIS_BACKUP_DIR/$_f" "$REDIS_HOST_DIR/$_f"; then
                log "WARN 回滚 redis/$_f 失败，请人工检查 $REDIS_BACKUP_DIR"
                rc=1
            fi
        elif { [ "${REDIS_RESTORE_STATE:-}" = "stashing" ] \
                || [ "${REDIS_RESTORE_STATE:-}" = "rolling_back" ]; } \
                && [ "$target_exists" -eq 1 ]; then
            # 信号落在逐项 mv 之间：该项尚未移走，host 上仍是原文件。
            :
        else
            log "WARN redis/$_f was originally present but no rollback copy can be proven"
            rc=1
        fi
    done
    if [ "$rc" -eq 0 ]; then
        REDIS_RESTORE_STATE="rolled_back"
        if [ "${RESTORE_JOURNAL_ACTIVE:-0}" -eq 1 ] \
                && ! lumen_restore_journal_write "redis_rolled_back"; then
            return 1
        fi
        rm -f -- "$REDIS_ORIGINAL_MANIFEST"
        rmdir "$REDIS_BACKUP_DIR" 2>/dev/null || true
    fi
    return "$rc"
}

redis_discard_rollback_after_success() {
    local backup_dir="${REDIS_BACKUP_DIR:-}"
    local backup_parent backup_name
    if [ -z "$backup_dir" ]; then
        return 0
    fi
    if [ -z "${REDIS_HOST_DIR:-}" ]; then
        log "ERROR: cannot clean Redis rollback without a validated host directory"
        return 1
    fi
    backup_parent="$(dirname -- "$backup_dir")"
    backup_name="$(basename -- "$backup_dir")"
    if [ "$backup_parent" != "$REDIS_HOST_DIR" ]; then
        log "ERROR: refusing to clean Redis rollback outside its validated volume: $backup_dir"
        return 1
    fi
    case "$backup_name" in
        "${REDIS_BACKUP_PREFIX}"*) ;;
        *)
            log "ERROR: refusing to clean Redis rollback with an invalid name: $backup_dir"
            return 1
            ;;
    esac
    if [ -e "$backup_dir" ] || [ -L "$backup_dir" ]; then
        log "removing Redis rollback after readiness commit: $backup_dir"
        if ! rm -rf -- "$backup_dir"; then
            log "WARN: failed to remove Redis rollback directory $backup_dir"
            return 1
        fi
    fi
    REDIS_BACKUP_DIR=""
    REDIS_ORIGINAL_MANIFEST=""
    log "Redis rollback cleanup completed"
}

redis_rollback_after_pg_failure() {
    # J-6：恢复顺序是 redis 先切、PG 后切。PG 这一步失败时 PG 还停在原库，
    # 而 redis 已经是备份时点的数据 —— 两边错配。错配远比"都旧"危险：redis 里
    # 是备份时点的队列/流/tracker，PG 里却是当前状态，服务一起来就会把早已完成
    # 并且已经扣过费的任务当成待处理再跑一遍（重复调用上游 = 重复扣费）。
    # 所以 PG 没换成时，把 redis 也退回原状态，让两边一起收敛回"全旧"。
    if [ ! -d "${REDIS_BACKUP_DIR:-}" ]; then
        log "ERROR redis 已是备份时点数据但 PG 仍是原库，且没有可回滚的原数据。"
        log "      两边错配，启服务前请人工确认 redis / PG 的时点是否一致。"
        return 1
    fi
    log "PG 未切换成功；回滚 redis 到 restore 前状态，避免 redis 与 PG 错配"
    REDIS_RESTORE_STATE="rolling_back"
    if [ "${RESTORE_JOURNAL_ACTIVE:-0}" -eq 1 ] \
            && ! lumen_restore_journal_write "redis_rolling_back"; then
        return 1
    fi
    REDIS_NEEDS_START=1
    if ! docker stop "$REDIS_CONTAINER" >/dev/null 2>&1; then
        REDIS_RESTORE_STATE="rollback_failed"
        log "ERROR redis 容器无法停止；拒绝修改仍可能被写入的数据目录"
        return 1
    fi
    if redis_rollback_from_backup; then
        REDIS_RESTORE_STATE="untouched"
        log "redis 已回滚到 restore 前状态，与 PG 重新一致（两边均为原数据）"
    else
        REDIS_RESTORE_STATE="rollback_failed"
        log "ERROR redis 回滚不完整，启服务前请人工检查 $REDIS_BACKUP_DIR"
        return 1
    fi
    if docker start "$REDIS_CONTAINER" >/dev/null 2>&1; then
        REDIS_NEEDS_START=0
    else
        log "ERROR redis 已回滚但容器启动失败"
        return 1
    fi
    return 0
}

trap cleanup EXIT
trap '' INT TERM HUP

# 维护锁：与 install/update/uninstall/backup 互斥；restore 是高风险操作，
# 被占用时立即失败（不要等定时 backup 完成）。
if command -v lumen_acquire_lock >/dev/null 2>&1; then
    lumen_acquire_lock "${LUMEN_DEPLOY_ROOT}" "restore.sh"
fi

# lumen_acquire_lock 会安装自己的 EXIT trap。信号在双锁状态尚未完整记录前保持
# ignored；先恢复统一 cleanup，再获取 backup/restore 锁，避免 mkdir 锁泄漏。
trap cleanup EXIT
acquire_lock
trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP

if ! lumen_restore_recover_interrupted; then
    RESTORE_RECOVERY_FAILED=1
    log "ERROR: interrupted restore could not be recovered automatically"
    exit 70
fi
if [ "$RESTORE_RECOVERY_ONLY" -eq 1 ]; then
    log "restore recovery consumer completed"
    exit 0
fi

if ! lumen_restore_prepare_backup_pair; then
    log "ERROR: restore source is not a committed, verifiable backup pair"
    exit 3
fi
if [ ! -f "$PG_FILE" ] || [ ! -f "$REDIS_FILE" ]; then
    echo "missing backup files for $TS" >&2
    echo "  $PG_FILE" >&2
    echo "  $REDIS_FILE" >&2
    exit 2
fi

# 验证文件完整性再停服，避免坏备份导致恢复空档
gzip -t "$PG_FILE" || { log "ERROR pg file corrupt"; exit 3; }
tar -tzf "$REDIS_FILE" >/dev/null || { log "ERROR redis file corrupt"; exit 3; }
TMP_DIR="$(make_tmp_dir)"
if ! python3 "${SCRIPT_DIR}/redis_backup_archive.py" \
        "$REDIS_FILE" "$TMP_DIR"; then
    log "ERROR redis archive failed path/type/content validation"
    exit 3
fi
if ! lumen_validate_redis_rdb_file \
        "$REDIS_CONTAINER" "$TMP_DIR/dump.rdb"; then
    log "ERROR redis-check-rdb rejected archived dump.rdb"
    exit 3
fi
if ! pg_validate_archive_list; then
    log "ERROR postgres archive catalog invalid; aborting before services are stopped"
    exit 3
fi
if ! pg_prepare_staged_restore; then
    log "ERROR postgres staged restore failed; aborting before services are stopped"
    exit 7
fi
if ! lumen_restore_verify_bound_backup_pair; then
    log "ERROR: backup pair changed while restore archives were being validated"
    exit 3
fi

if ! writer_snapshot="$(lumen_running_writer_services)"; then
    log "ERROR: failed to capture the pre-restore writer state"
    exit 70
fi
while IFS= read -r service; do
    [ -n "$service" ] && ACTIVE_WRITER_SERVICES+=("$service")
done <<< "$writer_snapshot"
if ! site_snapshot="$(lumen_running_site_services)"; then
    log "ERROR: failed to capture the pre-restore site state"
    exit 70
fi
while IFS= read -r service; do
    [ -n "$service" ] && ACTIVE_SITE_SERVICES+=("$service")
done <<< "$site_snapshot"
log "stopping active writers: ${ACTIVE_WRITER_SERVICES[*]:-<none>}; recorded site=${ACTIVE_SITE_SERVICES[*]:-<none>}"
SERVICES_STOPPED=1
if ! lumen_restore_journal_write "writers_stopping"; then
    exit 70
fi
if ! _restore_compose_stop_services; then
    log "ERROR: failed to quiesce and verify every writer before restore"
    exit 70
fi

# ---- Redis ----
log "restoring redis from $REDIS_FILE"

REDIS_NEEDS_START=1
if ! lumen_restore_journal_write "redis_stopping"; then
    exit 70
fi
docker stop "$REDIS_CONTAINER" >/dev/null
# 找到 volume mount 的 host 路径
if ! REDIS_HOST_DIR="$(redis_host_dir 2>/dev/null)"; then
    log "ERROR: cannot inspect redis container mount"
    exit 4
fi
if ! REDIS_HOST_DIR="$(validate_redis_host_dir "$REDIS_HOST_DIR")"; then
    exit 4
fi
# 不能直接 rm 旧数据再 cp：cp 失败（磁盘满 / cifs 抽风）会留下"清空但没装回"
# 的损毁状态，restore 后丢全部 redis 数据。改 mv 旧数据到备份目录 → cp 新数据
# → 成功才删 backup；任何 cp 失败都把 backup 里的旧数据 mv 回原位。
#
# J-5：备份目录必须轮转。旧实现用 pid 命名且从不删，每恢复一次就在 redis 数据
# 盘上永久多留一整份数据集，最终把盘撑爆 —— 而 redis 盘满 = 全站写不进去。
# 名字改成"时间戳 + pid"以便按时间排序；轮转放在创建之前，这样上一轮中途失败
# 退出留下的目录也能在这里被回收。
REDIS_BACKUP_DIR="$REDIS_HOST_DIR/${REDIS_BACKUP_PREFIX}$(date -u +%Y%m%dT%H%M%SZ).$$"
prune_redis_restore_backups "$REDIS_HOST_DIR" "$REDIS_BACKUP_KEEP"
mkdir -p "$REDIS_BACKUP_DIR" || { log "ERROR cannot mkdir $REDIS_BACKUP_DIR"; exit 4; }
REDIS_ORIGINAL_MANIFEST="$REDIS_BACKUP_DIR/.original-items"
_redis_manifest_tmp="$REDIS_BACKUP_DIR/.original-items.tmp.$$"
: > "$_redis_manifest_tmp" || { log "ERROR cannot create redis rollback manifest"; exit 4; }
for _f in dump.rdb appendonly.aof appendonlydir; do
    if [ -e "$REDIS_HOST_DIR/$_f" ] || [ -L "$REDIS_HOST_DIR/$_f" ]; then
        printf '%s\n' "$_f" >> "$_redis_manifest_tmp" \
            || { log "ERROR cannot write redis rollback manifest"; exit 4; }
    fi
done
chmod 0600 "$_redis_manifest_tmp" \
    || { log "ERROR cannot protect redis rollback manifest"; exit 4; }
mv "$_redis_manifest_tmp" "$REDIS_ORIGINAL_MANIFEST" \
    || { log "ERROR cannot commit redis rollback manifest"; exit 4; }
REDIS_RESTORE_STATE="stashing"
if ! lumen_restore_journal_write "redis_stashing"; then
    exit 70
fi
for _f in dump.rdb appendonly.aof appendonlydir; do
    if [ -e "$REDIS_HOST_DIR/$_f" ] || [ -L "$REDIS_HOST_DIR/$_f" ]; then
        mv "$REDIS_HOST_DIR/$_f" "$REDIS_BACKUP_DIR/$_f" \
            || { log "ERROR cannot stash existing redis/$_f"; exit 4; }
    fi
done
REDIS_RESTORE_STATE="stashed"
if ! lumen_restore_journal_write "redis_stashed"; then
    exit 70
fi

# 旧合法归档可能仍含 appendonly.aof/appendonlydir，但恢复只部署已验证的
# dump.rdb。目标卷原有 AOF 已全部移入 rollback 目录，不能覆盖恢复后的 RDB。
REDIS_RESTORE_STATE="applying"
if ! lumen_restore_journal_write "redis_applying"; then
    exit 70
fi
_redis_cp_ok=1
if [ -f "$TMP_DIR/dump.rdb" ]; then
    cp "$TMP_DIR/dump.rdb" "$REDIS_HOST_DIR/dump.rdb" || _redis_cp_ok=0
fi

if [ "$_redis_cp_ok" = "0" ]; then
    log "ERROR redis 数据拷贝失败，回滚到原状态"
    if redis_rollback_from_backup; then
        REDIS_RESTORE_STATE="untouched"
    else
        REDIS_RESTORE_STATE="rollback_failed"
    fi
    log "建议：检查磁盘空间 (df -h) / 文件系统挂载状态 / 重跑 restore"
    exit 5
fi
if ! redis_rebuild_aof_from_rdb; then
    log "ERROR Redis RDB could not be converted to a complete validated AOF"
    if ! redis_remove_bootstrap_container; then
        REDIS_RESTORE_STATE="rollback_failed"
    elif redis_rollback_from_backup; then
        REDIS_RESTORE_STATE="untouched"
    else
        REDIS_RESTORE_STATE="rollback_failed"
    fi
    exit 5
fi
REDIS_RESTORE_STATE="applied"
if ! lumen_restore_journal_write "redis_applied"; then
    exit 70
fi

# readiness 尚未通过前，这一份是 Redis 原地回滚的唯一依据。
log "redis 数据已恢复，原数据 rollback 暂存于 $REDIS_BACKUP_DIR，等待 readiness commit"

if ! ensure_redis_started; then
    log "ERROR: redis did not come back up (check container status & REDIS_URL/REDIS_PASSWORD vs requirepass)"
    exit 5
fi
if ! verify_restored_redis_dataset; then
    log "ERROR: Redis started but did not load the rebuilt AOF dataset"
    exit 5
fi
REDIS_NEEDS_START=0
if ! lumen_restore_journal_write "redis_started"; then
    exit 70
fi
log "redis restored"

# ---- Postgres ----
log "promoting staged postgres restore from $PG_TEMP_DB"
if ! pg_promote_staged_restore; then
    log "ERROR: postgres staged restore promotion failed"
    exit 7
fi

# Commit the restored data pair before any writer can observe it. Once this
# journal write succeeds, readiness failures must keep the restored pair and
# stop services; rolling back would erase writes or external side effects made
# during the readiness window.
log "durably committing restored PG/Redis pair before reopening writers"
restore_failpoint before_storage_commit
REDIS_RESTORE_STATE="committed"
trap '' INT TERM HUP
if ! lumen_restore_journal_write "committed"; then
    trap 'on_signal INT' INT
    trap 'on_signal TERM' TERM
    trap 'on_signal HUP' HUP
    log "ERROR: readiness passed but restore commit journal could not be persisted"
    exit 70
fi
RESTORE_COMMITTED=1
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP
restore_failpoint after_storage_commit

log "starting committed restored services and requiring API/Worker readiness"
if ! _restore_compose_start_services; then
    log "ERROR: committed restored data failed API/Worker readiness; keeping writers stopped"
    _restore_compose_stop_services >/dev/null 2>&1 || true
    SERVICES_STOPPED=1
    RESTORE_RECOVERY_FAILED=1
    exit 70
fi
SERVICES_STOPPED=0
restore_failpoint after_readiness_commit

if ! pg_discard_rollback_after_success; then
    log "ERROR: restore committed, but rollback cleanup is incomplete"
    exit 70
fi
if ! lumen_restore_journal_write "committed"; then
    log "ERROR: restore committed, but cleaned rollback state could not be persisted"
    exit 70
fi
log "postgres restored"

log "restore $TS done"
