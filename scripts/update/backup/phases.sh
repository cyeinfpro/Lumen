#!/usr/bin/env bash
# Storage, infrastructure, fast-anchor, and database migration phases.

_lumen_fast_sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" 2>/dev/null | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 "$1" 2>/dev/null | awk '{print $NF}'
    fi
}

lumen_update_write_fast_anchor() {
    local out_dir="${UPDATE_LOG_DIR:-}" anchor_file="${LUMEN_FAST_ANCHOR_FILE:-}"
    [ -n "${anchor_file}" ] || [ -n "${out_dir}" ] || return 0
    [ -n "${anchor_file}" ] || anchor_file="${out_dir}/fast-anchor.json"
    mkdir -p "$(dirname "${anchor_file}")" 2>/dev/null || return 0
    local env_sha="" db_user="${DB_USER:-}" db_name="${DB_NAME:-}" pg_c="${PG_CONTAINER:-lumen-pg}"
    if [ -n "${SHARED_ENV:-}" ] && [ -f "${SHARED_ENV}" ]; then
        env_sha="$(_lumen_fast_sha256_file "${SHARED_ENV}" || true)"
        [ -n "${db_user}" ] || db_user="$(awk -F= '/^DB_USER=/{gsub(/^["'\'']|["'\'']$/,"",$2); print $2; exit}' "${SHARED_ENV}" 2>/dev/null || true)"
        [ -n "${db_name}" ] || db_name="$(awk -F= '/^DB_NAME=/{gsub(/^["'\'']|["'\'']$/,"",$2); print $2; exit}' "${SHARED_ENV}" 2>/dev/null || true)"
    fi
    db_user="${db_user:-lumen}"; db_name="${db_name:-lumen}"
    local raw_rows="" reachable="false" sql=""
    if [ "${LUMEN_FAST_ANCHOR_SKIP_DOCKER:-0}" != "1" ] && command -v docker >/dev/null 2>&1; then
        sql="SET LOCAL lock_timeout = '200ms'; SET LOCAL statement_timeout = '500ms'; SELECT 'A', version_num, '', '' FROM alembic_version LIMIT 1; SELECT 'S', key, coalesce(value, ''), encode(sha256(convert_to(coalesce(value, ''), 'UTF8')), 'hex') FROM system_settings ORDER BY key;"
        if command -v timeout >/dev/null 2>&1 && timeout 1 true >/dev/null 2>&1; then
            raw_rows="$(timeout "${LUMEN_FAST_ANCHOR_TIMEOUT_SEC:-1}" docker exec -e PGCONNECT_TIMEOUT=1 "${pg_c}" psql -q -U "${db_user}" -d "${db_name}" -AtF $'\t' -c "${sql}" 2>/dev/null)" && reachable="true" || true
        elif declare -F lumen_docker >/dev/null 2>&1; then
            raw_rows="$(lumen_docker exec -e PGCONNECT_TIMEOUT=1 "${pg_c}" psql -q -U "${db_user}" -d "${db_name}" -AtF $'\t' -c "${sql}" 2>/dev/null)" && reachable="true" || true
        else
            raw_rows="$(docker exec -e PGCONNECT_TIMEOUT=1 "${pg_c}" psql -q -U "${db_user}" -d "${db_name}" -AtF $'\t' -c "${sql}" 2>/dev/null)" && reachable="true" || true
        fi
    fi
    local ts_iso tmp_file="${anchor_file}.tmp.$$"
    ts_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo 1970-01-01T00:00:00Z)"
    printf '%s\n' "${raw_rows}" | awk -F'\t' \
        -v op="${OPERATION_ID:-}" -v env_sha="${env_sha}" -v reach="${reachable}" \
        -v hint="${LUMEN_FAST_ANCHOR_ALEMBIC_HINT:-}" -v ts="${ts_iso}" '
    function jesc(s,    r) { r = s; gsub(/\\/, "\\\\", r); gsub(/"/, "\\\"", r); gsub(/\r/, "\\r", r); gsub(/\n/, "\\n", r); gsub(/\t/, "\\t", r); return r }
    function hhex(s,    i, n, h, c) { h = 2166136261; n = length(s); for (i = 1; i <= n; i++) { c = index(" !\"#$%&'\''()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~", substr(s, i, 1)); h = ((h + c) * 16777619) % 4294967296 }; return sprintf("%08x%08x", h, (n * 2654435761) % 4294967296) }
    BEGIN { rev = hint; cnt = 0 }
    $1 == "A" && $2 != "" { rev = $2 }
    $1 == "S" && $2 != "" {
        k = $2; v = $3; vh = ($4 != "" ? substr($4, 1, 16) : hhex(v)); kl = tolower(k)
        red = (kl ~ /(secret|password|token|key|provider|proxy|credential|dsn)/ || length(v) > 64) ? 1 : 0
        if (!(k in seen)) { seen[k] = 1; keys[++cnt] = k }
        red_m[k] = red; hash_m[k] = vh; prev_m[k] = (red ? "[REDACTED]" : v)
    }
    END {
        for (i = 2; i <= cnt; i++) { t = keys[i]; j = i - 1; while (j >= 1 && keys[j] > t) { keys[j + 1] = keys[j]; j-- }; keys[j + 1] = t }
        op_j = (op != "" ? "\"" jesc(op) "\"" : "null"); env_j = (env_sha != "" ? "\"" jesc(env_sha) "\"" : "null"); rev_j = (rev != "" ? "\"" jesc(rev) "\"" : "null")
        klist = ""; items = ""; dig_in = ""
        for (i = 1; i <= cnt; i++) {
            k = keys[i]; klist = klist (i > 1 ? "," : "") "\"" jesc(k) "\""
            items = items (i > 1 ? "," : "") "\"" jesc(k) "\":{\"preview\":\"" jesc(prev_m[k]) "\",\"redacted\":" (red_m[k] ? "true" : "false") ",\"value_sha256\":\"" jesc(hash_m[k]) "\"}"
            dig_in = dig_in k ":" hash_m[k] "\n"
        }
        sdig = (cnt > 0 ? "\"" hhex(dig_in) "\"" : "null")
        printf "{\"alembic_current\":%s,\"container_reachable\":%s,\"env_sha256\":%s,\"mode\":\"fast\",\"operation_id\":%s,\"recorded_at\":\"%s\",\"schema\":1,\"system_settings\":{\"count\":%d,\"digest_sha256\":%s,\"items\":{%s},\"keys\":[%s]}}\n", rev_j, reach, env_j, op_j, jesc(ts), cnt, sdig, items, klist
    }' > "${tmp_file}" 2>/dev/null || { rm -f "${tmp_file}" 2>/dev/null || true; return 0; }
    chmod 600 "${tmp_file}" 2>/dev/null || true
    mv -f "${tmp_file}" "${anchor_file}" 2>/dev/null || rm -f "${tmp_file}" 2>/dev/null || true
}

