from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import video_artifacts, video_billing
from app.tasks.video_generation_parts import default_runtime as video_generation
from app.video_upstream_service import PollResult
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


def _pre_submit_generation() -> VideoGeneration:
    generation = _generation()
    generation.provider_task_id = None
    generation.attempt = 0
    generation.submission_epoch = 0
    generation.submit_started_at = None
    generation.diagnostics = {}
    return generation


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
async def test_resolve_video_billing_charges_full_cost_when_exceeding_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成本远超预估时必须全额转嫁——平台绝不吸收上游成本（新-1）。"""
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def billing_enabled() -> bool:
        return True

    async def held_amount_for_ref(_session, user_id, ref_type, ref_id) -> int:
        return 1_000

    async def allow_negative_balance() -> bool:
        return True

    async def settle_cost(_session, **_kwargs) -> int:
        raise video_billing.VideoBillingError(
            "video_cost_exceeds_estimate",
            "actual cost 40000 exceeds estimate 1000 by more than 3x",
            500,
            actual_micro=40_000,
        )

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-40_000, balance_after=0, hold_after=0)

    monkeypatch.setattr(
        video_billing.worker_billing, "billing_enabled", billing_enabled
    )
    monkeypatch.setattr(
        video_billing.worker_billing, "held_amount_for_ref", held_amount_for_ref
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

    # 关键断言：收全额 40_000，而不是退回 max(held, est)=1_000。
    assert resolution.actual_micro == 40_000
    assert resolution.decision == "actual_usage_settle"
    assert calls[0][1]["actual_micro"] == 40_000


@pytest.mark.asyncio
async def test_resolve_video_billing_falls_back_when_pricing_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """定价缺失（成本无从得知）仍走默认兜底，与超额是两种语义。"""
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def billing_enabled() -> bool:
        return True

    async def held_amount_for_ref(_session, user_id, ref_type, ref_id) -> int:
        return 1_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle_cost(_session, **_kwargs) -> int:
        raise video_billing.VideoBillingError(
            "video_pricing_missing",
            "missing enabled video pricing rule",
            503,
        )

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-1_000, balance_after=0, hold_after=0)

    monkeypatch.setattr(
        video_billing.worker_billing, "billing_enabled", billing_enabled
    )
    monkeypatch.setattr(
        video_billing.worker_billing, "held_amount_for_ref", held_amount_for_ref
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

    assert resolution.decision == "pricing_missing_default_charge"
    assert resolution.actual_micro == 1_000


@pytest.mark.asyncio
async def test_resolve_video_billing_rejects_invalid_usage_instead_of_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()

    async def held_amount_for_ref(
        _session, _user_id: str, _ref_type: str, _ref_id: str
    ) -> int:
        return 1_000

    async def settle_cost(_session, **_kwargs) -> int:
        raise video_billing.VideoBillingError(
            "video_cost_factor_out_of_range",
            "video billing factor total_tokens exceeds limit",
            500,
        )

    async def allow_negative_balance() -> bool:
        return False

    async def fail_settle(*_args, **_kwargs) -> None:
        raise AssertionError("invalid upstream usage must not be billed by fallback")

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
    monkeypatch.setattr(video_billing.billing_core, "settle", fail_settle)

    with pytest.raises(video_billing.VideoBillingError) as exc:
        await video_billing.resolve_video_billing(
            session,  # type: ignore[arg-type]
            _generation(),
            poll_result={"status": "succeeded", "usage_total_tokens": 42_000},
            reason="succeeded",
        )

    assert exc.value.code == "video_cost_factor_out_of_range"
    assert exc.value.status_code == 500


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
    generation.upstream_request = {"upstream_model": "doubao-seedance-2-0-fast-260128"}
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
async def test_resolve_video_billing_uses_mini_model_from_upstream_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    generation = _generation()
    generation.upstream_request = {"upstream_model": "doubao-seedance-2-0-mini-260615"}
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
            "seedance-2.0-mini",
            "t2v",
            108_900,
        )
        assert resolution == "720p"
        assert pricing_variant == "t2v_720p"
        assert estimated_micro == 1_000
        return 2_504_700

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(
            amount_micro=-2_504_700,
            balance_after=7_495_300,
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
    assert resolution.actual_micro == 2_504_700
    assert calls[0][1]["meta"]["billing_model"] == "seedance-2.0-mini"


@pytest.mark.asyncio
async def test_resolve_video_billing_charges_untrusted_upstream_not_billable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def billing_enabled() -> bool:
        return True

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 1_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-1_000, balance_after=9_000, hold_after=0)

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
    monkeypatch.setattr(video_billing.billing_core, "settle", settle)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        _generation(),
        poll_result={"status": "failed", "upstream_billable": False},
        reason="failed",
    )

    assert resolution.decision == "upstream_not_billable_untrusted_default_charge"
    assert resolution.actual_micro == 1_000
    assert resolution.released is False
    assert calls[0][0] == "settle"
    assert calls[0][1]["idempotency_key"] == "video_generation:settle:video-gen-1"
    assert (
        calls[0][1]["meta"]["billing_decision"]
        == "upstream_not_billable_untrusted_default_charge"
    )
    assert session.info["lumen_post_commit_balance_cache"] == {"user-1": 9_000}
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_resolve_video_billing_releases_for_pre_submit_no_cost_receipt(
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
        return SimpleNamespace(
            kind="release",
            amount_micro=1_000,
            balance_after=10_000,
            hold_after=0,
            meta=kwargs["meta"],
        )

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
        _pre_submit_generation(),
        poll_result={
            "status": "cancelled",
            "upstream_billable": False,
            "raw": {"reason": "pre_submit_cancel"},
        },
        reason="pre_submit_cancel",
    )

    assert resolution.decision == "upstream_not_billable_release"
    assert resolution.actual_micro == 0
    assert resolution.released is True
    assert calls[0][0] == "release"
    assert calls[0][1]["idempotency_key"] == "video_generation:release:video-gen-1"
    assert calls[0][1]["meta"]["billing_decision"] == "upstream_not_billable_release"
    assert session.info["lumen_post_commit_balance_cache"] == {"user-1": 10_000}


@pytest.mark.asyncio
@pytest.mark.parametrize("release_returns_settle", [False, True])
async def test_release_resolution_preserves_existing_settlement(
    monkeypatch: pytest.MonkeyPatch,
    release_returns_settle: bool,
) -> None:
    session = FakeSession()
    existing_settle = SimpleNamespace(
        kind="settle",
        amount_micro=-420,
        balance_after=9_580,
        hold_after=0,
        meta={
            "actual_micro": 420,
            "actual_tokens": 42_000,
            "billing_decision": "actual_usage_settle",
        },
    )

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 0

    async def release(*_args, **_kwargs):
        return existing_settle if release_returns_settle else None

    async def existing_ref_consumption_tx(*_args, **_kwargs):
        if release_returns_settle:
            raise AssertionError("direct non-release result must not be re-queried")
        return existing_settle

    monkeypatch.setattr(
        video_billing.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        video_billing.worker_billing,
        "existing_ref_consumption_tx",
        existing_ref_consumption_tx,
    )
    monkeypatch.setattr(video_billing.billing_core, "release", release)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        _pre_submit_generation(),
        poll_result={
            "status": "cancelled",
            "upstream_billable": False,
            "raw": {"reason": "pre_submit_cancel"},
        },
        reason="pre_submit_cancel",
    )

    assert resolution.tx is existing_settle
    assert resolution.released is False
    assert resolution.actual_micro == 420
    assert resolution.actual_tokens == 42_000
    assert resolution.decision == "actual_usage_settle"
    assert session.info == {}
    assert session.added == []


@pytest.mark.asyncio
async def test_pre_submit_cancel_receipt_with_unknown_delivery_settles_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []
    generation = _pre_submit_generation()
    generation.attempt = 1
    generation.submission_epoch = 1
    generation.submit_started_at = datetime.now(timezone.utc)
    generation.provider_idempotency_key = f"video:{generation.id}"
    generation.diagnostics = {
        "submit_delivery_state": "unknown",
        "submit_delivery_history": [
            {
                "state": "unknown",
                "reason": "submit_exception",
                "submission_epoch": 1,
            }
        ],
    }

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 1_500

    async def allow_negative_balance() -> bool:
        return False

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-1_500, balance_after=0, hold_after=0)

    async def fail_release(*_args, **_kwargs):
        raise AssertionError("unknown delivery must not release")

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
    monkeypatch.setattr(video_billing.billing_core, "release", fail_release)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        generation,
        poll_result={
            "status": "cancelled",
            "upstream_billable": False,
            "raw": {"reason": "pre_submit_cancel"},
        },
        reason="pre_submit_cancel",
    )

    assert resolution.released is False
    assert resolution.decision == "upstream_not_billable_untrusted_default_charge"
    assert resolution.actual_micro == 1_500
    assert calls[0][0] == "settle"
    assert calls[0][1]["meta"]["submit_delivery_state"] == "unknown"
    assert calls[0][1]["meta"]["upstream_cost_knowledge"] == "unknown"


@pytest.mark.asyncio
async def test_resolve_video_billing_charges_invalid_artifact_after_upstream_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    operations: list[tuple[str, dict[str, object]]] = []

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
        operations.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-420, balance_after=9_580, hold_after=0)

    async def release(*_args, **_kwargs):
        pytest.fail("billable upstream success must not release the video hold")

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
    monkeypatch.setattr(video_billing.billing_core, "release", release)

    poll = video_generation._invalid_video_artifact_poll(  # noqa: SLF001
        PollResult(
            status="succeeded",
            usage_total_tokens=42_000,
            upstream_billable=True,
            raw={"provider_state": "succeeded"},
        ),
        video_artifacts.InvalidVideoArtifactError(
            "no video stream",
            diagnostics={"probe_error": "no video stream"},
        ),
    )

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        _generation(),
        poll_result=poll,
        reason=poll.raw["reason"],
    )

    assert poll.upstream_billable is True
    assert poll.raw["reason"] == "invalid_video_artifact_after_upstream_success"
    assert resolution.decision == "failure_usage_settle"
    assert resolution.actual_micro == 420
    assert resolution.actual_tokens == 42_000
    assert resolution.released is False
    assert [operation for operation, _details in operations] == ["settle"]
    assert operations[0][1]["actual_micro"] == 420
    assert operations[0][1]["meta"]["billing_decision"] == "failure_usage_settle"
    assert operations[0][1]["meta"]["actual_tokens"] == 42_000
    assert (
        operations[0][1]["meta"]["reason"]
        == "invalid_video_artifact_after_upstream_success"
    )


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


@pytest.mark.asyncio
async def test_resolve_video_billing_succeeded_with_release_receipt_still_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游已经跑成功，即使带着 pre_submit 收据也不许 release（决策表 PROVEN_PRESENT）。"""
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 1_000

    async def allow_negative_balance() -> bool:
        return False

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-1_000, balance_after=9_000, hold_after=0)

    async def fail_release(*_args, **_kwargs):
        raise AssertionError("upstream already produced a result; release is forbidden")

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
    monkeypatch.setattr(video_billing.billing_core, "release", fail_release)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        _generation(),
        poll_result={
            "status": "succeeded",
            "upstream_billable": True,
            "raw": {"reason": "pre_submit_cancel"},
        },
        reason="pre_submit_cancel",
    )

    assert resolution.released is False
    assert resolution.decision == "missing_usage_default_charge"
    assert resolution.actual_micro == 1_000
    assert calls[0][1]["meta"]["upstream_cost_knowledge"] == "proven_present"


