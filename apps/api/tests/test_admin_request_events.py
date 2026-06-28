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


def test_message_output_image_refs_extracts_deduped_generation_links() -> None:
    refs = admin._message_output_image_refs(
        {
            "images": [
                {"image_id": "img-1", "from_generation_id": "gen-1"},
                {"id": "img-2", "generation_id": "gen-2"},
                {"image_id": "img-1", "from_generation_id": "gen-1"},
                "img-3",
                {"image_id": ""},
                {"not_image": "ignored"},
            ]
        }
    )

    assert refs == [
        ("img-1", "gen-1"),
        ("img-2", "gen-2"),
        ("img-3", None),
    ]


def test_message_output_image_refs_ignores_non_image_content() -> None:
    assert admin._message_output_image_refs(None) == []
    assert admin._message_output_image_refs({"images": "img-1"}) == []
    assert admin._message_output_image_refs({"text": "hello"}) == []


def test_request_event_exposes_queue_observability_fields() -> None:
    created_at = admin.datetime(
        2026,
        5,
        19,
        tzinfo=admin.timezone.utc,
    )
    event = admin._RequestEventOut(
        id="gen-queue",
        kind="generation",
        created_at=created_at,
        started_at=created_at + admin.timedelta(seconds=2),
        finished_at=None,
        duration_ms=None,
        status="running",
        progress_stage="rendering",
        attempt=1,
        model="image2",
        user_id="user-1",
        user_email="admin@example.com",
        conversation_id=None,
        conversation_title=None,
        message_id="msg-queue",
        queue_lane="image:workflow:large",
        workflow_type="apparel_model_showcase",
        workflow_step_key="showcase_generation",
        pixel_count=8_294_400,
        size_bucket="large",
        cost_class="large",
        queue_wait_ms=2000,
    )

    payload = event.model_dump()

    assert payload["queue_lane"] == "image:workflow:large"
    assert payload["workflow_type"] == "apparel_model_showcase"
    assert payload["workflow_step_key"] == "showcase_generation"
    assert payload["pixel_count"] == 8_294_400
    assert payload["size_bucket"] == "large"
    assert payload["cost_class"] == "large"
    assert payload["queue_wait_ms"] == 2000


def test_safe_upstream_details_includes_queue_observability_fields() -> None:
    details = admin._safe_upstream_details(
        {
            "queue_lane": "image:interactive:small",
            "workflow_type": "poster_design",
            "workflow_step_key": "master_generation",
            "pixel_count": 1_048_576,
            "size_bucket": "small",
            "cost_class": "small",
            "queue_wait_ms": 123,
            "prompt": "must not leak",
        }
    )

    assert details == {
        "cost_class": "small",
        "pixel_count": 1_048_576,
        "queue_lane": "image:interactive:small",
        "queue_wait_ms": 123,
        "size_bucket": "small",
        "workflow_step_key": "master_generation",
        "workflow_type": "poster_design",
    }


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
                "request_event_provider": "winner-provider",
            }
        )
        == "winner-provider"
    )
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


def test_request_provider_can_read_legacy_diagnostics_provider() -> None:
    assert (
        admin._request_provider(
            {
                "provider": "dual_race",
                "generation_diagnostics": {"actual_provider": "diag-provider"},
            }
        )
        == "diag-provider"
    )


def test_request_provider_can_fallback_to_provider_attempts() -> None:
    assert (
        admin._request_provider(
            {
                "provider": "dual_race",
                "provider_attempts": [
                    {"provider": "first-failed", "status": "failover"},
                    {"provider": "winner-provider", "status": "used"},
                ],
            }
        )
        == "winner-provider"
    )
    assert (
        admin._request_provider(
            {
                "provider": "dual_race",
                "generation_diagnostics": {
                    "provider_attempts": [
                        {"actual_provider": "diag-winner", "status": "used"}
                    ]
                },
            }
        )
        == "diag-winner"
    )


def test_request_route_accepts_actual_route_fallback() -> None:
    assert admin._request_route({"actual_route": "responses"}) == "responses"
    assert (
        admin._request_route(
            {"upstream_route": "dual_race", "actual_route": "responses"}
        )
        == "dual_race"
    )


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


