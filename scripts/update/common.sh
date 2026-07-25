#!/usr/bin/env bash
# Shared updater phase protocol and environment helpers.

# ---------------------------------------------------------------------------
# 工具函数：emit 包装 + 安全 mask
# ---------------------------------------------------------------------------
# 用单变量而非 declare -A 关联数组，兼容 macOS bash 3.2（CI smoke runner）。
# update.sh 的 emit_start/done 调用是顺序成对的（不交叉），单 LAST_PHASE
# 足够；emit_fail 顺势清空。
_UPDATE_LAST_PHASE=""
_UPDATE_LAST_PHASE_START_TS=""
emit_start() {
    local _phase="$1"
    lumen_update_failpoint before "${_phase}"
    lumen_update_journal_phase_start "${_phase}"
    _UPDATE_LAST_PHASE="${_phase}"
    _UPDATE_LAST_PHASE_START_TS="$(date +%s 2>/dev/null || echo 0)"
    lumen_emit_step "phase=${_phase}" "status=start"
}
emit_done()  {
    local _phase="$1" _rc="${2:-0}"
    local _dur_arg=""
    if [ "${_UPDATE_LAST_PHASE}" = "${_phase}" ] \
            && [ -n "${_UPDATE_LAST_PHASE_START_TS}" ] \
            && [ "${_UPDATE_LAST_PHASE_START_TS}" -gt 0 ] 2>/dev/null; then
        local _end _dur
        _end="$(date +%s 2>/dev/null || echo 0)"
        _dur=$((_end - _UPDATE_LAST_PHASE_START_TS))
        if [ "${_dur}" -ge 0 ]; then
            log_info "  ✓ ${_phase} 完成（耗时 ${_dur}s）"
            _dur_arg="dur_ms=$((_dur * 1000))"
        fi
        _UPDATE_LAST_PHASE=""
        _UPDATE_LAST_PHASE_START_TS=""
    fi
    lumen_emit_step "phase=${_phase}" "status=done" "rc=${_rc}" ${_dur_arg:+"${_dur_arg}"}
    lumen_update_journal_phase_done "${_phase}"
    lumen_update_failpoint after "${_phase}"
}
emit_fail()  {
    local _phase="$1" _rc="${2:-1}"
    if [ "${_UPDATE_LAST_PHASE}" = "${_phase}" ]; then
        _UPDATE_LAST_PHASE=""
        _UPDATE_LAST_PHASE_START_TS=""
    fi
    lumen_emit_step "phase=${_phase}" "status=fail" "rc=${_rc}"
    lumen_update_journal_failed "${_phase}" "${_rc}"
}
emit_info()  { lumen_emit_info "phase=$1" "key=$2" "value=$3"; }
emit_warn()  { lumen_emit_info "phase=$1" "key=warn" "value=$2"; }

BYOK_DEV_MASTER_SECRET="lumen-dev-byok-secret-DO-NOT-USE-IN-PROD-aabbccdd"

# 检查 .env 是否存在指定 key 且非空，不输出 value。
env_key_present() {
    local file="$1"
    local key="$2"
    [ -f "${file}" ] || return 1
    grep -qE "^${key}=.+" "${file}"
}

shared_app_env_is_development() {
    local file="$1"
    local app_env
    app_env="$(lumen_env_value APP_ENV "${file}" 2>/dev/null | tr '[:upper:]' '[:lower:]' || true)"
    case "${app_env:-dev}" in
        dev|development|local|test)
            return 0
            ;;
    esac
    return 1
}

lumen_env_truthy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

update_requires_migration_restore_point() {
    if lumen_env_truthy "${LUMEN_UPDATE_SKIP_BACKUP:-0}"; then
        return 1
    fi
    if [ -n "${LUMEN_UPDATE_REQUIRE_MIGRATION_BACKUP+x}" ]; then
        lumen_env_truthy "${LUMEN_UPDATE_REQUIRE_MIGRATION_BACKUP}"
        return
    fi
    # The admin fallback path can invoke update.sh directly instead of going
    # through update_runner.py. Treat all non-interactive updates as protected
    # unless a trusted caller explicitly overrides the policy.
    lumen_env_truthy "${LUMEN_UPDATE_NONINTERACTIVE:-0}"
}
