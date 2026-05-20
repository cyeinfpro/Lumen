"""Queue observability metadata helpers.

These helpers intentionally derive labels from existing task fields. Image
workers now use the same stable labels for weighted-fair scheduling and for
queue observability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


_SMALL_PIXEL_MAX = 1_600_000
_MEDIUM_PIXEL_MAX = 4_000_000


def _request_dict(value: dict[str, Any] | None) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_pixel_count(size: str | None) -> int | None:
    """Parse ``"{width}x{height}"`` task sizes into pixels."""

    if not isinstance(size, str) or "x" not in size:
        return None
    raw_w, raw_h = size.lower().split("x", 1)
    if not raw_w.isdigit() or not raw_h.isdigit():
        return None
    width = int(raw_w)
    height = int(raw_h)
    if width <= 0 or height <= 0:
        return None
    return width * height


def size_bucket(pixel_count: int | None) -> str | None:
    """Return the fair-scheduling size bucket label for image tasks."""

    if pixel_count is None:
        return None
    if pixel_count <= _SMALL_PIXEL_MAX:
        return "small"
    if pixel_count <= _MEDIUM_PIXEL_MAX:
        return "medium"
    return "large"


def queue_wait_ms(
    *,
    created_at: datetime | None,
    started_at: datetime | None,
    finished_at: datetime | None = None,
    now: datetime | None = None,
) -> int | None:
    """Return observed queue wait.

    For started tasks, this is ``started_at - created_at``. For queued tasks it
    is the current waiting age. Terminal rows that never started keep ``None``.
    """

    created = _ensure_utc(created_at)
    if created is None:
        return None
    started = _ensure_utc(started_at)
    if started is None:
        if finished_at is not None:
            return None
        started = _ensure_utc(now) or datetime.now(timezone.utc)
    return max(0, int((started - created).total_seconds() * 1000))


def cost_class(
    *,
    action: str | None,
    size_bucket_value: str | None,
    has_mask: bool = False,
    is_dual_race: bool = False,
) -> str:
    """Coarse class for cost/latency observability."""

    if is_dual_race:
        return "dual_race"
    if action == "edit":
        return "mask_edit" if has_mask else "edit"
    if size_bucket_value in {"small", "medium", "large"}:
        return size_bucket_value
    return "unknown"


def queue_lane(
    *,
    kind: str,
    action: str | None = None,
    workflow_type: str | None = None,
    size_bucket_value: str | None = None,
    has_mask: bool = False,
) -> str:
    """Stable lane label for dashboards and image fair scheduling."""

    if kind == "completion":
        return "completion:interactive"
    source = "workflow" if workflow_type else "interactive"
    if action == "edit":
        bucket = "mask_edit" if has_mask else "edit"
    else:
        bucket = size_bucket_value or "unknown"
    return f"image:{source}:{bucket}"


def merge_queue_metadata(
    upstream_request: dict[str, Any] | None,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Copy queue metadata onto an upstream_request JSON payload.

    The flat keys are the legacy worker/read-model contract.  The nested
    ``queue_metadata`` copy is the durable namespace for newer consumers.  Keep
    both in sync and avoid feeding merged payloads back into this helper unless
    the caller intentionally wants to refresh the derived fields.
    """

    out = dict(upstream_request or {})
    clean = {
        key: value
        for key, value in metadata.items()
        if value is not None and value != ""
    }
    out.update(clean)
    nested = out.get("queue_metadata")
    nested_out = dict(nested) if isinstance(nested, dict) else {}
    nested_out.update(clean)
    out["queue_metadata"] = nested_out
    return out


def _metadata_value(
    request: dict[str, Any],
    key: str,
    nested_key: str | None = None,
) -> Any:
    if key in request:
        return request[key]
    nested = request.get("queue_metadata")
    if isinstance(nested, dict):
        return nested.get(nested_key or key)
    return None


def generation_queue_metadata(
    *,
    upstream_request: dict[str, Any] | None,
    action: str | None,
    size_requested: str | None,
    mask_image_id: str | None = None,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    upstream_pixels: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build queue observability fields for image generation tasks."""

    request = _request_dict(upstream_request)
    pixel_count = _positive_int(_metadata_value(request, "pixel_count"))
    if pixel_count is None:
        pixel_count = _positive_int(upstream_pixels) or parse_pixel_count(size_requested)
    bucket = _text(_metadata_value(request, "size_bucket")) or size_bucket(pixel_count)
    workflow_type = _text(_metadata_value(request, "workflow_type"))
    workflow_step_key = _text(_metadata_value(request, "workflow_step_key"))
    has_mask = bool(mask_image_id)
    is_dual_race = (
        request.get("image_route") == "dual_race"
        or request.get("upstream_route") == "dual_race"
    )
    queue_wait = queue_wait_ms(
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        now=now,
    )
    if queue_wait is None:
        queue_wait = _nonnegative_int(_metadata_value(request, "queue_wait_ms"))
    cost = _text(_metadata_value(request, "cost_class")) or cost_class(
        action=action,
        size_bucket_value=bucket,
        has_mask=has_mask,
        is_dual_race=is_dual_race,
    )
    # Recompute lane from canonical fields instead of trusting a stale stored
    # queue_lane. Workflow metadata can be attached after initial task creation,
    # and scheduling must reflect the current task shape.
    lane = queue_lane(
        kind="generation",
        action=action,
        workflow_type=workflow_type,
        size_bucket_value=bucket,
        has_mask=has_mask,
    )
    return {
        "queue_lane": lane,
        "workflow_type": workflow_type,
        "workflow_step_key": workflow_step_key,
        "pixel_count": pixel_count,
        "size_bucket": bucket,
        "cost_class": cost,
        "queue_wait_ms": queue_wait,
    }


def completion_queue_metadata(
    *,
    upstream_request: dict[str, Any] | None,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build queue observability fields for completion tasks."""

    request = _request_dict(upstream_request)
    workflow_type = _text(_metadata_value(request, "workflow_type"))
    workflow_step_key = _text(_metadata_value(request, "workflow_step_key"))
    queue_wait = queue_wait_ms(
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        now=now,
    )
    if queue_wait is None:
        queue_wait = _nonnegative_int(_metadata_value(request, "queue_wait_ms"))
    lane = _text(_metadata_value(request, "queue_lane")) or queue_lane(
        kind="completion",
        workflow_type=workflow_type,
    )
    cost = _text(_metadata_value(request, "cost_class")) or "completion"
    return {
        "queue_lane": lane,
        "workflow_type": workflow_type,
        "workflow_step_key": workflow_step_key,
        "pixel_count": None,
        "size_bucket": None,
        "cost_class": cost,
        "queue_wait_ms": queue_wait,
    }
