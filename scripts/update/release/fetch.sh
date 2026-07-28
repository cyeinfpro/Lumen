#!/usr/bin/env bash
# Release fetch and staging phase.

# Phase: fetch_release
update_phase_fetch_release() {
emit_start fetch_release

if [ "${LUMEN_UPDATE_JOURNAL_RESUMED:-0}" = "1" ] \
        && [ -n "${TARGET_TAG:-}" ] \
        && [ -n "${NEW_ID:-}" ] \
        && [ -n "${NEW_RELEASE:-}" ] \
        && [ -n "${RELEASE_SOURCE_COMMIT:-}" ] \
        && [ -n "${RELEASE_SOURCE_COMMIT_PROOF:-}" ] \
        && [ -n "${RELEASE_SOURCE_PROOF_FILE:-}" ] \
        && [ -n "${RELEASE_SOURCE_PROOF_SHA256:-}" ] \
        && [ -n "${RELEASE_SOURCE_TREE_SHA256:-}" ]; then
    if ! lumen_update_journal_bind_target; then
        log_error "[fetch_release] 已落盘 target proof 无法幂等重放。"
        emit_fail fetch_release 1
        exit 1
    fi
    emit_info fetch_release resume "target_contract_replayed"
    emit_done fetch_release 0
    return 0
fi

# 发布物来源目录：
#   - LUMEN_REPO_DIR / LUMEN_SOURCE_ROOT 显式指定时优先采用；
#   - 当前脚本或 /root/Lumen 来自完整 git 仓库时，优先从该仓库复制（让脚本/compose 修复进入新 release）；
#   - 标准 release 布局下从 current release 复制，确保新 release 根部有 docker-compose.yml；
#   - 旧 in-place / 开发仓库下才从 ROOT 复制。
if REPO_SOURCE="$(detect_repo_source_dir 2>/dev/null || true)" && [ -n "${REPO_SOURCE}" ]; then
    REPO_DIR="${REPO_SOURCE}"
elif [ -n "${CURRENT_RELEASE}" ] && [ -d "${CURRENT_RELEASE}" ]; then
    REPO_DIR="${CURRENT_RELEASE}"
else
    REPO_DIR="${ROOT}"
fi
emit_info fetch_release repo_dir "${REPO_DIR}"

# Image-extract fallback: host 不是 git repo 时,从 lumen-api image 里 docker cp
# 出 release-time files (docker-compose.yml + deploy/ + scripts/ + VERSION),让
# update 流程不再依赖 host 上是否有 git clone / 公网 GitHub 可达。
# 见 Dockerfile.python 的 "Release-time files" COPY 块。

RELEASE_SOURCE_REF=""
RELEASE_SOURCE_IMAGE_EXTRACT=0
if [ -n "${TARGET_RELEASE_TAG}" ] \
        && [ "${UPDATE_IMAGE_REGISTRY%/}" = "ghcr.io/cyeinfpro" ]; then
    RELEASE_SOURCE_MANIFEST_CACHE="$(
        mktemp "${ROOT}/.release-source-manifest.XXXXXXXXXX" 2>/dev/null
    )" || {
        log_error "[fetch_release] 无法创建 release source manifest 临时文件。"
        emit_fail fetch_release 1
        exit 1
    }
    if ! prepare_official_release_source_manifest \
            "${TARGET_RELEASE_TAG}" "${RELEASE_SOURCE_MANIFEST_CACHE}"; then
        emit_fail fetch_release 1
        exit 1
    fi
fi

