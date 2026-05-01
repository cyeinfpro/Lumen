"""dual_race 路径执行器测试。

覆盖 `_dual_race_image_action` 的核心不变量：
- 任一路成功 → cancel 另一路 → 返回胜方结果
- 一路失败 + 另一路成功 → 返回成功路结果
- 两路都失败 → raise，error_code=fallback_lanes_failed，message 含 [image2]/[responses]
- UpstreamCancelled 透传 → 两路都被 cancel
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app import upstream
from app.upstream import UpstreamCancelled, UpstreamError


async def _first_image_result(
    image_iter: Any,
) -> tuple[str, str | None]:
    async for item in image_iter:
        return item
    raise AssertionError("image iterator yielded no result")


@pytest.mark.asyncio
async def test_dual_race_image2_wins_cancels_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    async def fake_image2(**_kw: Any) -> tuple[str, str | None]:
        await asyncio.sleep(0.01)
        return ("img-from-image2", "https://x/image2.png")

    async def fake_responses(**_kw: Any) -> tuple[str, str | None]:
        try:
            await asyncio.sleep(5.0)
            return ("img-from-responses", None)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(upstream, "_direct_generate_image_with_failover", fake_image2)
    monkeypatch.setattr(
        upstream, "_responses_image_stream_with_failover", fake_responses
    )

    image_iter = upstream._dual_race_image_action(
        action="generate",
        prompt="hi",
        size="1024x1024",
        images=None,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        model=None,
        progress_callback=None,
        provider_override=None,
    )
    result = await _first_image_result(image_iter)
    await image_iter.aclose()
    assert result == ("img-from-image2", "https://x/image2.png")
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_dual_race_responses_wins_when_image2_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_image2(**_kw: Any) -> tuple[str, str | None]:
        raise UpstreamError("image2 server_error", error_code="server_error")

    async def fake_responses(**_kw: Any) -> tuple[str, str | None]:
        await asyncio.sleep(0.01)
        return ("img-from-responses", "https://x/responses.png")

    monkeypatch.setattr(upstream, "_direct_edit_image_with_failover", fake_image2)
    monkeypatch.setattr(
        upstream, "_responses_image_stream_with_failover", fake_responses
    )

    result = await _first_image_result(
        upstream._dual_race_image_action(
            action="edit",
            prompt="hi",
            size="2160x3840",
            images=[b"\x89PNG\r\n\x1a\n" + b"\x00" * 32],
            n=1,
            quality="high",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
            provider_override=None,
        )
    )
    assert result == ("img-from-responses", "https://x/responses.png")


@pytest.mark.asyncio
async def test_dual_race_both_lanes_fail_merges_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_image2(**_kw: Any) -> tuple[str, str | None]:
        raise UpstreamError(
            "direct edit boom", error_code="all_direct_image_providers_failed"
        )

    async def fake_responses(**_kw: Any) -> tuple[str, str | None]:
        raise UpstreamError("responses boom", error_code="all_providers_failed")

    monkeypatch.setattr(upstream, "_direct_edit_image_with_failover", fake_image2)
    monkeypatch.setattr(
        upstream, "_responses_image_stream_with_failover", fake_responses
    )

    with pytest.raises(UpstreamError) as exc_info:
        await _first_image_result(
            upstream._dual_race_image_action(
                action="edit",
                prompt="hi",
                size="1024x1024",
                images=[b"\x89PNG"],
                n=1,
                quality="high",
                output_format=None,
                output_compression=None,
                background=None,
                moderation=None,
                model=None,
                progress_callback=None,
                provider_override=None,
            )
        )
    msg = str(exc_info.value)
    assert "[image2]" in msg
    assert "[responses]" in msg
    assert exc_info.value.error_code == "fallback_lanes_failed"


@pytest.mark.asyncio
async def test_dual_race_caller_cancel_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两路都拿到 UpstreamCancelled → 透传，不进 race 合并逻辑。"""
    image2_cancelled = asyncio.Event()
    responses_cancelled = asyncio.Event()

    async def fake_image2(**_kw: Any) -> tuple[str, str | None]:
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            image2_cancelled.set()
            raise
        return ("never", None)

    async def fake_responses(**_kw: Any) -> tuple[str, str | None]:
        # 第一路就抛 UpstreamCancelled（模拟调用方主动取消）
        raise UpstreamCancelled("caller cancelled")

    monkeypatch.setattr(upstream, "_direct_generate_image_with_failover", fake_image2)
    monkeypatch.setattr(
        upstream, "_responses_image_stream_with_failover", fake_responses
    )

    with pytest.raises(UpstreamCancelled):
        await _first_image_result(
            upstream._dual_race_image_action(
                action="generate",
                prompt="hi",
                size="1024x1024",
                images=None,
                n=1,
                quality="high",
                output_format=None,
                output_compression=None,
                background=None,
                moderation=None,
                model=None,
                progress_callback=None,
                provider_override=None,
            )
        )
    # image2 lane 应该已经被 cancel
    assert image2_cancelled.is_set()
    _ = responses_cancelled  # 仅用于明示语义


