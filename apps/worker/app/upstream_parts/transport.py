"""Curl multipart and SSE transports used by the ``app.upstream`` facade."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import math
import os
import re
import signal
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

ImageProgressCallback = Callable[[dict[str, Any]], Any]

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"
_CURL_TEMP_FILE_MODE = 0o600
_CURL_STDERR_MAX_BYTES = 64 * 1024
_DEFAULT_JSON_RESPONSE_MAX_BYTES = 32 * 1024 * 1024
_DEFAULT_ERROR_RESPONSE_MAX_BYTES = 64 * 1024
_AMBIENT_PINNED_TARGET = object()


class _CurlOutputTooLarge(Exception):
    def __init__(self, *, label: str, max_bytes: int, received_bytes: int) -> None:
        super().__init__(f"{label} exceeded {max_bytes} bytes")
        self.label = label
        self.max_bytes = max_bytes
        self.received_bytes = received_bytes


def _facade() -> Any:
    """Resolve the compatibility module at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


def _curl_timeout_arg(timeout_s: float) -> str:
    timeout = math.ceil(timeout_s) if math.isfinite(timeout_s) else 1
    return str(max(1, timeout))


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("failed to write temporary curl payload")
        view = view[written:]


def _write_json_body_file(fd: int, json_body: dict[str, Any]) -> None:
    _write_all(fd, json.dumps(json_body).encode("utf-8"))


def _write_bytes_file(fd: int, raw: bytes) -> None:
    _write_all(fd, raw)


def _secure_mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    try:
        os.fchmod(fd, _CURL_TEMP_FILE_MODE)
    except BaseException:
        os.close(fd)
        with suppress(OSError):
            os.unlink(path)
        raise
    return fd, path


def _curl_config_quote(value: str) -> str:
    if any(char in value for char in ("\x00", "\r", "\n")):
        raise ValueError("curl config value contains a forbidden control character")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _curl_secret_config_bytes(
    *,
    headers: dict[str, str],
    proxy_url: str | None,
    pinned_target: Any | None,
) -> bytes:
    lines = [
        f"header = {_curl_config_quote(f'{key}: {value}')}\n"
        for key, value in headers.items()
    ]
    if proxy_url:
        lines.append(f"proxy = {_curl_config_quote(proxy_url)}\n")
    elif pinned_target is not None:
        parsed = urlsplit(str(pinned_target.url))
        host = (parsed.hostname or "").strip("[]")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        resolved_ip = str(pinned_target.resolved_ips[0]).strip("[]")
        if ":" in resolved_ip:
            resolved_ip = f"[{resolved_ip}]"
        lines.append(
            f"resolve = {_curl_config_quote(f'{host}:{port}:{resolved_ip}')}\n"
        )
    return "".join(lines).encode("utf-8")


def _current_byok_http_target(
    *,
    url: str,
    proxy_url: str | None,
) -> Any | None:
    if proxy_url is not None:
        return None
    from ..byok_runtime import current_byok_http_target

    return current_byok_http_target(url)


async def _stage_curl_secret_config(
    *,
    url: str,
    headers: dict[str, str],
    proxy_url: str | None,
    pinned_target: Any | None,
) -> str:
    facade = _facade()
    effective_target = (
        None
        if proxy_url is not None
        else facade._validated_byok_target_for_request(pinned_target, url)
    )
    fd, config_path = _secure_mkstemp(
        prefix="lumen_curl_",
        suffix=".conf",
    )
    try:
        await asyncio.to_thread(
            _write_bytes_file,
            fd,
            _curl_secret_config_bytes(
                headers=headers,
                proxy_url=proxy_url,
                pinned_target=effective_target,
            ),
        )
    except BaseException:
        with suppress(OSError):
            os.unlink(config_path)
        raise
    finally:
        os.close(fd)
    return config_path


def _configured_limit(facade: Any, name: str, default: int) -> int:
    try:
        value = int(getattr(facade, name))
    except (AttributeError, TypeError, ValueError):
        return default
    return max(0, value)


def _json_response_limit(facade: Any) -> int:
    return _configured_limit(
        facade,
        "_NON_SSE_JSON_MAX_BYTES",
        _DEFAULT_JSON_RESPONSE_MAX_BYTES,
    )


def _error_response_limit(facade: Any) -> int:
    return min(
        _json_response_limit(facade),
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
) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """Stage multipart byte payloads once so retries reuse the same files."""
    facade = _facade()
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
                await asyncio.to_thread(facade._write_bytes_file, fd, raw)
            finally:
                os.close(fd)
            staged.append((field_name, tmp_path, filename, mime))
        return staged, tmpfiles
    except BaseException:
        for tmp_path in tmpfiles:
            with suppress(Exception):
                os.unlink(tmp_path)
        raise


