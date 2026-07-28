"""Shared bounded HTTP POST retry policy."""

from __future__ import annotations

import asyncio
import email.utils
import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, NotRequired, TypedDict, Unpack

import httpx


logger = logging.getLogger(__name__)

RETRY_STATUS = frozenset({502, 503, 504})
DEFAULT_RESPONSE_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_ERROR_RESPONSE_MAX_BYTES = 64 * 1024
RETRY_HTTPX_EXC: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)


class _PostWithRetryArgs(TypedDict):
    client: httpx.AsyncClient
    url: str
    headers: dict[str, str]
    json_body: NotRequired[dict[str, Any] | None]
    data: NotRequired[dict[str, str] | None]
    files: NotRequired[list[tuple[str, tuple[str, bytes, str]]] | None]
    timeout: NotRequired[httpx.Timeout | None]
    retry_httpx_exceptions: NotRequired[bool]
    max_attempts: NotRequired[int]
    backoff_base_s: NotRequired[float]
    before_attempt: NotRequired[Callable[[int], Awaitable[None]] | None]
    max_response_bytes: NotRequired[int]
    max_error_response_bytes: NotRequired[int]


@dataclass(frozen=True)
class _PostWithRetryRequest:
    client: httpx.AsyncClient
    url: str
    headers: dict[str, str]
    json_body: dict[str, Any] | None = None
    data: dict[str, str] | None = None
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None
    timeout: httpx.Timeout | None = None
    retry_httpx_exceptions: bool = True
    max_attempts: int = 2
    backoff_base_s: float = 1.0
    before_attempt: Callable[[int], Awaitable[None]] | None = None
    max_response_bytes: int = DEFAULT_RESPONSE_MAX_BYTES
    max_error_response_bytes: int = DEFAULT_ERROR_RESPONSE_MAX_BYTES


class ResponseBodyTooLarge(httpx.HTTPError):
    def __init__(self, *, max_bytes: int, received_bytes: int) -> None:
        super().__init__(f"upstream response body exceeded {max_bytes} bytes")
        self.max_bytes = max_bytes
        self.received_bytes = received_bytes


async def _read_streamed_body(
    response: httpx.Response,
    *,
    max_bytes: int,
    truncate: bool,
) -> tuple[bytes, bool]:
    body = bytearray()
    received_bytes = 0
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        received_bytes += len(chunk)
        remaining = max_bytes - len(body)
        if received_bytes > max_bytes:
            if truncate and remaining > 0:
                body.extend(chunk[:remaining])
            if truncate:
                return bytes(body), True
            raise ResponseBodyTooLarge(
                max_bytes=max_bytes,
                received_bytes=received_bytes,
            )
        body.extend(chunk)
    return bytes(body), False


def _buffered_response(
    response: httpx.Response,
    *,
    content: bytes,
    truncated: bool,
) -> httpx.Response:
    headers = [
        (key, value)
        for key, value in response.headers.multi_items()
        if key.lower()
        not in {"content-encoding", "content-length", "transfer-encoding"}
    ]
    try:
        request = response.request
    except RuntimeError:
        request = None
    extensions = dict(response.extensions)
    if truncated:
        extensions["lumen_body_truncated"] = True
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=content,
        request=request,
        extensions=extensions,
    )


async def _post_once(
    *,
    client: httpx.AsyncClient,
    url: str,
    request_kwargs: dict[str, Any],
    max_response_bytes: int,
    max_error_response_bytes: int,
) -> httpx.Response:
    stream = getattr(client, "stream", None)
    if not callable(stream):
        response = await client.post(url, **request_kwargs)
        limit = (
            max_response_bytes
            if 200 <= response.status_code < 300
            else max_error_response_bytes
        )
        if len(response.content) > limit:
            if 200 <= response.status_code < 300:
                raise ResponseBodyTooLarge(
                    max_bytes=limit,
                    received_bytes=len(response.content),
                )
            return _buffered_response(
                response,
                content=response.content[:limit],
                truncated=True,
            )
        return response

    async with stream("POST", url, **request_kwargs) as response:
        is_success = 200 <= response.status_code < 300
        body, truncated = await _read_streamed_body(
            response,
            max_bytes=(max_response_bytes if is_success else max_error_response_bytes),
            truncate=not is_success,
        )
        return _buffered_response(
            response,
            content=body,
            truncated=truncated,
        )


def parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    stripped = value.strip()
    try:
        seconds = float(stripped)
    except (TypeError, ValueError):
        try:
            retry_at = email.utils.parsedate_to_datetime(stripped)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, 15.0)


def transient_retry_sleep_seconds(
    *,
    attempt: int,
    backoff_base_s: float,
    response: httpx.Response | None = None,
) -> float:
    retry_after = parse_retry_after_seconds(
        response.headers.get("retry-after") if response is not None else None
    )
    if retry_after is not None:
        return retry_after
    base = min(8.0, backoff_base_s * (2 ** max(0, attempt - 1)))
    return max(0.05, base * random.uniform(0.6, 1.4))


async def post_with_retry(**kwargs: Unpack[_PostWithRetryArgs]) -> httpx.Response:
    """POST with bounded retries for transient transport and gateway failures."""
    request = _PostWithRetryRequest(**kwargs)
    last_exc: BaseException | None = None
    last_resp: httpx.Response | None = None
    for attempt in range(request.max_attempts):
        if attempt > 0:
            await asyncio.sleep(
                transient_retry_sleep_seconds(
                    attempt=attempt,
                    backoff_base_s=request.backoff_base_s,
                    response=last_resp,
                )
            )
        if request.before_attempt is not None:
            await request.before_attempt(attempt + 1)
        try:
            request_kwargs: dict[str, Any] = {"headers": request.headers}
            if request.json_body is not None:
                request_kwargs["json"] = request.json_body
            else:
                request_kwargs["data"] = request.data
                request_kwargs["files"] = request.files
            if request.timeout is not None:
                request_kwargs["timeout"] = request.timeout
            response = await _post_once(
                client=request.client,
                url=request.url,
                request_kwargs=request_kwargs,
                max_response_bytes=request.max_response_bytes,
                max_error_response_bytes=request.max_error_response_bytes,
            )
        except RETRY_HTTPX_EXC as exc:
            if not request.retry_httpx_exceptions:
                raise
            last_exc = exc
            logger.warning(
                "upstream transient httpx error attempt=%d/%d url=%s err=%r",
                attempt + 1,
                request.max_attempts,
                request.url,
                exc,
            )
            continue
        if response.status_code in RETRY_STATUS:
            last_resp = response
            logger.warning(
                "upstream transient status attempt=%d/%d url=%s status=%d",
                attempt + 1,
                request.max_attempts,
                request.url,
                response.status_code,
            )
            continue
        return response
    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry loop exhausted without response or exception")
