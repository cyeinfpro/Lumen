from __future__ import annotations

import base64
import inspect
import io
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as PILImage

from app.billing_parts.common import (
    BILLING_OBLIGATION_STATE_KEY,
    BILLING_OBLIGATION_TERMINAL_REASON_KEY,
    BILLING_OBLIGATION_UNSETTLEABLE,
)
from app.reconciliation.bonus_billing import (
    BONUS_ARTIFACT_RECONCILED,
    BONUS_BILLING_RECONCILER,
)
from app.reconciliation.contracts import ReconcileContext
from app.tasks.generation_parts import (
    batch_obligations,
    batch_results,
    bonus_artifacts,
    bonus_obligation,
    persistence,
    runner_dispatch_phase,
    success,
)
from app.tasks.generation_parts.default_runtime import build_generation_runtime
from app.tasks.generation_parts.image_artifact_contracts import sha256
from app.upstream_clients.image_job_models import (
    ImageJobCancelOutcome,
    ImageJobCostKnowledge,
    ImageJobExecutionHandle,
    ImageJobResultState,
)
from lumen_core.constants import (
    EV_GEN_ATTACHED,
    EV_GEN_SUCCEEDED,
    GenerationStage,
    GenerationStatus,
)
from lumen_core.models import Generation, Image, Message
from lumen_core.upstream_billing import GENERATION_TAKEOVER_CHECKPOINT_KEY


class _FakeSession:
    def __init__(
        self,
        *,
        deleted: bool = False,
        delete_after_commit: int | None = None,
    ) -> None:
        self.added: list[Any] = []
        self.message = SimpleNamespace(content={})
        self.user = SimpleNamespace(
            id="user-1",
            deleted_at=object() if deleted else None,
        )
        self.parent_generation = SimpleNamespace(
            id="parent-gen",
            user_id="user-1",
            execution_epoch=3,
            attempt=1,
        )
        self.committed = False
        self.commit_count = 0
        self.delete_after_commit = delete_after_commit
        self.operations: list[str] = []
        self.lock_order: list[str] = []
        self.context_depth = 0

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def execute(self, _statement: Any) -> Any:
        self.lock_order.append("user")
        return SimpleNamespace(scalar_one_or_none=lambda: self.user)

    async def get(self, model: Any, key: str, **_kwargs: Any) -> Any:
        if model is Message and key == "msg-1":
            return self.message
        if model is Generation and key == "parent-gen":
            self.lock_order.append("parent")
            return self.parent_generation
        if model is Generation:
            self.lock_order.append("bonus")
        for row in self.added:
            if isinstance(row, model) and getattr(row, "id", None) == key:
                return row
        return None

    async def commit(self) -> None:
        self.commit_count += 1
        self.operations.append("commit")
        self.committed = True
        if self.delete_after_commit == self.commit_count:
            self.user.deleted_at = object()


class _FakeStore:
    def __init__(self, session: _FakeSession) -> None:
        self.session_value = session

    @asynccontextmanager
    async def session(self):
        self.session_value.context_depth += 1
        try:
            yield self.session_value
        finally:
            self.session_value.context_depth -= 1


class _FakeArtifacts:
    def __init__(
        self,
        session: _FakeSession,
        *,
        fail_on_write: bool = False,
    ) -> None:
        self.session = session
        self.fail_on_write = fail_on_write
        self.write_calls = 0
        self.deleted_keys: list[list[str]] = []

    async def write_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[str]:
        assert self.session.context_depth == 0
        self.write_calls += 1
        if self.fail_on_write:
            raise AssertionError("echoed reference must not be written")
        return [key for key, _data in files]

    async def delete_files(self, keys: list[str]) -> None:
        self.deleted_keys.append(list(keys))
        return None

    @asynccontextmanager
    async def cleanup_on_error(self, _keys: list[str]):
        yield

    def public_url(self, key: str) -> str:
        return f"/public/{key}"


class _FakeBilling:
    def __init__(
        self,
        session: _FakeSession,
        *,
        fail_settle: bool = False,
    ) -> None:
        self.session = session
        self.fail_settle = fail_settle
        self.settle_calls: list[dict[str, Any]] = []

    async def settle(
        self,
        _session: Any,
        row: Generation,
        **kwargs: Any,
    ) -> None:
        assert self.session.committed is True
        self.settle_calls.append({"generation_id": row.id, **kwargs})
        if self.fail_settle:
            raise RuntimeError("billing failed")
        self.session.operations.append("settle")

    async def flush_after_commit(self, _session: Any) -> None:
        assert self.session.committed is True
        self.session.operations.append("flush")