@pytest.mark.asyncio
async def test_dual_race_provider_override_skips_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider_override 给定时不进 race，直接走 responses 单路（原有语义）。"""
    image2_called = False

    async def fake_image2(**_kw: Any) -> tuple[str, str | None]:
        nonlocal image2_called
        image2_called = True
        return ("never", None)

    async def fake_responses(**kw: Any) -> tuple[str, str | None]:
        # 收到 provider_override 透传
        assert kw.get("provider_override") is not None
        return ("img-from-responses", None)

    monkeypatch.setattr(upstream, "_direct_generate_image_with_failover", fake_image2)
    monkeypatch.setattr(
        upstream, "_responses_image_stream_with_failover", fake_responses
    )

    sentinel = object()
    result = await _first_image_result(
        upstream._dual_race_image_action(
            action="generate",
            prompt="hi",
            size="1024x1024",
            images=None,
            n=1,
            quality="high",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
            provider_override=sentinel,
        )
    )
    assert result == ("img-from-responses", None)
    assert image2_called is False


class _FakePool:
    def __init__(self, *, has_image_jobs: bool) -> None:
        self._has = has_image_jobs

    def has_image_jobs_provider(self) -> bool:
        return self._has


# ---------------------------------------------------------------------------
# _resolve_image_primary_route：兼容旧 primary_route，但不再做 provider 全局升级
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_route_dual_race_wins_over_image_jobs_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 primary_route=dual_race 仍映射到兼容标签 dual_race。"""
    async def fake_resolve(key: str) -> str | None:
        if key == "image.primary_route":
            return "dual_race"
        return None

    async def fake_get_pool() -> _FakePool:
        return _FakePool(has_image_jobs=True)

    monkeypatch.setattr(upstream, "resolve", fake_resolve)
    from app import provider_pool as pp

    monkeypatch.setattr(pp, "get_pool", fake_get_pool)

    route = await upstream._resolve_image_primary_route()
    assert route == "dual_race"


@pytest.mark.asyncio
async def test_resolve_route_image_jobs_auto_when_provider_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧兼容标签不再被 provider opt-in 全局升级成 image_jobs。"""
    async def fake_resolve(key: str) -> str | None:
        return None  # 没设全局

    async def fake_get_pool() -> _FakePool:
        return _FakePool(has_image_jobs=True)

    monkeypatch.setattr(upstream, "resolve", fake_resolve)
    from app import provider_pool as pp

    monkeypatch.setattr(pp, "get_pool", fake_get_pool)

    route = await upstream._resolve_image_primary_route()
    assert route == "responses"


@pytest.mark.asyncio
async def test_resolve_route_responses_default_no_provider_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没设 dual_race 且 provider 没勾 image_jobs → 默认 responses。"""
    async def fake_resolve(key: str) -> str | None:
        return None

    async def fake_get_pool() -> _FakePool:
        return _FakePool(has_image_jobs=False)

    monkeypatch.setattr(upstream, "resolve", fake_resolve)
    from app import provider_pool as pp

    monkeypatch.setattr(pp, "get_pool", fake_get_pool)

    route = await upstream._resolve_image_primary_route()
    assert route == "responses"


