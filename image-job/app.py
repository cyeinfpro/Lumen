from __future__ import annotations

import asyncio
import base64
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
from typing import Any, Iterable
from urllib.parse import urljoin

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

ImageArtifactFacade = _image_artifacts_module.ImageArtifactFacade
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

_IMAGE_DOWNLOAD_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_IMAGE_DOWNLOAD_ERROR_BODY_MAX_BYTES = 64 * 1024


@dataclass
class ImageCandidate:
    data: bytes
    mime_type: str | None = None


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

_IMAGE_SIGNATURES: tuple[bytes, ...] = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF87a",
    b"GIF89a",
    b"RIFF",  # WEBP container starts with RIFF
    b"BM",  # BMP (rare but harmless)
    b"\x00\x00\x00\x0cftypheic",  # HEIC (offset 0)
    b"\x00\x00\x00\x18ftypheic",
)


def looks_like_image(data: bytes) -> bool:
    if len(data) < 8:
        return False
    if any(data.startswith(sig) for sig in _IMAGE_SIGNATURES):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if b"ftyp" in data[:32]:
        return True
    return False


def _candidate_size_error(max_bytes: int) -> JobFailure:
    if max_bytes < MAX_IMAGE_BYTES:
        return JobFailure(
            f"上游图片总字节超过限制（max {MAX_TOTAL_IMAGE_BYTES}）",
            error_class=ERROR_CLASS_IMAGE_SAVE,
        )
    return JobFailure(
        f"上游单图超过大小限制（max {MAX_IMAGE_BYTES}）",
        error_class=ERROR_CLASS_IMAGE_SAVE,
    )


def decode_data_url(
    value: str,
    *,
    max_bytes: int | None = None,
) -> ImageCandidate | None:
    if not value.startswith("data:image/") or "," not in value:
        return None
    effective_max = MAX_IMAGE_BYTES if max_bytes is None else max_bytes
    header, encoded = value.split(",", 1)
    mime_type = header.removeprefix("data:").split(";", 1)[0]
    is_b64 = ";base64" in header
    if is_b64:
        data = _b64_decode(encoded, max_bytes=effective_max)
        if data is None:
            return None
    else:
        data = encoded.encode("utf-8", "replace")
    if len(data) > effective_max:
        raise _candidate_size_error(effective_max)
    if not looks_like_image(data):
        return None
    return ImageCandidate(data, mime_type)


def _b64_decode(value: str, *, max_bytes: int | None = None) -> bytes | None:
    compact = "".join(value.split())
    if not compact:
        return None
    pad = len(compact) % 4
    if pad:
        compact += "=" * (4 - pad)
    padding = len(compact) - len(compact.rstrip("="))
    decoded_size = len(compact) // 4 * 3 - min(padding, 2)
    if max_bytes is not None and decoded_size > max_bytes:
        raise _candidate_size_error(max_bytes)
    try:
        return base64.b64decode(compact, validate=True)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return None


def decode_base64(value: str, *, max_bytes: int | None = None) -> bytes | None:
    value = value.strip()
    if not value:
        return None
    effective_max = MAX_IMAGE_BYTES if max_bytes is None else max_bytes
    if value.startswith("data:image/"):
        candidate = decode_data_url(value, max_bytes=effective_max)
        return candidate.data if candidate else None
    data = _b64_decode(value, max_bytes=effective_max)
    if data is None:
        return None
    if len(data) > effective_max:
        raise _candidate_size_error(effective_max)
    if not looks_like_image(data):
        return None
    return data


def object_image_context(value: dict[str, Any]) -> bool:
    type_value = str(value.get("type", "")).lower()
    mime_value = str(value.get("mimeType") or value.get("mime_type") or "").lower()
    if "image" in type_value or mime_value.startswith("image/"):
        return True
    keys = {str(k) for k in value.keys()}
    return bool({"b64_json", "inlineData", "inline_data", "partial_image_b64"} & keys)


