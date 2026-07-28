"""Direct Images API result extraction helpers."""

from __future__ import annotations

from typing import Any, Collection, Protocol

from .generated_payload import (
    DEFAULT_MAX_GENERATED_BATCH_BYTES,
    DEFAULT_MAX_GENERATED_IMAGE_BYTES,
    GeneratedImageResult,
    GeneratedPayload,
    coerce_generated_payload,
    decode_inline_image_base64,
    generated_payload_size,
    validate_remote_image_url,
)

ImageResult = GeneratedImageResult


class FetchImageUrlPayload(Protocol):
    async def __call__(
        self,
        image_url: str,
        *,
        proxy_url: str | None = None,
    ) -> GeneratedPayload | bytes: ...


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
    fetch_image_url_as_bytes: FetchImageUrlPayload,
    upstream_error_type: UpstreamErrorType,
    bad_response_error_code: str,
    no_image_returned_error_code: str,
    proxy_url: str | None = None,
    max_image_bytes: int = DEFAULT_MAX_GENERATED_IMAGE_BYTES,
    max_batch_bytes: int = DEFAULT_MAX_GENERATED_BATCH_BYTES,
    allowed_http_result_hosts: Collection[str] = (),
) -> list[ImageResult]:
    """Extract direct API results without normalizing them through base64 strings."""
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
    batch_bytes = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        revised = item.get("revised_prompt")
        if not isinstance(revised, str):
            revised = None

        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            try:
                image_payload = decode_inline_image_base64(
                    b64,
                    max_bytes=max_image_bytes,
                )
            except (TypeError, ValueError) as exc:
                raise upstream_error_type(
                    f"upstream returned invalid image base64: {exc}",
                    status_code=status_code,
                    error_code=bad_response_error_code,
                    payload=payload,
                ) from exc
        else:
            image_url = item.get("url")
            if not isinstance(image_url, str) or not image_url:
                continue
            try:
                validate_remote_image_url(
                    image_url,
                    allowed_http_hosts=allowed_http_result_hosts,
                )
            except ValueError as exc:
                raise upstream_error_type(
                    f"upstream returned unsafe image URL: {exc}",
                    status_code=status_code,
                    error_code=bad_response_error_code,
                    payload=payload,
                ) from exc
            downloaded = await fetch_image_url_as_bytes(
                image_url,
                proxy_url=proxy_url,
            )
            image_payload = coerce_generated_payload(downloaded)

        payload_bytes = generated_payload_size(image_payload)
        if payload_bytes is not None:
            if payload_bytes > max_image_bytes:
                raise upstream_error_type(
                    "upstream image exceeds single-image byte limit",
                    status_code=status_code,
                    error_code=bad_response_error_code,
                    payload=payload,
                )
            batch_bytes += payload_bytes
            if batch_bytes > max_batch_bytes:
                raise upstream_error_type(
                    "upstream image batch exceeds byte limit",
                    status_code=status_code,
                    error_code=bad_response_error_code,
                    payload=payload,
                )
        results.append((image_payload, revised))

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


__all__ = [
    "_extract_image_result",
    "_extract_image_results",
]
