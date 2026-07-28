"""Cross-process model library sync lease and state transitions."""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta
from typing import Any

from lumen_core.schemas import ApparelModelLibrarySyncOut

from ..domain.errors import ModelLibrarySyncLeaseLost
from ..domain.apparel_library import (
    MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS,
    MODEL_LIBRARY_SYNC_LEASE_SECONDS,
    MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS,
)
from ..domain.apparel_library import (
    model_library_sync_file_lock as _model_library_sync_file_lock,
)  # noqa: F401
from ..ports.runtime_state import AsyncLockPort
from .library_storage import default_sync_state as _default_sync_state  # noqa: F401
from .library_storage import library_sync_lock_path as _library_sync_lock_path  # noqa: F401
from .library_storage import library_sync_state_path as _library_sync_state_path  # noqa: F401
from .library_storage import read_json_file as _read_json_file  # noqa: F401
from .library_storage import save_global_library_index as _save_global_library_index  # noqa: F401
from .library_storage import save_sync_state as _save_sync_state  # noqa: F401
from .serialization import clean_optional_text as _clean_optional_text  # noqa: F401
from .serialization import clean_string_list as _clean_string_list  # noqa: F401
from .serialization import dict_or_empty as _dict_or_empty  # noqa: F401
from .serialization import now as _now  # noqa: F401
from .serialization import safe_datetime as _safe_datetime  # noqa: F401


class _ModelLibrarySyncLeaseLost(ModelLibrarySyncLeaseLost):
    """The sync lease expired or was replaced before this worker finished."""


def _sync_lease_owner(state: dict[str, Any]) -> tuple[str, datetime] | None:
    lease = state.get("sync_lease")
    if not isinstance(lease, dict):
        return None
    token = str(lease.get("token") or "").strip()
    expires_at = _safe_datetime(lease.get("expires_at"))
    if not token or expires_at is None:
        return None
    return token, expires_at


def _claim_library_sync_lease_sync() -> tuple[str | None, dict[str, Any]]:
    """Atomically claim one cross-process sync lease under a short file lock."""

    with _model_library_sync_file_lock(_library_sync_lock_path()):
        state = _read_json_file(
            _library_sync_state_path(),
            _default_sync_state(),
        )
        now = _now()
        last_success = _safe_datetime(state.get("last_success_at"))
        if last_success is not None:
            success_age = (now - last_success).total_seconds()
            if success_age < MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS:
                return None, state

        owner = _sync_lease_owner(state)
        if owner is not None and owner[1] > now:
            return None, state
        if owner is not None:
            state["sync_lease"] = None

        last_attempt = _safe_datetime(state.get("last_attempt_at"))
        if last_attempt is not None:
            attempt_age = (now - last_attempt).total_seconds()
            if attempt_age < MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS:
                return None, state

        token = secrets.token_hex(16)
        now_iso = now.isoformat().replace("+00:00", "Z")
        expires_at = now + timedelta(seconds=MODEL_LIBRARY_SYNC_LEASE_SECONDS)
        state["last_attempt_at"] = now_iso
        state["sync_lease"] = {
            "token": token,
            "started_at": now_iso,
            "heartbeat_at": now_iso,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }
        _save_sync_state(state)
        return token, state


async def _claim_library_sync_lease(
    sync_lock: AsyncLockPort,
) -> tuple[str | None, dict[str, Any]]:
    async with sync_lock:
        return await asyncio.to_thread(_claim_library_sync_lease_sync)


def _renew_library_sync_lease_sync(token: str) -> bool:
    with _model_library_sync_file_lock(_library_sync_lock_path()):
        state = _read_json_file(
            _library_sync_state_path(),
            _default_sync_state(),
        )
        owner = _sync_lease_owner(state)
        if owner is None or owner[0] != token:
            return False
        now = _now()
        now_iso = now.isoformat().replace("+00:00", "Z")
        lease = dict(state["sync_lease"])
        lease["heartbeat_at"] = now_iso
        lease["expires_at"] = (
            (now + timedelta(seconds=MODEL_LIBRARY_SYNC_LEASE_SECONDS))
            .isoformat()
            .replace("+00:00", "Z")
        )
        state["sync_lease"] = lease
        _save_sync_state(state)
        return True


