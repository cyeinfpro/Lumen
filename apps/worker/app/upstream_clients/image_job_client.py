"""Independent HTTP client for the image-job control plane."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from ..provider_runtime.contracts import ImageJobEndpoint
from .image_job_auth import (
    image_job_headers,
    image_job_service_headers,
    image_job_submit_headers,
)
from .image_job_models import (
    ImageJobCancelOutcome,
    ImageJobCancelResult,
    ImageJobHandle,
    ImageJobStatus,
    UploadedReference,
)
from .url_validation import validate_image_job_control_url

PostWithRetry = Callable[..., Awaitable[httpx.Response]]


class ImageJobClientError(Exception):
    def __init__(
        self,
        message: str,
        *,
        operation: str,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
        transient: bool = False,
        result_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.status_code = status_code
        self.payload = payload or {}
        self.transient = transient
        self.result_unknown = result_unknown


class ImageJobClient:
    """Control-plane adapter that cannot inherit supplier proxy configuration."""

    def __init__(
        self,
        endpoint: ImageJobEndpoint,
        *,
        timeout: httpx.Timeout,
        transport: httpx.AsyncBaseTransport | None = None,
        post_with_retry: PostWithRetry | None = None,
    ) -> None:
        base_url = validate_image_job_control_url(
            endpoint.base_url,
            allow_private_http=endpoint.allow_private_http,
        )
        self._endpoint = ImageJobEndpoint(
            base_url=base_url,
            service_token=endpoint.service_token,
            allow_private_http=endpoint.allow_private_http,
        )
        self._post_with_retry = post_with_retry
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )
        self._closed = False

    @property
    def base_url(self) -> str:
        return self._endpoint.base_url

    @property
    def http_client(self) -> httpx.AsyncClient:
        return self._client

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("image-job client is closed")

    async def submit(
        self,
        payload: dict[str, Any],
        *,
        upstream_api_key: str,
        trace_id: str,
        before_attempt: Callable[[int], Awaitable[None]] | None = None,
        idempotency_key: str | None = None,
    ) -> ImageJobHandle:
        self._ensure_open()
        url = f"{self.base_url}/v1/image-jobs"
        headers = image_job_submit_headers(
            service_token=self._endpoint.service_token,
            upstream_api_key=upstream_api_key,
            trace_id=trace_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        try:
            if self._post_with_retry is None:
                if before_attempt is not None:
                    await before_attempt(1)
                response = await self._client.post(url, headers=headers, json=payload)
            else:
                response = await self._post_with_retry(
                    client=self._client,
                    url=url,
                    headers=headers,
                    json_body=payload,
                    max_attempts=3,
                    retry_httpx_exceptions=False,
                    retry_status_codes=False,
                    before_attempt=before_attempt,
                )
        except (httpx.HTTPError, OSError) as exc:
            raise ImageJobClientError(
                f"image job submit failed: {exc}",
                operation="submit",
                transient=True,
                result_unknown=not isinstance(
                    exc,
                    (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
                ),
            ) from exc
        data = self._json_object(response, operation="submit")
        if response.status_code >= 400:
            raise ImageJobClientError(
                "image job submit rejected",
                operation="submit",
                status_code=response.status_code,
                payload=data,
                transient=response.status_code in {408, 425, 429}
                or response.status_code >= 500,
                result_unknown=response.status_code >= 500,
            )
        job_id = data.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ImageJobClientError(
                "image job submit returned no job_id",
                operation="submit",
                status_code=response.status_code,
                payload=data,
                result_unknown=True,
            )
        return ImageJobHandle(
            job_id=job_id,
            upstream_api_key=upstream_api_key,
            status_code=response.status_code,
        )

    async def poll(
        self,
        handle: ImageJobHandle,
        *,
        trace_id: str,
    ) -> ImageJobStatus | None:
        self._ensure_open()
        url = f"{self.base_url}/v1/image-jobs/{quote(handle.job_id, safe='')}"
        try:
            response = await self._client.get(
                url,
                headers=image_job_headers(
                    service_token=self._endpoint.service_token,
                    upstream_api_key=handle.upstream_api_key,
                    trace_id=trace_id,
                ),
            )
        except (httpx.HTTPError, OSError):
            return None
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            return None
        data = self._json_object(response, operation="poll")
        if response.status_code >= 400:
            raise ImageJobClientError(
                "image job poll rejected",
                operation="poll",
                status_code=response.status_code,
                payload=data,
            )
        return ImageJobStatus(payload=data, status_code=response.status_code)

    async def upload_reference(
        self,
        raw: bytes,
        mime: str,
        *,
        trace_id: str,
    ) -> UploadedReference | None:
        self._ensure_open()
        try:
            response = await self._client.post(
                f"{self.base_url}/v1/refs",
                content=raw,
                headers=image_job_service_headers(
                    service_token=self._endpoint.service_token,
                    trace_id=trace_id,
                    content_type=mime,
                ),
            )
        except (httpx.HTTPError, OSError):
            return None
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        url = data.get("url") if isinstance(data, dict) else None
        return UploadedReference(url) if isinstance(url, str) and url else None

    async def cancel(
        self,
        handle: ImageJobHandle,
        *,
        trace_id: str,
    ) -> ImageJobCancelResult:
        self._ensure_open()
        try:
            response = await self._client.delete(
                f"{self.base_url}/v1/image-jobs/{quote(handle.job_id, safe='')}",
                headers=image_job_headers(
                    service_token=self._endpoint.service_token,
                    upstream_api_key=handle.upstream_api_key,
                    trace_id=trace_id,
                ),
            )
        except (httpx.HTTPError, OSError):
            return ImageJobCancelResult(
                job_id=handle.job_id,
                outcome=ImageJobCancelOutcome.UNCERTAIN,
                status="unknown",
                status_code=None,
                outcome_uncertain=True,
            )
        try:
            data = self._json_object(response, operation="cancel")
        except ImageJobClientError:
            return ImageJobCancelResult(
                job_id=handle.job_id,
                outcome=ImageJobCancelOutcome.UNCERTAIN,
                status="unknown",
                status_code=response.status_code,
                outcome_uncertain=True,
            )
        raw_outcome = data.get("outcome")
        try:
            outcome = ImageJobCancelOutcome(str(raw_outcome))
        except ValueError:
            outcome = ImageJobCancelOutcome.UNCERTAIN
        if response.status_code >= 400:
            outcome = ImageJobCancelOutcome.UNCERTAIN
        return ImageJobCancelResult(
            job_id=handle.job_id,
            outcome=outcome,
            status=str(data.get("status") or "unknown"),
            status_code=response.status_code,
            outcome_uncertain=bool(data.get("outcome_uncertain"))
            or outcome
            in {
                ImageJobCancelOutcome.CANCEL_REQUESTED,
                ImageJobCancelOutcome.UNCERTAIN,
            },
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._client, "aclose", None)
        if callable(close):
            await close()

    async def __aenter__(self) -> "ImageJobClient":
        self._ensure_open()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    @staticmethod
    def _json_object(
        response: httpx.Response,
        *,
        operation: str,
    ) -> dict[str, Any]:
        transient = (
            response.status_code in {408, 425, 429} or response.status_code >= 500
        )
        result_unknown = operation == "submit" and (
            200 <= response.status_code < 300 or response.status_code >= 500
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise ImageJobClientError(
                f"image job {operation} returned invalid JSON",
                operation=operation,
                status_code=response.status_code,
                transient=transient,
                result_unknown=result_unknown,
            ) from exc
        if not isinstance(data, dict):
            raise ImageJobClientError(
                f"image job {operation} returned non-object",
                operation=operation,
                status_code=response.status_code,
                transient=transient,
                result_unknown=result_unknown,
            )
        return data


__all__ = ["ImageJobClient", "ImageJobClientError"]
