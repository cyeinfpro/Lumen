"""Application composition for apparel model-library synchronization."""

from __future__ import annotations

from typing import Any

import httpx
from lumen_core.schemas import ApparelModelLibrarySyncOut

from ..domain.apparel_library import (
    MODEL_LIBRARY_IMAGE_SUFFIXES,
    MODEL_LIBRARY_MAX_BINARY_BYTES,
    MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES,
    MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS,
)
from ..domain.apparel_library import (
    normalize_age_segment as _normalize_age_segment,
)
from ..ports.runtime_state import AsyncLockPort
from .library_github import (
    fetch_github_download_bytes as _fetch_github_download_bytes,
)
from .library_github import github_entry_size as _github_entry_size
from .library_github import metadata_from_github_file as _metadata_from_github_file
from .library_github import (
    validate_github_contents_url as _validate_github_contents_url,
)
from .library_github import walk_github_contents as _walk_github_contents
from .library_items import (
    model_library_http_client_kwargs as _model_library_http_client_kwargs,
)
from .library_lease import cached_sync_response as _cached_sync_response
from .library_lease import claim_library_sync_lease as _claim_library_sync_lease
from .library_lease import complete_library_sync_lease as _complete_library_sync_lease
from .library_lease import fail_library_sync_lease as _fail_library_sync_lease
from .library_lease import renew_library_sync_lease as _renew_library_sync_lease
from .library_storage import load_global_library_index as _load_global_library_index
from .library_storage import preset_storage_key as _preset_storage_key
from .library_storage import preset_thumb_storage_key as _preset_thumb_storage_key
from .library_storage import sha256_file_bounded as _sha256_file_bounded
from .library_storage import write_bytes_replace as _write_bytes_replace
from .library_sync_operation import (
    do_sync_library_presets as _do_sync_library_presets_impl,
)
from .library_sync_operation import (
    sync_library_presets_from_github_folder as _sync_library_presets_impl,
)
from .serialization import clean_optional_text as _clean_optional_text
from .serialization import http as _http
from .serialization import iso_now as _iso_now
from .serialization import now as _now
from .serialization import storage_path as _storage_path


class ApparelLibrarySyncDependencies:
    """Dependencies consumed by the shared sync operation engine."""

    MODEL_LIBRARY_IMAGE_SUFFIXES = MODEL_LIBRARY_IMAGE_SUFFIXES
    MODEL_LIBRARY_MAX_BINARY_BYTES = MODEL_LIBRARY_MAX_BINARY_BYTES
    MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES = MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES
    MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS = MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS
    httpx = httpx

    _cached_sync_response = staticmethod(_cached_sync_response)
    _clean_optional_text = staticmethod(_clean_optional_text)
    _fetch_github_download_bytes = staticmethod(_fetch_github_download_bytes)
    _github_entry_size = staticmethod(_github_entry_size)
    _http = staticmethod(_http)
    _iso_now = staticmethod(_iso_now)
    _load_global_library_index = staticmethod(_load_global_library_index)
    _metadata_from_github_file = staticmethod(_metadata_from_github_file)
    _model_library_http_client_kwargs = staticmethod(_model_library_http_client_kwargs)
    _normalize_age_segment = staticmethod(_normalize_age_segment)
    _now = staticmethod(_now)
    _preset_storage_key = staticmethod(_preset_storage_key)
    _preset_thumb_storage_key = staticmethod(_preset_thumb_storage_key)
    _sha256_file_bounded = staticmethod(_sha256_file_bounded)
    _storage_path = staticmethod(_storage_path)
    _validate_github_contents_url = staticmethod(_validate_github_contents_url)
    _walk_github_contents = staticmethod(_walk_github_contents)
    _write_bytes_replace = staticmethod(_write_bytes_replace)

    def __init__(self, sync_lock: AsyncLockPort) -> None:
        self.sync_lock = sync_lock

    async def _claim_library_sync_lease(
        self,
    ) -> tuple[str | None, dict[str, Any]]:
        return await _claim_library_sync_lease(self.sync_lock)

    async def _renew_library_sync_lease(self, token: str) -> bool:
        return await _renew_library_sync_lease(self.sync_lock, token)

    async def _complete_library_sync_lease(
        self,
        token: str,
        index: dict[str, Any],
        result: dict[str, Any],
        completed_at: Any,
    ) -> None:
        await _complete_library_sync_lease(
            self.sync_lock,
            token,
            index,
            result,
            completed_at,
        )

    async def _fail_library_sync_lease(
        self,
        token: str,
        *,
        message: str,
        result: dict[str, Any],
    ) -> bool:
        return await _fail_library_sync_lease(
            self.sync_lock,
            token,
            message=message,
            result=result,
        )

    async def _do_sync_library_presets(
        self,
        contents_url: str,
        state: dict[str, Any],
        *,
        proxy_url: str | None = None,
        lease_token: str | None = None,
    ) -> ApparelModelLibrarySyncOut:
        return await _do_sync_library_presets_impl(
            self,
            contents_url,
            state,
            proxy_url=proxy_url,
            lease_token=lease_token,
        )


async def sync_library_presets_from_github_folder(
    contents_url: str,
    *,
    dependencies: ApparelLibrarySyncDependencies,
    proxy_url: str | None = None,
) -> ApparelModelLibrarySyncOut:
    return await _sync_library_presets_impl(
        dependencies,
        contents_url,
        proxy_url=proxy_url,
    )


__all__ = [
    "ApparelLibrarySyncDependencies",
    "sync_library_presets_from_github_folder",
]
