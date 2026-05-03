"""Shared helpers for admin_*.py routes (HTTP envelope, audit boilerplate,
subprocess marker cleanup)."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any, Callable, Protocol

from fastapi import HTTPException, Request

from ..audit import hash_email, request_ip_hash, write_audit, write_audit_isolated


class _HasIdEmail(Protocol):
    id: str
    email: str


def admin_http(
    code: str,
    message: str,
    http: int = 400,
    details: Any | None = None,
) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": message}
    if details:
        err["details"] = details
    return HTTPException(status_code=http, detail={"error": err})


async def write_admin_audit(
    db: Any,
    request: Request,
    admin: _HasIdEmail,
    *,
    event_type: str,
    details: dict[str, Any] | None = None,
    target_user_id: str | None = None,
    autocommit: bool = True,
) -> bool:
    return await write_audit(
        db,
        event_type=event_type,
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        target_user_id=target_user_id,
        details=details,
        autocommit=autocommit,
    )


async def write_admin_audit_isolated(
    request: Request,
    admin: _HasIdEmail,
    *,
    event_type: str,
    details: dict[str, Any] | None = None,
    target_user_id: str | None = None,
) -> bool:
    return await write_audit_isolated(
        event_type=event_type,
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        target_user_id=target_user_id,
        details=details,
    )


MarkerLike = Any  # admin_update.UpdateMarker; typed as Any to avoid import cycles.


async def cleanup_marker_when_done(
    proc: subprocess.Popen[bytes],
    *,
    read_marker_fn: Callable[[], MarkerLike | None],
    marker_path_fn: Callable[[], Any],
) -> None:
    """Wait on ``proc``; unlink the marker iff it still references our pid.

    ``read_marker_fn`` and ``marker_path_fn`` are passed in (rather than
    imported here) so admin_update can monkey-patch its own globals in tests
    without touching this module.
    """
    await asyncio.to_thread(proc.wait)
    pid = int(proc.pid)
    marker = read_marker_fn()
    if marker and marker.pid == pid:
        try:
            marker_path_fn().unlink()
        except OSError:
            pass


__all__ = [
    "admin_http",
    "cleanup_marker_when_done",
    "write_admin_audit",
    "write_admin_audit_isolated",
]
