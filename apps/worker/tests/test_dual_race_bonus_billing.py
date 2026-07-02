from __future__ import annotations

import base64
import inspect
import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as PILImage

from app.tasks import generation
from lumen_core.constants import EV_GEN_ATTACHED, EV_GEN_SUCCEEDED
from lumen_core.models import Generation, Image


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.message = SimpleNamespace(content={})
        self.committed = False
        self.operations: list[str] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def get(self, model: Any, key: str) -> Any:
        if model is generation.Message and key == "msg-1":
            return self.message
        return None

    async def commit(self) -> None:
        self.operations.append("commit")
        self.committed = True


class _SessionLocal:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _png_b64() -> str:
    buf = io.BytesIO()
    PILImage.new("RGB", (8, 8), color=(12, 34, 56)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.mark.asyncio
async def test_dual_race_bonus_is_billable_and_settled_before_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    events: list[tuple[str, dict[str, Any]]] = []
    settle_calls: list[dict[str, Any]] = []

    async def fake_write_generation_files(
        files: list[tuple[str, bytes]],
    ) -> list[str]:
        return [key for key, _data in files]

    async def fake_publish_event(
        redis: Any,
        user_id: str,
        channel: str,
        event_name: str,
        data: dict[str, Any],
    ) -> None:
        events.append((event_name, data))

    async def fake_settle_generation(
        _session: Any,
        row: Generation,
        **kwargs: Any,
    ) -> None:
        assert session.committed is False
        session.operations.append("settle")
        settle_calls.append({"generation_id": row.id, **kwargs})

    async def fake_flush_balance_cache_refreshes(_session: Any) -> None:
        assert session.committed is True
        session.operations.append("flush")

    async def noop_record_candidate_image(**_kwargs: Any) -> None:
        return None

    async def noop_delete_storage_keys(_keys: list[str]) -> None:
        return None

    monkeypatch.setattr(
        generation, "SessionLocal", lambda: _SessionLocal(session)
    )
    monkeypatch.setattr(
        generation, "_write_generation_files", fake_write_generation_files
    )
    monkeypatch.setattr(generation, "_compute_blurhash", lambda _img: "blur")
    monkeypatch.setattr(
        generation,
        "_maybe_record_model_library_candidate_image",
        noop_record_candidate_image,
    )
    monkeypatch.setattr(generation, "publish_event", fake_publish_event)
    monkeypatch.setattr(generation.storage, "public_url", lambda key: f"/public/{key}")
    monkeypatch.setattr(
        generation.worker_billing, "settle_generation", fake_settle_generation
    )
    monkeypatch.setattr(
        generation.worker_billing,
        "flush_balance_cache_refreshes",
        fake_flush_balance_cache_refreshes,
    )

    ok = await generation._handle_dual_race_bonus_image(
        redis=object(),
        user_id="user-1",
        channel="task:parent-gen",
        parent_task_id="parent-gen",
        parent_idempotency_key="idem-parent",
        parent_upstream_request={
            "workflow_action": "model_library_generate",
            "workflow_model_library_age_segment": "adult",
            "workflow_model_library_gender": "female",
            "workflow_model_library_appearance_direction": "east_asian",
        },
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
        b64_result=_png_b64(),
        revised_prompt=None,
        upstream_provider="responses",
        settle_billing=True,
    )

    assert ok is True
    assert session.committed is True
    assert session.operations == ["settle", "commit", "flush"]
    bonus_row = next(row for row in session.added if isinstance(row, Generation))
    image_row = next(row for row in session.added if isinstance(row, Image))
    assert settle_calls == [
        {
            "generation_id": bonus_row.id,
            "width": 8,
            "height": 8,
            "image_count": 1,
        }
    ]
    assert bonus_row.upstream_request["is_dual_race_bonus"] is True
    assert bonus_row.upstream_request["billing_free"] is False
    assert bonus_row.upstream_request["billing_label"] == "billable"
    assert bonus_row.upstream_request["billing_policy"] == (
        "dual_race_loser_settled_separately"
    )
    assert "billing_exempt_reason" not in bonus_row.upstream_request
    assert image_row.metadata_jsonb["is_dual_race_bonus"] is True
    assert image_row.metadata_jsonb["billing_free"] is False
    assert image_row.metadata_jsonb["billing_label"] == "billable"

    message_image = session.message.content["images"][0]
    assert message_image["is_dual_race_bonus"] is True
    assert message_image["billing_free"] is False
    assert message_image["billing_label"] == "billable"

    event_by_name = {event_name: data for event_name, data in events}
    assert event_by_name[EV_GEN_ATTACHED]["billing_label"] == "billable"
    succeeded_image = event_by_name[EV_GEN_SUCCEEDED]["images"][0]
    assert succeeded_image["is_dual_race_bonus"] is True
    assert succeeded_image["billing_free"] is False
    assert succeeded_image["billing_label"] == "billable"


@pytest.mark.asyncio
async def test_dual_race_bonus_settle_failure_does_not_commit_or_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    events: list[tuple[str, dict[str, Any]]] = []

    async def fake_write_generation_files(
        files: list[tuple[str, bytes]],
    ) -> list[str]:
        return [key for key, _data in files]

    async def fake_publish_event(
        redis: Any,
        user_id: str,
        channel: str,
        event_name: str,
        data: dict[str, Any],
    ) -> None:
        events.append((event_name, data))

    async def fail_settle_generation(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("billing failed")

    async def noop_record_candidate_image(**_kwargs: Any) -> None:
        return None

    async def noop_delete_storage_keys(_keys: list[str]) -> None:
        return None

    monkeypatch.setattr(generation, "SessionLocal", lambda: _SessionLocal(session))
    monkeypatch.setattr(
        generation, "_write_generation_files", fake_write_generation_files
    )
    monkeypatch.setattr(generation, "_compute_blurhash", lambda _img: "blur")
    monkeypatch.setattr(
        generation,
        "_maybe_record_model_library_candidate_image",
        noop_record_candidate_image,
    )
    monkeypatch.setattr(generation, "publish_event", fake_publish_event)
    monkeypatch.setattr(generation.storage, "public_url", lambda key: f"/public/{key}")
    monkeypatch.setattr(generation, "_delete_storage_keys", noop_delete_storage_keys)
    monkeypatch.setattr(
        generation.worker_billing, "settle_generation", fail_settle_generation
    )

    ok = await generation._handle_dual_race_bonus_image(
        redis=object(),
        user_id="user-1",
        channel="task:parent-gen",
        parent_task_id="parent-gen",
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
        b64_result=_png_b64(),
        revised_prompt=None,
        upstream_provider="responses",
        settle_billing=True,
    )

    assert ok is False
    assert session.committed is False
    assert events == []


def test_batch_extra_images_are_not_charged_on_parent_settle() -> None:
    source = inspect.getsource(generation.run_generation)
    main_success_block = source.index('"image_count_actual"')
    parent_settle = source.index(
        "await worker_billing.settle_generation(",
        main_success_block,
    )
    parent_commit = source.index("await session.commit()", parent_settle)
    parent_settle_block = source[parent_settle:parent_commit]

    assert "image_count=1" in parent_settle_block
    assert "image_count=actual_image_count" not in parent_settle_block
    assert source.count("settle_billing=True") >= 2
