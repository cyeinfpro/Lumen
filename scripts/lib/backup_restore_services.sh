#!/usr/bin/env bash
# Shared service quiesce helpers for paired Postgres/Redis backup and restore.

LUMEN_WRITER_SERVICE_NAMES=(api worker tgbot)
LUMEN_SITE_SERVICE_NAMES=(web)
LUMEN_SYSTEMD_WRITER_UNIT_NAMES=(
    lumen-api.service
    lumen-worker.service
    lumen-tgbot.service
)

lumen_systemd_writer_running_state() {
    local unit="$1"
    local output="" rc=0
    if ! command -v systemctl >/dev/null 2>&1; then
        printf 'absent\n'
        return 0
    fi
    case "${LUMEN_SYSTEMD_RUNTIME_AVAILABLE:-auto}" in
        1) ;;
        0)
            printf 'absent\n'
            return 0
            ;;
        auto)
            if [ ! -d "${LUMEN_SYSTEMD_RUNTIME_DIR:-/run/systemd/system}" ]; then
                printf 'absent\n'
                return 0
            fi
            ;;
        *)
            return 2
            ;;
    esac
    output="$(systemctl is-active "${unit}" 2>/dev/null)" || rc=$?
    case "${output}" in
        active|activating|reloading|deactivating)
            printf 'running\n'
            return 0
            ;;
        inactive|failed|unknown)
            printf 'stopped\n'
            return 0
            ;;
        "")
            [ "${rc}" -eq 4 ] && {
                printf 'absent\n'
                return 0
            }
            ;;
    esac
    return 2
}

lumen_active_systemd_writer_units() {
    local unit="" state=""
    for unit in "${LUMEN_SYSTEMD_WRITER_UNIT_NAMES[@]}"; do
        state="$(lumen_systemd_writer_running_state "${unit}")" || return 2
        [ "${state}" = "running" ] && printf '%s\n' "${unit}"
    done
    return 0
}

lumen_require_no_active_systemd_fallback_writers() {
    local active=""
    if ! active="$(lumen_active_systemd_writer_units)"; then
        printf '%s\n' \
            "unable to verify systemd fallback writer state; refusing maintenance" \
            >&2
        return 1
    fi
    if [ -n "${active}" ]; then
        printf 'systemd fallback writers are active; refusing maintenance: %s\n' \
            "$(printf '%s' "${active}" | tr '\n' ' ' | sed 's/ $//')" >&2
        return 1
    fi
    return 0
}

lumen_service_container_name() {
    printf 'lumen-%s\n' "$1"
}

lumen_service_running_state() {
    local service="$1"
    local container output
    container="$(lumen_service_container_name "${service}")"
    if output="$(docker inspect -f '{{.State.Running}}' "${container}" 2>&1)"; then
        case "${output}" in
            true) printf 'running\n' ;;
            false) printf 'stopped\n' ;;
            *) return 2 ;;
        esac
        return 0
    fi
    case "${output}" in
        *"No such object"*|*"No such container"*|\
        *"no such object"*|*"no such container"*|*"not found"*)
            printf 'absent\n'
            return 0
            ;;
    esac
    return 2
}

lumen_running_services() {
    local service running
    for service in "$@"; do
        if ! running="$(lumen_service_running_state "${service}")"; then
            return 2
        fi
        [ "${running}" = "running" ] && printf '%s\n' "${service}"
    done
    return 0
}

lumen_running_writer_services() {
    lumen_require_no_active_systemd_fallback_writers || return 2
    lumen_running_services "${LUMEN_WRITER_SERVICE_NAMES[@]}"
}

lumen_running_site_services() {
    lumen_running_services "${LUMEN_SITE_SERVICE_NAMES[@]}"
}

lumen_stop_services() {
    [ "$#" -gt 0 ] || return 0
    if command -v lumen_compose >/dev/null 2>&1; then
        local rc=0
        if printf '%s\n' "$@" | grep -Fxq tgbot; then
            lumen_compose --profile tgbot stop "$@" || rc=$?
        else
            lumen_compose stop "$@" || rc=$?
        fi
        return "${rc}"
    fi
    local service
    local -a containers=()
    for service in "$@"; do
        containers+=("$(lumen_service_container_name "${service}")")
    done
    docker stop "${containers[@]}" >/dev/null
}

lumen_start_services() {
    [ "$#" -gt 0 ] || return 0
    if command -v lumen_compose >/dev/null 2>&1; then
        local rc=0
        if printf '%s\n' "$@" | grep -Fxq tgbot; then
            lumen_compose --profile tgbot start "$@" || rc=$?
        else
            lumen_compose start "$@" || rc=$?
        fi
        return "${rc}"
    fi
    local service
    local -a containers=()
    for service in "$@"; do
        containers+=("$(lumen_service_container_name "${service}")")
    done
    docker start "${containers[@]}" >/dev/null
}

lumen_stop_writer_services() {
    lumen_stop_services "$@"
}

lumen_start_writer_services() {
    lumen_start_services "$@"
}

lumen_services_all_stopped() {
    local service state
    for service in "$@"; do
        if ! state="$(lumen_service_running_state "${service}")"; then
            return 2
        fi
        [ "${state}" != "running" ] || return 1
    done
    return 0
}

