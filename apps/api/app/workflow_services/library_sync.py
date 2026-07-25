"""Apparel model library compatibility facade."""

# This module intentionally re-exports dependencies and private callables used by
# the historical routes.workflows facade and its monkeypatch-based tests.
# ruff: noqa: F401

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, cast
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.constants import ImageSource, ImageVisibility
from lumen_core.model_image_metadata import (
    build_model_image_metadata,
    model_image_filename,
)
from lumen_core.models import (
    Image,
    ModelLibraryHiddenPreset,
    ModelLibraryItem,
    User,
    new_uuid7,
)
from lumen_core.providers import (
    ProviderProxyDefinition,
    parse_proxy_json,
    resolve_provider_proxy_url,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    ApparelModelLibraryItemOut,
    ApparelModelLibrarySyncOut,
    ApparelModelLibrarySyncStateOut,
    ModelAgeSegment,
)

from ..config import settings
from ..runtime_settings import get_setting
from ..workflow_domain.apparel_library import (
    MODEL_LIBRARY_FETCH_TIMEOUT_SECONDS,
    MODEL_LIBRARY_IMAGE_SUFFIXES,
    MODEL_LIBRARY_MAX_BINARY_BYTES,
    MODEL_LIBRARY_MAX_GITHUB_DEPTH,
    MODEL_LIBRARY_MAX_GITHUB_DIRECTORIES,
    MODEL_LIBRARY_MAX_GITHUB_FILES,
    MODEL_LIBRARY_MAX_GITHUB_METADATA_BYTES,
    MODEL_LIBRARY_MAX_GITHUB_RESPONSE_BYTES,
    MODEL_LIBRARY_MAX_INDEX_BYTES,
    MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES,
    MODEL_LIBRARY_SCHEMA_VERSION,
    MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS,
    MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS,
    MODEL_LIBRARY_SYNC_LEASE_SECONDS,
    MODEL_LIBRARY_SYNC_MODES,
    MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS,
)
from ..workflow_domain.apparel_library import SYNC_LOCK as _SYNC_LOCK  # noqa: F401
from ..workflow_domain.apparel_library import (
    age_segment_from_folder_name as _age_segment_from_folder_name,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    gender_from_folder_name as _gender_from_folder_name,
)  # noqa: F401
from ..workflow_domain.apparel_library import library_item_url as _library_item_url  # noqa: F401
from ..workflow_domain.apparel_library import (
    model_library_folder_for_age as _model_library_folder_for_age,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    model_library_sync_file_lock as _model_library_sync_file_lock,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    normalize_age_segment as _normalize_age_segment,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    normalize_appearance as _normalize_appearance,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    normalize_model_gender as _normalize_model_gender,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    preset_id_from_path as _preset_id_from_path,
)  # noqa: F401
from ..workflow_domain.apparel_library import (
    title_from_preset_id as _title_from_preset_id,
)  # noqa: F401
from .library_github import (
    ModelLibrarySyncLimitExceeded as _ModelLibrarySyncLimitExceeded,
)  # noqa: F401
from .library_github import decoded_url_path_segments as _decoded_url_path_segments  # noqa: F401
from .library_github import fetch_bytes as _fetch_bytes  # noqa: F401
from .library_github import fetch_github_download_bytes as _fetch_github_download_bytes  # noqa: F401
from .library_github import github_api_child_url as _github_api_child_url  # noqa: F401
from .library_github import github_entry_size as _github_entry_size  # noqa: F401
from .library_github import metadata_from_github_file as _metadata_from_github_file  # noqa: F401
from .library_github import (
    validate_github_contents_url as _validate_github_contents_url,
)  # noqa: F401
from .library_github import (
    validate_github_download_url as _validate_github_download_url,
)  # noqa: F401
from .library_github import walk_github_contents as _walk_github_contents  # noqa: F401
from .library_items import can_sync_library as _can_sync_library  # noqa: F401
from .library_items import combined_library_items as _combined_library_items  # noqa: F401
from .library_items import (
    ensure_legacy_user_library_migrated as _ensure_legacy_user_library_migrated,
)  # noqa: F401
from .library_items import filter_library_items as _filter_library_items  # noqa: F401
from .library_items import find_library_item as _find_library_item  # noqa: F401
from .library_items import github_contents_url as _github_contents_url  # noqa: F401
from .library_items import (
    legacy_library_item_insert_values as _legacy_library_item_insert_values,
)  # noqa: F401
from .library_items import load_user_hidden_preset_ids as _load_user_hidden_preset_ids  # noqa: F401
from .library_items import load_user_library_items as _load_user_library_items  # noqa: F401
from .library_items import (
    model_library_http_client_kwargs as _model_library_http_client_kwargs,
)  # noqa: F401
from .library_items import model_library_item_out as _model_library_item_out  # noqa: F401
from .library_items import model_library_row_to_dict as _model_library_row_to_dict  # noqa: F401
from .library_items import (
    resolve_model_library_sync_proxy as _resolve_model_library_sync_proxy,
)  # noqa: F401
from .library_items import sync_mode as _sync_mode  # noqa: F401
from .library_items import sync_state_out as _sync_state_out  # noqa: F401
from .library_lease import ModelLibrarySyncLeaseLost as _ModelLibrarySyncLeaseLost  # noqa: F401
from .library_lease import cached_sync_response as _cached_sync_response  # noqa: F401
from .library_lease import claim_library_sync_lease as _claim_library_sync_lease  # noqa: F401
from .library_lease import (
    claim_library_sync_lease_sync as _claim_library_sync_lease_sync,
)  # noqa: F401
from .library_lease import complete_library_sync_lease as _complete_library_sync_lease  # noqa: F401
from .library_lease import (
    complete_library_sync_lease_sync as _complete_library_sync_lease_sync,
)  # noqa: F401
from .library_lease import fail_library_sync_lease as _fail_library_sync_lease  # noqa: F401
from .library_lease import fail_library_sync_lease_sync as _fail_library_sync_lease_sync  # noqa: F401
from .library_lease import renew_library_sync_lease as _renew_library_sync_lease  # noqa: F401
from .library_lease import (
    renew_library_sync_lease_sync as _renew_library_sync_lease_sync,
)  # noqa: F401
from .library_lease import sync_lease_owner as _sync_lease_owner  # noqa: F401
from .library_materialization import add_user_library_item as _add_user_library_item  # noqa: F401
from .library_materialization import (
    create_user_image_from_preset as _create_user_image_from_preset,
)  # noqa: F401
from .library_materialization import image_url as _image_url  # noqa: F401
from .library_materialization import (
    model_library_download_filename as _model_library_download_filename,
)  # noqa: F401
from .library_materialization import (
    model_library_image_metadata_from_fields as _model_library_image_metadata_from_fields,
)  # noqa: F401
from .library_materialization import owned_image as _owned_image  # noqa: F401
from .library_storage import default_library_index as _default_library_index  # noqa: F401
from .library_storage import default_sync_state as _default_sync_state  # noqa: F401
from .library_storage import default_user_library_index as _default_user_library_index  # noqa: F401
from .library_storage import fsync_dir as _fsync_dir  # noqa: F401
from .library_storage import guess_mime as _guess_mime  # noqa: F401
from .library_storage import (
    hide_preset_in_legacy_user_library_index as _hide_preset_in_legacy_user_library_index,
)  # noqa: F401
from .library_storage import library_binary_response as _library_binary_response  # noqa: F401
from .library_storage import library_index_path as _library_index_path  # noqa: F401
from .library_storage import library_root as _library_root  # noqa: F401
from .library_storage import library_sync_lock_path as _library_sync_lock_path  # noqa: F401
from .library_storage import library_sync_state_path as _library_sync_state_path  # noqa: F401
from .library_storage import library_user_index_path as _library_user_index_path  # noqa: F401
from .library_storage import load_global_library_index as _load_global_library_index  # noqa: F401
from .library_storage import load_user_library_index as _load_user_library_index  # noqa: F401
from .library_storage import open_library_storage_file as _open_library_storage_file  # noqa: F401
from .library_storage import preset_storage_key as _preset_storage_key  # noqa: F401
from .library_storage import preset_thumb_storage_key as _preset_thumb_storage_key  # noqa: F401
from .library_storage import read_file_bytes_bounded as _read_file_bytes_bounded  # noqa: F401
from .library_storage import read_json_file as _read_json_file  # noqa: F401
from .library_storage import (
    remove_user_library_item_from_legacy_index as _remove_user_library_item_from_legacy_index,
)  # noqa: F401
from .library_storage import save_global_library_index as _save_global_library_index  # noqa: F401
from .library_storage import save_sync_state as _save_sync_state  # noqa: F401
from .library_storage import save_user_library_index as _save_user_library_index  # noqa: F401
from .library_storage import sha256_file_bounded as _sha256_file_bounded  # noqa: F401
from .library_storage import stream_file as _stream_file  # noqa: F401
from .library_storage import write_bytes_replace as _write_bytes_replace  # noqa: F401
from .library_storage import write_json_atomic as _write_json_atomic  # noqa: F401
from .library_sync_operation import (
    do_sync_library_presets as _do_sync_library_presets_impl,
)  # noqa: F401
from .library_sync_operation import (
    sync_library_presets_from_github_folder as _sync_library_presets_impl,
)  # noqa: F401
from .serialization import clean_optional_text as _clean_optional_text  # noqa: F401
from .serialization import clean_string_list as _clean_string_list  # noqa: F401
from .serialization import clean_style_tags as _clean_style_tags  # noqa: F401
from .serialization import dedupe_nonempty as _dedupe_nonempty  # noqa: F401
from .serialization import dict_or_empty as _dict_or_empty  # noqa: F401
from .serialization import http as _http  # noqa: F401
from .serialization import iso_now as _iso_now  # noqa: F401
from .serialization import now as _now  # noqa: F401
from .serialization import safe_datetime as _safe_datetime  # noqa: F401
from .serialization import storage_path as _storage_path  # noqa: F401


