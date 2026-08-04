from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as PILImage

from app import completion_checkpoint
from app.reconciliation.contracts import ReconcileContext
from app.reconciliation import task_domains
from app.reconciliation import coordinator as reconciliation_coordinator
from app.reconciliation.contracts import ReconcileResult
from app.reconciliation.task_domains import COMPLETION_RECONCILER
from app.storage import LocalStorage
from app.storage_writes import StorageWriteCoordinator
from app.tasks.completion_parts import runner as completion_runner
from app.tasks.completion_parts import tool_images
from app.tasks.completion_parts import outcomes as completion_outcomes
from app.tasks.completion_parts.contracts import (
    ClaimResult,
    CompletionCommand,
    CompletionOutcome,
)
from app.tasks.completion_parts.image_storage_runtime import (
    CompletionToolImageBudget,
    CompletionToolImageCodec,
    CompletionToolImageEvents,
    CompletionToolImageRepository,
    CompletionToolImageService,
    CompletionToolImageStorage,
)
from lumen_core.constants import CompletionStatus, MessageStatus
from lumen_core.model_entities import Completion
from lumen_core.pricing import parse_usage


def _valid_image_bytes() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (2, 2), color=(12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


VALID_IMAGE_BYTES = _valid_image_bytes()
VALID_IMAGE_B64 = base64.b64encode(VALID_IMAGE_BYTES).decode("ascii")
VALID_IMAGE_SHA256 = hashlib.sha256(VALID_IMAGE_BYTES).hexdigest()
INVALID_CHECKPOINT_INTEGERS = (
    True,
    -1,
    2.0,
    2.9,
    float("nan"),
    float("inf"),
    "-1",
    "+2",
    "02",
    "2.0",
    "2.9",
    " 2",
    "2 ",
    "NaN",
    "inf",
)
REQUIRED_EXACT_USAGE_PATHS = (
    ("input_tokens",),
    ("output_tokens",),
)
OPTIONAL_EXACT_USAGE_PATHS = (
    ("total_tokens",),
    ("cache_read_tokens",),
    ("cache_creation_tokens",),
    ("cache_creation", "ephemeral_5m_input_tokens"),
    ("cache_creation", "ephemeral_1h_input_tokens"),
    ("input_tokens_details", "cached_tokens"),
    ("output_tokens_details", "reasoning_tokens"),
    ("output_tokens_details", "image_tokens"),
)


def _usage_with_value(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input_tokens": 12,
        "output_tokens": 45,
    }
    target = usage
    for key in path[:-1]:
        nested: dict[str, Any] = {}
        target[key] = nested
        target = nested
    target[path[-1]] = value
    return usage


class _CheckpointUpstreamError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        status_code: int,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _CheckpointSession:
    def __init__(self, completion: Any) -> None:
        self.completion = completion
        self.commits = 0
        self.commit_snapshots: list[dict[str, Any]] = []

    async def __aenter__(self) -> _CheckpointSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _statement: Any) -> _ScalarResult:
        return _ScalarResult(self.completion)

    async def commit(self) -> None:
        self.commits += 1
        self.commit_snapshots.append(
            {
                "upstream_request": deepcopy(self.completion.upstream_request or {}),
                "tokens_out": self.completion.tokens_out,
                "image_output_tokens": self.completion.image_output_tokens,
            }
        )


class _ToolTracker:
    def update_from_response(self, _response: dict[str, Any]) -> list[Any]:
        return []

    def finalize_active(self, _status: str) -> list[Any]:
        return []


def _checkpoint_fixture(
    *,
    image_events: list[dict[str, Any]] | None = None,
) -> tuple[Any, Any, _CheckpointSession]:
    completion = SimpleNamespace(
        id="comp-1",
        attempt=2,
        execution_epoch=7,
        status=CompletionStatus.STREAMING.value,
        cancel_requested_at=None,
        upstream_request={},
        text="",
        tokens_in=0,
        tokens_out=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=0,
    )
    session = _CheckpointSession(completion)
    usage_totals = tool_images._CompletionUsageAccumulator()  # noqa: SLF001
    usage_totals.start_round(input_fallback_tokens=999)

    async def noop_async(*_args: object, **_kwargs: object) -> None:
        return None

    async def store_image(**_kwargs: object) -> tuple[dict[str, Any], int]:
        completion.image_output_tokens = 77
        completion.tokens_out = 77
        return {"image_id": "image-1"}, 500

    state = SimpleNamespace(
        request=SimpleNamespace(
            redis=object(),
            task_id="comp-1",
            channel="task:comp-1",
        ),
        preparation=SimpleNamespace(
            user_id="user-1",
            message_id="message-1",
            attempt=2,
            attempt_epoch=2,
            chat_model="gpt-5.4",
            queue_metadata_payload={"execution_epoch": 7},
        ),
        settlement=SimpleNamespace(lease_lost=asyncio.Event()),
        streaming=SimpleNamespace(
            accumulated_text="",
            accumulated_thinking="",
            has_partial=False,
            tool_images=[],
            stored_image_call_ids=set(),
            reserved_tool_image_budget_micro=0,
            tool_loop_truncated=False,
        ),
        usage=SimpleNamespace(
            completed_response=None,
            usage_totals=usage_totals,
            tool_tracker=_ToolTracker(),
            upstream_provider_event=None,
        ),
        ports=SimpleNamespace(
            persistence=SimpleNamespace(
                Completion=Completion,
                SessionLocal=lambda: session,
                new_uuid7=lambda: "image-checkpoint-1",
                select=__import__("sqlalchemy").select,
            ),
            billing=SimpleNamespace(parse_usage=parse_usage),
            retry=SimpleNamespace(
                _RUNNING_COMPLETION_STATUSES=(CompletionStatus.STREAMING.value,),
                _CompletionEpochSuperseded=RuntimeError,
            ),
            upstream=SimpleNamespace(
                UpstreamError=_CheckpointUpstreamError,
                _extract_completed_output_text=lambda response: response.get(
                    "output_text",
                    "",
                ),
                _extract_reasoning_text_from_response=lambda _response: "",
                _apply_url_citations=lambda text, _citations: text,
                _extract_url_citations=lambda _response: [],
                _finalize_completion_text=lambda text, _response: text,
            ),
            tools=SimpleNamespace(
                _extract_image_events_from_response=lambda _response: list(
                    image_events or []
                ),
                _publish_completion_tool_updates=noop_async,
                _extract_response_image_b64=lambda event: event.get("image_b64"),
                _tool_image_dedupe_key=lambda _event, image_b64: image_b64,
                _extract_response_revised_prompt=lambda _event: None,
                tool_image_service=SimpleNamespace(
                    store_and_publish_tool_image=store_image
                ),
            ),
            events=SimpleNamespace(publish_event=noop_async),
        ),
    )
    return state, completion, session


def _terminal_checkpoint(
    *,
    usage_exact: bool,
    text: str = "durable final answer",
    execution_epoch: int = 7,
    usage_epoch: int = 7,
    response_id: str | None = "resp-1",
    images: list[dict[str, Any]] | None = None,
    version: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="comp-terminal",
        attempt=2,
        execution_epoch=execution_epoch,
        text=text,
        tokens_in=12,
        tokens_out=45,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=0,
        upstream_request={
            "upstream_dispatch_started_at": "2026-08-03T00:00:00+00:00",
            "upstream_dispatch_attempt": 2,
            "upstream_dispatch_execution_epoch": 7,
            "upstream_response_received_at": "2026-08-03T00:00:01+00:00",
            "upstream_response_attempt": 2,
            "upstream_response_execution_epoch": 7,
            "completion_usage_execution_epoch": usage_epoch,
            "completion_usage_attempt_epoch": 2,
            "completion_checkpoint_version": version,
            "completion_checkpoint_execution_epoch": 7,
            "completion_checkpoint_attempt_epoch": 2,
            "completion_checkpoint_response_id": response_id,
            "completion_checkpoint_usage_exact": usage_exact,
            "completion_checkpoint_usage_complete": usage_exact,
            "completion_checkpoint_state": (
                "billing_ready" if usage_exact else "artifacts_committed"
            ),
            "completion_checkpoint_images": list(images or []),
        },
    )


class _ClaimRedis:
    async def eval(self, *_args: Any) -> int:
        return 1


class _ClaimLeaseLost(BaseException):
    pass


class _ClaimSuperseded(RuntimeError):
    pass


class _ClaimSession:
    def __init__(self, completion: Any, message: Any) -> None:
        self.completion = completion
        self.message = message
        self.commits = 0

    async def __aenter__(self) -> _ClaimSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _statement: Any) -> _ScalarResult:
        return _ScalarResult(self.completion)

    async def get(self, model: Any, _row_id: str, **_kwargs: Any) -> Any:
        return self.message if model is _ClaimMessage else None

    async def commit(self) -> None:
        self.commits += 1


class _ClaimMessage:
    pass


class _ClaimUser:
    pass


class _ClaimSessionFactory:
    def __init__(self, rows: list[Any], message: Any) -> None:
        self.rows = iter(rows)
        self.message = message

    def __call__(self) -> _ClaimSession:
        return _ClaimSession(next(self.rows), self.message)


