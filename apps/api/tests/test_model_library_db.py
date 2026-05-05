"""Regression tests for the model-library DB cutover.

The original JSON-file design read & wrote a per-user index file for
every favorite/auto-tag call. Concurrent requests trampled each other.
The cutover replaced that with one INSERT per favorite and one row
UPDATE per vision result, so this file checks the new code paths really
don't touch the shared file anymore.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import workflows
from lumen_core.models import ModelLibraryItem


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any | None:
        return self._rows[0] if self._rows else None


class _ConcurrentDb:
    """Stub session that records every ``add`` call.

    Multiple awaits against the same session collect into ``added``;
    the production path no longer reads/writes a shared mutable JSON
    blob, so concurrent ``_add_user_library_item`` invocations should
    each append exactly one row regardless of interleaving.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.executes: list[Any] = []
        self.commits = 0
        self.flushes = 0

    async def execute(self, statement: Any) -> _Result:
        self.executes.append(statement)
        return _Result([])

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _row: Any) -> None:  # pragma: no cover - unused here
        return None

    async def rollback(self) -> None:  # pragma: no cover - unused here
        return None


class _LegacyMigrationDb:
    """Tiny async session stub for the lazy JSON -> DB migration guard."""

    def __init__(self, scalar_batches: list[list[Any]]) -> None:
        self.scalar_batches = scalar_batches
        self.statements: list[Any] = []
        self.flushes = 0

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        rows = self.scalar_batches.pop(0) if self.scalar_batches else []
        return _Result(rows)

    async def flush(self) -> None:
        self.flushes += 1


async def _noop_owned_image(_db: Any, *, user_id: str, image_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=image_id, user_id=user_id)