logger = logging.getLogger("app.routes.workflows")

MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY = "model_library.sync_use_proxy_pool"
MODEL_LIBRARY_SYNC_PROXY_NAME_KEY = "model_library.sync_proxy_name"
MODEL_LIBRARY_ROOT_KEY = "apparel-model-library"
_GITHUB_API_HOST = "api.github.com"
_GITHUB_RAW_HOSTS = frozenset(
    {
        "raw.githubusercontent.com",
        "media.githubusercontent.com",
    }
)


class _ApparelLibrarySyncDependencies:
    """Explicit dependencies consumed by the shared sync operation engine."""

    MODEL_LIBRARY_IMAGE_SUFFIXES = MODEL_LIBRARY_IMAGE_SUFFIXES
    MODEL_LIBRARY_MAX_BINARY_BYTES = MODEL_LIBRARY_MAX_BINARY_BYTES
    MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES = MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES
    MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS = MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS
    httpx = httpx

    _cached_sync_response = staticmethod(_cached_sync_response)
    _claim_library_sync_lease = staticmethod(_claim_library_sync_lease)
    _clean_optional_text = staticmethod(_clean_optional_text)
    _complete_library_sync_lease = staticmethod(_complete_library_sync_lease)
    _fail_library_sync_lease = staticmethod(_fail_library_sync_lease)
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
    _renew_library_sync_lease = staticmethod(_renew_library_sync_lease)
    _sha256_file_bounded = staticmethod(_sha256_file_bounded)
    _storage_path = staticmethod(_storage_path)
    _validate_github_contents_url = staticmethod(_validate_github_contents_url)
    _walk_github_contents = staticmethod(_walk_github_contents)
    _write_bytes_replace = staticmethod(_write_bytes_replace)

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


