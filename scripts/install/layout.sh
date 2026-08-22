#!/usr/bin/env bash
# Data directories, release layout, shared environment, and image probing.
# Sourced by scripts/install.sh after raw bootstrap has completed.

# ---------------------------------------------------------------------------
# B. 准备数据目录与权限（§15.2 + §17.0）
# LUMEN_DB_ROOT 承载 postgres/redis；LUMEN_DATA_ROOT 承载 storage/backup。
# 未显式设置 LUMEN_DB_ROOT 时保持旧行为：两者使用同一个根。
# ---------------------------------------------------------------------------
prepare_data_dirs() {
    emit_step_start prepare "准备数据目录与权限（data=${LUMEN_DATA_ROOT}, db=${LUMEN_DB_ROOT}）"
    local data_root="${LUMEN_DATA_ROOT}"
    local db_root="${LUMEN_DB_ROOT}"
    local app_uid="${LUMEN_APP_UID:-10001}"
    local app_storage_gid="${LUMEN_APP_STORAGE_GID:-${LUMEN_APP_GID:-10001}}"
    local postgres_uid="${LUMEN_POSTGRES_UID:-999}"
    local postgres_gid="${LUMEN_POSTGRES_GID:-999}"
    local redis_uid="${LUMEN_REDIS_UID:-999}"
    local redis_gid="${LUMEN_REDIS_GID:-999}"

    if [ -e "${data_root}" ] && [ ! -d "${data_root}" ]; then
        log_error "${data_root} 已存在但不是目录，请先移走或删除后重试。"
        exit 1
    fi
    if [ -e "${db_root}" ] && [ ! -d "${db_root}" ]; then
        log_error "${db_root} 已存在但不是目录，请先移走或删除后重试。"
        exit 1
    fi

    lumen_run_as_root mkdir -p "${db_root}" \
        "${db_root}/postgres" \
        "${db_root}/redis" \
        "${data_root}" \
        "${data_root}/storage" \
        "${data_root}/backup" \
        "${data_root}/backup/pg" \
        "${data_root}/backup/redis" || {
        log_error "无法创建数据目录。请确认当前用户有 sudo 权限。"
        exit 1
    }

    # 顶层 root:root 755（不递归）；CIFS/NAS 场景可能不支持，允许继续。
    lumen_run_as_root chown root:root "${data_root}" "${db_root}" \
        || log_warn "chown root:root 数据根失败（已忽略，子目录单独 chown）"
    lumen_run_as_root chmod 755 "${data_root}" "${db_root}" \
        || log_warn "chmod 755 数据根失败（已忽略）"

    # 按服务分别 chown（禁止整体 chown 给所有目录 —— §15.2）
    lumen_run_as_root chown -R "${postgres_uid}:${postgres_gid}" "${db_root}/postgres" || {
        log_error "chown postgres 数据目录失败。"
        exit 1
    }
    lumen_run_as_root chown -R "${redis_uid}:${redis_gid}" "${db_root}/redis" || {
        log_error "chown redis 数据目录失败。"
        exit 1
    }
    lumen_run_as_root chown -R "${app_uid}:${app_storage_gid}" "${data_root}/storage" "${data_root}/backup" || {
        log_error "chown storage/backup 数据目录失败。"
        exit 1
    }

    lumen_run_as_root chmod 700 "${db_root}/postgres" "${db_root}/redis" \
        || log_warn "chmod 700 postgres/redis 失败（已忽略，但容器可能因权限问题起不来）"
    lumen_run_as_root chmod 750 "${data_root}/storage" "${data_root}/backup" \
        || log_warn "chmod 750 storage/backup 失败（已忽略，但 api/worker 可能写不进去）"

    log_info "数据目录权限设置完成（postgres=${postgres_uid}:${postgres_gid}, redis=${redis_uid}:${redis_gid}；storage/backup 在 ${data_root}）。"
    emit_info "key=data_root" "value=${data_root}"
    emit_info "key=db_root" "value=${db_root}"
    emit_step_done
}

_sed_replacement_escape() {
    printf '%s' "$1" | sed 's/[\/&#]/\\&/g'
}