# OpenAI Responses API streams a sequence of events. Partial-image events deliver
# progressively-refined previews (b64) before the final image; if we extract them
# all we end up storing 3+ near-duplicates that sha256 dedupe can't catch (each
# partial differs by a few bytes). The sidecar only needs the final image.
_RESPONSES_PARTIAL_TYPE_HINT = ".partial_image"

# image-stability-hardening §P0：兼容网关常见 terminal 事件名。
# 成功 terminal：response.completed / response.done。
# 错误 terminal：response.failed / response.incomplete / error。
# 旧版只识别 ``response.failed`` + ``error``，response.incomplete 会被当成"流提前结束"
# 错判为可重试 network 错（实际是 incomplete_details，重试仍会 incomplete）。
_RESPONSES_SUCCESS_TERMINAL_EVENTS = frozenset({"response.completed", "response.done"})
_RESPONSES_ERROR_TERMINAL_EVENTS = frozenset(
    {"response.failed", "response.incomplete", "error"}
)


def _is_responses_partial_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    event_type = str(event.get("type", ""))
    return _RESPONSES_PARTIAL_TYPE_HINT in event_type


def _is_responses_success_terminal(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    return str(event.get("type", "")) in _RESPONSES_SUCCESS_TERMINAL_EVENTS


def _is_responses_error_terminal(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    return str(event.get("type", "")) in _RESPONSES_ERROR_TERMINAL_EVENTS


async def download_image_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    cache: dict[str, ImageCandidate],
    max_bytes: int | None = None,
    retry_requires_idempotency: bool = True,
) -> ImageCandidate | None:
    effective_max = MAX_IMAGE_BYTES if max_bytes is None else max_bytes
    candidate_url = url.strip()
    if candidate_url.startswith("data:image/"):
        return decode_data_url(candidate_url, max_bytes=effective_max)
    if not candidate_url.lower().startswith(("http://", "https://")):
        return None
    cached = cache.get(url)
    if cached is not None:
        return cached
    _ = client
    current_url = candidate_url
    try:
        redirects = 0
        while True:
            try:
                target = await resolve_public_image_download_target(current_url)
            except ImageDownloadResolutionError as exc:
                raise JobFailure(
                    f"下载上游图片失败: {exc}",
                    retryable=True,
                    retry_requires_idempotency=retry_requires_idempotency,
                    outcome_uncertain=retry_requires_idempotency,
                    error_class=ERROR_CLASS_NETWORK,
                ) from exc
            except ValueError as exc:
                prefix = (
                    "图片重定向目标不允许下载" if redirects else "图片 URL 不允许下载"
                )
                raise JobFailure(
                    f"{prefix}: {exc}",
                    upstream_status=400,
                    error_class=ERROR_CLASS_VALIDATION,
                ) from exc

            async with _new_pinned_image_download_client(target) as download_client:
                async with download_client.stream(
                    "GET",
                    target.url,
                    follow_redirects=False,
                ) as resp:
                    if resp.status_code in _IMAGE_DOWNLOAD_REDIRECT_STATUSES:
                        location = (resp.headers.get("location") or "").strip()
                        if not location:
                            raise JobFailure(
                                "上游图片重定向缺少 Location",
                                upstream_status=resp.status_code,
                                error_class=ERROR_CLASS_UPSTREAM_4XX,
                            )
                        if redirects >= MAX_IMAGE_URL_REDIRECTS:
                            raise JobFailure(
                                "上游图片重定向次数过多",
                                upstream_status=resp.status_code,
                                error_class=ERROR_CLASS_UPSTREAM_4XX,
                            )
                        redirects += 1
                        current_url = urljoin(target.url, location)
                        continue

                    if not 200 <= resp.status_code < 300:
                        error_limit = min(
                            MAX_IMAGE_BYTES,
                            _IMAGE_DOWNLOAD_ERROR_BODY_MAX_BYTES,
                        )
                        declared_size = _download_content_length(resp.headers)
                        if declared_size is not None and declared_size > error_limit:
                            err_content = b""
                            body_truncated = True
                        else:
                            (
                                err_content,
                                body_truncated,
                                _received_bytes,
                            ) = await _read_download_body_bounded(
                                resp,
                                max_bytes=error_limit,
                                truncate=True,
                            )
                        upstream_body: Any = body_preview(err_content)
                        if body_truncated:
                            upstream_body = {
                                "preview": upstream_body,
                                "truncated": True,
                            }
                        ec = (
                            ERROR_CLASS_UPSTREAM_5XX
                            if resp.status_code >= 500
                            else ERROR_CLASS_UPSTREAM_4XX
                        )
                        raise JobFailure(
                            f"下载上游图片失败 HTTP {resp.status_code}",
                            upstream_status=resp.status_code,
                            upstream_body=upstream_body,
                            retryable=resp.status_code >= 500,
                            retry_requires_idempotency=retry_requires_idempotency,
                            outcome_uncertain=(
                                resp.status_code >= 500 and retry_requires_idempotency
                            ),
                            error_class=ec,
                        )

                    declared_size = _download_content_length(resp.headers)
                    if declared_size is not None and declared_size > effective_max:
                        raise JobFailure(
                            "上游图片超过大小限制（Content-Length 预检）",
                            upstream_status=resp.status_code,
                            error_class=ERROR_CLASS_IMAGE_SAVE,
                        )
                    (
                        content,
                        body_truncated,
                        _received_bytes,
                    ) = await _read_download_body_bounded(
                        resp,
                        max_bytes=effective_max,
                        truncate=False,
                    )
                    if body_truncated:
                        failure = _candidate_size_error(effective_max)
                        failure.upstream_status = resp.status_code
                        raise failure
                    content_type = resp.headers.get("content-type")
                    break
    except JobFailure:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise JobFailure(
            f"下载上游图片失败: {exc.__class__.__name__}: {exc}",
            retryable=True,
            retry_requires_idempotency=retry_requires_idempotency,
            outcome_uncertain=retry_requires_idempotency,
            error_class=ERROR_CLASS_NETWORK,
        ) from exc
    candidate = ImageCandidate(content, content_type)
    cache[url] = candidate
    cache[current_url] = candidate
    return candidate


async def extract_candidates(
    value: Any,
    client: httpx.AsyncClient,
    *,
    image_context: bool = False,
    cache: dict[str, ImageCandidate] | None = None,
    budget: ImageCandidateBudget | None = None,
) -> list[ImageCandidate]:
    if cache is None:
        cache = {}
    if budget is None:
        budget = ImageCandidateBudget()
    candidates: list[ImageCandidate] = []
    if isinstance(value, list):
        for item in value:
            candidates.extend(
                await extract_candidates(
                    item,
                    client,
                    image_context=image_context,
                    cache=cache,
                    budget=budget,
                )
            )
        return candidates
    if not isinstance(value, dict):
        return candidates

    context = image_context or object_image_context(value)

    for inline_key in ("inlineData", "inline_data"):
        inline = value.get(inline_key)
        if isinstance(inline, dict) and isinstance(inline.get("data"), str):
            data = decode_base64(
                inline["data"],
                max_bytes=budget.next_max_bytes(),
            )
            if data is not None:
                candidates.append(
                    budget.record(
                        ImageCandidate(
                            data,
                            inline.get("mimeType") or inline.get("mime_type"),
                        )
                    )
                )

    for key, item in value.items():
        if key in {"inlineData", "inline_data"}:
            continue
        if isinstance(item, str):
            if key in {
                "b64_json",
                "image_b64",
                "image_base64",
                "base64_image",
                "partial_image_b64",
            }:
                data = decode_base64(item, max_bytes=budget.next_max_bytes())
                if data is not None:
                    candidates.append(
                        budget.record(
                            ImageCandidate(
                                data,
                                value.get("mimeType") or value.get("mime_type"),
                            )
                        )
                    )
            elif key in {"result", "data"} and context:
                data = decode_base64(item, max_bytes=budget.next_max_bytes())
                if data is not None:
                    candidates.append(
                        budget.record(
                            ImageCandidate(
                                data,
                                value.get("mimeType") or value.get("mime_type"),
                            )
                        )
                    )
            elif key in {"url", "image_url"}:
                downloaded = await download_image_url(
                    client,
                    item,
                    cache=cache,
                    max_bytes=budget.next_max_bytes(),
                )
                if downloaded is not None:
                    candidates.append(budget.record(downloaded))
        elif isinstance(item, (dict, list)):
            candidates.extend(
                await extract_candidates(
                    item,
                    client,
                    image_context=context,
                    cache=cache,
                    budget=budget,
                )
            )

    return candidates


def parse_sse_json_objects(text: str) -> list[Any]:
    objects: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        parsed = parse_json_bytes(data.encode("utf-8"))
        if parsed is not None:
            objects.append(parsed)
    return objects


def _try_parse_sse_data(data: str) -> Any | None:
    data = data.strip()
    if not data or data == "[DONE]":
        return None
    return parse_json_bytes(data.encode("utf-8"))


def _sse_data_from_lines(lines: list[str]) -> str | None:
    parts: list[str] = []
    for raw in lines:
        if raw.startswith("data:"):
            data = raw[5:]
            if data.startswith(" "):
                data = data[1:]
            parts.append(data)
    if not parts:
        return None
    return "\n".join(parts)


def _contains_result_key(value: Any) -> bool:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if "result" in current:
                return True
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def _first_stream_error(events: Iterable[Any]) -> dict[str, Any] | None:
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type == "error" and isinstance(event.get("error"), dict):
            return event["error"]
        if event_type == "response.failed":
            response = event.get("response")
            if isinstance(response, dict):
                error = response.get("error")
                if isinstance(error, dict):
                    return error
            return {
                "type": "response_failed",
                "code": "response_failed",
                "message": "Responses stream ended with response.failed",
            }
        # image-stability-hardening §P0：response.incomplete 也是 terminal error
        # 形态。typical payload：{"type":"response.incomplete","response":{
        # "incomplete_details":{"reason":"max_output_tokens"}}}。旧版漏识别 →
        # 当作"流提前结束"按 network 重试 → 反复 incomplete 烧配额。
        if event_type == "response.incomplete":
            response = event.get("response")
            if isinstance(response, dict):
                detail = response.get("incomplete_details") or response.get("error")
                if isinstance(detail, dict):
                    out = dict(detail)
                    out.setdefault("type", "response_incomplete")
                    out.setdefault("code", "response_incomplete")
                    return out
            return {
                "type": "response_incomplete",
                "code": "response_incomplete",
                "message": "Responses stream ended with response.incomplete",
            }
    return None


def _classify_stream_error(error: dict[str, Any]) -> str:
    code = str(error.get("code") or "").lower()
    error_type = str(error.get("type") or "").lower()
    message = str(error.get("message") or "").lower()
    joined = " ".join((code, error_type, message))
    if (
        "moderation" in joined
        or "safety" in joined
        or error_type.endswith("_user_error")
    ):
        return ERROR_CLASS_VALIDATION
    if "invalid" in joined or "bad_request" in joined or "bad request" in joined:
        return ERROR_CLASS_UPSTREAM_4XX
    return ERROR_CLASS_UPSTREAM_5XX


def _stream_error_message(error: dict[str, Any]) -> str:
    code = str(error.get("code") or error.get("type") or "stream_error")
    message = str(
        error.get("message") or "Responses stream failed before returning an image"
    )
    return f"上游流式错误 {code}: {message}"


async def extract_response_images(
    resp: httpx.Response,
    client: httpx.AsyncClient,
    *,
    budget: ImageCandidateBudget | None = None,
) -> list[ImageCandidate]:
    if budget is None:
        budget = ImageCandidateBudget()
    content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type.startswith("image/"):
        budget.next_max_bytes()
        return [budget.record(ImageCandidate(resp.content, content_type))]

    parsed = parse_json_bytes(resp.content)
    if parsed is not None:
        stream_error = _first_stream_error([parsed])
        if stream_error is not None:
            raise JobFailure(
                _stream_error_message(stream_error),
                upstream_status=resp.status_code,
                upstream_body=body_preview(resp.content),
                error_class=_classify_stream_error(stream_error),
            )
        return await extract_candidates(parsed, client, budget=budget)

    text = resp.content.decode("utf-8", "replace")
    cache: dict[str, ImageCandidate] = {}
    events = parse_sse_json_objects(text)
    stream_error = _first_stream_error(events)
    if stream_error is not None:
        raise JobFailure(
            _stream_error_message(stream_error),
            upstream_status=resp.status_code,
            upstream_body=body_preview(resp.content),
            error_class=_classify_stream_error(stream_error),
        )
    has_terminal = any(
        isinstance(ev, dict)
        and not _is_responses_partial_event(ev)
        and _contains_result_key(ev)
        for ev in events
    )
    candidates: list[ImageCandidate] = []
    for obj in events:
        # When a stream contains a terminal event (response.completed or
        # image_generation_call.completed), drop partial-image previews so we
        # only keep the final image. If no terminal event was emitted, fall
        # back to whatever partial frames we have — better one image than none.
        if has_terminal and _is_responses_partial_event(obj):
            continue
        candidates.extend(
            await extract_candidates(
                obj,
                client,
                cache=cache,
                budget=budget,
            )
        )
    return candidates


async def extract_responses_stream_images(
    resp: httpx.Response,
    client: httpx.AsyncClient,
    *,
    job_id: str,
) -> list[ImageCandidate]:
    """Consume a /v1/responses SSE stream and return only final image candidates.

    Partial-image frames are progress previews. If the stream ends before a final
    image appears, treat it as a network-style interruption so the caller can
    retry or fail over instead of saving a partial preview as the result.

    image-stability-hardening §P0 加固：
    - 行级 idle timeout（``RESPONSES_STREAM_IDLE_TIMEOUT_S``，默认 60s）：上游 TCP
      仍活但流卡住的场景下，过去只能等 client 全局 timeout 才放手；现在 idle
      超过阈值即抛 retryable JobFailure，sidecar 资源（连接 / fd / 内存）立刻释放。
    - 显式跟踪 ``response.completed`` / ``response.done`` 作为成功 terminal：流
      结束但已收到成功 terminal + final image 时算正常完成；只缺 final 而 terminal
      已到的极少数兼容网关也能识别（避免错判 retryable）。
    """
    cache: dict[str, ImageCandidate] = {}
    budget = ImageCandidateBudget()
    event_lines: list[str] = []
    line_decoder = _SseLineDecoder()
    events_seen = 0
    bytes_seen = 0
    partial_candidates: list[ImageCandidate] = []
    final_candidates: list[ImageCandidate] = []
    saw_done = False
    saw_success_terminal = False
    last_touch = time.monotonic()

    async def handle_event(obj: Any) -> None:
        nonlocal partial_candidates, final_candidates, saw_success_terminal
        stream_error = _first_stream_error([obj])
        if stream_error is not None:
            raise JobFailure(
                _stream_error_message(stream_error),
                upstream_status=resp.status_code,
                upstream_body=stream_error,
                error_class=_classify_stream_error(stream_error),
            )
        if _is_responses_success_terminal(obj):
            saw_success_terminal = True
        extracted = await extract_candidates(
            obj,
            client,
            cache=cache,
            budget=budget,
        )
        if not extracted:
            return
        if _is_responses_partial_event(obj):
            partial_candidates.extend(extracted)
        else:
            final_candidates.extend(extracted)

    async def handle_line(line: str) -> None:
        nonlocal event_lines, events_seen, saw_done
        if line == "":
            data = _sse_data_from_lines(event_lines)
            event_lines = []
            obj = _try_parse_sse_data(data or "")
            if obj is None:
                if data and data.strip() == "[DONE]":
                    saw_done = True
                return
            events_seen += 1
            await handle_event(obj)
            return
        event_lines.append(line)

    byte_iter = resp.aiter_bytes()
    while True:
        try:
            chunk = await asyncio.wait_for(
                byte_iter.__anext__(), timeout=RESPONSES_STREAM_IDLE_TIMEOUT_S
            )
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            raise JobFailure(
                f"Responses stream idle for {RESPONSES_STREAM_IDLE_TIMEOUT_S:.0f}s",
                upstream_status=resp.status_code,
                upstream_body={
                    "events_seen": events_seen,
                    "partial_images_seen": len(partial_candidates),
                    "bytes_seen": bytes_seen,
                    "saw_success_terminal": saw_success_terminal,
                },
                retryable=True,
                retry_requires_idempotency=True,
                outcome_uncertain=True,
                error_class=ERROR_CLASS_NETWORK,
            ) from None
        if not chunk:
            continue
        next_bytes_seen = bytes_seen + len(chunk)
        if next_bytes_seen > RESPONSES_STREAM_MAX_BYTES:
            raise JobFailure(
                "Responses stream exceeded sidecar byte budget before final image",
                upstream_status=resp.status_code,
                retryable=True,
                retry_requires_idempotency=True,
                outcome_uncertain=True,
                error_class=ERROR_CLASS_NETWORK,
            )
        bytes_seen = next_bytes_seen
        now = time.monotonic()
        if now - last_touch >= JOB_HEARTBEAT_INTERVAL_S:
            await touch_running(job_id)
            last_touch = now

        for line in line_decoder.feed(chunk):
            await handle_line(line)

    for line in line_decoder.finish():
        await handle_line(line)
    if event_lines:
        await handle_line("")

    if final_candidates:
        return final_candidates

    # No final image. Do not save partial previews as "successful" output.
    detail = {
        "events_seen": events_seen,
        "partial_images_seen": len(partial_candidates),
        "saw_done": saw_done,
        "saw_success_terminal": saw_success_terminal,
        "bytes_seen": bytes_seen,
    }
    if partial_candidates:
        raise JobFailure(
            "Responses stream ended after partial images but before final image",
            upstream_status=resp.status_code,
            upstream_body=detail,
            retryable=True,
            retry_requires_idempotency=True,
            outcome_uncertain=True,
            error_class=ERROR_CLASS_NETWORK,
        )
    raise JobFailure(
        "Responses stream ended before returning an image",
        upstream_status=resp.status_code,
        upstream_body=detail,
        retryable=True,
        retry_requires_idempotency=True,
        outcome_uncertain=True,
        error_class=ERROR_CLASS_NETWORK,
    )


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


def _classify_httpx_error(exc: httpx.HTTPError) -> bool:
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
        ),
    )


