"""Apparel and poster-style library GitHub sync orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, TypeVar

from fastapi import HTTPException
from lumen_core.schemas import ApparelModelLibrarySyncOut

from .library_github import _ModelLibrarySyncLimitExceeded
from .library_lease import _ModelLibrarySyncLeaseLost
from .library_runtime import runtime as _runtime


_SyncResponseT = TypeVar("_SyncResponseT")


@dataclass(slots=True)
class _LibrarySyncOperation:
    """Shared lease, budget, counters, and error bookkeeping for library syncs."""

    lease_token: str
    renew_lease: Callable[[str], Awaitable[bool]]
    lease_lost_error: type[Exception]
    lease_lost_message: str
    lease_renew_seconds: float
    download_limit_for: Callable[[int, int | None], int]
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    downloaded_bytes: int = 0
    _last_lease_renewal: float = field(default_factory=time.monotonic)

    def record_error(self, message: str) -> None:
        if len(self.errors) < 20:
            self.errors.append(message[:300])

    async def heartbeat(self, *, force: bool = False) -> None:
        now_mono = time.monotonic()
        if not force and now_mono - self._last_lease_renewal < self.lease_renew_seconds:
            return
        if not await self.renew_lease(self.lease_token):
            raise self.lease_lost_error(self.lease_lost_message)
        self._last_lease_renewal = time.monotonic()

    def download_limit(self, expected_size: int | None) -> int:
        return self.download_limit_for(self.downloaded_bytes, expected_size)

    def record_download(self, data: bytes) -> str:
        self.downloaded_bytes += len(data)
        return hashlib.sha256(data).hexdigest()

    def result(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": list(self.errors),
        }

    def failure_result(self, message: str) -> dict[str, Any]:
        failure_errors = list(self.errors[:19])
        short_message = message[:300]
        if not failure_errors or failure_errors[-1] != short_message:
            failure_errors.append(short_message)
        return {
            "added": self.added,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": failure_errors,
        }


async def _run_library_sync_operation(
    operation: _LibrarySyncOperation,
    *,
    build_index: Callable[[_LibrarySyncOperation], Awaitable[dict[str, Any]]],
    complete_sync: Callable[
        [str, dict[str, Any], dict[str, Any], datetime],
        Awaitable[None],
    ],
    fail_sync: Callable[..., Awaitable[bool]],
    now: Callable[[], datetime],
    success_response: Callable[[_LibrarySyncOperation, datetime], _SyncResponseT],
    map_error: Callable[[Exception, str], Exception | None],
) -> _SyncResponseT:
    """Execute one library sync and preserve its domain-specific error mapping."""

    try:
        index = await build_index(operation)
        await operation.heartbeat(force=True)
        completed_at = now()
        await complete_sync(
            operation.lease_token,
            index,
            operation.result(),
            completed_at,
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        await fail_sync(
            operation.lease_token,
            message=message,
            result=operation.failure_result(message),
        )
        mapped = map_error(exc, message)
        if mapped is None:
            raise
        raise mapped from exc
    return success_response(operation, completed_at)


def _group_poster_style_sync_files(
    runtime: Any,
    files: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_dir: dict[str, dict[str, Any]] = {}
    for entry in files:
        path = runtime._github_entry_path(entry)
        if path is None:
            continue
        parts = runtime._poster_relative_parts(path)
        if len(parts) < 2:
            continue
        directory_parts = parts[:-1]
        directory_key = "/".join(directory_parts)
        bucket = by_dir.setdefault(
            directory_key,
            {
                "meta": None,
                "samples": [],
                "thumbs": {},
                "directory_parts": directory_parts,
            },
        )
        file_name = path.name.lower()
        suffix = path.suffix.lower()
        if file_name == "meta.json":
            bucket["meta"] = entry
        elif suffix in runtime.POSTER_STYLE_IMAGE_SUFFIXES:
            stem = path.stem
            if stem.lower().endswith(".thumb"):
                base = stem[: -len(".thumb")]
                bucket["thumbs"][f"{base}{suffix}".lower()] = entry
            else:
                bucket["samples"].append(entry)
    return by_dir


def _poster_style_category_hint(runtime: Any, bucket: dict[str, Any]) -> str | None:
    directory_parts = bucket.get("directory_parts") or []
    return next(
        (
            category
            for part in reversed(directory_parts)
            if (category := runtime._category_from_folder_name(str(part))) is not None
        ),
        None,
    )


def _poster_style_previous_samples(
    previous: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    raw_samples = (previous or {}).get("samples")
    return raw_samples if isinstance(raw_samples, list) else []


def _matching_poster_style_sample(
    previous_samples: Iterable[dict[str, Any]],
    sample_name: str,
) -> dict[str, Any] | None:
    return next(
        (
            sample
            for sample in previous_samples
            if isinstance(sample, dict) and sample.get("name") == sample_name
        ),
        None,
    )


async def _materialize_poster_style_thumb(
    runtime: Any,
    *,
    client: Any,
    thumb_entry: Any,
    sample_path: Path,
    image_key: str,
    sample_sha: str,
    preset_id: str,
    version: int,
    previous_sample: dict[str, Any] | None,
    operation: _LibrarySyncOperation,
) -> tuple[str, str, str | None]:
    if not isinstance(thumb_entry, dict):
        return image_key, sample_sha, None

    thumb_url = (
        runtime._validate_github_download_url(
            str(thumb_entry.get("download_url") or "")
        )
        or ""
    )
    suffix = sample_path.suffix.lower() or ".webp"
    thumb_key = runtime._preset_thumb_storage_key(
        preset_id,
        version,
        sample_path.stem,
        suffix,
    )
    thumb_path = runtime._storage_path(thumb_key)
    github_thumb_sha = runtime._clean_optional_text(
        thumb_entry.get("sha"),
        max_len=80,
    )
    can_reuse_thumb = bool(
        previous_sample
        and thumb_url
        and github_thumb_sha
        and previous_sample.get("github_thumb_sha") == github_thumb_sha
        and previous_sample.get("thumb_sha256")
        and await asyncio.to_thread(thumb_path.is_file)
    )
    if can_reuse_thumb and previous_sample is not None:
        return thumb_key, str(previous_sample["thumb_sha256"]), github_thumb_sha
    if not thumb_url:
        operation.record_error(
            f"{preset_id}: thumb fallback to original: invalid download URL"
        )
        return image_key, sample_sha, None

    try:
        thumb_data = await runtime._fetch_github_download_bytes(
            client,
            thumb_url,
            max_bytes=operation.download_limit(runtime._github_entry_size(thumb_entry)),
            progress=operation.heartbeat,
        )
    except (
        runtime._PosterStyleSyncLimitExceeded,
        runtime._PosterStyleSyncLeaseLost,
        HTTPException,
    ):
        raise
    except Exception as exc:  # noqa: BLE001
        operation.record_error(f"{preset_id}: thumb fallback to original: {exc!r}")
        return image_key, sample_sha, None

    thumb_sha = operation.record_download(thumb_data)
    if (
        not await asyncio.to_thread(thumb_path.is_file)
        or not previous_sample
        or previous_sample.get("thumb_sha256") != thumb_sha
    ):
        await asyncio.to_thread(runtime._write_bytes_replace, thumb_path, thumb_data)
    return thumb_key, thumb_sha, github_thumb_sha


async def _materialize_poster_style_sample(
    runtime: Any,
    *,
    client: Any,
    bucket: dict[str, Any],
    sample_entry: dict[str, Any],
    preset_id: str,
    version: int,
    previous_samples: list[dict[str, Any]],
    operation: _LibrarySyncOperation,
) -> dict[str, Any] | None:
    sample_path = runtime._github_entry_path(sample_entry)
    if sample_path is None:
        operation.record_error(f"{preset_id}: invalid sample path")
        return None
    sample_name = sample_path.name
    sample_url = (
        runtime._validate_github_download_url(
            str(sample_entry.get("download_url") or "")
        )
        or ""
    )
    if not sample_url:
        operation.record_error(
            f"{preset_id}: sample {sample_name} has invalid download URL"
        )
        return None

    previous_sample = _matching_poster_style_sample(previous_samples, sample_name)
    github_sha = runtime._clean_optional_text(sample_entry.get("sha"), max_len=80)
    image_key = runtime._preset_storage_key(preset_id, version, sample_name)
    image_path = runtime._storage_path(image_key)
    can_reuse_image = bool(
        previous_sample
        and github_sha
        and previous_sample.get("github_sha") == github_sha
        and previous_sample.get("sha256")
        and await asyncio.to_thread(image_path.is_file)
    )
    if can_reuse_image and previous_sample is not None:
        sample_sha = str(previous_sample["sha256"])
    else:
        try:
            data = await runtime._fetch_github_download_bytes(
                client,
                sample_url,
                max_bytes=operation.download_limit(
                    runtime._github_entry_size(sample_entry)
                ),
                progress=operation.heartbeat,
            )
        except (
            runtime._PosterStyleSyncLimitExceeded,
            runtime._PosterStyleSyncLeaseLost,
            HTTPException,
        ):
            raise
        except Exception as exc:  # noqa: BLE001
            operation.record_error(
                f"{preset_id}: sample {sample_name} download failed: {exc!r}"
            )
            return None
        sample_sha = operation.record_download(data)
        if (
            not await asyncio.to_thread(image_path.is_file)
            or not previous_sample
            or previous_sample.get("sha256") != sample_sha
        ):
            await asyncio.to_thread(runtime._write_bytes_replace, image_path, data)

    thumb_key, thumb_sha, github_thumb_sha = await _materialize_poster_style_thumb(
        runtime,
        client=client,
        thumb_entry=bucket["thumbs"].get(sample_name.lower()),
        sample_path=sample_path,
        image_key=image_key,
        sample_sha=sample_sha,
        preset_id=preset_id,
        version=version,
        previous_sample=previous_sample,
        operation=operation,
    )
    return {
        "name": sample_name,
        "image_storage_key": image_key,
        "thumb_storage_key": thumb_key,
        "sha256": sample_sha,
        "thumb_sha256": thumb_sha,
        "github_sha": github_sha,
        "github_thumb_sha": github_thumb_sha,
    }


async def _materialize_poster_style_samples(
    runtime: Any,
    *,
    client: Any,
    bucket: dict[str, Any],
    preset_id: str,
    version: int,
    previous: dict[str, Any] | None,
    operation: _LibrarySyncOperation,
) -> list[dict[str, Any]]:
    sample_entries = sorted(
        bucket["samples"],
        key=lambda entry: str(entry.get("path") or entry.get("name") or "").lower(),
    )
    previous_samples = _poster_style_previous_samples(previous)
    samples: list[dict[str, Any]] = []
    for sample_entry in sample_entries[: runtime.POSTER_STYLE_MAX_SAMPLES]:
        await operation.heartbeat()
        sample = await _materialize_poster_style_sample(
            runtime,
            client=client,
            bucket=bucket,
            sample_entry=sample_entry,
            preset_id=preset_id,
            version=version,
            previous_samples=previous_samples,
            operation=operation,
        )
        if sample is not None:
            samples.append(sample)
    return samples


async def _sync_poster_style_directory(
    runtime: Any,
    *,
    client: Any,
    directory_key: str,
    bucket: dict[str, Any],
    next_items: dict[str, dict[str, Any]],
    seen_sync_keys: set[tuple[str, int]],
    operation: _LibrarySyncOperation,
) -> tuple[str, dict[str, Any]] | None:
    await operation.heartbeat()
    meta_entry = bucket["meta"]
    if not isinstance(meta_entry, dict):
        return None
    meta = await runtime._fetch_meta_json(
        client,
        meta_entry,
        progress=operation.heartbeat,
    )
    await operation.heartbeat()
    if meta is None:
        operation.skipped += 1
        operation.record_error(f"{directory_key}: meta.json missing or invalid")
        return None

    parsed = runtime._metadata_from_meta_json(
        meta,
        category_hint=_poster_style_category_hint(runtime, bucket),
    )
    if parsed is None:
        operation.skipped += 1
        operation.record_error(f"{directory_key}: meta.json has no preset_id")
        return None

    preset_id = parsed["preset_id"]
    version = int(parsed["version"])
    sync_key = (preset_id, version)
    if sync_key in seen_sync_keys:
        operation.skipped += 1
        operation.record_error(
            f"{directory_key}: duplicate preset_id/version {preset_id}@{version}"
        )
        return None
    seen_sync_keys.add(sync_key)

    item_id = runtime._preset_item_id(preset_id, version)
    previous = next_items.get(item_id)
    samples = await _materialize_poster_style_samples(
        runtime,
        client=client,
        bucket=bucket,
        preset_id=preset_id,
        version=version,
        previous=previous,
        operation=operation,
    )
    item = runtime._build_preset_entry(
        parsed_meta=parsed,
        samples_for_storage=samples,
        previous=previous,
    )
    if previous is None:
        operation.added += 1
    elif runtime._preset_changed(previous, item):
        operation.updated += 1
    else:
        operation.skipped += 1
    return item_id, item


async def _build_poster_style_sync_index(
    runtime: Any,
    contents_url: str,
    *,
    proxy_url: str | None,
    operation: _LibrarySyncOperation,
) -> dict[str, Any]:
    async with runtime.httpx.AsyncClient(
        **runtime._http_client_kwargs(proxy_url)
    ) as client:
        files = await runtime._walk_github_contents(
            client,
            contents_url,
            progress=operation.heartbeat,
        )
        by_dir = _group_poster_style_sync_files(runtime, files)
        index = await asyncio.to_thread(runtime._load_global_preset_index)
        existing_by_id = {
            str(item.get("id") or ""): dict(item)
            for item in index.get("preset_items", [])
            if isinstance(item, dict)
        }
        next_items: dict[str, dict[str, Any]] = dict(existing_by_id)
        seen_sync_keys: set[tuple[str, int]] = set()
        for directory_key, bucket in by_dir.items():
            synced = await _sync_poster_style_directory(
                runtime,
                client=client,
                directory_key=directory_key,
                bucket=bucket,
                next_items=next_items,
                seen_sync_keys=seen_sync_keys,
                operation=operation,
            )
            if synced is not None:
                item_id, item = synced
                next_items[item_id] = item

    index["preset_items"] = sorted(
        next_items.values(),
        key=lambda item: (
            runtime._normalize_category(item.get("category")),
            str(item.get("preset_id") or ""),
            int(item.get("version") or 0),
        ),
    )
    return index


def _poster_style_sync_success_response(
    runtime: Any,
    operation: _LibrarySyncOperation,
    completed_at: datetime,
) -> Any:
    return runtime.PosterStyleSyncOut(
        status="ok",
        added=operation.added,
        updated=operation.updated,
        skipped=operation.skipped,
        errors=list(operation.errors),
        last_success_at=completed_at,
        last_error=None,
    )


def _map_poster_style_sync_error(
    runtime: Any,
    exc: Exception,
    message: str,
) -> Exception | None:
    if isinstance(exc, HTTPException):
        return None
    if isinstance(exc, runtime._PosterStyleSyncLeaseLost):
        return runtime._http("preset_sync_conflict", message, 409)
    return runtime._http(
        "preset_sync_failed",
        message or "preset sync failed",
        502,
    )


async def _do_poster_style_sync(
    runtime: Any,
    contents_url: str,
    state: dict[str, Any],
    *,
    proxy_url: str | None = None,
    lease_token: str | None = None,
) -> Any:
    contents_url = runtime._validate_github_contents_url(contents_url)
    if lease_token is None:
        lease_token, state = await runtime._claim_library_sync_lease()
        if lease_token is None:
            return runtime._cached_sync_response(state)
    operation = _LibrarySyncOperation(
        lease_token=lease_token,
        renew_lease=runtime._renew_library_sync_lease,
        lease_lost_error=runtime._PosterStyleSyncLeaseLost,
        lease_lost_message="poster style sync lease was lost",
        lease_renew_seconds=runtime.POSTER_STYLE_SYNC_LEASE_RENEW_SECONDS,
        download_limit_for=lambda downloaded_bytes, expected_size: (
            runtime._sync_download_limit(
                downloaded_bytes=downloaded_bytes,
                expected_size=expected_size,
            )
        ),
    )
    return await _run_library_sync_operation(
        operation,
        build_index=lambda active_operation: _build_poster_style_sync_index(
            runtime,
            contents_url,
            proxy_url=proxy_url,
            operation=active_operation,
        ),
        complete_sync=runtime._complete_library_sync_lease,
        fail_sync=runtime._fail_library_sync_lease,
        now=runtime._now,
        success_response=lambda active_operation, completed_at: (
            _poster_style_sync_success_response(
                runtime,
                active_operation,
                completed_at,
            )
        ),
        map_error=lambda exc, message: _map_poster_style_sync_error(
            runtime,
            exc,
            message,
        ),
    )


_APPAREL_SYNC_COMPARE_FIELDS = (
    "title",
    "age_segment",
    "gender",
    "appearance_direction",
    "style_tags",
    "sha256",
    "thumb_sha256",
    "prompt_hint",
    "github_sha",
    "github_thumb_sha",
)


def _apparel_sync_download_limit(
    runtime: Any,
    downloaded_bytes: int,
    expected_size: int | None,
) -> int:
    remaining = runtime.MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES - downloaded_bytes
    if remaining <= 0:
        raise _ModelLibrarySyncLimitExceeded(
            "model library sync download budget exceeded"
        )
    if expected_size is not None:
        if expected_size > runtime.MODEL_LIBRARY_MAX_BINARY_BYTES:
            raise _ModelLibrarySyncLimitExceeded(
                "GitHub preset file exceeds the per-file byte limit"
            )
        if expected_size > remaining:
            raise _ModelLibrarySyncLimitExceeded(
                "model library sync download budget exceeded"
            )
    return min(runtime.MODEL_LIBRARY_MAX_BINARY_BYTES, remaining)


def _parse_apparel_sync_items(
    runtime: Any,
    files: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    parsed_items: list[dict[str, Any]] = []
    for entry in files:
        item = runtime._metadata_from_github_file(entry)
        if item is None:
            continue
        item["github_size"] = runtime._github_entry_size(entry)
        parsed_items.append(item)
    return parsed_items


def _index_apparel_sync_thumbs(
    runtime: Any,
    files: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    thumb_by_base: dict[str, dict[str, Any]] = {}
    for entry in files:
        path_value = str(entry.get("path") or entry.get("name") or "")
        path = Path(path_value)
        if path.suffix.lower() not in runtime.MODEL_LIBRARY_IMAGE_SUFFIXES:
            continue
        if not path.stem.endswith(".thumb"):
            continue
        base = str(path.with_name(f"{path.stem[: -len('.thumb')]}{path.suffix}"))
        thumb_by_base[base] = entry
    return thumb_by_base


async def _apparel_cached_binary_matches(
    runtime: Any,
    path: Path,
    expected_sha: Any,
) -> bool:
    try:
        cached_sha = await asyncio.to_thread(
            runtime._sha256_file_bounded,
            path,
            runtime.MODEL_LIBRARY_MAX_BINARY_BYTES,
        )
    except OSError:
        return False
    return cached_sha == expected_sha


async def _materialize_apparel_image(
    runtime: Any,
    *,
    client: Any,
    parsed: dict[str, Any],
    previous: dict[str, Any] | None,
    preset_id: str,
    version: int,
    operation: _LibrarySyncOperation,
) -> tuple[str, str, Any] | None:
    image_key = runtime._preset_storage_key(
        preset_id,
        version,
        str(parsed["image_path"]),
    )
    image_path = runtime._storage_path(image_key)
    github_sha = parsed.get("sha")
    can_reuse_image = bool(
        previous
        and github_sha
        and previous.get("github_sha") == github_sha
        and previous.get("sha256")
        and image_path.is_file()
    )
    if can_reuse_image and previous is not None:
        can_reuse_image = await _apparel_cached_binary_matches(
            runtime,
            image_path,
            previous.get("sha256"),
        )
    if can_reuse_image and previous is not None:
        return image_key, str(previous["sha256"]), github_sha

    try:
        data = await runtime._fetch_github_download_bytes(
            client,
            str(parsed["download_url"]),
            max_bytes=operation.download_limit(parsed.get("github_size")),
        )
    except (
        _ModelLibrarySyncLimitExceeded,
        _ModelLibrarySyncLeaseLost,
    ):
        raise
    except Exception as exc:  # noqa: BLE001
        operation.skipped += 1
        operation.record_error(f"{preset_id}: image download failed: {exc!r}")
        return None

    actual_sha = operation.record_download(data)
    if not image_path.is_file() or not previous or previous.get("sha256") != actual_sha:
        await asyncio.to_thread(runtime._write_bytes_replace, image_path, data)
    return image_key, actual_sha, github_sha


async def _materialize_apparel_thumb(
    runtime: Any,
    *,
    client: Any,
    thumb_entry: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    preset_id: str,
    image_key: str,
    actual_sha: str,
    operation: _LibrarySyncOperation,
) -> tuple[str, str, str | None]:
    thumb_key = runtime._preset_thumb_storage_key(
        preset_id,
        str(thumb_entry.get("path")) if thumb_entry else None,
        image_key,
    )
    if not thumb_entry:
        return thumb_key, actual_sha, None

    thumb_path = runtime._storage_path(thumb_key)
    thumb_url = str(thumb_entry.get("download_url") or "")
    github_thumb_sha = runtime._clean_optional_text(
        thumb_entry.get("sha"),
        max_len=80,
    )
    can_reuse_thumb = bool(
        previous
        and github_thumb_sha
        and previous.get("github_thumb_sha") == github_thumb_sha
        and previous.get("thumb_sha256")
        and thumb_path.is_file()
    )
    if can_reuse_thumb and previous is not None:
        can_reuse_thumb = await _apparel_cached_binary_matches(
            runtime,
            thumb_path,
            previous.get("thumb_sha256"),
        )
    if can_reuse_thumb and previous is not None:
        return thumb_key, str(previous["thumb_sha256"]), github_thumb_sha

    try:
        thumb_data = await runtime._fetch_github_download_bytes(
            client,
            thumb_url,
            max_bytes=operation.download_limit(runtime._github_entry_size(thumb_entry)),
        )
    except (
        _ModelLibrarySyncLimitExceeded,
        _ModelLibrarySyncLeaseLost,
    ):
        raise
    except Exception as exc:  # noqa: BLE001
        operation.record_error(f"{preset_id}: thumb fallback to original: {exc!r}")
        return image_key, actual_sha, None

    thumb_sha = operation.record_download(thumb_data)
    if (
        not thumb_path.is_file()
        or not previous
        or previous.get("thumb_sha256") != thumb_sha
    ):
        await asyncio.to_thread(runtime._write_bytes_replace, thumb_path, thumb_data)
    return thumb_key, thumb_sha, github_thumb_sha


def _build_apparel_sync_item(
    runtime: Any,
    *,
    parsed: dict[str, Any],
    previous: dict[str, Any] | None,
    item_id: str,
    image_key: str,
    actual_sha: str,
    github_sha: Any,
    thumb_entry: dict[str, Any] | None,
    thumb_key: str,
    thumb_sha: str,
    github_thumb_sha: str | None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "source": "preset",
        "preset_id": parsed["preset_id"],
        "version": int(parsed["version"]),
        "title": parsed["title"],
        "age_segment": parsed["age_segment"],
        "library_folder": parsed["library_folder"],
        "gender": parsed["gender"],
        "appearance_direction": parsed["appearance_direction"],
        "style_tags": parsed["style_tags"],
        "image_storage_key": image_key,
        "thumb_storage_key": thumb_key,
        "sha256": actual_sha,
        "thumb_sha256": thumb_sha,
        "prompt_hint": parsed["prompt_hint"],
        "github_image_path": parsed["image_path"],
        "github_thumb_path": (str(thumb_entry.get("path")) if thumb_entry else None),
        "github_sha": github_sha,
        "github_thumb_sha": github_thumb_sha,
        "created_at": (previous or {}).get("created_at") or runtime._iso_now(),
        "updated_at": runtime._iso_now(),
    }


def _apparel_sync_item_changed(
    previous: dict[str, Any],
    item: dict[str, Any],
) -> bool:
    return {key: previous.get(key) for key in _APPAREL_SYNC_COMPARE_FIELDS} != {
        key: item.get(key) for key in _APPAREL_SYNC_COMPARE_FIELDS
    }


async def _sync_apparel_library_item(
    runtime: Any,
    *,
    client: Any,
    parsed: dict[str, Any],
    thumb_by_base: dict[str, dict[str, Any]],
    next_items: dict[str, dict[str, Any]],
    operation: _LibrarySyncOperation,
) -> tuple[str, dict[str, Any]] | None:
    await operation.heartbeat()
    preset_id = parsed["preset_id"]
    version = int(parsed["version"])
    item_id = f"preset:{preset_id}:v{version}"
    previous = next_items.get(item_id)
    materialized_image = await _materialize_apparel_image(
        runtime,
        client=client,
        parsed=parsed,
        previous=previous,
        preset_id=preset_id,
        version=version,
        operation=operation,
    )
    if materialized_image is None:
        return None
    image_key, actual_sha, github_sha = materialized_image

    thumb_entry = thumb_by_base.get(str(parsed["image_path"]))
    thumb_key, thumb_sha, github_thumb_sha = await _materialize_apparel_thumb(
        runtime,
        client=client,
        thumb_entry=thumb_entry,
        previous=previous,
        preset_id=preset_id,
        image_key=image_key,
        actual_sha=actual_sha,
        operation=operation,
    )
    item = _build_apparel_sync_item(
        runtime,
        parsed=parsed,
        previous=previous,
        item_id=item_id,
        image_key=image_key,
        actual_sha=actual_sha,
        github_sha=github_sha,
        thumb_entry=thumb_entry,
        thumb_key=thumb_key,
        thumb_sha=thumb_sha,
        github_thumb_sha=github_thumb_sha,
    )
    if previous is None:
        operation.added += 1
    elif _apparel_sync_item_changed(previous, item):
        operation.updated += 1
    else:
        operation.skipped += 1
    return item_id, item


async def _build_apparel_sync_index(
    runtime: Any,
    contents_url: str,
    *,
    proxy_url: str | None,
    operation: _LibrarySyncOperation,
) -> dict[str, Any]:
    async with runtime.httpx.AsyncClient(
        **runtime._model_library_http_client_kwargs(proxy_url)
    ) as client:
        files = await runtime._walk_github_contents(
            client,
            contents_url,
            progress=operation.heartbeat,
        )
        parsed_items = _parse_apparel_sync_items(runtime, files)
        thumb_by_base = _index_apparel_sync_thumbs(runtime, files)
        index = await asyncio.to_thread(runtime._load_global_library_index)
        existing_by_id = {
            str(item.get("id") or ""): dict(item)
            for item in index.get("preset_items", [])
            if isinstance(item, dict)
        }
        next_items = dict(existing_by_id)
        for parsed in parsed_items:
            synced = await _sync_apparel_library_item(
                runtime,
                client=client,
                parsed=parsed,
                thumb_by_base=thumb_by_base,
                next_items=next_items,
                operation=operation,
            )
            if synced is not None:
                item_id, item = synced
                next_items[item_id] = item

    index["preset_items"] = sorted(
        next_items.values(),
        key=lambda item: (
            runtime._normalize_age_segment(item.get("age_segment")),
            str(item.get("preset_id") or ""),
            int(item.get("version") or 0),
        ),
    )
    return index


def _apparel_sync_success_response(
    operation: _LibrarySyncOperation,
    completed_at: datetime,
) -> ApparelModelLibrarySyncOut:
    return ApparelModelLibrarySyncOut(
        status="ok",
        added=operation.added,
        updated=operation.updated,
        skipped=operation.skipped,
        errors=list(operation.errors),
        last_success_at=completed_at,
        last_error=None,
    )


def _map_apparel_sync_error(
    runtime: Any,
    exc: Exception,
    message: str,
) -> Exception | None:
    if isinstance(exc, HTTPException):
        return None
    if isinstance(exc, _ModelLibrarySyncLeaseLost):
        return runtime._http("preset_sync_conflict", message, 409)
    return runtime._http(
        "preset_sync_failed",
        message or "preset sync failed",
        502,
    )


async def _sync_library_presets_from_github_folder(
    contents_url: str,
    *,
    proxy_url: str | None = None,
) -> ApparelModelLibrarySyncOut:
    runtime = _runtime()
    if not contents_url:
        raise runtime._http(
            "sync_not_configured", "preset GitHub folder url is not configured", 503
        )
    contents_url = runtime._validate_github_contents_url(contents_url)
    lease_token, state = await runtime._claim_library_sync_lease()
    if lease_token is None:
        return runtime._cached_sync_response(state)
    # The process/file locks are already released here. GitHub I/O is guarded
    # only by the renewable lease marker so no long-lived lock is held.
    return await runtime._do_sync_library_presets(
        contents_url,
        state,
        proxy_url=proxy_url,
        lease_token=lease_token,
    )


async def _do_sync_library_presets(
    contents_url: str,
    state: dict[str, Any],
    *,
    proxy_url: str | None = None,
    lease_token: str | None = None,
) -> ApparelModelLibrarySyncOut:
    runtime = _runtime()
    contents_url = runtime._validate_github_contents_url(contents_url)
    if lease_token is None:
        lease_token, state = await runtime._claim_library_sync_lease()
        if lease_token is None:
            return runtime._cached_sync_response(state)
    operation = _LibrarySyncOperation(
        lease_token=lease_token,
        renew_lease=runtime._renew_library_sync_lease,
        lease_lost_error=_ModelLibrarySyncLeaseLost,
        lease_lost_message="model library sync lease was lost",
        lease_renew_seconds=runtime.MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS,
        download_limit_for=lambda downloaded_bytes, expected_size: (
            _apparel_sync_download_limit(
                runtime,
                downloaded_bytes,
                expected_size,
            )
        ),
    )
    return await _run_library_sync_operation(
        operation,
        build_index=lambda active_operation: _build_apparel_sync_index(
            runtime,
            contents_url,
            proxy_url=proxy_url,
            operation=active_operation,
        ),
        complete_sync=runtime._complete_library_sync_lease,
        fail_sync=runtime._fail_library_sync_lease,
        now=runtime._now,
        success_response=_apparel_sync_success_response,
        map_error=lambda exc, message: _map_apparel_sync_error(
            runtime,
            exc,
            message,
        ),
    )
