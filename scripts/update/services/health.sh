#!/usr/bin/env bash
# Post-switch API, web, and compose health phase.

# Phase: health_check
update_phase_health_check() {
emit_start health_check

API_HEALTH_URL="${LUMEN_API_HEALTH_URL:-http://127.0.0.1:8000/healthz}"
WEB_HEALTH_URL="${LUMEN_WEB_HEALTH_URL:-http://127.0.0.1:3000/}"

# 4K 长任务场景下 worker warm-up 可能持续数分钟（layered timeout：nginx 1800
# → arq 1800 → task 1500 → upstream 660），原 60s 探测窗口对 cold-start 严重
# 不够。默认 300s（5 min），可通过 LUMEN_HEALTH_TIMEOUT_SECONDS 覆盖。
HEALTH_TIMEOUT="${LUMEN_HEALTH_TIMEOUT_SECONDS:-300}"
HEALTH_FAIL=0
if ! lumen_health_http "${API_HEALTH_URL}" "${HEALTH_TIMEOUT}" 2; then
    log_error "[health_check] API ${API_HEALTH_URL} 在 ${HEALTH_TIMEOUT}s 内不可达。"
    HEALTH_FAIL=1
fi
if ! lumen_health_http "${WEB_HEALTH_URL}" "${HEALTH_TIMEOUT}" 2; then
    log_error "[health_check] Web ${WEB_HEALTH_URL} 在 ${HEALTH_TIMEOUT}s 内不可达。"
    HEALTH_FAIL=1
fi
if ! lumen_health_compose api worker web; then
    log_error "[health_check] docker compose 状态检查失败。"
    HEALTH_FAIL=1
fi

if [ "${HEALTH_FAIL}" -eq 1 ]; then
    log_error "[health_check] 健康检查失败；新代码已上线但状态异常。"
    if [ "${UPDATE_MIGRATION_VERIFIED}" -eq 1 ]; then
        log_update_restore_boundary health_check
        log_error "  数据库迁移已应用；恢复策略将按已记录 restore boundary 执行。"
    else
        log_warn "  本轮未执行数据库迁移，将按更新前快照自动恢复旧 release。"
        log_error "  如自动恢复失败，请执行："
    fi
    log_error "    cd ${CURRENT_LINK}"
    log_error "    COMPOSE_PROJECT_NAME=lumen docker compose logs --tail=120 api worker web"
    log_error "    COMPOSE_PROJECT_NAME=lumen docker compose ps"
    log_error "  状态快照：release_id=${NEW_ID}  image_tag=${TARGET_TAG}  current → $(readlink "${ROOT}/current" 2>/dev/null || echo unknown)"
    log_error "  如需回滚，参考 docs/.. §18 或调高 LUMEN_HEALTH_TIMEOUT_SECONDS 重跑健康。"
    emit_fail health_check 1
    exit 1
fi
if ! mark_update_committed; then
    emit_fail health_check 1
    exit 1
fi
emit_done health_check 0
}
