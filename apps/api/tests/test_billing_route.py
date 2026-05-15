from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Response

from app.routes import billing
from lumen_core.schemas import AdminRedemptionCodeCreateIn


def test_openai_price_import_uses_decimal_half_up_rounding() -> None:
    assert billing._openai_price_micro("0.0005", 1.0) == 1  # noqa: SLF001


def test_usage_by_kind_uses_cost_breakdown_and_rate_multiplier() -> None:
    row = SimpleNamespace(
        kind="charge",
        amount_micro=-25_000,
        ref_type="completion",
        created_at=datetime.now(timezone.utc),
        meta={
            "cost_breakdown": {
                "input_cost_micro": 10_000,
                "output_cost_micro": 20_000,
                "cache_read_cost_micro": 5_000,
                "cache_creation_cost_micro": 3_000,
                "image_output_cost_micro": 2_000,
                "reasoning_cost_micro": 1_000,
                "rate_multiplier_x10000": 5000,
            }
        },
    )

    out = billing._usage_by_kind([row])  # noqa: SLF001

    assert out.input == 5_000
    assert out.output == 10_000
    assert out.cache_read == 2_500
    assert out.cache_creation == 1_500
    assert out.image == 1_000
    assert out.reasoning == 500


def test_window_usage_reports_reset_from_oldest_in_window() -> None:
    now = datetime(2026, 5, 15, 12, tzinfo=timezone.utc)
    old = SimpleNamespace(
        kind="charge",
        amount_micro=-10_000,
        ref_type="completion",
        created_at=now - timedelta(hours=6),
        meta={"cost_micro": 10_000},
    )
    recent = SimpleNamespace(
        kind="charge",
        amount_micro=-20_000,
        ref_type="completion",
        created_at=now - timedelta(hours=2),
        meta={"cost_micro": 20_000},
    )

    out = billing._window_usage(  # noqa: SLF001
        [old, recent],
        now=now,
        span=timedelta(hours=5),
        limit_micro=100_000,
    )

    assert out.used_micro == 20_000
    assert out.limit_micro == 100_000
    assert out.resets_at == recent.created_at + timedelta(hours=5)


def test_bulk_multiplier_converts_to_x10000() -> None:
    assert (
        billing._bulk_multiplier_x10000(2.25, field="rates.long_context_input_multiplier")  # noqa: SLF001
        == 22_500
    )


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


class _MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deleted: list[str] = []

    async def set(self, key: str, value: str, *_args: Any, **_kwargs: Any) -> None:
        self.values[key] = value

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        self.values.pop(key, None)
        return 1


class _FailingSecondSetRedis(_MemoryRedis):
    def __init__(self) -> None:
        super().__init__()
        self.set_calls = 0

    async def set(self, key: str, value: str, *_args: Any, **_kwargs: Any) -> None:
        self.set_calls += 1
        if self.set_calls == 2:
            raise RuntimeError("second write failed")
        await super().set(key, value, *_args, **_kwargs)


class _ScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list[Any]:
        return self._values


