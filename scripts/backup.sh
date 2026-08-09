#!/usr/bin/env bash
# Lumen 定时备份：pg_dump + Redis dump.rdb → /opt/lumendata/backup
# 每 4 小时触发一次（systemd timer）。保留最近 MAX_KEEP 份。
#
# 文件命名：<timestamp>.pg.dump.gz / <timestamp>.redis.tgz
# 同一 timestamp 两个文件配对，被视为一个"备份点"。
set -euo pipefail

_LUMEN_BACKUP_INPUT_DEPLOY_ROOT="${LUMEN_DEPLOY_ROOT-}"
_LUMEN_BACKUP_INPUT_MAINT_ROOT="${LUMEN_MAINT_ROOT-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"

# 复用 lib.sh 的 lumen_try_acquire_lock，让 backup 与 install/update/uninstall 互斥。
# 在 backup 自己的 backup-restore 锁之前加一层维护锁；维护锁被占用时返回
# EX_TEMPFAIL，让 systemd 持续退避重试并暴露失败。
if [ ! -f "${SCRIPT_DIR}/lib.sh" ]; then
    echo "[backup] ERROR: ${SCRIPT_DIR}/lib.sh missing" >&2
    exit 1
fi
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"
# shellcheck source=lib/backup_restore_services.sh
. "${SCRIPT_DIR}/lib/backup_restore_services.sh"

if ! LUMEN_DEPLOY_ROOT="$(
        lumen_resolve_deploy_root \
            "${SCRIPT_DIR}" \
            "${_LUMEN_BACKUP_INPUT_DEPLOY_ROOT}" \
            "${_LUMEN_BACKUP_INPUT_MAINT_ROOT}"
)"; then
    echo "[backup] ERROR: refusing unsafe or ambiguous deployment root" >&2
    exit 78
fi
export LUMEN_DEPLOY_ROOT
unset _LUMEN_BACKUP_INPUT_DEPLOY_ROOT _LUMEN_BACKUP_INPUT_MAINT_ROOT

if ! lumen_release_shared_env_path_safe "${LUMEN_DEPLOY_ROOT}"; then
    echo "[backup] ERROR: refusing unsafe shared/.env" >&2
    exit 78
fi
ENV_FILE="$(lumen_find_shared_env "${LUMEN_DEPLOY_ROOT}" 2>/dev/null || true)"
if [ -n "${ENV_FILE}" ]; then
    export LUMEN_ENV_FILE="${ENV_FILE}"
    for key in DB_USER DB_NAME DB_PASSWORD REDIS_URL REDIS_PASSWORD BACKUP_ROOT LUMEN_BACKUP_ROOT PG_CONTAINER REDIS_CONTAINER; do
        lumen_dotenv_export_if_unset "${key}" "${ENV_FILE}"
    done
fi

TS="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_OPERATION_ID="${LUMEN_BACKUP_OPERATION_ID:-backup-${TS}-$$}"
BACKUP_ROOT="${BACKUP_ROOT:-${LUMEN_BACKUP_ROOT:-/opt/lumendata/backup}}"
PG_DIR="$BACKUP_ROOT/pg"
REDIS_DIR="$BACKUP_ROOT/redis"
# MAX_KEEP=56 ≈ 4h 间隔 × 56 = 9.3 天，覆盖工作周末 + 周一来才发现问题的
# 排查窗口。改小到 40（≈ 6.7 天）容易出现"周末出去几天回来发现备份只剩
# 一周"的情况。可在 systemd unit 或 .env 中覆盖。
MAX_KEEP="${MAX_KEEP:-56}"
MAX_KEEP_LIMIT=1000

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
PG_ERR=""
PAIR_MARKER=""
PG_SHA256=""
REDIS_SHA256=""
PAIR_COMMITTED=0
SUCCESS_RECEIPT_COMMITTED=0
BACKUP_LOCK_OWNER_TOKEN=""
WRITERS_STOPPED=0
ACTIVE_WRITER_SERVICES=()

