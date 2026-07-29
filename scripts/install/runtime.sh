#!/usr/bin/env bash
# Compose, health, and phase-reporting helpers for installation.
# Sourced by scripts/install.sh after raw bootstrap has completed.

# ---------------------------------------------------------------------------
# Compose 调用 wrapper
# 优先使用 lib.sh 提供的 lumen_compose；缺失时降级到 docker compose 直调。
# lumen_compose_in 的签名固定为 <dir> 后接 docker compose 参数，并自动设置
# COMPOSE_PROJECT_NAME=lumen。
# ---------------------------------------------------------------------------
_install_compose() {
    if command -v lumen_compose_in >/dev/null 2>&1 && [ -n "${RELEASE_DIR:-}" ]; then
        lumen_compose_in "${RELEASE_DIR}" "$@"
    elif command -v lumen_compose >/dev/null 2>&1; then
        lumen_compose "$@"
    else
        # Fallback：手动设置 COMPOSE_PROJECT_NAME=lumen，cd 到 RELEASE_DIR
        local cwd_dir="${RELEASE_DIR:-${ROOT}}"
        ( cd "${cwd_dir}" && COMPOSE_PROJECT_NAME=lumen docker compose "$@" )
    fi
}

# 按镜像分组拉取的薄 wrapper，调 lib.sh:lumen_compose_pull_per_image。
# 保留 _install_compose_pull_per_image 名字向后兼容（pull_or_build_images
# 已在用），实际工作由 lib.sh 同款函数处理；update.sh 也走 lib.sh 一份。
_install_compose_pull_per_image() {
    lumen_compose_pull_per_image "${RELEASE_DIR:-${ROOT}}"
}

# 健康检查 wrapper
_install_health_http() {
    local url="$1"
    local timeout_s="${2:-60}"
    local interval_s="${3:-2}"
    if command -v lumen_health_http >/dev/null 2>&1; then
        lumen_health_http "${url}" "${timeout_s}" "${interval_s}"
    else
        # Fallback：用 lib.sh 已有的 lumen_wait_for_http_ok（attempts=timeout_s）
        lumen_wait_for_http_ok "${url}" "${timeout_s}"
    fi
}

_install_health_compose() {
    if command -v lumen_health_compose >/dev/null 2>&1; then
        lumen_health_compose "$@"
        return $?
    fi
    # Fallback：自己 inspect Container.State.Health.Status
    local svc cid status
    for svc in "$@"; do
        cid="$(_install_compose ps -q "${svc}" 2>/dev/null | head -n1 || true)"
        if [ -z "${cid}" ]; then
            log_error "compose service ${svc} 未运行，无法做健康检查。"
            return 1
        fi
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${cid}" 2>/dev/null || true)"
        case "${status}" in
            healthy|running) ;;
            *)
                log_error "compose service ${svc} 状态异常：${status}"
                return 1
                ;;
        esac
    done
}

# 阶段记录 wrapper
# 记录每个 phase 的 wall-clock 起止时间，emit_step_done 时打印耗时摘要给
# 终端用户（lumen_emit_step 的 dur_ms 仅写入 SSE 协议，终端看不到）。
# 用单变量而非 declare -A 关联数组，兼容 macOS bash 3.2（CI smoke runner）。
_now_seconds() {
    # 高精度 wall-clock；macOS date 不支持 +%s.%N，用 perl 兜底，再不行用秒精度。
    if date +%s.%N >/dev/null 2>&1 && [ "$(date +%N)" != "N" ]; then
        date +%s.%N
    elif command -v perl >/dev/null 2>&1; then
        perl -MTime::HiRes=time -e 'printf "%.3f\n", time'
    else
        date +%s
    fi
}

emit_step_start() {
    INSTALL_PHASE="$1"
    INSTALL_PHASE_START_TS="$(_now_seconds)"
    log_step "[${INSTALL_PHASE}] $2"
    if command -v lumen_emit_step >/dev/null 2>&1; then
        lumen_emit_step "phase=${INSTALL_PHASE}" "status=start" || true
    fi
}

emit_step_done() {
    local dur=""
    if [ -n "${INSTALL_PHASE_START_TS}" ] && [ -n "${INSTALL_PHASE:-}" ]; then
        local end_ts
        end_ts="$(_now_seconds)"
        # awk 处理浮点；不依赖 bc。失败时 dur 留空，不打耗时。
        dur="$(awk -v s="${INSTALL_PHASE_START_TS}" -v e="${end_ts}" \
            'BEGIN { d = e - s; if (d < 0) d = 0; printf "%.1f", d }' 2>/dev/null || true)"
        if [ -n "${dur}" ]; then
            log_info "  ✓ ${INSTALL_PHASE} 完成（耗时 ${dur}s）"
        fi
    fi
    if command -v lumen_emit_step >/dev/null 2>&1 && [ -n "${INSTALL_PHASE:-}" ]; then
        lumen_emit_step "phase=${INSTALL_PHASE}" "status=done" "rc=0" \
            ${dur:+dur_ms=$(awk -v d="${dur}" 'BEGIN { printf "%d", d * 1000 }')} || true
    fi
    INSTALL_PHASE=""
    INSTALL_PHASE_START_TS=""
}

emit_info() {
    if command -v lumen_emit_info >/dev/null 2>&1 && [ -n "${INSTALL_PHASE:-}" ]; then
        lumen_emit_info "phase=${INSTALL_PHASE}" "$@" || true
    fi
}