def test_completion_upstream_request_exposes_provider_and_responses_route() -> None:
    upstream_request = {
        "source": "composer",
        "action_source": "composer.vision_qa",
        "request_event_provider": "pool-a",
        "upstream_route": "responses",
        "actual_endpoint": "responses",
        "actual_source": "text",
    }

    assert admin._request_provider(upstream_request) == "pool-a"
    assert admin._request_route(upstream_request) == "responses"
    assert admin._safe_upstream_details(upstream_request) == {
        "action_source": "composer.vision_qa",
        "actual_endpoint": "responses",
        "actual_source": "text",
        "request_event_provider": "pool-a",
        "source": "composer",
        "upstream_route": "responses",
    }


class _AdminResult:
    def __init__(self, value=None, *, rowcount: int = 0):
        self.value = value
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self.value


class _AdminDb:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []
        self.committed = False

    async def execute(self, stmt):
        self.statements.append(stmt)
        if self.results:
            return self.results.pop(0)
        return _AdminResult()

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_admin_set_user_password_hashes_and_revokes_sessions(monkeypatch):
    target = SimpleNamespace(
        id="user-1",
        email="member@example.com",
        password_hash="old-hash",
    )
    db = _AdminDb([_AdminResult(target), _AdminResult(rowcount=2)])
    audits = []

    monkeypatch.setattr(admin, "hash_password", lambda password: f"hashed:{password}")

    async def fake_audit(*_args, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(admin, "write_admin_audit", fake_audit)

    out = await admin.set_user_password(
        "user-1",
        admin._AdminSetUserPasswordIn(password="new-password"),
        SimpleNamespace(),
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        db,  # type: ignore[arg-type]
    )

    assert out == {"ok": True}
    assert target.password_hash == "hashed:new-password"
    assert db.committed is True
    assert audits[0]["event_type"] == "admin.user.password_set"
    assert audits[0]["target_user_id"] == "user-1"


@pytest.mark.asyncio
async def test_admin_delete_user_rejects_self_delete():
    with pytest.raises(HTTPException) as exc:
        await admin.delete_user(
            "admin-1",
            SimpleNamespace(),
            SimpleNamespace(id="admin-1", email="admin@example.com"),
            _AdminDb([]),  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["error"]["code"] == "cannot_delete_self"


@pytest.mark.asyncio
async def test_admin_delete_user_soft_deletes_and_runs_cleanup(monkeypatch):
    target = SimpleNamespace(
        id="user-1",
        email="member@example.com",
        account_mode="byok",
        deleted_at=None,
    )
    db = _AdminDb(
        [
            _AdminResult(target),
            _AdminResult(rowcount=2),
            _AdminResult(rowcount=3),
            _AdminResult(rowcount=4),
        ]
    )
    task_cleanup = {"generations_canceled": 5, "completions_canceled": 6}
    cleanup_calls = []
    audits = []

    async def fake_cancel(*_args, **kwargs):
        assert kwargs["user_id"] == "user-1"
        assert kwargs["account_mode"] == "byok"
        return task_cleanup

    async def fake_post_commit(*_args, **kwargs):
        cleanup_calls.append(kwargs)

    async def fake_audit(*_args, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(admin, "_cancel_account_active_tasks", fake_cancel)
    monkeypatch.setattr(
        admin,
        "_post_commit_account_task_cleanup",
        fake_post_commit,
    )
    monkeypatch.setattr(admin, "write_admin_audit", fake_audit)

    out = await admin.delete_user(
        "user-1",
        SimpleNamespace(),
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        db,  # type: ignore[arg-type]
    )

    assert out == {"ok": True}
    assert target.deleted_at is not None
    assert db.committed is True
    assert audits[0]["event_type"] == "admin.user.delete"
    assert audits[0]["details"]["sessions_revoked"] == 2
    assert audits[0]["details"]["conversations_deleted"] == 3
    assert audits[0]["details"]["images_deleted"] == 4
    assert audits[0]["details"]["generations_canceled"] == 5
    assert audits[0]["details"]["completions_canceled"] == 6
    assert cleanup_calls == [{"user_id": "user-1", "cleanup": task_cleanup}]