log() { printf '[backup %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

if [ ! -f "${SCRIPT_DIR}/lib/backup_journal.sh" ]; then
    log "ERROR: ${SCRIPT_DIR}/lib/backup_journal.sh missing"
    exit 1
fi
# shellcheck source=lib/backup_journal.sh
. "${SCRIPT_DIR}/lib/backup_journal.sh"

validate_max_keep() {
    local value="$MAX_KEEP"
    local normalized
    case "$value" in
        ''|*[!0-9]*)
            log "ERROR: MAX_KEEP must be a decimal integer from 1 to ${MAX_KEEP_LIMIT} (got: ${value})"
            return 1
            ;;
    esac
    normalized="$(printf '%s' "$value" | sed 's/^0*//')"
    normalized="${normalized:-0}"
    if [ "$normalized" -lt 1 ]; then
        log "ERROR: MAX_KEEP must be at least 1; refusing to delete every backup (got: ${value})"
        return 1
    fi
    if [ "${#normalized}" -gt "${#MAX_KEEP_LIMIT}" ] \
            || [ "$normalized" -gt "$MAX_KEEP_LIMIT" ]; then
        log "ERROR: MAX_KEEP exceeds safety limit ${MAX_KEEP_LIMIT} (got: ${value})"
        return 1
    fi
    MAX_KEEP="$normalized"
}

if ! validate_max_keep; then
    exit 2
fi

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
    local marker_lock_mode=""
    if command -v flock >/dev/null 2>&1; then
        exec 5>>"${BACKUP_ROOT}/.maintenance-markers.lock"
        if ! chmod 0660 "${BACKUP_ROOT}/.maintenance-markers.lock"; then
            marker_lock_mode="$(
                stat -c '%a' "${BACKUP_ROOT}/.maintenance-markers.lock" 2>/dev/null \
                    || stat -f '%Lp' "${BACKUP_ROOT}/.maintenance-markers.lock" 2>/dev/null \
                    || printf '%s' ""
            )"
            case "$marker_lock_mode" in
                660|0660)
                    ;;
                *)
                    log "ERROR: backup ownership lock permissions are unsafe"
                    exec 5>&-
                    return 75
                    ;;
            esac
        fi
        if ! flock -n 5; then
            log "ERROR: backup ownership marker is busy"
            exec 5>&-
            return 75
        fi
    fi
    local existing_operation_id=""
    if [ -f "$BACKUP_RUNNING_FILE" ]; then
        while IFS= read -r _marker_line; do
            case "$_marker_line" in
                operation_id=*)
                    existing_operation_id="${_marker_line#operation_id=}"
                    ;;
            esac
        done < "$BACKUP_RUNNING_FILE"
    fi
    if [ -z "${LUMEN_BACKUP_OPERATION_ID:-}" ] \
            && [ -n "$existing_operation_id" ]; then
        case "$existing_operation_id" in
            *[!A-Za-z0-9._:-]*)
                ;;
            *)
                BACKUP_OPERATION_ID="$existing_operation_id"
                ;;
        esac
    fi
    {
        printf 'pid=%s\n' "$$"
        printf 'started_at=%s\n' "$(date -u +%FT%TZ)"
        printf 'operation_id=%s\n' "$BACKUP_OPERATION_ID"
        printf 'owner=host\n'
        printf 'generation=1\n'
    } > "$tmp"
    chmod 0660 "$tmp"
    mv -f "$tmp" "$BACKUP_RUNNING_FILE"
    if command -v flock >/dev/null 2>&1; then
        flock -u 5
        exec 5>&-
    fi
    BACKUP_TRIGGER_FINGERPRINT="$(trigger_fingerprint "$BACKUP_TRIGGER_FILE")"
    BACKUP_SERVICE_MARKER_ACTIVE=1
}

if [ -z "${LUMEN_BACKUP_OPERATION_ID:-}" ] && [ -f "$BACKUP_TRIGGER_FILE" ]; then
    _trigger_operation_id="$(
        python3 - "$BACKUP_TRIGGER_FILE" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    payload = None
