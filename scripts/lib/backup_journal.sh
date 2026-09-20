#!/usr/bin/env bash
# Durable service recovery journal for backup.sh.

LUMEN_BACKUP_EXECUTION_DOMAIN="${LUMEN_BACKUP_EXECUTION_DOMAIN:-scheduled}"
if [ -z "${LUMEN_BACKUP_JOURNAL_FILE:-}" ]; then
    case "$LUMEN_BACKUP_EXECUTION_DOMAIN" in
        scheduled)
            LUMEN_BACKUP_JOURNAL_FILE="${BACKUP_ROOT}/.recovery/backup.json"
            ;;
        update)
            _backup_journal_operation="${BACKUP_OPERATION_ID//[^A-Za-z0-9._-]/_}"
            [ -n "$_backup_journal_operation" ] || _backup_journal_operation="unknown"
            _backup_journal_root="${LUMEN_BACKUP_UPDATE_JOURNAL_ROOT:-/var/lib/lumen/backup/update}"
            LUMEN_BACKUP_JOURNAL_FILE="${_backup_journal_root}/${_backup_journal_operation}.json"
            unset _backup_journal_operation
            unset _backup_journal_root
            ;;
        *)
            log "ERROR: unsupported backup execution domain: $LUMEN_BACKUP_EXECUTION_DOMAIN"
            return 1 2>/dev/null || exit 1
            ;;
    esac
fi
LUMEN_BACKUP_JOURNAL_HELPER="${LUMEN_BACKUP_JOURNAL_HELPER:-${SCRIPT_DIR}/restore_journal.py}"
BACKUP_JOURNAL_ACTIVE=0
BACKUP_JOURNAL_RECOVERED=0

lumen_backup_journal_write() {
    local phase="$1"
    local service
    local -a args=(
        backup-write "$LUMEN_BACKUP_JOURNAL_FILE"
        --operation-id "${BACKUP_OPERATION_ID}"
        --phase "$phase"
    )
    for service in "${ACTIVE_WRITER_SERVICES[@]}"; do
        args+=(--service "$service")
    done
    if ! python3 "$LUMEN_BACKUP_JOURNAL_HELPER" "${args[@]}"; then
        log "ERROR: failed to persist backup recovery journal at phase $phase"
        return 1
    fi
    BACKUP_JOURNAL_ACTIVE=1
}

lumen_backup_journal_clear() {
    if ! python3 "$LUMEN_BACKUP_JOURNAL_HELPER" \
            backup-clear "$LUMEN_BACKUP_JOURNAL_FILE"; then
        log "ERROR: failed to clear backup recovery journal"
        return 1
    fi
    BACKUP_JOURNAL_ACTIVE=0
}

lumen_backup_journal_load() {
    local assignments rc
    set +e
    assignments="$(
        python3 "$LUMEN_BACKUP_JOURNAL_HELPER" \
            backup-load-shell "$LUMEN_BACKUP_JOURNAL_FILE"
    )"
    rc=$?
    set -e
    if [ "$rc" -eq 3 ]; then
        return 1
    fi
    if [ "$rc" -ne 0 ]; then
        log "ERROR: backup recovery journal is present but invalid"
        return 2
    fi
    eval "$assignments"
    BACKUP_JOURNAL_ACTIVE=1
}

lumen_backup_recover_interrupted() {
    local load_rc=0 service
    lumen_backup_journal_load || load_rc=$?
    if [ "$load_rc" -eq 1 ]; then
        return 0
    fi
    [ "$load_rc" -eq 0 ] || return 1
    case "$BACKUP_JOURNAL_PHASE" in
        writers_stopping|writers_stopped|writers_starting) ;;
        *)
            log "ERROR: unknown backup recovery phase: $BACKUP_JOURNAL_PHASE"
            return 1
            ;;
    esac
    ACTIVE_WRITER_SERVICES=()
    for service in $BACKUP_JOURNAL_ACTIVE_WRITER_SERVICES; do
        case "$service" in
            api|worker|tgbot) ACTIVE_WRITER_SERVICES+=("$service") ;;
            *)
                log "ERROR: invalid writer service in backup recovery journal"
                return 1
                ;;
        esac
    done
    log "recovering interrupted backup operation ${BACKUP_JOURNAL_OPERATION_ID}"
    if [ "${#ACTIVE_WRITER_SERVICES[@]}" -gt 0 ] \
            && ! lumen_start_services_verified "${ACTIVE_WRITER_SERVICES[@]}"; then
        log "ERROR: failed to restore writers after interrupted backup"
        return 1
    fi
    WRITERS_STOPPED=0
    if ! lumen_backup_journal_clear; then
        return 1
    fi
    ACTIVE_WRITER_SERVICES=()
    BACKUP_JOURNAL_RECOVERED=1
    log "interrupted backup service state recovered"
}

# Success resumes writers early; EXIT finalizes the journal. Keeping the
# journal through retention makes SIGKILL recovery restart-only, not a second
# backup that pauses already healthy writers again.
lumen_backup_resume_writers() {
    local finalize="${1:-1}"
    local journal_rc=0
    if [ "${WRITERS_STOPPED:-0}" -eq 1 ]; then
        log "restarting quiesced writers: ${ACTIVE_WRITER_SERVICES[*]:-<none>}"
        if [ "${BACKUP_JOURNAL_ACTIVE:-0}" -eq 1 ]; then
            lumen_backup_journal_write "writers_starting" || journal_rc=70
        fi
        if [ "${#ACTIVE_WRITER_SERVICES[@]}" -gt 0 ] \
                && ! lumen_start_services_verified "${ACTIVE_WRITER_SERVICES[@]}"; then
            log "ERROR: failed to restart one or more backup writers"
            return 70
        fi
        # Never erase evidence when persisting the state transition failed.
        [ "$journal_rc" -eq 0 ] || return "$journal_rc"
        WRITERS_STOPPED=0
        BACKUP_WRITERS_RESUMED=1
        log "backup writers restored and readiness verified"
    fi
    if [ "$finalize" = "1" ] \
            && [ "${BACKUP_WRITERS_RESUMED:-0}" = "1" ] \
            && [ "${BACKUP_JOURNAL_ACTIVE:-0}" = "1" ]; then
        lumen_backup_journal_clear || return 70
    fi
    return 0
}
