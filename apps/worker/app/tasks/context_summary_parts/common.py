from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeGuard

from lumen_core.context_window import (
    compare_message_position,
    is_summary_usable,
)
from lumen_core.models import Message


@dataclass(frozen=True)
class LoadedSummaryMessages:
    messages: list[Message]
    source_message_count: int
    source_token_estimate: int
    image_caption_count: int
    image_captions: dict[str, str] | None = None


@dataclass
class SummaryLock:
    kind: str
    token: str | None = None
    lost_reason: str | None = None
    pg_connection: Any | None = None
    pg_key: str | None = None


@dataclass
class SummaryCoverage:
    covered_message_count: int = 0
    partial_reason: str | None = None


@dataclass(frozen=True)
class SummarySegment:
    lines: list[str]
    covered_message_count: int
    ends_at_message_boundary: bool


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 20:
        return text[:limit]
    return text[: limit - 15].rstrip() + " [...truncated]"


def settings_get(settings: Any, key: str, default: Any) -> Any:
    if settings is None:
        return default
    if isinstance(settings, dict):
        if key in settings:
            return settings[key]
        alt = key.replace(".", "_")
        return settings.get(alt, default)
    if hasattr(settings, "get"):
        try:
            value = settings.get(key)
            if value is not None:
                return value
        except Exception:  # noqa: BLE001
            pass
    return getattr(settings, key.replace(".", "_"), default)


def settings_int(settings: Any, key: str, default: int) -> int:
    value = settings_get(settings, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def settings_float(settings: Any, key: str, default: float) -> float:
    value = settings_get(settings, key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def settings_str(settings: Any, key: str, default: str) -> str:
    value = settings_get(settings, key, default)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def extra_instruction_hash(extra_instruction: str | None) -> str | None:
    if not extra_instruction or not extra_instruction.strip():
        return None
    digest = hashlib.sha1(
        extra_instruction.strip().encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    return f"sha1:{digest}"


def boundary_id(boundary: Any) -> str | None:
    if boundary is None:
        return None
    if isinstance(boundary, str):
        return boundary
    if isinstance(boundary, dict):
        for key in ("message_id", "id", "boundary_id", "up_to_message_id"):
            value = boundary.get(key)
            if isinstance(value, str) and value:
                return value
        return None
    value = getattr(boundary, "id", None)
    return value if isinstance(value, str) and value else None


def boundary_created_at(boundary: Any) -> datetime | None:
    if isinstance(boundary, dict):
        value = boundary.get("created_at") or boundary.get("up_to_created_at")
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
    value = getattr(boundary, "created_at", None)
    return value if isinstance(value, datetime) else None


def coerce_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse_iso_datetime(raw: str) -> datetime | None:
    try:
        return coerce_aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def summary_covers_boundary(
    summary: object,
    boundary: Any,
) -> TypeGuard[dict[str, Any]]:
    if not isinstance(summary, dict) or not is_summary_usable(summary):
        return False
    bid = boundary_id(boundary)
    summary_id = summary.get("up_to_message_id")
    summary_id = summary_id if isinstance(summary_id, str) and summary_id else None
    if bid and summary_id == bid:
        return True
    bdt = boundary_created_at(boundary)
    if bdt is None:
        return False
    raw = summary.get("up_to_created_at")
    if not isinstance(raw, str):
        return False
    sdt = parse_iso_datetime(raw)
    if sdt is None:
        return False
    return compare_message_position(sdt, summary_id, bdt, bid) >= 0


def summary_satisfies_request(
    summary: object,
    boundary: Any,
    extra_hash: str | None,
) -> TypeGuard[dict[str, Any]]:
    if not summary_covers_boundary(summary, boundary):
        return False
    return summary.get("extra_instruction_hash") == extra_hash


def summary_quality_rank(summary: dict[str, Any] | None) -> int:
    if not isinstance(summary, dict) or not is_summary_usable(summary):
        return -1
    fallback_reason = summary.get("fallback_reason") or summary.get(
        "last_quality_signal"
    )
    return 0 if fallback_reason else 1


def summary_int(summary: dict[str, Any] | None, key: str) -> int:
    if not isinstance(summary, dict):
        return 0
    try:
        return max(0, int(summary.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def summary_dt(summary: dict[str, Any] | None, key: str) -> datetime | None:
    if not isinstance(summary, dict):
        return None
    raw = summary.get(key)
    if isinstance(raw, datetime):
        return coerce_aware(raw)
    if isinstance(raw, str):
        return parse_iso_datetime(raw)
    return None


def current_summary_wins_equal_boundary(
    current: dict[str, Any],
    new: dict[str, Any],
    *,
    allow_equal_boundary_refresh: bool = False,
) -> bool:
    """Return True when an equal-boundary CAS write should keep current."""
    if current.get("extra_instruction_hash") != new.get("extra_instruction_hash"):
        return False

    current_quality = summary_quality_rank(current)
    new_quality = summary_quality_rank(new)
    if current_quality > new_quality:
        return True
    if current_quality < new_quality:
        return False

    current_runs = summary_int(current, "compression_runs")
    new_runs = summary_int(new, "compression_runs")
    if current_runs > new_runs:
        return True
    if current_runs < new_runs:
        return False

    current_compressed_at = summary_dt(current, "compressed_at")
    new_compressed_at = summary_dt(new, "compressed_at")
    if (
        current_compressed_at
        and new_compressed_at
        and current_compressed_at > new_compressed_at
    ):
        return True

    return not allow_equal_boundary_refresh


def public_summary_result(
    summary: dict[str, Any],
    *,
    created: bool,
    status: str,
) -> dict[str, Any]:
    source_tokens = int(summary.get("source_token_estimate") or 0)
    summary_tokens = int(summary.get("tokens") or 0)
    return {
        "status": status,
        "summary_created": created,
        "summary_used": True,
        "summary_up_to_message_id": summary.get("up_to_message_id"),
        "summary_up_to_created_at": summary.get("up_to_created_at"),
        "summary_tokens": summary_tokens,
        "source_message_count": int(summary.get("source_message_count") or 0),
        "source_token_estimate": source_tokens,
        "image_caption_count": int(summary.get("image_caption_count") or 0),
        "tokens_freed": max(0, source_tokens - summary_tokens),
        "extra_instruction_hash": summary.get("extra_instruction_hash"),
        "fallback_reason": summary.get("fallback_reason"),
    }
