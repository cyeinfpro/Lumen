"""``upstream.responses_call`` 单元测试。

覆盖目标：
- 200 SSE 流：聚合 `response.completed` 帧的 response 对象返回 dict
- 200 JSON：直接返回 dict
- 4xx：抛 UpstreamError，携带 status_code & error_code
- CancelledError：透传不吞
- usage 字段：自动调 `_record_usage`，让 Prometheus 埋点跟着请求走

测试约束：
- 不真打 httpx，所有 HTTP 通过 monkeypatch `_get_client` + 假 client 替换。
- 所有 SSE 帧用字符串列表 yield，模拟 httpx `aiter_lines()`。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app import upstream


# ---------------------------------------------------------------------------
# 假 httpx client 工具
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    def __init__(
        self,
        *,
        status_code: int,
        headers: dict[str, str] | None = None,
        sse_lines: list[str] | None = None,
        body_bytes: bytes | None = None,
        raise_in_lines: BaseException | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._sse_lines = sse_lines
        self._body_bytes = body_bytes
        self._raise_in_lines = raise_in_lines
        self.aclose_called = False

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def aiter_lines(self):
        if self._raise_in_lines is not None:
            raise self._raise_in_lines
        for line in self._sse_lines or []:
            yield line

    async def aread(self) -> bytes:
        return self._body_bytes or b""

    async def aclose(self) -> None:
        self.aclose_called = True


class _FakeClient:
    """最小化 httpx.AsyncClient stub。``stream(...)`` 返回我们预先配好的
    `_FakeStreamResponse`，并把调用参数留下供断言用。"""

    def __init__(self, response: _FakeStreamResponse | BaseException) -> None:
        self._response = response
        self.stream_calls: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> Any:
        self.stream_calls.append({"method": method, "url": url, **kwargs})
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    async def fake_get_client() -> _FakeClient:
        return client

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_timeout_config() -> upstream._TimeoutConfig:
        return upstream._TimeoutConfig(connect=10.0, read=180.0, write=30.0)

    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_resolve_timeout_config", fake_timeout_config)


def _valid_body() -> dict[str, Any]:
    """构造一份能通过 `_validate_responses_body` 的最小 body。"""
    return {
        "model": "gpt-5.4",
        "instructions": "you are helpful",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "stream": False,
    }


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_client_rebuilds_when_runtime_timeout_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[upstream._TimeoutConfig] = []

    class _ClosableClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    clients: list[_ClosableClient] = []
    values = {
        "upstream.connect_timeout_s": "3",
        "upstream.read_timeout_s": "30",
        "upstream.write_timeout_s": "4",
    }

    async def fake_resolve(key: str) -> str | None:
        return values.get(key)

    def fake_build_client(timeout_config: upstream._TimeoutConfig) -> _ClosableClient:
        built.append(timeout_config)
        client = _ClosableClient()
        clients.append(client)
        return client

    monkeypatch.setattr(upstream, "resolve", fake_resolve)
    monkeypatch.setattr(upstream, "_build_client", fake_build_client)
    monkeypatch.setattr(upstream, "_client", None)
    monkeypatch.setattr(upstream, "_client_timeout_config", None)

    first = await upstream._get_client()
    values["upstream.read_timeout_s"] = "45"
    second = await upstream._get_client()

    assert first is clients[0]
    assert second is clients[1]
    assert clients[0].closed is True
    assert [cfg.read for cfg in built] == [30.0, 45.0]


@pytest.mark.asyncio
async def test_responses_call_aggregates_sse_completed_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE 路径：response.completed 帧的 response 子对象应作为返回值。"""
    response_obj = {
        "id": "resp_1",
        "model": "gpt-5.4",
        "output_text": "compressed text",
        "output": [],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 3},
        },
    }
    sse_lines = [
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"compressed "}',
        "",
        "event: response.completed",
        f"data: {json.dumps({'type': 'response.completed', 'response': response_obj})}",
        "",
    ]
    fake_response = _FakeStreamResponse(
        status_code=200,
        headers={"content-type": "text/event-stream", "x-request-id": "req-abc"},
        sse_lines=sse_lines,
    )
    client = _FakeClient(fake_response)
    _patch_client(monkeypatch, client)

    result = await upstream.responses_call(_valid_body())

    assert result == response_obj
    assert client.stream_calls[0]["url"] == "https://upstream.example/v1/responses"
    # outbound headers 自带 originator + x-trace-id
    headers = client.stream_calls[0]["headers"]
    assert headers["authorization"] == "Bearer test-key"
    assert "x-trace-id" in headers and headers["x-trace-id"]
    assert headers["originator"].startswith("lumen-prod-")


