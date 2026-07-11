from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app.handlers import generation  # noqa: E402


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