if isinstance(payload, dict):
    value = payload.get("operation_id")
    if isinstance(value, str) and value and "\n" not in value and "\r" not in value:
        print(value)
PY
    )"
    if [ -n "${_trigger_operation_id:-}" ]; then
        BACKUP_OPERATION_ID="$_trigger_operation_id"
    fi
    unset _trigger_operation_id
fi

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
        if ! lumen_release_owned_lock_dir \
                "$LOCKDIR" "${BACKUP_LOCK_OWNER_TOKEN:-}"; then
            log "WARN backup/restore lock owner changed; refusing removal: $LOCKDIR"
        fi
    fi
}

cleanup() {
    local rc=$?
    local backup_rc="$rc"
    trap - EXIT
    trap '' INT TERM HUP
    if [ "$WRITERS_STOPPED" -eq 1 ]; then
        log "restarting quiesced writers: ${ACTIVE_WRITER_SERVICES[*]:-<none>}"
        if [ "${BACKUP_JOURNAL_ACTIVE:-0}" -eq 1 ]; then
            lumen_backup_journal_write "writers_starting" || rc=70
        fi
        if [ "${#ACTIVE_WRITER_SERVICES[@]}" -gt 0 ] \
                && ! lumen_start_services_verified "${ACTIVE_WRITER_SERVICES[@]}"; then
            log "ERROR: failed to restart one or more backup writers"
            rc=70
        elif [ "${BACKUP_JOURNAL_ACTIVE:-0}" -eq 1 ] \
                && ! lumen_backup_journal_clear; then
            rc=70
        fi
        WRITERS_STOPPED=0
    fi
    mark_backup_pending_if_retriggered
    if [ "$BACKUP_SERVICE_MARKER_ACTIVE" = "1" ] && [ "$backup_rc" -eq 0 ]; then
        rm -f "$BACKUP_RUNNING_FILE" 2>/dev/null || true
    elif [ "$BACKUP_SERVICE_MARKER_ACTIVE" = "1" ]; then
        log "retaining host ownership marker after failed backup operation"
    fi
    if [ "$backup_rc" -ne 0 ]; then
        [ -n "${PG_TMP:-}" ] && rm -f "$PG_TMP" 2>/dev/null || true
        [ -n "${REDIS_TMP:-}" ] && rm -f "$REDIS_TMP" 2>/dev/null || true
        [ -n "${PG_ERR:-}" ] && rm -f "$PG_ERR" 2>/dev/null || true
        if [ "$PAIR_COMMITTED" -ne 1 ]; then
            [ -n "${PG_OUT:-}" ] && rm -f "$PG_OUT" 2>/dev/null || true
            [ -n "${REDIS_OUT:-}" ] && rm -f "$REDIS_OUT" 2>/dev/null || true
            if [ -n "${PAIR_MARKER:-}" ]; then
                rm -f "$PAIR_MARKER" 2>/dev/null || true
                backup_fsync_directory "$BACKUP_ROOT" 2>/dev/null || true
            fi
        elif [ "$SUCCESS_RECEIPT_COMMITTED" -ne 1 ]; then
            log "retaining committed backup pair after terminal receipt failure"
        fi
    fi
    if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR" 2>/dev/null || true
    fi
    release_lock
    if command -v lumen_release_lock >/dev/null 2>&1; then
        lumen_release_lock 2>/dev/null || true
    fi
    exit "$rc"
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

    if lumen_try_create_owned_lock_dir "$LOCKDIR" script "backup.sh"; then
        BACKUP_LOCK_OWNER_TOKEN="${LUMEN_LAST_LOCK_OWNER_TOKEN}"
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

file_size() {
    wc -c < "$1" | tr -d '[:space:]'
}

backup_failpoint() {
    local phase="$1"
    local configured=",${LUMEN_BACKUP_FAILPOINT:-},${LUMEN_BACKUP_FAILPOINTS:-},"
    case "$configured" in
        *",${phase},"*)
            log "ERROR: backup crash failpoint triggered: ${phase}"
            kill -KILL "$$"
            sleep 1
            exit 137
            ;;
    esac
}

