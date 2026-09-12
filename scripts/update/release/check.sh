#!/usr/bin/env bash
# Target release resolution and no-op decision phase.

# Phase: check
update_phase_check() {
emit_start check

# CURRENT_RELEASE / CURRENT_ID 已在 self_update_scripts phase 之前解析（line ~531）。

# 确保 shared/.env 至少存在（用 lib.sh 的 helper）
if ! lumen_release_ensure_shared_env "${ROOT}"; then
    emit_info check reason "missing_shared_env"
    log_error "[check] shared/.env 不可用，无法继续。"
    log_error "[check] 当前检查目录：${ROOT}"
    if [ "${ROOT}" != "${LUMEN_DEPLOY_ROOT}" ] && [ -f "${LUMEN_DEPLOY_ROOT}/shared/.env" ]; then
        log_error "[check] 发现 ${LUMEN_DEPLOY_ROOT}/shared/.env；请执行：LUMEN_UPDATE_ROOT=${LUMEN_DEPLOY_ROOT} bash ${SCRIPT_DIR}/update.sh"
    else
        log_error "[check] 如果还没完整安装，请先执行安装；已安装实例请从部署目录的 current/scripts/lumenctl.sh 执行更新。"
    fi
    emit_fail check 1
    exit 1
fi
if ! lumen_validate_update_runtime_prerequisites "${ROOT}"; then
    emit_info check reason "runtime_prerequisite_failed"
    emit_fail check 1
    exit 1
fi

if ! lumen_require_python_min_version python3 3 8; then
    emit_info check reason "python3_too_old_or_missing"
    emit_fail check 1
    exit 1
fi
if ! snapshot_update_state; then
    log_error "[check] 无法创建 shared/.env 事务快照，拒绝继续。"
    emit_fail check 1
    exit 1
fi

# 当前 tag 与 channel
CURRENT_TAG="$(lumen_env_value LUMEN_IMAGE_TAG "${SHARED_ENV}" 2>/dev/null || echo "")"
PREVIOUS_TAG="${CURRENT_TAG}"
if [ -z "${LUMEN_UPDATE_CHANNEL:-}" ]; then
    LUMEN_UPDATE_CHANNEL="$(lumen_env_value LUMEN_UPDATE_CHANNEL "${SHARED_ENV}" 2>/dev/null || echo "")"
fi
[ -n "${LUMEN_UPDATE_CHANNEL}" ] || LUMEN_UPDATE_CHANNEL="stable"
LUMEN_UPDATE_FORCE_REDEPLOY="${LUMEN_UPDATE_FORCE_REDEPLOY:-0}"
LUMEN_UPDATE_IDEMPOTENCY_KEY="${LUMEN_UPDATE_IDEMPOTENCY_KEY:-}"
if [ -n "${LUMEN_UPDATE_RESOLVED_TAG:-}" ]; then
    LUMEN_UPDATE_RESOLVED_TAG_SOURCE="api"
else
    LUMEN_UPDATE_RESOLVED_TAG_SOURCE="shell"
fi

# 统一代理来源：支持 shared/.env 里的 LUMEN_UPDATE_PROXY_URL / LUMEN_HTTP_PROXY，
# 也兼容面板触发时透传进来的 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。
LUMEN_PROXY_URL=""
if lumen_configure_proxy_env "${SHARED_ENV}" >/dev/null 2>&1; then
    LUMEN_PROXY_URL="${LUMEN_UPDATE_PROXY_URL:-${LUMEN_HTTP_PROXY:-}}"
fi

CONFIG_CHANGED=0
CURRENT_WEB_BIND_HOST="$(lumen_env_value WEB_BIND_HOST "${SHARED_ENV}" 2>/dev/null || echo "")"
CURRENT_EXPOSE_WEB_DIRECTLY="$(lumen_env_value LUMEN_EXPOSE_WEB_DIRECTLY "${SHARED_ENV}" 2>/dev/null || echo "")"
if [ -n "${LUMEN_WEB_BIND_HOST:-}" ]; then
    if [ "${CURRENT_WEB_BIND_HOST}" != "${LUMEN_WEB_BIND_HOST}" ]; then
        lumen_set_env_value_in_file "${SHARED_ENV}" WEB_BIND_HOST "${LUMEN_WEB_BIND_HOST}"
        CURRENT_WEB_BIND_HOST="${LUMEN_WEB_BIND_HOST}"
        CONFIG_CHANGED=1
    fi
elif lumen_env_truthy "${LUMEN_EXPOSE_WEB_DIRECTLY:-}" || lumen_env_truthy "${CURRENT_EXPOSE_WEB_DIRECTLY}"; then
    if [ "${CURRENT_WEB_BIND_HOST}" != "0.0.0.0" ]; then
        lumen_set_env_value_in_file "${SHARED_ENV}" WEB_BIND_HOST "0.0.0.0"
        CURRENT_WEB_BIND_HOST="0.0.0.0"
        CONFIG_CHANGED=1
    fi
    if [ "${CURRENT_EXPOSE_WEB_DIRECTLY}" != "1" ]; then
        lumen_set_env_value_in_file "${SHARED_ENV}" LUMEN_EXPOSE_WEB_DIRECTLY "1"
        CONFIG_CHANGED=1
    fi
    log_warn "[check] LUMEN_EXPOSE_WEB_DIRECTLY=1：Web 将监听所有网卡 3000，请确认防火墙与生产 APP_ENV。"
elif [ -z "${CURRENT_WEB_BIND_HOST}" ] || [ "${CURRENT_WEB_BIND_HOST}" = "0.0.0.0" ]; then
    if [ "${CURRENT_WEB_BIND_HOST}" = "0.0.0.0" ]; then
        log_warn "[check] WEB_BIND_HOST 是旧公开默认值 0.0.0.0，改为 127.0.0.1；如需直连公网，请设置 LUMEN_EXPOSE_WEB_DIRECTLY=1。"
    else
        log_info "[check] WEB_BIND_HOST 未设置，使用默认 127.0.0.1，Web 仅监听本机回环。"
    fi
    lumen_set_env_value_in_file "${SHARED_ENV}" WEB_BIND_HOST "127.0.0.1"
    CURRENT_WEB_BIND_HOST="127.0.0.1"
    CONFIG_CHANGED=1
fi

# 解析目标 tag。保留 resolver 的具体错误码；配置错误不能伪装成普通空结果。
TARGET_TAG=""
if TARGET_TAG="$(
        lumen_image_tag_resolve "${LUMEN_UPDATE_CHANNEL}" "${SHARED_ENV}"
)"; then
    :
