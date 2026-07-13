from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumen_core.canvas import canvas_node_definition_hash  # noqa: E402
from lumen_core.canvas_models import (  # noqa: E402
    CanvasDocument,
    CanvasExecutionTask,
    CanvasNodeExecution,
    CanvasNodeSelection,
    CanvasRun,
)
from lumen_core.constants import (  # noqa: E402
    GenerationStatus,
    VideoGenerationStatus,
)
from lumen_core.models import Generation, Image, VideoGeneration  # noqa: E402

from app.main import WorkerSettings  # noqa: E402
from app.tasks import canvas_execution_reconcile as reconcile  # noqa: E402


NOW = datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc)


class _Result:
    def __init__(
        self,
        *,
        scalar: Any = None,
        scalars: list[Any] | None = None,
        rows: list[Any] | None = None,
        rowcount: int = 0,
    ) -> None:
        self._scalar = scalar
        self._scalars = scalars or []
        self._rows = rows or []
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalars(self) -> list[Any]:
        return self._scalars

    def all(self) -> list[Any]:
        return self._rows


class _ProjectionSession:
    def __init__(self, real: Any, asset: Any = None) -> None:
        self.real = real
        self.asset = asset

    async def get(self, _model: Any, _key: str | None) -> Any:
        return self.real

    async def execute(self, _statement: Any) -> _Result:
        return _Result(scalar=self.asset)


class _Begin(AbstractAsyncContextManager["_FakeSession"]):
    def __init__(self, session: "_FakeSession") -> None:
        self.session = session

    async def __aenter__(self) -> "_FakeSession":
        return self.session

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeSession(AbstractAsyncContextManager["_FakeSession"]):
    bind = None

    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.statements: list[Any] = []
        self.added: list[Any] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def begin(self) -> _Begin:
        return _Begin(self)

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return self.results.pop(0)

    def add(self, row: Any) -> None:
        self.added.append(row)


def _execution(
    *,
    status: str = "running",
    selection_base_revision: int = 0,
) -> CanvasNodeExecution:
    return CanvasNodeExecution(
        id="exec-1",
        canvas_id="canvas-1",
        run_id="run-1",
        user_id="user-1",
        node_id="node-1",
        node_type="image_generation",
        node_schema_version=1,
        sequence=0,
        attempt=0,
        attempt_epoch=3,
        status=status,
        definition_hash="d" * 64,
        input_hash="i" * 64,
        execution_fingerprint="e" * 64,
        submission_idempotency_key="exec-idempotency",
        request_fingerprint="r" * 64,
        config_snapshot_jsonb={},
        input_snapshot_jsonb={},
        model_snapshot_jsonb={},
        pricing_snapshot_jsonb={},
        processor_version="test",
        outputs_jsonb=[],
        selection_base_revision=selection_base_revision,
    )


def _task(
    ordinal: int = 0,
    *,
    kind: str = "generation",
    status: str = "running",
) -> CanvasExecutionTask:
    return CanvasExecutionTask(
        id=f"task-{ordinal}",
        execution_id="exec-1",
        ordinal=ordinal,
        task_kind=kind,
        generation_id=f"gen-{ordinal}" if kind == "generation" else None,
        video_generation_id=f"video-gen-{ordinal}"
        if kind == "video_generation"
        else None,
        completion_id=None,
        status=status,
        idempotency_key=f"task-idempotency-{ordinal}",
        request_fingerprint="r" * 64,
        billing_ref_type=kind,
        billing_ref_id=f"billing-{ordinal}",
        output_jsonb={},
    )


def _run() -> CanvasRun:
    return CanvasRun(
        id="run-1",
        canvas_id="canvas-1",
        version_id="version-1",
        user_id="user-1",
        kind="single",
        status="running",
        failure_policy="continue_independent",
        run_epoch=0,
        last_event_seq=0,
        target_node_ids=["node-1"],
        idempotency_key="run-idempotency",
        request_fingerprint="r" * 64,
        budget_micro=0,
        reserved_micro=0,
        spent_micro=0,
        estimated_cost_micro=0,
        summary_jsonb={},
    )


