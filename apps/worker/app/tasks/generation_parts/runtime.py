from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class GenerationRunState:
    """Mutable state shared by the generation runner phases."""

    ctx: dict[str, Any]
    task_id: str
    redis: Any
    worker_id: str
    lease_token: str
    task_start: float
    task_deadline: float
    channel: str
    trace_id: str
    stage_timer: Any
    task_outcome: str = "unknown"
    attempt: int = 0
    renewer: asyncio.Task[None] | None = None
    lease_lost: asyncio.Event = field(default_factory=asyncio.Event)
    reserved_provider: Any | None = None
    reserved_provider_name: str | None = None
    user_api_credential_id: str | None = None
    user_runtime_provider: Any | None = None
    loaded_attempt: int = 0
    queue_metadata_payload: dict[str, Any] = field(default_factory=dict)
    route_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    gen_created_at: datetime | None = None

    generation: Any | None = None
    user_id: str = ""
    message_id: str = ""
    action: Any = None
    prompt: str = ""
    aspect_ratio: str = ""
    size_requested: str | None = None
    input_image_ids: list[str] = field(default_factory=list)
    primary_input_image_id: str | None = None
    mask_image_id: str | None = None
    gen_idempotency_key: str | None = None
    gen_model: str | None = None
    gen_upstream_request_snapshot: dict[str, Any] | None = None
    image_request_options: dict[str, Any] = field(default_factory=dict)

    raw_image_route: str = "responses"
    image_route: str = "responses"
    requires_mask_provider: bool = False
    is_dual_race: bool = False
    endpoint_kind: str | None = None
    upstream_provider_label: str | None = None
    lease_reacquired: bool = False

    has_partial: bool = False
    image_iter: AsyncIterator[tuple[str, str | None]] | None = None
    provider_attempt_log: list[dict[str, Any]] = field(default_factory=list)
    upstream_duration_ms: int | None = None
    requested_image_count: int = 1
    batch_extra_pairs: list[tuple[int, tuple[str, str | None]]] = field(
        default_factory=list
    )
    requested_params_for_diag: dict[str, Any] = field(default_factory=dict)

    resolved: Any | None = None
    references: list[tuple[str, bytes]] = field(default_factory=list)
    ref_for_body: list[tuple[str, bytes]] = field(default_factory=list)
    mask_bytes: bytes | None = None
    inpaint_size_override: str | None = None
    prompt_for_upstream: str = ""
    progress_publisher: Any | None = None

    b64_result: str | None = None
    revised_prompt: str | None = None
    actual_upstream_provider: str | None = None
    actual_upstream_route: str | None = None
    actual_upstream_source: str | None = None
    actual_upstream_endpoint: str | None = None
    image_job_meta: dict[str, Any] = field(default_factory=dict)
    provider_used_events: list[dict[str, str]] = field(default_factory=list)

    conversation_id_for_title: str | None = None
    parent_upstream_request_for_bonus: dict[str, Any] | None = None
