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
if [ ! -f "${SCRIPT_DIR}/lib.sh" ]; then
    echo "[backup] ERROR: ${SCRIPT_DIR}/lib.sh missing" >&2
    exit 1
fi
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

ENV_FILE="$(lumen_find_shared_env "${SCRIPT_ROOT}" 2>/dev/null || true)"
if [ -n "${ENV_FILE}" ]; then
    export LUMEN_ENV_FILE="${ENV_FILE}"
    for key in DB_USER DB_NAME DB_PASSWORD REDIS_URL REDIS_PASSWORD BACKUP_ROOT LUMEN_BACKUP_ROOT PG_CONTAINER REDIS_CONTAINER; do
        lumen_dotenv_export_if_unset "${key}" "${ENV_FILE}"
    done
fi

TS="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_ROOT="${BACKUP_ROOT:-${LUMEN_BACKUP_ROOT:-/opt/lumendata/backup}}"
PG_DIR="$BACKUP_ROOT/pg"
REDIS_DIR="$BACKUP_ROOT/redis"
# MAX_KEEP=56 ≈ 4h 间隔 × 56 = 9.3 天，覆盖工作周末 + 周一来才发现问题的
# 排查窗口。改小到 40（≈ 6.7 天）容易出现"周末出去几天回来发现备份只剩
# 一周"的情况。可在 systemd unit 或 .env 中覆盖。
MAX_KEEP="${MAX_KEEP:-56}"

PG_CONTAINER="${PG_CONTAINER:-lumen-pg}"
REDIS_CONTAINER="${REDIS_CONTAINER:-lumen-redis}"
# 优先用 REDIS_URL 嵌入的密码（与 api/worker 共用同一真值，即 lumen-redis 的 requirepass）；
# 兜底到单独那一行 REDIS_PASSWORD。这样 .env 两处字段漂移不会导致 backup 认证失败。
REDIS_PASSWORD="$(lumen_redis_resolve_password)"
PG_USER="${DB_USER:-lumen}"
PG_DB="${DB_NAME:-lumen}"
LOCK_BASE="${LUMEN_BACKUP_RESTORE_LOCKDIR:-${XDG_RUNTIME_DIR:-/run/lock}}"
if [ ! -d "$LOCK_BASE" ] || [ ! -w "$LOCK_BASE" ]; then
    LOCK_BASE="${TMPDIR:-/tmp}"
fi
LOCKFILE="${LUMEN_BACKUP_RESTORE_LOCKFILE:-${LOCK_BASE}/lumen-backup-restore.lock}"
LOCKDIR="$LOCKFILE.d"
BACKUP_TRIGGER_FILE="${LUMEN_BACKUP_TRIGGER_FILE:-${BACKUP_ROOT}/.backup.trigger}"
BACKUP_RUNNING_FILE="${LUMEN_BACKUP_RUNNING_FILE:-${BACKUP_ROOT}/.backup.running}"
BACKUP_PENDING_FILE="${LUMEN_BACKUP_PENDING_FILE:-${BACKUP_ROOT}/.backup.pending}"
BACKUP_TRIGGER_FINGERPRINT=""
BACKUP_SERVICE_MARKER_ACTIVE=0
LOCK_KIND=""
TMP_DIR=""
PG_OUT=""
REDIS_OUT=""
PG_TMP=""
REDIS_TMP=""

