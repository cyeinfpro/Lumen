from __future__ import annotations

import asyncio
from dataclasses import replace
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from aiogram.types import Message  # noqa: E402

from app.generation_state import (  # noqa: E402
    DurableGenerationSubmission,
    SubmissionJournalStatus,
    generation_request_fingerprint,
    generation_submission_idempotency_key,
    generation_submission_identity,
    generation_submission_operation_id,
    new_generation_flow_epoch,
)
from app.handlers import generation, menu  # noqa: E402
from app.tracker import Tracker  # noqa: E402


class LegacyGenerationApi:
    async def create_generation(
        self,
        _chat_id: int,
        _payload: dict[str, object],
        *,
        tg_user_id: int,
    ) -> dict[str, object]:
        assert tg_user_id == 100
        return {"generation_ids": ["gen-legacy-api"]}


class RecordingTracker:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.operations: dict[str, DurableGenerationSubmission] = {}
        self.update_index: dict[tuple[int, int, str], str] = {}
        self.pending_index: dict[tuple[int, int, str], str] = {}
        self.locked: set[str] = set()
        self.released: list[str] = []

    async def init_batch(self, batch_id: str, count: int) -> None:
        self.calls.append(("init_batch", batch_id, count))

    async def add(self, gen_id: str, track: object) -> None:
        self.calls.append(("add", gen_id, track))

    async def stage_generation_submission(
        self,
        *,
        chat_id: int,
        tg_user_id: int,
        update_token: str,
        payload: dict[str, object],
    ) -> DurableGenerationSubmission:
        fingerprint = generation_request_fingerprint(payload)
        update_ref = (chat_id, tg_user_id, update_token)
        operation_id = self.update_index.get(update_ref)
        if operation_id is None:
            operation_id = self.pending_index.get((chat_id, tg_user_id, fingerprint))
        if operation_id is not None:
            self.update_index[update_ref] = operation_id
            return self.operations[operation_id]

        operation_id = generation_submission_operation_id(
            chat_id,
            tg_user_id,
            update_token,
        )
        idempotency_key = generation_submission_idempotency_key(
            chat_id,
            tg_user_id,
            update_token,
        )
        canonical_payload = {
            **{
                key: value
                for key, value in payload.items()
                if key != "idempotency_key"
            },
            "idempotency_key": idempotency_key,
        }
        submission = DurableGenerationSubmission(
            operation_id=operation_id,
            identity_hash=generation_submission_identity(chat_id, tg_user_id),
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            payload=canonical_payload,
            status=SubmissionJournalStatus.PREPARED,
            update_token=update_token,
        )
        self.operations[operation_id] = submission
        self.update_index[update_ref] = operation_id
        self.pending_index[(chat_id, tg_user_id, fingerprint)] = operation_id
        return submission

    async def finish_generation_submission(
        self,
        submission: DurableGenerationSubmission,
        status: SubmissionJournalStatus,
    ) -> None:
        current = replace(submission, status=status)
        self.operations[submission.operation_id] = current
        if status in {
            SubmissionJournalStatus.ACCEPTED,
            SubmissionJournalStatus.REJECTED,
        }:
            for key, operation_id in list(self.pending_index.items()):
                if operation_id == submission.operation_id:
                    self.pending_index.pop(key)

    async def acquire_submit_once(self, key: str) -> bool:
        if key in self.locked:
            return False
        self.locked.add(key)
        return True

    async def release_submit_once(self, key: str) -> None:
        self.locked.discard(key)
        self.released.append(key)


@pytest.mark.asyncio
async def test_malformed_success_without_user_id_is_kept_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[str] = []
    recording_tracker = RecordingTracker()
    monkeypatch.setattr(generation, "tracker", recording_tracker)

    async def answer(text: str) -> SimpleNamespace:
        answers.append(text)
        return SimpleNamespace(message_id=123)

    disposition = await generation._submit_generation(
        100,
        100,
        {
            "idempotency_key": "tg:test",
            "prompt": "prompt",
            "aspect_ratio": "1:1",
            "render_quality": "high",
            "count": 1,
            "resolution": "2k",
            "output_format": "jpeg",
        },
        LegacyGenerationApi(),  # type: ignore[arg-type]
        answer,
    )

    assert disposition is generation.SubmissionDisposition.AMBIGUOUS
    assert recording_tracker.calls == []
    assert len(answers) == 1
    assert "结果暂时无法确认" in answers[0]


