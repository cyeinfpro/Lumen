from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import billing
from lumen_core.schemas import AdminRedemptionCodeCreateIn


def test_openai_price_import_uses_decimal_half_up_rounding() -> None:
    assert billing._openai_price_micro("0.0005", 1.0) == 1  # noqa: SLF001


class _Db:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FailingRedis:
    async def set(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("redis down")


@pytest.mark.asyncio
async def test_create_redemption_codes_rolls_back_when_download_cache_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_secret(_db: Any) -> str:
        return "test-redemption-secret"

    async def fake_write_audit(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(billing, "_redemption_secret", fake_secret)
    monkeypatch.setattr(billing, "write_audit", fake_write_audit)
    monkeypatch.setattr(billing, "request_ip_hash", lambda _request: "ip-hash")
    monkeypatch.setattr(billing, "get_redis", lambda: _FailingRedis())

    db = _Db()
    admin = SimpleNamespace(id="admin-1", email="admin@example.test")

    with pytest.raises(Exception) as excinfo:
        await billing.admin_create_redemption_codes(
            AdminRedemptionCodeCreateIn(amount_rmb="10", count=1),
            None,  # type: ignore[arg-type]
            admin,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 503
    assert excinfo.value.detail["error"]["code"] == "download_cache_unavailable"
    assert db.rolled_back is True
    assert db.committed is False