@pytest.mark.asyncio
async def test_projects_generation_success_to_stable_image_output() -> None:
    generation = Generation(
        id="gen-0",
        message_id="message-1",
        user_id="user-1",
        action="generate",
        prompt="test",
        size_requested="1024x1024",
        aspect_ratio="1:1",
        idempotency_key="task-idempotency-0",
        upstream_request={"request_fingerprint": "r" * 64},
        status=GenerationStatus.SUCCEEDED.value,
        attempt=2,
        started_at=NOW - timedelta(seconds=5),
        finished_at=NOW,
    )
    image = Image(
        id="image-1",
        user_id="user-1",
        owner_generation_id="gen-0",
        source="generated",
        storage_key="images/image-1.png",
        mime="image/png",
        width=1024,
        height=768,
        size_bytes=12,
        sha256="a" * 64,
    )

    projection = await reconcile._project_generation(  # noqa: SLF001
        _ProjectionSession(generation, image),
        _task(),
    )

    assert projection is not None
    assert projection.status == "succeeded"
    assert projection.task_epoch == 2
    assert projection.output == {
        "type": "image",
        "ordinal": 0,
        "image_id": "image-1",
        "generation_id": "gen-0",
        "width": 1024,
        "height": 768,
        "mime": "image/png",
        "sha256": "a" * 64,
    }


@pytest.mark.asyncio
async def test_projects_video_expired_as_failed_leaf_without_output() -> None:
    generation = VideoGeneration(
        id="video-gen-0",
        user_id="user-1",
        action="generate",
        model="video-model",
        prompt="test",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        deadline_at=NOW,
        idempotency_key="task-idempotency-0",
        request_fingerprint="r" * 64,
        est_token_upper=0,
        est_cost_micro=0,
        status=VideoGenerationStatus.EXPIRED.value,
        submission_epoch=4,
        error_code="deadline_expired",
        error_message="provider history expired",
        finished_at=NOW,
    )

    projection = await reconcile._project_video_generation(  # noqa: SLF001
        _ProjectionSession(generation),
        _task(kind="video_generation"),
    )

    assert projection is not None
    assert projection.status == "expired"
    assert projection.output is None
    assert projection.task_epoch == 4
    assert projection.error_code == "deadline_expired"


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["succeeded"], "succeeded"),
        (["succeeded", "failed"], "partial_failed"),
        (["succeeded", "canceled"], "partial_failed"),
        (["failed", "expired"], "failed"),
        (["canceled", "canceled"], "canceled"),
        (["succeeded", "running"], None),
    ],
)
def test_aggregate_execution_terminal_statuses(
    statuses: list[str],
    expected: str | None,
) -> None:
    assert reconcile._aggregate_execution_status(statuses) == expected  # noqa: SLF001


def test_active_projection_never_turns_transport_uncertainty_into_cancel() -> None:
    assert (
        reconcile._active_execution_status(  # noqa: SLF001
            "running",
            ["succeeded", "running"],
            unresolved_output=True,
        )
        == "reconciling"
    )
    assert (
        reconcile._active_execution_status(  # noqa: SLF001
            "canceling",
            ["running"],
            unresolved_output=True,
        )
        == "canceling"
    )