backup_fsync_file() {
    python3 - "$1" <<'PY'
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
descriptor = os.open(
    path,
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0),
)
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise SystemExit(1)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

backup_fsync_directory() {
    python3 - "$1" <<'PY'
import errno
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
descriptor = os.open(
    path,
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0),
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

publish_backup_pair_marker() {
    python3 - \
            "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" <<'PY'
import errno
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import stat
import sys
import tempfile

(
    marker_raw,
    root_raw,
    operation_id,
    timestamp,
    pg_raw,
    redis_raw,
    pg_size_raw,
    redis_size_raw,
) = sys.argv[1:]
marker = Path(marker_raw)
root = Path(root_raw)
pg = Path(pg_raw)
redis = Path(redis_raw)
if marker.parent != root:
    raise SystemExit("backup pair marker escaped backup root")
if pg != root / "pg" / f"{timestamp}.pg.dump.gz":
    raise SystemExit("postgres backup path does not match pair identity")
if redis != root / "redis" / f"{timestamp}.redis.tgz":
    raise SystemExit("redis backup path does not match pair identity")
try:
    pg_size = int(pg_size_raw)
    redis_size = int(redis_size_raw)
except ValueError:
    raise SystemExit("backup pair sizes are invalid")


def digest_regular(path: Path, expected_size: int) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
            raise SystemExit(f"backup payload changed before pair commit: {path}")
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(chunk)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


pg_hash = digest_regular(pg, pg_size)
redis_hash = digest_regular(redis, redis_size)
document = {
    "committed_at": datetime.now(timezone.utc).isoformat(),
    "operation_id": operation_id,
    "pg": {
        "name": pg.name,
        "sha256": pg_hash,
        "size": pg_size,
    },
    "redis": {
        "name": redis.name,
        "sha256": redis_hash,
        "size": redis_size,
    },
    "schema": 1,
    "timestamp": timestamp,
}
payload = (
    json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    + "\n"
).encode("utf-8")
try:
    existing = marker.lstat()
except FileNotFoundError:
    existing = None
if existing is not None and not stat.S_ISREG(existing.st_mode):
    raise SystemExit("backup pair marker destination is unsafe")
descriptor, temporary_raw = tempfile.mkstemp(
    prefix=f".{marker.name}.",
    suffix=".tmp",
    dir=root,
)
temporary = Path(temporary_raw)
try:
    os.fchmod(descriptor, 0o600)
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while persisting backup pair marker")
        view = view[written:]
    os.fsync(descriptor)
    os.close(descriptor)
    descriptor = -1
    os.replace(temporary, marker)
    directory_fd = os.open(
        root,
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
    if descriptor >= 0:
        os.close(descriptor)
    temporary.unlink(missing_ok=True)
print(f"{pg_hash}\t{redis_hash}")
PY
}

emit_backup_result() {
    python3 - \
            "$TS" "$BACKUP_OPERATION_ID" "$PG_SIZE" "$REDIS_SIZE" \
            "$PG_SHA256" "$REDIS_SHA256" "$PAIR_MARKER" <<'PY'
import json
import sys

(
    timestamp,
    operation_id,
    pg_size,
    redis_size,
    pg_sha256,
    redis_sha256,
    pair_marker,
) = sys.argv[1:]
print(
    json.dumps(
        {
            "operation_id": operation_id,
            "pair_marker": pair_marker,
            "pg_sha256": pg_sha256,
            "pg_size": int(pg_size),
            "redis_sha256": redis_sha256,
            "redis_size": int(redis_size),
            "timestamp": timestamp,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
)
PY
}

record_backup_success() {
    local marker="${BACKUP_ROOT}/.backup.last-success.json"
    python3 - \
            "${marker}" "${TS}" "${BACKUP_OPERATION_ID}" "${PAIR_MARKER}" <<'PY'
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
payload = {
    "completed_at": sys.argv[2],
    "operation_id": sys.argv[3],
    "pair_marker": Path(sys.argv[4]).name,
}
tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
data = (json.dumps(payload, sort_keys=True) + "\n").encode()
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
try:
    os.fchmod(fd, 0o640)
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting backup success receipt")
        view = view[written:]
    os.fsync(fd)
finally:
    os.close(fd)
os.replace(tmp, path)
dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(dir_fd)
finally:
    os.close(dir_fd)
PY
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
    rm -rf -- "$dest" 2>/dev/null || true

    case "$err_msg" in
        *"Could not find"*|*"not found"*|*"No such container:path"*)
            if [ "$required" = "required" ]; then
                log "ERROR: redis $label missing: ${err_msg:-docker cp exit $rc}"
            else
                log "redis $label not present; skipping"
                return 2
            fi
            ;;
        *)
            log "ERROR: docker cp failed for redis $label (exit $rc): ${err_msg:-unknown error}"
            ;;
    esac
    return 1
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

wait_for_redis_bgsave_idle() {
    local in_progress
    for _ in $(seq 1 60); do
        if ! in_progress="$(
            redis_info_value persistence rdb_bgsave_in_progress
        )"; then
            sleep 1
            continue
        fi
        case "$in_progress" in
            0) return 0 ;;
            1) ;;
            *)
                log "ERROR: redis rdb_bgsave_in_progress is invalid: ${in_progress}"
                return 1
                ;;
        esac
        sleep 1
    done
    log "ERROR: redis BGSAVE already in progress before the fresh backup did not become idle in 60s"
    return 1
}

