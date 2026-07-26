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
# 失败时：API/Worker 仍会被重启起来（避免服务长时间卡停），但会 exit 非零。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"

if [ ! -f "${SCRIPT_DIR}/lib.sh" ]; then
    echo "[restore] ERROR: ${SCRIPT_DIR}/lib.sh missing" >&2
    exit 1
fi
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

# 自动从 shared/.env 兜底：lumenctl 调用本脚本时只透传 LUMEN_* 系列 env，
# 不会传 REDIS_URL / REDIS_PASSWORD / DB_*。无 .env 兜底则 redis_cli 拿不到密码。
ENV_FILE="$(lumen_find_shared_env "${SCRIPT_ROOT}" 2>/dev/null || true)"
if [ -n "${ENV_FILE}" ]; then
    export LUMEN_ENV_FILE="${ENV_FILE}"
    for key in DB_USER DB_NAME DB_PASSWORD REDIS_URL REDIS_PASSWORD BACKUP_ROOT PG_CONTAINER REDIS_CONTAINER; do
        lumen_dotenv_export_if_unset "${key}" "${ENV_FILE}"
    done
fi

TS="${1:-}"
if [ -z "$TS" ]; then
    echo "usage: $0 <timestamp>" >&2
    exit 1
fi
if [[ ! "$TS" =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
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
REDIS_HOST_DIR=""
REDIS_BACKUP_DIR=""
# redis 旧数据备份目录的前缀；名字里再拼 UTC 时间戳（可排序，用于轮转）+ pid。
REDIS_BACKUP_PREFIX=".lumen-restore-old."
# 保留几份（含本次）。旧数据是拷贝失败后人工 rollback 的唯一退路，所以一份都
# 不留不行；但一份就是一整个 redis 数据集，不轮转就是每恢复一次永久多占一份盘。
REDIS_BACKUP_KEEP="${LUMEN_REDIS_RESTORE_BACKUP_KEEP:-2}"
case "$REDIS_BACKUP_KEEP" in
    ''|*[!0-9]*) REDIS_BACKUP_KEEP=2 ;;
    0) REDIS_BACKUP_KEEP=1 ;;
esac

log() { printf '[restore %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

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
    # 优先 lumen_compose（自动找 ${ROOT}/current 的 compose），fallback 到
    # docker start 容器名。Lumen 全栈已 docker 化，systemd 的 lumen-api.service
    # 在新部署上不一定存在，systemctl start 会直接报错并被吞，导致服务卡停。
    if command -v lumen_compose >/dev/null 2>&1 \
            && lumen_compose start api worker 2>/dev/null; then
        return 0
    fi
    docker start lumen-api lumen-worker >/dev/null 2>&1 || true
}

_restore_compose_stop_services() {
    if command -v lumen_compose >/dev/null 2>&1 \
            && lumen_compose stop api worker 2>/dev/null; then
        return 0
    fi
    docker stop lumen-api lumen-worker >/dev/null 2>&1
}

cleanup() {
    local rc=$?
    if [ "$REDIS_NEEDS_START" -eq 1 ]; then
        log "starting redis container"
        docker start "$REDIS_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [ "${PG_SWAP_IN_PROGRESS:-0}" = "1" ] && [ -n "${PG_ROLLBACK_DB:-}" ]; then
        pg_recover_active_from_rollback || true
    fi
    if [ -n "${PG_TEMP_DB:-}" ]; then
        pg_drop_database_if_exists "$PG_TEMP_DB" >/dev/null 2>&1 || true
    fi
    if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR" 2>/dev/null || true
    fi
    if [ "$SERVICES_STOPPED" -eq 1 ]; then
        log "starting api + worker（compose 优先 / 容器名 fallback）"
        _restore_compose_start_services
    fi
    release_lock
    if command -v lumen_release_lock >/dev/null 2>&1; then
        lumen_release_lock 2>/dev/null || true
    fi
    return "$rc"
}