@pytest.mark.asyncio
async def test_active_output_cas_requires_base_revision_and_unlocked_row() -> None:
    execution = _execution(selection_base_revision=7)
    success_session = _FakeSession([_Result(rowcount=1)])
    stale_session = _FakeSession([_Result(rowcount=0)])

    assert await reconcile._cas_active_output(success_session, execution) is True  # noqa: SLF001
    assert await reconcile._cas_active_output(stale_session, execution) is False  # noqa: SLF001

    sql = str(success_session.statements[0])
    assert "canvas_node_selections.revision" in sql
    assert "canvas_node_selections.locked" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_prompt", "expected"),
    [("Render a still", True), ("Changed prompt", False)],
)
async def test_auto_select_currentness_ignores_layout_only_revisions(
    current_prompt: str,
    expected: bool,
) -> None:
    graph = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "prompt-1",
                "type": "prompt",
                "schema_version": 1,
                "title": "Prompt",
                "position": {"x": 0, "y": 0},
                "config": {"text": current_prompt, "locked": False},
                "ui": {},
            },
            {
                "id": "node-1",
                "type": "image_generate",
                "schema_version": 1,
                "title": "Image",
                "position": {"x": 900, "y": 500},
                "config": {},
                "ui": {},
            },
        ],
        "edges": [
            {
                "id": "prompt-image",
                "source_node_id": "prompt-1",
                "source_handle": "text",
                "target_node_id": "node-1",
                "target_handle": "prompt",
                "data_type": "text",
                "binding_mode": "follow_active",
                "order": 0,
            }
        ],
        "frames": [],
        "settings": {"snap_to_grid": False, "grid_size": 16},
    }
    execution = _execution()
    execution.definition_hash = canvas_node_definition_hash(graph["nodes"][1])
    execution.config_snapshot_jsonb = {
        "_canvas": {"auto_select_on_success": True}
    }
    execution.input_snapshot_jsonb = {
        "prompt": "Render a still",
        "bindings": [
            {
                "edge_id": "prompt-image",
                "source_node_id": "prompt-1",
                "target_handle": "prompt",
                "role": None,
                "order": 0,
                "binding_mode": "follow_active",
                "text": "Render a still",
            }
        ],
    }
    canvas = CanvasDocument(
        id=execution.canvas_id,
        user_id=execution.user_id,
        title="Auto select",
        graph_schema_version=1,
        graph_jsonb=graph,
        revision=99,
    )
    selection = CanvasNodeSelection(
        canvas_id=execution.canvas_id,
        node_id=execution.node_id,
        execution_id=None,
        output_index=0,
        revision=0,
        locked=False,
    )
    session = _FakeSession(
        [
            _Result(scalar=canvas),
            _Result(scalars=[selection]),
        ]
    )

    assert (
        await reconcile._auto_select_is_current(session, execution)  # noqa: SLF001
        is expected
    )
    assert len(session.statements) == 2


@pytest.mark.asyncio
async def test_terminal_receipt_rejects_conflicting_same_epoch() -> None:
    task = _task()
    projection = reconcile._TaskProjection(  # noqa: SLF001
        "failed",
        error_code="provider_error",
        task_id="gen-0",
        task_epoch=2,
    )
    session = _FakeSession(
        [
            _Result(rowcount=0),
            _Result(scalar="different-terminal-fingerprint"),
        ]
    )

    assert (
        await reconcile._record_terminal_receipt(session, task, projection)  # noqa: SLF001
        is False
    )


