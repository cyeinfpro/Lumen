from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lumen_core.queue_metadata import (
    completion_queue_metadata,
    generation_queue_metadata,
    merge_queue_metadata,
)


def test_generation_queue_metadata_classifies_workflow_large_image() -> None:
    created = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    started = created + timedelta(milliseconds=2500)

    meta = generation_queue_metadata(
        upstream_request={
            "workflow_type": "apparel_model_showcase",
            "workflow_step_key": "showcase_generation",
        },
        action="generate",
        size_requested="3840x2160",
        created_at=created,
        started_at=started,
    )

    assert meta["queue_lane"] == "image:workflow:large"
    assert meta["workflow_type"] == "apparel_model_showcase"
    assert meta["workflow_step_key"] == "showcase_generation"
    assert meta["pixel_count"] == 8_294_400
    assert meta["size_bucket"] == "large"
    assert meta["cost_class"] == "large"
    assert meta["queue_wait_ms"] == 2500


def test_generation_queue_metadata_classifies_mask_edit() -> None:
    meta = generation_queue_metadata(
        upstream_request={},
        action="edit",
        size_requested="1024x1024",
        mask_image_id="mask-1",
    )

    assert meta["queue_lane"] == "image:interactive:mask_edit"
    assert meta["cost_class"] == "mask_edit"
    assert meta["size_bucket"] == "small"


def test_generation_queue_metadata_recomputes_stale_queue_lane() -> None:
    meta = generation_queue_metadata(
        upstream_request={
            "queue_lane": "image:interactive:small",
            "workflow_type": "poster_workflow",
        },
        action="generate",
        size_requested="3840x2160",
    )

    assert meta["queue_lane"] == "image:workflow:large"


def test_completion_queue_metadata_and_merge_are_flat_and_nested() -> None:
    created = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    started = created + timedelta(milliseconds=800)
    meta = completion_queue_metadata(
        upstream_request={"workflow_type": "poster_design"},
        created_at=created,
        started_at=started,
    )
    merged = merge_queue_metadata({"model": "gpt-5.5"}, meta)

    assert meta["queue_lane"] == "completion:interactive"
    assert meta["workflow_type"] == "poster_design"
    assert meta["cost_class"] == "completion"
    assert meta["queue_wait_ms"] == 800
    assert merged["queue_lane"] == "completion:interactive"
    assert merged["queue_metadata"]["queue_wait_ms"] == 800
    assert merged["model"] == "gpt-5.5"