@pytest.mark.asyncio
async def test_redemption_secret_missing_returns_actionable_412(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing_setting(_db: Any, _key: str) -> str | None:
        return None

    monkeypatch.setattr(billing, "_setting_raw", missing_setting)

    with pytest.raises(Exception) as excinfo:
        await billing._redemption_secret(object())  # noqa: SLF001

    assert getattr(excinfo.value, "status_code", None) == 412
    assert excinfo.value.detail["error"]["code"] == "REDEMPTION_SECRET_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_redemption_operational_gate_requires_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def disabled_setting(_db: Any, key: str) -> str | None:
        if key == "billing.enabled":
            return "0"
        if key == "billing.bootstrap_completed":
            return "1"
        return None

    monkeypatch.setattr(billing, "_setting_raw", disabled_setting)

    with pytest.raises(Exception) as excinfo:
        await billing._require_redemption_operational(object())  # noqa: SLF001

    assert getattr(excinfo.value, "status_code", None) == 412
    assert excinfo.value.detail["error"]["code"] == "BILLING_DISABLED"


@pytest.mark.asyncio
async def test_redemption_operational_gate_requires_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unbootstrapped_setting(_db: Any, key: str) -> str | None:
        if key == "billing.enabled":
            return "1"
        if key == "billing.bootstrap_completed":
            return "0"
        return None

    monkeypatch.setattr(billing, "_setting_raw", unbootstrapped_setting)

    with pytest.raises(Exception) as excinfo:
        await billing._require_redemption_operational(object())  # noqa: SLF001

    assert getattr(excinfo.value, "status_code", None) == 412
    assert excinfo.value.detail["error"]["code"] == "BOOTSTRAP_INCOMPLETE"


@pytest.mark.asyncio
async def test_create_redemption_codes_rolls_back_when_download_cache_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_secret(_db: Any) -> str:
        return "test-redemption-secret"

    async def fake_bootstrap(_db: Any) -> None:
        return None

    async def fake_write_audit(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(billing, "_redemption_secret", fake_secret)
    monkeypatch.setattr(billing, "_require_bootstrap_completed", fake_bootstrap)
    monkeypatch.setattr(billing, "write_audit", fake_write_audit)
    monkeypatch.setattr(billing, "request_ip_hash", lambda _request: "ip-hash")
    monkeypatch.setattr(billing, "get_redis", lambda: _FailingRedis())

    db = _Db()
    admin = SimpleNamespace(id="admin-1", email="admin@example.test")

    with pytest.raises(Exception) as excinfo:
        await billing.admin_create_redemption_codes(
            AdminRedemptionCodeCreateIn(amount_rmb="10", count=1),
            None,  # type: ignore[arg-type]
            Response(),
            admin,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 503
    assert excinfo.value.detail["error"]["code"] == "download_cache_unavailable"
    assert db.rolled_back is True
    assert db.committed is False


@pytest.mark.asyncio
async def test_create_redemption_codes_returns_plaintext_and_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_secret(_db: Any) -> str:
        return "test-redemption-secret"

    async def fake_bootstrap(_db: Any) -> None:
        return None

    async def fake_write_audit(*_args: Any, **_kwargs: Any) -> bool:
        return True

    redis = _MemoryRedis()
    monkeypatch.setattr(billing, "_redemption_secret", fake_secret)
    monkeypatch.setattr(billing, "_require_bootstrap_completed", fake_bootstrap)
    monkeypatch.setattr(billing, "write_audit", fake_write_audit)
    monkeypatch.setattr(billing, "request_ip_hash", lambda _request: "ip-hash")
    monkeypatch.setattr(billing, "get_redis", lambda: redis)

    db = _Db()
    admin = SimpleNamespace(id="admin-1", email="admin@example.test")
    response = Response()

    out = await billing.admin_create_redemption_codes(
        AdminRedemptionCodeCreateIn(amount_rmb="10", count=2),
        None,  # type: ignore[arg-type]
        response,
        admin,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out.count == 2
    assert len(out.plaintext_codes) == 2
    assert all(code.startswith("LMN-") for code in out.plaintext_codes)
    assert response.headers["Cache-Control"] == "no-store"
    assert any(key.startswith(billing._DOWNLOAD_TOKEN_PREFIX) for key in redis.values)  # noqa: SLF001
    assert any(key.startswith(billing._PLAINTEXT_BATCH_PREFIX) for key in redis.values)  # noqa: SLF001


@pytest.mark.asyncio
async def test_store_redemption_plaintext_batch_cleans_partial_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FailingSecondSetRedis()
    monkeypatch.setattr(billing, "get_redis", lambda: redis)

    with pytest.raises(RuntimeError, match="second write failed"):
        await billing._store_redemption_plaintext_batch(  # noqa: SLF001
            batch_id="batch-1",
            amount_micro=10_000_000,
            codes=["LMN-AAAA-BBBB-CCCC-DDDD"],
            expires_at=None,
        )

    assert redis.values == {}
    assert billing._PLAINTEXT_BATCH_PREFIX + "batch-1" in redis.deleted  # noqa: SLF001
    assert any(key.startswith(billing._DOWNLOAD_TOKEN_PREFIX) for key in redis.deleted)  # noqa: SLF001


@pytest.mark.asyncio
async def test_threshold_price_validation_treats_candidate_disable_as_missing() -> None:
    class Db:
        async def execute(self, *_args: Any, **_kwargs: Any) -> _ScalarResult:
            return _ScalarResult(["1k"])

    with pytest.raises(Exception) as excinfo:
        await billing._validate_thresholds_have_prices(  # noqa: SLF001
            Db(),  # type: ignore[arg-type]
            {"1k": 1_572_864},
            [
                {
                    "scope": "image_size",
                    "key": "1k",
                    "unit": "per_image",
                    "enabled": False,
                }
            ],
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert excinfo.value.detail["error"]["code"] == "THRESHOLDS_PRICING_MISMATCH"
    assert excinfo.value.detail["error"]["details"]["missing"] == ["1k"]
