from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import video_billing
from lumen_core.models import VideoGeneration


class FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)


def _generation() -> VideoGeneration:
    return VideoGeneration(
        id="video-gen-1",
        user_id="user-1",
        action="t2v",
        model="seedance-2.0",
        provider_name="volcano-main",
        provider_kind="volcano",
        provider_task_id="upstream-1",
        prompt="make a clip",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        deadline_at=datetime.now(timezone.utc),
        idempotency_key="idem-1",
        request_fingerprint="f" * 64,
        est_token_upper=60_000,
        est_cost_micro=1_000,
    )


@pytest.mark.asyncio
async def test_resolve_video_billing_settles_success_with_actual_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def billing_enabled() -> bool:
        return True

    async def held_amount_for_ref(
        _session, user_id: str, ref_type: str, ref_id: str
    ) -> int:
        assert (user_id, ref_type, ref_id) == (
            "user-1",
            "video_generation",
            "video-gen-1",
        )
        return 1_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle_cost(
        _session,
        *,
        model: str,
        action: str,
        actual_total_tokens: int,
        resolution: str | None = None,
        pricing_variant: str | None = None,
        estimated_micro: int | None = None,
    ) -> int:
        assert (model, action, actual_total_tokens) == ("seedance-2.0", "t2v", 42_000)
        assert resolution == "720p"
        assert pricing_variant == "t2v_720p"
        assert estimated_micro == 1_000
        return 420

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-420, balance_after=9_580, hold_after=0)

    monkeypatch.setattr(
        video_billing.worker_billing, "billing_enabled", billing_enabled
    )
    monkeypatch.setattr(
        video_billing.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        video_billing.worker_billing,
        "allow_negative_balance",
        allow_negative_balance,
    )
    monkeypatch.setattr(video_billing, "settle_video_cost", settle_cost)
    monkeypatch.setattr(video_billing.billing_core, "settle", settle)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        _generation(),
        poll_result={"status": "succeeded", "usage_total_tokens": 42_000},
        reason="succeeded",
    )

    assert resolution.decision == "actual_usage_settle"
    assert resolution.actual_micro == 420
    assert resolution.actual_tokens == 42_000
    assert resolution.released is False
    assert calls[0][0] == "settle"
    assert calls[0][1]["actual_micro"] == 420
    assert calls[0][1]["idempotency_key"] == "video_generation:settle:video-gen-1"
    assert calls[0][1]["meta"]["billing_decision"] == "actual_usage_settle"
    assert session.info["lumen_post_commit_balance_cache"] == {"user-1": 9_580}
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_resolve_video_billing_uses_reference_video_pricing_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    generation = _generation()
    generation.action = "reference"
    generation.upstream_request = {
        "reference_media": [{"kind": "image"}, {"kind": "video"}]
    }
    calls: list[tuple[str, dict[str, object]]] = []

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 1_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle_cost(
        _session,
        *,
        model: str,
        action: str,
        actual_total_tokens: int,
        resolution: str | None = None,
        pricing_variant: str | None = None,
        estimated_micro: int | None = None,
    ) -> int:
        assert (model, action, actual_total_tokens) == (
            "seedance-2.0",
            "reference",
            42_000,
        )
        assert resolution == "720p"
        assert pricing_variant == "reference_video_720p"
        assert estimated_micro == 1_000
        return 840

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-840, balance_after=9_160, hold_after=0)

    monkeypatch.setattr(
        video_billing.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        video_billing.worker_billing,
        "allow_negative_balance",
        allow_negative_balance,
    )
    monkeypatch.setattr(video_billing, "settle_video_cost", settle_cost)
    monkeypatch.setattr(video_billing.billing_core, "settle", settle)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        generation,
        poll_result={"status": "succeeded", "usage_total_tokens": 42_000},
        reason="succeeded",
    )

    assert resolution.actual_micro == 840
    assert calls[0][1]["meta"]["pricing_variant"] == "reference_video_720p"
    assert session.added[0].details["pricing_variant"] == "reference_video_720p"