capture_redis_bgsave_baseline() {
    LAST_BEFORE="$(redis_cli LASTSAVE | tr -d '\r\n')"
    if ! [[ "$LAST_BEFORE" =~ ^[0-9]+$ ]]; then
        log "ERROR: LASTSAVE returned non-numeric: ${LAST_BEFORE}"
        return 1
    fi
    LAST_NOW="$LAST_BEFORE"
    RDB_SAVES_BEFORE="$(redis_info_value persistence rdb_saves)"
    if ! [[ "$RDB_SAVES_BEFORE" =~ ^[0-9]+$ ]]; then
        log "ERROR: redis rdb_saves is unavailable before BGSAVE"
        return 1
    fi
}

wait_for_redis_bgsave_generation() {
    local saves_before="$1"
    local last_now in_progress status saves_now
    for _ in $(seq 1 60); do
        if ! in_progress="$(
            redis_info_value persistence rdb_bgsave_in_progress
        )" || ! status="$(
            redis_info_value persistence rdb_last_bgsave_status
        )" || ! saves_now="$(redis_info_value persistence rdb_saves)"; then
            sleep 1
            continue
        fi
        if ! last_now="$(redis_cli LASTSAVE | tr -d '\r\n')"; then
            sleep 1
            continue
        fi
        if ! [[ "$last_now" =~ ^[0-9]+$ ]]; then
            log "ERROR: LASTSAVE returned non-numeric: ${last_now}"
            return 1
        fi
        if ! [[ "$saves_now" =~ ^[0-9]+$ ]]; then
            log "ERROR: redis rdb_saves is not numeric: ${saves_now}"
            return 1
        fi
        if [ "$in_progress" = "0" ] \
                && [ "$status" = "ok" ] \
                && [ "$saves_now" -gt "$saves_before" ]; then
            LAST_NOW="$last_now"
            return 0
        fi
        if [ "$in_progress" != "0" ] && [ "$in_progress" != "1" ]; then
            log "ERROR: redis rdb_bgsave_in_progress is invalid: ${in_progress}"
            return 1
        fi
        if [ "$status" != "ok" ] && [ "$in_progress" = "0" ]; then
            log "ERROR: redis last BGSAVE status is ${status}"
            return 1
        fi
        sleep 1
    done
    log "ERROR: redis BGSAVE did not complete in 60s"
    return 1
}

