#!/usr/bin/env bash
# Atomic updater journal shell interface and deterministic failpoints.

LUMEN_UPDATE_JOURNAL_SCHEMA=2
LUMEN_UPDATE_JOURNAL_READY=0
LUMEN_UPDATE_JOURNAL_RESUMED=0
lumen_update_journal_path() {
    if [ -n "${LUMEN_UPDATE_JOURNAL:-}" ]; then
        printf '%s\n' "${LUMEN_UPDATE_JOURNAL}"
    else
        printf '%s\n' "${SHARED_DIR:?}/.update-journal.json"
    fi
}

lumen_update_journal_store_path() {
    local module_dir="${UPDATE_MODULE_DIR:-}"
    if [ -z "${module_dir}" ]; then
        module_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi
    printf '%s\n' "${module_dir}/journal_store.py"
}

lumen_update_file_sha256() {
    python3 - "$1" <<'PY'
import hashlib
from pathlib import Path
import sys

path = Path(sys.argv[1])
digest = hashlib.sha256()
with path.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

lumen_update_copy_file_durable() {
    python3 - "$1" "$2" <<'PY'
import errno
import os
from pathlib import Path
import stat
import sys
import tempfile

source = Path(sys.argv[1])
target = Path(sys.argv[2])


def reject_unsafe_destination() -> None:
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise SystemExit(f"destination symlink is not allowed: {target}")
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"destination is not a regular file: {target}")


reject_unsafe_destination()
fd, temporary_raw = tempfile.mkstemp(
    prefix=f".{target.name}.",
    suffix=".tmp",
    dir=target.parent,
)
temporary = Path(temporary_raw)
try:
    with source.open("rb") as source_handle, os.fdopen(fd, "wb") as target_handle:
        for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
            target_handle.write(chunk)
        os.fchmod(target_handle.fileno(), 0o600)
        target_handle.flush()
        os.fsync(target_handle.fileno())
    reject_unsafe_destination()
    os.replace(temporary, target)
    directory_fd = os.open(
        target.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
                raise
    finally:
        os.close(directory_fd)
finally:
    temporary.unlink(missing_ok=True)
PY
}

lumen_update_fsync_directory() {
    python3 - "$1" <<'PY'
import errno
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
directory_fd = os.open(
    path,
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
)
try:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
            raise
finally:
    os.close(directory_fd)
PY
}

lumen_update_journal_capture_runtime() {
    UPDATE_RUNTIME_CURRENT_KIND="unknown"
    UPDATE_RUNTIME_CURRENT_PRESENT=0
    UPDATE_RUNTIME_CURRENT_TARGET=""
    UPDATE_RUNTIME_PREVIOUS_KIND="unknown"
    UPDATE_RUNTIME_PREVIOUS_PRESENT=0
    UPDATE_RUNTIME_PREVIOUS_TARGET=""
    UPDATE_RUNTIME_ENV_SHA256=""
    UPDATE_RUNTIME_MIGRATION_HEAD="${UPDATE_RUNTIME_MIGRATION_HEAD:-}"
    UPDATE_RUNTIME_IMAGE_SET_DIGEST=""
    UPDATE_RUNTIME_ROLLING_DIGEST=""

    if [ -n "${ROOT:-}" ]; then
        if [ -L "${ROOT}/current" ]; then
            UPDATE_RUNTIME_CURRENT_KIND="symlink"
            UPDATE_RUNTIME_CURRENT_PRESENT=1
            UPDATE_RUNTIME_CURRENT_TARGET="$(readlink "${ROOT}/current")"
        elif [ -e "${ROOT}/current" ]; then
            UPDATE_RUNTIME_CURRENT_KIND="other"
        else
            UPDATE_RUNTIME_CURRENT_KIND="missing"
        fi
        if [ -L "${ROOT}/previous" ]; then
            UPDATE_RUNTIME_PREVIOUS_KIND="symlink"
            UPDATE_RUNTIME_PREVIOUS_PRESENT=1
            UPDATE_RUNTIME_PREVIOUS_TARGET="$(readlink "${ROOT}/previous")"
        elif [ -e "${ROOT}/previous" ]; then
            UPDATE_RUNTIME_PREVIOUS_KIND="other"
        else
            UPDATE_RUNTIME_PREVIOUS_KIND="missing"
        fi
    fi
    if [ -n "${SHARED_ENV:-}" ] && [ -f "${SHARED_ENV}" ]; then
        UPDATE_RUNTIME_ENV_SHA256="$(lumen_update_file_sha256 "${SHARED_ENV}")"
    fi
    if [ -n "${TARGET_IMAGE_SET_DIGEST:-}" ] \
            && command -v lumen_update_bound_image_set_digest \
                >/dev/null 2>&1; then
        UPDATE_RUNTIME_IMAGE_SET_DIGEST="$(
            lumen_update_bound_image_set_digest 2>/dev/null || true
        )"
    fi
    if [ -n "${TARGET_ROLLING_DIGEST:-}" ]; then
        UPDATE_RUNTIME_ROLLING_DIGEST="${UPDATE_RUNTIME_IMAGE_SET_DIGEST}"
    fi

    export UPDATE_RUNTIME_CURRENT_KIND UPDATE_RUNTIME_CURRENT_PRESENT
    export UPDATE_RUNTIME_CURRENT_TARGET UPDATE_RUNTIME_PREVIOUS_KIND
    export UPDATE_RUNTIME_PREVIOUS_PRESENT UPDATE_RUNTIME_PREVIOUS_TARGET
    export UPDATE_RUNTIME_ENV_SHA256 UPDATE_RUNTIME_MIGRATION_HEAD
    export UPDATE_RUNTIME_IMAGE_SET_DIGEST
    export UPDATE_RUNTIME_ROLLING_DIGEST
}