@pytest.mark.asyncio
async def test_resolve_video_billing_uses_fast_model_from_upstream_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    generation = _generation()
    generation.upstream_request = {
        "upstream_model": "doubao-seedance-2-0-fast-260128"
    }
    calls: list[tuple[str, dict[str, object]]] = []

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 1_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle_cost(
        _session,
        *,
        model: str,
        action: str,
        actual_total_tokens: int,
        resolution: str | None = None,
        pricing_variant: str | None = None,
        estimated_micro: int | None = None,
    ) -> int:
        assert (model, action, actual_total_tokens) == (
            "seedance-2.0-fast",
            "t2v",
            108_900,
        )
        assert resolution == "720p"
        assert pricing_variant == "t2v_720p"
        assert estimated_micro == 1_000
        return 4_029_300

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(
            amount_micro=-4_029_300,
            balance_after=5_970_700,
            hold_after=0,
        )

    monkeypatch.setattr(
        video_billing.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        video_billing.worker_billing,
        "allow_negative_balance",
        allow_negative_balance,
    )
    monkeypatch.setattr(video_billing, "settle_video_cost", settle_cost)
    monkeypatch.setattr(video_billing.billing_core, "settle", settle)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        generation,
        poll_result={"status": "succeeded", "usage_total_tokens": 108_900},
        reason="succeeded",
    )

    assert resolution.decision == "actual_usage_settle"
    assert resolution.actual_micro == 4_029_300
    assert calls[0][1]["meta"]["billing_model"] == "seedance-2.0-fast"


@pytest.mark.asyncio
async def test_resolve_video_billing_releases_when_upstream_not_billable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def billing_enabled() -> bool:
        return True

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 1_000

    async def release(_session, user_id: str, **kwargs):
        calls.append(("release", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=1_000, balance_after=10_000, hold_after=0)

    monkeypatch.setattr(
        video_billing.worker_billing, "billing_enabled", billing_enabled
    )
    monkeypatch.setattr(
        video_billing.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(video_billing.billing_core, "release", release)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        _generation(),
        poll_result={"status": "failed", "upstream_billable": False},
        reason="failed",
    )

    assert resolution.decision == "upstream_not_billable_release"
    assert resolution.actual_micro == 0
    assert resolution.released is True
    assert calls[0][0] == "release"
    assert calls[0][1]["idempotency_key"] == "video_generation:release:video-gen-1"
    assert calls[0][1]["meta"]["billing_decision"] == "upstream_not_billable_release"
    assert session.info["lumen_post_commit_balance_cache"] == {"user-1": 10_000}
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_resolve_video_billing_charges_success_when_usage_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 1_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-1_000, balance_after=9_000, hold_after=0)

    monkeypatch.setattr(
        video_billing.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        video_billing.worker_billing,
        "allow_negative_balance",
        allow_negative_balance,
    )
    monkeypatch.setattr(video_billing.billing_core, "settle", settle)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        _generation(),
        poll_result={"status": "succeeded"},
        reason="succeeded",
    )

    assert resolution.decision == "missing_usage_default_charge"
    assert resolution.actual_micro == 1_000
    assert resolution.actual_tokens is None
    assert resolution.released is False
    assert calls[0][1]["meta"]["billing_decision"] == "missing_usage_default_charge"
    assert calls[0][1]["meta"]["pricing_variant"] == "t2v_720p"
    assert session.added[0].details["decision"] == "missing_usage_default_charge"


@pytest.mark.asyncio
async def test_resolve_video_billing_charges_terminal_without_billable_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 1_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-1_000, balance_after=9_000, hold_after=0)

    monkeypatch.setattr(
        video_billing.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        video_billing.worker_billing,
        "allow_negative_balance",
        allow_negative_balance,
    )
    monkeypatch.setattr(video_billing.billing_core, "settle", settle)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        _generation(),
        poll_result={"status": "failed"},
        reason="failed",
    )

    assert resolution.decision == "unknown_default_charge"
    assert resolution.actual_micro == 1_000
    assert resolution.released is False
    assert calls[0][1]["idempotency_key"] == "video_generation:settle:video-gen-1"
    assert calls[0][1]["meta"]["billing_decision"] == "unknown_default_charge"


@pytest.mark.asyncio
async def test_resolve_video_billing_charges_failed_usage_without_billable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 1_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle_cost(
        _session,
        *,
        model: str,
        action: str,
        actual_total_tokens: int,
        resolution: str | None = None,
        pricing_variant: str | None = None,
        estimated_micro: int | None = None,
    ) -> int:
        assert (model, action, actual_total_tokens) == ("seedance-2.0", "t2v", 42_000)
        assert resolution == "720p"
        assert pricing_variant == "t2v_720p"
        assert estimated_micro == 1_000
        return 420

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-420, balance_after=9_580, hold_after=0)

    monkeypatch.setattr(
        video_billing.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        video_billing.worker_billing,
        "allow_negative_balance",
        allow_negative_balance,
    )
    monkeypatch.setattr(video_billing, "settle_video_cost", settle_cost)
    monkeypatch.setattr(video_billing.billing_core, "settle", settle)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        _generation(),
        poll_result={"status": "failed", "usage_total_tokens": 42_000},
        reason="failed",
    )

    assert resolution.decision == "failure_usage_settle"
    assert resolution.actual_micro == 420
    assert resolution.actual_tokens == 42_000
    assert resolution.released is False
    assert calls[0][1]["meta"]["actual_tokens"] == 42_000
