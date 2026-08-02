from __future__ import annotations

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
