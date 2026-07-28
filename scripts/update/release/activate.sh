#!/usr/bin/env bash
# Release manifest activation and image pull phases.

# Phase: set_image_tag
update_phase_set_image_tag() {
emit_start set_image_tag

if ! lumen_update_journal_assert_target_field effective_tag "${TARGET_TAG}"; then
    log_error "[set_image_tag] TARGET_TAG 与 fetch_release 已绑定目标冲突。"
    emit_fail set_image_tag 1
    exit 1
fi
if ! lumen_set_image_tag_in_env "${SHARED_ENV}" "${TARGET_TAG}"; then
    log_error "[set_image_tag] 写入 shared/.env 失败。"
    emit_fail set_image_tag 1
    exit 1
fi
export LUMEN_IMAGE_TAG="${TARGET_TAG}"

TARGET_VERSION="$(release_version_for_target "${NEW_RELEASE}" "${TARGET_TAG}" 2>/dev/null || true)"
if [ -n "${TARGET_VERSION}" ]; then
    if ! printf '%s\n' "${TARGET_VERSION}" > "${NEW_RELEASE}/VERSION"; then
        log_error "[set_image_tag] 写入新 release 的 VERSION=${TARGET_VERSION} 失败。"
        emit_fail set_image_tag 1
        exit 1
    fi
    if ! lumen_set_env_value_in_file "${SHARED_ENV}" LUMEN_VERSION "${TARGET_VERSION}"; then
        log_error "[set_image_tag] 写入 shared/.env 的 LUMEN_VERSION=${TARGET_VERSION} 失败。"
        emit_fail set_image_tag 1
        exit 1
    fi
    export LUMEN_VERSION="${TARGET_VERSION}"
    emit_info set_image_tag version "${TARGET_VERSION}"
fi

# 防御：再次 grep 校验 ==1
TAG_LINE_CNT="$(grep -cE '^LUMEN_IMAGE_TAG=' "${SHARED_ENV}" 2>/dev/null || echo 0)"
if [ "${TAG_LINE_CNT}" != "1" ]; then
    log_error "[set_image_tag] 校验失败：shared/.env 中 LUMEN_IMAGE_TAG 行数=${TAG_LINE_CNT}（期望 1）。"
    emit_fail set_image_tag 1
    exit 1
fi

# 把 target tag 落到 release 目录的 .image-tag（回滚与 resume proof）
RELEASE_IMAGE_TAG_FILE="${NEW_RELEASE}/.image-tag"
RELEASE_IMAGE_TAG_TMP="$(
    mktemp "${NEW_RELEASE}/.image-tag.XXXXXXXXXX" 2>/dev/null
)" || {
    log_error "[set_image_tag] 无法创建 .image-tag 临时文件。"
    emit_fail set_image_tag 1
    exit 1
}
if ! printf '%s\n' "${TARGET_TAG}" > "${RELEASE_IMAGE_TAG_TMP}" \
        || ! lumen_update_copy_file_durable \
            "${RELEASE_IMAGE_TAG_TMP}" "${RELEASE_IMAGE_TAG_FILE}"; then
    rm -f "${RELEASE_IMAGE_TAG_TMP}" 2>/dev/null || true
    log_error "[set_image_tag] 无法持久化 .image-tag proof。"
    emit_fail set_image_tag 1
    exit 1
fi
rm -f "${RELEASE_IMAGE_TAG_TMP}"
RELEASE_IMAGE_TAG_SHA256="$(
    lumen_update_file_sha256 "${RELEASE_IMAGE_TAG_FILE}"
)"

