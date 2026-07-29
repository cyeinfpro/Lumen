#!/usr/bin/env bash
# Image acquisition, database initialization, and application startup.
# Sourced by scripts/install.sh after raw bootstrap has completed.

# ---------------------------------------------------------------------------
# F. 拉镜像 / 构建 -> 起 PG/Redis -> migrate -> bootstrap -> api/worker/web (+tgbot)
# ---------------------------------------------------------------------------
pull_or_build_images() {
    local shared_env="${SHARED_DIR}/.env"
    local registry current_tag release_manifest="" tgbot_image_ready=0
    registry="$(env_file_get LUMEN_IMAGE_REGISTRY "${shared_env}")"
    current_tag="$(env_file_get LUMEN_IMAGE_TAG "${shared_env}")"
    if [ "${INSTALL_BUILD_FLAG}" != "1" ] && [ "${current_tag}" = "latest" ] \
            && [ "${registry%/}" = "ghcr.io/cyeinfpro" ]; then
        local guard="${RELEASE_DIR}/scripts/release_manifest_guard.py"
        local resolved_tag=""
        resolved_tag="$(python3 "${guard}" latest-tag 2>/dev/null || true)"
        if ! lumen_release_manifest_required "${resolved_tag}"; then
            log_error "无法把 mutable latest 解析为正式 GitHub Release tag。"
            exit 1
        fi
        env_file_set "${shared_env}" LUMEN_IMAGE_TAG "${resolved_tag}" || exit 1
        current_tag="${resolved_tag}"
        export LUMEN_IMAGE_TAG="${resolved_tag}"
        log_info "已把 latest 固定为 ${resolved_tag}。"
    fi
    if [ "${INSTALL_BUILD_FLAG}" != "1" ] \
            && lumen_release_manifest_required "${current_tag}"; then
        if [ "${registry%/}" != "ghcr.io/cyeinfpro" ]; then
            if ! lumen_env_truthy "${LUMEN_ALLOW_UNVERIFIED_CUSTOM_REGISTRY:-0}"; then
                log_error "正式 release tag 使用自定义 registry 时无法核对官方 digest。"
                log_error "如确认镜像源可信，请显式设置 LUMEN_ALLOW_UNVERIFIED_CUSTOM_REGISTRY=1。"
                exit 1
            fi
            log_warn "已显式允许未核验的自定义 registry。"
        else
            release_manifest="${RELEASE_DIR}/release-manifest.json"
            if ! lumen_fetch_release_manifest "${current_tag}" "${release_manifest}"; then
                log_error "无法获取或校验 ${current_tag} 的 release-manifest.json。"
                exit 1
            fi
        fi
    fi

    if [ "${INSTALL_BUILD_FLAG}" = "1" ]; then
        emit_step_start containers "本地构建镜像（lumen_compose build）"
        # build 失败通常是 Dockerfile / 资源问题，重试 2 次（每次都是 from-scratch 的网络拉基础镜像）。
        if ! lumen_retry 2 5 "docker compose build" _install_compose build; then
            log_error "本地 docker compose build 失败。"
            exit 1
        fi
    else
        emit_step_start containers "拉取镜像（lumen_compose pull）"
        # 网络抖动是 pull 失败最常见的原因；先重试 3 次（指数退避 5/10/20），仍失败再走 fallback。
        if ! lumen_retry 3 5 "docker compose pull" _install_compose_pull_per_image; then
            if [ -z "${INSTALL_IMAGE_TAG_OVERRIDE}" ] \
                && [[ "${registry}" == ghcr.io/cyeinfpro* ]] \
                && [ "${current_tag}" != "main" ] \
                && [ "${LUMEN_INSTALL_FALLBACK_MAIN:-0}" = "1" ]; then
                log_warn "docker compose pull 失败，疑似默认镜像 tag=${current_tag} 尚未发布；回退到 main 后重试一次。"
                env_file_set "${shared_env}" LUMEN_IMAGE_TAG "main"
                current_tag="main"
                export LUMEN_IMAGE_TAG="${current_tag}"
                release_manifest=""
                if ! grep -q '^# install.sh: fallback to main after pull failure' "${shared_env}"; then
                    printf '\n# install.sh: fallback to main after pull failure; publish stable/latest then switch back\n' >> "${shared_env}"
                fi
                if lumen_retry 2 5 "docker compose pull (main fallback)" _install_compose_pull_per_image; then
                    log_info "已使用 LUMEN_IMAGE_TAG=main 拉取镜像。"
                else
                    log_error "docker compose pull 失败（fallback main 后仍失败）。"
                    log_error "  常见原因：1) 国内网络访问 ghcr 受阻 → 设置 LUMEN_HTTP_PROXY 或自托管 registry"
                    log_error "            2) main 镜像也未发布 → 使用 --build 本地构建"
                    exit 1
                fi
            else
                log_error "docker compose pull 失败。"
                log_error "  常见原因：1) 国内网络访问 ghcr 受阻 → 设置 LUMEN_HTTP_PROXY 或自托管 registry"
                log_error "            2) 镜像 tag 不存在 → 用 --image-tag=vX.Y.Z 钉死 tag 或 --build 本地构建"
                exit 1
            fi
        fi
        if env_key_present "${shared_env}" "TELEGRAM_BOT_TOKEN"; then
            if lumen_retry 2 5 "docker compose pull tgbot" \
                    _install_compose --profile tgbot pull tgbot; then
                tgbot_image_ready=1
            else
                log_warn "tgbot pull 失败，跳过 tgbot manifest 校验；主栈安装继续。"
            fi
        fi
        if [ -n "${release_manifest}" ]; then
            local manifest_args=(
                --service api
                --service worker
                --service web
            )
            if [ "${tgbot_image_ready}" -eq 1 ]; then
                manifest_args+=(--service tgbot)
            fi
            if ! lumen_verify_release_manifest_images \
                    "${release_manifest}" "${current_tag}" "${current_tag}" \
                    "${manifest_args[@]}"; then
                log_error "本地镜像 digest 未通过 release manifest 校验。"
                exit 1
            fi
        fi
    fi
    printf '%s\n' "${current_tag}" > "${RELEASE_DIR}/.image-tag"
    emit_step_done
}

