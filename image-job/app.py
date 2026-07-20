from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import importlib.util
import logging
import os
import secrets
import socket as socket
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from PIL import Image


_LOCAL_MODULE_DIR = Path(__file__).resolve().parent
_LOCAL_MODULE_NAMESPACE = (
    "_lumen_image_job_"
    + hashlib.sha256(str(_LOCAL_MODULE_DIR).encode("utf-8")).hexdigest()[:12]
    + "_"
    + secrets.token_hex(6)
)


def _load_local_module(name: str) -> ModuleType:
    path = _LOCAL_MODULE_DIR / f"{name}.py"
    module_name = f"{_LOCAL_MODULE_NAMESPACE}_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load image-job module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


_runtime_config = _load_local_module("runtime_config")
_payload_helpers = _load_local_module("payload_helpers")
_request_bodies_module = _load_local_module("request_bodies")
_image_url_security_module = _load_local_module("image_url_security")
_job_persistence_module = _load_local_module("job_persistence")
_image_artifacts_module = _load_local_module("image_artifacts")
_image_candidates_module = _load_local_module("image_candidates")
_upstream_runtime_module = _load_local_module("upstream_runtime")

ImageArtifactFacade = _image_artifacts_module.ImageArtifactFacade
ImageCandidate = _image_candidates_module.ImageCandidate
ImageCandidateFacade = _image_candidates_module.ImageCandidateFacade
PublicImageDownloadTarget = _image_url_security_module.PublicImageDownloadTarget
ImageDownloadResolutionError = _image_url_security_module.ImageDownloadResolutionError
pinned_async_http_transport = _image_url_security_module.pinned_async_http_transport
resolve_public_image_download_target = (
    _image_url_security_module.resolve_public_image_download_target
)

_ALLOWED_SQLITE_JOURNAL_MODES = _job_persistence_module.ALLOWED_SQLITE_JOURNAL_MODES
JobPersistenceFacade = _job_persistence_module.JobPersistenceFacade
ReferencePersistenceFacade = _job_persistence_module.ReferencePersistenceFacade
RetentionFacade = _job_persistence_module.RetentionFacade
_persistence_db_all_sync = _job_persistence_module.db_all_sync
_persistence_db_exec_sync = _job_persistence_module.db_exec_sync
_persistence_db_one_sync = _job_persistence_module.db_one_sync
_persistence_init_storage = _job_persistence_module.init_storage
_persistence_open_connection = _job_persistence_module.open_connection
sqlite_tuning_pragmas = _job_persistence_module.sqlite_tuning_pragmas

PayloadPolicy = _payload_helpers.PayloadPolicy
JsonShapeLimits = _request_bodies_module.JsonShapeLimits
_load_json_bytes = _request_bodies_module.load_json_bytes
_download_content_length = _request_bodies_module.parse_content_length
parse_json_bytes = _request_bodies_module.parse_json_bytes
_read_request_body_bounded = _request_bodies_module.read_request_body_bounded
_read_download_body_bounded = _request_bodies_module.read_download_body_bounded
_read_response_body_bounded = _request_bodies_module.read_response_body_bounded
_SseLineDecoder = _request_bodies_module.SseLineDecoder
_validate_json_shape = _request_bodies_module.validate_json_shape
UpstreamFacade = _upstream_runtime_module.UpstreamFacade


