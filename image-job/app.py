from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

import httpx
from fastapi import FastAPI, HTTPException, Request
from PIL import Image, UnidentifiedImageError


LOG = logging.getLogger("image-job")

UPSTREAM_BASE_URL = os.getenv("IMAGE_JOB_UPSTREAM_BASE_URL", "http://127.0.0.1:8081").rstrip("/")
PUBLIC_BASE_URL = os.getenv("IMAGE_JOB_PUBLIC_BASE_URL", "https://example.com").rstrip("/")
ROOT_DIR = Path(os.getenv("IMAGE_JOB_ROOT_DIR", "/opt/image-job"))
DATA_DIR = Path(os.getenv("IMAGE_JOB_DATA_DIR", str(ROOT_DIR / "data")))
# 参考图临时存储目录——由 /v1/refs 端点写入，nginx 静态暴露在 /refs/{token}.{ext}。
# 给 caller（如 lumen worker）一个把 reference 转 URL 的通道，避免 base64 内联到 Codex
# 请求里（4K 图 base64 ~7MB body 在跨地域 / 高延迟链路上易断流）。
REFS_DIR = DATA_DIR / "refs"
# Reference 上传单个文件大小上限——保守取 50MB（已 normalize 后的图通常 <10MB）。
MAX_REF_BYTES = int(os.getenv("IMAGE_JOB_MAX_REF_BYTES", str(50 * 1024 * 1024)))
# Reference 文件 retention 天数；与 jobs 复用 MAX_RETENTION_DAYS。文件 mtime < cutoff 的会被 sweeper 清。
STATE_DIR = Path(os.getenv("IMAGE_JOB_STATE_DIR", "/var/lib/image-job/state"))
DB_PATH = Path(os.getenv("IMAGE_JOB_DB_PATH", str(STATE_DIR / "image_jobs.sqlite3")))
CONCURRENCY = max(1, int(os.getenv("IMAGE_JOB_CONCURRENCY", "2")))
UPSTREAM_TIMEOUT_S = float(os.getenv("IMAGE_JOB_UPSTREAM_TIMEOUT_S", "1800"))
UPSTREAM_CONNECT_TIMEOUT_S = float(os.getenv("IMAGE_JOB_UPSTREAM_CONNECT_TIMEOUT_S", "5"))
MAX_IMAGE_BYTES = int(os.getenv("IMAGE_JOB_MAX_IMAGE_BYTES", str(80 * 1024 * 1024)))
QUEUE_MAX = max(1, int(os.getenv("IMAGE_JOB_QUEUE_MAX", "1000")))
RETRY_NETWORK_MAX = max(0, int(os.getenv("IMAGE_JOB_RETRY_NETWORK_MAX", "1")))
RETRY_BACKOFF_S = float(os.getenv("IMAGE_JOB_RETRY_BACKOFF_S", "2"))
RETRY_RESPONSES_STREAM_MAX = max(
    0,
    int(os.getenv("IMAGE_JOB_RETRY_RESPONSES_STREAM_MAX", str(RETRY_NETWORK_MAX))),
)
RETRY_UPSTREAM_5XX_MAX = max(0, int(os.getenv("IMAGE_JOB_RETRY_UPSTREAM_5XX_MAX", "1")))
RESPONSES_STRIP_PARTIAL_IMAGES = os.getenv(
    "IMAGE_JOB_RESPONSES_STRIP_PARTIAL_IMAGES", "1"
).strip().lower() not in {"0", "false", "no", "off"}
RESPONSES_STREAM_MAX_BYTES = int(
    os.getenv(
        "IMAGE_JOB_RESPONSES_STREAM_MAX_BYTES",
        str(max(MAX_IMAGE_BYTES * 2, 64 * 1024 * 1024)),
    )
)
JOB_HEARTBEAT_INTERVAL_S = max(
    5, int(os.getenv("IMAGE_JOB_HEARTBEAT_INTERVAL_S", "15"))
)
RETENTION_SWEEP_INTERVAL_S = max(60, int(os.getenv("IMAGE_JOB_RETENTION_SWEEP_INTERVAL_S", "3600")))
DEFAULT_RETENTION_DAYS = min(30, max(1, int(os.getenv("IMAGE_JOB_RETENTION_DAYS", "1"))))
MAX_RETENTION_DAYS = min(
    30, max(1, int(os.getenv("IMAGE_JOB_MAX_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))))
)
if DEFAULT_RETENTION_DAYS > MAX_RETENTION_DAYS:
    DEFAULT_RETENTION_DAYS = MAX_RETENTION_DAYS
JOB_TTL_DAYS = max(1, int(os.getenv("IMAGE_JOB_JOB_TTL_DAYS", "30")))
GRACEFUL_SHUTDOWN_S = max(0, int(os.getenv("IMAGE_JOB_GRACEFUL_SHUTDOWN_S", "60")))
HTTP_POOL_KEEPALIVE = max(1, int(os.getenv("IMAGE_JOB_HTTP_POOL_KEEPALIVE", "8")))
HTTP_POOL_MAX = max(HTTP_POOL_KEEPALIVE, int(os.getenv("IMAGE_JOB_HTTP_POOL_MAX", "32")))
SQLITE_JOURNAL_MODE = os.getenv("IMAGE_JOB_SQLITE_JOURNAL_MODE", "WAL").strip().upper()
STUCK_RECONCILE_INTERVAL_S = max(
    15, int(os.getenv("IMAGE_JOB_STUCK_RECONCILE_INTERVAL_S", "60"))
)
STUCK_QUEUED_AFTER_S = max(30, int(os.getenv("IMAGE_JOB_STUCK_QUEUED_AFTER_S", "120")))
STUCK_RUNNING_AFTER_S = max(60, int(os.getenv("IMAGE_JOB_STUCK_RUNNING_AFTER_S", "300")))
STUCK_RECONCILE_BATCH = max(1, int(os.getenv("IMAGE_JOB_STUCK_RECONCILE_BATCH", "100")))

ALLOWED_FIXED_ENDPOINTS = (
    "/v1/images/generations",
    "/v1/images/edits",
    "/v1/responses",
)
ALLOWED_PREFIX_ENDPOINTS = ("/v1beta/models/",)

IMAGE_OUTPUT_FORMATS = {"png", "jpeg", "webp"}
DEFAULT_IMAGE_OUTPUT_FORMAT = "jpeg"
DEFAULT_IMAGE_OUTPUT_COMPRESSION = 0


# Error classification — emitted to DB and exposed in the failed-job response
# so the caller can decide between "switch endpoint" and "switch provider".
ERROR_CLASS_NETWORK = "network"            # connect/read/timeout — switch provider
ERROR_CLASS_UPSTREAM_4XX = "upstream_4xx"  # 4xx HTTP — switch endpoint (likely format mismatch)
ERROR_CLASS_UPSTREAM_5XX = "upstream_5xx"  # 5xx HTTP — switch provider
ERROR_CLASS_NO_IMAGE = "no_image"          # 200 but no image extractable — switch endpoint
ERROR_CLASS_IMAGE_SAVE = "image_save"      # save/decode failure — switch provider
ERROR_CLASS_INTERNAL = "internal"          # sidecar bug — switch provider
ERROR_CLASS_VALIDATION = "validation"      # bad input from caller — terminal


@dataclass
class ImageCandidate:
    data: bytes
    mime_type: str | None = None


class JobFailure(Exception):
    def __init__(
        self,
        error: str,
        *,
        upstream_status: int | None = None,
        upstream_body: Any | None = None,
        retryable: bool = False,
        error_class: str = ERROR_CLASS_INTERNAL,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.upstream_status = upstream_status
        self.upstream_body = upstream_body
        self.retryable = retryable
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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat()


def auth_hash(auth_header: str) -> str:
    return hashlib.sha256(auth_header.encode("utf-8")).hexdigest()


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# --- SQLite layer ------------------------------------------------------------
#
# Each call opens a fresh connection but applies tuning PRAGMAs. The SQLite
# state DB must live on local disk; CIFS/NAS mounts can split WAL files and make
# completed jobs appear stuck as queued/running. Every DB call is dispatched via
# ``asyncio.to_thread`` so writes never block the event loop.

_ALLOWED_SQLITE_JOURNAL_MODES = {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}
if SQLITE_JOURNAL_MODE not in _ALLOWED_SQLITE_JOURNAL_MODES:
    SQLITE_JOURNAL_MODE = "WAL"

_DB_TUNING_PRAGMAS = (
    f"PRAGMA journal_mode = {SQLITE_JOURNAL_MODE}",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA mmap_size = 67108864",
    "PRAGMA cache_size = -16384",
    "PRAGMA busy_timeout = 5000",
)


def _open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _DB_TUNING_PRAGMAS:
        conn.execute(pragma)
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, decl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_storage_sync() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _open_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                auth_hash TEXT NOT NULL,
                auth_header TEXT,
                request_type TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                relay_url TEXT NOT NULL,
                retention_days INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                elapsed_ms INTEGER,
                upstream_status INTEGER,
                image_count INTEGER NOT NULL DEFAULT 0,
                images_json TEXT,
                error TEXT,
                upstream_body TEXT
            );
            CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status);
            CREATE INDEX IF NOT EXISTS jobs_created_idx ON jobs(created_at);
            CREATE INDEX IF NOT EXISTS jobs_finished_idx ON jobs(finished_at);
            -- refs 表：sha256 → token 映射用于去重；同 sha 第二次上传直接复用已有 URL。
            -- 写盘 + 行落库要原子（不然 sweeper 可能清掉文件而 DB 行仍存在 → 复用时拿到失效 URL）。
            CREATE TABLE IF NOT EXISTS refs (
                sha256 TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                ext TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS refs_created_idx ON refs(created_at);
            """
        )
        _ensure_column(conn, "jobs", "attempts", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "jobs", "error_class", "TEXT")
        _ensure_column(conn, "jobs", "endpoint_used", "TEXT")
    finally:
        conn.close()


def _db_one_sync(sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
    conn = _open_conn()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _db_all_sync(sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    conn = _open_conn()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _db_exec_sync(sql: str, params: tuple[Any, ...]) -> int:
    conn = _open_conn()
    try:
        cur = conn.execute(sql, params)
        return cur.rowcount
    finally:
        conn.close()


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


# --- Validation --------------------------------------------------------------


def parse_json_bytes(data: bytes) -> Any | None:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def body_preview(data: bytes, limit: int = 20000) -> Any:
    parsed = parse_json_bytes(data)
    if parsed is not None:
        return parsed
    text = data.decode("utf-8", "replace")
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


def require_auth(request: Request) -> str:
    auth = request.headers.get("authorization", "").strip()
    if not auth.lower().startswith("bearer ") or len(auth) <= len("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer token")
    return auth


def normalize_endpoint(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise HTTPException(status_code=400, detail="endpoint must be an absolute API path")
    if "://" in value or ".." in value:
        raise HTTPException(status_code=400, detail="invalid endpoint")
    if value in ALLOWED_FIXED_ENDPOINTS:
        return value
    if any(value.startswith(prefix) for prefix in ALLOWED_PREFIX_ENDPOINTS):
        return value
    raise HTTPException(status_code=400, detail="unsupported image endpoint")


def infer_request_type(endpoint: str) -> str:
    if endpoint == "/v1/images/generations":
        return "generations"
    if endpoint == "/v1/images/edits":
        return "edits"
    if endpoint == "/v1/responses":
        return "responses"
    if endpoint.startswith("/v1beta/models/"):
        return "gemini"
    return "image"


def normalize_image_output_options(target: dict[str, Any]) -> None:
    """Fill image-generation output defaults without overriding explicit choices."""
    background = target.get("background")
    if background == "transparent":
        target["output_format"] = "png"
        target.pop("output_compression", None)
        return

    output_format = target.get("output_format")
    if output_format not in IMAGE_OUTPUT_FORMATS:
        output_format = DEFAULT_IMAGE_OUTPUT_FORMAT
        target["output_format"] = output_format

    if output_format in {"jpeg", "webp"} and target.get("output_compression") is None:
        target["output_compression"] = DEFAULT_IMAGE_OUTPUT_COMPRESSION
    elif output_format == "png":
        target.pop("output_compression", None)

    if target.get("background") not in {"auto", "opaque", "transparent"}:
        target["background"] = "auto"
    if target.get("moderation") not in {"auto", "low"}:
        target["moderation"] = "low"


def normalize_payload_body(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
    """Normalize sidecar-submitted image request bodies to the caller's JPEG default."""
    normalized = copy.deepcopy(body)
    if endpoint in {"/v1/images/generations", "/v1/images/edits"}:
        normalize_image_output_options(normalized)
    elif endpoint == "/v1/responses":
        tools = normalized.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict) and tool.get("type") == "image_generation":
                    normalize_image_output_options(tool)
                    if RESPONSES_STRIP_PARTIAL_IMAGES:
                        tool.pop("partial_images", None)
    return normalized


def validate_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    endpoint = normalize_endpoint(payload.get("endpoint"))
    body = payload.get("body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    body = normalize_payload_body(endpoint, body)
    request_type = payload.get("request_type") or infer_request_type(endpoint)
    if not isinstance(request_type, str) or not request_type:
        raise HTTPException(status_code=400, detail="request_type must be a string")
    try:
        retention_days = int(payload.get("retention_days", DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="retention_days must be an integer") from None
    if retention_days < 1 or retention_days > MAX_RETENTION_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"retention_days must be between 1 and {MAX_RETENTION_DAYS}",
        )
    return {
        "request_type": request_type,
        "endpoint": endpoint,
        "body": body,
        "retention_days": retention_days,
    }


def make_job_id() -> str:
    return f"img_{utc_now().strftime('%Y%m%d')}_{secrets.token_hex(5)}"


# --- Job CRUD ----------------------------------------------------------------


async def insert_job(job_id: str, payload: dict[str, Any], auth_header: str) -> None:
    now = iso()
    await db_exec(
        """
        INSERT INTO jobs (
            job_id, auth_hash, auth_header, request_type, endpoint, payload_json,
            status, relay_url, retention_days, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
        """,
        (
            job_id,
            auth_hash(auth_header),
            auth_header,
            payload["request_type"],
            payload["endpoint"],
            json_dump(payload),
            UPSTREAM_BASE_URL,
            payload["retention_days"],
            now,
            now,
        ),
    )


async def mark_running(job_id: str) -> None:
    now = iso()
    await db_exec(
        "UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?), "
        "updated_at = ?, attempts = attempts + 1 WHERE job_id = ?",
        (now, now, job_id),
    )


async def touch_running(job_id: str) -> None:
    await db_exec(
        "UPDATE jobs SET updated_at = ? WHERE job_id = ? AND status = 'running'",
        (iso(), job_id),
    )


async def mark_succeeded(
    job_id: str,
    *,
    upstream_status: int,
    elapsed_ms: int,
    images: list[dict[str, Any]],
    endpoint_used: str | None = None,
) -> None:
    now = iso()
    await db_exec(
        """
        UPDATE jobs
        SET status = 'succeeded', auth_header = NULL, finished_at = ?, updated_at = ?,
            elapsed_ms = ?, upstream_status = ?, image_count = ?, images_json = ?,
            error = NULL, upstream_body = NULL, error_class = NULL,
            endpoint_used = COALESCE(?, endpoint_used)
        WHERE job_id = ?
        """,
        (
            now,
            now,
            elapsed_ms,
            upstream_status,
            len(images),
            json_dump(images),
            endpoint_used,
            job_id,
        ),
    )


async def mark_failed(
    job_id: str,
    *,
    error: str,
    upstream_status: int | None = None,
    upstream_body: Any | None = None,
    elapsed_ms: int | None = None,
    error_class: str = ERROR_CLASS_INTERNAL,
    endpoint_used: str | None = None,
) -> None:
    now = iso()
    await db_exec(
        """
        UPDATE jobs
        SET status = 'failed', auth_header = NULL, finished_at = ?, updated_at = ?,
            elapsed_ms = ?, upstream_status = ?, error = ?, upstream_body = ?,
            error_class = ?, endpoint_used = COALESCE(?, endpoint_used)
        WHERE job_id = ?
        """,
        (
            now,
            now,
            elapsed_ms,
            upstream_status,
            error,
            json_dump(upstream_body) if upstream_body is not None else None,
            error_class,
            endpoint_used,
            job_id,
        ),
    )


async def fail_interrupted_running_jobs() -> None:
    now = iso()
    await db_exec(
        """
        UPDATE jobs
        SET status = 'failed', auth_header = NULL, finished_at = ?, updated_at = ?,
            error = 'image job worker restarted before completion',
            error_class = ?
        WHERE status = 'running'
        """,
        (now, now, ERROR_CLASS_INTERNAL),
    )


def row_to_response(row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "job_id": row["job_id"],
        "status": row["status"],
        "request_type": row["request_type"],
        "endpoint": row["endpoint"],
        "relay_url": row["relay_url"],
        "retention_days": row["retention_days"],
    }
    endpoint_used = _row_get(row, "endpoint_used")
    if endpoint_used:
        payload["endpoint_used"] = endpoint_used
    if row["status"] == "succeeded":
        payload.update(
            {
                "upstream_status": row["upstream_status"],
                "elapsed_ms": row["elapsed_ms"],
                "image_count": row["image_count"],
                "images": json.loads(row["images_json"] or "[]"),
            }
        )
    elif row["status"] == "failed":
        upstream_body: Any = None
        if row["upstream_body"]:
            try:
                upstream_body = json.loads(row["upstream_body"])
            except json.JSONDecodeError:
                upstream_body = row["upstream_body"]
        payload.update(
            {
                "upstream_status": row["upstream_status"],
                "elapsed_ms": row["elapsed_ms"],
                "error": row["error"],
                "error_class": _row_get(row, "error_class") or ERROR_CLASS_INTERNAL,
                "upstream_body": upstream_body,
            }
        )
    return payload


def _row_get(row: sqlite3.Row, key: str) -> Any:
    """Tolerant accessor — older rows written before a column existed lack it."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


# --- Image decoding / extraction --------------------------------------------

_IMAGE_SIGNATURES: tuple[bytes, ...] = (
    b"\xff\xd8\xff",                # JPEG
    b"\x89PNG\r\n\x1a\n",           # PNG
    b"GIF87a",
    b"GIF89a",
    b"RIFF",                        # WEBP container starts with RIFF
    b"BM",                          # BMP (rare but harmless)
    b"\x00\x00\x00\x0cftypheic",    # HEIC (offset 0)
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


def decode_data_url(value: str) -> ImageCandidate | None:
    if not value.startswith("data:image/") or "," not in value:
        return None
    header, encoded = value.split(",", 1)
    mime_type = header.removeprefix("data:").split(";", 1)[0]
    is_b64 = ";base64" in header
    if is_b64:
        data = _b64_decode(encoded)
        if data is None:
            return None
    else:
        data = encoded.encode("utf-8", "replace")
    if len(data) > MAX_IMAGE_BYTES:
        raise JobFailure("上游图片超过大小限制", error_class=ERROR_CLASS_IMAGE_SAVE)
    if not looks_like_image(data):
        return None
    return ImageCandidate(data, mime_type)


def _b64_decode(value: str) -> bytes | None:
    compact = "".join(value.split())
    if not compact:
        return None
    pad = len(compact) % 4
    if pad:
        compact += "=" * (4 - pad)
    try:
        return base64.b64decode(compact, validate=True)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return None


def decode_base64(value: str) -> bytes | None:
    value = value.strip()
    if not value:
        return None
    if value.startswith("data:image/"):
        candidate = decode_data_url(value)
        return candidate.data if candidate else None
    data = _b64_decode(value)
    if data is None:
        return None
    if len(data) > MAX_IMAGE_BYTES:
        raise JobFailure("上游图片超过大小限制", error_class=ERROR_CLASS_IMAGE_SAVE)
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


def _is_responses_partial_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    event_type = str(event.get("type", ""))
    return _RESPONSES_PARTIAL_TYPE_HINT in event_type


async def download_image_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    cache: dict[str, ImageCandidate],
) -> ImageCandidate | None:
    if url.startswith("data:image/"):
        return decode_data_url(url)
    if not (url.startswith("http://") or url.startswith("https://")):
        return None
    cached = cache.get(url)
    if cached is not None:
        return cached
    try:
        resp = await client.get(url, timeout=httpx.Timeout(60.0, connect=UPSTREAM_CONNECT_TIMEOUT_S))
    except httpx.HTTPError as exc:
        raise JobFailure(
            f"下载上游图片失败: {exc.__class__.__name__}: {exc}",
            retryable=True,
            error_class=ERROR_CLASS_NETWORK,
        ) from exc
    if not resp.is_success:
        ec = ERROR_CLASS_UPSTREAM_5XX if resp.status_code >= 500 else ERROR_CLASS_UPSTREAM_4XX
        raise JobFailure(
            f"下载上游图片失败 HTTP {resp.status_code}",
            upstream_status=resp.status_code,
            upstream_body=body_preview(resp.content),
            error_class=ec,
        )
    if len(resp.content) > MAX_IMAGE_BYTES:
        raise JobFailure(
            "上游图片超过大小限制",
            upstream_status=resp.status_code,
            error_class=ERROR_CLASS_IMAGE_SAVE,
        )
    candidate = ImageCandidate(resp.content, resp.headers.get("content-type"))
    cache[url] = candidate
    return candidate


async def extract_candidates(
    value: Any,
    client: httpx.AsyncClient,
    *,
    image_context: bool = False,
    cache: dict[str, ImageCandidate] | None = None,
) -> list[ImageCandidate]:
    if cache is None:
        cache = {}
    candidates: list[ImageCandidate] = []
    if isinstance(value, list):
        for item in value:
            candidates.extend(
                await extract_candidates(item, client, image_context=image_context, cache=cache)
            )
        return candidates
    if not isinstance(value, dict):
        return candidates

    context = image_context or object_image_context(value)

    for inline_key in ("inlineData", "inline_data"):
        inline = value.get(inline_key)
        if isinstance(inline, dict) and isinstance(inline.get("data"), str):
            data = decode_base64(inline["data"])
            if data is not None:
                candidates.append(
                    ImageCandidate(data, inline.get("mimeType") or inline.get("mime_type"))
                )

    for key, item in value.items():
        if isinstance(item, str):
            if key in {"b64_json", "image_b64", "image_base64", "base64_image", "partial_image_b64"}:
                data = decode_base64(item)
                if data is not None:
                    candidates.append(
                        ImageCandidate(data, value.get("mimeType") or value.get("mime_type"))
                    )
            elif key in {"result", "data"} and context:
                data = decode_base64(item)
                if data is not None:
                    candidates.append(
                        ImageCandidate(data, value.get("mimeType") or value.get("mime_type"))
                    )
            elif key in {"url", "image_url"}:
                downloaded = await download_image_url(client, item, cache=cache)
                if downloaded is not None:
                    candidates.append(downloaded)
        elif isinstance(item, (dict, list)):
            candidates.extend(
                await extract_candidates(item, client, image_context=context, cache=cache)
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
        try:
            objects.append(json.loads(data))
        except json.JSONDecodeError:
            continue
    return objects


def _try_parse_sse_data(data: str) -> Any | None:
    data = data.strip()
    if not data or data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


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
    return None


def _classify_stream_error(error: dict[str, Any]) -> str:
    code = str(error.get("code") or "").lower()
    error_type = str(error.get("type") or "").lower()
    message = str(error.get("message") or "").lower()
    joined = " ".join((code, error_type, message))
    if "moderation" in joined or "safety" in joined or error_type.endswith("_user_error"):
        return ERROR_CLASS_VALIDATION
    if "invalid" in joined or "bad_request" in joined or "bad request" in joined:
        return ERROR_CLASS_UPSTREAM_4XX
    return ERROR_CLASS_UPSTREAM_5XX


def _stream_error_message(error: dict[str, Any]) -> str:
    code = str(error.get("code") or error.get("type") or "stream_error")
    message = str(error.get("message") or "Responses stream failed before returning an image")
    return f"上游流式错误 {code}: {message}"


async def extract_response_images(
    resp: httpx.Response, client: httpx.AsyncClient
) -> list[ImageCandidate]:
    content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type.startswith("image/"):
        return [ImageCandidate(resp.content, content_type)]

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
        return await extract_candidates(parsed, client)

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
        isinstance(ev, dict) and not _is_responses_partial_event(ev)
        and "result" in json.dumps(ev)  # cheap check: a terminal event normally carries result
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
        candidates.extend(await extract_candidates(obj, client, cache=cache))
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
    """
    cache: dict[str, ImageCandidate] = {}
    event_lines: list[str] = []
    events_seen = 0
    bytes_seen = 0
    partial_candidates: list[ImageCandidate] = []
    final_candidates: list[ImageCandidate] = []
    saw_done = False
    last_touch = time.monotonic()

    async def handle_event(obj: Any) -> None:
        nonlocal partial_candidates, final_candidates
        stream_error = _first_stream_error([obj])
        if stream_error is not None:
            raise JobFailure(
                _stream_error_message(stream_error),
                upstream_status=resp.status_code,
                upstream_body=stream_error,
                error_class=_classify_stream_error(stream_error),
            )
        extracted = await extract_candidates(obj, client, cache=cache)
        if not extracted:
            return
        if _is_responses_partial_event(obj):
            partial_candidates.extend(extracted)
        else:
            final_candidates.extend(extracted)

    async for line in resp.aiter_lines():
        bytes_seen += len(line) + 1
        if bytes_seen > RESPONSES_STREAM_MAX_BYTES:
            raise JobFailure(
                "Responses stream exceeded sidecar byte budget before final image",
                upstream_status=resp.status_code,
                error_class=ERROR_CLASS_NETWORK,
            )
        now = time.monotonic()
        if now - last_touch >= JOB_HEARTBEAT_INTERVAL_S:
            await touch_running(job_id)
            last_touch = now

        if line == "":
            data = _sse_data_from_lines(event_lines)
            event_lines = []
            obj = _try_parse_sse_data(data or "")
            if obj is None:
                if data and data.strip() == "[DONE]":
                    saw_done = True
                continue
            events_seen += 1
            await handle_event(obj)
            continue
        event_lines.append(line)

    if event_lines:
        data = _sse_data_from_lines(event_lines)
        obj = _try_parse_sse_data(data or "")
        if obj is not None:
            events_seen += 1
            await handle_event(obj)
        elif data and data.strip() == "[DONE]":
            saw_done = True

    if final_candidates:
        return final_candidates

    # No final image. Do not save partial previews as "successful" output.
    detail = {
        "events_seen": events_seen,
        "partial_images_seen": len(partial_candidates),
        "saw_done": saw_done,
        "bytes_seen": bytes_seen,
    }
    if partial_candidates:
        raise JobFailure(
            "Responses stream ended after partial images but before final image",
            upstream_status=resp.status_code,
            upstream_body=detail,
            retryable=True,
            error_class=ERROR_CLASS_NETWORK,
        )
    raise JobFailure(
        "Responses stream ended before returning an image",
        upstream_status=resp.status_code,
        upstream_body=detail,
        retryable=True,
        error_class=ERROR_CLASS_NETWORK,
    )


def image_metadata(data: bytes, mime_type: str | None) -> tuple[int | None, int | None, str]:
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            fmt = (image.format or "").lower()
    except (UnidentifiedImageError, OSError):
        width = height = None
        fmt = ""

    if fmt in {"jpg", "jpeg"}:
        return width, height, "jpeg"
    if fmt in {"png", "webp", "gif"}:
        return width, height, fmt

    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime == "image/jpeg":
        return width, height, "jpeg"
    if mime == "image/png":
        return width, height, "png"
    if mime == "image/webp":
        return width, height, "webp"
    if mime == "image/gif":
        return width, height, "gif"
    return width, height, "bin"


def job_image_dir(job_id: str, created_at: str) -> tuple[Path, str]:
    created = datetime.fromisoformat(created_at)
    rel = (
        Path("images")
        / "temp"
        / created.strftime("%Y")
        / created.strftime("%m")
        / created.strftime("%d")
        / job_id
    )
    return DATA_DIR / rel, rel.as_posix()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp-{secrets.token_hex(4)}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def _save_one_image_sync(image_dir: Path, filename: str, data: bytes) -> None:
    image_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(image_dir / filename, data)


async def save_images(
    job_id: str,
    created_at: str,
    retention_days: int,
    candidates: Iterable[ImageCandidate],
) -> list[dict[str, Any]]:
    image_dir, rel_dir = job_image_dir(job_id, created_at)
    expires_at = (datetime.fromisoformat(created_at) + timedelta(days=retention_days)).isoformat()

    seen: set[str] = set()
    plan: list[tuple[str, ImageCandidate, int, int | None, int | None, str]] = []
    for candidate in candidates:
        digest = hashlib.sha256(candidate.data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        width, height, fmt = await asyncio.to_thread(
            image_metadata, candidate.data, candidate.mime_type
        )
        index = len(plan) + 1
        filename = f"image-{index}.{fmt}"
        plan.append((filename, candidate, len(candidate.data), width, height, fmt))

    await asyncio.gather(
        *(
            asyncio.to_thread(_save_one_image_sync, image_dir, filename, candidate.data)
            for filename, candidate, _, _, _, _ in plan
        )
    )

    return [
        {
            "url": f"{PUBLIC_BASE_URL}/{rel_dir}/{filename}",
            "width": width,
            "height": height,
            "bytes": size,
            "format": fmt,
            "expires_at": expires_at,
        }
        for filename, _, size, width, height, fmt in plan
    ]


async def save_input_image(
    job_id: str,
    created_at: str,
    retention_days: int,
    candidate: ImageCandidate,
    *,
    stem: str,
) -> str:
    image_dir, rel_dir = job_image_dir(job_id, created_at)
    width, height, fmt = await asyncio.to_thread(
        image_metadata, candidate.data, candidate.mime_type
    )
    if width is None or height is None or fmt == "bin":
        raise JobFailure("图生图输入不是可识别的图片", upstream_status=400)
    filename = f"{stem}.{fmt}"
    await asyncio.to_thread(_save_one_image_sync, image_dir, filename, candidate.data)
    return f"{PUBLIC_BASE_URL}/{rel_dir}/{filename}"


def image_candidate_from_ref(ref: dict[str, Any]) -> ImageCandidate | None:
    url = ref.get("image_url")
    if isinstance(url, str) and url.startswith("data:image/"):
        return decode_data_url(url)
    for key in ("b64_json", "image_b64", "image_base64", "base64_image", "data"):
        value = ref.get(key)
        if isinstance(value, str):
            data = decode_base64(value)
            if data is not None:
                return ImageCandidate(data, ref.get("mimeType") or ref.get("mime_type"))
    return None


async def materialize_edit_input_urls(row: sqlite3.Row, body: dict[str, Any]) -> dict[str, Any]:
    rewritten = copy.deepcopy(body)
    images = rewritten.get("images")
    if isinstance(images, list):
        for index, item in enumerate(images, start=1):
            if not isinstance(item, dict):
                continue
            url = item.get("image_url")
            if isinstance(url, str) and (url.startswith("http://") or url.startswith("https://")):
                continue
            candidate = image_candidate_from_ref(item)
            if candidate is None:
                continue
            new_url = await save_input_image(
                row["job_id"],
                row["created_at"],
                row["retention_days"],
                candidate,
                stem=f"input-{index}",
            )
            item.clear()
            item["image_url"] = new_url

    mask = rewritten.get("mask")
    if isinstance(mask, dict):
        url = mask.get("image_url")
        if not (isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))):
            candidate = image_candidate_from_ref(mask)
            if candidate is not None:
                new_url = await save_input_image(
                    row["job_id"],
                    row["created_at"],
                    row["retention_days"],
                    candidate,
                    stem="mask",
                )
                mask.clear()
                mask["image_url"] = new_url
    return rewritten


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


def _is_retryable_job_failure(exc: JobFailure) -> bool:
    if exc.retryable:
        return True
    if exc.error_class == ERROR_CLASS_NETWORK:
        return True
    if exc.error_class == ERROR_CLASS_UPSTREAM_5XX:
        return True
    return False


def _retry_budget_for_failure(exc: JobFailure, *, endpoint: str) -> int:
    if exc.error_class == ERROR_CLASS_NETWORK and endpoint == "/v1/responses":
        return max(RETRY_NETWORK_MAX, RETRY_RESPONSES_STREAM_MAX)
    if exc.error_class == ERROR_CLASS_NETWORK:
        return RETRY_NETWORK_MAX
    if exc.error_class == ERROR_CLASS_UPSTREAM_5XX:
        return RETRY_UPSTREAM_5XX_MAX
    return 0


async def _extract_non_stream_response_images(
    resp: httpx.Response,
    client: httpx.AsyncClient,
) -> list[ImageCandidate]:
    content = await resp.aread()
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
) -> tuple[int, list[dict[str, Any]]]:
    assert _http_client is not None
    if endpoint == "/v1/responses":
        async with _http_client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                content = await resp.aread()
                ec = ERROR_CLASS_UPSTREAM_5XX if resp.status_code >= 500 else ERROR_CLASS_UPSTREAM_4XX
                raise JobFailure(
                    f"上游返回 HTTP {resp.status_code}",
                    upstream_status=resp.status_code,
                    upstream_body=body_preview(content),
                    error_class=ec,
                )
            content_type = resp.headers.get("content-type", "").lower()
            if "text/event-stream" in content_type:
                try:
                    candidates = await extract_responses_stream_images(
                        resp,
                        _http_client,
                        job_id=row["job_id"],
                    )
                except (JobFailure, httpx.HTTPError):
                    raise
                except Exception as exc:
                    raise JobFailure(
                        f"解析上游流式响应失败: {exc.__class__.__name__}: {exc}",
                        upstream_status=resp.status_code,
                        error_class=ERROR_CLASS_IMAGE_SAVE,
                    ) from exc
            else:
                try:
                    candidates = await _extract_non_stream_response_images(resp, _http_client)
                except (JobFailure, httpx.HTTPError):
                    raise
                except Exception as exc:
                    raise JobFailure(
                        f"解析上游响应失败: {exc.__class__.__name__}: {exc}",
                        upstream_status=resp.status_code,
                        error_class=ERROR_CLASS_IMAGE_SAVE,
                    ) from exc
        status_code = resp.status_code
    else:
        resp = await _http_client.post(url, headers=headers, json=body)
        status_code = resp.status_code
        if resp.status_code >= 400:
            ec = ERROR_CLASS_UPSTREAM_5XX if resp.status_code >= 500 else ERROR_CLASS_UPSTREAM_4XX
            raise JobFailure(
                f"上游返回 HTTP {resp.status_code}",
                upstream_status=resp.status_code,
                upstream_body=body_preview(resp.content),
                error_class=ec,
            )
        try:
            candidates = await extract_response_images(resp, _http_client)
        except (JobFailure, httpx.HTTPError):
            raise
        except Exception as exc:
            raise JobFailure(
                f"解析上游响应失败: {exc.__class__.__name__}: {exc}",
                upstream_status=resp.status_code,
                upstream_body=body_preview(resp.content),
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
    payload = json.loads(row["payload_json"])
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
    }

    body = payload["body"]
    if endpoint == "/v1/images/edits" and isinstance(body, dict):
        body = await materialize_edit_input_urls(row, body)

    last_failure: JobFailure | None = None
    max_budget = max(RETRY_NETWORK_MAX, RETRY_RESPONSES_STREAM_MAX, RETRY_UPSTREAM_5XX_MAX)
    for attempt in range(max_budget + 1):
        try:
            return await _call_upstream_once(
                row,
                url=url,
                headers=headers,
                body=body,
                endpoint=endpoint,
            )
        except httpx.HTTPError as exc:
            failure = JobFailure(
                f"上游请求失败: {exc.__class__.__name__}: {exc}",
                retryable=_classify_httpx_error(exc),
                error_class=ERROR_CLASS_NETWORK,
            )
        except JobFailure as exc:
            failure = exc

        last_failure = failure
        retry_budget = _retry_budget_for_failure(failure, endpoint=endpoint)
        if attempt < retry_budget and _is_retryable_job_failure(failure):
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

    if last_failure is not None:
        raise last_failure
    raise JobFailure("upstream retry loop exited unexpectedly", error_class=ERROR_CLASS_INTERNAL)


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
    if row is None or row["status"] not in {"queued", "running"}:
        return

    LOG.info("starting image job %s endpoint=%s", job_id, row["endpoint"])
    await mark_running(job_id)
    started = time.monotonic()
    endpoint_used = row["endpoint"]
    heartbeat = asyncio.create_task(running_heartbeat(job_id), name=f"image-job-heartbeat-{job_id}")
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
        )
        LOG.warning(
            "image job %s failed endpoint=%s class=%s: %s",
            job_id,
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
                LOG.exception("worker %d unexpected error processing %s", worker_id, job_id)
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

        async with _queue_state_lock:
            if job_id in _queued_ids or job_id in _inflight:
                stats["active"] += 1
                continue
            if _queue.full():
                stats["queue_full"] += 1
                continue
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
                await asyncio.wait_for(_shutdown.wait(), timeout=STUCK_RECONCILE_INTERVAL_S)
                break
            except asyncio.TimeoutError:
                pass
            try:
                stats = await reconcile_stuck_jobs()
                requeued = stats["queued_requeued"] + stats["running_requeued"]
                if requeued or stats["failed_missing_auth"] or stats["queue_full"]:
                    LOG.warning("stuck reconciler repaired jobs stats=%s", stats)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("stuck reconciler iteration failed")
    except asyncio.CancelledError:
        raise


def _sweep_dir_sync(base: Path, cutoff_ts: float) -> tuple[int, int]:
    """单个目录的 mtime-based 清理；返回 (删除文件数, 释放字节)。"""
    if not base.exists():
        return 0, 0
    removed_files = 0
    removed_bytes = 0
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if stat.st_mtime < cutoff_ts:
            try:
                size = stat.st_size
                path.unlink()
                removed_files += 1
                removed_bytes += size
            except OSError:
                continue
    # Drop empty directories bottom-up.
    for path in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                continue
    return removed_files, removed_bytes


def _sweep_filesystem_sync(cutoff_ts: float) -> tuple[int, int]:
    """清两条数据通道：generated images 和 reference 临时文件。
    refs 目录额外清 sqlite 行（基于 cutoff），避免 DB/FS 漂移导致复用拿到失效 URL。
    """
    total_files = 0
    total_bytes = 0
    for base in (DATA_DIR / "images" / "temp", REFS_DIR):
        f, b = _sweep_dir_sync(base, cutoff_ts)
        total_files += f
        total_bytes += b
    # 清理 refs 表里 created_at < cutoff 的行（与 FS 保持一致；缺行没事，多行只会让下次复用 miss）。
    cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()
    try:
        _db_exec_sync("DELETE FROM refs WHERE created_at < ?", (cutoff_iso,))
    except sqlite3.OperationalError:
        # refs 表不存在（旧 db 升级前）→ 忽略，下次 init_storage_sync 会建好。
        pass
    return total_files, total_bytes


async def retention_sweeper() -> None:
    LOG.info(
        "retention sweeper started interval=%ss job_ttl_days=%d",
        RETENTION_SWEEP_INTERVAL_S,
        JOB_TTL_DAYS,
    )
    try:
        while not _shutdown.is_set():
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=RETENTION_SWEEP_INTERVAL_S)
                break
            except asyncio.TimeoutError:
                pass
            try:
                cutoff = utc_now() - timedelta(days=MAX_RETENTION_DAYS)
                files, freed = await asyncio.to_thread(
                    _sweep_filesystem_sync, cutoff.timestamp()
                )
                if files:
                    LOG.info("retention sweeper removed %d files (%d bytes)", files, freed)

                job_cutoff = (utc_now() - timedelta(days=JOB_TTL_DAYS)).isoformat()
                removed_jobs = await db_exec(
                    "DELETE FROM jobs WHERE finished_at IS NOT NULL AND finished_at < ?",
                    (job_cutoff,),
                )
                if removed_jobs:
                    LOG.info("retention sweeper removed %d job rows", removed_jobs)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("retention sweeper iteration failed")
    except asyncio.CancelledError:
        raise