start_infrastructure() {
    emit_step_start containers "启动 PostgreSQL / Redis 并等待健康"
    INSTALL_POSTGRES_DATA_PREEXISTING=0
    if postgres_data_initialized; then
        INSTALL_POSTGRES_DATA_PREEXISTING=1
    fi
    if ! _install_compose up --pull missing -d --wait postgres redis; then
        log_error "postgres / redis 启动或健康检查失败。"
        exit 1
    fi
    INSTALL_STARTED_SERVICES+=("postgres" "redis")
    log_info "PG / Redis 已健康。"
    sync_existing_postgres_password
    emit_step_done
}

sync_existing_postgres_password() {
    if [ "${INSTALL_POSTGRES_DATA_PREEXISTING:-0}" != "1" ]; then
        return 0
    fi

    local shared_env="${SHARED_DIR}/.env"
    local db_user db_name db_password
    db_user="$(env_file_get DB_USER "${shared_env}")"
    db_name="$(env_file_get DB_NAME "${shared_env}")"
    db_password="$(env_file_get DB_PASSWORD "${shared_env}")"
    if [ -z "${db_user}" ] || [ -z "${db_name}" ] || [ -z "${db_password}" ]; then
        log_error "shared/.env 缺少 DB_USER/DB_NAME/DB_PASSWORD，无法同步已有 Postgres 数据。"
        exit 1
    fi

    log_info "检测到已有 Postgres 数据目录，尝试同步数据库角色密码到当前 shared/.env。"
    if ! postgres_role_can_connect "${db_user}"; then
        if [ "${db_user}" = "lumen_app" ] && [ "${db_name}" = "lumen_app" ] \
                && postgres_role_can_connect "lumen" \
                && postgres_database_exists_for_role "lumen" "lumen"; then
            log_warn "PGDATA 使用旧默认 DB_USER=lumen / DB_NAME=lumen；已对齐 shared/.env。"
            db_user="lumen"
            db_name="lumen"
            env_file_set "${shared_env}" DB_USER "${db_user}" || exit 1
            env_file_set "${shared_env}" DB_NAME "${db_name}" || exit 1
            env_file_set "${shared_env}" DATABASE_URL \
                "postgresql+asyncpg://${db_user}:${db_password}@postgres:5432/${db_name}" || exit 1
        else
            log_error "无法用 shared/.env 中的 DB_USER=${db_user} 连接已有 PGDATA。"
            log_error "  该数据目录可能来自不同 DB_USER；请把 shared/.env 的 DB_USER/DB_NAME 改成原部署值后重跑。"
            exit 1
        fi
    fi

    if ! postgres_database_exists_for_role "${db_user}" "${db_name}"; then
        log_error "已有 PGDATA 中找不到数据库 DB_NAME=${db_name}。"
        log_error "  请把 shared/.env 的 DB_NAME 改成原部署值后重跑。"
        exit 1
    fi

    if postgres_set_role_password "${db_user}" "${db_password}"
    then
        log_info "Postgres 角色密码已与 shared/.env 对齐。"
        return 0
    fi

    log_error "无法同步 Postgres 角色密码。"
    log_error "  这通常表示拷贝来的 PGDATA 使用了不同 DB_USER，或本地 socket 也要求旧密码。"
    log_error "  请确认 /opt/lumen/shared/.env 与该 /opt/lumendata 备份来自同一套部署，"
    log_error "  或手动在 postgres 容器内 ALTER ROLE 后重跑安装。"
    exit 1
}