if declare -F update_phase_backup_preflight >/dev/null 2>&1 \
        && ! declare -F _lumen_base_update_phase_backup_preflight >/dev/null 2>&1; then
    eval "_lumen_base_$(declare -f update_phase_backup_preflight)"
    update_phase_backup_preflight() {
        if [ "${LUMEN_UPDATE_MODE:-}" = "fast" ] \
                && ! lumen_env_truthy "${LUMEN_UPDATE_SKIP_BACKUP:-0}" \
                && ! lumen_env_truthy "${LUMEN_UPDATE_FAST_BACKUP:-0}" \
                && ! update_requires_migration_restore_point; then
            lumen_update_write_fast_anchor || true
        fi
        _lumen_base_update_phase_backup_preflight "$@"
    }
fi

lumen_update_running_writer_services() {
    local entry="" service="" container="" running_ids="" running=""
    for entry in api:lumen-api worker:lumen-worker tgbot:lumen-tgbot; do
        service="${entry%%:*}"; container="${entry#*:}"
        running_ids="$(lumen_docker ps --quiet --filter "name=^/${container}$" 2>/dev/null)" || return 1
        [ -z "${running_ids}" ] || running="${running}${running:+ }${service}"
    done
    printf '%s\n' "${running}"
}

lumen_update_record_original_tgbot_state() {
    local tgbot_active="$1" state_file="" state_tmp=""
    case "${tgbot_active}" in
        0|1) ;;
        *) log_error "无法记录非法的更新前 tgbot 状态：${tgbot_active:-<empty>}。"; return 1 ;;
    esac
    if [ "${UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN:-0}" -eq 1 ]; then
        [ "${UPDATE_ORIGINAL_TGBOT_ACTIVE:-}" = "${tgbot_active}" ] && return 0
        log_error "更新前 tgbot 状态与已绑定状态冲突，拒绝覆盖。"; return 1
    fi
    state_file="$(lumen_update_original_service_state_file)" || {
        log_error "shared env 快照路径未知，无法持久化更新前服务状态。"; return 1
    }
    if [ -e "${state_file}" ] || [ -L "${state_file}" ]; then
        lumen_update_load_original_service_state && [ "${UPDATE_ORIGINAL_TGBOT_ACTIVE}" = "${tgbot_active}" ] && return 0
        log_error "更新前服务状态文件无效或与当前观测冲突：${state_file}"; return 1
    fi
    state_tmp="$(mktemp "${SHARED_DIR}/.update-services.XXXXXX")" || return 1
    if ! printf 'schema=1\ntgbot_active=%s\n' "${tgbot_active}" > "${state_tmp}" \
            || ! chmod 600 "${state_tmp}" \
            || ! lumen_update_copy_file_durable "${state_tmp}" "${state_file}"; then
        rm -f "${state_tmp}" 2>/dev/null || true
        log_error "无法持久化更新前服务状态：${state_file}"; return 1
    fi
    rm -f "${state_tmp}" 2>/dev/null || true
    UPDATE_ORIGINAL_TGBOT_ACTIVE="${tgbot_active}"; UPDATE_ORIGINAL_TGBOT_ACTIVE_KNOWN=1
}