@pytest.mark.asyncio
async def test_resolve_route_dual_race_falls_through_to_image2_responses_when_no_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dual_race + 没 provider 勾 image_jobs → 仍返回 dual_race（让 caller 走 image2+responses）。"""
    async def fake_resolve(key: str) -> str | None:
        if key == "image.primary_route":
            return "dual_race"
        return None

    async def fake_get_pool() -> _FakePool:
        return _FakePool(has_image_jobs=False)

    monkeypatch.setattr(upstream, "resolve", fake_resolve)
    from app import provider_pool as pp

    monkeypatch.setattr(pp, "get_pool", fake_get_pool)

    route = await upstream._resolve_image_primary_route()
    assert route == "dual_race"


# ---------------------------------------------------------------------------
# image_jobs dual_race（per-provider channel dispatch）
#
# channel=auto 时，选中的 Provider 支持 image_jobs_enabled 才会委托到
# _dual_race_image_jobs_action；不支持则走 stream dual_race。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_dual_race_uses_image_jobs_when_selected_provider_supports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """channel=auto + selected provider supports jobs → dual_race uses image-job race."""
    image2_called = False

    async def fake_image2(**_kw: Any) -> tuple[str, str | None]:
        nonlocal image2_called
        image2_called = True
        return ("never", None)

    async def fake_image_job(*, endpoint_override: str, **_kw: Any) -> tuple[str, str | None]:
        # 每条 lane 给一个明显的 fingerprint 以便断言
        await asyncio.sleep(0.01 if endpoint_override == "generations" else 0.05)
        return (f"img-from-{endpoint_override}", None)

    monkeypatch.setattr(upstream, "_direct_generate_image_with_failover", fake_image2)
    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)

    image_iter = upstream._run_image_once_for_provider(
        action="generate",
        provider=SimpleNamespace(name="jobs", image_jobs_enabled=True),
        channel="auto",
        engine="dual_race",
        prompt="hi",
        size="1024x1024",
        images=None,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        model=None,
        progress_callback=None,
    )
    result = await _first_image_result(image_iter)
    await image_iter.aclose()
    # generations 提前完成（0.01s vs 0.05s）→ winner
    assert result == ("img-from-generations", None)
    # image2 路径不再被触发
    assert image2_called is False


@pytest.mark.asyncio
async def test_dispatch_auto_dual_race_streams_when_provider_has_no_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_job_called = False

    async def fake_image2(**_kw: Any) -> tuple[str, str | None]:
        await asyncio.sleep(0.01)
        return ("img-from-image2", None)

    async def fake_responses(**_kw: Any) -> tuple[str, str | None]:
        await asyncio.sleep(0.05)
        return ("img-from-responses", None)

    async def fake_image_job(**_kw: Any) -> tuple[str, str | None]:
        nonlocal image_job_called
        image_job_called = True
        return ("never", None)

    monkeypatch.setattr(upstream, "_direct_generate_image_with_failover", fake_image2)
    monkeypatch.setattr(
        upstream, "_responses_image_stream_with_failover", fake_responses
    )
    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)

    image_iter = upstream._run_image_once_for_provider(
        action="generate",
        provider=SimpleNamespace(name="stream", image_jobs_enabled=False),
        channel="auto",
        engine="dual_race",
        prompt="hi",
        size="1024x1024",
        images=None,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        model=None,
        progress_callback=None,
    )
    result = await _first_image_result(image_iter)
    await image_iter.aclose()
    assert result == ("img-from-image2", None)
    assert image_job_called is False


@pytest.mark.asyncio
async def test_dispatch_image_jobs_only_rejects_provider_without_jobs() -> None:
    with pytest.raises(UpstreamError) as exc_info:
        await _first_image_result(
            upstream._run_image_once_for_provider(
                action="generate",
                provider=SimpleNamespace(name="plain", image_jobs_enabled=False),
                channel="image_jobs_only",
                engine="responses",
                prompt="hi",
                size="1024x1024",
                images=None,
                n=1,
                quality="high",
                output_format=None,
                output_compression=None,
                background=None,
                moderation=None,
                model=None,
                progress_callback=None,
            )
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.error_code == "all_accounts_failed"


@pytest.mark.asyncio
async def test_dispatch_image_jobs_provider_failover_continues_on_wrapped_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = [
        SimpleNamespace(name="acc1", image_jobs_enabled=True),
        SimpleNamespace(name="acc2", image_jobs_enabled=True),
    ]
    calls: list[str] = []

    async def fake_channel() -> str:
        return "auto"

    async def fake_engine() -> str:
        return "responses"

    async def fake_candidates(_provider_override: Any | None) -> list[Any]:
        return providers

    async def fake_image_job(*, provider_override: Any, **_kw: Any) -> tuple[str, str | None]:
        calls.append(provider_override.name)
        if provider_override.name == "acc1":
            raise UpstreamError(
                "all 1 image job providers failed",
                status_code=200,
                error_code="all_direct_image_providers_failed",
            )
        return ("img-from-acc2", None)

    monkeypatch.setattr(upstream, "_resolve_image_channel", fake_channel)
    monkeypatch.setattr(upstream, "_resolve_image_engine", fake_engine)
    monkeypatch.setattr(upstream, "_image_dispatch_candidates", fake_candidates)
    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)

    progress_events: list[dict[str, Any]] = []
    result = await _first_image_result(
        upstream._dispatch_image(
            action="generate",
            prompt="hi",
            size="1024x1024",
            images=None,
            n=1,
            quality="high",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=progress_events.append,
            provider_override=None,
        )
    )

    assert result == ("img-from-acc2", None)
    assert calls == ["acc1", "acc2"]
    failover = [e for e in progress_events if e.get("type") == "provider_failover"]
    assert len(failover) == 1
    assert failover[0]["from_provider"] == "acc1"


@pytest.mark.parametrize(
    "error_code",
    ["moderation_blocked", "content_policy_violation", "safety_violation"],
)
@pytest.mark.asyncio
async def test_dispatch_image_jobs_provider_failover_continues_on_safety_error(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
) -> None:
    providers = [
        SimpleNamespace(name="acc1", image_jobs_enabled=True),
        SimpleNamespace(name="acc2", image_jobs_enabled=True),
    ]
    calls: list[str] = []

    async def fake_channel() -> str:
        return "auto"

    async def fake_engine() -> str:
        return "responses"

    async def fake_candidates(_provider_override: Any | None) -> list[Any]:
        return providers

    async def fake_image_job(*, provider_override: Any, **_kw: Any) -> tuple[str, str | None]:
        calls.append(provider_override.name)
        if provider_override.name == "acc1":
            raise UpstreamError(
                "blocked by this upstream",
                status_code=200,
                error_code=error_code,
            )
        return ("img-from-acc2", None)

    monkeypatch.setattr(upstream, "_resolve_image_channel", fake_channel)
    monkeypatch.setattr(upstream, "_resolve_image_engine", fake_engine)
    monkeypatch.setattr(upstream, "_image_dispatch_candidates", fake_candidates)
    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)

    result = await _first_image_result(
        upstream._dispatch_image(
            action="generate",
            prompt="hi",
            size="1024x1024",
            images=None,
            n=1,
            quality="high",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
            provider_override=None,
        )
    )

    assert result == ("img-from-acc2", None)
    assert calls == ["acc1", "acc2"]


@pytest.mark.asyncio
async def test_dispatch_stream_only_dual_race_ignores_jobs_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_job_called = False

    async def fake_image2(**_kw: Any) -> tuple[str, str | None]:
        await asyncio.sleep(0.01)
        return ("img-from-image2", None)

    async def fake_responses(**_kw: Any) -> tuple[str, str | None]:
        await asyncio.sleep(0.05)
        return ("img-from-responses", None)

    async def fake_image_job(**_kw: Any) -> tuple[str, str | None]:
        nonlocal image_job_called
        image_job_called = True
        return ("never", None)

    monkeypatch.setattr(upstream, "_direct_generate_image_with_failover", fake_image2)
    monkeypatch.setattr(
        upstream, "_responses_image_stream_with_failover", fake_responses
    )
    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)

    image_iter = upstream._run_image_once_for_provider(
        action="generate",
        provider=SimpleNamespace(name="jobs", image_jobs_enabled=True),
        channel="stream_only",
        engine="dual_race",
        prompt="hi",
        size="1024x1024",
        images=None,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        model=None,
        progress_callback=None,
    )
    result = await _first_image_result(image_iter)
    await image_iter.aclose()
    assert result == ("img-from-image2", None)
    assert image_job_called is False


@pytest.mark.asyncio
async def test_image_jobs_dual_race_winner_then_bonus_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """winner yield 后 loser 在 grace 内成功 → 二次 yield bonus 图。"""
    async def fake_image_job(*, endpoint_override: str, **_kw: Any) -> tuple[str, str | None]:
        if endpoint_override == "generations":
            await asyncio.sleep(0.01)
            return ("winner-img", None)
        # responses lane 稍后完成
        await asyncio.sleep(0.05)
        return ("bonus-img", None)

    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)
    monkeypatch.setattr(
        upstream, "_DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S", 5.0,
    )

    image_iter = upstream._dual_race_image_jobs_action(
        action="generate",
        prompt="hi",
        size="1024x1024",
        images=None,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        model=None,
        progress_callback=None,
    )
    results = []
    async for item in image_iter:
        results.append(item)
    assert results == [("winner-img", None), ("bonus-img", None)]


@pytest.mark.asyncio
async def test_image_jobs_dual_race_loser_fails_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """winner 成功 / loser 在 grace 内失败 → 只 yield 一次（吞 loser 异常）。"""
    async def fake_image_job(*, endpoint_override: str, **_kw: Any) -> tuple[str, str | None]:
        if endpoint_override == "generations":
            await asyncio.sleep(0.01)
            return ("winner-img", None)
        await asyncio.sleep(0.02)
        raise UpstreamError("loser boom", error_code="server_error")

    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)
    monkeypatch.setattr(
        upstream, "_DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S", 5.0,
    )

    image_iter = upstream._dual_race_image_jobs_action(
        action="generate",
        prompt="hi",
        size="1024x1024",
        images=None,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        model=None,
        progress_callback=None,
    )
    results = []
    async for item in image_iter:
        results.append(item)
    assert results == [("winner-img", None)]


@pytest.mark.asyncio
async def test_image_jobs_dual_race_loser_grace_timeout_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """winner 完成 / loser 超过 grace → cancel 且静默吞，只 yield 一次。"""
    loser_cancelled = asyncio.Event()

    async def fake_image_job(*, endpoint_override: str, **_kw: Any) -> tuple[str, str | None]:
        if endpoint_override == "generations":
            await asyncio.sleep(0.01)
            return ("winner-img", None)
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            loser_cancelled.set()
            raise
        return ("never", None)

    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)
    # 把 grace 收紧到 50ms 触发超时
    monkeypatch.setattr(upstream, "_DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S", 0.05)

    image_iter = upstream._dual_race_image_jobs_action(
        action="generate",
        prompt="hi",
        size="1024x1024",
        images=None,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        model=None,
        progress_callback=None,
    )
    results = []
    async for item in image_iter:
        results.append(item)
    assert results == [("winner-img", None)]
    assert loser_cancelled.is_set()


@pytest.mark.asyncio
async def test_image_jobs_dual_race_both_lanes_fail_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两路都失败 → 抛 fallback_lanes_failed，message 含两条 lane 标识。"""
    async def fake_image_job(*, endpoint_override: str, **_kw: Any) -> tuple[str, str | None]:
        raise UpstreamError(
            f"{endpoint_override} boom",
            error_code="all_direct_image_providers_failed",
        )

    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)

    with pytest.raises(UpstreamError) as exc_info:
        await _first_image_result(
            upstream._dual_race_image_jobs_action(
                action="generate",
                prompt="hi",
                size="1024x1024",
                images=None,
                n=1,
                quality="high",
                output_format=None,
                output_compression=None,
                background=None,
                moderation=None,
                model=None,
                progress_callback=None,
            )
        )
    msg = str(exc_info.value)
    assert "image_jobs:generations" in msg
    assert "image_jobs:responses" in msg
    assert exc_info.value.error_code == "fallback_lanes_failed"


