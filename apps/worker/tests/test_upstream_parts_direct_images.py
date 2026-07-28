from __future__ import annotations

import base64
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from app.upstream_parts import direct_images
from app.upstream_parts import upstream_impl as _upstream_impl  # noqa: F401  组装服务


TEST_UPSTREAM_RUNTIME = _upstream_impl.build_image_upstream_runtime()
TEST_UPSTREAM_SERVICES = TEST_UPSTREAM_RUNTIME.services


class InjectedUpstreamError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.payload = payload or {}


async def _unexpected_fetch(
    image_url: str,
    *,
    proxy_url: str | None = None,
) -> bytes:
    raise AssertionError(f"unexpected fetch: {image_url=} {proxy_url=}")


@pytest.mark.asyncio
async def test_extract_image_results_accepts_all_b64_results() -> None:
    payload = {
        "data": [
            {"b64_json": "image-one", "revised_prompt": "one"},
            {"b64_json": "image-two", "revised_prompt": 2},
            "skip-me",
        ]
    }

    assert await direct_images._extract_image_results(
        payload,
        200,
        fetch_image_url_as_bytes=_unexpected_fetch,
        upstream_error_type=InjectedUpstreamError,
        bad_response_error_code="bad-response",
        no_image_returned_error_code="no-image",
    ) == [
        ("image-one", "one"),
        ("image-two", None),
    ]


@pytest.mark.asyncio
async def test_extract_image_results_downloads_urls_with_injected_fetcher() -> None:
    seen: list[tuple[str, str | None]] = []

    async def fake_fetch(
        image_url: str,
        *,
        proxy_url: str | None = None,
    ) -> bytes:
        seen.append((image_url, proxy_url))
        return b"downloaded-image"

    result = await direct_images._extract_image_results(
        {
            "data": [
                {
                    "url": "https://cdn.example/image.png",
                    "revised_prompt": "downloaded",
                }
            ]
        },
        201,
        fetch_image_url_as_bytes=fake_fetch,
        upstream_error_type=InjectedUpstreamError,
        bad_response_error_code="bad-response",
        no_image_returned_error_code="no-image",
        proxy_url="socks5://proxy.example:1080",
    )

    assert result == [
        (
            base64.b64encode(b"downloaded-image").decode("ascii"),
            "downloaded",
        )
    ]
    assert seen == [
        (
            "https://cdn.example/image.png",
            "socks5://proxy.example:1080",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (["not", "an", "object"], "bad-response"),
        ({"data": []}, "no-image"),
        ({"data": [{"revised_prompt": "missing image"}]}, "no-image"),
    ],
)
async def test_extract_image_results_uses_injected_error_type_and_codes(
    payload: Any,
    expected_code: str,
) -> None:
    with pytest.raises(InjectedUpstreamError) as exc_info:
        await direct_images._extract_image_results(
            payload,
            502,
            fetch_image_url_as_bytes=_unexpected_fetch,
            upstream_error_type=InjectedUpstreamError,
            bad_response_error_code="bad-response",
            no_image_returned_error_code="no-image",
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.error_code == expected_code


@pytest.mark.asyncio
async def test_direct_first_result_helper_uses_injected_results_facade() -> None:
    seen: list[tuple[Any, int, str | None]] = []

    async def fake_extract_results(
        payload: Any,
        status_code: int,
        *,
        proxy_url: str | None = None,
    ) -> list[direct_images.ImageResult]:
        seen.append((payload, status_code, proxy_url))
        return [("first", "prompt"), ("second", None)]

    payload: dict[str, Any] = {"data": []}
    assert await direct_images._extract_image_result(
        payload,
        202,
        extract_image_results=fake_extract_results,
        proxy_url="http://proxy.example",
    ) == ("first", "prompt")
    assert seen == [(payload, 202, "http://proxy.example")]


def test_upstream_facades_expose_explicit_runtime_signatures() -> None:
    for name in ("_extract_image_results", "_extract_image_result"):
        facade = getattr(TEST_UPSTREAM_SERVICES.core, name.lstrip("_"))
        signature = inspect.signature(facade)

        assert inspect.iscoroutinefunction(facade)
        assert tuple(signature.parameters) == (
            "payload",
            "status_code",
            "proxy_url",
            "request_context",
            "runtime",
        )
        assert signature.parameters["payload"].default is inspect.Parameter.empty
        assert signature.parameters["status_code"].default is inspect.Parameter.empty
        assert signature.parameters["proxy_url"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["proxy_url"].default is None


@pytest.mark.asyncio
async def test_results_facade_resolves_dependencies_and_codes_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_fetch(
        image_url: str,
        *,
        proxy_url: str | None = None,
    ) -> bytes:
        return f"{image_url}:{proxy_url}".encode()

    class CurrentUpstreamError(Exception):
        pass

    async def fake_extract(
        payload: Any,
        status_code: int,
        **kwargs: Any,
    ) -> list[direct_images.ImageResult]:
        seen["payload"] = payload
        seen["status_code"] = status_code
        seen.update(kwargs)
        return [("facade-result", None)]

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.direct, "fetch_image_url_as_bytes", fake_fetch
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure,
        "UpstreamError",
        CurrentUpstreamError,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure,
        "EC",
        SimpleNamespace(
            BAD_RESPONSE=SimpleNamespace(value="current-bad-response"),
            NO_IMAGE_RETURNED=SimpleNamespace(value="current-no-image"),
        ),
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.direct,
        "extract_image_results",
        fake_extract,
    )

    payload = {"data": [{"b64_json": "ignored"}]}
    assert await TEST_UPSTREAM_SERVICES.core.extract_image_results(
        payload,
        207,
        proxy_url="http://current-proxy",
    ) == [("facade-result", None)]
    assert seen == {
        "payload": payload,
        "status_code": 207,
        "fetch_image_url_as_bytes": fake_fetch,
        "upstream_error_type": CurrentUpstreamError,
        "bad_response_error_code": "current-bad-response",
        "no_image_returned_error_code": "current-no-image",
        "proxy_url": "http://current-proxy",
    }


@pytest.mark.asyncio
async def test_first_result_facade_chains_through_current_results_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Any, int, str | None]] = []

    async def fake_results_facade(
        payload: Any,
        status_code: int,
        *,
        proxy_url: str | None = None,
    ) -> list[direct_images.ImageResult]:
        seen.append((payload, status_code, proxy_url))
        return [("patched-first", "patched-prompt")]

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.core, "extract_image_results", fake_results_facade
    )

    payload = {"data": [{"b64_json": "ignored"}]}
    assert await TEST_UPSTREAM_SERVICES.core.extract_image_result(
        payload,
        208,
        proxy_url="http://chain-proxy",
    ) == ("patched-first", "patched-prompt")
    assert seen == [(payload, 208, "http://chain-proxy")]


