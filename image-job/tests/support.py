"""Object-scoped fixtures for legacy image-job regression coverage."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import socket
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import job_persistence
import payload_helpers
import request_bodies
from fastapi import HTTPException, Request
from PIL import Image
from image_job.application.auth import credential_hash
from image_job.config import ImageJobSettings
from image_job.adapters.sqlite_jobs import SQLiteJobRepository
from image_job.contracts import (
    ALLOWED_FIXED_ENDPOINTS,
    ALLOWED_PREFIX_ENDPOINTS,
    DEFAULT_IMAGE_OUTPUT_COMPRESSION,
    DEFAULT_IMAGE_OUTPUT_FORMAT,
    ERROR_CLASS_INTERNAL,
    ERROR_CLASS_NETWORK,
    IMAGE_OUTPUT_FORMATS,
    JobFailure,
)
from image_job.payloads import json_dump, request_hash
from image_job.processing import ImageProcessing
from image_candidates import ImageCandidate
from job_persistence import ReferencePersistenceFacade, RetentionFacade


class ImageJobHarness:
    """A test object that composes public contracts without module globals."""

    _SETTING_FIELDS = {
        "MAX_IMAGE_PIXELS": "max_image_pixels",
        "MAX_IMAGE_BYTES": "max_image_bytes",
        "MAX_IMAGE_CANDIDATES": "max_image_candidates",
        "MAX_TOTAL_IMAGE_BYTES": "max_total_image_bytes",
        "MAX_IMAGE_URL_REDIRECTS": "max_image_url_redirects",
        "MAX_UPSTREAM_RESPONSE_BYTES": "max_upstream_response_bytes",
        "MAX_UPSTREAM_ERROR_BODY_BYTES": "max_upstream_error_body_bytes",
        "RESPONSES_STREAM_MAX_BYTES": "responses_stream_max_bytes",
        "RESPONSES_STREAM_IDLE_TIMEOUT_S": "responses_stream_idle_timeout_s",
        "RESPONSES_STRIP_PARTIAL_IMAGES": "responses_strip_partial_images",
        "JOB_HEARTBEAT_INTERVAL_S": "job_heartbeat_interval_s",
        "RETRY_NETWORK_MAX": "retry_network_max",
        "RETRY_RESPONSES_STREAM_MAX": "retry_responses_stream_max",
        "RETRY_UPSTREAM_5XX_MAX": "retry_upstream_5xx_max",
        "RETRY_BACKOFF_S": "retry_backoff_s",
        "UPSTREAM_IDEMPOTENCY_GUARANTEED": "upstream_idempotency_guaranteed",
        "MAX_IMAGE_JOB_REQUEST_BYTES": "max_request_bytes",
        "MAX_JSON_DEPTH": "max_json_depth",
        "MAX_JSON_ARRAY_ITEMS": "max_json_array_items",
        "MAX_JSON_OBJECT_ITEMS": "max_json_object_items",
        "MAX_JSON_TOTAL_VALUES": "max_json_total_values",
        "MAX_JSON_KEY_CHARS": "max_json_key_chars",
        "MAX_JSON_STRING_CHARS": "max_json_string_chars",
        "MAX_RETENTION_DAYS": "max_retention_days",
        "JOB_TTL_DAYS": "job_ttl_days",
        "CONCURRENCY": "concurrency",
        "GRACEFUL_SHUTDOWN_S": "graceful_shutdown_s",
        "SIDECAR_TOKEN": "sidecar_token",
        "ALLOW_LEGACY_BEARER_AUTH": "allow_legacy_bearer",
        "DB_PATH": "db_path",
        "DATA_DIR": "data_dir",
        "REFS_DIR": "refs_dir",
    }

    def __init__(self) -> None:
        object.__setattr__(self, "settings", ImageJobSettings.from_env())
        object.__setattr__(self, "http_client", None)
        object.__setattr__(self, "touch_running", self._noop)
        object.__setattr__(self, "log", logging.getLogger("image-job.test"))
        repository = SQLiteJobRepository(self.settings)
        object.__setattr__(self, "repository", repository)
        processing = ImageProcessing(
            self.settings,
            http_client=lambda: self.http_client,
            touch_running=lambda job_id: self.touch_running(job_id),
            logger=self.log,
        )
        object.__setattr__(self, "processing", processing)
        object.__setattr__(
            self,
            "_persistence",
            job_persistence.JobPersistenceFacade(
                db_exec=lambda sql, params=(): self.db_exec(sql, params),
                enqueue_job=lambda job_id: self.enqueue_job(job_id),
                now_iso=lambda: self.iso(),
                auth_hash=credential_hash,
                json_dump=json_dump,
                upstream_base_url=lambda: self.settings.upstream_base_url,
                upstream_idempotency_guaranteed=(
                    lambda: self.settings.upstream_idempotency_guaranteed
                ),
                error_class_internal=lambda: ERROR_CLASS_INTERNAL,
                error_class_network=lambda: ERROR_CLASS_NETWORK,
                log=self.log,
            ),
        )
        object.__setattr__(self, "_job_persistence_module", job_persistence)
        object.__setattr__(self, "_queued_ids", set())
        object.__setattr__(self, "_inflight", set())
        object.__setattr__(
            self,
            "_reference_persistence",
            ReferencePersistenceFacade(
                db_one_sync=lambda sql, params: self._db_one_sync(sql, params),
                db_exec_sync=lambda sql, params: self._db_exec_sync(sql, params),
                refs_dir=lambda: self.settings.refs_dir,
                now_iso=lambda: self.iso(),
            ),
        )
        object.__setattr__(
            self,
            "_retention",
            RetentionFacade(
                data_dir=lambda: self.settings.data_dir,
                refs_dir=lambda: self.settings.refs_dir,
                db_exec_sync=lambda sql, params: self._db_exec_sync(sql, params),
                db_exec=lambda sql, params=(): self.db_exec(sql, params),
                db_all=lambda sql, params=(): self.db_all(sql, params),
                utc_now=lambda: self.utc_now(),
                max_retention_days=lambda: self.settings.max_retention_days,
                job_ttl_days=lambda: self.settings.job_ttl_days,
                log=self.log,
            ),
        )
        Image.MAX_IMAGE_PIXELS = self.settings.max_image_pixels

    async def _noop(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._SETTING_FIELDS:
            field_name = self._SETTING_FIELDS[name]
            if field_name == "graceful_shutdown_s":
                value = float(value)
                self.settings = replace(
                    self.settings,
                    timeouts=replace(
                        self.settings.timeouts,
                        graceful_shutdown_s=value,
                    ),
                )
            elif field_name == "sidecar_token":
                from image_job.config import SecretText

                self.settings = replace(
                    self.settings,
                    sidecar_token=SecretText(str(value)),
                )
            else:
                self.settings = replace(self.settings, **{field_name: value})
            self.repository.settings = self.settings
            self.processing.settings = self.settings
            return
        if name in {
            "_http_client",
            "_call_upstream_once",
            "_new_pinned_image_download_client",
            "resolve_public_image_download_target",
            "touch_running",
            "download_image_url",
            "extract_candidates",
            "extract_response_images",
            "extract_responses_stream_images",
            "save_images",
            "call_upstream_once",
            "materialize_edit_input_files",
            "materialize_edit_input_urls",
        }:
            if name == "_http_client":
                object.__setattr__(self, "http_client", value)
                return
            if name == "_new_pinned_image_download_client":
                setattr(self.processing, "new_pinned_download_client", value)
                return
            if name == "resolve_public_image_download_target":
                object.__setattr__(
                    self.processing.candidate_facade,
                    "resolve_public_image_download_target",
                    value,
                )
                return
            target_name = (
                "call_upstream_once" if name == "_call_upstream_once" else name
            )
            setattr(self.processing, target_name, value)
            if name == "touch_running":
                object.__setattr__(self, name, value)
            return
        object.__setattr__(self, name, value)

    def __getattr__(self, name: str) -> Any:
        if name in self._SETTING_FIELDS:
            field_name = self._SETTING_FIELDS[name]
            if field_name == "graceful_shutdown_s":
                return self.settings.timeouts.graceful_shutdown_s
            if field_name == "sidecar_token":
                return self.settings.sidecar_token.get_secret_value()
            return getattr(self.settings, field_name)
        if name == "require_auth":
            return payload_helpers.require_auth
        if name == "validate_sidecar_auth_config":
            return payload_helpers.validate_sidecar_auth_config
        if name == "_read_request_body_bounded":
            return request_bodies.read_request_body_bounded
        if name == "_try_parse_sse_data":
            return self.processing.try_parse_sse_data
        if name == "_is_responses_success_terminal":
            return self.processing.is_responses_success_terminal
        if name == "_is_responses_error_terminal":
            return self.processing.is_responses_error_terminal
        if name == "_first_stream_error":
            return self.processing.first_stream_error
        if name == "_new_pinned_image_download_client":
            return self.processing.new_pinned_download_client
        if name == "_extract_non_stream_response_images":
            return self.processing.upstream_facade.extract_non_stream_response_images
        if name == "_raise_upstream_http_error":
            return self.processing.upstream_facade.raise_upstream_http_error
        if name == "_run_retention_pass":
            return self._retention.run_pass
        if name == "_write_ref_sync":
            return self._reference_persistence.write_ref
        if name == "_existing_ref_sync":
            return self._reference_persistence.existing_ref
        if name == "Image":
            return Image
        if name == "socket":
            return socket
        if name == "pinned_async_http_transport":
            from image_url_security import pinned_async_http_transport

            return pinned_async_http_transport
        if name == "PublicImageDownloadTarget":
            from image_url_security import PublicImageDownloadTarget

            return PublicImageDownloadTarget
        if name == "request_hash":
            return request_hash
        if name == "_db_all_sync":
            return self._db_all_sync
        if name == "_db_exec_sync":
            return self._db_exec_sync
        if name == "_db_one_sync":
            return self._db_one_sync
        if name == "_payload_helpers":
            return payload_helpers
        if name.startswith("ERROR_CLASS_"):
            from image_job import contracts

            return getattr(contracts, name)
        if name == "ImageCandidate":
            return ImageCandidate
        if name == "ImageDownloadResolutionError":
            from image_url_security import ImageDownloadResolutionError

            return ImageDownloadResolutionError
        if name == "JobFailure":
            return JobFailure
        if name == "_call_upstream_once":
            return self.processing.call_upstream_once
        if name == "_candidate_filename":
            return self.processing.candidate_filename
        if name == "_upstream":
            return self.processing.upstream_facade
        if name == "_image_candidates_module":
            import image_candidates

            return image_candidates
        if name == "_image_artifacts_module":
            import image_artifacts

            return image_artifacts
        if hasattr(self.processing, name):
            return getattr(self.processing, name)
        if hasattr(self._persistence, name):
            return getattr(self._persistence, name)
        raise AttributeError(name)

    def __dir__(self) -> list[str]:
        return sorted(set(self.__dict__) | set(dir(self.processing)))

    def property(self, name: str) -> Any:
        return getattr(self, name)

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def iso(self, dt: datetime | None = None) -> str:
        return (dt or self.utc_now()).isoformat()

    def _open_conn(self):
        return self.repository._open()

    def init_storage_sync(self) -> None:
        job_persistence.init_storage(
            data_dir=self.settings.data_dir,
            refs_dir=self.settings.refs_dir,
            db_path=self.settings.db_path,
            open_conn=self._open_conn,
            auth_hash=credential_hash,
        )

    def _db_one_sync(self, sql: str, params: tuple[Any, ...] = ()):
        return self.repository._one_sync(sql, params)

    def _db_all_sync(self, sql: str, params: tuple[Any, ...] = ()):
        return self.repository._all_sync(sql, params)

    def _db_exec_sync(self, sql: str, params: tuple[Any, ...] = ()):
        return self.repository._execute_sync(sql, params)

    async def db_one(self, sql: str, params: tuple[Any, ...] = ()):
        return await self.repository.one(sql, params)

    async def db_all(self, sql: str, params: tuple[Any, ...] = ()):
        return await self.repository.all(sql, params)

    async def db_exec(self, sql: str, params: tuple[Any, ...] = ()):
        return await self.repository.execute(sql, params)

    async def enqueue_job(self, job_id: str) -> str:
        self._queued_ids.add(job_id)
        return "enqueued"

    def request_idempotency_key(self, request: Request, raw_payload: Any):
        return payload_helpers.request_idempotency_key(
            request,
            raw_payload,
            max_bytes=self.settings.max_idempotency_key_bytes,
        )

    def load_image_job_json(self, data: bytes) -> Any:
        limits = request_bodies.JsonShapeLimits(
            max_depth=self.settings.max_json_depth,
            max_array_items=self.settings.max_json_array_items,
            max_object_items=self.settings.max_json_object_items,
            max_total_values=self.settings.max_json_total_values,
            max_key_chars=self.settings.max_json_key_chars,
            max_string_chars=self.settings.max_json_string_chars,
        )
        return request_bodies.load_json_bytes(data, limits)

    def validate_json_shape(self, value: Any) -> None:
        limits = request_bodies.JsonShapeLimits(
            max_depth=self.settings.max_json_depth,
            max_array_items=self.settings.max_json_array_items,
            max_object_items=self.settings.max_json_object_items,
            max_total_values=self.settings.max_json_total_values,
            max_key_chars=self.settings.max_json_key_chars,
            max_string_chars=self.settings.max_json_string_chars,
        )
        request_bodies.validate_json_shape(value, limits)

    def validate_payload(self, payload: Any) -> dict[str, Any]:
        self.validate_json_shape(payload)
        policy = payload_helpers.PayloadPolicy(
            allowed_fixed_endpoints=ALLOWED_FIXED_ENDPOINTS,
            allowed_prefix_endpoints=ALLOWED_PREFIX_ENDPOINTS,
            image_output_formats=frozenset(IMAGE_OUTPUT_FORMATS),
            default_image_output_format=DEFAULT_IMAGE_OUTPUT_FORMAT,
            default_image_output_compression=DEFAULT_IMAGE_OUTPUT_COMPRESSION,
            responses_strip_partial_images=self.settings.responses_strip_partial_images,
            max_endpoint_chars=self.settings.max_endpoint_chars,
            max_request_type_chars=self.settings.max_request_type_chars,
            default_retention_days=self.settings.default_retention_days,
            max_retention_days=self.settings.max_retention_days,
        )
        return payload_helpers.validate_payload(payload, policy)

    def authenticate_caller(self, request: Request) -> tuple[str, bool]:
        return payload_helpers.require_sidecar_auth(
            request,
            expected_token=self.settings.sidecar_token.get_secret_value(),
            allow_legacy=self.settings.allow_legacy_bearer,
        )

    def scoped_request_hash(
        self,
        payload: dict[str, Any],
        upstream_auth_header: str,
        *,
        legacy_auth: bool,
    ) -> str:
        if legacy_auth:
            return request_hash(payload)
        return request_hash(
            {
                "payload": payload,
                "upstream_auth_hash": credential_hash(upstream_auth_header),
            }
        )

    async def process_job(self, job_id: str) -> None:
        row = await self.db_one(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        if row is None or not await self.mark_running(job_id):
            return
        try:
            fresh = await self.db_one(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            )
            status, images = await self.call_upstream(fresh)
            await self.mark_succeeded(
                job_id,
                upstream_status=status,
                elapsed_ms=0,
                images=images,
                endpoint_used=fresh["endpoint"],
            )
        except JobFailure as exc:
            await self.mark_failed(
                job_id,
                error=exc.error,
                upstream_status=exc.upstream_status,
                upstream_body=exc.upstream_body,
                elapsed_ms=0,
                error_class=exc.error_class,
                endpoint_used=row["endpoint"],
                retryable=self.processing.upstream_facade.is_retryable_job_failure(exc),
                retry_suppressed=exc.retry_suppressed,
                outcome_uncertain=exc.outcome_uncertain,
            )

    async def create_image_job(self, request: Request) -> dict[str, Any]:
        owner, legacy_auth = self.authenticate_caller(request)
        upstream = payload_helpers.require_upstream_auth(
            request,
            caller_auth_header=owner,
            legacy_auth=legacy_auth,
        )
        raw = await request_bodies.read_request_body_bounded(
            request,
            max_bytes=self.settings.max_request_bytes,
        )
        raw_payload = self.load_image_job_json(raw)
        payload = self.validate_payload(raw_payload)
        key = self.request_idempotency_key(request, raw_payload)
        request_hash_value = self.scoped_request_hash(
            payload,
            upstream,
            legacy_auth=legacy_auth,
        )
        queue = self.__dict__.get("_queue")
        if queue is not None and queue.full():
            raise HTTPException(status_code=503, detail="image job queue full")
        if key is not None:
            existing = await self.db_one(
                """
                SELECT * FROM jobs
                WHERE auth_hash = ? AND upstream_auth_hash = ?
                  AND idempotency_key = ?
                """,
                (
                    credential_hash(owner),
                    credential_hash(upstream),
                    key,
                ),
            )
            if existing is not None:
                if not hmac.compare_digest(
                    str(existing["request_hash"]),
                    request_hash_value,
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="idempotency key already used for a different image job",
                    )
                await self.enqueue_job(existing["job_id"])
                return self.row_to_response(existing)
        job_id = f"img_test_{secrets.token_hex(5)}"
        try:
            await self._persistence.insert_job(
                job_id,
                payload,
                upstream,
                owner_auth_header=owner,
                idempotency_key=key,
                payload_hash=request_hash_value,
            )
        except sqlite3.IntegrityError:
            existing = await self.db_one(
                """
                SELECT * FROM jobs
                WHERE auth_hash = ? AND upstream_auth_hash = ?
                  AND idempotency_key = ?
                """,
                (
                    credential_hash(owner),
                    credential_hash(upstream),
                    key,
                ),
            )
            if existing is None:
                raise
            if not hmac.compare_digest(
                str(existing["request_hash"]),
                request_hash_value,
            ):
                raise HTTPException(
                    status_code=409,
                    detail="idempotency key already used for a different image job",
                )
            return self.row_to_response(existing)
        await self.enqueue_job(job_id)
        row = await self.db_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        return self.row_to_response(row)

    async def get_image_job(self, job_id: str, request: Request) -> dict[str, Any]:
        owner, legacy_auth = self.authenticate_caller(request)
        row = await self.db_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="image job not found")
        candidate_hashes = [credential_hash(owner)]
        if not legacy_auth:
            upstream = payload_helpers.optional_upstream_auth(request)
            if upstream is not None:
                candidate_hashes.append(credential_hash(upstream))
        if not any(
            hmac.compare_digest(str(row["auth_hash"]), candidate)
            for candidate in candidate_hashes
        ):
            raise HTTPException(
                status_code=403,
                detail="image job belongs to a different key",
            )
        return self.row_to_response(row)

    async def upload_reference(self, request: Request) -> dict[str, Any]:
        owner, _legacy_auth = self.authenticate_caller(request)
        raw = await request_bodies.read_request_body_bounded(
            request,
            max_bytes=self.settings.max_ref_bytes,
        )
        if not raw:
            raise HTTPException(status_code=400, detail="empty body")
        width, height, fmt = await asyncio.to_thread(
            self.processing.image_metadata,
            raw,
            request.headers.get("content-type"),
        )
        if width is None or height is None or fmt not in {"png", "jpeg", "webp"}:
            raise HTTPException(status_code=400, detail="invalid reference image")
        ext = "jpg" if fmt == "jpeg" else fmt
        digest = hashlib.sha256(raw).hexdigest()
        auth_digest = credential_hash(owner)
        existing = await asyncio.to_thread(
            self._reference_persistence.existing_ref,
            auth_digest,
            digest,
        )
        if existing is not None:
            token, existing_ext = existing
            return {
                "url": f"{self.settings.public_base_url}/refs/{token}.{existing_ext}",
                "sha256": digest,
                "size": len(raw),
                "deduped": True,
            }
        token = secrets.token_urlsafe(24)
        await asyncio.to_thread(
            self._reference_persistence.write_ref,
            auth_digest,
            digest,
            token,
            ext,
            raw,
        )
        return {
            "url": f"{self.settings.public_base_url}/refs/{token}.{ext}",
            "sha256": digest,
            "size": len(raw),
            "deduped": False,
        }


def load_harness() -> ImageJobHarness:
    return ImageJobHarness()