@pytest.mark.asyncio
async def test_image_jobs_dual_race_4k_still_races_both_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4K 也跑双 lane：号池把不同 endpoint 分到不同账号，不存在同账号双 4K 风险。

    对比 _race_responses_image：那条路径两条 lane 都打到同一 provider 同一账号，
    所以 >_RACE_SINGLE_LANE_PIXELS 必须收回单 lane。image_jobs dual_race 的两条
    lane 经号池上游分发到两个账号，4K race 反而是收益最大的场景。
    """
    calls: list[str] = []

    async def fake_image_job(*, endpoint_override: str, **_kw: Any) -> tuple[str, str | None]:
        calls.append(endpoint_override)
        if endpoint_override == "generations":
            await asyncio.sleep(0.01)
            return ("img-4k-generations", None)
        await asyncio.sleep(0.05)
        return ("img-4k-responses-bonus", None)

    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)
    monkeypatch.setattr(upstream, "_DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_4K_S", 5.0)

    image_iter = upstream._dual_race_image_jobs_action(
        action="generate",
        prompt="hi",
        size="3840x2160",
        images=None,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        model=None,
        progress_callback=None,
    )
    results = [item async for item in image_iter]
    # 4K 同样 race：generations winner + responses bonus
    assert results == [
        ("img-4k-generations", None),
        ("img-4k-responses-bonus", None),
    ]
    assert sorted(calls) == ["generations", "responses"]


@pytest.mark.asyncio
async def test_image_jobs_dual_race_caller_cancel_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一路抛 UpstreamCancelled → 透传，另一路被 finally 段 cancel。"""
    other_cancelled = asyncio.Event()

    async def fake_image_job(*, endpoint_override: str, **_kw: Any) -> tuple[str, str | None]:
        if endpoint_override == "responses":
            raise UpstreamCancelled("caller cancelled")
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            other_cancelled.set()
            raise
        return ("never", None)

    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)

    with pytest.raises(UpstreamCancelled):
        await _first_image_result(
            upstream._dual_race_image_jobs_action(
                action="generate",
                prompt="hi",
                size="1024x1024",
                images=None,
                n=1,
                quality="high",
                output_format=None,
                output_compression=None,
                background=None,
                moderation=None,
                model=None,
                progress_callback=None,
            )
        )
    assert other_cancelled.is_set()