_render_systemd_unit_template() {
    local src="$1"
    local dst="$2"
    local data_root="$3"
    local backup_root="$4"
    local deploy_root="$5"
    local data_root_esc backup_root_esc deploy_root_esc
    data_root_esc="$(_sed_replacement_escape "${data_root}")"
    backup_root_esc="$(_sed_replacement_escape "${backup_root}")"
    deploy_root_esc="$(_sed_replacement_escape "${deploy_root}")"

    sed \
        -e 's#/opt/lumendata/backup#__LUMEN_BACKUP_ROOT__#g' \
        -e 's#/opt/lumendata#__LUMEN_DATA_ROOT__#g' \
        -e 's#/opt/lumen#__LUMEN_DEPLOY_ROOT__#g' \
        "${src}" \
        | sed \
            -e "s#__LUMEN_BACKUP_ROOT__#${backup_root_esc}#g" \
            -e "s#__LUMEN_DATA_ROOT__#${data_root_esc}#g" \
            -e "s#__LUMEN_DEPLOY_ROOT__#${deploy_root_esc}#g" \
        > "${dst}"
}

_render_update_runner_units() {
    local src_path="$1"
    local src_runner="$2"
    local out_dir="$3"
    local data_root="$4"
    local backup_root="$5"
    local deploy_root="$6"

    _render_systemd_unit_template \
        "${src_path}" \
        "${out_dir}/lumen-update.path" \
        "${data_root}" \
        "${backup_root}" \
        "${deploy_root}"
    _render_systemd_unit_template \
        "${src_runner}" \
        "${out_dir}/lumen-update-runner.service" \
        "${data_root}" \
        "${backup_root}" \
        "${deploy_root}"
    local src_dir src_warm_path src_warm_service
    src_dir="$(dirname "${src_path}")"
    src_warm_path="${src_dir}/lumen-update-warm.path"
    src_warm_service="${src_dir}/lumen-update-warm.service"
    if [ -f "${src_warm_path}" ]; then
        _render_systemd_unit_template \
            "${src_warm_path}" \
            "${out_dir}/lumen-update-warm.path" \
            "${data_root}" \
            "${backup_root}" \
            "${deploy_root}"
    fi
    if [ -f "${src_warm_service}" ]; then
        _render_systemd_unit_template \
            "${src_warm_service}" \
            "${out_dir}/lumen-update-warm.service" \
            "${data_root}" \
            "${backup_root}" \
            "${deploy_root}"
    fi
    local backup_unit
    for backup_unit in lumen-backup.service lumen-backup.timer lumen-backup.path \
            lumen-restore-runner.service lumen-restore.path; do
        if [ -f "${src_dir}/${backup_unit}" ]; then
            _render_systemd_unit_template \
                "${src_dir}/${backup_unit}" \
                "${out_dir}/${backup_unit}" \
                "${data_root}" \
                "${backup_root}" \
                "${deploy_root}"
        fi
    done
}

resolve_install_source_commit() {
    INSTALL_SOURCE_COMMIT=""
    INSTALL_SOURCE_COMMIT_PROOF=""
    if [ ! -d "${ROOT}/.git" ] || ! command -v git >/dev/null 2>&1; then
        return 0
    fi
    local commit="" untracked=""
    commit="$(git -C "${ROOT}" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true)"
    if [[ ! "${commit}" =~ ^[0-9a-f]{40}$ ]]; then
        return 0
    fi
    INSTALL_SOURCE_COMMIT="${commit}"
    INSTALL_SOURCE_COMMIT_PROOF="git-dirty"
    if ! git -C "${ROOT}" diff --quiet HEAD -- \
            || ! git -C "${ROOT}" diff --cached --quiet HEAD --; then
        return 0
    fi
    untracked="$(
        git -C "${ROOT}" ls-files --others --exclude-standard 2>/dev/null \
            | sed \
                -e '/^\.lumen-maintenance\.lock$/d' \
                -e '/^\.lumen-maintenance\.lock\.d\//d' \
                -e '/^\.lumen-script\.lock\//d' \
                -e '/^scripts\.lumen-self-update\.lock$/d' \
                -e '/^\.install-transaction\//d' \
                -e '/^\.install-logs\//d' \
                -e '/^\.update\.log$/d'
    )"
    if [ -z "${untracked}" ]; then
        INSTALL_SOURCE_COMMIT_PROOF="git-clean"
    fi
}

