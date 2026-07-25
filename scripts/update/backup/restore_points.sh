#!/usr/bin/env bash
# Backup restore-point creation, verification, and migration boundary helpers.

snapshot_update_backup_files() {
    local backup_root="$1"
    local output_file="$2"
    python3 - "${backup_root}" "${output_file}" <<'PY'
import json
import os
import re
import stat
import sys
from pathlib import Path

backup_root = Path(sys.argv[1])
output_path = Path(sys.argv[2])
signatures = {}
for directory, pattern in (
    (backup_root / "pg", re.compile(r"[0-9]{8}-[0-9]{6}\.pg\.dump\.gz")),
    (backup_root / "redis", re.compile(r"[0-9]{8}-[0-9]{6}\.redis\.tgz")),
):
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        continue
    except OSError:
        raise SystemExit(1)
    for path in entries:
        if not pattern.fullmatch(path.name):
            continue
        try:
            info = os.lstat(path)
        except OSError:
            raise SystemExit(1)
        if not stat.S_ISREG(info.st_mode):
            continue
        signatures[str(path.relative_to(backup_root))] = [
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ]

try:
    output_path.write_text(
        json.dumps(signatures, sort_keys=True), encoding="utf-8"
    )
except OSError:
    raise SystemExit(1)
PY
}

verify_update_restore_point() {
    local output_file="$1"
    local backup_root="$2"
    local started_epoch="$3"
    local baseline_file="$4"
    local fields=""
    if ! fields="$(python3 - \
            "${output_file}" \
            "${backup_root}" \
            "${started_epoch}" \
            "${baseline_file}" <<'PY'
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

output_path = Path(sys.argv[1])
backup_root = Path(sys.argv[2])
try:
    started_epoch = int(sys.argv[3])
    lines = output_path.read_text(encoding="utf-8").splitlines()
    baseline = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
if not isinstance(baseline, dict):
    raise SystemExit(1)

payload = None
for line in reversed(lines):
    try:
        candidate = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(candidate, dict) and {
        "timestamp",
        "pg_size",
        "redis_size",
    }.issubset(candidate):
        payload = candidate
        break
if payload is None:
    raise SystemExit(1)

timestamp = payload.get("timestamp")
pg_size = payload.get("pg_size")
redis_size = payload.get("redis_size")
if not isinstance(timestamp, str) or not re.fullmatch(
    r"[0-9]{8}-[0-9]{6}", timestamp
):
    raise SystemExit(1)
for size in (pg_size, redis_size):
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise SystemExit(1)

try:
    timestamp_epoch = datetime.strptime(
        timestamp, "%Y%m%d-%H%M%S"
    ).replace(tzinfo=timezone.utc).timestamp()
except ValueError:
    raise SystemExit(1)
if timestamp_epoch < started_epoch - 1 or timestamp_epoch > time.time() + 5:
    raise SystemExit(1)

paths = (
    (backup_root / "pg" / f"{timestamp}.pg.dump.gz", pg_size),
    (backup_root / "redis" / f"{timestamp}.redis.tgz", redis_size),
)
for path, expected_size in paths:
    try:
        info = os.lstat(path)
    except OSError:
        raise SystemExit(1)
    if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
        raise SystemExit(1)
    signature = [
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ]
    relative = str(path.relative_to(backup_root))
    if baseline.get(relative) == signature:
        raise SystemExit(1)

values = (
    timestamp,
    str(paths[0][0]),
    str(paths[1][0]),
    str(pg_size),
    str(redis_size),
)
if any("\t" in value or "\n" in value for value in values):
    raise SystemExit(1)
print("\t".join(values))
PY
    )"; then
        return 1
    fi

    local timestamp=""
    local pg_path=""
    local redis_path=""
    local pg_size=""
    local redis_size=""
    IFS=$'\t' read -r timestamp pg_path redis_path pg_size redis_size <<< "${fields}"
    if [ -z "${timestamp}" ] || [ -z "${pg_path}" ] || [ -z "${redis_path}" ]; then
        return 1
    fi
    UPDATE_RESTORE_POINT_TIMESTAMP="${timestamp}"
    UPDATE_RESTORE_POINT_PG="${pg_path}"
    UPDATE_RESTORE_POINT_REDIS="${redis_path}"
    UPDATE_RESTORE_POINT_PG_SIZE="${pg_size}"
    UPDATE_RESTORE_POINT_REDIS_SIZE="${redis_size}"
    return 0
}