RELEASE_MANIFEST_FILE=""
RELEASE_MANIFEST_TAG="${TARGET_RELEASE_TAG}"
if lumen_release_manifest_required "${TARGET_TAG}" \
        || lumen_release_alias_tag "${TARGET_TAG}"; then
    if [ "${LUMEN_IMAGE_REGISTRY%/}" != "ghcr.io/cyeinfpro" ]; then
        if ! lumen_env_truthy "${LUMEN_ALLOW_UNVERIFIED_CUSTOM_REGISTRY:-0}"; then
            log_error "[set_image_tag] 正式 release tag 使用自定义 registry 时无法核对官方 digest。"
            log_error "  如确认镜像镜像源可信，请显式设置 LUMEN_ALLOW_UNVERIFIED_CUSTOM_REGISTRY=1。"
            emit_fail set_image_tag 1
            exit 1
        fi
        log_warn "[set_image_tag] 已显式允许未核验的自定义 registry。"
    else
        if [ -z "${RELEASE_MANIFEST_TAG}" ] \
                && lumen_release_alias_tag "${TARGET_TAG}"; then
            RELEASE_MANIFEST_TAG="$(lumen_resolve_release_alias "${TARGET_TAG}" 2>/dev/null || true)"
            if ! lumen_release_manifest_required "${RELEASE_MANIFEST_TAG}"; then
                log_error "[set_image_tag] 无法把 ${TARGET_TAG} 解析为同系列的具体 GitHub Release。"
                emit_fail set_image_tag 1
                exit 1
            fi
            emit_info set_image_tag release_alias "${TARGET_TAG}->${RELEASE_MANIFEST_TAG}"
        fi
        RELEASE_MANIFEST_FILE="${NEW_RELEASE}/release-manifest.json"
        if [ -n "${RELEASE_SOURCE_MANIFEST_CACHE}" ] \
                && [ "${RELEASE_MANIFEST_TAG}" = "${TARGET_RELEASE_TAG}" ]; then
            if [ -f "${RELEASE_SOURCE_MANIFEST_CACHE}" ]; then
                if ! mv -f \
                        "${RELEASE_SOURCE_MANIFEST_CACHE}" "${RELEASE_MANIFEST_FILE}"; then
                    log_error "[set_image_tag] 无法提交已校验的 release manifest。"
                    emit_fail set_image_tag 1
                    exit 1
                fi
            elif [ -f "${RELEASE_MANIFEST_FILE}" ] \
                    && [ -n "${RELEASE_MANIFEST_SHA256:-}" ] \
                    && [ "$(lumen_update_file_sha256 "${RELEASE_MANIFEST_FILE}")" \
                        = "${RELEASE_MANIFEST_SHA256}" ]; then
                emit_info set_image_tag resume "manifest_already_committed"
            else
                log_error "[set_image_tag] manifest cache 已消失且 release 内无匹配 proof。"
                emit_fail set_image_tag 1
                exit 1
            fi
            RELEASE_SOURCE_MANIFEST_CACHE=""
        elif ! lumen_fetch_release_manifest \
                "${RELEASE_MANIFEST_TAG}" "${RELEASE_MANIFEST_FILE}"; then
            log_error "[set_image_tag] 无法获取或校验 ${RELEASE_MANIFEST_TAG} 的 release-manifest.json。"
            emit_fail set_image_tag 1
            exit 1
        fi
        if ! verify_release_source_manifest_binding \
                "${RELEASE_MANIFEST_FILE}" "${RELEASE_MANIFEST_TAG}"; then
            emit_fail set_image_tag 1
            exit 1
        fi
        emit_info set_image_tag source_commit "${RELEASE_SOURCE_COMMIT}"
        emit_info set_image_tag source_commit_proof "${RELEASE_SOURCE_COMMIT_PROOF}"
        emit_info set_image_tag release_manifest "verified"
    fi
