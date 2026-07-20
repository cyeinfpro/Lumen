"""Image candidate decoding, secure downloads, and upstream response parsing."""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urljoin

import httpx


_IMAGE_SIGNATURES: tuple[bytes, ...] = (
    b"\xff\xd8\xff",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"RIFF",
    b"BM",
    b"\x00\x00\x00\x0cftypheic",
    b"\x00\x00\x00\x18ftypheic",
)
_IMAGE_DOWNLOAD_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_IMAGE_DOWNLOAD_ERROR_BODY_MAX_BYTES = 64 * 1024
_RESPONSES_PARTIAL_TYPE_HINT = ".partial_image"
_RESPONSES_SUCCESS_TERMINAL_EVENTS = frozenset({"response.completed", "response.done"})
_RESPONSES_ERROR_TERMINAL_EVENTS = frozenset(
    {"response.failed", "response.incomplete", "error"}
)


@dataclass
class ImageCandidate:
    data: bytes
    mime_type: str | None = None


@dataclass
class _ResponsesStreamState:
    cache: dict[str, Any]
    budget: Any
    event_lines: list[str]
    line_decoder: Any
    events_seen: int = 0
    bytes_seen: int = 0
    partial_candidates: list[Any] = field(default_factory=list)
    final_candidates: list[Any] = field(default_factory=list)
    saw_done: bool = False
    saw_success_terminal: bool = False
    last_touch: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class ImageCandidateFacade:
    max_image_bytes: Callable[[], int]
    max_total_image_bytes: Callable[[], int]
    max_image_url_redirects: Callable[[], int]
    responses_stream_idle_timeout_s: Callable[[], float]
    responses_stream_max_bytes: Callable[[], int]
    job_heartbeat_interval_s: Callable[[], float]
    error_class_network: Callable[[], str]
    error_class_upstream_4xx: Callable[[], str]
    error_class_upstream_5xx: Callable[[], str]
    error_class_image_save: Callable[[], str]
    error_class_validation: Callable[[], str]
    job_failure: Callable[..., Any]
    job_failure_type: type[BaseException]
    image_candidate: Callable[[bytes, str | None], Any]
    budget_factory: Callable[[], Any]
    parse_json_bytes: Callable[[bytes], Any | None]
    body_preview: Callable[[bytes], Any]
    download_content_length: Callable[[Any], int | None]
    read_download_body_bounded: Callable[..., Awaitable[tuple[bytes, bool, int]]]
    new_pinned_image_download_client: Callable[[Any], Any]
    resolve_public_image_download_target: Callable[[str], Awaitable[Any]]
    image_download_resolution_error: type[BaseException]
    touch_running: Callable[[str], Awaitable[None]]
    download_image_url_fn: Callable[..., Awaitable[Any | None]]
    extract_candidates_fn: Callable[..., Awaitable[list[Any]]]
    sse_line_decoder_factory: Callable[[], Any]

    def looks_like_image(self, data: bytes) -> bool:
        if len(data) < 8:
            return False
        if any(data.startswith(signature) for signature in _IMAGE_SIGNATURES):
            return True
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return True
        return b"ftyp" in data[:32]

    def candidate_size_error(self, max_bytes: int) -> Any:
        if max_bytes < self.max_image_bytes():
            return self.job_failure(
                f"上游图片总字节超过限制（max {self.max_total_image_bytes()}）",
                error_class=self.error_class_image_save(),
            )
        return self.job_failure(
            f"上游单图超过大小限制（max {self.max_image_bytes()}）",
            error_class=self.error_class_image_save(),
        )

    def b64_decode(
        self,
        value: str,
        *,
        max_bytes: int | None = None,
    ) -> bytes | None:
        compact = "".join(value.split())
        if not compact:
            return None
        pad = len(compact) % 4
        if pad:
            compact += "=" * (4 - pad)
        padding = len(compact) - len(compact.rstrip("="))
        decoded_size = len(compact) // 4 * 3 - min(padding, 2)
        if max_bytes is not None and decoded_size > max_bytes:
            raise self.candidate_size_error(max_bytes)
        try:
            return base64.b64decode(compact, validate=True)
        except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
            return None

    def decode_data_url(
        self,
        value: str,
        *,
        max_bytes: int | None = None,
    ) -> Any | None:
        if not value.startswith("data:image/") or "," not in value:
            return None
        effective_max = self.max_image_bytes() if max_bytes is None else max_bytes
        header, encoded = value.split(",", 1)
        mime_type = header.removeprefix("data:").split(";", 1)[0]
        if ";base64" in header:
            data = self.b64_decode(encoded, max_bytes=effective_max)
            if data is None:
                return None
        else:
            data = encoded.encode("utf-8", "replace")
        if len(data) > effective_max:
            raise self.candidate_size_error(effective_max)
        if not self.looks_like_image(data):
            return None
        return self.image_candidate(data, mime_type)

    def decode_base64(
        self,
        value: str,
        *,
        max_bytes: int | None = None,
    ) -> bytes | None:
        value = value.strip()
        if not value:
            return None
        effective_max = self.max_image_bytes() if max_bytes is None else max_bytes
        if value.startswith("data:image/"):
            candidate = self.decode_data_url(value, max_bytes=effective_max)
            return candidate.data if candidate else None
        data = self.b64_decode(value, max_bytes=effective_max)
        if data is None:
            return None
        if len(data) > effective_max:
            raise self.candidate_size_error(effective_max)
        if not self.looks_like_image(data):
            return None
        return data

    @staticmethod
    def object_image_context(value: dict[str, Any]) -> bool:
        type_value = str(value.get("type", "")).lower()
        mime_value = str(value.get("mimeType") or value.get("mime_type") or "").lower()
        if "image" in type_value or mime_value.startswith("image/"):
            return True
        keys = {str(key) for key in value}
        return bool(
            {"b64_json", "inlineData", "inline_data", "partial_image_b64"} & keys
        )

    @staticmethod
    def is_responses_partial_event(event: Any) -> bool:
        if not isinstance(event, dict):
            return False
        return _RESPONSES_PARTIAL_TYPE_HINT in str(event.get("type", ""))

    @staticmethod
    def is_responses_success_terminal(event: Any) -> bool:
        if not isinstance(event, dict):
            return False
        return str(event.get("type", "")) in _RESPONSES_SUCCESS_TERMINAL_EVENTS

    @staticmethod
    def is_responses_error_terminal(event: Any) -> bool:
        if not isinstance(event, dict):
            return False
        return str(event.get("type", "")) in _RESPONSES_ERROR_TERMINAL_EVENTS

    async def _resolve_download_target(
        self,
        current_url: str,
        *,
        redirects: int,
        retry_requires_idempotency: bool,
    ) -> Any:
        try:
            return await self.resolve_public_image_download_target(current_url)
        except self.image_download_resolution_error as exc:
            raise self.job_failure(
                f"下载上游图片失败: {exc}",
                retryable=True,
                retry_requires_idempotency=retry_requires_idempotency,
                outcome_uncertain=retry_requires_idempotency,
                error_class=self.error_class_network(),
            ) from exc
        except ValueError as exc:
            prefix = "图片重定向目标不允许下载" if redirects else "图片 URL 不允许下载"
            raise self.job_failure(
                f"{prefix}: {exc}",
                upstream_status=400,
                error_class=self.error_class_validation(),
            ) from exc

    async def _raise_download_http_error(
        self,
        response: Any,
        *,
        retry_requires_idempotency: bool,
    ) -> None:
        error_limit = min(
            self.max_image_bytes(),
            _IMAGE_DOWNLOAD_ERROR_BODY_MAX_BYTES,
        )
        declared_size = self.download_content_length(response.headers)
        if declared_size is not None and declared_size > error_limit:
            content = b""
            truncated = True
        else:
            content, truncated, _received = await self.read_download_body_bounded(
                response,
                max_bytes=error_limit,
                truncate=True,
            )
        upstream_body: Any = self.body_preview(content)
        if truncated:
            upstream_body = {"preview": upstream_body, "truncated": True}
        is_5xx = response.status_code >= 500
        raise self.job_failure(
            f"下载上游图片失败 HTTP {response.status_code}",
            upstream_status=response.status_code,
            upstream_body=upstream_body,
            retryable=is_5xx,
            retry_requires_idempotency=retry_requires_idempotency,
            outcome_uncertain=is_5xx and retry_requires_idempotency,
            error_class=(
                self.error_class_upstream_5xx()
                if is_5xx
                else self.error_class_upstream_4xx()
            ),
        )

    async def _read_download_image(
        self,
        response: Any,
        *,
        max_bytes: int,
    ) -> tuple[bytes, str | None]:
        declared_size = self.download_content_length(response.headers)
        if declared_size is not None and declared_size > max_bytes:
            raise self.job_failure(
                "上游图片超过大小限制（Content-Length 预检）",
                upstream_status=response.status_code,
                error_class=self.error_class_image_save(),
            )
        content, truncated, _received = await self.read_download_body_bounded(
            response,
            max_bytes=max_bytes,
            truncate=False,
        )
        if truncated:
            failure = self.candidate_size_error(max_bytes)
            failure.upstream_status = response.status_code
            raise failure
        return content, response.headers.get("content-type")

    async def download_image_url(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        cache: dict[str, Any],
        max_bytes: int | None = None,
        retry_requires_idempotency: bool = True,
    ) -> Any | None:
        effective_max = self.max_image_bytes() if max_bytes is None else max_bytes
        candidate_url = url.strip()
        if candidate_url.startswith("data:image/"):
            return self.decode_data_url(candidate_url, max_bytes=effective_max)
        if not candidate_url.lower().startswith(("http://", "https://")):
            return None
        cached = cache.get(url)
        if cached is not None:
            return cached

        _ = client
        current_url = candidate_url
        content = b""
        content_type: str | None = None
        try:
            redirects = 0
            while True:
                target = await self._resolve_download_target(
                    current_url,
                    redirects=redirects,
                    retry_requires_idempotency=retry_requires_idempotency,
                )
                download_client = self.new_pinned_image_download_client(target)
                async with download_client:
                    async with download_client.stream(
                        "GET",
                        target.url,
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in _IMAGE_DOWNLOAD_REDIRECT_STATUSES:
                            location = (response.headers.get("location") or "").strip()
                            if not location:
                                raise self.job_failure(
                                    "上游图片重定向缺少 Location",
                                    upstream_status=response.status_code,
                                    error_class=self.error_class_upstream_4xx(),
                                )
                            if redirects >= self.max_image_url_redirects():
                                raise self.job_failure(
                                    "上游图片重定向次数过多",
                                    upstream_status=response.status_code,
                                    error_class=self.error_class_upstream_4xx(),
                                )
                            redirects += 1
                            current_url = urljoin(target.url, location)
                            continue
                        if not 200 <= response.status_code < 300:
                            await self._raise_download_http_error(
                                response,
                                retry_requires_idempotency=(retry_requires_idempotency),
                            )
                        content, content_type = await self._read_download_image(
                            response,
                            max_bytes=effective_max,
                        )
                        break
        except self.job_failure_type:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise self.job_failure(
                f"下载上游图片失败: {exc.__class__.__name__}: {exc}",
                retryable=True,
                retry_requires_idempotency=retry_requires_idempotency,
                outcome_uncertain=retry_requires_idempotency,
                error_class=self.error_class_network(),
            ) from exc

        candidate = self.image_candidate(content, content_type)
        cache[url] = candidate
        cache[current_url] = candidate
        return candidate

    async def _extract_list(
        self,
        values: list[Any],
        client: httpx.AsyncClient,
        *,
        image_context: bool,
        cache: dict[str, Any],
        budget: Any,
    ) -> list[Any]:
        candidates: list[Any] = []
        for item in values:
            candidates.extend(
                await self.extract_candidates_fn(
                    item,
                    client,
                    image_context=image_context,
                    cache=cache,
                    budget=budget,
                )
            )
        return candidates

    def _extract_inline_candidates(
        self,
        value: dict[str, Any],
        budget: Any,
    ) -> list[Any]:
        candidates: list[Any] = []
        for inline_key in ("inlineData", "inline_data"):
            inline = value.get(inline_key)
            if not isinstance(inline, dict) or not isinstance(inline.get("data"), str):
                continue
            data = self.decode_base64(
                inline["data"],
                max_bytes=budget.next_max_bytes(),
            )
            if data is not None:
                candidates.append(
                    budget.record(
                        self.image_candidate(
                            data,
                            inline.get("mimeType") or inline.get("mime_type"),
                        )
                    )
                )
        return candidates

    async def _extract_string_candidate(
        self,
        *,
        key: str,
        item: str,
        value: dict[str, Any],
        context: bool,
        client: httpx.AsyncClient,
        cache: dict[str, Any],
        budget: Any,
    ) -> Any | None:
        base64_keys = {
            "b64_json",
            "image_b64",
            "image_base64",
            "base64_image",
            "partial_image_b64",
        }
        if key in base64_keys or (key in {"result", "data"} and context):
            data = self.decode_base64(item, max_bytes=budget.next_max_bytes())
            if data is None:
                return None
            return budget.record(
                self.image_candidate(
                    data,
                    value.get("mimeType") or value.get("mime_type"),
                )
            )
        if key not in {"url", "image_url"}:
            return None
        downloaded = await self.download_image_url_fn(
            client,
            item,
            cache=cache,
            max_bytes=budget.next_max_bytes(),
        )
        return budget.record(downloaded) if downloaded is not None else None

    async def extract_candidates(
        self,
        value: Any,
        client: httpx.AsyncClient,
        *,
        image_context: bool = False,
        cache: dict[str, Any] | None = None,
        budget: Any | None = None,
    ) -> list[Any]:
        cache = {} if cache is None else cache
        budget = self.budget_factory() if budget is None else budget
        if isinstance(value, list):
            return await self._extract_list(
                value,
                client,
                image_context=image_context,
                cache=cache,
                budget=budget,
            )
        if not isinstance(value, dict):
            return []

        context = image_context or self.object_image_context(value)
        candidates = self._extract_inline_candidates(value, budget)
        for key, item in value.items():
            if key in {"inlineData", "inline_data"}:
                continue
            if isinstance(item, str):
                candidate = await self._extract_string_candidate(
                    key=key,
                    item=item,
                    value=value,
                    context=context,
                    client=client,
                    cache=cache,
                    budget=budget,
                )
                if candidate is not None:
                    candidates.append(candidate)
            elif isinstance(item, (dict, list)):
                candidates.extend(
                    await self.extract_candidates_fn(
                        item,
                        client,
                        image_context=context,
                        cache=cache,
                        budget=budget,
                    )
                )
        return candidates

    def parse_sse_json_objects(self, text: str) -> list[Any]:
        objects: list[Any] = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if not data or data == "[DONE]":
                continue
            parsed = self.parse_json_bytes(data.encode("utf-8"))
            if parsed is not None:
                objects.append(parsed)
        return objects

    def try_parse_sse_data(self, data: str) -> Any | None:
        data = data.strip()
        if not data or data == "[DONE]":
            return None
        return self.parse_json_bytes(data.encode("utf-8"))

    @staticmethod
    def sse_data_from_lines(lines: list[str]) -> str | None:
        parts: list[str] = []
        for raw in lines:
            if not raw.startswith("data:"):
                continue
            data = raw[5:]
            parts.append(data[1:] if data.startswith(" ") else data)
        return "\n".join(parts) if parts else None

    @staticmethod
    def contains_result_key(value: Any) -> bool:
        stack = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                if "result" in current:
                    return True
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
        return False

    @staticmethod
    def first_stream_error(events: Iterable[Any]) -> dict[str, Any] | None:
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            if event_type == "error" and isinstance(event.get("error"), dict):
                return event["error"]
            if event_type == "response.failed":
                response = event.get("response")
                if isinstance(response, dict) and isinstance(
                    response.get("error"),
                    dict,
                ):
                    return response["error"]
                return {
                    "type": "response_failed",
                    "code": "response_failed",
                    "message": "Responses stream ended with response.failed",
                }
            if event_type == "response.incomplete":
                response = event.get("response")
                if isinstance(response, dict):
                    detail = response.get("incomplete_details") or response.get("error")
                    if isinstance(detail, dict):
                        output = dict(detail)
                        output.setdefault("type", "response_incomplete")
                        output.setdefault("code", "response_incomplete")
                        return output
                return {
                    "type": "response_incomplete",
                    "code": "response_incomplete",
                    "message": "Responses stream ended with response.incomplete",
                }
        return None

    def classify_stream_error(self, error: dict[str, Any]) -> str:
        code = str(error.get("code") or "").lower()
        error_type = str(error.get("type") or "").lower()
        message = str(error.get("message") or "").lower()
        joined = " ".join((code, error_type, message))
        if (
            "moderation" in joined
            or "safety" in joined
            or error_type.endswith("_user_error")
        ):
            return self.error_class_validation()
        if "invalid" in joined or "bad_request" in joined or "bad request" in joined:
            return self.error_class_upstream_4xx()
        return self.error_class_upstream_5xx()

    @staticmethod
    def stream_error_message(error: dict[str, Any]) -> str:
        code = str(error.get("code") or error.get("type") or "stream_error")
        message = str(
            error.get("message") or "Responses stream failed before returning an image"
        )
        return f"上游流式错误 {code}: {message}"

    async def extract_response_images(
        self,
        response: httpx.Response,
        client: httpx.AsyncClient,
        *,
        budget: Any | None = None,
    ) -> list[Any]:
        budget = self.budget_factory() if budget is None else budget
        content_type = (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        if content_type.startswith("image/"):
            budget.next_max_bytes()
            return [budget.record(self.image_candidate(response.content, content_type))]

        parsed = self.parse_json_bytes(response.content)
        if parsed is not None:
            stream_error = self.first_stream_error([parsed])
            if stream_error is not None:
                raise self.job_failure(
                    self.stream_error_message(stream_error),
                    upstream_status=response.status_code,
                    upstream_body=self.body_preview(response.content),
                    error_class=self.classify_stream_error(stream_error),
                )
            return await self.extract_candidates_fn(parsed, client, budget=budget)

        events = self.parse_sse_json_objects(
            response.content.decode("utf-8", "replace")
        )
        stream_error = self.first_stream_error(events)
        if stream_error is not None:
            raise self.job_failure(
                self.stream_error_message(stream_error),
                upstream_status=response.status_code,
                upstream_body=self.body_preview(response.content),
                error_class=self.classify_stream_error(stream_error),
            )
        has_terminal = any(
            isinstance(event, dict)
            and not self.is_responses_partial_event(event)
            and self.contains_result_key(event)
            for event in events
        )
        cache: dict[str, Any] = {}
        candidates: list[Any] = []
        for event in events:
            if has_terminal and self.is_responses_partial_event(event):
                continue
            candidates.extend(
                await self.extract_candidates_fn(
                    event,
                    client,
                    cache=cache,
                    budget=budget,
                )
            )
        return candidates

    async def _handle_responses_stream_event(
        self,
        response: httpx.Response,
        client: httpx.AsyncClient,
        state: _ResponsesStreamState,
        event: Any,
    ) -> None:
        stream_error = self.first_stream_error([event])
        if stream_error is not None:
            raise self.job_failure(
                self.stream_error_message(stream_error),
                upstream_status=response.status_code,
                upstream_body=stream_error,
                error_class=self.classify_stream_error(stream_error),
            )
        if self.is_responses_success_terminal(event):
            state.saw_success_terminal = True
        extracted = await self.extract_candidates_fn(
            event,
            client,
            cache=state.cache,
            budget=state.budget,
        )
        target = (
            state.partial_candidates
            if self.is_responses_partial_event(event)
            else state.final_candidates
        )
        target.extend(extracted)

    async def _handle_responses_stream_line(
        self,
        response: httpx.Response,
        client: httpx.AsyncClient,
        state: _ResponsesStreamState,
        line: str,
    ) -> None:
        if line:
            state.event_lines.append(line)
            return
        data = self.sse_data_from_lines(state.event_lines)
        state.event_lines.clear()
        event = self.try_parse_sse_data(data or "")
        if event is None:
            if data and data.strip() == "[DONE]":
                state.saw_done = True
            return
        state.events_seen += 1
        await self._handle_responses_stream_event(
            response,
            client,
            state,
            event,
        )

    def _responses_stream_detail(
        self,
        state: _ResponsesStreamState,
    ) -> dict[str, Any]:
        return {
            "events_seen": state.events_seen,
            "partial_images_seen": len(state.partial_candidates),
            "saw_done": state.saw_done,
            "saw_success_terminal": state.saw_success_terminal,
            "bytes_seen": state.bytes_seen,
        }

    async def _next_responses_stream_chunk(
        self,
        response: httpx.Response,
        state: _ResponsesStreamState,
        byte_iter: Any,
    ) -> bytes | None:
        try:
            return await asyncio.wait_for(
                byte_iter.__anext__(),
                timeout=self.responses_stream_idle_timeout_s(),
            )
        except StopAsyncIteration:
            return None
        except asyncio.TimeoutError:
            raise self.job_failure(
                "Responses stream idle for "
                f"{self.responses_stream_idle_timeout_s():.0f}s",
                upstream_status=response.status_code,
                upstream_body=self._responses_stream_detail(state),
                retryable=True,
                retry_requires_idempotency=True,
                outcome_uncertain=True,
                error_class=self.error_class_network(),
            ) from None

    async def _accept_responses_stream_chunk(
        self,
        response: httpx.Response,
        state: _ResponsesStreamState,
        chunk: bytes,
        *,
        job_id: str,
    ) -> None:
        next_bytes_seen = state.bytes_seen + len(chunk)
        if next_bytes_seen > self.responses_stream_max_bytes():
            raise self.job_failure(
                "Responses stream exceeded sidecar byte budget before final image",
                upstream_status=response.status_code,
                retryable=True,
                retry_requires_idempotency=True,
                outcome_uncertain=True,
                error_class=self.error_class_network(),
            )
        state.bytes_seen = next_bytes_seen
        now = time.monotonic()
        if now - state.last_touch >= self.job_heartbeat_interval_s():
            await self.touch_running(job_id)
            state.last_touch = now

    async def _consume_responses_stream(
        self,
        response: httpx.Response,
        client: httpx.AsyncClient,
        state: _ResponsesStreamState,
        *,
        job_id: str,
    ) -> None:
        byte_iter = response.aiter_bytes()
        while True:
            chunk = await self._next_responses_stream_chunk(
                response,
                state,
                byte_iter,
            )
            if chunk is None:
                break
            if not chunk:
                continue
            await self._accept_responses_stream_chunk(
                response,
                state,
                chunk,
                job_id=job_id,
            )
            for line in state.line_decoder.feed(chunk):
                await self._handle_responses_stream_line(
                    response,
                    client,
                    state,
                    line,
                )

    async def _finish_responses_stream(
        self,
        response: httpx.Response,
        client: httpx.AsyncClient,
        state: _ResponsesStreamState,
    ) -> None:
        for line in state.line_decoder.finish():
            await self._handle_responses_stream_line(
                response,
                client,
                state,
                line,
            )
        if state.event_lines:
            await self._handle_responses_stream_line(
                response,
                client,
                state,
                "",
            )

    async def extract_responses_stream_images(
        self,
        response: httpx.Response,
        client: httpx.AsyncClient,
        *,
        job_id: str,
    ) -> list[Any]:
        state = _ResponsesStreamState(
            cache={},
            budget=self.budget_factory(),
            event_lines=[],
            line_decoder=self.sse_line_decoder_factory(),
        )
        await self._consume_responses_stream(
            response,
            client,
            state,
            job_id=job_id,
        )
        await self._finish_responses_stream(response, client, state)
        if state.final_candidates:
            return state.final_candidates
        error = (
            "Responses stream ended after partial images but before final image"
            if state.partial_candidates
            else "Responses stream ended before returning an image"
        )
        raise self.job_failure(
            error,
            upstream_status=response.status_code,
            upstream_body=self._responses_stream_detail(state),
            retryable=True,
            retry_requires_idempotency=True,
            outcome_uncertain=True,
            error_class=self.error_class_network(),
        )


class _SseLineDecoder:
    """Incrementally decode SSE lines from bounded byte chunks."""

    def __init__(self) -> None:
        self._line = bytearray()
        self._pending_cr = False

    def _finish_line(self) -> str:
        line = bytes(self._line).decode("utf-8", "replace")
        self._line.clear()
        return line

    def feed(self, chunk: bytes) -> list[str]:
        lines: list[str] = []
        for value in chunk:
            if self._pending_cr:
                if value == 0x0A:
                    lines.append(self._finish_line())
                    self._pending_cr = False
                    continue
                lines.append(self._finish_line())
                self._pending_cr = False
            if value == 0x0D:
                self._pending_cr = True
            elif value == 0x0A:
                lines.append(self._finish_line())
            else:
                self._line.append(value)
        return lines

    def finish(self) -> list[str]:
        lines: list[str] = []
        if self._pending_cr:
            lines.append(self._finish_line())
            self._pending_cr = False
        if self._line:
            lines.append(self._finish_line())
        return lines
