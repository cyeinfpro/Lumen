from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from app import billing as worker_billing
from app.billing_parts.contracts import CompletionBillingRuntimeSnapshot
from app.tasks.completion_parts import outcomes
from lumen_core.constants import CompletionStatus, MessageStatus
from lumen_core.models import Completion, Message
from lumen_core.pricing import CostBreakdown


class _Metric:
    def labels(self, **_kwargs: Any) -> _Metric:
        return self

    def inc(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _UsageTotals:
    tokens_in = 120
    tokens_out = 40
    image_output_tokens = 0

    def model_values(self) -> dict[str, int]:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "image_output_tokens": self.image_output_tokens,
        }

    def apply_to(self, completion: Any) -> None:
        for key, value in self.model_values().items():
            setattr(completion, key, value)


class _ToolTracker:
    def finalize_active(self, _status: str) -> list[Any]:
        return []

    def content(self) -> list[Any]:
        return []


class _UpdateResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _SuccessSession:
    def __init__(self, active: dict[str, bool], *, rowcount: int = 1) -> None:
        self.active = active
        self.rowcount = rowcount
        self.commits = 0
        self.info: dict[str, Any] = {}
        self.completion = SimpleNamespace(
            id="comp-1",
            user_id="user-1",
            model="gpt-5.4",
            attempt=2,
            status=CompletionStatus.STREAMING.value,
            upstream_request={},
            user_api_credential_id=None,
        )
        self.message = SimpleNamespace(
            id="msg-1",
            status=MessageStatus.STREAMING.value,
            content={},
            parent_message_id=None,
        )

    async def __aenter__(self) -> _SuccessSession:
        assert self.active["value"] is False
        self.active["value"] = True
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.active["value"] = False

    async def execute(self, _statement: Any) -> _UpdateResult:
        assert self.active["value"] is True
        return _UpdateResult(self.rowcount)

    async def get(self, model: Any, _row_id: str) -> Any:
        assert self.active["value"] is True
        if model is Completion:
            return self.completion
        if model is Message:
            return self.message
        raise AssertionError(f"unexpected model {model}")

    async def commit(self) -> None:
        assert self.active["value"] is True
        self.commits += 1


def _completion_state(
    session: _SuccessSession,
    *,
    cancel_probe: Any,
) -> Any:
    async def publish_tool_updates(**_kwargs: Any) -> None:
        assert session.active["value"] is False

    async def deliver_event(_redis: Any, _delivery: Any) -> None:
        assert session.active["value"] is False

    return SimpleNamespace(
        ports=SimpleNamespace(
            persistence=SimpleNamespace(
                Completion=Completion,
                Message=Message,
                SessionLocal=lambda: session,
                update=update,
                affected_rows=lambda result: result.rowcount,
            ),
            upstream=SimpleNamespace(
                _merge_completion_upstream_metadata=(
                    lambda current, **_kwargs: dict(current)
                ),
            ),
            billing=SimpleNamespace(
                worker_billing=worker_billing,
                _fallback_completion_tool_image_tokens=None,
            ),
            tools=SimpleNamespace(
                _publish_completion_tool_updates=publish_tool_updates,
            ),
            retry=SimpleNamespace(
                _RUNNING_COMPLETION_STATUSES=(CompletionStatus.STREAMING.value,),
                _raise_if_completion_cancelled=cancel_probe,
                _CompletionEpochSuperseded=RuntimeError,
                _LeaseLost=RuntimeError,
            ),
            events=SimpleNamespace(
                _deliver_completion_event=deliver_event,
                upstream_calls_total=_Metric(),
            ),
        ),
        request=SimpleNamespace(
            redis=object(),
            task_id="comp-1",
            channel="task:comp-1",
        ),
        preparation=SimpleNamespace(
            user_id="user-1",
            message_id="msg-1",
            attempt=2,
            attempt_epoch=2,
            conversation_id=None,
            fast_mode=False,
        ),
        streaming=SimpleNamespace(
            tool_images=[],
            reserved_tool_image_budget_micro=0,
            accumulated_thinking="",
        ),
        usage=SimpleNamespace(
            usage_totals=_UsageTotals(),
            upstream_provider_event=None,
            tool_tracker=_ToolTracker(),
            memory_meta_for_event={
                "used_memory_ids": [],
                "used_memory_summary": [],
            },
        ),
        settlement=SimpleNamespace(
            lease_lost=asyncio.Event(),
            task_outcome="unknown",
        ),
    )


