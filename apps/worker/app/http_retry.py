"""Shared bounded HTTP POST retry policy."""

from __future__ import annotations

import asyncio
import email.utils
import logging
import math
import random
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx


logger = logging.getLogger(__name__)

RETRY_STATUS = {502, 503, 504}
RETRY_HTTPX_EXC: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
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


async def post_with_retry(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    data: dict[str, str] | None = None,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    timeout: httpx.Timeout | None = None,
    retry_httpx_exceptions: bool = True,
    max_attempts: int = 2,
    backoff_base_s: float = 1.0,
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
) -> httpx.Response:
    """POST with bounded retries for transient transport and gateway failures."""
    last_exc: BaseException | None = None
    last_resp: httpx.Response | None = None
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(
                transient_retry_sleep_seconds(
                    attempt=attempt,
                    backoff_base_s=backoff_base_s,
                    response=last_resp,
                )
            )
        if before_attempt is not None:
            await before_attempt(attempt + 1)
        try:
            if json_body is not None:
                if timeout is None:
                    response = await client.post(
                        url,
                        json=json_body,
                        headers=headers,
                    )
                else:
                    response = await client.post(
                        url,
                        json=json_body,
                        headers=headers,
                        timeout=timeout,
                    )
            else:
                if timeout is None:
                    response = await client.post(
                        url,
                        data=data,
                        files=files,
                        headers=headers,
                    )
                else:
                    response = await client.post(
                        url,
                        data=data,
                        files=files,
                        headers=headers,
                        timeout=timeout,
                    )
        except RETRY_HTTPX_EXC as exc:
            if not retry_httpx_exceptions:
                raise
            last_exc = exc
            logger.warning(
                "upstream transient httpx error attempt=%d/%d url=%s err=%r",
                attempt + 1,
                max_attempts,
                url,
                exc,
            )
            continue
        if response.status_code in RETRY_STATUS:
            last_resp = response
            logger.warning(
                "upstream transient status attempt=%d/%d url=%s status=%d",
                attempt + 1,
                max_attempts,
                url,
                response.status_code,
            )
            continue
        return response
    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry loop exhausted without response or exception")
