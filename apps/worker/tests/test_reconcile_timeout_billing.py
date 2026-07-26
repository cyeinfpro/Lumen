"""对账器把卡死任务判超时时的计费动作。

核心不变式（纯转嫁）：**超时不等于可以退款**。只有能证明上游请求从未发出去
才 release，其余一律按 hold 全额结算。任务被 worker 认领过就意味着上游可能
已经计了费，此时退款等于平台替用户吸收这笔成本。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.reconciliation.contracts import ReconcileContext
from app.reconciliation.bonus_billing import BONUS_BILLING_RECONCILER
from app.reconciliation.task_domains import (
    COMPLETION_RECONCILER,
    GENERATION_RECONCILER,
    RECON_TIMEOUT_CODE,
)


class _RecordingBilling:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str):
        async def _call(_session: Any, _task: Any, **kwargs: Any) -> None:
            self.calls.append((name, kwargs))

        return _call

    def __getattr__(self, name: str):
        if name.startswith("release_") or name.startswith("settle_"):
            return self._record(name)
        raise AttributeError(name)


class _NullSession:
    async def get(self, _model: Any, _pk: Any) -> None:
        return None


class _ScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def scalars(self) -> _ScalarResult:
        return self

    def __iter__(self):
        return iter(self.values)

    def scalar_one_or_none(self) -> Any | None:
        return self.values[0] if self.values else None


class _NestedTransaction:
    async def __aenter__(self) -> _NestedTransaction:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _BonusSession(_NullSession):
    def __init__(self, results: list[list[Any]]) -> None:
        self.results = [_ScalarResult(values) for values in results]
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        return self.results.pop(0)

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()


def _context(billing: _RecordingBilling) -> ReconcileContext:
    return ReconcileContext(
        redis=None,
        session=_NullSession(),
        now=datetime(2026, 7, 26, tzinfo=timezone.utc),
        billing=billing,
        logger=logging.getLogger("test-reconcile"),
        lease_unknowns=None,
        stage_event=lambda *_args, **_kwargs: ("sse", "chan", {}),
    )


def _task(
    status: str,
    attempt: int,
    *,
    response_received: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-1",
        user_id="user-1",
        message_id="msg-1",
        status=status,
        progress_stage=status,
        attempt=attempt,
        error_code=None,
        error_message=None,
        finished_at=None,
        updated_at=None,
        upstream_request=(
            {"upstream_response_received_at": "2026-07-26T00:00:00+00:00"}
            if response_received
            else {}
        ),
    )


async def _timeout(reconciler: Any, task: SimpleNamespace) -> _RecordingBilling:
    billing = _RecordingBilling()
    await reconciler._apply_timeout(_context(billing), task)
    return billing


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reconciler", "expected"),
    [
        (GENERATION_RECONCILER, "release_generation"),
        (COMPLETION_RECONCILER, "release_completion"),
    ],
)
async def test_never_claimed_task_is_released(
    reconciler: Any,
    expected: str,
) -> None:
    # 还躺在队列里、从没被 worker 取走过：上游请求不可能发出去，退款不让平台
    # 吸收任何成本，也不该让用户为一个没跑过的任务付钱。
    billing = await _timeout(reconciler, _task(reconciler.spec.queued_status, 0))
    assert [name for name, _ in billing.calls] == [expected]
    assert billing.calls[0][1] == {"reason": RECON_TIMEOUT_CODE}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reconciler", "running_status", "expected"),
    [
        (GENERATION_RECONCILER, "running", "settle_generation_unknown_upstream"),
        (COMPLETION_RECONCILER, "streaming", "settle_completion_unknown_upstream"),
    ],
)
async def test_running_task_without_response_receipt_is_released(
    reconciler: Any,
    running_status: str,
    expected: str,
) -> None:
    del expected
    billing = await _timeout(reconciler, _task(running_status, 1))
    assert [name for name, _ in billing.calls] == [reconciler.spec.release_method]
    assert billing.calls[0][1] == {"reason": RECON_TIMEOUT_CODE}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reconciler", "expected"),
    [
        (GENERATION_RECONCILER, "settle_generation_unknown_upstream"),
        (COMPLETION_RECONCILER, "settle_completion_unknown_upstream"),
    ],
)
async def test_response_receipt_settles_even_after_requeue(
    reconciler: Any,
    expected: str,
) -> None:
    billing = await _timeout(
        reconciler,
        _task(
            reconciler.spec.queued_status,
            2,
            response_received=True,
        ),
    )
    assert [name for name, _ in billing.calls] == [expected]
    assert billing.calls[0][1] == {
        "reason": RECON_TIMEOUT_CODE,
        "knowledge": "unknown",
    }


@pytest.mark.asyncio
async def test_billing_failure_aborts_timeout_marking() -> None:
    class _ExplodingBilling(_RecordingBilling):
        def __getattr__(self, name: str):
            async def _boom(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("wallet unavailable")

            return _boom

    task = _task("running", 1, response_received=True)
    with pytest.raises(RuntimeError, match="wallet unavailable"):
        await GENERATION_RECONCILER._apply_timeout(
            _context(_ExplodingBilling()),
            task,
        )
    assert task.status == "running"
    assert task.error_code is None


@pytest.mark.asyncio
async def test_bonus_billing_reconciler_repairs_succeeded_row_without_ledger() -> None:
    generation = SimpleNamespace(
        id="bonus-1",
        status="succeeded",
        upstream_request={
            "billing_policy": "dual_race_loser_settled_separately",
            "billing_free": False,
        },
    )
    image = SimpleNamespace(width=1024, height=1024)
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _BonusSession([[generation], [image]])

    result = await BONUS_BILLING_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert billing.calls == [
        (
            "settle_generation",
            {"width": 1024, "height": 1024, "image_count": 1},
        )
    ]
    assert "deleted_at" not in str(context.session.statements[1].whereclause)


@pytest.mark.asyncio
async def test_bonus_billing_reconciler_skips_missing_image_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing = SimpleNamespace(
        id="bonus-missing",
        status="succeeded",
        upstream_request={
            "billing_policy": "dual_race_loser_settled_separately",
            "billing_free": False,
        },
    )
    valid = SimpleNamespace(
        id="bonus-valid",
        status="succeeded",
        upstream_request={
            "billing_policy": "batch_extra_settled_separately",
            "billing_free": False,
        },
    )
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _BonusSession(
        [
            [missing, valid],
            [],
            [SimpleNamespace(width=512, height=768)],
        ]
    )

    with caplog.at_level(logging.ERROR, logger="test-reconcile"):
        result = await BONUS_BILLING_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert billing.calls == [
        (
            "settle_generation",
            {"width": 512, "height": 768, "image_count": 1},
        )
    ]
    assert "bonus-missing" in caplog.text


@pytest.mark.asyncio
async def test_bonus_billing_reconciler_isolates_single_settlement_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failed = SimpleNamespace(
        id="bonus-failed",
        status="succeeded",
        upstream_request={
            "billing_policy": "dual_race_loser_settled_separately",
            "billing_free": False,
        },
    )
    valid = SimpleNamespace(
        id="bonus-valid",
        status="succeeded",
        upstream_request={
            "billing_policy": "batch_extra_settled_separately",
            "billing_free": False,
        },
    )

    class _FailFirstBilling(_RecordingBilling):
        async def settle_generation(
            self,
            _session: Any,
            generation: Any,
            **kwargs: Any,
        ) -> None:
            if generation.id == "bonus-failed":
                raise RuntimeError("malformed billing row")
            self.calls.append(("settle_generation", kwargs))

    billing = _FailFirstBilling()
    context = _context(billing)
    context.session = _BonusSession(
        [
            [failed, valid],
            [SimpleNamespace(width=256, height=256)],
            [SimpleNamespace(width=1024, height=1536)],
        ]
    )

    with caplog.at_level(logging.ERROR, logger="test-reconcile"):
        result = await BONUS_BILLING_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert billing.calls == [
        (
            "settle_generation",
            {"width": 1024, "height": 1536, "image_count": 1},
        )
    ]
    assert "bonus-failed" in caplog.text
