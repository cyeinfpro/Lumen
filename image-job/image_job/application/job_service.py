"""Image job application service."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .. import http_bodies, payloads, persistence
from ..config import ImageJobSettings
from ..contracts import (
    ALLOWED_FIXED_ENDPOINTS,
    ALLOWED_PREFIX_ENDPOINTS,
    ArtifactCorrupt,
    DEFAULT_IMAGE_OUTPUT_COMPRESSION,
    DEFAULT_IMAGE_OUTPUT_FORMAT,
    ERROR_CLASS_INTERNAL,
    ERROR_CLASS_NETWORK,
    IMAGE_OUTPUT_FORMATS,
    JobFailure,
    JobProcessOutcome,
    PreDispatchFailure,
    UpstreamDispatchReceipt,
)
from ..credential_vault import CredentialVault, CredentialVaultError
from ..domain.identity import CallerIdentity, UpstreamCredential
from ..observability import current_request_id
from ..payloads import json_dump, request_hash
from .auth import credential_hash
from .result_service import ResultService
from .stale_jobs import ActiveStalePolicy


LOG = logging.getLogger("image-job.jobs")
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9._:~-]+\Z")


@dataclass(frozen=True)
class CancelResult:
    job_id: str
    outcome: str
    status: str
    status_code: int = 200
    outcome_uncertain: bool = False

    def response(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "outcome": self.outcome,
            "status": self.status,
            "outcome_uncertain": self.outcome_uncertain,
        }


class JobServiceFailure(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_request_id(row: Any) -> str:
    """从 job 行里取提交侧的 request_id；老库/假行取不到就退化成空串。"""
    try:
        return str(row["request_id"] or "")
    except (IndexError, KeyError, TypeError):
        return ""


class JobService:
    def __init__(
        self,
        settings: ImageJobSettings,
        repository: Any,
        upstream: Any,
        queue: Any,
        credential_vault: CredentialVault,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.upstream = upstream
        self.queue = queue
        self.credential_vault = credential_vault
        self.persistence = persistence.JobPersistenceFacade(
            db_exec=lambda sql, params=(): repository.execute(sql, params),
            enqueue_job=queue.enqueue,
            now_iso=_utc_iso,
            auth_hash=credential_hash,
            credential_vault=credential_vault,
            json_dump=json_dump,
            upstream_base_url=lambda: settings.upstream_base_url,
            upstream_idempotency_guaranteed=(
                lambda: settings.upstream_idempotency_guaranteed
            ),
            error_class_internal=lambda: ERROR_CLASS_INTERNAL,
            error_class_network=lambda: ERROR_CLASS_NETWORK,
            job_ttl_days=lambda: settings.job_ttl_days,
            verify_artifacts=upstream.verify_saved_artifacts,
            log=LOG,
        )
        # H-17：RetentionFacade 过去只在测试脚手架里被实例化过，生产链路根本
        # 没人构造它，`retention_sweep_interval_s` 形同虚设，过期图片和 jobs
        # 行会无限堆积。这里把它接到真正的 runtime 上。
        self.retention = persistence.RetentionFacade(
            data_dir=lambda: settings.data_dir,
            refs_dir=lambda: settings.refs_dir,
            db_exec_sync=lambda sql, params: repository._execute_sync(sql, params),
            db_exec=lambda sql, params=(): repository.execute(sql, params),
            db_all=lambda sql, params=(): repository.all(sql, params),
            utc_now=lambda: datetime.now(timezone.utc),
            max_retention_days=lambda: settings.max_retention_days,
            job_ttl_days=lambda: settings.job_ttl_days,
            log=LOG,
            open_db=getattr(repository, "_open", None),
        )
        self.results = ResultService(repository, self.persistence)
        self.stale_jobs = ActiveStalePolicy(settings, repository, LOG)
        # H-19：/metrics 过去只有队列水位，看不出业务是不是在正常出图。
        # uncertain 单独计数尤其重要——它直接等价于「上游可能已扣费但没交付」
        # 的对账工单量，是纯转嫁下必须盯住的资金风险指标。
        self.outcomes: dict[str, int] = {
            "jobs_succeeded_total": 0,
            "jobs_failed_total": 0,
            "jobs_uncertain_total": 0,
            "images_delivered_total": 0,
            "upstream_latency_ms_total": 0,
        }
        self.dispatch_outcomes: dict[tuple[str, str], int] = {}

    def _record_outcome(self, name: str, elapsed_ms: int, images: int = 0) -> None:
        self.outcomes[name] += 1
        self.outcomes["images_delivered_total"] += images
        self.outcomes["upstream_latency_ms_total"] += elapsed_ms

    def _record_dispatch_outcome(
        self,
        dispatch: UpstreamDispatchReceipt,
        outcome: JobProcessOutcome,
    ) -> None:
        key = (dispatch.phase.value, outcome.value)
        self.dispatch_outcomes[key] = self.dispatch_outcomes.get(key, 0) + 1

    def parse_payload(self, raw: bytes) -> tuple[Any, dict[str, Any]]:
        limits = http_bodies.JsonShapeLimits(
            max_depth=self.settings.max_json_depth,
            max_array_items=self.settings.max_json_array_items,
            max_object_items=self.settings.max_json_object_items,
            max_total_values=self.settings.max_json_total_values,
            max_key_chars=self.settings.max_json_key_chars,
            max_string_chars=self.settings.max_json_string_chars,
        )
        raw_payload = http_bodies.load_json_bytes(raw, limits)
        policy = payloads.PayloadPolicy(
            allowed_fixed_endpoints=ALLOWED_FIXED_ENDPOINTS,
            allowed_prefix_endpoints=ALLOWED_PREFIX_ENDPOINTS,
            image_output_formats=frozenset(IMAGE_OUTPUT_FORMATS),
            default_image_output_format=DEFAULT_IMAGE_OUTPUT_FORMAT,
            default_image_output_compression=DEFAULT_IMAGE_OUTPUT_COMPRESSION,
            responses_strip_partial_images=(
                self.settings.responses_strip_partial_images
            ),
            max_endpoint_chars=self.settings.max_endpoint_chars,
            max_request_type_chars=self.settings.max_request_type_chars,
            default_retention_days=self.settings.default_retention_days,
            max_retention_days=self.settings.max_retention_days,
        )
        return raw_payload, payloads.validate_payload(raw_payload, policy)

    def idempotency_key(
        self,
        headers: Any,
        raw_payload: Any,
    ) -> str:
        raw = str(headers.get("idempotency-key", "") or "").strip()
        if not raw and isinstance(raw_payload, dict):
            candidate = raw_payload.get("idempotency_key")
            raw = candidate.strip() if isinstance(candidate, str) else ""
        if not raw:
            raise JobServiceFailure(
                428,
                "Idempotency-Key is required for image job creation",
            )
        encoded = raw.encode("utf-8")
        if len(encoded) > self.settings.max_idempotency_key_bytes:
            raise JobServiceFailure(
                400,
                "idempotency key exceeds "
                f"{self.settings.max_idempotency_key_bytes} bytes",
            )
        if _IDEMPOTENCY_KEY_RE.fullmatch(raw) is None:
            raise JobServiceFailure(
                400,
                "idempotency key contains invalid characters",
            )
        return hashlib.sha256(encoded).hexdigest()

    def _request_hash(
        self,
        payload: dict[str, Any],
        _caller: CallerIdentity,
        upstream: UpstreamCredential,
    ) -> str:
        return request_hash(
            {
                "payload": payload,
                "upstream_auth_hash": credential_hash(upstream.authorization),
            }
        )

    async def _find_idempotent(
        self,
        *,
        caller: CallerIdentity,
        upstream: UpstreamCredential,
        key: str,
        payload: dict[str, Any],
        payload_hash: str,
    ) -> tuple[Any | None, str]:
        upstream_hash = credential_hash(upstream.authorization)
        row = await self.repository.one(
            """
            SELECT * FROM jobs
            WHERE auth_hash = ? AND upstream_auth_hash = ? AND idempotency_key = ?
            """,
            (caller.owner_hash, upstream_hash, key),
        )
        if row is not None:
            return row, payload_hash
        legacy_hash = request_hash(payload)
        row = await self.repository.one(
            """
            SELECT * FROM jobs
            WHERE auth_hash = ? AND upstream_auth_hash IS NULL
              AND idempotency_key = ? AND request_hash = ?
            """,
            (caller.owner_hash, key, legacy_hash),
        )
        if row is not None:
            return row, legacy_hash
        if hmac.compare_digest(caller.owner_hash, upstream_hash):
            return None, payload_hash
        row = await self.repository.one(
            """
            SELECT * FROM jobs
            WHERE auth_hash = ? AND idempotency_key = ?
              AND (upstream_auth_hash = ? OR upstream_auth_hash IS NULL)
              AND request_hash = ?
            """,
            (upstream_hash, key, upstream_hash, legacy_hash),
        )
        return row, legacy_hash

    async def submit(
        self,
        *,
        caller: CallerIdentity,
        upstream: UpstreamCredential,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise AssertionError("validated idempotency key required")
        payload_hash = self._request_hash(payload, caller, upstream)
        existing, expected = await self._find_idempotent(
            caller=caller,
            upstream=upstream,
            key=idempotency_key,
            payload=payload,
            payload_hash=payload_hash,
        )
        if existing is not None:
            if not hmac.compare_digest(str(existing["request_hash"]), expected):
                raise JobServiceFailure(
                    409,
                    "idempotency key already used for a different image job",
                )
            await self.persistence.ensure_queued_job_scheduled(existing)
            return await self.results.response_for_row(existing)

        job_id = (
            f"img_{datetime.now(timezone.utc).strftime('%Y%m%d')}_"
            f"{secrets.token_hex(5)}"
        )

        request_id = current_request_id()

        async def persist() -> None:
            await self.persistence.insert_job(
                job_id,
                payload,
                upstream.authorization,
                owner_auth_header=caller.authorization,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                request_id=request_id,
            )

        try:
            result = await self.queue.persist_and_enqueue(job_id, persist)
        except sqlite3.IntegrityError:
            existing, expected = await self._find_idempotent(
                caller=caller,
                upstream=upstream,
                key=idempotency_key,
                payload=payload,
                payload_hash=payload_hash,
            )
            if existing is not None and hmac.compare_digest(
                str(existing["request_hash"]),
                expected,
            ):
                await self.persistence.ensure_queued_job_scheduled(existing)
                return await self.results.response_for_row(existing)
            raise JobServiceFailure(
                409,
                "idempotency key already used for a different image job",
            ) from None
        if result == "full":
            raise JobServiceFailure(503, "image job queue full")
        return {
            "job_id": job_id,
            "status": "queued",
            "request_type": payload["request_type"],
            "endpoint": payload["endpoint"],
            "relay_url": self.settings.upstream_base_url,
            "retention_days": payload["retention_days"],
        }

    async def fail_interrupted(self) -> None:
        await self.persistence.fail_interrupted_running_jobs()

    async def cancel(
        self,
        job_id: str,
        *,
        caller: CallerIdentity,
        upstream: UpstreamCredential | None = None,
    ) -> CancelResult:
        row = await self.repository.one(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        if row is None:
            raise JobServiceFailure(404, "image job not found")
        owner_candidates = [caller.owner_hash]
        if upstream is not None:
            owner_candidates.append(credential_hash(upstream.authorization))
        if not any(
            hmac.compare_digest(str(row["auth_hash"]), candidate)
            for candidate in owner_candidates
        ):
            raise JobServiceFailure(403, "image job belongs to a different key")

        status = str(row["status"])
        if status in persistence.TERMINAL_JOB_STATUSES:
            return CancelResult(
                job_id=job_id,
                outcome="already_terminal",
                status=status,
                outcome_uncertain=bool(
                    self.persistence.row_get(row, "outcome_uncertain")
                ),
            )

        if status == "queued":
            changed = await self.persistence.mark_cancelled(job_id)
            if changed:
                return CancelResult(
                    job_id=job_id,
                    outcome="cancelled_before_dispatch",
                    status="cancelled",
                )
        elif status == "running":
            execution_token = self.persistence.row_get(row, "execution_token")
            if isinstance(execution_token, str) and execution_token:
                changed = await self.persistence.mark_cancelled(
                    job_id,
                    execution_token=execution_token,
                    error=(
                        "cancellation requested after dispatch; "
                        "upstream outcome is uncertain"
                    ),
                )
                if changed:
                    return CancelResult(
                        job_id=job_id,
                        outcome="cancel_requested",
                        status="cancel_requested",
                        status_code=202,
                        outcome_uncertain=True,
                    )

        current = await self.repository.one(
            "SELECT status, outcome_uncertain FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        if current is not None and str(current["status"]) in (
            persistence.TERMINAL_JOB_STATUSES
        ):
            return CancelResult(
                job_id=job_id,
                outcome="already_terminal",
                status=str(current["status"]),
                outcome_uncertain=bool(current["outcome_uncertain"]),
            )
        return CancelResult(
            job_id=job_id,
            outcome="uncertain",
            status=str(current["status"]) if current is not None else status,
            status_code=409,
            outcome_uncertain=True,
        )

    async def _persist_failed_outcome(
        self,
        *,
        job_id: str,
        execution_token: str,
        endpoint: str,
        elapsed_ms: int,
        dispatch: UpstreamDispatchReceipt,
        exc: JobFailure,
        uncertain: bool,
        request_id: str,
    ) -> JobProcessOutcome:
        changed = await self.persistence.mark_failed(
            job_id,
            execution_token=execution_token,
            error=exc.error,
            upstream_status=exc.upstream_status,
            upstream_body=exc.upstream_body,
            elapsed_ms=elapsed_ms,
            error_class=exc.error_class,
            endpoint_used=endpoint,
            retryable=self.upstream.is_retryable_failure(exc),
            retry_suppressed=bool(exc.retry_suppressed or uncertain),
            outcome_uncertain=uncertain,
        )
        if not changed:
            outcome = JobProcessOutcome.SKIPPED_FENCE_LOST
        elif uncertain:
            self._record_outcome("jobs_uncertain_total", elapsed_ms)
            outcome = JobProcessOutcome.UNCERTAIN
            LOG.warning(
                "image job %s outcome uncertain transport_event=%s "
                "upstream_idempotency_key_hash=%s request_id=%s",
                job_id,
                dispatch.transport_event,
                dispatch.upstream_idempotency_key_hash,
                request_id,
            )
        else:
            self._record_outcome("jobs_failed_total", elapsed_ms)
            outcome = JobProcessOutcome.FAILED
        self._record_dispatch_outcome(dispatch, outcome)
        return outcome

    async def process(self, job_id: str) -> JobProcessOutcome:
        row = await self.repository.one(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        if row is None or row["status"] != "queued":
            return JobProcessOutcome.SKIPPED_NOT_QUEUED
        execution_token = await self.persistence.mark_running(job_id)
        if execution_token is None:
            return JobProcessOutcome.SKIPPED_FENCE_LOST
        started = time.monotonic()
        endpoint = str(row["endpoint"])
        # H-19：worker 是在提交请求结束之后的另一个协程里跑的，ContextVar 已经
        # 失效，只能从落库的那一列把提交侧的 request_id 捞回来接上日志链。
        request_id = row_request_id(row)
        dispatch = UpstreamDispatchReceipt()
        try:
            fresh = await self.repository.one(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            )
            if fresh is None:
                return JobProcessOutcome.SKIPPED_FENCE_LOST
            if (
                fresh["status"] != "running"
                or self.persistence.row_get(fresh, "execution_token")
                != execution_token
            ):
                return JobProcessOutcome.SKIPPED_FENCE_LOST
            try:
                authorization = self.credential_vault.decrypt_job_row(fresh)
            except CredentialVaultError as exc:
                raise JobFailure(
                    "stored Authorization credential is unavailable",
                    error_class=ERROR_CLASS_INTERNAL,
                ) from exc
            status, images = await self.upstream.call(
                fresh,
                authorization=authorization,
                dispatch=dispatch,
            )
            if not dispatch.started:
                raise JobFailure(
                    "upstream returned without a transport dispatch receipt",
                    retry_requires_idempotency=True,
                    outcome_uncertain=True,
                    error_class=ERROR_CLASS_INTERNAL,
                )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            changed = await self.persistence.mark_succeeded(
                job_id,
                execution_token=execution_token,
                upstream_status=status,
                elapsed_ms=elapsed_ms,
                images=images,
                endpoint_used=endpoint,
            )
            if changed:
                self._record_outcome(
                    "jobs_succeeded_total",
                    elapsed_ms,
                    images=len(images),
                )
                outcome = JobProcessOutcome.SUCCEEDED
            else:
                outcome = JobProcessOutcome.SKIPPED_FENCE_LOST
            self._record_dispatch_outcome(dispatch, outcome)
            return outcome
        except asyncio.CancelledError:
            raise
        except ArtifactCorrupt as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            changed = await self.persistence.mark_artifact_corrupt(
                job_id,
                execution_token=execution_token,
                reason=exc.reason,
                elapsed_ms=elapsed_ms,
                endpoint_used=endpoint,
            )
            if changed:
                self._record_outcome("jobs_uncertain_total", elapsed_ms)
                outcome = JobProcessOutcome.UNCERTAIN
            else:
                outcome = JobProcessOutcome.SKIPPED_FENCE_LOST
            self._record_dispatch_outcome(dispatch, outcome)
            return outcome
        except PreDispatchFailure as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return await self._persist_failed_outcome(
                job_id=job_id,
                execution_token=execution_token,
                endpoint=endpoint,
                elapsed_ms=elapsed_ms,
                dispatch=dispatch,
                exc=exc,
                uncertain=False,
                request_id=request_id,
            )
        except JobFailure as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return await self._persist_failed_outcome(
                job_id=job_id,
                execution_token=execution_token,
                endpoint=endpoint,
                elapsed_ms=elapsed_ms,
                dispatch=dispatch,
                exc=exc,
                uncertain=bool(
                    exc.outcome_uncertain
                    or (dispatch.started and not exc.cost_proven_absent)
                ),
                request_id=request_id,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            failure = JobFailure(
                error=f"image job worker error: {exc.__class__.__name__}: {exc}",
                error_class=ERROR_CLASS_INTERNAL,
            )
            LOG.exception(
                "image job %s crashed request_id=%s",
                job_id,
                request_id,
            )
            return await self._persist_failed_outcome(
                job_id=job_id,
                execution_token=execution_token,
                endpoint=endpoint,
                elapsed_ms=elapsed_ms,
                dispatch=dispatch,
                exc=failure,
                uncertain=dispatch.started,
                request_id=request_id,
            )

    async def reconcile(self) -> None:
        await self.stale_jobs.run_pass()

        rows = await self.repository.all(
            """
            SELECT job_id
            FROM jobs
            WHERE status = 'queued'
              AND auth_ciphertext IS NOT NULL
              AND auth_nonce IS NOT NULL
              AND auth_key_id IS NOT NULL
            ORDER BY created_at
            """
        )
        for row in rows:
            if await self.queue.enqueue(str(row["job_id"])) == "full":
                break