create_fresh_redis_bgsave() {
    local attempts="${LUMEN_REDIS_BGSAVE_START_ATTEMPTS:-3}"
    local attempt bgsave_out bgsave_rc
    case "$attempts" in
        ''|*[!0-9]*|0)
            log "ERROR: invalid LUMEN_REDIS_BGSAVE_START_ATTEMPTS: ${attempts}"
            return 1
            ;;
    esac

    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if ! wait_for_redis_bgsave_idle; then
            return 1
        fi
        if ! capture_redis_bgsave_baseline; then
            return 1
        fi

        set +e
        bgsave_out="$(redis_bgsave_start)"
        bgsave_rc=$?
        set -e
        if [ "$bgsave_rc" -eq 0 ]; then
            log "redis BGSAVE response: ${bgsave_out}"
            if wait_for_redis_bgsave_generation "$RDB_SAVES_BEFORE"; then
                return 0
            fi
            return 1
        fi
        if [ "$bgsave_rc" -ne 2 ]; then
            return 1
        fi
        log "redis BGSAVE raced with another save after quiesce; waiting before retry ${attempt}/${attempts}"
    done

    log "ERROR: could not start a backup-owned Redis BGSAVE after ${attempts} attempts"
    return 1
}

trap cleanup EXIT
trap '' INT TERM HUP

# 维护锁：与 install/update/uninstall 互斥；被占用时返回可重试失败。
# updater 的 preflight 只能通过可验证的 inherited FD/token capability 借用父进程
# 已持有的同一把锁。环境开关本身不再具有绕过能力。
if [ -n "${LUMEN_BORROWED_MAINTENANCE_LOCK_KIND:-}" ]; then
    if ! lumen_verify_borrowed_maintenance_lock "${LUMEN_DEPLOY_ROOT}"; then
        log "ERROR: invalid borrowed maintenance lock capability"
        exit 78
    fi
    log "using verified borrowed maintenance lock (${LUMEN_BORROWED_MAINTENANCE_LOCK_KIND})"
elif command -v lumen_try_acquire_lock >/dev/null 2>&1; then
    if [ "${LUMEN_BACKUP_SERVICE_MODE:-0}" = "1" ] \
            && ! command -v flock >/dev/null 2>&1; then
        log "ERROR: systemd backup service requires flock (install util-linux)"
        exit 78
    fi
    if ! lumen_try_acquire_lock "${LUMEN_DEPLOY_ROOT}" "backup.sh"; then
        log "DEFERRED: maintenance lock held; systemd must retry this backup"
        exit 75
    fi
fi

# Maintenance lock helpers install their own EXIT trap. Keep catchable signals
# ignored until both locks and owner tokens are fully recorded, then restore the
# unified cleanup handler before enabling signal exits.
trap cleanup EXIT
mark_backup_running
acquire_lock
trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP
mkdir -p "$PG_DIR" "$REDIS_DIR"
PAIR_MARKER="$BACKUP_ROOT/.backup-pair.$TS.json"
if ! backup_fsync_directory "$BACKUP_ROOT"; then
    log "ERROR: failed to fsync backup directory creation"
    exit 5
fi

if ! lumen_backup_recover_interrupted; then
    log "ERROR: interrupted backup service state could not be recovered"
    exit 70
fi
if [ "${BACKUP_JOURNAL_RECOVERED:-0}" -eq 1 ]; then
    log "backup recovery consumer completed; next timer/trigger will create a new pair"
    exit 0
fi

# Freeze every application writer before taking either side of the backup pair.
if ! writer_snapshot="$(lumen_running_writer_services)"; then
    log "ERROR: failed to capture the pre-backup writer state"
    exit 70
fi
while IFS= read -r service; do
    [ -n "$service" ] && ACTIVE_WRITER_SERVICES+=("$service")
done <<< "$writer_snapshot"
if ! lumen_backup_journal_write "writers_stopping"; then
    exit 70
