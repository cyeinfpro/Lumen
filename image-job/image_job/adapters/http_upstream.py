"""Runtime-scoped upstream HTTP adapter."""

from __future__ import annotations

from typing import Any

import httpx

from ..config import ImageJobSettings
from ..contracts import JobFailure
from ..processing import ImageProcessing


class HttpUpstreamGateway:
    def __init__(self, settings: ImageJobSettings) -> None:
        self.settings = settings
        self.client: httpx.AsyncClient | None = None
        self.processing = ImageProcessing(
            settings,
            http_client=lambda: self.client,
        )

    async def startup(self) -> None:
        timeout = httpx.Timeout(
            self.settings.timeouts.upstream_s,
            connect=self.settings.timeouts.connect_s,
            write=60.0,
            pool=30.0,
        )
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(
                max_keepalive_connections=self.settings.http_pool_keepalive,
                max_connections=self.settings.http_pool_max,
            ),
            follow_redirects=True,
            http2=False,
            trust_env=False,
            headers={"User-Agent": "lumen-image"},
        )

    async def shutdown(self) -> None:
        client = self.client
        self.client = None
        if client is not None:
            await client.aclose()

    async def call(self, row: Any) -> tuple[int, list[dict[str, Any]]]:
        return await self.processing.call_upstream(row)

    def is_retryable_failure(self, failure: JobFailure) -> bool:
        return self.processing.upstream_facade.is_retryable_job_failure(failure)
