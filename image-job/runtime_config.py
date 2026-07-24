"""Environment-backed runtime settings for the image-job sidecar."""

from __future__ import annotations

import os
from pathlib import Path


def env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


UPSTREAM_BASE_URL = os.getenv(
    "IMAGE_JOB_UPSTREAM_BASE_URL", "http://127.0.0.1:8081"
).rstrip("/")
PUBLIC_BASE_URL = os.getenv("IMAGE_JOB_PUBLIC_BASE_URL", "https://example.com").rstrip(
    "/"
)
ROOT_DIR = Path(os.getenv("IMAGE_JOB_ROOT_DIR", "/opt/image-job"))
DATA_DIR = Path(os.getenv("IMAGE_JOB_DATA_DIR", str(ROOT_DIR / "data")))
REFS_DIR = DATA_DIR / "refs"
MAX_REF_BYTES = int(os.getenv("IMAGE_JOB_MAX_REF_BYTES", str(50 * 1024 * 1024)))
STATE_DIR = Path(os.getenv("IMAGE_JOB_STATE_DIR", "/var/lib/image-job/state"))
DB_PATH = Path(os.getenv("IMAGE_JOB_DB_PATH", str(STATE_DIR / "image_jobs.sqlite3")))

SIDECAR_TOKEN = os.getenv("IMAGE_JOB_SIDECAR_TOKEN", "").strip()
ALLOW_LEGACY_BEARER_AUTH = env_flag("IMAGE_JOB_ALLOW_LEGACY_BEARER_AUTH")
MIN_SIDECAR_TOKEN_CHARS = 32

CONCURRENCY = max(1, int(os.getenv("IMAGE_JOB_CONCURRENCY", "2")))
UPSTREAM_TIMEOUT_S = float(os.getenv("IMAGE_JOB_UPSTREAM_TIMEOUT_S", "1800"))
UPSTREAM_CONNECT_TIMEOUT_S = float(
    os.getenv("IMAGE_JOB_UPSTREAM_CONNECT_TIMEOUT_S", "5")
)
UPSTREAM_IDEMPOTENCY_GUARANTEED = env_flag("IMAGE_JOB_UPSTREAM_IDEMPOTENCY_GUARANTEED")

MAX_IMAGE_JOB_REQUEST_BYTES = max(
    1024,
    int(
        os.getenv(
            "IMAGE_JOB_MAX_REQUEST_BYTES",
            str(64 * 1024 * 1024),
        )
    ),
)
MAX_JSON_DEPTH = max(1, int(os.getenv("IMAGE_JOB_MAX_JSON_DEPTH", "32")))
MAX_JSON_ARRAY_ITEMS = max(1, int(os.getenv("IMAGE_JOB_MAX_JSON_ARRAY_ITEMS", "256")))
MAX_JSON_OBJECT_ITEMS = max(1, int(os.getenv("IMAGE_JOB_MAX_JSON_OBJECT_ITEMS", "256")))
MAX_JSON_TOTAL_VALUES = max(
    1, int(os.getenv("IMAGE_JOB_MAX_JSON_TOTAL_VALUES", "10000"))
)
MAX_JSON_KEY_CHARS = max(1, int(os.getenv("IMAGE_JOB_MAX_JSON_KEY_CHARS", "256")))
MAX_JSON_STRING_CHARS = max(
    1,
    int(
        os.getenv(
            "IMAGE_JOB_MAX_JSON_STRING_CHARS",
            str(MAX_IMAGE_JOB_REQUEST_BYTES),
        )
    ),
)
MAX_IDEMPOTENCY_KEY_BYTES = 512
MAX_ENDPOINT_CHARS = 512
MAX_REQUEST_TYPE_CHARS = 128

MAX_IMAGE_BYTES = max(
    1,
    int(os.getenv("IMAGE_JOB_MAX_IMAGE_BYTES", str(80 * 1024 * 1024))),
)
MAX_IMAGE_CANDIDATES = max(1, int(os.getenv("IMAGE_JOB_MAX_IMAGE_CANDIDATES", "8")))
MAX_TOTAL_IMAGE_BYTES = max(
    MAX_IMAGE_BYTES,
    int(
        os.getenv(
            "IMAGE_JOB_MAX_TOTAL_IMAGE_BYTES",
            str(MAX_IMAGE_BYTES * 2),
        )
    ),
)
MAX_UPSTREAM_RESPONSE_BYTES = max(
    MAX_IMAGE_BYTES,
    int(
        os.getenv(
            "IMAGE_JOB_MAX_UPSTREAM_RESPONSE_BYTES",
            str(
                max(
                    256 * 1024 * 1024,
                    MAX_TOTAL_IMAGE_BYTES * 4 // 3 + 1024 * 1024,
                )
            ),
        )
    ),
)
MAX_UPSTREAM_ERROR_BODY_BYTES = max(
    1024,
    int(os.getenv("IMAGE_JOB_MAX_UPSTREAM_ERROR_BODY_BYTES", str(64 * 1024))),
)
MAX_IMAGE_PIXELS = max(
    1,
    int(os.getenv("IMAGE_JOB_MAX_IMAGE_PIXELS", str(100 * 1000 * 1000))),
)

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
RESPONSES_STREAM_IDLE_TIMEOUT_S = max(
    10.0, float(os.getenv("IMAGE_JOB_RESPONSES_STREAM_IDLE_TIMEOUT_S", "60"))
)

RETENTION_SWEEP_INTERVAL_S = max(
    60, int(os.getenv("IMAGE_JOB_RETENTION_SWEEP_INTERVAL_S", "3600"))
)
DEFAULT_RETENTION_DAYS = min(
    30, max(1, int(os.getenv("IMAGE_JOB_RETENTION_DAYS", "1")))
)
MAX_RETENTION_DAYS = min(
    30,
    max(
        1,
        int(
            os.getenv(
                "IMAGE_JOB_MAX_RETENTION_DAYS",
                str(DEFAULT_RETENTION_DAYS),
            )
        ),
    ),
)
if DEFAULT_RETENTION_DAYS > MAX_RETENTION_DAYS:
    DEFAULT_RETENTION_DAYS = MAX_RETENTION_DAYS
JOB_TTL_DAYS = max(1, int(os.getenv("IMAGE_JOB_JOB_TTL_DAYS", "30")))
GRACEFUL_SHUTDOWN_S = max(0, int(os.getenv("IMAGE_JOB_GRACEFUL_SHUTDOWN_S", "60")))

HTTP_POOL_KEEPALIVE = max(1, int(os.getenv("IMAGE_JOB_HTTP_POOL_KEEPALIVE", "8")))
HTTP_POOL_MAX = max(
    HTTP_POOL_KEEPALIVE,
    int(os.getenv("IMAGE_JOB_HTTP_POOL_MAX", "32")),
)
MAX_IMAGE_URL_REDIRECTS = max(
    0, int(os.getenv("IMAGE_JOB_MAX_IMAGE_URL_REDIRECTS", "5"))
)
SQLITE_JOURNAL_MODE = os.getenv("IMAGE_JOB_SQLITE_JOURNAL_MODE", "WAL").strip().upper()

STUCK_RECONCILE_INTERVAL_S = max(
    15, int(os.getenv("IMAGE_JOB_STUCK_RECONCILE_INTERVAL_S", "60"))
)
STUCK_QUEUED_AFTER_S = max(30, int(os.getenv("IMAGE_JOB_STUCK_QUEUED_AFTER_S", "120")))
STUCK_RUNNING_AFTER_S = max(
    60, int(os.getenv("IMAGE_JOB_STUCK_RUNNING_AFTER_S", "300"))
)
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