fi
WRITERS_STOPPED=1
log "quiescing writers for paired backup: ${ACTIVE_WRITER_SERVICES[*]:-<none>}"
if ! lumen_quiesce_all_writer_services; then
    log "ERROR: failed to stop and verify every writer before backup"
    exit 6
fi
if ! lumen_backup_journal_write "writers_stopped"; then
    exit 70
fi

# ---- Postgres ----
PG_OUT="$PG_DIR/$TS.pg.dump.gz"
PG_TMP="$PG_OUT.tmp.$$"
log "dumping postgres → $PG_OUT"
PG_ERR="$(mktemp "${BACKUP_ROOT}/.pg-dump.err.XXXXXX")" || {
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
if ! backup_fsync_file "$PG_TMP"; then
    log "ERROR: failed to fsync postgres backup payload"
    exit 2
fi
backup_failpoint after_pg_temp_fsync
mv -f "$PG_TMP" "$PG_OUT"
PG_TMP=""
if ! backup_fsync_directory "$PG_DIR"; then
    log "ERROR: failed to fsync postgres backup rename"
    exit 2
fi
backup_failpoint after_pg_rename
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
# A BGSAVE that started before writer quiesce may contain an older Redis view
# than the later Postgres dump. Wait it out, then record a new generation
# baseline and require a BGSAVE started after quiesce to complete successfully.
if ! create_fresh_redis_bgsave; then
    exit 3
fi
log "BGSAVE done (lastsave ${LAST_BEFORE} -> ${LAST_NOW}), packaging verified RDB"

# 新备份只提交已验证的 dump.rdb。Redis 7 multipart AOF 仍可能在 BGSAVE 后继续
# rotate；复制 live appendonlydir 无法证明 manifest 与 segments 属于同一时点。
TMP_DIR="$(make_tmp_dir)"
if ! docker_cp_redis "/data/dump.rdb" "$TMP_DIR/dump.rdb" "dump.rdb" "required"; then
    exit 4
fi
if [ ! -f "$TMP_DIR/dump.rdb" ] || [ -L "$TMP_DIR/dump.rdb" ] \
        || [ ! -s "$TMP_DIR/dump.rdb" ]; then
    log "ERROR: redis dump.rdb is missing, unsafe, or empty"
    exit 4
fi

tar -czf "$REDIS_TMP" -C "$TMP_DIR" dump.rdb
if ! tar -tzf "$REDIS_TMP" >/dev/null; then
    log "ERROR: redis archive invalid, removing"
    rm -f "$REDIS_TMP" "$REDIS_OUT"
    exit 4
fi
REDIS_VALIDATION_DIR="$TMP_DIR/.archive-validation"
if ! python3 "${SCRIPT_DIR}/redis_backup_archive.py" \
        "$REDIS_TMP" "$REDIS_VALIDATION_DIR"; then
    log "ERROR: redis archive content validation failed"
    rm -rf "$REDIS_VALIDATION_DIR"
    exit 4
fi
if ! lumen_validate_redis_rdb_file \
        "$REDIS_CONTAINER" "$REDIS_VALIDATION_DIR/dump.rdb"; then
    log "ERROR: redis-check-rdb rejected the archived dump.rdb"
    rm -rf "$REDIS_VALIDATION_DIR"
    exit 4
fi
rm -rf "$REDIS_VALIDATION_DIR"
if ! backup_fsync_file "$REDIS_TMP"; then
    log "ERROR: failed to fsync redis backup payload"
    exit 4
fi
backup_failpoint after_redis_temp_fsync
mv -f "$REDIS_TMP" "$REDIS_OUT"
REDIS_TMP=""
if ! backup_fsync_directory "$REDIS_DIR"; then
    log "ERROR: failed to fsync redis backup rename"
    exit 4
fi
backup_failpoint after_redis_rename
REDIS_SIZE="$(file_size "$REDIS_OUT")"
log "redis RDB-only pack verified size=$REDIS_SIZE"

if ! pair_hashes="$(
        publish_backup_pair_marker \
            "$PAIR_MARKER" \
            "$BACKUP_ROOT" \
            "$BACKUP_OPERATION_ID" \
            "$TS" \
            "$PG_OUT" \
            "$REDIS_OUT" \
            "$PG_SIZE" \
            "$REDIS_SIZE"
)"; then
    log "ERROR: failed to durably commit postgres/redis backup pair"
    exit 5
