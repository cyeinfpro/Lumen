"""Secure temporary files and curl secret configuration helpers."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    resolve_image_upstream_services,
)

_CURL_TEMP_FILE_MODE = 0o600


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("failed to write temporary curl payload")
        view = view[written:]


def write_json_body_file(fd: int, json_body: dict[str, Any]) -> None:
    _write_all(fd, json.dumps(json_body).encode("utf-8"))


def write_bytes_file(fd: int, raw: bytes) -> None:
    _write_all(fd, raw)


def secure_mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
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


async def stage_curl_secret_config(
    *,
    url: str,
    headers: dict[str, str],
    proxy_url: str | None,
    pinned_target: Any | None,
    runtime: ImageUpstreamRuntime | None = None,
) -> str:
    services = resolve_image_upstream_services(runtime)
    effective_target = (
        None
        if proxy_url is not None
        else services.requests.validated_byok_target_for_request(pinned_target, url)
    )
    fd, config_path = secure_mkstemp(
        prefix="lumen_curl_",
        suffix=".conf",
    )
    try:
        await asyncio.to_thread(
            write_bytes_file,
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


__all__ = [
    "secure_mkstemp",
    "stage_curl_secret_config",
    "write_bytes_file",
    "write_json_body_file",
]