@pytest.mark.asyncio
async def test_resolve_video_billing_unknown_submit_settles_not_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E-2：submit 结果不可知（billable=None）必须默认结算。"""
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 1_500

    async def allow_negative_balance() -> bool:
        return False

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-1_500, balance_after=0, hold_after=0)

    async def fail_release(*_args, **_kwargs):
        raise AssertionError("unknown upstream cost must never release")

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
    monkeypatch.setattr(video_billing.billing_core, "release", fail_release)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        _generation(),
        poll_result={
            "status": "failed",
            "upstream_billable": None,
            "raw": {"reason": "submit_unknown_timeout"},
        },
        reason="submit_unknown_timeout",
    )

    assert resolution.released is False
    assert resolution.decision == "unknown_default_charge"
    assert resolution.actual_micro == 1_500
    assert calls[0][1]["meta"]["upstream_cost_knowledge"] == "unknown"


@pytest.mark.asyncio
async def test_resolve_video_billing_billable_false_without_receipt_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E-1：上游声称未计费但拿不出本地收据 → 不可信，仍然结算。"""
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 800

    async def allow_negative_balance() -> bool:
        return False

    async def settle(_session, user_id: str, **kwargs):
        calls.append(("settle", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(amount_micro=-1_000, balance_after=0, hold_after=0)

    async def fail_release(*_args, **_kwargs):
        raise AssertionError("untrusted billable=False must not release")

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
    monkeypatch.setattr(video_billing.billing_core, "release", fail_release)

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        _generation(),
        poll_result={
            "status": "failed",
            "upstream_billable": False,
            "raw": {"reason": "poll_reported_failure"},
        },
        reason="poll_reported_failure",
    )

    assert resolution.released is False
    assert resolution.decision == "upstream_not_billable_untrusted_default_charge"
    # hold=800 < est=1000，默认金额取较大者，绝不少收。
    assert resolution.actual_micro == 1_000
    assert calls[0][1]["meta"]["upstream_cost_knowledge"] == "unknown"


@pytest.mark.asyncio
async def test_resolve_video_billing_release_records_proven_absent_knowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    calls: list[tuple[str, dict[str, object]]] = []

    async def held_amount_for_ref(*_args, **_kwargs) -> int:
        return 1_000

    async def release(_session, user_id: str, **kwargs):
        calls.append(("release", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(
            kind="release",
            amount_micro=1_000,
            balance_after=10_000,
            hold_after=0,
            meta=kwargs["meta"],
        )

    async def fail_settle(*_args, **_kwargs):
        raise AssertionError("proven-absent upstream cost must release, not settle")

    monkeypatch.setattr(
        video_billing.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(video_billing.billing_core, "release", release)
    monkeypatch.setattr(video_billing.billing_core, "settle", fail_settle)

    generation = _generation()
    generation.provider_task_id = None
    generation.attempt = 1
    generation.submission_epoch = 1
    generation.diagnostics = {
        "submit_delivery_state": "proven_absent",
        "submit_delivery_history": [
            {
                "state": "proven_absent",
                "reason": "submit_exception",
                "submission_epoch": 1,
            }
        ],
    }

    resolution = await video_billing.resolve_video_billing(
        session,  # type: ignore[arg-type]
        generation,
        poll_result={
            "status": "failed",
            "upstream_billable": False,
            "raw": {"reason": "submit_failed_before_upstream_cost"},
        },
        reason="submit_failed",
    )

    assert resolution.released is True
    assert resolution.decision == "upstream_not_billable_release"
    assert calls[0][1]["meta"]["upstream_cost_knowledge"] == "proven_absent"
