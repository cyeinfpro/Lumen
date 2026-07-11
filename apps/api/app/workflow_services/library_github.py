"""Bounded GitHub contents traversal and download transport."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, unquote, urlsplit

import httpx

from ..routes._apparel_library import MODEL_LIBRARY_MAX_BINARY_BYTES
from .library_runtime import runtime as _runtime


class _ModelLibrarySyncLimitExceeded(ValueError):
    """A configured GitHub traversal or download budget was exceeded."""


async def _fetch_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> bytes:
    if max_bytes <= 0:
        raise _ModelLibrarySyncLimitExceeded("GitHub response byte budget exhausted")
    async with client.stream("GET", url, headers=headers) as resp:
        resp.raise_for_status()
        raw_length = resp.headers.get("content-length")
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise ValueError("invalid GitHub Content-Length") from exc
            if content_length < 0:
                raise ValueError("invalid GitHub Content-Length")
            if content_length > max_bytes:
                raise _ModelLibrarySyncLimitExceeded(
                    f"GitHub response exceeds {max_bytes} bytes"
                )
        payload = bytearray()
        async for chunk in resp.aiter_bytes():
            if len(payload) + len(chunk) > max_bytes:
                raise _ModelLibrarySyncLimitExceeded(
                    f"GitHub response exceeds {max_bytes} bytes"
                )
            payload.extend(chunk)
        return bytes(payload)


async def _fetch_github_download_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = MODEL_LIBRARY_MAX_BINARY_BYTES,
) -> bytes:
    runtime = _runtime()
    clean_url = runtime._validate_github_download_url(url)
    if clean_url is None:
        raise ValueError("preset file download URL must be a GitHub raw URL")
    return await runtime._fetch_bytes(client, clean_url, max_bytes=max_bytes)


def _github_api_child_url(base_url: str, child_name: str) -> str:
    if (
        not child_name
        or child_name in {".", ".."}
        or "/" in child_name
        or "\\" in child_name
    ):
        raise ValueError("invalid GitHub child path")
    prefix, _, query = base_url.partition("?")
    safe_child_name = quote(child_name, safe="")
    return (
        f"{prefix.rstrip('/')}/{safe_child_name}?{query}"
        if query
        else f"{prefix.rstrip('/')}/{safe_child_name}"
    )


def _decoded_url_path_segments(url: str) -> list[str]:
    return [unquote(part) for part in urlsplit(url).path.split("/") if part]


def _validate_github_contents_url(url: str) -> str:
    runtime = _runtime()
    clean = (url or "").strip()
    parts = urlsplit(clean)
    if (
        parts.scheme != "https"
        or (parts.hostname or "").lower() != runtime._GITHUB_API_HOST
        or parts.port is not None
        or parts.username is not None
        or parts.password is not None
    ):
        raise runtime._http(
            "invalid_preset_sync_url",
            "preset sync URL must be a GitHub contents API URL",
            503,
        )
    segments = runtime._decoded_url_path_segments(clean)
    if (
        len(segments) < 5
        or segments[0] != "repos"
        or segments[3] != "contents"
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise runtime._http(
            "invalid_preset_sync_url",
            "preset sync URL must be a GitHub contents API URL",
            503,
        )
    return clean


def _validate_github_download_url(url: str) -> str | None:
    runtime = _runtime()
    clean = (url or "").strip()
    parts = urlsplit(clean)
    if (
        parts.scheme != "https"
        or (parts.hostname or "").lower() not in runtime._GITHUB_RAW_HOSTS
        or parts.port is not None
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    segments = runtime._decoded_url_path_segments(clean)
    if len(segments) < 4 or any(segment in {"", ".", ".."} for segment in segments):
        return None
    return clean


def _github_content_entries(data: Any) -> list[Any]:
    if isinstance(data, dict):
        if data.get("type") != "file":
            raise ValueError("GitHub contents response must be an array")
        return [data]
    if not isinstance(data, list):
        raise ValueError("GitHub contents response must be an array")
    return data


async def _walk_github_contents(
    client: httpx.AsyncClient,
    contents_url: str,
    *,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    runtime = _runtime()
    root_url = runtime._validate_github_contents_url(contents_url)
    pending: list[tuple[str, int]] = [(root_url, 0)]
    queued: set[str] = {root_url}
    visited: set[str] = set()
    files: list[dict[str, Any]] = []
    directory_count = 0
    metadata_bytes = 0
    cursor = 0
    while cursor < len(pending):
        current_url, depth = pending[cursor]
        cursor += 1
        if current_url in visited:
            continue
        visited.add(current_url)
        directory_count += 1
        if directory_count > runtime.MODEL_LIBRARY_MAX_GITHUB_DIRECTORIES:
            raise _ModelLibrarySyncLimitExceeded(
                "GitHub contents directory limit exceeded"
            )
        remaining_metadata = (
            runtime.MODEL_LIBRARY_MAX_GITHUB_METADATA_BYTES - metadata_bytes
        )
        raw = await runtime._fetch_bytes(
            client,
            current_url,
            max_bytes=min(
                runtime.MODEL_LIBRARY_MAX_GITHUB_RESPONSE_BYTES,
                remaining_metadata,
            ),
            headers={"Accept": "application/vnd.github+json"},
        )
        metadata_bytes += len(raw)
        entries = _github_content_entries(json.loads(raw))
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            name = str(entry.get("name") or "")
            if entry_type == "dir":
                if depth >= runtime.MODEL_LIBRARY_MAX_GITHUB_DEPTH:
                    raise _ModelLibrarySyncLimitExceeded(
                        "GitHub contents depth limit exceeded"
                    )
                child_url = runtime._github_api_child_url(current_url, name)
                if child_url not in queued:
                    if len(queued) >= runtime.MODEL_LIBRARY_MAX_GITHUB_DIRECTORIES:
                        raise _ModelLibrarySyncLimitExceeded(
                            "GitHub contents directory limit exceeded"
                        )
                    queued.add(child_url)
                    pending.append((child_url, depth + 1))
            elif entry_type == "file":
                files.append(entry)
                if len(files) > runtime.MODEL_LIBRARY_MAX_GITHUB_FILES:
                    raise _ModelLibrarySyncLimitExceeded(
                        "GitHub contents file limit exceeded"
                    )
        if progress is not None:
            await progress()
    return files


def _metadata_from_github_file(entry: dict[str, Any]) -> dict[str, Any] | None:
    runtime = _runtime()
    path_value = str(entry.get("path") or entry.get("name") or "").strip()
    if not path_value:
        return None
    path = Path(path_value)
    suffix = path.suffix.lower()
    if suffix not in runtime.MODEL_LIBRARY_IMAGE_SUFFIXES:
        return None
    stem = path.stem
    if stem.endswith(".thumb"):
        return None
    download_url = str(entry.get("download_url") or "").strip()
    download_url = runtime._validate_github_download_url(download_url) or ""
    if not download_url:
        return None
    path_parts = [
        part for part in path.parts if part not in {"assets", "apparel-model-presets"}
    ]
    parent_dirs = path_parts[:-1]
    age_segment = next(
        (
            age
            for part in reversed(parent_dirs)
            if (age := runtime._age_segment_from_folder_name(part)) is not None
        ),
        "user_favorites",
    )
    gender_from_folder = next(
        (
            gender_value
            for part in reversed(parent_dirs)
            if (gender_value := runtime._gender_from_folder_name(part)) is not None
        ),
        None,
    )
    preset_id = runtime._preset_id_from_path(path_value)
    lower_name = path.name.lower()
    gender = gender_from_folder
    if any(token in lower_name for token in ("female", "woman", "girl")):
        gender = "female"
    elif any(token in lower_name for token in ("male", "man", "boy")):
        gender = "male"
    normalized_name = re.sub(r"[_\s]+", "-", lower_name)
    appearance = None
    for token, value in (
        ("southeast-asian", "southeast_asian"),
        ("south-asian", "south_asian"),
        ("east-asian", "east_asian"),
        ("middle-eastern", "middle_eastern"),
        ("middle-east", "middle_eastern"),
        ("european", "european"),
        ("latin", "latin"),
        ("african", "african"),
        ("asian", "asian"),
    ):
        if token in normalized_name:
            appearance = value
            break
    words = [
        part
        for part in re.split(r"[-_]+", path.stem)
        if part
        and not part.isdigit()
        and part not in {age_segment, "female", "male", "woman", "man"}
    ]
    return {
        "preset_id": preset_id,
        "version": 1,
        "title": runtime._title_from_preset_id(preset_id),
        "age_segment": age_segment,
        "library_folder": runtime._model_library_folder_for_age(
            age_segment,
            gender,
        ),
        "gender": gender,
        "appearance_direction": appearance,
        "style_tags": runtime._clean_style_tags(words[:6]),
        "image_path": path_value,
        "download_url": download_url,
        "sha": runtime._clean_optional_text(entry.get("sha"), max_len=80),
        "prompt_hint": runtime._title_from_preset_id(preset_id),
    }


def _github_entry_size(entry: dict[str, Any]) -> int | None:
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