ALLOWED_FIXED_ENDPOINTS = _runtime_config.ALLOWED_FIXED_ENDPOINTS
ALLOWED_PREFIX_ENDPOINTS = _runtime_config.ALLOWED_PREFIX_ENDPOINTS
CONCURRENCY = _runtime_config.CONCURRENCY
DATA_DIR = _runtime_config.DATA_DIR
DB_PATH = _runtime_config.DB_PATH
DEFAULT_IMAGE_OUTPUT_COMPRESSION = _runtime_config.DEFAULT_IMAGE_OUTPUT_COMPRESSION
DEFAULT_IMAGE_OUTPUT_FORMAT = _runtime_config.DEFAULT_IMAGE_OUTPUT_FORMAT
DEFAULT_RETENTION_DAYS = _runtime_config.DEFAULT_RETENTION_DAYS
GRACEFUL_SHUTDOWN_S = _runtime_config.GRACEFUL_SHUTDOWN_S
HTTP_POOL_KEEPALIVE = _runtime_config.HTTP_POOL_KEEPALIVE
HTTP_POOL_MAX = _runtime_config.HTTP_POOL_MAX
IMAGE_OUTPUT_FORMATS = _runtime_config.IMAGE_OUTPUT_FORMATS
JOB_HEARTBEAT_INTERVAL_S = _runtime_config.JOB_HEARTBEAT_INTERVAL_S
JOB_TTL_DAYS = _runtime_config.JOB_TTL_DAYS
MAX_ENDPOINT_CHARS = _runtime_config.MAX_ENDPOINT_CHARS
MAX_IDEMPOTENCY_KEY_BYTES = _runtime_config.MAX_IDEMPOTENCY_KEY_BYTES
MAX_IMAGE_BYTES = _runtime_config.MAX_IMAGE_BYTES
MAX_IMAGE_CANDIDATES = _runtime_config.MAX_IMAGE_CANDIDATES
MAX_IMAGE_JOB_REQUEST_BYTES = _runtime_config.MAX_IMAGE_JOB_REQUEST_BYTES
MAX_IMAGE_PIXELS = _runtime_config.MAX_IMAGE_PIXELS
MAX_IMAGE_URL_REDIRECTS = _runtime_config.MAX_IMAGE_URL_REDIRECTS
MAX_JSON_ARRAY_ITEMS = _runtime_config.MAX_JSON_ARRAY_ITEMS
MAX_JSON_DEPTH = _runtime_config.MAX_JSON_DEPTH
MAX_JSON_KEY_CHARS = _runtime_config.MAX_JSON_KEY_CHARS
MAX_JSON_OBJECT_ITEMS = _runtime_config.MAX_JSON_OBJECT_ITEMS
MAX_JSON_STRING_CHARS = _runtime_config.MAX_JSON_STRING_CHARS
MAX_JSON_TOTAL_VALUES = _runtime_config.MAX_JSON_TOTAL_VALUES
MAX_REF_BYTES = _runtime_config.MAX_REF_BYTES
MAX_REQUEST_TYPE_CHARS = _runtime_config.MAX_REQUEST_TYPE_CHARS
MAX_RETENTION_DAYS = _runtime_config.MAX_RETENTION_DAYS
MAX_TOTAL_IMAGE_BYTES = _runtime_config.MAX_TOTAL_IMAGE_BYTES
MAX_UPSTREAM_ERROR_BODY_BYTES = _runtime_config.MAX_UPSTREAM_ERROR_BODY_BYTES
MAX_UPSTREAM_RESPONSE_BYTES = _runtime_config.MAX_UPSTREAM_RESPONSE_BYTES
PUBLIC_BASE_URL = _runtime_config.PUBLIC_BASE_URL
QUEUE_MAX = _runtime_config.QUEUE_MAX
REFS_DIR = _runtime_config.REFS_DIR
RESPONSES_STREAM_IDLE_TIMEOUT_S = _runtime_config.RESPONSES_STREAM_IDLE_TIMEOUT_S
RESPONSES_STREAM_MAX_BYTES = _runtime_config.RESPONSES_STREAM_MAX_BYTES
RESPONSES_STRIP_PARTIAL_IMAGES = _runtime_config.RESPONSES_STRIP_PARTIAL_IMAGES
RETENTION_SWEEP_INTERVAL_S = _runtime_config.RETENTION_SWEEP_INTERVAL_S
RETRY_BACKOFF_S = _runtime_config.RETRY_BACKOFF_S
RETRY_NETWORK_MAX = _runtime_config.RETRY_NETWORK_MAX
RETRY_RESPONSES_STREAM_MAX = _runtime_config.RETRY_RESPONSES_STREAM_MAX
RETRY_UPSTREAM_5XX_MAX = _runtime_config.RETRY_UPSTREAM_5XX_MAX
ROOT_DIR = _runtime_config.ROOT_DIR
SQLITE_JOURNAL_MODE = _runtime_config.SQLITE_JOURNAL_MODE
STATE_DIR = _runtime_config.STATE_DIR
STUCK_QUEUED_AFTER_S = _runtime_config.STUCK_QUEUED_AFTER_S
STUCK_RECONCILE_BATCH = _runtime_config.STUCK_RECONCILE_BATCH
STUCK_RECONCILE_INTERVAL_S = _runtime_config.STUCK_RECONCILE_INTERVAL_S
STUCK_RUNNING_AFTER_S = _runtime_config.STUCK_RUNNING_AFTER_S
UPSTREAM_BASE_URL = _runtime_config.UPSTREAM_BASE_URL
UPSTREAM_CONNECT_TIMEOUT_S = _runtime_config.UPSTREAM_CONNECT_TIMEOUT_S
UPSTREAM_IDEMPOTENCY_GUARANTEED = _runtime_config.UPSTREAM_IDEMPOTENCY_GUARANTEED
UPSTREAM_TIMEOUT_S = _runtime_config.UPSTREAM_TIMEOUT_S


LOG = logging.getLogger("image-job")

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


# Error classification — emitted to DB and exposed in the failed-job response
# so the caller can decide between "switch endpoint" and "switch provider".
ERROR_CLASS_NETWORK = "network"  # connect/read/timeout — switch provider
ERROR_CLASS_UPSTREAM_4XX = (
    "upstream_4xx"  # 4xx HTTP — switch endpoint (likely format mismatch)
)
ERROR_CLASS_UPSTREAM_5XX = "upstream_5xx"  # 5xx HTTP — switch provider
ERROR_CLASS_NO_IMAGE = "no_image"  # 200 but no image extractable — switch endpoint
ERROR_CLASS_IMAGE_SAVE = "image_save"  # save/decode failure — switch provider
ERROR_CLASS_INTERNAL = "internal"  # sidecar bug — switch provider
ERROR_CLASS_VALIDATION = "validation"  # bad input from caller — terminal


@dataclass
class ImageCandidateBudget:
    count: int = 0
    total_bytes: int = 0

    def next_max_bytes(self) -> int:
        if self.count >= MAX_IMAGE_CANDIDATES:
            raise JobFailure(
                f"上游图片候选数超过限制（max {MAX_IMAGE_CANDIDATES}）",
                error_class=ERROR_CLASS_IMAGE_SAVE,
            )
        remaining = MAX_TOTAL_IMAGE_BYTES - self.total_bytes
        if remaining <= 0:
            raise JobFailure(
                f"上游图片总字节超过限制（max {MAX_TOTAL_IMAGE_BYTES}）",
                error_class=ERROR_CLASS_IMAGE_SAVE,
            )
        return min(MAX_IMAGE_BYTES, remaining)

    def record(self, candidate: ImageCandidate) -> ImageCandidate:
        size = len(candidate.data)
        if size > MAX_IMAGE_BYTES:
            raise JobFailure(
                f"上游单图超过大小限制（max {MAX_IMAGE_BYTES}）",
                error_class=ERROR_CLASS_IMAGE_SAVE,
            )
        if self.count >= MAX_IMAGE_CANDIDATES:
            raise JobFailure(
                f"上游图片候选数超过限制（max {MAX_IMAGE_CANDIDATES}）",
                error_class=ERROR_CLASS_IMAGE_SAVE,
            )
        if self.total_bytes + size > MAX_TOTAL_IMAGE_BYTES:
            raise JobFailure(
                f"上游图片总字节超过限制（max {MAX_TOTAL_IMAGE_BYTES}）",
                error_class=ERROR_CLASS_IMAGE_SAVE,
            )
        self.count += 1
        self.total_bytes += size
        return candidate