def _claim_checkpoint_row(
    checkpoint: Any,
    *,
    attempt: int | None = None,
) -> Any:
    checkpoint.id = "comp-redelivery"
    checkpoint.user_id = "user-1"
    checkpoint.message_id = "message-1"
    checkpoint.status = CompletionStatus.STREAMING.value
    checkpoint.progress_stage = CompletionStatus.STREAMING.value
    checkpoint.cancel_requested_at = None
    checkpoint.system_prompt = None
    checkpoint.model = "gpt-5.4"
    checkpoint.user_api_credential_id = None
    checkpoint.error_code = None
    checkpoint.error_message = None
    checkpoint.finished_at = None
    checkpoint.created_at = datetime(2026, 8, 3, tzinfo=timezone.utc)
    if attempt is not None:
        checkpoint.attempt = attempt
    return checkpoint


def _claim_state(
    rows: list[Any],
    *,
    message: Any,
) -> tuple[Any, Any]:
    async def noop_async(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def renewer(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.Event().wait()

    async def fail_preflight(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("checkpoint takeover must run before preflight")

    session_factory = _ClaimSessionFactory(rows, message)
    staged: list[Any] = []
    billing_calls: list[str] = []

    async def settle_unknown(
        _session: Any,
        _completion: Any,
        *,
        reason: str,
        knowledge: str,
    ) -> None:
        billing_calls.append(f"{reason}:{knowledge}")

    async def charge(
        _session: Any,
        _completion: Any,
    ) -> None:
        billing_calls.append("charge")

    def stage_event(
        _session: Any,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        delivery = ("event-claim", kind, payload)
        staged.append(delivery)
        return delivery

    ports = SimpleNamespace(
        context=SimpleNamespace(DEFAULT_CHAT_MODEL="gpt-5.4"),
        persistence=SimpleNamespace(
            Completion=Completion,
            Message=_ClaimMessage,
            User=_ClaimUser,
            SessionLocal=session_factory,
            _acquire_completion_xact_lock=noop_async,
            is_completion_terminal=lambda status: (
                status
                in {
                    CompletionStatus.SUCCEEDED.value,
                    CompletionStatus.FAILED.value,
                    CompletionStatus.CANCELED.value,
                }
            ),
            new_uuid7=lambda: "lease-1",
            select=__import__("sqlalchemy").select,
        ),
        retry=SimpleNamespace(
            _acquire_lease=noop_async,
            _lease_renewer=renewer,
            _completion_preflight_failure=fail_preflight,
            _LeaseLost=_ClaimLeaseLost,
            _CompletionEpochSuperseded=_ClaimSuperseded,
        ),
        billing=SimpleNamespace(
            worker_billing=SimpleNamespace(
                charge_completion=charge,
                settle_completion_unknown_upstream=settle_unknown,
                flush_balance_cache_refreshes=noop_async,
            )
        ),
        tools=SimpleNamespace(tool_image_service=object()),
        events=SimpleNamespace(
            logger=logging.getLogger("test-completion-claim"),
            _completion_event_payload=lambda *args, **kwargs: {
                "args": args,
                **kwargs,
            },
            _stage_completion_event=lambda *args: stage_event(
                args[0],
                kind="sse",
                payload=args[-1],
            ),
            _deliver_completion_event=noop_async,
            _COMPLETION_EVENT_HOOKS=SimpleNamespace(
                stage_outbox_event=stage_event,
            ),
        ),
    )
    state = completion_runner._new_execution(  # noqa: SLF001
        CompletionCommand(
            task_id="comp-redelivery",
            redis=_ClaimRedis(),
            worker_id="worker-1",
        ),
        ports,
        object(),
        execution_epoch=7,
    )
    ports.claim_test = SimpleNamespace(
        billing_calls=billing_calls,
        staged=staged,
    )
    return state, ports


async def _stop_claim_renewer(state: Any) -> None:
    renewer = state.settlement.renewer
    if renewer is None:
        return
    renewer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await renewer
    state.settlement.renewer = None


def _claim_only_services(state: Any, upstream_calls: list[str]) -> Any:
    class Repository:
        async def claim(self, execution: Any) -> ClaimResult:
            claimed = await completion_runner.claim_completion(execution)
            return ClaimResult(
                claimed=claimed,
                outcome=CompletionOutcome(execution.settlement.task_outcome),
            )

        async def cleanup(self, execution: Any) -> None:
            await _stop_claim_renewer(execution)

    class Upstream:
        async def consume(self, _execution: Any) -> None:
            upstream_calls.append("consume")

    fail = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # noqa: E731
        AssertionError("post-claim service must not run")
    )
    return SimpleNamespace(
        repository=Repository(),
        context_builder=SimpleNamespace(prepare=fail),
        tool_executor=SimpleNamespace(initialize=fail),
        upstream_client=Upstream(),
        billing=SimpleNamespace(settle_success=fail),
        events=SimpleNamespace(publish_started=fail),
        lease_retry=object(),
    )


@pytest.mark.asyncio
async def test_completed_checkpoint_redelivery_never_dispatches_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = _claim_checkpoint_row(
        _terminal_checkpoint(usage_exact=True, version=2),
    )
    message = SimpleNamespace(
        conversation_id="conversation-1",
        status=MessageStatus.STREAMING.value,
        content={},
    )
    state, _ports = _claim_state([completion], message=message)
    upstream_calls: list[str] = []

    async def take_over(takeover_state: Any, checkpoint_row: Any) -> bool:
        assert checkpoint_row is completion
        assert completion.attempt == 2
        assert completion.text == "durable final answer"
        takeover_state.settlement.task_outcome = CompletionOutcome.SUCCEEDED.value
        return True

    monkeypatch.setattr(
        completion_runner,
        "take_over_completion_checkpoint",
        take_over,
    )
    monkeypatch.setattr(
        completion_runner,
        "bind_completion_execution_fence",
        lambda *_args: None,
    )
    services = _claim_only_services(state, upstream_calls)

    result = await completion_runner._run_completion_scoped(  # noqa: SLF001
        CompletionCommand(
            task_id="comp-redelivery",
            redis=state.request.redis,
            worker_id="worker-1",
        ),
        state,
        services,
    )

    assert result.outcome is CompletionOutcome.SUCCEEDED
    assert upstream_calls == []
    assert completion.attempt == 2
    assert completion.text == "durable final answer"


@pytest.mark.asyncio
async def test_pending_checkpoint_redelivery_never_mutates_or_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = _claim_checkpoint_row(
        _terminal_checkpoint(
            usage_exact=True,
            text="durable pending text",
            version=2,
            images=[
                {
                    "image_id": "image-pending",
                    "dedupe_key": "image-pending",
                    "state": "pending",
                    "image_b64": VALID_IMAGE_B64,
                }
            ],
        ),
    )
    completion.upstream_request["completion_checkpoint_state"] = "artifacts_pending"
    completion.upstream_request["completion_checkpoint_usage_complete"] = False
    message = SimpleNamespace(
        conversation_id="conversation-1",
        status=MessageStatus.STREAMING.value,
        content={},
    )
    state, _ports = _claim_state([completion], message=message)
    upstream_calls: list[str] = []

    async def take_over(takeover_state: Any, checkpoint_row: Any) -> bool:
        assert checkpoint_row is completion
        assert completion.attempt == 2
        assert completion.text == "durable pending text"
        assert [
            image["image_id"]
            for image in completion_checkpoint.completion_checkpoint_pending_images(
                completion
            )
        ] == ["image-pending"]
        takeover_state.settlement.task_outcome = CompletionOutcome.SUCCEEDED.value
        return True

    monkeypatch.setattr(
        completion_runner,
        "take_over_completion_checkpoint",
        take_over,
    )
    monkeypatch.setattr(
        completion_runner,
        "bind_completion_execution_fence",
        lambda *_args: None,
    )
    services = _claim_only_services(state, upstream_calls)

    result = await completion_runner._run_completion_scoped(  # noqa: SLF001
        CompletionCommand(
            task_id="comp-redelivery",
            redis=state.request.redis,
            worker_id="worker-1",
        ),
        state,
        services,
    )

    assert result.outcome is CompletionOutcome.SUCCEEDED
    assert upstream_calls == []
    assert completion.attempt == 2
    assert completion.text == "durable pending text"


@pytest.mark.asyncio
async def test_unknown_checkpoint_version_redelivery_quarantines_before_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = _claim_checkpoint_row(
        _terminal_checkpoint(usage_exact=True, version=99),
        attempt=3,
    )
    message = SimpleNamespace(
        conversation_id="conversation-1",
        status=MessageStatus.STREAMING.value,
        content={},
    )
    state, ports = _claim_state([completion, completion], message=message)
    upstream_calls: list[str] = []
    services = _claim_only_services(state, upstream_calls)
    monkeypatch.setattr(
        completion_runner,
        "bind_completion_execution_fence",
        lambda *_args: None,
    )

    result = await completion_runner._run_completion_scoped(  # noqa: SLF001
        CompletionCommand(
            task_id="comp-redelivery",
            redis=state.request.redis,
            worker_id="worker-1",
        ),
        state,
        services,
    )

    assert result.outcome is CompletionOutcome.FAILED
    assert upstream_calls == []
    assert completion.attempt == 3
    assert completion.status == CompletionStatus.FAILED.value
    assert completion.error_code == "completion_checkpoint_corrupt"
    assert (
        completion.upstream_request["completion_checkpoint_quarantine_reason"]
        == "unsupported completion checkpoint version: 99"
    )
    assert ports.claim_test.billing_calls == ["charge"]


@pytest.mark.asyncio
async def test_checkpoint_takeover_rejects_concurrent_attempt_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _claim_checkpoint_row(
        _terminal_checkpoint(usage_exact=True, version=2),
    )
    current = _claim_checkpoint_row(
        _terminal_checkpoint(usage_exact=True, version=2),
        attempt=3,
    )
    current.text = "new attempt text"
    message = SimpleNamespace(
        conversation_id="conversation-1",
        status=MessageStatus.STREAMING.value,
        content={},
    )
    state, _ports = _claim_state([original, current], message=message)
    monkeypatch.setattr(
        completion_runner,
        "bind_completion_execution_fence",
        lambda *_args: None,
    )

    try:
        with pytest.raises(_ClaimSuperseded):
            await completion_runner.claim_completion(state)
    finally:
        await _stop_claim_renewer(state)

    assert original.attempt == 2
    assert original.text == "durable final answer"
    assert current.attempt == 3
    assert current.text == "new attempt text"


def test_inexact_terminal_checkpoint_with_matching_usage_is_recoverable() -> None:
    completion = _terminal_checkpoint(usage_exact=False)

    assert completion_checkpoint.completion_has_completed_checkpoint(completion)
    assert completion_checkpoint.completion_has_trustworthy_persisted_usage(completion)


@pytest.mark.parametrize(
    "completion",
    [
        _terminal_checkpoint(usage_exact=False, usage_epoch=6),
        _terminal_checkpoint(usage_exact=False, text=""),
        _terminal_checkpoint(usage_exact=False, response_id=None),
        _terminal_checkpoint(
            usage_exact=False,
            images=[
                {
                    "image_id": "image-pending",
                    "dedupe_key": "encoded-image",
                    "state": "pending",
                    "image_b64": VALID_IMAGE_B64,
                }
            ],
        ),
    ],
)
def test_inexact_terminal_checkpoint_fails_closed_when_incomplete(
    completion: SimpleNamespace,
) -> None:
    assert not completion_checkpoint.completion_has_completed_checkpoint(completion)


@pytest.mark.parametrize(
    ("image_b64", "extra", "expected_error"),
    [
        ("not-valid-base64***", {}, "invalid base64"),
        (
            base64.b64encode(b"not an image").decode("ascii"),
            {},
            "not a valid image",
        ),
        (VALID_IMAGE_B64, {"size_bytes": 1}, "size does not match"),
        (VALID_IMAGE_B64, {"sha256": "0" * 64}, "hash does not match"),
    ],
)
def test_checkpoint_validation_quarantines_corrupt_pending_image(
    image_b64: str,
    extra: dict[str, Any],
    expected_error: str,
) -> None:
    completion = _terminal_checkpoint(
        usage_exact=True,
        images=[
            {
                "image_id": "image-corrupt",
                "dedupe_key": "image-corrupt",
                "state": "pending",
                "image_b64": image_b64,
                **extra,
            }
        ],
    )

    error = completion_checkpoint.completion_checkpoint_validation_error(completion)

    assert error is None
    assert completion_checkpoint.completion_checkpoint_pending_images(completion) == []
    assert completion_checkpoint.completion_checkpoint_requires_recovery(completion)
    assert not completion_checkpoint.completion_has_completed_checkpoint(completion)
    normalized = completion_checkpoint.validated_checkpoint_images(
        completion.upstream_request
    )
    assert normalized[0]["state"] == "quarantined"
    assert expected_error in normalized[0]["quarantine_reason"]


def test_legacy_pending_checkpoint_normalizes_missing_hash_and_size() -> None:
    completion = _terminal_checkpoint(
        usage_exact=True,
        images=[
            {
                "image_id": "image-legacy",
                "dedupe_key": "image-legacy",
                "state": "pending",
                "image_b64": VALID_IMAGE_B64,
            }
        ],
    )

    pending = completion_checkpoint.completion_checkpoint_pending_images(completion)

    assert pending == [
        {
            "image_id": "image-legacy",
            "dedupe_key": "image-legacy",
            "state": "pending",
            "image_b64": VALID_IMAGE_B64,
            "size_bytes": len(VALID_IMAGE_BYTES),
            "sha256": VALID_IMAGE_SHA256,
        }
    ]


def test_v2_committed_checkpoint_restores_payload_usage_and_receipt() -> None:
    payload = {
        "image_id": "image-v2",
        "url": "/media/image-v2.png",
    }
    completion = _terminal_checkpoint(
        usage_exact=True,
        version=2,
        images=[
            {
                "image_id": "image-v2",
                "dedupe_key": "persisted:image-v2",
                "state": "committed",
                "payload": payload,
            }
        ],
    )

    assert (
        completion_checkpoint.completion_checkpoint_validation_error(completion) is None
    )
    assert completion_checkpoint.completion_checkpoint_committed_payloads(
        completion
    ) == [payload]
    assert completion_checkpoint.completion_has_completed_checkpoint(completion)
    assert completion_checkpoint.completion_has_trustworthy_persisted_usage(completion)


def test_v2_pending_checkpoint_restores_validated_payload() -> None:
    completion = _terminal_checkpoint(
        usage_exact=True,
        version=2,
        images=[
            {
                "image_id": "image-v2-pending",
                "dedupe_key": "image-v2-pending",
                "state": "pending",
                "image_b64": VALID_IMAGE_B64,
            }
        ],
    )

    pending = completion_checkpoint.completion_checkpoint_pending_images(completion)

    assert pending[0]["image_id"] == "image-v2-pending"
    assert pending[0]["size_bytes"] == len(VALID_IMAGE_BYTES)
    assert pending[0]["sha256"] == VALID_IMAGE_SHA256
    assert completion_checkpoint.completion_checkpoint_requires_recovery(completion)


def test_nested_v2_checkpoint_normalizes_to_canonical_fields() -> None:
    completion = _terminal_checkpoint(
        usage_exact=True,
        version=2,
        images=[],
    )
    request = completion.upstream_request
    nested = {
        "version": request.pop("completion_checkpoint_version"),
        "execution_epoch": request.pop("completion_checkpoint_execution_epoch"),
        "attempt_epoch": request.pop("completion_checkpoint_attempt_epoch"),
        "response_id": request.pop("completion_checkpoint_response_id"),
        "usage_exact": request.pop("completion_checkpoint_usage_exact"),
        "usage_complete": request.pop("completion_checkpoint_usage_complete"),
        "usage": request.pop("completion_checkpoint_usage", None),
        "state": request.pop("completion_checkpoint_state"),
        "images": request.pop("completion_checkpoint_images"),
    }
    request["completion_checkpoint"] = nested

    parsed = completion_checkpoint.parse_completion_checkpoint(completion)

    assert parsed.error is None
    assert parsed.request is not None
    assert parsed.request["completion_checkpoint_version"] == 2
    assert parsed.request["completion_checkpoint_response_id"] == "resp-1"
    assert parsed.request["completion_checkpoint_usage_exact"] is True
    assert parsed.request["completion_checkpoint_images"] == []
    assert completion_checkpoint.completion_checkpoint_requires_recovery(completion)


def test_unknown_checkpoint_version_is_explicitly_invalid() -> None:
    completion = _terminal_checkpoint(
        usage_exact=True,
        version=99,
    )

    error = completion_checkpoint.completion_checkpoint_validation_error(completion)

    assert error == "unsupported completion checkpoint version: 99"
    assert completion_checkpoint.completion_checkpoint_pending_images(completion) == []
    assert not completion_checkpoint.completion_has_completed_checkpoint(completion)


@pytest.mark.parametrize("invalid_version", INVALID_CHECKPOINT_INTEGERS)
def test_checkpoint_version_rejects_lossy_or_noncanonical_integers(
    invalid_version: Any,
) -> None:
    completion = _terminal_checkpoint(usage_exact=True, version=2)
    completion.attempt = 3
    completion.upstream_request["completion_checkpoint_version"] = invalid_version

    parsed = completion_checkpoint.parse_completion_checkpoint(completion)

    assert parsed.error == "completion checkpoint version is invalid"
    assert parsed.stale is False


@pytest.mark.parametrize("invalid_identity", INVALID_CHECKPOINT_INTEGERS)
@pytest.mark.parametrize(
    "field",
    [
        "completion_checkpoint_execution_epoch",
        "completion_checkpoint_attempt_epoch",
    ],
)
def test_checkpoint_identity_rejects_lossy_or_noncanonical_integers(
    field: str,
    invalid_identity: Any,
) -> None:
    completion = _terminal_checkpoint(usage_exact=True, version=2)
    completion.upstream_request[field] = invalid_identity

    parsed = completion_checkpoint.parse_completion_checkpoint(completion)

    assert parsed.error == "completion checkpoint execution identity is invalid"
    assert parsed.stale is False


@pytest.mark.parametrize("invalid_identity", INVALID_CHECKPOINT_INTEGERS)
@pytest.mark.parametrize(
    "field",
    [
        "completion_usage_execution_epoch",
        "completion_usage_attempt_epoch",
        "upstream_response_execution_epoch",
        "upstream_response_attempt",
    ],
)
def test_checkpoint_receipts_reject_lossy_or_noncanonical_integers(
    field: str,
    invalid_identity: Any,
) -> None:
    completion = _terminal_checkpoint(usage_exact=True, version=2)
    completion.upstream_request[field] = invalid_identity

    parsed = completion_checkpoint.parse_completion_checkpoint(completion)

    assert parsed.error == (
        "completion checkpoint response or usage receipt is invalid"
    )
    assert parsed.stale is False


@pytest.mark.parametrize("invalid_identity", INVALID_CHECKPOINT_INTEGERS)
@pytest.mark.parametrize("attribute", ["execution_epoch", "attempt"])
def test_completion_identity_rejects_lossy_or_noncanonical_integers(
    attribute: str,
    invalid_identity: Any,
) -> None:
    completion = _terminal_checkpoint(usage_exact=True, version=2)
    setattr(completion, attribute, invalid_identity)

    parsed = completion_checkpoint.parse_completion_checkpoint(completion)

    assert parsed.error == "completion execution identity is invalid"
    assert parsed.stale is False


def test_checkpoint_identity_accepts_canonical_integer_strings() -> None:
    completion = _terminal_checkpoint(usage_exact=True, version=2)
    request = completion.upstream_request
    request["completion_checkpoint_version"] = "2"
    request["completion_checkpoint_execution_epoch"] = "7"
    request["completion_checkpoint_attempt_epoch"] = "2"
    request["completion_usage_execution_epoch"] = "7"
    request["completion_usage_attempt_epoch"] = "2"
    request["upstream_response_execution_epoch"] = "7"
    request["upstream_response_attempt"] = "2"

    parsed = completion_checkpoint.parse_completion_checkpoint(completion)

    assert parsed.error is None
    assert parsed.stale is False
    assert parsed.request is not None
    assert parsed.request["completion_checkpoint_version"] == 2


@pytest.mark.asyncio
async def test_response_completed_persists_text_usage_and_response_identity() -> None:
    state, completion, session = _checkpoint_fixture()
    response = {
        "id": "resp-1",
        "output_text": "durable final answer",
        "usage": {
            "input_tokens": 123,
            "output_tokens": 45,
            "total_tokens": 168,
        },
    }

    await completion_runner._handle_completed(  # noqa: SLF001
        state,
        {"type": "response.completed", "response": response},
        append_completed_text=False,
        finalize_tools=False,
    )

    assert completion.text == "durable final answer"
    assert completion.tokens_in == 123
    assert completion.tokens_out == 45
    assert completion.upstream_request["completion_checkpoint_version"] == 2
    assert completion.upstream_request["completion_checkpoint_execution_epoch"] == 7
    assert completion.upstream_request["completion_checkpoint_attempt_epoch"] == 2
    assert completion.upstream_request["completion_checkpoint_response_id"] == "resp-1"
    assert completion.upstream_request["completion_checkpoint_usage_complete"] is True
    assert completion.upstream_request["completion_checkpoint_state"] == "billing_ready"
    assert completion.upstream_request["completion_checkpoint_images"] == []
    assert completion.upstream_request["completion_usage_execution_epoch"] == 7
    assert session.commits == 1


@pytest.mark.asyncio
async def test_response_completed_rejects_invalid_exact_usage_marker() -> None:
    state, completion, _session = _checkpoint_fixture()

    await completion_runner._handle_completed(  # noqa: SLF001
        state,
        {
            "type": "response.completed",
            "response": {
                "id": "resp-invalid",
                "output_text": "durable final answer",
                "usage": {
                    "input_tokens": "not-a-number",
                    "output_tokens": 45,
                },
            },
        },
        append_completed_text=False,
        finalize_tools=False,
    )

    assert completion.upstream_request["completion_checkpoint_usage_complete"] is False
    assert completion.upstream_request["completion_checkpoint_usage_exact"] is False
    assert completion.upstream_request["completion_checkpoint_state"] == (
        "artifacts_committed"
    )
    assert completion_checkpoint.completion_has_completed_checkpoint(completion)


@pytest.mark.asyncio
@pytest.mark.parametrize("field_path", REQUIRED_EXACT_USAGE_PATHS)
@pytest.mark.parametrize("invalid_value", INVALID_CHECKPOINT_INTEGERS)
async def test_response_completed_noncanonical_required_usage_is_unknown(
    field_path: tuple[str, ...],
    invalid_value: Any,
) -> None:
    state, completion, session = _checkpoint_fixture()

    await completion_runner._handle_completed(  # noqa: SLF001
        state,
        {
            "type": "response.completed",
            "response": {
                "id": "resp-invalid-required",
                "output_text": "durable final answer",
                "usage": _usage_with_value(field_path, invalid_value),
            },
        },
        append_completed_text=False,
        finalize_tools=False,
    )

    request = completion.upstream_request
    assert request["completion_checkpoint_usage_complete"] is False
    assert request["completion_checkpoint_usage_exact"] is False
    assert request["completion_checkpoint_state"] == "artifacts_committed"
    assert session.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field_path", OPTIONAL_EXACT_USAGE_PATHS)
async def test_response_completed_noncanonical_optional_usage_is_unknown(
    field_path: tuple[str, ...],
) -> None:
    state, completion, _session = _checkpoint_fixture()

    await completion_runner._handle_completed(  # noqa: SLF001
        state,
        {
            "type": "response.completed",
            "response": {
                "id": "resp-invalid-optional",
                "output_text": "durable final answer",
                "usage": _usage_with_value(field_path, "02"),
            },
        },
        append_completed_text=False,
        finalize_tools=False,
    )

    assert completion.upstream_request["completion_checkpoint_usage_exact"] is False
    assert completion.upstream_request["completion_checkpoint_usage_complete"] is False


@pytest.mark.asyncio
async def test_response_completed_accepts_canonical_usage_strings() -> None:
    state, completion, _session = _checkpoint_fixture()

    await completion_runner._handle_completed(  # noqa: SLF001
        state,
        {
            "type": "response.completed",
            "response": {
                "id": "resp-canonical-strings",
                "output_text": "durable final answer",
                "usage": {
                    "input_tokens": "12",
                    "output_tokens": "45",
                    "cache_read_tokens": "0",
                },
            },
        },
        append_completed_text=False,
        finalize_tools=False,
    )

    assert completion.tokens_in == 12
    assert completion.tokens_out == 45
    assert completion.upstream_request["completion_checkpoint_usage_exact"] is True
    assert completion.upstream_request["completion_checkpoint_usage_complete"] is True


@pytest.mark.asyncio
async def test_tool_loop_truncation_keeps_terminal_checkpoint_recoverable() -> None:
    state, completion, _session = _checkpoint_fixture()
    state.usage.completed_usage_exact = False

    await completion_runner._handle_completed(  # noqa: SLF001
        state,
        {
            "type": "response.completed",
            "response": {
                "id": "resp-truncated",
                "output_text": "durable truncated answer",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 45,
                },
            },
        },
        append_completed_text=False,
        finalize_tools=False,
    )

    assert completion.upstream_request["completion_checkpoint_usage_exact"] is False
    assert completion.upstream_request["completion_checkpoint_usage_complete"] is False
    assert completion_checkpoint.completion_has_completed_checkpoint(completion)


@pytest.mark.asyncio
@pytest.mark.parametrize("output_text", ["", " \n\t"])
async def test_all_corrupt_live_checkpoint_keeps_empty_output_failed(
    output_text: str,
) -> None:
    state, completion, _session = _checkpoint_fixture(
        image_events=[
            {
                "item": {"id": "corrupt-image"},
                "image_b64": "not-valid-base64***",
            }
        ],
    )

    await completion_runner._handle_completed(  # noqa: SLF001
        state,
        {
            "type": "response.completed",
            "response": {
                "id": "resp-corrupt",
                "output_text": output_text,
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 7,
                },
            },
        },
        append_completed_text=False,
        finalize_tools=False,
    )

    assert completion.text == ""
    assert state.streaming.tool_images == []
    assert completion.upstream_request["completion_checkpoint_state"] == (
        "partial_corruption"
    )
    assert completion_checkpoint.completion_checkpoint_has_no_usable_output(completion)
    assert not completion_checkpoint.completion_has_completed_checkpoint(completion)
    with pytest.raises(
        _CheckpointUpstreamError,
        match="upstream returned empty completion",
    ) as exc_info:
        completion_outcomes.completion_final_text(state)
    assert exc_info.value.error_code == "no_text_returned"


@pytest.mark.asyncio
async def test_response_completed_finalizes_checkpoint_after_tool_image_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, completion, session = _checkpoint_fixture(
        image_events=[{"image_b64": VALID_IMAGE_B64}],
    )

    async def persist_image(
        _service: Any,
        *,
        image_record: dict[str, Any],
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], int]:
        completion.image_output_tokens = 77
        completion.tokens_out = 77
        return {"image_id": image_record["image_id"]}, 500

    monkeypatch.setattr(
        completion_checkpoint,
        "persist_completion_checkpoint_image",
        persist_image,
    )

    await completion_runner._handle_completed(  # noqa: SLF001
        state,
        {
            "type": "response.completed",
            "response": {
                "id": "resp-image",
                "output_text": "image result",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 5,
                },
            },
        },
        append_completed_text=False,
        finalize_tools=False,
    )

    assert session.commits == 2
    assert (
        session.commit_snapshots[0]["upstream_request"][
            "completion_checkpoint_usage_complete"
        ]
        is False
    )
    pending = session.commit_snapshots[0]["upstream_request"][
        "completion_checkpoint_images"
    ][0]
    assert pending == {
        "image_id": "image-checkpoint-1",
        "dedupe_key": VALID_IMAGE_B64,
        "state": "pending",
        "image_b64": VALID_IMAGE_B64,
        "size_bytes": len(VALID_IMAGE_BYTES),
        "sha256": VALID_IMAGE_SHA256,
    }
    assert completion.upstream_request["completion_checkpoint_usage_complete"] is True
    assert completion.upstream_request["completion_checkpoint_state"] == "billing_ready"
    committed = completion.upstream_request["completion_checkpoint_images"][0]
    assert committed["image_id"] == "image-checkpoint-1"
    assert committed["state"] == "committed"
    assert committed["payload"] == {"image_id": "image-checkpoint-1"}
    assert "image_b64" not in committed
    assert completion.image_output_tokens == 77
    assert completion.tokens_out == 77