class _FakeEvents:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def deliver_many(
        self,
        _redis: object,
        deliveries: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        assert self.session.committed is True
        assert self.session.context_depth == 0
        for _event_id, _kind, payload in deliveries:
            self.events.append((payload["event_name"], payload["data"]))


class _Result:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def scalars(self) -> _Result:
        return self

    def __iter__(self):
        return iter(self.values)

    def scalar_one_or_none(self) -> Any | None:
        return self.values[0] if self.values else None


class _Nested:
    async def __aenter__(self) -> _Nested:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _ReconcileSession:
    def __init__(self, results: list[list[Any]]) -> None:
        self.results = [_Result(values) for values in results]
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return self.results.pop(0)

    def begin_nested(self) -> _Nested:
        return _Nested()


class _ReconcileBilling:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def settle_generation(
        self,
        _session: Any,
        generation: Any,
        **kwargs: Any,
    ) -> None:
        self.calls.append({"generation_id": generation.id, **kwargs})


def _reconcile_context(
    session: _ReconcileSession,
    billing: _ReconcileBilling,
) -> ReconcileContext:
    return ReconcileContext(
        redis=None,
        session=session,
        now=datetime(2026, 8, 3, tzinfo=timezone.utc),
        billing=billing,
        logger=logging.getLogger("test-bonus-obligation"),
        lease_unknowns=None,
        stage_event=lambda *_args, **_kwargs: ("event", "sse", {}),
    )


def _png_b64() -> str:
    buf = io.BytesIO()
    PILImage.new("RGB", (8, 8), color=(12, 34, 56)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _deps(
    session: _FakeSession,
    *,
    fail_settle: bool = False,
    fail_on_write: bool = False,
):
    runtime = build_generation_runtime()
    billing = _FakeBilling(session, fail_settle=fail_settle)
    events = _FakeEvents(session)
    deps = replace(
        runtime.deps,
        store=_FakeStore(session),
        artifacts=_FakeArtifacts(session, fail_on_write=fail_on_write),
        billing=billing,
        events=events,
    )
    return deps, billing, events


def _bonus_context(deps: object, b64_result: str) -> persistence.BonusGenerationContext:
    return persistence.BonusGenerationContext(
        services=deps,
        redis=object(),
        user_id="user-1",
        channel="task:parent-gen",
        parent_task_id="parent-gen",
        execution_epoch=3,
        attempt=1,
        parent_idempotency_key="idem-parent",
        parent_upstream_request={},
        message_id="msg-1",
        action="generate",
        model="gpt-image-2",
        prompt="portrait",
        size_requested="1024x1024",
        aspect_ratio="1:1",
        input_image_ids=[],
        primary_input_image_id=None,
        references=[],
        image_request_options={},
        b64_result=b64_result,
        revised_prompt=None,
        upstream_provider="responses",
        upstream_actual_route=None,
        upstream_actual_source=None,
        upstream_actual_endpoint=None,
        billing_meta=None,
        idempotency_suffix=":b",
        extra_upstream_fields=None,
        record_model_library_candidate=True,
        settle_billing=True,
        log_label="dual_race bonus",
    )


@pytest.mark.asyncio
async def test_dual_race_bonus_is_billable_and_settled_after_commit() -> None:
    session = _FakeSession()
    deps, billing, events = _deps(session)

    async def noop_record_candidate_image(**_kwargs: Any) -> None:
        session.operations.append("hook")

    deps = replace(
        deps,
        workflows=SimpleNamespace(
            record_model_library_candidate_image=noop_record_candidate_image,
        ),
    )
    context = replace(
        _bonus_context(deps, _png_b64()),
        parent_upstream_request={
            "workflow_action": "model_library_generate",
            "workflow_model_library_age_segment": "adult",
            "workflow_model_library_gender": "female",
            "workflow_model_library_appearance_direction": "east_asian",
        },
    )

    ok = await persistence.handle_dual_race_bonus_image(context)

    assert ok is True
    assert session.operations == ["hook", "commit", "settle", "commit", "flush"]
    bonus_row = next(row for row in session.added if isinstance(row, Generation))
    image_row = next(row for row in session.added if isinstance(row, Image))
    assert billing.settle_calls == [
        {
            "generation_id": bonus_row.id,
            "width": 8,
            "height": 8,
            "image_count": 1,
        }
    ]
    assert bonus_row.upstream_request["billing_free"] is False
    assert image_row.metadata_jsonb["billing_label"] == "billable"
    event_by_name = dict(events.events)
    assert event_by_name[EV_GEN_ATTACHED]["billing_label"] == "billable"
    assert event_by_name[EV_GEN_SUCCEEDED]["images"][0]["billing_free"] is False


@pytest.mark.asyncio
async def test_dual_race_bonus_settle_failure_keeps_committed_image() -> None:
    session = _FakeSession()
    deps, billing, events = _deps(session, fail_settle=True)

    ok = await persistence.handle_dual_race_bonus_image(
        _bonus_context(deps, _png_b64())
    )

    # 结算失败不再被吞掉：向调用方返回 False 以便重试，事件不发布；
    # 行已落盘（SUCCEEDED 但无 settle 流水），对账仍可兜底重扣。
    assert ok is False
    assert session.committed is True
    bonus_row = next(row for row in session.added if isinstance(row, Generation))
    assert [call["generation_id"] for call in billing.settle_calls] == [bonus_row.id]
    assert events.events == []


@pytest.mark.asyncio
async def test_bonus_image_echoing_reference_is_rejected_for_any_action() -> None:
    session = _FakeSession()
    deps, billing, _events = _deps(session, fail_on_write=True)
    b64 = _png_b64()
    raw = base64.b64decode(b64)
    context = replace(
        _bonus_context(deps, b64),
        references=[(sha256(raw), raw)],
    )

    ok = await persistence.handle_dual_race_bonus_image(context)

    assert ok is False
    assert billing.settle_calls == []
    assert session.added == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_bonus_fence_blocks_artifacts_rows_attachments_events_and_billing_after_delete() -> (
    None
):
    session = _FakeSession(deleted=True)
    deps, billing, events = _deps(session)
    artifacts = deps.artifacts

    ok = await persistence.handle_dual_race_bonus_image(
        _bonus_context(deps, _png_b64())
    )

    assert ok is False
    assert artifacts.write_calls == 0
    assert artifacts.deleted_keys == []
    assert session.added == []
    assert session.message.content == {}
    assert billing.settle_calls == []
    assert events.events == []
    assert session.lock_order == ["user"]


@pytest.mark.asyncio
async def test_bonus_persistence_fence_cleans_artifacts_after_delete_before_rows() -> (
    None
):
    session = _FakeSession()
    deps, billing, events = _deps(session)
    artifacts = deps.artifacts
    real_write_files = artifacts.write_files

    async def write_then_delete(
        files: list[tuple[str, bytes]],
    ) -> list[str]:
        keys = await real_write_files(files)
        session.user.deleted_at = object()
        return keys

    artifacts.write_files = write_then_delete  # type: ignore[method-assign]

    ok = await persistence.handle_dual_race_bonus_image(
        _bonus_context(deps, _png_b64())
    )

    assert ok is False
    assert artifacts.write_calls == 1
    assert len(artifacts.deleted_keys) == 1
    assert len(artifacts.deleted_keys[0]) == 4
    assert session.added == []
    assert session.message.content == {}
    assert billing.settle_calls == []
    assert events.events == []
    assert session.lock_order == ["user", "parent", "user"]


@pytest.mark.asyncio
async def test_bonus_settlement_and_delivery_fences_block_after_delete_commit() -> None:
    session = _FakeSession(delete_after_commit=1)
    deps, billing, events = _deps(session)
    artifacts = deps.artifacts

    ok = await persistence.handle_dual_race_bonus_image(
        _bonus_context(deps, _png_b64())
    )

    assert ok is False
    assert artifacts.write_calls == 1
    assert session.user.deleted_at is not None
    assert len([row for row in session.added if isinstance(row, Generation)]) == 1
    assert len([row for row in session.added if isinstance(row, Image)]) == 1
    assert billing.settle_calls == []
    assert events.events == []
    assert session.lock_order == [
        "user",
        "parent",
        "user",
        "parent",
        "user",
    ]


def test_batch_extra_images_are_not_charged_on_parent_settle() -> None:
    source = inspect.getsource(success._persist_generation_success)
    assert "image_count=1" in source
    assert "image_count=artifact.actual_image_count" not in source

    bonus_source = inspect.getsource(batch_results.finalize_batch_extra_images)
    assert "settle_billing=True" in bonus_source


@pytest.mark.asyncio
async def test_parent_billing_admission_is_free_without_hold_snapshot_or_receipt() -> (
    None
):
    parent = SimpleNamespace(
        id="parent-free",
        user_id="user-1",
        execution_epoch=0,
        upstream_request={"billing_free": True},
    )
    state = SimpleNamespace(
        task_id=parent.id,
        user_id=parent.user_id,
        gen_upstream_request_snapshot=dict(parent.upstream_request),
        parent_upstream_request_for_bonus=None,
    )
    hold_calls: list[tuple[str, str, str]] = []

    async def no_hold(
        _session: Any,
        user_id: str,
        ref_type: str,
        ref_id: str,
    ) -> int:
        hold_calls.append((user_id, ref_type, ref_id))
        return 0

    admitted = await bonus_obligation.capture_parent_billing_admission(
        object(),
        parent,
        state,
        held_amount_for_ref=no_hold,
    )

    assert admitted is False
    assert hold_calls == [("user-1", "generation", "parent-free")]
    assert (
        parent.upstream_request[bonus_obligation.BILLING_ADMISSION_BILLABLE_KEY]
        is False
    )
    assert state.billing_admission_billable is False
    assert bonus_obligation.billing_obligation_metadata(
        state,
        policy="dual_race_loser_settled_separately",
        is_dual_race_bonus=True,
    ) == {
        "billing_free": True,
        "billing_label": "free",
        "billing_policy": "dual_race_loser_settled_separately",
        "is_dual_race_bonus": True,
        "billing_exempt_reason": "parent_billing_admission_free",
    }


@pytest.mark.asyncio
async def test_parent_billing_admission_keeps_existing_billable_receipt() -> None:
    parent = SimpleNamespace(
        id="parent-receipt",
        user_id="user-1",
        execution_epoch=4,
        upstream_request={
            "billing_free": True,
            "upstream_dispatch_started_at": "2026-08-04T00:00:00+00:00",
            "upstream_dispatch_attempt": 1,
            "upstream_dispatch_execution_epoch": 4,
        },
    )
    state = SimpleNamespace(
        task_id=parent.id,
        user_id=parent.user_id,
        gen_upstream_request_snapshot=dict(parent.upstream_request),
        parent_upstream_request_for_bonus=None,
    )

    async def hold_must_not_run(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("a billable receipt must settle admission without hold")

    admitted = await bonus_obligation.capture_parent_billing_admission(
        object(),
        parent,
        state,
        held_amount_for_ref=hold_must_not_run,
    )

    assert admitted is True
    assert state.billing_admission_billable is True
    assert (
        parent.upstream_request[bonus_obligation.BILLING_ADMISSION_SOURCE_KEY]
        == "billable_upstream_receipt"
    )


@pytest.mark.asyncio
async def test_parent_billing_admission_keeps_existing_hold() -> None:
    parent = SimpleNamespace(
        id="parent-hold",
        user_id="user-1",
        execution_epoch=0,
        upstream_request={"billing_free": True},
    )
    state = SimpleNamespace(
        task_id=parent.id,
        user_id=parent.user_id,
        gen_upstream_request_snapshot=dict(parent.upstream_request),
        parent_upstream_request_for_bonus=None,
    )

    async def existing_hold(*_args: Any, **_kwargs: Any) -> int:
        return 700

    admitted = await bonus_obligation.capture_parent_billing_admission(
        object(),
        parent,
        state,
        held_amount_for_ref=existing_hold,
    )

    assert admitted is True
    assert state.billing_admission_billable is True
    assert (
        parent.upstream_request[bonus_obligation.BILLING_ADMISSION_SOURCE_KEY]
        == "wallet_hold"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_ref_id",
    [None, "parent-retry"],
)
async def test_manual_retry_recomputes_admission_for_current_billing_ref(
    stored_ref_id: str | None,
) -> None:
    upstream_request: dict[str, Any] = {
        "billing_free": True,
        bonus_obligation.BILLING_ADMISSION_BILLABLE_KEY: False,
        bonus_obligation.BILLING_ADMISSION_SOURCE_KEY: (
            bonus_obligation.BILLING_ADMISSION_FREE_SOURCE
        ),
    }
    if stored_ref_id is not None:
        upstream_request[bonus_obligation.BILLING_ADMISSION_REF_ID_KEY] = stored_ref_id
    parent = SimpleNamespace(
        id="parent-retry",
        user_id="user-1",
        billing_retry_count=2,
        execution_epoch=4,
        upstream_request=upstream_request,
    )
    state = SimpleNamespace(
        task_id=parent.id,
        user_id=parent.user_id,
        gen_upstream_request_snapshot=dict(parent.upstream_request),
        parent_upstream_request_for_bonus=None,
    )
    hold_calls: list[tuple[str, str, str]] = []

    async def current_retry_hold(
        _session: Any,
        user_id: str,
        ref_type: str,
        ref_id: str,
    ) -> int:
        hold_calls.append((user_id, ref_type, ref_id))
        return 700

    first = await bonus_obligation.capture_parent_billing_admission(
        object(),
        parent,
        state,
        held_amount_for_ref=current_retry_hold,
    )
    second = await bonus_obligation.capture_parent_billing_admission(
        object(),
        parent,
        state,
        held_amount_for_ref=current_retry_hold,
    )

    assert first is True
    assert second is True
    assert hold_calls == [("user-1", "generation", "parent-retry:retry:2")]
    assert parent.upstream_request["billing_free"] is False
    assert parent.upstream_request["billing_label"] == "billable"
    assert (
        parent.upstream_request[bonus_obligation.BILLING_ADMISSION_BILLABLE_KEY] is True
    )
    assert (
        parent.upstream_request[bonus_obligation.BILLING_ADMISSION_REF_ID_KEY]
        == "parent-retry:retry:2"
    )


def test_batch_extra_obligations_inherit_free_parent_admission() -> None:
    session = _FakeSession()
    state = SimpleNamespace(
        task_id="parent-gen",
        user_id="user-1",
        message_id="msg-1",
        action="generate",
        gen_model="gpt-image-2",
        prompt="portrait",
        size_requested="1024x1024",
        resolved=SimpleNamespace(size="1024x1024"),
        aspect_ratio="1:1",
        input_image_ids=[],
        primary_input_image_id=None,
        gen_idempotency_key="idem-parent",
        generation=SimpleNamespace(execution_epoch=3),
        image_request_options={"quality": "high"},
        billing_admission_billable=False,
        billing_admission_source=(bonus_obligation.BILLING_ADMISSION_FREE_SOURCE),
        gen_upstream_request_snapshot={
            "billing_free": True,
            bonus_obligation.BILLING_ADMISSION_BILLABLE_KEY: False,
            bonus_obligation.BILLING_ADMISSION_SOURCE_KEY: (
                bonus_obligation.BILLING_ADMISSION_FREE_SOURCE
            ),
        },
        parent_upstream_request_for_bonus=None,
    )

    batch_obligations.add_batch_extra_billing_obligations(
        session,
        state,
        bonus_results=((2, "batch-free-2"), (3, "batch-free-3")),
        source_attempt=1,
        expected_count=3,
    )

    obligations = [row for row in session.added if isinstance(row, Generation)]
    assert [row.id for row in obligations] == ["batch-free-2", "batch-free-3"]
    assert all(
        row.upstream_request["bonus_billing_obligation"] is True
        and row.upstream_request["billing_free"] is True
        and row.upstream_request["billing_label"] == "free"
        and row.upstream_request["billing_exempt_reason"]
        == "parent_billing_admission_free"
        for row in obligations
    )


def test_dual_race_obligation_is_durable_before_parent_success_commit() -> None:
    source = inspect.getsource(success.finalize_generation_success)
    observe = source.index("_next_bonus_pair")
    ensure = source.index("ensure_dual_race_bonus_obligation")
    persist = source.index("_persist_generation_success")

    assert observe < ensure < persist


@pytest.mark.asyncio
async def test_precreated_bonus_artifact_identity_is_stable() -> None:
    session = _FakeSession()
    deps, _billing, _events = _deps(session)
    context = replace(
        _bonus_context(deps, _png_b64()),
        bonus_generation_id="bonus-obligation-1",
    )

    first = await bonus_artifacts.prepare_bonus_artifact(context)
    second = await bonus_artifacts.prepare_bonus_artifact(context)

    assert first is not None
    assert second is not None
    assert first.bonus_generation_id == second.bonus_generation_id
    assert first.image_id == second.image_id
    assert first.key_orig == second.key_orig


@pytest.mark.asyncio
async def test_batch_extra_takeover_adopts_source_attempt_obligation() -> None:
    session = _FakeSession()
    session.parent_generation.attempt = 2
    deps, billing, _events = _deps(session)
    bonus_id = "batch-extra-obligation"
    obligation = Generation(
        id=bonus_id,
        message_id="msg-1",
        user_id="user-1",
        action="generate",
        model="gpt-image-2",
        prompt="portrait",
        size_requested="1024x1024",
        aspect_ratio="1:1",
        input_image_ids=[],
        upstream_request={
            "billing_free": False,
            "billing_policy": "batch_extra_settled_separately",
            "bonus_billing_obligation": True,
            "bonus_billing_width": 1024,
            "bonus_billing_height": 1024,
            "bonus_artifact_state": "pending",
            "parent_generation_id": "parent-gen",
            "parent_execution_epoch": 3,
            "parent_attempt": 1,
            "batch_parent_generation_id": "parent-gen",
            "batch_index": 2,
            "batch_count": 3,
        },
        status=GenerationStatus.SUCCEEDED.value,
        progress_stage=GenerationStage.FINALIZING.value,
        attempt=0,
        idempotency_key="idem-parent:n2:e3:a1",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        upstream_pixels=1024 * 1024,
    )
    session.added.append(obligation)
    context = replace(
        _bonus_context(deps, _png_b64()),
        attempt=2,
        source_attempt=1,
        bonus_generation_id=bonus_id,
        require_precreated_generation=True,
        parent_upstream_request={
            GENERATION_TAKEOVER_CHECKPOINT_KEY: {"version": 2},
        },
        billing_meta={
            "billing_free": False,
            "billing_label": "billable",
            "billing_policy": "batch_extra_settled_separately",
        },
        idempotency_suffix=":n2:e3:a1",
        extra_upstream_fields={
            "batch_parent_generation_id": "parent-gen",
            "batch_index": 2,
            "batch_count": 3,
        },
        record_model_library_candidate=False,
    )

    assert await persistence.handle_dual_race_bonus_image(context)
    assert await persistence.handle_dual_race_bonus_image(context)

    images = [row for row in session.added if isinstance(row, Image)]
    assert len(images) == 1
    assert images[0].owner_generation_id == bonus_id
    assert obligation.upstream_request["parent_attempt"] == 1
    assert GENERATION_TAKEOVER_CHECKPOINT_KEY not in obligation.upstream_request
    assert [call["generation_id"] for call in billing.settle_calls] == [
        bonus_id,
        bonus_id,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_billing_request", "admission_billable", "expected_free"),
    [
        (
            {"billing_rate_multiplier_x10000": 10_000},
            True,
            False,
        ),
        (
            {
                "billing_free": True,
                bonus_obligation.BILLING_ADMISSION_BILLABLE_KEY: False,
                bonus_obligation.BILLING_ADMISSION_SOURCE_KEY: (
                    bonus_obligation.BILLING_ADMISSION_FREE_SOURCE
                ),
                "upstream_dispatch_started_at": "2026-08-04T00:00:00+00:00",
                "upstream_dispatch_attempt": 1,
                "upstream_dispatch_execution_epoch": 9,
            },
            False,
            True,
        ),
    ],
)
async def test_dual_race_bonus_progress_precreates_persistent_billing_obligation(
    monkeypatch: pytest.MonkeyPatch,
    parent_billing_request: dict[str, Any],
    admission_billable: bool,
    expected_free: bool,
) -> None:
    parent = SimpleNamespace(
        id="parent-gen",
        user_id="user-1",
        execution_epoch=9,
        attempt=3,
        upstream_request=dict(parent_billing_request),
    )

    class ObligationSession:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.commits = 0

        async def get(self, model: Any, key: str, **_kwargs: Any) -> Any:
            if model is Generation and key == "parent-gen":
                return parent
            return None

        async def execute(self, _statement: Any) -> _Result:
            bonuses = [row for row in self.added if isinstance(row, Generation)]
            return _Result(bonuses[-1:])

        def add(self, row: Any) -> None:
            self.added.append(row)

        async def commit(self) -> None:
            self.commits += 1

    session = ObligationSession()

    class Store:
        @asynccontextmanager
        async def session(self):
            yield session

    forwarded: list[dict[str, Any]] = []

    class Publisher:
        async def __call__(self, event: dict[str, Any]) -> None:
            forwarded.append(event)

        def pop_provider_used_event(self) -> dict[str, str]:
            return {}

    state = SimpleNamespace(
        services=SimpleNamespace(store=Store()),
        task_id="parent-gen",
        user_id="user-1",
        message_id="msg-1",
        attempt=1,
        generation=SimpleNamespace(execution_epoch=0),
        gen_idempotency_key="idem-parent",
        gen_upstream_request_snapshot=dict(parent_billing_request),
        billing_admission_billable=admission_billable,
        billing_admission_source=(
            "pricing_snapshot"
            if admission_billable
            else bonus_obligation.BILLING_ADMISSION_FREE_SOURCE
        ),
        image_request_options={"quality": "high"},
        action="generate",
        gen_model="gpt-image-2",
        prompt="portrait",
        size_requested="1024x1024",
        resolved=SimpleNamespace(size="1024x1024"),
        aspect_ratio="1:1",
        input_image_ids=[],
        primary_input_image_id=None,
        dual_race_bonus_obligation_id=None,
    )

    async def current_attempt_must_not_gate_obligation(
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        raise AssertionError("bonus cost obligation must survive parent lease loss")

    async def active_user(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        runner_dispatch_phase,
        "ensure_generation_attempt_current",
        current_attempt_must_not_gate_obligation,
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "lock_active_generation_user",
        active_user,
    )
    publisher = runner_dispatch_phase._EpochGuardedProgressPublisher(  # noqa: SLF001
        state,
        Publisher(),
    )

    cancel_execution = ImageJobExecutionHandle(
        job_id="job-responses",
        provider_id="provider-responses",
        endpoint="responses",
        base_url="https://image-job.example",
        idempotency_key="idem-responses",
        result_state=ImageJobResultState.UNCERTAIN,
        cost_knowledge=ImageJobCostKnowledge.UNKNOWN,
        sidecar_status="unknown",
        cancel_outcome=ImageJobCancelOutcome.UNCERTAIN,
    )
    event = {
        "type": "dual_race_bonus_ready",
        "lane": "responses",
        "race_name": "dual_race",
        "size": "1024x1024",
        "artifact_ready": False,
        "obligation_reason": "grace_cancel_cost",
        "execution": cancel_execution.to_dict(),
    }
    await publisher(event)
    await publisher(event)

    bonuses = [row for row in session.added if isinstance(row, Generation)]
    assert len(bonuses) == 1
    bonus = bonuses[0]
    assert forwarded == []
    assert session.commits == 1
    assert state.dual_race_bonus_obligation_id == bonus.id
    assert bonus.upstream_request["bonus_billing_obligation"] is True
    assert bonus.upstream_request["bonus_billing_width"] == 1024
    assert bonus.upstream_request["bonus_billing_height"] == 1024
    assert bonus.upstream_request["billing_free"] is expected_free
    assert bonus.upstream_request["billing_label"] == (
        "free" if expected_free else "billable"
    )
    assert bonus.upstream_request["dual_race_bonus_artifact_ready"] is False
    assert (
        bonus.upstream_request["dual_race_bonus_execution"]
        == cancel_execution.to_dict()
    )
    assert bonus.idempotency_key.endswith(":b:e0:a1")
    assert bonus.upstream_request["billing_policy"] == (
        "dual_race_loser_settled_separately"
    )


@pytest.mark.asyncio
async def test_manual_retry_bonus_obligation_is_billable_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = SimpleNamespace(
        id="parent-gen",
        user_id="user-1",
        billing_retry_count=2,
        execution_epoch=9,
        attempt=1,
        upstream_request={
            "billing_free": True,
            bonus_obligation.BILLING_ADMISSION_BILLABLE_KEY: False,
            bonus_obligation.BILLING_ADMISSION_SOURCE_KEY: (
                bonus_obligation.BILLING_ADMISSION_FREE_SOURCE
            ),
            bonus_obligation.BILLING_ADMISSION_REF_ID_KEY: "parent-gen",
        },
    )

    class ObligationSession:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.commits = 0

        async def get(self, model: Any, key: str, **_kwargs: Any) -> Any:
            if model is Generation and key == parent.id:
                return parent
            return None

        async def execute(self, _statement: Any) -> _Result:
            bonuses = [row for row in self.added if isinstance(row, Generation)]
            return _Result(bonuses[-1:])

        def add(self, row: Any) -> None:
            self.added.append(row)

        async def commit(self) -> None:
            self.commits += 1

    session = ObligationSession()

    class Store:
        @asynccontextmanager
        async def session(self):
            yield session

    state = SimpleNamespace(
        services=SimpleNamespace(store=Store()),
        task_id=parent.id,
        user_id=parent.user_id,
        message_id="msg-1",
        attempt=1,
        generation=parent,
        gen_idempotency_key="idem-parent",
        gen_upstream_request_snapshot=dict(parent.upstream_request),
        parent_upstream_request_for_bonus=None,
        image_request_options={"quality": "high"},
        action="generate",
        gen_model="gpt-image-2",
        prompt="portrait",
        size_requested="1024x1024",
        resolved=SimpleNamespace(size="1024x1024"),
        aspect_ratio="1:1",
        input_image_ids=[],
        primary_input_image_id=None,
        dual_race_bonus_obligation_id=None,
    )
    hold_calls: list[tuple[str, str, str]] = []

    async def held_amount_for_ref(
        _session: Any,
        user_id: str,
        ref_type: str,
        ref_id: str,
    ) -> int:
        hold_calls.append((user_id, ref_type, ref_id))
        return 700

    async def active_user(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        bonus_obligation.billing_core,
        "_held_amount_for_ref",
        held_amount_for_ref,
    )
    event = {
        "type": "dual_race_bonus_ready",
        "lane": "responses",
        "race_name": "dual_race",
        "size": "1024x1024",
        "artifact_ready": False,
        "obligation_reason": "grace_cancel_cost",
    }

    first = await bonus_obligation.record_dual_race_bonus_obligation(
        state,
        event,
        lock_active_user=active_user,
    )
    second = await bonus_obligation.record_dual_race_bonus_obligation(
        state,
        event,
        lock_active_user=active_user,
    )

    bonuses = [row for row in session.added if isinstance(row, Generation)]
    assert len(bonuses) == 1
    assert first == second == bonuses[0].id
    assert session.commits == 1
    assert hold_calls == [("user-1", "generation", "parent-gen:retry:2")]
    assert bonuses[0].upstream_request["billing_free"] is False
    assert bonuses[0].upstream_request["billing_label"] == "billable"
    assert bonuses[0].upstream_request["bonus_billing_obligation"] is True
    assert (
        bonuses[0].upstream_request[bonus_obligation.BILLING_ADMISSION_REF_ID_KEY]
        == "parent-gen:retry:2"
    )
    assert bonuses[0].idempotency_key.endswith(":b:e9:a1")


@pytest.mark.asyncio
async def test_bonus_obligation_adopts_ambiguous_commit() -> None:
    parent = SimpleNamespace(
        id="parent-gen",
        user_id="user-1",
        execution_epoch=0,
        attempt=1,
    )

    class AdoptingSession:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.adopted: Any | None = None
            self.commits = 0

        async def get(self, model: Any, key: str, **_kwargs: Any) -> Any:
            if model is Generation and key == "parent-gen":
                return parent
            return None

        async def execute(self, _statement: Any) -> _Result:
            return _Result([self.adopted] if self.adopted is not None else [])

        def add(self, row: Any) -> None:
            self.added.append(row)

        async def commit(self) -> None:
            self.commits += 1
            inserted = next(row for row in self.added if isinstance(row, Generation))
            self.adopted = SimpleNamespace(
                id="bonus-adopted",
                user_id=inserted.user_id,
                idempotency_key=inserted.idempotency_key,
                upstream_request=dict(inserted.upstream_request),
            )
            raise RuntimeError("commit acknowledgement lost")

        async def rollback(self) -> None:
            return None

    session = AdoptingSession()

    class Store:
        @asynccontextmanager
        async def session(self):
            yield session

    state = SimpleNamespace(
        services=SimpleNamespace(store=Store()),
        task_id="parent-gen",
        user_id="user-1",
        message_id="msg-1",
        attempt=1,
        generation=SimpleNamespace(execution_epoch=0),
        gen_idempotency_key="idem-parent",
        gen_upstream_request_snapshot={"billing_rate_multiplier_x10000": 10_000},
        image_request_options={"quality": "high"},
        action="generate",
        gen_model="gpt-image-2",
        prompt="portrait",
        size_requested="1024x1024",
        resolved=SimpleNamespace(size="1024x1024"),
        aspect_ratio="1:1",
        input_image_ids=[],
        primary_input_image_id=None,
        dual_race_bonus_obligation_id=None,
    )

    async def active_user(*_args: Any, **_kwargs: Any) -> bool:
        return True

    obligation_id = await bonus_obligation.record_dual_race_bonus_obligation(
        state,
        {
            "type": "dual_race_bonus_ready",
            "lane": "responses",
            "race_name": "dual_race",
            "size": "1024x1024",
        },
        lock_active_user=active_user,
    )

    assert obligation_id == "bonus-adopted"
    assert state.dual_race_bonus_obligation_id == "bonus-adopted"
    assert session.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "outcome", "result_state", "cost_knowledge"),
    [
        (
            "succeeded",
            ImageJobCancelOutcome.ALREADY_TERMINAL,
            ImageJobResultState.SUCCEEDED,
            ImageJobCostKnowledge.INCURRED,
        ),
        (
            "unknown",
            ImageJobCancelOutcome.UNCERTAIN,
            ImageJobResultState.UNCERTAIN,
            ImageJobCostKnowledge.UNKNOWN,
        ),
    ],
)
async def test_success_rebuilds_missing_obligation_from_cancel_execution(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    outcome: ImageJobCancelOutcome,
    result_state: ImageJobResultState,
    cost_knowledge: ImageJobCostKnowledge,
) -> None:
    winner = ImageJobExecutionHandle(
        job_id="job-generations",
        provider_id="provider-generations",
        endpoint="generations",
        base_url="https://image-job.example",
        idempotency_key="idem-generations",
        result_state=ImageJobResultState.SUCCEEDED,
        cost_knowledge=ImageJobCostKnowledge.INCURRED,
        sidecar_status="succeeded",
        result_artifact={"url": "https://image-job.example/winner.png"},
    )
    loser = ImageJobExecutionHandle(
        job_id="job-responses",
        provider_id="provider-responses",
        endpoint="responses",
        base_url="https://image-job.example",
        idempotency_key="idem-responses",
        result_state=result_state,
        cost_knowledge=cost_knowledge,
        sidecar_status=status,
        cancel_outcome=outcome,
    )
    state = SimpleNamespace(
        is_dual_race=True,
        dual_race_bonus_obligation_id=None,
        actual_upstream_endpoint="image-jobs:generations",
        resolved=SimpleNamespace(size="1024x1024"),
        gen_upstream_request_snapshot={
            "sidecar_execution": winner.to_dict(),
            "sidecar_executions": {
                "generations": winner.to_dict(),
                "responses": loser.to_dict(),
            },
        },
    )
    events: list[dict[str, Any]] = []

    async def record(
        received_state: Any,
        event: dict[str, Any],
        **_kwargs: Any,
    ) -> str:
        events.append(event)
        received_state.dual_race_bonus_obligation_id = "bonus-obligation"
        return "bonus-obligation"

    monkeypatch.setattr(
        bonus_obligation,
        "record_dual_race_bonus_obligation",
        record,
    )

    first = await bonus_obligation.ensure_dual_race_bonus_obligation(state)
    second = await bonus_obligation.ensure_dual_race_bonus_obligation(state)

    assert first == "bonus-obligation"
    assert second == "bonus-obligation"
    assert len(events) == 1
    assert events[0]["artifact_ready"] is False
    assert events[0]["obligation_reason"] == "recovered_loser_cancel_cost"
    evidence = ImageJobExecutionHandle.from_mapping(events[0]["execution"])
    assert evidence == loser


@pytest.mark.asyncio
async def test_bonus_reconciler_requires_real_settlement_ledger() -> None:
    generation = SimpleNamespace(
        id="bonus-ledger-missing",
        user_id="user-1",
        status="succeeded",
        upstream_request={
            "billing_policy": "dual_race_loser_settled_separately",
            "billing_free": False,
        },
    )
    billing = _ReconcileBilling()
    session = _ReconcileSession(
        [
            [generation],
            [SimpleNamespace(width=1024, height=1024)],
            [],
        ]
    )

    result = await BONUS_BILLING_RECONCILER.reconcile(
        _reconcile_context(session, billing)
    )

    assert billing.calls == [
        {
            "generation_id": generation.id,
            "width": 1024,
            "height": 1024,
            "image_count": 1,
        }
    ]
    assert result.touched == 0


@pytest.mark.asyncio
async def test_bonus_reconciler_settles_precreated_obligation_without_image() -> None:
    generation = SimpleNamespace(
        id="bonus-precreated",
        user_id="user-1",
        status="succeeded",
        upstream_request={
            "billing_policy": "dual_race_loser_settled_separately",
            "billing_free": False,
            "bonus_billing_obligation": True,
            "bonus_billing_width": 1536,
            "bonus_billing_height": 1024,
        },
    )
    billing = _ReconcileBilling()
    session = _ReconcileSession(
        [
            [generation],
            [],
            ["wallet-tx-1"],
        ]
    )

    result = await BONUS_BILLING_RECONCILER.reconcile(
        _reconcile_context(session, billing)
    )

    assert result.touched == 1
    assert (
        generation.upstream_request["bonus_artifact_state"] == BONUS_ARTIFACT_RECONCILED
    )
    assert "bonus_artifact_reconciled_at" in generation.upstream_request
    assert billing.calls == [
        {
            "generation_id": generation.id,
            "width": 1536,
            "height": 1024,
            "image_count": 1,
        }
    ]


@pytest.mark.asyncio
async def test_bonus_reconciler_converges_already_settled_pending_obligation() -> None:
    generation = SimpleNamespace(
        id="bonus-already-settled-pending",
        user_id="user-1",
        status="succeeded",
        upstream_request={
            "billing_policy": "dual_race_loser_settled_separately",
            "billing_free": False,
            "bonus_billing_obligation": True,
            "bonus_billing_width": 1024,
            "bonus_billing_height": 1024,
            "bonus_artifact_state": "pending",
        },
    )
    billing = _ReconcileBilling()
    session = _ReconcileSession(
        [
            [generation],
            [],
            ["wallet-tx-existing"],
        ]
    )

    result = await BONUS_BILLING_RECONCILER.reconcile(
        _reconcile_context(session, billing)
    )

    assert result.touched == 1
    assert "bonus_artifact_state" in session.statements[0].compile().params.values()
    assert (
        generation.upstream_request["bonus_artifact_state"] == BONUS_ARTIFACT_RECONCILED
    )


@pytest.mark.asyncio
async def test_bonus_reconciler_excludes_unsettleable_obligations() -> None:
    billing = _ReconcileBilling()
    session = _ReconcileSession([[]])

    result = await BONUS_BILLING_RECONCILER.reconcile(
        _reconcile_context(session, billing)
    )

    query_params = list(session.statements[0].compile().params.values())
    assert result.touched == 0
    assert billing.calls == []
    assert BILLING_OBLIGATION_STATE_KEY in query_params
    assert BILLING_OBLIGATION_UNSETTLEABLE in query_params


@pytest.mark.asyncio
async def test_bonus_reconciler_converges_unsettleable_without_ledger() -> None:
    generation = SimpleNamespace(
        id="bonus-unsettleable",
        user_id="user-1",
        status="succeeded",
        upstream_request={
            "billing_policy": "dual_race_loser_settled_separately",
            "billing_free": False,
            "bonus_billing_obligation": True,
            "bonus_billing_width": 1024,
            "bonus_billing_height": 1024,
            "bonus_artifact_state": "pending",
        },
    )

    class _UnsettleableBilling(_ReconcileBilling):
        async def settle_generation(
            self,
            _session: Any,
            row: Any,
            **kwargs: Any,
        ) -> None:
            await super().settle_generation(_session, row, **kwargs)
            updated = dict(row.upstream_request)
            updated[BILLING_OBLIGATION_STATE_KEY] = BILLING_OBLIGATION_UNSETTLEABLE
            updated[BILLING_OBLIGATION_TERMINAL_REASON_KEY] = (
                "pricing_missing_without_hold"
            )
            row.upstream_request = updated

    billing = _UnsettleableBilling()
    session = _ReconcileSession([[generation], [], []])

    result = await BONUS_BILLING_RECONCILER.reconcile(
        _reconcile_context(session, billing)
    )

    assert result.touched == 1
    assert len(billing.calls) == 1
    assert (
        generation.upstream_request["bonus_artifact_state"] == BONUS_ARTIFACT_RECONCILED
    )
    assert (
        generation.upstream_request[BILLING_OBLIGATION_STATE_KEY]
        == BILLING_OBLIGATION_UNSETTLEABLE
    )
