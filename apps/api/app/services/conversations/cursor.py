"""Cursor encoding and shared conversation query helpers."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from lumen_core.models import Conversation, Message


CURSOR_VERSION = 1
logger = logging.getLogger(__name__)


def enc_cursor(payload: dict[str, Any]) -> str:
    body = {"v": CURSOR_VERSION, **payload}
    raw = json.dumps(body, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def dec_cursor(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        pad = "=" * (-len(raw) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(raw + pad).decode())
    except Exception:
        logger.warning("cursor decode failed", exc_info=True)
        return None
    if not isinstance(decoded, dict):
        return None
    version = decoded.get("v")
    if version is not None and version != CURSOR_VERSION:
        logger.warning(
            "cursor version mismatch: got=%r want=%d", version, CURSOR_VERSION
        )
        return None
    return decoded


def bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": {"code": code, "message": message}},
    )


def cursor_field_str(cur: dict[str, Any], field: str) -> str:
    value = cur.get(field)
    if not isinstance(value, str) or not value:
        raise bad_request("invalid_cursor", "invalid cursor")
    return value


def coerce_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def cursor_field_datetime(cur: dict[str, Any], field: str) -> datetime:
    value = cursor_field_str(cur, field)
    try:
        return coerce_aware(datetime.fromisoformat(value))
    except ValueError as exc:
        raise bad_request("invalid_cursor", "invalid cursor timestamp") from exc


def exclude_workflow_conversations(stmt: Any) -> Any:
    return stmt.where(Conversation.default_params["workflow_type"].astext.is_(None))


def message_alive_filters() -> tuple[Any, ...]:
    deleted_at = getattr(Message, "deleted_at", None)
    if deleted_at is None:
        return ()
    return (deleted_at.is_(None),)


__all__ = [
    "CURSOR_VERSION",
    "bad_request",
    "coerce_aware",
    "cursor_field_datetime",
    "cursor_field_str",
    "dec_cursor",
    "enc_cursor",
    "exclude_workflow_conversations",
    "message_alive_filters",
]
