#!/usr/bin/env bash
# Lumen 恢复：成对恢复指定 timestamp 的 PG + Redis 备份。
# 用法：restore.sh <timestamp>  （timestamp 形如 20260424-123000）
#
# 执行顺序：
#   1. 停 lumen-api、lumen-worker（避免恢复期间写入）
#   2. 恢复 Redis（需要重启 container）
#   3. 恢复 Postgres（drop+restore 到同名 db）
#   4. 启 lumen-api、lumen-worker
#
# 失败时：API/Worker 仍会被重启起来（避免服务长时间卡停），但会 exit 非零。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"

# shellcheck source=lib.sh
if [ -f "${SCRIPT_DIR}/lib.sh" ]; then
    . "${SCRIPT_DIR}/lib.sh"
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
REDIS_PASSWORD="${REDIS_PASSWORD:-}"
PG_USER="${DB_USER:-lumen}"
PG_DB="${DB_NAME:-lumen}"
LOCKFILE="${LUMEN_BACKUP_RESTORE_LOCKFILE:-${TMPDIR:-/tmp}/lumen-backup-restore.lock}"
LOCKDIR="$LOCKFILE.d"
LOCK_KIND=""
TMP_DIR=""
SERVICES_STOPPED=0
REDIS_NEEDS_START=0

log() { printf '[restore %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

release_lock() {
    if [ "$LOCK_KIND" = "flock" ]; then
        flock -u 9 2>/dev/null || true
    elif [ "$LOCK_KIND" = "mkdir" ]; then
        rm -rf "$LOCKDIR" 2>/dev/null || true
    fi
}

cleanup() {
    local rc=$?
    if [ "$REDIS_NEEDS_START" -eq 1 ]; then
        log "starting redis container"
        docker start "$REDIS_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR" 2>/dev/null || true
    fi
    if [ "$SERVICES_STOPPED" -eq 1 ]; then
        log "starting lumen-api + lumen-worker"
        systemctl start lumen-api lumen-worker || true
    fi
    release_lock
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
        if ! { exec 9>"$LOCKFILE"; } 2>/dev/null; then
            log "ERROR: cannot open lock file: $LOCKFILE"
            exit 10
        fi
        if ! flock -n 9; then
            log "ERROR: another backup/restore is already running (lock: $LOCKFILE)"
            exit 10
        fi
        LOCK_KIND="flock"
        return 0
    fi

    if mkdir "$LOCKDIR" 2>/dev/null; then
        printf '%s\n' "$$" > "$LOCKDIR/pid" 2>/dev/null || true
        LOCK_KIND="mkdir"
        return 0
    fi

    log "ERROR: another backup/restore is already running (lock: $LOCKDIR)"
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

redis_cli() {
    if [ -n "$REDIS_PASSWORD" ]; then
        REDISCLI_AUTH="$REDIS_PASSWORD" docker exec -e REDISCLI_AUTH "$REDIS_CONTAINER" redis-cli --no-auth-warning "$@"
    else
        docker exec "$REDIS_CONTAINER" redis-cli "$@"
    fi
}

redis_host_dir() {
    docker inspect "$REDIS_CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}'
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

if [ ! -f "$PG_FILE" ] || [ ! -f "$REDIS_FILE" ]; then
    echo "missing backup files for $TS" >&2
    echo "  $PG_FILE" >&2
    echo "  $REDIS_FILE" >&2
    exit 2
fi

# 验证文件完整性再停服，避免坏备份导致恢复空档
gzip -t "$PG_FILE" || { log "ERROR pg file corrupt"; exit 3; }
tar -tzf "$REDIS_FILE" >/dev/null || { log "ERROR redis file corrupt"; exit 3; }

log "stopping lumen-api + lumen-worker"
SERVICES_STOPPED=1
systemctl stop lumen-api lumen-worker

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
if [ -z "$REDIS_HOST_DIR" ] || [ ! -d "$REDIS_HOST_DIR" ]; then
    log "ERROR: cannot locate redis volume mountpoint"
    exit 4
fi
# 清空旧数据
rm -rf "$REDIS_HOST_DIR"/dump.rdb "$REDIS_HOST_DIR"/appendonly.aof "$REDIS_HOST_DIR"/appendonlydir
# 拷回新数据
[ -f "$TMP_DIR/dump.rdb" ] && cp "$TMP_DIR/dump.rdb" "$REDIS_HOST_DIR/dump.rdb"
[ -d "$TMP_DIR/appendonlydir" ] && cp -r "$TMP_DIR/appendonlydir" "$REDIS_HOST_DIR/appendonlydir"
[ -f "$TMP_DIR/appendonly.aof" ] && cp "$TMP_DIR/appendonly.aof" "$REDIS_HOST_DIR/appendonly.aof"

docker start "$REDIS_CONTAINER" >/dev/null
REDIS_NEEDS_START=0
# 等 redis 起来
for _ in $(seq 1 30); do
    if redis_cli ping 2>/dev/null | grep -q PONG; then
        break
    fi
    sleep 1
done
if ! redis_cli ping 2>/dev/null | grep -q PONG; then
    log "ERROR: redis did not come back up"
    exit 5
fi
log "redis restored"

# ---- Postgres ----
log "restoring postgres from $PG_FILE"
# drop + recreate: 避免残留数据；--clean --if-exists 对复杂外键有时有坑，所以走 drop/create
PG_DB_IDENT="$(pg_quote_ident "$PG_DB")"
PG_USER_IDENT="$(pg_quote_ident "$PG_USER")"
PG_DB_LITERAL="$(pg_quote_literal "$PG_DB")"
if ! docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $PG_DB_LITERAL AND pid <> pg_backend_pid();" >/dev/null; then
    log "ERROR: pg terminate connections failed"
    exit 6
fi
if ! docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS $PG_DB_IDENT;" >/dev/null; then
    log "ERROR: pg drop database failed"
    exit 6
fi
if ! docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres -c "CREATE DATABASE $PG_DB_IDENT OWNER $PG_USER_IDENT;" >/dev/null; then
    log "ERROR: pg drop/create failed"
    exit 6
fi

set +e
gunzip -c "$PG_FILE" | docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$PG_DB" --no-owner --no-acl
gunzip_rc=${PIPESTATUS[0]}
pg_restore_rc=${PIPESTATUS[1]}
set -e
if [ "$pg_restore_rc" -ge 2 ]; then
    log "ERROR: pg_restore failed with exit $pg_restore_rc"
    exit 7
fi
if [ "$gunzip_rc" -ne 0 ]; then
    log "ERROR: failed to read pg dump (gunzip exit $gunzip_rc)"
    exit 7
fi
if [ "$pg_restore_rc" -eq 1 ]; then
    log "WARN: pg_restore returned non-zero (common with FKs); continuing and letting app validate"
fi
log "postgres restored"

log "restore $TS done"
