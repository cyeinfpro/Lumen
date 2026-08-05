from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateTable

from app.routes import billing
from app.routes.billing_parts import composition as billing_composition
from app.routes.billing_parts import (
    orphan_hold_settlement as billing_orphan_settlement_routes,
)
from app.routes.billing_parts import overview as billing_overview_routes
from app.routes.billing_parts import pricing as billing_pricing_routes
from app.routes.billing_parts import redemptions as billing_redemption_routes
from app.routes.billing_parts import services as billing_services
from app.routes.billing_parts import wallets as billing_wallet_routes
from app.services import pricing_cache
from lumen_core import billing as billing_core
from lumen_core.models import (
    AuditLog,
    Base,
    Completion,
    Generation,
    UserWallet,
    VideoGeneration,
    WalletTransaction,
    WorkflowRun,
)
from lumen_core.schemas import (
    AdminBillingBootstrapIn,
    AdminRedemptionCodeCreateIn,
    AdminSetAccountModeIn,
    AdminWalletAdjustIn,
    RedemptionIn,
    WalletOut,
)


@pytest.fixture(autouse=True)
def _allow_redemption_active_user_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lock_snapshot(_db: Any, user: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            user=user,
            account_mode=getattr(user, "account_mode", "wallet"),
        )

    monkeypatch.setattr(
        billing_redemption_routes,
        "lock_authenticated_user_snapshot",
        lock_snapshot,
    )


def _request(
    method: str = "GET", headers: list[tuple[bytes, bytes]] | None = None
) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": headers or [],
            "client": ("127.0.0.1", 12345),
        }
    )


@asynccontextmanager
async def _wallet_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    UserWallet.__table__,
                    WalletTransaction.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _create_orphan_hold_route_tables(sync_connection: Any) -> None:
    for table in (
        UserWallet.__table__,
        WalletTransaction.__table__,
        AuditLog.__table__,
        Generation.__table__,
        Completion.__table__,
        VideoGeneration.__table__,
        WorkflowRun.__table__,
    ):
        ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        ddl = ddl.replace("DEFAULT (ARRAY[]::varchar[])", "DEFAULT '[]'")
        sync_connection.exec_driver_sql(ddl)


@asynccontextmanager
async def _orphan_hold_route_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(_create_orphan_hold_route_tables)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _wallet_tx(
    *,
    tx_id: str,
    user_id: str,
    kind: str,
    ref_type: str,
    ref_id: str,
    created_at: datetime,
) -> WalletTransaction:
    amount_micro = -1_000 if kind == "hold" else 1_000
    return WalletTransaction(
        id=tx_id,
        user_id=user_id,
        kind=kind,
        amount_micro=amount_micro,
        balance_after=9_000,
        hold_after=1_000 if kind == "hold" else 0,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=tx_id,
        meta={},
        created_at=created_at,
    )


async def _stale_prompt_operation(
    db: AsyncSession,
    *,
    user_id: str,
    idempotency_key: str,
    dispatched: bool,
    telegram: bool = False,
    stale: bool = True,
) -> Any:
    from app.routes.prompt_parts import idempotency

    if telegram:
        from app.routes import telegram_prompt_idempotency

        operation = telegram_prompt_idempotency.telegram_prompt_enhance_operation(
            user_id=user_id,
            idempotency_key=idempotency_key,
            chat_id="-100123",
            tg_user_id="42",
            text="cat",
        )
        reservation = await telegram_prompt_idempotency.reserve_telegram_prompt_enhance(
            db,
            operation,
        )
    else:
        operation = idempotency.prompt_enhance_operation(
            user_id=user_id,
            idempotency_key=idempotency_key,
            operation_namespace=idempotency.TEXT_PROMPT_ENHANCE_OPERATION,
            payload={"text": "cat"},
        )
        reservation = await idempotency.reserve_prompt_enhance_operation(db, operation)
    assert reservation.attempt is not None
    await idempotency.bind_billing_snapshot(
        db,
        operation,
        reservation.attempt,
        {
            "version": 1,
            "mode": "wallet",
            "request_id": operation.record_id,
            "user_id": user_id,
            "rate_multiplier_x10000": 10_000,
            "cache_aware": True,
            "allow_negative": False,
            "hold_amount_micro": 1_000,
            "pricing_snapshots": {},
        },
    )
    run = await db.get(WorkflowRun, operation.record_id)
    assert run is not None
    metadata = dict(run.metadata_jsonb)
    record = dict(metadata[operation.metadata_key])
    if stale:
        record["lease_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
    record["dispatch_inflight"] = dispatched
    record["upstream_cost_possible"] = dispatched
    metadata[operation.metadata_key] = record
    run.metadata_jsonb = metadata
    await db.commit()
    return operation


@pytest.mark.asyncio
async def test_admin_prompt_orphan_hold_rejects_active_attempt_recovery() -> None:
    now = datetime.now(timezone.utc)
    user_id = "user-prompt-orphan-active"
    hold_tx_id = "hold-prompt-orphan-active"

    async with _orphan_hold_route_session() as db:
        operation = await _stale_prompt_operation(
            db,
            user_id=user_id,
            idempotency_key="prompt-orphan-active",
            dispatched=False,
            stale=False,
        )
        db.add(UserWallet(user_id=user_id, balance_micro=9_000, hold_micro=1_000))
        db.add(
            _wallet_tx(
                tx_id=hold_tx_id,
                user_id=user_id,
                kind="hold",
                ref_type="prompt_enhance",
                ref_id=operation.record_id,
                created_at=now - timedelta(hours=2),
            )
        )
        await db.commit()

        listed = await billing.admin_list_orphan_holds(
            SimpleNamespace(id="admin-1"),
            db,
            min_age_minutes=60,
            limit=10,
        )
        with pytest.raises(HTTPException) as release_error:
            await billing.admin_release_orphan_hold(
                hold_tx_id,
                _request(method="POST"),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                db,
            )
        await db.rollback()
        with pytest.raises(HTTPException) as settle_error:
            await billing.admin_settle_orphan_prompt_hold(
                hold_tx_id,
                _request(method="POST"),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                db,
            )

    assert listed[0].recovery_action == "manual_review"
    assert release_error.value.detail["error"]["code"] == "HOLD_TASK_ACTIVE"
    assert settle_error.value.detail["error"]["code"] == "HOLD_TASK_ACTIVE"


def _hold_task(
    *,
    ref_type: str,
    task_id: str,
    user_id: str,
    status: str,
    now: datetime,
    proven_absent: bool = False,
    billing_retry_count: int = 0,
) -> Generation | Completion | VideoGeneration:
    dispatch_evidence: dict[str, Any] = {}
    if proven_absent:
        dispatch_evidence.update(
            {
                "upstream_dispatch_started_at": now.isoformat(),
                "upstream_dispatch_attempt": 1,
                "upstream_dispatch_execution_epoch": 0,
                "upstream_dispatch_delivery": "proven_undelivered",
            }
        )
    if ref_type == "generation":
        return Generation(
            id=task_id,
            message_id="message-1",
            user_id=user_id,
            action="generate",
            model="test-model",
            prompt="test prompt",
            size_requested="1024x1024",
            aspect_ratio="1:1",
            input_image_ids=[],
            upstream_request=dispatch_evidence or None,
            status=status,
            progress_stage="rendering",
            attempt=0,
            billing_retry_count=billing_retry_count,
            idempotency_key=f"idempotency-{task_id}",
        )
    if ref_type == "completion":
        if billing_retry_count > 0:
            dispatch_evidence["billing_retry_count"] = billing_retry_count
        return Completion(
            id=task_id,
            message_id="message-1",
            user_id=user_id,
            model="test-model",
            input_image_ids=[],
            upstream_request=dispatch_evidence or None,
            text="",
            tokens_in=0,
            tokens_out=0,
            status=status,
            progress_stage="streaming",
            attempt=0,
            idempotency_key=f"idempotency-{task_id}",
        )
    if ref_type == "video_generation":
        return VideoGeneration(
            id=task_id,
            user_id=user_id,
            action="generate",
            model="test-model",
            prompt="test prompt",
            duration_s=5,
            resolution="720p",
            aspect_ratio="16:9",
            diagnostics=(
                {"submit_delivery_state": "proven_absent"} if proven_absent else {}
            ),
            status=status,
            deadline_at=now + timedelta(hours=1),
            idempotency_key=f"idempotency-{task_id}",
            request_fingerprint="f" * 64,
            est_token_upper=1_000,
            est_cost_micro=1_000,
        )
    raise AssertionError(f"unsupported hold task type: {ref_type}")


def test_openai_price_import_uses_decimal_half_up_rounding() -> None:
    assert billing._openai_price_micro("0.0005", 1.0) == 1  # noqa: SLF001


def test_billing_route_composition_is_typed_and_static() -> None:
    services = billing.build_billing_services()

    assert isinstance(services, billing.BillingServices)
    assert isinstance(services.queries, billing.BillingQueries)
    assert isinstance(services.commands, billing.BillingCommands)

    source = "\n".join(
        [
            inspect.getsource(billing),
            inspect.getsource(billing_composition),
            inspect.getsource(billing_overview_routes),
            inspect.getsource(billing_pricing_routes),
            inspect.getsource(billing_redemption_routes),
            inspect.getsource(billing_wallet_routes),
        ]
    )
    assert "globals()" not in source
    assert "ContextVar" not in source
    assert "__getattr__" not in source
    assert "current_runtime" not in source


@pytest.mark.asyncio
async def test_wallet_route_accepts_typed_query_fake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = WalletOut(mode="byok", balance=None, hold=None, frozen=False)

    async def wallet_out(_db: Any, _user: Any) -> WalletOut:
        return expected

    services = billing.replace_billing_queries(
        billing.build_billing_services(),
        wallet_out=wallet_out,
    )
    monkeypatch.setattr(
        billing_wallet_routes,
        "build_billing_services",
        lambda: services,
    )

    out = await billing.get_my_wallet(
        SimpleNamespace(id="typed-fake-user", account_mode="byok"),
        object(),  # type: ignore[arg-type]
    )

    assert out is expected


def test_redemption_expiry_boundary_is_consistently_expired() -> None:
    now = datetime.now(timezone.utc)
    code = SimpleNamespace(
        revoked_at=None,
        expires_at=now,
        redeemed_count=0,
        max_redemptions=1,
    )

    assert billing._redemption_status(code, now=now) == "expired"  # noqa: SLF001
    overview_source = inspect.getsource(billing.admin_billing_overview)
    assert "RedemptionCode.expires_at > now" in overview_source


def test_admin_wallet_routes_exclude_soft_deleted_users() -> None:
    list_source = inspect.getsource(billing.admin_list_wallets)
    mode_source = inspect.getsource(billing.admin_set_account_mode)

    assert "User.deleted_at.is_(None)" in list_source
    assert "User.deleted_at.is_(None)" in mode_source


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


def test_usage_by_kind_classifies_settlements_by_ref_type() -> None:
    rows = [
        SimpleNamespace(
            kind="settle",
            amount_micro=-20_000,
            ref_type="completion",
            meta={"actual_micro": 20_000},
        ),
        SimpleNamespace(
            kind="settle",
            amount_micro=-30_000,
            ref_type="prompt_enhance",
            meta={"actual_micro": 30_000},
        ),
        SimpleNamespace(
            kind="settle",
            amount_micro=-40_000,
            ref_type="generation",
            meta={"actual_micro": 40_000},
        ),
    ]

    out = billing._usage_by_kind(rows)  # noqa: SLF001

    assert out.output == 50_000
    assert out.image == 40_000


def test_redemption_batch_legacy_idempotency_is_bounded_to_plaintext_window() -> None:
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)

    first = billing._redemption_batch_idempotency_key(  # noqa: SLF001
        None,
        admin_id="admin-1",
        request_hash="request-hash",
        now=now,
    )
    retry = billing._redemption_batch_idempotency_key(  # noqa: SLF001
        None,
        admin_id="admin-1",
        request_hash="request-hash",
        now=now + timedelta(seconds=299),
    )
    later = billing._redemption_batch_idempotency_key(  # noqa: SLF001
        None,
        admin_id="admin-1",
        request_hash="request-hash",
        now=now + timedelta(seconds=300),
    )

    assert retry == first
    assert later != first
    assert billing._redemption_batch_lock_identity(  # noqa: SLF001
        first, "request-hash"
    ) == billing._redemption_batch_lock_identity(  # noqa: SLF001
        later, "request-hash"
    )


