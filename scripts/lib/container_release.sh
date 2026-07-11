#!/usr/bin/env bash
# Docker Compose, image verification, release manifest, and image tag helpers.
# Sourced by scripts/lib.sh; do not execute directly.

# ---------------------------------------------------------------------------
# Docker Compose 包装与容器化健康检查
# 详见 docs/docker-full-stack-cutover-plan.md §11.4 / §13 / §17.5-§17.7。
# ---------------------------------------------------------------------------

# 返回固定的 Compose project name 常量（§11.4 死规则）。
lumen_compose_project_name() {
    printf '%s' "${LUMEN_COMPOSE_PROJECT:-lumen}"
}

# 包装 docker compose；自动注入 COMPOSE_PROJECT_NAME，并探测 v2 可用性。
#
# 显式 -f / --env-file 兜底：
# 当 caller cwd 不是 release 目录（例如 lumenctl/update.sh 从 /opt/lumen 而非
# /opt/lumen/current 调起）时，docker compose 默认无法读到 release dir 的 .env，
# 所有 ${VAR:-default} 走 fallback。曾在 update-lumen 时把 LUMEN_DB_ROOT
# 从 /var/lib/lumen-data 错回 /opt/lumendata（SMB），触发 pg recreate 后
# initdb 错乱清空数据目录。这里探测 caller cwd 没有 docker-compose.yml 时，
# 自动指向 ${LUMEN_DEPLOY_ROOT:-/opt/lumen}/current 的 compose 文件 + .env。
lumen_compose() {
    if ! docker compose version >/dev/null 2>&1; then
        log_error "未检测到 docker compose v2，请安装/升级到 Docker Compose v2 后重试。"
        return 1
    fi
    local explicit=()
    if [ ! -f "./docker-compose.yml" ]; then
        local _cur="${LUMEN_DEPLOY_ROOT:-/opt/lumen}/current"
        if [ -f "${_cur}/docker-compose.yml" ]; then
            explicit+=("-f" "${_cur}/docker-compose.yml")
            [ -f "${_cur}/.env" ] && explicit+=("--env-file" "${_cur}/.env")
        fi
    fi
    # ${explicit[@]+"${explicit[@]}"}: 兼容 set -u — 空数组 ${arr[@]} 报
    # unbound variable，需用 + 形式 "如果定义了就展开"。
    COMPOSE_PROJECT_NAME="${LUMEN_COMPOSE_PROJECT:-lumen}" \
        lumen_docker compose --ansi=never ${explicit[@]+"${explicit[@]}"} "$@"
}

# 在指定目录执行 lumen_compose（release 切换时用）。
lumen_compose_in() {
    local dir="$1"
    shift
    ( cd "${dir}" && lumen_compose "$@" )
}

# 按镜像分组拉取 compose 中的所有 image：先 `compose config --images` 枚举，
# 再逐个 `lumen_docker pull`，每个镜像之前打 `[i/n] image:tag` 头部分隔。
# docker 自身的 layer 进度（下载条/速度）保留，TTY 下原地刷新；非 TTY 下虽然
# 会逐行输出但每个镜像之间有清晰边界，不会像 `compose pull` 那样把所有镜像的
# layer 进度混在一起刷屏。
# 用法：lumen_compose_pull_per_image <compose_dir>
# 枚举失败兜底回退 `lumen_compose_in <dir> pull`，保证最差也能 work。
lumen_verify_image_signature_if_required() {
    local image="$1"
    if ! lumen_env_truthy "${LUMEN_VERIFY_IMAGE_SIGNATURES:-0}"; then
        return 0
    fi
    if ! command -v cosign >/dev/null 2>&1; then
        log_error "LUMEN_VERIFY_IMAGE_SIGNATURES=1 但未找到 cosign，无法校验镜像签名。"
        return 1
    fi
    cosign verify \
        --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
        --certificate-identity-regexp "https://github.com/cyeinfpro/Lumen/.github/workflows/docker-release.yml@refs/(tags/v.*|heads/main)" \
        "${image}" >/dev/null
}

lumen_record_image_digest() {
    local image="$1"
    local digest lock_file
    digest="$(lumen_docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${image}" 2>/dev/null | head -n1 || true)"
    if [ -z "${digest}" ]; then
        log_warn "未能读取镜像 digest：${image}"
        return 0
    fi
    log_info "镜像 digest：${digest}"
    lock_file="${LUMEN_IMAGE_DIGEST_LOCK_FILE:-}"
    if [ -n "${lock_file}" ]; then
        mkdir -p "$(dirname "${lock_file}")"
        printf '%s %s\n' "${image}" "${digest}" >> "${lock_file}"
    fi
}

