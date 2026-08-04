#!/usr/bin/env bash
# Release source synchronization, image extraction, and registry fallback helpers.

_LUMEN_UPDATE_SCRIPT_UNIT_FILES=(
    lib.sh
    lib/system.sh
    lib/environment.sh
    lib/step_protocol.sh
    lib/runtime.sh
    lib/locking.sh
    lib/container_release.sh
    lib/release_layout.sh
    lib/self_update.sh
    lib/backup_restore_services.sh
    lib/backup_journal.sh
    lib/restore_journal.sh
    release_manifest_guard.py
    update_runner.py
    restore_runner.py
    redis_backup_archive.py
    backup_permissions.py
    restore_journal.py
    backup.sh
    restore.sh
    update.sh
    update/entry_lock.py
    update/runner.sh
    update/phases.sh
    update/bootstrap.sh
    update/common.sh
    update/phase_contract.sh
    update/journal.sh
    update/durable_io.py
    update/journal_store.py
    update/journal_validation.py
    update/release/manifest.sh
    update/release/runner_units.sh
    update/release/source_helpers.sh
    update/release/self_update.sh
    update/release/check.sh
    update/release/fetch.sh
    update/release/digest.sh
    update/release/image_proof_store.py
    update/release/activate.sh
    update/backup/restore_points.sh
    update/backup/storage_identity.sh
    update/backup/migration_helpers.sh
    update/backup/preflight.sh
    update/backup/phases.sh
    update/services/compose.sh
    update/services/switch.sh
    update/services/release_activation.sh
    update/services/restart.sh
    update/services/health.sh
    update/recovery/cleanup.sh
    update/recovery/consumer.sh
    update/recovery/state.sh
    update/recovery/blue_green.sh
)

rsync_repo_to_release() {
    local src="$1"
    local dst="$2"
    local err_file rc
    if ! command -v rsync >/dev/null 2>&1; then
        log_error "缺少 rsync，请先安装。"
        return 1
    fi
    err_file="$(mktemp "${UPDATE_LOG_DIR:-/tmp}/lumen-rsync.XXXXXX.err" 2>/dev/null || mktemp)"
    rc=0
    rsync -a \
        --exclude='/.git/' \
        --exclude='/.env' \
        --exclude='/.env.local' \
        --exclude='/shared/' \
        --exclude='/releases/' \
        --exclude='/current' \
        --exclude='/previous' \
        --exclude='/node_modules/' \
        --exclude='/.venv/' \
        --exclude='/.pytest_cache/' \
        --exclude='/.mypy_cache/' \
        --exclude='/.ruff_cache/' \
        --exclude='/apps/web/.next/' \
        --exclude='/apps/web/node_modules/' \
        --exclude='/apps/worker/var/' \
        --exclude='/var/' \
        --exclude='/.lumen-script.lock/' \
        --exclude='/.update.log' \
        --exclude='/.install-logs/' \
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        --exclude='.DS_Store' \
        "${src}/" "${dst}/" 2>"${err_file}" || rc=$?
    if [ "${rc}" -ne 0 ]; then
        log_error "rsync 失败（rc=${rc}）：${src} -> ${dst}"
        sed -n '1,30p' "${err_file}" 2>/dev/null | while IFS= read -r line; do
            [ -n "${line}" ] && log_error "rsync stderr: ${line}"
        done
        rm -f "${err_file}"
        return "${rc}"
    fi
    rm -f "${err_file}"
    return 0
}

sync_repo_to_release() {
    local src="$1"
    local dst="$2"
    local source_ref="${3:-}"
    local archive_ref="${source_ref:-HEAD}"
    local err_file rc
    if [ -d "${src}/.git" ] && command -v git >/dev/null 2>&1 && command -v tar >/dev/null 2>&1; then
        err_file="$(mktemp "${UPDATE_LOG_DIR:-/tmp}/lumen-git-archive.XXXXXX.err" 2>/dev/null || mktemp)"
        log_info "[fetch_release] git archive ${archive_ref} from ${src} -> ${dst}"
        rc=0
        ( cd "${src}" && git archive --format=tar "${archive_ref}" ) 2>"${err_file}" \
            | tar -xf - -C "${dst}" 2>>"${err_file}" || rc=$?
        if [ "${rc}" -eq 0 ]; then
            rm -f "${err_file}"
            return 0
        fi
        if [ -n "${source_ref}" ]; then
            log_error "[fetch_release] git archive ${source_ref} 失败（rc=${rc}），拒绝回退到工作树 HEAD。"
            sed -n '1,20p' "${err_file}" 2>/dev/null | while IFS= read -r line; do
                [ -n "${line}" ] && log_error "git archive stderr: ${line}"
            done
            rm -f "${err_file}"
            return "${rc}"
        fi
        log_warn "[fetch_release] git archive 失败（rc=${rc}），回退 rsync。"
        sed -n '1,20p' "${err_file}" 2>/dev/null | while IFS= read -r line; do
            [ -n "${line}" ] && log_warn "git archive stderr: ${line}"
        done
        rm -f "${err_file}"
    fi
    rsync_repo_to_release "${src}" "${dst}"
}

