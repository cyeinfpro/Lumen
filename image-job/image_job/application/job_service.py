"""Image job application service."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import job_persistence
import payload_helpers
import request_bodies

from ..config import ImageJobSettings
from ..contracts import (
    ALLOWED_FIXED_ENDPOINTS,
    ALLOWED_PREFIX_ENDPOINTS,
    DEFAULT_IMAGE_OUTPUT_COMPRESSION,
    DEFAULT_IMAGE_OUTPUT_FORMAT,
    ERROR_CLASS_INTERNAL,
    ERROR_CLASS_NETWORK,
    IMAGE_OUTPUT_FORMATS,
    JobFailure,
)
from ..domain.identity import CallerIdentity, UpstreamCredential
from ..payloads import json_dump, request_hash
from .auth import credential_hash
from .result_service import ResultService


LOG = logging.getLogger("image-job.jobs")


class JobServiceFailure(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobService:
    def __init__(
        self,
        settings: ImageJobSettings,
        repository: Any,
        upstream: Any,
        queue: Any,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.upstream = upstream
        self.queue = queue
        self.persistence = job_persistence.JobPersistenceFacade(
            db_exec=lambda sql, params=(): repository.execute(sql, params),
            enqueue_job=queue.enqueue,
            now_iso=_utc_iso,
            auth_hash=credential_hash,
            json_dump=json_dump,
            upstream_base_url=lambda: settings.upstream_base_url,
            upstream_idempotency_guaranteed=(
                lambda: settings.upstream_idempotency_guaranteed
            ),
            error_class_internal=lambda: ERROR_CLASS_INTERNAL,
            error_class_network=lambda: ERROR_CLASS_NETWORK,
            log=LOG,
        )
        self.results = ResultService(repository, self.persistence)

    def parse_payload(self, raw: bytes) -> tuple[Any, dict[str, Any]]:
        limits = request_bodies.JsonShapeLimits(
            max_depth=self.settings.max_json_depth,
            max_array_items=self.settings.max_json_array_items,
            max_object_items=self.settings.max_json_object_items,
            max_total_values=self.settings.max_json_total_values,
            max_key_chars=self.settings.max_json_key_chars,
            max_string_chars=self.settings.max_json_string_chars,
        )
        raw_payload = request_bodies.load_json_bytes(raw, limits)
        policy = payload_helpers.PayloadPolicy(
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
        return raw_payload, payload_helpers.validate_payload(raw_payload, policy)

    def idempotency_key(
        self,
        headers: Any,
        raw_payload: Any,
    ) -> str | None:
        raw = str(headers.get("idempotency-key", "") or "").strip()
        if not raw and isinstance(raw_payload, dict):
            candidate = raw_payload.get("idempotency_key")
            raw = candidate.strip() if isinstance(candidate, str) else ""
        if not raw:
            return None
        if len(raw.encode()) > self.settings.max_idempotency_key_bytes:
            raise JobServiceFailure(
                400,
                "idempotency key exceeds "
                f"{self.settings.max_idempotency_key_bytes} bytes",
            )
        return hashlib.sha256(raw.encode()).hexdigest()

    def _request_hash(
        self,
        payload: dict[str, Any],
        caller: CallerIdentity,
        upstream: UpstreamCredential,
    ) -> str:
        if caller.legacy:
            return request_hash(payload)
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
        if caller.legacy or hmac.compare_digest(caller.owner_hash, upstream_hash):
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
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        payload_hash = self._request_hash(payload, caller, upstream)
        if idempotency_key is not None:
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
                return self.persistence.row_to_response(existing)

        job_id = (
            f"img_{datetime.now(timezone.utc).strftime('%Y%m%d')}_"
            f"{secrets.token_hex(5)}"
        )

        async def persist() -> None:
            await self.persistence.insert_job(
                job_id,
                payload,
                upstream.authorization,
                owner_auth_header=caller.authorization,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )

        try:
            result = await self.queue.persist_and_enqueue(job_id, persist)
        except sqlite3.IntegrityError:
            if idempotency_key is None:
                raise
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
                return self.persistence.row_to_response(existing)
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

    async def process(self, job_id: str) -> None:
        row = await self.repository.one(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        if row is None or row["status"] != "queued":
            return
        if not await self.persistence.mark_running(job_id):
            return
        started = time.monotonic()
        endpoint = str(row["endpoint"])
        try:
            fresh = await self.repository.one(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            )
            if fresh is None:
                return
            status, images = await self.upstream.call(fresh)
            await self.persistence.mark_succeeded(
                job_id,
                upstream_status=status,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                images=images,
                endpoint_used=endpoint,
            )
        except asyncio.CancelledError:
            raise
        except JobFailure as exc:
            await self.persistence.mark_failed(
                job_id,
                error=exc.error,
                upstream_status=exc.upstream_status,
                upstream_body=exc.upstream_body,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_class=exc.error_class,
                endpoint_used=endpoint,
                retryable=self.upstream.is_retryable_failure(exc),
                retry_suppressed=exc.retry_suppressed,
                outcome_uncertain=exc.outcome_uncertain,
            )
        except Exception as exc:
            await self.persistence.mark_failed(
                job_id,
                error=f"image job worker error: {exc.__class__.__name__}: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_class=ERROR_CLASS_INTERNAL,
                endpoint_used=endpoint,
            )
            LOG.exception("image job %s crashed", job_id)

    async def reconcile(self) -> None:
        rows = await self.repository.all(
            "SELECT job_id FROM jobs WHERE status = 'queued' ORDER BY created_at"
        )
        for row in rows:
            if await self.queue.enqueue(str(row["job_id"])) == "full":
                break