APPAREL_LIBRARY_SYNC_DEPENDENCIES = _ApparelLibrarySyncDependencies()


async def _sync_library_presets_from_github_folder(
    contents_url: str,
    *,
    proxy_url: str | None = None,
) -> ApparelModelLibrarySyncOut:
    return await _sync_library_presets_impl(
        APPAREL_LIBRARY_SYNC_DEPENDENCIES,
        contents_url,
        proxy_url=proxy_url,
    )


async def _do_sync_library_presets(
    contents_url: str,
    state: dict[str, Any],
    *,
    proxy_url: str | None = None,
    lease_token: str | None = None,
) -> ApparelModelLibrarySyncOut:
    return await _do_sync_library_presets_impl(
        APPAREL_LIBRARY_SYNC_DEPENDENCIES,
        contents_url,
        state,
        proxy_url=proxy_url,
        lease_token=lease_token,
    )


# Public workflow contracts.
add_user_library_item = _add_user_library_item
can_sync_library = _can_sync_library
combined_library_items = _combined_library_items
create_user_image_from_preset = _create_user_image_from_preset
ensure_legacy_user_library_migrated = _ensure_legacy_user_library_migrated
filter_library_items = _filter_library_items
find_library_item = _find_library_item
github_contents_url = _github_contents_url
hide_preset_in_legacy_user_library_index = _hide_preset_in_legacy_user_library_index
image_url = _image_url
library_binary_response = _library_binary_response
model_library_download_filename = _model_library_download_filename
model_library_item_out = _model_library_item_out
model_library_row_to_dict = _model_library_row_to_dict
owned_image = _owned_image
remove_user_library_item_from_legacy_index = _remove_user_library_item_from_legacy_index
resolve_model_library_sync_proxy = _resolve_model_library_sync_proxy
sync_library_presets_from_github_folder = _sync_library_presets_from_github_folder
sync_state_out = _sync_state_out