detect_repo_source_dir() {
    local candidate
    for candidate in \
        "${LUMEN_REPO_DIR:-}" \
        "${LUMEN_SOURCE_ROOT:-}" \
        "${SCRIPT_ROOT}" \
        "/root/Lumen" \
        "/opt/Lumen"; do
        [ -n "${candidate}" ] || continue
        [ -f "${candidate}/docker-compose.yml" ] || continue
        if [ -n "${LUMEN_REPO_DIR:-}" ] && [ "${candidate}" = "${LUMEN_REPO_DIR}" ]; then
            printf '%s' "${candidate}"
            return 0
        fi
        if [ -d "${candidate}/.git" ]; then
            printf '%s' "${candidate}"
            return 0
        fi
    done
    return 1
}

# 探测 GHCR 上 tag 是否存在。在没有 token 的情况下只能尽力 HEAD：
# 失败时 warn 但不 abort（pull_images 阶段会真实暴露问题）。
probe_ghcr_tag() {
    local image="$1"  # e.g. ghcr.io/cyeinfpro/lumen-api
    local tag="$2"
    local manifest_url="https://ghcr.io/v2/${image#ghcr.io/}/manifests/${tag}"
    if ! command -v curl >/dev/null 2>&1; then
        return 0
    fi
    # 先拿匿名 token（GHCR 公开包流程：/token?scope=repository:<image>:pull）
    local token http_code
    token="$(curl -fsSL "https://ghcr.io/token?scope=repository:${image#ghcr.io/}:pull" 2>/dev/null \
        | sed -nE 's/.*"token"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' | head -n1)"
    if [ -z "${token}" ]; then
        return 0
    fi
    # 多架构 buildx 推上来的 tag 在 GHCR 是 OCI Image Index / manifest list，
    # 不带 list/index 的 mediaType 会被 registry 当成 unknown manifest 返回 404，
    # 导致 update.sh 误判镜像不存在并回退到本地 build。所以四种 mediaType 全列上。
    http_code="$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${token}" \
        -H "Accept: application/vnd.oci.image.index.v1+json" \
        -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
        -H "Accept: application/vnd.oci.image.manifest.v1+json" \
        -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
        "${manifest_url}" 2>/dev/null || echo "000")"
    case "${http_code}" in
        2??) return 0 ;;
        404)
            log_error "GHCR 上未找到镜像：${image}:${tag}"
            return 1
            ;;
        *)
            log_warn "GHCR 探测 ${image}:${tag} 返回 HTTP ${http_code}，跳过严格校验，由 pull 阶段兜底。"
            return 0
            ;;
    esac
}

try_image_extract_release() {
    local tag="${1:-main}"
    local out_dir="$2"
    local immutable_image="${3:-}"
    if ! command -v docker >/dev/null 2>&1; then
        return 1
    fi
    local registry="${LUMEN_IMAGE_REGISTRY:-ghcr.io/cyeinfpro}"
    local image="${immutable_image:-${registry}/lumen-api:${tag}}"
    if [ "${LUMEN_UPDATE_MODE}" = "fast" ] \
            && ! lumen_image_tag_is_rolling "${tag}" \
            && docker image inspect "${image}" >/dev/null 2>&1; then
        log_info "[fetch_release] fast 模式：复用本地已有 ${image} 提取发布物。"
    else
        log_info "[fetch_release] 尝试 docker pull ${image}"
        if ! docker pull "${image}" >/dev/null 2>&1; then
            log_warn "[fetch_release] docker pull 失败 (image=${image})"
            return 1
        fi
    fi
    rm -rf "${out_dir}"
    mkdir -p "${out_dir}"
    local cid
    cid="$(docker create "${image}" /bin/true 2>/dev/null)" || return 1
    local rc=0
    # docker cp 不支持通配符;逐个 cp 完整 release-time 内容
    # （host 仅严格需要 docker-compose.yml + scripts + deploy；apps/packages/pyproject/
    # uv.lock 主要让 host ssh 调试时能看到完整代码树，不影响 runtime — 容器从 image 起。）
    local required_paths=(docker-compose.yml VERSION deploy scripts)
    local optional_paths=(apps packages pyproject.toml uv.lock)
    local path
    for path in "${required_paths[@]}"; do
        if ! docker cp "${cid}:/app/${path}" "${out_dir}/${path}" 2>/dev/null; then
            log_warn "[fetch_release] image 内缺少必须的 /app/${path}（image 可能是旧版本）"
            rc=1
        fi
    done
    for path in "${optional_paths[@]}"; do
        docker cp "${cid}:/app/${path}" "${out_dir}/${path}" 2>/dev/null || true
    done
    docker rm "${cid}" >/dev/null 2>&1 || true
    [ "${rc}" = "0" ] || return 1
    test -f "${out_dir}/docker-compose.yml" || return 1
    test -d "${out_dir}/scripts" || return 1
    local image_commit=""
    image_commit="$(release_image_revision_commit "${image}" 2>/dev/null || true)"
    if [ -n "${image_commit}" ]; then
        if ! record_release_source_commit \
                "${image_commit}" "image:${image}:org.opencontainers.image.revision"; then
            return 1
        fi
        emit_info fetch_release source_commit "${image_commit}"
    elif [ -n "${RELEASE_EXPECTED_COMMIT:-}" ]; then
        log_warn "[fetch_release] ${image} 缺少有效 OCI revision，不能证明 release 源码。"
        return 1
    fi
    return 0
}

