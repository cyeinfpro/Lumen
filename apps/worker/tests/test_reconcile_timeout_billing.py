"""对账器把卡死任务判超时时的计费动作。

核心不变式：可信实际用量已持久化时按实际用量结算；当前 execution 只要已经
dispatch 且没有 durable 的 undelivered/no-cost 证据，就按 hold 结算。稳定
idempotency key 只允许安全重放，不把最终未知成本改成 release。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from lumen_core.constants import CompletionStatus, GenerationStatus
from lumen_core.upstream_billing import (
    GENERATION_TAKEOVER_CHECKPOINT_KEY,
    mark_upstream_dispatch_proven_no_cost,
)

from app.reconciliation.contracts import ReconcileContext
from app.reconciliation.bonus_billing import BONUS_BILLING_RECONCILER
from app.reconciliation.task_domains import (
    COMPLETION_RECONCILER,
    GENERATION_RECONCILER,
    RECON_RESULT_UNKNOWN_CODE,
    RECON_TIMEOUT_CODE,
    settle_completion_actual_or_unknown,
)
from app.tasks.completion_parts.default_runtime_parts import persistence_runtime
from app.tasks.generation_parts.execution_boundary import (
    release_or_settle_generation,
)


class _RecordingBilling:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str):
        async def _call(_session: Any, _task: Any, **kwargs: Any) -> None:
            self.calls.append((name, kwargs))

        return _call

    def __getattr__(self, name: str):
        if (
            name.startswith("release_")
            or name.startswith("settle_")
            or name.startswith("charge_")
        ):
            return self._record(name)
        raise AttributeError(name)


class _NullSession:
    async def get(self, _model: Any, _pk: Any) -> None:
        return None


class _TaskSession(_NullSession):
    def __init__(self, task: Any) -> None:
        self.task = task

    async def execute(self, _statement: Any) -> _ScalarResult:
        return _ScalarResult([self.task])


class _ExpiredLeaseRedis:
    async def get(self, _key: str) -> None:
        return None


class _MappedLeaseRedis:
    def __init__(self, active_task_ids: set[str]) -> None:
        self.active_task_ids = active_task_ids

    async def get(self, key: str) -> str | None:
        task_id = key.removeprefix("task:").removesuffix(":lease")
        return "active-lease" if task_id in self.active_task_ids else None


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


def _render(statement: Any) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class _PagedTaskSession(_NullSession):
    def __init__(self, pages: list[list[Any]], lock_rows: dict[str, Any]) -> None:
        self.pages = [_ScalarResult(page) for page in pages]
        self.lock_rows = lock_rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        rendered = _render(statement)
        if "FOR UPDATE" not in rendered:
            return self.pages.pop(0)
        for task_id, task in self.lock_rows.items():
            if f"'{task_id}'" in rendered:
                return _ScalarResult([task])
        return _ScalarResult([])


class _ImageFilteringBonusSession(_BonusSession):
    def __init__(
        self,
        generations: list[Any],
        images: dict[str, Any],
    ) -> None:
        super().__init__([])
        self.generations = generations
        self.images = images
        self.candidate_query_seen = False

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        rendered = _render(statement)
        if not self.candidate_query_seen:
            self.candidate_query_seen = True
            candidates = self.generations
            if "EXISTS (SELECT images.id" in rendered:
                candidates = [
                    generation
                    for generation in candidates
                    if generation.id in self.images
                ]
            if "billing_free" in rendered:
                candidates = [
                    generation
                    for generation in candidates
                    if generation.upstream_request.get("billing_free") is not True
                ]
            return _ScalarResult(candidates[:100])
        for generation_id, image in self.images.items():
            if f"'{generation_id}'" in rendered:
                return _ScalarResult([image])
        return _ScalarResult([])


class _LedgerFilteringBonusSession(_BonusSession):
    def __init__(
        self,
        generation: Any,
        image: Any,
        *,
        transaction_kind: str,
        actual_micro: int | None,
        rate_multiplier_x10000: int | None = None,
    ) -> None:
        super().__init__([])
        self.generation = generation
        self.image = image
        self.transaction_kind = transaction_kind
        self.actual_micro = actual_micro
        self.rate_multiplier_x10000 = rate_multiplier_x10000
        self.candidate_query_seen = False

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        rendered = _render(statement)
        if not self.candidate_query_seen:
            self.candidate_query_seen = True
            broad_consumption_guard = (
                "wallet_transactions.kind IN ('settle', 'release')" in rendered
            )
            positive_settlement_guard = (
                "wallet_transactions.kind = 'settle'" in rendered
                and "actual_micro" in rendered
                and "> 0" in rendered
            )
            completed_zero_guard = "rate_multiplier_x10000" in rendered
            blocked = broad_consumption_guard
            if not blocked and self.transaction_kind == "settle":
                blocked = (
                    positive_settlement_guard
                    and (self.actual_micro is None or self.actual_micro > 0)
                ) or (
                    completed_zero_guard
                    and self.actual_micro == 0
                    and self.rate_multiplier_x10000 == 0
                )
            return _ScalarResult([] if blocked else [self.generation])
        return _ScalarResult([self.image])


class _StatefulBonusSession(_BonusSession):
    def __init__(
        self,
        generations: list[Any],
        images: dict[str, Any],
    ) -> None:
        super().__init__([])
        self.generations = generations
        self.images = images
        self.settlements: dict[str, tuple[int | None, int | None]] = {}
        self.candidate_statements: list[Any] = []

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        rendered = _render(statement)
        if "FROM generations" in rendered and "FOR UPDATE" in rendered:
            self.candidate_statements.append(statement)
            completed_zero_guard = "rate_multiplier_x10000" in rendered
            candidates: list[Any] = []
            for generation in self.generations:
                if generation.id not in self.images:
                    continue
                if generation.upstream_request.get("billing_free") is True:
                    continue
                settlement = self.settlements.get(generation.id)
                if settlement is not None:
                    actual_micro, rate_multiplier = settlement
                    if actual_micro is None or actual_micro > 0:
                        continue
                    if (
                        completed_zero_guard
                        and actual_micro == 0
                        and rate_multiplier == 0
                    ):
                        continue
                candidates.append(generation)
            return _ScalarResult(candidates[:100])
        for generation_id, image in self.images.items():
            if f"'{generation_id}'" in rendered:
                return _ScalarResult([image])
        return _ScalarResult([])


class _StatefulBonusBilling(_RecordingBilling):
    def __init__(self, session: _StatefulBonusSession) -> None:
        super().__init__()
        self.session = session
        self.settled_generation_ids: list[str] = []

    async def settle_generation(
        self,
        _session: Any,
        generation: Any,
        **kwargs: Any,
    ) -> None:
        if generation.id in self.session.settlements:
            return
        rate_multiplier = int(
            generation.upstream_request.get(
                "billing_rate_multiplier_x10000",
                10_000,
            )
        )
        actual_micro = 0 if rate_multiplier == 0 else 900
        self.session.settlements[generation.id] = (
            actual_micro,
            rate_multiplier,
        )
        self.settled_generation_ids.append(generation.id)
        self.calls.append(("settle_generation", kwargs))


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
    task_id: str = "task-1",
    updated_at: datetime | None = None,
    dispatch_started: bool = False,
    response_received: bool = False,
    execution_epoch: int = 3,
    receipt_epoch: int | None = None,
    stable_idempotency: bool = False,
    usage_epoch: int | None = None,
    tokens_out: int = 0,
    image_output_tokens: int = 0,
) -> SimpleNamespace:
    marker_epoch = execution_epoch if receipt_epoch is None else receipt_epoch
    upstream_request: dict[str, Any] = {}
    if dispatch_started or response_received:
        upstream_request.update(
            {
                "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
                "upstream_dispatch_attempt": attempt,
                "upstream_dispatch_execution_epoch": marker_epoch,
            }
        )
    if response_received:
        upstream_request.update(
            {
                "upstream_response_received_at": "2026-07-30T00:00:01+00:00",
                "upstream_response_attempt": attempt,
                "upstream_response_execution_epoch": marker_epoch,
            }
        )
    if stable_idempotency:
        upstream_request.update(
            {
                "provider_idempotency_key": "provider-key-1",
                "provider_idempotency_stable": True,
            }
        )
    if usage_epoch is not None:
        upstream_request["completion_usage_execution_epoch"] = usage_epoch
    return SimpleNamespace(
        id=task_id,
        user_id="user-1",
        message_id="msg-1",
        status=status,
        progress_stage=status,
        attempt=attempt,
        execution_epoch=execution_epoch,
        error_code=None,
        error_message=None,
        finished_at=None,
        updated_at=updated_at,
        upstream_request=upstream_request,
        cancel_requested_at=None,
        tokens_in=0,
        tokens_out=tokens_out,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=image_output_tokens,
    )


async def _timeout(reconciler: Any, task: SimpleNamespace) -> _RecordingBilling:
    billing = _RecordingBilling()
    await reconciler._apply_timeout(_context(billing), task)
    return billing


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("no_dispatch", "release_completion"),
        ("dispatch_only", "settle_completion_unknown_upstream"),
        ("response", "settle_completion_unknown_upstream"),
        ("proven_no_cost", "release_completion"),
        ("usage", "charge_completion"),
    ],
)
async def test_completion_live_and_reconcile_billing_actions_match(
    case: str,
    expected: str,
) -> None:
    task = _task(
        CompletionStatus.STREAMING.value,
        1,
        dispatch_started=case == "dispatch_only",
        response_received=case == "response",
        execution_epoch=7,
        usage_epoch=7 if case == "usage" else None,
        tokens_out=9 if case == "usage" else 0,
    )
    if case == "proven_no_cost":
        task.upstream_request = mark_upstream_dispatch_proven_no_cost(
            task,
            at="2026-08-03T00:00:00+00:00",
            attempt=1,
            execution_epoch=7,
        )
    live_billing = _RecordingBilling()
    reconcile_billing = _RecordingBilling()

    await persistence_runtime.settle_failed_billing(
        object(),
        task,
        usage_values=((9,) if case == "usage" else (0,)),
        reason="failed",
        worker_billing=live_billing,
    )
    await COMPLETION_RECONCILER._settle_timeout_billing(
        _context(reconcile_billing),
        task,
        reason="failed",
    )

    assert [name for name, _ in live_billing.calls] == [expected]
    assert [name for name, _ in reconcile_billing.calls] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("no_dispatch", "release"),
        ("dispatch_only", "settle"),
        ("response", "settle"),
        ("proven_no_cost", "release"),
    ],
)
async def test_generation_live_and_reconcile_billing_actions_match(
    case: str,
    expected: str,
) -> None:
    task = _task(
        GenerationStatus.RUNNING.value,
        1,
        dispatch_started=case == "dispatch_only",
        response_received=case == "response",
        execution_epoch=7,
    )
    if case == "proven_no_cost":
        task.upstream_request = mark_upstream_dispatch_proven_no_cost(
            task,
            at="2026-08-03T00:00:00+00:00",
            attempt=1,
            execution_epoch=7,
        )
    actions: list[str] = []

    class LiveBilling:
        async def release(self, *_args: Any, **_kwargs: Any) -> None:
            actions.append("release")

        async def settle_unknown_upstream(
            self,
            *_args: Any,
            **_kwargs: Any,
        ) -> None:
            actions.append("settle")

    class ReconcileBilling(_RecordingBilling):
        async def release_generation(
            self,
            *_args: Any,
            **_kwargs: Any,
        ) -> None:
            actions.append("release")

        async def settle_generation_unknown_upstream(
            self,
            *_args: Any,
            **_kwargs: Any,
        ) -> None:
            actions.append("settle")

    await release_or_settle_generation(
        LiveBilling(),
        object(),
        task,
        reason="failed",
    )
    await GENERATION_RECONCILER._settle_timeout_billing(
        _context(ReconcileBilling()),
        task,
        reason="failed",
    )

    assert actions == [expected, expected]


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
async def test_completion_crash_after_dispatch_settles_default() -> None:
    billing = _RecordingBilling()
    task = _task(
        CompletionStatus.STREAMING.value,
        1,
        dispatch_started=True,
    )

    await settle_completion_actual_or_unknown(
        billing,
        object(),
        task,
        reason="completion_result_unknown",
        knowledge="unknown",
    )

    assert billing.calls == [
        (
            "settle_completion_unknown_upstream",
            {
                "reason": "completion_result_unknown",
                "knowledge": "unknown",
            },
        )
    ]


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
@pytest.mark.parametrize(
    "method_name",
    ["_apply_timeout", "_apply_result_unknown", "_apply_cancel"],
)
async def test_completion_reconciliation_charges_trusted_persisted_usage(
    method_name: str,
) -> None:
    task = _task(
        CompletionStatus.STREAMING.value,
        1,
        execution_epoch=7,
        usage_epoch=7,
        tokens_out=41,
        image_output_tokens=37,
    )
    billing = _RecordingBilling()

    await getattr(COMPLETION_RECONCILER, method_name)(_context(billing), task)

    assert billing.calls == [("charge_completion", {})]


@pytest.mark.asyncio
async def test_completion_reconciler_adopts_completed_response_checkpoint() -> None:
    task = _task(
        CompletionStatus.STREAMING.value,
        2,
        response_received=True,
        execution_epoch=7,
        usage_epoch=7,
        tokens_out=41,
    )
    task.text = "durable final answer"
    task.upstream_request.update(
        {
            "completion_checkpoint_version": 1,
            "completion_checkpoint_execution_epoch": 7,
            "completion_checkpoint_attempt_epoch": 2,
            "completion_checkpoint_response_id": "resp-1",
            "completion_checkpoint_usage_exact": True,
            "completion_checkpoint_usage_complete": True,
            "completion_checkpoint_state": "billing_ready",
            "completion_checkpoint_images": [],
            "completion_usage_attempt_epoch": 2,
        }
    )
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _TaskSession(task)
    context.redis = _ExpiredLeaseRedis()
    context.stage_event = lambda _session, *, kind, payload: (
        "event-1",
        kind,
        payload,
    )

    result = await COMPLETION_RECONCILER.reconcile(context)
    repeated = await COMPLETION_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert repeated.touched == 0
    assert task.status == CompletionStatus.SUCCEEDED.value
    assert task.error_code is None
    assert billing.calls == [("charge_completion", {})]
    assert len(result.pending_outbox) == 1
    assert result.pending_outbox[0][2]["event_name"] == "completion.succeeded"


@pytest.mark.asyncio
async def test_completion_reconciler_adopts_inexact_usage_checkpoint_once() -> None:
    task = _task(
        CompletionStatus.STREAMING.value,
        2,
        response_received=True,
        execution_epoch=7,
        usage_epoch=7,
        tokens_out=41,
    )
    task.text = "durable inexact answer"
    task.upstream_request.update(
        {
            "completion_checkpoint_version": 1,
            "completion_checkpoint_execution_epoch": 7,
            "completion_checkpoint_attempt_epoch": 2,
            "completion_checkpoint_response_id": "resp-inexact",
            "completion_checkpoint_usage_exact": False,
            "completion_checkpoint_usage_complete": False,
            "completion_checkpoint_state": "artifacts_committed",
            "completion_checkpoint_images": [],
            "completion_usage_attempt_epoch": 2,
        }
    )
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _TaskSession(task)
    context.redis = _ExpiredLeaseRedis()
    context.stage_event = lambda _session, *, kind, payload: (
        "event-inexact",
        kind,
        payload,
    )

    result = await COMPLETION_RECONCILER.reconcile(context)
    repeated = await COMPLETION_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert repeated.touched == 0
    assert task.status == CompletionStatus.SUCCEEDED.value
    assert task.error_code is None
    assert billing.calls == [("charge_completion", {})]
    assert len(result.pending_outbox) == 1
    event = result.pending_outbox[0][2]
    assert event["event_name"] == "completion.succeeded"
    assert event["data"]["text"] == "durable inexact answer"
    assert event["data"]["response_id"] == "resp-inexact"


@pytest.mark.asyncio
async def test_completion_old_usage_marker_falls_back_to_unknown_hold() -> None:
    task = _task(
        CompletionStatus.STREAMING.value,
        1,
        response_received=True,
        execution_epoch=8,
        usage_epoch=7,
        tokens_out=41,
        image_output_tokens=37,
    )

    billing = await _timeout(COMPLETION_RECONCILER, task)

    assert billing.calls == [
        (
            "settle_completion_unknown_upstream",
            {"reason": RECON_TIMEOUT_CODE, "knowledge": "unknown"},
        )
    ]


@pytest.mark.asyncio
async def test_old_response_receipt_cannot_settle_new_execution_hold() -> None:
    task = _task(
        "running",
        1,
        response_received=True,
        execution_epoch=8,
        receipt_epoch=7,
    )

    billing = await _timeout(GENERATION_RECONCILER, task)

    assert billing.calls == [("release_generation", {"reason": RECON_TIMEOUT_CODE})]


@pytest.mark.asyncio
async def test_generation_crash_after_dispatch_converges_to_settled_unknown() -> None:
    task = _task("running", 1, dispatch_started=True)
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _TaskSession(task)
    context.redis = _ExpiredLeaseRedis()

    result = await GENERATION_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert task.status == "failed"
    assert task.error_code == RECON_RESULT_UNKNOWN_CODE
    assert [name for name, _ in billing.calls] == [
        "settle_generation_unknown_upstream"
    ]
    assert billing.calls[0][1] == {
        "reason": RECON_RESULT_UNKNOWN_CODE,
        "knowledge": "unknown",
    }
    assert len(result.pending_outbox) == 1


@pytest.mark.asyncio
async def test_completion_crash_after_dispatch_converges_to_settled_unknown() -> None:
    task = _task(CompletionStatus.STREAMING.value, 1, dispatch_started=True)
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _TaskSession(task)
    context.redis = _ExpiredLeaseRedis()

    result = await COMPLETION_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert task.status == CompletionStatus.FAILED.value
    assert task.error_code == RECON_RESULT_UNKNOWN_CODE
    assert billing.calls == [
        (
            "settle_completion_unknown_upstream",
            {
                "reason": RECON_RESULT_UNKNOWN_CODE,
                "knowledge": "unknown",
            },
        )
    ]


@pytest.mark.asyncio
async def test_stable_idempotency_exhaustion_still_settles_dispatch_cost() -> None:
    task = _task(
        GenerationStatus.RUNNING.value,
        GENERATION_RECONCILER.spec.max_attempts,
        dispatch_started=True,
        stable_idempotency=True,
    )

    billing = await _timeout(GENERATION_RECONCILER, task)

    assert billing.calls == [
        (
            "settle_generation_unknown_upstream",
            {"reason": RECON_TIMEOUT_CODE, "knowledge": "unknown"},
        )
    ]


@pytest.mark.asyncio
async def test_generation_takeover_checkpoint_precedes_result_unknown() -> None:
    payload = b"durable-generation-result"
    task = _task(
        "running",
        1,
        task_id="generation-checkpoint-reconcile",
        response_received=True,
        execution_epoch=7,
    )
    task.upstream_request[GENERATION_TAKEOVER_CHECKPOINT_KEY] = {
        "version": 1,
        "execution_epoch": 7,
        "attempt": 1,
        "storage_key": (
            "u/user-1/g/generation-checkpoint-reconcile/executions/7/"
            "attempts/1/takeover-result.bin"
        ),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "revised_prompt": "restored",
        "provider": "provider-1",
        "route": "image2",
        "source": "image2_direct",
        "endpoint": "images/generations",
    }
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _TaskSession(task)
    context.redis = _ExpiredLeaseRedis()

    result = await GENERATION_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert task.status == "queued"
    assert task.error_code is None
    assert task.upstream_request[GENERATION_TAKEOVER_CHECKPOINT_KEY]["sha256"] == (
        hashlib.sha256(payload).hexdigest()
    )
    assert billing.calls == []
    assert len(result.pending_outbox) == 2


@pytest.mark.asyncio
async def test_stable_provider_idempotency_allows_safe_requeue() -> None:
    task = _task(
        "running",
        1,
        dispatch_started=True,
        stable_idempotency=True,
    )
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _TaskSession(task)
    context.redis = _ExpiredLeaseRedis()

    result = await GENERATION_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert task.status == "queued"
    assert billing.calls == []
    assert len(result.pending_outbox) == 2


@pytest.mark.asyncio
async def test_task_reconciler_scans_past_more_than_one_batch_of_active_leases() -> (
    None
):
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)
    stale_at = now - timedelta(minutes=10)
    skipped = [
        _task(
            "running",
            1,
            task_id=f"skip-{index:03d}",
            updated_at=stale_at,
        )
        for index in range(101)
    ]
    valid = _task(
        "running",
        1,
        task_id="valid-z",
        updated_at=stale_at,
        stable_idempotency=True,
    )
    billing = _RecordingBilling()
    context = _context(billing)
    context.now = now
    context.redis = _MappedLeaseRedis({task.id for task in skipped})
    context.session = _PagedTaskSession(
        [skipped[:100], [skipped[100], valid]],
        {valid.id: valid},
    )

    result = await GENERATION_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert valid.status == "queued"
    assert billing.calls == []
    assert len(result.pending_outbox) == 2
    rendered = [_render(statement) for statement in context.session.statements]
    assert "FOR UPDATE" not in rendered[0]
    assert "FOR UPDATE" not in rendered[1]
    assert "generations.updated_at >" in rendered[1]
    assert "FOR UPDATE SKIP LOCKED" in rendered[2]
    assert "'valid-z'" in rendered[2]


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
        user_id="user-1",
        status="succeeded",
        upstream_request={
            "billing_policy": "dual_race_loser_settled_separately",
            "billing_free": False,
        },
    )
    image = SimpleNamespace(width=1024, height=1024)
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _BonusSession([[generation], [image], ["settlement-bonus-1"]])

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
        user_id="user-1",
        status="succeeded",
        upstream_request={
            "billing_policy": "dual_race_loser_settled_separately",
            "billing_free": False,
        },
    )
    valid = SimpleNamespace(
        id="bonus-valid",
        user_id="user-1",
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
            ["settlement-bonus-valid"],
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
async def test_bonus_billing_filters_more_than_100_missing_images_before_limit() -> (
    None
):
    missing = [
        SimpleNamespace(
            id=f"bonus-missing-{index:03d}",
            status="succeeded",
            upstream_request={
                "billing_policy": "dual_race_loser_settled_separately",
                "billing_free": False,
            },
        )
        for index in range(101)
    ]
    valid = SimpleNamespace(
        id="bonus-valid-z",
        status="succeeded",
        upstream_request={
            "billing_policy": "batch_extra_settled_separately",
            "billing_free": False,
        },
    )
    image = SimpleNamespace(width=768, height=1024)
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _ImageFilteringBonusSession(
        [*missing, valid],
        {valid.id: image},
    )

    result = await BONUS_BILLING_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert billing.calls == [
        (
            "settle_generation",
            {"width": 768, "height": 1024, "image_count": 1},
        )
    ]
    candidate_sql = _render(context.session.statements[0])
    assert "EXISTS (SELECT images.id" in candidate_sql
    assert "billing_free" in candidate_sql
    assert candidate_sql.index("EXISTS (SELECT images.id") < candidate_sql.index(
        "LIMIT 100"
    )


@pytest.mark.asyncio
async def test_bonus_billing_filters_free_rows_before_limit() -> None:
    free_rows = [
        SimpleNamespace(
            id=f"bonus-free-{index:03d}",
            status="succeeded",
            upstream_request={
                "billing_policy": "dual_race_loser_settled_separately",
                "billing_free": True,
            },
        )
        for index in range(101)
    ]
    valid = SimpleNamespace(
        id="bonus-billable-z",
        status="succeeded",
        upstream_request={
            "billing_policy": "batch_extra_settled_separately",
            "billing_free": False,
        },
    )
    image = SimpleNamespace(width=1024, height=1024)
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _ImageFilteringBonusSession(
        [*free_rows, valid],
        {
            **{
                generation.id: SimpleNamespace(width=512, height=512)
                for generation in free_rows
            },
            valid.id: image,
        },
    )

    result = await BONUS_BILLING_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert billing.calls == [
        (
            "settle_generation",
            {"width": 1024, "height": 1024, "image_count": 1},
        )
    ]
    candidate_sql = _render(context.session.statements[0])
    assert "billing_free" in candidate_sql
    assert candidate_sql.index("billing_free") < candidate_sql.index("LIMIT 100")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transaction_kind", "actual_micro"),
    [
        ("release", None),
        ("settle", 0),
    ],
)
async def test_bonus_billing_reconciles_without_positive_settlement(
    transaction_kind: str,
    actual_micro: int | None,
) -> None:
    generation = SimpleNamespace(
        id="bonus-after-release",
        status="succeeded",
        upstream_request={
            "billing_policy": "dual_race_loser_settled_separately",
            "billing_free": False,
        },
    )
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _LedgerFilteringBonusSession(
        generation,
        SimpleNamespace(width=1280, height=720),
        transaction_kind=transaction_kind,
        actual_micro=actual_micro,
    )

    result = await BONUS_BILLING_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert billing.calls == [
        (
            "settle_generation",
            {"width": 1280, "height": 720, "image_count": 1},
        )
    ]
    candidate_sql = _render(context.session.statements[0])
    assert "wallet_transactions.kind = 'settle'" in candidate_sql
    assert "actual_micro" in candidate_sql
    assert "> 0" in candidate_sql
    assert "wallet_transactions.kind IN ('settle', 'release')" not in candidate_sql


@pytest.mark.asyncio
async def test_bonus_billing_skips_completed_zero_rate_settlement() -> None:
    generation = SimpleNamespace(
        id="bonus-zero-rate",
        status="succeeded",
        upstream_request={
            "billing_policy": "batch_extra_settled_separately",
            "billing_free": False,
        },
    )
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _LedgerFilteringBonusSession(
        generation,
        SimpleNamespace(width=1024, height=1024),
        transaction_kind="settle",
        actual_micro=0,
        rate_multiplier_x10000=0,
    )

    result = await BONUS_BILLING_RECONCILER.reconcile(context)

    assert result.touched == 0
    assert billing.calls == []
    candidate_sql = _render(context.session.statements[0])
    assert "rate_multiplier_x10000" in candidate_sql
    assert candidate_sql.index("rate_multiplier_x10000") < candidate_sql.index(
        "LIMIT 100"
    )


@pytest.mark.asyncio
async def test_bonus_billing_zero_rate_settlements_do_not_starve_later_debt() -> None:
    zero_rate_rows = [
        SimpleNamespace(
            id=f"bonus-zero-{index:03d}",
            status="succeeded",
            upstream_request={
                "billing_policy": "dual_race_loser_settled_separately",
                "billing_free": False,
                "billing_rate_multiplier_x10000": 0,
            },
        )
        for index in range(101)
    ]
    owed = SimpleNamespace(
        id="bonus-owed-z",
        status="succeeded",
        upstream_request={
            "billing_policy": "batch_extra_settled_separately",
            "billing_free": False,
            "billing_rate_multiplier_x10000": 10_000,
        },
    )
    generations = [*zero_rate_rows, owed]
    images = {
        generation.id: SimpleNamespace(width=1024, height=1024)
        for generation in generations
    }
    session = _StatefulBonusSession(generations, images)
    billing = _StatefulBonusBilling(session)
    context = _context(billing)
    context.session = session

    first = await BONUS_BILLING_RECONCILER.reconcile(context)
    second = await BONUS_BILLING_RECONCILER.reconcile(context)

    assert first.touched == 100
    assert second.touched == 2
    assert billing.settled_generation_ids[:100] == [
        generation.id for generation in zero_rate_rows[:100]
    ]
    assert billing.settled_generation_ids[100:] == [
        zero_rate_rows[100].id,
        owed.id,
    ]
    assert session.settlements[owed.id] == (900, 10_000)
    assert len(session.candidate_statements) == 2
    for statement in session.candidate_statements:
        candidate_sql = _render(statement)
        assert "rate_multiplier_x10000" in candidate_sql
        assert candidate_sql.index("rate_multiplier_x10000") < candidate_sql.index(
            "LIMIT 100"
        )


@pytest.mark.asyncio
async def test_bonus_billing_skips_positive_settlement() -> None:
    generation = SimpleNamespace(
        id="bonus-already-settled",
        status="succeeded",
        upstream_request={
            "billing_policy": "batch_extra_settled_separately",
            "billing_free": False,
        },
    )
    billing = _RecordingBilling()
    context = _context(billing)
    context.session = _LedgerFilteringBonusSession(
        generation,
        SimpleNamespace(width=1024, height=1024),
        transaction_kind="settle",
        actual_micro=900,
    )

    result = await BONUS_BILLING_RECONCILER.reconcile(context)

    assert result.touched == 0
    assert billing.calls == []
    assert len(context.session.statements) == 1


@pytest.mark.asyncio
async def test_bonus_billing_reconciler_isolates_single_settlement_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failed = SimpleNamespace(
        id="bonus-failed",
        user_id="user-1",
        status="succeeded",
        upstream_request={
            "billing_policy": "dual_race_loser_settled_separately",
            "billing_free": False,
        },
    )
    valid = SimpleNamespace(
        id="bonus-valid",
        user_id="user-1",
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
            ["settlement-bonus-valid"],
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