lumen_services_all_running() {
    local service state
    for service in "$@"; do
        if ! state="$(lumen_service_running_state "${service}")"; then
            return 2
        fi
        [ "${state}" = "running" ] || return 1
    done
    return 0
}

lumen_quiesce_services() {
    [ "$#" -gt 0 ] || return 0
    local attempts="${LUMEN_SERVICE_QUIESCE_ATTEMPTS:-10}"
    local stable_polls="${LUMEN_SERVICE_QUIESCE_STABLE_POLLS:-2}"
    local interval="${LUMEN_SERVICE_STATE_INTERVAL_SECONDS:-1}"
    local poll consecutive=0 state_rc=0
    case "${attempts}:${stable_polls}:${interval}" in
        *[!0-9:]*|0:*|*:0:*) return 2 ;;
    esac
    if [ "${stable_polls}" -gt "${attempts}" ]; then
        return 2
    fi

    lumen_stop_services "$@" >/dev/null 2>&1 || true
    for ((poll = 1; poll <= attempts; poll++)); do
        state_rc=0
        lumen_services_all_stopped "$@" || state_rc=$?
        if [ "${state_rc}" -eq 0 ]; then
            consecutive=$((consecutive + 1))
            if [ "${consecutive}" -ge "${stable_polls}" ]; then
                return 0
            fi
        elif [ "${state_rc}" -eq 1 ]; then
            consecutive=0
            lumen_stop_services "$@" >/dev/null 2>&1 || true
        else
            return 1
        fi
        if [ "${poll}" -lt "${attempts}" ] && [ "${interval}" -gt 0 ]; then
            sleep "${interval}"
        fi
    done
    return 1
}

lumen_quiesce_all_writer_services() {
    lumen_require_no_active_systemd_fallback_writers || return 1
    lumen_quiesce_services "${LUMEN_WRITER_SERVICE_NAMES[@]}" || return 1
    lumen_require_no_active_systemd_fallback_writers
}

lumen_start_services_verified() {
    [ "$#" -gt 0 ] || return 0
    local attempts="${LUMEN_SERVICE_START_ATTEMPTS:-30}"
    local stable_polls="${LUMEN_SERVICE_START_STABLE_POLLS:-2}"
    local interval="${LUMEN_SERVICE_STATE_INTERVAL_SECONDS:-1}"
    local poll consecutive=0 state_rc=0 service needs_core=0
    case "${attempts}:${stable_polls}:${interval}" in
        *[!0-9:]*|0:*|*:0:*) return 2 ;;
    esac
    if [ "${stable_polls}" -gt "${attempts}" ]; then
        return 2
    fi
    for service in "$@"; do
        case "${service}" in
            api|worker|tgbot)
                lumen_require_no_active_systemd_fallback_writers || return 1
                break
                ;;
        esac
    done
    lumen_start_services "$@" || return 1
    for ((poll = 1; poll <= attempts; poll++)); do
        state_rc=0
        lumen_services_all_running "$@" || state_rc=$?
        if [ "${state_rc}" -eq 0 ]; then
            consecutive=$((consecutive + 1))
            if [ "${consecutive}" -ge "${stable_polls}" ]; then
                break
            fi
        elif [ "${state_rc}" -eq 1 ]; then
            consecutive=0
        else
            return 1
        fi
        if [ "${poll}" -lt "${attempts}" ] && [ "${interval}" -gt 0 ]; then
            sleep "${interval}"
        fi
    done
    if [ "${consecutive}" -lt "${stable_polls}" ]; then
        return 1
    fi
    for service in "$@"; do
        case "${service}" in
            api|worker) needs_core=1 ;;
        esac
    done
    if [ "${needs_core}" -eq 0 ]; then
        return 0
    fi

    local compose_dir="${LUMEN_READINESS_COMPOSE_DIR:-${LUMEN_DEPLOY_ROOT:-/opt/lumen}/current}"
    local ready_url="${LUMEN_API_READY_URL:-http://127.0.0.1:8000/readyz}"
    local ready_attempts="${LUMEN_CORE_READINESS_ATTEMPTS:-${attempts}}"
    local ready_interval="${LUMEN_CORE_READINESS_INTERVAL_SECONDS:-${interval}}"
    [ -d "${compose_dir}" ] || compose_dir=""
    lumen_require_compose_core_readiness \
        "${compose_dir}" "${ready_url}" \
        "${ready_attempts}" "${ready_interval}"
}

lumen_validate_redis_rdb_file() {
    local container="$1"
    local source="$2"
    local remote="/tmp/lumen-rdb-check-$$-${RANDOM:-0}.rdb"
    local rc=0
    if [ ! -f "${source}" ] || [ -L "${source}" ] || [ ! -s "${source}" ]; then
        return 1
    fi
    if ! docker cp "${source}" "${container}:${remote}" >/dev/null 2>&1; then
        return 1
    fi
    if ! docker exec "${container}" redis-check-rdb "${remote}" \
            >/dev/null 2>&1; then
        rc=1
    fi
    if ! docker exec "${container}" rm -f "${remote}" >/dev/null 2>&1; then
        rc=1
    fi
    return "${rc}"
}
