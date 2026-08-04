#!/usr/bin/env bash
# Durable crash recovery for restore.sh. The caller provides rollback helpers.

LUMEN_RESTORE_STATE_DIR="${LUMEN_RESTORE_STATE_DIR:-/var/lib/lumen/restore}"
LUMEN_RESTORE_JOURNAL_FILE="${LUMEN_RESTORE_JOURNAL_FILE:-${LUMEN_RESTORE_STATE_DIR}/active.json}"
LUMEN_RESTORE_JOURNAL_HELPER="${LUMEN_RESTORE_JOURNAL_HELPER:-${SCRIPT_DIR}/restore_journal.py}"
RESTORE_REQUEST_OPERATION_ID="${RESTORE_REQUEST_OPERATION_ID:-${TS}:$$}"
RESTORE_REQUEST_TIMESTAMP="${RESTORE_REQUEST_TIMESTAMP:-${TS}}"
RESTORE_OPERATION_ID="${RESTORE_OPERATION_ID:-${RESTORE_REQUEST_OPERATION_ID}}"
RESTORE_OPERATION_TIMESTAMP="${RESTORE_OPERATION_TIMESTAMP:-${RESTORE_REQUEST_TIMESTAMP}}"
RESTORE_BACKUP_OPERATION_ID="${RESTORE_BACKUP_OPERATION_ID:-}"
RESTORE_BACKUP_PAIR_MARKER="${RESTORE_BACKUP_PAIR_MARKER:-}"
RESTORE_BACKUP_PG_PATH="${RESTORE_BACKUP_PG_PATH:-}"
RESTORE_BACKUP_REDIS_PATH="${RESTORE_BACKUP_REDIS_PATH:-}"
RESTORE_BACKUP_PG_SIZE="${RESTORE_BACKUP_PG_SIZE:-0}"
RESTORE_BACKUP_REDIS_SIZE="${RESTORE_BACKUP_REDIS_SIZE:-0}"
RESTORE_BACKUP_PG_SHA256="${RESTORE_BACKUP_PG_SHA256:-}"
RESTORE_BACKUP_REDIS_SHA256="${RESTORE_BACKUP_REDIS_SHA256:-}"
RESTORE_JOURNAL_ACTIVE=0

lumen_restore_fsync_redis_phase() {
    local phase="$1"
    if ! python3 "$LUMEN_RESTORE_JOURNAL_HELPER" \
            redis-state-fsync \
            --phase "$phase" \
            --host-dir "${REDIS_HOST_DIR:-}" \
            --backup-dir "${REDIS_BACKUP_DIR:-}" \
            --manifest "${REDIS_ORIGINAL_MANIFEST:-}"; then
        log "ERROR: failed to durably persist redis filesystem state before phase $phase"
        return 1
    fi
}

lumen_restore_journal_write() {
    local phase="$1"
    local service
    case "$phase" in
        redis_stashing|redis_stashed|redis_applying|redis_applied|redis_rolling_back|redis_rolled_back)
            if ! lumen_restore_fsync_redis_phase "$phase"; then
                return 1
            fi
            ;;
    esac
    local -a args=(
        write "$LUMEN_RESTORE_JOURNAL_FILE"
        --operation-id "$RESTORE_OPERATION_ID"
        --timestamp "$RESTORE_OPERATION_TIMESTAMP"
        --phase "$phase"
        --pg-db "$PG_DB"
        --pg-container "$PG_CONTAINER"
        --redis-container "$REDIS_CONTAINER"
        --pg-temp-db "${PG_TEMP_DB:-}"
        --pg-rollback-db "${PG_ROLLBACK_DB:-}"
        --redis-host-dir "${REDIS_HOST_DIR:-}"
        --redis-backup-dir "${REDIS_BACKUP_DIR:-}"
        --redis-original-manifest "${REDIS_ORIGINAL_MANIFEST:-}"
        --redis-state "${REDIS_RESTORE_STATE:-untouched}"
        --backup-operation-id "${RESTORE_BACKUP_OPERATION_ID:-}"
        --backup-pair-marker "${RESTORE_BACKUP_PAIR_MARKER:-}"
        --pg-backup-path "${RESTORE_BACKUP_PG_PATH:-}"
        --redis-backup-path "${RESTORE_BACKUP_REDIS_PATH:-}"
        --pg-backup-size "${RESTORE_BACKUP_PG_SIZE:-0}"
        --redis-backup-size "${RESTORE_BACKUP_REDIS_SIZE:-0}"
        --pg-backup-sha256 "${RESTORE_BACKUP_PG_SHA256:-}"
        --redis-backup-sha256 "${RESTORE_BACKUP_REDIS_SHA256:-}"
        --services-stopped "${SERVICES_STOPPED:-0}"
        --redis-needs-start "${REDIS_NEEDS_START:-0}"
        --pg-swap-in-progress "${PG_SWAP_IN_PROGRESS:-0}"
        --pg-promoted "${PG_PROMOTED:-0}"
    )
    for service in "${ACTIVE_WRITER_SERVICES[@]}"; do
        args+=(--service "$service")
    done
    for service in "${ACTIVE_SITE_SERVICES[@]}"; do
        args+=(--site-service "$service")
    done
    if ! python3 "$LUMEN_RESTORE_JOURNAL_HELPER" "${args[@]}"; then
        log "ERROR: failed to persist restore crash journal at phase $phase"
        return 1
    fi
    RESTORE_JOURNAL_ACTIVE=1
}