@pytest.mark.asyncio
async def test_response_completed_crash_before_image_save_keeps_recoverable_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _completion, session = _checkpoint_fixture(
        image_events=[{"image_b64": VALID_IMAGE_B64}],
    )

    async def crash_before_save(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("crash before image save")

    monkeypatch.setattr(
        completion_checkpoint,
        "persist_completion_checkpoint_image",
        crash_before_save,
    )

    with pytest.raises(RuntimeError, match="before image save"):
        await completion_runner._handle_completed(  # noqa: SLF001
            state,
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-image",
                    "output_text": "image result",
                    "usage": {"input_tokens": 12, "output_tokens": 5},
                },
            },
            append_completed_text=False,
            finalize_tools=False,
        )

    assert session.commits == 1
    request = session.commit_snapshots[0]["upstream_request"]
    assert request["completion_checkpoint_state"] == "artifacts_pending"
    assert request["completion_checkpoint_usage_complete"] is False
    assert request["completion_checkpoint_images"][0]["image_b64"] == VALID_IMAGE_B64


@pytest.mark.asyncio
async def test_response_completed_crash_after_image_save_keeps_fixed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _completion, session = _checkpoint_fixture(
        image_events=[{"image_b64": VALID_IMAGE_B64}],
    )
    saved_image_ids: list[str] = []
    original_record = completion_checkpoint.record_completed_event_checkpoint
    record_calls = 0

    async def persist_image(
        _service: Any,
        *,
        image_record: dict[str, Any],
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], int]:
        saved_image_ids.append(str(image_record["image_id"]))
        return {"image_id": image_record["image_id"]}, 500

    async def crash_before_second_checkpoint(*args: Any, **kwargs: Any) -> None:
        nonlocal record_calls
        record_calls += 1
        if record_calls == 2:
            raise RuntimeError("crash after image save")
        await original_record(*args, **kwargs)

    monkeypatch.setattr(
        completion_checkpoint,
        "persist_completion_checkpoint_image",
        persist_image,
    )
    monkeypatch.setattr(
        completion_checkpoint,
        "record_completed_event_checkpoint",
        crash_before_second_checkpoint,
    )

    with pytest.raises(RuntimeError, match="after image save"):
        await completion_runner._handle_completed(  # noqa: SLF001
            state,
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-image",
                    "output_text": "image result",
                    "usage": {"input_tokens": 12, "output_tokens": 5},
                },
            },
            append_completed_text=False,
            finalize_tools=False,
        )

    pending = session.commit_snapshots[0]["upstream_request"][
        "completion_checkpoint_images"
    ][0]
    assert session.commits == 1
    assert saved_image_ids == [pending["image_id"]]
    assert pending["state"] == "pending"
    assert pending["image_b64"] == VALID_IMAGE_B64