fi
PAIR_COMMITTED=1
IFS=$'\t' read -r PG_SHA256 REDIS_SHA256 <<< "$pair_hashes"
if [ -z "$PG_SHA256" ] || [ -z "$REDIS_SHA256" ]; then
    log "ERROR: backup pair marker did not return payload hashes"
    exit 5
fi
backup_failpoint after_pair_marker

# ---- Retention ----
# 严格 YYYYMMDD-HHMMSS timestamp 提取；忽略手工 cp 进来的非时间戳文件（例如
# manual-2024.pg.dump.gz），避免它们干扰排序导致超额删掉真正的 backup。
_extract_ts() {
    local dir="$1"
    local suffix="$2"
    local path
    [ -d "$dir" ] || return 0
    for path in "$dir"/*."$suffix"; do
        [ -f "$path" ] || continue
        printf '%s\n' "${path##*/}"
    done \
        | grep -E "^[0-9]{8}-[0-9]{6}\\.${suffix//\./\\.}$" \
        | sed -E "s/\\.${suffix//\./\\.}$//" \
        | sort -u
}

prune_timestamp() {
    local pg_dir="$1"
    local redis_dir="$2"
    local ts="$3"
    local marker="$BACKUP_ROOT/.backup-pair.$ts.json"

    if [ -e "$marker" ]; then
        rm -f "$marker"
        backup_fsync_directory "$BACKUP_ROOT"
        backup_failpoint after_prune_marker
    fi
    rm -f \
        "$pg_dir/$ts.pg.dump.gz" \
        "$redis_dir/$ts.redis.tgz"
    backup_fsync_directory "$pg_dir"
    backup_fsync_directory "$redis_dir"
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
    local protected_ts="$4"

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
        prune_timestamp "$pg_dir" "$redis_dir" "$ts"
    done <<< "$orphan_pg"
    while IFS= read -r ts; do
        [ -z "$ts" ] && continue
        log "prune orphan Redis (no pg pair): $ts"
        prune_timestamp "$pg_dir" "$redis_dir" "$ts"
    done <<< "$orphan_redis"

    local committed=""
    local ts
    while IFS= read -r ts; do
        [ -z "$ts" ] && continue
        if python3 -I "${SCRIPT_DIR}/restore_journal.py" \
                backup-pair-bind-json "$BACKUP_ROOT" "$ts" \
                >/dev/null 2>&1; then
            committed="${committed}${ts}"$'\n'
        else
            log "retention ignores uncommitted backup pair: $ts"
        fi
    done <<< "$paired"

    local total excess
    total="$(printf '%s' "$committed" | grep -c . || true)"
    if [ "$total" -le "$keep" ]; then
        return 0
    fi
    excess=$((total - keep))
    printf '%s' "$committed" \
        | sort \
        | awk -v protected="$protected_ts" '$0 != protected' \
        | sed -n "1,${excess}p" \
        | while IFS= read -r ts; do
        [ -z "$ts" ] && continue
        log "prune old paired: $ts"
        prune_timestamp "$pg_dir" "$redis_dir" "$ts"
    done
}

if ! record_backup_success; then
    log "ERROR: backup pair exists but last-success marker was not durably recorded"
    exit 70
fi
SUCCESS_RECEIPT_COMMITTED=1
backup_failpoint after_success_receipt
backup_failpoint before_retention
if ! prune_paired "$PG_DIR" "$REDIS_DIR" "$MAX_KEEP" "$TS"; then
    log "WARN: backup pair committed but retention is pending"
fi

emit_backup_result
log "backup $TS complete"
