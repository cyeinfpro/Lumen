"""回调 handler 的行为回归（审计 J-1 / J-2）。

J-1：redo 必须把原任务的参考图带上，否则 API 会把 intent 降级成 text_to_image，
     出一张跟原图无关的图 —— 而这一次是真扣费的。
J-2：cfg:* 的 callback_data 不是可信输入，非法值不能把 handler 打断。
"""

from __future__ import annotations

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
from lumen_core.constants import MAX_MESSAGE_ATTACHMENTS  # noqa: E402

from app.handlers import actions, menu  # noqa: E402
from app.keyboards import DEFAULT_PARAMS  # noqa: E402


class RedoApi:
    def __init__(self, gen: dict[str, object]) -> None:
        self.gen = gen
        self.payloads: list[dict[str, object]] = []

    async def get_generation(self, _chat_id: int, _gen_id: str) -> dict[str, object]:
        return dict(self.gen)

    async def create_generation(
        self, _chat_id: int, payload: dict[str, object]
    ) -> dict[str, object]:
        self.payloads.append(dict(payload))
        return {"generation_ids": ["gen-new"], "user_id": "user-1"}


class NoopTracker:
    def __init__(self) -> None:
        self.added: list[tuple[str, object]] = []

    async def add(self, gen_id: str, track: object) -> None:
        self.added.append((gen_id, track))


def _make_redo_callback(chat_id: int = 42) -> object:
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=chat_id)
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=999))
    cb = MagicMock()
    cb.data = "redo:gen-src"
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_redo_carries_original_reference_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actions, "tracker", NoopTracker())
    api = RedoApi(
        {
            "prompt": "a cat",
            "aspect_ratio": "16:9",
            "size_requested": "3200x1800",
            "render_quality": "high",
            "output_format": "png",
            "fast": True,
            "input_image_ids": ["img-a", "img-b"],
        }
    )

    await actions.on_redo(_make_redo_callback(), api)  # type: ignore[arg-type]

    assert len(api.payloads) == 1
    assert api.payloads[0]["attachment_image_ids"] == ["img-a", "img-b"]
    # 其余参数照抄原任务
    assert api.payloads[0]["aspect_ratio"] == "16:9"
    assert api.payloads[0]["resolution"] == "4k"


@pytest.mark.asyncio
async def test_redo_truncates_reference_images_to_api_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actions, "tracker", NoopTracker())
    too_many = [f"img-{i}" for i in range(MAX_MESSAGE_ATTACHMENTS + 3)]
    api = RedoApi({"prompt": "a cat", "input_image_ids": too_many})

    await actions.on_redo(_make_redo_callback(), api)  # type: ignore[arg-type]

    assert api.payloads[0]["attachment_image_ids"] == too_many[:MAX_MESSAGE_ATTACHMENTS]


@pytest.mark.asyncio
async def test_redo_without_reference_images_stays_text_to_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actions, "tracker", NoopTracker())
    api = RedoApi({"prompt": "a cat"})

    await actions.on_redo(_make_redo_callback(), api)  # type: ignore[arg-type]

    assert api.payloads[0]["attachment_image_ids"] == []


# ---------- J-2：cfg 回调的输入校验 ----------


class CfgState:
    def __init__(self, params: dict[str, object]) -> None:
        self.data: dict[str, object] = {"params": dict(params)}

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def update_data(self, **kwargs: object) -> None:
        self.data.update(kwargs)

    async def set_state(self, _state: object) -> None:
        pass

    async def clear(self) -> None:
        self.data = {}


def _make_cfg_callback(data: str) -> object:
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=7)
    msg.edit_text = AsyncMock()
    cb = MagicMock()
    cb.data = data
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


@pytest.mark.parametrize(
    "callback_data",
    [
        "cfg:count:abc",
        "cfg:count:",
        "cfg:count:99",
        "cfg:resolution:8k",
        "cfg:aspect_ratio:0:0",
    ],
)
@pytest.mark.asyncio
async def test_invalid_cfg_callback_is_rejected_without_crashing(
    callback_data: str,
) -> None:
    state = CfgState(DEFAULT_PARAMS)
    cb = _make_cfg_callback(callback_data)

    await menu.on_cfg(cb, state)  # type: ignore[arg-type]

    # 参数不能被污染，而且必须给用户一个 ack（否则 TG 客户端按钮一直转圈）
    assert state.data["params"] == dict(DEFAULT_PARAMS)
    assert cb.answer.await_count == 1


@pytest.mark.asyncio
async def test_valid_cfg_callback_still_applies() -> None:
    state = CfgState(DEFAULT_PARAMS)

    await menu.on_cfg(_make_cfg_callback("cfg:count:4"), state)  # type: ignore[arg-type]

    assert state.data["params"]["count"] == 4
    assert isinstance(state.data["params"]["count"], int)


@pytest.mark.asyncio
async def test_bool_cfg_callback_coerces_to_bool() -> None:
    state = CfgState(DEFAULT_PARAMS)

    await menu.on_cfg(_make_cfg_callback("cfg:fast:false"), state)  # type: ignore[arg-type]

    assert state.data["params"]["fast"] is False
