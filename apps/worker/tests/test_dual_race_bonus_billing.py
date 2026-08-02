from __future__ import annotations

import base64
import inspect
import io
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as PILImage

from app.tasks.generation_parts import persistence, success
from app.tasks.generation_parts.default_runtime import build_generation_runtime
from app.tasks.generation_parts.image_artifact_contracts import sha256
from lumen_core.constants import EV_GEN_ATTACHED, EV_GEN_SUCCEEDED
from lumen_core.models import Generation, Image, Message


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

    bonus_source = inspect.getsource(success._finalize_batch_extra_images)
    assert "settle_billing=True" in bonus_source
