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

guard_automatic_app_rollback_compatibility() {
    local rollback_release="${1:-}"
    local database_release="${2:-${NEW_RELEASE:-}}"
    local rollback_head=""
    local database_head=""
    local rollback_tag=""
    local database_tag="${TARGET_TAG:-${LUMEN_IMAGE_TAG:-}}"

    if [ "${UPDATE_MIGRATION_STARTED:-0}" -ne 1 ] \
            && [ "${UPDATE_MIGRATION_VERIFIED:-0}" -ne 1 ]; then
        return 0
    fi
    if [ -z "${rollback_release}" ] || [ ! -d "${rollback_release}" ]; then
        log_error "自动应用回滚兼容性无法验证：旧 release 不存在。"
        return 1
    fi
    if [ -z "${database_release}" ] || [ ! -d "${database_release}" ]; then
        log_error "自动应用回滚兼容性无法验证：数据库探测 release 不存在。"
        return 1
    fi
    if [ -f "${rollback_release}/.image-tag" ]; then
        rollback_tag="$(
            head -n1 "${rollback_release}/.image-tag" 2>/dev/null \
                | tr -d '[:space:]'
        )"
    fi
    rollback_tag="${rollback_tag:-${PREVIOUS_TAG:-}}"
    if [ -z "${rollback_tag}" ] || [ -z "${database_tag}" ]; then
        log_error "自动应用回滚兼容性无法验证：rollback_tag=${rollback_tag:-<unknown>} database_tag=${database_tag:-<unknown>}。"
        return 1
    fi

    rollback_head="$(
        LUMEN_IMAGE_TAG="${rollback_tag}" \
            target_alembic_head "${rollback_release}" 2>/dev/null || true
    )"
    database_head="$(
        LUMEN_IMAGE_TAG="${database_tag}" \
            current_alembic_revision "${database_release}" 2>/dev/null || true
    )"
    if [ -z "${rollback_head}" ] || [ -z "${database_head}" ]; then
        log_error "自动应用回滚兼容性无法验证：rollback_head=${rollback_head:-<unknown>} database_head=${database_head:-<unknown>}。"
        return 1
    fi
    if [ -n "${UPDATE_MIGRATION_HEAD:-}" ] \
            && [ "${database_head}" != "${UPDATE_MIGRATION_HEAD}" ]; then
        log_error "自动应用回滚兼容性无法验证：数据库 revision ${database_head} 与已验证 migration head ${UPDATE_MIGRATION_HEAD} 不一致。"
        return 1
    fi
    if [ "${rollback_head}" != "${database_head}" ]; then
        log_error "拒绝自动应用回滚：旧应用 Alembic head=${rollback_head}，当前数据库 revision=${database_head}。"
        log_error "数据库不会自动 downgrade；请保持新版本并执行前向恢复，或使用已验证恢复点进行人工整库回滚。"
        return 1
    fi
    return 0
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