def _httpx_error_requires_idempotency(exc: httpx.HTTPError) -> bool:
    return not isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
        ),
    )


def _is_retryable_job_failure(exc: JobFailure) -> bool:
    if exc.retryable:
        return True
    if exc.error_class == ERROR_CLASS_NETWORK:
        return True
    if exc.error_class == ERROR_CLASS_UPSTREAM_5XX:
        return True
    return False


def _mark_post_dispatch_failure(exc: JobFailure) -> JobFailure:
    if _is_retryable_job_failure(exc) and (
        exc.retry_requires_idempotency or exc.error_class == ERROR_CLASS_UPSTREAM_5XX
    ):
        exc.retry_requires_idempotency = True
        exc.outcome_uncertain = True
    return exc


def _retry_budget_for_failure(exc: JobFailure, *, endpoint: str) -> int:
    if exc.error_class == ERROR_CLASS_NETWORK and endpoint == "/v1/responses":
        return max(RETRY_NETWORK_MAX, RETRY_RESPONSES_STREAM_MAX)
    if exc.error_class == ERROR_CLASS_NETWORK:
        return RETRY_NETWORK_MAX
    if exc.error_class == ERROR_CLASS_UPSTREAM_5XX:
        return RETRY_UPSTREAM_5XX_MAX
    return 0


