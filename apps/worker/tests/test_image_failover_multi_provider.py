"""multi-provider image failover 集成测试。

覆盖"sub2api 一号一 key + Lumen 多 provider 调度"的端到端路径：
- 第一个号 429 → report_image_rate_limited(acc1) → 切第二个号成功
- 第一个号普通失败 → report_image_failure(acc1) → 切第二个号成功
- 第一个号 policy → 切第二个号成功（不同上游 policy 行为可能不同）
- 全部失败 → 抛 all_providers_failed
"""

from __future__ import annotations

import base64
import io as _io
from typing import Any

import pytest
from PIL import Image as _PILImage

from app import provider_pool, upstream

PNG_B64 = base64.b64encode(b"fake-png-bytes").decode("ascii")


def _make_tiny_png() -> bytes:
    buf = _io.BytesIO()
    _PILImage.new("RGB", (2, 2), color=(128, 128, 128)).save(buf, format="PNG")
    return buf.getvalue()


# --- Mock Pool: 多 provider + 上报记录 ---------------------------------------


class RecordingPool:
    """模拟 ProviderPool，按 provider_names 顺序返回，记录上报序列。"""

    _redis: Any = None

    def __init__(self, provider_names: list[str]) -> None:
        self.provider_names = provider_names
        self.calls: list[tuple[str, str, dict[str, Any]]] = []  # (method, name, kwargs)

    async def select(
        self, *, route: str = "text", ignore_cooldown: bool = False
    ) -> list[provider_pool.ResolvedProvider]:
        _ = route, ignore_cooldown
        return [
            provider_pool.ResolvedProvider(
                name=n, base_url=f"https://{n}.example", api_key=f"sk-{n}"
            )
            for n in self.provider_names
        ]

    def report_success(self, name: str) -> None:
        self.calls.append(("report_success", name, {}))

    def report_failure(self, name: str) -> None:
        self.calls.append(("report_failure", name, {}))

    def report_image_success(self, name: str) -> None:
        self.calls.append(("report_image_success", name, {}))

    def report_image_failure(self, name: str) -> None:
        self.calls.append(("report_image_failure", name, {}))

    def report_image_rate_limited(
        self, name: str, *, retry_after_s: float | None = None
    ) -> None:
        self.calls.append(
            ("report_image_rate_limited", name, {"retry_after_s": retry_after_s})
        )

    def get_redis(self) -> Any:
        return None

    def record_endpoint_success(
        self, name: str, endpoint: str, *, latency_ms: float | None = None
    ) -> None:
        self.calls.append(
            (
                "record_endpoint_success",
                name,
                {"endpoint": endpoint, "latency_ms": latency_ms},
            )
        )

    def record_endpoint_failure(self, name: str, endpoint: str) -> None:
        self.calls.append(("record_endpoint_failure", name, {"endpoint": endpoint}))

    def endpoint_chain(
        self, provider_name: str, action: str, configured: str
    ) -> list[str]:
        _ = provider_name, action, configured
        return ["generations", "responses"]


def _make_per_provider_stream(
    monkeypatch: pytest.MonkeyPatch,
    behaviors: dict[str, str],
) -> None:
    """给每个 provider api_key 注入不同行为：'success' / '429' / 'server_error' / 'policy'."""

    async def fake_stream_with_retry(
        *,
        prompt: str,
        size: str,
        action: str,
        images: list[bytes] | None,
        quality: str,
        model: str | None = None,
        progress_callback: Any = None,
        use_httpx: bool = False,
        base_url_override: str | None = None,
        api_key_override: str | None = None,
        **_kwargs: Any,
    ) -> tuple[str, str | None]:
        _ = (prompt, size, action, images, quality, model, progress_callback, use_httpx, base_url_override)
        # api_key_override 形如 "sk-acc1"，去掉前缀拿 provider name
        name = (api_key_override or "").removeprefix("sk-")
        beh = behaviors.get(name, "success")
        if beh == "success":
            return PNG_B64, None
        if beh == "429":
            raise upstream.UpstreamError(
                "Rate limit exceeded",
                status_code=429,
                error_code="rate_limit_error",
                payload={"error": {"retry_after": 30.0}},
            )
        if beh == "server_error":
            raise upstream.UpstreamError(
                "responses image fallback returned no image",
                status_code=200,
                error_code="server_error",
            )
        if beh in {"moderation_blocked", "content_policy_violation", "safety_violation"}:
            raise upstream.UpstreamError(
                "Your request was rejected by the safety system.",
                status_code=200,
                error_code=beh,
            )
        raise AssertionError(f"unknown behavior: {beh}")

    monkeypatch.setattr(
        upstream, "_responses_image_stream_with_retry", fake_stream_with_retry
    )


