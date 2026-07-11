"""Cross-process model library sync lease and state transitions."""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta
from typing import Any

from lumen_core.schemas import ApparelModelLibrarySyncOut

from .library_runtime import runtime as _runtime


class _ModelLibrarySyncLeaseLost(RuntimeError):
    """The sync lease expired or was replaced before this worker finished."""


def _sync_lease_owner(state: dict[str, Any]) -> tuple[str, datetime] | None:
    lease = state.get("sync_lease")
    if not isinstance(lease, dict):
        return None
    token = str(lease.get("token") or "").strip()
    expires_at = _runtime()._safe_datetime(lease.get("expires_at"))
    if not token or expires_at is None:
        return None
    return token, expires_at


def _claim_library_sync_lease_sync() -> tuple[str | None, dict[str, Any]]:
    """Atomically claim one cross-process sync lease under a short file lock."""

    runtime = _runtime()
    with runtime._model_library_sync_file_lock(runtime._library_sync_lock_path()):
        state = runtime._read_json_file(
            runtime._library_sync_state_path(),
            runtime._default_sync_state(),
        )
        now = runtime._now()
        last_success = runtime._safe_datetime(state.get("last_success_at"))
        if last_success is not None:
            success_age = (now - last_success).total_seconds()
            if success_age < runtime.MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS:
                return None, state

        owner = runtime._sync_lease_owner(state)
        if owner is not None and owner[1] > now:
            return None, state
        if owner is not None:
            state["sync_lease"] = None

        last_attempt = runtime._safe_datetime(state.get("last_attempt_at"))
        if last_attempt is not None:
            attempt_age = (now - last_attempt).total_seconds()
            if attempt_age < runtime.MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS:
                return None, state

        token = secrets.token_hex(16)
        now_iso = now.isoformat().replace("+00:00", "Z")
        expires_at = now + timedelta(seconds=runtime.MODEL_LIBRARY_SYNC_LEASE_SECONDS)
        state["last_attempt_at"] = now_iso
        state["sync_lease"] = {
            "token": token,
            "started_at": now_iso,
            "heartbeat_at": now_iso,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }
        runtime._save_sync_state(state)
        return token, state


async def _claim_library_sync_lease() -> tuple[str | None, dict[str, Any]]:
    runtime = _runtime()
    async with runtime._SYNC_LOCK:
        return await asyncio.to_thread(runtime._claim_library_sync_lease_sync)


def _renew_library_sync_lease_sync(token: str) -> bool:
    runtime = _runtime()
    with runtime._model_library_sync_file_lock(runtime._library_sync_lock_path()):
        state = runtime._read_json_file(
            runtime._library_sync_state_path(),
            runtime._default_sync_state(),
        )
        owner = runtime._sync_lease_owner(state)
        if owner is None or owner[0] != token:
            return False
        now = runtime._now()
        now_iso = now.isoformat().replace("+00:00", "Z")
        lease = dict(state["sync_lease"])
        lease["heartbeat_at"] = now_iso
        lease["expires_at"] = (
            (now + timedelta(seconds=runtime.MODEL_LIBRARY_SYNC_LEASE_SECONDS))
            .isoformat()
            .replace("+00:00", "Z")
        )
        state["sync_lease"] = lease
        runtime._save_sync_state(state)
        return True


async def _renew_library_sync_lease(token: str) -> bool:
    runtime = _runtime()
    async with runtime._SYNC_LOCK:
        return await asyncio.to_thread(
            runtime._renew_library_sync_lease_sync,
            token,
        )


def _complete_library_sync_lease_sync(
    token: str,
    index: dict[str, Any],
    result: dict[str, Any],
    completed_at: datetime,
) -> None:
    runtime = _runtime()
    with runtime._model_library_sync_file_lock(runtime._library_sync_lock_path()):
        state = runtime._read_json_file(
            runtime._library_sync_state_path(),
            runtime._default_sync_state(),
        )
        owner = runtime._sync_lease_owner(state)
        if owner is None or owner[0] != token:
            raise _ModelLibrarySyncLeaseLost("model library sync lease was lost")
        # Publish the atomic index first. A crash before the state write leaves
        # an expiring lease, while the reverse ordering could expose cooldown
        # success with an old index.
        runtime._save_global_library_index(index)
        state["last_success_at"] = completed_at.isoformat().replace("+00:00", "Z")
        state["last_error"] = None
        state["last_result"] = result
        state["sync_lease"] = None
        runtime._save_sync_state(state)


async def _complete_library_sync_lease(
    token: str,
    index: dict[str, Any],
    result: dict[str, Any],
    completed_at: datetime,
) -> None:
    runtime = _runtime()
    async with runtime._SYNC_LOCK:
        await asyncio.to_thread(
            runtime._complete_library_sync_lease_sync,
            token,
            index,
            result,
            completed_at,
        )


def _fail_library_sync_lease_sync(
    token: str,
    *,
    message: str,
    result: dict[str, Any],
) -> bool:
    runtime = _runtime()
    with runtime._model_library_sync_file_lock(runtime._library_sync_lock_path()):
        state = runtime._read_json_file(
            runtime._library_sync_state_path(),
            runtime._default_sync_state(),
        )
        owner = runtime._sync_lease_owner(state)
        if owner is None or owner[0] != token:
            return False
        state["last_error"] = message[:1000]
        state["last_result"] = result
        state["sync_lease"] = None
        runtime._save_sync_state(state)
        return True


async def _fail_library_sync_lease(
    token: str,
    *,
    message: str,
    result: dict[str, Any],
) -> bool:
    runtime = _runtime()
    async with runtime._SYNC_LOCK:
        return await asyncio.to_thread(
            runtime._fail_library_sync_lease_sync,
            token,
            message=message,
            result=result,
        )


def _cached_sync_response(state: dict[str, Any]) -> ApparelModelLibrarySyncOut:
    """从 sync state 拼装一个 'skipped' 响应，用于 cooldown 命中时返回。"""
    runtime = _runtime()
    result = runtime._dict_or_empty(state.get("last_result"))
    return ApparelModelLibrarySyncOut(
        status="skipped",
        added=int(result.get("added") or 0),
        updated=int(result.get("updated") or 0),
        skipped=int(result.get("skipped") or 0),
        errors=runtime._clean_string_list(
            result.get("errors") or [],
            max_items=20,
            max_len=300,
        ),
        last_success_at=runtime._safe_datetime(state.get("last_success_at")),
        last_error=runtime._clean_optional_text(
            state.get("last_error"),
            max_len=1000,
        ),
    )
