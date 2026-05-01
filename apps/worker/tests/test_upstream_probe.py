"""上游探针单元测试。

四类场景：
  1. 完整 SSE response.completed 帧 + model 一致 → ok=True；
  2. response.completed 帧缺字段 → ok=False，schema_drift=1；
  3. 200 但 response.model 与请求 model 不符 → ok=False，schema_drift=1；
  4. 上游 5xx（UpstreamError）→ ok=False，schema_drift=1（无完成帧也算）。

测试约束：
- 不允许真打 httpx；通过 monkeypatch 替换 `app.upstream.stream_completion`。
- 同一进程多次注册同名 Gauge 会冲突——直接复用模块级单例，验证它的 set() 调用结果。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from app.jobs import upstream_probe
from app.jobs.upstream_probe import EXPECTED_FIELDS, probe_upstream
from app import upstream as upstream_mod


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _completed_event(model: str, *, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    """构造一条 `response.completed` 事件，可选丢字段模拟 schema drift。"""
    response = {
        "id": "resp_probe_test",
        "object": "response",
        "created_at": 1700000000,
        "model": model,
        "output": [],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }
    for k in drop:
        response.pop(k, None)
    return {"type": "response.completed", "response": response}


def _make_stream(events: list[dict[str, Any]]):
    """工厂：返回 monkeypatchable async generator function。"""

    async def _stream(_body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        for ev in events:
            yield ev

    return _stream


def _make_failing_stream(exc: BaseException):
    async def _stream(_body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        # 必须先 yield 才能让 `async for` 触发；不 yield 就抛即可。
        if False:
            yield {}
        raise exc

    return _stream


# pytest fixture：让每个用例都拿到 isolated probe model（避免依赖 settings 真值）
@pytest.fixture
def probe_model(monkeypatch: pytest.MonkeyPatch) -> str:
    fixed = "gpt-5.4"
    monkeypatch.setattr(upstream_probe, "_resolve_probe_model", lambda: fixed)
    return fixed


# ---------------------------------------------------------------------------
# 场景 1：成功
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_ok_when_completed_frame_full(
    monkeypatch: pytest.MonkeyPatch, probe_model: str
) -> None:
    monkeypatch.setattr(
        upstream_mod,
        "stream_completion",
        _make_stream([_completed_event(probe_model)]),
    )

    result = await probe_upstream({})

    assert result["ok"] is True
    assert result["status"] == 200
    assert result["model"] == probe_model
    assert result["got_model"] == probe_model
    assert result["missing_fields"] == []
    assert result["schema_drift"] is False
    assert result["latency_ms"] >= 0


# ---------------------------------------------------------------------------
# 场景 2：缺字段 → schema drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_schema_drift_when_field_missing(
    monkeypatch: pytest.MonkeyPatch, probe_model: str
) -> None:
    # 故意丢掉 usage 字段
    monkeypatch.setattr(
        upstream_mod,
        "stream_completion",
        _make_stream([_completed_event(probe_model, drop=("usage",))]),
    )

    result = await probe_upstream({})

    assert result["ok"] is False
    assert result["schema_drift"] is True
    assert "usage" in result["missing_fields"]
    # 即便 schema 漂，HTTP-like status 仍应是 200（流读完了）
    assert result["status"] == 200


@pytest.mark.asyncio
async def test_probe_schema_drift_when_multiple_fields_missing(
    monkeypatch: pytest.MonkeyPatch, probe_model: str
) -> None:
    monkeypatch.setattr(
        upstream_mod,
        "stream_completion",
        _make_stream(
            [_completed_event(probe_model, drop=("output", "created_at"))]
        ),
    )

    result = await probe_upstream({})

    assert result["ok"] is False
    assert result["schema_drift"] is True
    assert set(result["missing_fields"]) >= {"output", "created_at"}


# ---------------------------------------------------------------------------
# 场景 3：model swap → schema drift（即便所有字段都在）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_schema_drift_when_model_swapped(
    monkeypatch: pytest.MonkeyPatch, probe_model: str
) -> None:
    swapped = _completed_event("gpt-OTHER")
    monkeypatch.setattr(
        upstream_mod,
        "stream_completion",
        _make_stream([swapped]),
    )

    result = await probe_upstream({})

    assert result["ok"] is False
    assert result["schema_drift"] is True
    assert result["missing_fields"] == []  # 字段都齐
    assert result["got_model"] == "gpt-OTHER"
    assert result["model"] == probe_model
    assert result["status"] == 200


# ---------------------------------------------------------------------------
# 场景 4：上游 5xx → 软失败
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_failure_on_upstream_5xx(
    monkeypatch: pytest.MonkeyPatch, probe_model: str
) -> None:
    monkeypatch.setattr(
        upstream_mod,
        "stream_completion",
        _make_failing_stream(
            upstream_mod.UpstreamError(
                "upstream http 503", status_code=503, error_code="upstream_error"
            )
        ),
    )

    result = await probe_upstream({})

    assert result["ok"] is False
    assert result["schema_drift"] is True  # 没拿到完成帧也算 drift
    assert result["status"] == 503
    assert sorted(result["missing_fields"]) == sorted(EXPECTED_FIELDS)
    assert result["got_model"] is None


@pytest.mark.asyncio
async def test_probe_failure_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, probe_model: str
) -> None:
    """非 UpstreamError 异常也必须被吞掉，cron 不能崩。"""
    monkeypatch.setattr(
        upstream_mod,
        "stream_completion",
        _make_failing_stream(RuntimeError("boom")),
    )

    result = await probe_upstream({})

    assert result["ok"] is False
    assert result["status"] == 500
    assert result["schema_drift"] is True


# ---------------------------------------------------------------------------
# 场景 5：流走到尽头但从未出现 response.completed → 视作失败
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_failure_when_no_completed_frame(
    monkeypatch: pytest.MonkeyPatch, probe_model: str
) -> None:
    # 只有 delta，没 completed
    monkeypatch.setattr(
        upstream_mod,
        "stream_completion",
        _make_stream(
            [
                {"type": "response.output_text.delta", "delta": "ok"},
            ]
        ),
    )

    result = await probe_upstream({})

    assert result["ok"] is False
    assert result["schema_drift"] is True
    # 没读到完成帧的 status 是我们约定的 599
    assert result["status"] == 599
    assert result["got_model"] is None


# ---------------------------------------------------------------------------
# 场景 6：超时
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_timeout(monkeypatch: pytest.MonkeyPatch, probe_model: str) -> None:
    """探针超过 _PROBE_TIMEOUT_S 应被 wait_for 切断，状态记为 504。"""

    async def _slow_stream(_body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        await asyncio.sleep(10)  # 远超我们要 patch 的超时
        if False:
            yield {}

    monkeypatch.setattr(upstream_mod, "stream_completion", _slow_stream)
    # 把超时缩到很短，避免测试本身慢
    monkeypatch.setattr(upstream_probe, "_PROBE_TIMEOUT_S", 0.05)

    result = await probe_upstream({})

    assert result["ok"] is False
    assert result["status"] == 504
    assert result["schema_drift"] is True


# ---------------------------------------------------------------------------
# 软导入兜底：metrics_upstream 不存在不应让探针崩
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_runs_without_metrics_upstream(
    monkeypatch: pytest.MonkeyPatch, probe_model: str
) -> None:
    """metrics_upstream 不可 import / 调用失败时，探针仍要给出正常结果。

    生产里 _record_request / _record_duration 内部已经把 ImportError 吞了。
    这里直接打到 silent no-op，验证 probe 的核心逻辑不依赖 metrics 也能跑完。
    """
    calls: list[str] = []

    def _noop_req(*_a: Any, **_kw: Any) -> None:
        calls.append("req")

    def _noop_dur(*_a: Any, **_kw: Any) -> None:
        calls.append("dur")

    monkeypatch.setattr(upstream_probe, "_record_request", _noop_req)
    monkeypatch.setattr(upstream_probe, "_record_duration", _noop_dur)

    monkeypatch.setattr(
        upstream_mod,
        "stream_completion",
        _make_stream([_completed_event(probe_model)]),
    )

    result = await probe_upstream({})

    assert result["ok"] is True
    assert "req" in calls and "dur" in calls


# ---------------------------------------------------------------------------
# pytest-asyncio event_loop（与现有 test_provider_pool_probes 风格一致）
# ---------------------------------------------------------------------------


@pytest.fixture
def event_loop():  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
