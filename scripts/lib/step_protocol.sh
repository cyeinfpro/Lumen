#!/usr/bin/env bash
# Sourced by scripts/lib.sh; do not execute directly.

# Step protocol parsed by admin_update.py. Keep the wire format stable.
LUMEN_CURRENT_PHASE=""
LUMEN_CURRENT_PHASE_START_MS=""
LUMEN_VALID_PHASES="lock check preflight backup_preflight fetch_release set_image_tag pull_images warm_pull start_infra migrate_db switch restart_services start_green shift_traffic shift_traffic_50 shift_traffic_100 drain_blue stop_blue start_blue shift_traffic_blue stop_green health_check cleanup rollback prepare fetch link_shared containers deps_python deps_node build_web health_post"

lumen_iso_now() {
    date -u +%FT%TZ 2>/dev/null || date
}

lumen_now_ms() {
    local out
    out="$(date -u +%s%3N 2>/dev/null || true)"
    case "${out}" in
        ''|*[!0-9]*) ;;
        *N*) ;;
        *)
            printf '%s' "${out}"
            return 0
            ;;
    esac
    if command -v perl >/dev/null 2>&1; then
        perl -MTime::HiRes=time -e 'printf "%d", time()*1000' 2>/dev/null && return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import time;print(int(time.time()*1000))' 2>/dev/null && return 0
    fi
    printf '%s000' "$(date -u +%s 2>/dev/null || echo 0)"
}

lumen_step_phase_is_valid() {
    local phase="$1"
    case " ${LUMEN_VALID_PHASES} " in
        *" ${phase} "*) return 0 ;;
        *) return 1 ;;
    esac
}

lumen_step_begin() {
    local phase="$1"
    if [ -z "${phase}" ]; then
        log_warn "lumen_step_begin: 空 phase 参数。"
        return 0
    fi
    if ! lumen_step_phase_is_valid "${phase}"; then
        log_warn "lumen_step_begin: 未登记的 phase=${phase}（允许列表：${LUMEN_VALID_PHASES}）。"
    fi
    LUMEN_CURRENT_PHASE="${phase}"
    LUMEN_CURRENT_PHASE_START_MS="$(lumen_now_ms)"
    printf '::lumen-step:: phase=%s status=start ts=%s\n' \
        "${phase}" "$(lumen_iso_now)"
}

lumen_step_end() {
    local phase="$1"
    local rc="${2:-0}"
    local dur_ms=0
    if [ -z "${phase}" ]; then
        return 0
    fi
    if [ -n "${LUMEN_CURRENT_PHASE_START_MS:-}" ]; then
        local now_ms
        now_ms="$(lumen_now_ms)"
        dur_ms=$(( now_ms - LUMEN_CURRENT_PHASE_START_MS ))
        if [ "${dur_ms}" -lt 0 ]; then
            dur_ms=0
        fi
    fi
    printf '::lumen-step:: phase=%s status=done rc=%s dur_ms=%s ts=%s\n' \
        "${phase}" "${rc}" "${dur_ms}" "$(lumen_iso_now)"
    if [ "${LUMEN_CURRENT_PHASE:-}" = "${phase}" ]; then
        LUMEN_CURRENT_PHASE=""
        LUMEN_CURRENT_PHASE_START_MS=""
    fi
}

lumen_step_info() {
    local phase="$1"
    local key="$2"
    shift 2 || true
    local raw="$*"
    local value
    value="$(printf '%s' "${raw}" | tr '\r\n' '  ')"
    printf '::lumen-info:: phase=%s key=%s value=%s\n' \
        "${phase}" "${key}" "${value}"
}

lumen_step_finalize_failure() {
    local rc="${1:-1}"
    if [ -n "${LUMEN_CURRENT_PHASE:-}" ]; then
        lumen_step_end "${LUMEN_CURRENT_PHASE}" "${rc}"
    fi
}

LUMEN_DOCKER_USE_SUDO="${LUMEN_DOCKER_USE_SUDO:-0}"

lumen_handle_signal() {
    local signal="$1"
    local line="${2:-unknown}"
    local code=1
    case "${signal}" in
        INT) code=130 ;;
        TERM) code=143 ;;
    esac
    trap - INT TERM
    log_error "收到 ${signal}，脚本已中断（第 ${line} 行）。"
    exit "${code}"
}

lumen_install_signal_handlers() {
    trap 'lumen_handle_signal INT "${LINENO}"' INT
    trap 'lumen_handle_signal TERM "${LINENO}"' TERM
}

lumen_emit_step() {
    local line
    line="$(printf '::lumen-step::')"
    local arg
    for arg in "$@"; do
        line="${line} ${arg}"
    done
    line="${line} ts=$(lumen_iso_now)"
    printf '%s\n' "${line}"
    printf '%s\n' "${line}" >&2
}

lumen_emit_info() {
    local line
    line="$(printf '::lumen-info::')"
    local arg
    for arg in "$@"; do
        line="${line} ${arg}"
    done
    line="${line} ts=$(lumen_iso_now)"
    printf '%s\n' "${line}"
    printf '%s\n' "${line}" >&2
}
