from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.canvas_services.plan_retry_guard import require_terminal_owner


class OwnerSession:
    def __init__(self, owner):
        self.owner = owner

    async def execute(self, statement):
        assert "user_id" in str(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: self.owner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["submit_unknown", "submitting", "submitted", "running", "queued", "succeeded"],
)
async def test_real_owner_blocks_retry_even_if_canvas_projection_says_failed(status):
    task = SimpleNamespace(
        task_kind="video_generation", video_generation_id="video-task", output_jsonb={}
    )
    owner = SimpleNamespace(status=status, progress_stage="submitting")
    with pytest.raises(HTTPException) as denied:
        await require_terminal_owner(OwnerSession(owner), user_id="user", task=task)
    assert denied.value.detail["error"]["code"] == "canvas_plan_retry_unsafe"


@pytest.mark.asyncio
async def test_terminal_owner_and_empty_output_are_both_required():
    task = SimpleNamespace(
        task_kind="generation", generation_id="task", output_jsonb={}
    )
    owner = SimpleNamespace(status="failed", progress_stage="failed")
    await require_terminal_owner(OwnerSession(owner), user_id="user", task=task)
    task.output_jsonb = {"image_id": "saved"}
    with pytest.raises(HTTPException):
        await require_terminal_owner(OwnerSession(owner), user_id="user", task=task)
    task.output_jsonb = {}
    owner.progress_stage = "submit_unknown"
    with pytest.raises(HTTPException):
        await require_terminal_owner(OwnerSession(owner), user_id="user", task=task)
