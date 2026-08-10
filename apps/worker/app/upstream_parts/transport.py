"""Curl multipart and SSE transports used by the ``app.upstream`` facade."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
    service_name,
)
from .progress_events import (
    ImageProgressCallback as ImageProgressCallback,
    emit_image_progress as _emit_image_progress,
    maybe_record_usage_from_event as _maybe_record_usage_from_event,
)
from .curl_files import (
    secure_mkstemp as _secure_mkstemp,
    stage_curl_secret_config as _stage_curl_secret_config,
    write_bytes_file as _write_bytes_file,
    write_json_body_file as _write_json_body_file,
)
from .sse_transport import CurlSSEEventParser, CurlSSEProcess, decode_sse_event

DispatchReadyHook = Callable[[], Awaitable[None]]
ResponseReadyHook = Callable[[], Awaitable[None]]
ResponseHeadHook = Callable[[int, dict[str, str]], Awaitable[None]]

_CURL_STDERR_MAX_BYTES = 64 * 1024
_DEFAULT_JSON_RESPONSE_MAX_BYTES = 32 * 1024 * 1024
_DEFAULT_ERROR_RESPONSE_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class CurlSSEResponseContext:
    endpoint_label: str = "responses"
    error_path: str = "responses"


class _CurlOutputTooLarge(Exception):
    def __init__(self, *, label: str, max_bytes: int, received_bytes: int) -> None:
        super().__init__(f"{label} exceeded {max_bytes} bytes")
        self.label = label
        self.max_bytes = max_bytes
        self.received_bytes = received_bytes


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def _curl_timeout_arg(timeout_s: float) -> str:
    timeout = math.ceil(timeout_s) if math.isfinite(timeout_s) else 1
    return str(max(1, timeout))


def _configured_limit(
    name: str,
    default: int,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> int:
    services = _runtime_services(runtime)
    try:
        value = int(getattr(services.core, service_name(name)))
    except (AttributeError, TypeError, ValueError):
        return default
    return max(0, value)


def _json_response_limit(
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> int:
    return _configured_limit(
        "_NON_SSE_JSON_MAX_BYTES",
        _DEFAULT_JSON_RESPONSE_MAX_BYTES,
        runtime=runtime,
    )


def _error_response_limit(
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> int:
    return min(
        _json_response_limit(runtime=runtime),
        _DEFAULT_ERROR_RESPONSE_MAX_BYTES,
    )


async def _read_stream_limited(
    stream: Any,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    body = bytearray()
    received_bytes = 0
    while True:
        remaining = max_bytes - len(body)
        chunk = await stream.read(max(1, min(65536, remaining + 1)))
        if not chunk:
            return bytes(body)
        received_bytes += len(chunk)
        if received_bytes > max_bytes:
            raise _CurlOutputTooLarge(
                label=label,
                max_bytes=max_bytes,
                received_bytes=received_bytes,
            )
        body.extend(chunk)


async def _collect_curl_output(
    proc: asyncio.subprocess.Process,
    *,
    stdout_max_bytes: int,
    stderr_max_bytes: int = _CURL_STDERR_MAX_BYTES,
) -> tuple[bytes, bytes]:
    stdout = getattr(proc, "stdout", None)
    stderr = getattr(proc, "stderr", None)
    if stdout is None or stderr is None:
        communicate = getattr(proc, "communicate", None)
        if not callable(communicate):
            raise RuntimeError("curl process pipes are unavailable")
        stdout_b, stderr_b = await communicate()
        if len(stdout_b) > stdout_max_bytes:
            raise _CurlOutputTooLarge(
                label="curl stdout",
                max_bytes=stdout_max_bytes,
                received_bytes=len(stdout_b),
            )
        if len(stderr_b) > stderr_max_bytes:
            raise _CurlOutputTooLarge(
                label="curl stderr",
                max_bytes=stderr_max_bytes,
                received_bytes=len(stderr_b),
            )
        return stdout_b, stderr_b

    stdout_task = asyncio.create_task(
        _read_stream_limited(
            stdout,
            max_bytes=stdout_max_bytes,
            label="curl stdout",
        )
    )
    stderr_task = asyncio.create_task(
        _read_stream_limited(
            stderr,
            max_bytes=stderr_max_bytes,
            label="curl stderr",
        )
    )
    try:
        stdout_b, stderr_b = await asyncio.gather(stdout_task, stderr_task)
        wait = getattr(proc, "wait", None)
        if callable(wait):
            await wait()
        return stdout_b, stderr_b
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)


async def _terminate_curl_proc_group(
    proc: asyncio.subprocess.Process | None,
) -> None:
    """SIGTERM the curl process group, then SIGKILL after a short grace.

    ``start_new_session=True`` makes curl the process-group leader. Killing the
    group also reaches DNS, TLS, and proxy helpers that may otherwise retain
    sockets or file descriptors after the parent task is cancelled.
    """
    if proc is None or proc.returncode is not None:
        return
    pgid: int | None = None
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None
    try:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                with suppress(Exception):
                    proc.terminate()
        else:
            with suppress(Exception):
                proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except Exception:  # noqa: BLE001
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                with suppress(Exception):
                    proc.kill()
        else:
            with suppress(Exception):
                proc.kill()
        with suppress(Exception):
            await proc.wait()


async def _stage_multipart_bytes_to_tmp(
    files: list[tuple[str, tuple[str, bytes, str]]],
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """Stage multipart byte payloads once so retries reuse the same files."""
    services = _runtime_services(runtime)
    staged: list[tuple[str, str, str, str]] = []
    tmpfiles: list[str] = []
    try:
        for field_name, (filename, raw, mime) in files:
            fd, tmp_path = _secure_mkstemp(
                prefix="lumen_curl_",
                suffix=".bin",
            )
            tmpfiles.append(tmp_path)
            try:
                await asyncio.to_thread(services.transport.write_bytes_file, fd, raw)
            finally:
                os.close(fd)
            staged.append((field_name, tmp_path, filename, mime))
        return staged, tmpfiles
    except BaseException:
        for tmp_path in tmpfiles:
            with suppress(Exception):
                os.unlink(tmp_path)
        raise


def _raise_curl_multipart_process_error(
    returncode: int | None,
    stderr_b: bytes,
) -> None:
    if returncode == 0:
        return
    stderr = stderr_b.decode("utf-8", "replace")[:500]
    if returncode == 28:
        raise httpx.TimeoutException(f"curl multipart timeout rc=28 stderr={stderr}")
    if returncode in {6, 7}:
        raise httpx.ConnectError(
            f"curl failed before request delivery rc={returncode} stderr={stderr}"
        )
    raise httpx.HTTPError(f"curl failed rc={returncode} stderr={stderr}")


async def _curl_post_multipart_using_paths(
    *,
    url: str,
    data: dict[str, str],
    staged_files: list[tuple[str, str, str, str]],
    headers: dict[str, str],
    timeout_s: float,
    proxy_url: str | None = None,
    pinned_target: Any | None = None,
    on_dispatch_ready: DispatchReadyHook | None = None,
    on_response_ready: ResponseReadyHook | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Curl multipart POST against caller-owned, pre-staged file paths."""
    services = _runtime_services(runtime)
    proc: asyncio.subprocess.Process | None = None
    config_path: str | None = None
    try:
        form_args: list[str] = []
        for key, value in data.items():
            form_args += ["--form-string", f"{key}={value}"]
        for field_name, tmp_path, filename, mime in staged_files:
            form_args += [
                "--form",
                f"{field_name}=@{tmp_path};filename={filename};type={mime}",
            ]
        config_path = await _stage_curl_secret_config(
            url=url,
            headers=headers,
            proxy_url=proxy_url,
            pinned_target=pinned_target,
            runtime=runtime,
        )
        status_marker = "\n__HTTP_STATUS__:"
        status_marker_b = status_marker.encode("ascii")
        cmd = [
            services.core.CURL_BIN,
            "-sS",
            "-m",
            services.transport.curl_timeout_arg(timeout_s),
            "-w",
            f"{status_marker}%{{http_code}}",
            "--config",
            config_path,
            *form_args,
            url,
        ]
        try:
            if on_dispatch_ready is not None:
                await on_dispatch_ready()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise httpx.ConnectError(
                f"curl executable failed to start: {services.core.CURL_BIN!r}: {exc}"
            ) from exc
        curl_timeout_s = float(services.transport.curl_timeout_arg(timeout_s))
        guard_timeout_s = curl_timeout_s + min(
            5.0,
            max(0.25, curl_timeout_s * 0.1),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                _collect_curl_output(
                    proc,
                    stdout_max_bytes=(
                        _json_response_limit(runtime=runtime)
                        + len(status_marker_b)
                        + 16
                    ),
                ),
                timeout=guard_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise httpx.TimeoutException(
                f"curl multipart timed out after {guard_timeout_s:.2f}s"
            ) from exc
        except _CurlOutputTooLarge as exc:
            raise httpx.HTTPError(
                "curl multipart response exceeded its byte limit "
                f"label={exc.label} max_bytes={exc.max_bytes} "
                f"received_bytes={exc.received_bytes}"
            ) from exc
        _raise_curl_multipart_process_error(proc.returncode, stderr_b)
        if status_marker_b not in stdout_b:
            raise httpx.HTTPError(
                f"curl output missing status marker (head={stdout_b[:200]!r})"
            )
        body_b, _, status_b = stdout_b.rpartition(status_marker_b)
        if len(body_b) > _json_response_limit(runtime=runtime):
            raise httpx.HTTPError(
                "curl multipart response exceeded its byte limit "
                f"max_bytes={_json_response_limit(runtime=runtime)} "
                f"received_bytes={len(body_b)}"
            )
        body_s = body_b.decode("utf-8", "replace")
        try:
            payload = json.loads(body_s)
        except Exception:  # noqa: BLE001
            payload = {"raw": body_s[:2000]}
        status = int(status_b.strip())
        if 200 <= status < 300 and on_response_ready is not None:
            await on_response_ready()
        return status, payload
    except asyncio.CancelledError:
        raise
    finally:
        await services.transport.terminate_curl_proc_group(proc)
        if config_path is not None:
            with suppress(OSError):
                os.unlink(config_path)


async def _curl_post_multipart(
    *,
    url: str,
    data: dict[str, str],
    files: list[tuple[str, tuple[str, bytes, str]]],
    headers: dict[str, str],
    timeout_s: float,
    proxy_url: str | None = None,
    pinned_target: Any | None = None,
    on_dispatch_ready: DispatchReadyHook | None = None,
    on_response_ready: ResponseReadyHook | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Stage multipart bytes, send them with curl, and always unlink them."""
    staged: list[tuple[str, str, str, str]] = []
    tmpfiles: list[str] = []
    try:
        (
            staged,
            tmpfiles,
        ) = await _stage_multipart_bytes_to_tmp(files, runtime=runtime)
        return await _curl_post_multipart_using_paths(
            url=url,
            data=data,
            staged_files=staged,
            headers=headers,
            timeout_s=timeout_s,
            proxy_url=proxy_url,
            pinned_target=pinned_target,
            on_dispatch_ready=on_dispatch_ready,
            on_response_ready=on_response_ready,
            runtime=runtime,
        )
    finally:
        for path in tmpfiles:
            try:
                os.unlink(path)
            except Exception:  # noqa: BLE001
                pass


class _CurlSSEReader:
    def __init__(
        self,
        stream: Any,
        *,
        idle_timeout_s: float,
        services: UpstreamServices,
    ) -> None:
        self._stream = stream
        self._services = services
        self._idle_timeout_s = max(0.001, float(idle_timeout_s))
        self._buffer = bytearray()
        self._search_from = 0
        self._stream_eof = False
        self._byte_count = 0
        self._line_count = 0
        self.response_status_code = 0

    async def next_line(self) -> bytes | None:
        while True:
            index = self._buffer.find(b"\n", self._search_from)
            if index >= 0:
                line = bytes(self._buffer[: index + 1])
                del self._buffer[: index + 1]
                self._search_from = 0
                self._record_line(line)
                return line
            self._search_from = len(self._buffer)
            if self._stream_eof:
                if not self._buffer:
                    return None
                line = bytes(self._buffer)
                self._buffer.clear()
                self._search_from = 0
                self._record_line(line)
                return line
            try:
                chunk = await asyncio.wait_for(
                    self._stream.read(65536),
                    timeout=self._idle_timeout_s,
                )
            except asyncio.TimeoutError as exc:
                raise self._services.infrastructure.UpstreamError(
                    f"curl sse idle timeout after {self._idle_timeout_s:.0f}s",
                    error_code=self._services.infrastructure.EC.SSE_CURL_FAILED.value,
                    status_code=None,
                ) from exc
            if not chunk:
                self._stream_eof = True
                continue
            self._byte_count += len(chunk)
            if self._byte_count > self._services.core.SSE_MAX_BYTES:
                raise self._services.infrastructure.UpstreamError(
                    "sse exceeded max bytes",
                    error_code=self._services.infrastructure.EC.STREAM_TOO_LARGE.value,
                    status_code=200,
                )
            self._buffer.extend(chunk)
            if (
                len(self._buffer) > self._services.core.SSE_MAX_LINE_BYTES
                and b"\n" not in self._buffer
            ):
                raise self._services.infrastructure.UpstreamError(
                    "sse exceeded max line bytes",
                    error_code=self._services.infrastructure.EC.STREAM_TOO_LARGE.value,
                    status_code=200,
                )

    def _record_line(self, line: bytes) -> None:
        self._line_count += 1
        if len(line) > self._services.core.SSE_MAX_LINE_BYTES:
            raise self._services.infrastructure.UpstreamError(
                "sse exceeded max line bytes",
                error_code=self._services.infrastructure.EC.STREAM_TOO_LARGE.value,
                status_code=200,
            )
        if self._line_count > self._services.core.SSE_MAX_LINES:
            raise self._services.infrastructure.UpstreamError(
                "sse exceeded max lines",
                error_code=self._services.infrastructure.EC.STREAM_TOO_LARGE.value,
                status_code=200,
            )

    async def drain(
        self,
        *,
        max_bytes: int,
        label: str,
        status_code: int,
        url: str,
        trace_id: str,
        path: str = "responses",
    ) -> bytes:
        body = bytearray()
        if self._buffer:
            body.extend(self._buffer)
            self._buffer.clear()
            self._search_from = 0
            self._raise_if_payload_too_large(
                body,
                max_bytes=max_bytes,
                label=label,
                status_code=status_code,
                url=url,
                trace_id=trace_id,
                path=path,
            )
        while True:
            line = await self.next_line()
            if line is None:
                return bytes(body)
            body.extend(line)
            self._raise_if_payload_too_large(
                body,
                max_bytes=max_bytes,
                label=label,
                status_code=status_code,
                url=url,
                trace_id=trace_id,
                path=path,
            )

    def _raise_if_payload_too_large(
        self,
        body: bytearray,
        *,
        max_bytes: int,
        label: str,
        status_code: int,
        url: str,
        trace_id: str,
        path: str,
    ) -> None:
        if len(body) <= max_bytes:
            return
        raise self._services.infrastructure.UpstreamError(
            f"{label} exceeds max bytes",
            status_code=status_code or None,
            error_code=self._services.infrastructure.EC.STREAM_TOO_LARGE.value,
            payload={
                "path": path,
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
                "max_bytes": max_bytes,
                "actual_bytes": len(body),
            },
        )


async def _curl_stderr_text(stderr_task: asyncio.Task[bytes] | None) -> str:
    if stderr_task is None:
        return ""
    try:
        raw = await stderr_task
    except _CurlOutputTooLarge as exc:
        return (
            f"{exc.label} exceeded {exc.max_bytes} bytes "
            f"(received at least {exc.received_bytes})"
        )
    return raw.decode("utf-8", "replace")


async def _read_curl_stderr(stream: Any) -> bytes:
    return await _read_stream_limited(
        stream,
        max_bytes=_CURL_STDERR_MAX_BYTES,
        label="curl stderr",
    )


async def _read_curl_response_head(
    reader: _CurlSSEReader,
    *,
    services: UpstreamServices,
) -> tuple[int, dict[str, str]]:
    status_line = await reader.next_line()
    if not status_line:
        raise services.infrastructure.UpstreamError(
            "curl sse empty response",
            error_code=services.infrastructure.EC.SSE_CURL_FAILED.value,
            status_code=0,
        )
    status_text = status_line.decode("utf-8", "replace").strip()
    match = re.match(r"HTTP/[\d.]+\s+(\d+)", status_text)
    status_code = int(match.group(1)) if match else 0
    reader.response_status_code = status_code
    response_headers: dict[str, str] = {}
    while True:
        line = await reader.next_line()
        if line is None or line.strip() == b"":
            return status_code, response_headers
        header = line.decode("utf-8", "replace").rstrip("\r\n")
        if ":" in header:
            key, _, value = header.partition(":")
            response_headers[key.strip().lower()] = value.strip()


async def _iter_curl_sse_events(
    reader: _CurlSSEReader,
    *,
    services: UpstreamServices,
) -> AsyncIterator[dict[str, Any]]:
    parser = CurlSSEEventParser()
    while True:
        raw = await reader.next_line()
        if raw is None:
            break
        event = parser.feed_line(raw)
        if event is not None:
            _maybe_record_usage_from_event(event, services=services)
            yield event

    event = parser.finish()
    if event is not None:
        _maybe_record_usage_from_event(event, services=services)
        yield event


def _raise_curl_sse_upstream_error(
    exc: Exception,
    *,
    final_status: int,
    services: UpstreamServices,
) -> None:
    status_code = getattr(exc, "status_code", None)
    if 200 <= final_status < 300 and not (
        isinstance(status_code, int) and 200 <= status_code < 300
    ):
        raw_payload = getattr(exc, "payload", None)
        payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        payload["response_received"] = True
        raise services.infrastructure.UpstreamError(
            str(exc),
            error_code=getattr(exc, "error_code", None),
            status_code=final_status,
            payload=payload,
        ) from exc
    raise exc


def _decode_curl_sse_event(
    event_type: str | None,
    event_data: list[str],
) -> dict[str, Any] | None:
    return decode_sse_event(event_type, event_data)


async def _raise_curl_sse_http_error(
    reader: _CurlSSEReader,
    *,
    services: UpstreamServices,
    status_code: int,
    response_headers: dict[str, str],
    url: str,
    trace_id: str,
    error_path: str,
    runtime: ImageUpstreamRuntime | None,
) -> None:
    err_raw = await reader.drain(
        max_bytes=_error_response_limit(runtime=runtime),
        label="upstream error payload",
        status_code=status_code,
        url=url,
        trace_id=trace_id,
        path=error_path,
    )
    err_text = err_raw.decode("utf-8", "replace")
    services.infrastructure.logger.warning(
        "curl sse non-2xx status=%s url=%s body=%.1000s "
        "trace_id=%s x_request_id=%s",
        status_code,
        url,
        err_text,
        trace_id,
        response_headers.get("x-request-id"),
    )
    try:
        payload = json.loads(err_text)
    except Exception:  # noqa: BLE001
        payload = {"raw": err_text[:2000]}
    raise services.core.with_error_context(
        services.core.parse_error(
            payload if isinstance(payload, dict) else {},
            status_code or 0,
        ),
        path=error_path,
        method="POST",
        url=url,
    )


async def _non_sse_json_event(
    reader: _CurlSSEReader,
    process: CurlSSEProcess,
    *,
    services: UpstreamServices,
    allow_non_sse_payload: bool,
    response_headers: dict[str, str],
    status_code: int,
    url: str,
    trace_id: str,
    error_path: str,
    runtime: ImageUpstreamRuntime | None,
) -> dict[str, Any] | None:
    if not allow_non_sse_payload:
        return None
    content_type = response_headers.get("content-type", "")
    if "text/event-stream" in content_type.lower():
        return None
    body_bytes = await reader.drain(
        max_bytes=_json_response_limit(runtime=runtime),
        label="non-sse json payload",
        status_code=status_code,
        url=url,
        trace_id=trace_id,
        path=error_path,
    )
    body_text = body_bytes.decode("utf-8", errors="replace")
    try:
        json_payload = json.loads(body_text)
    except Exception as exc:  # noqa: BLE001
        raise services.infrastructure.UpstreamError(
            f"non-sse payload is not valid JSON: {exc}",
            status_code=status_code,
            error_code=services.infrastructure.EC.BAD_RESPONSE.value,
            payload={
                "path": error_path,
                "method": "POST",
                "url": url,
                "x_trace_id": trace_id,
                "content_type": content_type,
                "body_summary": body_text[:200],
            },
        ) from exc
    rc = await process.wait()
    if rc != 0:
        stderr_s = await _curl_stderr_text(process.stderr_task)
        services.infrastructure.logger.debug(
            "curl json fallback exited rc=%s stderr=%.500s",
            rc,
            stderr_s,
        )
    return {
        "type": services.core.JSON_PAYLOAD_SENTINEL_TYPE,
        "payload": json_payload,
        "content_type": content_type,
    }


async def _raise_if_curl_sse_process_failed(
    process: CurlSSEProcess,
    *,
    services: UpstreamServices,
) -> None:
    rc = await process.wait()
    if rc == 0:
        return
    stderr_s = await _curl_stderr_text(process.stderr_task)
    raise services.infrastructure.UpstreamError(
        f"curl sse exited rc={rc} stderr={stderr_s[:500]}",
        error_code=services.infrastructure.EC.SSE_CURL_FAILED.value,
        status_code=200,
    )


async def _iter_sse_curl(
    *,
    url: str,
    json_body: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
    proxy_url: str | None = None,
    pinned_target: Any | None = None,
    allow_non_sse_payload: bool = False,
    on_dispatch_ready: DispatchReadyHook | None = None,
    on_response_ready: ResponseReadyHook | None = None,
    on_response_head: ResponseHeadHook | None = None,
    response_context: CurlSSEResponseContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a curl POST and parse bounded SSE or an allowed JSON fallback.

    Runtime bindings supply stream limits, trace and usage recording, log
    context, curl binary selection, and process cleanup.
    """
    services = _runtime_services(runtime)
    response_context = response_context or CurlSSEResponseContext()
    trace_id = headers.get("x-trace-id") or services.core.generate_trace_id()
    fd, body_path = _secure_mkstemp(
        prefix="lumen_sse_body_",
        suffix=".json",
    )
    process = CurlSSEProcess(body_fd=fd, body_path=body_path)
    started = time.monotonic()
    response_headers: dict[str, str] = {}
    final_status = 0
    reader: _CurlSSEReader | None = None

    try:
        try:
            await asyncio.to_thread(
                services.transport.write_json_body_file, fd, json_body
            )
        finally:
            process.close_body_fd()

        process.set_config_path(
            await _stage_curl_secret_config(
                url=url,
                headers={**headers, "Content-Type": "application/json"},
                proxy_url=proxy_url,
                pinned_target=pinned_target,
                runtime=runtime,
            )
        )
        cmd = [
            services.core.CURL_BIN,
            "-sS",
            "-N",
            "-i",
            "--config",
            process.config_path,
            "--data-binary",
            f"@{body_path}",
            url,
        ]
        try:
            if on_dispatch_ready is not None:
                await on_dispatch_ready()
            proc = await process.start(cmd, stderr_reader=_read_curl_stderr)
        except OSError as exc:
            raise services.infrastructure.UpstreamError(
                f"curl sse executable failed to start: {services.core.CURL_BIN!r}: {exc}",
                error_code=services.infrastructure.EC.SSE_CURL_FAILED.value,
                status_code=None,
            ) from exc
        assert proc.stdout is not None

        reader = _CurlSSEReader(
            proc.stdout,
            idle_timeout_s=timeout_s,
            services=services,
        )
        status_code, response_headers = await _read_curl_response_head(
            reader,
            services=services,
        )
        final_status = status_code
        if on_response_head is not None:
            await on_response_head(status_code, response_headers)

        if not 200 <= status_code < 300:
            await _raise_curl_sse_http_error(
                reader,
                services=services,
                status_code=status_code,
                response_headers=response_headers,
                url=url,
                trace_id=trace_id,
                error_path=response_context.error_path,
                runtime=runtime,
            )
        if on_response_ready is not None:
            await on_response_ready()

        fallback_event = await _non_sse_json_event(
            reader,
            process,
            services=services,
            allow_non_sse_payload=allow_non_sse_payload,
            response_headers=response_headers,
            status_code=status_code,
            url=url,
            trace_id=trace_id,
            error_path=response_context.error_path,
            runtime=runtime,
        )
        if fallback_event is not None:
            yield fallback_event
            return

        async for event in _iter_curl_sse_events(reader, services=services):
            yield event

        await _raise_if_curl_sse_process_failed(process, services=services)
    except asyncio.CancelledError:
        raise
    except services.infrastructure.UpstreamError as exc:
        _raise_curl_sse_upstream_error(
            exc,
            final_status=(
                final_status
                or (reader.response_status_code if reader is not None else 0)
            ),
            services=services,
        )
    finally:
        await process.cleanup(services.transport.terminate_curl_proc_group)
        duration_ms = (time.monotonic() - started) * 1000.0
        try:
            services.core.log_upstream_call(
                endpoint=response_context.endpoint_label,
                status=final_status,
                duration_ms=duration_ms,
                trace_id=trace_id,
                response_headers=response_headers,
            )
        except Exception:  # noqa: BLE001
            services.infrastructure.logger.debug(
                "failed to log upstream call meta", exc_info=True
            )


__all__ = [
    "_curl_post_multipart",
    "_curl_post_multipart_using_paths",
    "_curl_timeout_arg",
    "_emit_image_progress",
    "_iter_sse_curl",
    "_maybe_record_usage_from_event",
    "_stage_multipart_bytes_to_tmp",
    "_terminate_curl_proc_group",
    "_write_bytes_file",
    "_write_json_body_file",
]
