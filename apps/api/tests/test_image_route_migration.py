from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.runtime_settings import (
    image_primary_route_to_parts,
    migrate_image_primary_route,
)


class _Rows:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, str]]:
        return self._rows


class _FakeDb:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows
        self.added: list[SimpleNamespace] = []

    async def execute(self, _stmt: object) -> _Rows:
        return _Rows(self.rows)

    def add(self, obj: object) -> None:
        self.added.append(SimpleNamespace(key=obj.key, value=obj.value))


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        ("responses", ("auto", "responses")),
        ("image2", ("auto", "image2")),
        ("image_jobs", ("image_jobs_only", "responses")),
        ("dual_race", ("auto", "dual_race")),
        ("bad", ("auto", "responses")),
    ],
)
def test_image_primary_route_to_parts(old: str, expected: tuple[str, str]) -> None:
    assert image_primary_route_to_parts(old) == expected


@pytest.mark.asyncio
async def test_migrate_image_primary_route_backfills_new_keys() -> None:
    db = _FakeDb([("image.primary_route", "image_jobs")])

    changed = await migrate_image_primary_route(db)  # type: ignore[arg-type]

    assert changed is True
    assert [(item.key, item.value) for item in db.added] == [
        ("image.channel", "image_jobs_only"),
        ("image.engine", "responses"),
    ]


@pytest.mark.asyncio
async def test_migrate_image_primary_route_is_idempotent_when_new_key_exists() -> None:
    db = _FakeDb(
        [
            ("image.primary_route", "dual_race"),
            ("image.channel", "auto"),
        ]
    )

    changed = await migrate_image_primary_route(db)  # type: ignore[arg-type]

    assert changed is False
    assert db.added == []