# Official releases on non-git hosts always refresh from the manifest's
# immutable API image. This path is independent of LUMEN_UPDATE_GIT_PULL.
if [ -n "${RELEASE_EXPECTED_COMMIT}" ] && [ ! -d "${REPO_DIR}/.git" ]; then
    if [ "${LUMEN_UPDATE_DISABLE_IMAGE_EXTRACT:-0}" = "1" ]; then
        log_error "[fetch_release] 正式 release 在无 .git 主机上不能禁用 immutable image source；拒绝使用当前快照。"
        emit_fail fetch_release 1
        exit 1
    fi
    IMAGE_EXTRACT_DIR="${ROOT}/.update-image-extract"
    if ! try_image_extract_release \
            "${TARGET_TAG}" \
            "${IMAGE_EXTRACT_DIR}" \
            "${RELEASE_SOURCE_API_IMAGE}"; then
        log_error "[fetch_release] 无法从 ${RELEASE_SOURCE_API_IMAGE} 取得 commit-proven 发布物。"
        emit_fail fetch_release 1
        exit 1
    fi
    REPO_DIR="${IMAGE_EXTRACT_DIR}"
    RELEASE_SOURCE_IMAGE_EXTRACT=1
    RELEASE_SOURCE_REF="${RELEASE_SOURCE_COMMIT}"
    emit_info fetch_release source "immutable_image_extract"
    log_info "[fetch_release] 已从 immutable image 提取代码到 ${REPO_DIR}"
fi

if [ "${LUMEN_UPDATE_GIT_PULL:-0}" = "1" ]; then
    if [ ! -d "${REPO_DIR}/.git" ]; then
        if [ "${RELEASE_SOURCE_IMAGE_EXTRACT}" != "1" ]; then
            IMAGE_EXTRACT_DIR="${ROOT}/.update-image-extract"
            if [ "${LUMEN_UPDATE_DISABLE_IMAGE_EXTRACT:-0}" != "1" ] \
                    && try_image_extract_release \
                        "${TARGET_TAG:-main}" "${IMAGE_EXTRACT_DIR}"; then
                REPO_DIR="${IMAGE_EXTRACT_DIR}"
                RELEASE_SOURCE_IMAGE_EXTRACT=1
                RELEASE_SOURCE_REF="${RELEASE_SOURCE_COMMIT}"
                emit_info fetch_release source "image_extract"
                log_info "[fetch_release] 已从 image 提取代码到 ${REPO_DIR}"
            else
                log_warn "[fetch_release] 非正式/rolling 更新未取得 image source；按显式兼容语义使用当前快照。"
            fi
        fi
    else
        if ! command -v git >/dev/null 2>&1; then
            log_error "[fetch_release] LUMEN_UPDATE_GIT_PULL=1 但缺少 git。"
            emit_fail fetch_release 1
            exit 1
        fi
        GIT_REF="${LUMEN_UPDATE_GIT_REF:-${TARGET_RELEASE_TAG}}"
        log_info "[fetch_release] git fetch in ${REPO_DIR}"
        if ! ( cd "${REPO_DIR}" && git fetch --quiet --all --prune --tags ); then
            log_error "[fetch_release] git fetch 失败。"
            emit_fail fetch_release 1
            exit 1
        fi
        if [ -n "${LUMEN_UPDATE_GIT_REF:-}" ] \
                && ! printf '%s\n' "${GIT_REF}" \
                    | grep -Eq '^([0-9a-f]{40}|v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?)$'; then
            log_error "[fetch_release] LUMEN_UPDATE_GIT_REF 必须是具体 release tag 或 40 位 commit，拒绝可变 branch：${GIT_REF}"
            emit_fail fetch_release 1
            exit 1
        fi
        if [ -z "${GIT_REF}" ]; then
            GIT_REF="refs/remotes/origin/main"
        fi
        RELEASE_SOURCE_REF="$(cd "${REPO_DIR}" \
            && git rev-parse --verify "${GIT_REF}^{commit}" 2>/dev/null || true)"
        if ! printf '%s\n' "${RELEASE_SOURCE_REF}" | grep -Eq '^[0-9a-f]{40}$'; then
            log_error "[fetch_release] 无法把 ${GIT_REF} 解析为不可变 commit。"
            emit_fail fetch_release 1
            exit 1
        fi
        emit_info fetch_release source_commit "${RELEASE_SOURCE_REF}"
    fi
fi
if [ -z "${RELEASE_SOURCE_REF}" ]; then
    RELEASE_SOURCE_REF="${TARGET_RELEASE_TAG}"
fi

