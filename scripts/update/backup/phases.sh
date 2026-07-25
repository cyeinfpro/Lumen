#!/usr/bin/env bash
# Storage, infrastructure, and database migration phases.

# Phase: check_storage
update_phase_check_storage() {
emit_start check_storage
if [ "${SKIP_STORAGE_CHECK:-0}" = "1" ]; then
    emit_info check_storage status "skipped_via_env"
    emit_done check_storage 0
else
    _storage_target="${LUMEN_DATA_ROOT:-/opt/lumendata}"
    if ! findmnt -T "${_storage_target}" >/dev/null 2>&1; then
        log_error "[check_storage] ${_storage_target} 未挂载。"
        log_error "  在管理后台「存储后端」页面配置 local 或 smb 后即可生效；"
        log_error "  紧急绕过：SKIP_STORAGE_CHECK=1 ./update.sh"
        emit_fail check_storage 1
        exit 1
    fi
    _storage_probe="${_storage_target}/.update_probe_$$"
    if ! touch "${_storage_probe}" 2>/dev/null; then
        log_error "[check_storage] ${_storage_target} 不可写（host 端挂载源可能不可达）。"
        emit_fail check_storage 1
        exit 1
    fi
    rm -f "${_storage_probe}"
    _storage_fstype="$(findmnt -T "${_storage_target}" -no FSTYPE 2>/dev/null || true)"
    emit_info check_storage target "${_storage_target}"
    emit_info check_storage fstype "${_storage_fstype:-unknown}"
    emit_done check_storage 0
fi
}

# Phase: start_infra
update_phase_start_infra() {
emit_start start_infra

# self-heal: 如果历史上有人手工 `cd current && docker compose up` 起过容器
# (project=current 而非 lumen)，先 down 掉，避免新 project=lumen 撞容器名。
# idempotent — 无 stale 直接返回。
lumen_compose_project_unify

# v1.0.48 镜像切到 pgvector/pgvector:pg16, postgres uid 70 → 999.
# 老老 install 留下的 alpine 数据目录 uid=70, 必须先 chown 否则 PG 起不来.
if ! migrate_postgres_uid; then
    log_error "[start_infra] postgres 数据目录 chown 999 失败,中止升级."
    emit_fail start_infra 1
    exit 1
fi

_infra_healthy=0
if [ "${LUMEN_UPDATE_MODE}" = "fast" ] \
        && LUMEN_HEALTH_COMPOSE_ATTEMPTS="${LUMEN_UPDATE_FAST_HEALTH_ATTEMPTS:-1}" \
           LUMEN_HEALTH_COMPOSE_INTERVAL="${LUMEN_UPDATE_FAST_HEALTH_INTERVAL:-1}" \
           lumen_health_compose postgres redis >/dev/null 2>&1; then
    _infra_healthy=1
fi

if [ "${LUMEN_UPDATE_MODE}" = "fast" ] && [ "${_infra_healthy}" = "1" ]; then
    log_info "[start_infra] fast 模式：postgres/redis 已 healthy，复用 postgres。"
    emit_info start_infra postgres "reuse_healthy"
    # redis 挂载 release 内的 entrypoint 脚本；如果跨 release 复用旧容器，
    # cleanup 删除旧 release 后 docker cp/重启会被坏 bind mount 卡住。
    # 因此 fast 模式也重建 redis，保留 /data bind mount，不丢数据。
    if ! lumen_compose_in "${NEW_RELEASE}" up --pull missing -d --wait --force-recreate redis; then
        log_error "[start_infra] redis 重建或健康检查失败。"
        log_error "  当前 API/Worker/Web 服务保持不变。"
        emit_fail start_infra 1
        exit 1
    fi
    emit_info start_infra redis "recreated_for_release_bind_mount"
else
    # standard 模式保留 force-recreate；fast 模式只在 infra 不健康/缺失时启动。
    if [ "${LUMEN_UPDATE_MODE}" = "fast" ]; then
        if ! lumen_compose_in "${NEW_RELEASE}" up --pull missing -d --wait postgres redis; then
            log_error "[start_infra] postgres / redis 启动或健康检查失败。"
            log_error "  当前 API/Worker/Web 服务保持不变。"
            emit_fail start_infra 1
            exit 1
        fi
    elif ! lumen_compose_in "${NEW_RELEASE}" up --pull missing -d --wait --force-recreate postgres redis; then
        log_error "[start_infra] postgres / redis 启动或健康检查失败。"
        log_error "  当前 API/Worker/Web 服务保持不变。"
        emit_fail start_infra 1
        exit 1
    fi
fi
emit_done start_infra 0
}