lumen_restore_journal_clear() {
    if ! python3 "$LUMEN_RESTORE_JOURNAL_HELPER" \
            clear "$LUMEN_RESTORE_JOURNAL_FILE"; then
        log "ERROR: failed to clear restore crash journal"
        return 1
    fi
    RESTORE_JOURNAL_ACTIVE=0
}

lumen_restore_journal_load() {
    local assignments rc
    set +e
    assignments="$(
        python3 "$LUMEN_RESTORE_JOURNAL_HELPER" \
            load-shell "$LUMEN_RESTORE_JOURNAL_FILE"
    )"
    rc=$?
    set -e
    if [ "$rc" -eq 3 ]; then
        return 1
    fi
    if [ "$rc" -ne 0 ]; then
        log "ERROR: restore crash journal is present but invalid"
        return 2
    fi
    eval "$assignments"
    RESTORE_JOURNAL_ACTIVE=1
}

lumen_restore_apply_loaded_backup_binding() {
    RESTORE_BACKUP_OPERATION_ID="$RESTORE_JOURNAL_BACKUP_OPERATION_ID"
    RESTORE_BACKUP_PAIR_MARKER="$RESTORE_JOURNAL_BACKUP_PAIR_MARKER"
    RESTORE_BACKUP_PG_PATH="$RESTORE_JOURNAL_PG_BACKUP_PATH"
    RESTORE_BACKUP_REDIS_PATH="$RESTORE_JOURNAL_REDIS_BACKUP_PATH"
    RESTORE_BACKUP_PG_SIZE="$RESTORE_JOURNAL_PG_BACKUP_SIZE"
    RESTORE_BACKUP_REDIS_SIZE="$RESTORE_JOURNAL_REDIS_BACKUP_SIZE"
    RESTORE_BACKUP_PG_SHA256="$RESTORE_JOURNAL_PG_BACKUP_SHA256"
    RESTORE_BACKUP_REDIS_SHA256="$RESTORE_JOURNAL_REDIS_BACKUP_SHA256"
}

lumen_restore_bind_backup_pair() {
    local assignments
    if ! assignments="$(
        python3 "$LUMEN_RESTORE_JOURNAL_HELPER" \
            backup-pair-bind-shell "$BACKUP_ROOT" "$TS"
    )"; then
        log "ERROR: backup pair marker or payload binding is invalid for $TS"
        return 1
    fi
    eval "$assignments"
}

lumen_restore_verify_bound_backup_pair() {
    python3 "$LUMEN_RESTORE_JOURNAL_HELPER" \
        backup-pair-verify-bound \
        "$BACKUP_ROOT" \
        "$TS" \
        "$RESTORE_BACKUP_PG_PATH" \
        "$RESTORE_BACKUP_REDIS_PATH" \
        "$RESTORE_BACKUP_PG_SIZE" \
        "$RESTORE_BACKUP_REDIS_SIZE" \
        "$RESTORE_BACKUP_PG_SHA256" \
        "$RESTORE_BACKUP_REDIS_SHA256" \
        --operation-id "$RESTORE_BACKUP_OPERATION_ID" \
        --pair-marker "$RESTORE_BACKUP_PAIR_MARKER"
}

lumen_restore_prepare_backup_pair() {
    if [ -z "${RESTORE_BACKUP_OPERATION_ID:-}" ]; then
        if ! lumen_restore_bind_backup_pair; then
            return 1
        fi
    elif ! lumen_restore_verify_bound_backup_pair; then
        log "ERROR: bound backup pair changed or no longer matches its marker"
        return 1
    fi
    PG_FILE="$RESTORE_BACKUP_PG_PATH"
    REDIS_FILE="$RESTORE_BACKUP_REDIS_PATH"
}

