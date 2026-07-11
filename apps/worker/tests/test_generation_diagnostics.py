import pytest

from app.tasks import generation
from app.tasks.generation_parts import diagnostics


def test_diagnostics_facade_keeps_pure_helper_aliases() -> None:
    assert generation._compact_diag_value is diagnostics.compact_diag_value
    assert (
        generation._build_generation_diagnostics
        is diagnostics.build_generation_diagnostics
    )


def test_stage_timer_facade_uses_current_monotonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation.time, "monotonic", lambda: 10.75)

    timer = generation._StageTimer()
    timer.add_elapsed("upstream", 10.0)
    timer.set_ms("queue", -5)

    assert timer.snapshot() == {"upstream": 750, "queue": 0}


def test_diagnostics_facade_injects_current_redis_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []

    def fake_redis_text(value: object) -> str | None:
        seen.append(value)
        return f"decoded:{value!r}" if value is not None else None

    monkeypatch.setattr(generation, "_redis_text", fake_redis_text)

    attempt = generation._provider_attempt_from_progress(
        {"provider": b"provider", "reason": b"retry"},
        status="failover",
        attempt_epoch=3,
    )
    selected = generation._request_event_provider_from_attempts(
        [{"actual_provider": b"winner", "status": "used"}]
    )

    assert attempt["provider"] == "decoded:b'provider'"
    assert attempt["reason"] == "decoded:b'retry'"
    assert selected == "decoded:b'winner'"
    assert seen == [b"provider", b"retry", b"winner"]


def test_safe_generation_error_summary_compacts_message() -> None:
    summary = generation._safe_generation_error_summary(
        code="upstream_error",
        status_code=502,
        message="first line\n" + "x" * 500,
    )

    parts = summary.split(" · ")
    assert parts[:2] == ["upstream_error", "http 502"]
    assert "\n" not in parts[2]
    assert len(parts[2]) == 300


def test_image_requested_params_snapshot_whitelists_and_compacts() -> None:
    snapshot = generation._image_requested_params_snapshot(
        {
            "fast": True,
            "billing_tier": list(range(30)),
            "unknown": "do-not-leak",
        },
        size="1024x1024",
        aspect_ratio="1:1",
        action="generate",
        input_count=2,
        has_mask=False,
    )

    assert snapshot["fast"] is True
    assert snapshot["billing_tier"] == list(range(20))
    assert "unknown" not in snapshot


def test_build_generation_diagnostics_redacts_provider_details_by_default() -> None:
    diagnostics = generation._build_generation_diagnostics(
        requested_params={"size": "1024x1024"},
        provider="secret-provider",
        actual_endpoint="https://internal.example/v1/images",
        upstream_route="image_jobs",
        debug_id="task-123",
        provider_attempts=[
            {
                "provider": "secret-provider",
                "endpoint": "https://internal.example/v1/responses",
                "route": "responses",
                "status": "failover",
                "error_summary": "line one\n" + "x" * 400,
            }
        ],
    )

    assert "provider" not in diagnostics
    assert "actual_provider" not in diagnostics
    assert "actual_endpoint" not in diagnostics
    assert "debug_id" not in diagnostics
    assert diagnostics["failover"] is True
    assert diagnostics["failover_count"] == 1
    attempt = diagnostics["provider_attempts"][0]
    assert "provider" not in attempt
    assert "endpoint" not in attempt
    assert attempt["status"] == "failover"
    assert "\n" not in attempt["error_summary"]
    assert len(attempt["error_summary"]) == 300


def test_build_generation_diagnostics_can_expose_provider_details() -> None:
    diagnostics = generation._build_generation_diagnostics(
        requested_params={"size": "1024x1024"},
        provider="internal-provider",
        actual_endpoint="https://internal.example/v1/images",
        debug_id="task-123",
        provider_attempts=[
            {
                "provider": "internal-provider",
                "endpoint": "https://internal.example/v1/images",
                "status": "used",
            }
        ],
        expose_provider_diagnostics=True,
    )

    assert diagnostics["provider"] == "internal-provider"
    assert diagnostics["actual_provider"] == "internal-provider"
    assert diagnostics["actual_endpoint"] == "https://internal.example/v1/images"
    assert diagnostics["debug_id"] == "task-123"
    assert (
        diagnostics["provider_attempts"][0]["endpoint"]
        == "https://internal.example/v1/images"
    )


def test_sanitize_upstream_request_preserves_request_event_provider() -> None:
    upstream_request = generation._sanitize_generation_upstream_request(
        {
            "provider": "secret-provider",
            "actual_provider": "secret-provider",
            "request_event_provider": "secret-provider",
            "actual_endpoint": "https://internal.example/v1/images",
            "upstream_route": "dual_race",
        },
        expose_provider_diagnostics=False,
    )

    assert "provider" not in upstream_request
    assert "actual_provider" not in upstream_request
    assert "actual_endpoint" not in upstream_request
    assert upstream_request["request_event_provider"] == "secret-provider"
    assert upstream_request["upstream_route"] == "dual_race"


def test_request_event_provider_from_attempts_prefers_used_attempt() -> None:
    assert (
        generation._request_event_provider_from_attempts(
            [
                {"provider": "failed-provider", "status": "failover"},
                {"provider": "winner-provider", "status": "used"},
            ]
        )
        == "winner-provider"
    )
    assert (
        generation._request_event_provider_from_attempts(
            [{"provider": "last-known-provider", "status": "failover"}]
        )
        == "last-known-provider"
    )
    assert (
        generation._request_event_provider_from_attempts(
            [{"provider": "dual_race", "status": "used"}]
        )
        is None
    )