postgres_role_can_connect() {
    local db_user="$1"
    _install_compose exec -T postgres psql -X -qAt -v ON_ERROR_STOP=1 \
        -U "${db_user}" -d postgres -c 'SELECT 1' >/dev/null 2>&1
}

postgres_database_exists_for_role() {
    local db_user="$1"
    local db_name="$2"
    local found
    found="$(
        _install_compose exec -T postgres psql -X -qAt -v ON_ERROR_STOP=1 \
            -v lumen_db="${db_name}" \
            -U "${db_user}" -d postgres <<'SQL' 2>/dev/null || true
SELECT 1 FROM pg_database WHERE datname = :'lumen_db';
SQL
    )"
    [ "${found}" = "1" ]
}

postgres_set_role_password() {
    local db_user="$1"
    local db_password="$2"
    _install_compose exec -T postgres psql -X -v ON_ERROR_STOP=1 \
        -v lumen_role="${db_user}" \
        -v lumen_password="${db_password}" \
        -U "${db_user}" -d postgres <<'SQL'
ALTER ROLE :"lumen_role" WITH PASSWORD :'lumen_password';
SQL
}

run_migration() {
    emit_step_start migrate_db "执行数据库迁移（migrate profile，alembic upgrade head）"
    if ! _install_compose --profile migrate run --rm migrate; then
        log_error "alembic 迁移失败。检查 PG 容器健康状态与 DATABASE_URL。"
        exit 1
    fi
    log_info "数据库迁移完成。"
    emit_step_done
}

