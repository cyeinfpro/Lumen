#!/usr/bin/env bash
# Docker compose runtime helpers for lumenctl.sh.

lumenctl_compose_workdir() {
    if [ -d "${ROOT}/current" ]; then
        printf '%s' "${ROOT}/current"
        return 0
    fi
    if [ -d "${LUMEN_DEPLOY_ROOT}/current" ]; then
        printf '%s' "${LUMEN_DEPLOY_ROOT}/current"
        return 0
    fi
    if [ -f "${ROOT}/docker-compose.yml" ]; then
        printf '%s' "${ROOT}"
        return 0
    fi
    if [ -f "${LUMEN_DEPLOY_ROOT}/docker-compose.yml" ]; then
        printf '%s' "${LUMEN_DEPLOY_ROOT}"
        return 0
    fi
    return 1
}
lumenctl_compose() {
    local workdir
    if ! workdir="$(lumenctl_compose_workdir)"; then
        log_error "找不到 docker-compose.yml；预期位置：${ROOT}/current 或 ${LUMEN_DEPLOY_ROOT}/current"
        return 1
    fi
    lumen_compose_in "${workdir}" "$@"
}

lumen_compose_status() {
    lumen_require_docker_access
    log_step "docker compose ps（project=${LUMEN_COMPOSE_PROJECT}）"
    lumenctl_compose ps || true
    printf '\n---\n'
    log_step "容器健康状态"
    local cn state
    for cn in lumen-api lumen-worker lumen-web lumen-pg lumen-redis lumen-tgbot; do
        state="$(lumen_docker inspect --format '{{.Name}} {{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "${cn}" 2>/dev/null || true)"
        if [ -n "${state}" ]; then
            printf '  %s\n' "${state#/}"
        fi
    done
    printf '\n---\n'
    log_step "本地健康检查"
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS --noproxy '*' --max-time 8 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
            log_info "API healthz: OK"
        else
            log_warn "API healthz: 失败 (http://127.0.0.1:8000/healthz)"
        fi
        if curl -fsS --noproxy '*' --max-time 8 -o /dev/null http://127.0.0.1:3000/ >/dev/null 2>&1; then
            log_info "Web /: OK"
        else
            log_warn "Web /: 失败 (http://127.0.0.1:3000/)"
        fi
    else
        log_warn "未安装 curl，跳过 HTTP 健康检查。"
    fi
}

_LUMENCTL_VALID_SERVICES="api worker web tgbot postgres redis migrate bootstrap"

lumen_compose_logs() {
    lumen_require_docker_access
    local service="${1:-api}"
    # 校验 service 名，避免用户敲错（例如 lumen-api 而非 api）后看到困惑的
    # docker compose 错误。
    case " ${_LUMENCTL_VALID_SERVICES} " in
        *" ${service} "*) ;;
        *)
            log_error "无效服务名：'${service}'"
            log_error "  可用：${_LUMENCTL_VALID_SERVICES}"
            log_error "  注意：docker 容器名是 'lumen-api' 等，但 logs 命令的参数是 service 名（不带 lumen- 前缀）。"
            exit 1
            ;;
    esac
    log_step "docker compose logs -f --tail=200 ${service}"
    lumenctl_compose logs -f --tail=200 "${service}"
}

lumen_compose_restart() {
    lumen_require_docker_access
    log_step "docker compose up -d --force-recreate api worker web"
    lumenctl_compose up -d --force-recreate api worker web
}

lumen_compose_stop() {
    lumen_require_docker_access
    log_step "docker compose stop api worker web tgbot"
    # tgbot 走 profile，stop 时不在默认范围；显式指定即可，未运行也是 noop。
    lumenctl_compose stop api worker web tgbot || true
}

lumen_compose_start() {
    lumen_require_docker_access
    log_step "docker compose up -d --wait api worker web"
    lumenctl_compose up -d --wait api worker web
}

lumen_compose_migrate() {
    lumen_require_docker_access
    log_step "docker compose --profile migrate run --rm migrate"
    lumenctl_compose --profile migrate run --rm migrate
}