class JobFailure(Exception):
    def __init__(
        self,
        error: str,
        *,
        upstream_status: int | None = None,
        upstream_body: Any | None = None,
        retryable: bool = False,
        retry_requires_idempotency: bool = False,
        outcome_uncertain: bool = False,
        error_class: str = ERROR_CLASS_INTERNAL,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.upstream_status = upstream_status
        self.upstream_body = upstream_body
        self.retryable = retryable
        self.retry_requires_idempotency = retry_requires_idempotency
        self.outcome_uncertain = outcome_uncertain
        self.retry_suppressed = False
        self.error_class = error_class


# --- Process-wide runtime state ----------------------------------------------

_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAX)
_workers: list[asyncio.Task[None]] = []
_background_tasks: list[asyncio.Task[None]] = []
_inflight: set[str] = set()
_queued_ids: set[str] = set()
_queue_state_lock = asyncio.Lock()
_shutdown = asyncio.Event()
_http_client: httpx.AsyncClient | None = None


def _reset_runtime_state() -> None:
    global _queue, _workers, _background_tasks
    global _inflight, _queued_ids, _queue_state_lock, _shutdown

    _queue = asyncio.Queue(maxsize=QUEUE_MAX)
    _workers = []
    _background_tasks = []
    _inflight = set()
    _queued_ids = set()
    _queue_state_lock = asyncio.Lock()
    _shutdown = asyncio.Event()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat()


auth_hash = _payload_helpers.auth_hash
json_dump = _payload_helpers.json_dump
stable_json_dump = _payload_helpers.stable_json_dump
request_hash = _payload_helpers.request_hash


def _new_pinned_image_download_client(
    target: PublicImageDownloadTarget,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=pinned_async_http_transport(target),
        timeout=httpx.Timeout(60.0, connect=UPSTREAM_CONNECT_TIMEOUT_S),
        follow_redirects=False,
        trust_env=False,
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": "lumen-image",
        },
    )


def request_idempotency_key(request: Request, raw_payload: Any) -> str | None:
    return _payload_helpers.request_idempotency_key(
        request,
        raw_payload,
        max_bytes=MAX_IDEMPOTENCY_KEY_BYTES,
    )


upstream_idempotency_key = _payload_helpers.upstream_idempotency_key
normalize_image_edit_input_transport = (
    _payload_helpers.normalize_image_edit_input_transport
)


# --- SQLite layer ------------------------------------------------------------
#
# Each call opens a fresh connection but applies tuning PRAGMAs. The SQLite
# state DB must live on local disk; CIFS/NAS mounts can split WAL files and make
# completed jobs appear stuck as queued/running. Every DB call is dispatched via
# ``asyncio.to_thread`` so writes never block the event loop.

if SQLITE_JOURNAL_MODE not in _ALLOWED_SQLITE_JOURNAL_MODES:
    SQLITE_JOURNAL_MODE = "WAL"

_DB_TUNING_PRAGMAS = sqlite_tuning_pragmas(SQLITE_JOURNAL_MODE)


def _open_conn() -> sqlite3.Connection:
    return _persistence_open_connection(DB_PATH, _DB_TUNING_PRAGMAS)


def init_storage_sync() -> None:
    _persistence_init_storage(
        data_dir=DATA_DIR,
        refs_dir=REFS_DIR,
        db_path=DB_PATH,
        open_conn=_open_conn,
    )


