#!/usr/bin/env bash
# Atomic update journal, resume metadata, and deterministic failpoints.

LUMEN_UPDATE_JOURNAL_SCHEMA=1
LUMEN_UPDATE_JOURNAL_READY=0
LUMEN_UPDATE_JOURNAL_RESUMED=0

lumen_update_journal_path() {
    if [ -n "${LUMEN_UPDATE_JOURNAL:-}" ]; then
        printf '%s\n' "${LUMEN_UPDATE_JOURNAL}"
    else
        printf '%s\n' "${SHARED_DIR:?}/.update-journal.json"
    fi
}

lumen_update_journal_exec() {
    local action="$1"
    shift
    python3 - "${action}" "$(lumen_update_journal_path)" "$@" <<'PY'
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from datetime import datetime, timezone

action = sys.argv[1]
path = Path(sys.argv[2])
args = sys.argv[3:]
now = datetime.now(timezone.utc).isoformat()
context_keys = (
    "CURRENT_ID",
    "CURRENT_RELEASE",
    "CURRENT_TAG",
    "NEW_ID",
    "NEW_RELEASE",
    "PREVIOUS_TAG",
    "TARGET_TAG",
    "TARGET_RELEASE_TAG",
    "TARGET_VERSION",
    "REPO_DIR",
    "RELEASE_SOURCE_REF",
    "RELEASE_SOURCE_IMAGE_EXTRACT",
    "RELEASE_SOURCE_COMMIT",
    "RELEASE_SOURCE_COMMIT_PROOF",
    "RELEASE_EXPECTED_COMMIT",
    "RELEASE_SOURCE_API_IMAGE",
    "RELEASE_SOURCE_MANIFEST_CACHE",
    "RELEASE_MANIFEST_FILE",
    "RELEASE_MANIFEST_TAG",
    "LUMEN_IMAGE_REGISTRY",
    "LUMEN_PROXY_URL",
    "LUMEN_UPDATE_BUILD",
    "SKIP_TO_CLEANUP",
    "CURRENT_LINK",
    "API_HEALTH_URL",
    "WEB_HEALTH_URL",
    "UPDATE_STATE_SNAPSHOT_READY",
    "UPDATE_ENV_SNAPSHOT",
    "UPDATE_HOST_ARTIFACT_SNAPSHOT",
    "UPDATE_RELEASE_SWITCHED",
    "UPDATE_OLD_SERVICES_STOPPED",
    "UPDATE_MIGRATION_STARTED",
    "UPDATE_MIGRATION_VERIFIED",
    "UPDATE_RESTORE_POINT_TIMESTAMP",
    "UPDATE_RESTORE_POINT_PG",
    "UPDATE_RESTORE_POINT_REDIS",
)


def read() -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid update journal {path}: {exc}")
    if raw.get("schema") != 1:
        raise SystemExit(f"unsupported update journal schema: {raw.get('schema')}")
    return raw


def write(payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def context() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in context_keys
        if key in os.environ and os.environ[key] != ""
    }


if action == "init":
    operation_id, resume_raw = args
    resume = resume_raw == "1"
    payload = read()
    resumable = payload.get("status") in {"failed", "running"}
    if resume and payload and resumable:
        payload["status"] = "running"
        payload["resumed_at"] = now
        payload["resume_count"] = int(payload.get("resume_count") or 0) + 1
        payload["last_error"] = None
        write(payload)
        print(f"{payload['operation_id']}\t1")
    else:
        payload = {
            "attempts": {},
            "completed_phases": [],
            "context": {},
            "created_at": now,
            "current_phase": None,
            "last_error": None,
            "operation_id": operation_id,
            "resume_count": 0,
            "schema": 1,
            "status": "running",
            "updated_at": now,
        }
        write(payload)
        print(f"{operation_id}\t0")
elif action == "phase-start":
    phase = args[0]
    payload = read()
    attempts = payload.setdefault("attempts", {})
    attempts[phase] = int(attempts.get(phase) or 0) + 1
    payload["current_phase"] = phase
    payload["status"] = "running"
    payload["updated_at"] = now
    payload["context"] = context()
    write(payload)
elif action == "phase-done":
    phase = args[0]
    payload = read()
    completed = payload.setdefault("completed_phases", [])
    if phase not in completed:
        completed.append(phase)
    payload["current_phase"] = None
    payload["updated_at"] = now
    payload["context"] = context()
    write(payload)
elif action == "phase-completed":
    phase = args[0]
    payload = read()
    print("1" if phase in payload.get("completed_phases", []) else "0")
elif action == "failed":
    phase, return_code = args
    payload = read()
    payload["current_phase"] = phase or payload.get("current_phase")
    payload["last_error"] = {
        "phase": payload.get("current_phase"),
        "return_code": int(return_code),
        "timestamp": now,
    }
    payload["status"] = "failed"
    payload["updated_at"] = now
    payload["context"] = context()
    write(payload)
elif action == "status":
    status = args[0]
    payload = read()
    payload["status"] = status
    payload["current_phase"] = None
    payload["updated_at"] = now
    payload["context"] = context()
    write(payload)
elif action == "restore-context":
    payload = read()
    for key, value in sorted((payload.get("context") or {}).items()):
        if key in context_keys and isinstance(value, str):
            print(f"{key}={shlex.quote(value)}")
else:
    raise SystemExit(f"unknown update journal action: {action}")
PY
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
    export CURRENT_ID CURRENT_RELEASE CURRENT_TAG NEW_ID NEW_RELEASE
    export PREVIOUS_TAG TARGET_TAG TARGET_RELEASE_TAG TARGET_VERSION
    export REPO_DIR RELEASE_SOURCE_REF RELEASE_SOURCE_IMAGE_EXTRACT
    export RELEASE_SOURCE_COMMIT RELEASE_SOURCE_COMMIT_PROOF
    export RELEASE_EXPECTED_COMMIT RELEASE_SOURCE_API_IMAGE
    export RELEASE_SOURCE_MANIFEST_CACHE RELEASE_MANIFEST_FILE
    export RELEASE_MANIFEST_TAG LUMEN_IMAGE_REGISTRY LUMEN_PROXY_URL
    export LUMEN_UPDATE_BUILD SKIP_TO_CLEANUP CURRENT_LINK
    export API_HEALTH_URL WEB_HEALTH_URL UPDATE_STATE_SNAPSHOT_READY
    export UPDATE_ENV_SNAPSHOT UPDATE_HOST_ARTIFACT_SNAPSHOT
    export UPDATE_RELEASE_SWITCHED UPDATE_OLD_SERVICES_STOPPED
    export UPDATE_MIGRATION_STARTED UPDATE_MIGRATION_VERIFIED
    export UPDATE_RESTORE_POINT_TIMESTAMP UPDATE_RESTORE_POINT_PG
    export UPDATE_RESTORE_POINT_REDIS
}

lumen_update_journal_phase_start() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 0
    lumen_update_journal_export_context
    lumen_update_journal_exec phase-start "$1"
}

lumen_update_journal_phase_done() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 0
    lumen_update_journal_export_context
    lumen_update_journal_exec phase-done "$1"
}

lumen_update_journal_phase_completed() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 1
    [ "$(lumen_update_journal_exec phase-completed "$1")" = "1" ]
}

lumen_update_journal_failed() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 0
    lumen_update_journal_exec failed "${1:-}" "${2:-1}"
}

lumen_update_journal_status() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 0
    lumen_update_journal_exec status "$1"
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
