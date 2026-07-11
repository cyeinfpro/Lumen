from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app import upstream
from app.upstream_parts import image_stream, reference_images, responses_client


def test_wave2_modules_are_exposed_through_upstream_facade() -> None:
    reference_exports = (
        "_sniff_image_mime",
        "_normalize_reference_image",
        "_reference_cache_keys",
        "_redis_text",
        "_reference_cache_get",
        "_reference_cache_store",
        "_reference_cache_delete",
        "_reference_cache_trim",
        "_reference_url_is_live",
        "_get_or_upload_reference",
        "_push_reference_to_image_job",
        "_resolve_reference_image_urls",
    )
    for name in reference_exports:
        assert getattr(upstream, name) is getattr(reference_images, name)

    assert upstream._responses_image_stream is image_stream._responses_image_stream
    assert upstream._iter_sse_with_runtime is responses_client._iter_sse_with_runtime
    assert upstream._iter_sse is responses_client._iter_sse
    assert upstream.stream_completion is responses_client.stream_completion
    assert upstream.responses_call is not responses_client.responses_call
    assert upstream.responses_call.__module__ == "app.upstream"


def test_reference_limits_are_read_from_late_bound_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upstream, "_MAX_REFERENCE_IMAGE_BYTES", 3)
    with pytest.raises(upstream.UpstreamError) as bytes_error:
        upstream._normalize_reference_image(b"four")
    assert bytes_error.value.status_code == 413
    assert bytes_error.value.error_code == "reference_image_too_large"

    source = io.BytesIO()
    upstream.PILImage.new("RGB", (2, 1), color=(1, 2, 3)).save(
        source,
        format="PNG",
    )
    monkeypatch.setattr(upstream, "_MAX_REFERENCE_IMAGE_BYTES", 1024 * 1024)
    monkeypatch.setattr(upstream, "_MAX_REFERENCE_IMAGE_PIXELS", 1)
    with pytest.raises(upstream.UpstreamError) as pixels_error:
        upstream._normalize_reference_image(source.getvalue())
    assert pixels_error.value.status_code == 413
    assert pixels_error.value.error_code == "reference_image_too_large"


@pytest.mark.asyncio
async def test_reference_url_live_uses_resolved_target_and_pinned_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned_transport = object()
    seen: dict[str, Any] = {}

    async def fake_resolve(url: str, *, allow_http: bool) -> Any:
        seen["resolved"] = (url, allow_http)
        return SimpleNamespace(
            url="https://203.0.113.20/reference.webp",
            resolved_ips=("203.0.113.20",),
        )

    def fake_pinned(target: Any) -> object:
        seen["pinned_target"] = target
        return pinned_transport

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            seen["client_kwargs"] = kwargs

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def head(self, url: str) -> Any:
            seen["head_url"] = url
            return SimpleNamespace(status_code=204)

    monkeypatch.setattr(upstream, "resolve_public_http_target", fake_resolve)
    monkeypatch.setattr(upstream, "pinned_async_http_transport", fake_pinned)
    monkeypatch.setattr(upstream.httpx, "AsyncClient", FakeClient)

    assert await upstream._reference_url_is_live(
        "https://user.example/reference.webp"
    )
    assert seen["resolved"] == (
        "https://user.example/reference.webp",
        True,
    )
    assert seen["head_url"] == "https://203.0.113.20/reference.webp"
    assert seen["client_kwargs"]["transport"] is pinned_transport
    assert seen["client_kwargs"]["follow_redirects"] is False
    assert seen["client_kwargs"]["trust_env"] is False


@pytest.mark.asyncio
async def test_reference_sidecar_push_disables_redirects_and_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, str]:
            return {"url": "https://refs.example/reference.webp"}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            seen["client_kwargs"] = kwargs

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            seen["post_url"] = url
            seen["post_kwargs"] = kwargs
            return FakeResponse()

    monkeypatch.setattr(upstream.httpx, "AsyncClient", FakeClient)

    result = await upstream._push_reference_to_image_job(
        b"normalized-webp",
        "image/webp",
        base_url="https://sidecar.example/",
        api_key="sk-test",
    )

    assert result == "https://refs.example/reference.webp"
    assert seen["post_url"] == "https://sidecar.example/v1/refs"
    assert seen["client_kwargs"]["follow_redirects"] is False
    assert seen["client_kwargs"]["trust_env"] is False
    timeout = seen["client_kwargs"]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == upstream._REFERENCE_PUSH_TIMEOUT_S
    assert seen["post_kwargs"]["headers"]["Content-Type"] == "image/webp"