else
    TARGET_TAG_RC=$?
    emit_info check reason "target_tag_resolution_failed"
    log_error "[check] 无法解析目标 tag（channel=${LUMEN_UPDATE_CHANNEL}, rc=${TARGET_TAG_RC}）。"
    emit_fail check "${TARGET_TAG_RC}"
    exit "${TARGET_TAG_RC}"
fi
if [ -z "${TARGET_TAG}" ]; then
    emit_info check reason "target_tag_empty"
    log_error "[check] 无法解析目标 tag（channel=${LUMEN_UPDATE_CHANNEL}）。"
    log_error "[check] 可临时执行：LUMEN_UPDATE_CHANNEL=main bash ${SCRIPT_DIR}/update.sh"
    emit_fail check 1
    exit 1
fi
unset TARGET_TAG_RC
export LUMEN_IMAGE_TAG="${TARGET_TAG}"
TARGET_RELEASE_TAG=""
UPDATE_IMAGE_REGISTRY="$(lumen_env_value LUMEN_IMAGE_REGISTRY "${SHARED_ENV}" 2>/dev/null || true)"
[ -n "${UPDATE_IMAGE_REGISTRY}" ] || UPDATE_IMAGE_REGISTRY="ghcr.io/cyeinfpro"
if lumen_release_manifest_required "${TARGET_TAG}"; then
    TARGET_RELEASE_TAG="${TARGET_TAG}"
