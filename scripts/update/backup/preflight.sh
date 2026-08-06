#!/usr/bin/env bash
# Update preflight and backup preflight phases.

# Phase: preflight
update_phase_preflight() {
emit_start preflight

if ! lumen_validate_update_runtime_prerequisites "${ROOT}"; then
    log_error "[preflight] 更新运行时前置条件未通过，拒绝进入备份、迁移或服务切换。"
    emit_fail preflight 1
    exit 1
fi

# Docker / docker compose 可用
lumen_require_docker_access

# 磁盘 ≥ 5GB
DISK_FREE_GB="$(disk_free_gb_opt)"
emit_info preflight disk_free_gb "${DISK_FREE_GB}"
if [ "${DISK_FREE_GB}" != "-1" ] && [ "${DISK_FREE_GB}" -lt 5 ]; then
    log_error "[preflight] /opt 可用磁盘 ${DISK_FREE_GB}GB < 5GB，请先清理。"
    emit_fail preflight 1
    exit 1
fi

# .env 关键字段。BYOK 主密钥和应用配置保持一致：开发/本地/测试环境允许
# API/worker 使用 deterministic dev fallback。因为 docker compose 插值早于
# 应用启动，这里会把同一个 fallback 显式写回 shared/.env；非开发环境必须
# 显式配置真实密钥，避免重启后无法解密已有用户 API Key。
ENV_MISSING=0
for k in DATABASE_URL REDIS_URL SESSION_SECRET; do
    if ! env_key_present "${SHARED_ENV}" "${k}"; then
        log_error "[preflight] shared/.env 缺少 ${k} 或为空。"
        ENV_MISSING=1
    fi
done
BYOK_SECRET_VALUE="$(lumen_env_value BYOK_API_KEY_MASTER_SECRET "${SHARED_ENV}" 2>/dev/null || true)"
if [ -n "${BYOK_SECRET_VALUE}" ] && ! shared_app_env_is_development "${SHARED_ENV}" \
        && [ "${BYOK_SECRET_VALUE}" = "${BYOK_DEV_MASTER_SECRET}" ]; then
    log_error "[preflight] 非开发 APP_ENV 不能使用公开的 BYOK dev fallback；请配置真实 BYOK_API_KEY_MASTER_SECRET。"
    ENV_MISSING=1
elif [ -z "${BYOK_SECRET_VALUE}" ]; then
    if shared_app_env_is_development "${SHARED_ENV}"; then
        log_warn "[preflight] shared/.env 缺少 BYOK_API_KEY_MASTER_SECRET；APP_ENV 为开发/本地/测试模式，写入应用内 dev fallback 供 docker compose 使用。"
        if ! lumen_set_env_value_in_file "${SHARED_ENV}" BYOK_API_KEY_MASTER_SECRET "${BYOK_DEV_MASTER_SECRET}"; then
            log_error "[preflight] 写入 BYOK_API_KEY_MASTER_SECRET dev fallback 失败。"
            ENV_MISSING=1
        else
            emit_info preflight byok_secret "dev_fallback_backfilled"
        fi
    else
        log_error "[preflight] shared/.env 缺少 BYOK_API_KEY_MASTER_SECRET 或为空。"
        ENV_MISSING=1
    fi
fi
IMAGE_PROXY_SECRET_VALUE="$(lumen_env_value IMAGE_PROXY_SECRET "${SHARED_ENV}" 2>/dev/null || true)"
if [ -z "${IMAGE_PROXY_SECRET_VALUE}" ]; then
    if command -v openssl >/dev/null 2>&1; then
        IMAGE_PROXY_SECRET_VALUE="$(openssl rand -hex 32)"
    else
        IMAGE_PROXY_SECRET_VALUE="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    fi
    log_warn "[preflight] shared/.env 缺少 IMAGE_PROXY_SECRET；已生成随机值，避免新版 API 启动失败。"
    if ! lumen_set_env_value_in_file "${SHARED_ENV}" IMAGE_PROXY_SECRET "${IMAGE_PROXY_SECRET_VALUE}"; then
        log_error "[preflight] 写入 IMAGE_PROXY_SECRET 失败。"
        ENV_MISSING=1
    else
        emit_info preflight image_proxy_secret "generated"
    fi
elif ! shared_app_env_is_development "${SHARED_ENV}" && [ "${#IMAGE_PROXY_SECRET_VALUE}" -lt 32 ]; then
    log_error "[preflight] 非开发 APP_ENV 的 IMAGE_PROXY_SECRET 长度必须至少 32 字符。"
    ENV_MISSING=1
fi
if [ "${ENV_MISSING}" -eq 1 ]; then
    emit_fail preflight 1
    exit 1
fi

# 数据目录与权限。PG/Redis 可通过 LUMEN_DB_ROOT 放本机盘；
# storage/backup 继续跟随 LUMEN_DATA_ROOT（可为 CIFS/NAS）。
if ! check_data_owners; then
    log_error "[preflight] 数据目录不齐全，请先跑 install.sh 或手动准备 LUMEN_DB_ROOT / LUMEN_DATA_ROOT。"
    emit_fail preflight 1
    exit 1
fi

emit_done preflight 0
}

# Phase: backup_preflight
update_phase_backup_preflight() {
emit_start backup_preflight

case "${LUMEN_UPDATE_MODE:-}" in
    fast|standard)
        ;;
    *)
        log_error "[backup_preflight] 非法 LUMEN_UPDATE_MODE=${LUMEN_UPDATE_MODE:-<empty>}，拒绝采用较弱备份策略。"
        emit_fail backup_preflight 64
        exit 64
        ;;
esac

if lumen_env_truthy "${LUMEN_UPDATE_SKIP_BACKUP:-0}"; then
    log_warn "[backup_preflight] LUMEN_UPDATE_SKIP_BACKUP=1，跳过备份（强烈不推荐）。"
    emit_warn backup_preflight "skipped_by_env"
    emit_done backup_preflight 0
elif [ "${LUMEN_UPDATE_MODE}" = "fast" ] \
        && ! lumen_env_truthy "${LUMEN_UPDATE_FAST_BACKUP:-0}" \
        && ! update_requires_migration_restore_point; then
    log_warn "[backup_preflight] fast 模式默认跳过备份；需要强制备份请设置 LUMEN_UPDATE_FAST_BACKUP=1 或 LUMEN_UPDATE_MODE=standard。"
    emit_warn backup_preflight "skipped_by_fast_mode"
    emit_done backup_preflight 0
else
    if ! run_update_backup_preflight; then
        emit_fail backup_preflight 1
        exit 1
    fi
    emit_done backup_preflight 0
fi
}
