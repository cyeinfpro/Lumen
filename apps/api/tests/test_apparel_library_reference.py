from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import _apparel_library_reference as reference


class _Result:
    def __init__(self, row: Any | None) -> None:
        self.row = row

    def scalar_one_or_none(self) -> Any | None:
        return self.row


class _Db:
    def __init__(self, row: Any | None) -> None:
        self.row = row

    async def execute(self, _statement: Any) -> _Result:
        return _Result(self.row)


@pytest.mark.asyncio
async def test_reference_extract_empty_storage_key_returns_user_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_providers(_db: Any) -> list[Any]:
        raise AssertionError("empty storage_key must not call providers")

    monkeypatch.setattr(reference, "_ordered_response_providers", fail_providers)
    image = SimpleNamespace(id="image-1", storage_key="")

    result = await reference.auto_tag_owned_model_library_image(
        _Db(image),  # type: ignore[arg-type]
        user_id="user-1",
        image_id="image-1",
    )

    assert result.notes == reference._REFERENCE_STORAGE_MISSING_NOTE  # noqa: SLF001
    assert result.age_segment is None
    assert result.gender is None


@pytest.mark.asyncio
async def test_reference_extract_read_failure_returns_user_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_data_url(_image: Any) -> None:
        return None

    async def fail_providers(_db: Any) -> list[Any]:
        raise AssertionError("unreadable storage must not call providers")

    monkeypatch.setattr(reference, "_image_data_url", no_data_url)
    monkeypatch.setattr(reference, "_ordered_response_providers", fail_providers)
    image = SimpleNamespace(id="image-1", storage_key="u/user-1/missing.png")

    result = await reference.auto_tag_owned_model_library_image(
        _Db(image),  # type: ignore[arg-type]
        user_id="user-1",
        image_id="image-1",
    )

    assert result.notes == reference._REFERENCE_STORAGE_READ_FAILED_NOTE  # noqa: SLF001
    assert result.age_segment is None
    assert result.gender is None