@pytest.mark.parametrize(
    "error_code",
    [
        "direct_image_result_unknown",
        "image_job_result_unknown",
        "no_image_returned",
    ],
)
def test_result_unknown_predicate_blocks_provider_fallback(error_code: str) -> None:
    """上游 2xx 之后的失败不许换 provider 重试，否则一次请求产生两笔上游成本。

    direct 超时、image-job sidecar 的 uncertain 终态、上游回 2xx 却不给图，是同
    一类事故：钱已经花出去了，是否计费无从确认或已确认发生。三个码都必须命中。
    """
    exc = TEST_UPSTREAM_SERVICES.infrastructure.UpstreamError(
        "upstream result unknown",
        status_code=200,
        error_code=error_code,
    )
    assert TEST_UPSTREAM_SERVICES.direct.is_direct_image_result_unknown(exc) is True


@pytest.mark.parametrize(
    "error_code",
    [None, "moderation_blocked", "bad_response"],
)
def test_ordinary_failures_still_allow_provider_fallback(
    error_code: str | None,
) -> None:
    # 能判定未交付且未扣费的失败保持既有行为：允许换 provider 重试。
    # no_image_returned 已移出本组：它只在上游 2xx 之后产生，换 provider 会
    # 再付一笔，见上面 test_result_unknown_predicate_blocks_provider_fallback。
    exc = TEST_UPSTREAM_SERVICES.infrastructure.UpstreamError(
        "ordinary failure",
        error_code=error_code,
    )
    assert TEST_UPSTREAM_SERVICES.direct.is_direct_image_result_unknown(exc) is False


def test_non_upstream_exception_is_not_treated_as_unknown() -> None:
    # 谓词只认 UpstreamError，裸异常不得被误判成「上游可能已扣费」。
    assert (
        TEST_UPSTREAM_SERVICES.direct.is_direct_image_result_unknown(
            TimeoutError("plain timeout"),
        )
        is False
    )
