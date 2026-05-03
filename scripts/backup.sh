#!/usr/bin/env bash
# Lumen 定时备份：pg_dump + Redis dump.rdb → /opt/lumendata/backup
# 每 4 小时触发一次（systemd timer）。保留最近 MAX_KEEP 份。
#
# 文件命名：<timestamp>.pg.dump.gz / <timestamp>.redis.tgz
# 同一 timestamp 两个文件配对，被视为一个"备份点"。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"

# 复用 lib.sh 的 lumen_try_acquire_lock，让 backup 与 install/update/uninstall 互斥。
# 在 backup 自己的 backup-restore 锁之前加一层维护锁；维护锁被占用时跳过本次（exit 0）。
# shellcheck source=lib.sh
if [ -f "${SCRIPT_DIR}/lib.sh" ]; then
    . "${SCRIPT_DIR}/lib.sh"
fi

backup_dotenv_value() {
    local key="$1"
    local file="$2"
    local raw=""
    raw="$(sed -n "s/^${key}=//p" "${file}" 2>/dev/null | head -n1 || true)"
    raw="${raw%$'\r'}"
    if [[ "${raw}" == \'*\' && "${raw}" == *\' ]]; then
        raw="${raw:1:${#raw}-2}"
    elif [[ "${raw}" == \"*\" && "${raw}" == *\" ]]; then
        raw="${raw:1:${#raw}-2}"
    fi
    printf '%s' "${raw}"
}

backup_find_env_file() {
    local candidate
    for candidate in \
        "${LUMEN_ENV_FILE:-}" \
        "${SCRIPT_ROOT}/.env" \
        "${SCRIPT_ROOT}/shared/.env" \
        "/opt/lumen/shared/.env"; do
        [ -n "${candidate}" ] || continue
        if [ -f "${candidate}" ]; then
            printf '%s' "${candidate}"
            return 0
        fi
    done
    return 1
}

backup_export_dotenv_key() {
    local key="$1"
    local file="$2"
    local value=""
    if [ -n "${!key:-}" ]; then
        return 0
    fi
    value="$(backup_dotenv_value "${key}" "${file}")"
    if [ -n "${value}" ]; then
        export "${key}=${value}"
    fi
}

ENV_FILE="$(backup_find_env_file 2>/dev/null || true)"
if [ -n "${ENV_FILE}" ]; then
    export LUMEN_ENV_FILE="${ENV_FILE}"
    for key in DB_USER DB_NAME DB_PASSWORD REDIS_PASSWORD BACKUP_ROOT LUMEN_BACKUP_ROOT PG_CONTAINER REDIS_CONTAINER; do
        backup_export_dotenv_key "${key}" "${ENV_FILE}"
    done
fi

TS="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_ROOT="${BACKUP_ROOT:-${LUMEN_BACKUP_ROOT:-/opt/lumendata/backup}}"
PG_DIR="$BACKUP_ROOT/pg"
REDIS_DIR="$BACKUP_ROOT/redis"
MAX_KEEP="${MAX_KEEP:-40}"

PG_CONTAINER="${PG_CONTAINER:-lumen-pg}"
REDIS_CONTAINER="${REDIS_CONTAINER:-lumen-redis}"
REDIS_PASSWORD="${REDIS_PASSWORD:-}"
PG_USER="${DB_USER:-lumen}"
PG_DB="${DB_NAME:-lumen}"
LOCKFILE="${LUMEN_BACKUP_RESTORE_LOCKFILE:-${TMPDIR:-/tmp}/lumen-backup-restore.lock}"
LOCKDIR="$LOCKFILE.d"
LOCK_KIND=""
TMP_DIR=""

log() { printf '[backup %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

release_lock() {
    if [ "$LOCK_KIND" = "flock" ]; then
        flock -u 9 2>/dev/null || true
    elif [ "$LOCK_KIND" = "mkdir" ]; then
        rm -rf "$LOCKDIR" 2>/dev/null || true
    fi
}

cleanup() {
    local rc=$?
    if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR" 2>/dev/null || true
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

file_size() {
    wc -c < "$1" | tr -d '[:space:]'
}

make_tmp_dir() {
    local base
    local tmp_dir
    for base in "${TMPDIR:-}" /var/tmp /tmp "$BACKUP_ROOT/.tmp"; do
        [ -n "$base" ] || continue
        mkdir -p "$base" 2>/dev/null || true
        if tmp_dir="$(mktemp -d "$base/lumen-backup.XXXXXXXXXX" 2>/dev/null)"; then
            printf '%s\n' "$tmp_dir"
            return 0
        fi
    done
    log "ERROR: failed to create temporary directory"
    exit 5
}

redis_cli() {
    if [ -n "$REDIS_PASSWORD" ]; then
        REDISCLI_AUTH="$REDIS_PASSWORD" docker exec -e REDISCLI_AUTH "$REDIS_CONTAINER" redis-cli --no-auth-warning "$@"
    else
        docker exec "$REDIS_CONTAINER" redis-cli "$@"
    fi
}

docker_cp_redis() {
    local src="$1"
    local dest="$2"
    local label="$3"
    local required="$4"
    local err_file="$TMP_DIR/docker-cp-$label.err"
    local rc
    local err_msg

    if docker cp "$REDIS_CONTAINER:$src" "$dest" 2>"$err_file"; then
        rm -f "$err_file"
        return 0
    fi

    rc=$?
    err_msg="$(sed -n '1p' "$err_file" 2>/dev/null || true)"
    rm -f "$err_file"

    case "$err_msg" in
        *"Could not find"*|*"not found"*|*"No such container:path"*)
            if [ "$required" = "required" ]; then
                log "WARN: redis $label missing: ${err_msg:-docker cp exit $rc}"
            else
                log "redis $label not present; skipping"
            fi
            ;;
        *)
            log "WARN: docker cp failed for redis $label (exit $rc): ${err_msg:-unknown error}"
            ;;
    esac
    return "$rc"
}

trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM

# 维护锁：与 install/update/uninstall 互斥；被占用时跳过本次 backup（exit 0，不让 systemd timer 报警）。
# 受 LUMEN_BACKUP_FORCE=1 控制：强制运行（用于 update.sh 的 backup_preflight）；
# 此时由调用方持有维护锁，本进程跳过 try-acquire。
if command -v lumen_try_acquire_lock >/dev/null 2>&1 && [ "${LUMEN_BACKUP_FORCE:-0}" != "1" ]; then
    LUMEN_MAINT_ROOT="${LUMEN_MAINT_ROOT:-}"
    if [ -z "${LUMEN_MAINT_ROOT}" ]; then
        if [ -d "/opt/lumen" ]; then
            LUMEN_MAINT_ROOT="/opt/lumen"
        else
            LUMEN_MAINT_ROOT="${SCRIPT_ROOT}"
        fi
    fi
    if ! lumen_try_acquire_lock "${LUMEN_MAINT_ROOT}" "backup.sh"; then
        log "skipped: maintenance lock held (install/update/uninstall in progress); next timer cycle will retry"
        exit 0
    fi
fi

acquire_lock
mkdir -p "$PG_DIR" "$REDIS_DIR"

# ---- Postgres ----
PG_OUT="$PG_DIR/$TS.pg.dump.gz"
log "dumping postgres → $PG_OUT"
PG_ERR="$(mktemp "${BACKUP_ROOT}/.pg-dump.XXXXXX.err")" || {
    log "ERROR: failed to create pg_dump error log"
    exit 5
}
set +e
docker exec -i "$PG_CONTAINER" pg_dump -U "$PG_USER" -Fc "$PG_DB" 2>"$PG_ERR" | gzip -c > "$PG_OUT"
PIPE_RC=("${PIPESTATUS[@]}")
PG_RC="${PIPE_RC[0]:-1}"
GZIP_RC="${PIPE_RC[1]:-1}"
set -e
if [ "${PG_RC}" -ne 0 ] || [ "${GZIP_RC}" -ne 0 ]; then
    log "ERROR: pg_dump failed (pg_rc=${PG_RC}, gzip_rc=${GZIP_RC}, container=${PG_CONTAINER}, db=${PG_DB}, user=${PG_USER})"
    docker ps -a --filter "name=^/${PG_CONTAINER}$" --format 'container={{.Names}} status={{.Status}}' 2>/dev/null | while IFS= read -r line; do
        [ -n "$line" ] && log "$line"
    done
    sed -n '1,20p' "$PG_ERR" 2>/dev/null | while IFS= read -r line; do
        [ -n "$line" ] && log "pg_dump stderr: $line"
    done
    rm -f "$PG_ERR" "$PG_OUT"
    exit 2
fi
rm -f "$PG_ERR"
# 基本合理性：gzip 有效 + 非空
if ! gzip -t "$PG_OUT" 2>/dev/null || [ ! -s "$PG_OUT" ]; then
    log "ERROR: pg dump invalid, removing"
    rm -f "$PG_OUT"
    exit 2
fi
PG_SIZE="$(file_size "$PG_OUT")"
log "pg dump ok size=$PG_SIZE"

# ---- Redis ----
REDIS_OUT="$REDIS_DIR/$TS.redis.tgz"
log "triggering redis BGSAVE"
# 记录 lastsave 时间戳，轮询到它变化视为 BGSAVE 完成
LAST_BEFORE="$(redis_cli LASTSAVE | tr -d '\r\n')"
LAST_NOW="$LAST_BEFORE"
redis_cli BGSAVE >/dev/null
for _ in $(seq 1 60); do
    sleep 1
    LAST_NOW="$(redis_cli LASTSAVE | tr -d '\r\n')"
    if [ "$LAST_NOW" != "$LAST_BEFORE" ]; then
        break
    fi
done
if [ "$LAST_NOW" = "$LAST_BEFORE" ]; then
    log "ERROR: redis BGSAVE did not complete in 60s"
    exit 3
fi
log "BGSAVE done, packaging"

# 从 redis 容器里把 dump.rdb 和 appendonly 拷出来打包
TMP_DIR="$(make_tmp_dir)"
if docker_cp_redis "/data/dump.rdb" "$TMP_DIR/dump.rdb" "dump.rdb" "required"; then
    :
fi
# appendonly 在 redis 7 可能是目录 appendonlydir/ 或旧版单文件 appendonly.aof
if docker_cp_redis "/data/appendonlydir" "$TMP_DIR/appendonlydir" "appendonlydir" "optional"; then
    :
elif docker_cp_redis "/data/appendonly.aof" "$TMP_DIR/appendonly.aof" "appendonly.aof" "optional"; then
    :
fi

if [ ! -f "$TMP_DIR/dump.rdb" ] && [ ! -d "$TMP_DIR/appendonlydir" ] && [ ! -f "$TMP_DIR/appendonly.aof" ]; then
    log "ERROR: no redis data files extracted"
    exit 4
fi

tar -czf "$REDIS_OUT" -C "$TMP_DIR" .
REDIS_SIZE="$(file_size "$REDIS_OUT")"
log "redis pack ok size=$REDIS_SIZE"

# ---- Retention ----
# 按文件名字典序（timestamp 格式保证等价于时间序）取最新 MAX_KEEP 份；其余删除。
prune() {
    local dir="$1"
    local pat="$2"
    local keep="$3"
    local all
    all=$(
        shopt -s nullglob
        for path in "$dir"/$pat; do
            [ -f "$path" ] || continue
            basename "$path"
        done | sort
    )
    local total
    total=$(printf '%s\n' "$all" | grep -c . || true)
    if [ "$total" -le "$keep" ]; then
        return 0
    fi
    local excess=$((total - keep))
    printf '%s\n' "$all" | sed -n "1,${excess}p" | while IFS= read -r f; do
        [ -z "$f" ] && continue
        log "prune old: $f"
        rm -f "$dir/$f"
    done
}
prune "$PG_DIR" "*.pg.dump.gz" "$MAX_KEEP"
prune "$REDIS_DIR" "*.redis.tgz" "$MAX_KEEP"

log "backup $TS complete"
