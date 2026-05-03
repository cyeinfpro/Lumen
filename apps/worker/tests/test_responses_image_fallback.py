from __future__ import annotations

import asyncio
import base64
import io as _io
import json
import os
import textwrap
from typing import Any

import pytest
from PIL import Image as _PILImage

from app import provider_pool
from app import upstream
from app.tasks import generation
from lumen_core.constants import (
    DEFAULT_IMAGE_RESPONSES_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    UPSTREAM_MODEL,
)

PNG_B64 = base64.b64encode(b"fake-png-bytes").decode("ascii")


def _make_tiny_png(
    size: tuple[int, int] = (2, 2), color: tuple[int, int, int] = (128, 128, 128)
) -> bytes:
    buf = _io.BytesIO()
    _PILImage.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


TINY_PNG = _make_tiny_png()


def test_image_request_options_respect_render_quality_for_4k_and_fast() -> None:
    medium_4k = generation._image_request_options(  # noqa: SLF001
        {"render_quality": "medium", "fast": False},
        size="3840x2160",
    )
    assert medium_4k["render_quality"] == "medium"
    assert medium_4k["output_compression"] == 100

    fast_4k = generation._image_request_options(  # noqa: SLF001
        {"render_quality": "high", "fast": True, "output_compression": 95},
        size="3840x2160",
    )
    assert fast_4k["render_quality"] == "high"
    assert fast_4k["output_compression"] == 95


async def _first_image_result(
    image_iter: Any,
) -> tuple[str, str | None]:
    async for item in image_iter:
        return item
    raise AssertionError("image iterator yielded no result")