sync_main_fallback_release() {
    local source_dir="" source_ref="" extracted_dir=""
    local staged_release="${NEW_RELEASE}.main.$$"
    RELEASE_SOURCE_COMMIT=""
    RELEASE_SOURCE_COMMIT_PROOF=""
    RELEASE_EXPECTED_COMMIT=""
    RELEASE_SOURCE_API_IMAGE=""
    discard_release_source_manifest_cache
    extracted_dir="${ROOT}/.update-main-source.$$"
    rm -rf "${staged_release}" "${extracted_dir}" 2>/dev/null || true

    if try_image_extract_release main "${extracted_dir}"; then
        source_dir="${extracted_dir}"
        RELEASE_SOURCE_IMAGE_EXTRACT=1
        emit_info fetch_release fallback_source "image_extract_main"
    elif [ -d "${REPO_DIR}/.git" ]; then
        if ! (cd "${REPO_DIR}" && git fetch --quiet origin main); then
            log_error "[fetch_release] fallback main 源码 fetch 失败。"
            rm -rf "${extracted_dir}" 2>/dev/null || true
            return 1
        fi
        source_dir="${REPO_DIR}"
        source_ref="$(cd "${REPO_DIR}" \
            && git rev-parse --verify "refs/remotes/origin/main^{commit}")"
        if ! record_release_source_commit "${source_ref}" "git:refs/remotes/origin/main"; then
            log_error "[fetch_release] fallback main 源码 commit 无效。"
            rm -rf "${extracted_dir}" 2>/dev/null || true
            return 1
        fi
        RELEASE_SOURCE_IMAGE_EXTRACT=0
        emit_info fetch_release fallback_source "git_origin_main@${source_ref}"
    else
        log_error "[fetch_release] 镜像已回退 main，但无法取得匹配的 main 源码。"
        rm -rf "${extracted_dir}" 2>/dev/null || true
        return 1
    fi

    mkdir -p "${staged_release}"
    if ! sync_repo_to_release \
            "${source_dir}" "${staged_release}" "${source_ref}"; then
        rm -rf "${staged_release}" "${extracted_dir}" 2>/dev/null || true
        return 1
    fi
    ln -sfn "${SHARED_ENV}" "${staged_release}/.env"
    if ! lumen_safe_rm_rf "${NEW_RELEASE}" \
            || ! mv "${staged_release}" "${NEW_RELEASE}"; then
        log_error "[fetch_release] 用 main 源码替换待发布 release 失败。"
        rm -rf "${staged_release}" "${extracted_dir}" 2>/dev/null || true
        return 1
    fi

    RELEASE_SOURCE_REF="${source_ref}"
    TARGET_RELEASE_TAG=""
    RELEASE_MANIFEST_FILE=""
    RELEASE_MANIFEST_TAG=""
    rm -rf "${extracted_dir}" 2>/dev/null || true
    log_info "[fetch_release] 待发布源码已同步到 main，与 fallback 镜像一致。"
    return 0
}

enable_local_build_fallback() {
    if [ "${LUMEN_UPDATE_BUILD:-0}" != "1" ]; then
        log_warn "[fetch_release] GHCR 镜像不可用，自动启用本地 build 继续。"
    fi
    LUMEN_UPDATE_BUILD=1
    export LUMEN_UPDATE_BUILD
    emit_info fetch_release build_fallback "local"
}
