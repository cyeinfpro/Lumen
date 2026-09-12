"""Audit persistence helpers for isolated events and caller-owned transactions."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from fastapi import Request

from lumen_core.models import AuditLog

from .observability import audit_write_failures_total
from .ratelimit import client_ip


logger = logging.getLogger(__name__)


class AuditPersistenceError(RuntimeError):
    """Raised when an audit row cannot join a caller-managed transaction."""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        super().__init__(f"audit persistence failed for event_type={event_type!r}")


def hash_email(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def hash_ip(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:16]


def request_ip_hash(request: Request | None) -> str | None:
    if request is None:
        return None
    ip = client_ip(request)
    if ip == "unknown":
        return None
    return hash_ip(ip)


async def write_audit(
    session: Any,
    *,
    event_type: str,
    user_id: str | None = None,
    actor_email: str | None = None,
    actor_email_hash: str | None = None,
    actor_ip_hash: str | None = None,
    target_user_id: str | None = None,
    details: dict[str, Any] | None = None,
    autocommit: bool = True,
) -> bool:
    """Write audit rows without relying on implicit session type checks.

    `autocommit=True` uses an isolated transaction so audit failures do not
    poison the caller's transaction and reports success as a best-effort
    boolean. `autocommit=False` writes through the supplied session and leaves
    commit/rollback ownership with the caller.

    Caller-transaction writes return ``True`` on success and raise
    :class:`AuditPersistenceError` on failure. A database flush failure leaves
    the caller transaction unusable until rollback, preventing business changes
    from committing without their audit row.
    """
    if autocommit:
        return await write_audit_isolated(
            event_type=event_type,
            user_id=user_id,
            actor_email=actor_email,
            actor_email_hash=actor_email_hash,
            actor_ip_hash=actor_ip_hash,
            target_user_id=target_user_id,
            details=details,
        )

    row = AuditLog(
        user_id=user_id,
        event_type=event_type,
        actor_email_hash=actor_email_hash or hash_email(actor_email),
        actor_ip_hash=actor_ip_hash,
        target_user_id=target_user_id,
        details=details or {},
    )
    session.add(row)
    try:
        await session.flush()
    except Exception as exc:  # noqa: BLE001
        audit_write_failures_total.labels(mode="session").inc()
        logger.exception(
            "audit transaction write failed event_type=%s",
            event_type,
        )
        raise AuditPersistenceError(event_type) from exc
    return True


async def write_audit_isolated(
    *,
    event_type: str,
    user_id: str | None = None,
    actor_email: str | None = None,
    actor_email_hash: str | None = None,
    actor_ip_hash: str | None = None,
    target_user_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> bool:
    """独立事务写 audit_log，用于即将 raise/rollback 的场景（如登录失败）。

    自带 SessionLocal()，写完立即 commit；任何异常 logger.error 并返回 False。
    """
    from .db import SessionLocal

    try:
        async with SessionLocal() as session, session.begin():
            row = AuditLog(
                user_id=user_id,
                event_type=event_type,
                actor_email_hash=actor_email_hash or hash_email(actor_email),
                actor_ip_hash=actor_ip_hash,
                target_user_id=target_user_id,
                details=details or {},
            )
            session.add(row)
        return True
    except Exception as exc:  # noqa: BLE001
        audit_write_failures_total.labels(mode="isolated").inc()
        # ERROR (was warning): audit failures must surface in alerting so
        # security events are not silently dropped on DB outages.
        logger.error(
            "CRITICAL: audit isolated write failed event_type=%s err=%s",
            event_type,
            exc,
        )
        return False


__all__ = [
    "AuditPersistenceError",
    "write_audit",
    "write_audit_isolated",
    "hash_email",
    "hash_ip",
    "request_ip_hash",
]