lumen_release_manifest_required() {
    local tag="${1:-}"
    [[ "${tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]
}

lumen_release_alias_tag() {
    local tag="${1:-}"
    [[ "${tag}" =~ ^v[0-9]+(\.[0-9]+)?$ ]]
}

lumen_resolve_release_alias() {
    local alias="$1"
    local guard="${LUMEN_RELEASE_MANIFEST_GUARD:-${SCRIPT_DIR}/release_manifest_guard.py}"
    python3 "${guard}" resolve-alias --alias "${alias}"
}

lumen_fetch_release_manifest() {
    local tag="$1"
    local output="$2"
    local guard="${LUMEN_RELEASE_MANIFEST_GUARD:-${SCRIPT_DIR}/release_manifest_guard.py}"
    if [ ! -f "${guard}" ]; then
        log_error "缺少 release manifest 校验器：${guard}"
        return 1
    fi
    python3 "${guard}" fetch --tag "${tag}" --output "${output}"
}

lumen_verify_release_manifest_images() {
    local manifest="$1"
    local manifest_tag="$2"
    local inspect_tag="$3"
    shift 3
    local guard="${LUMEN_RELEASE_MANIFEST_GUARD:-${SCRIPT_DIR}/release_manifest_guard.py}"
    local entries service image_ref inspect_ref digest immutable_ref repo_digests
    if ! entries="$(python3 "${guard}" entries --manifest "${manifest}" --tag "${manifest_tag}" "$@")"; then
        return 1
    fi
    while IFS=$'\t' read -r service image_ref digest immutable_ref; do
        [ -n "${service}" ] || continue
        inspect_ref="${image_ref%:*}:${inspect_tag}"
        repo_digests="$(lumen_docker image inspect \
            --format '{{range .RepoDigests}}{{println .}}{{end}}' \
            "${inspect_ref}" 2>/dev/null || true)"
        if ! printf '%s\n' "${repo_digests}" | grep -Fxq "${immutable_ref}"; then
            log_error "镜像 digest 与 release manifest 不一致：service=${service} tag=${inspect_ref} release=${manifest_tag} expected=${digest}"
            return 1
        fi
        log_info "release manifest digest 通过：${service} ${digest}"
    done <<< "${entries}"
}

lumen_compose_pull_per_image() {
    local compose_dir="$1"
    if [ -z "${compose_dir}" ]; then
        log_error "lumen_compose_pull_per_image: compose_dir 参数缺失"
        return 1
    fi
    local images raw_images
    if ! raw_images="$(lumen_compose_in "${compose_dir}" config --images 2>/dev/null)"; then
        if lumen_env_truthy "${LUMEN_VERIFY_IMAGE_SIGNATURES:-0}"; then
            log_error "LUMEN_VERIFY_IMAGE_SIGNATURES=1 时无法枚举 compose 镜像列表，拒绝执行未校验的 docker compose pull。"
            return 1
        fi
        log_warn "无法枚举 compose 镜像列表（${compose_dir}），回退到默认 docker compose pull。"
        lumen_compose_in "${compose_dir}" pull
        return $?
    fi
    images="$(printf '%s\n' "${raw_images}" | sort -u)"
    if [ -z "${images}" ]; then
        log_warn "compose 镜像列表为空（${compose_dir}），跳过 pull。"
        return 0
    fi

    local total idx=0 img rc=0
    local failed=()
    total="$(printf '%s\n' "${images}" | sed '/^$/d' | wc -l | tr -d ' ')"
    log_info "拉取 ${total} 个镜像（按镜像分组，docker 进度保留）"
    while IFS= read -r img; do
        [ -z "${img}" ] && continue
        idx=$((idx + 1))
        printf '\n  [%d/%d] %s\n' "${idx}" "${total}" "${img}"
        if ! lumen_docker pull "${img}"; then
            failed+=("${img}")
            rc=1
        elif ! lumen_verify_image_signature_if_required "${img}"; then
            failed+=("${img}")
            rc=1
        else
            lumen_record_image_digest "${img}"
        fi
    done <<< "${images}"

    if [ "${rc}" -ne 0 ]; then
        log_error "以下镜像拉取失败（${#failed[@]}/${total}）："
        local f
        for f in "${failed[@]}"; do
            log_error "  - ${f}"
        done
    fi
    return "${rc}"
}

# 把 lumen-* 容器从任意 stale compose project 迁移到 LUMEN_COMPOSE_PROJECT
# (默认 lumen)。idempotent — 没有 stale 直接返回。
#
# Why: 历史上有人在 /opt/lumen/current/ 直接 `cd && docker compose up` 起过容器
# (project 取 cwd basename = "current"，或随 release dir 变化)。新版 lib.sh 强制
# COMPOSE_PROJECT_NAME=lumen 后，docker 视角 project=lumen 无该容器要新建，但容器
# 名 lumen-redis 是全局唯一被 stale project 占用 → "container name in use" 冲突，
# --force-recreate 跨 project 不生效。
#
# 操作：
#   1. detect 所有 name 形如 lumen-* 的容器，按 project label 分组
#   2. 找出 project ≠ ${LUMEN_COMPOSE_PROJECT:-lumen} 的，逐个 docker compose -p
#      <stale> down --remove-orphans (volume 是 bind mount /opt/lumendata/*，不
#      会被 down 删掉)
#   3. 让调用方继续正常 up 到目标 project
lumen_compose_project_unify() {
    local target="${LUMEN_COMPOSE_PROJECT:-lumen}"
    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi
    local stale
    # docker ps 的 .Labels 是 "k1=v1,k2=v2" 字符串不能 index；用单数
    # {{.Label "key"}}（docker ps 专用）取单个 label。
    stale="$(docker ps -a \
        --filter 'name=^lumen-' \
        --format '{{.Label "com.docker.compose.project"}}' \
        2>/dev/null | sort -u | grep -v "^${target}$" | grep -v '^$' || true)"
    if [ -z "${stale}" ]; then
        return 0
    fi
    log_info "[compose-project] 检测到 lumen-* 容器跑在非 ${target} 的 project："
    while IFS= read -r p; do
        [ -z "${p}" ] && continue
        log_info "  - project=${p}; 即将 docker compose -p '${p}' down --remove-orphans"
    done <<< "${stale}"
    log_info "[compose-project] volumes 是 bind mount (/opt/lumendata/*)，不会丢数据。"
    while IFS= read -r p; do
        [ -z "${p}" ] && continue
        if ! docker compose -p "${p}" down --remove-orphans 2>&1 | tail -10; then
            log_warn "[compose-project] docker compose -p '${p}' down 失败；后续 up 会撞容器名。"
        fi
    done <<< "${stale}"
}

# 轮询 HTTP 健康端点；用法：lumen_health_http <url> <max_seconds> <interval_seconds>。
lumen_health_http() {
    local url="$1"
    local max_seconds="${2:-30}"
    local interval="${3:-2}"
    [ "${interval}" -gt 0 ] || interval=1
    local attempts=$(( max_seconds / interval ))
    [ "${attempts}" -gt 0 ] || attempts=1
    local _i
    for _i in $(seq 1 "${attempts}"); do
        if curl --noproxy '*' -fsS --max-time 5 -o /dev/null "${url}" 2>/dev/null; then
            return 0
        fi
        sleep "${interval}"
    done
    return 1
}

# 检查 compose 服务是否 running 且（如有 healthcheck）healthy；变长服务名。
lumen_health_compose() {
    local attempts="${LUMEN_HEALTH_COMPOSE_ATTEMPTS:-60}"
    local interval="${LUMEN_HEALTH_COMPOSE_INTERVAL:-2}"
    local proj="${LUMEN_COMPOSE_PROJECT:-lumen}"
    local svc cid status _i ok
    for svc in "$@"; do
        cid=""
        ok=0
        for _i in $(seq 1 "${attempts}"); do
            cid="$(lumen_compose ps --status running --quiet "${svc}" 2>/dev/null | head -n1)"
            if [ -z "${cid}" ]; then
                sleep "${interval}"
                continue
            fi
            status="$(lumen_docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "${proj}-${svc}" 2>/dev/null || true)"
            if [ -z "${status}" ]; then
                # 容器可能用其它命名（service-1 等）；按容器 id 再查一次。
                status="$(lumen_docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "${cid}" 2>/dev/null || true)"
            fi
            case "${status}" in
                ""|healthy)
                    ok=1
                    break
                    ;;
                unhealthy)
                    log_error "compose 服务 ${svc} 进入 unhealthy 状态。"
                    return 1
                    ;;
                starting|*)
                    sleep "${interval}"
                    continue
                ;;
            esac
        done
        if [ -z "${cid}" ]; then
            log_error "compose 服务 ${svc} 未在 ${attempts}×${interval}s 内 running。"
            return 1
        fi
        if [ "${ok}" -ne 1 ]; then
            log_error "compose 服务 ${svc} 未在 ${attempts}×${interval}s 内 healthy。"
            return 1
        fi
    done
    return 0
}