fi
if [ -n "${RELEASE_MANIFEST_TAG}" ] \
        && TARGET_VERSION_FROM_MANIFEST="$(semver_from_image_tag "${RELEASE_MANIFEST_TAG}" 2>/dev/null || true)" \
        && [ -n "${TARGET_VERSION_FROM_MANIFEST}" ] \
        && [ "${TARGET_VERSION_FROM_MANIFEST}" != "${TARGET_VERSION:-}" ]; then
    if ! printf '%s\n' "${TARGET_VERSION_FROM_MANIFEST}" > "${NEW_RELEASE}/VERSION"; then
        log_error "[set_image_tag] 写入 manifest 版本 ${TARGET_VERSION_FROM_MANIFEST} 失败。"
        emit_fail set_image_tag 1
        exit 1
    fi
    if ! lumen_set_env_value_in_file "${SHARED_ENV}" LUMEN_VERSION "${TARGET_VERSION_FROM_MANIFEST}"; then
        log_error "[set_image_tag] 写入 LUMEN_VERSION=${TARGET_VERSION_FROM_MANIFEST} 失败。"
        emit_fail set_image_tag 1
        exit 1
    fi
    TARGET_VERSION="${TARGET_VERSION_FROM_MANIFEST}"
    export LUMEN_VERSION="${TARGET_VERSION_FROM_MANIFEST}"
    emit_info set_image_tag version "${TARGET_VERSION_FROM_MANIFEST}"
fi

FINAL_MANIFEST_SHA256=""
if [ -n "${RELEASE_MANIFEST_FILE}" ]; then
    if [ ! -f "${RELEASE_MANIFEST_FILE}" ]; then
        log_error "[set_image_tag] 已声明的 release manifest 不存在。"
        emit_fail set_image_tag 1
        exit 1
    fi
    FINAL_MANIFEST_SHA256="$(
        lumen_update_file_sha256 "${RELEASE_MANIFEST_FILE}"
    )"
fi
RELEASE_MANIFEST_SHA256="${FINAL_MANIFEST_SHA256}"
if ! lumen_update_journal_bind_target; then
    log_error "[set_image_tag] release proof 与已绑定 target 冲突。"
    emit_fail set_image_tag 1
    exit 1
fi

emit_info set_image_tag tag "${TARGET_TAG}"
emit_done set_image_tag 0
}

