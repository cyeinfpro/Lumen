from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.tasks import generation
from lumen_core.constants import ImageSource


class _ScalarResult:
    def __init__(self, row: Any) -> None:
        self.row = row

    def scalar_one_or_none(self) -> Any:
        return self.row


class _Session:
    def __init__(self, row: Any) -> None:
        self.row = row

    async def execute(self, _stmt: Any) -> _ScalarResult:
        return _ScalarResult(self.row)


def _image(**overrides: Any) -> SimpleNamespace:
    base = {
        "id": "img-1",
        "user_id": "user-1",
        "source": ImageSource.GENERATED.value,
        "width": 1024,
        "height": 1024,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_existing_generated_image_short_circuit_accepts_valid_image() -> None:
    row = _image()

    found = await generation._find_existing_generated_image(
        _Session(row),
        task_id="gen-1",
        user_id="user-1",
    )

    assert found is row


@pytest.mark.asyncio
async def test_existing_generated_image_short_circuit_rejects_non_generated() -> None:
    found = await generation._find_existing_generated_image(
        _Session(_image(source="upload")),
        task_id="gen-1",
        user_id="user-1",
    )

    assert found is None


@pytest.mark.asyncio
async def test_existing_generated_image_short_circuit_rejects_bad_dimensions() -> None:
    found = await generation._find_existing_generated_image(
        _Session(_image(width=0, height=1024)),
        task_id="gen-1",
        user_id="user-1",
    )

    assert found is None
