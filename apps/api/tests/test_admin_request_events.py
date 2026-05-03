from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.routes import admin
from lumen_core.models import Generation


def test_request_event_status_filter_is_normalized() -> None:
    assert admin._normalize_request_event_status(None) is None
    assert admin._normalize_request_event_status("") is None
    assert admin._normalize_request_event_status(" all ") is None
    assert admin._normalize_request_event_status("FAILED") == "failed"

    with pytest.raises(HTTPException) as exc:
        admin._normalize_request_event_status("deleted")

    assert exc.value.status_code == 400
    assert exc.value.detail["error"]["code"] == "invalid_status"


def test_request_event_since_uses_selected_range() -> None:
    now = admin.datetime(2026, 4, 29, 12, tzinfo=admin.timezone.utc)

    assert admin._request_event_since("24h", now) == now - admin.timedelta(hours=24)
    assert admin._request_event_since("7d", now) == now - admin.timedelta(days=7)
    assert admin._request_event_since("30d", now) == now - admin.timedelta(days=30)


def test_request_event_sort_puts_unfinished_events_before_finished() -> None:
    base = admin.datetime(2026, 4, 29, 12, tzinfo=admin.timezone.utc)
    rows = [
        {
            "task": SimpleNamespace(
                id="created-newer",
                created_at=base + admin.timedelta(minutes=10),
                finished_at=base + admin.timedelta(minutes=11),
            )
        },
        {
            "task": SimpleNamespace(
                id="finished-newer",
                created_at=base,
                finished_at=base + admin.timedelta(minutes=30),
            )
        },
        {
            "task": SimpleNamespace(
                id="unfinished",
                created_at=base + admin.timedelta(hours=1),
                finished_at=None,
            )
        },
    ]

    rows.sort(key=admin._request_event_sort_key, reverse=True)

    # 进行中（finished_at IS NULL）排前，再按 finished_at / created_at 倒序。
    assert [row["task"].id for row in rows] == [
        "unfinished",
        "finished-newer",
        "created-newer",
    ]


def test_request_event_time_filter_uses_finished_at_before_created_at() -> None:
    since = admin.datetime(2026, 4, 29, 12, tzinfo=admin.timezone.utc)

    stmt = select(Generation.id).where(
        admin._request_event_time_filter(Generation, since)
    )
    rendered = str(stmt.compile(dialect=postgresql.dialect()))

    assert "generations.finished_at >=" in rendered
    assert "generations.finished_at IS NULL" in rendered
    assert "generations.created_at >=" in rendered


def test_request_event_response_defaults_are_isolated() -> None:
    created_at = admin.datetime(
        2026,
        4,
        29,
        tzinfo=admin.timezone.utc,
    )
    first = admin._RequestEventOut(
        id="gen-1",
        kind="generation",
        created_at=created_at,
        started_at=None,
        finished_at=None,
        duration_ms=None,
        status="queued",
        progress_stage="queued",
        attempt=0,
        model="model-a",
        user_id="user-1",
        user_email="admin@example.com",
        conversation_id=None,
        conversation_title=None,
        message_id="msg-1",
    )
    second = admin._RequestEventOut(
        id="gen-2",
        kind="generation",
        created_at=created_at,
        started_at=None,
        finished_at=None,
        duration_ms=None,
        status="queued",
        progress_stage="queued",
        attempt=0,
        model="model-a",
        user_id="user-1",
        user_email="admin@example.com",
        conversation_id=None,
        conversation_title=None,
        message_id="msg-2",
    )

    first.upstream["route"] = "responses"

    assert second.images == []
    assert second.upstream == {}


def test_request_event_model_stats_merge_codex_native_variants() -> None:
    created_at = admin.datetime(
        2026,
        4,
        29,
        tzinfo=admin.timezone.utc,
    )

    def event(event_id: str, model: str) -> admin._RequestEventOut:
        return admin._RequestEventOut(
            id=event_id,
            kind="generation",
            created_at=created_at,
            started_at=None,
            finished_at=None,
            duration_ms=None,
            status="succeeded",
            progress_stage="finalizing",
            attempt=1,
            model=model,
            user_id="user-1",
            user_email="admin@example.com",
            conversation_id=None,
            conversation_title=None,
            message_id=f"msg-{event_id}",
        )

    stats = admin._request_event_model_stats(
        [
            event("gen-1", "5.4"),
            event("gen-2", "image2"),
            event("gen-3", "5.4"),
            event("gen-4", "5.4 mini"),
        ]
    )

    assert [(stat.model, stat.count) for stat in stats] == [
        ("Codex 原生", 3),
        ("image2 直连", 1),
    ]
    assert stats[0].share == pytest.approx(3 / 4)
    assert stats[1].share == pytest.approx(1 / 4)