if [ -d "${REPO_DIR}/.git" ]; then
    RELEASE_SOURCE_GIT_REF="${RELEASE_SOURCE_REF:-HEAD}"
    RELEASE_SOURCE_GIT_COMMIT="$(cd "${REPO_DIR}" \
        && git rev-parse --verify \
            "${RELEASE_SOURCE_GIT_REF}^{commit}" 2>/dev/null || true)"
    if release_commit_is_valid "${RELEASE_SOURCE_GIT_COMMIT}"; then
        if ! record_release_source_commit \
                "${RELEASE_SOURCE_GIT_COMMIT}" \
                "git:${RELEASE_SOURCE_GIT_REF}"; then
            emit_fail fetch_release 1
            exit 1
        fi
    elif [ -n "${RELEASE_EXPECTED_COMMIT}" ]; then
        log_error "[fetch_release] 无法从本地 git 解析 ${RELEASE_SOURCE_GIT_REF} 的 immutable commit。"
        emit_fail fetch_release 1
        exit 1
    fi
fi
if [ -n "${RELEASE_EXPECTED_COMMIT}" ] \
        && ! release_commit_is_valid "${RELEASE_SOURCE_COMMIT}"; then
    log_error "[fetch_release] 正式 release 未取得可验证源码 commit；拒绝继续。"
    emit_fail fetch_release 1
    exit 1
fi

# 新 release id + 目录
NEW_ID="releases-$(date -u +%Y%m%d-%H%M%S)"
NEW_RELEASE="${ROOT}/releases/${NEW_ID}"
mkdir -p "${ROOT}/releases" "${ROOT}/shared"
if [ -e "${NEW_RELEASE}" ]; then
    log_error "[fetch_release] 目标 release 目录已存在：${NEW_RELEASE}"
    emit_fail fetch_release 1
    exit 1
fi
mkdir -p "${NEW_RELEASE}"
emit_info fetch_release release_id   "${NEW_ID}"
emit_info fetch_release release_path "${NEW_RELEASE}"

log_info "[fetch_release] 同步发布物 ${REPO_DIR} -> ${NEW_RELEASE}"
if ! sync_repo_to_release "${REPO_DIR}" "${NEW_RELEASE}" "${RELEASE_SOURCE_REF}"; then
    log_error "[fetch_release] 同步仓库到 release 失败。"
    emit_fail fetch_release 1
    exit 1
fi

# 把 shared/.env 软链回 release 根（让 docker compose --env-file 默认行为生效）
mkdir -p "${SHARED_DIR}"
if [ -e "${NEW_RELEASE}/.env" ] && [ ! -L "${NEW_RELEASE}/.env" ]; then
    mv "${NEW_RELEASE}/.env" "${NEW_RELEASE}/.env.pre-link.$(date -u +%Y%m%d%H%M%S)" 2>/dev/null || \
        rm -f "${NEW_RELEASE}/.env" 2>/dev/null || true
fi
ln -sfn "${SHARED_ENV}" "${NEW_RELEASE}/.env"

# 探测 GHCR 上 tag 是否真的存在（lumen-api 作为代表）
LUMEN_IMAGE_REGISTRY="$(lumen_env_value LUMEN_IMAGE_REGISTRY "${SHARED_ENV}" 2>/dev/null || echo "")"
[ -n "${LUMEN_IMAGE_REGISTRY}" ] || LUMEN_IMAGE_REGISTRY="ghcr.io/cyeinfpro"

if [ "${RELEASE_SOURCE_IMAGE_EXTRACT:-0}" = "1" ]; then
    log_info "[fetch_release] image_extract 已验证 ${TARGET_TAG} 可拉取，跳过 GHCR manifest 探测。"
    emit_info fetch_release tag_probe "skipped_image_extract_verified"
elif ! probe_ghcr_tag "${LUMEN_IMAGE_REGISTRY}/lumen-api" "${TARGET_TAG}"; then
    if [ "${TARGET_TAG}" != "main" ] && [ "${LUMEN_UPDATE_FALLBACK_MAIN:-0}" = "1" ]; then
        log_warn "[fetch_release] 目标镜像 tag=${TARGET_TAG} 不存在，自动回退到 main。"
        emit_info fetch_release target_tag_fallback "main"
        TARGET_TAG="main"
        if ! sync_main_fallback_release; then
            emit_fail fetch_release 1
            exit 1
        fi
        if ! probe_ghcr_tag "${LUMEN_IMAGE_REGISTRY}/lumen-api" "${TARGET_TAG}"; then
            enable_local_build_fallback
        fi
    else
        if [ "${TARGET_TAG}" != "main" ]; then
            log_warn "[fetch_release] 目标镜像 tag=${TARGET_TAG} 不存在；stable 通道不会自动回退 main。"
            emit_info fetch_release target_tag_fallback "disabled"
        fi
        enable_local_build_fallback
    fi