# --- Lifespan ----------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await asyncio.to_thread(init_storage_sync)
    await fail_interrupted_running_jobs()

    global _http_client
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
        headers={"User-Agent": "lumen-image"},
    )

    for row in await db_all(
        "SELECT job_id FROM jobs WHERE status = 'queued' ORDER BY created_at"
    ):
        result = await enqueue_job(row["job_id"])
        if result == "full":
            LOG.warning("queue full while restoring backlog; remaining queued jobs deferred")
            break

    for index in range(CONCURRENCY):
        _workers.append(asyncio.create_task(worker_loop(index + 1), name=f"image-worker-{index+1}"))
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
        "upstream_base_url": UPSTREAM_BASE_URL,
        "data_dir": str(DATA_DIR),
        "db_path": str(DB_PATH),
        "sqlite_journal_mode": SQLITE_JOURNAL_MODE,
        "default_retention_days": DEFAULT_RETENTION_DAYS,
        "max_retention_days": MAX_RETENTION_DAYS,
        "stuck_reconciler": {
            "interval_s": STUCK_RECONCILE_INTERVAL_S,
            "queued_after_s": STUCK_QUEUED_AFTER_S,
            "running_after_s": STUCK_RUNNING_AFTER_S,
        },
    }


@app.post("/v1/image-jobs")
async def create_image_job(request: Request) -> dict[str, Any]:
    auth_header = require_auth(request)
    try:
        raw_payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    payload = validate_payload(raw_payload)
    job_id = make_job_id()
    await insert_job(job_id, payload, auth_header)
    result = await enqueue_job(job_id)
    if result == "full":
        await mark_failed(
            job_id,
            error="queue full; rejected before processing",
            error_class=ERROR_CLASS_INTERNAL,
        )
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
        raise HTTPException(status_code=403, detail="image job belongs to a different key")
    return row_to_response(row)


