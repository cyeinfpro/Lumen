"""Poster-style preset synchronization services.

The implementation is runtime-injected so the route facade remains a stable
compatibility surface for existing tests and operational monkeypatches.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from fastapi import HTTPException
from lumen_core.schemas import PosterStyleSyncOut


class PosterStyleSyncLimitExceeded(ValueError):
    """A configured GitHub traversal or download budget was exceeded."""


class PosterStyleSyncLeaseLost(RuntimeError):
    """The sync lease expired or was replaced before this worker finished."""


def decoded_url_path_segments(
    url: str,
    *,
    allow_trailing_slash: bool = False,
) -> list[str] | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    raw_segments = path.split("/")
    if not raw_segments or raw_segments[0] != "":
        return None
    segments: list[str] = []
    path_segments = raw_segments[1:]
    for index, raw_segment in enumerate(path_segments):
        if not raw_segment:
            if allow_trailing_slash and index == len(path_segments) - 1:
                continue
            return None
        segment = unquote(raw_segment)
        if (
            segment in {".", ".."}
            or "/" in segment
            or "\\" in segment
            or "\x00" in segment
        ):
            return None
        segments.append(segment)
    return segments


def validate_github_contents_url(runtime: Any, url: str) -> str:
    clean = (url or "").strip()
    try:
        parts = urlsplit(clean)
        port = parts.port
    except ValueError:
        parts = None
        port = None
    if (
        parts is None
        or parts.scheme != "https"
        or (parts.hostname or "").lower() != runtime._GITHUB_API_HOST
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or bool(parts.fragment)
    ):
        raise runtime._http(
            "invalid_preset_sync_url",
            "preset sync URL must be a GitHub contents API URL",
            503,
        )
    segments = decoded_url_path_segments(clean, allow_trailing_slash=True)
    if (
        segments is None
        or len(segments) < 5
        or segments[0] != "repos"
        or segments[3] != "contents"
    ):
        raise runtime._http(
            "invalid_preset_sync_url",
            "preset sync URL must be a GitHub contents API URL",
            503,
        )
    return clean


def validate_github_download_url(runtime: Any, url: str) -> str | None:
    clean = (url or "").strip()
    try:
        parts = urlsplit(clean)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or (parts.hostname or "").lower() not in runtime._GITHUB_RAW_HOSTS
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or bool(parts.fragment)
    ):
        return None
    segments = decoded_url_path_segments(clean)
    if segments is None or len(segments) < 4:
        return None
    return clean


def require_github_download_url(runtime: Any, url: str) -> str:
    clean = validate_github_download_url(runtime, url)
    if clean is None:
        raise ValueError("preset file download URL must be a GitHub raw URL")
    return clean


def github_api_child_url(base_url: str, child_name: str) -> str:
    if (
        not child_name
        or child_name in {".", ".."}
        or "/" in child_name
        or "\\" in child_name
        or "\x00" in child_name
    ):
        raise ValueError("invalid GitHub child path")
    prefix, _, query = base_url.partition("?")
    safe_child_name = quote(child_name, safe="")
    return (
        f"{prefix.rstrip('/')}/{safe_child_name}?{query}"
        if query
        else f"{prefix.rstrip('/')}/{safe_child_name}"
    )


async def fetch_bytes(
    runtime: Any,
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    validate_url: Callable[[str], str],
    headers: dict[str, str] | None = None,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> bytes:
    if max_bytes <= 0:
        raise PosterStyleSyncLimitExceeded("GitHub response byte budget exhausted")
    current_url = validate_url(url)
    redirect_count = 0
    while True:
        async with client.stream(
            "GET",
            current_url,
            headers=headers,
            follow_redirects=False,
        ) as response:
            if response.status_code in runtime._HTTP_REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("GitHub redirect is missing Location")
                if redirect_count >= runtime.POSTER_STYLE_MAX_REDIRECTS:
                    raise ValueError("GitHub redirect limit exceeded")
                if len(location) > 4096:
                    raise ValueError("GitHub redirect Location is too long")
                current_url = validate_url(urljoin(current_url, location))
                redirect_count += 1
                if progress is not None:
                    await progress()
                continue

            response.raise_for_status()
            raw_length = response.headers.get("content-length")
            if raw_length:
                try:
                    content_length = int(raw_length)
                except ValueError as exc:
                    raise ValueError("invalid GitHub Content-Length") from exc
                if content_length < 0:
                    raise ValueError("invalid GitHub Content-Length")
                if content_length > max_bytes:
                    raise PosterStyleSyncLimitExceeded(
                        f"GitHub response exceeds {max_bytes} bytes"
                    )
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > max_bytes:
                    raise PosterStyleSyncLimitExceeded(
                        f"GitHub response exceeds {max_bytes} bytes"
                    )
                payload.extend(chunk)
                if progress is not None:
                    await progress()
            return bytes(payload)


async def fetch_github_contents_bytes(
    runtime: Any,
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> bytes:
    return await runtime._fetch_bytes(
        client,
        url,
        max_bytes=max_bytes,
        validate_url=lambda value: runtime._validate_github_contents_url(value),
        headers={"Accept": "application/vnd.github+json"},
        progress=progress,
    )


async def fetch_github_download_bytes(
    runtime: Any,
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int | None = None,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> bytes:
    if max_bytes is None:
        max_bytes = runtime.POSTER_STYLE_MAX_BINARY_BYTES
    return await runtime._fetch_bytes(
        client,
        url,
        max_bytes=max_bytes,
        validate_url=lambda value: runtime._require_github_download_url(value),
        progress=progress,
    )


async def walk_github_contents(
    runtime: Any,
    client: httpx.AsyncClient,
    contents_url: str,
    *,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    root_url = runtime._validate_github_contents_url(contents_url)
    pending: list[tuple[str, int]] = [(root_url, 0)]
    scheduled: set[str] = {root_url}
    files: list[dict[str, Any]] = []
    metadata_bytes = 0
    cursor = 0
    while cursor < len(pending):
        current_url, depth = pending[cursor]
        cursor += 1
        remaining_metadata = (
            runtime.POSTER_STYLE_MAX_GITHUB_METADATA_BYTES - metadata_bytes
        )
        raw = await runtime._fetch_github_contents_bytes(
            client,
            current_url,
            max_bytes=min(
                runtime.POSTER_STYLE_MAX_GITHUB_RESPONSE_BYTES,
                remaining_metadata,
            ),
            progress=progress,
        )
        metadata_bytes += len(raw)
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("type") == "file":
            entries: list[Any] = [data]
        elif isinstance(data, list):
            entries = data
        else:
            raise ValueError("GitHub contents response must be an array")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            name = str(entry.get("name") or "")
            if entry_type == "dir":
                if depth >= runtime.POSTER_STYLE_MAX_GITHUB_DEPTH:
                    raise PosterStyleSyncLimitExceeded(
                        "GitHub contents depth limit exceeded"
                    )
                child_url = runtime._github_api_child_url(current_url, name)
                if child_url in scheduled:
                    continue
                if len(scheduled) >= runtime.POSTER_STYLE_MAX_GITHUB_DIRECTORIES:
                    raise PosterStyleSyncLimitExceeded(
                        "GitHub contents directory limit exceeded"
                    )
                scheduled.add(child_url)
                pending.append((child_url, depth + 1))
            elif entry_type == "file":
                files.append(entry)
                if len(files) > runtime.POSTER_STYLE_MAX_GITHUB_FILES:
                    raise PosterStyleSyncLimitExceeded(
                        "GitHub contents file limit exceeded"
                    )
        if progress is not None:
            await progress()
    return files


def github_entry_size(entry: dict[str, Any]) -> int | None:
    raw = entry.get("size")
    if raw is None:
        return None
    try:
        size = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid GitHub file size") from exc
    if size < 0:
        raise ValueError("invalid GitHub file size")
    return size


def sync_download_limit(
    runtime: Any,
    *,
    downloaded_bytes: int,
    expected_size: int | None,
) -> int:
    remaining = runtime.POSTER_STYLE_MAX_SYNC_DOWNLOAD_BYTES - downloaded_bytes
    if remaining <= 0:
        raise PosterStyleSyncLimitExceeded("poster style sync download budget exceeded")
    if expected_size is not None:
        if expected_size > runtime.POSTER_STYLE_MAX_BINARY_BYTES:
            raise PosterStyleSyncLimitExceeded(
                "GitHub poster style binary exceeds the per-file byte limit"
            )
        if expected_size > remaining:
            raise PosterStyleSyncLimitExceeded(
                "poster style sync download budget exceeded"
            )
    return min(runtime.POSTER_STYLE_MAX_BINARY_BYTES, remaining)


def github_entry_path(entry: dict[str, Any]) -> Path | None:
    raw = str(entry.get("path") or entry.get("name") or "").strip()
    if not raw or "\x00" in raw or "\\" in raw:
        return None
    path = Path(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def poster_relative_parts(path: Path) -> list[str]:
    parts = list(path.parts)
    for index in range(max(0, len(parts) - 1)):
        if parts[index : index + 2] == ["assets", "poster-style-presets"]:
            return parts[index + 2 :]
    return parts


async def fetch_meta_json(
    runtime: Any,
    client: httpx.AsyncClient,
    entry: dict[str, Any],
    *,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any] | None:
    download_url = str(entry.get("download_url") or "").strip()
    if not download_url:
        return None
    expected_size = runtime._github_entry_size(entry)
    if (
        expected_size is not None
        and expected_size > runtime.POSTER_STYLE_MAX_META_BYTES
    ):
        raise PosterStyleSyncLimitExceeded(
            "GitHub poster style meta.json exceeds the byte limit"
        )
    try:
        raw = await runtime._fetch_github_download_bytes(
            client,
            download_url,
            max_bytes=runtime.POSTER_STYLE_MAX_META_BYTES,
            progress=progress,
        )
    except (PosterStyleSyncLimitExceeded, PosterStyleSyncLeaseLost):
        raise
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        runtime.logger.info(
            "poster style: meta.json download failed url=%s err=%s",
            download_url,
            exc,
        )
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        runtime.logger.info("poster style: meta.json decode failed err=%s", exc)
        return None
    return data if isinstance(data, dict) else None


def sync_lease_owner(state: dict[str, Any]) -> tuple[str, datetime] | None:
    lease = state.get("sync_lease")
    if not isinstance(lease, dict):
        return None
    token = str(lease.get("token") or "").strip()
    expires_at = _safe_datetime(state.get("sync_lease", {}).get("expires_at"))
    if not token or expires_at is None:
        return None
    return token, expires_at


def _safe_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(parsed.tzinfo)


def claim_library_sync_lease_sync(runtime: Any) -> tuple[str | None, dict[str, Any]]:
    with runtime._poster_style_sync_file_lock(runtime._library_sync_lock_path()):
        state = runtime._read_json_file(
            runtime._library_sync_state_path(),
            runtime._default_sync_state(),
        )
        now = runtime._now()
        last_success = runtime._safe_datetime(state.get("last_success_at"))
        if last_success is not None:
            success_age = (now - last_success).total_seconds()
            if success_age < runtime.POSTER_STYLE_SYNC_COOLDOWN_S:
                return None, state

        owner = runtime._sync_lease_owner(state)
        if owner is not None and owner[1] > now:
            return None, state
        if owner is not None:
            state["sync_lease"] = None

        last_attempt = runtime._safe_datetime(state.get("last_attempt_at"))
        if last_attempt is not None:
            attempt_age = (now - last_attempt).total_seconds()
            if attempt_age < runtime.POSTER_STYLE_SYNC_FAILURE_COOLDOWN_S:
                return None, state

        token = secrets.token_hex(16)
        now_iso = now.isoformat().replace("+00:00", "Z")
        state["last_attempt_at"] = now_iso
        state["sync_lease"] = {
            "token": token,
            "started_at": now_iso,
            "heartbeat_at": now_iso,
            "expires_at": (
                now + timedelta(seconds=runtime.POSTER_STYLE_SYNC_LEASE_SECONDS)
            )
            .isoformat()
            .replace("+00:00", "Z"),
        }
        runtime._save_sync_state(state)
        return token, state


async def claim_library_sync_lease(
    runtime: Any,
) -> tuple[str | None, dict[str, Any]]:
    async with runtime._SYNC_LOCK:
        return await asyncio.to_thread(runtime._claim_library_sync_lease_sync)


def renew_library_sync_lease_sync(runtime: Any, token: str) -> bool:
    with runtime._poster_style_sync_file_lock(runtime._library_sync_lock_path()):
        state = runtime._read_json_file(
            runtime._library_sync_state_path(),
            runtime._default_sync_state(),
        )
        owner = runtime._sync_lease_owner(state)
        if owner is None or owner[0] != token:
            return False
        now = runtime._now()
        now_iso = now.isoformat().replace("+00:00", "Z")
        lease = dict(state["sync_lease"])
        lease["heartbeat_at"] = now_iso
        lease["expires_at"] = (
            (now + timedelta(seconds=runtime.POSTER_STYLE_SYNC_LEASE_SECONDS))
            .isoformat()
            .replace("+00:00", "Z")
        )
        state["sync_lease"] = lease
        runtime._save_sync_state(state)
        return True


async def renew_library_sync_lease(runtime: Any, token: str) -> bool:
    async with runtime._SYNC_LOCK:
        return await asyncio.to_thread(
            runtime._renew_library_sync_lease_sync,
            token,
        )


def complete_library_sync_lease_sync(
    runtime: Any,
    token: str,
    index: dict[str, Any],
    result: dict[str, Any],
    completed_at: datetime,
) -> None:
    with runtime._poster_style_sync_file_lock(runtime._library_sync_lock_path()):
        state = runtime._read_json_file(
            runtime._library_sync_state_path(),
            runtime._default_sync_state(),
        )
        owner = runtime._sync_lease_owner(state)
        if owner is None or owner[0] != token:
            raise runtime._PosterStyleSyncLeaseLost("poster style sync lease was lost")
        runtime._save_global_preset_index(index)
        state["last_success_at"] = completed_at.isoformat().replace("+00:00", "Z")
        state["last_error"] = None
        state["last_result"] = result
        state["sync_lease"] = None
        runtime._save_sync_state(state)


async def complete_library_sync_lease(
    runtime: Any,
    token: str,
    index: dict[str, Any],
    result: dict[str, Any],
    completed_at: datetime,
) -> None:
    async with runtime._SYNC_LOCK:
        await asyncio.to_thread(
            runtime._complete_library_sync_lease_sync,
            token,
            index,
            result,
            completed_at,
        )


def fail_library_sync_lease_sync(
    runtime: Any,
    token: str,
    *,
    message: str,
    result: dict[str, Any],
) -> bool:
    with runtime._poster_style_sync_file_lock(runtime._library_sync_lock_path()):
        state = runtime._read_json_file(
            runtime._library_sync_state_path(),
            runtime._default_sync_state(),
        )
        owner = runtime._sync_lease_owner(state)
        if owner is None or owner[0] != token:
            return False
        state["last_error"] = message[:1000]
        state["last_result"] = result
        state["sync_lease"] = None
        runtime._save_sync_state(state)
        return True


async def fail_library_sync_lease(
    runtime: Any,
    token: str,
    *,
    message: str,
    result: dict[str, Any],
) -> bool:
    async with runtime._SYNC_LOCK:
        return await asyncio.to_thread(
            runtime._fail_library_sync_lease_sync,
            token,
            message=message,
            result=result,
        )


def cached_sync_response(runtime: Any, state: dict[str, Any]) -> PosterStyleSyncOut:
    raw_result = state.get("last_result")
    result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    return PosterStyleSyncOut(
        status="skipped",
        added=int(result.get("added") or 0),
        updated=int(result.get("updated") or 0),
        skipped=int(result.get("skipped") or 0),
        errors=runtime._clean_string_list(
            result.get("errors") or [],
            max_items=20,
            max_len=300,
        ),
        last_success_at=runtime._safe_datetime(state.get("last_success_at")),
        last_error=runtime._clean_optional_text(
            state.get("last_error"),
            max_len=1000,
        ),
    )


async def sync_library_presets_from_github_folder(
    runtime: Any,
    contents_url: str,
    *,
    proxy_url: str | None = None,
) -> PosterStyleSyncOut:
    if not contents_url:
        raise runtime._http(
            "sync_not_configured",
            "preset GitHub folder url is not configured",
            503,
        )
    validated_url = runtime._validate_github_contents_url(contents_url)
    lease_token, state = await runtime._claim_library_sync_lease()
    if lease_token is None:
        return runtime._cached_sync_response(state)
    return await runtime._do_sync_library_presets(
        validated_url,
        state,
        proxy_url=proxy_url,
        lease_token=lease_token,
    )


def build_preset_entry(
    runtime: Any,
    *,
    parsed_meta: dict[str, Any],
    samples_for_storage: list[dict[str, Any]],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    now_iso = runtime._iso_now()
    return {
        "id": runtime._preset_item_id(
            parsed_meta["preset_id"],
            parsed_meta["version"],
        ),
        "source": "preset",
        "preset_id": parsed_meta["preset_id"],
        "version": parsed_meta["version"],
        "title": parsed_meta["title"],
        "category": parsed_meta["category"],
        "library_folder": parsed_meta["library_folder"],
        "mood": parsed_meta["mood"],
        "prompt_template": parsed_meta["prompt_template"],
        "palette": parsed_meta["palette"],
        "recommended_aspects": parsed_meta["recommended_aspects"],
        "style_tags": parsed_meta["style_tags"],
        "samples": samples_for_storage,
        "created_at": (previous or {}).get("created_at") or now_iso,
        "updated_at": now_iso,
    }


def preset_changed(
    prev: dict[str, Any],
    cur: dict[str, Any],
) -> bool:
    fields = (
        "title",
        "category",
        "mood",
        "prompt_template",
        "palette",
        "recommended_aspects",
        "style_tags",
    )
    if any(prev.get(field) != cur.get(field) for field in fields):
        return True
    raw_prev = prev.get("samples")
    raw_cur = cur.get("samples")
    prev_samples = raw_prev if isinstance(raw_prev, list) else []
    cur_samples = raw_cur if isinstance(raw_cur, list) else []
    if len(prev_samples) != len(cur_samples):
        return True
    compare_fields = (
        "name",
        "sha256",
        "thumb_sha256",
        "github_sha",
        "github_thumb_sha",
    )
    return any(
        not isinstance(left, dict)
        or not isinstance(right, dict)
        or any(left.get(field) != right.get(field) for field in compare_fields)
        for left, right in zip(prev_samples, cur_samples)
    )


def publish_local_bootstrap_sync(
    runtime: Any,
    items: list[dict[str, Any]],
) -> bool:
    with runtime._poster_style_sync_file_lock(runtime._library_sync_lock_path()):
        state = runtime._read_json_file(
            runtime._library_sync_state_path(),
            runtime._default_sync_state(),
        )
        owner = runtime._sync_lease_owner(state)
        if owner is not None and owner[1] > runtime._now():
            return False
        index = runtime._load_global_preset_index()
        if index.get("preset_items"):
            return False
        index["preset_items"] = items
        runtime._save_global_preset_index(index)
        return True


async def bootstrap_local_presets_if_empty(runtime: Any) -> None:
    index = await asyncio.to_thread(runtime._load_global_preset_index)
    if index.get("preset_items"):
        return
    local_root = runtime._local_presets_root()
    if local_root is None:
        return
    scanned = await asyncio.to_thread(runtime._scan_local_presets, local_root)
    if not scanned:
        return
    items = [
        runtime._build_preset_entry(
            parsed_meta=parsed,
            samples_for_storage=[],
            previous=None,
        )
        for parsed in scanned
    ]
    async with runtime._SYNC_LOCK:
        published = await asyncio.to_thread(
            runtime._publish_local_bootstrap_sync,
            items,
        )
    if published:
        runtime.logger.info(
            "poster style: bootstrapped %d presets from local assets",
            len(items),
        )