log() { printf '[backup %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

trigger_fingerprint() {
    local file="$1"
    [ -f "$file" ] || return 0
    {
        stat -c '%y:%s:%i' "$file" 2>/dev/null \
            || stat -f '%Sm:%z:%i' "$file" 2>/dev/null \
            || ls -l "$file" 2>/dev/null
        cksum "$file" 2>/dev/null || true
    } | tr '\n' '|'
}

mark_backup_running() {
    [ "${LUMEN_BACKUP_SERVICE_MODE:-0}" = "1" ] || return 0
    mkdir -p "$BACKUP_ROOT"
    local tmp="${BACKUP_RUNNING_FILE}.$$"
    {
        printf 'pid=%s\n' "$$"
        printf 'started_at=%s\n' "$(date -u +%FT%TZ)"
    } > "$tmp"
    mv -f "$tmp" "$BACKUP_RUNNING_FILE"
    BACKUP_TRIGGER_FINGERPRINT="$(trigger_fingerprint "$BACKUP_TRIGGER_FILE")"
    BACKUP_SERVICE_MARKER_ACTIVE=1
}

mark_backup_pending_if_retriggered() {
    [ "$BACKUP_SERVICE_MARKER_ACTIVE" = "1" ] || return 0
    [ -f "$BACKUP_TRIGGER_FILE" ] || return 0
    local current
    current="$(trigger_fingerprint "$BACKUP_TRIGGER_FILE")"
    if [ -n "$current" ] && [ "$current" != "$BACKUP_TRIGGER_FINGERPRINT" ]; then
        {
            printf 'pid=%s\n' "$$"
            printf 'queued_at=%s\n' "$(date -u +%FT%TZ)"
        } > "$BACKUP_PENDING_FILE"
        log "detected another backup trigger while running; queued one follow-up run"
    fi
}

release_lock() {
    if [ "$LOCK_KIND" = "flock" ]; then
        flock -u 7 2>/dev/null || true
        exec 7>&- 2>/dev/null || true
    elif [ "$LOCK_KIND" = "mkdir" ]; then
        rm -rf "$LOCKDIR" 2>/dev/null || true
    fi
}

cleanup() {
    local rc=$?
    mark_backup_pending_if_retriggered
    if [ "$BACKUP_SERVICE_MARKER_ACTIVE" = "1" ]; then
        rm -f "$BACKUP_RUNNING_FILE" 2>/dev/null || true
    fi
    if [ "$rc" -ne 0 ]; then
        [ -n "${PG_TMP:-}" ] && rm -f "$PG_TMP" 2>/dev/null || true
        [ -n "${REDIS_TMP:-}" ] && rm -f "$REDIS_TMP" 2>/dev/null || true
        [ -n "${PG_OUT:-}" ] && rm -f "$PG_OUT" 2>/dev/null || true
        [ -n "${REDIS_OUT:-}" ] && rm -f "$REDIS_OUT" 2>/dev/null || true
    fi
    if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR" 2>/dev/null || true
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

    if mkdir "$LOCKDIR" 2>/dev/null; then
        printf '%s\n' "$$" > "$LOCKDIR/pid" 2>/dev/null || true
        LOCK_KIND="mkdir"
        return 0
    fi

    # mkdir 失败：stale-check（进程被 kill -9 后锁残留）。同 lib.sh 行为。
    local _owner_pid="" _stale=0
    if [ -f "$LOCKDIR/pid" ]; then
        _owner_pid="$(cat "$LOCKDIR/pid" 2>/dev/null | tr -d '[:space:]')"
        if [ -n "$_owner_pid" ] && ! kill -0 "$_owner_pid" 2>/dev/null; then
            _stale=1
        fi
    fi
    if [ "$_stale" = "1" ]; then
        log "WARN stale lock (owner pid=$_owner_pid 已死)，清理后重试"
        rm -rf "$LOCKDIR" 2>/dev/null || true
        if mkdir "$LOCKDIR" 2>/dev/null; then
            printf '%s\n' "$$" > "$LOCKDIR/pid" 2>/dev/null || true
            LOCK_KIND="mkdir"
            return 0
        fi
    fi

    log "ERROR: another backup/restore is already running (lock: $LOCKDIR, owner=${_owner_pid:-未知})"
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
    # redis-cli 把协议错误（NOAUTH/WRONGPASS/...）当作正常回复打印到 stdout 并 exit 0；
    # set -euo pipefail 拦不住，必须 wrapper 里识别。捕获合并后的输出再判决：
    #   - docker exec 非零 → 报错返回非零
    #   - 输出匹配协议错误前缀 → 报错返回非零（不输出 stdout，避免上层把错误当数据）
    #   - 否则 stdout 透传
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
                log "ERROR: redis $label missing: ${err_msg:-docker cp exit $rc}"
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

redis_info_value() {
    local section="$1"
    local key="$2"
    local out
    if ! out="$(redis_cli INFO "$section" | tr -d '\r')"; then
        return 1
    fi
    printf '%s\n' "$out" | sed -n "s/^${key}://p" | head -n1
}

redis_bgsave_start() {
    local out rc
    if [ -n "$REDIS_PASSWORD" ]; then
        out="$(REDISCLI_AUTH="$REDIS_PASSWORD" docker exec -e REDISCLI_AUTH "$REDIS_CONTAINER" redis-cli --no-auth-warning BGSAVE 2>&1)"
    else
        out="$(docker exec "$REDIS_CONTAINER" redis-cli BGSAVE 2>&1)"
    fi
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log "ERROR: redis-cli BGSAVE exit=$rc out=${out}"
        return "$rc"
    fi
    case "$out" in
        *"Background save already in progress"*)
            return 2
            ;;
    esac
    if lumen_redis_is_error_reply "$out"; then
        log "ERROR: redis-cli BGSAVE protocol error: ${out}"
        return 1
    fi
    printf '%s' "$out"
}

wait_for_redis_bgsave() {
    local last_now in_progress status
    for _ in $(seq 1 60); do
        in_progress="$(redis_info_value persistence rdb_bgsave_in_progress 2>/dev/null || true)"
        last_now="$(redis_cli LASTSAVE | tr -d '\r\n')"
        if ! [[ "$last_now" =~ ^[0-9]+$ ]]; then
            log "ERROR: LASTSAVE returned non-numeric: ${last_now}"
            return 1
        fi
        if [ "$in_progress" != "1" ]; then
            status="$(redis_info_value persistence rdb_last_bgsave_status 2>/dev/null || true)"
            if [ -n "$status" ] && [ "$status" != "ok" ]; then
                log "ERROR: redis last BGSAVE status is ${status}"
                return 1
            fi
            LAST_NOW="$last_now"
            return 0
        fi
        sleep 1
    done
    log "ERROR: redis BGSAVE did not complete in 60s"
    return 1
}

trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM

mark_backup_running

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
# 注意：lumen_try_acquire_lock（上面的维护锁）会自己 `trap 'lumen_release_lock' EXIT`，
# 这里再次 `trap cleanup EXIT` 会覆盖它 —— 但 cleanup() 内显式 fall through 调
# `lumen_release_lock`，所以维护锁仍会被释放。change order/拆函数前请保留这条不变量。
trap cleanup EXIT
mkdir -p "$PG_DIR" "$REDIS_DIR"

# ---- Postgres ----
PG_OUT="$PG_DIR/$TS.pg.dump.gz"
PG_TMP="$PG_OUT.tmp.$$"
log "dumping postgres → $PG_OUT"
PG_ERR="$(mktemp "${BACKUP_ROOT}/.pg-dump.XXXXXX.err")" || {
    log "ERROR: failed to create pg_dump error log"
    exit 5
}
set +e
docker exec -i "$PG_CONTAINER" pg_dump -U "$PG_USER" -Fc "$PG_DB" 2>"$PG_ERR" | gzip -c > "$PG_TMP"
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
    rm -f "$PG_ERR" "$PG_TMP" "$PG_OUT"
    exit 2
fi
rm -f "$PG_ERR"
# 基本合理性：gzip 有效 + 非空
if ! gzip -t "$PG_TMP" 2>/dev/null || [ ! -s "$PG_TMP" ]; then
    log "ERROR: pg dump invalid, removing"
    rm -f "$PG_TMP" "$PG_OUT"
    exit 2
fi
mv -f "$PG_TMP" "$PG_OUT"
PG_TMP=""
PG_SIZE="$(file_size "$PG_OUT")"
log "pg dump ok size=$PG_SIZE"

# ---- Redis ----
REDIS_OUT="$REDIS_DIR/$TS.redis.tgz"
REDIS_TMP="$REDIS_OUT.tmp.$$"
# BGSAVE 前先 ping，让认证失败立刻报错，而不是绕一圈伪装成 "BGSAVE did not complete in 60s"。
if ! ping_out="$(redis_cli PING)" || [ "$ping_out" != "PONG" ]; then
    log "ERROR: redis ping failed before BGSAVE — check REDIS_URL/REDIS_PASSWORD vs lumen-redis requirepass"
    exit 3
fi
log "triggering redis BGSAVE"
# 记录 lastsave 时间戳作为观测字段；真正完成条件看 rdb_bgsave_in_progress。
# 只看 LASTSAVE 秒级变化不可靠：同一秒内 BGSAVE 完成会被误判超时。
LAST_BEFORE="$(redis_cli LASTSAVE | tr -d '\r\n')"
if ! [[ "$LAST_BEFORE" =~ ^[0-9]+$ ]]; then
    log "ERROR: LASTSAVE returned non-numeric: ${LAST_BEFORE}"
    exit 3
fi
LAST_NOW="$LAST_BEFORE"
set +e
bgsave_out="$(redis_bgsave_start)"
bgsave_rc=$?
set -e
if [ "$bgsave_rc" -eq 0 ]; then
    log "redis BGSAVE response: ${bgsave_out}"
elif [ "$bgsave_rc" -eq 2 ]; then
    log "redis BGSAVE already in progress; waiting for current save"
else
    exit 3
fi
if ! wait_for_redis_bgsave; then
    exit 3
fi
log "BGSAVE done (lastsave ${LAST_BEFORE} -> ${LAST_NOW}), packaging"

# 从 redis 容器里把 dump.rdb 和 appendonly 拷出来打包
TMP_DIR="$(make_tmp_dir)"
if ! docker_cp_redis "/data/dump.rdb" "$TMP_DIR/dump.rdb" "dump.rdb" "required"; then
    exit 4
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

tar -czf "$REDIS_TMP" -C "$TMP_DIR" .
if ! tar -tzf "$REDIS_TMP" >/dev/null; then
    log "ERROR: redis archive invalid, removing"
    rm -f "$REDIS_TMP" "$REDIS_OUT"
    exit 4
fi
mv -f "$REDIS_TMP" "$REDIS_OUT"
REDIS_TMP=""
REDIS_SIZE="$(file_size "$REDIS_OUT")"
log "redis pack ok size=$REDIS_SIZE"

# ---- Retention ----
# 严格 YYYYMMDD-HHMMSS timestamp 提取；忽略手工 cp 进来的非时间戳文件（例如
# manual-2024.pg.dump.gz），避免它们干扰排序导致超额删掉真正的 backup。
_extract_ts() {
    local dir="$1"
    local suffix="$2"
    [ -d "$dir" ] || return 0
    ls "$dir" 2>/dev/null \
        | grep -E "^[0-9]{8}-[0-9]{6}\\.${suffix//\./\\.}$" \
        | sed -E "s/\\.${suffix//\./\\.}$//" \
        | sort -u
}

# 配对 prune：之前 PG / Redis 各自独立删，可能淘汰掉"PG 有但 Redis 没有"
# 的 timestamp，反过来 restore 拿到孤儿对直接 exit 2。
# 修复：先取 PG ∩ Redis 的成对 timestamp，按字典序保留最新 keep 份；其余
# 成对删除。同时把没配对的孤儿（PG 有 Redis 没有，或反之）也删——保留没用，
# restore 也用不了，徒占磁盘。
prune_paired() {
    local pg_dir="$1"
    local redis_dir="$2"
    local keep="$3"

    local pg_ts redis_ts
    pg_ts="$(_extract_ts "$pg_dir" "pg.dump.gz")"
    redis_ts="$(_extract_ts "$redis_dir" "redis.tgz")"

    # comm 要求两个输入排序；上面 sort -u 已排序。
    local paired orphan_pg orphan_redis
    paired="$(comm -12 <(printf '%s\n' "$pg_ts") <(printf '%s\n' "$redis_ts"))"
    orphan_pg="$(comm -23 <(printf '%s\n' "$pg_ts") <(printf '%s\n' "$redis_ts"))"
    orphan_redis="$(comm -13 <(printf '%s\n' "$pg_ts") <(printf '%s\n' "$redis_ts"))"

    while IFS= read -r ts; do
        [ -z "$ts" ] && continue
        log "prune orphan PG (no redis pair): $ts"
        rm -f "$pg_dir/$ts.pg.dump.gz"
    done <<< "$orphan_pg"
    while IFS= read -r ts; do
        [ -z "$ts" ] && continue
        log "prune orphan Redis (no pg pair): $ts"
        rm -f "$redis_dir/$ts.redis.tgz"
    done <<< "$orphan_redis"

    local total excess
    total="$(printf '%s\n' "$paired" | grep -c . || true)"
    if [ "$total" -le "$keep" ]; then
        return 0
    fi
    excess=$((total - keep))
    printf '%s\n' "$paired" | sort | sed -n "1,${excess}p" | while IFS= read -r ts; do
        [ -z "$ts" ] && continue
        log "prune old paired: $ts"
        rm -f "$pg_dir/$ts.pg.dump.gz" "$redis_dir/$ts.redis.tgz"
    done
}
prune_paired "$PG_DIR" "$REDIS_DIR" "$MAX_KEEP"

log "backup $TS complete"
printf '{"timestamp":"%s","pg_size":%s,"redis_size":%s}\n' "$TS" "${PG_SIZE:-0}" "${REDIS_SIZE:-0}"