lumen_compose_bootstrap() {
    lumen_require_docker_access
    if [ -z "${LUMEN_ADMIN_EMAIL:-}" ] || [ -z "${LUMEN_ADMIN_PASSWORD:-}" ]; then
        log_error "bootstrap 需要 LUMEN_ADMIN_EMAIL 与 LUMEN_ADMIN_PASSWORD 环境变量。"
        log_error "示例：LUMEN_ADMIN_EMAIL=admin@example.com LUMEN_ADMIN_PASSWORD='...' bash scripts/lumenctl.sh bootstrap"
        exit 1
    fi
    log_step "docker compose --profile bootstrap run --rm bootstrap"
    lumenctl_compose --profile bootstrap run --rm \
        -e LUMEN_ADMIN_EMAIL="${LUMEN_ADMIN_EMAIL}" \
        -e LUMEN_ADMIN_PASSWORD="${LUMEN_ADMIN_PASSWORD}" \
        bootstrap python -m app.scripts.bootstrap "${LUMEN_ADMIN_EMAIL}" --role admin --password "${LUMEN_ADMIN_PASSWORD}"
}

lumen_env_migrate_file() {
    local mode="$1"
    local env_file="${2:-}"
    if [ -z "${env_file}" ]; then
        if [ -f "${ROOT}/shared/.env" ]; then
            env_file="${ROOT}/shared/.env"
        elif [ -f "${ROOT}/current/.env" ]; then
            env_file="${ROOT}/current/.env"
        elif [ -f "${LUMEN_DEPLOY_ROOT}/shared/.env" ]; then
            env_file="${LUMEN_DEPLOY_ROOT}/shared/.env"
        elif [ -f "${ROOT}/.env" ]; then
            env_file="${ROOT}/.env"
        else
            log_error "找不到 .env；请显式传入路径：bash scripts/lumenctl.sh migrate-env /path/to/.env"
            exit 1
        fi
    fi
    log_step "迁移容器内 URL (${mode})"
    lumen_migrate_container_urls "${env_file}" "${mode}"
}

lumen_compose_backup() {
    run_lumen_script backup.sh "$@"
}

lumen_compose_restore() {
    if [ "$#" -lt 1 ] || [ -z "${1:-}" ]; then
        log_error "restore 需要一个 timestamp 参数（形如 20260424-123000）。"
        log_error "用法：bash scripts/lumenctl.sh restore <timestamp>"
        exit 1
    fi
    # restore 是不可逆操作：DROP database + 覆盖 redis volume。LUMEN_NONINTERACTIVE=1
    # 或 LUMEN_RESTORE_YES=1 才跳过确认（自动化场景）；其余都要人工确认 timestamp。
    if [ "${LUMEN_NONINTERACTIVE:-}" != "1" ] && [ "${LUMEN_RESTORE_YES:-}" != "1" ]; then
        printf '\n'
        log_warn "restore $1 将："
        log_warn "  1) 停止 lumen-api / lumen-worker"
        log_warn "  2) DROP 现有数据库并从 backup/pg/$1.pg.dump.gz 恢复"
        log_warn "  3) 覆盖 redis volume 数据"
        log_warn "此操作不可逆，请确认 timestamp 正确。"
        if ! confirm "继续 restore $1？"; then
            log_info "已取消。"
            exit 0
        fi
    fi
    run_lumen_script restore.sh "$@"
}