@pytest.mark.asyncio
async def test_reference_url_resolution_uses_current_normalize_and_upload_facades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Any, ...]] = []

    def fake_normalize(raw: bytes) -> tuple[bytes, str]:
        seen.append(("normalize", raw))
        return b"normalized", "image/webp"

    async def fake_upload(
        raw: bytes,
        mime: str,
        *,
        base_url: str,
        api_key: str,
        user_id: str | None,
    ) -> str | None:
        seen.append(("upload", raw, mime, base_url, api_key, user_id))
        return "https://refs.example/current.webp"

    monkeypatch.setattr(upstream, "_normalize_reference_image", fake_normalize)
    monkeypatch.setattr(upstream, "_get_or_upload_reference", fake_upload)

    assert await upstream._resolve_reference_image_urls(
        [b"original"],
        base_url="https://sidecar.example",
        api_key="sk-current",
        user_id="user-current",
    ) == ["https://refs.example/current.webp"]
    assert seen == [
        ("normalize", b"original"),
        (
            "upload",
            b"normalized",
            "image/webp",
            "https://sidecar.example",
            "sk-current",
            "user-current",
        ),
    ]


@pytest.mark.asyncio
async def test_completion_client_uses_current_validation_sort_and_sse_facades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    runtime = object()

    def fake_validate(body: dict[str, Any]) -> None:
        seen["validated"] = body
        body["instructions"] = "validated"

    def fake_sort(tools: list[Any]) -> list[Any]:
        seen["tools_before_sort"] = list(tools)
        return [{"name": "a"}, {"name": "z"}]

    def fake_runtime_parts(current_runtime: Any) -> tuple[str, str, None]:
        assert current_runtime is runtime
        return "https://upstream.example/v1", "sk-runtime", None

    def fake_runtime_provider_name(current_runtime: Any) -> str:
        assert current_runtime is runtime
        return "provider-current"

    async def fake_resolve_proxy(proxy: Any) -> None:
        assert proxy is None
        return None

    async def fake_iter_runtime(**kwargs: Any):
        seen["runtime_kwargs"] = kwargs
        yield {"type": "response.completed", "response": {"id": "response-1"}}

    monkeypatch.setattr(upstream, "_validate_responses_body", fake_validate)
    monkeypatch.setattr(upstream, "_stable_sort_tools", fake_sort)
    monkeypatch.setattr(upstream, "_runtime_parts", fake_runtime_parts)
    monkeypatch.setattr(
        upstream,
        "_runtime_provider_name",
        fake_runtime_provider_name,
    )
    monkeypatch.setattr(
        upstream,
        "resolve_provider_proxy_url",
        fake_resolve_proxy,
    )
    monkeypatch.setattr(
        upstream,
        "_iter_sse_with_runtime",
        fake_iter_runtime,
    )

    body = {
        "model": "gpt-test",
        "instructions": "",
        "input": [],
        "tools": [{"name": "z"}, {"name": "a"}],
    }
    events = [
        event
        async for event in upstream.stream_completion(
            body,
            runtime_override=runtime,
        )
    ]

    assert seen["validated"] is body
    assert seen["tools_before_sort"] == [{"name": "z"}, {"name": "a"}]
    assert body["tools"] == [{"name": "a"}, {"name": "z"}]
    assert events == [
        {
            "type": "provider_used",
            "provider": "provider-current",
            "route": "responses",
            "endpoint": "responses",
            "source": "text",
        },
        {"type": "response.completed", "response": {"id": "response-1"}},
    ]
    assert seen["runtime_kwargs"] == {
        "base": "https://upstream.example/v1",
        "api_key": "sk-runtime",
        "body": body,
        "interruption_error_code": upstream._TEXT_STREAM_INTERRUPTED_ERROR_CODE,
        "proxy_url": None,
    }