async def _raise_upstream_http_error(resp: httpx.Response) -> None:
    content, truncated, _received = await _read_response_body_bounded(
        resp,
        max_bytes=MAX_UPSTREAM_ERROR_BODY_BYTES,
        truncate=True,
    )
    upstream_body: Any = body_preview(content)
    if truncated:
        upstream_body = {
            "preview": upstream_body,
            "truncated": True,
        }
    is_5xx = resp.status_code >= 500
    raise JobFailure(
        f"上游返回 HTTP {resp.status_code}",
        upstream_status=resp.status_code,
        upstream_body=upstream_body,
        retryable=is_5xx,
        retry_requires_idempotency=is_5xx,
        outcome_uncertain=is_5xx,
        error_class=(ERROR_CLASS_UPSTREAM_5XX if is_5xx else ERROR_CLASS_UPSTREAM_4XX),
    )


async def _extract_non_stream_response_images(
    resp: httpx.Response,
    client: httpx.AsyncClient,
) -> list[ImageCandidate]:
    content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    is_direct_image = content_type.startswith("image/")
    body_limit = (
        min(MAX_UPSTREAM_RESPONSE_BYTES, MAX_IMAGE_BYTES)
        if is_direct_image
        else MAX_UPSTREAM_RESPONSE_BYTES
    )
    content, truncated, _received = await _read_response_body_bounded(
        resp,
        max_bytes=body_limit,
        truncate=False,
    )
    if truncated:
        limit_name = "单图" if is_direct_image else "非流式响应"
        raise JobFailure(
            f"上游{limit_name}超过大小限制（max {body_limit} bytes）",
            upstream_status=resp.status_code,
            retry_requires_idempotency=True,
            outcome_uncertain=True,
            error_class=ERROR_CLASS_IMAGE_SAVE,
        )
    buffered = httpx.Response(
        resp.status_code,
        headers=resp.headers,
        content=content,
    )
    return await extract_response_images(buffered, client)