def _image_job_exc(kind: str) -> upstream.UpstreamError:
    if kind == "502":
        return upstream.UpstreamError(
            "sidecar upstream 502",
            status_code=502,
            error_code="upstream_error",
            payload={"image_job_error_class": "upstream_5xx"},
        )
    if kind == "429":
        return upstream.UpstreamError(
            "Rate limit exceeded",
            status_code=429,
            error_code="rate_limit_error",
            payload={
                "image_job_error_class": "upstream_4xx",
                "error": {"retry_after": 12.0},
            },
        )
    if kind == "no_image":
        return upstream.UpstreamError(
            "image job returned no image",
            status_code=200,
            error_code="no_image_returned",
            payload={"image_job_error_class": "no_image"},
        )
    if kind == "timeout":
        return upstream.UpstreamError(
            "image job timeout",
            status_code=None,
            error_code="upstream_timeout",
            payload={"path": "image-jobs"},
        )
    raise AssertionError(f"unknown image job failure kind: {kind}")


# --- 测试 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_account_rate_limited_failover_to_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = RecordingPool(["acc1", "acc2"])

    async def fake_get_pool() -> RecordingPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    _make_per_provider_stream(monkeypatch, {"acc1": "429", "acc2": "success"})

    b64, _ = await upstream._responses_image_stream_with_failover(
        prompt="test",
        size="1024x1024",
        action="generate",
        images=None,
        quality="high",
        progress_callback=None,
        use_httpx=False,
    )
    assert b64 == PNG_B64
    # 上报序列：acc1 rate_limited → acc2 success
    assert pool.calls == [
        ("report_image_rate_limited", "acc1", {"retry_after_s": 30.0}),
        ("report_image_success", "acc2", {}),
    ]


@pytest.mark.asyncio
async def test_first_account_server_error_failover_to_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = RecordingPool(["acc1", "acc2"])

    async def fake_get_pool() -> RecordingPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    _make_per_provider_stream(
        monkeypatch, {"acc1": "server_error", "acc2": "success"}
    )

    b64, _ = await upstream._responses_image_stream_with_failover(
        prompt="test",
        size="1024x1024",
        action="generate",
        images=None,
        quality="high",
        progress_callback=None,
        use_httpx=False,
    )
    assert b64 == PNG_B64
    # 上报序列：acc1 image_failure → acc2 success
    assert pool.calls == [
        ("report_image_failure", "acc1", {}),
        ("report_image_success", "acc2", {}),
    ]


@pytest.mark.parametrize(
    "error_code",
    ["moderation_blocked", "content_policy_violation", "safety_violation"],
)
@pytest.mark.asyncio
async def test_policy_error_failovers_to_second_provider(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
) -> None:
    pool = RecordingPool(["acc1", "acc2"])

    async def fake_get_pool() -> RecordingPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    _make_per_provider_stream(
        monkeypatch, {"acc1": error_code, "acc2": "success"}
    )

    b64, _ = await upstream._responses_image_stream_with_failover(
        prompt="test",
        size="1024x1024",
        action="generate",
        images=None,
        quality="high",
        progress_callback=None,
        use_httpx=False,
    )
    assert b64 == PNG_B64
    assert pool.calls == [
        ("report_image_failure", "acc1", {}),
        ("report_image_success", "acc2", {}),
    ]


@pytest.mark.asyncio
async def test_all_providers_fail_raises_all_providers_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = RecordingPool(["acc1", "acc2"])

    async def fake_get_pool() -> RecordingPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    _make_per_provider_stream(
        monkeypatch, {"acc1": "server_error", "acc2": "server_error"}
    )

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._responses_image_stream_with_failover(
            prompt="test",
            size="1024x1024",
            action="generate",
            images=None,
            quality="high",
            progress_callback=None,
            use_httpx=False,
        )
    assert exc_info.value.error_code == "all_providers_failed"
    assert exc_info.value.payload["provider_errors"] == [
        {
            "provider": "acc1",
            "type": "UpstreamError",
            "message": "responses image fallback returned no image",
            "status_code": 200,
            "error_code": "server_error",
        },
        {
            "provider": "acc2",
            "type": "UpstreamError",
            "message": "responses image fallback returned no image",
            "status_code": 200,
            "error_code": "server_error",
        },
    ]
    # 两个号都报了 image_failure
    assert pool.calls == [
        ("report_image_failure", "acc1", {}),
        ("report_image_failure", "acc2", {}),
    ]


