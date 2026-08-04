"""Schema and resume validation for the durable updater journal."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from release.image_proof_store import (
    validate_target_artifacts as _validate_target_artifacts,
)

SCHEMA = 2
PHASE_ORDER = (
    "lock",
    "self_update_scripts",
    "check",
    "preflight",
    "backup_preflight",
    "fetch_release",
    "set_image_tag",
    "pull_images",
    "check_storage",
    "start_infra",
    "migrate_db",
    "switch",
    "restart_services",
    "health_check",
    "cleanup",
)
PHASE_INDEX = {phase: index for index, phase in enumerate(PHASE_ORDER)}
SNAPSHOT_REQUIRED_AFTER_PHASE = PHASE_INDEX["check"]
SUBPHASE_PARENT = {
    phase: "restart_services"
    for phase in (
        "start_target_worker",
        "start_green",
        "shift_traffic_50",
        "shift_traffic_100",
        "drain_blue",
        "stop_blue",
        "start_blue",
        "shift_traffic_blue",
        "stop_green",
    )
}
NOOP_PREFIX = PHASE_ORDER[: PHASE_INDEX["check"] + 1]
NOOP_COMPLETION = (*NOOP_PREFIX, "cleanup")
TERMINAL_STATUSES = frozenset({"complete", "manual_required", "rolled_back"})
VALID_STATUSES = TERMINAL_STATUSES | {"failed", "running"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ROLLING_TAG_RE = re.compile(r"^v[0-9]+(?:\.[0-9]+)?$")
CONTEXT_KEYS = (
    "ROOT",
    "SHARED_ENV",
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
    "RELEASE_SOURCE_PROOF_FILE",
    "RELEASE_SOURCE_PROOF_SHA256",
    "RELEASE_SOURCE_TREE_SHA256",
    "RELEASE_EXPECTED_COMMIT",
    "RELEASE_SOURCE_API_IMAGE",
    "RELEASE_SOURCE_MANIFEST_CACHE",
    "RELEASE_MANIFEST_FILE",
    "RELEASE_MANIFEST_SHA256",
    "RELEASE_MANIFEST_TAG",
    "RELEASE_IMAGE_TAG_FILE",
    "RELEASE_IMAGE_TAG_SHA256",
    "TARGET_IMAGE_PROOF_FILE",
    "TARGET_IMAGE_PROOF_SHA256",
    "TARGET_IMAGE_OVERRIDE_FILE",
    "TARGET_IMAGE_OVERRIDE_SHA256",
    "TARGET_IMAGE_SET_DIGEST",
    "TARGET_ROLLING_DIGEST",
    "TGBOT_IMAGE_READY",
    "LUMEN_IMAGE_REGISTRY",
    "LUMEN_UPDATE_BUILD",
    "SKIP_TO_CLEANUP",
    "CURRENT_LINK",
    "API_READY_URL",
    "API_HEALTH_URL",
    "WEB_HEALTH_URL",
    "UPDATE_STATE_SNAPSHOT_READY",
    "UPDATE_ENV_SNAPSHOT",
    "UPDATE_HOST_ARTIFACT_SNAPSHOT",
    "UPDATE_RELEASE_SWITCHED",
    "UPDATE_OLD_SERVICES_STOPPED",
    "UPDATE_MIGRATION_STARTED",
    "UPDATE_MIGRATION_VERIFIED",
    "UPDATE_MIGRATION_HEAD",
    "UPDATE_RESTORE_POINT_TIMESTAMP",
    "UPDATE_RESTORE_POINT_PG",
    "UPDATE_RESTORE_POINT_REDIS",
    "UPDATE_RESTORE_POINT_PG_SIZE",
    "UPDATE_RESTORE_POINT_REDIS_SIZE",
    "UPDATE_RESTORE_POINT_PG_SHA256",
    "UPDATE_RESTORE_POINT_REDIS_SHA256",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _context() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in CONTEXT_KEYS
        if key in os.environ and os.environ[key] != ""
    }


def _env_flag(name: str) -> bool:
    return os.environ.get(name) == "1"


def _runtime() -> dict[str, object]:
    def link(prefix: str) -> dict[str, object]:
        target = os.environ.get(f"UPDATE_RUNTIME_{prefix}_TARGET") or None
        return {
            "kind": os.environ.get(f"UPDATE_RUNTIME_{prefix}_KIND", "unknown"),
            "present": _env_flag(f"UPDATE_RUNTIME_{prefix}_PRESENT"),
            "target": target,
        }

    return {
        "current": link("CURRENT"),
        "previous": link("PREVIOUS"),
        "shared_env_sha256": os.environ.get("UPDATE_RUNTIME_ENV_SHA256") or None,
        "migration_head": os.environ.get("UPDATE_RUNTIME_MIGRATION_HEAD") or None,
        "image_set_digest": (os.environ.get("UPDATE_RUNTIME_IMAGE_SET_DIGEST") or None),
        "rolling_digest": os.environ.get("UPDATE_RUNTIME_ROLLING_DIGEST") or None,
    }


def _link_tuple(value: dict[str, object]) -> tuple[str, bool, str | None]:
    present = bool(value.get("present"))
    target = value.get("target") if present else None
    return (
        "symlink" if present else "missing",
        present,
        str(target) if target else None,
    )


def _runtime_link_tuple(value: dict[str, object]) -> tuple[str, bool, str | None]:
    present = bool(value.get("present"))
    target = value.get("target") if present else None
    return (
        str(value.get("kind") or "unknown"),
        present,
        str(target) if target else None,
    )


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SystemExit(f"update journal v2 is missing {label}")
    return value


def _phase_completed(payload: dict[str, object], phase: str) -> bool:
    completed = payload.get("completed_phases", [])
    return isinstance(completed, list) and phase in completed


def _is_rolling_tag(tag: object) -> bool:
    return isinstance(tag, str) and (
        tag in {"latest", "main"} or ROLLING_TAG_RE.fullmatch(tag) is not None
    )


def _completed_phases(payload: dict[str, object]) -> list[str]:
    raw = payload.get("completed_phases")
    if not isinstance(raw, list) or not all(isinstance(phase, str) for phase in raw):
        raise SystemExit("update journal v2 completed_phases is invalid")
    completed = list(raw)
    if len(set(completed)) != len(completed):
        raise SystemExit("update journal v2 completed_phases contains duplicates")
    return completed


def _completion_mode(payload: dict[str, object]) -> str | None:
    mode = payload.get("completion_mode")
    if mode not in {None, "noop", "updated"}:
        raise SystemExit("update journal v2 completion_mode is invalid")
    return mode if isinstance(mode, str) else None


def _normal_phase_prefix(completed: list[str]) -> bool:
    return tuple(completed) == PHASE_ORDER[: len(completed)]


def _checkpoint_is_complete(payload: dict[str, object]) -> bool:
    completed = tuple(_completed_phases(payload))
    mode = _completion_mode(payload)
    return (mode == "noop" and completed == NOOP_COMPLETION) or (
        mode == "updated" and completed == PHASE_ORDER
    )


def _require_contract_text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise SystemExit(f"update journal {label} is invalid")
    return value


def _validate_request_contract(
    value: object,
    *,
    required: bool,
) -> dict[str, object] | None:
    if value is None:
        if required:
            raise SystemExit("resume request contract proof is missing")
        return None
    request = _require_mapping(value, "request contract")
    if set(request) != {
        "channel",
        "force_redeploy",
        "idempotency_key_sha256",
        "resolved_tag",
    }:
        raise SystemExit("update journal request contract fields are invalid")
    _require_contract_text(request.get("channel"), "request channel")
    _require_contract_text(request.get("resolved_tag"), "request resolved tag")
    if not isinstance(request.get("force_redeploy"), bool):
        raise SystemExit("update journal request force_redeploy is invalid")
    key_hash = request.get("idempotency_key_sha256")
    if not isinstance(key_hash, str) or not SHA256_RE.fullmatch(key_hash):
        raise SystemExit("update journal request idempotency hash is invalid")
    return request


def _validate_target_contract(
    value: object,
    *,
    required: bool,
) -> dict[str, object] | None:
    if value is None:
        if required:
            raise SystemExit("resume target contract proof is missing")
        return None
    target = _require_mapping(value, "target contract")
    if not target:
        if required:
            raise SystemExit("resume target contract proof is missing")
        return None
    required_fields = {
        "effective_tag",
        "manifest_sha256",
        "release_id",
        "release_path",
        "release_tag",
        "rolling_digest",
        "source_commit",
        "source_commit_proof",
        "source_proof_path",
        "source_proof_sha256",
        "source_tree_sha256",
    }
    allowed_fields = required_fields | {
        "image_override_path",
        "image_override_sha256",
        "image_proof_path",
        "image_proof_sha256",
        "image_set_digest",
        "image_tag_path",
        "image_tag_sha256",
        "manifest_cache_path",
        "manifest_path",
    }
    if set(target) - allowed_fields:
        raise SystemExit("update journal target contract fields are invalid")
    if not required_fields.issubset(target):
        raise SystemExit("update journal target contract fields are incomplete")
    for field in (
        "effective_tag",
        "release_id",
        "release_path",
        "source_commit_proof",
        "source_proof_path",
    ):
        _require_contract_text(target.get(field), f"target {field}")
    release_tag = target.get("release_tag")
    if release_tag is not None:
        _require_contract_text(release_tag, "target release_tag")
    source_commit = target.get("source_commit")
    if not isinstance(source_commit, str) or not COMMIT_RE.fullmatch(source_commit):
        raise SystemExit("update journal target source_commit is invalid")
    for field in ("source_proof_sha256", "source_tree_sha256"):
        digest = target.get(field)
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise SystemExit(f"update journal target {field} is invalid")
    manifest_sha = target.get("manifest_sha256")
    if manifest_sha is not None and (
        not isinstance(manifest_sha, str) or not SHA256_RE.fullmatch(manifest_sha)
    ):
        raise SystemExit("update journal target manifest_sha256 is invalid")
    if release_tag is not None and manifest_sha is None:
        raise SystemExit("update journal release target manifest SHA-256 is missing")
    rolling_digest = target.get("rolling_digest")
    if rolling_digest is not None and (
        not isinstance(rolling_digest, str)
        or not IMAGE_DIGEST_RE.fullmatch(rolling_digest)
    ):
        raise SystemExit("update journal target rolling_digest is invalid")
    if rolling_digest is not None and not _is_rolling_tag(target.get("effective_tag")):
        raise SystemExit("update journal fixed target cannot bind a rolling digest")
    image_binding_fields = {
        "image_override_path",
        "image_override_sha256",
        "image_proof_path",
        "image_proof_sha256",
        "image_set_digest",
    }
    present_image_fields = image_binding_fields.intersection(target)
    if present_image_fields and present_image_fields != image_binding_fields:
        raise SystemExit("update journal target image binding proof is incomplete")
    if present_image_fields:
        for field in ("image_override_path", "image_proof_path"):
            _require_contract_text(target.get(field), f"target {field}")
        for field in ("image_override_sha256", "image_proof_sha256"):
            digest = target.get(field)
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise SystemExit(f"update journal target {field} is invalid")
        image_set_digest = target.get("image_set_digest")
        if (
            not isinstance(image_set_digest, str)
            or not IMAGE_DIGEST_RE.fullmatch(image_set_digest)
            or image_set_digest != f"sha256:{target['image_proof_sha256']}"
        ):
            raise SystemExit("update journal target image_set_digest is invalid")
        if rolling_digest is not None and rolling_digest != image_set_digest:
            raise SystemExit(
                "update journal rolling digest does not match image binding proof"
            )
    for field in ("manifest_cache_path", "manifest_path", "image_tag_path"):
        if field in target:
            _require_contract_text(target.get(field), f"target {field}")
    image_tag_sha = target.get("image_tag_sha256")
    if ("image_tag_path" in target) != (image_tag_sha is not None):
        raise SystemExit("update journal target image tag proof is incomplete")
    if image_tag_sha is not None and (
        not isinstance(image_tag_sha, str) or not SHA256_RE.fullmatch(image_tag_sha)
    ):
        raise SystemExit("update journal target image_tag_sha256 is invalid")
    if manifest_sha is None and any(
        field in target for field in ("manifest_cache_path", "manifest_path")
    ):
        raise SystemExit("update journal target manifest path lacks a digest")
    return target


def _set_once_request(
    payload: dict[str, object],
    request: dict[str, object],
) -> bool:
    existing = payload.get("request")
    if existing is None:
        payload["request"] = request
        return True
    current = _validate_request_contract(existing, required=True)
    assert current is not None
    if current == request:
        return False
    conflicts = sorted(
        field for field in request if current.get(field) != request.get(field)
    )
    raise SystemExit(
        "immutable update request contract conflict: " + ", ".join(conflicts)
    )


def _monotonic_bind_target(
    payload: dict[str, object],
    updates: dict[str, object],
) -> bool:
    existing_raw = payload.get("target")
    if existing_raw is None:
        existing: dict[str, object] = {}
    else:
        existing = _require_mapping(existing_raw, "target contract")
    merged = dict(existing)
    changed = False
    for field, value in updates.items():
        if field in merged:
            current = merged[field]
            if field in {"manifest_sha256", "rolling_digest"}:
                if value is None or current == value:
                    continue
                if current is None:
                    merged[field] = value
                    changed = True
                    continue
            if current != value:
                raise SystemExit(
                    "immutable update target contract conflict: "
                    f"{field} expected {current!r}, got {value!r}"
                )
            continue
        merged[field] = value
        changed = True
    _validate_target_contract(merged, required=True)
    if changed:
        payload["target"] = merged
    return changed


def _validate_resume_request(request: dict[str, object]) -> None:
    channel = os.environ.get("LUMEN_UPDATE_CHANNEL")
    if channel and channel != request["channel"]:
        raise SystemExit(
            "resume request channel mismatch: "
            f"expected {request['channel']!r}, got {channel!r}"
        )
    resolved_tag = os.environ.get("LUMEN_UPDATE_RESOLVED_TAG")
    if resolved_tag and resolved_tag != request["resolved_tag"]:
        raise SystemExit(
            "resume request resolved tag mismatch: "
            f"expected {request['resolved_tag']!r}, got {resolved_tag!r}"
        )
    force_raw = os.environ.get("LUMEN_UPDATE_FORCE_REDEPLOY", "0")
    if force_raw not in {"0", "1"}:
        raise SystemExit("resume request force_redeploy input is invalid")
    if (force_raw == "1") != request["force_redeploy"]:
        raise SystemExit("resume request force_redeploy mismatch")
    key_hash = _sha256_bytes(
        os.environ.get("LUMEN_UPDATE_IDEMPOTENCY_KEY", "").encode("utf-8")
    )
    if key_hash != request["idempotency_key_sha256"]:
        raise SystemExit("resume request idempotency key mismatch")


def _validate_completed_sequence(
    payload: dict[str, object],
    completed: list[str],
) -> str | None:
    mode = _completion_mode(payload)
    if mode == "noop":
        if tuple(completed) not in {NOOP_PREFIX, NOOP_COMPLETION}:
            raise SystemExit("update journal v2 noop completion sequence is invalid")
        return mode
    if not _normal_phase_prefix(completed):
        raise SystemExit("update journal v2 completed_phases is not an ordered prefix")
    if mode == "updated" and tuple(completed) not in {
        PHASE_ORDER[:-1],
        PHASE_ORDER,
    }:
        raise SystemExit("update journal v2 updated completion sequence is invalid")
    if mode is None and completed == list(PHASE_ORDER):
        raise SystemExit("update journal v2 cleanup completion mode is missing")
    return mode


def _validate_active_phase(
    payload: dict[str, object],
    completed: list[str],
    mode: str | None,
) -> None:
    current_phase = payload.get("current_phase")
    if current_phase is not None:
        if not isinstance(current_phase, str) or current_phase not in PHASE_INDEX:
            raise SystemExit("update journal v2 current_phase is invalid")
        noop_cleanup = (
            mode == "noop"
            and current_phase == "cleanup"
            and tuple(completed) == NOOP_PREFIX
        )
        ordered_next = _normal_phase_prefix(completed) and (
            PHASE_INDEX[current_phase] == len(completed)
        )
        if not (noop_cleanup or ordered_next):
            raise SystemExit(
                "update journal v2 current_phase does not follow completed_phases"
            )
    current_subphase = payload.get("current_subphase")
    if current_subphase is None:
        return
    if not isinstance(current_subphase, str):
        raise SystemExit("update journal v2 current_subphase is invalid")
    if SUBPHASE_PARENT.get(current_subphase) != current_phase:
        raise SystemExit("update journal v2 current_subphase parent is invalid")


def _validate_runtime_shape(state: dict[str, object]) -> None:
    runtime = _require_mapping(state.get("runtime"), "state runtime")
    for name in ("current", "previous"):
        link = _require_mapping(runtime.get(name), f"state runtime {name}")
        if not isinstance(link.get("kind"), str):
            raise SystemExit(f"update journal v2 state runtime {name} kind is invalid")
        if not isinstance(link.get("present"), bool):
            raise SystemExit(
                f"update journal v2 state runtime {name} present is invalid"
            )
        target = link.get("target")
        if target is not None and not isinstance(target, str):
            raise SystemExit(
                f"update journal v2 state runtime {name} target is invalid"
            )
    for field in ("image_set_digest", "rolling_digest"):
        digest = runtime.get(field)
        if digest is not None and (
            not isinstance(digest, str) or not IMAGE_DIGEST_RE.fullmatch(digest)
        ):
            raise SystemExit(f"update journal v2 runtime {field} is invalid")


def _validate_snapshot_boundary(
    completed: list[str],
    current_phase: object,
    snapshot: dict[str, object],
    committed: bool,
) -> None:
    ready = snapshot.get("ready")
    if not isinstance(ready, bool):
        raise SystemExit("update journal v2 snapshot.ready is invalid")
    if ready:
        return
    completed_requires_snapshot = any(
        phase in PHASE_INDEX and PHASE_INDEX[phase] >= SNAPSHOT_REQUIRED_AFTER_PHASE
        for phase in completed
    )
    if completed_requires_snapshot:
        raise SystemExit(
            "update journal v2 completed phase requires a durable snapshot"
        )
    if (
        isinstance(current_phase, str)
        and current_phase in PHASE_INDEX
        and PHASE_INDEX[current_phase] > SNAPSHOT_REQUIRED_AFTER_PHASE
    ):
        raise SystemExit("update journal v2 current phase requires a durable snapshot")
    if committed:
        raise SystemExit(
            "update journal v2 committed state requires a durable snapshot"
        )


def _validate_checkpoint_shape(
    payload: dict[str, object],
    snapshot: dict[str, object],
    state: dict[str, object],
) -> None:
    completed = _completed_phases(payload)
    mode = _validate_completed_sequence(payload, completed)
    _validate_active_phase(payload, completed, mode)
    status = payload.get("status")
    if status not in VALID_STATUSES:
        raise SystemExit("update journal v2 status is invalid")
    committed = state.get("committed")
    if not isinstance(committed, bool):
        raise SystemExit("update journal v2 state.committed is invalid")
    if _phase_completed(payload, "health_check") and not committed:
        raise SystemExit("health_check completion requires committed state")
    expected_env_sha = state.get("expected_env_sha256")
    if expected_env_sha is not None and not isinstance(expected_env_sha, str):
        raise SystemExit("update journal v2 state.expected_env_sha256 is invalid")
    _validate_runtime_shape(state)
    _validate_snapshot_boundary(
        completed,
        payload.get("current_phase"),
        snapshot,
        committed,
    )
    if status in TERMINAL_STATUSES and (
        payload.get("current_phase") is not None
        or payload.get("current_subphase") is not None
    ):
        raise SystemExit("update journal terminal status cannot have an active phase")
    if status == "complete":
        if not _checkpoint_is_complete(payload):
            raise SystemExit("update journal cannot complete before cleanup")
        if mode == "updated" and not committed:
            raise SystemExit("updated journal cannot complete before commit")


def _validate_checkpoint_contracts(
    payload: dict[str, object],
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    request = _validate_request_contract(
        payload.get("request"),
        required=_phase_completed(payload, "check"),
    )
    target = _validate_target_contract(
        payload.get("target"),
        required=_phase_completed(payload, "fetch_release"),
    )
    if (
        target is not None
        and _phase_completed(payload, "pull_images")
        and _is_rolling_tag(target.get("effective_tag"))
        and target.get("rolling_digest") is None
    ):
        raise SystemExit("rolling target pull completion requires an image digest")
    context = _require_mapping(payload.get("context", {}), "context")
    binding_required = (
        target is not None
        and _phase_completed(payload, "pull_images")
        and (
            _is_rolling_tag(target.get("effective_tag"))
            or context.get("LUMEN_UPDATE_BUILD") == "1"
        )
    )
    if binding_required and target.get("image_set_digest") is None:
        raise SystemExit(
            "immutable target pull completion requires an image binding proof"
        )
    return request, target


def _validate_checkpoint(
    payload: dict[str, object],
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object] | None,
    dict[str, object] | None,
]:
    snapshot = _require_mapping(payload.get("snapshot"), "snapshot")
    state = _require_mapping(payload.get("state"), "state")
    _validate_checkpoint_shape(payload, snapshot, state)
    request, target = _validate_checkpoint_contracts(payload)
    return snapshot, state, request, target


def _validate_resume(payload: dict[str, object]) -> None:
    snapshot, state, request, target = _validate_checkpoint(payload)
    invariants = _require_mapping(payload.get("invariants", {}), "invariants")
    if request is not None:
        _validate_resume_request(request)
    if target is not None:
        _validate_target_artifacts(payload, target)
    current_context = _context()

    for key, snapshot_key in (("ROOT", "root"), ("SHARED_ENV", "shared_env")):
        expected = snapshot.get(snapshot_key)
        actual = current_context.get(key)
        if expected and actual != expected:
            raise SystemExit(
                f"resume {key.lower()} invariant mismatch: "
                f"expected {expected!r}, got {actual!r}"
            )

    if not snapshot["ready"]:
        return

    snapshot_links: dict[str, dict[str, object]] = {}
    for name in ("current", "previous"):
        link = _require_mapping(snapshot.get(name), f"snapshot {name}")
        if link.get("known") is not True:
            raise SystemExit(f"resume snapshot {name} link is unknown")
        if link.get("present") and not link.get("target"):
            raise SystemExit(f"resume snapshot {name} target is missing")
        snapshot_links[name] = link

    env_snapshot_raw = snapshot.get("env_snapshot")
    env_snapshot = Path(str(env_snapshot_raw)) if env_snapshot_raw else None
    if env_snapshot is None or not env_snapshot.is_file():
        raise SystemExit("resume env snapshot is missing")
    expected_env_sha = snapshot.get("shared_env_sha256")
    if not expected_env_sha:
        raise SystemExit("resume env snapshot hash is missing")
    digest = hashlib.sha256(env_snapshot.read_bytes()).hexdigest()
    if digest != expected_env_sha:
        raise SystemExit("resume env snapshot hash mismatch")

    host_snapshot_raw = snapshot.get("host_artifact_snapshot")
    if host_snapshot_raw and not Path(str(host_snapshot_raw)).is_dir():
        raise SystemExit("resume host artifact snapshot is missing")

    live = _runtime()
    persisted_runtime = _require_mapping(state.get("runtime", {}), "state runtime")
    live_current = _runtime_link_tuple(
        _require_mapping(live.get("current"), "live current")
    )
    live_previous = _runtime_link_tuple(
        _require_mapping(live.get("previous"), "live previous")
    )
    if live_current[0] not in {"missing", "symlink"}:
        raise SystemExit("resume current link invariant mismatch: not a symlink")
    if live_previous[0] not in {"missing", "symlink"}:
        raise SystemExit("resume previous link invariant mismatch: not a symlink")

    persisted_current = _require_mapping(
        persisted_runtime.get("current", {}), "persisted current"
    )
    allowed_current = {
        _link_tuple(snapshot_links["current"]),
        _runtime_link_tuple(persisted_current),
    }
    new_id = current_context.get("NEW_ID") or invariants.get("NEW_ID")
    if new_id:
        allowed_current.add(("symlink", True, f"releases/{new_id}"))
    allowed_current.discard(("unknown", False, None))
    if live_current not in allowed_current:
        raise SystemExit(
            f"resume current link invariant mismatch: got {live_current!r}"
        )

    persisted_previous = _require_mapping(
        persisted_runtime.get("previous", {}), "persisted previous"
    )
    allowed_previous = {
        _link_tuple(snapshot_links["previous"]),
        _runtime_link_tuple(persisted_previous),
    }
    if snapshot_links["current"].get("present"):
        allowed_previous.add(_link_tuple(snapshot_links["current"]))
    allowed_previous.discard(("unknown", False, None))
    if live_previous not in allowed_previous:
        raise SystemExit(
            f"resume previous link invariant mismatch: got {live_previous!r}"
        )

    allowed_env_hashes = {
        str(expected_env_sha),
        persisted_runtime.get("shared_env_sha256"),
        state.get("expected_env_sha256"),
    }
    allowed_env_hashes.discard(None)
    if live.get("shared_env_sha256") not in allowed_env_hashes:
        raise SystemExit("resume shared env hash invariant mismatch")
    if target is not None and target.get("image_set_digest") is not None:
        if live.get("image_set_digest") != target["image_set_digest"]:
            raise SystemExit("resume immutable image set digest mismatch")
    if target is not None and target.get("rolling_digest") is not None:
        if live.get("rolling_digest") != target["rolling_digest"]:
            raise SystemExit("resume rolling image digest mismatch")

    new_release_raw = current_context.get("NEW_RELEASE") or invariants.get(
        "NEW_RELEASE"
    )
    if new_release_raw and not Path(str(new_release_raw)).is_dir():
        raise SystemExit("resume target release invariant mismatch")

    migration_verified = current_context.get("UPDATE_MIGRATION_VERIFIED") == "1"
    expected_head = current_context.get("UPDATE_MIGRATION_HEAD") or invariants.get(
        "UPDATE_MIGRATION_HEAD"
    )
    if migration_verified and expected_head:
        if live.get("migration_head") != expected_head:
            raise SystemExit(
                "resume migration head invariant mismatch: "
                f"expected {expected_head!r}, got {live.get('migration_head')!r}"
            )
