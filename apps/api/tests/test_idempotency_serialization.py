from __future__ import annotations

import json
from decimal import Decimal

from pydantic import BaseModel

from app.services.idempotency import _dump


class _NestedPayload(BaseModel):
    amount: str
    tags: list[str]


def test_idempotency_dump_serializes_nested_pydantic_models() -> None:
    raw = _dump(
        {
            "request_hash": "req-1",
            "response": _NestedPayload(amount="10.000000", tags=["redeem"]),
            "decimal": Decimal("1.23"),
            "binary": b"ok",
        }
    )

    assert json.loads(raw) == {
        "binary": "ok",
        "decimal": "1.23",
        "request_hash": "req-1",
        "response": {"amount": "10.000000", "tags": ["redeem"]},
    }