# ---------- enhance-choice 双击竞态（审计 新-9） ----------


class SubmitOnceRedis:
    """Minimal SET NX/EX semantics for the submit-once guard."""

    def __init__(self) -> None:
        self.keys: dict[str, bytes] = {}
        self.set_calls: list[tuple[str, bool, int]] = []

    async def set(
        self,
        key: str,
        value: bytes,
        *,
        nx: bool = False,
        ex: int = 0,
    ) -> bool | None:
        self.set_calls.append((key, nx, ex))
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.keys.pop(key, None) is not None else 0


class SubmitCountingApi:
    """Counts create_generation calls and yields control to expose races."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def create_generation(
        self,
        _chat_id: int,
        payload: dict[str, object],
        *,
        tg_user_id: int,
    ) -> dict[str, object]:
        assert tg_user_id == 42
        self.payloads.append(dict(payload))
        # 模拟 HTTP 往返：让出 event loop，给第二个回调追上来的机会
        await asyncio.sleep(0)
        return {"generation_ids": ["gen-1"], "user_id": "user-1"}


class FakeState:
    """Stand-in for aiogram FSMContext with clear()/get_data()."""

    def __init__(self, data: dict[str, object]) -> None:
        self._data = dict(data)
        self.cleared = 0
        self.current_state: object | None = generation.GenFlow.awaiting_prompt
        self.state_history: list[object | None] = []

    async def get_data(self) -> dict[str, object]:
        return dict(self._data)

    async def clear(self) -> None:
        self.cleared += 1
        self._data = {}
        self.current_state = None

    async def set_state(self, state: object) -> None:
        self.current_state = state
        self.state_history.append(state)

    async def update_data(self, **kwargs: object) -> None:
        self._data.update(kwargs)


def _make_callback(
    chat_id: int = 42,
    *,
    callback_id: str = "callback-1",
    message_id: int = 77,
) -> tuple[object, MagicMock]:
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=chat_id)
    msg.message_id = message_id
    msg.edit_reply_markup = AsyncMock()

    async def answer(text: str) -> SimpleNamespace:
        # status message；同样让出 loop
        await asyncio.sleep(0)
        return SimpleNamespace(message_id=999)

    msg.answer = AsyncMock(side_effect=answer)

    cb = MagicMock()
    cb.id = callback_id
    cb.data = "enh:use"
    cb.message = msg
    cb.from_user = SimpleNamespace(id=chat_id)
    cb.answer = AsyncMock()
    return cb, msg


def _make_prompt_message(
    text: str,
    *,
    message_id: int,
    chat_id: int = 42,
    tg_user_id: int | None = None,
) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.caption = None
    msg.chat = SimpleNamespace(id=chat_id)
    msg.from_user = SimpleNamespace(id=tg_user_id or chat_id)
    msg.message_id = message_id
    msg.bot = None

    async def answer(_text: str, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(message_id=message_id + 1000, delete=AsyncMock())

    msg.answer = AsyncMock(side_effect=answer)
    return msg


class AmbiguousThenSuccessApi:
    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.payloads: list[dict[str, object]] = []

    async def create_generation(
        self,
        _chat_id: int,
        payload: dict[str, object],
        *,
        tg_user_id: int,
    ) -> dict[str, object]:
        assert tg_user_id == 42
        self.payloads.append(dict(payload))
        if len(self.payloads) == 1:
            if self.failure == "5xx":
                raise generation.ApiError(
                    "server_error",
                    "temporary failure",
                    503,
                    outcome_unknown=True,
                )
            if self.failure == "connection":
                raise ConnectionError("connection lost after write")
            return {"ok": True}
        return {"generation_ids": ["gen-1"], "user_id": "user-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["5xx", "connection", "malformed"])
async def test_ambiguous_prompt_submission_reuses_exact_payload_and_state(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    monkeypatch.setattr(generation, "tracker", RecordingTracker())
    api = AmbiguousThenSuccessApi(failure)
    state = FakeState({"params": dict(generation.DEFAULT_PARAMS)})
    runtime = generation.GenerationRuntime()

    await generation.on_prompt(
        _make_prompt_message("a cat", message_id=10),
        state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        runtime,
    )

    assert state.cleared == 0
    pending = state._data["pending_generation"]
    assert isinstance(pending, dict)
    original_key = pending["idempotency_key"]

    await generation.on_prompt(
        _make_prompt_message("a cat", message_id=11),
        state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        runtime,
    )

    assert len(api.payloads) == 2
    assert api.payloads[0] == api.payloads[1]
    assert api.payloads[1]["idempotency_key"] == original_key
    assert state.cleared == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_mode", ["new", "cancel", "fsm_expiry"])
async def test_ambiguous_submission_survives_disposable_fsm_state(
    monkeypatch: pytest.MonkeyPatch,
    reset_mode: str,
) -> None:
    durable_tracker = RecordingTracker()
    monkeypatch.setattr(generation, "tracker", durable_tracker)
    api = AmbiguousThenSuccessApi("connection")
    params = dict(generation.DEFAULT_PARAMS)
    state = FakeState({"params": params})
    runtime = generation.GenerationRuntime()

    await generation.on_prompt(
        _make_prompt_message("a cat", message_id=20),
        state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        runtime,
    )
    first_key = api.payloads[0]["idempotency_key"]

    if reset_mode == "new":
        await menu.cmd_new(
            _make_prompt_message("/new", message_id=21),
            state,  # type: ignore[arg-type]
            runtime,
        )
        await state.set_state(generation.GenFlow.awaiting_prompt)
    elif reset_mode == "cancel":
        await generation.on_prompt(
            _make_prompt_message("/cancel", message_id=21),
            state,  # type: ignore[arg-type]
            api,  # type: ignore[arg-type]
            runtime,
        )
        await state.set_state(generation.GenFlow.awaiting_prompt)
        await state.update_data(
            params=params,
            generation_flow_epoch=new_generation_flow_epoch(),
        )
    else:
        state = FakeState({"params": params})

    await generation.on_prompt(
        _make_prompt_message("a cat", message_id=22),
        state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        runtime,
    )

    assert len(api.payloads) == 2
    assert api.payloads[1]["idempotency_key"] == first_key
    assert state.cleared >= 1


@pytest.mark.asyncio
async def test_telegram_update_redelivery_regenerates_same_submission_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable_tracker = RecordingTracker()
    monkeypatch.setattr(generation, "tracker", durable_tracker)
    api = AmbiguousThenSuccessApi("5xx")
    params = dict(generation.DEFAULT_PARAMS)
    runtime = generation.GenerationRuntime()

    first_state = FakeState({"params": params})
    await generation.on_prompt(
        _make_prompt_message("a cat", message_id=30),
        first_state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        runtime,
    )
    first_key = api.payloads[0]["idempotency_key"]

    redelivered_state = FakeState({"params": params})
    await generation.on_prompt(
        _make_prompt_message("a cat", message_id=30),
        redelivered_state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        runtime,
    )

    assert len(api.payloads) == 2
    assert api.payloads[1]["idempotency_key"] == first_key


@pytest.mark.asyncio
async def test_generation_handler_passes_group_actor_identity_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities: list[tuple[int, int]] = []

    class IdentityApi:
        async def create_generation(
            self,
            chat_id: int,
            _payload: dict[str, object],
            *,
            tg_user_id: int,
        ) -> dict[str, object]:
            identities.append((chat_id, tg_user_id))
            return {"generation_ids": ["gen-1"], "user_id": "user-1"}

    monkeypatch.setattr(generation, "tracker", RecordingTracker())
    state = FakeState({"params": dict(generation.DEFAULT_PARAMS)})

    await generation.on_prompt(
        _make_prompt_message(
            "a cat",
            message_id=12,
            chat_id=-100123,
            tg_user_id=42,
        ),
        state,  # type: ignore[arg-type]
        IdentityApi(),  # type: ignore[arg-type]
        generation.GenerationRuntime(),
    )

    assert identities == [(-100123, 42)]
    assert state.cleared == 1


class RetryableEnhanceThenSuccessApi:
    def __init__(self, first_failure: str) -> None:
        self.first_failure = first_failure
        self.calls: list[tuple[int, str, int, str]] = []

    async def enhance_prompt(
        self,
        chat_id: int,
        prompt: str,
        *,
        idempotency_key: str,
        tg_user_id: int,
    ) -> str:
        self.calls.append((chat_id, prompt, tg_user_id, idempotency_key))
        if len(self.calls) == 1:
            if self.first_failure == "in_progress":
                raise generation.ApiError(
                    "idempotency_in_progress",
                    "still running",
                    425,
                )
            raise generation.ApiError(
                "temporary_failure",
                "response lost",
                503,
                outcome_unknown=True,
            )
        return "enhanced cat"

    async def create_generation(
        self,
        _chat_id: int,
        _payload: dict[str, object],
        *,
        tg_user_id: int,
    ) -> dict[str, object]:
        raise AssertionError(
            f"enhancement retry must not submit generation for user {tg_user_id}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["5xx", "in_progress"])
async def test_prompt_enhance_retry_reuses_semantic_key_and_flow_state(
    first_failure: str,
) -> None:
    params = dict(generation.DEFAULT_PARAMS)
    params["enhance"] = True
    state = FakeState({"params": params})
    runtime = generation.GenerationRuntime()
    api = RetryableEnhanceThenSuccessApi(first_failure)

    first_message = _make_prompt_message("a cat", message_id=40)
    await generation.on_prompt(
        first_message,
        state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        runtime,
    )

    stable_key = state._data.get("prompt_enhance_idempotency_key")
    generation_key = state._data.get("idempotency_key")
    assert isinstance(stable_key, str)
    assert stable_key.startswith("tg:")
    assert state._data["prompt_enhance_pending"] is True
    assert state.current_state == generation.GenFlow.awaiting_prompt
    assert state.cleared == 0

    await generation.on_prompt(
        _make_prompt_message("a cat", message_id=41),
        state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        runtime,
    )

    assert [call[3] for call in api.calls] == [stable_key, stable_key]
    assert state._data["idempotency_key"] == generation_key
    assert state._data["prompt_enhance_pending"] is False
    assert state._data["enhanced_prompt"] == "enhanced cat"
    assert state.current_state == generation.GenFlow.confirming_enhanced
    assert state.cleared == 0


@pytest.mark.asyncio
async def test_prompt_enhance_ambiguous_retry_rejects_changed_text_without_new_key() -> (
    None
):
    params = dict(generation.DEFAULT_PARAMS)
    params["enhance"] = True
    state = FakeState({"params": params})
    runtime = generation.GenerationRuntime()
    api = RetryableEnhanceThenSuccessApi("5xx")

    await generation.on_prompt(
        _make_prompt_message("a cat", message_id=50),
        state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        runtime,
    )
    stable_key = state._data["prompt_enhance_idempotency_key"]

    changed_message = _make_prompt_message("a dog", message_id=51)
    await generation.on_prompt(
        changed_message,
        state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        runtime,
    )

    assert len(api.calls) == 1
    assert state._data["prompt_enhance_idempotency_key"] == stable_key
    answers = [
        str(call.args[0]) if call.args else ""
        for call in changed_message.answer.call_args_list
    ]
    assert any("原提示词重试" in text for text in answers)


class LateEnhanceApi:
    def __init__(self, outcome: str = "success") -> None:
        self.outcome = outcome
        self.started = asyncio.Event()
        self.create_payloads: list[dict[str, object]] = []
        self.enhance_keys: list[str] = []

    async def enhance_prompt(
        self,
        _chat_id: int,
        _prompt: str,
        *,
        idempotency_key: str,
        tg_user_id: int,
    ) -> str:
        assert tg_user_id == 42
        self.enhance_keys.append(idempotency_key)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Simulate an upstream wrapper that swallows cancellation and completes late.
            if self.outcome == "failure":
                raise generation.ApiError(
                    "enhance_failed",
                    "late failure",
                    502,
                )
            return "late enhanced prompt"

    async def create_generation(
        self,
        _chat_id: int,
        payload: dict[str, object],
        *,
        tg_user_id: int,
    ) -> dict[str, object]:
        assert tg_user_id == 42
        self.create_payloads.append(dict(payload))
        return {"generation_ids": ["gen-1"], "user_id": "user-1"}


@pytest.mark.asyncio
async def test_cancel_invalidates_late_prompt_enhance_result() -> None:
    params = dict(generation.DEFAULT_PARAMS)
    params["enhance"] = True
    state = FakeState({"params": params})
    runtime = generation.GenerationRuntime()
    api = LateEnhanceApi()

    prompt_task = asyncio.create_task(
        generation.on_prompt(
            _make_prompt_message("a cat", message_id=20),
            state,  # type: ignore[arg-type]
            api,  # type: ignore[arg-type]
            runtime,
        )
    )
    await asyncio.wait_for(api.started.wait(), timeout=1)

    await generation.on_prompt(
        _make_prompt_message("/cancel", message_id=21),
        state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
        runtime,
    )
    await asyncio.wait_for(prompt_task, timeout=1)

    assert state._data == {}
    assert generation.GenFlow.confirming_enhanced not in state.state_history
    assert api.create_payloads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure"])
async def test_new_flow_epoch_rejects_late_enhance_completion(outcome: str) -> None:
    params = dict(generation.DEFAULT_PARAMS)
    params["enhance"] = True
    state = FakeState({"params": params})
    runtime = generation.GenerationRuntime()
    api = LateEnhanceApi(outcome)
    prompt_message = _make_prompt_message("a cat", message_id=30)

    prompt_task = asyncio.create_task(
        generation.on_prompt(
            prompt_message,
            state,  # type: ignore[arg-type]
            api,  # type: ignore[arg-type]
            runtime,
        )
    )
    await asyncio.wait_for(api.started.wait(), timeout=1)
    old_epoch = state._data.get("generation_flow_epoch")
    assert isinstance(old_epoch, str)

    await menu.cmd_new(
        _make_prompt_message("/new", message_id=31),
        state,  # type: ignore[arg-type]
        runtime,
    )
    await asyncio.wait_for(prompt_task, timeout=1)

    new_epoch = state._data.get("generation_flow_epoch")
    assert isinstance(new_epoch, str)
    assert new_epoch != old_epoch
    assert state.current_state == generation.GenFlow.configuring
    assert state._data == {
        "params": params,
        "generation_flow_epoch": new_epoch,
    }
    assert generation.GenFlow.confirming_enhanced not in state.state_history
    assert api.create_payloads == []
    assert state.cleared == 1
    old_answers = [
        str(call.args[0]) if call.args else ""
        for call in prompt_message.answer.call_args_list
    ]
    assert not any("优化失败" in text or "优化后" in text for text in old_answers)


@pytest.mark.asyncio
async def test_submit_once_guard_blocks_concurrent_double_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连点两次「使用优化版」只能产生一次提交（一次扣费）。"""
    durable_tracker = RecordingTracker()
    monkeypatch.setattr(generation, "tracker", durable_tracker)

    api = SubmitCountingApi()
    # 两次回调共享同一份 FSM 数据（Redis FSM storage 在竞态窗口内尚未被清空）
    shared_data = {
        "params": dict(generation.DEFAULT_PARAMS),
        "original_prompt": "orig",
        "enhanced_prompt": "enhanced prompt",
        "idempotency_key": "tg:stable-key",
    }
    state_a = FakeState(shared_data)
    state_b = FakeState(shared_data)
    cb_a, _msg_a = _make_callback(callback_id="callback-a")
    cb_b, _msg_b = _make_callback(callback_id="callback-b")

    await asyncio.gather(
        generation.on_enhance_choice(cb_a, state_a, api),  # type: ignore[arg-type]
        generation.on_enhance_choice(cb_b, state_b, api),  # type: ignore[arg-type]
    )

    assert len(api.payloads) == 1, "double-click must submit exactly once"
    assert str(api.payloads[0]["idempotency_key"]).startswith("tg:")

    # 被挡下的那次要给用户明确反馈，而不是静默吞掉
    all_answers = [
        str(call.args[0]) if call.args else ""
        for cb in (cb_a, cb_b)
        for call in cb.answer.call_args_list
    ]
    assert any("已提交" in text for text in all_answers)
    assert any("重复点击" in text for text in all_answers)