def test_bulk_multiplier_converts_to_x10000() -> None:
    assert (
        billing._bulk_multiplier_x10000(
            2.25, field="rates.long_context_input_multiplier"
        )  # noqa: SLF001
        == 22_500
    )


def test_enabled_pricing_rejects_zero_billable_rate() -> None:
    with pytest.raises(HTTPException) as exc_info:
        billing._validate_enabled_pricing_value(  # noqa: SLF001
            unit="per_1k_tokens_in",
            price_micro=0,
            enabled=True,
            field="price_rmb",
        )

    assert exc_info.value.detail["error"]["code"] == "invalid_amount"


def test_zero_long_context_threshold_can_remain_enabled() -> None:
    billing._validate_enabled_pricing_value(  # noqa: SLF001
        unit="long_context_threshold",
        price_micro=0,
        enabled=True,
        field="rates.long_context_threshold",
    )


def test_pricing_group_rejects_mixed_priorities() -> None:
    with pytest.raises(HTTPException) as exc_info:
        billing._pricing_group_priorities(  # noqa: SLF001
            [
                {
                    "scope": "chat_model",
                    "key": "gpt-*",
                    "variant": "default",
                    "priority": 10,
                },
                {
                    "scope": "chat_model",
                    "key": "gpt-*",
                    "variant": "default",
                    "priority": 20,
                },
            ]
        )

    assert exc_info.value.detail["error"]["code"] == "pricing_priority_mismatch"


def test_wallet_search_escapes_like_wildcards() -> None:
    assert billing._escape_like_pattern(r"100%_\path") == r"100\%\_\\path"  # noqa: SLF001


def test_generated_redemption_secret_is_strong_and_random() -> None:
    first = billing._generate_redemption_secret()  # noqa: SLF001
    second = billing._generate_redemption_secret()  # noqa: SLF001

    assert len(first) >= 48
    assert first != second


@pytest.mark.asyncio
async def test_wallet_api_24h_activity_aggregates_all_rows_and_window_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    window_start = now - timedelta(hours=24)
    user_id = "user-aggregate"

    async def low_balance_threshold(_db: Any) -> int:
        return 2_000_000

    monkeypatch.setattr(
        billing_services, "_low_balance_threshold", low_balance_threshold
    )
    monkeypatch.setattr(billing_services, "_wallet_activity_window_end", lambda: now)

    rows = [
        WalletTransaction(
            id=f"tx-{index:04d}",
            user_id=user_id,
            kind="topup_redeem",
            amount_micro=1_000,
            balance_after=0,
            hold_after=0,
            idempotency_key=f"idempotency-{index:04d}",
            meta={},
            created_at=now - timedelta(hours=1, seconds=index),
        )
        for index in range(35)
    ]
    rows.extend(
        [
            WalletTransaction(
                id="tx-window-start",
                user_id=user_id,
                kind="topup_redeem",
                amount_micro=2_000,
                balance_after=0,
                hold_after=0,
                idempotency_key="idempotency-window-start",
                meta={},
                created_at=window_start,
            ),
            WalletTransaction(
                id="tx-before-window",
                user_id=user_id,
                kind="topup_redeem",
                amount_micro=500_000,
                balance_after=0,
                hold_after=0,
                idempotency_key="idempotency-before-window",
                meta={},
                created_at=window_start - timedelta(microseconds=1),
            ),
            WalletTransaction(
                id="tx-inside-spend",
                user_id=user_id,
                kind="charge",
                amount_micro=-4_000,
                balance_after=0,
                hold_after=0,
                idempotency_key="idempotency-inside-spend",
                meta={},
                created_at=now - timedelta(hours=2),
            ),
            WalletTransaction(
                id="tx-window-end",
                user_id=user_id,
                kind="charge",
                amount_micro=-3_000,
                balance_after=0,
                hold_after=0,
                idempotency_key="idempotency-window-end",
                meta={},
                created_at=now,
            ),
            WalletTransaction(
                id="tx-future",
                user_id=user_id,
                kind="charge",
                amount_micro=-900_000,
                balance_after=0,
                hold_after=0,
                idempotency_key="idempotency-future",
                meta={},
                created_at=now + timedelta(microseconds=1),
            ),
        ]
    )

    async with _wallet_session() as db:
        db.add(
            UserWallet(
                user_id=user_id,
                balance_micro=123_000,
                hold_micro=0,
            )
        )
        db.add_all(rows)
        await db.flush()

        first_page = await billing.list_my_wallet_transactions(
            SimpleNamespace(id=user_id, account_mode="wallet"),
            db,
            limit=30,
            cursor=None,
            kind=None,
        )
        wallet = await billing.get_my_wallet(
            SimpleNamespace(id=user_id, account_mode="wallet"),
            db,
        )

    assert len(first_page.items) == 30
    assert first_page.next_cursor is not None
    assert wallet.activity_24h.topup.micro == 37_000
    assert wallet.activity_24h.spend.micro == 7_000


@pytest.mark.asyncio
async def test_byok_wallet_activity_has_explicit_zero_semantics() -> None:
    out = await billing._wallet_out(  # noqa: SLF001
        _Db(),
        SimpleNamespace(id="byok-user", account_mode="byok"),
    )

    assert out.mode == "byok"
    assert out.balance is None
    assert out.activity_24h.topup.micro == 0
    assert out.activity_24h.spend.micro == 0


class _Db:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flush_calls: list[list[Any] | None] = []
        self.committed = False
        self.rolled_back = False

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        return _ScalarOneOrNoneResult(None)

    async def flush(self, values: list[Any] | None = None) -> None:
        self.flush_calls.append(values)

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


class _FailingDeleteRedis(_MemoryRedis):
    async def delete(self, key: str) -> int:
        raise RuntimeError(f"delete failed: {key}")


class _ScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list[Any]:
        return self._values


class _ScalarOneOrNoneResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FirstResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def first(self) -> Any:
        if self.value is None:
            return None
        return _Row(self.value)


class _Row:
    def __init__(self, value: Any) -> None:
        self._value = value
        if isinstance(value, tuple):
            self._mapping = {idx: item for idx, item in enumerate(value)}
        else:
            self._mapping = {0: value}

    def __iter__(self):
        if isinstance(self._value, tuple):
            return iter(self._value)
        return iter((self._value,))


class _FirstDb:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def execute(self, *_args: Any, **_kwargs: Any) -> _FirstResult:
        return _FirstResult(self.value)


@pytest.mark.asyncio
async def test_wallet_audit_uses_database_window_and_limits_mismatch_rows() -> None:
    statements: list[str] = []

    class StatsResult:
        def one(self) -> tuple[int, int, int]:
            return 4, 2, 1

    class MismatchResult:
        def all(self) -> list[tuple[str, str, str, int, int]]:
            return [("user-1", "tx-2", "charge", 75, 70)]

    class Db:
        async def execute(self, stmt: Any) -> Any:
            statements.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
            return StatsResult() if len(statements) == 1 else MismatchResult()

    out = await billing.admin_wallet_audit(
        SimpleNamespace(id="admin-1"),
        Db(),  # type: ignore[arg-type]
        user_id="user-1",
        limit=1,
    )

    assert out.transactions == 4
    assert out.users == 2
    assert out.mismatch_count == 1
    assert out.mismatches == [
        "user=user-1 tx=tx-2 kind=charge running=75 balance_after=70"
    ]
    assert "OVER (PARTITION BY wallet_transactions.user_id" in statements[0]
    assert "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW" in statements[0]
    assert "wallet_transactions.user_id = 'user-1'" in statements[0]
    assert "LIMIT 1" in statements[1]