async def _call_upstream_once(
    row: sqlite3.Row,
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    endpoint: str,
    image_edit_input_transport: str = "url",
) -> tuple[int, list[dict[str, Any]]]:
    assert _http_client is not None
    request_headers = headers
    request_kwargs: dict[str, Any]
    if endpoint == "/v1/images/edits" and image_edit_input_transport == "file":
        multipart_headers = dict(headers)
        multipart_headers.pop("Content-Type", None)
        data, files = await materialize_edit_input_files(_http_client, body)
        request_headers = multipart_headers
        request_kwargs = {
            "data": data,
            "files": files,
        }
    else:
        request_kwargs = {"json": body}

    async with _http_client.stream(
        "POST",
        url,
        headers=request_headers,
        **request_kwargs,
    ) as resp:
        status_code = resp.status_code
        if resp.status_code >= 400:
            await _raise_upstream_http_error(resp)

        content_type = resp.headers.get("content-type", "").lower()
        if endpoint == "/v1/responses" and "text/event-stream" in content_type:
            try:
                candidates = await extract_responses_stream_images(
                    resp,
                    _http_client,
                    job_id=row["job_id"],
                )
            except JobFailure as exc:
                raise _mark_post_dispatch_failure(exc)
            except httpx.HTTPError:
                raise
            except Exception as exc:
                raise JobFailure(
                    f"解析上游流式响应失败: {exc.__class__.__name__}: {exc}",
                    upstream_status=resp.status_code,
                    retry_requires_idempotency=True,
                    outcome_uncertain=True,
                    error_class=ERROR_CLASS_IMAGE_SAVE,
                ) from exc
        else:
            try:
                candidates = await _extract_non_stream_response_images(
                    resp,
                    _http_client,
                )
            except JobFailure as exc:
                raise _mark_post_dispatch_failure(exc)
            except httpx.HTTPError:
                raise
            except Exception as exc:
                raise JobFailure(
                    f"解析上游响应失败: {exc.__class__.__name__}: {exc}",
                    upstream_status=resp.status_code,
                    retry_requires_idempotency=True,
                    outcome_uncertain=True,
                    error_class=ERROR_CLASS_IMAGE_SAVE,
                ) from exc

    if not candidates:
        # Most common cause: caller asked /v1/images/generations against a
        # provider that only speaks /v1/responses (or vice versa). Surface
        # `no_image` so the caller can switch endpoint, not just provider.
        raise JobFailure(
            "上游没有返回可保存的图片",
            upstream_status=status_code,
            error_class=ERROR_CLASS_NO_IMAGE,
        )
    try:
        images = await save_images(
            row["job_id"], row["created_at"], row["retention_days"], candidates
        )
    except JobFailure:
        raise
    except Exception as exc:
        raise JobFailure(
            f"保存图片失败: {exc.__class__.__name__}: {exc}",
            upstream_status=status_code,
            error_class=ERROR_CLASS_IMAGE_SAVE,
        ) from exc
    if not images:
        raise JobFailure(
            "没有保存任何图片",
            upstream_status=status_code,
            error_class=ERROR_CLASS_IMAGE_SAVE,
        )
    return status_code, images


