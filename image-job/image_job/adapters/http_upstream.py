"""Runtime-scoped upstream HTTP adapter."""

from __future__ import annotations

from typing import Any

import httpx

from ..config import ImageJobSettings
from ..contracts import JobFailure
from ..ports.jobs import JobHeartbeatPort
from ..processing import ImageProcessing
from ..url_security import (
    UpstreamHttpTarget,
    UpstreamRedirectGuard,
    canonical_host,
    pinned_async_http_transport,
    resolve_upstream_redirect_url,
)


_UPSTREAM_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_UPSTREAM_REDIRECTS = 5


class _OwnedResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        owner: httpx.AsyncClient,
    ) -> None:
        self._stream = stream
        self._owner = owner
        self._closed = False

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._stream.aclose()
        finally:
            await self._owner.aclose()


def _http_origin(url: httpx.URL) -> tuple[str, str, int]:
    default_port = 443 if url.scheme == "https" else 80
    return url.scheme, canonical_host(url.host), url.port or default_port


def _is_https_upgrade(source: httpx.URL, target: httpx.URL) -> bool:
    return (
        source.scheme == "http"
        and target.scheme == "https"
        and canonical_host(source.host) == canonical_host(target.host)
        and (source.port or 80) == 80
        and (target.port or 443) == 443
    )


class RedirectSafeAsyncClient(httpx.AsyncClient):
    """AsyncClient-compatible upstream client with explicit safe redirects."""

    def __init__(
        self,
        *,
        upstream_base_url: str,
        timeout: httpx.Timeout,
        limits: httpx.Limits,
        max_redirects: int = _MAX_UPSTREAM_REDIRECTS,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._upstream_base_url = upstream_base_url
        self._hop_timeout = timeout
        self._hop_limits = limits
        super().__init__(
            timeout=timeout,
            limits=limits,
            max_redirects=max_redirects,
            follow_redirects=False,
            http2=False,
            trust_env=False,
            headers=headers,
        )

    async def _open_pinned_response(
        self,
        request: httpx.Request,
        target: UpstreamHttpTarget,
    ) -> httpx.Response:
        transport = pinned_async_http_transport(
            target,
            limits=self._hop_limits,
            http2=False,
        )
        client = httpx.AsyncClient(
            transport=transport,
            timeout=self._hop_timeout,
            follow_redirects=False,
            http2=False,
            trust_env=False,
        )
        try:
            response = await client.send(
                request,
                stream=True,
                follow_redirects=False,
            )
        except BaseException:
            await client.aclose()
            raise
        if not isinstance(response.stream, httpx.AsyncByteStream):
            await response.aclose()
            await client.aclose()
            raise RuntimeError("upstream response did not provide an async stream")
        response.stream = _OwnedResponseStream(response.stream, client)
        return response

    @staticmethod
    def _redirect_method(request: httpx.Request, status_code: int) -> str:
        if status_code == 303 and request.method != "HEAD":
            return "GET"
        if status_code == 302 and request.method != "HEAD":
            return "GET"
        if status_code == 301 and request.method == "POST":
            return "GET"
        return request.method

    def _build_redirect_request(
        self,
        request: httpx.Request,
        *,
        status_code: int,
        url: str,
    ) -> httpx.Request:
        target_url = httpx.URL(url)
        method = self._redirect_method(request, status_code)
        headers = httpx.Headers(request.headers)
        if _http_origin(request.url) != _http_origin(target_url):
            if not _is_https_upgrade(request.url, target_url):
                headers.pop("Authorization", None)
            headers["Host"] = target_url.netloc.decode("ascii")
        if method == "GET" and method != request.method:
            headers.pop("Content-Length", None)
            headers.pop("Transfer-Encoding", None)
        headers.pop("Cookie", None)
        stream = (
            None if method == "GET" and method != request.method else request.stream
        )
        return httpx.Request(
            method=method,
            url=target_url,
            headers=headers,
            cookies=httpx.Cookies(self.cookies),
            stream=stream,
            extensions=request.extensions,
        )

    async def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        auth: Any = None,
        follow_redirects: Any = None,
    ) -> httpx.Response:
        _ = auth, follow_redirects
        if self.is_closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise RuntimeError(
                "Attempted to send a sync request with an AsyncClient instance."
            )

        guard = UpstreamRedirectGuard(self._upstream_base_url)
        current_request = request
        previous_url: str | None = None
        history: list[httpx.Response] = []
        while True:
            target = await guard.resolve(
                str(current_request.url),
                previous_url=previous_url,
            )
            response = await self._open_pinned_response(current_request, target)
            self.cookies.extract_cookies(response)
            response.history = list(history)
            if response.status_code not in _UPSTREAM_REDIRECT_STATUSES:
                if not stream:
                    try:
                        await response.aread()
                    except BaseException:
                        await response.aclose()
                        raise
                return response

            try:
                location = response.headers.get("location") or ""
                if len(history) >= self.max_redirects:
                    raise httpx.TooManyRedirects(
                        "Exceeded maximum allowed upstream redirects.",
                        request=current_request,
                    )
                redirect_url = resolve_upstream_redirect_url(
                    target.url,
                    location,
                )
                next_request = self._build_redirect_request(
                    current_request,
                    status_code=response.status_code,
                    url=redirect_url,
                )
            except BaseException:
                await response.aclose()
                raise

            await response.aclose()
            history.append(response)
            previous_url = target.url
            current_request = next_request


class HttpUpstreamGateway:
    def __init__(
        self,
        settings: ImageJobSettings,
        *,
        heartbeat: JobHeartbeatPort,
    ) -> None:
        self.settings = settings
        self.client: httpx.AsyncClient | None = None
        self.processing = ImageProcessing(
            settings,
            http_client=lambda: self.client,
            heartbeat=heartbeat,
        )

    async def startup(self) -> None:
        timeout = httpx.Timeout(
            self.settings.timeouts.upstream_s,
            connect=self.settings.timeouts.connect_s,
            write=60.0,
            pool=30.0,
        )
        self.client = RedirectSafeAsyncClient(
            upstream_base_url=self.settings.upstream_base_url,
            timeout=timeout,
            limits=httpx.Limits(
                max_keepalive_connections=self.settings.http_pool_keepalive,
                max_connections=self.settings.http_pool_max,
            ),
            headers={"User-Agent": "lumen-image"},
        )

    async def shutdown(self) -> None:
        client = self.client
        self.client = None
        if client is not None:
            await client.aclose()

    async def call(
        self,
        row: Any,
        *,
        authorization: str,
    ) -> tuple[int, list[dict[str, Any]]]:
        return await self.processing.call_upstream(
            row,
            authorization=authorization,
        )

    def is_retryable_failure(self, failure: JobFailure) -> bool:
        return self.processing.upstream_facade.is_retryable_job_failure(failure)