@pytest.mark.asyncio
async def test_responses_call_returns_json_body_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON 路径：当上游真的尊重 stream:false 时（compact 端点），直接返回 dict。"""
    payload = {
        "id": "resp_2",
        "model": "gpt-5.4",
        "output_text": "ok",
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }
    fake_response = _FakeStreamResponse(
        status_code=200,
        headers={"content-type": "application/json", "x-request-id": "req-json"},
        body_bytes=json.dumps(payload).encode("utf-8"),
    )
    client = _FakeClient(fake_response)
    _patch_client(monkeypatch, client)

    result = await upstream.responses_call(
        _valid_body(),
        endpoint_label="responses_summary",
    )

    assert result == payload


@pytest.mark.asyncio
async def test_responses_call_4xx_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4xx：解析 body 里的 error code，抛 UpstreamError 让上层 retry classifier 工作。"""
    err_body = {
        "error": {"message": "rate limit exceeded", "code": "rate_limit_error"}
    }
    fake_response = _FakeStreamResponse(
        status_code=429,
        headers={"content-type": "application/json", "x-request-id": "req-429"},
        body_bytes=json.dumps(err_body).encode("utf-8"),
    )
    client = _FakeClient(fake_response)
    _patch_client(monkeypatch, client)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream.responses_call(_valid_body())

    assert exc_info.value.status_code == 429
    assert exc_info.value.error_code == "rate_limit_error"


@pytest.mark.asyncio
async def test_responses_call_5xx_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err_body = {"error": {"message": "boom", "code": "server_error"}}
    fake_response = _FakeStreamResponse(
        status_code=502,
        headers={"content-type": "application/json"},
        body_bytes=json.dumps(err_body).encode("utf-8"),
    )
    client = _FakeClient(fake_response)
    _patch_client(monkeypatch, client)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream.responses_call(_valid_body())

    assert exc_info.value.status_code == 502
    assert exc_info.value.error_code == "server_error"