# Phase: migrate_db
update_phase_migrate_db() {
emit_start migrate_db

MIGRATE_NEEDED=1
if [ "${LUMEN_UPDATE_MODE}" = "fast" ]; then
    _alembic_heads_pre="$(target_alembic_head "${NEW_RELEASE}" 2>/dev/null || true)"
    _alembic_current_pre="$(current_alembic_revision "${NEW_RELEASE}" 2>/dev/null || true)"
    if [ -n "${_alembic_heads_pre}" ] && [ "${_alembic_current_pre}" = "${_alembic_heads_pre}" ]; then
        MIGRATE_NEEDED=0
        log_info "[migrate_db] fast 模式：DB 已在目标 head=${_alembic_heads_pre}，跳过 stop api/worker 与 alembic upgrade。"
        emit_info migrate_db action "skip_already_at_head"
        emit_info migrate_db head "${_alembic_heads_pre}"
        emit_done migrate_db 0
    else
        emit_info migrate_db current "${_alembic_current_pre:-<unknown>}"
        emit_info migrate_db head "${_alembic_heads_pre:-<unknown>}"
    fi
fi

_stopped_old_services=0
if [ "${MIGRATE_NEEDED}" = "0" ]; then
    :
elif ! guard_migration_restore_point; then
    log_update_restore_boundary migrate_db
    emit_fail migrate_db 1
    exit 1
elif [ "${LUMEN_UPDATE_BLUE_GREEN:-0}" = "1" ]; then
    log_info "[migrate_db] LUMEN_UPDATE_BLUE_GREEN=1：保持旧 api/worker 运行，依赖 expand-then-contract 迁移闸门。"
    emit_info migrate_db old_services "kept_running_blue_green"
else
    log_info "[migrate_db] stop api/worker/tgbot 让出活跃事务,避免 schema lock 死锁"
    emit_info migrate_db stop_timeout "${LUMEN_UPDATE_STOP_TIMEOUT:-30}"
    # stop 失败 (容器本来没起 / 无该 service 之类) 不阻塞 migrate.
    lumen_compose_in "${NEW_RELEASE}" stop -t "${LUMEN_UPDATE_STOP_TIMEOUT:-30}" api worker tgbot >/dev/null 2>&1 || true
    _stopped_old_services=1
    UPDATE_OLD_SERVICES_STOPPED=1
fi

_migrate_run_failed=0
if [ "${MIGRATE_NEEDED}" = "1" ]; then
    UPDATE_MIGRATION_STARTED=1
    if ! lumen_compose_in "${NEW_RELEASE}" --profile migrate run --rm migrate; then
        _migrate_run_failed=1
    fi
fi

# Verify alembic 真到 head — 已观察到 alembic upgrade 在某些情况下 silent
# exit=0 但 transaction rollback（lock_timeout / FK 验证 abort 时异常被
# SA 内部吞掉）。仅看 docker compose run 的 exit code 不可靠：必须二次
# query alembic_version 与 heads 比对。否则 update.sh 误以为 success 后切
# current → api 用新代码查旧 schema → 全站 500（v1.1.0 prod 已踩过）。
if [ "${MIGRATE_NEEDED}" = "1" ]; then
    _alembic_heads="$(target_alembic_head "${NEW_RELEASE}" 2>/dev/null || true)"
    _alembic_current="$(current_alembic_revision "${NEW_RELEASE}" 2>/dev/null || true)"
else
    _alembic_heads="${_alembic_heads_pre:-}"
    _alembic_current="${_alembic_current_pre:-}"
fi

if [ "${MIGRATE_NEEDED}" = "1" ] && { [ "${_migrate_run_failed}" = "1" ] \
        || [ -z "${_alembic_heads}" ] \
        || [ "${_alembic_current}" != "${_alembic_heads}" ]; }; then
    log_error "[migrate_db] alembic upgrade 失败或未真正落地 → fail-fast。"
    log_error "  observed alembic current=${_alembic_current:-<空>}"
    log_error "  expected head=${_alembic_heads:-<空>}"
    log_error "  原始 docker compose run rc：${_migrate_run_failed}（0=看起来 success，但 verify 不通过仍 fail-fast）"
    log_error "  根据 §11.3 / §17.6：不切 current、不重启新版本业务容器。"
    log_update_restore_boundary migrate_db
    # 关键修复：如果之前 stop 了旧 api/worker/tgbot，migrate 失败后必须把它们用旧
    # release 起回来，否则业务停摆 — 旧 schema 与旧代码兼容，仍可正常服务。
    if [ "${_stopped_old_services}" = "1" ] && [ -n "${CURRENT_ID:-}" ] && [ -d "${ROOT}/releases/${CURRENT_ID}" ]; then
        log_warn "[migrate_db] 用旧 release ${CURRENT_ID} 重启 api/worker，让业务恢复旧 schema 服务..."
        # 旧 release 的 compose 文件指向旧镜像 tag（PREVIOUS_TAG），SHARED_ENV
        # 还没被 set_image_tag 改写之前已经被改过；如果已改，先恢复成旧 tag。
        if [ -n "${PREVIOUS_TAG:-}" ] && [ -n "${TARGET_TAG:-}" ] && [ "${PREVIOUS_TAG}" != "${TARGET_TAG}" ]; then
            lumen_set_image_tag_in_env "${SHARED_ENV}" "${PREVIOUS_TAG}" 2>/dev/null \
                || log_warn "  恢复 SHARED_ENV 到 ${PREVIOUS_TAG} 失败，旧服务可能拉错镜像 tag。"
        fi
        if lumen_compose_in "${ROOT}/releases/${CURRENT_ID}" up --pull missing -d worker api 2>/dev/null; then
            log_info "[migrate_db] 旧服务 (${CURRENT_ID}) 已重启，业务可用旧 schema 继续。"
            UPDATE_OLD_SERVICES_STOPPED=0
        else
            log_error "[migrate_db] 旧服务重启失败！业务此时停摆，请人工处理："
            log_error "    cd ${ROOT}/releases/${CURRENT_ID}"
            log_error "    COMPOSE_PROJECT_NAME=lumen docker compose up -d worker api"
        fi
    elif [ "${_stopped_old_services}" = "1" ]; then
        log_error "[migrate_db] 无可用的旧 release（CURRENT_ID=${CURRENT_ID:-<none>}），业务停摆。"
    else
        log_warn "[migrate_db] 蓝绿模式下旧服务未停止；保持 current 不切换，业务继续由旧版本服务。"
    fi
    log_error "  请人工查 migrate 日志：docker compose logs --tail=120 migrate"
    emit_fail migrate_db 1
    exit 1
fi
if [ "${MIGRATE_NEEDED}" = "1" ]; then
    UPDATE_MIGRATION_VERIFIED=1
    emit_done migrate_db 0
fi
}