async def call_upstream(row: sqlite3.Row) -> tuple[int, list[dict[str, Any]]]:
    if _http_client is None:
        raise JobFailure("HTTP client not ready", error_class=ERROR_CLASS_INTERNAL)
    payload = parse_json_bytes(row["payload_json"].encode("utf-8"))
    if not isinstance(payload, dict):
        raise JobFailure(
            "job payload is not valid strict JSON",
            error_class=ERROR_CLASS_INTERNAL,
        )
    auth_header = row["auth_header"]
    if not auth_header:
        raise JobFailure(
            "job is missing Authorization header", error_class=ERROR_CLASS_INTERNAL
        )

    endpoint = payload["endpoint"]
    url = f"{UPSTREAM_BASE_URL}{endpoint}"
    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream, image/*",
        "Accept-Encoding": "identity",
        "Idempotency-Key": upstream_idempotency_key(row["job_id"]),
    }

    body = payload["body"]
    image_edit_input_transport = normalize_image_edit_input_transport(
        payload.get("image_edit_input_transport")
    )
    if endpoint == "/v1/images/edits" and isinstance(body, dict):
        if image_edit_input_transport == "url":
            body = await materialize_edit_input_urls(row, body)

    max_budget = max(
        RETRY_NETWORK_MAX, RETRY_RESPONSES_STREAM_MAX, RETRY_UPSTREAM_5XX_MAX
    )
    for attempt in range(max_budget + 1):
        try:
            return await _call_upstream_once(
                row,
                url=url,
                headers=headers,
                body=body,
                endpoint=endpoint,
                image_edit_input_transport=image_edit_input_transport,
            )
        except httpx.HTTPError as exc:
            requires_idempotency = _httpx_error_requires_idempotency(exc)
            failure = JobFailure(
                f"上游请求失败: {exc.__class__.__name__}: {exc}",
                retryable=_classify_httpx_error(exc),
                retry_requires_idempotency=requires_idempotency,
                outcome_uncertain=requires_idempotency,
                error_class=ERROR_CLASS_NETWORK,
            )
        except JobFailure as exc:
            failure = exc

        retry_budget = _retry_budget_for_failure(failure, endpoint=endpoint)
        retryable = _is_retryable_job_failure(failure)
        requires_idempotency = (
            failure.retry_requires_idempotency
            or failure.error_class == ERROR_CLASS_UPSTREAM_5XX
        )
        if retryable and requires_idempotency and not UPSTREAM_IDEMPOTENCY_GUARANTEED:
            failure.retry_suppressed = attempt < retry_budget
            if failure.retry_suppressed:
                LOG.warning(
                    "image job %s automatic retry suppressed endpoint=%s class=%s; "
                    "upstream idempotency is not guaranteed",
                    row["job_id"],
                    endpoint,
                    failure.error_class,
                )
            raise failure
        if attempt < retry_budget and retryable:
            LOG.warning(
                "image job %s upstream retryable failure, retry %d/%d endpoint=%s class=%s: %s",
                row["job_id"],
                attempt + 1,
                retry_budget,
                endpoint,
                failure.error_class,
                failure.error,
            )
            await asyncio.sleep(RETRY_BACKOFF_S * (2**attempt))
            continue
        raise failure


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