fi

if ! release_commit_is_valid "${RELEASE_SOURCE_COMMIT:-}" \
        || [ -z "${RELEASE_SOURCE_COMMIT_PROOF:-}" ]; then
    log_error "[fetch_release] 待发布源码缺少不可变 commit/proof，拒绝完成 fetch_release。"
    emit_fail fetch_release 1
    exit 1
fi
if ! rm -f \
        "${NEW_RELEASE}/.image-tag" \
        "${NEW_RELEASE}/.release-source-proof" \
        "${NEW_RELEASE}/release-manifest.json"; then
    log_error "[fetch_release] 无法清理从旧 release 继承的 proof 文件。"
    emit_fail fetch_release 1
    exit 1
fi
if ! RELEASE_SOURCE_TREE_SHA256="$(
        lumen_update_release_source_sha256 "${NEW_RELEASE}"
    )"; then
    log_error "[fetch_release] 无法计算 staged source tree SHA-256。"
    emit_fail fetch_release 1
    exit 1
fi
RELEASE_SOURCE_PROOF_FILE="${NEW_RELEASE}/.release-source-proof"
RELEASE_SOURCE_PROOF_TMP="$(
    mktemp "${NEW_RELEASE}/.release-source-proof.XXXXXXXXXX" 2>/dev/null
)" || {
    log_error "[fetch_release] 无法创建 source proof 临时文件。"
    emit_fail fetch_release 1
    exit 1
}
if ! printf '%s\n%s\n' \
        "${RELEASE_SOURCE_COMMIT}" \
        "${RELEASE_SOURCE_COMMIT_PROOF}" > "${RELEASE_SOURCE_PROOF_TMP}" \
        || ! lumen_update_copy_file_durable \
            "${RELEASE_SOURCE_PROOF_TMP}" "${RELEASE_SOURCE_PROOF_FILE}"; then
    rm -f "${RELEASE_SOURCE_PROOF_TMP}" 2>/dev/null || true
    log_error "[fetch_release] 无法持久化 source commit proof。"
    emit_fail fetch_release 1
    exit 1
fi
rm -f "${RELEASE_SOURCE_PROOF_TMP}"
RELEASE_SOURCE_PROOF_SHA256="$(
    lumen_update_file_sha256 "${RELEASE_SOURCE_PROOF_FILE}"
)"

RELEASE_MANIFEST_FILE=""
RELEASE_MANIFEST_SHA256=""
if [ -n "${RELEASE_SOURCE_MANIFEST_CACHE}" ]; then
    if [ ! -f "${RELEASE_SOURCE_MANIFEST_CACHE}" ]; then
        log_error "[fetch_release] release manifest cache 缺失，拒绝绑定 target。"
        emit_fail fetch_release 1
        exit 1
    fi
    RELEASE_MANIFEST_SHA256="$(
        lumen_update_file_sha256 "${RELEASE_SOURCE_MANIFEST_CACHE}"
    )"
fi
if [ -n "${TARGET_RELEASE_TAG}" ] && [ -z "${RELEASE_MANIFEST_SHA256}" ]; then
    log_error "[fetch_release] fixed release 缺少 release manifest SHA-256，拒绝绑定 target。"
    emit_fail fetch_release 1
    exit 1
fi
RELEASE_IMAGE_TAG_FILE=""
RELEASE_IMAGE_TAG_SHA256=""
# rolling digest 只有本地 pull/build 后才能证明；先绑定空值，pull_images 再用
# Docker 的 immutable RepoDigest/Image ID 单调补全，不能用 tag/source commit 冒充。
TARGET_ROLLING_DIGEST=""
if ! lumen_update_journal_bind_target; then
    log_error "[fetch_release] update target 与 journal 中已绑定的不可变目标冲突。"
    emit_fail fetch_release 1
    exit 1
fi

emit_done fetch_release 0
}