lumen_restore_validate_loaded_journal() {
    local service resolved_host backup_parent backup_name
    [ "$RESTORE_JOURNAL_PG_DB" = "$PG_DB" ] \
        && [ "$RESTORE_JOURNAL_PG_CONTAINER" = "$PG_CONTAINER" ] \
        && [ "$RESTORE_JOURNAL_REDIS_CONTAINER" = "$REDIS_CONTAINER" ] || {
        log "ERROR: restore crash journal targets do not match current containers/database"
        return 1
    }
    case "$RESTORE_JOURNAL_PG_TEMP_DB" in
        ""|lumen_restore_*) ;;
        *) log "ERROR: invalid postgres temporary database in restore journal"; return 1 ;;
    esac
    case "$RESTORE_JOURNAL_PG_ROLLBACK_DB" in
        ""|lumen_rollback_*) ;;
        *) log "ERROR: invalid postgres rollback database in restore journal"; return 1 ;;
    esac

    ACTIVE_WRITER_SERVICES=()
    for service in $RESTORE_JOURNAL_ACTIVE_WRITER_SERVICES; do
        case "$service" in
            api|worker|tgbot) ACTIVE_WRITER_SERVICES+=("$service") ;;
            *) log "ERROR: invalid writer service in restore journal"; return 1 ;;
        esac
    done
    ACTIVE_SITE_SERVICES=()
    for service in $RESTORE_JOURNAL_ACTIVE_SITE_SERVICES; do
        case "$service" in
            web) ACTIVE_SITE_SERVICES+=("$service") ;;
            *) log "ERROR: invalid site service in restore journal"; return 1 ;;
        esac
    done

    REDIS_HOST_DIR="$RESTORE_JOURNAL_REDIS_HOST_DIR"
    REDIS_BACKUP_DIR="$RESTORE_JOURNAL_REDIS_BACKUP_DIR"
    REDIS_ORIGINAL_MANIFEST="$RESTORE_JOURNAL_REDIS_ORIGINAL_MANIFEST"
    if [ -n "$REDIS_HOST_DIR" ]; then
        if ! resolved_host="$(validate_redis_host_dir "$REDIS_HOST_DIR")"; then
            return 1
        fi
        REDIS_HOST_DIR="$resolved_host"
    fi
    if [ -n "$REDIS_BACKUP_DIR" ]; then
        [ -n "$REDIS_HOST_DIR" ] || {
            log "ERROR: restore journal has a redis backup without a host directory"
            return 1
        }
        backup_parent="$(dirname -- "$REDIS_BACKUP_DIR")"
        backup_name="$(basename -- "$REDIS_BACKUP_DIR")"
        [ "$backup_parent" = "$REDIS_HOST_DIR" ] || {
            log "ERROR: redis rollback directory escaped its validated volume"
            return 1
        }
        case "$backup_name" in
            "${REDIS_BACKUP_PREFIX}"*) ;;
            *) log "ERROR: invalid redis rollback directory in restore journal"; return 1 ;;
        esac
        [ "$REDIS_ORIGINAL_MANIFEST" = "$REDIS_BACKUP_DIR/.original-items" ] || {
            log "ERROR: invalid redis rollback manifest in restore journal"
            return 1
        }
    elif [ -n "$REDIS_ORIGINAL_MANIFEST" ]; then
        log "ERROR: restore journal has an orphan redis rollback manifest"
        return 1
    fi

    PG_TEMP_DB="$RESTORE_JOURNAL_PG_TEMP_DB"
    PG_ROLLBACK_DB="$RESTORE_JOURNAL_PG_ROLLBACK_DB"
    RESTORE_OPERATION_ID="$RESTORE_JOURNAL_OPERATION_ID"
    RESTORE_OPERATION_TIMESTAMP="$RESTORE_JOURNAL_TIMESTAMP"
    PG_SWAP_IN_PROGRESS="$RESTORE_JOURNAL_PG_SWAP_IN_PROGRESS"
    PG_PROMOTED="$RESTORE_JOURNAL_PG_PROMOTED"
    REDIS_RESTORE_STATE="$RESTORE_JOURNAL_REDIS_STATE"
    REDIS_NEEDS_START="$RESTORE_JOURNAL_REDIS_NEEDS_START"
    SERVICES_STOPPED="$RESTORE_JOURNAL_SERVICES_STOPPED"
    lumen_restore_apply_loaded_backup_binding
}