lumen_update_journal_exec() {
    local action="$1"
    shift
    python3 "$(lumen_update_journal_store_path)" \
        "${action}" "$(lumen_update_journal_path)" "$@"
}

lumen_update_journal_init() {
    local result resumed
    result="$(
        lumen_update_journal_exec \
            init \
            "${OPERATION_ID:?}" \
            "${LUMEN_UPDATE_RESUME:-0}"
    )"
    OPERATION_ID="${result%%	*}"
    resumed="${result#*	}"
    export OPERATION_ID
    LUMEN_UPDATE_JOURNAL_RESUMED="${resumed}"
    LUMEN_UPDATE_JOURNAL_READY=1
    if [ "${resumed}" = "1" ]; then
        eval "$(lumen_update_journal_exec restore-context)"
    fi
}

lumen_update_journal_export_context() {
    export ROOT SHARED_ENV
    export CURRENT_ID CURRENT_RELEASE CURRENT_TAG NEW_ID NEW_RELEASE
    export PREVIOUS_TAG TARGET_TAG TARGET_RELEASE_TAG TARGET_VERSION
    export REPO_DIR RELEASE_SOURCE_REF RELEASE_SOURCE_IMAGE_EXTRACT
    export RELEASE_SOURCE_COMMIT RELEASE_SOURCE_COMMIT_PROOF
    export RELEASE_SOURCE_PROOF_FILE RELEASE_SOURCE_PROOF_SHA256
    export RELEASE_SOURCE_TREE_SHA256
    export RELEASE_EXPECTED_COMMIT RELEASE_SOURCE_API_IMAGE
    export RELEASE_SOURCE_MANIFEST_CACHE RELEASE_MANIFEST_FILE
    export RELEASE_MANIFEST_SHA256 RELEASE_MANIFEST_TAG
    export RELEASE_IMAGE_TAG_FILE RELEASE_IMAGE_TAG_SHA256
    export TARGET_IMAGE_PROOF_FILE TARGET_IMAGE_PROOF_SHA256 TARGET_IMAGE_SET_DIGEST
    export TARGET_IMAGE_OVERRIDE_FILE TARGET_IMAGE_OVERRIDE_SHA256
    export TARGET_ROLLING_DIGEST TGBOT_IMAGE_READY LUMEN_IMAGE_REGISTRY
    export LUMEN_UPDATE_BUILD SKIP_TO_CLEANUP CURRENT_LINK
    export API_HEALTH_URL WEB_HEALTH_URL UPDATE_STATE_SNAPSHOT_READY
    export UPDATE_ENV_SNAPSHOT UPDATE_HOST_ARTIFACT_SNAPSHOT
    export UPDATE_SNAPSHOT_LINKS_KNOWN UPDATE_SNAPSHOT_ENV_SHA256
    export UPDATE_ORIGINAL_CURRENT_PRESENT UPDATE_ORIGINAL_CURRENT_TARGET
    export UPDATE_ORIGINAL_PREVIOUS_PRESENT UPDATE_ORIGINAL_PREVIOUS_TARGET
    export UPDATE_STATE_COMMITTED
    export UPDATE_RELEASE_SWITCHED UPDATE_OLD_SERVICES_STOPPED
    export UPDATE_MIGRATION_STARTED UPDATE_MIGRATION_VERIFIED
    export UPDATE_MIGRATION_HEAD
    export UPDATE_RESTORE_POINT_TIMESTAMP UPDATE_RESTORE_POINT_PG
    export UPDATE_RESTORE_POINT_REDIS
}
lumen_update_journal_refresh_state() {
    lumen_update_journal_capture_runtime
    lumen_update_journal_export_context
}
lumen_update_journal_phase_is_recoverable() {
    if command -v lumen_update_phase_is_resumable >/dev/null 2>&1; then
        lumen_update_phase_is_resumable "$1"
        return
    fi
    return 0
}

