#!/usr/bin/env bash
# Alembic revision and host data migration helpers.

alembic_revision_from_output() {
    awk 'NF && !/^INFO/ {print $1; exit}'
}

target_alembic_head() {
    local compose_dir="$1"
    lumen_compose_in "${compose_dir}" --profile migrate run --rm migrate alembic heads 2>/dev/null \
        | alembic_revision_from_output
}

current_alembic_revision() {
    local compose_dir="$1"
    lumen_compose_in "${compose_dir}" --profile migrate run --rm migrate alembic current 2>/dev/null \
        | alembic_revision_from_output
}

disk_free_gb_opt() {
    local out
    if command -v df >/dev/null 2>&1; then
        # df -P -k 输出 1024-blocks，第 4 列是 available
        out="$(df -P -k /opt 2>/dev/null | awk 'NR==2 {print int($4/1024/1024)}')"
        if [ -n "${out}" ]; then
            printf '%s' "${out}"
            return 0
        fi
    fi
    printf '%s' "-1"
}

# 校验数据目录属主：postgres=999, redis=999, storage/backup 对齐应用 storage gid。
# v1.0.48 起 postgres 容器换到 pgvector/pgvector:pg16（Debian, uid=999）。
# 老老 alpine 镜像 postgres uid=70 的数据目录会在 migrate_postgres_uid 阶段
# 一次性 chown 70 → 999；这里仍仅 warn 兜底。
check_data_owners() {
    local missing=0
    local path
    for path in \
        "${LUMEN_DB_ROOT}/postgres" \
        "${LUMEN_DB_ROOT}/redis" \
        "${LUMEN_DATA_ROOT}/storage" \
        "${LUMEN_DATA_ROOT}/backup"; do
        if [ ! -d "${path}" ]; then
            log_error "缺少数据目录：${path}"
            missing=1
        fi
    done
    if [ "${missing}" -eq 1 ]; then
        return 1
    fi
    # 仅做 warn，不阻断（install.sh 是 single source of truth）
    local uid gid
    if command -v stat >/dev/null 2>&1; then
        uid="$(stat -c '%u' "${LUMEN_DB_ROOT}/postgres" 2>/dev/null || stat -f '%u' "${LUMEN_DB_ROOT}/postgres" 2>/dev/null || echo "")"
        [ -n "${uid}" ] && [ "${uid}" != "${LUMEN_POSTGRES_UID}" ] && log_warn "${LUMEN_DB_ROOT}/postgres 属主非 ${LUMEN_POSTGRES_UID}（实际 ${uid}），postgres 容器可能起不来。"
        uid="$(stat -c '%u' "${LUMEN_DB_ROOT}/redis" 2>/dev/null || stat -f '%u' "${LUMEN_DB_ROOT}/redis" 2>/dev/null || echo "")"
        [ -n "${uid}" ] && [ "${uid}" != "${LUMEN_REDIS_UID}" ] && log_warn "${LUMEN_DB_ROOT}/redis 属主非 ${LUMEN_REDIS_UID}（实际 ${uid}），redis 容器可能起不来。"
        gid="$(stat -c '%g' "${LUMEN_DATA_ROOT}/storage" 2>/dev/null || stat -f '%g' "${LUMEN_DATA_ROOT}/storage" 2>/dev/null || echo "")"
        [ -n "${gid}" ] && [ "${gid}" != "${LUMEN_APP_STORAGE_GID}" ] && log_warn "${LUMEN_DATA_ROOT}/storage 属组非 ${LUMEN_APP_STORAGE_GID}（实际 ${gid}），api/worker 可能写不进去。"
        gid="$(stat -c '%g' "${LUMEN_DATA_ROOT}/backup" 2>/dev/null || stat -f '%g' "${LUMEN_DATA_ROOT}/backup" 2>/dev/null || echo "")"
        [ -n "${gid}" ] && [ "${gid}" != "${LUMEN_APP_STORAGE_GID}" ] && log_warn "${LUMEN_DATA_ROOT}/backup 属组非 ${LUMEN_APP_STORAGE_GID}（实际 ${gid}），备份/日志可能写不进去。"
    fi
    return 0
}

# v1.0.48: postgres 镜像从 alpine (uid=70) 切到 pgvector/pgvector:pg16 (默认 uid=999).
# 老 install 在数据目录写过 owner=70 的文件,新容器 uid 启动会 EACCES.
# 这个 helper 检测属主, 仅在 ≠ 目标 uid 时 chown 一次, idempotent.
migrate_postgres_uid() {
    local pg_dir="${LUMEN_DB_ROOT}/postgres"
    if [ ! -d "${pg_dir}" ]; then
        return 0
    fi
    local current_uid=""
    if command -v stat >/dev/null 2>&1; then
        current_uid="$(stat -c '%u' "${pg_dir}" 2>/dev/null || stat -f '%u' "${pg_dir}" 2>/dev/null || echo "")"
    fi
    if [ -z "${current_uid}" ] || [ "${current_uid}" = "${LUMEN_POSTGRES_UID}" ]; then
        return 0
    fi
    log_info "[migrate_postgres_uid] ${pg_dir} 属主 ${current_uid} → ${LUMEN_POSTGRES_UID}:${LUMEN_POSTGRES_GID} (postgres image uid/gid)"
    if lumen_run_as_root chown -R "${LUMEN_POSTGRES_UID}:${LUMEN_POSTGRES_GID}" "${pg_dir}"; then
        log_info "[migrate_postgres_uid] chown 完成"
        return 0
    fi
    log_error "[migrate_postgres_uid] chown 失败,postgres 容器可能起不来"
    return 1
}

# rsync 仓库内容到 release 目录；与 install 的发布物布局对齐。
# 排除 .git / node_modules / .venv / .next 等大目录。
