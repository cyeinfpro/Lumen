from __future__ import annotations

import asyncio
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

from app.handlers import generation  # noqa: E402
from app.tracker import Tracker  # noqa: E402


class LegacyGenerationApi:
    async def create_generation(
        self,
        _chat_id: int,
        _payload: dict[str, object],
    ) -> dict[str, object]:
        return {"generation_ids": ["gen-legacy-api"]}


class RecordingTracker:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def init_batch(self, batch_id: str, count: int) -> None:
        self.calls.append(("init_batch", batch_id, count))

    async def add(self, gen_id: str, track: object) -> None:
        self.calls.append(("add", gen_id, track))


@pytest.mark.asyncio
async def test_legacy_api_without_user_id_does_not_register_empty_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[str] = []
    recording_tracker = RecordingTracker()
    monkeypatch.setattr(generation, "tracker", recording_tracker)

    async def answer(text: str) -> SimpleNamespace:
        answers.append(text)
        return SimpleNamespace(message_id=123)

    await generation._submit_generation(
        100,
        "prompt",
        {
            "aspect_ratio": "1:1",
            "render_quality": "high",
            "count": 1,
            "resolution": "2k",
            "output_format": "jpeg",
            "fast": False,
        },
        LegacyGenerationApi(),  # type: ignore[arg-type]
        answer,
        "tg:test",
    )

    assert recording_tracker.calls == []
    assert len(answers) == 1
    assert "#gen-lega" in answers[0]
    assert "user_id" in answers[0]
    assert "/tasks" in answers[0]


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


class SubmitCountingApi:
    """Counts create_generation calls and yields control to expose races."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def create_generation(
        self,
        _chat_id: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self.payloads.append(dict(payload))
        # 模拟 HTTP 往返：让出 event loop，给第二个回调追上来的机会
        await asyncio.sleep(0)
        return {"generation_ids": ["gen-1"], "user_id": "user-1"}


class FakeState:
    """Stand-in for aiogram FSMContext with clear()/get_data()."""

    def __init__(self, data: dict[str, object]) -> None:
        self._data = dict(data)
        self.cleared = 0

    async def get_data(self) -> dict[str, object]:
        return dict(self._data)

    async def clear(self) -> None:
        self.cleared += 1
        self._data = {}

    async def set_state(self, _state: object) -> None:
        pass

    async def update_data(self, **kwargs: object) -> None:
        self._data.update(kwargs)


def _make_callback(chat_id: int = 42) -> tuple[object, MagicMock]:
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=chat_id)
    msg.edit_reply_markup = AsyncMock()

    async def answer(text: str) -> SimpleNamespace:
        # status message；同样让出 loop
        await asyncio.sleep(0)
        return SimpleNamespace(message_id=999)

    msg.answer = AsyncMock(side_effect=answer)

    cb = MagicMock()
    cb.data = "enh:use"
    cb.message = msg
    cb.answer = AsyncMock()
    return cb, msg


@pytest.mark.asyncio
async def test_submit_once_guard_blocks_concurrent_double_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连点两次「使用优化版」只能产生一次提交（一次扣费）。"""
    tracker = Tracker()
    tracker._redis = SubmitOnceRedis()  # type: ignore[assignment]
    monkeypatch.setattr(generation, "tracker", tracker)

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
    cb_a, _msg_a = _make_callback()
    cb_b, _msg_b = _make_callback()

    await asyncio.gather(
        generation.on_enhance_choice(cb_a, state_a, api),  # type: ignore[arg-type]
        generation.on_enhance_choice(cb_b, state_b, api),  # type: ignore[arg-type]
    )

    assert len(api.payloads) == 1, "double-click must submit exactly once"
    assert api.payloads[0]["idempotency_key"] == "tg:stable-key"

    # 被挡下的那次要给用户明确反馈，而不是静默吞掉
    all_answers = [
        str(call.args[0]) if call.args else ""
        for cb in (cb_a, cb_b)
        for call in cb.answer.call_args_list
    ]
    assert any("已提交" in text for text in all_answers)
    assert any("重复点击" in text for text in all_answers)


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
    """Redis 挂了不能卡死用户：放行提交，靠固化的 idempotency_key 兜底去重。"""

    class BrokenTracker:
        async def acquire_submit_once(self, _key: str) -> bool:
            raise ConnectionError("redis down")

        async def init_batch(self, _batch_id: str, _count: int) -> None:
            pass

        async def add(self, _gen_id: str, _track: object) -> None:
            pass

    monkeypatch.setattr(generation, "tracker", BrokenTracker())

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
    assert api.payloads[0]["idempotency_key"] == "tg:stable-key"


@pytest.mark.asyncio
async def test_acquire_submit_once_is_single_winner() -> None:
    tracker = Tracker()
    redis = SubmitOnceRedis()
    tracker._redis = redis  # type: ignore[assignment]

    first = await tracker.acquire_submit_once("tg:key")
    second = await tracker.acquire_submit_once("tg:key")

    assert first is True
    assert second is False
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