elif lumen_release_alias_tag "${TARGET_TAG}" \
        && [ "${UPDATE_IMAGE_REGISTRY%/}" = "ghcr.io/cyeinfpro" ]; then
    TARGET_RELEASE_TAG="$(lumen_resolve_release_alias "${TARGET_TAG}" 2>/dev/null || true)"
    if ! lumen_release_manifest_required "${TARGET_RELEASE_TAG}"; then
        log_error "[check] 无法把 ${TARGET_TAG} 解析为同系列具体 GitHub Release。"
        emit_fail check 1
        exit 1
    fi
    emit_info check release_alias "${TARGET_TAG}->${TARGET_RELEASE_TAG}"
fi

if [ "${UPDATE_IMAGE_REGISTRY%/}" = "ghcr.io/cyeinfpro" ] \
        && [ -n "${TARGET_RELEASE_TAG}" ]; then
    CHECK_IMAGE_MANIFEST="$(
        mktemp "${SHARED_DIR}/.check-image-manifest.XXXXXXXXXX" 2>/dev/null
    )" || {
        log_error "[check] 无法创建 release image manifest 临时文件。"
        emit_fail check 1
        exit 1
    }
    if ! lumen_fetch_release_manifest \
            "${TARGET_RELEASE_TAG}" "${CHECK_IMAGE_MANIFEST}" \
            || ! lumen_apply_release_manifest_compose_env \
                "${CHECK_IMAGE_MANIFEST}" "${TARGET_RELEASE_TAG}" "${SHARED_ENV}"; then
        rm -f "${CHECK_IMAGE_MANIFEST}" 2>/dev/null || true
        log_error "[check] 无法从 ${TARGET_RELEASE_TAG} 绑定完整生产镜像 digest。"
        emit_fail check 1
        exit 1
    fi
    rm -f "${CHECK_IMAGE_MANIFEST}"
elif [ "${UPDATE_IMAGE_REGISTRY%/}" = "ghcr.io/cyeinfpro" ] \
        && [ "${TARGET_TAG}" = "main" ]; then
    if ! lumen_require_immutable_image_refs \
            "${SHARED_ENV}" \
            LUMEN_POSTGRES_IMAGE_REF \
            LUMEN_REDIS_IMAGE_REF; then
        log_error "[check] rolling main 不得隐式升级数据库或缓存镜像；请先配置其 immutable refs。"
        emit_fail check 64
        exit 64
    fi
    if ! lumen_apply_rolling_app_image_refs \
            "${UPDATE_IMAGE_REGISTRY}" "${TARGET_TAG}" "${SHARED_ENV}"; then
        log_error "[check] rolling main 应用镜像 digest 解析失败。"
        emit_fail check 1
        exit 1
    fi
else
    if ! lumen_require_immutable_image_refs \
            "${SHARED_ENV}" \
            LUMEN_POSTGRES_IMAGE_REF \
            LUMEN_REDIS_IMAGE_REF \
            LUMEN_API_IMAGE_REF \
            LUMEN_WORKER_IMAGE_REF \
            LUMEN_WEB_IMAGE_REF \
            LUMEN_TGBOT_IMAGE_REF; then
        log_error "[check] 自定义 registry/通道必须预先提供完整生产镜像 digest refs。"
        emit_fail check 64
        exit 64
    fi
fi

if TARGET_VERSION_FROM_TAG="$(semver_from_image_tag "${TARGET_TAG}" 2>/dev/null || true)" \
        && [ -n "${TARGET_VERSION_FROM_TAG}" ]; then
    export LUMEN_VERSION="${TARGET_VERSION_FROM_TAG}"
fi
if ! lumen_update_journal_bind_request; then
    log_error "[check] update request 与 journal 中已绑定的不可变请求冲突。"
    emit_fail check 1
    exit 1
fi

RUNNING_API_TAG=""
if command -v docker >/dev/null 2>&1; then
    RUNNING_API_IMAGE="$(docker inspect lumen-api --format '{{.Config.Image}}' 2>/dev/null || true)"
    case "${RUNNING_API_IMAGE}" in
        *:*) RUNNING_API_TAG="${RUNNING_API_IMAGE##*:}" ;;
    esac
