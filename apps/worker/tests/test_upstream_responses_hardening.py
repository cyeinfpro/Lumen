"""image-stability-hardening-plan 新增能力的单元测试。

覆盖：
- ``_is_responses_success_terminal`` / ``_is_responses_error_terminal`` 帮助函数
- ``_extract_image_b64_from_payload`` 多路径 b64 提取
- ``_extract_image_billable_count`` usage.images / tool_usage.image_gen.images
- ``responses_call``：``response.done`` 当成功 terminal、``response.failed`` 抛上游 error
- ``_responses_image_stream``：JSON content-type fallback 提图（Image API + Responses）
- ``_responses_image_stream``：JSON 无图时失败诊断带 content_type / body 摘要
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app import upstream


# ---------------------------------------------------------------------------
# 1) terminal event 帮助函数
# ---------------------------------------------------------------------------


def test_is_responses_success_terminal_accepts_completed_and_done() -> None:
    assert upstream._is_responses_success_terminal("response.completed") is True
    assert upstream._is_responses_success_terminal("response.done") is True
    assert upstream._is_responses_success_terminal("response.failed") is False
    assert upstream._is_responses_success_terminal(None) is False
    assert upstream._is_responses_success_terminal(123) is False


def test_is_responses_error_terminal_accepts_failed_incomplete_error() -> None:
    assert upstream._is_responses_error_terminal("response.failed") is True
    assert upstream._is_responses_error_terminal("response.incomplete") is True
    assert upstream._is_responses_error_terminal("error") is True
    assert upstream._is_responses_error_terminal("response.completed") is False
    assert upstream._is_responses_error_terminal("response.done") is False


# ---------------------------------------------------------------------------
# 2) _extract_image_b64_from_payload
# ---------------------------------------------------------------------------


def test_extract_image_b64_from_image_api_data_b64_json() -> None:
    payload = {"data": [{"b64_json": "AAAA", "revised_prompt": "p"}]}
    assert upstream._extract_image_b64_from_payload(payload) == "AAAA"


def test_extract_image_b64_from_responses_output_result() -> None:
    payload = {
        "output": [
            {"type": "reasoning"},
            {"type": "image_generation_call", "result": "BBBB"},
        ]
    }
    assert upstream._extract_image_b64_from_payload(payload) == "BBBB"


def test_extract_image_b64_from_response_wrapper_with_content_array() -> None:
    payload = {
        "response": {
            "output": [
                {"content": [{"type": "image", "result": "CCCC"}]},
            ]
        }
    }
    assert upstream._extract_image_b64_from_payload(payload) == "CCCC"


def test_extract_image_b64_from_event_wrapper_item() -> None:
    payload = {"item": {"type": "image_generation_call", "result": "DDDD"}}
    assert upstream._extract_image_b64_from_payload(payload) == "DDDD"


def test_extract_image_b64_returns_none_for_url_only_data() -> None:
    payload = {"data": [{"url": "https://cdn.example/missing.png"}]}
    assert upstream._extract_image_b64_from_payload(payload) is None


def test_extract_image_b64_returns_none_for_non_dict() -> None:
    assert upstream._extract_image_b64_from_payload(None) is None
    assert upstream._extract_image_b64_from_payload("string") is None
    assert upstream._extract_image_b64_from_payload([{"b64_json": "x"}]) is None


# ---------------------------------------------------------------------------
# 3) _extract_image_billable_count
# ---------------------------------------------------------------------------


def test_extract_image_billable_count_from_usage_images() -> None:
    assert upstream._extract_image_billable_count({"usage": {"images": 3}}) == 3


def test_extract_image_billable_count_from_response_usage_images() -> None:
    payload = {"response": {"usage": {"images": 2}}}
    assert upstream._extract_image_billable_count(payload) == 2


def test_extract_image_billable_count_from_tool_usage_image_gen() -> None:
    payload = {"tool_usage": {"image_gen": {"images": 4}}}
    assert upstream._extract_image_billable_count(payload) == 4


def test_extract_image_billable_count_rejects_negative_and_bool() -> None:
    assert upstream._extract_image_billable_count({"usage": {"images": -1}}) is None
    # bool 是 int 子类——不能误判 True == 1
    assert upstream._extract_image_billable_count({"usage": {"images": True}}) is None


def test_extract_image_billable_count_returns_none_when_missing() -> None:
    assert upstream._extract_image_billable_count({}) is None
    assert upstream._extract_image_billable_count({"usage": {}}) is None
    assert upstream._extract_image_billable_count(None) is None


# ---------------------------------------------------------------------------
# 4) responses_call 兼容 response.done 与 error terminal
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    def __init__(
        self,
        *,
        status_code: int,
        headers: dict[str, str] | None = None,
        sse_lines: list[str] | None = None,
        body_bytes: bytes | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._sse_lines = sse_lines
        self._body_bytes = body_bytes
        self.aclose_called = False

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def aiter_lines(self):
        for line in self._sse_lines or []:
            yield line

    async def aread(self) -> bytes:
        return self._body_bytes or b""

    async def aclose(self) -> None:
        self.aclose_called = True


class _FakeClient:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response
        self.stream_calls: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> Any:
        self.stream_calls.append({"method": method, "url": url, **kwargs})
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
    return {
        "model": "gpt-5.4",
        "instructions": "you are helpful",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "stream": False,
    }


@pytest.mark.asyncio
async def test_responses_call_accepts_response_done_as_success_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容网关：response.done 与 response.completed 一样作为成功 terminal。"""
    response_obj = {
        "id": "resp_done_1",
        "model": "gpt-5.4",
        "output_text": "done variant",
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }
    sse_lines = [
        "event: response.done",
        f"data: {json.dumps({'type': 'response.done', 'response': response_obj})}",
        "",
    ]
    fake_response = _FakeStreamResponse(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        sse_lines=sse_lines,
    )
    client = _FakeClient(fake_response)
    _patch_client(monkeypatch, client)

    result = await upstream.responses_call(_valid_body())
    assert result == response_obj