def _db_one_sync(sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
    return _persistence_db_one_sync(_open_conn, sql, params)


def _db_all_sync(sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    return _persistence_db_all_sync(_open_conn, sql, params)


def _db_exec_sync(sql: str, params: tuple[Any, ...]) -> int:
    return _persistence_db_exec_sync(_open_conn, sql, params)


async def db_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return await asyncio.to_thread(_db_one_sync, sql, params)


async def db_all(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return await asyncio.to_thread(_db_all_sync, sql, params)


async def db_exec(sql: str, params: tuple[Any, ...] = ()) -> int:
    return await asyncio.to_thread(_db_exec_sync, sql, params)


async def enqueue_job(job_id: str) -> str:
    """Queue a job once.

    Returns one of: ``enqueued``, ``queued``, ``inflight``, ``full``.
    """
    async with _queue_state_lock:
        if job_id in _queued_ids:
            return "queued"
        if job_id in _inflight:
            return "inflight"
        try:
            _queue.put_nowait(job_id)
        except asyncio.QueueFull:
            return "full"
        _queued_ids.add(job_id)
        return "enqueued"


async def insert_and_enqueue_job(
    job_id: str,
    payload: dict[str, Any],
    auth_header: str,
    *,
    idempotency_key: str | None = None,
    payload_hash: str | None = None,
) -> str:
    """Persist only after queue capacity is reserved under the queue lock."""

    async with _queue_state_lock:
        if _queue.full():
            return "full"
        await insert_job(
            job_id,
            payload,
            auth_header,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        _queue.put_nowait(job_id)
        _queued_ids.add(job_id)
        return "enqueued"


# --- Validation --------------------------------------------------------------


def _json_shape_limits() -> JsonShapeLimits:
    return JsonShapeLimits(
        max_depth=MAX_JSON_DEPTH,
        max_array_items=MAX_JSON_ARRAY_ITEMS,
        max_object_items=MAX_JSON_OBJECT_ITEMS,
        max_total_values=MAX_JSON_TOTAL_VALUES,
        max_key_chars=MAX_JSON_KEY_CHARS,
        max_string_chars=MAX_JSON_STRING_CHARS,
    )


def load_image_job_json(data: bytes) -> Any:
    return _load_json_bytes(data, _json_shape_limits())


def validate_json_shape(value: Any) -> None:
    _validate_json_shape(value, _json_shape_limits())


body_preview = _payload_helpers.body_preview
require_auth = _payload_helpers.require_auth
infer_request_type = _payload_helpers.infer_request_type


def _payload_policy() -> PayloadPolicy:
    return PayloadPolicy(
        allowed_fixed_endpoints=ALLOWED_FIXED_ENDPOINTS,
        allowed_prefix_endpoints=ALLOWED_PREFIX_ENDPOINTS,
        image_output_formats=IMAGE_OUTPUT_FORMATS,
        default_image_output_format=DEFAULT_IMAGE_OUTPUT_FORMAT,
        default_image_output_compression=DEFAULT_IMAGE_OUTPUT_COMPRESSION,
        responses_strip_partial_images=RESPONSES_STRIP_PARTIAL_IMAGES,
        max_endpoint_chars=MAX_ENDPOINT_CHARS,
        max_request_type_chars=MAX_REQUEST_TYPE_CHARS,
        default_retention_days=DEFAULT_RETENTION_DAYS,
        max_retention_days=MAX_RETENTION_DAYS,
    )


def normalize_endpoint(value: Any) -> str:
    return _payload_helpers.normalize_endpoint(value, _payload_policy())


def normalize_image_output_options(target: dict[str, Any]) -> None:
    _payload_helpers.normalize_image_output_options(target, _payload_policy())


def normalize_payload_body(
    endpoint: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    return _payload_helpers.normalize_payload_body(
        endpoint,
        body,
        _payload_policy(),
    )


def validate_payload(payload: Any) -> dict[str, Any]:
    validate_json_shape(payload)
    return _payload_helpers.validate_payload(payload, _payload_policy())


def make_job_id() -> str:
    return f"img_{utc_now().strftime('%Y%m%d')}_{secrets.token_hex(5)}"


# --- Job CRUD ----------------------------------------------------------------


_job_persistence = JobPersistenceFacade(
    db_exec=lambda sql, params=(): db_exec(sql, params),
    enqueue_job=lambda job_id: enqueue_job(job_id),
    now_iso=lambda: iso(),
    auth_hash=lambda value: auth_hash(value),
    json_dump=lambda value: json_dump(value),
    upstream_base_url=lambda: UPSTREAM_BASE_URL,
    upstream_idempotency_guaranteed=(lambda: UPSTREAM_IDEMPOTENCY_GUARANTEED),
    error_class_internal=lambda: ERROR_CLASS_INTERNAL,
    error_class_network=lambda: ERROR_CLASS_NETWORK,
    log=LOG,
)

insert_job = _job_persistence.insert_job
ensure_queued_job_scheduled = _job_persistence.ensure_queued_job_scheduled
mark_running = _job_persistence.mark_running
touch_running = _job_persistence.touch_running
mark_succeeded = _job_persistence.mark_succeeded
mark_failed = _job_persistence.mark_failed
fail_interrupted_running_jobs = _job_persistence.fail_interrupted_running_jobs
row_to_response = _job_persistence.row_to_response


# --- Image decoding / extraction --------------------------------------------

_IMAGE_SIGNATURES = _image_candidates_module._IMAGE_SIGNATURES
_IMAGE_DOWNLOAD_REDIRECT_STATUSES = (
    _image_candidates_module._IMAGE_DOWNLOAD_REDIRECT_STATUSES
)
_IMAGE_DOWNLOAD_ERROR_BODY_MAX_BYTES = (
    _image_candidates_module._IMAGE_DOWNLOAD_ERROR_BODY_MAX_BYTES
)
_RESPONSES_PARTIAL_TYPE_HINT = _image_candidates_module._RESPONSES_PARTIAL_TYPE_HINT
_RESPONSES_SUCCESS_TERMINAL_EVENTS = (
    _image_candidates_module._RESPONSES_SUCCESS_TERMINAL_EVENTS
)
_RESPONSES_ERROR_TERMINAL_EVENTS = (
    _image_candidates_module._RESPONSES_ERROR_TERMINAL_EVENTS
)

_image_candidates = ImageCandidateFacade(
    max_image_bytes=lambda: MAX_IMAGE_BYTES,
    max_total_image_bytes=lambda: MAX_TOTAL_IMAGE_BYTES,
    max_image_url_redirects=lambda: MAX_IMAGE_URL_REDIRECTS,
    responses_stream_idle_timeout_s=lambda: RESPONSES_STREAM_IDLE_TIMEOUT_S,
    responses_stream_max_bytes=lambda: RESPONSES_STREAM_MAX_BYTES,
    job_heartbeat_interval_s=lambda: JOB_HEARTBEAT_INTERVAL_S,
    error_class_network=lambda: ERROR_CLASS_NETWORK,
    error_class_upstream_4xx=lambda: ERROR_CLASS_UPSTREAM_4XX,
    error_class_upstream_5xx=lambda: ERROR_CLASS_UPSTREAM_5XX,
    error_class_image_save=lambda: ERROR_CLASS_IMAGE_SAVE,
    error_class_validation=lambda: ERROR_CLASS_VALIDATION,
    job_failure=lambda error, **kwargs: JobFailure(error, **kwargs),
    job_failure_type=JobFailure,
    image_candidate=lambda data, mime_type=None: ImageCandidate(data, mime_type),
    budget_factory=lambda: ImageCandidateBudget(),
    parse_json_bytes=lambda data: parse_json_bytes(data),
    body_preview=lambda data: body_preview(data),
    download_content_length=lambda headers: _download_content_length(headers),
    read_download_body_bounded=(
        lambda response, **kwargs: _read_download_body_bounded(response, **kwargs)
    ),
    new_pinned_image_download_client=(
        lambda target: _new_pinned_image_download_client(target)
    ),
    resolve_public_image_download_target=(
        lambda url: resolve_public_image_download_target(url)
    ),
    image_download_resolution_error=ImageDownloadResolutionError,
    touch_running=lambda job_id: touch_running(job_id),
    download_image_url_fn=(
        lambda client, url, **kwargs: download_image_url(
            client,
            url,
            **kwargs,
        )
    ),
    extract_candidates_fn=(
        lambda value, client, **kwargs: extract_candidates(
            value,
            client,
            **kwargs,
        )
    ),
    sse_line_decoder_factory=lambda: _SseLineDecoder(),
)


looks_like_image = _image_candidates.looks_like_image
_candidate_size_error = _image_candidates.candidate_size_error
decode_data_url = _image_candidates.decode_data_url
_b64_decode = _image_candidates.b64_decode
decode_base64 = _image_candidates.decode_base64
object_image_context = _image_candidates.object_image_context
_is_responses_partial_event = _image_candidates.is_responses_partial_event
_is_responses_success_terminal = _image_candidates.is_responses_success_terminal
_is_responses_error_terminal = _image_candidates.is_responses_error_terminal
download_image_url = _image_candidates.download_image_url
extract_candidates = _image_candidates.extract_candidates
parse_sse_json_objects = _image_candidates.parse_sse_json_objects
_try_parse_sse_data = _image_candidates.try_parse_sse_data
_sse_data_from_lines = _image_candidates.sse_data_from_lines
_contains_result_key = _image_candidates.contains_result_key
_first_stream_error = _image_candidates.first_stream_error
_classify_stream_error = _image_candidates.classify_stream_error
_stream_error_message = _image_candidates.stream_error_message
extract_response_images = _image_candidates.extract_response_images
extract_responses_stream_images = _image_candidates.extract_responses_stream_images


_image_artifacts = ImageArtifactFacade(
    data_dir=lambda: DATA_DIR,
    public_base_url=lambda: PUBLIC_BASE_URL,
    max_image_bytes=lambda: MAX_IMAGE_BYTES,
    max_image_candidates=lambda: MAX_IMAGE_CANDIDATES,
    max_total_image_bytes=lambda: MAX_TOTAL_IMAGE_BYTES,
    max_image_pixels=lambda: MAX_IMAGE_PIXELS,
    error_class_image_save=lambda: ERROR_CLASS_IMAGE_SAVE,
    error_class_validation=lambda: ERROR_CLASS_VALIDATION,
    job_failure=lambda error, **kwargs: JobFailure(error, **kwargs),
    image_candidate=lambda data, mime_type=None: ImageCandidate(data, mime_type),
    decode_data_url=lambda value: decode_data_url(value),
    decode_base64=lambda value: decode_base64(value),
    download_image_url=(
        lambda client, url, **kwargs: download_image_url(
            client,
            url,
            **kwargs,
        )
    ),
    json_dump=lambda value: json_dump(value),
    job_image_dir_fn=lambda job_id, created_at: job_image_dir(
        job_id,
        created_at,
    ),
    image_metadata_fn=lambda data, mime_type: image_metadata(data, mime_type),
    atomic_write_fn=lambda path, data: _atomic_write(path, data),
    save_one_image_sync_fn=(
        lambda image_dir, filename, data: _save_one_image_sync(
            image_dir,
            filename,
            data,
        )
    ),
    save_input_image_fn=(lambda *args, **kwargs: save_input_image(*args, **kwargs)),
    image_candidate_from_ref_fn=lambda ref: image_candidate_from_ref(ref),
    candidate_filename_fn=lambda stem, candidate: _candidate_filename(
        stem,
        candidate,
    ),
    token_hex=lambda size: secrets.token_hex(size),
)

image_metadata = _image_artifacts.image_metadata
job_image_dir = _image_artifacts.job_image_dir
_atomic_write = _image_artifacts.atomic_write
_save_one_image_sync = _image_artifacts.save_one_image_sync
save_images = _image_artifacts.save_images
save_input_image = _image_artifacts.save_input_image
image_candidate_from_ref = _image_artifacts.image_candidate_from_ref
materialize_edit_input_urls = _image_artifacts.materialize_edit_input_urls
_candidate_filename = _image_artifacts.candidate_filename
materialize_edit_input_files = _image_artifacts.materialize_edit_input_files


# --- Upstream call -----------------------------------------------------------

_upstream = UpstreamFacade(
    http_client=lambda: _http_client,
    upstream_base_url=lambda: UPSTREAM_BASE_URL,
    upstream_idempotency_guaranteed=lambda: UPSTREAM_IDEMPOTENCY_GUARANTEED,
    retry_network_max=lambda: RETRY_NETWORK_MAX,
    retry_responses_stream_max=lambda: RETRY_RESPONSES_STREAM_MAX,
    retry_upstream_5xx_max=lambda: RETRY_UPSTREAM_5XX_MAX,
    retry_backoff_s=lambda: RETRY_BACKOFF_S,
    max_upstream_error_body_bytes=lambda: MAX_UPSTREAM_ERROR_BODY_BYTES,
    max_upstream_response_bytes=lambda: MAX_UPSTREAM_RESPONSE_BYTES,
    max_image_bytes=lambda: MAX_IMAGE_BYTES,
    error_class_network=lambda: ERROR_CLASS_NETWORK,
    error_class_upstream_4xx=lambda: ERROR_CLASS_UPSTREAM_4XX,
    error_class_upstream_5xx=lambda: ERROR_CLASS_UPSTREAM_5XX,
    error_class_no_image=lambda: ERROR_CLASS_NO_IMAGE,
    error_class_image_save=lambda: ERROR_CLASS_IMAGE_SAVE,
    error_class_internal=lambda: ERROR_CLASS_INTERNAL,
    job_failure=lambda error, **kwargs: JobFailure(error, **kwargs),
    job_failure_type=JobFailure,
    parse_json_bytes=lambda data: parse_json_bytes(data),
    body_preview=lambda data: body_preview(data),
    read_response_body_bounded=(
        lambda response, **kwargs: _read_response_body_bounded(response, **kwargs)
    ),
    extract_response_images=(
        lambda response, client, **kwargs: extract_response_images(
            response,
            client,
            **kwargs,
        )
    ),
    extract_responses_stream_images=(
        lambda response, client, **kwargs: extract_responses_stream_images(
            response,
            client,
            **kwargs,
        )
    ),
    materialize_edit_input_files=(
        lambda client, body: materialize_edit_input_files(client, body)
    ),
    materialize_edit_input_urls=(
        lambda row, body: materialize_edit_input_urls(row, body)
    ),
    save_images=lambda *args, **kwargs: save_images(*args, **kwargs),
    normalize_image_edit_input_transport=(
        lambda value: normalize_image_edit_input_transport(value)
    ),
    upstream_idempotency_key=lambda job_id: upstream_idempotency_key(job_id),
    call_upstream_once_fn=(lambda row, **kwargs: _call_upstream_once(row, **kwargs)),
    log=LOG,
)


_classify_httpx_error = _upstream.classify_httpx_error
_httpx_error_requires_idempotency = _upstream.httpx_error_requires_idempotency
_is_retryable_job_failure = _upstream.is_retryable_job_failure
_mark_post_dispatch_failure = _upstream.mark_post_dispatch_failure
_retry_budget_for_failure = _upstream.retry_budget_for_failure
_raise_upstream_http_error = _upstream.raise_upstream_http_error
_extract_non_stream_response_images = _upstream.extract_non_stream_response_images
_call_upstream_once = _upstream.call_upstream_once
call_upstream = _upstream.call_upstream


# --- Worker loop -------------------------------------------------------------


async def running_heartbeat(job_id: str) -> None:
    try:
        while True:
            await asyncio.sleep(JOB_HEARTBEAT_INTERVAL_S)
            await touch_running(job_id)
    except asyncio.CancelledError:
        raise


async def process_job(job_id: str) -> None:
    row = await db_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if row is None or row["status"] != "queued":
        return

    LOG.info("starting image job %s endpoint=%s", job_id, row["endpoint"])
    if not await mark_running(job_id):
        return
    started = time.monotonic()
    endpoint_used = row["endpoint"]
    heartbeat = asyncio.create_task(
        running_heartbeat(job_id), name=f"image-job-heartbeat-{job_id}"
    )
    try:
        fresh_row = await db_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        if fresh_row is None:
            return
        upstream_status, images = await call_upstream(fresh_row)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await mark_succeeded(
            job_id,
            upstream_status=upstream_status,
            elapsed_ms=elapsed_ms,
            images=images,
            endpoint_used=endpoint_used,
        )
        LOG.info(
            "image job %s succeeded endpoint=%s images=%d elapsed_ms=%d",
            job_id,
            endpoint_used,
            len(images),
            elapsed_ms,
        )
    except JobFailure as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await mark_failed(
            job_id,
            error=exc.error,
            upstream_status=exc.upstream_status,
            upstream_body=exc.upstream_body,
            elapsed_ms=elapsed_ms,
            error_class=exc.error_class,
            endpoint_used=endpoint_used,
            retryable=_is_retryable_job_failure(exc),
            retry_suppressed=exc.retry_suppressed,
            outcome_uncertain=exc.outcome_uncertain,
        )
        LOG.warning(
            "image job %s terminal status=%s endpoint=%s class=%s: %s",
            job_id,
            "uncertain" if exc.outcome_uncertain else "failed",
            endpoint_used,
            exc.error_class,
            exc.error,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await mark_failed(
            job_id,
            error=f"image job worker error: {exc.__class__.__name__}: {exc}",
            elapsed_ms=elapsed_ms,
            error_class=ERROR_CLASS_INTERNAL,
            endpoint_used=endpoint_used,
        )
        LOG.exception("image job %s crashed", job_id)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def worker_loop(worker_id: int) -> None:
    LOG.info("image job worker %d started", worker_id)
    try:
        while not _shutdown.is_set():
            try:
                job_id = await asyncio.wait_for(_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            async with _queue_state_lock:
                _queued_ids.discard(job_id)
                _inflight.add(job_id)
            try:
                await process_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception(
                    "worker %d unexpected error processing %s", worker_id, job_id
                )
            finally:
                async with _queue_state_lock:
                    _inflight.discard(job_id)
                _queue.task_done()
    except asyncio.CancelledError:
        LOG.info("image job worker %d cancelled", worker_id)
        raise
    finally:
        LOG.info("image job worker %d exiting", worker_id)


# --- Retention sweeper -------------------------------------------------------


async def reconcile_stuck_jobs() -> dict[str, int]:
    queued_cutoff = (utc_now() - timedelta(seconds=STUCK_QUEUED_AFTER_S)).isoformat()
    running_cutoff = (utc_now() - timedelta(seconds=STUCK_RUNNING_AFTER_S)).isoformat()
    stats = {
        "queued_requeued": 0,
        "running_requeued": 0,
        "running_uncertain": 0,
        "failed_missing_auth": 0,
        "active": 0,
        "queue_full": 0,
    }

    queued_rows = await db_all(
        """
        SELECT job_id, auth_header
        FROM jobs
        WHERE status = 'queued' AND updated_at < ?
        ORDER BY created_at
        LIMIT ?
        """,
        (queued_cutoff, STUCK_RECONCILE_BATCH),
    )
    for row in queued_rows:
        job_id = row["job_id"]
        if not row["auth_header"]:
            await mark_failed(
                job_id,
                error="stuck queued job has no auth header",
                error_class=ERROR_CLASS_INTERNAL,
            )
            stats["failed_missing_auth"] += 1
            continue
        result = await enqueue_job(job_id)
        if result == "enqueued":
            stats["queued_requeued"] += 1
            await db_exec(
                "UPDATE jobs SET updated_at = ? WHERE job_id = ? AND status = 'queued'",
                (iso(), job_id),
            )
        elif result == "full":
            stats["queue_full"] += 1
        else:
            stats["active"] += 1

    running_rows = await db_all(
        """
        SELECT job_id, auth_header
        FROM jobs
        WHERE status = 'running' AND updated_at < ?
        ORDER BY COALESCE(started_at, updated_at)
        LIMIT ?
        """,
        (running_cutoff, STUCK_RECONCILE_BATCH),
    )
    for row in running_rows:
        job_id = row["job_id"]
        if not row["auth_header"]:
            await mark_failed(
                job_id,
                error="stuck running job has no auth header",
                error_class=ERROR_CLASS_INTERNAL,
            )
            stats["failed_missing_auth"] += 1
            continue

        mark_uncertain = False
        async with _queue_state_lock:
            if job_id in _queued_ids or job_id in _inflight:
                stats["active"] += 1
                continue
            if not UPSTREAM_IDEMPOTENCY_GUARANTEED:
                mark_uncertain = True
            elif _queue.full():
                stats["queue_full"] += 1
                continue
            else:
                updated = await db_exec(
                    """
                    UPDATE jobs
                    SET status = 'queued', updated_at = ?
                    WHERE job_id = ? AND status = 'running' AND updated_at < ?
                    """,
                    (iso(), job_id, running_cutoff),
                )
                if updated:
                    _queue.put_nowait(job_id)
                    _queued_ids.add(job_id)
                    stats["running_requeued"] += 1
        if mark_uncertain:
            await mark_failed(
                job_id,
                error="stuck running job has an unresolved upstream result",
                error_class=ERROR_CLASS_NETWORK,
                retryable=True,
                retry_suppressed=True,
                outcome_uncertain=True,
            )
            stats["running_uncertain"] += 1
    return stats


async def stuck_reconciler() -> None:
    LOG.info(
        "stuck reconciler started interval=%ss queued_after=%ss running_after=%ss",
        STUCK_RECONCILE_INTERVAL_S,
        STUCK_QUEUED_AFTER_S,
        STUCK_RUNNING_AFTER_S,
    )
    try:
        while not _shutdown.is_set():
            try:
                await asyncio.wait_for(
                    _shutdown.wait(), timeout=STUCK_RECONCILE_INTERVAL_S
                )
                break
            except asyncio.TimeoutError:
                pass
            try:
                stats = await reconcile_stuck_jobs()
                requeued = stats["queued_requeued"] + stats["running_requeued"]
                if (
                    requeued
                    or stats["running_uncertain"]
                    or stats["failed_missing_auth"]
                    or stats["queue_full"]
                ):
                    LOG.warning("stuck reconciler repaired jobs stats=%s", stats)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("stuck reconciler iteration failed")
    except asyncio.CancelledError:
        raise


_retention = RetentionFacade(
    data_dir=lambda: DATA_DIR,
    refs_dir=lambda: REFS_DIR,
    db_exec_sync=lambda sql, params: _db_exec_sync(sql, params),
    db_exec=lambda sql, params=(): db_exec(sql, params),
    db_all=lambda sql, params=(): db_all(sql, params),
    utc_now=lambda: utc_now(),
    max_retention_days=lambda: MAX_RETENTION_DAYS,
    job_ttl_days=lambda: JOB_TTL_DAYS,
    log=LOG,
    sweep_dir_fn=lambda base, cutoff: _sweep_dir_sync(base, cutoff),
    sweep_filesystem_fn=lambda cutoff: _sweep_filesystem_sync(cutoff),
)

_sweep_dir_sync = _retention.sweep_dir
_sweep_filesystem_sync = _retention.sweep_filesystem
_run_retention_pass = _retention.run_pass


async def retention_sweeper() -> None:
    LOG.info(
        "retention sweeper started interval=%ss job_ttl_days=%d",
        RETENTION_SWEEP_INTERVAL_S,
        JOB_TTL_DAYS,
    )
    # image-stability-hardening §image-job：启动时先 sweep 一次。崩溃恢复 / 长时间
    # 停机后立刻起来如果还要等一个 INTERVAL（默认 1h），磁盘会持续被旧文件占用。
    try:
        await _run_retention_pass()
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.exception("retention sweeper initial pass failed")
    try:
        while not _shutdown.is_set():
            try:
                await asyncio.wait_for(
                    _shutdown.wait(), timeout=RETENTION_SWEEP_INTERVAL_S
                )
                break
            except asyncio.TimeoutError:
                pass
            try:
                await _run_retention_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("retention sweeper iteration failed")
    except asyncio.CancelledError:
        raise


# --- Lifespan ----------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
    _reset_runtime_state()

    await asyncio.to_thread(init_storage_sync)
    await fail_interrupted_running_jobs()

    timeout = httpx.Timeout(
        UPSTREAM_TIMEOUT_S,
        connect=UPSTREAM_CONNECT_TIMEOUT_S,
        write=60.0,
        pool=30.0,
    )
    limits = httpx.Limits(
        max_keepalive_connections=HTTP_POOL_KEEPALIVE,
        max_connections=HTTP_POOL_MAX,
    )
    _http_client = httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
        http2=False,
        trust_env=False,
        headers={"User-Agent": "lumen-image"},
    )

    for row in await db_all(
        "SELECT job_id FROM jobs WHERE status = 'queued' ORDER BY created_at"
    ):
        result = await enqueue_job(row["job_id"])
        if result == "full":
            LOG.warning(
                "queue full while restoring backlog; remaining queued jobs deferred"
            )
            break

    for index in range(CONCURRENCY):
        _workers.append(
            asyncio.create_task(
                worker_loop(index + 1), name=f"image-worker-{index + 1}"
            )
        )
    _background_tasks.append(
        asyncio.create_task(retention_sweeper(), name="image-retention-sweeper")
    )
    _background_tasks.append(
        asyncio.create_task(stuck_reconciler(), name="image-stuck-reconciler")
    )

    try:
        yield
    finally:
        LOG.info("image-job sidecar shutting down")
        _shutdown.set()

        # Wait for in-flight jobs to drain (bounded by GRACEFUL_SHUTDOWN_S).
        deadline = time.monotonic() + GRACEFUL_SHUTDOWN_S
        while time.monotonic() < deadline:
            async with _queue_state_lock:
                if not _inflight:
                    break
            await asyncio.sleep(0.5)

        for worker in _workers + _background_tasks:
            worker.cancel()
        await asyncio.gather(*_workers, *_background_tasks, return_exceptions=True)
        _workers.clear()
        _background_tasks.clear()

        if _http_client is not None:
            await _http_client.aclose()
            _http_client = None


app = FastAPI(title="sub2api image job sidecar", lifespan=lifespan)


# --- HTTP routes -------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    async with _queue_state_lock:
        inflight = list(_inflight)
        queued_known = len(_queued_ids)
    return {
        "status": "ok",
        "queue_size": _queue.qsize(),
        "queued_known": queued_known,
        "queue_max": QUEUE_MAX,
        "inflight": len(inflight),
        "concurrency": CONCURRENCY,
    }


@app.post("/v1/image-jobs")
async def create_image_job(request: Request) -> dict[str, Any]:
    auth_header = require_auth(request)
    raw = await _read_request_body_bounded(
        request,
        max_bytes=MAX_IMAGE_JOB_REQUEST_BYTES,
    )
    if not raw:
        raise HTTPException(status_code=400, detail="empty JSON body")
    raw_payload = load_image_job_json(raw)
    payload = validate_payload(raw_payload)
    auth_digest = auth_hash(auth_header)
    idempotency_key = request_idempotency_key(request, raw_payload)
    payload_hash = request_hash(payload)
    if idempotency_key is not None:
        existing = await db_one(
            "SELECT * FROM jobs WHERE auth_hash = ? AND idempotency_key = ?",
            (auth_digest, idempotency_key),
        )
        if existing is not None:
            if existing["request_hash"] != payload_hash:
                raise HTTPException(
                    status_code=409,
                    detail="idempotency key already used for a different image job",
                )
            await ensure_queued_job_scheduled(existing)
            return row_to_response(existing)
    job_id = make_job_id()
    try:
        result = await insert_and_enqueue_job(
            job_id,
            payload,
            auth_header,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
    except sqlite3.IntegrityError:
        if idempotency_key is None:
            raise
        existing = await db_one(
            "SELECT * FROM jobs WHERE auth_hash = ? AND idempotency_key = ?",
            (auth_digest, idempotency_key),
        )
        if existing is not None and existing["request_hash"] == payload_hash:
            await ensure_queued_job_scheduled(existing)
            return row_to_response(existing)
        raise HTTPException(
            status_code=409,
            detail="idempotency key already used for a different image job",
        ) from None
    if result == "full":
        if idempotency_key is not None:
            existing = await db_one(
                "SELECT * FROM jobs WHERE auth_hash = ? AND idempotency_key = ?",
                (auth_digest, idempotency_key),
            )
            if existing is not None:
                if existing["request_hash"] != payload_hash:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "idempotency key already used for a different image job"
                        ),
                    )
                await ensure_queued_job_scheduled(existing)
                return row_to_response(existing)
        raise HTTPException(status_code=503, detail="image job queue full") from None
    return {
        "job_id": job_id,
        "status": "queued",
        "request_type": payload["request_type"],
        "endpoint": payload["endpoint"],
        "relay_url": UPSTREAM_BASE_URL,
        "retention_days": payload["retention_days"],
    }


@app.get("/v1/image-jobs/{job_id}")
async def get_image_job(job_id: str, request: Request) -> dict[str, Any]:
    auth_header = require_auth(request)
    row = await db_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="image job not found")
    if not hmac.compare_digest(row["auth_hash"], auth_hash(auth_header)):
        raise HTTPException(
            status_code=403, detail="image job belongs to a different key"
        )
    return row_to_response(row)


_REF_MIME_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}
_REF_FORMAT_MIME: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def _refs_public_url(token: str, ext: str) -> str:
    return f"{PUBLIC_BASE_URL}/refs/{token}.{ext}"


_reference_persistence = ReferencePersistenceFacade(
    db_one_sync=lambda sql, params: _db_one_sync(sql, params),
    db_exec_sync=lambda sql, params: _db_exec_sync(sql, params),
    refs_dir=lambda: REFS_DIR,
    now_iso=lambda: iso(),
    token_hex=lambda size: secrets.token_hex(size),
    file_path_fn=lambda token, ext: _refs_file_path(token, ext),
)

_refs_file_path = _reference_persistence.file_path
_existing_ref_sync = _reference_persistence.existing_ref
_write_ref_sync = _reference_persistence.write_ref


@app.post("/v1/refs")
async def upload_reference(request: Request) -> dict[str, Any]:
    """接收参考图 raw bytes，返回公网 URL。

    Body：raw 图片 bytes（PNG / JPEG / WebP）。Content-Type 只做允许性校验；
    落盘扩展名和实际 MIME 以 Pillow 识别的格式为准。
    Auth：与其他端点一致（Authorization: Bearer <api_key>）；同一 auth 下同 sha256
        复用已有 URL，不同 auth 会生成不同 token，避免跨 key 共享 bearer URL。
    幂等：同 auth + 同 sha256 复用已有 URL；不重复写盘。
    TTL：与 jobs 共用 MAX_RETENTION_DAYS（默认 1d）。
    """
    auth_header = require_auth(request)
    auth_digest = auth_hash(auth_header)
    raw = await _read_request_body_bounded(request, max_bytes=MAX_REF_BYTES)
    if not raw:
        raise HTTPException(status_code=400, detail="empty body")

    mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = _REF_MIME_EXT.get(mime)
    if ext is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported content-type {mime!r}; expected image/png|jpeg|webp",
        )
    try:
        width, height, fmt = await asyncio.to_thread(image_metadata, raw, None)
    except JobFailure as exc:
        status_code = exc.upstream_status if exc.upstream_status in {400, 413} else 400
        raise HTTPException(status_code=status_code, detail=exc.error) from exc
    if width is None or height is None or fmt == "bin":
        raise HTTPException(status_code=400, detail="reference is not a valid image")
    actual_mime = _REF_FORMAT_MIME.get(fmt)
    if actual_mime is None:
        raise HTTPException(
            status_code=400,
            detail="reference is not a supported image format",
        )
    ext = _REF_MIME_EXT[actual_mime]

    sha = hashlib.sha256(raw).hexdigest()
    existing = await asyncio.to_thread(_existing_ref_sync, auth_digest, sha)
    if existing is not None:
        token, ext_existing = existing
        return {
            "url": _refs_public_url(token, ext_existing),
            "sha256": sha,
            "size": len(raw),
            "deduped": True,
        }

    token = secrets.token_urlsafe(24)
    await asyncio.to_thread(_write_ref_sync, auth_digest, sha, token, ext, raw)
    # 成功后 _existing_ref_sync 应总能命中（除非并发 race，那种情况下 token 用 first writer 的）
    final = await asyncio.to_thread(_existing_ref_sync, auth_digest, sha)
    if final is not None:
        token_final, ext_final = final
    else:
        # 极端兜底：DB INSERT 没成且文件没事，用我们刚写的 token
        token_final, ext_final = token, ext
    return {
        "url": _refs_public_url(token_final, ext_final),
        "sha256": sha,
        "size": len(raw),
        "deduped": token_final != token,
    }
