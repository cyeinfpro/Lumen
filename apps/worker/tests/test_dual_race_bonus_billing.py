from __future__ import annotations

import base64
import inspect
import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as PILImage

from app.tasks.generation_parts import default_runtime as generation
from .task_parts_runtime_testing import synchronize_module_ports
from lumen_core.constants import EV_GEN_ATTACHED, EV_GEN_SUCCEEDED
from lumen_core.models import Generation, Image


@pytest.fixture(autouse=True)
def _sync_generation_ports(monkeypatch: pytest.MonkeyPatch):
    with synchronize_module_ports(
        monkeypatch,
        generation,
        generation.DEFAULT_GENERATION_RUNTIME.ports,
    ):
        yield


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
        for row in self.added:
            if isinstance(row, model) and getattr(row, "id", None) == key:
                return row
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
async def test_dual_race_bonus_is_billable_and_settled_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计 D-1：bonus 的 settle 必须排在行 commit 之后，且自成事务。

    与插入行共用事务时，commit 前的任何异常都会把钱包流水一起回滚，而上游
    那张图已经产出并计过费——平台替用户吸收上游成本，纯转嫁不允许。
    """
    session = _FakeSession()
    events: list[tuple[str, dict[str, Any]]] = []
    settle_calls: list[dict[str, Any]] = []

    async def fake_write_generation_files(
        files: list[tuple[str, bytes]],
    ) -> list[str]:
        return [key for key, _data in files]

    async def fake_deliver_generation_events(
        _redis: Any,
        deliveries: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        assert session.committed is True
        for _event_id, _kind, payload in deliveries:
            events.append((payload["event_name"], payload["data"]))

    async def fake_settle_generation(
        _session: Any,
        row: Generation,
        **kwargs: Any,
    ) -> None:
        assert session.committed is True
        session.operations.append("settle")
        settle_calls.append({"generation_id": row.id, **kwargs})

    async def fake_flush_balance_cache_refreshes(_session: Any) -> None:
        assert session.committed is True
        session.operations.append("flush")

    async def noop_record_candidate_image(**_kwargs: Any) -> None:
        session.operations.append("hook")

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
    monkeypatch.setattr(
        generation,
        "_deliver_generation_events",
        fake_deliver_generation_events,
    )
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
    assert session.operations == ["hook", "commit", "settle", "commit", "flush"]
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
async def test_dual_race_bonus_settle_failure_keeps_committed_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计 D-1：结算失败不得回滚已落盘的 bonus 图。

    settle 抛异常时图早已在上游产出并计过费。回滚整个事务只会让这笔上游成本
    彻底消失在日志里；保留 SUCCEEDED 但缺 settle 流水的 generation，对账才能
    捡起来重扣。这也是刻意不采纳审计里「补偿性 release」建议的地方——那是退款。
    """
    session = _FakeSession()
    events: list[tuple[str, dict[str, Any]]] = []
    settle_attempts: list[str] = []

    async def fake_write_generation_files(
        files: list[tuple[str, bytes]],
    ) -> list[str]:
        return [key for key, _data in files]

    async def fake_deliver_generation_events(
        _redis: Any,
        deliveries: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        for _event_id, _kind, payload in deliveries:
            events.append((payload["event_name"], payload["data"]))

    async def fail_settle_generation(_session: Any, row: Any, **_kwargs: Any) -> None:
        settle_attempts.append(row.id)
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
    monkeypatch.setattr(
        generation,
        "_deliver_generation_events",
        fake_deliver_generation_events,
    )
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

    assert ok is True
    assert session.committed is True
    bonus_row = next(row for row in session.added if isinstance(row, Generation))
    assert settle_attempts == [bonus_row.id]
    assert {event_name for event_name, _data in events} == {
        EV_GEN_ATTACHED,
        EV_GEN_SUCCEEDED,
    }


@pytest.mark.asyncio
async def test_bonus_image_echoing_reference_is_rejected_for_any_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计 D-9：sha 回声检测原本只在 action==EDIT 时生效。

    回声图（上游把输入原样退回）会被当成 bonus image 建行并**单独结算**，
    等于让用户为自己上传的图付钱。判据必须是「这次请求带没带参考图」，
    这样将来新增带参考图的 action 时自动继承这道防线。
    """
    session = _FakeSession()
    settle_calls: list[Any] = []
    b64 = _png_b64()
    raw = base64.b64decode(b64)

    async def fail_write_generation_files(_files: list[tuple[str, bytes]]) -> list[str]:
        raise AssertionError("echoed reference must not be written to storage")

    async def fake_settle_generation(*_args: Any, **kwargs: Any) -> None:
        settle_calls.append(kwargs)

    monkeypatch.setattr(generation, "SessionLocal", lambda: _SessionLocal(session))
    monkeypatch.setattr(
        generation, "_write_generation_files", fail_write_generation_files
    )
    monkeypatch.setattr(
        generation.worker_billing, "settle_generation", fake_settle_generation
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
        references=[(generation._sha256(raw), raw)],
        image_request_options={},
        b64_result=b64,
        revised_prompt=None,
        upstream_provider="responses",
        settle_billing=True,
    )

    assert ok is False
    assert settle_calls == []
    assert session.added == []
    assert session.committed is False


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
