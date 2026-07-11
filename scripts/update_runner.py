#!/usr/bin/env python3
"""Validate an API-authored update request before invoking update.sh as root."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


_DEFAULT_REQUEST = Path("/opt/lumendata/backup/.update.request.json")
_DEFAULT_SCRIPT = Path("/opt/lumen/current/scripts/update.sh")
_MAX_REQUEST_BYTES = 16 * 1024
_ALLOWED_FIELDS = {
    "schema",
    "target_tag",
    "channel",
    "force_redeploy",
    "idempotency_key",
    "proxy_url",
    "issued_at",
}
_TAG_RE = re.compile(
    r"^(?:v[0-9]+(?:\.[0-9]+){0,2}(?:-[0-9A-Za-z.-]+)?|main)$"
)
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_CHANNELS = {"stable", "main", "pinned", "minor", "major"}
_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
_MAX_REQUEST_AGE = timedelta(minutes=5)


class UpdateRequestError(ValueError):
    pass


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UpdateRequestError("request is not a regular file")
        if info.st_size <= 0 or info.st_size > _MAX_REQUEST_BYTES:
            raise UpdateRequestError("request size is invalid")
        data = os.read(fd, _MAX_REQUEST_BYTES + 1)
        if len(data) != info.st_size:
            raise UpdateRequestError("request changed while being read")
        return data
    finally:
        os.close(fd)


def _validated_proxy_url(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw or len(raw) > 2048:
        raise UpdateRequestError("proxy_url is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise UpdateRequestError("proxy_url contains control characters")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in _PROXY_SCHEMES or not parsed.hostname:
        raise UpdateRequestError("proxy_url scheme or host is invalid")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise UpdateRequestError("proxy_url must not contain path, query, or fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise UpdateRequestError("proxy_url port is invalid") from exc
    return raw


def load_request(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(_read_regular_file(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateRequestError("cannot read update request") from exc
    if not isinstance(payload, dict):
        raise UpdateRequestError("request must be an object")
    extra = set(payload) - _ALLOWED_FIELDS
    missing = _ALLOWED_FIELDS - set(payload)
    if extra or missing:
        raise UpdateRequestError("request fields do not match schema")
    if payload.get("schema") != 1:
        raise UpdateRequestError("unsupported request schema")

    target_tag = payload.get("target_tag")
    channel = payload.get("channel")
    idempotency_key = payload.get("idempotency_key")
    issued_at = payload.get("issued_at")
    force_redeploy = payload.get("force_redeploy")
    if not isinstance(target_tag, str) or not _TAG_RE.fullmatch(target_tag):
        raise UpdateRequestError("target_tag is invalid")
    if not isinstance(channel, str) or channel not in _CHANNELS:
        raise UpdateRequestError("channel is invalid")
    if (
        not isinstance(idempotency_key, str)
        or not _IDEMPOTENCY_RE.fullmatch(idempotency_key)
    ):
        raise UpdateRequestError("idempotency_key is invalid")
    if not isinstance(force_redeploy, bool):
        raise UpdateRequestError("force_redeploy must be boolean")
    if not isinstance(issued_at, str):
        raise UpdateRequestError("issued_at is invalid")
    try:
        issued = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpdateRequestError("issued_at is invalid") from exc
    if issued.tzinfo is None:
        raise UpdateRequestError("issued_at must include a timezone")
    age = datetime.now(timezone.utc) - issued.astimezone(timezone.utc)
    if age < -timedelta(minutes=1) or age > _MAX_REQUEST_AGE:
        raise UpdateRequestError("request is stale")

    return {
        "target_tag": target_tag,
        "channel": channel,
        "force_redeploy": force_redeploy,
        "idempotency_key": idempotency_key,
        "proxy_url": _validated_proxy_url(payload.get("proxy_url")),
    }


def build_environment(request: dict[str, object]) -> dict[str, str]:
    target_tag = str(request["target_tag"])
    env = {
        "HOME": "/root",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LUMEN_UPDATE_NONINTERACTIVE": "1",
        "LUMEN_UPDATE_MODE": "fast",
        "LUMEN_UPDATE_GIT_PULL": "1",
        "LUMEN_UPDATE_BUILD": "0",
        "LUMEN_UPDATE_CHANNEL": str(request["channel"]),
        "LUMEN_UPDATE_RESOLVED_TAG": target_tag,
        "LUMEN_UPDATE_IDEMPOTENCY_KEY": str(request["idempotency_key"]),
        "LUMEN_IMAGE_TAG": target_tag,
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    version_match = re.fullmatch(
        r"v([0-9]+(?:\.[0-9]+){2}(?:-[0-9A-Za-z.-]+)?)",
        target_tag,
    )
    if version_match:
        env["LUMEN_VERSION"] = version_match.group(1)
    if request["force_redeploy"]:
        env["LUMEN_UPDATE_FORCE_REDEPLOY"] = "1"
    proxy_url = request.get("proxy_url")
    if isinstance(proxy_url, str):
        for key in (
            "LUMEN_UPDATE_PROXY_URL",
            "LUMEN_HTTP_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            env[key] = proxy_url
    return env


def main() -> int:
    request_path = Path(os.environ.get("LUMEN_UPDATE_REQUEST", _DEFAULT_REQUEST))
    update_script = Path(os.environ.get("LUMEN_UPDATE_SCRIPT", _DEFAULT_SCRIPT))
    try:
        request = load_request(request_path)
        script_info = update_script.stat()
        if not stat.S_ISREG(script_info.st_mode):
            raise UpdateRequestError("update script is not a regular file")
    except (OSError, UpdateRequestError) as exc:
        print(f"update runner rejected request: {exc}", file=sys.stderr)
        return 2
    os.execve(
        "/usr/bin/env",
        ["/usr/bin/env", "bash", str(update_script)],
        build_environment(request),
    )
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