on_signal() {
    local sig="$1"
    local rc=130
    if [ "$sig" = "TERM" ]; then
        rc=143
    fi
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

pg_recover_active_from_rollback() {
    if [ -z "${PG_ROLLBACK_DB:-}" ]; then
        return 1
    fi
    local active_exists_rc
    set +e
    pg_database_exists "$PG_DB"
    active_exists_rc=$?
    set -e
    if [ "$active_exists_rc" -eq 0 ]; then
        PG_SWAP_IN_PROGRESS=0
        log "postgres active database $PG_DB already exists; rollback swap recovery is not needed"
        return 0
    fi
    if [ "$active_exists_rc" -ne 1 ]; then
        log "ERROR: failed to inspect active postgres database $PG_DB before rollback recovery"
        return 1
    fi
    log "attempting postgres rollback swap: $PG_ROLLBACK_DB -> $PG_DB"
    if pg_rename_database "$PG_ROLLBACK_DB" "$PG_DB"; then
        PG_ROLLBACK_DB=""
        PG_SWAP_IN_PROGRESS=0
        log "postgres active database restored from rollback"
        return 0
    fi
    log "ERROR: postgres rollback database $PG_ROLLBACK_DB could not be renamed back to $PG_DB"
    return 1
}

pg_discard_rollback_after_success() {
    if [ -z "${PG_ROLLBACK_DB:-}" ]; then
        return 0
    fi
    local rollback_db="$PG_ROLLBACK_DB"
    log "dropping postgres rollback database after successful restore: $rollback_db"
    if pg_drop_database_if_exists "$rollback_db"; then
        PG_ROLLBACK_DB=""
        log "postgres rollback database dropped: $rollback_db"
        return 0
    fi
    log "WARN: failed to drop postgres rollback database $rollback_db; manual cleanup may be required"
    return 1
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
        if ! pg_rename_database "$PG_TEMP_DB" "$PG_DB"; then
            log "ERROR: failed to promote postgres temporary restore database $PG_TEMP_DB"
            return 1
        fi
        PG_TEMP_DB=""
        PG_PROMOTED=1
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
    if ! pg_rename_database "$PG_DB" "$PG_ROLLBACK_DB"; then
        PG_SWAP_IN_PROGRESS=0
        PG_ROLLBACK_DB=""
        log "ERROR: failed to move active postgres database $PG_DB to rollback database"
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
    PG_SWAP_IN_PROGRESS=0
    # 库已经是恢复后的数据了。下面的 discard 只是清理垃圾库，失败也不代表
    # 恢复失败 —— 这个标记就是给失败分支用来区分"没换成"和"换成了但没扫干净"。
    PG_PROMOTED=1
    if ! pg_discard_rollback_after_success; then
        return 1
    fi
    log "postgres restored; previous active database discarded"
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
    local rc=0 _f
    if [ -z "${REDIS_BACKUP_DIR:-}" ] || [ ! -d "$REDIS_BACKUP_DIR" ] \
            || [ -z "${REDIS_HOST_DIR:-}" ]; then
        return 1
    fi
    for _f in dump.rdb appendonly.aof appendonlydir; do
        rm -rf "${REDIS_HOST_DIR:?}/$_f" 2>/dev/null || true
        if [ -e "$REDIS_BACKUP_DIR/$_f" ]; then
            if ! mv "$REDIS_BACKUP_DIR/$_f" "$REDIS_HOST_DIR/$_f"; then
                log "WARN 回滚 redis/$_f 失败，请人工检查 $REDIS_BACKUP_DIR"
                rc=1
            fi
        fi
    done
    rmdir "$REDIS_BACKUP_DIR" 2>/dev/null || true
    return "$rc"
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
    REDIS_NEEDS_START=1
    docker stop "$REDIS_CONTAINER" >/dev/null 2>&1 || true
    if redis_rollback_from_backup; then
        log "redis 已回滚到 restore 前状态，与 PG 重新一致（两边均为原数据）"
    else
        log "ERROR redis 回滚不完整，启服务前请人工检查 $REDIS_BACKUP_DIR"
    fi
    if docker start "$REDIS_CONTAINER" >/dev/null 2>&1; then
        REDIS_NEEDS_START=0
    fi
    return 0
}

trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM

# 维护锁：与 install/update/uninstall/backup 互斥；restore 是高风险操作，
# 被占用时立即失败（不要等定时 backup 完成）。
if command -v lumen_acquire_lock >/dev/null 2>&1; then
    LUMEN_MAINT_ROOT="${LUMEN_MAINT_ROOT:-}"
    if [ -z "${LUMEN_MAINT_ROOT}" ]; then
        if [ -d "/opt/lumen" ]; then
            LUMEN_MAINT_ROOT="/opt/lumen"
        else
            LUMEN_MAINT_ROOT="${SCRIPT_ROOT}"
        fi
    fi
    lumen_acquire_lock "${LUMEN_MAINT_ROOT}" "restore.sh"
fi

acquire_lock
# 注意：lumen_acquire_lock 会自己 `trap 'lumen_release_lock' EXIT`，这里再次
# `trap cleanup EXIT` 会覆盖它 —— 但 cleanup() 内显式 fall through 调
# `lumen_release_lock`，维护锁仍会被释放。改 order 前请保留这条不变量。
trap cleanup EXIT

if [ ! -f "$PG_FILE" ] || [ ! -f "$REDIS_FILE" ]; then
    echo "missing backup files for $TS" >&2
    echo "  $PG_FILE" >&2
    echo "  $REDIS_FILE" >&2
    exit 2
fi

# 验证文件完整性再停服，避免坏备份导致恢复空档
gzip -t "$PG_FILE" || { log "ERROR pg file corrupt"; exit 3; }
tar -tzf "$REDIS_FILE" >/dev/null || { log "ERROR redis file corrupt"; exit 3; }
if ! pg_validate_archive_list; then
    log "ERROR postgres archive catalog invalid; aborting before services are stopped"
    exit 3
fi
if ! pg_prepare_staged_restore; then
    log "ERROR postgres staged restore failed; aborting before services are stopped"
    exit 7
fi

log "stopping api + worker（compose 优先 / 容器名 fallback）"
SERVICES_STOPPED=1
_restore_compose_stop_services

# ---- Redis ----
log "restoring redis from $REDIS_FILE"
TMP_DIR="$(make_tmp_dir)"
tar -xzf "$REDIS_FILE" -C "$TMP_DIR"

REDIS_NEEDS_START=1
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
for _f in dump.rdb appendonly.aof appendonlydir; do
    if [ -e "$REDIS_HOST_DIR/$_f" ]; then
        mv "$REDIS_HOST_DIR/$_f" "$REDIS_BACKUP_DIR/$_f" \
            || { log "ERROR cannot stash existing redis/$_f"; exit 4; }
    fi
done

# 拷回新数据；任何一个失败立即回滚
_redis_cp_ok=1
if [ -f "$TMP_DIR/dump.rdb" ]; then
    cp "$TMP_DIR/dump.rdb" "$REDIS_HOST_DIR/dump.rdb" || _redis_cp_ok=0
fi
if [ "$_redis_cp_ok" = "1" ] && [ -d "$TMP_DIR/appendonlydir" ]; then
    cp -r "$TMP_DIR/appendonlydir" "$REDIS_HOST_DIR/appendonlydir" || _redis_cp_ok=0
fi
if [ "$_redis_cp_ok" = "1" ] && [ -f "$TMP_DIR/appendonly.aof" ]; then
    cp "$TMP_DIR/appendonly.aof" "$REDIS_HOST_DIR/appendonly.aof" || _redis_cp_ok=0
fi

if [ "$_redis_cp_ok" = "0" ]; then
    log "ERROR redis 数据拷贝失败，回滚到原状态"
    redis_rollback_from_backup || true
    log "建议：检查磁盘空间 (df -h) / 文件系统挂载状态 / 重跑 restore"
    exit 5
fi

# 这一份留着给人工应急 rollback；由下一次 restore 开头的 prune 按
# LUMEN_REDIS_RESTORE_BACKUP_KEEP（默认 2 份）轮转回收，不在这里删。
log "redis 数据已恢复，原数据备份在 $REDIS_BACKUP_DIR（保留最近 $REDIS_BACKUP_KEEP 份，下次 restore 时自动轮转）"

docker start "$REDIS_CONTAINER" >/dev/null
REDIS_NEEDS_START=0
# 等 redis 起来：循环里用静默探测（启动初期 docker exec 必然报错，不打日志）。
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
for _ in $(seq 1 30); do
    if redis_ping_quiet; then
        break
    fi
    sleep 1
done
# 最终判决用 verbose 版：失败时 log 会留下是 docker exec 错还是协议错（AUTH 等）。
if ! ping_out="$(redis_cli PING)" || [ "$ping_out" != "PONG" ]; then
    log "ERROR: redis did not come back up (check container status & REDIS_URL/REDIS_PASSWORD vs requirepass)"
    exit 5
fi
log "redis restored"

# ---- Postgres ----
log "promoting staged postgres restore from $PG_TEMP_DB"
if ! pg_promote_staged_restore; then
    log "ERROR: postgres staged restore promotion failed"
    # PG_PROMOTED=1 表示库其实已经换成了恢复后的数据（失败出在后续清理垃圾库），
    # 这时 redis 和 PG 是一致的，不能再动 redis —— 回滚反而制造错配。
    if [ "$PG_PROMOTED" -ne 1 ]; then
        redis_rollback_after_pg_failure || true
    else
        log "postgres 数据已切换成功（失败发生在收尾清理），redis 保持恢复后状态"
    fi
    exit 7
fi
log "postgres restored"

log "restore $TS done"