@pytest.mark.asyncio
async def test_failover_keeps_progress_callback_for_second_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = RecordingPool(["acc1", "acc2"])
    seen_callbacks: dict[str, bool] = {}

    async def fake_get_pool() -> RecordingPool:
        return pool

    async def fake_stream_with_retry(
        *,
        prompt: str,
        size: str,
        action: str,
        images: list[bytes] | None,
        quality: str,
        model: str | None = None,
        progress_callback: Any = None,
        use_httpx: bool = False,
        base_url_override: str | None = None,
        api_key_override: str | None = None,
        **_kwargs: Any,
    ) -> tuple[str, str | None]:
        _ = (prompt, size, action, images, quality, model, use_httpx, base_url_override)
        name = (api_key_override or "").removeprefix("sk-")
        seen_callbacks[name] = progress_callback is not None
        if name == "acc1":
            raise upstream.UpstreamError(
                "temporary failure",
                status_code=503,
                error_code="server_error",
            )
        return PNG_B64, None

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        upstream, "_responses_image_stream_with_retry", fake_stream_with_retry
    )

    await upstream._responses_image_stream_with_failover(
        prompt="test",
        size="1024x1024",
        action="generate",
        images=None,
        quality="high",
        progress_callback=lambda event: None,
        use_httpx=False,
    )

    assert seen_callbacks == {"acc1": True, "acc2": True}


@pytest.mark.asyncio
async def test_all_accounts_failed_propagates_when_pool_select_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pool.select 已经把所有号过滤掉时，failover 直接拿到 all_accounts_failed。"""

    class EmptyPool(RecordingPool):
        async def select(
            self, *, route: str = "text", ignore_cooldown: bool = False
        ) -> list[provider_pool.ResolvedProvider]:
            _ = route, ignore_cooldown
            raise upstream.UpstreamError(
                "all accounts unavailable for image: acc1(image_cooldown), acc2(image_rate_limited)",
                error_code="all_accounts_failed",
                status_code=503,
                payload={"skipped": [["acc1", "image_cooldown"]]},
            )

    pool = EmptyPool([])

    async def fake_get_pool() -> EmptyPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    # 不需要 _make_per_provider_stream，永远到不了

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._responses_image_stream_with_failover(
            prompt="test",
            size="1024x1024",
            action="generate",
            images=None,
            quality="high",
            progress_callback=None,
            use_httpx=False,
        )
    assert exc_info.value.error_code == "all_accounts_failed"
    assert "acc1" in str(exc_info.value)


# --- P2: failover 通知前端 -------------------------------------------------

@pytest.mark.asyncio
async def test_failover_emits_provider_failover_progress_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨 provider failover 时必须通过 progress_callback 发 provider_failover 事件，
    让前端能在 DevelopingCard 上显示"换号重试"。事件应包含 from_provider / remaining。"""
    pool = RecordingPool(["acc1", "acc2", "acc3"])

    async def fake_get_pool() -> RecordingPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    _make_per_provider_stream(
        monkeypatch, {"acc1": "server_error", "acc2": "429", "acc3": "success"}
    )

    progress_events: list[dict[str, Any]] = []

    async def cb(event: dict[str, Any]) -> None:
        progress_events.append(event)

    b64, _ = await upstream._responses_image_stream_with_failover(
        prompt="test",
        size="1024x1024",
        action="generate",
        images=None,
        quality="high",
        progress_callback=cb,
        use_httpx=False,
    )
    assert b64 == PNG_B64

    failover_events = [e for e in progress_events if e.get("type") == "provider_failover"]
    # acc1 → acc2 → acc3：两次切号
    assert len(failover_events) == 2
    assert failover_events[0]["from_provider"] == "acc1"
    assert failover_events[0]["remaining"] == 2
    assert failover_events[1]["from_provider"] == "acc2"
    assert failover_events[1]["remaining"] == 1
    # 都附带换号原因
    assert all("reason" in e for e in failover_events)
    assert all(e.get("route") == "responses" for e in failover_events)