# 根据 LUMEN_UPDATE_CHANNEL 解析目标镜像 tag；输出到 stdout。
# 注意：stable/latest 解析失败时不能沿用 current_tag，否则 update-lumen 会因为
# current_tag == target_tag 走 noop，造成"更新成功但仍是旧版本"。也不能静默
# 回退 main，否则稳定通道会变成 rolling 通道。只有 pinned channel 才允许明确
# 保持当前 tag。
# 用法：
#   lumen_image_tag_resolve [channel] [env_file]
# 兼容旧调用：如果第一个参数是存在的文件路径，则视为 env_file，channel 从环境读取。
lumen_image_tag_resolve() {
    local channel="${1:-${LUMEN_UPDATE_CHANNEL:-stable}}"
    local env_file="${2:-${LUMEN_DEPLOY_ROOT}/shared/.env}"
    if [ -n "${1:-}" ] && [ -f "${1}" ] && [ -z "${2:-}" ]; then
        env_file="$1"
        channel="${LUMEN_UPDATE_CHANNEL:-stable}"
    fi
    local resolved_tag="${LUMEN_UPDATE_RESOLVED_TAG:-}"
    if [ -n "${resolved_tag}" ]; then
        if lumen_image_tag_is_valid "${resolved_tag}"; then
            printf '%s\n' "${resolved_tag}"
            return 0
        fi
        log_warn "LUMEN_UPDATE_RESOLVED_TAG=${resolved_tag} 非法，忽略并继续解析。"
    fi
    local current_tag=""
    if [ -f "${env_file}" ]; then
        current_tag="$(lumen_env_value LUMEN_IMAGE_TAG "${env_file}")"
    fi
    case "${channel}" in
        main)
            printf 'main\n'
            return 0
            ;;
        pinned)
            if [ -n "${current_tag}" ]; then
                printf '%s\n' "${current_tag}"
                return 0
            fi
            log_warn "channel=pinned 但 ${env_file} 未设置 LUMEN_IMAGE_TAG，回退 main。"
            printf 'main\n'
            return 0
            ;;
        minor)
            if printf '%s\n' "${current_tag}" | grep -Eq '^v[0-9]+\.[0-9]+(\.[0-9]+)?$'; then
                printf '%s\n' "${current_tag}" | sed -E 's/^(v[0-9]+\.[0-9]+)(\.[0-9]+)?$/\1/'
                return 0
            fi
            if [ -n "${current_tag}" ]; then
                log_warn "channel=minor 需要当前 LUMEN_IMAGE_TAG 形如 v1.2 或 v1.2.3，当前为 ${current_tag}；保持当前 tag。"
                printf '%s\n' "${current_tag}"
                return 0
            fi
            log_warn "channel=minor 但 ${env_file} 未设置 LUMEN_IMAGE_TAG，回退 main。"
            printf 'main\n'
            return 0
            ;;
        major)
            if printf '%s\n' "${current_tag}" | grep -Eq '^v[0-9]+(\.[0-9]+){0,2}$'; then
                printf '%s\n' "${current_tag}" | sed -E 's/^(v[0-9]+)(\.[0-9]+){0,2}$/\1/'
                return 0
            fi
            if [ -n "${current_tag}" ]; then
                log_warn "channel=major 需要当前 LUMEN_IMAGE_TAG 形如 v1、v1.2 或 v1.2.3，当前为 ${current_tag}；保持当前 tag。"
                printf '%s\n' "${current_tag}"
                return 0
            fi
            log_warn "channel=major 但 ${env_file} 未设置 LUMEN_IMAGE_TAG，回退 main。"
            printf 'main\n'
            return 0
            ;;
        v[0-9]*)
            printf '%s\n' "${channel}"
            return 0
            ;;
    esac
    # stable / latest：查 GitHub Releases API 取 latest tag_name
    local api_url="https://api.github.com/repos/cyeinfpro/Lumen/releases/latest"
    local proxy_args=()
    if [ -n "${LUMEN_UPDATE_PROXY_URL:-}" ]; then
        proxy_args=(-x "${LUMEN_UPDATE_PROXY_URL}")
    fi
    local body=""
    if command -v curl >/dev/null 2>&1; then
        body="$(curl -fsSL --max-time 8 "${proxy_args[@]}" \
            -H 'Accept: application/vnd.github+json' \
            "${api_url}" 2>/dev/null || true)"
    fi
    local tag=""
    if [ -n "${body}" ]; then
        tag="$(printf '%s' "${body}" \
            | grep -m1 '"tag_name"' \
            | sed -E 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
    fi
    if [ -n "${tag}" ]; then
        printf '%s\n' "${tag}"
        return 0
    fi
    if [ -n "${current_tag}" ]; then
        log_warn "GitHub Releases API 不可达，stable/latest 无法解析；当前 LUMEN_IMAGE_TAG=${current_tag}，请稍后重试或显式设置 LUMEN_UPDATE_CHANNEL=main。"
    else
        log_warn "GitHub Releases API 不可达且 .env 无 LUMEN_IMAGE_TAG；stable/latest 无法解析，请稍后重试或显式设置 LUMEN_UPDATE_CHANNEL=main。"
    fi
    return 1
}

