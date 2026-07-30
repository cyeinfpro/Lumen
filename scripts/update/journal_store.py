#!/usr/bin/env python3
"""Durable JSON store and resume invariant checks for the Lumen updater."""

from __future__ import annotations

from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

from release.image_proof_store import (
    sha256_file as _sha256_file,
    source_tree_sha256 as _source_tree_sha256,
)

try:
    from .journal_validation import (
        CONTEXT_KEYS,
        NOOP_PREFIX,
        PHASE_INDEX,
        PHASE_ORDER,
        SCHEMA,
        SUBPHASE_PARENT,
        TERMINAL_STATUSES,
        _completed_phases,
        _completion_mode,
        _context,
        _env_flag,
        _monotonic_bind_target,
        _require_contract_text,
        _require_mapping,
        _runtime,
        _set_once_request,
        _sha256_bytes,
        _validate_checkpoint,
        _validate_resume,
        _validate_target_contract,
        _validate_target_artifacts,
    )
except ImportError:
    from journal_validation import (
        CONTEXT_KEYS,
        NOOP_PREFIX,
        PHASE_INDEX,
        PHASE_ORDER,
        SCHEMA,
        SUBPHASE_PARENT,
        TERMINAL_STATUSES,
        _completed_phases,
        _completion_mode,
        _context,
        _env_flag,
        _monotonic_bind_target,
        _require_contract_text,
        _require_mapping,
        _runtime,
        _set_once_request,
        _sha256_bytes,
        _validate_checkpoint,
        _validate_resume,
        _validate_target_contract,
        _validate_target_artifacts,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_snapshot() -> dict[str, object]:
    unknown_link = {"known": False, "present": False, "target": None}
    return {
        "ready": False,
        "root": None,
        "shared_env": None,
        "shared_env_sha256": None,
        "env_snapshot": None,
        "host_artifact_snapshot": None,
        "current": dict(unknown_link),
        "previous": dict(unknown_link),
    }


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read_raw(self) -> dict[str, object]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"invalid update journal {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SystemExit(
                f"invalid update journal {self.path}: root must be an object"
            )
        return raw

    def read(self) -> dict[str, object]:
        raw = self.read_raw()
        if raw.get("schema") != SCHEMA:
            raise SystemExit(f"unsupported update journal schema: {raw.get('schema')}")
        return raw

    def write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_raw = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(temporary_raw)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            self._fsync_parent()
        finally:
            temporary.unlink(missing_ok=True)

    def _fsync_parent(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(self.path.parent, flags)
        try:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
                    raise
        finally:
            os.close(directory_fd)


def _refresh_state(payload: dict[str, object]) -> None:
    payload["context"] = _context()
    state = payload.setdefault("state", {})
    assert isinstance(state, dict)
    state["runtime"] = _runtime()
    state.setdefault("committed", False)
    state.setdefault("expected_env_sha256", None)
    invariants = payload.setdefault("invariants", {})
    assert isinstance(invariants, dict)
    for key in (
        "ROOT",
        "SHARED_ENV",
        "TARGET_TAG",
        "TARGET_RELEASE_TAG",
        "NEW_ID",
        "NEW_RELEASE",
        "UPDATE_MIGRATION_HEAD",
    ):
        value = os.environ.get(key)
        if value:
            invariants[key] = value


def _new_payload(operation_id: str, now: str) -> dict[str, object]:
    return {
        "attempts": {},
        "completed_phases": [],
        "completion_mode": None,
        "context": {},
        "created_at": now,
        "current_phase": None,
        "current_subphase": None,
        "invariants": {},
        "last_error": None,
        "operation_id": operation_id,
        "request": None,
        "resume_count": 0,
        "schema": SCHEMA,
        "snapshot": _default_snapshot(),
        "state": {
            "committed": False,
            "expected_env_sha256": None,
            "runtime": _runtime(),
        },
        "subphase_attempts": {},
        "status": "running",
        "target": None,
        "updated_at": now,
    }


def _action_init(store: Journal, args: list[str], now: str) -> None:
    operation_id, resume_raw = args
    resume = resume_raw == "1"
    payload = store.read_raw()
    resumable = payload.get("status") in {"failed", "running"}
    if resume and payload and resumable:
        if payload.get("schema") == 1:
            raise SystemExit(
                "update journal schema 1 cannot be resumed automatically; "
                "inspect current/previous and start a new update"
            )
        if payload.get("schema") != SCHEMA:
            raise SystemExit(
                f"unsupported update journal schema: {payload.get('schema')}"
            )
        if not payload.get("operation_id"):
            raise SystemExit("update journal operation_id is missing")
        _validate_checkpoint(payload)
        payload["status"] = "running"
        payload["resumed_at"] = now
        payload["resume_count"] = int(payload.get("resume_count") or 0) + 1
        payload["last_error"] = None
        store.write(payload)
        print(f"{payload['operation_id']}\t1")
        return
    if payload and resumable:
        raise SystemExit(
            "an active update journal already exists; "
            "set LUMEN_UPDATE_RESUME=1 or inspect and archive it"
        )
    store.write(_new_payload(operation_id, now))
    print(f"{operation_id}\t0")


def _require_running(payload: dict[str, object], action: str) -> None:
    if payload.get("status") != "running":
        raise SystemExit(f"cannot {action} when journal status is not running")


def _expected_recoverable_phase(payload: dict[str, object]) -> str | None:
    completed = _completed_phases(payload)
    if _completion_mode(payload) == "noop":
        return "cleanup" if tuple(completed) == NOOP_PREFIX else None
    if len(completed) >= len(PHASE_ORDER):
        return None
    return PHASE_ORDER[len(completed)]


def _set_cleanup_mode(payload: dict[str, object], completed: list[str]) -> None:
    if _completion_mode(payload) is not None:
        return
    context = _require_mapping(payload.get("context", {}), "context")
    persisted_noop = context.get("SKIP_TO_CLEANUP") == "1"
    if (
        tuple(completed) == NOOP_PREFIX
        and persisted_noop
        and _env_flag("SKIP_TO_CLEANUP")
    ):
        payload["completion_mode"] = "noop"
        return
    if tuple(completed) == PHASE_ORDER[:-1]:
        payload["completion_mode"] = "updated"
        return
    raise SystemExit("cleanup cannot start from the current phase boundary")


def _start_recoverable_phase(payload: dict[str, object], phase: str) -> bool:
    if phase not in PHASE_INDEX:
        raise SystemExit(f"unknown recoverable update phase: {phase}")
    completed = _completed_phases(payload)
    if phase == "cleanup" and phase in completed:
        return False
    if phase == "cleanup":
        _set_cleanup_mode(payload, completed)
    current = payload.get("current_phase")
    if current is not None:
        if current != phase:
            raise SystemExit(
                f"cannot start phase {phase!r} while {current!r} is active"
            )
        payload["current_subphase"] = None
        return True
    expected = _expected_recoverable_phase(payload)
    if phase != expected:
        raise SystemExit(
            f"illegal update phase transition: expected {expected!r}, got {phase!r}"
        )
    payload["current_phase"] = phase
    payload["current_subphase"] = None
    return True


def _start_subphase(payload: dict[str, object], phase: str) -> None:
    parent = SUBPHASE_PARENT.get(phase)
    if parent is None:
        raise SystemExit(f"unknown update subphase: {phase}")
    if payload.get("current_phase") != parent:
        raise SystemExit(f"update subphase {phase!r} requires active phase {parent!r}")
    current = payload.get("current_subphase")
    if current not in {None, phase}:
        raise SystemExit(f"cannot start subphase {phase!r} while {current!r} is active")
    payload["current_subphase"] = phase


def _action_phase_start(store: Journal, args: list[str], now: str) -> None:
    phase, recoverable_raw = args
    recoverable = recoverable_raw == "1"
    payload = store.read()
    _validate_checkpoint(payload)
    _require_running(payload, "start a phase")
    changed = (
        _start_recoverable_phase(payload, phase)
        if recoverable
        else (_start_subphase(payload, phase) is None)
    )
    if not changed:
        return
    attempts_key = "attempts" if recoverable else "subphase_attempts"
    attempts = _require_mapping(payload.setdefault(attempts_key, {}), attempts_key)
    attempts[phase] = int(attempts.get(phase) or 0) + 1
    payload["updated_at"] = now
    _refresh_state(payload)
    state = _require_mapping(payload.get("state"), "state")
    state["expected_env_sha256"] = None
    _validate_checkpoint(payload)
    store.write(payload)


def _finish_recoverable_phase(payload: dict[str, object], phase: str) -> bool:
    completed = _completed_phases(payload)
    if phase == "cleanup" and phase in completed:
        return False
    if payload.get("current_phase") != phase:
        raise SystemExit(f"cannot complete inactive update phase: {phase}")
    if payload.get("current_subphase") is not None:
        raise SystemExit(f"cannot complete phase {phase!r} with an active subphase")
    completed.append(phase)
    payload["completed_phases"] = completed
    payload["current_phase"] = None
    return True


def _finish_subphase(payload: dict[str, object], phase: str) -> bool:
    if payload.get("current_subphase") != phase:
        raise SystemExit(f"cannot complete inactive update subphase: {phase}")
    payload["current_subphase"] = None
    return True


def _action_phase_done(store: Journal, args: list[str], now: str) -> None:
    phase, recoverable_raw = args
    recoverable = recoverable_raw == "1"
    payload = store.read()
    _validate_checkpoint(payload)
    _require_running(payload, "complete a phase")
    changed = (
        _finish_recoverable_phase(payload, phase)
        if recoverable
        else _finish_subphase(payload, phase)
    )
    if not changed:
        return
    payload["updated_at"] = now
    _refresh_state(payload)
    state = _require_mapping(payload.get("state"), "state")
    state["expected_env_sha256"] = None
    _validate_checkpoint(payload)
    store.write(payload)


def _record_failure_position(
    payload: dict[str, object],
    phase: str,
    recoverable: bool,
) -> None:
    if not phase or phase == "resume_validation":
        return
    if recoverable and phase in PHASE_INDEX:
        completed = _completed_phases(payload)
        if phase in completed:
            return
        current = payload.get("current_phase")
        if current not in {None, phase}:
            raise SystemExit(f"cannot fail phase {phase!r} while {current!r} is active")
        if current is None and _expected_recoverable_phase(payload) != phase:
            raise SystemExit(f"cannot fail out-of-order update phase: {phase}")
        payload["current_phase"] = phase
        payload["current_subphase"] = None
        return
    parent = SUBPHASE_PARENT.get(phase)
    if parent is not None and payload.get("current_phase") == parent:
        payload["current_subphase"] = phase


def _action_failed(store: Journal, args: list[str], now: str) -> None:
    phase, return_code, recoverable_raw = args
    recoverable = recoverable_raw == "1"
    payload = store.read()
    _validate_checkpoint(payload)
    _record_failure_position(payload, phase, recoverable)
    payload["last_error"] = {
        "phase": phase
        or payload.get("current_subphase")
        or payload.get("current_phase"),
        "return_code": int(return_code),
        "timestamp": now,
    }
    payload["status"] = "failed"
    payload["updated_at"] = now
    if phase != "resume_validation":
        _refresh_state(payload)
    _validate_checkpoint(payload)
    store.write(payload)


def _action_status(store: Journal, args: list[str], now: str) -> None:
    requested = args[0]
    if requested not in TERMINAL_STATUSES:
        raise SystemExit(f"unsupported terminal update journal status: {requested}")
    payload = store.read()
    _validate_checkpoint(payload)
    current = payload.get("status")
    if current == requested:
        return
    if current in TERMINAL_STATUSES:
        raise SystemExit(f"cannot change terminal journal status {current!r}")
    payload["status"] = requested
    payload["current_phase"] = None
    payload["current_subphase"] = None
    payload["updated_at"] = now
    _refresh_state(payload)
    _validate_checkpoint(payload)
    store.write(payload)


def _action_snapshot(store: Journal, now: str) -> None:
    payload = store.read()
    known = _env_flag("UPDATE_SNAPSHOT_LINKS_KNOWN")
    ready = _env_flag("UPDATE_STATE_SNAPSHOT_READY")

    def snapshot_link(prefix: str) -> dict[str, object]:
        present = _env_flag(f"UPDATE_ORIGINAL_{prefix}_PRESENT")
        target = os.environ.get(f"UPDATE_ORIGINAL_{prefix}_TARGET") or None
        return {
            "known": known,
            "present": present,
            "target": target if present else None,
        }

    snapshot = {
        "ready": ready,
        "root": os.environ.get("ROOT") or None,
        "shared_env": os.environ.get("SHARED_ENV") or None,
        "shared_env_sha256": os.environ.get("UPDATE_SNAPSHOT_ENV_SHA256") or None,
        "env_snapshot": os.environ.get("UPDATE_ENV_SNAPSHOT") or None,
        "host_artifact_snapshot": (
            os.environ.get("UPDATE_HOST_ARTIFACT_SNAPSHOT") or None
        ),
        "current": snapshot_link("CURRENT"),
        "previous": snapshot_link("PREVIOUS"),
    }
    if ready:
        if not known:
            raise SystemExit("cannot persist updater snapshot with unknown links")
        if not snapshot["env_snapshot"] or not snapshot["shared_env_sha256"]:
            raise SystemExit("cannot persist updater snapshot without env proof")
    payload["snapshot"] = snapshot
    state = payload.setdefault("state", {})
    assert isinstance(state, dict)
    state["committed"] = False
    payload["updated_at"] = now
    _refresh_state(payload)
    store.write(payload)


def _action_mark_committed(store: Journal, now: str) -> None:
    payload = store.read()
    _refresh_state(payload)
    state = payload.setdefault("state", {})
    assert isinstance(state, dict)
    state["committed"] = True
    payload["updated_at"] = now
    store.write(payload)


def _action_expect_env(store: Journal, args: list[str], now: str) -> None:
    candidate = Path(args[0])
    if not candidate.is_file():
        raise SystemExit("updater env candidate is missing")
    digest = _sha256_file(candidate)
    payload = store.read()
    _refresh_state(payload)
    state = _require_mapping(payload.get("state"), "state")
    state["expected_env_sha256"] = digest
    payload["updated_at"] = now
    store.write(payload)


def _action_bind_request(store: Journal, args: list[str], now: str) -> None:
    channel, resolved_tag, force_raw = args
    if force_raw not in {"0", "1"}:
        raise SystemExit("update request force_redeploy must be 0 or 1")
    idempotency_key = sys.stdin.buffer.read()
    request = {
        "channel": _require_contract_text(channel, "request channel"),
        "force_redeploy": force_raw == "1",
        "idempotency_key_sha256": _sha256_bytes(idempotency_key),
        "resolved_tag": _require_contract_text(
            resolved_tag,
            "request resolved tag",
        ),
    }
    payload = store.read()
    if not _set_once_request(payload, request):
        return
    _refresh_state(payload)
    payload["updated_at"] = now
    store.write(payload)


def _action_bind_target(store: Journal, args: list[str], now: str) -> None:
    (
        effective_tag,
        release_tag,
        release_path,
        release_id,
        source_commit,
        source_commit_proof,
        source_proof_path,
        source_proof_sha256,
        source_tree_sha256,
        manifest_sha256,
        manifest_cache_path,
        manifest_path,
        image_tag_path,
        image_tag_sha256,
        rolling_digest,
        image_proof_path,
        image_proof_sha256,
        image_override_path,
        image_override_sha256,
        image_set_digest,
    ) = args
    updates: dict[str, object] = {
        "effective_tag": effective_tag,
        "manifest_sha256": manifest_sha256 or None,
        "release_id": release_id,
        "release_path": release_path,
        "release_tag": release_tag or None,
        "rolling_digest": rolling_digest or None,
        "source_commit": source_commit,
        "source_commit_proof": source_commit_proof,
        "source_proof_path": source_proof_path,
        "source_proof_sha256": source_proof_sha256,
        "source_tree_sha256": source_tree_sha256,
    }
    for field, value in (
        ("manifest_cache_path", manifest_cache_path),
        ("manifest_path", manifest_path),
        ("image_tag_path", image_tag_path),
        ("image_tag_sha256", image_tag_sha256),
        ("image_proof_path", image_proof_path),
        ("image_proof_sha256", image_proof_sha256),
        ("image_override_path", image_override_path),
        ("image_override_sha256", image_override_sha256),
        ("image_set_digest", image_set_digest),
    ):
        if value:
            updates[field] = value
    payload = store.read()
    changed = _monotonic_bind_target(payload, updates)
    target = _validate_target_contract(payload.get("target"), required=True)
    assert target is not None
    _validate_target_artifacts(payload, target)
    if not changed:
        return
    _refresh_state(payload)
    payload["updated_at"] = now
    store.write(payload)


def _action_assert_target_field(store: Journal, args: list[str]) -> None:
    field, expected = args
    if field not in {
        "effective_tag",
        "release_id",
        "release_path",
        "source_commit",
    }:
        raise SystemExit(f"unsupported immutable target field assertion: {field}")
    payload = store.read()
    target = _validate_target_contract(payload.get("target"), required=True)
    assert target is not None
    if target.get(field) != expected:
        raise SystemExit(
            "immutable update target contract conflict: "
            f"{field} expected {target.get(field)!r}, got {expected!r}"
        )


def _action_restore_context(store: Journal) -> None:
    payload = store.read()
    context = _require_mapping(payload.get("context", {}), "context")
    for key, value in sorted(context.items()):
        if key in {"ROOT", "SHARED_ENV"}:
            continue
        if key in CONTEXT_KEYS and isinstance(value, str):
            print(f"{key}={shlex.quote(value)}")

    snapshot = _require_mapping(
        payload.get("snapshot", _default_snapshot()), "snapshot"
    )
    state = _require_mapping(payload.get("state", {}), "state")
    current = _require_mapping(snapshot.get("current", {}), "snapshot current")
    previous = _require_mapping(snapshot.get("previous", {}), "snapshot previous")
    target = _validate_target_contract(payload.get("target"), required=False)
    assignments = {
        "UPDATE_STATE_SNAPSHOT_READY": "1" if snapshot.get("ready") else "0",
        "UPDATE_SNAPSHOT_LINKS_KNOWN": (
            "1" if current.get("known") and previous.get("known") else "0"
        ),
        "UPDATE_ORIGINAL_CURRENT_PRESENT": "1" if current.get("present") else "0",
        "UPDATE_ORIGINAL_CURRENT_TARGET": current.get("target") or "",
        "UPDATE_ORIGINAL_PREVIOUS_PRESENT": "1" if previous.get("present") else "0",
        "UPDATE_ORIGINAL_PREVIOUS_TARGET": previous.get("target") or "",
        "UPDATE_SNAPSHOT_ENV_SHA256": snapshot.get("shared_env_sha256") or "",
        "UPDATE_ENV_SNAPSHOT": snapshot.get("env_snapshot") or "",
        "UPDATE_HOST_ARTIFACT_SNAPSHOT": (snapshot.get("host_artifact_snapshot") or ""),
        "UPDATE_STATE_COMMITTED": "1" if state.get("committed") else "0",
    }
    if target is not None:
        assignments.update(
            {
                "TARGET_TAG": target.get("effective_tag") or "",
                "TARGET_RELEASE_TAG": target.get("release_tag") or "",
                "NEW_RELEASE": target.get("release_path") or "",
                "NEW_ID": target.get("release_id") or "",
                "RELEASE_SOURCE_COMMIT": target.get("source_commit") or "",
                "RELEASE_SOURCE_COMMIT_PROOF": (
                    target.get("source_commit_proof") or ""
                ),
                "RELEASE_SOURCE_PROOF_FILE": target.get("source_proof_path") or "",
                "RELEASE_SOURCE_PROOF_SHA256": (
                    target.get("source_proof_sha256") or ""
                ),
                "RELEASE_SOURCE_TREE_SHA256": (target.get("source_tree_sha256") or ""),
                "RELEASE_SOURCE_MANIFEST_CACHE": (
                    target.get("manifest_cache_path") or ""
                ),
                "RELEASE_MANIFEST_FILE": target.get("manifest_path") or "",
                "RELEASE_MANIFEST_SHA256": target.get("manifest_sha256") or "",
                "RELEASE_IMAGE_TAG_FILE": target.get("image_tag_path") or "",
                "RELEASE_IMAGE_TAG_SHA256": (target.get("image_tag_sha256") or ""),
                "TARGET_IMAGE_PROOF_FILE": target.get("image_proof_path") or "",
                "TARGET_IMAGE_PROOF_SHA256": (target.get("image_proof_sha256") or ""),
                "TARGET_IMAGE_OVERRIDE_FILE": (target.get("image_override_path") or ""),
                "TARGET_IMAGE_OVERRIDE_SHA256": (
                    target.get("image_override_sha256") or ""
                ),
                "TARGET_IMAGE_SET_DIGEST": target.get("image_set_digest") or "",
                "TARGET_ROLLING_DIGEST": target.get("rolling_digest") or "",
            }
        )
    for key, value in assignments.items():
        print(f"{key}={shlex.quote(str(value))}")


def main() -> None:
    action = sys.argv[1]
    store = Journal(Path(sys.argv[2]))
    args = sys.argv[3:]
    now = _now()

    if action == "init":
        _action_init(store, args, now)
    elif action == "phase-start":
        _action_phase_start(store, args, now)
    elif action == "phase-done":
        _action_phase_done(store, args, now)
    elif action == "phase-completed":
        payload = store.read()
        completed = payload.get("completed_phases", [])
        print("1" if args[0] in completed else "0")
    elif action == "failed":
        _action_failed(store, args, now)
    elif action == "status":
        _action_status(store, args, now)
    elif action == "snapshot":
        _action_snapshot(store, now)
    elif action == "mark-committed":
        _action_mark_committed(store, now)
    elif action == "expect-env":
        _action_expect_env(store, args, now)
    elif action == "bind-request":
        _action_bind_request(store, args, now)
    elif action == "bind-target":
        _action_bind_target(store, args, now)
    elif action == "assert-target-field":
        _action_assert_target_field(store, args)
    elif action == "source-tree-sha256":
        print(_source_tree_sha256(Path(args[0])))
    elif action == "validate-resume":
        _validate_resume(store.read())
    elif action == "restore-context":
        _action_restore_context(store)
    else:
        raise SystemExit(f"unknown update journal action: {action}")


if __name__ == "__main__":
    main()