# Rollback：持有维护锁 + update 锁，事务化切回 previous release。
_lumen_compose_rollback_locked() {
    local deploy_root="$1"
    if [ ! -L "${deploy_root}/previous" ]; then
        log_error "${deploy_root}/previous 不存在，无法自动 rollback。"
        return 1
    fi

    local current_target previous_target old_id old_dir old_tag old_version
    local shared_env="${deploy_root}/shared/.env"
    current_target="$(readlink "${deploy_root}/current" 2>/dev/null || true)"
    previous_target="$(readlink "${deploy_root}/previous" 2>/dev/null || true)"
    old_id="$(basename "${previous_target}")"
    old_dir="${deploy_root}/releases/${old_id}"
    if [ -z "${current_target}" ] || [ -z "${old_id}" ] || [ ! -d "${old_dir}" ]; then
        log_error "无法解析 rollback 的 current/previous release。"
        return 1
    fi
    if [ ! -f "${shared_env}" ]; then
        log_error "${shared_env} 不存在，无法保证 rollback 配置一致性。"
        return 1
    fi
    old_tag="$(head -n1 "${old_dir}/.image-tag" 2>/dev/null | tr -d '[:space:]' || true)"
    old_version="$(head -n1 "${old_dir}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
    if [ -z "${old_tag}" ] || [ -z "${old_version}" ]; then
        log_error "rollback 目标缺少 .image-tag 或 VERSION，拒绝产生源码/镜像/版本错位。"
        return 1
    fi

    if [ "${LUMEN_NONINTERACTIVE:-}" != "1" ] \
            && [ "${LUMEN_ROLLBACK_YES:-}" != "1" ]; then
        printf '\n'
        log_warn "rollback 将切换到 release ${old_id}（镜像 tag=${old_tag}, version=${old_version}），并重启 api/worker/web。"
        if ! confirm "继续 rollback？"; then
            log_info "已取消。"
            return 0
        fi
    fi

    local env_snapshot
    env_snapshot="$(mktemp "${deploy_root}/shared/.env.rollback.XXXXXX")" \
        || return 1
    if ! cp -p "${shared_env}" "${env_snapshot}"; then
        rm -f "${env_snapshot}" 2>/dev/null || true
        return 1
    fi

    local switched=0 rollback_rc=0
    log_step "rollback 到 release ${old_id}"
    log_info "rollback 目标镜像 tag：${old_tag}"
    log_info "rollback 目标版本：${old_version}"
    if ! lumen_set_image_tag_in_env "${shared_env}" "${old_tag}" \
            || ! lumen_set_env_value_in_file \
                "${shared_env}" LUMEN_VERSION "${old_version}"; then
        rollback_rc=1
    elif ! lumen_release_atomic_switch "${deploy_root}" "${old_id}"; then
        rollback_rc=1
    else
        switched=1
        if [ -f "${deploy_root}/current/VERSION" ]; then
            ln -sfn current/VERSION "${deploy_root}/VERSION" 2>/dev/null \
                || cp "${deploy_root}/current/VERSION" "${deploy_root}/VERSION"
        fi
        log_info "current 已切回 releases/${old_id}"
        log_step "docker compose pull"
        lumen_compose_in "${deploy_root}/current" pull \
            || log_warn "compose pull 返回非零，将继续 up 使用本地旧镜像兜底"
        log_step "docker compose up -d --wait api worker web"
        if ! lumen_compose_in "${deploy_root}/current" \
                up --pull missing -d --wait api worker web; then
            rollback_rc=1
        fi
    fi

    if [ "${rollback_rc}" -eq 0 ]; then
        rm -f "${env_snapshot}"
        return 0
    fi

    log_error "rollback 失败，恢复执行前的 env 与 symlink 状态。"
    local restore_tmp="${deploy_root}/shared/.env.restore.$$" restore_ok=1
    if ! cp -p "${env_snapshot}" "${restore_tmp}" \
            || ! mv -f "${restore_tmp}" "${shared_env}"; then
        rm -f "${restore_tmp}" 2>/dev/null || true
        restore_ok=0
        log_error "shared/.env 原字节恢复失败；快照保留在 ${env_snapshot}。"
    fi
    if [ "${switched}" -eq 1 ]; then
        lumen_atomic_replace_symlink \
            "${current_target}" "${deploy_root}/current" || restore_ok=0
        lumen_atomic_replace_symlink \
            "${previous_target}" "${deploy_root}/previous" || restore_ok=0
    fi
    if [ -f "${deploy_root}/current/VERSION" ]; then
        ln -sfn current/VERSION "${deploy_root}/VERSION" 2>/dev/null \
            || cp "${deploy_root}/current/VERSION" "${deploy_root}/VERSION"
    fi
    if [ "${restore_ok}" -eq 1 ]; then
        rm -f "${env_snapshot}"
        log_warn "rollback 前状态已恢复，尝试重新拉起原 release 核心服务。"
        lumen_compose_in "${deploy_root}/current" \
            up --pull missing -d --wait api worker web \
            || log_error "原 release 核心服务恢复失败，请人工检查 compose 日志。"
    fi
    return 1
}

