"""Side-effect-free image-job configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


def _flag(env: Mapping[str, str], name: str, default: str = "0") -> bool:
    return env.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SecretText:
    value: str = field(repr=False)

    def get_secret_value(self) -> str:
        return self.value

    def __str__(self) -> str:
        return "********"


@dataclass(frozen=True)
class ImageJobTimeouts:
    upstream_s: float = 1800.0
    connect_s: float = 5.0
    graceful_shutdown_s: float = 60.0


@dataclass(frozen=True)
class ImageJobSettings:
    data_dir: Path
    refs_dir: Path
    state_dir: Path
    db_path: Path
    queue_max: int
    concurrency: int
    sidecar_token: SecretText
    allow_legacy_bearer: bool
    upstream_base_url: str
    public_base_url: str
    timeouts: ImageJobTimeouts
    sqlite_journal_mode: str = "WAL"
    max_request_bytes: int = 64 * 1024 * 1024
    max_ref_bytes: int = 50 * 1024 * 1024
    max_json_depth: int = 32
    max_json_array_items: int = 256
    max_json_object_items: int = 256
    max_json_total_values: int = 10_000
    max_json_key_chars: int = 256
    max_json_string_chars: int = 64 * 1024 * 1024
    max_image_pixels: int = 100 * 1000 * 1000
    max_image_bytes: int = 80 * 1024 * 1024
    max_image_candidates: int = 8
    max_total_image_bytes: int = 160 * 1024 * 1024
    max_image_url_redirects: int = 5
    max_upstream_response_bytes: int = 256 * 1024 * 1024
    max_upstream_error_body_bytes: int = 64 * 1024
    responses_stream_max_bytes: int = 160 * 1024 * 1024
    responses_stream_idle_timeout_s: float = 60.0
    responses_strip_partial_images: bool = True
    job_heartbeat_interval_s: float = 15.0
    retry_network_max: int = 1
    retry_responses_stream_max: int = 1
    retry_upstream_5xx_max: int = 1
    retry_backoff_s: float = 2.0
    http_pool_keepalive: int = 8
    http_pool_max: int = 32
    max_idempotency_key_bytes: int = 512
    max_endpoint_chars: int = 512
    max_request_type_chars: int = 128
    default_retention_days: int = 1
    max_retention_days: int = 1
    job_ttl_days: int = 1
    upstream_idempotency_guaranteed: bool = False
    stuck_reconcile_interval_s: float = 60.0
    retention_sweep_interval_s: float = 3600.0
    legacy_auth_removal_version: str = "2.0.0"

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> ImageJobSettings:
        env = os.environ if environ is None else environ
        root_dir = Path(env.get("IMAGE_JOB_ROOT_DIR", "/opt/image-job"))
        data_dir = Path(env.get("IMAGE_JOB_DATA_DIR", str(root_dir / "data")))
        state_dir = Path(env.get("IMAGE_JOB_STATE_DIR", "/var/lib/image-job/state"))
        max_request_bytes = max(
            1024,
            int(env.get("IMAGE_JOB_MAX_REQUEST_BYTES", str(64 * 1024 * 1024))),
        )
        max_image_bytes = max(
            1,
            int(env.get("IMAGE_JOB_MAX_IMAGE_BYTES", str(80 * 1024 * 1024))),
        )
        max_total_image_bytes = max(
            max_image_bytes,
            int(
                env.get(
                    "IMAGE_JOB_MAX_TOTAL_IMAGE_BYTES",
                    str(max_image_bytes * 2),
                )
            ),
        )
        max_upstream_response_bytes = max(
            max_image_bytes,
            int(
                env.get(
                    "IMAGE_JOB_MAX_UPSTREAM_RESPONSE_BYTES",
                    str(
                        max(
                            256 * 1024 * 1024,
                            max_total_image_bytes * 4 // 3 + 1024 * 1024,
                        )
                    ),
                )
            ),
        )
        retry_network_max = max(
            0,
            int(env.get("IMAGE_JOB_RETRY_NETWORK_MAX", "1")),
        )
        default_retention = min(
            30,
            max(1, int(env.get("IMAGE_JOB_RETENTION_DAYS", "1"))),
        )
        max_retention = min(
            30,
            max(
                1,
                int(env.get("IMAGE_JOB_MAX_RETENTION_DAYS", str(default_retention))),
            ),
        )
        return cls(
            data_dir=data_dir,
            refs_dir=data_dir / "refs",
            state_dir=state_dir,
            db_path=Path(
                env.get(
                    "IMAGE_JOB_DB_PATH",
                    str(state_dir / "image_jobs.sqlite3"),
                )
            ),
            queue_max=max(1, int(env.get("IMAGE_JOB_QUEUE_MAX", "1000"))),
            concurrency=max(1, int(env.get("IMAGE_JOB_CONCURRENCY", "2"))),
            sidecar_token=SecretText(env.get("IMAGE_JOB_SIDECAR_TOKEN", "").strip()),
            allow_legacy_bearer=_flag(
                env,
                "IMAGE_JOB_ALLOW_LEGACY_BEARER_AUTH",
            ),
            upstream_base_url=env.get(
                "IMAGE_JOB_UPSTREAM_BASE_URL",
                "http://127.0.0.1:8081",
            ).rstrip("/"),
            public_base_url=env.get(
                "IMAGE_JOB_PUBLIC_BASE_URL",
                "https://example.com",
            ).rstrip("/"),
            timeouts=ImageJobTimeouts(
                upstream_s=float(env.get("IMAGE_JOB_UPSTREAM_TIMEOUT_S", "1800")),
                connect_s=float(env.get("IMAGE_JOB_UPSTREAM_CONNECT_TIMEOUT_S", "5")),
                graceful_shutdown_s=max(
                    0.0,
                    float(env.get("IMAGE_JOB_GRACEFUL_SHUTDOWN_S", "60")),
                ),
            ),
            sqlite_journal_mode=env.get(
                "IMAGE_JOB_SQLITE_JOURNAL_MODE",
                "WAL",
            )
            .strip()
            .upper(),
            max_request_bytes=max_request_bytes,
            max_ref_bytes=max(
                1,
                int(env.get("IMAGE_JOB_MAX_REF_BYTES", str(50 * 1024 * 1024))),
            ),
            max_json_depth=max(1, int(env.get("IMAGE_JOB_MAX_JSON_DEPTH", "32"))),
            max_json_array_items=max(
                1,
                int(env.get("IMAGE_JOB_MAX_JSON_ARRAY_ITEMS", "256")),
            ),
            max_json_object_items=max(
                1,
                int(env.get("IMAGE_JOB_MAX_JSON_OBJECT_ITEMS", "256")),
            ),
            max_json_total_values=max(
                1,
                int(env.get("IMAGE_JOB_MAX_JSON_TOTAL_VALUES", "10000")),
            ),
            max_json_key_chars=max(
                1,
                int(env.get("IMAGE_JOB_MAX_JSON_KEY_CHARS", "256")),
            ),
            max_json_string_chars=max(
                1,
                int(
                    env.get(
                        "IMAGE_JOB_MAX_JSON_STRING_CHARS",
                        str(max_request_bytes),
                    )
                ),
            ),
            max_image_pixels=max(
                1,
                int(
                    env.get(
                        "IMAGE_JOB_MAX_IMAGE_PIXELS",
                        str(100 * 1000 * 1000),
                    )
                ),
            ),
            max_image_bytes=max_image_bytes,
            max_image_candidates=max(
                1,
                int(env.get("IMAGE_JOB_MAX_IMAGE_CANDIDATES", "8")),
            ),
            max_total_image_bytes=max_total_image_bytes,
            max_image_url_redirects=max(
                0,
                int(env.get("IMAGE_JOB_MAX_IMAGE_URL_REDIRECTS", "5")),
            ),
            max_upstream_response_bytes=max_upstream_response_bytes,
            max_upstream_error_body_bytes=max(
                1024,
                int(
                    env.get(
                        "IMAGE_JOB_MAX_UPSTREAM_ERROR_BODY_BYTES",
                        str(64 * 1024),
                    )
                ),
            ),
            responses_stream_max_bytes=max(
                max_image_bytes,
                int(
                    env.get(
                        "IMAGE_JOB_RESPONSES_STREAM_MAX_BYTES",
                        str(max(max_image_bytes * 2, 64 * 1024 * 1024)),
                    )
                ),
            ),
            responses_stream_idle_timeout_s=max(
                10.0,
                float(env.get("IMAGE_JOB_RESPONSES_STREAM_IDLE_TIMEOUT_S", "60")),
            ),
            responses_strip_partial_images=not _flag(
                env,
                "IMAGE_JOB_RESPONSES_KEEP_PARTIAL_IMAGES",
            )
            and env.get(
                "IMAGE_JOB_RESPONSES_STRIP_PARTIAL_IMAGES",
                "1",
            )
            .strip()
            .lower()
            not in {"0", "false", "no", "off"},
            job_heartbeat_interval_s=max(
                5.0,
                float(env.get("IMAGE_JOB_HEARTBEAT_INTERVAL_S", "15")),
            ),
            retry_network_max=retry_network_max,
            retry_responses_stream_max=max(
                0,
                int(
                    env.get(
                        "IMAGE_JOB_RETRY_RESPONSES_STREAM_MAX",
                        str(retry_network_max),
                    )
                ),
            ),
            retry_upstream_5xx_max=max(
                0,
                int(env.get("IMAGE_JOB_RETRY_UPSTREAM_5XX_MAX", "1")),
            ),
            retry_backoff_s=max(
                0.0,
                float(env.get("IMAGE_JOB_RETRY_BACKOFF_S", "2")),
            ),
            http_pool_keepalive=max(
                1,
                int(env.get("IMAGE_JOB_HTTP_POOL_KEEPALIVE", "8")),
            ),
            http_pool_max=max(
                max(1, int(env.get("IMAGE_JOB_HTTP_POOL_KEEPALIVE", "8"))),
                int(env.get("IMAGE_JOB_HTTP_POOL_MAX", "32")),
            ),
            default_retention_days=min(default_retention, max_retention),
            max_retention_days=max_retention,
            job_ttl_days=max(
                1,
                int(env.get("IMAGE_JOB_JOB_TTL_DAYS", "1")),
            ),
            upstream_idempotency_guaranteed=_flag(
                env,
                "IMAGE_JOB_UPSTREAM_IDEMPOTENCY_GUARANTEED",
            ),
            stuck_reconcile_interval_s=max(
                15.0,
                float(env.get("IMAGE_JOB_STUCK_RECONCILE_INTERVAL_S", "60")),
            ),
            retention_sweep_interval_s=max(
                60.0,
                float(env.get("IMAGE_JOB_RETENTION_SWEEP_INTERVAL_S", "3600")),
            ),
        )

    def validate(self) -> None:
        token = self.sidecar_token.get_secret_value()
        if token and (len(token) < 32 or any(char.isspace() for char in token)):
            raise RuntimeError(
                "IMAGE_JOB_SIDECAR_TOKEN must be a whitespace-free token "
                "with at least 32 characters"
            )
        if not token and not self.allow_legacy_bearer:
            raise RuntimeError(
                "IMAGE_JOB_SIDECAR_TOKEN is required unless "
                "IMAGE_JOB_ALLOW_LEGACY_BEARER_AUTH=1 is explicitly enabled"
            )
