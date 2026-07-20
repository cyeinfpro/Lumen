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
import sys
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
    _SYNC_LOCK,
    _age_segment_from_folder_name,
    _gender_from_folder_name,
    _library_item_url,
    _model_library_folder_for_age,
    _model_library_sync_file_lock,
    _normalize_age_segment,
    _normalize_appearance,
    _normalize_model_gender,
    _preset_id_from_path,
    _title_from_preset_id,
)
from .facade import FacadeRuntime
from .library_github import (
    _ModelLibrarySyncLimitExceeded,
    _decoded_url_path_segments,
    _fetch_bytes,
    _fetch_github_download_bytes,
    _github_api_child_url,
    _github_entry_size,
    _metadata_from_github_file,
    _validate_github_contents_url,
    _validate_github_download_url,
    _walk_github_contents,
)
from .library_items import (
    _can_sync_library,
    _combined_library_items,
    _ensure_legacy_user_library_migrated,
    _filter_library_items,
    _find_library_item,
    _github_contents_url,
    _legacy_library_item_insert_values,
    _load_user_hidden_preset_ids,
    _load_user_library_items,
    _model_library_http_client_kwargs,
    _model_library_item_out,
    _model_library_row_to_dict,
    _resolve_model_library_sync_proxy,
    _sync_mode,
    _sync_state_out,
)
from .library_lease import (
    _ModelLibrarySyncLeaseLost,
    _cached_sync_response,
    _claim_library_sync_lease,
    _claim_library_sync_lease_sync,
    _complete_library_sync_lease,
    _complete_library_sync_lease_sync,
    _fail_library_sync_lease,
    _fail_library_sync_lease_sync,
    _renew_library_sync_lease,
    _renew_library_sync_lease_sync,
    _sync_lease_owner,
)
from .library_materialization import (
    _add_user_library_item,
    _create_user_image_from_preset,
    _image_url,
    _model_library_download_filename,
    _model_library_image_metadata_from_fields,
    _owned_image,
)
from .library_runtime import FACADE_RUNTIME, runtime as _runtime
from .library_storage import (
    _default_library_index,
    _default_sync_state,
    _default_user_library_index,
    _fsync_dir,
    _guess_mime,
    _hide_preset_in_legacy_user_library_index,
    _library_binary_response,
    _library_index_path,
    _library_root,
    _library_sync_lock_path,
    _library_sync_state_path,
    _library_user_index_path,
    _load_global_library_index,
    _load_user_library_index,
    _open_library_storage_file,
    _preset_storage_key,
    _preset_thumb_storage_key,
    _read_file_bytes_bounded,
    _read_json_file,
    _remove_user_library_item_from_legacy_index,
    _save_global_library_index,
    _save_sync_state,
    _save_user_library_index,
    _sha256_file_bounded,
    _stream_file,
    _write_bytes_replace,
    _write_json_atomic,
)
from .library_sync_operation import (
    _do_sync_library_presets,
    _sync_library_presets_from_github_folder,
)
from .serialization import (
    _clean_optional_text,
    _clean_string_list,
    _clean_style_tags,
    _dedupe_nonempty,
    _dict_or_empty,
    _http,
    _iso_now,
    _now,
    _safe_datetime,
    _storage_path,
)


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
