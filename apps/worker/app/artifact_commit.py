"""Resolve ambiguous database commits for file-backed worker artifacts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ArtifactAdoption(Enum):
    COMMITTED = "committed"
    ADOPTED = "adopted"
    NOT_ADOPTED = "not_adopted"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ArtifactCommitResult:
    outcome: ArtifactAdoption
    commit_error: BaseException | None = None
    probe_error: BaseException | None = None

    @property
    def adopted(self) -> bool:
        return self.outcome in {
            ArtifactAdoption.COMMITTED,
            ArtifactAdoption.ADOPTED,
        }


class ArtifactCommitOutcomeUnknown(RuntimeError):
    """The database could not confirm whether an artifact transaction committed."""


class ArtifactCommitNotAdopted(RuntimeError):
    """Artifact commit confirmed not adopted while upstream cost was already incurred.

    The upstream produced the artifact (response receipt exists, upstream billed)
    but the local artifact transaction did not take effect. The failure path must
    settle the wallet hold, never release it — 纯转嫁: 上游已产生成本时不释放 hold。
    ``error_code`` is one of IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES so the runner's
    unknown-result settlement and decide_image_failure_billing both route the
    terminal failure to settlement instead of refunding the hold.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        commit_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.commit_error = commit_error


async def _wait_for_started_task(task: asyncio.Future[Any]) -> Any:
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def rollback_artifact_transaction(
    session: Any,
    *,
    logger: logging.Logger,
    label: str,
) -> bool:
    rollback = getattr(session, "rollback", None)
    if not callable(rollback):
        return False
    task = asyncio.ensure_future(rollback())
    try:
        await _wait_for_started_task(task)
    except BaseException as exc:  # noqa: BLE001
        logger.warning("%s rollback confirmation failed err=%s", label, exc)
        return False
    return True


async def commit_with_adoption_probe(
    session: Any,
    *,
    probe: Callable[[], Awaitable[ArtifactAdoption]],
    logger: logging.Logger,
    label: str,
) -> ArtifactCommitResult:
    try:
        await session.commit()
    except BaseException as commit_error:  # noqa: BLE001
        await rollback_artifact_transaction(
            session,
            logger=logger,
            label=label,
        )
        probe_task = asyncio.ensure_future(probe())
        try:
            outcome = await _wait_for_started_task(probe_task)
        except BaseException as probe_error:  # noqa: BLE001
            logger.error(
                "%s commit outcome remains unknown commit_err=%s probe_err=%s",
                label,
                commit_error,
                probe_error,
            )
            return ArtifactCommitResult(
                outcome=ArtifactAdoption.UNKNOWN,
                commit_error=commit_error,
                probe_error=probe_error,
            )
        if outcome not in {
            ArtifactAdoption.ADOPTED,
            ArtifactAdoption.NOT_ADOPTED,
            ArtifactAdoption.UNKNOWN,
        }:
            probe_error = TypeError(f"invalid artifact adoption outcome: {outcome!r}")
            logger.error("%s adoption probe returned invalid outcome", label)
            return ArtifactCommitResult(
                outcome=ArtifactAdoption.UNKNOWN,
                commit_error=commit_error,
                probe_error=probe_error,
            )
        logger.warning(
            "%s commit acknowledgement reconciled outcome=%s err=%s",
            label,
            outcome.value,
            commit_error,
        )
        return ArtifactCommitResult(
            outcome=outcome,
            commit_error=commit_error,
        )
    return ArtifactCommitResult(outcome=ArtifactAdoption.COMMITTED)


def commit_error_or_default(
    result: ArtifactCommitResult,
    *,
    label: str,
) -> BaseException:
    return result.commit_error or RuntimeError(f"{label} commit was not adopted")


__all__ = [
    "ArtifactAdoption",
    "ArtifactCommitNotAdopted",
    "ArtifactCommitOutcomeUnknown",
    "ArtifactCommitResult",
    "commit_error_or_default",
    "commit_with_adoption_probe",
    "rollback_artifact_transaction",
]
