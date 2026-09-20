"""Preset reuse spans projects and must survive overlapping transactions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import itertools
import threading
from types import SimpleNamespace
from typing import Any

from PIL import Image as PILImage
import pytest
from sqlalchemy.exc import MultipleResultsFound

from app.workflows.adapters import library_materialization as materialization


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalar_one_or_none(self):
        if len(self.rows) > 1:
            raise MultipleResultsFound("multiple private preset copies")
        return self.rows[0] if self.rows else None


@dataclass
class _State:
    rows: list[Any] = field(default_factory=list)
    locks: dict[int, asyncio.Lock] = field(default_factory=dict)


class _Db:
    """READ COMMITTED rows plus transaction-scoped PostgreSQL lock behavior."""

    def __init__(self, state, user_id, preset_id, *, fail_lock=False):
        self.state = state
        self.user_id = user_id
        self.preset_id = preset_id
        self.fail_lock = fail_lock
        self.pending = []
        self.held = {}
        self.selects = []
        self.attempted = asyncio.Event()

    async def connection(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(self, statement):
        self.attempted.set()
        compiled = statement.compile()
        if "pg_advisory_xact_lock" in str(compiled):
            if self.fail_lock:
                raise RuntimeError("preset lock unavailable")
            key = next(iter(compiled.params.values()))
            lock = self.state.locks.setdefault(key, asyncio.Lock())
            if key not in self.held:
                await lock.acquire()
                self.held[key] = lock
            return _Result([])
        self.selects.append(statement)
        rows = [
            row for row in self.state.rows
            if row.user_id == self.user_id
            and row.metadata_jsonb["apparel_model_library_item_id"] == self.preset_id
            and getattr(row, "deleted_at", None) is None
        ]
        if statement._order_by_clauses:
            rows.sort(key=lambda row: (row.created_at, row.id))
        if statement._limit_clause is not None:
            rows = rows[:statement._limit_clause.value]
        return _Result(rows)

    def add(self, row):
        row.created_at = datetime.now(timezone.utc)
        self.pending.append(row)

    async def flush(self):
        pass

    def finish(self, *, commit):
        if commit:
            self.state.rows.extend(self.pending)
        self.pending.clear()
        for lock in self.held.values():
            lock.release()
        self.held.clear()


async def _create(db, item):
    try:
        image = await materialization.create_user_image_from_preset(
            db, user_id=db.user_id, item=item,
        )
    except BaseException:
        db.finish(commit=False)
        raise
    db.finish(commit=True)
    return image


@pytest.fixture
def preset(tmp_path, monkeypatch):
    source = tmp_path / "presets/model.png"
    source.parent.mkdir()
    PILImage.new("RGB", (3, 2)).save(source, format="PNG")
    monkeypatch.setattr(materialization, "_storage_path", lambda key: tmp_path / key)

    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    monkeypatch.setattr(materialization, "_write_bytes_replace", write)
    return tmp_path, {"id": "preset:model:1", "image_storage_key": "presets/model.png"}


def _hold_first_copy(monkeypatch):
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    indices = itertools.count()
    original = materialization._copy_preset_binary_sync

    def copy(*args):
        if next(indices) == 0:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(10), "copy was not released by the test"
        return original(*args)

    monkeypatch.setattr(materialization, "_copy_preset_binary_sync", copy)
    return started, release


@pytest.mark.asyncio
async def test_same_preset_across_projects_materializes_one_private_image(preset, monkeypatch):
    root, item = preset
    state = _State()
    first_db = _Db(state, "owner", item["id"])
    second_db = _Db(state, "owner", item["id"])
    started, release = _hold_first_copy(monkeypatch)
    first = asyncio.create_task(_create(first_db, item))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 5)
        second = asyncio.create_task(_create(second_db, item))
        await asyncio.wait_for(second_db.attempted.wait(), 5)
    finally:
        release.set()
    images = await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert len(state.rows) == 1
    assert images[0].id == images[1].id
    assert len(list((root / "u").rglob("*.png"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id,preset_id", [
    ("other", "preset:model:1"),
    ("owner", "preset:other:1"),
])
async def test_other_owners_and_presets_do_not_wait_for_an_unrelated_copy(
    preset, monkeypatch, user_id, preset_id,
):
    _root, item = preset
    state = _State()
    started, release = _hold_first_copy(monkeypatch)
    first = asyncio.create_task(_create(_Db(state, "owner", item["id"]), item))
    try:
        await asyncio.wait_for(started.wait(), 5)
        other = await asyncio.wait_for(_create(
            _Db(state, user_id, preset_id), {**item, "id": preset_id},
        ), 5)
        assert other.user_id == user_id
        assert not first.done()
    finally:
        release.set()
        await first
    assert len(state.rows) == 2


@pytest.mark.asyncio
async def test_cancelling_a_waiter_preserves_the_owner_transaction(preset, monkeypatch):
    _root, item = preset
    state = _State()
    started, release = _hold_first_copy(monkeypatch)
    first = asyncio.create_task(_create(_Db(state, "owner", item["id"]), item))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 5)
        waiter_db = _Db(state, "owner", item["id"])
        second = asyncio.create_task(_create(waiter_db, item))
        await asyncio.wait_for(waiter_db.attempted.wait(), 5)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert not first.done()
    finally:
        release.set()
    image = await first
    retry = await _create(_Db(state, "owner", item["id"]), item)
    assert retry.id == image.id
    assert len(state.rows) == 1


@pytest.mark.asyncio
async def test_historical_duplicates_reuse_the_oldest_without_deleting_user_data(preset, monkeypatch):
    _root, item = preset
    def row(image_id, year):
        return SimpleNamespace(
            id=image_id, user_id="owner", deleted_at=None,
            metadata_jsonb={"apparel_model_library_item_id": item["id"]},
            created_at=datetime(year, 1, 1, tzinfo=timezone.utc),
        )
    newer, older = row("newer", 2026), row("older", 2025)
    state = _State(rows=[newer, older])
    db = _Db(state, "owner", item["id"])
    def unexpected(*_args):
        pytest.fail("historical preset copies must be reused, not recopied")
    monkeypatch.setattr(materialization, "_copy_preset_binary_sync", unexpected)
    assert await _create(db, item) is older
    assert state.rows == [newer, older]
    assert db.selects[0]._limit_clause.value == 1


@pytest.mark.asyncio
async def test_failed_transaction_lock_never_starts_a_copy(preset):
    root, item = preset
    state = _State()
    with pytest.raises(RuntimeError, match="preset lock unavailable"):
        await _create(_Db(state, "owner", item["id"], fail_lock=True), item)
    assert not state.rows
    assert not (root / "u").exists()
