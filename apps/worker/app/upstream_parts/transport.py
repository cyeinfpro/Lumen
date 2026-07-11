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

import httpx

ImageProgressCallback = Callable[[dict[str, Any]], Any]

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    """Resolve the compatibility module at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


def _curl_timeout_arg(timeout_s: float) -> str:
    timeout = math.ceil(timeout_s) if math.isfinite(timeout_s) else 1
    return str(max(1, timeout))


def _write_json_body_file(fd: int, json_body: dict[str, Any]) -> None:
    os.write(fd, json.dumps(json_body).encode("utf-8"))


def _write_bytes_file(fd: int, raw: bytes) -> None:
    os.write(fd, raw)


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
            fd, tmp_path = tempfile.mkstemp(prefix="lumen_curl_", suffix=".bin")
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
) -> tuple[int, dict[str, Any]]:
    """Curl multipart POST against caller-owned, pre-staged file paths."""
    facade = _facade()
    proc: asyncio.subprocess.Process | None = None
    try:
        form_args: list[str] = []
        for key, value in data.items():
            form_args += ["-F", f"{key}={value}"]
        for field_name, tmp_path, filename, mime in staged_files:
            form_args += [
                "-F",
                f"{field_name}=@{tmp_path};filename={filename};type={mime}",
            ]
        header_args: list[str] = []
        for key, value in headers.items():
            header_args += ["-H", f"{key}: {value}"]
        status_marker = "\n__HTTP_STATUS__:"
        cmd = [
            facade._CURL_BIN,
            "-sS",
            "-m",
            facade._curl_timeout_arg(timeout_s),
            "-w",
            f"{status_marker}%{{http_code}}",
            *(["--proxy", proxy_url] if proxy_url else []),
            *header_args,
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
                proc.communicate(),
                timeout=guard_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise httpx.TimeoutException(
                f"curl multipart timed out after {guard_timeout_s:.2f}s"
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
        out = stdout_b.decode("utf-8", "replace")
        if status_marker not in out:
            raise httpx.HTTPError(
                f"curl output missing status marker (head={out[:200]!r})"
            )
        body_s, _, status_s = out.rpartition(status_marker)
        try:
            payload = json.loads(body_s)
        except Exception:  # noqa: BLE001
            payload = {"raw": body_s[:2000]}
        return int(status_s.strip()), payload
    except asyncio.CancelledError:
        raise
    finally:
        await facade._terminate_curl_proc_group(proc)


async def _curl_post_multipart(
    *,
    url: str,
    data: dict[str, str],
    files: list[tuple[str, tuple[str, bytes, str]]],
    headers: dict[str, str],
    timeout_s: float,
    proxy_url: str | None = None,
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
        )
    finally:
        for path in tmpfiles:
            try:
                os.unlink(path)
            except Exception:  # noqa: BLE001
                pass


async def _iter_sse_curl(
    *,
    url: str,
    json_body: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
    proxy_url: str | None = None,
    allow_non_sse_payload: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a curl POST and parse bounded SSE or an allowed JSON fallback.

    The facade supplies all policy and hooks at call time: error classes,
    100000-line/80MB/32MB stream limits, trace and usage recording, log context,
    curl binary selection, and process cleanup.
    """
    facade = _facade()
    trace_id = headers.get("x-trace-id") or facade._generate_trace_id()
    fd, body_path = tempfile.mkstemp(prefix="lumen_sse_body_", suffix=".json")
    proc: asyncio.subprocess.Process | None = None
    started = time.monotonic()
    response_headers: dict[str, str] = {}
    final_status = 0

    buf = bytearray()
    search_from = 0
    stream_eof = False
    byte_count = 0
    line_count = 0
    idle_timeout_s = max(0.001, float(timeout_s))

    async def next_line() -> bytes | None:
        nonlocal search_from, stream_eof, byte_count, line_count
        while True:
            idx = buf.find(b"\n", search_from)
            if idx >= 0:
                line = bytes(buf[: idx + 1])
                del buf[: idx + 1]
                search_from = 0
                line_count += 1
                if len(line) > facade._SSE_MAX_LINE_BYTES:
                    raise facade.UpstreamError(
                        "sse exceeded max line bytes",
                        error_code=facade.EC.STREAM_TOO_LARGE.value,
                        status_code=200,
                    )
                if line_count > facade._SSE_MAX_LINES:
                    raise facade.UpstreamError(
                        "sse exceeded max lines",
                        error_code=facade.EC.STREAM_TOO_LARGE.value,
                        status_code=200,
                    )
                return line
            search_from = len(buf)
            if stream_eof:
                if buf:
                    line = bytes(buf)
                    if len(line) > facade._SSE_MAX_LINE_BYTES:
                        raise facade.UpstreamError(
                            "sse exceeded max line bytes",
                            error_code=facade.EC.STREAM_TOO_LARGE.value,
                            status_code=200,
                        )
                    buf.clear()
                    search_from = 0
                    line_count += 1
                    return line
                return None
            current_proc = proc
            if current_proc is None or current_proc.stdout is None:
                raise RuntimeError("curl sse process stdout is unavailable")
            try:
                chunk = await asyncio.wait_for(
                    current_proc.stdout.read(65536),
                    timeout=idle_timeout_s,
                )
            except asyncio.TimeoutError as exc:
                raise facade.UpstreamError(
                    f"curl sse idle timeout after {idle_timeout_s:.0f}s",
                    error_code=facade.EC.SSE_CURL_FAILED.value,
                    status_code=None,
                ) from exc
            if not chunk:
                stream_eof = True
                continue
            byte_count += len(chunk)
            if byte_count > facade._SSE_MAX_BYTES:
                raise facade.UpstreamError(
                    "sse exceeded max bytes",
                    error_code=facade.EC.STREAM_TOO_LARGE.value,
                    status_code=200,
                )
            buf.extend(chunk)
            if len(buf) > facade._SSE_MAX_LINE_BYTES and b"\n" not in buf:
                raise facade.UpstreamError(
                    "sse exceeded max line bytes",
                    error_code=facade.EC.STREAM_TOO_LARGE.value,
                    status_code=200,
                )

    async def drain_remaining() -> bytes:
        chunks: list[bytes] = []
        if buf:
            chunks.append(bytes(buf))
            buf.clear()
        while True:
            line = await next_line()
            if line is None:
                break
            chunks.append(line)
        return b"".join(chunks)

    try:
        try:
            await asyncio.to_thread(facade._write_json_body_file, fd, json_body)
        finally:
            os.close(fd)

        header_args: list[str] = []
        for key, value in headers.items():
            header_args += ["-H", f"{key}: {value}"]
        header_args += ["-H", "Content-Type: application/json"]
        cmd = [
            facade._CURL_BIN,
            "-sS",
            "-N",
            "-i",
            *(["--proxy", proxy_url] if proxy_url else []),
            *header_args,
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

        status_line = await next_line()
        if not status_line:
            raise facade.UpstreamError(
                "curl sse empty response",
                error_code=facade.EC.SSE_CURL_FAILED.value,
                status_code=0,
            )
        status_s = status_line.decode("utf-8", "replace").strip()
        match = re.match(r"HTTP/[\d.]+\s+(\d+)", status_s)
        status_code = int(match.group(1)) if match else 0
        final_status = status_code

        while True:
            line = await next_line()
            if line is None or line.strip() == b"":
                break
            try:
                header = line.decode("utf-8", "replace").rstrip("\r\n")
            except Exception:  # noqa: BLE001
                continue
            if ":" in header:
                key, _, value = header.partition(":")
                response_headers[key.strip().lower()] = value.strip()

        if status_code >= 400 or status_code == 0:
            err_raw = await drain_remaining()
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
                body_bytes = await drain_remaining()
                if len(body_bytes) > facade._NON_SSE_JSON_MAX_BYTES:
                    raise facade.UpstreamError(
                        "non-sse json payload exceeds max bytes",
                        status_code=status_code,
                        error_code=facade.EC.STREAM_TOO_LARGE.value,
                        payload={
                            "path": "responses",
                            "method": "POST",
                            "url": url,
                            "x_trace_id": trace_id,
                            "max_bytes": facade._NON_SSE_JSON_MAX_BYTES,
                            "actual_bytes": len(body_bytes),
                        },
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
                    stderr_s = ""
                    if proc.stderr is not None:
                        stderr_s = (await proc.stderr.read()).decode(
                            "utf-8",
                            "replace",
                        )
                    facade.logger.debug(
                        "curl json fallback exited rc=%s stderr=%.500s",
                        rc,
                        stderr_s,
                    )
                return

        event_type: str | None = None
        event_data: list[str] = []

        while True:
            raw = await next_line()
            if raw is None:
                break
            line_text = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line_text == "":
                if event_data:
                    data_s = "\n".join(event_data)
                    if data_s and data_s != "[DONE]":
                        try:
                            event = json.loads(data_s)
                        except Exception:  # noqa: BLE001
                            event = None
                        if isinstance(event, dict):
                            if event_type and "type" not in event:
                                event["type"] = event_type
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

        if event_data:
            data_s = "\n".join(event_data)
            if data_s and data_s != "[DONE]":
                try:
                    event = json.loads(data_s)
                    if isinstance(event, dict):
                        if event_type and "type" not in event:
                            event["type"] = event_type
                        facade._maybe_record_usage_from_event(event)
                        yield event
                except Exception:  # noqa: BLE001
                    pass

        rc = await proc.wait()
        if rc != 0:
            stderr_s = ""
            if proc.stderr is not None:
                stderr_s = (await proc.stderr.read()).decode("utf-8", "replace")
            raise facade.UpstreamError(
                f"curl sse exited rc={rc} stderr={stderr_s[:500]}",
                error_code=facade.EC.SSE_CURL_FAILED.value,
                status_code=200,
            )
    except asyncio.CancelledError:
        raise
    finally:
        await facade._terminate_curl_proc_group(proc)
        try:
            os.close(fd)
        except OSError:
            pass
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