fi
IMAGE_TAG_DRIFT=0
if [ -n "${RUNNING_API_TAG}" ] \
        && [ -n "${CURRENT_TAG}" ] \
        && [ "${CURRENT_TAG}" = "${TARGET_TAG}" ] \
        && [ "${RUNNING_API_TAG}" != "${TARGET_TAG}" ]; then
    IMAGE_TAG_DRIFT=1
fi

emit_info check channel       "${LUMEN_UPDATE_CHANNEL}"
emit_info check current_tag   "${CURRENT_TAG:-<none>}"
emit_info check target_tag    "${TARGET_TAG}"
emit_info check running_api_tag "${RUNNING_API_TAG:-<unknown>}"
emit_info check current_id    "${CURRENT_ID:-<none>}"
emit_info check data_root     "${LUMEN_DATA_ROOT}"
emit_info check db_root       "${LUMEN_DB_ROOT}"
emit_info check web_bind_host "${CURRENT_WEB_BIND_HOST:-<default>}"
emit_info check update_mode "${LUMEN_UPDATE_MODE}"
if [ -n "${LUMEN_UPDATE_IDEMPOTENCY_KEY}" ]; then
    emit_info check idempotency_key "configured"
else
    emit_info check idempotency_key "<none>"
fi
emit_info check resolved_tag_source "${LUMEN_UPDATE_RESOLVED_TAG_SOURCE}"
emit_info check force_redeploy "${LUMEN_UPDATE_FORCE_REDEPLOY}"
if [ -n "${LUMEN_PROXY_URL}" ]; then
    emit_info check proxy "configured"
fi

# rolling image tag（main / latest / vMAJOR / vMAJOR.MINOR）即使 tag 名不变，
# GHCR 上的 digest 仍会随 CI 推送变化；用 tag 名做 noop 比较等于永远拉不到
# 新镜像。识别最终 TARGET_TAG 并跳过 noop，让 pull/migrate/restart 完整跑一遍
# ——`docker compose pull` 自带 layer-level 去重，digest 没变时也只是 HEAD 几个
# manifest 即返。
NOOP_BY_TAG_NAME=1
if lumen_image_tag_is_rolling "${TARGET_TAG}"; then
    NOOP_BY_TAG_NAME=0
fi

if [ "${LUMEN_UPDATE_FORCE_REDEPLOY}" != "1" ] \
        && [ -n "${CURRENT_TAG}" ] \
        && [ "${CURRENT_TAG}" = "${TARGET_TAG}" ] \
        && [ "${CONFIG_CHANGED}" -eq 0 ] \
        && [ "${IMAGE_TAG_DRIFT}" -eq 0 ] \
        && [ "${NOOP_BY_TAG_NAME}" -eq 1 ]; then
    log_info "[check] 当前 tag ${CURRENT_TAG} 已是目标版本，跳过中间阶段，仅做 cleanup。"
    emit_info check action "noop_already_latest"
    SKIP_TO_CLEANUP=1
    emit_done  check 0
else
    if [ "${NOOP_BY_TAG_NAME}" -eq 0 ] \
            && [ -n "${CURRENT_TAG}" ] \
            && [ "${CURRENT_TAG}" = "${TARGET_TAG}" ]; then
        log_info "[check] target_tag=${TARGET_TAG} 是 rolling tag，跳过 tag 名 noop 检查，强制 pull 拉新 digest。"
        emit_info check action "rolling_force_redeploy"
    elif [ -n "${CURRENT_TAG}" ] && [ "${CURRENT_TAG}" = "${TARGET_TAG}" ] && [ "${CONFIG_CHANGED}" -eq 1 ]; then
        log_info "[check] 当前 tag ${CURRENT_TAG} 已是目标版本，但配置已变更，继续重建 release 并重启服务。"
        emit_info check action "config_changed_redeploy"
    elif [ "${IMAGE_TAG_DRIFT}" -eq 1 ]; then
        log_warn "[check] shared/.env 已是 ${TARGET_TAG}，但运行中的 api 镜像是 ${RUNNING_API_TAG}，继续重建容器修复漂移。"
        emit_info check action "image_tag_drift_redeploy"
    fi
    SKIP_TO_CLEANUP=0
    emit_done check 0
fi
}
