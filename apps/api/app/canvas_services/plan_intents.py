"""Read-only recovery of lost admission acknowledgements, scoped to the owner.

Absence is not permission to allocate a new paid intent. Only the original
immutable body/key can be replayed through the serialized admission service.
"""
from sqlalchemy import select
from lumen_core.canvas import canonical_hash
from lumen_core.canvas_models import CanvasRun
from .document_service import get_owned_canvas
from .run_serialization import get_run_detail


async def get_plan_intent(db, *, user_id, canvas_id, idempotency_key, run_id=None):
    await get_owned_canvas(db, user_id=user_id, canvas_id=canvas_id)
    query = select(CanvasRun).where(
        CanvasRun.user_id == user_id, CanvasRun.canvas_id == canvas_id
    )
    if run_id is None:
        query = query.where(CanvasRun.idempotency_key == idempotency_key)
    else:
        query = query.where(CanvasRun.id == run_id)
    run = (await db.execute(query)).scalar_one_or_none()
    admitted = run is not None and run.kind != "single"
    if admitted and run_id is not None:
        requests = (run.summary_jsonb or {}).get("repair_requests", {})
        admitted = canonical_hash({"key": idempotency_key}) in requests
    if not admitted:
        return {"admitted": False, "run": None}
    return {
        "admitted": True,
        "run": await get_run_detail(
            db, user_id=user_id, canvas_id=canvas_id, run_id=run.id
        ),
    }