run_bootstrap_admin() {
    local shared_env="${SHARED_DIR}/.env"
    # 已 bootstrapped 过则跳过
    if grep -q '^LUMEN_BOOTSTRAPPED=1' "${shared_env}" 2>/dev/null; then
        log_info "shared/.env 中已记录 LUMEN_BOOTSTRAPPED=1，跳过管理员创建。"
        return 0
    fi

    emit_step_start migrate_db "创建首个管理员账号（bootstrap profile）"

    local admin_email admin_pwd
    if [ "${LUMEN_NONINTERACTIVE:-}" = "1" ]; then
        admin_email="${LUMEN_ADMIN_EMAIL:-}"
        admin_pwd="${LUMEN_ADMIN_PASSWORD:-}"
        if [ -z "${admin_email}" ] || [ -z "${admin_pwd}" ]; then
            log_error "LUMEN_NONINTERACTIVE=1 但未提供 LUMEN_ADMIN_EMAIL / LUMEN_ADMIN_PASSWORD。"
            exit 1
        fi
        if [ "${#admin_pwd}" -lt 12 ]; then
            log_error "LUMEN_ADMIN_PASSWORD 长度不能少于 12 位。"
            exit 1
        fi
    else
        admin_email="$(read_or_default '管理员邮箱' 'admin@example.com')"
        admin_pwd=""
        while [ -z "${admin_pwd}" ]; do
            admin_pwd="$(read_secret '管理员密码（≥12 chars）')"
            if [ -z "${admin_pwd}" ]; then
                log_warn "密码不能为空。"
            elif [ "${#admin_pwd}" -lt 12 ]; then
                log_warn "密码长度不能少于 12 位。"
                admin_pwd=""
            fi
        done
    fi

    # bootstrap 容器读 LUMEN_ADMIN_EMAIL / LUMEN_ADMIN_PASSWORD env（compose 已声明）
    # 不写入 .env（§10.3：不要把管理员密码写入 .env）
    # 注意：不再把 --password 作为 CLI 位置参数传，避免密码出现在 host
    # `ps -ef` / docker inspect Args / journalctl logs 里。bootstrap.py 已支持
    # 读 LUMEN_ADMIN_PASSWORD env 兜底。
    # 捕获 bootstrap 输出，区分"已存在"（无害，幂等重跑常见）vs "真错误"（DB
    # 连接 / migration 漂移 / 校验失败），让用户能立即定位是不是真问题。
    local _boot_log
    _boot_log="$(mktemp)" || _boot_log=""
    local _boot_rc=0
    if [ -n "${_boot_log}" ]; then
        LUMEN_ADMIN_EMAIL="${admin_email}" LUMEN_ADMIN_PASSWORD="${admin_pwd}" \
            _install_compose --profile bootstrap run --rm \
            -e "LUMEN_ADMIN_EMAIL=${admin_email}" \
            -e "LUMEN_ADMIN_PASSWORD=${admin_pwd}" \
            bootstrap python -m app.scripts.bootstrap "${admin_email}" --role admin \
            >"${_boot_log}" 2>&1 || _boot_rc=$?
        if [ "${_boot_rc}" -eq 0 ]; then
            cat "${_boot_log}" || true
        elif grep -qiE 'already (exists|created)|duplicate key|user_already_exists|already_admin' "${_boot_log}"; then
            log_info "管理员账号 ${admin_email} 已存在（bootstrap 幂等跳过）。"
        else
            log_error "bootstrap 返回非零（rc=${_boot_rc}），未写入 LUMEN_BOOTSTRAPPED。"
            log_error "  最近输出："
            tail -n 15 "${_boot_log}" | sed 's/^/    /' >&2
            log_error "  请检查：docker compose logs --tail=120 migrate api"
            rm -f "${_boot_log}"
            return "${_boot_rc}"
        fi
        rm -f "${_boot_log}"
    else
        # 无法创建日志文件时仍 fail-closed；不能把未知失败误记为已完成。
        if ! LUMEN_ADMIN_EMAIL="${admin_email}" LUMEN_ADMIN_PASSWORD="${admin_pwd}" \
                _install_compose --profile bootstrap run --rm \
                -e "LUMEN_ADMIN_EMAIL=${admin_email}" \
                -e "LUMEN_ADMIN_PASSWORD=${admin_pwd}" \
                bootstrap python -m app.scripts.bootstrap "${admin_email}" --role admin; then
            log_error "bootstrap 返回非零，且无法捕获日志确认幂等已存在；未写入 LUMEN_BOOTSTRAPPED。"
            return 1
        fi
    fi

    # 仅在成功或已确认账号存在时标记，避免失败重跑被永久跳过。
    if ! grep -q '^LUMEN_BOOTSTRAPPED=1' "${shared_env}"; then
        printf 'LUMEN_BOOTSTRAPPED=1\n' >> "${shared_env}"
    fi

    INSTALL_ADMIN_EMAIL="${admin_email}"
    log_info "管理员账号：${admin_email}"
    emit_info "key=admin_email" "value=${admin_email}"
    emit_step_done
}

start_application_services() {
    emit_step_start containers "启动 API / Worker / Web（compose --wait）"
    if ! _install_compose up --pull missing -d --wait api worker web; then
        log_error "api / worker / web 启动或健康检查失败。"
        exit 1
    fi
    INSTALL_STARTED_SERVICES+=("api" "worker" "web")

    # tgbot 仅在 .env 提供了非空 TELEGRAM_BOT_TOKEN 时启动
    local shared_env="${SHARED_DIR}/.env"
    local bot_token
    bot_token="$(env_file_get TELEGRAM_BOT_TOKEN "${shared_env}")"
    if [ -n "${bot_token}" ]; then
        log_info "检测到 TELEGRAM_BOT_TOKEN 非空，启动 tgbot service。"
        if ! _install_compose --profile tgbot up --pull missing -d tgbot; then
            log_warn "tgbot 启动失败（可能是 token 无效或网络问题）。主栈不受影响。"
            INSTALL_TGBOT_STATUS="failed"
        else
            INSTALL_STARTED_SERVICES+=("tgbot")
            INSTALL_TGBOT_STATUS="started"
        fi
    else
        log_info "未配置 TELEGRAM_BOT_TOKEN，跳过 tgbot。"
        INSTALL_TGBOT_STATUS="skipped"
    fi
    emit_step_done
}