# ---------------------------------------------------------------------------
# C. 准备 release 布局
#   ${LUMEN_DEPLOY_ROOT}/
#     releases/<id>/      <- 当前 release，rsync 整个仓库进来
#     shared/.env         <- 跨 release 持久化的密钥与配置
#     current -> releases/<id>
# ---------------------------------------------------------------------------
prepare_release_layout() {
    emit_step_start prepare "准备 release 布局（${DEPLOY_ROOT}）"

    resolve_install_source_commit

    # 决定 release id：UTC 时间戳 + 可选 git short sha
    local release_id sha=""
    if [ -d "${ROOT}/.git" ] && command -v git >/dev/null 2>&1; then
        sha="$(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || true)"
    fi
    if command -v lumen_release_id >/dev/null 2>&1; then
        release_id="$(lumen_release_id "${sha:-unknown}")"
    else
        release_id="$(date -u +%Y%m%dT%H%M%SZ)-${sha:-unknown}"
    fi

    RELEASE_ID="${release_id}"
    RELEASE_DIR="${DEPLOY_ROOT}/releases/${release_id}"
    SHARED_DIR="${DEPLOY_ROOT}/shared"

    if ! lumen_release_shared_env_path_safe "${DEPLOY_ROOT}"; then
        log_error "shared 路径不安全，拒绝准备 release 布局。"
        exit 78
    fi
    # 创建顶层 + releases + shared
    lumen_run_as_root mkdir -p "${DEPLOY_ROOT}/releases" "${SHARED_DIR}" || {
        log_error "无法创建部署目录 ${DEPLOY_ROOT}。请确认 sudo 权限。"
        exit 1
    }
    if ! lumen_release_shared_env_path_safe "${DEPLOY_ROOT}"; then
        log_error "shared 路径在创建后发生变化，拒绝继续安装。"
        exit 78
    fi
    if ! install_transaction_begin; then
        log_error "无法创建 durable fresh-install journal。"
        exit 1
    fi
    install_transaction_failpoint layout
    # DEPLOY_ROOT 写权限给当前用户（compose 要从 RELEASE_DIR 读 docker-compose.yml）
    if [ ! -w "${DEPLOY_ROOT}" ]; then
        lumen_run_as_root chown "$(id -un):$(id -gn)" "${DEPLOY_ROOT}" "${DEPLOY_ROOT}/releases" "${SHARED_DIR}" 2>/dev/null \
            || log_warn "chown ${DEPLOY_ROOT} 失败（已忽略，rsync 可能因权限失败）"
    fi

    if [ -e "${RELEASE_DIR}" ] && [ "$(ls -A "${RELEASE_DIR}" 2>/dev/null | head -1)" ]; then
        # 加了 PID 后缀后同秒冲突理论上不会发生；非空 = 上次失败留下的半成品
        # 或两个 shell 同时跑（lumen_acquire_lock 应已挡住，但兜底）。fail-fast
        # 比 rsync 不带 --delete 留半新半旧文件、再产生诡异问题更好。
        # 紧急绕过：LUMEN_INSTALL_OVERWRITE_RELEASE=1（手动确认要覆盖）。
        if [ "${LUMEN_INSTALL_OVERWRITE_RELEASE:-0}" != "1" ]; then
            log_error "release 目录已存在且非空：${RELEASE_DIR}"
            log_error "  说明：上次 install 中途失败留下半成品，或两个 install 并发。"
            log_error "  排查：ls -la ${RELEASE_DIR}"
            log_error "  清理：sudo rm -rf '${RELEASE_DIR}' 然后重跑 install"
            log_error "  或显式覆盖：LUMEN_INSTALL_OVERWRITE_RELEASE=1 bash scripts/install.sh --install"
            exit 1
        fi
        log_warn "release 目录已存在且非空：${RELEASE_DIR}（OVERWRITE_RELEASE=1，覆盖式继续）"
    fi
    mkdir -p "${RELEASE_DIR}" 2>/dev/null || lumen_run_as_root mkdir -p "${RELEASE_DIR}"
    if [ ! -w "${RELEASE_DIR}" ]; then
        lumen_run_as_root chown -R "$(id -un):$(id -gn)" "${RELEASE_DIR}" 2>/dev/null \
            || log_warn "chown ${RELEASE_DIR} 失败（已忽略，rsync 可能因权限失败）"
    fi

    # 把当前仓库内容 rsync 到 release 目录（保留 release 布局，§11.1）
    # check_prerequisites 已经会自动装 rsync；这里保留兜底，便于直接调用本函数
    # （或老版本 install.sh 跳过 prepare 时）也能自愈。
    if ! command -v rsync >/dev/null 2>&1; then
        log_warn "缺少 rsync，尝试自动安装。"
        if ! _auto_install_basics rsync; then
            log_error "缺少 rsync 且自动安装失败；无法把仓库内容复制到 release 目录。"
            log_error "  Debian/Ubuntu：sudo apt install rsync"
            log_error "  RHEL/Alma：sudo dnf install rsync"
            log_error "  macOS：brew install rsync"
            exit 1
        fi
    fi
    log_info "rsync 仓库 → ${RELEASE_DIR}"
    rsync -a \
        --exclude='/.git/' \
        --exclude='/.env' \
        --exclude='/.env.local' \
        --exclude='/shared/' \
        --exclude='/releases/' \
        --exclude='/current' \
        --exclude='/previous' \
        --exclude='/var/' \
        --exclude='/.venv/' \
        --exclude='/node_modules/' \
        --exclude='/apps/worker/var/' \
        --exclude='/apps/web/.next/' \
        --exclude='/apps/web/node_modules/' \
        --exclude='/.lumen-script.lock/' \
        --exclude='/.lumen-maintenance.lock' \
        --exclude='/.lumen-maintenance.lock.d/' \
        --exclude='/scripts.lumen-self-update.lock' \
        --exclude='/.install-transaction/' \
        --exclude='/.update.log' \
        --exclude='/.install-logs/' \
        "${ROOT}/" "${RELEASE_DIR}/"
    if [ -n "${INSTALL_SOURCE_COMMIT}" ]; then
        printf '%s\n' "${INSTALL_SOURCE_COMMIT}" > "${RELEASE_DIR}/.source-commit"
        printf '%s\n' "${INSTALL_SOURCE_COMMIT_PROOF}" \
            > "${RELEASE_DIR}/.source-commit-proof"
    fi

    emit_info "key=release_id" "value=${release_id}"
    emit_info "key=release_dir" "value=${RELEASE_DIR}"
    emit_step_done
}

