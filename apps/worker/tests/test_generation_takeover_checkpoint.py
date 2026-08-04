from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.artifact_commit import ArtifactAdoption, ArtifactCommitResult
from app.provider_runtime.errors import UpstreamError
from app.reconciliation import task_domains as reconciliation_task_domains
from app.tasks.generation_parts import (
    batch_results,
    retry_state,
    runner,
    runner_claim_phase,
    runner_dispatch_phase,
    success,
    takeover_checkpoint,
)
from app.upstream_parts import InlineImageBytes, materialize_generated_payload
from lumen_core.constants import GenerationStatus
from lumen_core.models import Generation
from lumen_core.upstream_billing import (
    GENERATION_TAKEOVER_CHECKPOINT_KEY as CHECKPOINT_KEY,
)


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _CheckpointSession:
    def __init__(self, generation: Any, events: list[str]) -> None:
        self.generation = generation
        self.events = events
        self.commits = 0
        self.added: list[Any] = []
        self.checkpoints: list[dict[str, Any]] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def execute(self, _statement: Any) -> _ScalarResult:
        return _ScalarResult(self.generation)

    async def commit(self) -> None:
        self.commits += 1
        self.events.append("checkpoint-commit")
        checkpoint = self.generation.upstream_request.get(CHECKPOINT_KEY)
        if isinstance(checkpoint, dict):
            self.checkpoints.append(checkpoint)


class _CheckpointStore:
    def __init__(self, session: Any) -> None:
        self.session_value = session

    @asynccontextmanager
    async def session(self):
        yield self.session_value


class _CheckpointArtifacts:
    def __init__(
        self,
        payload: bytes | BaseException | dict[str, bytes | BaseException],
        events: list[str],
    ) -> None:
        self.payload = payload
        self.events = events
        self.writes: list[tuple[str, bytes]] = []
        self.reads: list[str] = []
        self.deleted: list[str] = []

    async def write_files(self, files: list[tuple[str, bytes]]) -> list[str]:
        self.events.append("checkpoint-write")
        self.writes.extend(files)
        return [key for key, _data in files]

    async def get_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        payload = (
            self.payload.get(key, FileNotFoundError(key))
            if isinstance(self.payload, dict)
            else self.payload
        )
        if isinstance(payload, BaseException):
            raise payload
        return payload

    async def delete_files(self, keys: list[str]) -> None:
        self.deleted.extend(keys)