@pytest.fixture(autouse=True)
def _patch_provider_pool_to_use_resolved_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep these upstream tests isolated from DB-backed runtime settings.

    The tests already monkeypatch `upstream._resolve_runtime`; image generation now
    enters through ProviderPool first, so the fake pool delegates back to that same
    patched resolver.
    """

    class TestPool:
        # 模拟 ProviderPool 必要的属性 / 方法。account_limiter.record_image_call
        # 在 redis=None 时短路，所以 _redis=None 即可。
        _redis: Any = None

        async def select(
            self, *, route: str = "text", ignore_cooldown: bool = False
        ) -> list[provider_pool.ResolvedProvider]:
            _ = route, ignore_cooldown
            base_url, api_key = await upstream._resolve_runtime()
            image_jobs_enabled = False
            try:
                image_jobs_enabled = (
                    await upstream.resolve("image.primary_route")
                ) == "image_jobs"
            except Exception:
                image_jobs_enabled = False
            return [
                provider_pool.ResolvedProvider(
                    name="test",
                    base_url=base_url,
                    api_key=api_key,
                    image_jobs_enabled=image_jobs_enabled,
                )
            ]

        def report_success(self, _provider_name: str) -> None:
            return None

        def report_failure(self, _provider_name: str) -> None:
            return None

        # image route 上报（新增）；测试场景下不需要内部状态，pass-through 即可
        def report_image_success(self, _provider_name: str) -> None:
            return None

        def report_image_failure(self, _provider_name: str) -> None:
            return None

        def report_image_rate_limited(
            self, _provider_name: str, *, retry_after_s: float | None = None
        ) -> None:
            _ = retry_after_s
            return None

        # image-job per-endpoint stats (auto-mode endpoint chain)。测试用 stub
        # 不维护状态；endpoint_chain 直接返回 configured 或默认顺序。
        def record_endpoint_success(
            self, _provider_name: str, _endpoint: str, *, latency_ms: float | None = None
        ) -> None:
            _ = latency_ms
            return None

        def record_endpoint_failure(
            self, _provider_name: str, _endpoint: str
        ) -> None:
            return None

        def endpoint_chain(
            self, _provider_name: str, _action: str, configured: str
        ) -> list[str]:
            if configured == "responses":
                return ["responses", "generations"]
            if configured == "generations":
                return ["generations", "responses"]
            return ["generations", "responses"]

    async def fake_get_pool() -> TestPool:
        return TestPool()

    async def fake_resolve(_key: str) -> str | None:
        return None

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "resolve", fake_resolve)


def _assert_webp_bytes(raw: bytes) -> None:
    assert raw.startswith(b"RIFF")
    assert raw[8:12] == b"WEBP"


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class DummyResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class DummyStreamResponse:
    status_code = 200

    async def __aenter__(self) -> "DummyStreamResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def aiter_lines(self):
        yield "event: response.image_generation_call.partial_image"
        yield f'data: {{"type":"response.image_generation_call.partial_image","partial_image":"{PNG_B64}","partial_image_index":0}}'
        yield ""
        yield "event: response.output_item.done"
        yield f'data: {{"type":"response.output_item.done","item":{{"type":"image_generation_call","result":"{PNG_B64}"}}}}'
        yield ""
        yield "event: response.completed"
        yield 'data: {"type":"response.completed","response":{"status":"completed"}}'
        yield ""


class RealGatewayStreamResponse:
    status_code = 200

    async def __aenter__(self) -> "RealGatewayStreamResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def aiter_lines(self):
        yield "event: response.image_generation_call.partial_image"
        yield f'data: {{"type":"response.image_generation_call.partial_image","partial_image_b64":"{PNG_B64}","partial_image_index":0}}'
        yield ""
        yield "event: response.output_item.done"
        yield f'data: {{"type":"response.output_item.done","item":{{"type":"image_generation_call","result":"{PNG_B64}","revised_prompt":"cleaner prompt"}}}}'
        yield ""
        yield "event: response.completed"
        yield 'data: {"type":"response.completed","response":{"status":"completed"}}'
        yield ""


class NoImageStreamResponse:
    status_code = 200

    async def __aenter__(self) -> "NoImageStreamResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def aiter_lines(self):
        yield "event: response.output_item.done"
        yield 'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":""}]}}'
        yield ""
        yield "event: response.completed"
        yield 'data: {"type":"response.completed","response":{"status":"completed","error":null}}'
        yield ""


class ErrorStreamResponse:
    status_code = 502

    async def __aenter__(self) -> "ErrorStreamResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def aread(self) -> bytes:
        return b'{"error":{"message":"gateway unavailable","code":"upstream_error"}}'


class ModerationBlockedStreamResponse:
    status_code = 200

    async def __aenter__(self) -> "ModerationBlockedStreamResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def aiter_lines(self):
        yield "event: response.failed"
        yield (
            'data: {"type":"response.failed","response":{"status":"failed",'
            '"error":{"code":"moderation_blocked","message":"Your request was rejected by the safety system."}}}'
        )
        yield ""


class InterruptedStreamResponse:
    status_code = 200

    async def __aenter__(self) -> "InterruptedStreamResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def aiter_lines(self):
        import httpx

        yield "event: response.image_generation_call.generating"
        yield 'data: {"type":"response.image_generation_call.generating"}'
        yield ""
        raise httpx.RemoteProtocolError("server disconnected")


class DummyClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.streams: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> DummyResponse:
        self.posts.append({"url": url, **kwargs})
        return DummyResponse(
            500, {"error": {"message": "direct failed", "code": "boom"}}
        )

    def stream(self, method: str, url: str, **kwargs: Any) -> DummyStreamResponse:
        self.streams.append({"method": method, "url": url, **kwargs})
        return DummyStreamResponse()


class SuccessfulDirectClient(DummyClient):
    async def post(self, url: str, **kwargs: Any) -> DummyResponse:
        self.posts.append({"url": url, **kwargs})
        return DummyResponse(
            200,
            {"data": [{"b64_json": PNG_B64, "revised_prompt": "direct prompt"}]},
        )


class ImageJobResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | None = None,
        *,
        content: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class SuccessfulImageJobClient(DummyClient):
    def __init__(self) -> None:
        super().__init__()
        self.gets: list[dict[str, Any]] = []
        self.poll_count = 0

    async def post(self, url: str, **kwargs: Any) -> ImageJobResponse:
        self.posts.append({"url": url, **kwargs})
        return ImageJobResponse(
            200,
            {
                "job_id": "img_test_123",
                "status": "queued",
                "request_type": "generations",
            },
        )

    async def get(self, url: str, **kwargs: Any) -> ImageJobResponse:
        self.gets.append({"url": url, **kwargs})
        if url.endswith("/v1/image-jobs/img_test_123"):
            self.poll_count += 1
            if self.poll_count == 1:
                return ImageJobResponse(200, {"job_id": "img_test_123", "status": "running"})
            return ImageJobResponse(
                200,
                {
                    "job_id": "img_test_123",
                    "status": "succeeded",
                    "images": [
                        {
                            "url": "https://image-cdn.example/images/img_test_123.jpeg",
                            "width": 1024,
                            "height": 1024,
                            "format": "jpeg",
                        }
                    ],
                },
            )
        return ImageJobResponse(200, None, content=TINY_PNG)


class FailingThenSuccessfulImageJobClient(SuccessfulImageJobClient):
    async def post(self, url: str, **kwargs: Any) -> ImageJobResponse:
        self.posts.append({"url": url, **kwargs})
        endpoint = kwargs["json"]["endpoint"]
        if endpoint == "/v1/responses":
            return ImageJobResponse(
                200,
                {
                    "job_id": "img_fail_responses",
                    "status": "queued",
                    "request_type": "responses",
                },
            )
        return ImageJobResponse(
            200,
            {
                "job_id": "img_test_123",
                "status": "queued",
                "request_type": "generations",
            },
        )

    async def get(self, url: str, **kwargs: Any) -> ImageJobResponse:
        self.gets.append({"url": url, **kwargs})
        if url.endswith("/v1/image-jobs/img_fail_responses"):
            return ImageJobResponse(
                200,
                {
                    "job_id": "img_fail_responses",
                    "status": "failed",
                    "error_class": "validation",
                    "endpoint_used": "/v1/responses",
                    "upstream_status": 200,
                    "upstream_body": {
                        "error": {
                            "code": "moderation_blocked",
                            "message": "Your request was rejected by the safety system.",
                        }
                    },
                },
            )
        return await super().get(url, **kwargs)


class TimeoutDirectClient(DummyClient):
    async def post(self, url: str, **kwargs: Any) -> DummyResponse:
        import httpx

        self.posts.append({"url": url, **kwargs})
        raise httpx.ReadTimeout("direct image endpoint timed out")


class BadJsonDirectClient(DummyClient):
    async def post(self, url: str, **kwargs: Any) -> DummyResponse:
        self.posts.append({"url": url, **kwargs})

        class BadJsonResponse(DummyResponse):
            text = "not-json"

            def __init__(self) -> None:
                self.status_code = 200

            def json(self) -> dict[str, Any]:
                raise ValueError("invalid json")

        return BadJsonResponse()


class BadJsonDirectImagesClient(BadJsonDirectClient):
    pass


class RealGatewayStreamClient(DummyClient):
    def stream(self, method: str, url: str, **kwargs: Any) -> RealGatewayStreamResponse:
        self.streams.append({"method": method, "url": url, **kwargs})
        return RealGatewayStreamResponse()


class NoImageStreamClient(DummyClient):
    def stream(self, method: str, url: str, **kwargs: Any) -> NoImageStreamResponse:
        self.streams.append({"method": method, "url": url, **kwargs})
        return NoImageStreamResponse()


class ErrorStreamClient(DummyClient):
    async def post(self, url: str, **kwargs: Any) -> DummyResponse:
        self.posts.append({"url": url, **kwargs})
        return DummyResponse(
            500, {"error": {"message": "direct failed", "code": "boom"}}
        )

    def stream(self, method: str, url: str, **kwargs: Any) -> ErrorStreamResponse:
        self.streams.append({"method": method, "url": url, **kwargs})
        return ErrorStreamResponse()


class InterruptedStreamClient(DummyClient):
    def stream(self, method: str, url: str, **kwargs: Any) -> InterruptedStreamResponse:
        self.streams.append({"method": method, "url": url, **kwargs})
        return InterruptedStreamResponse()


async def _events_from_stream_response(response: Any, url: str):
    if response.status_code >= 400:
        raw = await response.aread()
        payload = json.loads(raw.decode("utf-8"))
        raise upstream._with_error_context(
            upstream._parse_error(payload, response.status_code),
            path="responses",
            method="POST",
            url=url,
        )

    current_event: str | None = None
    try:
        async for line in response.aiter_lines():
            if line == "":
                current_event = None
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                current_event = line[len("event:") :].strip() or None
                continue
            if line.startswith("data:"):
                data_raw = line[len("data:") :].lstrip()
                if data_raw == "[DONE]":
                    return
                event = json.loads(data_raw)
                if isinstance(event, dict):
                    if "type" not in event and current_event:
                        event["type"] = current_event
                    yield event
    except upstream.UpstreamError:
        raise
    except Exception as exc:
        raise upstream.UpstreamError(
            f"responses stream interrupted: {exc}",
            status_code=response.status_code,
            error_code="stream_interrupted",
            payload={"path": "responses", "method": "POST", "url": url},
        ) from exc


def patch_responses_stream(
    monkeypatch: pytest.MonkeyPatch,
    client: DummyClient,
    response_type: type[Any] = DummyStreamResponse,
) -> None:
    async def fake_iter_sse_curl(
        *,
        url: str,
        json_body: dict[str, Any],
        headers: dict[str, str],
        timeout_s: float,
        proxy_url: str | None = None,
    ):
        client.streams.append(
            {
                "method": "POST",
                "url": url,
                "json": json_body,
                "headers": headers,
                "timeout": timeout_s,
                "proxy_url": proxy_url,
            }
        )
        async for event in _events_from_stream_response(response_type(), url):
            yield event

    monkeypatch.setattr(upstream, "_iter_sse_curl", fake_iter_sse_curl)


@pytest.mark.asyncio
async def test_iter_sse_curl_handles_large_single_line_image_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    fake_curl = tmp_path / "fake-curl"
    fake_curl.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import sys

            result = "A" * (70 * 1024)
            payload = {
                "type": "response.output_item.done",
                "item": {"type": "image_generation_call", "result": result},
            }
            sys.stdout.write("HTTP/1.1 200 OK\\r\\n")
            sys.stdout.write("content-type: text/event-stream\\r\\n")
            sys.stdout.write("\\r\\n")
            sys.stdout.write("event: response.output_item.done\\n")
            sys.stdout.write("data: " + json.dumps(payload) + "\\n\\n")
            sys.stdout.flush()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    monkeypatch.setattr(upstream, "_CURL_BIN", str(fake_curl))

    events = [
        event
        async for event in upstream._iter_sse_curl(
            url="https://upstream.example/v1/responses",
            json_body={"stream": True},
            headers={"authorization": "Bearer test-key"},
            timeout_s=10,
        )
    ]

    assert events[0]["type"] == "response.output_item.done"
    assert len(events[0]["item"]["result"]) == 70 * 1024


@pytest.mark.asyncio
async def test_iter_sse_curl_does_not_set_curl_total_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    argv_path = tmp_path / "argv.json"
    fake_curl = tmp_path / "fake-curl"
    fake_curl.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env python3
            import json
            import sys

            with open({str(argv_path)!r}, "w", encoding="utf-8") as fh:
                json.dump(sys.argv[1:], fh)
            payload = {{"type": "response.completed"}}
            sys.stdout.write("HTTP/1.1 200 OK\\r\\n")
            sys.stdout.write("content-type: text/event-stream\\r\\n")
            sys.stdout.write("\\r\\n")
            sys.stdout.write("event: response.completed\\n")
            sys.stdout.write("data: " + json.dumps(payload) + "\\n\\n")
            sys.stdout.flush()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    monkeypatch.setattr(upstream, "_CURL_BIN", str(fake_curl))

    events = [
        event
        async for event in upstream._iter_sse_curl(
            url="https://upstream.example/v1/responses",
            json_body={"stream": True},
            headers={"authorization": "Bearer test-key"},
            timeout_s=180,
        )
    ]

    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    assert events[0]["type"] == "response.completed"
    assert "-m" not in argv
    assert "--max-time" not in argv


@pytest.mark.asyncio
async def test_iter_sse_curl_idle_timeout_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    fake_curl = tmp_path / "fake-curl"
    fake_curl.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import sys
            import time

            sys.stdout.write("HTTP/1.1 200 OK\\r\\n")
            sys.stdout.write("content-type: text/event-stream\\r\\n")
            sys.stdout.write("\\r\\n")
            sys.stdout.flush()
            time.sleep(0.3)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    monkeypatch.setattr(upstream, "_CURL_BIN", str(fake_curl))

    with pytest.raises(upstream.UpstreamError) as exc_info:
        _ = [
            event
            async for event in upstream._iter_sse_curl(
                url="https://upstream.example/v1/responses",
                json_body={"stream": True},
                headers={"authorization": "Bearer test-key"},
                timeout_s=0.05,
            )
        ]

    assert exc_info.value.error_code == "sse_curl_failed"
    assert "idle timeout" in str(exc_info.value)


@pytest.mark.asyncio
async def test_curl_post_multipart_kills_child_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    pid_path = tmp_path / "curl.pid"
    fake_curl = tmp_path / "fake-curl"
    fake_curl.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env python3
            import os
            import pathlib
            import time

            pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(60)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    monkeypatch.setattr(upstream, "_CURL_BIN", str(fake_curl))

    task = asyncio.create_task(
        upstream._curl_post_multipart(
            url="https://upstream.example/v1/images/edits",
            data={"model": "gpt-image-2", "prompt": "edit"},
            files=[("image[]", ("input.png", TINY_PNG, "image/png"))],
            headers={"authorization": "Bearer test-key"},
            timeout_s=30,
        )
    )

    for _ in range(100):
        if pid_path.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_path.exists()
    pid = int(pid_path.read_text(encoding="utf-8"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3)

    for _ in range(100):
        if not _pid_exists(pid):
            break
        await asyncio.sleep(0.01)
    assert not _pid_exists(pid)


@pytest.mark.asyncio
async def test_generate_image_uses_responses_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 默认文生图主路径走 /v1/responses；设置可切到 /v1/images/generations direct。
    client = DummyClient()
    progress_events: list[dict[str, Any]] = []

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    patch_responses_stream(monkeypatch, client)

    b64, revised = await _first_image_result(
        upstream.generate_image(
            prompt="make a 4k landscape",
            size="3840x2160",
            n=1,
            quality="high",
            progress_callback=progress_events.append,
        )
    )

    assert b64 == PNG_B64
    assert revised is None
    # direct path 不应被调用
    assert client.posts == []
    # 4K 尺寸强制单 lane——同账号并发 race 会让上游偶发 server_error（test-summary §11）
    assert len(client.streams) == 1
    stream_body = client.streams[0]["json"]
    assert client.streams[0]["url"] == "https://upstream.example/v1/responses"
    # 生图 reasoning 主模型走 gpt-5.4（不是 chat 的 5.5）；对齐 Codex CLI 标准模板
    # 后统一带 medium reasoning + summary auto，去掉 effort=high / service_tier=priority。
    assert stream_body["model"] == DEFAULT_IMAGE_RESPONSES_MODEL == "gpt-5.4"
    assert stream_body["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert "service_tier" not in stream_body
    assert stream_body["stream"] is True
    # tool_choice 用 Codex CLI 实际发的对象形式而非 "required" 字符串。
    assert stream_body["tool_choice"] == {"type": "image_generation"}
    assert stream_body["parallel_tool_calls"] is True
    assert stream_body["include"] == ["reasoning.encrypted_content"]
    assert stream_body["tools"][0]["type"] == "image_generation"
    assert stream_body["tools"][0]["model"] == UPSTREAM_MODEL
    assert stream_body["tools"][0]["action"] == "generate"
    assert stream_body["tools"][0]["size"] == "3840x2160"
    assert stream_body["tools"][0]["quality"] == "high"
    # 默认 PNG（OpenAI codex 端忽略 output_compression，JPEG 固定低 quality 有压缩痕迹）
    assert stream_body["tools"][0]["output_format"] == "png"
    # PNG 不带 output_compression 字段
    assert "output_compression" not in stream_body["tools"][0]
    assert stream_body["tools"][0]["background"] == "auto"
    assert stream_body["tools"][0]["moderation"] == "low"
    # 4K 不再发 partial_images——省 SSE 带宽与 Redis 压力（单帧 4K base64 可达 10MB+）
    assert "partial_images" not in stream_body["tools"][0]
    # input item 必须显式带 type:"message"——Codex 私有端点对此字段验证更严
    assert stream_body["input"][0]["type"] == "message"
    assert stream_body["input"][0]["role"] == "user"
    # instructions 对齐 Codex CLI 标准模板：字段必须存在但内容为空串
    assert stream_body.get("instructions") == ""
    progress_types = [
        event["type"]
        for event in progress_events
        if event["type"] != "provider_used"
    ]
    assert progress_types == [
        "fallback_started",
        "partial_image",
        "final_image",
        "completed",
    ]
    assert "b64" not in progress_events[1]


def test_fast_responses_body_uses_mini_model_without_forcing_image_options() -> None:
    body = upstream._build_responses_image_body(  # noqa: SLF001
        action="generate",
        prompt="make a fast 4k landscape",
        size="3840x2160",
        images=None,
        quality="high",
        output_format="jpeg",
        output_compression=0,
        background="auto",
        moderation="low",
        model=DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    )

    assert body["model"] == DEFAULT_IMAGE_RESPONSES_MODEL_FAST
    # 对齐 Codex CLI 标准模板后，fast 路径也统一带 medium reasoning + summary auto。
    assert body["reasoning"] == {"effort": "medium", "summary": "auto"}
    tool = body["tools"][0]
    assert tool["quality"] == "high"
    assert tool["output_format"] == "jpeg"
    assert tool["output_compression"] == 0
    assert tool["size"] == "3840x2160"


def test_retry_attempt_injects_cache_busters() -> None:
    """retry_attempt > 1 时打散三件套必须生效——这是绕开 ChatGPT codex 故障 cache 的核心。"""
    base_kwargs = dict(
        action="generate",
        prompt="make a 4k landscape",
        size="3840x2160",
        images=None,
        quality="high",
        output_format="jpeg",
        output_compression=100,
        background="auto",
        moderation="low",
        model=None,
    )

    # attempt=1 默认值——不打散，body 保持原样
    body1 = upstream._build_responses_image_body(**base_kwargs)  # noqa: SLF001
    assert "prompt_cache_key" not in body1
    assert body1["reasoning"] == {"effort": "medium", "summary": "auto"}

    # attempt=2/3/4 通过 ContextVar 触发打散
    cv_token = upstream.push_image_retry_attempt(2)
    try:
        body2 = upstream._build_responses_image_body(**base_kwargs)  # noqa: SLF001
    finally:
        upstream.pop_image_retry_attempt(cv_token)
    assert body2["prompt_cache_key"].startswith("lumen-retry-")
    # effort rotation: 2 → minimal
    assert body2["reasoning"] == {"effort": "minimal", "summary": "auto"}
    # 4K size 在 attempt=1 也不带 partial_images（pixels > _PARTIAL_IMAGES_MAX_PIXELS），
    # 所以这里只能验证 retry 时确实没残留 partial_images
    assert "partial_images" not in body2["tools"][0]

    # attempt=3 → high
    cv_token = upstream.push_image_retry_attempt(3)
    try:
        body3 = upstream._build_responses_image_body(**base_kwargs)  # noqa: SLF001
    finally:
        upstream.pop_image_retry_attempt(cv_token)
    assert body3["reasoning"]["effort"] == "high"
    # 不同 attempt 必须给不同 prompt_cache_key（cache miss 才能跳出故障 cache）
    assert body3["prompt_cache_key"] != body2["prompt_cache_key"]

    # 验证 push/pop 后 ContextVar 恢复默认（不漂移）
    body_after = upstream._build_responses_image_body(**base_kwargs)  # noqa: SLF001
    assert "prompt_cache_key" not in body_after


def test_retry_cache_busters_remove_partial_images_for_small_size() -> None:
    """对 1K 小图，attempt=1 会带 partial_images=3；retry 必须把它去掉，否则 server_error 复现。"""
    base_kwargs = dict(
        action="generate",
        prompt="quick test",
        size="1024x1024",
        images=None,
        quality="high",
        output_format="jpeg",
        output_compression=100,
        background="auto",
        moderation="low",
        model=None,
    )
    body1 = upstream._build_responses_image_body(**base_kwargs)  # noqa: SLF001
    assert body1["tools"][0].get("partial_images") == 3

    cv_token = upstream.push_image_retry_attempt(2)
    try:
        body2 = upstream._build_responses_image_body(**base_kwargs)  # noqa: SLF001
    finally:
        upstream.pop_image_retry_attempt(cv_token)
    # retry 时 partial_images 必须被移除
    assert "partial_images" not in body2["tools"][0]


@pytest.mark.asyncio
async def test_generate_image_can_use_image2_direct_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SuccessfulDirectClient()
    progress_events: list[dict[str, Any]] = []

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_images_client() -> SuccessfulDirectClient:
        return client

    async def fake_resolve(key: str) -> str | None:
        # _resolve_image_primary_route 先查新键 image.primary_route；命中即 break，不会再查旧键
        assert key == "image.primary_route"
        return "image2"

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    monkeypatch.setattr(upstream, "resolve", fake_resolve)

    b64, revised = await _first_image_result(
        upstream.generate_image(
            prompt="make a 4k landscape",
            size="3840x2160",
            n=1,
            quality="high",
            progress_callback=progress_events.append,
        )
    )

    assert b64 == PNG_B64
    assert revised == "direct prompt"
    assert client.streams == []
    assert len(client.posts) == 1
    post = client.posts[0]
    assert post["url"] == "https://upstream.example/v1/images/generations"
    body = post["json"]
    assert body == {
        "model": UPSTREAM_MODEL,
        "prompt": "make a 4k landscape",
        "size": "3840x2160",
        "n": 1,
        "quality": "high",
        # 默认 PNG（OpenAI 忽略 output_compression，JPEG 仍有压缩痕迹）；PNG 不带 compression
        "output_format": "png",
        "background": "auto",
        "moderation": "low",
    }
    progress_types = [
        event["type"]
        for event in progress_events
        if event["type"] != "provider_used"
    ]
    assert progress_types == [
        "final_image",
        "completed",
    ]
    assert {event["source"] for event in progress_events} == {"image2_direct"}


@pytest.mark.asyncio
async def test_image_jobs_responses_falls_back_to_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FailingThenSuccessfulImageJobClient()
    progress_events: list[dict[str, Any]] = []

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_images_client() -> FailingThenSuccessfulImageJobClient:
        return client

    async def fake_resolve(key: str) -> str | None:
        if key == "image.primary_route":
            return "image_jobs"
        if key == "image.job_base_url":
            return "https://image-job.example"
        return None

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    monkeypatch.setattr(upstream, "resolve", fake_resolve)
    monkeypatch.setattr(upstream, "_IMAGE_JOB_POLL_INTERVAL_S", 0.0)

    b64, revised = await _first_image_result(
        upstream.generate_image(
            prompt="make a product image",
            size="1024x1024",
            n=1,
            quality="medium",
            progress_callback=progress_events.append,
        )
    )

    assert base64.b64decode(b64) == TINY_PNG
    assert revised is None
    assert [post["json"]["endpoint"] for post in client.posts] == [
        "/v1/responses",
        "/v1/images/generations",
    ]
    assert any(event.get("type") == "endpoint_failover" for event in progress_events)


@pytest.mark.asyncio
async def test_stream_responses_falls_back_to_direct_image2_on_moderation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SuccessfulDirectClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_client() -> SuccessfulDirectClient:
        return client

    async def fake_get_images_client() -> SuccessfulDirectClient:
        return client

    async def fake_resolve(key: str) -> str | None:
        if key == "image.channel":
            return "stream_only"
        if key == "image.engine":
            return "responses"
        return None

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    monkeypatch.setattr(upstream, "resolve", fake_resolve)
    patch_responses_stream(monkeypatch, client, ModerationBlockedStreamResponse)

    b64, revised = await _first_image_result(
        upstream.generate_image(
            prompt="try the paired route",
            size="1024x1024",
            n=1,
            quality="high",
        )
    )

    assert b64 == PNG_B64
    assert revised == "direct prompt"
    assert len(client.streams) == 1
    assert len(client.posts) == 1
    assert client.posts[0]["url"] == "https://upstream.example/v1/images/generations"


@pytest.mark.asyncio
async def test_generate_image_can_use_image_jobs_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SuccessfulImageJobClient()
    progress_events: list[dict[str, Any]] = []

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_images_client() -> SuccessfulImageJobClient:
        return client

    async def fake_resolve(key: str) -> str | None:
        if key == "image.primary_route":
            return "image_jobs"
        if key == "image.job_base_url":
            return "https://image-job.example"
        return None

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    monkeypatch.setattr(upstream, "resolve", fake_resolve)
    monkeypatch.setattr(upstream, "_IMAGE_JOB_POLL_INTERVAL_S", 0.0)

    b64, revised = await _first_image_result(
        upstream.generate_image(
            prompt="make a product image",
            size="1024x1024",
            n=1,
            quality="medium",
            progress_callback=progress_events.append,
        )
    )

    assert base64.b64decode(b64) == TINY_PNG
    assert revised is None
    assert len(client.posts) == 1
    post = client.posts[0]
    assert post["url"] == "https://image-job.example/v1/image-jobs"
    payload = post["json"]
    assert payload["request_type"] == "responses"
    assert payload["endpoint"] == "/v1/responses"
    assert payload["retention_days"] == 1
    assert payload["body"]["model"] == DEFAULT_IMAGE_RESPONSES_MODEL
    assert payload["body"]["input"][0]["content"][0]["text"] == "make a product image"
    assert payload["body"]["tools"][0]["size"] == "1024x1024"
    assert [item["url"] for item in client.gets] == [
        "https://image-job.example/v1/image-jobs/img_test_123",
        "https://image-job.example/v1/image-jobs/img_test_123",
        "https://image-cdn.example/images/img_test_123.jpeg",
    ]
    progress_types = [
        event["type"]
        for event in progress_events
        if event["type"] != "fallback_started"
    ]
    assert progress_types == [
        "image_job_image",
        "provider_used",
        "final_image",
        "completed",
    ]


@pytest.mark.asyncio
async def test_edit_image_can_use_image_jobs_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SuccessfulImageJobClient()
    progress_events: list[dict[str, Any]] = []

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_images_client() -> SuccessfulImageJobClient:
        return client

    async def fake_resolve(key: str) -> str | None:
        if key == "image.primary_route":
            return "image_jobs"
        if key == "image.job_base_url":
            return "https://image-job.example"
        return None

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    monkeypatch.setattr(upstream, "resolve", fake_resolve)
    monkeypatch.setattr(upstream, "_IMAGE_JOB_POLL_INTERVAL_S", 0.0)

    b64, revised = await _first_image_result(
        upstream.edit_image(
            prompt="change the background",
            size="1024x1024",
            images=[TINY_PNG],
            n=1,
            quality="medium",
            progress_callback=progress_events.append,
        )
    )

    assert base64.b64decode(b64) == TINY_PNG
    assert revised is None
    assert len(client.posts) == 1
    payload = client.posts[0]["json"]
    assert payload["request_type"] == "responses"
    assert payload["endpoint"] == "/v1/responses"
    assert payload["body"]["model"] == DEFAULT_IMAGE_RESPONSES_MODEL
    assert payload["body"]["input"][0]["content"][0]["text"] == "change the background"
    assert payload["body"]["input"][0]["content"][1]["image_url"].startswith(
        "data:image/webp;base64,"
    )
    progress_types = [
        event["type"]
        for event in progress_events
        if event["type"] != "fallback_started"
    ]
    assert progress_types == [
        "image_job_image",
        "provider_used",
        "final_image",
        "completed",
    ]
    assert {event["source"] for event in progress_events if "source" in event} == {
        "image_jobs",
        "image_jobs_edit",
    }


def test_transparent_background_converts_to_matte_upstream_options() -> None:
    prompt, output_format, background = upstream._transparent_matte_upstream_options(
        prompt="clean product badge",
        output_format="webp",
        background="transparent",
    )

    assert prompt.startswith("clean product badge")
    assert "transparent PNG" in prompt
    assert "single-color matte background" in prompt
    assert output_format == "png"
    assert background == "opaque"


@pytest.mark.asyncio
async def test_responses_transparent_background_uses_matte_png_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DummyClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    patch_responses_stream(monkeypatch, client)

    await _first_image_result(
        upstream.generate_image(
            prompt="clean product badge",
            size="3840x2160",
            n=1,
            quality="high",
            output_format="webp",
            output_compression=90,
            background="transparent",
        )
    )

    assert len(client.streams) == 1
    stream_body = client.streams[0]["json"]
    tool = stream_body["tools"][0]
    assert tool["background"] == "opaque"
    assert tool["output_format"] == "png"
    assert "output_compression" not in tool
    prompt_text = stream_body["input"][0]["content"][0]["text"]
    assert prompt_text.startswith("clean product badge")
    assert "transparent PNG" in prompt_text
    assert "single-color matte background" in prompt_text


@pytest.mark.asyncio
async def test_direct_transparent_background_uses_matte_png_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SuccessfulDirectClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_images_client() -> SuccessfulDirectClient:
        return client

    async def fake_resolve(key: str) -> str | None:
        assert key == "image.primary_route"
        return "image2"

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    monkeypatch.setattr(upstream, "resolve", fake_resolve)

    await _first_image_result(
        upstream.generate_image(
            prompt="clean product badge",
            size="1024x1024",
            n=1,
            quality="high",
            output_format="webp",
            output_compression=90,
            background="transparent",
        )
    )

    assert len(client.posts) == 1
    body = client.posts[0]["json"]
    assert body["background"] == "opaque"
    assert body["output_format"] == "png"
    assert "output_compression" not in body
    assert body["prompt"].startswith("clean product badge")
    assert "transparent PNG" in body["prompt"]
    assert "single-color matte background" in body["prompt"]


@pytest.mark.asyncio
async def test_direct_edit_transparent_background_uses_matte_png_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_curl_post_multipart(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        captured.update(kwargs)
        return 200, {"data": [{"b64_json": PNG_B64}]}

    monkeypatch.setattr(upstream, "_curl_post_multipart", fake_curl_post_multipart)

    await upstream._direct_edit_image_once(
        prompt="clean product badge",
        size="1024x1024",
        images=[TINY_PNG],
        n=1,
        quality="high",
        output_format="webp",
        output_compression=90,
        background="transparent",
        moderation="low",
        base_url_override="https://upstream.example/v1",
        api_key_override="test-key",
    )

    data = captured["data"]
    assert data["background"] == "opaque"
    assert data["output_format"] == "png"
    assert "output_compression" not in data
    assert data["prompt"].startswith("clean product badge")
    assert "transparent PNG" in data["prompt"]
    assert "single-color matte background" in data["prompt"]


@pytest.mark.asyncio
async def test_generate_image_image2_route_falls_back_to_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DummyClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_images_client() -> DummyClient:
        return client

    async def fake_resolve(key: str) -> str | None:
        # _resolve_image_primary_route 先查新键 image.primary_route；命中即 break，不会再查旧键
        assert key == "image.primary_route"
        return "image2"

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    monkeypatch.setattr(upstream, "resolve", fake_resolve)
    patch_responses_stream(monkeypatch, client)

    b64, revised = await _first_image_result(
        upstream.generate_image(
            prompt="make an image",
            size="1536x864",
            n=1,
            quality="high",
        )
    )

    assert b64 == PNG_B64
    assert revised is None
    assert len(client.posts) == 1
    assert len(client.streams) == 1
    assert client.posts[0]["url"] == "https://upstream.example/v1/images/generations"
    assert client.streams[0]["url"] == "https://upstream.example/v1/responses"


@pytest.mark.asyncio
async def test_generate_small_size_keeps_partial_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """小尺寸（≤2M 像素）仍然下发 partial_images=3，保留渐进预览。"""
    client = DummyClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    patch_responses_stream(monkeypatch, client)

    await _first_image_result(
        upstream.generate_image(
            prompt="small image",
            size="1536x864",  # 1.3M 像素 — 在阈值内
            n=1,
            quality="high",
        )
    )
    tool = client.streams[0]["json"]["tools"][0]
    assert tool["size"] == "1536x864"
    assert tool["partial_images"] == 3


@pytest.mark.asyncio
async def test_generate_low_quality_omits_partial_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DummyClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example/v1", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    patch_responses_stream(monkeypatch, client)

    await _first_image_result(
        upstream.generate_image(
            prompt="small draft",
            size="1024x1024",
            n=1,
            quality="low",
        )
    )
    tool = client.streams[0]["json"]["tools"][0]
    assert tool["quality"] == "low"
    assert "partial_images" not in tool


@pytest.mark.asyncio
async def test_edit_image_falls_back_with_input_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DummyClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    async def fake_get_images_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    patch_responses_stream(monkeypatch, client)

    b64, revised = await _first_image_result(
        upstream.edit_image(
            prompt="upscale/edit this image",
            size="3840x2160",
            images=[TINY_PNG],
            n=1,
            quality="high",
        )
    )

    assert b64 == PNG_B64
    assert revised is None
    stream_body = client.streams[0]["json"]
    assert stream_body["tools"][0]["action"] == "edit"
    # instructions 对齐 Codex CLI 标准模板：字段必须存在但内容为空串
    assert stream_body.get("instructions") == ""
    # input item 显式带 type:"message" 对齐标准模板
    assert stream_body["input"][0]["type"] == "message"
    content = stream_body["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "upscale/edit this image"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/webp;base64,")


def test_parse_error_surfaces_fastapi_detail_payload() -> None:
    err = upstream._parse_error({"detail": "Instructions are required"}, 400)

    assert err.status_code == 400
    assert str(err) == "Instructions are required"


@pytest.mark.asyncio
async def test_edit_image_uses_responses_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TimeoutDirectClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    async def fake_get_images_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    patch_responses_stream(monkeypatch, client)

    b64, revised = await _first_image_result(
        upstream.edit_image(
            prompt="edit this image",
            size="3840x2160",
            images=[TINY_PNG],
            n=1,
            quality="high",
        )
    )

    assert b64 == PNG_B64
    assert revised is None
    assert client.posts == []
    assert client.streams[0]["url"] == "https://upstream.example/v1/responses"


@pytest.mark.asyncio
async def test_responses_fallback_accepts_real_gateway_partial_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RealGatewayStreamClient()
    progress_events: list[dict[str, Any]] = []

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    patch_responses_stream(monkeypatch, client, RealGatewayStreamResponse)

    b64, revised = await upstream._responses_image_stream(
        prompt="make an image",
        size="3840x2160",
        action="generate",
        quality="high",
        progress_callback=progress_events.append,
    )

    assert b64 == PNG_B64
    assert revised == "cleaner prompt"
    partial = next(
        event for event in progress_events if event["type"] == "partial_image"
    )
    assert partial["has_preview"] is True
    assert "b64" not in partial


@pytest.mark.asyncio
async def test_responses_fallback_no_image_has_diagnostic_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = NoImageStreamClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    patch_responses_stream(monkeypatch, client, NoImageStreamResponse)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._responses_image_stream(
            prompt="make an image",
            size="3840x2160",
            action="generate",
            quality="high",
        )

    assert exc_info.value.error_code == "no_image_returned"
    assert exc_info.value.payload["path"] == "responses"
    assert exc_info.value.payload["last_event_type"] == "response.completed"
    assert exc_info.value.payload["partial_count"] == 0


@pytest.mark.asyncio
async def test_fallback_stream_error_includes_path_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ErrorStreamClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    monkeypatch.setattr(upstream, "_get_images_client", fake_get_client)
    patch_responses_stream(monkeypatch, client, ErrorStreamResponse)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await _first_image_result(
            upstream.generate_image(
                prompt="make an image",
                size="3840x2160",
                n=1,
                quality="high",
            )
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.payload["path"] == "responses"
    assert exc_info.value.payload["primary_path"] == "responses"
    assert exc_info.value.payload["fallback_path"] == "image2"
    assert len(client.streams) == 5
    assert len(client.posts) == 1
    errors = exc_info.value.payload["path_errors"]
    assert errors[0]["path"] == "responses"
    assert errors[0]["payload"]["method"] == "POST"
    assert errors[1]["path"] == "image2"
    assert errors[1]["payload"]["path"] == "images/generations"


@pytest.mark.asyncio
async def test_fallback_stream_interruption_is_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = InterruptedStreamClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    patch_responses_stream(monkeypatch, client, InterruptedStreamResponse)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._responses_image_stream(
            prompt="make an image",
            size="3840x2160",
            action="generate",
            quality="high",
        )

    assert exc_info.value.error_code == "stream_interrupted"
    assert exc_info.value.payload["path"] == "responses"


def test_sniff_image_mime_detects_real_formats() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"rest"
    jpeg = b"\xff\xd8\xff\xe0" + b"rest"
    webp = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"rest"
    assert upstream._sniff_image_mime(png) == "image/png"
    assert upstream._sniff_image_mime(jpeg) == "image/jpeg"
    assert upstream._sniff_image_mime(webp) == "image/webp"
    assert upstream._sniff_image_mime(b"garbage") is None


def test_normalize_reference_image_reencodes_png_to_clean_webp() -> None:
    # 即使输入已经是 PNG，也要过一次 PIL 洗掉潜在的 EXIF/ICC/非标 filter——
    # 这是 OpenAI image_generation 稳定性关键（用户相册原图 raw PNG 会让上游报 server_error）。
    src = _make_tiny_png(size=(5, 5), color=(10, 20, 30))
    out_bytes, mime = upstream._normalize_reference_image(src)
    assert mime == "image/webp"
    _assert_webp_bytes(out_bytes)
    with _PILImage.open(_io.BytesIO(out_bytes)) as reloaded:
        assert reloaded.format == "WEBP"
        assert reloaded.size == (5, 5)


def test_normalize_reference_image_reencodes_non_webp_to_webp() -> None:
    # JPEG / GIF / CMYK 等也统一转标准 8-bit WebP
    buf = _io.BytesIO()
    _PILImage.new("RGB", (4, 4), color=(1, 2, 3)).save(buf, format="GIF")
    gif_bytes = buf.getvalue()

    out_bytes, mime = upstream._normalize_reference_image(gif_bytes)
    assert mime == "image/webp"
    _assert_webp_bytes(out_bytes)
    with _PILImage.open(_io.BytesIO(out_bytes)) as reloaded:
        assert reloaded.format == "WEBP"
        assert reloaded.size == (4, 4)
        assert reloaded.mode in ("RGB", "RGBA")


def test_normalize_reference_image_converts_non_rgb_modes_to_rgb() -> None:
    # 16-bit / L / P / CMYK 等非 RGB 模式要统一转 RGB——PIL save 到 WebP 时
    # 不同 mode 会产生不同编码路径，上游处理起来最稳的就是 RGB 8-bit
    buf = _io.BytesIO()
    _PILImage.new("L", (3, 3), color=200).save(buf, format="PNG")
    gray_png = buf.getvalue()

    out_bytes, mime = upstream._normalize_reference_image(gray_png)
    assert mime == "image/webp"
    _assert_webp_bytes(out_bytes)
    with _PILImage.open(_io.BytesIO(out_bytes)) as reloaded:
        assert reloaded.format == "WEBP"
        assert reloaded.mode == "RGB"
        assert reloaded.size == (3, 3)


def test_normalize_reference_image_raises_on_undecodable() -> None:
    with pytest.raises(upstream.UpstreamError) as exc_info:
        upstream._normalize_reference_image(b"not an image at all")
    assert exc_info.value.error_code == "bad_reference_image"


@pytest.mark.asyncio
async def test_responses_image_stream_uses_httpx_when_flag_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 关键断言：use_httpx=True 时必须完全跳过 _iter_sse_curl，走 httpx 路径
    async def curl_must_not_run(**_: Any):
        raise AssertionError("_iter_sse_curl must not be called when use_httpx=True")
        yield  # 让它成为 async generator

    monkeypatch.setattr(upstream, "_iter_sse_curl", curl_must_not_run)

    client = DummyClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)

    b64, _ = await upstream._responses_image_stream(
        prompt="hello",
        size="3840x2160",
        action="generate",
        quality="high",
        use_httpx=True,
    )

    assert b64 == PNG_B64
    assert len(client.streams) == 1
    assert client.streams[0]["url"] == "https://upstream.example/v1/responses"


@pytest.mark.asyncio
async def test_edit_image_rejects_empty_images() -> None:
    with pytest.raises(upstream.UpstreamError) as exc_info:
        await _first_image_result(
            upstream.edit_image(
                prompt="edit",
                size="3840x2160",
                images=[],
                n=1,
                quality="high",
            )
        )
    assert exc_info.value.error_code == "missing_input_images"


@pytest.mark.asyncio
async def test_edit_image_reencodes_jpeg_input_to_webp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # JPEG 输入（以及任何非 WebP 或带 metadata 的 WebP）都要过 PIL 转成干净 WebP 再发给上游
    client = DummyClient()

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example", "test-key"

    async def fake_get_client() -> DummyClient:
        return client

    async def fake_get_images_client() -> DummyClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)
    patch_responses_stream(monkeypatch, client)

    jpeg_buf = _io.BytesIO()
    _PILImage.new("RGB", (4, 4), color=(10, 20, 30)).save(jpeg_buf, format="JPEG")
    jpeg_bytes = jpeg_buf.getvalue()
    await _first_image_result(
        upstream.edit_image(
            prompt="edit",
            size="3840x2160",
            images=[jpeg_bytes],
            n=1,
            quality="high",
        )
    )

    content = client.streams[0]["json"]["input"][0]["content"]
    assert content[1]["image_url"].startswith("data:image/webp;base64,")


@pytest.mark.asyncio
async def test_responses_image_stream_surfaces_moderation_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 上游 response.failed 明确给了 moderation_blocked，必须透传到 UpstreamError.error_code，
    # 否则 classifier 当 no_image_returned 去 retry 6 次、既徒劳又烧配额
    client = RealGatewayStreamClient()  # 用 httpx 路径注入 fake response

    async def fake_resolve_runtime() -> tuple[str, str]:
        return "https://upstream.example", "test-key"

    async def fake_get_client() -> RealGatewayStreamClient:
        return client

    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "_get_client", fake_get_client)
    patch_responses_stream(monkeypatch, client, ModerationBlockedStreamResponse)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._responses_image_stream(
            prompt="some prompt",
            size="3840x2160",
            action="generate",
            quality="high",
        )

    assert exc_info.value.error_code == "moderation_blocked"
    assert "safety" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_extract_image_result_accepts_b64_json() -> None:
    payload = {"data": [{"b64_json": PNG_B64, "revised_prompt": "rp"}]}
    b64, revised = await upstream._extract_image_result(payload, 200)
    assert b64 == PNG_B64
    assert revised == "rp"


@pytest.mark.asyncio
async def test_extract_image_result_falls_back_to_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """data[].url 形态（部分第三方网关默认 response_format=url）应被下载并转 b64。"""
    raw_bytes = TINY_PNG
    requested_urls: list[str] = []

    class _UrlClient:
        async def get(self, url: str) -> ImageJobResponse:
            requested_urls.append(url)
            return ImageJobResponse(200, None, content=raw_bytes)

    async def fake_get_images_client(proxy_url: str | None = None) -> _UrlClient:
        return _UrlClient()

    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)

    payload = {
        "data": [
            {
                "url": "https://cdn.example/imgs/abc.png",
                "revised_prompt": "via url",
            }
        ]
    }
    b64, revised = await upstream._extract_image_result(payload, 200)

    assert base64.b64decode(b64) == raw_bytes
    assert revised == "via url"
    assert requested_urls == ["https://cdn.example/imgs/abc.png"]


@pytest.mark.asyncio
async def test_extract_image_result_raises_when_neither_b64_nor_url() -> None:
    payload = {"data": [{"revised_prompt": "no image"}]}
    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._extract_image_result(payload, 200)
    assert exc_info.value.error_code == "no_image_returned"


@pytest.mark.asyncio
async def test_extract_image_result_url_download_failure_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BadUrlClient:
        async def get(self, url: str) -> ImageJobResponse:
            return ImageJobResponse(404, {"error": "not found"})

    async def fake_get_images_client(proxy_url: str | None = None) -> _BadUrlClient:
        return _BadUrlClient()

    monkeypatch.setattr(upstream, "_get_images_client", fake_get_images_client)

    payload = {"data": [{"url": "https://cdn.example/missing.png"}]}
    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream._extract_image_result(payload, 200)
    assert exc_info.value.status_code == 404
    assert "image url download" in str(exc_info.value).lower()