lumen_restore_reset_recovered_state() {
    SERVICES_STOPPED=0
    REDIS_NEEDS_START=0
    PG_TEMP_DB=""
    PG_ROLLBACK_DB=""
    PG_SWAP_IN_PROGRESS=0
    PG_PROMOTED=0
    REDIS_HOST_DIR=""
    REDIS_BACKUP_DIR=""
    REDIS_ORIGINAL_MANIFEST=""
    REDIS_RESTORE_STATE="untouched"
    RESTORE_BACKUP_OPERATION_ID=""
    RESTORE_BACKUP_PAIR_MARKER=""
    RESTORE_BACKUP_PG_PATH=""
    RESTORE_BACKUP_REDIS_PATH=""
    RESTORE_BACKUP_PG_SIZE=0
    RESTORE_BACKUP_REDIS_SIZE=0
    RESTORE_BACKUP_PG_SHA256=""
    RESTORE_BACKUP_REDIS_SHA256=""
    ACTIVE_WRITER_SERVICES=()
    ACTIVE_SITE_SERVICES=()
    RESTORE_OPERATION_ID="$RESTORE_REQUEST_OPERATION_ID"
    RESTORE_OPERATION_TIMESTAMP="$RESTORE_REQUEST_TIMESTAMP"
}

lumen_restore_recover_interrupted() {
    local load_rc=0
    lumen_restore_journal_load || load_rc=$?
    if [ "$load_rc" -eq 1 ]; then
        return 0
    fi
    if [ "$load_rc" -ne 0 ]; then
        return 1
    fi
    if [ "$RESTORE_JOURNAL_PHASE" = "request_pending" ]; then
        if [ "$RESTORE_JOURNAL_TIMESTAMP" != "$RESTORE_REQUEST_TIMESTAMP" ]; then
            log "ERROR: pending restore journal belongs to a different request"
            return 1
        fi
        RESTORE_OPERATION_ID="$RESTORE_JOURNAL_OPERATION_ID"
        RESTORE_OPERATION_TIMESTAMP="$RESTORE_JOURNAL_TIMESTAMP"
        lumen_restore_apply_loaded_backup_binding
        # Pre-operational failures must retain this durable request. The first
        # operational journal write will reactivate normal cleanup semantics.
        RESTORE_JOURNAL_ACTIVE=0
        log "accepted durable pending restore request $RESTORE_OPERATION_ID"
        return 0
    fi
    if ! redis_cleanup_stale_bootstrap_containers; then
        log "ERROR: failed to clean a stale Redis RDB bootstrap container"
        return 1
    fi
    if ! lumen_restore_validate_loaded_journal; then
        return 1
    fi

    log "recovering interrupted restore operation $RESTORE_JOURNAL_OPERATION_ID phase=$RESTORE_JOURNAL_PHASE"
    SERVICES_STOPPED=1
    case "$RESTORE_JOURNAL_PHASE" in
        pg_promoting|pg_promoted|committed)
            PG_SWAP_IN_PROGRESS=1
            if ! pg_recover_active_from_rollback; then
                return 1
            fi
            ;;
    esac

    if [ "$PG_PROMOTED" -eq 1 ]; then
        REDIS_RESTORE_STATE="committed"
    else
        case "$REDIS_RESTORE_STATE" in
            stashing|stashed|applying|applied|rolling_back)
                if ! redis_rollback_after_pg_failure; then
                    return 1
                fi
                ;;
            rolled_back|untouched) ;;
            committed)
                log "ERROR: restore journal claims committed redis without promoted postgres"
                return 1
                ;;
            *)
                log "ERROR: unknown redis state in restore journal: $REDIS_RESTORE_STATE"
                return 1
                ;;
        esac
    fi

    if [ "$REDIS_NEEDS_START" -eq 1 ] || [ "$PG_PROMOTED" -eq 1 ]; then
        if ! ensure_redis_started; then
            return 1
        fi
        REDIS_NEEDS_START=0
    fi
    if [ -n "$PG_TEMP_DB" ]; then
        if ! pg_drop_database_if_exists "$PG_TEMP_DB"; then
            log "ERROR: failed to drop interrupted postgres staging database $PG_TEMP_DB"
            return 1
        fi
        PG_TEMP_DB=""
    fi
    if ! _restore_compose_start_services; then
        log "ERROR: failed to restart writers after interrupted restore recovery"
        return 1
    fi
    SERVICES_STOPPED=0
    if [ "$PG_PROMOTED" -eq 1 ] \
            && ! pg_discard_rollback_after_success; then
        log "ERROR: interrupted restore passed readiness, but rollback cleanup is incomplete"
        return 1
    fi
    if ! lumen_restore_journal_clear; then
        return 1
    fi
    log "interrupted restore recovered to a consistent state"
    lumen_restore_reset_recovered_state
}