@pytest.mark.asyncio
async def test_add_user_library_item_inserts_one_row_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent favorites must each add exactly one row to the
    session — no shared JSON read-modify-write that could lose updates.
    """
    monkeypatch.setattr(workflows, "_owned_image", _noop_owned_image)
    db = _ConcurrentDb()

    async def _favorite(image_id: str, title: str) -> dict[str, Any]:
        return await workflows._add_user_library_item(  # noqa: SLF001
            db,
            user_id="user-1",
            source="favorite",
            image_id=image_id,
            title=title,
            age_segment="adult",
            gender="female",
            appearance_direction="asian",
            style_tags=["minimal"],
        )

    results = await asyncio.gather(
        *[_favorite(f"img-{i}", f"Model {i}") for i in range(8)]
    )

    assert len(results) == 8
    assert len(db.added) == 8, "every favorite must INSERT a row regardless of concurrency"
    assert all(isinstance(row, ModelLibraryItem) for row in db.added)
    image_ids_added = {row.image_id for row in db.added}
    assert image_ids_added == {f"img-{i}" for i in range(8)}
    item_ids = {row.id for row in db.added}
    assert len(item_ids) == 8, "uuid7 ids must be unique across concurrent calls"
    assert all(item_id.startswith("user:") for item_id in item_ids)


@pytest.mark.asyncio
async def test_legacy_user_library_is_lazily_migrated(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "user:legacy-1",
                        "source": "favorite",
                        "image_id": "img-1",
                        "title": "Legacy saved model",
                        "age_segment": "adult",
                        "gender": "female",
                        "style_tags": ["minimal"],
                    }
                ],
                "hidden_preset_ids": ["preset:hidden-1"],
            }
        ),
        "utf-8",
    )
    monkeypatch.setattr(
        workflows,
        "_library_user_index_path",
        lambda _user_id: index_path,
    )
    db = _LegacyMigrationDb(
        [
            [],  # existing model_library_items ids
            ["img-1"],  # valid owned Image ids
            [],  # insert model_library_items result
            [],  # existing hidden presets
            [],  # insert hidden presets result
        ]
    )

    migrated = await workflows._ensure_legacy_user_library_migrated(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        "user-1",
    )

    assert migrated is True
    assert db.flushes == 1
    rendered = [str(statement) for statement in db.statements]
    assert any("INSERT INTO model_library_items" in statement for statement in rendered)
    assert any("INSERT INTO model_library_hidden_presets" in statement for statement in rendered)


@pytest.mark.asyncio
async def test_legacy_user_library_skips_items_without_valid_image(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "user:legacy-missing-image",
                        "source": "favorite",
                        "image_id": "missing-img",
                        "title": "Broken legacy row",
                    }
                ],
                "hidden_preset_ids": [],
            }
        ),
        "utf-8",
    )
    monkeypatch.setattr(
        workflows,
        "_library_user_index_path",
        lambda _user_id: index_path,
    )
    db = _LegacyMigrationDb(
        [
            [],  # existing model_library_items ids
            [],  # no valid owned Image ids
        ]
    )

    migrated = await workflows._ensure_legacy_user_library_migrated(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        "user-1",
    )

    assert migrated is False
    assert db.flushes == 0
    assert not any(
        "INSERT INTO model_library_items" in str(statement)
        for statement in db.statements
    )


def test_delete_user_item_removes_legacy_index_entry(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "items": [
                    {"id": "user:keep", "image_id": "img-keep"},
                    {"id": "user:delete-me", "image_id": "img-delete"},
                ],
                "hidden_preset_ids": [],
            }
        ),
        "utf-8",
    )
    monkeypatch.setattr(
        workflows,
        "_library_user_index_path",
        lambda _user_id: index_path,
    )

    removed = workflows._remove_user_library_item_from_legacy_index(  # noqa: SLF001
        "user-1",
        "user:delete-me",
    )

    assert removed is True
    updated = json.loads(index_path.read_text("utf-8"))
    assert [item["id"] for item in updated["items"]] == ["user:keep"]


def test_hide_preset_updates_legacy_hidden_index(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "items": [],
                "hidden_preset_ids": ["preset:existing"],
            }
        ),
        "utf-8",
    )
    monkeypatch.setattr(
        workflows,
        "_library_user_index_path",
        lambda _user_id: index_path,
    )

    hidden = workflows._hide_preset_in_legacy_user_library_index(  # noqa: SLF001
        "user-1",
        "preset:new",
    )

    assert hidden is True
    updated = json.loads(index_path.read_text("utf-8"))
    assert updated["hidden_preset_ids"] == ["preset:existing", "preset:new"]


class _SingleRowDb:
    """Stub that returns one preloaded row for SELECT queries."""

    def __init__(self, row: ModelLibraryItem) -> None:
        self.row = row
        self.commits = 0
        self.refreshes = 0

    async def execute(self, _statement: Any) -> _Result:
        return _Result([self.row])

    def add(self, _row: Any) -> None:  # pragma: no cover - not used
        return None

    async def flush(self) -> None:  # pragma: no cover - not used
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _row: Any) -> None:
        self.refreshes += 1


def _empty_item() -> ModelLibraryItem:
    row = ModelLibraryItem(
        id="user:test-1",
        user_id="user-1",
        source="favorite",
        image_id="img-1",
        title="Saved",
        age_segment="user_favorites",
        gender=None,
        appearance_direction=None,
        style_tags=[],
        library_folder=None,
    )
    return row


@pytest.mark.asyncio
async def test_auto_tag_skips_persistence_when_provider_returns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vision provider failure → auto_tagged_at must stay NULL so the
    UI can distinguish "not yet identified" from "identified but empty".
    """
    row = _empty_item()
    db = _SingleRowDb(row)

    async def _empty_upstream(_db: Any, *, image_id: str, user_id: str) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(workflows, "_api_call_tagging_upstream", _empty_upstream)

    out = await workflows._auto_tag_library_item(  # noqa: SLF001
        db=db, user_id="user-1", item_id="user:test-1"
    )

    assert out.style_tags == []
    assert out.appearance_direction is None
    assert row.auto_tagged_at is None, "empty vision payload must not flag the row identified"
    assert row.style_tags == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_auto_tag_writes_when_provider_returns_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _empty_item()
    db = _SingleRowDb(row)

    async def _vision_upstream(_db: Any, *, image_id: str, user_id: str) -> dict[str, Any]:
        return {
            "style_tags": ["minimal", "soft"],
            "appearance_direction": "asian",
            "age_segment": "young_adult",
            "gender": "female",
            "notes": "soft natural light",
        }

    monkeypatch.setattr(workflows, "_api_call_tagging_upstream", _vision_upstream)

    out = await workflows._auto_tag_library_item(  # noqa: SLF001
        db=db, user_id="user-1", item_id="user:test-1"
    )

    assert out.style_tags == ["minimal", "soft"]
    assert out.appearance_direction == "asian"
    assert row.style_tags == ["minimal", "soft"]
    assert row.appearance_direction == "asian"
    assert row.age_segment == "young_adult"
    assert row.gender == "female"
    assert row.auto_tagged_at is not None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_auto_tag_preserves_user_filled_appearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-provided appearance_direction is conservative — vision
    must not overwrite a non-empty user value.
    """
    row = _empty_item()
    row.appearance_direction = "european"  # user filled in advance
    db = _SingleRowDb(row)

    async def _vision_upstream(_db: Any, *, image_id: str, user_id: str) -> dict[str, Any]:
        return {
            "style_tags": ["editorial"],
            "appearance_direction": "asian",
            "gender": "female",
        }

    monkeypatch.setattr(workflows, "_api_call_tagging_upstream", _vision_upstream)

    await workflows._auto_tag_library_item(  # noqa: SLF001
        db=db, user_id="user-1", item_id="user:test-1"
    )

    assert row.appearance_direction == "european"
    assert row.style_tags == ["editorial"]


@pytest.mark.asyncio
async def test_auto_tag_appends_existing_style_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _empty_item()
    row.style_tags = ["温柔亲和"]
    db = _SingleRowDb(row)

    async def _vision_upstream(_db: Any, *, image_id: str, user_id: str) -> dict[str, Any]:
        return {"style_tags": ["温柔亲和", "清冷高级"]}

    monkeypatch.setattr(workflows, "_api_call_tagging_upstream", _vision_upstream)

    await workflows._auto_tag_library_item(  # noqa: SLF001
        db=db, user_id="user-1", item_id="user:test-1"
    )

    assert row.style_tags == ["温柔亲和", "清冷高级"]