lumen_update_journal_phase_start() {
    local recoverable=0
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 0
    if lumen_update_journal_phase_is_recoverable "$1"; then
        recoverable=1
    fi
    lumen_update_journal_refresh_state
    lumen_update_journal_exec phase-start "$1" "${recoverable}"
}

lumen_update_journal_phase_done() {
    local recoverable=0
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 0
    if lumen_update_journal_phase_is_recoverable "$1"; then
        recoverable=1
    fi
    lumen_update_journal_refresh_state
    lumen_update_journal_exec phase-done "$1" "${recoverable}"
}

lumen_update_journal_phase_completed() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 1
    [ "$(lumen_update_journal_exec phase-completed "$1")" = "1" ]
}

lumen_update_journal_failed() {
    local recoverable=0
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 0
    if [ -n "${1:-}" ] && lumen_update_journal_phase_is_recoverable "$1"; then
        recoverable=1
    fi
    lumen_update_journal_refresh_state
    lumen_update_journal_exec failed "${1:-}" "${2:-1}" "${recoverable}"
}

lumen_update_journal_status() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 0
    lumen_update_journal_refresh_state
    lumen_update_journal_exec status "$1"
}

lumen_update_journal_snapshot_state() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 1
    lumen_update_journal_refresh_state
    lumen_update_journal_exec snapshot
}

lumen_update_journal_mark_committed() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 1
    local previous_committed="${UPDATE_STATE_COMMITTED:-0}"
    lumen_update_journal_refresh_state
    if ! lumen_update_journal_exec mark-committed; then
        UPDATE_STATE_COMMITTED="${previous_committed}"
        export UPDATE_STATE_COMMITTED
        return 1
    fi
    UPDATE_STATE_COMMITTED=1
    export UPDATE_STATE_COMMITTED
}

lumen_update_journal_expect_env_file() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 0
    lumen_update_journal_exec expect-env "$1"
}