@pytest.mark.asyncio
async def test_failover_does_not_emit_when_first_provider_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首个 provider 即成功时不应产生 provider_failover 事件，避免前端多余抖动。"""
    pool = RecordingPool(["acc1", "acc2"])

    async def fake_get_pool() -> RecordingPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    _make_per_provider_stream(monkeypatch, {"acc1": "success", "acc2": "success"})

    progress_events: list[dict[str, Any]] = []

    async def cb(event: dict[str, Any]) -> None:
        progress_events.append(event)

    await upstream._responses_image_stream_with_failover(
        prompt="test",
        size="1024x1024",
        action="generate",
        images=None,
        quality="high",
        progress_callback=cb,
        use_httpx=False,
    )
    assert all(e.get("type") != "provider_failover" for e in progress_events)


@pytest.mark.parametrize(
    "error_code",
    ["moderation_blocked", "content_policy_violation", "safety_violation"],
)
@pytest.mark.asyncio
async def test_failover_emitted_on_policy_error(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
) -> None:
    """policy 错误现在会切号，因为不同 provider 可能采用不同安全策略。"""
    pool = RecordingPool(["acc1", "acc2"])

    async def fake_get_pool() -> RecordingPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    _make_per_provider_stream(monkeypatch, {"acc1": error_code, "acc2": "success"})

    progress_events: list[dict[str, Any]] = []

    async def cb(event: dict[str, Any]) -> None:
        progress_events.append(event)

    b64, _ = await upstream._responses_image_stream_with_failover(
        prompt="test",
        size="1024x1024",
        action="generate",
        images=None,
        quality="high",
        progress_callback=cb,
        use_httpx=False,
    )
    assert b64 == PNG_B64
    failover_events = [e for e in progress_events if e.get("type") == "provider_failover"]
    assert len(failover_events) == 1
    assert failover_events[0]["from_provider"] == "acc1"


@pytest.mark.asyncio
async def test_failover_not_emitted_on_last_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最后一个 provider 失败后没有"下一个"可切，不应再推 provider_failover。
    （remaining = 0 时 if remaining > 0 块跳过）"""
    pool = RecordingPool(["acc1", "acc2"])

    async def fake_get_pool() -> RecordingPool:
        return pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    _make_per_provider_stream(
        monkeypatch, {"acc1": "server_error", "acc2": "server_error"}
    )

    progress_events: list[dict[str, Any]] = []

    async def cb(event: dict[str, Any]) -> None:
        progress_events.append(event)

    with pytest.raises(upstream.UpstreamError):
        await upstream._responses_image_stream_with_failover(
            prompt="test",
            size="1024x1024",
            action="generate",
            images=None,
            quality="high",
            progress_callback=cb,
            use_httpx=False,
        )
    # acc1 失败 → 切到 acc2（1 个 failover 事件）→ acc2 失败 → 没有下一个
    failover_events = [e for e in progress_events if e.get("type") == "provider_failover"]
    assert len(failover_events) == 1
    assert failover_events[0]["from_provider"] == "acc1"


@pytest.mark.parametrize("failure_kind", ["502", "429", "no_image", "timeout"])
@pytest.mark.asyncio
async def test_image_jobs_failover_continues_endpoint_then_provider(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    pool = RecordingPool(["acc1", "acc2"])

    async def fake_get_pool() -> RecordingPool:
        return pool

    attempts: list[tuple[str, str]] = []

    async def fake_run_once(
        *,
        endpoint: str,
        api_key: str,
        **_kwargs: Any,
    ) -> tuple[str, str | None]:
        provider = api_key.removeprefix("sk-")
        attempts.append((provider, endpoint))
        if provider == "acc1":
            raise _image_job_exc(failure_kind)
        return PNG_B64, None

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "_image_job_run_once", fake_run_once)
    monkeypatch.setattr(upstream, "_resolve_image_job_base_url", _resolved_job_base)

    progress_events: list[dict[str, Any]] = []

    b64, _ = await upstream._image_job_with_failover(
        action="generate",
        prompt="test",
        size="1024x1024",
        images=None,
        n=1,
        quality="high",
        progress_callback=progress_events.append,
    )

    assert b64 == PNG_B64
    assert attempts == [
        ("acc1", "generations"),
        ("acc1", "responses"),
        ("acc2", "generations"),
    ]
    assert any(e.get("type") == "endpoint_failover" for e in progress_events)
    assert any(e.get("type") == "provider_failover" for e in progress_events)
    if failure_kind == "429":
        assert ("report_image_rate_limited", "acc1", {"retry_after_s": 12.0}) in pool.calls
    else:
        assert ("report_image_failure", "acc1", {}) in pool.calls
    assert ("report_image_success", "acc2", {}) in pool.calls


async def _resolved_job_base() -> str:
    return "https://image-job.example"