@pytest.mark.asyncio
async def test_responses_call_cancelled_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelledError 必须穿透到调用方，不能被 except Exception 吞。"""
    fake_response = _FakeStreamResponse(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        raise_in_lines=asyncio.CancelledError(),
    )
    client = _FakeClient(fake_response)
    _patch_client(monkeypatch, client)

    with pytest.raises(asyncio.CancelledError):
        await upstream.responses_call(_valid_body())

    # 取消时显式 aclose，让 httpx 释放底层连接
    assert fake_response.aclose_called is True


@pytest.mark.asyncio
async def test_responses_call_records_usage_metric_for_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE 路径：response.completed 帧里的 usage 自动喂到 _record_usage。"""
    captured: list[dict[str, Any]] = []

    def fake_record_usage(usage: Any) -> None:
        if isinstance(usage, dict):
            captured.append(usage)

    monkeypatch.setattr(upstream, "_record_usage", fake_record_usage)

    response_obj = {
        "id": "resp_3",
        "model": "gpt-5.4",
        "output_text": "ok",
        "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    }
    sse_lines = [
        "event: response.completed",
        f"data: {json.dumps({'type': 'response.completed', 'response': response_obj})}",
        "",
    ]
    fake_response = _FakeStreamResponse(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        sse_lines=sse_lines,
    )
    client = _FakeClient(fake_response)
    _patch_client(monkeypatch, client)

    await upstream.responses_call(_valid_body())

    assert captured, "_record_usage was not invoked for SSE completed frame"
    assert captured[0]["input_tokens"] == 7


@pytest.mark.asyncio
async def test_responses_call_records_usage_metric_for_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON 路径：顶层 usage 也走标准埋点。"""
    captured: list[dict[str, Any]] = []

    def fake_record_usage(usage: Any) -> None:
        if isinstance(usage, dict):
            captured.append(usage)

    monkeypatch.setattr(upstream, "_record_usage", fake_record_usage)

    payload = {
        "id": "resp_4",
        "model": "gpt-5.4",
        "output_text": "ok",
        "usage": {"input_tokens": 11, "output_tokens": 9, "total_tokens": 20},
    }
    fake_response = _FakeStreamResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body_bytes=json.dumps(payload).encode("utf-8"),
    )
    client = _FakeClient(fake_response)
    _patch_client(monkeypatch, client)

    await upstream.responses_call(_valid_body())

    assert captured == [payload["usage"]]


@pytest.mark.asyncio
async def test_responses_call_uses_overrides_skips_resolve_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式给 override 时不该调 _resolve_runtime，避免触发 provider_pool。"""
    resolve_called = False

    async def fake_resolve_runtime() -> tuple[str, str]:
        nonlocal resolve_called
        resolve_called = True
        return "https://should-not-be-used.example/v1", "wrong-key"

    payload = {"output_text": "x"}
    fake_response = _FakeStreamResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body_bytes=json.dumps(payload).encode("utf-8"),
    )
    client = _FakeClient(fake_response)

    async def fake_get_client() -> _FakeClient:
        return client

    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)

    await upstream.responses_call(
        _valid_body(),
        api_key_override="override-key",
        base_url_override="https://override.example/v1",
    )

    assert resolve_called is False
    assert client.stream_calls[0]["url"] == "https://override.example/v1/responses"
    assert client.stream_calls[0]["headers"]["authorization"] == "Bearer override-key"


@pytest.mark.asyncio
async def test_responses_call_validates_body_and_sorts_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """body 必须经过 schema 校验 + tools 排序——保证和 stream 路径一致的 cache 前缀。"""
    payload = {"output_text": "x"}
    fake_response = _FakeStreamResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body_bytes=json.dumps(payload).encode("utf-8"),
    )
    client = _FakeClient(fake_response)
    _patch_client(monkeypatch, client)

    body = _valid_body()
    body["tools"] = [
        {"type": "function", "name": "zeta"},
        {"type": "function", "name": "alpha"},
    ]
    body["tool_choice"] = "auto"

    await upstream.responses_call(body)

    sent = client.stream_calls[0]["json"]
    assert [t["name"] for t in sent["tools"]] == ["alpha", "zeta"]
    # _validate_responses_body 在缺 parallel_tool_calls 时会注入 False
    assert sent["parallel_tool_calls"] is False


@pytest.mark.asyncio
async def test_responses_call_timeout_maps_to_upstream_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx.TimeoutException → UpstreamError(error_code=upstream_timeout)，
    让 context_summary 的 retry classifier 把它当 retriable。"""
    client = _FakeClient(httpx.ReadTimeout("read timed out"))
    _patch_client(monkeypatch, client)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream.responses_call(_valid_body())

    assert exc_info.value.error_code == "upstream_timeout"
    assert exc_info.value.status_code is None


@pytest.mark.asyncio
async def test_responses_call_sse_missing_completed_frame_raises_bad_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sse_lines = [
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"partial"}',
        "",
    ]
    fake_response = _FakeStreamResponse(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        sse_lines=sse_lines,
    )
    client = _FakeClient(fake_response)
    _patch_client(monkeypatch, client)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream.responses_call(_valid_body())

    assert exc_info.value.error_code == "bad_response"