@pytest.mark.asyncio
async def test_admin_list_orphan_holds_finds_hold_beyond_consumed_window() -> None:
    now = datetime.now(timezone.utc)
    base = now - timedelta(hours=3)
    user_id = "user-orphan-window"
    rows: list[WalletTransaction] = []
    for index in range(5):
        ref_id = f"consumed-{index}"
        rows.extend(
            [
                _wallet_tx(
                    tx_id=f"hold-consumed-{index}",
                    user_id=user_id,
                    kind="hold",
                    ref_type="generation",
                    ref_id=ref_id,
                    created_at=base + timedelta(seconds=index),
                ),
                _wallet_tx(
                    tx_id=f"release-consumed-{index}",
                    user_id=user_id,
                    kind="release",
                    ref_type="generation",
                    ref_id=ref_id,
                    created_at=base + timedelta(minutes=1, seconds=index),
                ),
            ]
        )
    rows.append(
        _wallet_tx(
            tx_id="hold-orphan",
            user_id=user_id,
            kind="hold",
            ref_type="generation",
            ref_id="orphan-ref:retry:1",
            created_at=base + timedelta(seconds=10),
        )
    )

    async with _wallet_session() as db:
        db.add(UserWallet(user_id=user_id, balance_micro=9_000, hold_micro=1_000))
        db.add_all(rows)
        await db.commit()

        out = await billing.admin_list_orphan_holds(
            SimpleNamespace(id="admin-1"),
            db,
            min_age_minutes=60,
            limit=2,
        )

    assert [item.tx.id for item in out] == ["hold-orphan"]
    assert out[0].recovery_action == "release"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ref_type", "status"),
    [
        ("generation", "queued"),
        ("generation", "running"),
        ("completion", "queued"),
        ("completion", "streaming"),
        ("video_generation", "queued"),
        ("video_generation", "submitting"),
        ("video_generation", "submit_unknown"),
        ("video_generation", "submitted"),
        ("video_generation", "running"),
    ],
)
async def test_admin_release_orphan_hold_rejects_active_task(
    monkeypatch: pytest.MonkeyPatch,
    ref_type: str,
    status: str,
) -> None:
    now = datetime.now(timezone.utc)
    user_id = "user-active-hold"
    task_id = "task-active"
    hold_ref_id = task_id if ref_type == "video_generation" else f"{task_id}:retry:1"

    async def fail_release(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("active task hold must not be released")

    monkeypatch.setattr(billing_core, "release", fail_release)

    async with _orphan_hold_route_session() as db:
        db.add(
            _wallet_tx(
                tx_id="hold-active",
                user_id=user_id,
                kind="hold",
                ref_type=ref_type,
                ref_id=hold_ref_id,
                created_at=now - timedelta(hours=2),
            )
        )
        db.add(
            _hold_task(
                ref_type=ref_type,
                task_id=task_id,
                user_id=user_id,
                status=status,
                now=now,
                billing_retry_count=(0 if ref_type == "video_generation" else 1),
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as excinfo:
            await billing.admin_release_orphan_hold(
                "hold-active",
                _request(method="POST"),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                db,
            )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"]["code"] == "HOLD_TASK_ACTIVE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ref_type", "status"),
    [
        ("generation", "succeeded"),
        ("generation", "failed"),
        ("generation", "canceled"),
        ("completion", "succeeded"),
        ("completion", "failed"),
        ("completion", "canceled"),
        ("video_generation", "succeeded"),
        ("video_generation", "failed"),
        ("video_generation", "canceled"),
        ("video_generation", "expired"),
        ("external_task", None),
    ],
)
async def test_admin_release_orphan_hold_rejects_without_proven_absent_evidence(
    monkeypatch: pytest.MonkeyPatch,
    ref_type: str,
    status: str | None,
) -> None:
    now = datetime.now(timezone.utc)
    user_id = "user-unsafe-hold"
    task_id = "task-unsafe"
    hold_ref_id = (
        f"{task_id}:retry:1"
        if status is not None and ref_type in {"generation", "completion"}
        else task_id
    )

    async def fail_release(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("unsafe hold must not be released")

    monkeypatch.setattr(billing_core, "release", fail_release)

    async with _orphan_hold_route_session() as db:
        db.add(
            _wallet_tx(
                tx_id="hold-unsafe",
                user_id=user_id,
                kind="hold",
                ref_type=ref_type,
                ref_id=hold_ref_id,
                created_at=now - timedelta(hours=2),
            )
        )
        if status is not None:
            db.add(
                _hold_task(
                    ref_type=ref_type,
                    task_id=task_id,
                    user_id=user_id,
                    status=status,
                    now=now,
                    billing_retry_count=(
                        1 if ref_type in {"generation", "completion"} else 0
                    ),
                )
            )
        await db.commit()

        with pytest.raises(HTTPException) as excinfo:
            await billing.admin_release_orphan_hold(
                "hold-unsafe",
                _request(method="POST"),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                db,
            )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"]["code"] == "HOLD_RELEASE_NOT_PROVEN_SAFE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ref_type", "status", "expected_proof"),
    [
        ("generation", "failed", "upstream_dispatch:proven_undelivered"),
        ("completion", "canceled", "upstream_dispatch:proven_undelivered"),
        (
            "video_generation",
            "expired",
            "submit_delivery_state:proven_absent",
        ),
    ],
)
async def test_admin_release_orphan_hold_is_audited_idempotent_and_attributed(
    monkeypatch: pytest.MonkeyPatch,
    ref_type: str,
    status: str,
    expected_proof: str,
) -> None:
    now = datetime.now(timezone.utc)
    user_id = f"user-release-{ref_type}"
    task_id = f"task-release-{ref_type}"
    hold_tx_id = f"hold-release-{ref_type}"
    hold_ref_id = (
        f"{task_id}:retry:1" if ref_type in {"generation", "completion"} else task_id
    )
    audit_calls: list[dict[str, Any]] = []
    invalidations: list[str] = []

    async def write_audit(*_args: Any, **kwargs: Any) -> bool:
        audit_calls.append(kwargs)
        db = _args[0]
        db.add(
            AuditLog(
                user_id=kwargs.get("user_id"),
                event_type=kwargs["event_type"],
                actor_email_hash=kwargs.get("actor_email_hash"),
                actor_ip_hash=kwargs.get("actor_ip_hash"),
                target_user_id=kwargs.get("target_user_id"),
                details=kwargs.get("details") or {},
            )
        )
        await db.flush()
        return True

    async def invalidate_balance_cache(target_user_id: str) -> None:
        invalidations.append(target_user_id)

    services = billing.replace_billing_queries(
        billing.build_billing_services(),
        tx_out=lambda tx: tx,
    )
    services = billing.replace_billing_commands(
        services,
        write_audit=write_audit,
        invalidate_balance_cache=invalidate_balance_cache,
        request_ip_hash=lambda _request: "ip-hash",
    )
    monkeypatch.setattr(
        billing_overview_routes,
        "build_billing_services",
        lambda: services,
    )

    async with _orphan_hold_route_session() as db:
        secondary_hold_tx_id = f"{hold_tx_id}-secondary"
        db.add(UserWallet(user_id=user_id, balance_micro=8_000, hold_micro=2_000))
        db.add_all(
            [
                _wallet_tx(
                    tx_id=hold_tx_id,
                    user_id=user_id,
                    kind="hold",
                    ref_type=ref_type,
                    ref_id=hold_ref_id,
                    created_at=now - timedelta(hours=2),
                ),
                _wallet_tx(
                    tx_id=secondary_hold_tx_id,
                    user_id=user_id,
                    kind="hold",
                    ref_type=ref_type,
                    ref_id=hold_ref_id,
                    created_at=now - timedelta(hours=1, minutes=59),
                ),
            ]
        )
        db.add(
            _hold_task(
                ref_type=ref_type,
                task_id=task_id,
                user_id=user_id,
                status=status,
                now=now,
                proven_absent=True,
                billing_retry_count=(
                    1 if ref_type in {"generation", "completion"} else 0
                ),
            )
        )
        await db.commit()

        first = await billing.admin_release_orphan_hold(
            hold_tx_id,
            _request(method="POST"),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            db,
        )
        replay = await billing.admin_release_orphan_hold(
            hold_tx_id,
            _request(method="POST"),
            SimpleNamespace(id="admin-2", email="other-admin@example.test"),
            db,
        )

    assert first.id == replay.id
    assert first.kind == "release"
    assert first.idempotency_key == f"admin_release_hold:{hold_tx_id}"
    assert first.created_by_admin == "admin-1"
    assert first.amount_micro == 2_000
    assert first.meta["release_proof"] == expected_proof
    assert first.meta["hold_tx_ids"] == [hold_tx_id, secondary_hold_tx_id]
    assert first.meta["aggregate_held_micro"] == 2_000
    assert len(audit_calls) == 1
    assert audit_calls[0]["details"]["release_proof"] == expected_proof
    assert audit_calls[0]["details"]["hold_tx_ids"] == [
        hold_tx_id,
        secondary_hold_tx_id,
    ]
    assert invalidations == [user_id, user_id]


@pytest.mark.asyncio
async def test_admin_release_replay_repairs_missing_legacy_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    user_id = "user-release-audit-repair"
    hold_tx_id = "hold-release-audit-repair"
    ref_id = "prompt-release-audit-repair"

    async def invalidate_balance_cache(_target_user_id: str) -> None:
        return None

    services = billing.replace_billing_queries(
        billing.build_billing_services(),
        tx_out=lambda tx: tx,
    )
    services = billing.replace_billing_commands(
        services,
        invalidate_balance_cache=invalidate_balance_cache,
    )
    monkeypatch.setattr(
        billing_overview_routes,
        "build_billing_services",
        lambda: services,
    )

    async with _orphan_hold_route_session() as db:
        hold = _wallet_tx(
            tx_id=hold_tx_id,
            user_id=user_id,
            kind="hold",
            ref_type="prompt_enhance",
            ref_id=ref_id,
            created_at=now - timedelta(hours=2),
        )
        release = WalletTransaction(
            id="release-audit-repair",
            user_id=user_id,
            kind="release",
            amount_micro=1_000,
            balance_after=10_000,
            hold_after=0,
            ref_type="prompt_enhance",
            ref_id=ref_id,
            idempotency_key=f"admin_release_hold:{hold_tx_id}",
            meta={
                "hold_tx_id": hold_tx_id,
                "hold_tx_ids": [hold_tx_id],
                "hold_count": 1,
                "aggregate_held_micro": 1_000,
                "release_proof": "legacy-unrecorded",
            },
            created_at=now - timedelta(hours=1),
        )
        db.add(UserWallet(user_id=user_id, balance_micro=10_000, hold_micro=0))
        db.add_all([hold, release])
        await db.commit()

        replay = await billing.admin_release_orphan_hold(
            hold_tx_id,
            _request(method="POST"),
            SimpleNamespace(id="admin-repair", email="repair@example.test"),
            db,
        )
        audit_row = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.event_type == "wallet.hold.force_release"
                )
            )
        ).scalar_one()

    assert replay.id == release.id
    assert audit_row.user_id == "admin-repair"
    assert audit_row.target_user_id == user_id
    assert audit_row.details["release_tx_id"] == release.id
    assert audit_row.details["audit_recovery"] is True
    assert audit_row.details["release_proof"] == "legacy-unrecorded"


