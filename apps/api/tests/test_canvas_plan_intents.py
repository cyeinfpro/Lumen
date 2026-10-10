from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from lumen_core.canvas import canonical_hash
from app.canvas_services.plan_intents import get_plan_intent
from app.canvas_services.plan_overlap_guard import require_plan_nodes_available


class ReadSession:
    def __init__(self, row=None, rows=None):
        self.row, self.rows, self.statements = row, rows or [], []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(
            scalar_one_or_none=lambda: self.row,
            scalars=lambda: self.rows,
        )


@pytest.fixture
def owned(monkeypatch):
    async def owned_canvas(db, *, user_id, canvas_id):
        assert user_id == "owner" and canvas_id == "canvas"
        return object()

    async def detail(db, *, user_id, canvas_id, run_id):
        assert user_id == "owner" and canvas_id == "canvas"
        return {"id": run_id, "executions": []}

    monkeypatch.setattr("app.canvas_services.plan_intents.get_owned_canvas", owned_canvas)
    monkeypatch.setattr("app.canvas_services.plan_intents.get_run_detail", detail)


@pytest.mark.asyncio
async def test_intent_lookup_is_owner_canvas_key_scoped_read_only(owned):
    db = ReadSession(SimpleNamespace(id="run", kind="selection"))
    result = await get_plan_intent(db, user_id="owner", canvas_id="canvas", idempotency_key="key")
    assert result == {"admitted": True, "run": {"id": "run", "executions": []}}
    params = db.statements[0].compile().params
    assert {"owner", "canvas", "key"} <= set(params.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, SimpleNamespace(kind="single")])
async def test_absence_and_single_run_are_not_batch_admission(owned, row):
    result = await get_plan_intent(ReadSession(row), user_id="owner", canvas_id="canvas", idempotency_key="key")
    assert result == {"admitted": False, "run": None}


@pytest.mark.asyncio
async def test_repair_requires_exact_persisted_receipt_key(owned):
    row = SimpleNamespace(id="run", kind="selection", summary_jsonb={
        "repair_requests": {canonical_hash({"key": "repair-key"}): "fingerprint"}
    })
    db = ReadSession(row)
    missing = await get_plan_intent(db, user_id="owner", canvas_id="canvas", idempotency_key="other", run_id="run")
    assert not missing["admitted"]
    found = await get_plan_intent(db, user_id="owner", canvas_id="canvas", idempotency_key="repair-key", run_id="run")
    assert found["admitted"]


@pytest.mark.asyncio
async def test_unauthorized_canvas_never_queries_intent(monkeypatch):
    async def denied(*args, **kwargs):
        raise HTTPException(404, "not found")
    monkeypatch.setattr("app.canvas_services.plan_intents.get_owned_canvas", denied)
    db = ReadSession()
    with pytest.raises(HTTPException):
        await get_plan_intent(db, user_id="other", canvas_id="canvas", idempotency_key="key")
    assert not db.statements


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "ready", "queued", "running", "reconciling", "canceling"])
async def test_overlap_guard_blocks_every_active_phase(status):
    row = SimpleNamespace(id="execution", node_id="a", run_id="run", status=status)
    db = ReadSession(rows=[row])
    with pytest.raises(HTTPException) as error:
        await require_plan_nodes_available(db, user_id="owner", canvas_id="canvas", node_ids=["a"])
    assert error.value.detail["error"]["code"] == "canvas_execution_active"
    params = db.statements[0].compile().params
    assert params["user_id_1"] == "owner" and params["canvas_id_1"] == "canvas"
    assert params["node_id_1"] == ["a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["submission_unknown", "unavailable", "running", "queued", "reconciling", "saving_artifact", "cancel_requested"])
async def test_terminal_canvas_projection_cannot_hide_unknown_owner(monkeypatch, state):
    row = SimpleNamespace(id="execution", node_id="a", run_id="run", status="expired")
    async def task_details(db, rows):
        return {"execution": [{"recovery": {"state": state}}]}
    monkeypatch.setattr("app.canvas_services.plan_overlap_guard.execution_tasks_by_execution", task_details)
    with pytest.raises(HTTPException) as error:
        await require_plan_nodes_available(ReadSession(rows=[row]), user_id="owner", canvas_id="canvas", node_ids=["a"])
    assert error.value.detail["error"]["code"] == "canvas_execution_unknown"


@pytest.mark.asyncio
async def test_confirmed_terminal_owner_does_not_block_new_plan(monkeypatch):
    row = SimpleNamespace(id="execution", node_id="a", run_id="run", status="failed")
    async def task_details(db, rows):
        return {"execution": [{"recovery": {"state": "failed"}}]}
    monkeypatch.setattr("app.canvas_services.plan_overlap_guard.execution_tasks_by_execution", task_details)
    await require_plan_nodes_available(ReadSession(rows=[row]), user_id="owner", canvas_id="canvas", node_ids=["a"])
    db = ReadSession()
    await require_plan_nodes_available(db, user_id="owner", canvas_id="canvas", node_ids=[])
    assert not db.statements