lumen_update_capture_original_tgbot_state() {
    local state_file="" running_ids="" tgbot_active=0
    lumen_update_load_original_service_state && return 0
    state_file="$(lumen_update_original_service_state_file)" || {
        log_error "无法定位更新前服务状态文件。"
        return 1
    }
    if [ -e "${state_file}" ] || [ -L "${state_file}" ]; then
        log_error "更新前服务状态文件存在但校验失败：${state_file}"
        return 1
    fi
    if ! running_ids="$(
        lumen_docker ps --quiet --filter "name=^/lumen-tgbot$" 2>/dev/null
    )"; then
        log_error "无法查询更新前 tgbot 运行状态，拒绝进入服务切换。"
        return 1
    fi
    [ -z "${running_ids}" ] || tgbot_active=1
    lumen_update_record_original_tgbot_state "${tgbot_active}" || return 1
    emit_info migrate_db original_tgbot_active "${tgbot_active}"
}

# Phase: check_storage
update_phase_check_storage() {
emit_start check_storage
if [ "${SKIP_STORAGE_CHECK:-0}" = "1" ]; then
    emit_info check_storage status "skipped_via_env"
    emit_done check_storage 0
else
    _storage_target="${LUMEN_DATA_ROOT:-/opt/lumendata}"
    if ! lumen_update_require_storage_identity check_storage; then
        log_error "[check_storage] ${_storage_target} 未通过已验证存储 identity 校验。"
        log_error "  请在管理后台「存储后端」页面完成 local/smb 配置，或先完成旧部署存储迁移。"
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

if ! lumen_update_require_storage_identity start_infra; then
    log_error "[start_infra] 数据根 identity 无法验证，拒绝修改目录或启动基础设施。"
    emit_fail start_infra 1
    exit 1
fi

# self-heal: 清理非标准 project=current 容器，避免与 project=lumen 撞名。
lumen_compose_project_unify

# v1.0.48 切到 pgvector:pg16，旧 alpine 数据目录 uid=70 需迁至 999。
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

if ! lumen_update_require_storage_identity start_infra_recreate; then
    log_error "[start_infra] recreate 前数据根 identity 已漂移，保持现有容器不变。"
    emit_fail start_infra 1
    exit 1
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

if ! lumen_update_require_storage_identity migrate_db; then
    log_error "[migrate_db] 数据根 identity 无法验证，拒绝探测或迁移数据库。"
    emit_fail migrate_db 1
    exit 1
fi

if ! lumen_update_capture_original_tgbot_state; then
    log_error "[migrate_db] 无法绑定更新前 tgbot active 状态，拒绝继续。"
    emit_fail migrate_db 1
    exit 1
fi

MIGRATE_NEEDED=1
if [ "${LUMEN_UPDATE_MODE}" = "fast" ]; then
    _alembic_heads_pre="$(target_alembic_head "${NEW_RELEASE}" 2>/dev/null || true)"
    _alembic_current_pre="$(current_alembic_revision "${NEW_RELEASE}" 2>/dev/null || true)"
    if [ -n "${UPDATE_LOG_DIR:-}" ] && [ ! -f "${UPDATE_LOG_DIR}/fast-anchor.json" ] \
            && ! lumen_env_truthy "${LUMEN_UPDATE_FAST_BACKUP:-0}"; then
        LUMEN_FAST_ANCHOR_ALEMBIC_HINT="${_alembic_current_pre:-}" lumen_update_write_fast_anchor || true
    fi
    if [ -n "${_alembic_heads_pre}" ] && [ "${_alembic_current_pre}" = "${_alembic_heads_pre}" ]; then
        MIGRATE_NEEDED=0
        UPDATE_MIGRATION_VERIFIED=1
        UPDATE_MIGRATION_HEAD="${_alembic_heads_pre}"
        log_info "[migrate_db] fast 模式：DB 已在目标 head=${_alembic_heads_pre}，跳过 stop api/worker 与 alembic upgrade。"
        emit_info migrate_db action "skip_already_at_head"
        emit_info migrate_db head "${_alembic_heads_pre}"
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
    _stopped_old_services=1
    UPDATE_OLD_SERVICES_STOPPED=1
    if ! lumen_compose_in "${NEW_RELEASE}" stop -t "${LUMEN_UPDATE_STOP_TIMEOUT:-30}" api worker tgbot \
            >/dev/null 2>&1; then
        log_error "[migrate_db] stop api/worker/tgbot 失败，拒绝执行 Alembic。"
        emit_fail migrate_db 1
        exit 1
    fi
    _running_writers=""
    if ! _running_writers="$(lumen_update_running_writer_services)"; then
        log_error "[migrate_db] 无法验证 api/worker/tgbot 已停止，拒绝执行 Alembic。"
        emit_fail migrate_db 1
        exit 1
    fi
    if [ -n "${_running_writers}" ]; then
        log_error "[migrate_db] writer 容器仍在运行：${_running_writers}；拒绝执行 Alembic。"
        emit_info migrate_db running_writers "${_running_writers}"
        emit_fail migrate_db 1
        exit 1
    fi
    emit_info migrate_db writers "stopped_verified"
fi

_migrate_run_failed=0
if [ "${MIGRATE_NEEDED}" = "1" ]; then
    if ! lumen_update_require_storage_identity migrate_db_execute; then
        log_error "[migrate_db] Alembic 执行前数据根 identity 已漂移；writer 保持停止。"
        emit_fail migrate_db 1
        exit 1
    fi
    UPDATE_MIGRATION_STARTED=1
    if ! lumen_compose_in "${NEW_RELEASE}" --profile migrate run --rm migrate; then
        _migrate_run_failed=1
    fi
fi

# 二次 query alembic_version 与 heads 比对，防止 transaction rollback 被吞导致假成功。
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
    if [ "${_stopped_old_services}" = "1" ] && [ -n "${CURRENT_ID:-}" ] && [ -d "${ROOT}/releases/${CURRENT_ID}" ]; then
        _migration_recovery_services="api/worker"
        if [ "${UPDATE_ORIGINAL_TGBOT_ACTIVE:-0}" -eq 1 ]; then
            _migration_recovery_services="${_migration_recovery_services}/tgbot"
        fi
        if ! guard_automatic_app_rollback_compatibility \
                "${ROOT}/releases/${CURRENT_ID}" \
                "${NEW_RELEASE}" \
                "${_alembic_current:-}"; then
            log_error "[migrate_db] 旧 release ${CURRENT_ID} 无法安全写入当前数据库 revision=${_alembic_current:-<unknown>}。"
            log_error "  已保持 api/worker/tgbot 停止，禁止旧 writer 继续写入部分迁移后的 schema。"
            log_error "  请修复迁移后继续前向升级；如无法前向恢复，请使用本轮 PostgreSQL/Redis 恢复点执行人工整库回滚。"
            emit_warn migrate_db "old_writer_restart_blocked_schema_incompatible"
        else
            log_warn "[migrate_db] schema capability 已验证，用旧 release ${CURRENT_ID} 重启 ${_migration_recovery_services}..."
            # 旧 release 的 compose 文件指向旧镜像 tag（PREVIOUS_TAG），SHARED_ENV
            # 还没被 set_image_tag 改写之前已经被改过；如果已改，先恢复成旧 tag。
            if [ -n "${PREVIOUS_TAG:-}" ] && [ -n "${TARGET_TAG:-}" ] && [ "${PREVIOUS_TAG}" != "${TARGET_TAG}" ]; then
                lumen_set_image_tag_in_env "${SHARED_ENV}" "${PREVIOUS_TAG}" 2>/dev/null \
                    || log_warn "  恢复 SHARED_ENV 到 ${PREVIOUS_TAG} 失败，旧服务可能拉错镜像 tag。"
            fi
            if lumen_update_restore_original_services \
                    "${ROOT}/releases/${CURRENT_ID}" worker api 2>/dev/null; then
                log_info "[migrate_db] 旧服务 (${CURRENT_ID}: ${_migration_recovery_services}) 已重启。"
                UPDATE_OLD_SERVICES_STOPPED=0
            else
                log_error "[migrate_db] 旧服务重启失败！业务此时停摆，请人工处理："
                log_error "    cd ${ROOT}/releases/${CURRENT_ID}"
                if [ "${UPDATE_ORIGINAL_TGBOT_ACTIVE:-0}" -eq 1 ]; then
                    log_error "    COMPOSE_PROJECT_NAME=lumen docker compose --profile tgbot up -d worker api tgbot"
                else
                    log_error "    COMPOSE_PROJECT_NAME=lumen docker compose up -d worker api"
                fi
            fi
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
    UPDATE_MIGRATION_HEAD="${_alembic_heads}"
fi
if ! lumen_update_require_storage_identity restart_services_recreate; then
    log_error "[migrate_db] 服务 recreate 前数据根 identity 已漂移，拒绝切换 release。"
    emit_fail migrate_db 1
    exit 1
fi
emit_done migrate_db 0
}