@pytest.mark.asyncio
async def test_ambiguous_enhance_choice_releases_guard_and_retries_same_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GuardedTracker(RecordingTracker):
        def __init__(self) -> None:
            super().__init__()
            self.locked: set[str] = set()
            self.released: list[str] = []

        async def acquire_submit_once(self, key: str) -> bool:
            if key in self.locked:
                return False
            self.locked.add(key)
            return True

        async def release_submit_once(self, key: str) -> None:
            self.locked.discard(key)
            self.released.append(key)

    guarded_tracker = GuardedTracker()
    monkeypatch.setattr(generation, "tracker", guarded_tracker)
    api = AmbiguousThenSuccessApi("malformed")
    state = FakeState(
        {
            "params": dict(generation.DEFAULT_PARAMS),
            "original_prompt": "orig",
            "enhanced_prompt": "enhanced prompt",
            "idempotency_key": "tg:stable-key",
        }
    )

    first, _msg = _make_callback(callback_id="callback-first")
    await generation.on_enhance_choice(
        first,
        state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
    )

    assert state.cleared == 0
    stable_key = api.payloads[0]["idempotency_key"]
    assert guarded_tracker.released == [stable_key]

    second, _msg = _make_callback(callback_id="callback-second")
    await generation.on_enhance_choice(
        second,
        state,  # type: ignore[arg-type]
        api,  # type: ignore[arg-type]
    )

    assert api.payloads[0] == api.payloads[1]
    assert state.cleared == 1


