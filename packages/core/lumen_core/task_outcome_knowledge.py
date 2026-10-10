"""Conservative owner/receipt knowledge shared by repair and presentation."""

from collections.abc import Mapping
from lumen_core.upstream_billing import (
    IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES,
    has_proven_no_cost_dispatch,
    has_proven_undelivered_dispatch,
    has_upstream_dispatch_receipt,
    has_upstream_response_receipt,
)


def task_outcome_unknown(source, *, task_kind):
    if source is None:
        return True
    status = getattr(source, "status", None)
    stage = getattr(source, "progress_stage", None)
    code = str(getattr(source, "error_code", "") or "").strip().lower()
    if (
        status == "submit_unknown"
        or stage == "submit_unknown"
        or code in {"submit_unknown", "result_unknown"}
    ):
        return True
    if task_kind == "generation":
        if code in IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES:
            return True
        # Receipt helpers bind to the owner's current execution_epoch.
        return (
            has_upstream_dispatch_receipt(source)
            and not has_upstream_response_receipt(source)
            and not has_proven_undelivered_dispatch(source)
            and not has_proven_no_cost_dispatch(source)
        )
    if task_kind == "video_generation":
        return video_delivery_unknown(source)
    return True


def video_delivery_unknown(source):
    if getattr(source, "provider_task_id", None):
        return False
    diagnostics = getattr(source, "diagnostics", None)
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    states = [diagnostics.get("submit_delivery_state")]
    history = diagnostics.get("submit_delivery_history")
    if isinstance(history, list):
        states.extend(
            item.get("state") for item in history if isinstance(item, Mapping)
        )
    if "unknown" in states:
        return True
    # Historical confirmation cannot prove the current canceled attempt absent.
    # A known provider task above or an explicit current absence is required.
    if diagnostics.get("submit_delivery_state") == "proven_absent":
        return False
    if "confirmed" in states or isinstance(diagnostics.get("submit_receipt"), Mapping):
        return True
    return bool(
        int(getattr(source, "attempt", 0) or 0) > 0
        or int(getattr(source, "submission_epoch", 0) or 0) > 0
        or getattr(source, "submit_started_at", None) is not None
    )