def test_request_provider_prefers_actual_provider_over_dual_race_strategy() -> None:
    assert (
        admin._request_provider(
            {
                "provider": "dual_race",
                "actual_provider": "shanghai-provider",
            }
        )
        == "shanghai-provider"
    )
    assert admin._request_provider({"provider": "dual_race"}) is None


def test_build_live_lanes_single_provider_snapshot() -> None:
    summary, lanes = admin._build_live_lanes_from_snapshot(
        {
            "mode": "single",
            "provider": "shanghai-1",
            "endpoint": "responses:image_generation",
        }
    )
    assert summary == "shanghai-1"
    assert len(lanes) == 1
    assert lanes[0].label == "main"
    assert lanes[0].provider == "shanghai-1"
    assert lanes[0].endpoint == "responses:image_generation"


def test_build_live_lanes_single_failover_window() -> None:
    summary, lanes = admin._build_live_lanes_from_snapshot(
        {
            "mode": "single",
            "status": "failover",
            "last_failed": "shanghai-1",
        }
    )
    assert summary == "切换中 (上一个 shanghai-1)"
    assert lanes[0].status == "failover"
    assert lanes[0].provider is None


def test_build_live_lanes_dual_race_two_providers() -> None:
    summary, lanes = admin._build_live_lanes_from_snapshot(
        {
            "mode": "dual_race",
            "lane_a_provider": "alpha",
            "lane_a_route": "image2",
            "lane_a_endpoint": "images/generations",
            "lane_b_provider": "beta",
            "lane_b_route": "responses",
        }
    )
    assert summary == "alpha vs beta"
    assert [lane.label for lane in lanes] == ["image2", "responses"]
    assert lanes[0].provider == "alpha"
    assert lanes[1].provider == "beta"


def test_build_live_lanes_dual_race_image_jobs_labels() -> None:
    summary, lanes = admin._build_live_lanes_from_snapshot(
        {
            "mode": "dual_race",
            "lane_a_provider": "pool-1",
            "lane_a_route": "image_jobs",
            "lane_a_endpoint": "image-jobs:generations",
            "lane_b_provider": "pool-2",
            "lane_b_route": "image_jobs",
            "lane_b_endpoint": "image-jobs:responses",
        }
    )
    assert summary == "pool-1 vs pool-2"
    assert lanes[0].label == "image_jobs:generations"
    assert lanes[1].label == "image_jobs:responses"


def test_build_live_lanes_dual_race_one_lane_still_picking() -> None:
    summary, lanes = admin._build_live_lanes_from_snapshot(
        {
            "mode": "dual_race",
            "lane_a_provider": "alpha",
            "lane_b_status": "failover",
            "lane_b_last_failed": "beta",
        }
    )
    assert summary == "alpha vs 切换中 (上一个 beta)"
    assert lanes[0].provider == "alpha"
    assert lanes[1].provider is None
    assert lanes[1].status == "failover"


def test_decode_inflight_hash_handles_bytes_and_str() -> None:
    assert admin._decode_inflight_hash(None) == {}
    assert admin._decode_inflight_hash({}) == {}
    assert admin._decode_inflight_hash({b"mode": b"single", b"provider": b"x"}) == {
        "mode": "single",
        "provider": "x",
    }
    assert admin._decode_inflight_hash({"mode": "dual_race"}) == {
        "mode": "dual_race",
    }


def test_is_inflight_status_set() -> None:
    assert admin._is_inflight_status("queued") is True
    assert admin._is_inflight_status("running") is True
    assert admin._is_inflight_status("streaming") is True
    assert admin._is_inflight_status("succeeded") is False
    assert admin._is_inflight_status(None) is False


def test_generation_model_label_uses_actual_dual_race_winner() -> None:
    image2 = SimpleNamespace(
        upstream_request={"upstream_route": "dual_race", "actual_route": "image2"},
        action="generate",
        status="succeeded",
    )
    responses = SimpleNamespace(
        upstream_request={"upstream_route": "dual_race", "actual_route": "responses"},
        action="generate",
        status="succeeded",
    )
    historical = SimpleNamespace(
        upstream_request={"upstream_route": "dual_race", "provider": "dual_race"},
        action="generate",
        status="succeeded",
    )

    assert admin._generation_model_label(image2) == "image2"
    assert admin._generation_model_label(responses) == "5.4"
    assert admin._generation_model_label(historical) == "历史未记录"


def test_generation_model_label_uses_fast_responses_model() -> None:
    fast_responses = SimpleNamespace(
        upstream_request={
            "upstream_route": "dual_race",
            "actual_route": "responses",
            "fast": True,
        },
        action="generate",
        status="succeeded",
    )
    queued_fast_race = SimpleNamespace(
        upstream_request={"upstream_route": "dual_race", "fast": True},
        action="generate",
        status="running",
    )

    assert admin._generation_model_label(fast_responses) == "5.4 mini"
    assert admin._generation_model_label(queued_fast_race) == "竞速中: 5.4 mini / image2"