@pytest.mark.asyncio
async def test_completion_success_reads_cancel_and_billing_settings_outside_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = {"value": False}
    session = _SuccessSession(active)
    cancel_reasons: list[str] = []
    settings_reads: list[str] = []
    charged: dict[str, bool] = {}

    async def cancel_probe(_redis: Any, _task_id: str, reason: str) -> None:
        assert active["value"] is False
        cancel_reasons.append(reason)

    def setting(name: str, value: bool) -> Any:
        async def read() -> bool:
            assert active["value"] is False
            settings_reads.append(name)
            return value

        return read

    async def charge_completion(
        _session: Any,
        _completion: Any,
        **runtime: bool,
    ) -> None:
        assert active["value"] is True
        charged.update(runtime)

    async def flush_balance_cache_refreshes(_session: Any) -> None:
        assert active["value"] is True
        assert session.commits == 1

    async def stage_deliveries(
        _state: Any,
        _session: Any,
        _message: Any,
        _final_text: str,
    ) -> tuple[tuple[str, str, dict[str, Any]], None]:
        assert active["value"] is True
        return ("event-1", "completion", {}), None

    monkeypatch.setattr(
        worker_billing,
        "_billing_enabled",
        setting("billing_enabled", True),
    )
    monkeypatch.setattr(
        worker_billing,
        "_cache_aware_enabled",
        setting("cache_aware", False),
    )
    monkeypatch.setattr(
        worker_billing,
        "_allow_negative_balance",
        setting("allow_negative", True),
    )
    monkeypatch.setattr(
        worker_billing,
        "_window_rate_limit_enabled",
        setting("window_rate_limit", False),
    )
    monkeypatch.setattr(worker_billing, "charge_completion", charge_completion)
    monkeypatch.setattr(
        worker_billing,
        "flush_balance_cache_refreshes",
        flush_balance_cache_refreshes,
    )
    monkeypatch.setattr(outcomes, "completion_final_text", lambda _state: "done")
    monkeypatch.setattr(outcomes, "_stage_success_deliveries", stage_deliveries)

    state = _completion_state(session, cancel_probe=cancel_probe)
    await outcomes.settle_success(state)

    assert settings_reads == [
        "billing_enabled",
        "cache_aware",
        "allow_negative",
        "window_rate_limit",
    ]
    assert cancel_reasons == [
        "cancelled before success commit",
        "cancelled before billing settle",
    ]
    assert charged == {
        "billing_enabled": True,
        "cache_aware": False,
        "allow_negative": True,
        "window_rate_limit": False,
    }
    assert state.settlement.task_outcome == "succeeded"
    assert active["value"] is False