@pytest.mark.asyncio
async def test_reconcile_partial_failure_materializes_only_successes_in_ordinal_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    tasks = [_task(0, status="succeeded"), _task(1)]
    run = _run()
    session = _FakeSession(
        [
            _Result(scalar=execution),
            _Result(scalars=tasks),
            _Result(scalar=None),
        ]
    )
    monkeypatch.setattr(reconcile, "SessionLocal", lambda: session)

    async def project(_session: Any, task: CanvasExecutionTask):
        if task.ordinal == 0:
            return reconcile._TaskProjection(  # noqa: SLF001
                "succeeded",
                output={
                    "type": "image",
                    "ordinal": 0,
                    "image_id": "image-1",
                    "generation_id": "gen-0",
                },
                task_id="gen-0",
                task_epoch=1,
                finished_at=NOW,
            )
        return reconcile._TaskProjection(  # noqa: SLF001
            "failed",
            error_code="provider_error",
            error_message="failed",
            task_id="gen-1",
            task_epoch=1,
            finished_at=NOW,
        )

    async def receipt(*_args: Any) -> bool:
        return True

    materialized: list[dict[str, Any]] = []

    async def materialize(
        _session: Any,
        _execution: CanvasNodeExecution,
        outputs: list[dict[str, Any]],
    ) -> None:
        materialized.extend(outputs)

    async def select_output(*_args: Any) -> bool:
        return True

    async def auto_select_is_current(*_args: Any) -> bool:
        return True

    async def lock_run(*_args: Any) -> CanvasRun:
        return run

    async def aggregate_run(
        _session: Any,
        locked_run: CanvasRun,
        locked_execution: CanvasNodeExecution,
        *,
        now: datetime,
    ) -> None:
        del now
        locked_run.status = reconcile._single_run_status(locked_execution.status)  # noqa: SLF001

    events: list[dict[str, Any]] = []

    async def append_event(
        _session: Any,
        locked_run: CanvasRun,
        locked_execution: CanvasNodeExecution,
        **kwargs: Any,
    ) -> None:
        locked_run.last_event_seq += 1
        events.append(
            {
                "seq": locked_run.last_event_seq,
                "status": locked_execution.status,
                **kwargs,
            }
        )

    monkeypatch.setattr(reconcile, "_project_task", project)
    monkeypatch.setattr(reconcile, "_record_terminal_receipt", receipt)
    monkeypatch.setattr(reconcile, "_materialize_asset_refs", materialize)
    monkeypatch.setattr(reconcile, "_auto_select_is_current", auto_select_is_current)
    monkeypatch.setattr(reconcile, "_cas_active_output", select_output)
    monkeypatch.setattr(reconcile, "_lock_run", lock_run)
    monkeypatch.setattr(reconcile, "_aggregate_single_node_run", aggregate_run)
    monkeypatch.setattr(reconcile, "_append_run_event", append_event)

    assert await reconcile._reconcile_execution_id("exec-1") is True  # noqa: SLF001
    assert execution.status == "partial_failed"
    assert (
        execution.outputs_jsonb
        == materialized
        == [
            {
                "type": "image",
                "ordinal": 0,
                "image_id": "image-1",
                "generation_id": "gen-0",
            }
        ]
    )
    assert execution.error_code == "provider_error"
    assert run.status == "partial_failed"
    assert events[0]["seq"] == 1
    assert events[0]["payload"]["selection_updated"] is True


@pytest.mark.asyncio
async def test_terminal_execution_replay_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession([_Result(scalar=None)])
    monkeypatch.setattr(reconcile, "SessionLocal", lambda: session)

    assert await reconcile._reconcile_execution_id("exec-terminal") is False  # noqa: SLF001
    assert session.added == []


@pytest.mark.asyncio
async def test_run_event_sequence_is_monotonic_and_deduplicated() -> None:
    run = _run()
    run.last_event_seq = 9
    execution = _execution()
    create_session = _FakeSession([_Result(scalar=None)])

    await reconcile._append_run_event(  # noqa: SLF001
        create_session,
        run,
        execution,
        event_key="execution:exec-1:epoch:3:status:succeeded",
        event_type="canvas.execution.status_changed",
        payload={"status": "succeeded"},
    )

    assert run.last_event_seq == 10
    assert len(create_session.added) == 1
    assert create_session.added[0].seq == 10

    duplicate_session = _FakeSession([_Result(scalar="event-1")])
    await reconcile._append_run_event(  # noqa: SLF001
        duplicate_session,
        run,
        execution,
        event_key="execution:exec-1:epoch:3:status:succeeded",
        event_type="canvas.execution.status_changed",
        payload={"status": "succeeded"},
    )
    assert run.last_event_seq == 10
    assert duplicate_session.added == []


@pytest.mark.asyncio
async def test_periodic_reconcile_ignores_redis_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExplodingRedis:
        def __getattr__(self, _name: str) -> Any:
            raise AssertionError("Canvas reconciliation must not consult Redis")

    async def scan() -> list[str]:
        return ["exec-1", "exec-2"]

    async def reconcile_one(_execution_id: str) -> bool:
        return True

    monkeypatch.setattr(reconcile, "_scan_execution_ids", scan)
    monkeypatch.setattr(reconcile, "_reconcile_execution_id", reconcile_one)

    touched = await reconcile.reconcile_canvas_executions({"redis": ExplodingRedis()})

    assert touched == 2


def test_worker_registers_canvas_task_and_cron() -> None:
    assert reconcile.reconcile_canvas_execution in WorkerSettings.functions
    assert any(
        job.coroutine is reconcile.reconcile_canvas_executions
        for job in WorkerSettings.cron_jobs
    )
