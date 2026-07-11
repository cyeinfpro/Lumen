"""Direct Images API result extraction helpers."""

from __future__ import annotations

import base64
from typing import Any, Protocol

ImageResult = tuple[str, str | None]


class FetchImageUrlAsBytes(Protocol):
    async def __call__(
        self,
        image_url: str,
        *,
        proxy_url: str | None = None,
    ) -> bytes: ...


class UpstreamErrorType(Protocol):
    def __call__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Exception: ...


class ExtractImageResults(Protocol):
    async def __call__(
        self,
        payload: Any,
        status_code: int,
        *,
        proxy_url: str | None = None,
    ) -> list[ImageResult]: ...


async def _extract_image_results(
    payload: Any,
    status_code: int,
    *,
    fetch_image_url_as_bytes: FetchImageUrlAsBytes,
    upstream_error_type: UpstreamErrorType,
    bad_response_error_code: str,
    no_image_returned_error_code: str,
    proxy_url: str | None = None,
) -> list[ImageResult]:
    """Extract every direct Images API result, downloading URL results as needed."""
    if not isinstance(payload, dict):
        raise upstream_error_type(
            "upstream returned non-object",
            status_code=status_code,
            error_code=bad_response_error_code,
        )
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise upstream_error_type(
            "upstream returned no image",
            status_code=status_code,
            error_code=no_image_returned_error_code,
            payload=payload,
        )

    results: list[ImageResult] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        revised = item.get("revised_prompt")
        if not isinstance(revised, str):
            revised = None

        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            results.append((b64, revised))
            continue

        image_url = item.get("url")
        if isinstance(image_url, str) and image_url:
            raw = await fetch_image_url_as_bytes(image_url, proxy_url=proxy_url)
            results.append((base64.b64encode(raw).decode("ascii"), revised))

    if results:
        return results

    raise upstream_error_type(
        "upstream returned no image",
        status_code=status_code,
        error_code=no_image_returned_error_code,
        payload=payload,
    )


async def _extract_image_result(
    payload: Any,
    status_code: int,
    *,
    extract_image_results: ExtractImageResults,
    proxy_url: str | None = None,
) -> ImageResult:
    """Compatibility helper for callers that expect only the first image."""
    return (
        await extract_image_results(
            payload,
            status_code,
            proxy_url=proxy_url,
        )
    )[0]