lumen_compose_rollback() {
    if [ "$(detect_os)" = "linux" ] \
            && [ "${EUID:-$(id -u)}" -ne 0 ] \
            && [ "${LUMEN_ROLLBACK_PRIVILEGED:-0}" != "1" ]; then
        ensure_cmd sudo "rollback 需要 root 权限以持有维护锁并原子切换 release。"
        lumen_sudo env \
            LUMEN_ROLLBACK_PRIVILEGED=1 \
            LUMEN_LUMENCTL_SELF_UPDATE=0 \
            LUMEN_SELF_UPDATE=0 \
            LUMEN_NONINTERACTIVE="${LUMEN_NONINTERACTIVE:-0}" \
            LUMEN_ROLLBACK_YES="${LUMEN_ROLLBACK_YES:-0}" \
            LUMEN_DEPLOY_ROOT="${LUMEN_DEPLOY_ROOT}" \
            LUMEN_BACKUP_ROOT="${LUMEN_BACKUP_ROOT}" \
            COMPOSE_PROJECT_NAME="${LUMEN_COMPOSE_PROJECT}" \
            bash "${SCRIPT_DIR}/lumenctl.sh" rollback "$@"
        return $?
    fi

    lumen_require_docker_access
    local deploy_root
    if [ -L "${ROOT}/current" ]; then
        deploy_root="${ROOT}"
    elif [ -L "${LUMEN_DEPLOY_ROOT}/current" ]; then
        deploy_root="${LUMEN_DEPLOY_ROOT}"
    else
        log_error "找不到 release 布局的 current symlink；rollback 仅适用于 release 布局。"
        return 1
    fi

    lumen_acquire_lock "${deploy_root}" "lumenctl rollback"
    local operation_id rc=0
    operation_id="rollback-$(date -u +%Y%m%d-%H%M%S)-$$"
    lumen_with_lock "${operation_id}" 1830 \
        _lumen_compose_rollback_locked "${deploy_root}" || rc=$?
    lumen_release_lock
    return "${rc}"
}

lumen_compose_version() {
    log_step "Lumen 版本信息"
    local version_file=""
    if [ -f "${ROOT}/current/VERSION" ]; then
        version_file="${ROOT}/current/VERSION"
    elif [ -f "${ROOT}/VERSION" ]; then
        version_file="${ROOT}/VERSION"
    fi
    if [ -n "${version_file}" ]; then
        printf 'VERSION:        %s\n' "$(head -n1 "${version_file}" | tr -d '[:space:]')"
    else
        printf 'VERSION:        (unknown)\n'
    fi

    local env_file=""
    if [ -f "${ROOT}/current/.env" ]; then
        env_file="${ROOT}/current/.env"
    elif [ -f "${ROOT}/.env" ]; then
        env_file="${ROOT}/.env"
    elif [ -f "${LUMEN_DEPLOY_ROOT}/shared/.env" ]; then
        env_file="${LUMEN_DEPLOY_ROOT}/shared/.env"
    fi
    if [ -n "${env_file}" ]; then
        local tag
        tag="$(lumen_env_value LUMEN_IMAGE_TAG "${env_file}")"
        printf 'IMAGE_TAG:      %s\n' "${tag:-(default)}"
        local registry
        registry="$(lumen_env_value LUMEN_IMAGE_REGISTRY "${env_file}")"
        printf 'IMAGE_REGISTRY: %s\n' "${registry:-ghcr.io/cyeinfpro}"
    fi

    if command -v git >/dev/null 2>&1 && [ -d "${ROOT}/.git" ]; then
        printf 'GIT_SHA:        %s\n' "$(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    elif [ -f "${ROOT}/current/.lumen_release.json" ] && command -v python3 >/dev/null 2>&1; then
        printf 'GIT_SHA:        %s\n' "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("git_sha","unknown"))' "${ROOT}/current/.lumen_release.json" 2>/dev/null || echo unknown)"
    fi

    if [ -L "${ROOT}/current" ]; then
        printf 'CURRENT_LINK:   %s -> %s\n' "${ROOT}/current" "$(readlink "${ROOT}/current" 2>/dev/null || true)"
    fi
}
