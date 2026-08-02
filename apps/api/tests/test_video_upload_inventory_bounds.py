from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from app.routes import video_upload_inventory
from fastapi import HTTPException


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows


class _Db:
    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.responses = [
            _Result([]),
            _Result([]),
            _Result([]),
            _Result([]),
            _Result(
                [
                    SimpleNamespace(id="video-1"),
                    SimpleNamespace(id="video-2"),
                    SimpleNamespace(id="video-3"),
                ]
            ),
        ]

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_variant_inventory_scan_is_bounded_without_row_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        video_upload_inventory,
        "_REFERENCE_INVENTORY_VARIANT_SCAN_LIMIT",
        2,
    )
    db = _Db()

    def http_error(code: str, message: str, status_code: int) -> HTTPException:
        return HTTPException(
            status_code=status_code,
            detail={"error": {"code": code, "message": message}},
        )

    with pytest.raises(HTTPException) as exc_info:
        await video_upload_inventory._query_locked_inventory(  # noqa: SLF001
            user_id="user-1",
            sha256="a" * 64,
            db=db,  # type: ignore[arg-type]
            deps=SimpleNamespace(max_count=8, http_error=http_error),
            cleanup_page_size=32,
            result_rows=lambda result: list(result.rows),
        )

    assert exc_info.value.status_code == 503
    assert (
        exc_info.value.detail["error"]["code"] == "reference_video_inventory_too_large"
    )
    variant_statement = db.statements[-1]
    assert getattr(variant_statement, "_for_update_arg", None) is None
    assert variant_statement._limit_clause.value == 3  # noqa: SLF001


@pytest.mark.asyncio
async def test_aged_adoption_reconciliation_reprobes_inside_mutation_lock() -> None:
    marker = SimpleNamespace(
        video_id="video-1",
        user_id="user-1",
        storage_key="u/user-1/vref/video-1/source.mp4",
        sha256="a" * 64,
        size_bytes=123,
    )
    not_adopted = object()
    state = {"locked": False, "probes": 0, "discarded": False}

    class Lifecycle:
        async def aged_upload_adoption_markers(self, **_kwargs: Any) -> list[Any]:
            return [marker]

        @asynccontextmanager
        async def reference_mutation_lock(self, **kwargs: Any) -> Any:
            assert kwargs == {"user_id": "user-1", "video_id": "video-1"}
            assert state["locked"] is False
            state["locked"] = True
            try:
                yield
            finally:
                state["locked"] = False

        async def discard_unadopted_upload(self, selected: Any) -> bool:
            assert state["locked"] is True
            assert selected is marker
            state["discarded"] = True
            return True

    async def probe_adoption(**kwargs: Any) -> Any:
        assert state["locked"] is True
        assert kwargs["video_id"] == "video-1"
        state["probes"] += 1
        return SimpleNamespace(outcome=not_adopted)

    async def fail_clear(**_kwargs: Any) -> None:
        raise AssertionError("not-adopted marker must be discarded")

    await video_upload_inventory._reconcile_aged_adoption_markers(  # noqa: SLF001
        user_id="user-1",
        deps=SimpleNamespace(
            storage_lifecycle=Lifecycle(),
            probe_adoption=probe_adoption,
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        ),
        clear_adoption_marker=fail_clear,
        adopted_outcome=object(),
        not_adopted_outcome=not_adopted,
    )

    assert state == {"locked": False, "probes": 1, "discarded": True}