run_update_backup_preflight() {
    local backup_script=""
    if [ -x "${SCRIPT_DIR}/backup.sh" ]; then
        backup_script="${SCRIPT_DIR}/backup.sh"
    elif [ -n "${CURRENT_RELEASE}" ] \
            && [ -x "${CURRENT_RELEASE}/scripts/backup.sh" ]; then
        backup_script="${CURRENT_RELEASE}/scripts/backup.sh"
    fi
    if [ -z "${backup_script}" ]; then
        log_error "[backup_preflight] 找不到 backup.sh，无法生成本轮恢复点。"
        return 1
    fi

    local output_file=""
    local baseline_file=""
    mkdir -p "${UPDATE_LOG_DIR}"
    output_file="$(
        mktemp "${UPDATE_LOG_DIR}/.update-backup.${OPERATION_ID}.XXXXXX" 2>/dev/null
    )" || {
        log_error "[backup_preflight] 无法创建备份校验输出文件。"
        return 1
    }
    baseline_file="$(
        mktemp "${UPDATE_LOG_DIR}/.update-backup-baseline.${OPERATION_ID}.XXXXXX" \
            2>/dev/null
    )" || {
        rm -f "${output_file}" 2>/dev/null || true
        log_error "[backup_preflight] 无法创建备份基线文件。"
        return 1
    }
    chmod 0600 "${output_file}" "${baseline_file}" 2>/dev/null || {
        rm -f "${output_file}" "${baseline_file}" 2>/dev/null || true
        log_error "[backup_preflight] 无法收紧备份校验输出文件权限。"
        return 1
    }
    if ! snapshot_update_backup_files "${UPDATE_LOG_DIR}" "${baseline_file}"; then
        rm -f "${output_file}" "${baseline_file}" 2>/dev/null || true
        log_error "[backup_preflight] 无法记录备份前文件签名基线。"
        return 1
    fi

    local backup_started_epoch=""
    local backup_rc=1
    local tee_rc=1
    local pipe_status=()
    backup_started_epoch="$(date +%s)"
    log_info "[backup_preflight] 调用 ${backup_script}（BACKUP_ROOT=${UPDATE_LOG_DIR}）"
    # LUMEN_BACKUP_FORCE=1：调用方已持有同一把维护锁。
    set +e
    LUMEN_ENV_FILE="${SHARED_ENV}" \
        LUMEN_BACKUP_ROOT="${UPDATE_LOG_DIR}" \
        BACKUP_ROOT="${UPDATE_LOG_DIR}" \
        LUMEN_BACKUP_FORCE=1 \
        DB_USER="$(lumen_env_value DB_USER "${SHARED_ENV}")" \
        DB_NAME="$(lumen_env_value DB_NAME "${SHARED_ENV}")" \
        REDIS_PASSWORD="$(lumen_env_value REDIS_PASSWORD "${SHARED_ENV}")" \
        bash "${backup_script}" | tee "${output_file}"
    pipe_status=("${PIPESTATUS[@]}")
    set -e
    backup_rc="${pipe_status[0]:-1}"
    tee_rc="${pipe_status[1]:-1}"
    if [ "${backup_rc}" -ne 0 ] || [ "${tee_rc}" -ne 0 ]; then
        rm -f "${output_file}" "${baseline_file}" 2>/dev/null || true
        log_error "[backup_preflight] 备份失败（backup_rc=${backup_rc}, tee_rc=${tee_rc}），拒绝继续。"
        log_error "[backup_preflight] 已使用 env 文件：${SHARED_ENV}"
        log_error "[backup_preflight] 请查看上方 backup 日志中的 pg_dump/redis 具体错误。"
        return 1
    fi
    if ! verify_update_restore_point \
            "${output_file}" \
            "${UPDATE_LOG_DIR}" \
            "${backup_started_epoch}" \
            "${baseline_file}"; then
        rm -f "${output_file}" "${baseline_file}" 2>/dev/null || true
        log_error "[backup_preflight] backup.sh 返回成功，但未找到本轮新生成且大小匹配的 PG/Redis 成对恢复点。"
        log_error "[backup_preflight] 拒绝把人工预备份或旧文件当成本轮迁移恢复边界。"
        return 1
    fi
    rm -f "${output_file}" "${baseline_file}" 2>/dev/null || true

    log_info "[backup_preflight] 本轮恢复点已验证：timestamp=${UPDATE_RESTORE_POINT_TIMESTAMP}"
    log_info "  PostgreSQL: ${UPDATE_RESTORE_POINT_PG} (${UPDATE_RESTORE_POINT_PG_SIZE} bytes)"
    log_info "  Redis:      ${UPDATE_RESTORE_POINT_REDIS} (${UPDATE_RESTORE_POINT_REDIS_SIZE} bytes)"
    emit_info backup_preflight backup_script "${backup_script}"
    emit_info backup_preflight restore_point "${UPDATE_RESTORE_POINT_TIMESTAMP}"
    emit_info backup_preflight pg_path "${UPDATE_RESTORE_POINT_PG}"
    emit_info backup_preflight redis_path "${UPDATE_RESTORE_POINT_REDIS}"
    return 0
}