async def _renew_library_sync_lease(
    sync_lock: AsyncLockPort,
    token: str,
) -> bool:
    async with sync_lock:
        return await asyncio.to_thread(
            _renew_library_sync_lease_sync,
            token,
        )


def _complete_library_sync_lease_sync(
    token: str,
    index: dict[str, Any],
    result: dict[str, Any],
    completed_at: datetime,
) -> None:
    with _model_library_sync_file_lock(_library_sync_lock_path()):
        state = _read_json_file(
            _library_sync_state_path(),
            _default_sync_state(),
        )
        owner = _sync_lease_owner(state)
        if owner is None or owner[0] != token:
            raise _ModelLibrarySyncLeaseLost("model library sync lease was lost")
        # Publish the atomic index first. A crash before the state write leaves
        # an expiring lease, while the reverse ordering could expose cooldown
        # success with an old index.
        _save_global_library_index(index)
        state["last_success_at"] = completed_at.isoformat().replace("+00:00", "Z")
        state["last_error"] = None
        state["last_result"] = result
        state["sync_lease"] = None
        _save_sync_state(state)


async def _complete_library_sync_lease(
    sync_lock: AsyncLockPort,
    token: str,
    index: dict[str, Any],
    result: dict[str, Any],
    completed_at: datetime,
) -> None:
    async with sync_lock:
        await asyncio.to_thread(
            _complete_library_sync_lease_sync,
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
    with _model_library_sync_file_lock(_library_sync_lock_path()):
        state = _read_json_file(
            _library_sync_state_path(),
            _default_sync_state(),
        )
        owner = _sync_lease_owner(state)
        if owner is None or owner[0] != token:
            return False
        state["last_error"] = message[:1000]
        state["last_result"] = result
        state["sync_lease"] = None
        _save_sync_state(state)
        return True


async def _fail_library_sync_lease(
    sync_lock: AsyncLockPort,
    token: str,
    *,
    message: str,
    result: dict[str, Any],
) -> bool:
    async with sync_lock:
        return await asyncio.to_thread(
            _fail_library_sync_lease_sync,
            token,
            message=message,
            result=result,
        )


def _cached_sync_response(state: dict[str, Any]) -> ApparelModelLibrarySyncOut:
    """从 sync state 拼装一个 'skipped' 响应，用于 cooldown 命中时返回。"""
    result = _dict_or_empty(state.get("last_result"))
    return ApparelModelLibrarySyncOut(
        status="skipped",
        added=int(result.get("added") or 0),
        updated=int(result.get("updated") or 0),
        skipped=int(result.get("skipped") or 0),
        errors=_clean_string_list(
            result.get("errors") or [],
            max_items=20,
            max_len=300,
        ),
        last_success_at=_safe_datetime(state.get("last_success_at")),
        last_error=_clean_optional_text(
            state.get("last_error"),
            max_len=1000,
        ),
    )


# Public workflow contracts.
ModelLibrarySyncLeaseLost = _ModelLibrarySyncLeaseLost
cached_sync_response = _cached_sync_response
claim_library_sync_lease = _claim_library_sync_lease
claim_library_sync_lease_sync = _claim_library_sync_lease_sync
complete_library_sync_lease = _complete_library_sync_lease
complete_library_sync_lease_sync = _complete_library_sync_lease_sync
fail_library_sync_lease = _fail_library_sync_lease
fail_library_sync_lease_sync = _fail_library_sync_lease_sync
renew_library_sync_lease = _renew_library_sync_lease
renew_library_sync_lease_sync = _renew_library_sync_lease_sync
sync_lease_owner = _sync_lease_owner