# ---------------------------------------------------------------------------
# D. 生成或合并 shared/.env
#   - 不存在：从 release 内的 .env.example 拷贝，然后 awk 替换 placeholder
#   - 存在：原样保留
#   - 写入 LUMEN_IMAGE_REGISTRY / LUMEN_IMAGE_TAG / LUMEN_VERSION / LUMEN_DATA_ROOT / LUMEN_DB_ROOT
#   - 在 release dir 创建 .env -> shared/.env 的相对 symlink，让 docker compose 自动读
# ---------------------------------------------------------------------------
prepare_env_file() {
    emit_step_start prepare "生成或合并 shared/.env"
    local shared_env="${SHARED_DIR}/.env"
    local example="${RELEASE_DIR}/.env.example"

    if ! lumen_release_shared_env_path_safe "${DEPLOY_ROOT}"; then
        log_error "shared/.env 不安全，拒绝生成或修改安装配置。"
        exit 78
    fi
    if [ ! -f "${example}" ]; then
        log_error "找不到 ${example}（仓库 .env.example 缺失？）"
        exit 1
    fi

    if [ ! -f "${shared_env}" ]; then
        log_info "shared/.env 不存在，从 .env.example 拷贝并生成强随机密钥。"
        cp "${example}" "${shared_env}"
        chmod 600 "${shared_env}"
        ensure_required_env_secrets "${shared_env}" || exit 1
        log_info "已写入数据库、会话、BYOK、Telegram 与 Agent 内部随机密钥。"
    else
        log_info "shared/.env 已存在，跳过密钥生成。"
        ensure_required_env_secrets "${shared_env}" || exit 1
        # 兜底：补齐 docker compose 必需的 DB_USER/DB_PASSWORD/DB_NAME
        lumen_ensure_compose_db_env_vars "${shared_env}" || exit 1
        case "${LUMEN_ENV_MIGRATE_CONTAINER_URLS:-dry-run}" in
            0|false|FALSE|False|no|NO|No|off|OFF|Off)
                log_info "跳过旧 .env 容器内 URL 检查（LUMEN_ENV_MIGRATE_CONTAINER_URLS=0）。"
                ;;
            apply|--apply)
                log_info "检查并迁移旧 .env 容器内 URL（白名单 + backup）。"
                lumen_migrate_container_urls "${shared_env}" --dry-run || exit 1
                lumen_migrate_container_urls "${shared_env}" --apply || exit 1
                ;;
            *)
                log_info "检查旧 .env 容器内 URL（白名单 dry-run，不落盘）。"
                local dry_run_output
                dry_run_output="$(lumen_migrate_container_urls "${shared_env}" --dry-run)" || {
                    printf '%s\n' "${dry_run_output:-}" >&2
                    exit 1
                }
                printf '%s\n' "${dry_run_output}"
                case "${dry_run_output}" in
                    *"dry-run only;"*)
                        log_error "检测到旧 .env 仍需要容器地址迁移；默认 dry-run 不落盘，安装已停止。"
                        log_error "请确认上方 diff 后执行："
                        log_error "  bash ${RELEASE_DIR}/scripts/lumenctl.sh migrate-env-apply ${shared_env}"
                        log_error "或显式：LUMEN_ENV_MIGRATE_CONTAINER_URLS=apply bash ${SCRIPT_DIR}/install.sh --install"
                        exit 1
                        ;;
                esac
                log_warn "如上方显示 DATABASE_URL/REDIS_URL 等变更，请确认后执行："
                log_warn "  bash ${RELEASE_DIR}/scripts/lumenctl.sh migrate-env-apply ${shared_env}"
                ;;
        esac
    fi

    # 写入/覆盖镜像与版本变量（每次安装都更新，便于 update.sh 读到一致 tag）
    local image_registry image_tag lumen_version
    image_registry="${LUMEN_IMAGE_REGISTRY:-ghcr.io/cyeinfpro}"
    image_tag="${INSTALL_IMAGE_TAG_OVERRIDE:-${LUMEN_IMAGE_TAG:-${LUMEN_INSTALL_RESOLVED_TAG:-latest}}}"
    if [ -f "${RELEASE_DIR}/VERSION" ]; then
        lumen_version="$(head -n1 "${RELEASE_DIR}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
    fi
    if [ -z "${lumen_version:-}" ] && [ -d "${ROOT}/.git" ] && command -v git >/dev/null 2>&1; then
        lumen_version="$(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || true)"
    fi
    lumen_version="${lumen_version:-unknown}"

    env_file_set "${shared_env}" LUMEN_IMAGE_REGISTRY "${image_registry}"
    env_file_set "${shared_env}" LUMEN_IMAGE_TAG      "${image_tag}"
    env_file_set "${shared_env}" LUMEN_VERSION        "${lumen_version}"
    env_file_set "${shared_env}" LUMEN_DATA_ROOT      "${LUMEN_DATA_ROOT}"
    env_file_set "${shared_env}" LUMEN_DB_ROOT        "${LUMEN_DB_ROOT}"
    env_file_set "${shared_env}" LUMEN_POSTGRES_UID   "${LUMEN_POSTGRES_UID}"
    env_file_set "${shared_env}" LUMEN_POSTGRES_GID   "${LUMEN_POSTGRES_GID}"
    env_file_set "${shared_env}" LUMEN_REDIS_UID      "${LUMEN_REDIS_UID}"
    env_file_set "${shared_env}" LUMEN_REDIS_GID      "${LUMEN_REDIS_GID}"
    env_file_set "${shared_env}" LUMEN_APP_UID        "${LUMEN_APP_UID}"
    env_file_set "${shared_env}" LUMEN_APP_GID        "${LUMEN_APP_GID}"
    env_file_set "${shared_env}" LUMEN_APP_STORAGE_GID "${LUMEN_APP_STORAGE_GID}"
    local current_web_bind_host current_expose_web_directly
    current_web_bind_host="$(env_file_get WEB_BIND_HOST "${shared_env}")"
    current_expose_web_directly="$(env_file_get LUMEN_EXPOSE_WEB_DIRECTLY "${shared_env}")"
    if [ -n "${LUMEN_WEB_BIND_HOST:-}" ]; then
        env_file_set "${shared_env}" WEB_BIND_HOST "${LUMEN_WEB_BIND_HOST}"
    elif lumen_env_truthy "${LUMEN_EXPOSE_WEB_DIRECTLY:-}" || lumen_env_truthy "${current_expose_web_directly}"; then
        env_file_set "${shared_env}" LUMEN_EXPOSE_WEB_DIRECTLY "1"
        env_file_set "${shared_env}" WEB_BIND_HOST "0.0.0.0"
        log_warn "LUMEN_EXPOSE_WEB_DIRECTLY=1：Web 将监听所有网卡 3000，请确认防火墙与生产 APP_ENV。"
    elif [ -z "${current_web_bind_host}" ] || [ "${current_web_bind_host}" = "0.0.0.0" ]; then
        if [ "${current_web_bind_host}" = "0.0.0.0" ]; then
            log_warn "WEB_BIND_HOST 是旧公开默认值 0.0.0.0，改为 127.0.0.1；如需直连公网，请设置 LUMEN_EXPOSE_WEB_DIRECTLY=1。"
        fi
        env_file_set "${shared_env}" WEB_BIND_HOST "127.0.0.1"
    fi

    # 创建 release/.env -> ../../shared/.env 的相对 symlink
    # docker compose 默认从 -f 所在目录加载 .env；让它读到 shared/.env。
    # 用 lumen_atomic_replace_symlink 替代 rm -f + ln -s 的两步操作，避免
    # 中间窗口（compose 在此瞬间读 .env 会拿到 ENOENT）。
    if command -v lumen_atomic_replace_symlink >/dev/null 2>&1; then
        lumen_atomic_replace_symlink "../../shared/.env" "${RELEASE_DIR}/.env"
    else
        if [ -e "${RELEASE_DIR}/.env" ] || [ -L "${RELEASE_DIR}/.env" ]; then
            rm -f "${RELEASE_DIR}/.env"
        fi
        ln -s "../../shared/.env" "${RELEASE_DIR}/.env"
    fi
    log_info "已 symlink ${RELEASE_DIR}/.env -> ../../shared/.env"

    # 友善提示：PUBLIC_BASE_URL / CORS_ALLOW_ORIGINS / NEXT_PUBLIC_API_BASE 保留默认
    local pub_url cors_url
    pub_url="$(env_file_get PUBLIC_BASE_URL "${shared_env}")"
    cors_url="$(env_file_get CORS_ALLOW_ORIGINS "${shared_env}")"
    if [[ "${pub_url}" == http://localhost* ]] || [[ "${cors_url}" == http://localhost* ]]; then
        log_warn "PUBLIC_BASE_URL / CORS_ALLOW_ORIGINS 仍是 localhost 默认值。"
        log_warn "  生产部署后请编辑 ${shared_env}，改成你的公网域名（例如 https://lumen.example.com）。"
        log_warn "  并在 nginx 配置正确的 server_name + 反代到 127.0.0.1:3000。"
    fi
    if lumen_configure_proxy_env "${shared_env}" >/dev/null 2>&1; then
        log_info "已配置更新/拉镜像代理（LUMEN_UPDATE_PROXY_URL / LUMEN_HTTP_PROXY / HTTP_PROXY）。"
        emit_info "key=proxy" "value=configured"
    fi

    emit_info "key=shared_env" "value=${shared_env}"
    emit_info "key=image_registry" "value=${image_registry}"
    emit_info "key=image_tag" "value=${image_tag}"
    emit_step_done
}

# ---------------------------------------------------------------------------
# E. 探测 GHCR 镜像可用性
# ---------------------------------------------------------------------------
probe_ghcr_image_tag() {
    emit_step_start prepare "探测 GHCR 镜像 tag 可用性"
    local shared_env="${SHARED_DIR}/.env"
    local registry tag api_url
    registry="$(env_file_get LUMEN_IMAGE_REGISTRY "${shared_env}")"
    tag="$(env_file_get LUMEN_IMAGE_TAG "${shared_env}")"

    # 只在默认 ghcr.io/cyeinfpro 路径下做探测；自定义 registry 直接信任用户配置
    if [[ "${registry}" != ghcr.io/cyeinfpro* ]]; then
        log_info "自定义镜像 registry=${registry}，跳过 GHCR tag 探测。"
        emit_step_done
        return 0
    fi

    # 用户显式 --image-tag 覆盖时不做 fallback（信任用户）
    if [ -n "${INSTALL_IMAGE_TAG_OVERRIDE}" ]; then
        log_info "已用 --image-tag=${INSTALL_IMAGE_TAG_OVERRIDE}，跳过 GHCR 探测。"
        emit_step_done
        return 0
    fi

    # --build 模式不需要远程镜像
    if [ "${INSTALL_BUILD_FLAG}" = "1" ]; then
        log_info "--build 模式，跳过 GHCR 探测（将本地构建镜像）。"
        emit_step_done
        return 0
    fi

    # GHCR public packages tags API（对未 token 也返回 200/404）
    api_url="https://ghcr.io/v2/cyeinfpro/lumen-api/tags/list"
    log_info "探测 ${api_url}（tag=${tag}）..."
    local resp http_code probe_file
    if ! probe_file="$(umask 077; mktemp "${TMPDIR:-/tmp}/lumen-ghcr-probe.XXXXXXXXXX" 2>/dev/null)"; then
        log_warn "无法创建安全的 GHCR 探测临时文件；跳过预探测，pull 阶段仍会校验镜像。"
        emit_step_done
        return 0
    fi
    INSTALL_GHCR_PROBE_FILE="${probe_file}"
    chmod 0600 "${probe_file}" 2>/dev/null || {
        rm -f "${probe_file}" 2>/dev/null || true
        INSTALL_GHCR_PROBE_FILE=""
        log_warn "无法收紧 GHCR 探测临时文件权限；跳过预探测。"
        emit_step_done
        return 0
    }
    if http_code="$(curl -fsS -o "${probe_file}" -w '%{http_code}' --max-time 10 "${api_url}" 2>/dev/null)"; then
        :
    else
        http_code="000"
    fi
    resp="$(cat "${probe_file}" 2>/dev/null || true)"
    rm -f "${probe_file}" 2>/dev/null || true
    INSTALL_GHCR_PROBE_FILE=""

    if [ "${http_code}" = "200" ] && printf '%s' "${resp}" | grep -q "\"${tag}\""; then
        log_info "GHCR 上存在 tag=${tag}，使用配置值。"
    elif [ "${http_code}" = "200" ]; then
        # 探测到 tags 列表但缺 ${tag}：stable 默认 fail-closed；只有显式
        # LUMEN_INSTALL_FALLBACK_MAIN=1 才允许回退 rolling main。
        if [ "${LUMEN_INSTALL_FALLBACK_MAIN:-0}" = "1" ] \
                && printf '%s' "${resp}" | grep -q '"main"'; then
            log_warn "GHCR 上未找到 tag=${tag}，回退到 main。v1.0.0 发布后请改回 latest。"
            if ! env_file_set "${shared_env}" LUMEN_IMAGE_TAG "main" \
                    || ! lumen_env_file_append_line_if_missing \
                        "${shared_env}" \
                        "# install.sh: fallback to main; v1.0.0 发布后改回 latest"; then
                log_error "无法安全记录 fallback main 配置，拒绝继续安装。"
                exit 1
            fi
        else
            log_warn "GHCR 上未找到 tag=${tag}。stable 安装不会自动回退 main；保留配置，pull 时可能失败。"
            log_warn "如需 rolling main，请显式设置 LUMEN_IMAGE_TAG=main；如需本地构建，用 --build。"
        fi
    else
        # API 探测失败但 .env 已有 tag → 不动
        log_warn "GHCR API 探测失败（HTTP ${http_code}），保留 .env 配置 LUMEN_IMAGE_TAG=${tag}。"
    fi
    emit_step_done
}