guard_migration_restore_point() {
    if [ -n "${UPDATE_RESTORE_POINT_TIMESTAMP}" ]; then
        log_info "[migrate_db] 使用本轮已验证恢复点 ${UPDATE_RESTORE_POINT_TIMESTAMP} 作为数据库回滚边界。"
        emit_info migrate_db restore_point "${UPDATE_RESTORE_POINT_TIMESTAMP}"
        emit_info migrate_db restore_point_pg "${UPDATE_RESTORE_POINT_PG}"
        emit_info migrate_db restore_point_redis "${UPDATE_RESTORE_POINT_REDIS}"
        return 0
    fi
    if update_requires_migration_restore_point; then
        log_error "[migrate_db] 后台/受保护更新缺少本轮可验证恢复点；拒绝停止旧服务或执行 Alembic。"
        emit_warn migrate_db "missing_required_restore_point"
        return 1
    fi
    log_warn "[migrate_db] 本轮没有可验证恢复点；按显式 fast/skip 语义继续。"
    log_warn "[migrate_db] 若迁移已启动，应用 release 回滚不会回滚数据库。"
    emit_warn migrate_db "missing_restore_point_explicit_override"
    return 0
}

log_update_restore_boundary() {
    local phase="${1:-update}"
    if [ "${UPDATE_RESTORE_BOUNDARY_LOGGED}" -eq 1 ]; then
        return 0
    fi
    UPDATE_RESTORE_BOUNDARY_LOGGED=1
    if [ -n "${UPDATE_RESTORE_POINT_TIMESTAMP}" ]; then
        log_error "[${phase}] 本轮恢复点：timestamp=${UPDATE_RESTORE_POINT_TIMESTAMP}"
        log_error "[${phase}]   PostgreSQL=${UPDATE_RESTORE_POINT_PG}"
        log_error "[${phase}]   Redis=${UPDATE_RESTORE_POINT_REDIS}"
    else
        log_error "[${phase}] 本轮恢复点：<none>（显式 fast/skip override）。"
    fi
    if [ "${UPDATE_MIGRATION_VERIFIED}" -eq 1 ]; then
        log_error "[${phase}] 回滚边界：数据库已迁移并验证到目标 head；自动 release/env/服务回滚不会回滚数据库。"
        log_error "[${phase}] 如需数据库回退，必须使用上述恢复点走受控 restore，不能只切换 current。"
    elif [ "${UPDATE_MIGRATION_STARTED}" -eq 1 ]; then
        log_error "[${phase}] 回滚边界：Alembic 已启动但未验证到目标 head，数据库可能已部分变更。"
        log_error "[${phase}] 自动回滚仅覆盖 release/env/服务；数据库恢复必须使用上述恢复点。"
    else
        log_warn "[${phase}] 回滚边界：Alembic 尚未启动，数据库未被本轮迁移修改。"
    fi
}