@pytest.mark.asyncio
async def test_checkpoint_image_store_pins_preallocated_artifact_id() -> None:
    stored_ids: list[str] = []
    completion = SimpleNamespace(
        attempt=2,
        execution_epoch=7,
        status=CompletionStatus.STREAMING.value,
        cancel_requested_at=None,
        upstream_request={},
    )

    @dataclass(frozen=True)
    class Repository:
        new_id: Any
        session_factory: Any
        acquire_task_lock: Any
        completion_model: Any
        image_model: Any
        superseded_error_type: type[Exception] = RuntimeError
        public_url: Any = str

    @dataclass(frozen=True)
    class Service:
        repository: Repository
        budget: Any
        codec: Any

        async def store_tool_image(
            self,
            **kwargs: Any,
        ) -> dict[str, Any]:
            assert kwargs["cleanup_created_files_on_failure"] is False
            image_id = self.repository.new_id()
            stored_ids.append(image_id)
            return {"image_id": image_id}

        async def deliver_tool_image_event(self, **_kwargs: Any) -> None:
            return None

    class CompletionModel:
        pass

    class ImageModel:
        pass

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(
            self,
            model: Any,
            _row_id: str,
            **_kwargs: Any,
        ) -> Any:
            return completion if model is CompletionModel else None

    async def no_op(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def reserve(**_kwargs: Any) -> int:
        return 500

    (
        payload,
        budget_micro,
    ) = await completion_checkpoint.persist_completion_checkpoint_image(
        Service(
            repository=Repository(
                new_id=lambda: "random-image",
                session_factory=Session,
                acquire_task_lock=no_op,
                completion_model=CompletionModel,
                image_model=ImageModel,
            ),
            budget=SimpleNamespace(reserve=reserve),
            codec=SimpleNamespace(
                decode=lambda _value: VALID_IMAGE_BYTES,
                sha256=lambda _value: VALID_IMAGE_SHA256,
            ),
        ),
        redis=object(),
        user_id="user-1",
        channel="task:comp-1",
        task_id="comp-1",
        message_id="message-1",
        attempt=2,
        attempt_epoch=2,
        execution_epoch=7,
        image_record={
            "image_id": "image-checkpoint-1",
            "dedupe_key": "encoded-image",
            "state": "pending",
            "image_b64": VALID_IMAGE_B64,
        },
        reserved_micro=0,
    )

    assert stored_ids == ["image-checkpoint-1"]
    assert payload == {"image_id": "image-checkpoint-1"}
    assert budget_micro == 500


@pytest.mark.asyncio
async def test_concurrent_checkpoint_image_recovery_serializes_single_commit(
    tmp_path: Any,
) -> None:
    image_id = "image-checkpoint-concurrent"
    completion = SimpleNamespace(
        id="comp-concurrent",
        user_id="user-1",
        message_id="message-1",
        attempt=2,
        execution_epoch=7,
        status=CompletionStatus.STREAMING.value,
        cancel_requested_at=None,
        upstream_request={},
        image_output_tokens=0,
        tokens_out=0,
    )
    message = SimpleNamespace(content={})
    task_lock = asyncio.Lock()
    committed_image: Any | None = None
    committed_variants: list[Any] = []
    committed_outboxes: dict[str, Any] = {}
    usage_calls = 0
    write_calls = 0
    emitted_events: set[str] = set()

    class Row:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class CompletionModel:
        pass

    class ImageModel(Row):
        pass

    class VariantModel(Row):
        pass

    class MessageModel:
        pass

    class OutboxModel(Row):
        pass

    class Session:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.owns_lock = False

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            self._release_lock()

        def _release_lock(self) -> None:
            if self.owns_lock:
                self.owns_lock = False
                task_lock.release()

        async def get(
            self,
            model: Any,
            row_id: str,
            **_kwargs: Any,
        ) -> Any:
            if model is CompletionModel:
                return completion
            if model is ImageModel and row_id == image_id:
                return committed_image
            if model is MessageModel:
                return message
            if model is OutboxModel:
                return committed_outboxes.get(row_id)
            return None

        def add(self, row: Any) -> None:
            self.added.append(row)

        async def commit(self) -> None:
            nonlocal committed_image
            images = [row for row in self.added if isinstance(row, ImageModel)]
            if images:
                assert committed_image is None
                committed_image = images[0]
            committed_variants.extend(
                row for row in self.added if isinstance(row, VariantModel)
            )
            for row in self.added:
                if isinstance(row, OutboxModel):
                    committed_outboxes[row.id] = row
            self.added.clear()
            self._release_lock()

        async def rollback(self) -> None:
            self.added.clear()
            self._release_lock()

    async def acquire_lock(session: Session, _task_id: str) -> None:
        await task_lock.acquire()
        session.owns_lock = True

    async def reserve(**_kwargs: Any) -> int:
        return 500

    async def record_usage(**_kwargs: Any) -> None:
        nonlocal usage_calls
        usage_calls += 1
        completion.upstream_request = {
            **completion.upstream_request,
            "tool_image_reserved_micro": 500,
        }
        completion.image_output_tokens = 17
        completion.tokens_out = 17

    storage = LocalStorage(tmp_path)

    class Lease:
        async def renew(self) -> bool:
            return True

        async def release(self) -> None:
            return None

    class Capacity:
        async def reserve(self, _bytes_required: int) -> Lease:
            return Lease()

    coordinator = StorageWriteCoordinator(
        storage=storage,
        capacity=Capacity(),
        lease_ttl_seconds=60,
    )

    async def write_files(files: list[tuple[str, bytes]]) -> list[str]:
        nonlocal write_calls
        assert task_lock.locked()
        write_calls += 1
        return await coordinator.write_files(files)

    def stage(
        session: Session,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        event_id = "event-image-concurrent"
        durable_payload = {**payload, "outbox_id": event_id}
        session.add(
            OutboxModel(
                id=event_id,
                kind=kind,
                payload=durable_payload,
                published_at=None,
            )
        )
        return event_id, kind, durable_payload

    async def deliver(
        _redis: Any,
        deliveries: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        event_id = deliveries[0][0]
        emitted_events.add(event_id)
        committed_outboxes[event_id].published_at = datetime.now(timezone.utc)

    service = CompletionToolImageService(
        budget=CompletionToolImageBudget(reserve=reserve),
        codec=CompletionToolImageCodec(
            decode=lambda _value: VALID_IMAGE_BYTES,
            format_and_meta=lambda _raw: (
                "png",
                "image/png",
                2,
                2,
                None,
                b"display",
                (2, 2),
                b"preview",
                (2, 2),
                b"thumb",
                (2, 2),
            ),
            sha256=lambda _raw: VALID_IMAGE_SHA256,
            upstream_error_type=_CheckpointUpstreamError,
            bad_response_error_code="bad_response",
        ),
        repository=CompletionToolImageRepository(
            session_factory=Session,
            new_id=lambda: "un-pinned",
            acquire_task_lock=acquire_lock,
            completion_model=CompletionModel,
            superseded_error_type=RuntimeError,
            record_usage=record_usage,
            image_model=ImageModel,
            image_variant_model=VariantModel,
            message_model=MessageModel,
            public_url=lambda key: f"/media/{key}",
        ),
        storage=CompletionToolImageStorage(
            write_files=write_files,
            cleanup_on_error=coordinator.cleanup_on_error,
            delete_files=coordinator.delete_files,
        ),
        events=CompletionToolImageEvents(
            image_event="completion.image",
            stage=stage,
            deliver=deliver,
            outbox_model=OutboxModel,
        ),
    )
    image_record = {
        "image_id": image_id,
        "dedupe_key": "image-concurrent",
        "state": "pending",
        "image_b64": VALID_IMAGE_B64,
    }

    results = await asyncio.gather(
        *(
            completion_checkpoint.persist_completion_checkpoint_image(
                service,
                redis=object(),
                user_id="user-1",
                channel="task:comp-concurrent",
                task_id="comp-concurrent",
                message_id="message-1",
                attempt=2,
                attempt_epoch=2,
                execution_epoch=7,
                image_record=image_record,
                reserved_micro=0,
            )
            for _ in range(2)
        )
    )

    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert write_calls == 1
    assert len(files) == 4
    assert committed_image is not None
    assert committed_image.id == image_id
    assert len(committed_variants) == 3
    assert usage_calls == 1
    assert len(message.content["images"]) == 1
    assert len(committed_outboxes) == 1
    assert emitted_events == {"event-image-concurrent"}
    assert {result[0]["image_id"] for result in results} == {image_id}


class _RowsResult:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> _RowsResult:
        return self

    def __iter__(self):
        return iter(self.rows)


class _ExpiredLeaseRedis:
    async def get(self, _key: str) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_already_saved", [False, True])
@pytest.mark.parametrize("checkpoint_version", [1, 2])
async def test_reconciler_recovers_image_checkpoint_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    artifact_already_saved: bool,
    checkpoint_version: int,
) -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    image_id = "image-checkpoint-1"
    payload = {
        "image_id": image_id,
        "from_completion_id": "comp-1",
        "completion_execution_epoch": 7,
        "completion_attempt_epoch": 2,
    }
    task = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        message_id="message-1",
        status=CompletionStatus.STREAMING.value,
        progress_stage=CompletionStatus.STREAMING.value,
        attempt=2,
        execution_epoch=7,
        error_code=None,
        error_message=None,
        finished_at=None,
        updated_at=now - timedelta(minutes=10),
        cancel_requested_at=None,
        text="image result",
        tokens_in=12,
        tokens_out=77 if artifact_already_saved else 5,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=77 if artifact_already_saved else 0,
        upstream_request={
            "upstream_dispatch_started_at": "2026-08-03T00:00:00+00:00",
            "upstream_dispatch_attempt": 2,
            "upstream_dispatch_execution_epoch": 7,
            "upstream_response_received_at": "2026-08-03T00:00:01+00:00",
            "upstream_response_attempt": 2,
            "upstream_response_execution_epoch": 7,
            "completion_usage_execution_epoch": 7,
            "completion_usage_attempt_epoch": 2,
            "completion_checkpoint_version": checkpoint_version,
            "completion_checkpoint_execution_epoch": 7,
            "completion_checkpoint_attempt_epoch": 2,
            "completion_checkpoint_response_id": "resp-image",
            "completion_checkpoint_usage_exact": True,
            "completion_checkpoint_usage_complete": False,
            "completion_checkpoint_state": "artifacts_pending",
            "completion_checkpoint_images": [
                {
                    "image_id": image_id,
                    "dedupe_key": "encoded-image",
                    "state": "pending",
                    "image_b64": VALID_IMAGE_B64,
                }
            ],
            **({"tool_image_reserved_micro": 500} if artifact_already_saved else {}),
        },
    )
    message = SimpleNamespace(
        content={"images": [payload]} if artifact_already_saved else {},
        status=CompletionStatus.STREAMING.value,
    )
    stored_image_ids = {image_id} if artifact_already_saved else set()
    actual_stores = 0
    billing_calls = 0

    class ReconcileSession:
        async def execute(self, _statement: Any) -> _RowsResult:
            rows = (
                [task]
                if task.status
                in {
                    CompletionStatus.QUEUED.value,
                    CompletionStatus.STREAMING.value,
                }
                else []
            )
            return _RowsResult(rows)

        async def get(self, _model: Any, _row_id: str) -> Any:
            return message

    class RepositorySession:
        async def __aenter__(self) -> RepositorySession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(
            self,
            _model: Any,
            _row_id: str,
            **_kwargs: Any,
        ) -> Any:
            return task

        async def commit(self) -> None:
            return None

    async def acquire_lock(_session: Any, _task_id: str) -> None:
        return None

    repository = SimpleNamespace(
        session_factory=RepositorySession,
        acquire_task_lock=acquire_lock,
        completion_model=object(),
        superseded_error_type=RuntimeError,
    )
    service = SimpleNamespace(repository=repository)

    async def persist_image(
        _service: Any,
        *,
        image_record: dict[str, Any],
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], int]:
        nonlocal actual_stores
        recovered_id = str(image_record["image_id"])
        if recovered_id not in stored_image_ids:
            stored_image_ids.add(recovered_id)
            actual_stores += 1
            message.content = {"images": [payload]}
            task.image_output_tokens = 77
            task.tokens_out = 77
            task.upstream_request["tool_image_reserved_micro"] = 500
            return payload, 500
        return payload, 0

    class Billing:
        async def charge_completion(
            self,
            _session: Any,
            _completion: Any,
        ) -> None:
            nonlocal billing_calls
            billing_calls += 1

    async def recover_checkpoint(
        context: ReconcileContext,
        completion: Any,
    ) -> bool:
        return await completion_checkpoint.recover_completion_checkpoint_images(
            completion,
            redis=context.redis,
            channel=f"task:{completion.id}",
            tool_image_service=service,
        )

    monkeypatch.setattr(
        task_domains,
        "_recover_completion_checkpoint",
        recover_checkpoint,
    )
    monkeypatch.setattr(
        completion_checkpoint,
        "persist_completion_checkpoint_image",
        persist_image,
    )
    context = ReconcileContext(
        redis=_ExpiredLeaseRedis(),
        session=ReconcileSession(),
        now=now,
        billing=Billing(),
        logger=logging.getLogger("test-completion-checkpoint"),
        lease_unknowns=None,
        stage_event=lambda _session, *, kind, payload: (
            "event-1",
            kind,
            payload,
        ),
    )

    first = await COMPLETION_RECONCILER.reconcile(context)
    second = await COMPLETION_RECONCILER.reconcile(context)

    assert first.touched == 1
    assert second.touched == 0
    assert actual_stores == (0 if artifact_already_saved else 1)
    assert stored_image_ids == {image_id}
    assert billing_calls == 1
    assert task.status == CompletionStatus.SUCCEEDED.value
    assert task.upstream_request["completion_checkpoint_state"] == "billing_ready"
    assert task.upstream_request["completion_checkpoint_usage_complete"] is True
    committed = task.upstream_request["completion_checkpoint_images"][0]
    assert committed["state"] == "committed"
    assert "image_b64" not in committed
    assert message.content["images"] == [payload]
    assert first.pending_outbox[0][2]["data"]["images"] == [payload]