class _ClaimSession:
    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.statements: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        return _ScalarResult(self.results.pop(0))

    async def get(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("valid checkpoint claim must not terminalize")

    async def commit(self) -> None:
        self.commits += 1


def _checkpoint(
    payload: bytes,
    *,
    task_id: str,
    user_id: str = "user-1",
    execution_epoch: int = 3,
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "version": 1,
        "execution_epoch": execution_epoch,
        "attempt": attempt,
        "storage_key": (
            f"u/{user_id}/g/{task_id}/executions/{execution_epoch}/"
            f"attempts/{attempt}/takeover-result.bin"
        ),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "revised_prompt": "restored",
        "provider": "provider-1",
        "route": "image2",
        "source": "image2_direct",
        "endpoint": "images/generations",
    }


def _request_with_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    attempt = int(checkpoint["attempt"])
    execution_epoch = int(checkpoint["execution_epoch"])
    return {
        "upstream_dispatch_started_at": "2026-08-03T00:00:00+00:00",
        "upstream_dispatch_attempt": attempt,
        "upstream_dispatch_execution_epoch": execution_epoch,
        "upstream_response_received_at": "2026-08-03T00:00:01+00:00",
        "upstream_response_attempt": attempt,
        "upstream_response_execution_epoch": execution_epoch,
        CHECKPOINT_KEY: checkpoint,
    }


def _checkpoint_v2(
    payloads: list[bytes],
    *,
    task_id: str,
    user_id: str = "user-1",
    execution_epoch: int = 3,
    attempt: int = 1,
    finalized_indexes: frozenset[int] = frozenset(),
) -> dict[str, Any]:
    identity_state = SimpleNamespace(
        task_id=task_id,
        user_id=user_id,
        generation=SimpleNamespace(execution_epoch=execution_epoch),
    )
    results = []
    for index, payload in enumerate(payloads, start=1):
        results.append(
            {
                "index": index,
                "storage_key": (
                    f"u/{user_id}/g/{task_id}/executions/{execution_epoch}/"
                    f"attempts/{attempt}/takeover-result-{index}.bin"
                ),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "revised_prompt": f"restored-{index}",
                "bonus_generation_id": (
                    takeover_checkpoint.batch_extra_generation_id(
                        identity_state,
                        index=index,
                        attempt=attempt,
                    )
                    if index > 1
                    else None
                ),
                "finalization_state": (
                    takeover_checkpoint.RESULT_FINALIZATION_FINALIZED
                    if index in finalized_indexes
                    else takeover_checkpoint.RESULT_FINALIZATION_PENDING
                ),
            }
        )
    return {
        "version": takeover_checkpoint.GENERATION_TAKEOVER_CHECKPOINT_VERSION,
        "execution_epoch": execution_epoch,
        "attempt": attempt,
        "expected_count": len(results),
        "collection_complete": True,
        "results": results,
        "provider": "provider-1",
        "route": "image2",
        "source": "image2_direct",
        "endpoint": "images/generations",
    }


def _batch_finalize_state(
    checkpoint: dict[str, Any],
    payloads: list[bytes],
    *,
    current_attempt: int = 2,
) -> SimpleNamespace:
    request = _request_with_checkpoint(checkpoint)
    return SimpleNamespace(
        services=SimpleNamespace(),
        redis=object(),
        task_id="gen-batch-crash",
        user_id="user-1",
        channel="task:gen-batch-crash",
        generation=SimpleNamespace(
            id="gen-batch-crash",
            user_id="user-1",
            execution_epoch=checkpoint["execution_epoch"],
            attempt=current_attempt,
            upstream_request=request,
        ),
        attempt=current_attempt,
        gen_upstream_request_snapshot=request,
        gen_idempotency_key="idempotency-batch",
        parent_upstream_request_for_bonus=None,
        dual_race_bonus_obligation_id=None,
        message_id="message-1",
        action="generate",
        gen_model="gpt-image-test",
        prompt="render",
        size_requested="1024x1024",
        aspect_ratio="1:1",
        input_image_ids=[],
        primary_input_image_id=None,
        references=[],
        image_request_options={},
        actual_upstream_provider="provider-1",
        actual_upstream_route="image2",
        actual_upstream_source="image2_direct",
        actual_upstream_endpoint="images/generations",
        batch_extra_pairs=[
            (
                index,
                (InlineImageBytes(payload), f"restored-{index}"),
            )
            for index, payload in enumerate(payloads[1:], start=2)
        ],
    )


def test_legacy_single_payload_checkpoint_factory_and_deserialize_round_trip() -> None:
    payload = b"legacy-checkpoint"
    mapping = _checkpoint(payload, task_id="gen-legacy-checkpoint")
    checkpoint = takeover_checkpoint.GenerationTakeoverCheckpoint.from_legacy_payload(
        execution_epoch=mapping["execution_epoch"],
        attempt=mapping["attempt"],
        payload=takeover_checkpoint.GenerationTakeoverPayload(
            storage_key=mapping["storage_key"],
            size_bytes=mapping["size_bytes"],
            sha256=mapping["sha256"],
            revised_prompt=mapping["revised_prompt"],
        ),
        provider=mapping["provider"],
        route=mapping["route"],
        source=mapping["source"],
        endpoint=mapping["endpoint"],
    )

    assert checkpoint.schema_version == 1
    assert checkpoint.expected_count == 1
    assert checkpoint.to_mapping() == mapping
    assert (
        takeover_checkpoint.GenerationTakeoverCheckpoint.from_mapping(mapping)
        == checkpoint
    )


@pytest.mark.asyncio
async def test_generation_response_is_checkpointed_before_artifact_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = [
        b"durable-image-result-1",
        b"durable-image-result-2",
        b"durable-image-result-3",
    ]
    events: list[str] = []
    generation = SimpleNamespace(
        id="gen-checkpoint",
        user_id="user-1",
        attempt=2,
        execution_epoch=4,
        status=GenerationStatus.RUNNING.value,
        cancel_requested_at=None,
        upstream_request={},
    )
    session = _CheckpointSession(generation, events)
    artifacts = _CheckpointArtifacts(payloads[0], events)
    progress = SimpleNamespace(
        pop_provider_used_event=lambda: {
            "provider": "provider-1",
            "route": "image2",
            "source": "image2_direct",
            "endpoint": "images/generations",
        }
    )
    state = SimpleNamespace(
        services=SimpleNamespace(
            store=_CheckpointStore(session),
            artifacts=artifacts,
            events=SimpleNamespace(),
            provider=SimpleNamespace(),
        ),
        task_id="gen-checkpoint",
        user_id="user-1",
        message_id="message-1",
        action="generate",
        gen_model="gpt-image-test",
        prompt="render",
        size_requested="1024x1024",
        aspect_ratio="1:1",
        input_image_ids=[],
        primary_input_image_id=None,
        gen_idempotency_key="idempotency-checkpoint",
        attempt=2,
        generation=generation,
        gen_upstream_request_snapshot={},
        image_request_options={},
        resolved=SimpleNamespace(size="1024x1024"),
        lease_lost=asyncio.Event(),
        redis=object(),
        image_iter=None,
        progress_publisher=progress,
        requested_image_count=3,
        batch_extra_pairs=[],
        b64_result=None,
        revised_prompt=None,
        upstream_duration_ms=None,
        actual_upstream_provider=None,
        actual_upstream_route=None,
        actual_upstream_source=None,
        actual_upstream_endpoint=None,
        stage_timer=SimpleNamespace(set_ms=lambda *_args: None),
    )

    async def not_cancelled(*_args: object, **_kwargs: object) -> bool:
        return False

    async def active_user(*_args: object, **_kwargs: object) -> bool:
        return True

    async def image_iter():
        for index, payload in enumerate(payloads, start=1):
            yield InlineImageBytes(payload), f"revised-{index}"

    monkeypatch.setattr(runner_dispatch_phase, "is_cancelled", not_cancelled)
    monkeypatch.setattr(retry_state, "is_cancelled", not_cancelled)
    monkeypatch.setattr(
        runner_dispatch_phase,
        "lock_active_generation_user",
        active_user,
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "build_image_iterator",
        lambda _state: image_iter(),
    )

    await runner_dispatch_phase.call_upstream(state)

    checkpoint = generation.upstream_request[CHECKPOINT_KEY]
    assert events[:2] == ["checkpoint-write", "checkpoint-commit"]
    assert checkpoint["version"] == 2
    assert checkpoint["collection_complete"] is True
    assert checkpoint["expected_count"] == 3
    expected_writes = [
        (result["storage_key"], payload)
        for result, payload in zip(checkpoint["results"], payloads, strict=True)
    ]
    assert artifacts.writes[-3:] == expected_writes
    assert len(artifacts.writes) == 6
    assert [item["collection_complete"] for item in session.checkpoints] == [
        False,
        False,
        True,
    ]
    assert [len(item["results"]) for item in session.checkpoints] == [1, 2, 3]
    assert checkpoint["execution_epoch"] == 4
    assert checkpoint["attempt"] == 2
    for index, (result, payload) in enumerate(
        zip(checkpoint["results"], payloads, strict=True),
        start=1,
    ):
        assert result["index"] == index
        assert result["size_bytes"] == len(payload)
        assert result["sha256"] == hashlib.sha256(payload).hexdigest()
        assert result["revised_prompt"] == f"revised-{index}"
        assert result["finalization_state"] == "pending"
    assert materialize_generated_payload(state.b64_result) == payloads[0]
    assert [
        materialize_generated_payload(pair[0])
        for _index, pair in state.batch_extra_pairs
    ] == payloads[1:]
    obligations = [row for row in session.added if isinstance(row, Generation)]
    assert [row.id for row in obligations] == [
        result["bonus_generation_id"] for result in checkpoint["results"][1:]
    ]
    assert all(
        row.upstream_request["bonus_billing_obligation"] is True
        and row.upstream_request["bonus_artifact_state"] == "pending"
        and row.upstream_request["parent_attempt"] == 2
        for row in obligations
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("yielded_count", [1, 2])
async def test_incomplete_batch_collection_is_durable_but_not_replayable(
    monkeypatch: pytest.MonkeyPatch,
    yielded_count: int,
) -> None:
    payloads = [b"partial-primary", b"partial-extra-2", b"partial-extra-3"]
    events: list[str] = []
    generation = SimpleNamespace(
        id="gen-partial-checkpoint",
        user_id="user-1",
        attempt=1,
        execution_epoch=6,
        status=GenerationStatus.RUNNING.value,
        cancel_requested_at=None,
        upstream_request={},
    )
    session = _CheckpointSession(generation, events)
    artifacts = _CheckpointArtifacts(payloads[0], events)
    state = SimpleNamespace(
        services=SimpleNamespace(
            store=_CheckpointStore(session),
            artifacts=artifacts,
        ),
        task_id=generation.id,
        user_id=generation.user_id,
        message_id="message-1",
        action="generate",
        gen_model="gpt-image-test",
        prompt="render",
        size_requested="1024x1024",
        aspect_ratio="1:1",
        input_image_ids=[],
        primary_input_image_id=None,
        gen_idempotency_key="idempotency-partial-checkpoint",
        attempt=1,
        generation=generation,
        gen_upstream_request_snapshot={},
        image_request_options={},
        resolved=SimpleNamespace(size="1024x1024"),
        lease_lost=asyncio.Event(),
        redis=object(),
        image_iter=None,
        progress_publisher=SimpleNamespace(
            pop_provider_used_event=lambda: {
                "provider": "provider-1",
                "route": "image2",
                "source": "image2_direct",
                "endpoint": "images/generations",
            }
        ),
        requested_image_count=3,
        batch_extra_pairs=[],
        b64_result=None,
        revised_prompt=None,
        upstream_duration_ms=None,
        actual_upstream_provider=None,
        actual_upstream_route=None,
        actual_upstream_source=None,
        actual_upstream_endpoint=None,
        stage_timer=SimpleNamespace(set_ms=lambda *_args: None),
    )

    async def not_cancelled(*_args: object, **_kwargs: object) -> bool:
        return False

    async def active_user(*_args: object, **_kwargs: object) -> bool:
        return True

    async def image_iter():
        for index, payload in enumerate(payloads[:yielded_count], start=1):
            yield InlineImageBytes(payload), f"partial-{index}"
        raise RuntimeError(f"forced crash after payload {yielded_count}")

    monkeypatch.setattr(runner_dispatch_phase, "is_cancelled", not_cancelled)
    monkeypatch.setattr(retry_state, "is_cancelled", not_cancelled)
    monkeypatch.setattr(
        takeover_checkpoint,
        "lock_active_generation_user",
        active_user,
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "build_image_iterator",
        lambda _state: image_iter(),
    )

    with pytest.raises(UpstreamError) as exc_info:
        await runner_dispatch_phase.call_upstream(state)

    checkpoint = generation.upstream_request[CHECKPOINT_KEY]
    assert exc_info.value.error_code == "image_job_result_unknown"
    assert checkpoint["collection_complete"] is False
    assert checkpoint["expected_count"] == 3
    assert len(checkpoint["results"]) == yielded_count
    assert not takeover_checkpoint.generation_has_takeover_checkpoint(state)
    obligations = [row for row in session.added if isinstance(row, Generation)]
    assert len(obligations) == 2
    assert [row.upstream_request["batch_index"] for row in obligations] == [2, 3]
    assert [len(item["results"]) for item in session.checkpoints] == list(
        range(1, yielded_count + 1)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expect_cleanup"),
    [
        (ArtifactAdoption.ADOPTED, False),
        (ArtifactAdoption.NOT_ADOPTED, True),
    ],
)
async def test_multi_result_checkpoint_commit_crash_retains_only_adopted_payloads(
    monkeypatch: pytest.MonkeyPatch,
    outcome: ArtifactAdoption,
    expect_cleanup: bool,
) -> None:
    payloads = [b"primary", b"extra-2", b"extra-3"]
    events: list[str] = []
    generation = SimpleNamespace(
        id="gen-checkpoint-crash",
        user_id="user-1",
        attempt=1,
        execution_epoch=7,
        status=GenerationStatus.RUNNING.value,
        cancel_requested_at=None,
        upstream_request={},
    )
    session = _CheckpointSession(generation, events)
    artifacts = _CheckpointArtifacts(payloads[0], events)
    state = SimpleNamespace(
        services=SimpleNamespace(
            store=_CheckpointStore(session),
            artifacts=artifacts,
        ),
        task_id=generation.id,
        user_id=generation.user_id,
        message_id="message-1",
        action="generate",
        gen_model="gpt-image-test",
        prompt="render",
        size_requested="1024x1024",
        aspect_ratio="1:1",
        input_image_ids=[],
        primary_input_image_id=None,
        gen_idempotency_key="idempotency-checkpoint-crash",
        attempt=1,
        generation=generation,
        gen_upstream_request_snapshot={},
        image_request_options={},
        resolved=SimpleNamespace(size="1024x1024"),
        requested_image_count=3,
        b64_result=InlineImageBytes(payloads[0]),
        revised_prompt="primary",
        batch_extra_pairs=[
            (2, (InlineImageBytes(payloads[1]), "extra-2")),
            (3, (InlineImageBytes(payloads[2]), "extra-3")),
        ],
        actual_upstream_provider="provider-1",
        actual_upstream_route="image2",
        actual_upstream_source="image2_direct",
        actual_upstream_endpoint="images/generations",
        dispatch_marker_recorded=False,
    )

    async def active_user(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def commit_result(*_args: Any, **_kwargs: Any) -> ArtifactCommitResult:
        return ArtifactCommitResult(
            outcome=outcome,
            commit_error=RuntimeError("checkpoint commit acknowledgement lost"),
        )

    monkeypatch.setattr(
        takeover_checkpoint,
        "lock_active_generation_user",
        active_user,
    )
    monkeypatch.setattr(
        takeover_checkpoint,
        "commit_with_adoption_probe",
        commit_result,
    )

    if outcome is ArtifactAdoption.ADOPTED:
        await takeover_checkpoint.persist_generation_takeover_checkpoint(state)
        assert (
            state.gen_upstream_request_snapshot[CHECKPOINT_KEY]["expected_count"] == 3
        )
    else:
        with pytest.raises(
            RuntimeError,
            match="checkpoint commit acknowledgement lost",
        ):
            await takeover_checkpoint.persist_generation_takeover_checkpoint(state)

    written_keys = [key for key, _payload in artifacts.writes]
    assert len(written_keys) == 3
    assert bool(artifacts.deleted) is expect_cleanup
    if expect_cleanup:
        assert artifacts.deleted == written_keys


@pytest.mark.asyncio
async def test_real_generation_claim_preserves_valid_takeover_checkpoint() -> None:
    payload = b"claimed-checkpoint"
    task_id = "gen-real-claim"
    checkpoint = _checkpoint(
        payload,
        task_id=task_id,
        execution_epoch=4,
    )
    request = _request_with_checkpoint(checkpoint)
    generation = SimpleNamespace(
        id=task_id,
        user_id="user-1",
        message_id="message-1",
        action="generate",
        prompt="render",
        aspect_ratio="1:1",
        size_requested="1024x1024",
        input_image_ids=[],
        primary_input_image_id=None,
        user_api_credential_id=None,
        mask_image_id=None,
        idempotency_key="idempotency-1",
        model="gpt-image-test",
        upstream_request=request,
        status=GenerationStatus.QUEUED.value,
        cancel_requested_at=None,
        attempt=1,
        execution_epoch=4,
        created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    session = _ClaimSession(
        [
            "user-1",
            SimpleNamespace(deleted_at=None),
            generation,
            "conversation-1",
            None,
        ]
    )
    state = SimpleNamespace(
        services=SimpleNamespace(
            store=_CheckpointStore(session),
            artifacts=SimpleNamespace(),
            billing=SimpleNamespace(),
            events=SimpleNamespace(),
        ),
        task_id=task_id,
        redis=object(),
        channel=f"task:{task_id}",
        task_start=asyncio.get_running_loop().time(),
        task_outcome="unknown",
        stage_timer=SimpleNamespace(set_ms=lambda *_args: None),
    )

    claimed = await runner_claim_phase.load_initial_generation(state)

    assert claimed is True
    assert state.attempt == 2
    assert state.gen_upstream_request_snapshot[CHECKPOINT_KEY] == checkpoint
    assert session.commits == 0
    assert session.results == []
    claim_sql = str(session.statements[2].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in claim_sql


def test_takeover_checkpoint_accepts_prior_source_attempt_after_takeover_claim() -> (
    None
):
    payload = b"stale-checkpoint"
    checkpoint = _checkpoint(
        payload,
        task_id="gen-stale-checkpoint",
        execution_epoch=4,
        attempt=1,
    )
    generation = SimpleNamespace(
        id="gen-stale-checkpoint",
        user_id="user-1",
        attempt=2,
        execution_epoch=4,
        upstream_request=_request_with_checkpoint(checkpoint),
    )

    assert takeover_checkpoint.generation_has_takeover_checkpoint(generation)
    assert reconciliation_task_domains._generation_has_takeover_checkpoint(  # noqa: SLF001
        generation
    )


def test_takeover_checkpoint_rejects_future_attempt_identity() -> None:
    payload = b"future-checkpoint"
    checkpoint = _checkpoint(
        payload,
        task_id="gen-future-checkpoint",
        execution_epoch=4,
        attempt=2,
    )
    generation = SimpleNamespace(
        id="gen-future-checkpoint",
        user_id="user-1",
        attempt=1,
        execution_epoch=4,
        upstream_request=_request_with_checkpoint(checkpoint),
    )

    assert not takeover_checkpoint.generation_has_takeover_checkpoint(generation)
    assert not reconciliation_task_domains._generation_has_takeover_checkpoint(  # noqa: SLF001
        generation
    )


def test_reconciler_and_runner_accept_same_valid_checkpoint_envelope() -> None:
    payload = b"shared-checkpoint-contract"
    checkpoint = _checkpoint(
        payload,
        task_id="gen-shared-checkpoint",
        execution_epoch=4,
        attempt=2,
    )
    generation = SimpleNamespace(
        id="gen-shared-checkpoint",
        user_id="user-1",
        attempt=2,
        execution_epoch=4,
        upstream_request=_request_with_checkpoint(checkpoint),
    )

    assert takeover_checkpoint.generation_has_takeover_checkpoint(generation)
    assert reconciliation_task_domains._generation_has_takeover_checkpoint(  # noqa: SLF001
        generation
    )

    generation.upstream_request.pop("upstream_response_received_at")

    assert not takeover_checkpoint.generation_has_takeover_checkpoint(generation)
    assert not reconciliation_task_domains._generation_has_takeover_checkpoint(  # noqa: SLF001
        generation
    )


@pytest.mark.asyncio
async def test_invalid_takeover_checkpoint_metadata_fails_closed_at_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failures: list[dict[str, Any]] = []
    state = SimpleNamespace(
        task_id="gen-invalid-checkpoint",
        user_id="user-1",
        generation=SimpleNamespace(
            id="gen-invalid-checkpoint",
            user_id="user-1",
            execution_epoch=2,
            attempt=1,
            upstream_request={CHECKPOINT_KEY: {"storage_key": "broken"}},
        ),
        gen_upstream_request_snapshot={CHECKPOINT_KEY: {"storage_key": "broken"}},
    )

    async def fail_queued(
        _state: Any,
        _session: Any,
        **kwargs: Any,
    ) -> None:
        failures.append(kwargs)

    monkeypatch.setattr(
        runner_claim_phase,
        "fail_queued_generation",
        fail_queued,
    )

    blocked = await runner_claim_phase.fail_nonreplayable_dispatch(
        state,
        object(),
        object(),
    )

    assert blocked is True
    assert failures[0]["code"] == "result_unknown"
    assert "checkpoint metadata is invalid" in failures[0]["message"]
    assert failures[0]["settle_unknown"] is True


@pytest.mark.asyncio
async def test_generation_takeover_checkpoint_skips_second_upstream_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = [
        b"checkpointed-result-1",
        b"checkpointed-result-2",
        b"checkpointed-result-3",
    ]
    checkpoint = _checkpoint_v2(payloads, task_id="gen-takeover")
    stored = {
        result["storage_key"]: payload
        for result, payload in zip(checkpoint["results"], payloads, strict=True)
    }
    artifacts = _CheckpointArtifacts(stored, [])
    state = SimpleNamespace(
        services=SimpleNamespace(artifacts=artifacts),
        task_id="gen-takeover",
        user_id="user-1",
        generation=SimpleNamespace(execution_epoch=3),
        gen_upstream_request_snapshot=_request_with_checkpoint(checkpoint),
        attempt=1,
        loaded_attempt=1,
        lease_token="lease",
        task_outcome="unknown",
        image_iter=None,
        b64_result=None,
        revised_prompt=None,
        actual_upstream_provider=None,
        actual_upstream_route=None,
        actual_upstream_source=None,
        actual_upstream_endpoint=None,
    )
    dispatch_calls = 0
    prepare_calls = 0
    finalized = False

    async def yes(_state: Any) -> bool:
        return True

    async def noop_async(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fail_provider_reservation(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("takeover checkpoint must bypass provider reservation")

    async def prepare_local_state(_state: Any) -> None:
        nonlocal prepare_calls
        prepare_calls += 1

    async def fail_dispatch(_state: Any) -> None:
        nonlocal dispatch_calls
        dispatch_calls += 1
        raise AssertionError("takeover checkpoint must prevent another upstream call")

    async def rethrow_failure(_state: Any, exc: BaseException, _services: Any) -> None:
        raise exc

    async def finalize(restored: Any, _services: Any) -> None:
        nonlocal finalized
        finalized = True
        assert materialize_generated_payload(restored.b64_result) == payloads[0]
        assert restored.revised_prompt == "restored-1"
        assert [
            materialize_generated_payload(pair[0])
            for _index, pair in restored.batch_extra_pairs
        ] == payloads[1:]
        assert [index for index, _pair in restored.batch_extra_pairs] == [2, 3]
        assert restored.requested_image_count == 3
        assert restored.actual_upstream_provider == "provider-1"
        assert restored.image_route == "image2"

    monkeypatch.setattr(runner, "_load_initial_generation", yes)
    monkeypatch.setattr(
        runner,
        "_prepare_provider_reservation",
        fail_provider_reservation,
    )
    monkeypatch.setattr(runner, "_start_generation_attempt", yes)
    monkeypatch.setattr(runner, "_initialize_execution_state", lambda _state: None)
    monkeypatch.setattr(runner, "_prepare_upstream_request", prepare_local_state)
    monkeypatch.setattr(runner, "_dispatch_upstream_request", fail_dispatch)
    monkeypatch.setattr(runner.success, "finalize_generation_success", finalize)
    monkeypatch.setattr(
        runner.failure,
        "handle_generation_exception",
        rethrow_failure,
    )
    monkeypatch.setattr(runner, "_cleanup_generation_run", noop_async)

    await runner._run_generation_scoped(state)  # noqa: SLF001

    assert dispatch_calls == 0
    assert prepare_calls == 1
    assert finalized is True
    assert artifacts.reads == [
        result["storage_key"] for result in checkpoint["results"]
    ]


@pytest.mark.asyncio
async def test_takeover_after_finalized_extra_marker_skips_its_payload() -> None:
    payloads = [b"primary", b"finalized-extra", b"pending-extra"]
    checkpoint = _checkpoint_v2(
        payloads,
        task_id="gen-marker-crash",
        finalized_indexes=frozenset({2}),
    )
    stored = {
        checkpoint["results"][0]["storage_key"]: payloads[0],
        checkpoint["results"][2]["storage_key"]: payloads[2],
    }
    artifacts = _CheckpointArtifacts(stored, [])
    state = SimpleNamespace(
        services=SimpleNamespace(artifacts=artifacts),
        task_id="gen-marker-crash",
        user_id="user-1",
        generation=SimpleNamespace(
            id="gen-marker-crash",
            user_id="user-1",
            execution_epoch=3,
            attempt=2,
            upstream_request=_request_with_checkpoint(checkpoint),
        ),
        attempt=2,
        gen_upstream_request_snapshot=_request_with_checkpoint(checkpoint),
        b64_result=None,
        revised_prompt=None,
        batch_extra_pairs=[],
    )

    await takeover_checkpoint.restore_generation_takeover_checkpoint(state)

    assert materialize_generated_payload(state.b64_result) == payloads[0]
    assert [index for index, _pair in state.batch_extra_pairs] == [3]
    assert (
        materialize_generated_payload(state.batch_extra_pairs[0][1][0]) == (payloads[2])
    )
    assert artifacts.reads == [
        checkpoint["results"][0]["storage_key"],
        checkpoint["results"][2]["storage_key"],
    ]


@pytest.mark.asyncio
async def test_crash_after_extra_billing_before_marker_replays_same_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = [b"primary", b"extra-2", b"extra-3"]
    checkpoint = _checkpoint_v2(payloads, task_id="gen-batch-crash")
    state = _batch_finalize_state(checkpoint, payloads)
    handle_calls: list[str] = []
    marker_calls: list[int] = []
    contexts: list[Any] = []
    settled_ids: set[str] = set()
    crash_once = True

    async def finalize_extra(context: Any) -> bool:
        contexts.append(context)
        handle_calls.append(str(context.bonus_generation_id))
        settled_ids.add(str(context.bonus_generation_id))
        return True

    async def mark_finalized(
        _state: Any,
        *,
        index: int,
        bonus_generation_id: str,
    ) -> None:
        nonlocal crash_once
        marker_calls.append(index)
        assert (
            bonus_generation_id
            == checkpoint["results"][index - 1]["bonus_generation_id"]
        )
        if crash_once:
            crash_once = False
            raise RuntimeError("crash after extra billing commit")

    monkeypatch.setattr(
        batch_results,
        "handle_dual_race_bonus_image",
        finalize_extra,
    )
    monkeypatch.setattr(
        batch_results,
        "mark_generation_takeover_result_finalized",
        mark_finalized,
    )
    monkeypatch.setattr(
        batch_results,
        "generation_takeover_extras_finalized",
        lambda _state: True,
    )

    with pytest.raises(
        batch_results.BatchExtraFinalizationError,
        match="checkpoint update requires takeover",
    ):
        await batch_results.finalize_batch_extra_images(state, 3)

    await batch_results.finalize_batch_extra_images(state, 3)

    extra_ids = [
        str(result["bonus_generation_id"]) for result in checkpoint["results"][1:]
    ]
    assert handle_calls == [extra_ids[0], extra_ids[0], extra_ids[1]]
    assert settled_ids == set(extra_ids)
    assert marker_calls == [2, 2, 3]
    assert contexts[0].attempt == 2
    assert contexts[0].source_attempt == 1
    assert contexts[0].require_precreated_generation is True
    assert contexts[0].idempotency_suffix.endswith(":e3:a1")


@pytest.mark.asyncio
async def test_batch_extra_finalization_keeps_free_admission_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = [b"primary", b"free-extra"]
    checkpoint = _checkpoint_v2(payloads, task_id="gen-batch-crash")
    state = _batch_finalize_state(checkpoint, payloads)
    state.billing_admission_billable = False
    state.billing_admission_source = "no_billable_admission_evidence"
    contexts: list[Any] = []

    async def finalize_extra(context: Any) -> bool:
        contexts.append(context)
        return True

    async def mark_finalized(
        _state: Any,
        *,
        index: int,
        bonus_generation_id: str,
    ) -> None:
        assert index == 2
        assert bonus_generation_id == checkpoint["results"][1]["bonus_generation_id"]

    monkeypatch.setattr(
        batch_results,
        "handle_dual_race_bonus_image",
        finalize_extra,
    )
    monkeypatch.setattr(
        batch_results,
        "mark_generation_takeover_result_finalized",
        mark_finalized,
    )
    monkeypatch.setattr(
        batch_results,
        "generation_takeover_extras_finalized",
        lambda _state: True,
    )

    await batch_results.finalize_batch_extra_images(state, 2)

    assert len(contexts) == 1
    assert contexts[0].billing_meta == {
        "billing_free": True,
        "billing_label": "free",
        "billing_policy": "batch_extra_settled_separately",
        "billing_exempt_reason": "parent_billing_admission_free",
    }


@pytest.mark.asyncio
async def test_extra_failure_blocks_parent_storage_billing_and_success_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    state = SimpleNamespace(
        is_dual_race=False,
        image_iter=None,
    )

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def postprocess(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(actual_image_count=3)

    async def fail_extra(*_args: Any, **_kwargs: Any) -> None:
        raise batch_results.BatchExtraFinalizationError(
            "forced crash before parent success"
        )

    async def write_parent(*_args: Any, **_kwargs: Any) -> list[str]:
        calls.append("write")
        return []

    async def persist_parent(*_args: Any, **_kwargs: Any) -> None:
        calls.append("persist")

    monkeypatch.setattr(success, "_validate_result_and_publish_finalizing", noop)
    monkeypatch.setattr(success, "ensure_dual_race_bonus_obligation", noop)
    monkeypatch.setattr(success, "_postprocess_generated_image", postprocess)
    monkeypatch.setattr(success, "finalize_batch_extra_images", fail_extra)
    monkeypatch.setattr(success, "_write_artifact_files", write_parent)
    monkeypatch.setattr(success, "_persist_generation_success", persist_parent)

    with pytest.raises(
        batch_results.BatchExtraFinalizationError,
        match="forced crash",
    ):
        await success.finalize_generation_success(state, SimpleNamespace())

    assert calls == []


@pytest.mark.asyncio
async def test_lease_loss_with_durable_checkpoint_requeues_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"checkpointed"
    checkpoint = _checkpoint(payload, task_id="gen-lease-takeover")
    request = _request_with_checkpoint(checkpoint)
    state = SimpleNamespace(
        services=object(),
        redis=object(),
        task_id="gen-lease-takeover",
        user_id="user-1",
        attempt=2,
        generation=SimpleNamespace(
            id="gen-lease-takeover",
            user_id="user-1",
            attempt=2,
            execution_epoch=3,
            upstream_request=request,
        ),
        gen_upstream_request_snapshot=request,
    )
    handled: list[BaseException] = []

    async def requeue(
        _state: Any,
        exc: BaseException,
        _services: Any,
    ) -> None:
        handled.append(exc)

    async def fail_unknown(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("durable checkpoint lease loss must remain replayable")

    monkeypatch.setattr(runner.failure, "handle_lease_lost", requeue)
    monkeypatch.setattr(runner, "finalize_generation_result_unknown", fail_unknown)
    monkeypatch.setattr(runner, "finalize_generation_cancel_unknown", fail_unknown)

    error = runner.LeaseLost("lease lost after checkpoint")
    await runner._handle_generation_lease_lost(state, error)  # noqa: SLF001

    assert handled == [error]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_payload", "message"),
    [
        (
            FileNotFoundError("checkpoint missing"),
            "checkpoint payload is missing",
        ),
        (
            b"x" * len(b"checkpointed-result"),
            "checkpoint integrity mismatch",
        ),
    ],
)
async def test_takeover_checkpoint_storage_failure_is_result_unknown_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    stored_payload: bytes | BaseException,
    message: str,
) -> None:
    payload = b"checkpointed-result"
    checkpoint = _checkpoint(payload, task_id="gen-checkpoint-failure")
    artifacts = _CheckpointArtifacts(stored_payload, [])
    state = SimpleNamespace(
        services=SimpleNamespace(artifacts=artifacts),
        redis=object(),
        task_id="gen-checkpoint-failure",
        user_id="user-1",
        generation=SimpleNamespace(execution_epoch=3),
        gen_upstream_request_snapshot=_request_with_checkpoint(checkpoint),
        attempt=2,
        loaded_attempt=1,
        lease_token="lease",
        task_outcome="unknown",
        image_iter=None,
    )
    unknown_failures: list[BaseException] = []

    async def yes(_state: Any) -> bool:
        return True

    async def noop_async(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fail_provider_reservation(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("checkpoint recovery must not reserve a provider")

    async def fail_success(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("unavailable checkpoint cannot finalize successfully")

    async def fail_generic(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("checkpoint failure must use unknown settlement")

    async def not_cancelled(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def settle_unknown(
        recovered_state: Any,
        exc: BaseException,
    ) -> None:
        unknown_failures.append(exc)
        recovered_state.task_outcome = "failed"

    monkeypatch.setattr(runner, "_load_initial_generation", yes)
    monkeypatch.setattr(
        runner,
        "_prepare_provider_reservation",
        fail_provider_reservation,
    )
    monkeypatch.setattr(runner, "_start_generation_attempt", yes)
    monkeypatch.setattr(runner, "_initialize_execution_state", lambda _state: None)
    monkeypatch.setattr(runner, "_prepare_upstream_request", noop_async)
    monkeypatch.setattr(runner, "_dispatch_upstream_request", fail_provider_reservation)
    monkeypatch.setattr(runner.success, "finalize_generation_success", fail_success)
    monkeypatch.setattr(
        runner.failure,
        "handle_generation_exception",
        fail_generic,
    )
    monkeypatch.setattr(runner, "is_cancelled", not_cancelled)
    monkeypatch.setattr(
        runner,
        "finalize_generation_result_unknown",
        settle_unknown,
    )
    monkeypatch.setattr(
        runner,
        "finalize_generation_cancel_unknown",
        fail_generic,
    )
    monkeypatch.setattr(runner, "_cleanup_generation_run", noop_async)

    await runner._run_generation_scoped(state)  # noqa: SLF001

    assert len(unknown_failures) == 1
    assert message in str(unknown_failures[0])
    assert getattr(unknown_failures[0], "error_code", None) == (
        "image_job_result_unknown"
    )
    assert state.task_outcome == "failed"
    assert artifacts.reads == [checkpoint["storage_key"]]
