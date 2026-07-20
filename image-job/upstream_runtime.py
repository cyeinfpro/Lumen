"""Upstream request execution, response buffering, and retry policy."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx


@dataclass(frozen=True)
class UpstreamFacade:
    http_client: Callable[[], Any | None]
    upstream_base_url: Callable[[], str]
    upstream_idempotency_guaranteed: Callable[[], bool]
    retry_network_max: Callable[[], int]
    retry_responses_stream_max: Callable[[], int]
    retry_upstream_5xx_max: Callable[[], int]
    retry_backoff_s: Callable[[], float]
    max_upstream_error_body_bytes: Callable[[], int]
    max_upstream_response_bytes: Callable[[], int]
    max_image_bytes: Callable[[], int]
    error_class_network: Callable[[], str]
    error_class_upstream_4xx: Callable[[], str]
    error_class_upstream_5xx: Callable[[], str]
    error_class_no_image: Callable[[], str]
    error_class_image_save: Callable[[], str]
    error_class_internal: Callable[[], str]
    job_failure: Callable[..., Any]
    job_failure_type: type[BaseException]
    parse_json_bytes: Callable[[bytes], Any | None]
    body_preview: Callable[[bytes], Any]
    read_response_body_bounded: Callable[
        ...,
        Awaitable[tuple[bytes, bool, int]],
    ]
    extract_response_images: Callable[..., Awaitable[list[Any]]]
    extract_responses_stream_images: Callable[..., Awaitable[list[Any]]]
    materialize_edit_input_files: Callable[..., Awaitable[Any]]
    materialize_edit_input_urls: Callable[..., Awaitable[dict[str, Any]]]
    save_images: Callable[..., Awaitable[list[dict[str, Any]]]]
    normalize_image_edit_input_transport: Callable[[Any], str]
    upstream_idempotency_key: Callable[[str], str]
    call_upstream_once_fn: Callable[..., Awaitable[tuple[int, list[dict[str, Any]]]]]
    log: logging.Logger

    @staticmethod
    def classify_httpx_error(exc: httpx.HTTPError) -> bool:
        return isinstance(
            exc,
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.PoolTimeout,
                httpx.WriteError,
                httpx.WriteTimeout,
            ),
        )

    @staticmethod
    def httpx_error_requires_idempotency(exc: httpx.HTTPError) -> bool:
        return not isinstance(
            exc,
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.PoolTimeout,
            ),
        )

    def is_retryable_job_failure(self, exc: Any) -> bool:
        return bool(
            exc.retryable
            or exc.error_class
            in {
                self.error_class_network(),
                self.error_class_upstream_5xx(),
            }
        )

    def mark_post_dispatch_failure(self, exc: Any) -> Any:
        if self.is_retryable_job_failure(exc) and (
            exc.retry_requires_idempotency
            or exc.error_class == self.error_class_upstream_5xx()
        ):
            exc.retry_requires_idempotency = True
            exc.outcome_uncertain = True
        return exc

    def retry_budget_for_failure(self, exc: Any, *, endpoint: str) -> int:
        if exc.error_class == self.error_class_network():
            if endpoint == "/v1/responses":
                return max(
                    self.retry_network_max(),
                    self.retry_responses_stream_max(),
                )
            return self.retry_network_max()
        if exc.error_class == self.error_class_upstream_5xx():
            return self.retry_upstream_5xx_max()
        return 0

    async def raise_upstream_http_error(self, response: httpx.Response) -> None:
        content, truncated, _received = await self.read_response_body_bounded(
            response,
            max_bytes=self.max_upstream_error_body_bytes(),
            truncate=True,
        )
        upstream_body: Any = self.body_preview(content)
        if truncated:
            upstream_body = {"preview": upstream_body, "truncated": True}
        is_5xx = response.status_code >= 500
        raise self.job_failure(
            f"上游返回 HTTP {response.status_code}",
            upstream_status=response.status_code,
            upstream_body=upstream_body,
            retryable=is_5xx,
            retry_requires_idempotency=is_5xx,
            outcome_uncertain=is_5xx,
            error_class=(
                self.error_class_upstream_5xx()
                if is_5xx
                else self.error_class_upstream_4xx()
            ),
        )

    async def extract_non_stream_response_images(
        self,
        response: httpx.Response,
        client: httpx.AsyncClient,
    ) -> list[Any]:
        content_type = (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        is_direct_image = content_type.startswith("image/")
        body_limit = (
            min(self.max_upstream_response_bytes(), self.max_image_bytes())
            if is_direct_image
            else self.max_upstream_response_bytes()
        )
        content, truncated, _received = await self.read_response_body_bounded(
            response,
            max_bytes=body_limit,
            truncate=False,
        )
        if truncated:
            limit_name = "单图" if is_direct_image else "非流式响应"
            raise self.job_failure(
                f"上游{limit_name}超过大小限制（max {body_limit} bytes）",
                upstream_status=response.status_code,
                retry_requires_idempotency=True,
                outcome_uncertain=True,
                error_class=self.error_class_image_save(),
            )
        buffered = httpx.Response(
            response.status_code,
            headers=response.headers,
            content=content,
        )
        return await self.extract_response_images(buffered, client)

    async def _extract_dispatched_response(
        self,
        row: Any,
        response: Any,
        client: Any,
        *,
        endpoint: str,
    ) -> list[Any]:
        content_type = response.headers.get("content-type", "").lower()
        try:
            if endpoint == "/v1/responses" and "text/event-stream" in content_type:
                return await self.extract_responses_stream_images(
                    response,
                    client,
                    job_id=row["job_id"],
                )
            return await self.extract_non_stream_response_images(response, client)
        except self.job_failure_type as exc:
            raise self.mark_post_dispatch_failure(exc)
        except httpx.HTTPError:
            raise
        except Exception as exc:
            response_kind = (
                "流式响应" if "text/event-stream" in content_type else "响应"
            )
            raise self.job_failure(
                f"解析上游{response_kind}失败: {exc.__class__.__name__}: {exc}",
                upstream_status=response.status_code,
                retry_requires_idempotency=True,
                outcome_uncertain=True,
                error_class=self.error_class_image_save(),
            ) from exc

    async def call_upstream_once(
        self,
        row: Any,
        *,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        endpoint: str,
        image_edit_input_transport: str = "url",
    ) -> tuple[int, list[dict[str, Any]]]:
        client = self.http_client()
        assert client is not None
        request_headers = headers
        request_kwargs: dict[str, Any]
        if endpoint == "/v1/images/edits" and image_edit_input_transport == "file":
            request_headers = dict(headers)
            request_headers.pop("Content-Type", None)
            data, files = await self.materialize_edit_input_files(client, body)
            request_kwargs = {"data": data, "files": files}
        else:
            request_kwargs = {"json": body}

        async with client.stream(
            "POST",
            url,
            headers=request_headers,
            **request_kwargs,
        ) as response:
            status_code = response.status_code
            if response.status_code >= 400:
                await self.raise_upstream_http_error(response)
            candidates = await self._extract_dispatched_response(
                row,
                response,
                client,
                endpoint=endpoint,
            )

        if not candidates:
            raise self.job_failure(
                "上游没有返回可保存的图片",
                upstream_status=status_code,
                error_class=self.error_class_no_image(),
            )
        try:
            images = await self.save_images(
                row["job_id"],
                row["created_at"],
                row["retention_days"],
                candidates,
            )
        except self.job_failure_type:
            raise
        except Exception as exc:
            raise self.job_failure(
                f"保存图片失败: {exc.__class__.__name__}: {exc}",
                upstream_status=status_code,
                error_class=self.error_class_image_save(),
            ) from exc
        if not images:
            raise self.job_failure(
                "没有保存任何图片",
                upstream_status=status_code,
                error_class=self.error_class_image_save(),
            )
        return status_code, images

    async def call_upstream(self, row: Any) -> tuple[int, list[dict[str, Any]]]:
        if self.http_client() is None:
            raise self.job_failure(
                "HTTP client not ready",
                error_class=self.error_class_internal(),
            )
        payload = self.parse_json_bytes(row["payload_json"].encode("utf-8"))
        if not isinstance(payload, dict):
            raise self.job_failure(
                "job payload is not valid strict JSON",
                error_class=self.error_class_internal(),
            )
        auth_header = row["auth_header"]
        if not auth_header:
            raise self.job_failure(
                "job is missing Authorization header",
                error_class=self.error_class_internal(),
            )

        endpoint = payload["endpoint"]
        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream, image/*",
            "Accept-Encoding": "identity",
            "Idempotency-Key": self.upstream_idempotency_key(row["job_id"]),
        }
        body = payload["body"]
        image_edit_input_transport = self.normalize_image_edit_input_transport(
            payload.get("image_edit_input_transport")
        )
        if (
            endpoint == "/v1/images/edits"
            and isinstance(body, dict)
            and image_edit_input_transport == "url"
        ):
            body = await self.materialize_edit_input_urls(row, body)

        max_budget = max(
            self.retry_network_max(),
            self.retry_responses_stream_max(),
            self.retry_upstream_5xx_max(),
        )
        for attempt in range(max_budget + 1):
            try:
                return await self.call_upstream_once_fn(
                    row,
                    url=f"{self.upstream_base_url()}{endpoint}",
                    headers=headers,
                    body=body,
                    endpoint=endpoint,
                    image_edit_input_transport=image_edit_input_transport,
                )
            except httpx.HTTPError as exc:
                requires_idempotency = self.httpx_error_requires_idempotency(exc)
                failure = self.job_failure(
                    f"上游请求失败: {exc.__class__.__name__}: {exc}",
                    retryable=self.classify_httpx_error(exc),
                    retry_requires_idempotency=requires_idempotency,
                    outcome_uncertain=requires_idempotency,
                    error_class=self.error_class_network(),
                )
            except self.job_failure_type as exc:
                failure = exc

            retry_budget = self.retry_budget_for_failure(
                failure,
                endpoint=endpoint,
            )
            retryable = self.is_retryable_job_failure(failure)
            requires_idempotency = (
                failure.retry_requires_idempotency
                or failure.error_class == self.error_class_upstream_5xx()
            )
            if (
                retryable
                and requires_idempotency
                and not self.upstream_idempotency_guaranteed()
            ):
                failure.retry_suppressed = attempt < retry_budget
                if failure.retry_suppressed:
                    self.log.warning(
                        "image job %s automatic retry suppressed endpoint=%s "
                        "class=%s; upstream idempotency is not guaranteed",
                        row["job_id"],
                        endpoint,
                        failure.error_class,
                    )
                raise failure
            if attempt < retry_budget and retryable:
                self.log.warning(
                    "image job %s upstream retryable failure, retry %d/%d "
                    "endpoint=%s class=%s: %s",
                    row["job_id"],
                    attempt + 1,
                    retry_budget,
                    endpoint,
                    failure.error_class,
                    failure.error,
                )
                await asyncio.sleep(self.retry_backoff_s() * (2**attempt))
                continue
            raise failure

        raise AssertionError("unreachable upstream retry loop")
