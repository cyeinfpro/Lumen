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
if ! REDIS_HOST_DIR="$(validate_redis_host_dir "$REDIS_HOST_DIR")"; then
    exit 4
fi
# 清空旧数据
find "$REDIS_HOST_DIR" -mindepth 1 -maxdepth 1 \
    \( -name dump.rdb -o -name appendonly.aof -o -name appendonlydir \) \
    -exec rm -rf -- {} +
# 拷回新数据
[ -f "$TMP_DIR/dump.rdb" ] && cp "$TMP_DIR/dump.rdb" "$REDIS_HOST_DIR/dump.rdb"
[ -d "$TMP_DIR/appendonlydir" ] && cp -r "$TMP_DIR/appendonlydir" "$REDIS_HOST_DIR/appendonlydir"
[ -f "$TMP_DIR/appendonly.aof" ] && cp "$TMP_DIR/appendonly.aof" "$REDIS_HOST_DIR/appendonly.aof"

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