lumen_image_tag_is_valid() {
    local tag="${1:-}"
    case "${tag}" in
        main|latest)
            return 0
            ;;
    esac
    printf '%s\n' "${tag}" | grep -Eq '^v[0-9]+(\.[0-9]+){0,2}(-[0-9A-Za-z.-]+)?$'
}

lumen_image_tag_is_rolling() {
    local tag="${1:-}"
    case "${tag}" in
        main|latest)
            return 0
            ;;
    esac
    printf '%s\n' "${tag}" | grep -Eq '^v[0-9]+$|^v[0-9]+\.[0-9]+$'
}

# 把 LUMEN_IMAGE_TAG=<tag> 唯一写入指定 .env，禁止动其他字段（§6.4.1）。
lumen_set_image_tag_in_env() {
    local file="$1"
    local tag="$2"
    local dir base tmp
    if [ -z "${file}" ] || [ -z "${tag}" ]; then
        log_error "lumen_set_image_tag_in_env：参数不完整 (file=${file} tag=${tag})。"
        return 1
    fi
    if [ ! -f "${file}" ]; then
        log_error "lumen_set_image_tag_in_env：${file} 不存在。"
        return 1
    fi
    dir="$(dirname "${file}")"
    base="$(basename "${file}")"
    # 显式 `.tmp` 后缀：dir 可能恰好是 nginx sites-enabled 之类含 `include *` 的
    # 目录（运维误把 .env 放进去过），mktemp 默认无后缀的临时名 `.foo.image-tag.AbCdEf`
    # 会被纳入 include。`.tmp` 后缀让所有 conf 风格 include 一致跳过。
    if ! tmp="$(mktemp "${dir}/.${base}.image-tag.XXXXXX.tmp")"; then
        log_error "lumen_set_image_tag_in_env：无法创建临时文件。"
        return 1
    fi
    if ! awk -v tag="${tag}" '
        BEGIN { done = 0 }
        /^LUMEN_IMAGE_TAG=/ {
            if (done == 0) {
                print "LUMEN_IMAGE_TAG=" tag
                done = 1
            }
            next
        }
        { print }
        END {
            if (done == 0) {
                print "LUMEN_IMAGE_TAG=" tag
            }
        }
    ' "${file}" > "${tmp}"; then
        rm -f "${tmp}" 2>/dev/null || true
        log_error "写入 LUMEN_IMAGE_TAG 临时文件失败：${file}"
        return 1
    fi
    if ! mv "${tmp}" "${file}"; then
        rm -f "${tmp}" 2>/dev/null || true
        log_error "替换 LUMEN_IMAGE_TAG 文件失败：${file}"
        return 1
    fi
    local count
    count="$(grep -cE '^LUMEN_IMAGE_TAG=' "${file}" || true)"
    if [ "${count}" != "1" ]; then
        log_error "${file} 中 LUMEN_IMAGE_TAG 出现 ${count} 次，期望唯一存在。"
        return 1
    fi
    return 0
}