@pytest.mark.asyncio
async def test_responses_call_response_failed_raises_with_upstream_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """response.failed 帧的 error.code 要透传给上层 retry classifier。"""
    sse_lines = [
        "event: response.failed",
        (
            'data: {"type":"response.failed","response":{"status":"failed",'
            '"error":{"code":"moderation_blocked","message":"blocked by safety"}}}'
        ),
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

    assert exc_info.value.error_code == "moderation_blocked"
    assert "blocked by safety" in str(exc_info.value)
    assert exc_info.value.payload.get("last_event_type") == "response.failed"


@pytest.mark.asyncio
async def test_responses_call_done_without_terminal_includes_last_event_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[DONE] 之前没有任何 terminal event：错误 payload 要带 last_event_type。"""
    sse_lines = [
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"partial"}',
        "",
        "data: [DONE]",
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
    assert exc_info.value.payload.get("last_event_type") == "response.output_text.delta"


# ---------------------------------------------------------------------------
# 5) _responses_image_stream JSON fallback (Image API & Responses payload)
# ---------------------------------------------------------------------------


PNG_B64 = "AAAA"


class _NonSseJsonResponse:
    """模拟上游声明 stream=true 但 Content-Type=application/json 的响应。"""

    status_code = 200

    def __init__(self, body: dict[str, Any] | str) -> None:
        self.headers = {"content-type": "application/json; charset=utf-8"}
        if isinstance(body, dict):
            self._body = json.dumps(body).encode("utf-8")
        else:
            self._body = body.encode("utf-8")

    async def __aenter__(self) -> "_NonSseJsonResponse":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def aread(self) -> bytes:
        return self._body

    async def aiter_lines(self):
        # 不应被调用；JSON fallback 不会进入 SSE 解析
        if False:
            yield ""


class _StubClient:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.stream_calls: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> Any:
        self.stream_calls.append({"method": method, "url": url, **kwargs})
        return self._response


def _patch_image_stream_runtime(
    monkeypatch: pytest.MonkeyPatch, client: _StubClient
) -> None:
    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_client(*_a: Any, **_kw: Any) -> _StubClient:
        return client

    async def fake_timeout_config() -> upstream._TimeoutConfig:
        return upstream._TimeoutConfig(connect=10.0, read=180.0, write=30.0)

    async def curl_must_not_run(**_: Any):
        raise AssertionError("curl path must not run for httpx test")
        yield  # pragma: no cover - 让函数成为 async generator

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    monkeypatch.setattr(upstream, "_resolve_timeout_config", fake_timeout_config)
    monkeypatch.setattr(upstream, "_iter_sse_curl", curl_must_not_run)


@pytest.mark.asyncio
async def test_responses_image_stream_handles_image_api_json_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream=true 但上游回 application/json + data[].b64_json：能成功提图。"""
    payload = {
        "data": [{"b64_json": PNG_B64, "revised_prompt": "json variant"}],
        "usage": {"images": 1},
    }
    response = _NonSseJsonResponse(payload)
    client = _StubClient(response)
    _patch_image_stream_runtime(monkeypatch, client)

    b64, revised = await upstream._responses_image_stream(
        prompt="hi",
        size="1024x1024",
        action="generate",
        quality="high",
        use_httpx=True,
    )
    assert b64 == PNG_B64
    assert revised == "json variant"
    assert client.stream_calls[0]["url"] == "https://upstream.example/v1/responses"


@pytest.mark.asyncio
async def test_responses_image_stream_handles_responses_object_json_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream=true 但上游回 application/json + Responses output[].result：能成功提图。"""
    payload = {
        "id": "resp_json_1",
        "output": [
            {"type": "reasoning"},
            {
                "type": "image_generation_call",
                "result": PNG_B64,
                "revised_prompt": "responses variant",
            },
        ],
    }
    response = _NonSseJsonResponse(payload)
    client = _StubClient(response)
    _patch_image_stream_runtime(monkeypatch, client)

    b64, revised = await upstream._responses_image_stream(
        prompt="hi",
        size="1024x1024",
        action="generate",
        quality="high",
        use_httpx=True,
    )
    assert b64 == PNG_B64
    assert revised == "responses variant"


@pytest.mark.asyncio
async def test_responses_image_stream_json_payload_without_image_includes_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON body 不含图：抛 UpstreamError，payload 带 content_type / 摘要 / trace_id。"""
    payload = {"error": {"code": "rate_limit_exceeded", "message": "slow down"}}
    response = _NonSseJsonResponse(payload)
    client = _StubClient(response)
    _patch_image_stream_runtime(monkeypatch, client)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._responses_image_stream(
            prompt="hi",
            size="1024x1024",
            action="generate",
            quality="high",
            use_httpx=True,
        )

    assert exc_info.value.error_code == "rate_limit_exceeded"
    payload_out = exc_info.value.payload
    assert payload_out.get("trace_id")
    assert payload_out.get("json_fallback_content_type", "").startswith(
        "application/json"
    )
    assert "keys=" in (payload_out.get("json_fallback_body_summary") or "")


@pytest.mark.asyncio
async def test_responses_image_stream_json_payload_records_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON fallback 命中时，顶层 usage 应该走 _record_usage 标准埋点。"""
    captured: list[dict[str, Any]] = []

    def fake_record_usage(usage: Any) -> None:
        if isinstance(usage, dict):
            captured.append(usage)

    monkeypatch.setattr(upstream, "_record_usage", fake_record_usage)

    payload = {
        "data": [{"b64_json": PNG_B64}],
        "usage": {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
    }
    response = _NonSseJsonResponse(payload)
    client = _StubClient(response)
    _patch_image_stream_runtime(monkeypatch, client)

    b64, _ = await upstream._responses_image_stream(
        prompt="hi",
        size="1024x1024",
        action="generate",
        quality="high",
        use_httpx=True,
    )
    assert b64 == PNG_B64
    assert captured and captured[0]["input_tokens"] == 5


@pytest.mark.asyncio
async def test_responses_image_stream_treats_response_done_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE 帧用 response.done 而非 response.completed 时也能正常完成（带 final image）。"""

    async def fake_iter_curl(*, url: str, json_body: dict[str, Any], **_kw: Any):
        yield {
            "type": "response.output_item.done",
            "item": {"type": "image_generation_call", "result": PNG_B64},
        }
        # 仅给 response.done，没有 response.completed
        yield {
            "type": "response.done",
            "response": {"status": "done"},
        }

    monkeypatch.setattr(upstream, "_iter_sse_curl", fake_iter_curl)

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)

    progress_events: list[str] = []

    async def progress(event: dict[str, Any]) -> None:
        et = event.get("type")
        if isinstance(et, str):
            progress_events.append(et)

    b64, _ = await upstream._responses_image_stream(
        prompt="hi",
        size="1024x1024",
        action="generate",
        quality="high",
        progress_callback=progress,
    )
    assert b64 == PNG_B64
    # response.done 必须触发 completed progress（兼容网关）
    assert "completed" in progress_events