@pytest.mark.asyncio
async def test_checkpoint_recovery_adopts_image_and_redelivers_event_once() -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    image_id = "image-committed-before-crash"
    task = _terminal_checkpoint(
        usage_exact=True,
        text="",
        version=2,
        images=[
            {
                "image_id": image_id,
                "dedupe_key": "image-before-crash",
                "state": "pending",
                "image_b64": VALID_IMAGE_B64,
            }
        ],
    )
    task.id = "comp-event-recovery"
    task.user_id = "user-1"
    task.message_id = "message-1"
    task.status = CompletionStatus.STREAMING.value
    task.cancel_requested_at = None
    task.upstream_request["completion_checkpoint_state"] = "artifacts_pending"
    task.upstream_request["completion_checkpoint_usage_complete"] = False
    key = (
        "u/user-1/completion-tools/comp-event-recovery/executions/"
        f"7/attempts/2/{image_id}/orig.png"
    )
    image = SimpleNamespace(
        id=image_id,
        user_id="user-1",
        storage_key=key,
        sha256=VALID_IMAGE_SHA256,
        mime="image/png",
        width=2,
        height=2,
        metadata_jsonb={
            "source": "completion_tool",
            "completion_id": task.id,
            "completion_attempt_epoch": 2,
            "completion_execution_epoch": 7,
        },
    )
    message = SimpleNamespace(
        content={"images": [{"image_id": image_id}]},
        status=MessageStatus.STREAMING.value,
    )
    delivered: list[dict[str, Any]] = []
    outboxes: dict[str, Any] = {}

    class CompletionModel:
        pass

    class ImageModel:
        pass

    class MessageModel:
        pass

    class OutboxModel:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class RepositorySession:
        async def __aenter__(self) -> RepositorySession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(
            self,
            model: Any,
            row_id: str,
            **_kwargs: Any,
        ) -> Any:
            if model is CompletionModel:
                return task
            if model is ImageModel and row_id == image_id:
                return image
            if model is MessageModel:
                return message
            if model is OutboxModel:
                return outboxes.get(row_id)
            return None

        def add(self, row: Any) -> None:
            if isinstance(row, OutboxModel):
                outboxes[row.id] = row

        async def commit(self) -> None:
            return None

    async def acquire_lock(_session: Any, _task_id: str) -> None:
        return None

    async def reserve(**_kwargs: Any) -> int:
        raise AssertionError("adopted checkpoint image must not recheck budget")

    async def record_usage(**_kwargs: Any) -> None:
        return None

    async def unused_write(_files: list[tuple[str, bytes]]) -> list[str]:
        raise AssertionError("adopted image must not be written again")

    @asynccontextmanager
    async def unused_cleanup(_keys: list[str]):
        yield

    async def unused_delete(_keys: list[str]) -> None:
        return None

    def stage(
        session: RepositorySession,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        assert outboxes == {}
        event_id = "event-image-before-crash"
        durable_payload = {**payload, "outbox_id": event_id}
        row = OutboxModel(
            id=event_id,
            kind=kind,
            payload=durable_payload,
            published_at=None,
        )
        session.add(row)
        return event_id, kind, durable_payload

    async def deliver(
        _redis: Any,
        deliveries: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        event_id, _kind, payload = deliveries[0]
        event = outboxes[event_id]
        data = {
            **payload["data"],
            "outbox_id": event_id,
            "event_id": event_id,
        }
        delivered.append(data)
        event.published_at = now

    repository = CompletionToolImageRepository(
        session_factory=RepositorySession,
        new_id=lambda: (_ for _ in ()).throw(
            AssertionError("adopted image id must remain pinned")
        ),
        acquire_task_lock=acquire_lock,
        completion_model=CompletionModel,
        superseded_error_type=RuntimeError,
        record_usage=record_usage,
        image_model=ImageModel,
        image_variant_model=object(),
        message_model=MessageModel,
        public_url=lambda storage_key: f"/media/{storage_key}",
    )
    service = CompletionToolImageService(
        budget=CompletionToolImageBudget(reserve=reserve),
        codec=CompletionToolImageCodec(
            decode=lambda value: base64.b64decode(value, validate=True),
            format_and_meta=lambda _raw: (),
            sha256=lambda raw: hashlib.sha256(raw).hexdigest(),
            upstream_error_type=_CheckpointUpstreamError,
            bad_response_error_code="bad_response",
        ),
        repository=repository,
        storage=CompletionToolImageStorage(
            write_files=unused_write,
            cleanup_on_error=unused_cleanup,
            delete_files=unused_delete,
        ),
        events=CompletionToolImageEvents(
            stage=stage,
            deliver=deliver,
            outbox_model=OutboxModel,
            image_event="completion.image",
        ),
    )

    first = await completion_checkpoint.recover_completion_checkpoint_images(
        task,
        redis=object(),
        channel="task:comp-event-recovery",
        tool_image_service=service,
    )
    second = await completion_checkpoint.recover_completion_checkpoint_images(
        task,
        redis=object(),
        channel="task:comp-event-recovery",
        tool_image_service=service,
    )

    checkpoint_image = task.upstream_request["completion_checkpoint_images"][0]
    event = outboxes["event-image-before-crash"]
    assert first is True
    assert second is False
    assert task.text == "已生成图片。"
    assert checkpoint_image["state"] == "committed"
    assert checkpoint_image["event_outbox_id"] == event.id
    assert checkpoint_image["event_published"] is True
    assert image.metadata_jsonb["completion_image_event_outbox_id"] == event.id
    assert event.published_at == now
    assert len(delivered) == 1
    assert delivered[0]["event_id"] == event.id
    assert delivered[0]["images"][0]["image_id"] == image_id
    assert message.content["images"] == [{"image_id": image_id}]


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_first", [False, True])
@pytest.mark.parametrize("initial_text", ["", " \n\t"])
async def test_mixed_checkpoint_recovers_valid_image_and_accounts_corruption(
    monkeypatch: pytest.MonkeyPatch,
    corrupt_first: bool,
    initial_text: str,
) -> None:
    valid = {
        "image_id": "image-valid",
        "dedupe_key": "image-valid",
        "state": "pending",
        "image_b64": VALID_IMAGE_B64,
    }
    corrupt = {
        "image_id": "image-corrupt",
        "dedupe_key": "image-corrupt",
        "state": "pending",
        "image_b64": "not-valid-base64***",
    }
    task = _terminal_checkpoint(
        usage_exact=True,
        text=initial_text,
        version=2,
        images=[corrupt, valid] if corrupt_first else [valid, corrupt],
    )
    task.id = "comp-mixed"
    task.user_id = "user-1"
    task.message_id = "message-1"
    task.status = CompletionStatus.STREAMING.value
    task.progress_stage = CompletionStatus.STREAMING.value
    task.cancel_requested_at = None
    payload = {
        "image_id": "image-valid",
        "from_completion_id": "comp-mixed",
        "url": "/media/image-valid.png",
        "display_url": "/api/images/image-valid/variants/display2048",
    }
    message = SimpleNamespace(
        content={},
        status=CompletionStatus.STREAMING.value,
    )
    image_rows: list[Any] = []

    class RepositorySession:
        async def __aenter__(self) -> RepositorySession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(
            self,
            _model: Any,
            _row_id: str,
            **_kwargs: Any,
        ) -> Any:
            return task

        async def commit(self) -> None:
            return None

    async def acquire_lock(_session: Any, _task_id: str) -> None:
        return None

    repository = SimpleNamespace(
        session_factory=RepositorySession,
        acquire_task_lock=acquire_lock,
        completion_model=object(),
        superseded_error_type=RuntimeError,
    )
    service = SimpleNamespace(repository=repository)

    async def persist_image(
        _service: Any,
        *,
        image_record: dict[str, Any],
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], int]:
        assert image_record["image_id"] == "image-valid"
        image_rows.append(
            SimpleNamespace(
                id="image-valid",
                storage_key=(
                    "u/user-1/completion-tools/comp-mixed/executions/"
                    "7/attempts/2/image-valid/orig.png"
                ),
            )
        )
        message.content = {"images": [payload]}
        task.image_output_tokens = 17
        task.tokens_out = 45
        return payload, 500

    monkeypatch.setattr(
        completion_checkpoint,
        "persist_completion_checkpoint_image",
        persist_image,
    )

    recovered = await completion_checkpoint.recover_completion_checkpoint_images(
        task,
        redis=object(),
        channel="task:comp-mixed",
        tool_image_service=service,
    )

    checkpoint_images = task.upstream_request["completion_checkpoint_images"]
    assert recovered is True
    assert [row.id for row in image_rows] == ["image-valid"]
    assert task.upstream_request["completion_checkpoint_version"] == 2
    assert task.upstream_request["completion_checkpoint_state"] == (
        "partial_corruption"
    )
    assert task.upstream_request["completion_checkpoint_usage_complete"] is True
    assert task.upstream_request["completion_checkpoint_corrupt_image_count"] == 1
    assert {image["image_id"]: image["state"] for image in checkpoint_images} == {
        "image-valid": "committed",
        "image-corrupt": "quarantined",
    }
    corrupt_record = next(
        image for image in checkpoint_images if image["image_id"] == "image-corrupt"
    )
    assert "invalid base64" in corrupt_record["quarantine_reason"]
    assert "image_b64" not in corrupt_record
    assert completion_checkpoint.completion_has_completed_checkpoint(task)
    assert task.text == "已生成图片。"

    class Billing:
        def __init__(self) -> None:
            self.calls = 0

        async def charge_completion(
            self,
            _session: Any,
            _completion: Any,
        ) -> None:
            self.calls += 1

    class ApplySession:
        async def get(self, _model: Any, _row_id: str) -> Any:
            return message

    billing = Billing()
    context = SimpleNamespace(
        billing=billing,
        session=ApplySession(),
        now=datetime(2026, 8, 3, tzinfo=timezone.utc),
        stage_event=lambda _session, *, kind, payload: (
            "event-mixed",
            kind,
            payload,
        ),
    )

    event = await completion_checkpoint.apply_completed_checkpoint(context, task)

    assert billing.calls == 1
    assert task.status == CompletionStatus.SUCCEEDED.value
    assert message.content["images"] == [payload]
    assert event[2]["data"]["images"] == [payload]
    assert event[2]["data"]["text"] == "已生成图片。"


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_exact", [False, True])
@pytest.mark.parametrize("initial_text", ["", " \n\t"])
async def test_all_corrupt_checkpoint_reconciles_to_failure_once(
    monkeypatch: pytest.MonkeyPatch,
    usage_exact: bool,
    initial_text: str,
) -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    task = _terminal_checkpoint(
        usage_exact=usage_exact,
        text=initial_text,
        version=2,
        images=[
            {
                "image_id": "image-corrupt",
                "dedupe_key": "image-corrupt",
                "state": "pending",
                "image_b64": "not-valid-base64***",
            }
        ],
    )
    task.id = "comp-all-corrupt"
    task.user_id = "user-1"
    task.message_id = "message-1"
    task.status = CompletionStatus.STREAMING.value
    task.progress_stage = CompletionStatus.STREAMING.value
    task.cancel_requested_at = None
    task.finished_at = None
    task.error_code = None
    task.error_message = None
    task.updated_at = now - timedelta(minutes=10)
    if initial_text:
        task.upstream_request["completion_checkpoint_images"] = [
            {
                "image_id": "image-corrupt",
                "dedupe_key": "image-corrupt",
                "state": "quarantined",
                "quarantine_reason": "pending image contains invalid base64",
            }
        ]
        task.upstream_request["completion_checkpoint_state"] = "partial_corruption"
        task.upstream_request["completion_checkpoint_usage_complete"] = usage_exact
        task.upstream_request["completion_checkpoint_corrupt_image_count"] = 1
    if not usage_exact:
        task.tokens_in = 0
        task.tokens_out = 0
    message = SimpleNamespace(
        content={},
        status=MessageStatus.STREAMING.value,
    )
    charges = 0
    unknown_settlements = 0
    staged: list[Any] = []

    class ReconcileSession:
        async def execute(self, _statement: Any) -> _RowsResult:
            rows = (
                [task]
                if task.status
                in {
                    CompletionStatus.QUEUED.value,
                    CompletionStatus.STREAMING.value,
                }
                else []
            )
            return _RowsResult(rows)

        async def get(self, _model: Any, _row_id: str) -> Any:
            return message

    class RepositorySession:
        async def __aenter__(self) -> RepositorySession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(
            self,
            _model: Any,
            _row_id: str,
            **_kwargs: Any,
        ) -> Any:
            return task

        async def commit(self) -> None:
            return None

    async def acquire_lock(_session: Any, _task_id: str) -> None:
        return None

    repository = SimpleNamespace(
        session_factory=RepositorySession,
        acquire_task_lock=acquire_lock,
        completion_model=object(),
        superseded_error_type=RuntimeError,
    )
    service = SimpleNamespace(repository=repository)

    async def recover_checkpoint(
        context: ReconcileContext,
        completion: Any,
    ) -> bool:
        return await completion_checkpoint.recover_completion_checkpoint_images(
            completion,
            redis=context.redis,
            channel=f"task:{completion.id}",
            tool_image_service=service,
        )

    class Billing:
        async def charge_completion(
            self,
            _session: Any,
            _completion: Any,
        ) -> None:
            nonlocal charges
            charges += 1

        async def release_completion(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("trusted upstream usage must not be released")

        async def settle_completion_unknown_upstream(
            self,
            *_args: Any,
            **_kwargs: Any,
        ) -> None:
            nonlocal unknown_settlements
            unknown_settlements += 1

    def stage_event(
        _session: Any,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        delivery = ("event-all-corrupt", kind, payload)
        staged.append(delivery)
        return delivery

    monkeypatch.setattr(
        task_domains,
        "_recover_completion_checkpoint",
        recover_checkpoint,
    )
    context = ReconcileContext(
        redis=_ExpiredLeaseRedis(),
        session=ReconcileSession(),
        now=now,
        billing=Billing(),
        logger=logging.getLogger("test-all-corrupt-checkpoint"),
        lease_unknowns=None,
        stage_event=stage_event,
    )

    first = await COMPLETION_RECONCILER.reconcile(context)
    second = await COMPLETION_RECONCILER.reconcile(context)

    assert first.touched == 1
    assert second.touched == 0
    assert charges == int(usage_exact)
    assert unknown_settlements == int(not usage_exact)
    assert task.status == CompletionStatus.FAILED.value
    assert task.error_code == "no_text_returned"
    assert task.text == ""
    assert task.upstream_request["completion_checkpoint_state"] == (
        "partial_corruption"
    )
    assert task.upstream_request["completion_checkpoint_corrupt_image_count"] == 1
    assert task.upstream_request["completion_checkpoint_images"][0]["state"] == (
        "quarantined"
    )
    assert message.status == MessageStatus.FAILED.value
    assert staged[0][2]["data"]["code"] == "no_text_returned"


@pytest.mark.asyncio
async def test_unknown_checkpoint_version_is_quarantined_without_blocking_later_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _terminal_checkpoint(
        usage_exact=True,
        version=99,
    )
    task.id = "comp-corrupt"
    task.user_id = "user-1"
    task.message_id = "message-1"
    task.status = CompletionStatus.STREAMING.value
    task.progress_stage = CompletionStatus.STREAMING.value
    task.cancel_requested_at = None
    task.finished_at = None
    task.error_code = None
    task.error_message = None
    task.updated_at = datetime(2026, 8, 2, tzinfo=timezone.utc)
    task.tokens_in = 0
    task.tokens_out = 0
    message = SimpleNamespace(status=CompletionStatus.STREAMING.value)
    staged: list[Any] = []

    class Transaction:
        async def __aenter__(self) -> Transaction:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def begin(self) -> Transaction:
            return Transaction()

        def add(self, value: Any) -> None:
            staged.append(value)

        async def execute(self, _statement: Any) -> _RowsResult:
            rows = (
                [task]
                if task.status
                in {
                    CompletionStatus.QUEUED.value,
                    CompletionStatus.STREAMING.value,
                }
                else []
            )
            return _RowsResult(rows)

        async def get(self, _model: Any, _row_id: str) -> Any:
            return message

    class Billing:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.flushes = 0

        async def settle_completion_unknown_upstream(
            self,
            _session: Any,
            _completion: Any,
            **_kwargs: Any,
        ) -> None:
            self.calls.append("settle_completion_unknown_upstream")

        async def flush_balance_cache_refreshes(self, _session: Any) -> None:
            self.flushes += 1

    called: list[str] = []

    class ProbeReconciler:
        def __init__(self, name: str) -> None:
            self.name = name

        async def reconcile(self, _context: ReconcileContext) -> ReconcileResult:
            called.append(self.name)
            return ReconcileResult(touched=1)

    @asynccontextmanager
    async def acquired_lock(*_args: Any, **_kwargs: Any):
        yield True

    delivered: list[Any] = []

    async def deliver_pending(_redis: Any, deliveries: list[Any]) -> None:
        delivered.extend(deliveries)

    monkeypatch.setattr(
        reconciliation_coordinator,
        "owned_redis_lock",
        acquired_lock,
    )
    billing = Billing()

    touched = await reconciliation_coordinator.run_reconciliation(
        _ExpiredLeaseRedis(),
        name="checkpoint-progress",
        lock_key="lock:test",
        lock_ttl_s=10,
        session_factory=Session,
        billing=billing,
        reconcilers=(
            COMPLETION_RECONCILER,
            ProbeReconciler("generation"),
            ProbeReconciler("bonus"),
        ),
        deliver_pending=deliver_pending,
        log=logging.getLogger("test-checkpoint-progress"),
        after_commit=billing.flush_balance_cache_refreshes,
    )

    assert touched == 3
    assert called == ["generation", "bonus"]
    assert task.status == CompletionStatus.FAILED.value
    assert task.error_code == "completion_checkpoint_corrupt"
    assert task.upstream_request["completion_checkpoint_state"] == "quarantined"
    assert billing.calls == ["settle_completion_unknown_upstream"]
    assert billing.flushes == 1
    assert len(staged) == 1
    assert len(delivered) == 1