@pytest.mark.asyncio
async def test_image_jobs_dual_race_progress_only_from_generations_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """progress: generations lane 透真实事件，responses lane 只透 provider_used。"""
    events: list[dict[str, Any]] = []

    async def fake_image_job(
        *, endpoint_override: str, progress_callback: Any, **_kw: Any
    ) -> tuple[str, str | None]:
        # 模拟 _image_job_with_failover success 推 provider_used + final_image + completed
        if progress_callback is not None:
            await progress_callback({"type": "provider_used", "endpoint": endpoint_override, "provider": "p"})
            await progress_callback({"type": "final_image", "endpoint_used": endpoint_override})
            await progress_callback({"type": "completed", "endpoint_used": endpoint_override})
        await asyncio.sleep(0.01 if endpoint_override == "generations" else 0.05)
        return (f"img-{endpoint_override}", None)

    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job)
    monkeypatch.setattr(upstream, "_DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S", 5.0)

    async def collect(event: dict[str, Any]) -> None:
        events.append(event)

    image_iter = upstream._dual_race_image_jobs_action(
        action="generate",
        prompt="hi",
        size="1024x1024",
        images=None,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        model=None,
        progress_callback=collect,
    )
    async for _ in image_iter:
        pass

    # generations lane: provider_used + final_image + completed（3 个 raw 事件）
    # responses lane: 只有 provider_used 经 _emit_image_progress 透出（1 个）
    types_by_endpoint: dict[str, list[str]] = {}
    for e in events:
        ep = e.get("endpoint") or e.get("endpoint_used")
        types_by_endpoint.setdefault(ep, []).append(e["type"])
    assert "final_image" in types_by_endpoint.get("generations", [])
    assert "completed" in types_by_endpoint.get("generations", [])
    # responses lane 不应该推 final_image / completed（那会让 caller 重复处理）
    assert "final_image" not in types_by_endpoint.get("responses", [])
    assert "completed" not in types_by_endpoint.get("responses", [])
    # 但 provider_used 事件应该至少有一个 endpoint=responses
    assert any(
        e.get("type") == "provider_used" and e.get("endpoint") == "responses"
        for e in events
    )