@pytest.mark.asyncio
async def test_completion_success_conflict_checks_cancel_after_session_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancelled(RuntimeError):
        pass

    active = {"value": False}
    session = _SuccessSession(active, rowcount=0)
    cancel_reasons: list[str] = []

    async def cancel_probe(_redis: Any, _task_id: str, reason: str) -> None:
        assert active["value"] is False
        cancel_reasons.append(reason)
        if reason == "cancelled during success commit":
            raise Cancelled(reason)

    async def snapshot() -> CompletionBillingRuntimeSnapshot:
        assert active["value"] is False
        return CompletionBillingRuntimeSnapshot(
            billing_enabled=True,
            cache_aware=True,
            allow_negative=False,
            window_rate_limit=False,
        )

    async def fail_charge(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("conflicted success must not charge")

    monkeypatch.setattr(
        worker_billing,
        "snapshot_completion_billing_runtime",
        snapshot,
    )
    monkeypatch.setattr(worker_billing, "charge_completion", fail_charge)
    monkeypatch.setattr(outcomes, "completion_final_text", lambda _state: "done")

    state = _completion_state(session, cancel_probe=cancel_probe)
    with pytest.raises(Cancelled, match="cancelled during success commit"):
        await outcomes.settle_success(state)

    assert cancel_reasons == [
        "cancelled before success commit",
        "cancelled before billing settle",
        "cancelled during success commit",
    ]
    assert active["value"] is False
    assert session.commits == 0


@pytest.mark.asyncio
async def test_charge_completion_explicit_runtime_snapshot_skips_dynamic_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.info: dict[str, Any] = {}

        def add(self, row: Any) -> None:
            self.added.append(row)

        async def get(self, _model: Any, _row_id: str) -> None:
            return None

    class Cache:
        async def evaluate_rate_limits(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("explicitly disabled window limit must not run")

        async def credential_limits(
            self,
            _session: Any,
            _key_id: str,
        ) -> dict[str, int]:
            return {"5h": 0, "1d": 0, "7d": 0}

    completion = SimpleNamespace(
        id="comp-explicit",
        user_id="user-1",
        model="gpt-5.4",
        tokens_in=100,
        tokens_out=50,
        cache_read_tokens=20,
        cache_creation_tokens=30,
        cache_creation_5m_tokens=10,
        cache_creation_1h_tokens=5,
        reasoning_tokens=7,
        image_output_tokens=9,
        user_api_credential_id="cred-1",
        upstream_request={"billing_rate_multiplier_x10000": 10_000},
    )
    session = Session()
    settle_kwargs: dict[str, Any] = {}

    async def fail_setting() -> bool:
        raise AssertionError(
            "explicit runtime snapshot must suppress dynamic setting read"
        )

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def no_existing(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def breakdown(*_args: Any, **_kwargs: Any) -> CostBreakdown:
        return CostBreakdown(
            input_cost_micro=90,
            output_cost_micro=10,
            cache_read_cost_micro=0,
            cache_creation_cost_micro=0,
            image_output_cost_micro=0,
            reasoning_cost_micro=0,
            long_context_applied=False,
            priority_tier_applied=False,
            rate_multiplier_x10000=10_000,
            total_cost_micro=100,
            actual_cost_micro=100,
            pricing_source="snapshot",
        )

    async def settle(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        settle_kwargs.update(kwargs)
        return SimpleNamespace(
            id="tx-1",
            kind="settle",
            amount_micro=-100,
            balance_after=900,
            hold_after=0,
            meta=kwargs["meta"],
        )

    monkeypatch.setattr(worker_billing, "AsyncSession", Session)
    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_billing_enabled", fail_setting)
    monkeypatch.setattr(worker_billing, "_cache_aware_enabled", fail_setting)
    monkeypatch.setattr(worker_billing, "_allow_negative_balance", fail_setting)
    monkeypatch.setattr(worker_billing, "_window_rate_limit_enabled", fail_setting)
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_existing_fingerprint_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_completion_cost_breakdown", breakdown)
    monkeypatch.setattr(worker_billing.billing_core, "settle", settle)
    monkeypatch.setattr(worker_billing, "get_billing_cache", lambda: Cache())

    await worker_billing.charge_completion(  # type: ignore[arg-type]
        session,
        completion,
        billing_enabled=True,
        cache_aware=False,
        allow_negative=True,
        window_rate_limit=False,
    )

    assert settle_kwargs["allow_negative"] is True
    assert settle_kwargs["meta"]["tokens_in"] == 150
    assert settle_kwargs["meta"]["cache_read_tokens"] == 0
    assert settle_kwargs["meta"]["cache_creation_tokens"] == 0


@pytest.mark.asyncio
async def test_charge_completion_pricing_db_failure_propagates_without_fake_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.info: dict[str, Any] = {}

        def add(self, row: Any) -> None:
            self.added.append(row)

    completion = SimpleNamespace(
        id="comp-db-pending",
        user_id="user-1",
        model="gpt-4o",
        tokens_in=100,
        tokens_out=50,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=0,
        user_api_credential_id=None,
        upstream_request={
            "billing_rate_multiplier_x10000": 10_000,
            "tool_image_reserved_micro": 2_000,
        },
    )
    session = Session()

    async def account_mode(*_args: Any) -> str:
        return "wallet"

    async def no_existing(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fail_breakdown(*_args: Any, **_kwargs: Any) -> CostBreakdown:
        raise SQLAlchemyError("pricing db unavailable")

    async def fail_settle(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("pricing DB failure must not settle the hold")

    monkeypatch.setattr(worker_billing, "AsyncSession", Session)
    monkeypatch.setattr(worker_billing, "_account_mode", account_mode)
    monkeypatch.setattr(worker_billing, "_existing_wallet_tx", no_existing)
    monkeypatch.setattr(worker_billing, "_completion_cost_breakdown", fail_breakdown)
    monkeypatch.setattr(worker_billing.billing_core, "settle", fail_settle)

    with pytest.raises(SQLAlchemyError, match="pricing db unavailable"):
        await worker_billing.charge_completion(  # type: ignore[arg-type]
            session,
            completion,
            billing_enabled=True,
            cache_aware=True,
            allow_negative=False,
            window_rate_limit=False,
        )

    assert completion.upstream_request["tool_image_reserved_micro"] == 2_000
    assert "completion_billing_state" not in completion.upstream_request
    assert session.added == []