@pytest.mark.asyncio
@pytest.mark.parametrize("telegram", [False, True])
async def test_admin_prompt_orphan_hold_releases_with_fenced_no_dispatch_evidence(
    monkeypatch: pytest.MonkeyPatch,
    telegram: bool,
) -> None:
    now = datetime.now(timezone.utc)
    suffix = "telegram" if telegram else "web"
    user_id = f"user-prompt-orphan-release-{suffix}"
    hold_tx_id = f"hold-prompt-orphan-release-{suffix}"

    async def write_audit(db: AsyncSession, **kwargs: Any) -> bool:
        db.add(
            AuditLog(
                user_id=kwargs.get("user_id"),
                event_type=kwargs["event_type"],
                actor_email_hash=kwargs.get("actor_email_hash"),
                actor_ip_hash=kwargs.get("actor_ip_hash"),
                target_user_id=kwargs.get("target_user_id"),
                details=kwargs.get("details") or {},
            )
        )
        await db.flush()
        return True

    services = billing.build_billing_services()
    services = billing.replace_billing_commands(
        services,
        write_audit=write_audit,
        invalidate_balance_cache=lambda _user_id: asyncio.sleep(0),
        request_ip_hash=lambda _request: "ip-hash",
    )
    monkeypatch.setattr(
        billing_overview_routes,
        "build_billing_services",
        lambda: services,
    )
    monkeypatch.setattr(
        billing_orphan_settlement_routes,
        "build_billing_services",
        lambda: services,
    )

    async with _orphan_hold_route_session() as db:
        operation = await _stale_prompt_operation(
            db,
            user_id=user_id,
            idempotency_key=f"prompt-orphan-before-dispatch-{suffix}",
            dispatched=False,
            telegram=telegram,
        )
        db.add(UserWallet(user_id=user_id, balance_micro=9_000, hold_micro=1_000))
        db.add(
            _wallet_tx(
                tx_id=hold_tx_id,
                user_id=user_id,
                kind="hold",
                ref_type="prompt_enhance",
                ref_id=operation.record_id,
                created_at=now - timedelta(hours=2),
            )
        )
        await db.commit()

        listed = await billing.admin_list_orphan_holds(
            SimpleNamespace(id="admin-1"),
            db,
            min_age_minutes=60,
            limit=10,
        )
        with pytest.raises(HTTPException) as settle_error:
            await billing.admin_settle_orphan_prompt_hold(
                hold_tx_id,
                _request(method="POST"),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                db,
            )
        await db.rollback()
        release = await billing.admin_release_orphan_hold(
            hold_tx_id,
            _request(method="POST"),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            db,
        )
        wallet = await db.get(UserWallet, user_id)
        operation_run = await db.get(WorkflowRun, operation.record_id)

    assert listed[0].recovery_action == "release"
    assert settle_error.value.detail["error"]["code"] == (
        "HOLD_SETTLEMENT_NOT_RECOMMENDED"
    )
    assert release.kind == "release"
    assert release.meta["release_proof"] == (
        "prompt_operation:attempt_fenced_no_dispatch"
    )
    assert wallet is not None
    assert wallet.balance_micro == 10_000
    assert wallet.hold_micro == 0
    assert operation_run is not None
    assert operation_run.status == "failed"
    operation_record = operation_run.metadata_jsonb[operation.metadata_key]
    assert operation_record["state"] == "failed"
    assert operation_record["finalization"]["billing_action"] == "release"
    assert operation_record["response_chunks"] == [
        'data: {"error": "idempotency_orphan_hold_released"}\n\n'
    ]


