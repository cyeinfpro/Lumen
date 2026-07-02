from __future__ import annotations

import sys
from pathlib import Path

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from aiogram.types import InlineKeyboardMarkup  # noqa: E402

from app import keyboards  # noqa: E402


def _callback_values(markup: InlineKeyboardMarkup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


def test_generation_action_callbacks_fit_telegram_limit_for_uuid_ids() -> None:
    gen_id = "018f0000-0000-7000-8000-000000000000"

    retry = keyboards.retry_keyboard(gen_id)
    success = keyboards.post_success_keyboard(gen_id)

    assert retry is not None
    assert success is not None
    values = _callback_values(retry) + _callback_values(success)
    assert values == [f"retry:{gen_id}", f"redo:{gen_id}", f"iter:{gen_id}"]
    assert all(len(value.encode("utf-8")) <= 64 for value in values)


def test_generation_action_callbacks_are_omitted_when_id_is_too_long() -> None:
    gen_id = "g" * 80

    assert keyboards.retry_keyboard(gen_id) is None
    assert keyboards.post_success_keyboard(gen_id) is None

    links_only = keyboards.post_success_keyboard(
        gen_id,
        web_url="https://example.com/edit",
        project_url="http://example.com/unsafe",
    )

    assert links_only is not None
    assert _callback_values(links_only) == []
    buttons = [button for row in links_only.inline_keyboard for button in row]
    assert [button.url for button in buttons] == ["https://example.com/edit"]