# Phase: pull_images
update_phase_pull_images() {
if [ "${LUMEN_UPDATE_BUILD:-0}" = "1" ]; then
    emit_start pull_images   # build 兜底复用 pull_images 阶段，便于后台进度兼容。
    # 为清晰起见，单独发一个 info 行
    emit_info pull_images action "build_images"
    log_info "[build_images] LUMEN_UPDATE_BUILD=1 → docker compose build api worker web"
    if ! lumen_compose_in "${NEW_RELEASE}" build api worker web; then
        log_error "[build_images] docker compose build 失败。"
        emit_fail pull_images 1
        exit 1
    fi
    # tgbot 可选
    TGBOT_IMAGE_READY=0
    if env_key_present "${SHARED_ENV}" "TELEGRAM_BOT_TOKEN"; then
        if lumen_compose_in "${NEW_RELEASE}" build tgbot 2>/dev/null; then
            TGBOT_IMAGE_READY=1
        else
            log_error "[build_images] tgbot 已启用但 build 失败，拒绝启动未证明镜像。"
            emit_fail pull_images 1
            exit 1
        fi
    fi
    if ! lumen_update_bind_immutable_images; then
        emit_fail pull_images 1
        exit 1
    fi
    emit_done pull_images 0
fi

if [ "${LUMEN_UPDATE_BUILD:-0}" != "1" ] \
        && [ "${LUMEN_UPDATE_MODE}" = "fast" ] \
        && ! lumen_image_tag_is_rolling "${TARGET_TAG}" \
        && [ -z "${RELEASE_MANIFEST_FILE}" ] \
        && [ "${LUMEN_UPDATE_FAST_EXPLICIT_PULL:-0}" != "1" ]; then
    # Fast 模式不做单独的 compose pull 阻塞阶段。restart_services 用
    # `up --pull missing` 按服务拉缺失镜像：已有镜像直接复用，缺哪个才拉哪个。
    emit_start pull_images
    log_info "[pull_images] fast 模式：跳过显式 docker compose pull，稍后按服务 --pull missing。"
    emit_info pull_images action "skipped_by_fast_mode"
    emit_info pull_images pull_policy "up_pull_missing"
    TGBOT_IMAGE_READY=0
    if env_key_present "${SHARED_ENV}" "TELEGRAM_BOT_TOKEN"; then
        TGBOT_IMAGE_READY=1
    fi
    if ! lumen_update_bind_immutable_images; then
        log_info "[pull_images] 本地目标镜像缺失或 proof 不匹配，执行一次显式拉取后重绑。"
        if ! lumen_retry 3 5 "docker compose pull tag=${TARGET_TAG}" \
                lumen_compose_pull_per_image "${NEW_RELEASE}"; then
            log_error "[pull_images] 无法取得可绑定的目标镜像。"
            emit_fail pull_images 1
            exit 1
        fi
        if [ "${TGBOT_IMAGE_READY}" = "1" ] \
                && ! lumen_retry 2 5 "docker compose pull tgbot" \
                    lumen_compose_in "${NEW_RELEASE}" \
                        --profile tgbot pull tgbot; then
            log_error "[pull_images] tgbot 已启用但镜像拉取失败。"
            emit_fail pull_images 1
            exit 1
        fi
        if ! lumen_update_bind_immutable_images; then
            emit_fail pull_images 1
            exit 1
        fi
    fi
    emit_done pull_images 0
elif [ "${LUMEN_UPDATE_BUILD:-0}" != "1" ]; then
    # -----------------------------------------------------------------------
    # Phase: pull_images
    # -----------------------------------------------------------------------
    emit_start pull_images
    TGBOT_IMAGE_READY=0

    if [ -n "${LUMEN_PROXY_URL}" ]; then
        emit_info pull_images proxy "configured"
    fi

    # 网络抖动是 pull 失败最常见原因，先重试 3 次（指数退避 5/10/20s），仍失败再走 fallback。
    if ! lumen_retry 3 5 "docker compose pull tag=${TARGET_TAG}" \
            lumen_compose_pull_per_image "${NEW_RELEASE}"; then
        if [ "${TARGET_TAG}" != "main" ] && [ "${LUMEN_UPDATE_FALLBACK_MAIN:-0}" = "1" ]; then
            if ! lumen_update_journal_assert_target_field effective_tag main; then
                log_error "[pull_images] target 已不可变绑定为 ${TARGET_TAG}，拒绝在续跑边界内改绑 main。"
                emit_fail pull_images 1
                exit 1
            fi
            log_warn "[pull_images] docker compose pull tag=${TARGET_TAG} 失败，自动回退到 main 后重试。"
            emit_info pull_images target_tag_fallback "main"
            TARGET_TAG="main"
            export LUMEN_IMAGE_TAG="${TARGET_TAG}"
            RELEASE_MANIFEST_FILE=""
            RELEASE_MANIFEST_TAG=""
            if ! sync_main_fallback_release; then
                log_error "[pull_images] 无法把待发布源码同步到 main，拒绝混用 release 源码与 main 镜像。"
                emit_fail pull_images 1
                exit 1
            fi
            if ! lumen_set_image_tag_in_env "${SHARED_ENV}" "${TARGET_TAG}"; then
                log_error "[pull_images] 回退 main 时写入 shared/.env 失败。"
                emit_fail pull_images 1
                exit 1
            fi
            TARGET_VERSION="$(release_version_for_target \
                "${NEW_RELEASE}" "${TARGET_TAG}" 2>/dev/null || true)"
            if [ -z "${TARGET_VERSION}" ] \
                    || ! printf '%s\n' "${TARGET_VERSION}" \
                        > "${NEW_RELEASE}/VERSION" \
                    || ! lumen_set_env_value_in_file \
                        "${SHARED_ENV}" LUMEN_VERSION "${TARGET_VERSION}"; then
                log_error "[pull_images] fallback main 的 VERSION/LUMEN_VERSION 同步失败。"
                emit_fail pull_images 1
                exit 1
            fi
            export LUMEN_VERSION="${TARGET_VERSION}"
            emit_info pull_images version "${TARGET_VERSION}"
            printf '%s\n' "${TARGET_TAG}" > "${NEW_RELEASE}/.image-tag" 2>/dev/null \
                || log_warn "[pull_images] .image-tag 写入失败（已忽略，仅影响事后定位）"
            if ! lumen_retry 2 5 "docker compose pull (main fallback)" \
                    lumen_compose_pull_per_image "${NEW_RELEASE}"; then
                log_error "[pull_images] fallback main 后 docker compose pull 仍失败。"
                log_error "  请检查 GHCR 可达性或代理配置。"
                log_error "  当前服务保持不变。"
                emit_fail pull_images 1
                exit 1
            fi
        elif [ "${TARGET_TAG}" != "main" ]; then
            log_error "[pull_images] docker compose pull tag=${TARGET_TAG} 失败；stable 通道不会自动回退 main。"
            log_error "  如需跟随 rolling main，请显式设置 LUMEN_UPDATE_CHANNEL=main。"
            log_error "  如需临时允许 fallback，请显式设置 LUMEN_UPDATE_FALLBACK_MAIN=1。"
            log_error "  当前服务保持不变。"
            emit_fail pull_images 1
            exit 1
        else
            log_error "[pull_images] docker compose pull 失败。"
            log_error "  请检查 GHCR 可达性或代理配置。"
            log_error "  当前服务保持不变。"
            emit_fail pull_images 1
            exit 1
        fi
    fi
    # tgbot 在 docker-compose.yml 里走 profile=tgbot，bare `docker compose pull`
    # 会跳过它。如果 .env 启用了 telegram，单独拉一次让 tgbot 镜像也跟到目标
    # tag 对应的 GHCR digest——否则 restart_services 阶段的
    # `--profile tgbot up -d tgbot` 会复用本地旧 image。启用 tgbot 时它属于
    # 本次部署的服务集合；启用后必须与核心服务进入同一 immutable proof。
    if env_key_present "${SHARED_ENV}" "TELEGRAM_BOT_TOKEN"; then
        if ! lumen_retry 2 5 "docker compose pull tgbot" \
                lumen_compose_in "${NEW_RELEASE}" --profile tgbot pull tgbot; then
            log_error "[pull_images] tgbot 已启用但 pull 失败，拒绝启动未证明镜像。"
            emit_fail pull_images 1
            exit 1
        else
            TGBOT_IMAGE_READY=1
            emit_info pull_images tgbot_pull "ok"
        fi
    fi
    if [ -n "${RELEASE_MANIFEST_FILE}" ]; then
        manifest_args=(
            --service api
            --service worker
            --service web
        )
        if [ "${TGBOT_IMAGE_READY}" -eq 1 ]; then
            manifest_args+=(--service tgbot)
        fi
        if ! lumen_verify_release_manifest_images \
                "${RELEASE_MANIFEST_FILE}" "${RELEASE_MANIFEST_TAG}" "${TARGET_TAG}" \
                "${manifest_args[@]}"; then
            log_error "[pull_images] 本地镜像 digest 未通过 release manifest 校验。"
            emit_fail pull_images 1
            exit 1
        fi
        emit_info pull_images digest_manifest "verified"
    fi
    if ! lumen_update_bind_immutable_images; then
        emit_fail pull_images 1
        exit 1
    fi
    emit_info pull_images tag "${TARGET_TAG}"
    emit_done pull_images 0
else
    log_info "[pull_images] LUMEN_UPDATE_BUILD=1 已完成本地 build，跳过远程 pull。"
fi
}