lumen_update_journal_bind_request() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 1
    lumen_update_journal_refresh_state
    printf '%s' "${LUMEN_UPDATE_IDEMPOTENCY_KEY:-}" \
        | python3 "$(lumen_update_journal_store_path)" \
        bind-request "$(lumen_update_journal_path)" \
        "${LUMEN_UPDATE_CHANNEL:-stable}" \
        "${TARGET_TAG:?}" \
        "${LUMEN_UPDATE_FORCE_REDEPLOY:-0}"
}

lumen_update_release_source_sha256() {
    lumen_update_journal_exec source-tree-sha256 "$1"
}

lumen_update_journal_bind_target() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 1
    lumen_update_journal_refresh_state
    lumen_update_journal_exec \
        bind-target \
        "${TARGET_TAG:?}" \
        "${TARGET_RELEASE_TAG:-}" \
        "${NEW_RELEASE:?}" \
        "${NEW_ID:?}" \
        "${RELEASE_SOURCE_COMMIT:?}" \
        "${RELEASE_SOURCE_COMMIT_PROOF:?}" \
        "${RELEASE_SOURCE_PROOF_FILE:?}" \
        "${RELEASE_SOURCE_PROOF_SHA256:?}" \
        "${RELEASE_SOURCE_TREE_SHA256:?}" \
        "${RELEASE_MANIFEST_SHA256:-}" \
        "${RELEASE_SOURCE_MANIFEST_CACHE:-}" \
        "${RELEASE_MANIFEST_FILE:-}" \
        "${RELEASE_IMAGE_TAG_FILE:-}" \
        "${RELEASE_IMAGE_TAG_SHA256:-}" \
        "${TARGET_ROLLING_DIGEST:-}" \
        "${TARGET_IMAGE_PROOF_FILE:-}" \
        "${TARGET_IMAGE_PROOF_SHA256:-}" \
        "${TARGET_IMAGE_OVERRIDE_FILE:-}" \
        "${TARGET_IMAGE_OVERRIDE_SHA256:-}" \
        "${TARGET_IMAGE_SET_DIGEST:-}"
}

lumen_update_journal_assert_target_field() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 1
    lumen_update_journal_exec assert-target-field "$1" "$2"
}

lumen_update_journal_validate_resume() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 1
    lumen_update_journal_refresh_state
    lumen_update_journal_exec validate-resume || return 1
    if [ -n "${TARGET_IMAGE_SET_DIGEST:-}" ] \
            && command -v lumen_update_activate_bound_image_override \
                >/dev/null 2>&1; then
        lumen_update_activate_bound_image_override
    fi
}

lumen_update_failpoint_matches() {
    local timing="$1" phase="$2" item
    local configured="${LUMEN_UPDATE_FAILPOINT:-}"
    configured="${configured},${LUMEN_UPDATE_FAILPOINTS:-}"
    local old_ifs="${IFS}"
    IFS=","
    for item in ${configured}; do
        case "${item}" in
            "${timing}:${phase}"|"${phase}:${timing}")
                IFS="${old_ifs}"
                return 0
                ;;
            "${phase}")
                if [ "${timing}" = "before" ]; then
                    IFS="${old_ifs}"
                    return 0
                fi
                ;;
        esac
    done
    IFS="${old_ifs}"
    return 1
}

lumen_update_failpoint() {
    local timing="$1" phase="$2"
    local exit_code="${LUMEN_UPDATE_FAILPOINT_EXIT_CODE:-97}"
    if ! lumen_update_failpoint_matches "${timing}" "${phase}"; then
        return 0
    fi
    lumen_update_journal_failed "${phase}" "${exit_code}" || true
    if command -v lumen_emit_info >/dev/null 2>&1; then
        lumen_emit_info \
            "phase=${phase}" \
            "key=failpoint" \
            "value=${timing}"
    fi
    printf 'Lumen update failpoint triggered: %s:%s\n' \
        "${timing}" "${phase}" >&2
    return "${exit_code}"
}
