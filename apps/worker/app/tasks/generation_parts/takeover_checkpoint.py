"""Durable upstream image checkpoints used by generation takeovers."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from lumen_core.constants import GenerationErrorCode as EC
from lumen_core.model_entities.tasks import Generation
from lumen_core.upstream_billing import (
    GENERATION_TAKEOVER_CHECKPOINT_KEY,
    has_upstream_response_receipt,
    mark_upstream_response_received,
    receipt_execution_identity,
)

from ...artifact_commit import (
    ArtifactAdoption,
    ArtifactCommitOutcomeUnknown,
    commit_error_or_default,
    commit_with_adoption_probe,
)
from ...upstream_parts import (
    InlineImageBytes,
    cleanup_owned_generated_payload,
    materialize_generated_payload,
)
from .active_user_fence import lock_active_generation_user
from .batch_obligations import add_batch_extra_billing_obligations
from .errors import StaleGenerationAttempt, TaskCancelled
from .retry_state import (
    RUNNING_GENERATION_STATUSES,
    generation_execution_epoch,
)


GENERATION_TAKEOVER_CHECKPOINT_VERSION = 2
GENERATION_TAKEOVER_LEGACY_VERSION = 1
RESULT_FINALIZATION_PENDING = "pending"
RESULT_FINALIZATION_FINALIZED = "finalized"
_RESULT_FINALIZATION_STATES = frozenset(
    {
        RESULT_FINALIZATION_PENDING,
        RESULT_FINALIZATION_FINALIZED,
    }
)
logger = logging.getLogger(__name__)


class GenerationTakeoverCheckpointUnavailable(RuntimeError):
    """A recorded upstream result cannot be recovered without replaying it."""

    error_code = EC.IMAGE_JOB_RESULT_UNKNOWN.value

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.payload = {
            "upstream_result_unknown": True,
            "takeover_checkpoint_unavailable": True,
        }


@dataclass(frozen=True, slots=True)
class GenerationTakeoverPayload:
    storage_key: str
    size_bytes: int
    sha256: str
    revised_prompt: str | None

    def as_result(
        self,
        *,
        index: int,
        bonus_generation_id: str | None,
    ) -> GenerationTakeoverResultCheckpoint:
        return GenerationTakeoverResultCheckpoint(
            index=index,
            storage_key=self.storage_key,
            size_bytes=self.size_bytes,
            sha256=self.sha256.lower(),
            revised_prompt=self.revised_prompt,
            bonus_generation_id=bonus_generation_id,
            finalization_state=RESULT_FINALIZATION_PENDING,
        )


@dataclass(frozen=True, slots=True)
class GenerationTakeoverResultCheckpoint:
    index: int
    storage_key: str
    size_bytes: int
    sha256: str
    revised_prompt: str | None
    bonus_generation_id: str | None
    finalization_state: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "storage_key": self.storage_key,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "revised_prompt": self.revised_prompt,
            "bonus_generation_id": self.bonus_generation_id,
            "finalization_state": self.finalization_state,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
    ) -> GenerationTakeoverResultCheckpoint | None:
        if not isinstance(raw, dict):
            return None
        try:
            index = max(1, int(raw.get("index")))
            size_bytes = max(0, int(raw.get("size_bytes")))
        except (TypeError, ValueError):
            return None
        storage_key = raw.get("storage_key")
        digest = raw.get("sha256")
        finalization_state = raw.get(
            "finalization_state",
            RESULT_FINALIZATION_PENDING,
        )
        if (
            not isinstance(storage_key, str)
            or not storage_key
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
            or size_bytes <= 0
            or finalization_state not in _RESULT_FINALIZATION_STATES
        ):
            return None
        revised_prompt = raw.get("revised_prompt")
        bonus_generation_id = raw.get("bonus_generation_id")
        return cls(
            index=index,
            storage_key=storage_key,
            size_bytes=size_bytes,
            sha256=digest.lower(),
            revised_prompt=(
                revised_prompt
                if isinstance(revised_prompt, str) and revised_prompt
                else None
            ),
            bonus_generation_id=(
                bonus_generation_id
                if isinstance(bonus_generation_id, str) and bonus_generation_id
                else None
            ),
            finalization_state=str(finalization_state),
        )


@dataclass(frozen=True, slots=True)
class GenerationTakeoverCheckpoint:
    schema_version: int
    execution_epoch: int
    attempt: int
    expected_count: int
    collection_complete: bool
    results: tuple[GenerationTakeoverResultCheckpoint, ...]
    provider: str | None
    route: str | None
    source: str | None
    endpoint: str | None

    @classmethod
    def from_legacy_payload(
        cls,
        execution_epoch: int,
        attempt: int,
        payload: GenerationTakeoverPayload,
        *,
        provider: str | None = None,
        route: str | None = None,
        source: str | None = None,
        endpoint: str | None = None,
    ) -> GenerationTakeoverCheckpoint:
        return cls(
            schema_version=GENERATION_TAKEOVER_LEGACY_VERSION,
            execution_epoch=int(execution_epoch),
            attempt=int(attempt),
            expected_count=1,
            collection_complete=True,
            results=(payload.as_result(index=1, bonus_generation_id=None),),
            provider=provider,
            route=route,
            source=source,
            endpoint=endpoint,
        )

    @property
    def primary(self) -> GenerationTakeoverResultCheckpoint:
        return self.results[0]

    @property
    def storage_key(self) -> str:
        return self.primary.storage_key

    @property
    def size_bytes(self) -> int:
        return self.primary.size_bytes

    @property
    def sha256(self) -> str:
        return self.primary.sha256

    @property
    def revised_prompt(self) -> str | None:
        return self.primary.revised_prompt

    @property
    def storage_keys(self) -> list[str]:
        return [result.storage_key for result in self.results]

    @property
    def extras_finalized(self) -> bool:
        return all(
            result.finalization_state == RESULT_FINALIZATION_FINALIZED
            for result in self.results[1:]
        )

    def result(self, index: int) -> GenerationTakeoverResultCheckpoint | None:
        return next(
            (result for result in self.results if result.index == index),
            None,
        )

    def mark_finalized(
        self,
        *,
        index: int,
        bonus_generation_id: str,
    ) -> GenerationTakeoverCheckpoint:
        updated: list[GenerationTakeoverResultCheckpoint] = []
        matched = False
        for result in self.results:
            if result.index != index:
                updated.append(result)
                continue
            if result.bonus_generation_id != bonus_generation_id:
                raise ValueError(f"checkpoint result identity mismatch index={index}")
            updated.append(
                replace(
                    result,
                    finalization_state=RESULT_FINALIZATION_FINALIZED,
                )
            )
            matched = True
        if not matched:
            raise ValueError(f"checkpoint result not found index={index}")
        return replace(self, results=tuple(updated))

    def to_mapping(self) -> dict[str, Any]:
        if self.schema_version == GENERATION_TAKEOVER_LEGACY_VERSION:
            return {
                "version": self.schema_version,
                "execution_epoch": self.execution_epoch,
                "attempt": self.attempt,
                "storage_key": self.storage_key,
                "size_bytes": self.size_bytes,
                "sha256": self.sha256,
                "revised_prompt": self.revised_prompt,
                "provider": self.provider,
                "route": self.route,
                "source": self.source,
                "endpoint": self.endpoint,
            }
        return {
            "version": self.schema_version,
            "execution_epoch": self.execution_epoch,
            "attempt": self.attempt,
            "expected_count": self.expected_count,
            "collection_complete": self.collection_complete,
            "results": [result.to_mapping() for result in self.results],
            "provider": self.provider,
            "route": self.route,
            "source": self.source,
            "endpoint": self.endpoint,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        execution_epoch: int | None = None,
    ) -> GenerationTakeoverCheckpoint | None:
        if not isinstance(raw, dict):
            return None
        try:
            version = int(raw.get("version"))
            checkpoint_epoch = max(0, int(raw.get("execution_epoch")))
            attempt = max(1, int(raw.get("attempt")))
        except (TypeError, ValueError):
            return None
        if execution_epoch is not None and checkpoint_epoch != max(
            0, int(execution_epoch)
        ):
            return None
        results = _checkpoint_results(raw, version=version)
        if results is None:
            return None
        expected_count = len(results)
        collection_complete = True
        if version == GENERATION_TAKEOVER_CHECKPOINT_VERSION:
            try:
                expected_count = max(1, int(raw.get("expected_count")))
            except (TypeError, ValueError):
                return None
            collection_complete = raw.get("collection_complete") is True
            if (
                len(results) > expected_count
                or (collection_complete and expected_count != len(results))
                or (not collection_complete and expected_count <= len(results))
            ):
                return None
        optional: dict[str, str | None] = {}
        for key in ("provider", "route", "source", "endpoint"):
            value = raw.get(key)
            optional[key] = value if isinstance(value, str) and value else None
        return cls(
            schema_version=version,
            execution_epoch=checkpoint_epoch,
            attempt=attempt,
            expected_count=expected_count,
            collection_complete=collection_complete,
            results=results,
            provider=optional["provider"],
            route=optional["route"],
            source=optional["source"],
            endpoint=optional["endpoint"],
        )


def _checkpoint_results(
    raw: dict[str, Any],
    *,
    version: int,
) -> tuple[GenerationTakeoverResultCheckpoint, ...] | None:
    if version == GENERATION_TAKEOVER_LEGACY_VERSION:
        legacy = GenerationTakeoverResultCheckpoint.from_mapping(
            {
                "index": 1,
                "storage_key": raw.get("storage_key"),
                "size_bytes": raw.get("size_bytes"),
                "sha256": raw.get("sha256"),
                "revised_prompt": raw.get("revised_prompt"),
                "finalization_state": RESULT_FINALIZATION_PENDING,
            }
        )
        return (legacy,) if legacy is not None else None
    if version != GENERATION_TAKEOVER_CHECKPOINT_VERSION:
        return None
    raw_results = raw.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        return None
    parsed = tuple(
        GenerationTakeoverResultCheckpoint.from_mapping(result)
        for result in raw_results
    )
    if any(result is None for result in parsed):
        return None
    results = tuple(result for result in parsed if result is not None)
    if [result.index for result in results] != list(range(1, len(results) + 1)):
        return None
    if results[0].bonus_generation_id is not None:
        return None
    bonus_ids = [result.bonus_generation_id for result in results[1:]]
    if any(bonus_id is None for bonus_id in bonus_ids):
        return None
    if len(set(bonus_ids)) != len(bonus_ids):
        return None
    return results


def _checkpoint_request(state: Any) -> dict[str, Any]:
    snapshot = getattr(state, "gen_upstream_request_snapshot", None)
    if isinstance(snapshot, dict):
        return snapshot
    generation = getattr(state, "generation", state)
    request = getattr(generation, "upstream_request", None)
    return request if isinstance(request, dict) else {}


def generation_takeover_checkpoint_present(state: Any) -> bool:
    return GENERATION_TAKEOVER_CHECKPOINT_KEY in _checkpoint_request(state)


def _checkpoint_owner(state: Any) -> tuple[str, str]:
    generation = getattr(state, "generation", None) or state
    user_id = getattr(state, "user_id", None) or getattr(generation, "user_id", None)
    task_id = getattr(state, "task_id", None) or getattr(generation, "id", None)
    return str(user_id or ""), str(task_id or "")


def _checkpoint_expected_attempt(state: Any) -> int | None:
    loaded_attempt = getattr(state, "loaded_attempt", None)
    generation = getattr(state, "generation", None)
    raw_attempt = (
        loaded_attempt
        if loaded_attempt is not None
        else getattr(generation, "attempt", None)
        if generation is not None
        else getattr(state, "attempt", None)
    )
    try:
        return max(0, int(raw_attempt))
    except (TypeError, ValueError):
        return None


def _checkpoint_storage_key(
    state: Any,
    *,
    index: int,
    attempt: int,
    schema_version: int = GENERATION_TAKEOVER_CHECKPOINT_VERSION,
) -> str:
    user_id, task_id = _checkpoint_owner(state)
    prefix = (
        f"u/{user_id}/g/{task_id}/executions/"
        f"{generation_execution_epoch(state)}/attempts/{max(1, int(attempt))}"
    )
    if schema_version == GENERATION_TAKEOVER_LEGACY_VERSION:
        return f"{prefix}/takeover-result.bin"
    return f"{prefix}/takeover-result-{max(1, int(index))}.bin"


def batch_extra_generation_id(
    state: Any,
    *,
    index: int,
    attempt: int,
) -> str:
    _user_id, task_id = _checkpoint_owner(state)
    return str(
        uuid5(
            NAMESPACE_URL,
            (
                f"lumen:batch-extra:{task_id}:"
                f"{generation_execution_epoch(state)}:{max(1, int(attempt))}:"
                f"{max(2, int(index))}"
            ),
        )
    )


def generation_takeover_checkpoint(state: Any) -> GenerationTakeoverCheckpoint | None:
    request = _checkpoint_request(state)
    checkpoint = GenerationTakeoverCheckpoint.from_mapping(
        request.get(GENERATION_TAKEOVER_CHECKPOINT_KEY),
        execution_epoch=generation_execution_epoch(state),
    )
    if checkpoint is None:
        return None
    user_id, task_id = _checkpoint_owner(state)
    expected_attempt = _checkpoint_expected_attempt(state)
    response_attempt, response_epoch = receipt_execution_identity(
        request,
        response=True,
    )
    if (
        not user_id
        or not task_id
        or expected_attempt is None
        or not checkpoint.collection_complete
        or checkpoint.attempt > expected_attempt
        or not has_upstream_response_receipt(
            request,
            execution_epoch=checkpoint.execution_epoch,
        )
        or response_attempt != checkpoint.attempt
        or response_epoch != checkpoint.execution_epoch
    ):
        return None
    for result in checkpoint.results:
        if result.storage_key != _checkpoint_storage_key(
            state,
            index=result.index,
            attempt=checkpoint.attempt,
            schema_version=checkpoint.schema_version,
        ):
            return None
        if result.index > 1 and result.bonus_generation_id != batch_extra_generation_id(
            state,
            index=result.index,
            attempt=checkpoint.attempt,
        ):
            return None
    return checkpoint


def generation_has_takeover_checkpoint(state: Any) -> bool:
    return generation_takeover_checkpoint(state) is not None


def generation_takeover_result(
    state: Any,
    index: int,
) -> GenerationTakeoverResultCheckpoint | None:
    checkpoint = generation_takeover_checkpoint(state)
    return checkpoint.result(index) if checkpoint is not None else None


def generation_takeover_result_count(state: Any) -> int | None:
    checkpoint = generation_takeover_checkpoint(state)
    return checkpoint.expected_count if checkpoint is not None else None


def generation_takeover_extras_finalized(state: Any) -> bool:
    checkpoint = generation_takeover_checkpoint(state)
    return checkpoint is None or checkpoint.extras_finalized


async def _checkpoint_adoption(
    state: Any,
    checkpoint: GenerationTakeoverCheckpoint,
) -> ArtifactAdoption:
    async with state.services.store.session() as session:
        generation = await session.get(Generation, state.task_id)
        if generation is None:
            return ArtifactAdoption.NOT_ADOPTED
        current = GenerationTakeoverCheckpoint.from_mapping(
            (
                generation.upstream_request.get(GENERATION_TAKEOVER_CHECKPOINT_KEY)
                if isinstance(generation.upstream_request, dict)
                else None
            ),
            execution_epoch=generation_execution_epoch(state),
        )
        if current == checkpoint:
            return ArtifactAdoption.ADOPTED
        if (
            int(getattr(generation, "execution_epoch", 0) or 0)
            != generation_execution_epoch(state)
            or int(getattr(generation, "attempt", 0) or 0) != state.attempt
        ):
            return ArtifactAdoption.UNKNOWN
        return ArtifactAdoption.NOT_ADOPTED


def _materialize_result_payload(payload: Any) -> bytes:
    try:
        return materialize_generated_payload(payload)
    finally:
        if not isinstance(payload, str):
            cleanup_owned_generated_payload(payload)


def _checkpoint_pairs(state: Any) -> list[tuple[int, Any, str | None]]:
    pairs = [(1, state.b64_result, state.revised_prompt)]
    pairs.extend(
        (index, payload, revised_prompt)
        for index, (payload, revised_prompt) in state.batch_extra_pairs
    )
    return pairs


async def persist_generation_takeover_checkpoint(state: Any) -> None:
    pairs = _checkpoint_pairs(state)
    expected_count = max(1, int(state.requested_image_count))
    if pairs[0][1] is None:
        raise ValueError("generation result is missing before takeover checkpoint")
    if len(pairs) > expected_count:
        raise ValueError(
            "generation result batch exceeds takeover checkpoint expectation "
            f"expected={expected_count} actual={len(pairs)}"
        )
    checkpoint_attempt = max(1, int(state.attempt))
    results: list[GenerationTakeoverResultCheckpoint] = []
    materialized: list[tuple[int, InlineImageBytes, str | None]] = []
    files: list[tuple[str, bytes]] = []
    for index, payload, revised_prompt in pairs:
        raw = _materialize_result_payload(payload)
        storage_key = _checkpoint_storage_key(
            state,
            index=index,
            attempt=checkpoint_attempt,
        )
        result = GenerationTakeoverResultCheckpoint(
            index=index,
            storage_key=storage_key,
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            revised_prompt=revised_prompt,
            bonus_generation_id=(
                batch_extra_generation_id(
                    state,
                    index=index,
                    attempt=checkpoint_attempt,
                )
                if index > 1
                else None
            ),
            finalization_state=RESULT_FINALIZATION_PENDING,
        )
        results.append(result)
        materialized.append((index, InlineImageBytes(raw), revised_prompt))
        files.append((storage_key, raw))
    state.b64_result = materialized[0][1]
    state.batch_extra_pairs = [
        (index, (payload, revised_prompt))
        for index, payload, revised_prompt in materialized[1:]
    ]
    checkpoint = GenerationTakeoverCheckpoint(
        schema_version=GENERATION_TAKEOVER_CHECKPOINT_VERSION,
        execution_epoch=generation_execution_epoch(state),
        attempt=checkpoint_attempt,
        expected_count=expected_count,
        collection_complete=len(results) == expected_count,
        results=tuple(results),
        provider=state.actual_upstream_provider,
        route=state.actual_upstream_route,
        source=state.actual_upstream_source,
        endpoint=state.actual_upstream_endpoint,
    )
    created_keys = await state.services.artifacts.write_files(files)
    cleanup_allowed = True
    try:
        async with state.services.store.session() as session:
            if not await lock_active_generation_user(
                session,
                user_id=state.user_id,
            ):
                raise TaskCancelled("account deleted before generation checkpoint")
            current = (
                await session.execute(
                    select(Generation)
                    .where(
                        Generation.id == state.task_id,
                        Generation.attempt == state.attempt,
                        Generation.execution_epoch == generation_execution_epoch(state),
                        Generation.status.in_(RUNNING_GENERATION_STATUSES),
                        Generation.cancel_requested_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current is None:
                raise StaleGenerationAttempt(
                    "generation checkpoint lost ownership "
                    f"task={state.task_id} attempt={state.attempt}"
                )
            recorded_at = datetime.now(timezone.utc).isoformat()
            request = mark_upstream_response_received(
                current,
                at=recorded_at,
                attempt=state.attempt,
                execution_epoch=generation_execution_epoch(state),
            )
            request[GENERATION_TAKEOVER_CHECKPOINT_KEY] = checkpoint.to_mapping()
            current.upstream_request = request
            if GENERATION_TAKEOVER_CHECKPOINT_KEY not in (
                state.gen_upstream_request_snapshot or {}
            ):
                add_batch_extra_billing_obligations(
                    session,
                    state,
                    bonus_results=tuple(
                        (
                            index,
                            batch_extra_generation_id(
                                state,
                                index=index,
                                attempt=checkpoint_attempt,
                            ),
                        )
                        for index in range(2, expected_count + 1)
                    ),
                    source_attempt=checkpoint.attempt,
                    expected_count=checkpoint.expected_count,
                )
            commit_result = await commit_with_adoption_probe(
                session,
                probe=lambda: _checkpoint_adoption(state, checkpoint),
                logger=logger,
                label=f"generation takeover checkpoint {state.task_id}",
            )
            if commit_result.adopted:
                cleanup_allowed = False
            elif commit_result.outcome is ArtifactAdoption.NOT_ADOPTED:
                raise commit_error_or_default(
                    commit_result,
                    label=f"generation takeover checkpoint {state.task_id}",
                )
            else:
                cleanup_allowed = False
                unknown = ArtifactCommitOutcomeUnknown(
                    "generation takeover checkpoint outcome unknown "
                    f"task={state.task_id} attempt={state.attempt}"
                )
                if commit_result.commit_error is not None:
                    raise unknown from commit_result.commit_error
                raise unknown
            state.gen_upstream_request_snapshot = dict(request)
            state.dispatch_marker_recorded = True
    except BaseException:
        if cleanup_allowed:
            await state.services.artifacts.delete_files(created_keys)
        raise


async def _restore_result_payload(
    state: Any,
    result: GenerationTakeoverResultCheckpoint,
) -> InlineImageBytes:
    try:
        raw = await state.services.artifacts.get_bytes(result.storage_key)
    except Exception as exc:  # noqa: BLE001
        raise GenerationTakeoverCheckpointUnavailable(
            f"generation takeover checkpoint payload is missing index={result.index}"
        ) from exc
    if (
        len(raw) != result.size_bytes
        or hashlib.sha256(raw).hexdigest() != result.sha256
    ):
        raise GenerationTakeoverCheckpointUnavailable(
            f"generation takeover checkpoint integrity mismatch index={result.index}"
        )
    return InlineImageBytes(raw)


async def restore_generation_takeover_checkpoint(state: Any) -> None:
    checkpoint = generation_takeover_checkpoint(state)
    if checkpoint is None:
        raise GenerationTakeoverCheckpointUnavailable(
            "generation takeover checkpoint metadata is invalid"
        )
    primary = checkpoint.primary
    state.b64_result = await _restore_result_payload(state, primary)
    state.revised_prompt = primary.revised_prompt
    state.batch_extra_pairs = []
    for result in checkpoint.results[1:]:
        if result.finalization_state == RESULT_FINALIZATION_FINALIZED:
            continue
        payload = await _restore_result_payload(state, result)
        state.batch_extra_pairs.append((result.index, (payload, result.revised_prompt)))
    state.requested_image_count = checkpoint.expected_count
    state.actual_upstream_provider = checkpoint.provider
    state.actual_upstream_route = checkpoint.route
    state.actual_upstream_source = checkpoint.source
    state.actual_upstream_endpoint = checkpoint.endpoint
    if checkpoint.provider is not None:
        state.upstream_provider_label = checkpoint.provider
    if checkpoint.route is not None:
        state.raw_image_route = checkpoint.route
        state.image_route = checkpoint.route


async def mark_generation_takeover_result_finalized(
    state: Any,
    *,
    index: int,
    bonus_generation_id: str,
) -> None:
    async with state.services.store.session() as session:
        current = (
            await session.execute(
                select(Generation)
                .where(
                    Generation.id == state.task_id,
                    Generation.attempt == state.attempt,
                    Generation.execution_epoch == generation_execution_epoch(state),
                    Generation.status.in_(RUNNING_GENERATION_STATUSES),
                    Generation.cancel_requested_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current is None:
            raise StaleGenerationAttempt(
                "generation result finalization lost ownership "
                f"task={state.task_id} attempt={state.attempt}"
            )
        checkpoint = generation_takeover_checkpoint(current)
        if checkpoint is None:
            raise GenerationTakeoverCheckpointUnavailable(
                "generation takeover checkpoint metadata is invalid during finalization"
            )
        result = checkpoint.result(index)
        if result is None or result.bonus_generation_id != bonus_generation_id:
            raise GenerationTakeoverCheckpointUnavailable(
                f"generation takeover checkpoint result is invalid index={index}"
            )
        if result.finalization_state == RESULT_FINALIZATION_FINALIZED:
            state.gen_upstream_request_snapshot = dict(current.upstream_request or {})
            return
        updated = checkpoint.mark_finalized(
            index=index,
            bonus_generation_id=bonus_generation_id,
        )
        request = dict(current.upstream_request or {})
        request[GENERATION_TAKEOVER_CHECKPOINT_KEY] = updated.to_mapping()
        current.upstream_request = request
        await session.commit()
        state.gen_upstream_request_snapshot = dict(request)


def clear_generation_takeover_checkpoint(
    upstream_request: dict[str, Any],
) -> list[str]:
    raw = upstream_request.pop(GENERATION_TAKEOVER_CHECKPOINT_KEY, None)
    checkpoint = GenerationTakeoverCheckpoint.from_mapping(raw)
    return checkpoint.storage_keys if checkpoint is not None else []


def consume_checkpoint(state: Any, upstream_request: dict[str, Any]) -> None:
    storage_keys = clear_generation_takeover_checkpoint(upstream_request)
    state.takeover_checkpoint_storage_keys = storage_keys
    state.takeover_checkpoint_storage_key = storage_keys[0] if storage_keys else None


async def cleanup_consumed_checkpoint(state: Any, artifacts: Any) -> None:
    storage_keys = list(getattr(state, "takeover_checkpoint_storage_keys", None) or [])
    legacy_key = getattr(state, "takeover_checkpoint_storage_key", None)
    if legacy_key and legacy_key not in storage_keys:
        storage_keys.append(legacy_key)
    if storage_keys:
        await artifacts.delete_files(storage_keys)
    state.takeover_checkpoint_storage_keys = []
    state.takeover_checkpoint_storage_key = None


__all__ = [
    "GENERATION_TAKEOVER_CHECKPOINT_KEY",
    "GENERATION_TAKEOVER_CHECKPOINT_VERSION",
    "GenerationTakeoverCheckpoint",
    "GenerationTakeoverCheckpointUnavailable",
    "GenerationTakeoverPayload",
    "GenerationTakeoverResultCheckpoint",
    "RESULT_FINALIZATION_FINALIZED",
    "RESULT_FINALIZATION_PENDING",
    "batch_extra_generation_id",
    "cleanup_consumed_checkpoint",
    "clear_generation_takeover_checkpoint",
    "consume_checkpoint",
    "generation_has_takeover_checkpoint",
    "generation_takeover_checkpoint",
    "generation_takeover_checkpoint_present",
    "generation_takeover_extras_finalized",
    "generation_takeover_result",
    "generation_takeover_result_count",
    "mark_generation_takeover_result_finalized",
    "persist_generation_takeover_checkpoint",
    "restore_generation_takeover_checkpoint",
]
