from __future__ import annotations

import base64
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from app import upstream
from app.upstream_parts import direct_images


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


def test_upstream_facades_keep_legacy_async_signatures() -> None:
    for name in ("_extract_image_results", "_extract_image_result"):
        facade = getattr(upstream, name)
        signature = inspect.signature(facade)

        assert inspect.iscoroutinefunction(facade)
        assert tuple(signature.parameters) == (
            "payload",
            "status_code",
            "proxy_url",
        )
        assert signature.parameters["payload"].default is inspect.Parameter.empty
        assert (
            signature.parameters["status_code"].default is inspect.Parameter.empty
        )
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

    monkeypatch.setattr(upstream, "_fetch_image_url_as_bytes", fake_fetch)
    monkeypatch.setattr(upstream, "UpstreamError", CurrentUpstreamError)
    monkeypatch.setattr(
        upstream,
        "EC",
        SimpleNamespace(
            BAD_RESPONSE=SimpleNamespace(value="current-bad-response"),
            NO_IMAGE_RETURNED=SimpleNamespace(value="current-no-image"),
        ),
    )
    monkeypatch.setattr(direct_images, "_extract_image_results", fake_extract)

    payload = {"data": [{"b64_json": "ignored"}]}
    assert await upstream._extract_image_results(
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

    monkeypatch.setattr(upstream, "_extract_image_results", fake_results_facade)

    payload = {"data": [{"b64_json": "ignored"}]}
    assert await upstream._extract_image_result(
        payload,
        208,
        proxy_url="http://chain-proxy",
    ) == ("patched-first", "patched-prompt")
    assert seen == [(payload, 208, "http://chain-proxy")]
