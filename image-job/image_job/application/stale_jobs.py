"""Independent CAS policy for stale active image jobs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import ImageJobSettings
from ..contracts import ERROR_CLASS_NETWORK


@dataclass(frozen=True)
class ActiveStalePolicy:
    settings: ImageJobSettings
    repository: Any
    log: logging.Logger
    batch_size: int = 256

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def active_stale_after_s(self) -> float:
        retry_budget = max(
            self.settings.retry_network_max,
            self.settings.retry_responses_stream_max,
            self.settings.retry_upstream_5xx_max,
        )
        retry_backoff = sum(
            self.settings.retry_backoff_s * (2**attempt)
            for attempt in range(retry_budget)
        )
        request_budget = (
            self.settings.timeouts.upstream_s * (retry_budget + 1)
            + retry_backoff
        )
        heartbeat_budget = max(
            self.settings.job_heartbeat_interval_s * 4,
            self.settings.stuck_reconcile_interval_s * 2,
        )
        return request_budget + heartbeat_budget

    async def recover(
        self,
        job_id: str,
        *,
        execution_token: str,
        requeue: bool,
    ) -> bool:
        now = self._now().isoformat()
        if requeue:
            changed = await self.repository.execute(
                """
                UPDATE jobs
                SET status = 'queued',
                    started_at = NULL,
                    updated_at = ?,
                    execution_token = NULL,
                    retryable = 0,
                    retry_suppressed = 0,
                    outcome_uncertain = 0
                WHERE job_id = ?
                  AND status = 'running'
                  AND execution_token = ?
                  AND auth_ciphertext IS NOT NULL
                  AND auth_nonce IS NOT NULL
                  AND auth_key_id IS NOT NULL
                """,
                (now, job_id, execution_token),
            )
            return changed == 1

        retention_expires_at = (
            self._now() + timedelta(days=max(1, self.settings.job_ttl_days))
        ).isoformat()
        changed = await self.repository.execute(
            """
            UPDATE jobs
            SET status = 'uncertain',
                auth_header = NULL,
                auth_ciphertext = NULL,
                auth_nonce = NULL,
                auth_key_id = NULL,
                execution_token = NULL,
                finished_at = ?,
                updated_at = ?,
                error = 'image job heartbeat expired while the upstream result was unresolved',
                error_class = ?,
                retryable = 1,
                retry_suppressed = 1,
                outcome_uncertain = 1,
                retention_expires_at = CASE
                    WHEN retention_expires_at IS NULL
                         OR retention_expires_at > ?
                    THEN ?
                    ELSE retention_expires_at
                END
            WHERE job_id = ?
              AND status = 'running'
              AND execution_token = ?
            """,
            (
                now,
                now,
                ERROR_CLASS_NETWORK,
                retention_expires_at,
                retention_expires_at,
                job_id,
                execution_token,
            ),
        )
        return changed == 1

    async def run_pass(self) -> None:
        stale_before = (
            self._now() - timedelta(seconds=self.active_stale_after_s())
        ).isoformat()
        rows = await self.repository.all(
            """
            SELECT job_id, execution_token
            FROM jobs
            WHERE status = 'running'
              AND updated_at <= ?
              AND execution_token IS NOT NULL
            ORDER BY updated_at ASC, job_id ASC
            LIMIT ?
            """,
            (stale_before, self.batch_size),
        )
        for row in rows:
            recovered = await self.recover(
                str(row["job_id"]),
                execution_token=str(row["execution_token"]),
                requeue=self.settings.upstream_idempotency_guaranteed,
            )
            if recovered:
                self.log.warning(
                    "recovered stale running image job %s as %s",
                    row["job_id"],
                    (
                        "queued"
                        if self.settings.upstream_idempotency_guaranteed
                        else "uncertain"
                    ),
                )