@pytest.mark.asyncio
async def test_admin_prompt_orphan_hold_uses_default_settlement_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    user_id = "user-prompt-orphan-settle"
    hold_tx_id = "hold-prompt-orphan-settle"
    audit_calls: list[dict[str, Any]] = []
    invalidations: list[str] = []

    async def write_audit(db: AsyncSession, **kwargs: Any) -> bool:
        audit_calls.append(kwargs)
        db.add(
            AuditLog(
                user_id=kwargs.get("user_id"),
                event_type=kwargs["event_type"],
                actor_email_hash=kwargs.get("actor_email_hash"),
                actor_ip_hash=kwargs.get("actor_ip_hash"),
                target_user_id=kwargs.get("target_user_id"),
                details=kwargs.get("details") or {},
            )
        )
        await db.flush()
        return True

    async def invalidate_balance_cache(target_user_id: str) -> None:
        invalidations.append(target_user_id)

    services = billing.replace_billing_queries(
        billing.build_billing_services(),
        tx_out=lambda tx: tx,
    )
    services = billing.replace_billing_commands(
        services,
        write_audit=write_audit,
        invalidate_balance_cache=invalidate_balance_cache,
        request_ip_hash=lambda _request: "ip-hash",
    )
    monkeypatch.setattr(
        billing_orphan_settlement_routes,
        "build_billing_services",
        lambda: services,
    )

    async with _orphan_hold_route_session() as db:
        operation = await _stale_prompt_operation(
            db,
            user_id=user_id,
            idempotency_key="prompt-orphan-post-dispatch",
            dispatched=True,
        )
        ref_id = operation.record_id
        db.add(UserWallet(user_id=user_id, balance_micro=9_000, hold_micro=1_000))
        db.add(
            _wallet_tx(
                tx_id=hold_tx_id,
                user_id=user_id,
                kind="hold",
                ref_type="prompt_enhance",
                ref_id=ref_id,
                created_at=now - timedelta(hours=2),
            )
        )
        await db.commit()

        listed = await billing.admin_list_orphan_holds(
            SimpleNamespace(id="admin-1"),
            db,
            min_age_minutes=60,
            limit=10,
        )
        with pytest.raises(HTTPException) as release_error:
            await billing.admin_release_orphan_hold(
                hold_tx_id,
                _request(method="POST"),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                db,
            )
        first = await billing.admin_settle_orphan_prompt_hold(
            hold_tx_id,
            _request(method="POST"),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            db,
        )
        replay = await billing.admin_settle_orphan_prompt_hold(
            hold_tx_id,
            _request(method="POST"),
            SimpleNamespace(id="admin-2", email="other-admin@example.test"),
            db,
        )
        wallet = await db.get(UserWallet, user_id)
        operation_run = await db.get(WorkflowRun, operation.record_id)
        settlements = (
            (
                await db.execute(
                    select(WalletTransaction).where(
                        WalletTransaction.idempotency_key
                        == f"admin_settle_hold:{hold_tx_id}"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert listed[0].recovery_action == "settle_default"
    assert release_error.value.status_code == 409
    assert release_error.value.detail["error"]["code"] == (
        "HOLD_RELEASE_NOT_PROVEN_SAFE"
    )
    assert first.id == replay.id
    assert first.kind == "settle"
    assert first.amount_micro == 0
    assert first.created_by_admin == "admin-1"
    assert first.meta["actual_micro"] == 1_000
    assert first.meta["settlement_basis"] == "aggregate_held_micro"
    assert first.meta["recovery_proof"] == (
        "prompt_operation:dispatch_or_cost_possible"
    )
    assert wallet is not None
    assert wallet.balance_micro == 9_000
    assert wallet.hold_micro == 0
    assert operation_run is not None
    assert operation_run.status == "failed"
    operation_record = operation_run.metadata_jsonb[operation.metadata_key]
    assert operation_record["state"] == "failed"
    assert operation_record["finalization"]["billing_action"] == "settle_default"
    assert operation_record["response_chunks"] == [
        'data: {"error": "idempotency_orphan_hold_settled"}\n\n'
    ]
    assert len(settlements) == 1
    assert len(audit_calls) == 1
    assert audit_calls[0]["event_type"] == "wallet.hold.force_settle"
    assert audit_calls[0]["details"]["aggregate_held_micro"] == 1_000
    assert invalidations == [user_id, user_id]


@pytest.mark.asyncio
async def test_admin_release_orphan_hold_rejects_concurrent_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    user_id = "user-concurrent-settle"
    task_id = "task-concurrent-settle"
    audit_called = False

    async def settle_won(*_args: Any, **_kwargs: Any) -> WalletTransaction:
        return _wallet_tx(
            tx_id="settle-won",
            user_id=user_id,
            kind="settle",
            ref_type="generation",
            ref_id=f"{task_id}:retry:1",
            created_at=now,
        )

    async def fail_audit(*_args: Any, **_kwargs: Any) -> bool:
        nonlocal audit_called
        audit_called = True
        return True

    services = billing.replace_billing_commands(
        billing.build_billing_services(),
        write_audit=fail_audit,
    )
    monkeypatch.setattr(
        billing_overview_routes,
        "build_billing_services",
        lambda: services,
    )
    monkeypatch.setattr(billing_core, "release", settle_won)

    async with _orphan_hold_route_session() as db:
        db.add(
            _wallet_tx(
                tx_id="hold-concurrent",
                user_id=user_id,
                kind="hold",
                ref_type="generation",
                ref_id=f"{task_id}:retry:1",
                created_at=now - timedelta(hours=2),
            )
        )
        db.add(
            _hold_task(
                ref_type="generation",
                task_id=task_id,
                user_id=user_id,
                status="failed",
                now=now,
                proven_absent=True,
                billing_retry_count=1,
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as excinfo:
            await billing.admin_release_orphan_hold(
                "hold-concurrent",
                _request(method="POST"),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                db,
            )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"]["code"] == "HOLD_ALREADY_CONSUMED"
    assert audit_called is False


@pytest.mark.asyncio
async def test_admin_release_orphan_hold_rolls_back_when_audit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    user_id = "user-audit-failure"
    task_id = "task-audit-failure"

    async def fail_audit(*_args: Any, **_kwargs: Any) -> bool:
        from app.audit import AuditPersistenceError

        raise AuditPersistenceError("wallet.hold.force_release")

    services = billing.replace_billing_commands(
        billing.build_billing_services(),
        write_audit=fail_audit,
    )
    monkeypatch.setattr(
        billing_overview_routes,
        "build_billing_services",
        lambda: services,
    )

    async with _orphan_hold_route_session() as db:
        db.add(UserWallet(user_id=user_id, balance_micro=9_000, hold_micro=1_000))
        db.add(
            _wallet_tx(
                tx_id="hold-audit-failure",
                user_id=user_id,
                kind="hold",
                ref_type="generation",
                ref_id=f"{task_id}:retry:1",
                created_at=now - timedelta(hours=2),
            )
        )
        db.add(
            _hold_task(
                ref_type="generation",
                task_id=task_id,
                user_id=user_id,
                status="failed",
                now=now,
                proven_absent=True,
                billing_retry_count=1,
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as excinfo:
            await billing.admin_release_orphan_hold(
                "hold-audit-failure",
                _request(method="POST"),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                db,
            )

        wallet = await db.get(UserWallet, user_id)
        releases = (
            (
                await db.execute(
                    select(WalletTransaction).where(
                        WalletTransaction.idempotency_key
                        == "admin_release_hold:hold-audit-failure"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error"]["code"] == "AUDIT_WRITE_FAILED"
    assert wallet is not None
    assert wallet.balance_micro == 9_000
    assert wallet.hold_micro == 1_000
    assert releases == []


@pytest.mark.asyncio
async def test_admin_release_orphan_hold_rejects_retry_evidence_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    user_id = "user-retry-mismatch"
    task_id = "task-retry-mismatch"

    async def fail_release(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("older retry hold must not use current retry evidence")

    monkeypatch.setattr(billing_core, "release", fail_release)

    async with _orphan_hold_route_session() as db:
        db.add(
            _wallet_tx(
                tx_id="hold-retry-mismatch",
                user_id=user_id,
                kind="hold",
                ref_type="generation",
                ref_id=f"{task_id}:retry:1",
                created_at=now - timedelta(hours=2),
            )
        )
        db.add(
            _hold_task(
                ref_type="generation",
                task_id=task_id,
                user_id=user_id,
                status="failed",
                now=now,
                proven_absent=True,
                billing_retry_count=2,
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as excinfo:
            await billing.admin_release_orphan_hold(
                "hold-retry-mismatch",
                _request(method="POST"),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                db,
            )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"]["code"] == "HOLD_RELEASE_EVIDENCE_MISMATCH"


@pytest.mark.asyncio
async def test_admin_release_orphan_hold_rejects_confirmed_video_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    user_id = "user-video-confirmed"
    task_id = "task-video-confirmed"

    async def fail_release(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("confirmed video submission must not be released")

    monkeypatch.setattr(billing_core, "release", fail_release)

    async with _orphan_hold_route_session() as db:
        db.add(
            _wallet_tx(
                tx_id="hold-video-confirmed",
                user_id=user_id,
                kind="hold",
                ref_type="video_generation",
                ref_id=task_id,
                created_at=now - timedelta(hours=2),
            )
        )
        task = _hold_task(
            ref_type="video_generation",
            task_id=task_id,
            user_id=user_id,
            status="failed",
            now=now,
            proven_absent=True,
        )
        task.provider_task_id = "provider-confirmed-task"
        task.upstream_response = {"submit_delivery_state": "proven_absent"}
        db.add(task)
        await db.commit()

        with pytest.raises(HTTPException) as excinfo:
            await billing.admin_release_orphan_hold(
                "hold-video-confirmed",
                _request(method="POST"),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                db,
            )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"]["code"] == "HOLD_RELEASE_NOT_PROVEN_SAFE"


@pytest.mark.asyncio
async def test_credential_windows_use_persisted_credential_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, int, str]] = []
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)

    class Service:
        def __init__(self, redis: Any | None = None) -> None:
            assert redis is None

        async def ledger_window_usage(
            self,
            _db: Any,
            credential_id: str,
            window: str,
            *,
            limit_micro: int,
            now: datetime,
            user_id: str,
        ) -> Any:
            calls.append((credential_id, window, limit_micro, user_id))
            return SimpleNamespace(
                used_micro={"5h": 5, "1d": 10, "7d": 20}[window],
                limit_micro=limit_micro,
                resets_at=now + timedelta(hours=1),
            )

    monkeypatch.setattr(billing_services, "BillingCacheService", Service)

    windows = await billing._credential_windows(  # noqa: SLF001
        object(),  # type: ignore[arg-type]
        user_id="user-1",
        credential_id="cred-1",
        limits={"5h": 50, "1d": 100, "7d": 200},
        now=now,
    )

    assert windows["5h"].used_micro == 5
    assert windows["1d"].used_micro == 10
    assert windows["7d"].used_micro == 20
    assert calls == [
        ("cred-1", "5h", 50, "user-1"),
        ("cred-1", "1d", 100, "user-1"),
        ("cred-1", "7d", 200, "user-1"),
    ]


@pytest.mark.asyncio
async def test_billing_balance_respects_disabled_redis_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cache:
        async def get_balance(self, *_args: Any, **_kwargs: Any) -> int:
            raise AssertionError("shared redis cache must be bypassed when disabled")

    class Result:
        def scalar_one_or_none(self) -> int:
            return 321

    class Db:
        async def execute(self, *_args: Any, **_kwargs: Any) -> Result:
            return Result()

    async def setting_raw(_db: Any, key: str) -> str | None:
        if key == "billing.use_redis_cache":
            return "0"
        return None

    monkeypatch.setattr(billing_services, "_billing_cache", lambda: Cache())
    monkeypatch.setattr(billing_services, "_setting_raw", setting_raw)

    assert await billing._billing_balance_micro(Db(), "user-1") == 321  # noqa: SLF001


@pytest.mark.asyncio
async def test_redemption_secret_missing_returns_actionable_412(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing_setting(_db: Any, _key: str) -> str | None:
        return None

    monkeypatch.setattr(billing_services, "_setting_raw", missing_setting)

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

    monkeypatch.setattr(billing_services, "_setting_raw", disabled_setting)

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

    monkeypatch.setattr(billing_services, "_setting_raw", unbootstrapped_setting)

    with pytest.raises(Exception) as excinfo:
        await billing._require_redemption_operational(object())  # noqa: SLF001

    assert getattr(excinfo.value, "status_code", None) == 412
    assert excinfo.value.detail["error"]["code"] == "BOOTSTRAP_INCOMPLETE"


@pytest.mark.asyncio
async def test_billing_bootstrap_rejects_negative_low_balance_threshold() -> None:
    with pytest.raises(Exception) as excinfo:
        await billing.admin_billing_bootstrap(
            AdminBillingBootstrapIn(low_balance_warn_rmb="-0.01"),
            _request(method="POST"),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            object(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_amount"


@pytest.mark.asyncio
async def test_billing_bootstrap_rejects_negative_image_price() -> None:
    with pytest.raises(Exception) as excinfo:
        await billing.admin_billing_bootstrap(
            AdminBillingBootstrapIn(
                image_size_thresholds={"1k": 1_572_864},
                image_prices_rmb={"1k": "-0.01"},
            ),
            _request(method="POST"),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            object(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_amount"


@pytest.mark.asyncio
async def test_billing_bootstrap_rejects_zero_or_missing_enabled_tier_price() -> None:
    for prices in ({"1k": "0"}, {}):
        with pytest.raises(Exception) as excinfo:
            await billing.admin_billing_bootstrap(
                AdminBillingBootstrapIn(
                    image_size_thresholds={"1k": 1_572_864},
                    image_prices_rmb=prices,
                ),
                _request(method="POST"),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                object(),  # type: ignore[arg-type]
            )

        assert getattr(excinfo.value, "status_code", None) == 422


@pytest.mark.asyncio
async def test_wildcard_pricing_update_invalidates_all_resolved_model_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def scan_iter(self, *, match: str):
            assert match == "lumen:pricing:v1:*"
            for key in (
                "lumen:pricing:v1:default:gpt-5.4",
                "lumen:pricing:v1:priority:gpt-5.5",
            ):
                yield key

        async def delete(self, *keys: str) -> None:
            self.deleted.extend(keys)

    redis = Redis()
    monkeypatch.setattr(pricing_cache, "get_redis", lambda: redis)

    await billing._invalidate_pricing_cache("gpt-*", "default")  # noqa: SLF001

    assert redis.deleted == [
        "lumen:pricing:v1:default:gpt-5.4",
        "lumen:pricing:v1:priority:gpt-5.5",
    ]


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

    monkeypatch.setattr(billing_services, "_redemption_secret", fake_secret)
    monkeypatch.setattr(
        billing_services, "_require_bootstrap_completed", fake_bootstrap
    )
    monkeypatch.setattr(billing_composition, "write_audit", fake_write_audit)
    monkeypatch.setattr(
        billing_composition, "request_ip_hash", lambda _request: "ip-hash"
    )
    monkeypatch.setattr(billing_services, "get_redis", lambda: _FailingRedis())

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
    monkeypatch.setattr(billing_services, "_redemption_secret", fake_secret)
    monkeypatch.setattr(
        billing_services, "_require_bootstrap_completed", fake_bootstrap
    )
    monkeypatch.setattr(billing_composition, "write_audit", fake_write_audit)
    monkeypatch.setattr(
        billing_composition, "request_ip_hash", lambda _request: "ip-hash"
    )
    monkeypatch.setattr(billing_services, "get_redis", lambda: redis)

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
    assert response.headers["Idempotency-Key"].startswith("derived:")
    assert isinstance(db.added[0], billing.RedemptionBatch)
    assert db.flush_calls[0] == [db.added[0]]
    assert db.flush_calls[-1] is None
    assert any(key.startswith(billing._DOWNLOAD_TOKEN_PREFIX) for key in redis.values)  # noqa: SLF001
    assert any(key.startswith(billing._PLAINTEXT_BATCH_PREFIX) for key in redis.values)  # noqa: SLF001


@pytest.mark.asyncio
async def test_create_redemption_codes_replays_persisted_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = billing.RedemptionBatch(
        id="batch-1",
        created_by="admin-1",
        idempotency_key="client:create-1",
        request_hash="persisted-request-hash",
        amount_micro=10_000_000,
        code_count=2,
        max_redemptions=1,
        expires_at=None,
    )
    replayed: list[tuple[str, str]] = []
    expected = billing.AdminRedemptionCodeCreateOut(
        batch_id="batch-1",
        count=2,
        amount=billing._money(10_000_000),  # noqa: SLF001
        download_token="tok_replay",
        plaintext_codes=["LMN-AAAA-BBBB-CCCC-DDDD"],
    )

    async def fail_new_batch_checks(_db: Any) -> None:
        raise AssertionError(
            "persisted replay must not require current billing secrets"
        )

    async def fake_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def existing(*_args: Any, **_kwargs: Any) -> Any:
        return batch

    async def replay(
        persisted: Any,
        *,
        request_hash: str,
        idempotency_key: str,
        response: Response,
    ) -> Any:
        assert persisted is batch
        assert response is not None
        replayed.append((request_hash, idempotency_key))
        return expected

    monkeypatch.setattr(
        billing_services,
        "_require_bootstrap_completed",
        fail_new_batch_checks,
    )
    monkeypatch.setattr(billing_services, "_redemption_secret", fail_new_batch_checks)
    monkeypatch.setattr(
        billing_services, "_lock_redemption_batch_idempotency_key", fake_lock
    )
    monkeypatch.setattr(billing_services, "_redemption_batch_for_idempotency", existing)
    monkeypatch.setattr(billing_services, "_replay_redemption_batch", replay)
    monkeypatch.setattr(
        billing_services,
        "_redemption_batch_request_hash",
        lambda *_args, **_kwargs: "persisted-request-hash",
    )
    monkeypatch.setattr(
        billing.billing_core,
        "generate_redemption_code",
        lambda: (_ for _ in ()).throw(
            AssertionError("persisted replay must not generate new codes")
        ),
    )

    out = await billing.admin_create_redemption_codes(
        AdminRedemptionCodeCreateIn(amount_rmb="10", count=2),
        _request(
            method="POST",
            headers=[(b"idempotency-key", b"create-1")],
        ),
        Response(),
        SimpleNamespace(id="admin-1", email="admin@example.test"),
        _Db(),  # type: ignore[arg-type]
    )

    assert out is expected
    assert replayed == [("persisted-request-hash", "client:create-1")]


@pytest.mark.asyncio
async def test_redemption_batch_replay_rejects_changed_request() -> None:
    batch = billing.RedemptionBatch(
        id="batch-1",
        created_by="admin-1",
        idempotency_key="client:create-1",
        request_hash="first-request",
        amount_micro=10_000_000,
        code_count=1,
        max_redemptions=1,
        expires_at=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await billing._replay_redemption_batch(  # noqa: SLF001
            batch,
            request_hash="second-request",
            idempotency_key="client:create-1",
            response=Response(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"]["code"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_redemption_batch_replay_returns_persisted_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expires_at = datetime(2026, 7, 12, tzinfo=timezone.utc)
    codes = [
        "LMN-AAAA-BBBB-CCCC-DDDD",
        "LMN-EEEE-FFFF-GGGG-HHHH",
    ]
    batch = billing.RedemptionBatch(
        id="batch-1",
        created_by="admin-1",
        idempotency_key="client:create-1",
        request_hash="request-hash",
        amount_micro=10_000_000,
        code_count=2,
        max_redemptions=1,
        expires_at=expires_at,
    )

    async def load(batch_id: str) -> dict[str, Any]:
        assert batch_id == "batch-1"
        return {
            "batch_id": batch_id,
            "amount_rmb": "10",
            "expires_at": expires_at.isoformat(),
            "codes": codes,
        }

    async def store(**kwargs: Any) -> str:
        assert kwargs == {
            "batch_id": "batch-1",
            "amount_micro": 10_000_000,
            "codes": codes,
            "expires_at": expires_at,
        }
        return "tok_replay"

    monkeypatch.setattr(billing_services, "_load_redemption_plaintext_batch", load)
    monkeypatch.setattr(billing_services, "_store_redemption_plaintext_batch", store)
    response = Response()

    out = await billing._replay_redemption_batch(  # noqa: SLF001
        batch,
        request_hash="request-hash",
        idempotency_key="client:create-1",
        response=response,
    )

    assert out.batch_id == "batch-1"
    assert out.plaintext_codes == codes
    assert out.download_token == "tok_replay"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Idempotency-Key"] == "client:create-1"


@pytest.mark.asyncio
async def test_create_redemption_codes_logs_cache_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_secret(_db: Any) -> str:
        return "test-redemption-secret"

    async def fake_bootstrap(_db: Any) -> None:
        return None

    async def fake_write_audit(*_args: Any, **_kwargs: Any) -> bool:
        return True

    class CommitFailDb(_Db):
        async def commit(self) -> None:
            raise RuntimeError("commit failed")

    monkeypatch.setattr(billing_services, "_redemption_secret", fake_secret)
    monkeypatch.setattr(
        billing_services, "_require_bootstrap_completed", fake_bootstrap
    )
    monkeypatch.setattr(billing_composition, "write_audit", fake_write_audit)
    monkeypatch.setattr(
        billing_composition, "request_ip_hash", lambda _request: "ip-hash"
    )
    monkeypatch.setattr(
        billing_services,
        "get_redis",
        lambda: _FailingDeleteRedis(),
    )
    monkeypatch.setattr(
        billing_composition,
        "get_redis",
        lambda: _FailingDeleteRedis(),
    )

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError, match="commit failed"):
            await billing.admin_create_redemption_codes(
                AdminRedemptionCodeCreateIn(amount_rmb="10", count=1),
                None,  # type: ignore[arg-type]
                Response(),
                SimpleNamespace(id="admin-1", email="admin@example.test"),
                CommitFailDb(),  # type: ignore[arg-type]
            )

    assert "redemption plaintext cache cleanup failed" in caplog.text


@pytest.mark.asyncio
async def test_store_redemption_plaintext_batch_cleans_partial_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FailingSecondSetRedis()
    monkeypatch.setattr(billing_services, "get_redis", lambda: redis)

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


@pytest.mark.asyncio
async def test_topup_redeem_requests_wallet_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    wallet = SimpleNamespace(
        balance_micro=0,
        hold_micro=0,
        lifetime_topup_micro=0,
        version=0,
    )

    async def fake_get_wallet(_db: Any, _user_id: str, *, lock: bool) -> Any:
        calls.append(lock)
        return wallet

    async def fake_existing_tx(_db: Any, _user_id: str, _idempotency_key: str) -> None:
        return None

    async def fake_insert_tx(
        _db: Any,
        wallet_arg: Any,
        **_kwargs: Any,
    ) -> Any:
        return SimpleNamespace(id="tx-1", balance_after=wallet_arg.balance_micro)

    monkeypatch.setattr(billing.billing_core, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing.billing_core, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing.billing_core, "_insert_tx", fake_insert_tx)

    tx = await billing.billing_core.topup_redeem(
        object(),  # type: ignore[arg-type]
        "user-1",
        123,
        usage_id="usage-1",
        code_id="code-1",
    )

    assert calls == [True]
    assert wallet.balance_micro == 123
    assert tx.balance_after == 123


@pytest.mark.asyncio
async def test_redemption_idempotency_replays_existing_usage() -> None:
    out = await billing._redemption_out_for_usage(  # noqa: SLF001
        _FirstDb(
            (
                SimpleNamespace(amount_micro=5_000_000),
                SimpleNamespace(
                    balance_after=12_000_000,
                    meta={"redemption_request_hash": "request-hash"},
                ),
            )
        ),  # type: ignore[arg-type]
        user_id="user-1",
        usage_id="usage-1",
        request_hash="request-hash",
    )

    assert out is not None
    assert out.amount.micro == 5_000_000
    assert out.balance.micro == 12_000_000


@pytest.mark.asyncio
async def test_redemption_idempotency_rejects_reused_key_for_different_code() -> None:
    with pytest.raises(Exception) as excinfo:
        await billing._redemption_out_for_usage(  # noqa: SLF001
            _FirstDb(
                (
                    SimpleNamespace(amount_micro=5_000_000),
                    SimpleNamespace(
                        balance_after=12_000_000,
                        meta={"redemption_request_hash": "first-code"},
                    ),
                )
            ),  # type: ignore[arg-type]
            user_id="user-1",
            usage_id="usage-1",
            request_hash="second-code",
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert excinfo.value.detail["error"]["code"] == "idempotency_conflict"


def test_redemption_integrity_constraint_name_uses_structured_diag() -> None:
    class Orig(Exception):
        diag = SimpleNamespace(constraint_name="uq_redeem_code_user")

    exc = IntegrityError("insert usage", {}, Orig("duplicate"))

    assert billing._integrity_constraint_name(exc) == "uq_redeem_code_user"  # noqa: SLF001


@pytest.mark.asyncio
async def test_redeem_code_cache_miss_replays_existing_usage_from_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay = billing.RedemptionOut(
        amount=billing._money(5_000_000),  # noqa: SLF001
        balance=billing._money(12_000_000),  # noqa: SLF001
    )
    cached: list[billing.RedemptionOut] = []
    locks: list[tuple[str, str]] = []

    async def no_cached(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def lock_key(_db: Any, user_id: str, idempotency_key: str) -> None:
        locks.append((user_id, idempotency_key))

    async def existing_usage(
        _db: Any,
        *,
        user_id: str,
        usage_id: str,
        request_hash: str,
    ) -> billing.RedemptionOut:
        assert user_id == "user-1"
        assert usage_id
        assert request_hash
        return replay

    async def cache_response(
        _user_id: str,
        _idempotency_key: str,
        _request_hash: str,
        response: billing.RedemptionOut,
    ) -> None:
        cached.append(response)

    async def fail_operational(_db: Any) -> None:
        raise AssertionError("DB idempotency fallback must avoid a second redeem")

    async def allow_redemption(_redis: Any, _key: str) -> None:
        return None

    monkeypatch.setattr(billing_services, "_cached_redemption_out", no_cached)
    monkeypatch.setattr(billing_services, "_lock_redemption_idempotency_key", lock_key)
    monkeypatch.setattr(billing_services, "_redemption_out_for_usage", existing_usage)
    monkeypatch.setattr(billing_services, "_cache_redemption_out", cache_response)
    monkeypatch.setattr(billing_composition, "get_redis", object)
    monkeypatch.setattr(
        billing_services.REDEMPTION_LIMITER,
        "check",
        allow_redemption,
    )
    monkeypatch.setattr(
        billing_services, "_require_redemption_operational", fail_operational
    )

    out = await billing.redeem_code(
        RedemptionIn(code="LMN-AAAA-BBBB-CCCC"),
        _request(method="POST", headers=[(b"idempotency-key", b"redeem-1")]),
        SimpleNamespace(
            id="user-1",
            email="user@example.test",
            account_mode="wallet",
        ),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert out is replay
    assert cached == [replay]
    assert locks == [("user-1", "client:redeem-1")]


@pytest.mark.asyncio
async def test_redeem_code_integrity_error_replays_wallet_tx_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized = billing_core.normalize_redemption_code("LMN-AAAA-BBBB-CCCC")
    code = SimpleNamespace(
        id="code-1",
        code_hash=billing_core.hash_redemption_code(normalized, "secret"),
        revoked_at=None,
        expires_at=None,
        redeemed_count=0,
        max_redemptions=1,
        amount_micro=5_000_000,
    )
    replay = billing.RedemptionOut(
        amount=billing._money(5_000_000),  # noqa: SLF001
        balance=billing._money(12_000_000),  # noqa: SLF001
    )
    existing_calls = 0
    cached: list[billing.RedemptionOut] = []

    class Db(_Db):
        async def execute(self, *_args: Any, **_kwargs: Any) -> _ScalarResult:
            return _ScalarResult([code])

    class Limiter:
        async def check(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class Orig(Exception):
        diag = SimpleNamespace(constraint_name="uq_wallet_tx_idemp")

    async def no_cached(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def existing_usage(
        *_args: Any, **_kwargs: Any
    ) -> billing.RedemptionOut | None:
        nonlocal existing_calls
        existing_calls += 1
        if existing_calls == 1:
            return None
        return replay

    async def fail_topup(*_args: Any, **_kwargs: Any) -> None:
        raise IntegrityError("insert wallet tx", {}, Orig("duplicate"))

    async def secrets(_db: Any) -> list[str]:
        return ["secret"]

    async def cache_response(
        _user_id: str,
        _idempotency_key: str,
        _request_hash: str,
        response: billing.RedemptionOut,
    ) -> None:
        cached.append(response)

    monkeypatch.setattr(billing_services, "_cached_redemption_out", no_cached)
    monkeypatch.setattr(billing_services, "_lock_redemption_idempotency_key", noop)
    monkeypatch.setattr(billing_services, "_redemption_out_for_usage", existing_usage)
    monkeypatch.setattr(billing_services, "_cache_redemption_out", cache_response)
    monkeypatch.setattr(billing_services, "_require_redemption_operational", noop)
    monkeypatch.setattr(billing_services, "REDEMPTION_LIMITER", Limiter())
    monkeypatch.setattr(billing_composition, "get_redis", lambda: object())
    monkeypatch.setattr(billing_services, "_redemption_secrets", secrets)
    monkeypatch.setattr(billing.billing_core, "topup_redeem", fail_topup)

    db = Db()
    out = await billing.redeem_code(
        RedemptionIn(code="LMN-AAAA-BBBB-CCCC"),
        _request(method="POST", headers=[(b"idempotency-key", b"redeem-1")]),
        SimpleNamespace(
            id="user-1",
            email="user@example.test",
            account_mode="wallet",
        ),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out is replay
    assert db.rolled_back is True
    assert existing_calls == 2
    assert cached == [replay]


@pytest.mark.asyncio
async def test_redeem_code_samples_clock_after_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计新-10：FOR UPDATE 可能阻塞任意久，过期判定必须用拿到锁之后的时钟。"""
    from app.routes.billing_parts import redemptions as redemptions_route

    normalized = billing_core.normalize_redemption_code("LMN-AAAA-BBBB-CCCC")
    start = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    clock = {"now": start}
    code = SimpleNamespace(
        id="code-1",
        code_hash=billing_core.hash_redemption_code(normalized, "secret"),
        revoked_at=None,
        # 请求进来时还有 30 秒有效期，但排在锁后面等了 60 秒。
        expires_at=start + timedelta(seconds=30),
        redeemed_count=0,
        max_redemptions=1,
        amount_micro=5_000_000,
    )

    class _Clock:
        @staticmethod
        def now(_tz: Any = None) -> datetime:
            return clock["now"]

    class Db(_Db):
        async def execute(self, *_args: Any, **_kwargs: Any) -> _ScalarResult:
            clock["now"] = start + timedelta(seconds=60)  # 模拟锁等待
            return _ScalarResult([code])

    class Limiter:
        async def check(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    async def no_cached(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def secrets(_db: Any) -> list[str]:
        return ["secret"]

    async def fail_topup(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("expired code must not be redeemed")

    monkeypatch.setattr(redemptions_route, "datetime", _Clock)
    monkeypatch.setattr(billing_services, "_cached_redemption_out", no_cached)
    monkeypatch.setattr(billing_services, "_lock_redemption_idempotency_key", noop)
    monkeypatch.setattr(billing_services, "_redemption_out_for_usage", no_cached)
    monkeypatch.setattr(billing_services, "_cache_redemption_out", noop)
    monkeypatch.setattr(billing_services, "_require_redemption_operational", noop)
    monkeypatch.setattr(billing_services, "REDEMPTION_LIMITER", Limiter())
    monkeypatch.setattr(billing_composition, "get_redis", lambda: object())
    monkeypatch.setattr(billing_services, "_redemption_secrets", secrets)
    monkeypatch.setattr(billing.billing_core, "topup_redeem", fail_topup)

    with pytest.raises(HTTPException) as exc_info:
        await billing.redeem_code(
            RedemptionIn(code="LMN-AAAA-BBBB-CCCC"),
            _request(method="POST", headers=[(b"idempotency-key", b"redeem-1")]),
            SimpleNamespace(
                id="user-1",
                email="user@example.test",
                account_mode="wallet",
            ),  # type: ignore[arg-type]
            Db(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 410
    assert exc_info.value.detail["error"]["code"] == "CODE_EXPIRED"


@pytest.mark.asyncio
async def test_rotate_redemption_secret_keeps_previous_secret_for_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    remembered: list[str | None] = []
    updated: list[list[tuple[str, str]]] = []
    audits: list[dict[str, Any]] = []

    class Db:
        committed = False

        async def commit(self) -> None:
            events.append("commit")
            self.committed = True

    async def fake_lock(_db: Any) -> str:
        events.append("lock")
        return "old-secret-value-123456"

    async def fail_get_setting(_db: Any, _spec: Any) -> str:
        raise AssertionError("persisted current secret must come from the locked read")

    async def fake_update_settings(_db: Any, pairs: list[tuple[str, str]]) -> None:
        events.append("update")
        updated.append(pairs)

    async def fake_remember(_db: Any, old_secret: str | None) -> str:
        events.append("remember")
        remembered.append(old_secret)
        return "2026-05-17T00:00:00+00:00"

    async def fake_write_audit(_db: Any, **kwargs: Any) -> bool:
        events.append("audit")
        audits.append(kwargs)
        return True

    async def fake_overview(_admin: Any, _db: Any) -> Any:
        events.append("overview")
        return "overview"

    monkeypatch.setattr(
        billing_composition, "lock_redemption_secret_rotation", fake_lock
    )
    monkeypatch.setattr(billing_composition, "get_setting", fail_get_setting)
    monkeypatch.setattr(billing_composition, "update_settings", fake_update_settings)
    monkeypatch.setattr(
        billing_services, "_generate_redemption_secret", lambda: "new-secret"
    )
    monkeypatch.setattr(
        billing_composition, "remember_previous_redemption_secret", fake_remember
    )
    monkeypatch.setattr(billing_composition, "write_audit", fake_write_audit)
    monkeypatch.setattr(
        billing_composition, "request_ip_hash", lambda _request: "ip-hash"
    )
    monkeypatch.setattr(
        billing_overview_routes, "admin_billing_overview", fake_overview
    )

    db = Db()
    out = await billing.admin_rotate_redemption_secret(
        object(),  # type: ignore[arg-type]
        SimpleNamespace(id="admin-1", email="admin@example.test"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == "overview"
    assert db.committed is True
    assert updated == [[("billing.redemption_code_secret", "new-secret")]]
    assert remembered == ["old-secret-value-123456"]
    assert audits[0]["details"]["revoked_unredeemed_count"] == 0
    assert audits[0]["details"]["previous_secret_valid_until"] is not None
    assert events == ["lock", "update", "remember", "audit", "commit", "overview"]


@pytest.mark.asyncio
async def test_rotate_redemption_secret_rolls_back_second_serialized_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Db:
        async def rollback(self) -> None:
            events.append("rollback")

        async def commit(self) -> None:
            raise AssertionError("rejected second rotation must not commit")

    async def fake_lock(_db: Any) -> str:
        events.append("lock")
        return "first-committed-new-secret"

    async def fake_update_settings(_db: Any, _pairs: list[tuple[str, str]]) -> None:
        events.append("update")

    async def reject_active_previous(_db: Any, old_secret: str | None) -> None:
        events.append(f"remember:{old_secret}")
        raise billing_overview_routes.PreviousRedemptionSecretLocked("active previous")

    async def fail_audit(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("rejected second rotation must not write an audit")

    monkeypatch.setattr(
        billing_composition, "lock_redemption_secret_rotation", fake_lock
    )
    monkeypatch.setattr(billing_composition, "update_settings", fake_update_settings)
    monkeypatch.setattr(
        billing_composition,
        "remember_previous_redemption_secret",
        reject_active_previous,
    )
    monkeypatch.setattr(billing_composition, "write_audit", fail_audit)
    monkeypatch.setattr(
        billing_services, "_generate_redemption_secret", lambda: "second-new-secret"
    )

    with pytest.raises(HTTPException) as excinfo:
        await billing.admin_rotate_redemption_secret(
            object(),  # type: ignore[arg-type]
            SimpleNamespace(id="admin-2", email="admin-2@example.test"),  # type: ignore[arg-type]
            Db(),  # type: ignore[arg-type]
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"]["code"] == "previous_secret_locked"
    assert events == [
        "lock",
        "update",
        "remember:first-committed-new-secret",
        "rollback",
    ]


@pytest.mark.asyncio
async def test_topup_redeem_locks_wallet_before_balance_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool | str]] = []
    wallet = SimpleNamespace(balance_micro=0, lifetime_topup_micro=0, version=0)

    async def fake_get_wallet(_db: Any, user_id: str, *, lock: bool = False) -> Any:
        calls.append((user_id, lock))
        return wallet

    async def fake_existing_tx(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_insert_tx(
        _db: Any,
        wallet_arg: Any,
        *,
        user_id: str,
        kind: str,
        amount_micro: int,
        ref_type: str,
        ref_id: str,
        idempotency_key: str,
        meta: dict[str, Any],
    ) -> SimpleNamespace:
        assert wallet_arg is wallet
        assert wallet.balance_micro == amount_micro
        return SimpleNamespace(
            id="wallet-tx-1",
            user_id=user_id,
            kind=kind,
            amount_micro=amount_micro,
            ref_type=ref_type,
            ref_id=ref_id,
            idempotency_key=idempotency_key,
            meta=meta,
            balance_after=wallet.balance_micro,
        )

    monkeypatch.setattr(billing_core, "get_wallet", fake_get_wallet)
    monkeypatch.setattr(billing_core, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing_core, "_insert_tx", fake_insert_tx)

    tx = await billing_core.topup_redeem(
        object(),  # type: ignore[arg-type]
        "user-1",
        25_000_000,
        usage_id="usage-1",
        code_id="code-1",
    )

    assert calls == [("user-1", True)]
    assert wallet.balance_micro == 25_000_000
    assert tx.idempotency_key == "redeem:usage-1"


@pytest.mark.asyncio
async def test_topup_redeem_replay_rejects_different_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_meta = {
        "code_id": "code-original",
        "redemption_request_hash": "hash-original",
    }
    existing_tx = SimpleNamespace(
        id="wallet-tx-existing",
        idempotency_key="redeem:usage-1",
        meta=original_meta,
    )

    async def fake_existing_tx(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return existing_tx

    async def fail_get_wallet(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("topup replay must not lock wallet or mutate balance")

    async def fail_insert_tx(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("topup replay must not insert a replacement tx")

    monkeypatch.setattr(billing_core, "_existing_tx", fake_existing_tx)
    monkeypatch.setattr(billing_core, "get_wallet", fail_get_wallet)
    monkeypatch.setattr(billing_core, "_insert_tx", fail_insert_tx)

    with pytest.raises(billing_core.BillingError) as exc:
        await billing_core.topup_redeem(
            object(),  # type: ignore[arg-type]
            "user-1",
            25_000_000,
            usage_id="usage-1",
            code_id="code-new",
            meta={
                "code_id": "code-new",
                "redemption_request_hash": "hash-new",
            },
        )

    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert exc.value.status_code == 409
    assert existing_tx.meta is original_meta
    assert existing_tx.meta == {
        "code_id": "code-original",
        "redemption_request_hash": "hash-original",
    }


def test_redemption_idempotency_key_derives_for_legacy_clients() -> None:
    request = _request(method="POST")

    first = billing._redemption_idempotency_key(  # noqa: SLF001
        request,
        user_id="user-1",
        normalized_code="ABCD-1234",
    )
    second = billing._redemption_idempotency_key(  # noqa: SLF001
        request,
        user_id="user-1",
        normalized_code="ABCD-1234",
    )

    assert first == second
    assert first.startswith("derived:")


def test_redemption_idempotency_key_rejects_blank_header() -> None:
    request = _request(method="POST", headers=[(b"idempotency-key", b"  ")])

    with pytest.raises(Exception) as excinfo:
        billing._redemption_idempotency_key(  # noqa: SLF001
            request,
            user_id="user-1",
            normalized_code="ABCD-1234",
        )

    assert getattr(excinfo.value, "status_code", None) == 422


@pytest.mark.asyncio
async def test_admin_adjust_wallet_rejects_per_operation_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Db:
        async def get(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(account_mode="wallet")

    async def fail_adjust(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("oversized adjustment must not mutate the wallet")

    monkeypatch.setattr(billing.billing_core, "adjust", fail_adjust)

    with pytest.raises(Exception) as excinfo:
        await billing.admin_adjust_wallet(
            "user-1",
            AdminWalletAdjustIn(amount_rmb_signed="1000001", reason="test"),
            _request(method="POST"),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert excinfo.value.detail["error"]["code"] == "amount_too_large"


@pytest.mark.asyncio
async def test_admin_adjust_wallet_rejects_negative_balance_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Db:
        async def get(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(account_mode="wallet")

    async def allow_negative(_db: Any) -> bool:
        return True

    seen_min_balance: list[int | None] = []

    async def fail_adjust(*_args: Any, **kwargs: Any) -> None:
        seen_min_balance.append(kwargs.get("min_balance_micro"))
        raise billing.billing_core.BillingError(
            "negative_balance_limit_exceeded",
            "admin wallet adjustment would exceed the negative balance limit",
            422,
        )

    monkeypatch.setattr(billing_services, "_allow_negative_balance", allow_negative)
    monkeypatch.setattr(billing.billing_core, "adjust", fail_adjust)

    with pytest.raises(Exception) as excinfo:
        await billing.admin_adjust_wallet(
            "user-1",
            AdminWalletAdjustIn(amount_rmb_signed="-100001", reason="test"),
            _request(method="POST"),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert excinfo.value.detail["error"]["code"] == "negative_balance_limit_exceeded"
    assert seen_min_balance == [-billing.MAX_ADMIN_NEGATIVE_BALANCE_MICRO]


@pytest.mark.asyncio
async def test_admin_adjust_wallet_passes_client_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """客户端每次表单提交生成的 per-operation 幂等键必须原样透传给 adjust。

    缺省时 adjust 由入参哈希派生键，会把两次参数完全相同的合法调账静默
    去重；显式键是唯一逃生门，路由层必须透传。
    """

    class Db:
        async def get(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(account_mode="wallet")

        async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
            return _ScalarOneOrNoneResult(None)

        async def commit(self) -> None:
            return None

    seen: list[tuple[int, str | None]] = []

    async def adjust(_db: Any, _user_id: str, amount: int, **_kwargs: Any) -> Any:
        seen.append((amount, _kwargs.get("idempotency_key")))
        return SimpleNamespace(id="tx-1")

    async def allow_negative(_db: Any) -> bool:
        return False

    def tx_out(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(id="tx-1")

    async def write_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def invalidate(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(billing.billing_core, "adjust", adjust)
    monkeypatch.setattr(billing_services, "_allow_negative_balance", allow_negative)
    monkeypatch.setattr(billing_services, "_tx_out", tx_out)
    monkeypatch.setattr(billing_composition, "write_audit", write_audit)
    monkeypatch.setattr(billing_services, "_invalidate_balance_cache", invalidate)

    await billing.admin_adjust_wallet(
        "user-1",
        AdminWalletAdjustIn(
            amount_rmb_signed="5",
            reason="客服补偿",
            idempotency_key="op-1",
        ),
        _request(method="POST"),
        SimpleNamespace(id="admin-1", email="admin@example.test"),
        Db(),  # type: ignore[arg-type]
        idempotency_key="op-1",
    )

    assert seen == [(billing_core.rmb_to_micro("5"), "op-1")]

    # Header-only clients are also recognized by the backend and reach the
    # same persistent wallet idempotency path.
    seen.clear()
    await billing.admin_adjust_wallet(
        "user-1",
        AdminWalletAdjustIn(amount_rmb_signed="5", reason="客服补偿"),
        _request(method="POST"),
        SimpleNamespace(id="admin-1", email="admin@example.test"),
        Db(),  # type: ignore[arg-type]
        idempotency_key="op-header",
    )
    assert seen == [(billing_core.rmb_to_micro("5"), "op-header")]

    # Old durable browser journals used ``semantic-`` + SHA-256 (73 chars).
    # Accept them during the client rollout so an already-started retry can
    # finish with its original operation identity.
    legacy_key = f"semantic-{'a' * 64}"
    seen.clear()
    await billing.admin_adjust_wallet(
        "user-1",
        AdminWalletAdjustIn(
            amount_rmb_signed="5",
            reason="客服补偿",
            idempotency_key=legacy_key,
        ),
        _request(method="POST"),
        SimpleNamespace(id="admin-1", email="admin@example.test"),
        Db(),  # type: ignore[arg-type]
        idempotency_key=legacy_key,
    )
    assert seen == [(billing_core.rmb_to_micro("5"), legacy_key)]

    # 未传 key 时后端收到 None，走输入派生键兜底。
    seen.clear()
    await billing.admin_adjust_wallet(
        "user-1",
        AdminWalletAdjustIn(amount_rmb_signed="5", reason="客服补偿"),
        _request(method="POST"),
        SimpleNamespace(id="admin-1", email="admin@example.test"),
        Db(),  # type: ignore[arg-type]
    )
    assert seen == [(billing_core.rmb_to_micro("5"), None)]


@pytest.mark.asyncio
async def test_admin_adjust_wallet_rejects_mismatched_header_and_body_keys() -> None:
    with pytest.raises(Exception) as excinfo:
        await billing.admin_adjust_wallet(
            "user-1",
            AdminWalletAdjustIn(
                amount_rmb_signed="5",
                reason="客服补偿",
                idempotency_key="body-key",
            ),
            _request(method="POST"),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            object(),  # type: ignore[arg-type]
            idempotency_key="header-key",
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert excinfo.value.detail["error"]["code"] == "idempotency_key_mismatch"


@pytest.mark.asyncio
async def test_admin_adjust_wallet_replay_does_not_duplicate_transaction_audit() -> (
    None
):
    class Db:
        def __init__(self) -> None:
            self.statements: list[Any] = []

        async def execute(self, statement: Any) -> Any:
            self.statements.append(statement)
            if len(self.statements) == 1:
                return _ScalarOneOrNoneResult("tx-1")
            return _ScalarOneOrNoneResult("audit-existing")

    class Commands:
        async def write_audit(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("idempotent replay must not write another audit row")

        def request_ip_hash(self, _request: Any) -> None:
            return None

    db = Db()
    await billing_wallet_routes._write_admin_adjust_audit_once(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        commands=Commands(),
        request=_request(method="POST"),
        admin=SimpleNamespace(id="admin-1", email="admin@example.test"),
        target_user_id="user-1",
        amount_micro=500_000,
        reason="manual credit",
        transaction=SimpleNamespace(id="tx-1"),  # type: ignore[arg-type]
    )

    assert len(db.statements) == 2
    assert getattr(db.statements[0], "_for_update_arg", None) is not None
    assert "audit_logs" in str(db.statements[1]).lower()


class _AccountModeDb:
    """只为 set_account_mode 提供 User 行锁查询与提交语义的最小 session。"""

    def __init__(self, target: Any) -> None:
        self._target = target
        self.committed = False

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        return _ScalarOneOrNoneResult(self._target)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_set_account_mode_to_byok_rejects_wallet_with_active_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wallet→byok 必须在还有冻结额度时拒绝切换，否则 hold_micro 会被永久锁死。

    切成 byok 后钱包不再有新的 hold/settle 入口，若此刻放行，已冻结的
    hold_micro 就没有任何路径可以释放。这里用 409 把切换挡在前面，属于
    「先解冻再切换」的强制顺序，不是事后补救。
    """
    target = SimpleNamespace(
        id="user-1",
        email="user@example.test",
        account_mode="wallet",
        deleted_at=None,
    )
    db = _AccountModeDb(target)

    async def get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(user_id="user-1", balance_micro=5_000, hold_micro=1_200)

    async def fail_adjust(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("blocked mode switch must not touch the balance")

    monkeypatch.setattr(billing.billing_core, "get_wallet", get_wallet)
    monkeypatch.setattr(billing.billing_core, "adjust", fail_adjust)

    with pytest.raises(HTTPException) as excinfo:
        await billing.admin_set_account_mode(
            "user-1",
            AdminSetAccountModeIn(mode="byok", on_residual_balance="zero"),
            _request(method="POST"),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            db,  # type: ignore[arg-type]
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"]["code"] == "WALLET_HAS_ACTIVE_HOLDS"
    assert excinfo.value.detail["error"]["details"]["hold_micro"] == 1_200
    # 切换被拒后账户仍是 wallet，冻结额度还能照常 settle/release。
    assert target.account_mode == "wallet"
    assert db.committed is False


@pytest.mark.asyncio
async def test_set_account_mode_to_byok_allows_switch_without_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有冻结额度时才放行，残余余额按 on_residual_balance 清零。"""
    target = SimpleNamespace(
        id="user-1",
        email="user@example.test",
        account_mode="wallet",
        deleted_at=None,
    )
    db = _AccountModeDb(target)
    adjustments: list[int] = []

    async def get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(user_id="user-1", balance_micro=5_000, hold_micro=0)

    async def adjust(_db: Any, _user_id: str, amount: int, **_kwargs: Any) -> Any:
        adjustments.append(amount)
        return SimpleNamespace(id="tx-1")

    async def wallet_out(*_args: Any, **_kwargs: Any) -> Any:
        return WalletOut(mode="byok", balance=None, hold=None, frozen=True)

    async def write_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def invalidate(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(billing.billing_core, "get_wallet", get_wallet)
    monkeypatch.setattr(billing.billing_core, "adjust", adjust)
    monkeypatch.setattr(billing_services, "_wallet_out", wallet_out)
    monkeypatch.setattr(billing_composition, "write_audit", write_audit)
    monkeypatch.setattr(billing_services, "_invalidate_balance_cache", invalidate)

    out = await billing.admin_set_account_mode(
        "user-1",
        AdminSetAccountModeIn(mode="byok", on_residual_balance="zero"),
        _request(method="POST"),
        SimpleNamespace(id="admin-1", email="admin@example.test"),
        db,  # type: ignore[arg-type]
    )

    assert out.account_mode == "byok"
    assert adjustments == [-5_000]
    assert db.committed is True