_REF_MIME_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}


def _refs_file_path(token: str, ext: str) -> Path:
    # token 是 url-safe base64（_ - 字母数字），ext 是白名单——拼接安全。
    return REFS_DIR / f"{token}.{ext}"


def _refs_public_url(token: str, ext: str) -> str:
    return f"{PUBLIC_BASE_URL}/refs/{token}.{ext}"


def _existing_ref_sync(sha: str) -> tuple[str, str] | None:
    """返回 (token, ext) 或 None。如果 DB 有行但文件已被 sweep，则同时清 DB 行。"""
    row = _db_one_sync("SELECT token, ext FROM refs WHERE sha256 = ?", (sha,))
    if row is None:
        return None
    token = row["token"]
    ext = row["ext"]
    if _refs_file_path(token, ext).exists():
        return token, ext
    # 文件已被 sweep，但 DB 行还在——清掉让上层重新写入。
    _db_exec_sync("DELETE FROM refs WHERE sha256 = ?", (sha,))
    return None


def _write_ref_sync(sha: str, token: str, ext: str, raw: bytes) -> None:
    """原子写盘 + 落库：先写临时文件再 rename，再 INSERT。
    INSERT 失败（并发同 sha）→ 回退用已有 token；调用方 deduped。
    """
    path = _refs_file_path(token, ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    try:
        _db_exec_sync(
            "INSERT INTO refs (sha256, token, ext, size, created_at) VALUES (?, ?, ?, ?, ?)",
            (sha, token, ext, len(raw), iso()),
        )
    except sqlite3.IntegrityError:
        # 并发场景：另一请求已经为同 sha 落库；保留我们的文件，但 DB 视图维持 first-writer-wins。
        # 调用方下次查 _existing_ref_sync 会拿到 first writer 的 token。
        # 我们这次写的文件是孤儿，retention sweeper 会按 mtime 清掉。
        pass


@app.post("/v1/refs")
async def upload_reference(request: Request) -> dict[str, Any]:
    """接收参考图 raw bytes，返回公网 URL。

    Body：raw 图片 bytes（PNG / JPEG / WebP），由 Content-Type 决定扩展名。
    Auth：与其他端点一致（Authorization: Bearer <api_key>）；这里不做 owner 隔离，
        只把 auth 当限速凭据——参考图本质上是用户自己上传的素材，URL 一旦生成
        谁拿到都能 GET（公网静态），所以 owner 检查没意义。
    幂等：同 sha256 复用已有 URL；不重复写盘。
    TTL：与 jobs 共用 MAX_RETENTION_DAYS（默认 1d）。
    """
    require_auth(request)
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="empty body")
    if len(raw) > MAX_REF_BYTES:
        raise HTTPException(
            status_code=413, detail=f"reference exceeds {MAX_REF_BYTES} bytes"
        )

    mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = _REF_MIME_EXT.get(mime)
    if ext is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported content-type {mime!r}; expected image/png|jpeg|webp",
        )

    sha = hashlib.sha256(raw).hexdigest()
    existing = await asyncio.to_thread(_existing_ref_sync, sha)
    if existing is not None:
        token, ext_existing = existing
        return {
            "url": _refs_public_url(token, ext_existing),
            "sha256": sha,
            "size": len(raw),
            "deduped": True,
        }

    token = secrets.token_urlsafe(24)
    await asyncio.to_thread(_write_ref_sync, sha, token, ext, raw)
    # 成功后 _existing_ref_sync 应总能命中（除非并发 race，那种情况下 token 用 first writer 的）
    final = await asyncio.to_thread(_existing_ref_sync, sha)
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