@pytest.mark.asyncio
async def test_second_click_after_state_cleared_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二次回调读到空 state 时不得回退成新 idempotency_key 再提交。"""
    tracker = Tracker()
    tracker._redis = SubmitOnceRedis()  # type: ignore[assignment]
    monkeypatch.setattr(generation, "tracker", tracker)

    api = SubmitCountingApi()
    cb, _msg = _make_callback()
    # state 已被前一个协程 clear()
    empty_state = FakeState({})

    await generation.on_enhance_choice(cb, empty_state, api)  # type: ignore[arg-type]

    assert api.payloads == [], "cleared state must not fall back to a fresh key"
    text = str(cb.answer.call_args.args[0]) if cb.answer.call_args.args else ""
    assert "会话已失效" in text


@pytest.mark.asyncio
async def test_submit_proceeds_when_redis_guard_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard outage after durable staging still relies on server idempotency."""

    class BrokenGuardTracker(RecordingTracker):
        async def acquire_submit_once(self, _key: str) -> bool:
            raise ConnectionError("redis down")

    monkeypatch.setattr(generation, "tracker", BrokenGuardTracker())

    api = SubmitCountingApi()
    cb, _msg = _make_callback()
    state = FakeState(
        {
            "params": dict(generation.DEFAULT_PARAMS),
            "enhanced_prompt": "enhanced prompt",
            "idempotency_key": "tg:stable-key",
        }
    )

    await generation.on_enhance_choice(cb, state, api)  # type: ignore[arg-type]

    assert len(api.payloads) == 1
    assert str(api.payloads[0]["idempotency_key"]).startswith("tg:")