# Public compatibility contracts.
GITHUB_API_HOST = _GITHUB_API_HOST
GITHUB_RAW_HOSTS = _GITHUB_RAW_HOSTS
ModelLibrarySyncLeaseLost = _ModelLibrarySyncLeaseLost
ModelLibrarySyncLimitExceeded = _ModelLibrarySyncLimitExceeded
cached_sync_response = _cached_sync_response
claim_library_sync_lease = _claim_library_sync_lease
claim_library_sync_lease_sync = _claim_library_sync_lease_sync
complete_library_sync_lease = _complete_library_sync_lease
complete_library_sync_lease_sync = _complete_library_sync_lease_sync
decoded_url_path_segments = _decoded_url_path_segments
default_library_index = _default_library_index
default_sync_state = _default_sync_state
default_user_library_index = _default_user_library_index
do_sync_library_presets = _do_sync_library_presets
fail_library_sync_lease = _fail_library_sync_lease
fail_library_sync_lease_sync = _fail_library_sync_lease_sync
fetch_bytes = _fetch_bytes
fetch_github_download_bytes = _fetch_github_download_bytes
fsync_dir = _fsync_dir
github_api_child_url = _github_api_child_url
github_entry_size = _github_entry_size
guess_mime = _guess_mime
legacy_library_item_insert_values = _legacy_library_item_insert_values
library_index_path = _library_index_path
library_root = _library_root
library_sync_lock_path = _library_sync_lock_path
library_sync_state_path = _library_sync_state_path
library_user_index_path = _library_user_index_path
load_global_library_index = _load_global_library_index
load_user_hidden_preset_ids = _load_user_hidden_preset_ids
load_user_library_index = _load_user_library_index
load_user_library_items = _load_user_library_items
metadata_from_github_file = _metadata_from_github_file
model_library_http_client_kwargs = _model_library_http_client_kwargs
model_library_image_metadata_from_fields = _model_library_image_metadata_from_fields
open_library_storage_file = _open_library_storage_file
preset_storage_key = _preset_storage_key
preset_thumb_storage_key = _preset_thumb_storage_key
read_file_bytes_bounded = _read_file_bytes_bounded
read_json_file = _read_json_file
renew_library_sync_lease = _renew_library_sync_lease
renew_library_sync_lease_sync = _renew_library_sync_lease_sync
save_global_library_index = _save_global_library_index
save_sync_state = _save_sync_state
save_user_library_index = _save_user_library_index
sha256_file_bounded = _sha256_file_bounded
stream_file = _stream_file
sync_lease_owner = _sync_lease_owner
sync_mode = _sync_mode
validate_github_contents_url = _validate_github_contents_url
validate_github_download_url = _validate_github_download_url
walk_github_contents = _walk_github_contents
write_bytes_replace = _write_bytes_replace
write_json_atomic = _write_json_atomic
