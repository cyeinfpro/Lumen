from __future__ import annotations

IMAGE_QUEUE_LOCK_KEY = "generation:image_queue:lock"
IMAGE_QUEUE_ACTIVE_KEY = "generation:image_queue:active"
IMAGE_QUEUE_PROVIDER_LOCK_PREFIX = "generation:image_queue:provider:"
IMAGE_QUEUE_TASK_PROVIDER_PREFIX = "generation:image_queue:task_provider:"
IMAGE_QUEUE_NOT_BEFORE_PREFIX = "generation:image_queue:not_before:"
IMAGE_QUEUE_AVOID_PREFIX = "generation:image_queue:avoid:"
IMAGE_QUEUE_LANE_CURSOR_KEY = "generation:image_queue:lane_cursor"
IMAGE_INFLIGHT_PREFIX = "generation:image_inflight:"
IMAGE_QUEUE_LOCK_TTL_S = 10
IMAGE_QUEUE_LOCK_WAIT_S = 5.0
IMAGE_QUEUE_FAIR_SCAN_LIMIT = 1000
IMAGE_QUEUE_NOT_BEFORE_GRACE_S = 600
IMAGE_PROVIDER_UNAVAILABLE_RETRY_S = 30
IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S = 5.0
IMAGE_QUEUE_AVOID_TTL_S = 120
IMAGE_QUEUE_DEFAULT_LANE = "image:interactive:unknown"
IMAGE_GENERATION_CONCURRENCY_SETTING = "image.generation_concurrency"
DUAL_RACE_SENTINEL_PREFIX = "__dr:"


def redis_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