@pytest.mark.asyncio
async def test_memory_storage_fallback_fails_closed_without_durable_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableJournalTracker:
        async def stage_generation_submission(self, **_kwargs: object) -> object:
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(generation, "tracker", UnavailableJournalTracker())
    api = SubmitCountingApi()
    state = FakeState({"params": dict(generation.DEFAULT_PARAMS)})
    message = _make_prompt_message("a cat", message_id=90)

    await generation.on_prompt(
        message,
        state,  # type: ignore[arg-type]
        api,
        generation.GenerationRuntime(),
    )

    assert api.payloads == []
    answers = [
        str(call.args[0]) if call.args else ""
        for call in message.answer.call_args_list
    ]
    assert any("没有创建任务" in text for text in answers)
    assert state.cleared == 0


@pytest.mark.asyncio
async def test_acquire_submit_once_is_single_winner() -> None:
    tracker = Tracker()
    redis = SubmitOnceRedis()
    tracker._redis = redis  # type: ignore[assignment]

    first = await tracker.acquire_submit_once("tg:key")
    second = await tracker.acquire_submit_once("tg:key")
    await tracker.release_submit_once("tg:key")
    third = await tracker.acquire_submit_once("tg:key")

    assert first is True
    assert second is False
    assert third is True
    # 锁必须带 NX + 有限 TTL，避免永久占位
    key, nx, ex = redis.set_calls[0]
    assert key == "tg:submit-once:tg:key"
    assert nx is True
    assert ex > 0


@pytest.mark.asyncio
async def test_acquire_submit_once_rejects_empty_key() -> None:
    tracker = Tracker()
    redis = SubmitOnceRedis()
    tracker._redis = redis  # type: ignore[assignment]

    assert await tracker.acquire_submit_once("") is False
    assert redis.set_calls == []