async def _curl_post_multipart_using_paths(
    *,
    url: str,
    data: dict[str, str],
    staged_files: list[tuple[str, str, str, str]],
    headers: dict[str, str],
    timeout_s: float,
    proxy_url: str | None = None,
    pinned_target: Any = _AMBIENT_PINNED_TARGET,
) -> tuple[int, dict[str, Any]]:
    """Curl multipart POST against caller-owned, pre-staged file paths."""
    facade = _facade()
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
        effective_target = pinned_target
        if effective_target is _AMBIENT_PINNED_TARGET:
            effective_target = _current_byok_http_target(
                url=url,
                proxy_url=proxy_url,
            )
        config_path = await _stage_curl_secret_config(
            url=url,
            headers=headers,
            proxy_url=proxy_url,
            pinned_target=effective_target,
        )
        status_marker = "\n__HTTP_STATUS__:"
        status_marker_b = status_marker.encode("ascii")
        cmd = [
            facade._CURL_BIN,
            "-sS",
            "-m",
            facade._curl_timeout_arg(timeout_s),
            "-w",
            f"{status_marker}%{{http_code}}",
            "--config",
            config_path,
            *form_args,
            url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise httpx.ConnectError(
                f"curl executable failed to start: {facade._CURL_BIN!r}: {exc}"
            ) from exc
        curl_timeout_s = float(facade._curl_timeout_arg(timeout_s))
        guard_timeout_s = curl_timeout_s + min(
            5.0,
            max(0.25, curl_timeout_s * 0.1),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                _collect_curl_output(
                    proc,
                    stdout_max_bytes=(
                        _json_response_limit(facade) + len(status_marker_b) + 16
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
        if proc.returncode == 28:
            stderr = stderr_b.decode("utf-8", "replace")[:500]
            raise httpx.TimeoutException(
                f"curl multipart timeout rc=28 stderr={stderr}"
            )
        if proc.returncode != 0:
            raise httpx.HTTPError(
                "curl failed "
                f"rc={proc.returncode} "
                f"stderr={stderr_b.decode('utf-8', 'replace')[:500]}"
            )
        if status_marker_b not in stdout_b:
            raise httpx.HTTPError(
                f"curl output missing status marker (head={stdout_b[:200]!r})"
            )
        body_b, _, status_b = stdout_b.rpartition(status_marker_b)
        if len(body_b) > _json_response_limit(facade):
            raise httpx.HTTPError(
                "curl multipart response exceeded its byte limit "
                f"max_bytes={_json_response_limit(facade)} "
                f"received_bytes={len(body_b)}"
            )
        body_s = body_b.decode("utf-8", "replace")
        try:
            payload = json.loads(body_s)
        except Exception:  # noqa: BLE001
            payload = {"raw": body_s[:2000]}
        return int(status_b.strip()), payload
    except asyncio.CancelledError:
        raise
    finally:
        await facade._terminate_curl_proc_group(proc)
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
) -> tuple[int, dict[str, Any]]:
    """Stage multipart bytes, send them with curl, and always unlink them."""
    facade = _facade()
    staged: list[tuple[str, str, str, str]] = []
    tmpfiles: list[str] = []
    try:
        staged, tmpfiles = await facade._stage_multipart_bytes_to_tmp(files)
        return await facade._curl_post_multipart_using_paths(
            url=url,
            data=data,
            staged_files=staged,
            headers=headers,
            timeout_s=timeout_s,
            proxy_url=proxy_url,
            pinned_target=pinned_target,
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
        facade: Any,
        idle_timeout_s: float,
    ) -> None:
        self._stream = stream
        self._facade = facade
        self._idle_timeout_s = max(0.001, float(idle_timeout_s))
        self._buffer = bytearray()
        self._search_from = 0
        self._stream_eof = False
        self._byte_count = 0
        self._line_count = 0

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
                raise self._facade.UpstreamError(
                    f"curl sse idle timeout after {self._idle_timeout_s:.0f}s",
                    error_code=self._facade.EC.SSE_CURL_FAILED.value,
                    status_code=None,
                ) from exc
            if not chunk:
                self._stream_eof = True
                continue
            self._byte_count += len(chunk)
            if self._byte_count > self._facade._SSE_MAX_BYTES:
                raise self._facade.UpstreamError(
                    "sse exceeded max bytes",
                    error_code=self._facade.EC.STREAM_TOO_LARGE.value,
                    status_code=200,
                )
            self._buffer.extend(chunk)
            if (
                len(self._buffer) > self._facade._SSE_MAX_LINE_BYTES
                and b"\n" not in self._buffer
            ):
                raise self._facade.UpstreamError(
                    "sse exceeded max line bytes",
                    error_code=self._facade.EC.STREAM_TOO_LARGE.value,
                    status_code=200,
                )

    def _record_line(self, line: bytes) -> None:
        self._line_count += 1
        if len(line) > self._facade._SSE_MAX_LINE_BYTES:
            raise self._facade.UpstreamError(
                "sse exceeded max line bytes",
                error_code=self._facade.EC.STREAM_TOO_LARGE.value,
                status_code=200,
            )
        if self._line_count > self._facade._SSE_MAX_LINES:
            raise self._facade.UpstreamError(
                "sse exceeded max lines",
                error_code=self._facade.EC.STREAM_TOO_LARGE.value,
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
    ) -> None:
        if len(body) <= max_bytes:
            return
        raise self._facade.UpstreamError(
            f"{label} exceeds max bytes",
            status_code=status_code or None,
            error_code=self._facade.EC.STREAM_TOO_LARGE.value,
            payload={
                "path": "responses",
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


async def _read_curl_response_head(
    reader: _CurlSSEReader,
    *,
    facade: Any,
) -> tuple[int, dict[str, str]]:
    status_line = await reader.next_line()
    if not status_line:
        raise facade.UpstreamError(
            "curl sse empty response",
            error_code=facade.EC.SSE_CURL_FAILED.value,
            status_code=0,
        )
    status_text = status_line.decode("utf-8", "replace").strip()
    match = re.match(r"HTTP/[\d.]+\s+(\d+)", status_text)
    status_code = int(match.group(1)) if match else 0
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
    facade: Any,
) -> AsyncIterator[dict[str, Any]]:
    event_type: str | None = None
    event_data: list[str] = []
    while True:
        raw = await reader.next_line()
        if raw is None:
            break
        line_text = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line_text == "":
            if event_data:
                event = _decode_curl_sse_event(event_type, event_data)
                if event is not None:
                    facade._maybe_record_usage_from_event(event)
                    yield event
            event_type = None
            event_data = []
            continue
        if line_text.startswith(":"):
            continue
        if line_text.startswith("event:"):
            event_type = line_text[6:].strip()
        elif line_text.startswith("data:"):
            event_data.append(line_text[5:].lstrip())

    event = _decode_curl_sse_event(event_type, event_data)
    if event is not None:
        facade._maybe_record_usage_from_event(event)
        yield event


def _decode_curl_sse_event(
    event_type: str | None,
    event_data: list[str],
) -> dict[str, Any] | None:
    data = "\n".join(event_data)
    if not data or data == "[DONE]":
        return None
    try:
        event = json.loads(data)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(event, dict):
        return None
    if event_type and "type" not in event:
        event["type"] = event_type
    return event


async def _iter_sse_curl(
    *,
    url: str,
    json_body: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
    proxy_url: str | None = None,
    pinned_target: Any | None = None,
    allow_non_sse_payload: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a curl POST and parse bounded SSE or an allowed JSON fallback.

    The facade supplies all policy and hooks at call time: error classes,
    100000-line/80MB/32MB stream limits, trace and usage recording, log context,
    curl binary selection, and process cleanup.
    """
    facade = _facade()
    trace_id = headers.get("x-trace-id") or facade._generate_trace_id()
    fd, body_path = _secure_mkstemp(
        prefix="lumen_sse_body_",
        suffix=".json",
    )
    proc: asyncio.subprocess.Process | None = None
    config_path: str | None = None
    stderr_task: asyncio.Task[bytes] | None = None
    reader: _CurlSSEReader | None = None
    started = time.monotonic()
    response_headers: dict[str, str] = {}
    final_status = 0

    try:
        try:
            await asyncio.to_thread(facade._write_json_body_file, fd, json_body)
        finally:
            os.close(fd)

        config_path = await _stage_curl_secret_config(
            url=url,
            headers={**headers, "Content-Type": "application/json"},
            proxy_url=proxy_url,
            pinned_target=pinned_target,
        )
        cmd = [
            facade._CURL_BIN,
            "-sS",
            "-N",
            "-i",
            "--config",
            config_path,
            "--data-binary",
            f"@{body_path}",
            url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise facade.UpstreamError(
                f"curl sse executable failed to start: {facade._CURL_BIN!r}: {exc}",
                error_code=facade.EC.SSE_CURL_FAILED.value,
                status_code=None,
            ) from exc
        assert proc.stdout is not None
        if proc.stderr is not None:
            stderr_task = asyncio.create_task(
                _read_stream_limited(
                    proc.stderr,
                    max_bytes=_CURL_STDERR_MAX_BYTES,
                    label="curl stderr",
                )
            )

        reader = _CurlSSEReader(
            proc.stdout,
            facade=facade,
            idle_timeout_s=timeout_s,
        )
        status_code, response_headers = await _read_curl_response_head(
            reader,
            facade=facade,
        )
        final_status = status_code

        if not 200 <= status_code < 300:
            err_raw = await reader.drain(
                max_bytes=_error_response_limit(facade),
                label="upstream error payload",
                status_code=status_code,
                url=url,
                trace_id=trace_id,
            )
            err_text = err_raw.decode("utf-8", "replace")
            facade.logger.warning(
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
            raise facade._with_error_context(
                facade._parse_error(
                    payload if isinstance(payload, dict) else {},
                    status_code or 0,
                ),
                path="responses",
                method="POST",
                url=url,
            )

        if allow_non_sse_payload:
            content_type = response_headers.get("content-type", "")
            if "text/event-stream" not in content_type.lower():
                body_bytes = await reader.drain(
                    max_bytes=_json_response_limit(facade),
                    label="non-sse json payload",
                    status_code=status_code,
                    url=url,
                    trace_id=trace_id,
                )
                body_text = body_bytes.decode("utf-8", errors="replace")
                try:
                    json_payload = json.loads(body_text)
                except Exception as exc:  # noqa: BLE001
                    raise facade.UpstreamError(
                        f"non-sse payload is not valid JSON: {exc}",
                        status_code=status_code,
                        error_code=facade.EC.BAD_RESPONSE.value,
                        payload={
                            "path": "responses",
                            "method": "POST",
                            "url": url,
                            "x_trace_id": trace_id,
                            "content_type": content_type,
                            "body_summary": body_text[:200],
                        },
                    ) from exc
                yield {
                    "type": facade._JSON_PAYLOAD_SENTINEL_TYPE,
                    "payload": json_payload,
                    "content_type": content_type,
                }
                rc = await proc.wait()
                if rc != 0:
                    stderr_s = await _curl_stderr_text(stderr_task)
                    facade.logger.debug(
                        "curl json fallback exited rc=%s stderr=%.500s",
                        rc,
                        stderr_s,
                    )
                return

        async for event in _iter_curl_sse_events(reader, facade=facade):
            yield event

        rc = await proc.wait()
        if rc != 0:
            stderr_s = await _curl_stderr_text(stderr_task)
            raise facade.UpstreamError(
                f"curl sse exited rc={rc} stderr={stderr_s[:500]}",
                error_code=facade.EC.SSE_CURL_FAILED.value,
                status_code=200,
            )
    except asyncio.CancelledError:
        raise
    finally:
        await facade._terminate_curl_proc_group(proc)
        if stderr_task is not None:
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        try:
            os.close(fd)
        except OSError:
            pass
        if config_path is not None:
            with suppress(OSError):
                os.unlink(config_path)
        try:
            os.unlink(body_path)
        except Exception:  # noqa: BLE001
            pass
        duration_ms = (time.monotonic() - started) * 1000.0
        try:
            facade._log_upstream_call(
                endpoint="responses",
                status=final_status,
                duration_ms=duration_ms,
                trace_id=trace_id,
                response_headers=response_headers,
            )
        except Exception:  # noqa: BLE001
            facade.logger.debug("failed to log upstream call meta", exc_info=True)


def _maybe_record_usage_from_event(event: dict[str, Any]) -> None:
    """Record terminal usage and warn about unknown response output types."""
    facade = _facade()
    usage = event.get("usage")
    if not isinstance(usage, dict):
        response = event.get("response")
        if isinstance(response, dict):
            usage = response.get("usage")
    if isinstance(usage, dict):
        facade._record_usage(usage)
    if facade._is_responses_success_terminal(event.get("type")):
        response = event.get("response")
        if isinstance(response, dict):
            outputs = response.get("output")
            if isinstance(outputs, list):
                for item in outputs:
                    if isinstance(item, dict):
                        item_type = item.get("type")
                        if (
                            isinstance(item_type, str)
                            and item_type not in facade._KNOWN_OUTPUT_ITEM_TYPES
                        ):
                            facade.logger.warning(
                                "upstream output item with unknown type=%r; skipping",
                                item_type,
                            )


async def _emit_image_progress(
    progress_callback: ImageProgressCallback | None,
    event_type: str,
    **payload: Any,
) -> None:
    if progress_callback is None:
        return
    facade = _facade()
    event = {"type": event_type, **payload}
    try:
        result = progress_callback(event)
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001
        facade.logger.warning("image progress callback failed", exc_info=True)


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
