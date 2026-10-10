"""Minimal owner-stream notices for committed Canvas Run events.

The database RunEvent sequence remains authoritative. These notices carry no
provider payload or pricing data; clients fetch owner-checked event batches.
"""

from __future__ import annotations

import re
from typing import Any

CANVAS_RUN_EVENT = "canvas.run.updated"
_EVENT_KIND = re.compile(r"[a-z][a-z0-9_.:-]{0,79}")


def canvas_run_notification(
    *,
    canvas_id: str,
    run_id: str,
    seq: int,
    execution_id: str | None = None,
    event_type: str,
) -> dict[str, Any]:
    """Build one stable, allow-listed hint, never an execution command."""
    for value in (canvas_id, run_id):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError("invalid Canvas identity")
    if execution_id is not None and (
        not isinstance(execution_id, str) or not execution_id or len(execution_id) > 128
    ):
        raise ValueError("invalid execution identity")
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 < seq <= 2**53 - 1:
        raise ValueError("invalid Canvas event sequence")
    if not isinstance(event_type, str) or not _EVENT_KIND.fullmatch(event_type):
        raise ValueError("invalid Canvas event kind")
    return {
        "schema_version": 1,
        "canvas_id": canvas_id,
        "run_id": run_id,
        "seq": seq,
        "execution_id": execution_id,
        "event_type": event_type,
        "event_id": f"canvas-run:{run_id}:{seq}",
    }
