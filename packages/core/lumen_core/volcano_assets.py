"""Shared Volcano Ark AIGC asset client and response normalization."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import httpx

from lumen_core.video_providers import VideoProviderDefinition

from .volcano_asset_quota import (
    VOLCANO_ASSET_CREATE_DEFAULT_COOLDOWN_MS,
    VOLCANO_ASSET_CREATE_INTERVAL_MS,
    VOLCANO_ASSET_CREATE_QPM,
    VOLCANO_ASSET_CREATE_SCHEDULE_TTL_SECONDS,
    VOLCANO_ASSET_CREATE_WINDOW_SECONDS,
    VOLCANO_ASSET_MAX_ASSETS,
    VOLCANO_ASSET_MAX_GROUPS,
    VOLCANO_ASSET_OPERATION_TTL_SECONDS,
    VOLCANO_ASSET_RESERVATION_TTL_SECONDS,
    VolcanoAssetCreateRateLimited,
    VolcanoAssetOperationOwnershipError,
    VolcanoAssetQuotaExceeded,
    VolcanoAssetQuotaKey,
    VolcanoAssetRedisUnavailable,
    acquire_volcano_create_rate_limit,
    compare_and_set_volcano_asset_operation,
    defer_volcano_create_rate_limit,
    release_volcano_asset_quota,
    release_volcano_create_rate_limit,
    reserve_volcano_asset_quota,
    volcano_asset_operation_key,
    volcano_asset_quota_key,
    volcano_asset_quota_scope,
    volcano_asset_rate_limit_cooldown_key,
    volcano_asset_rate_limit_key,
    volcano_asset_reservation_key,
)


VOLCANO_ASSET_VERSION = "2024-01-01"
VOLCANO_ASSET_SERVICE = "ark"
VOLCANO_ASSET_ACTIONS = frozenset(
    {
        "CreateAssetGroup",
        "ListAssetGroups",
        "GetAssetGroup",
        "UpdateAssetGroup",
        "DeleteAssetGroup",
        "CreateAsset",
        "ListAssets",
        "GetAsset",
        "UpdateAsset",
        "DeleteAsset",
    }
)
_SIGNED_HEADERS = "content-type;host;x-content-sha256;x-date"
_REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_REGION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_INTERNAL_REFERENCE_PATH_PREFIXES = (
    "/api/images/reference/",
    "/api/videos/reference/",
)
_INTERNAL_REFERENCE_PATH_RE = re.compile(
    r"^/api/(?P<collection>images|videos)/reference/"
    r"(?P<resource_id>[^/]+)/binary(?:/[^/]+)?/?$"
)
_SENSITIVE_ASSET_URL_QUERY_MARKERS = (
    "accesskey",
    "apikey",
    "authorization",
    "credential",
    "secret",
    "signature",
    "token",
)
_SAFE_ASSET_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class VolcanoAssetServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        *,
        details: dict[str, Any] | None = None,
        retry_after_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.retry_after_ms = retry_after_ms
        self.headers = headers


def _http(
    code: str,
    message: str,
    status_code: int,
    *,
    details: dict[str, Any] | None = None,
    retry_after_ms: int | None = None,
    headers: dict[str, str] | None = None,
) -> VolcanoAssetServiceError:
    return VolcanoAssetServiceError(
        code,
        message,
        status_code,
        details=details,
        retry_after_ms=retry_after_ms,
        headers=headers,
    )


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac_sha256(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _canonical_json(body: dict[str, Any]) -> bytes:
    return json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_query(action: str) -> str:
    return urlencode(
        sorted(
            {
                "Action": action,
                "Version": VOLCANO_ASSET_VERSION,
            }.items()
        ),
        quote_via=quote,
        safe="-_.~",
    )


def volcano_asset_host(region: str) -> str:
    if not _REGION_RE.fullmatch(region):
        raise ValueError("invalid Volcano region")
    return f"ark.{region}.volcengineapi.com"


@dataclass(frozen=True)
class VolcanoSignedRequest:
    url: str
    body: bytes = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    canonical_request: str
    string_to_sign: str


def volcano_asset_safe_filename(resource_id: str, *, asset_type: str) -> str:
    clean_id = resource_id.strip().lower()
    if not clean_id or len(clean_id) > 64 or not _SAFE_ASSET_ID_RE.fullmatch(clean_id):
        clean_id = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()[:32]
    normalized_type = asset_type.strip().lower()
    if normalized_type == "image":
        extension = "jpg"
    elif normalized_type == "video":
        extension = "mp4"
    else:
        raise ValueError("asset_type must be Image or Video")
    return f"lumen-asset-{clean_id}.{extension}"


def normalize_volcano_asset_name(
    value: Any,
    *,
    fallback_id: str,
) -> str:
    raw = str(value or "")
    without_controls = "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in raw
    )
    cleaned = " ".join(without_controls.split()).strip()
    if cleaned:
        return cleaned[:64]
    digest = hashlib.sha256(fallback_id.encode("utf-8")).hexdigest()[:12]
    return f"lumen-asset-{digest}"


def volcano_asset_reference_url(
    public_base_url: str,
    *,
    resource_id: str,
    asset_type: str,
    token: str,
) -> str:
    normalized_type = asset_type.strip().lower()
    if normalized_type == "image":
        collection = "images"
        variant = "volcano_asset_img_v1"
    elif normalized_type == "video":
        collection = "videos"
        variant = "volcano_asset_video_v1"
    else:
        raise ValueError("asset_type must be Image or Video")
    filename = volcano_asset_safe_filename(
        resource_id,
        asset_type=asset_type,
    )
    query = urlencode({"token": token, "variant": variant})
    return (
        f"{public_base_url.rstrip('/')}/api/{collection}/reference/"
        f"{quote(resource_id, safe='')}/binary/{filename}?{query}"
    )


def build_signed_asset_request(
    *,
    action: str,
    body: dict[str, Any],
    access_key_id: str,
    secret_access_key: str,
    region: str,
    now: datetime | None = None,
) -> VolcanoSignedRequest:
    if action not in VOLCANO_ASSET_ACTIONS:
        raise ValueError("unsupported Volcano asset action")
    if not access_key_id or not secret_access_key:
        raise ValueError("Volcano asset credentials are required")

    request_time = now or datetime.now(timezone.utc)
    if request_time.tzinfo is None:
        request_time = request_time.replace(tzinfo=timezone.utc)
    request_time = request_time.astimezone(timezone.utc)
    x_date = request_time.strftime("%Y%m%dT%H%M%SZ")
    short_date = request_time.strftime("%Y%m%d")
    payload = _canonical_json(body)
    payload_hash = _sha256_hex(payload)
    host = volcano_asset_host(region)
    canonical_headers = (
        "content-type:application/json\n"
        f"host:{host}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    canonical_query = _canonical_query(action)
    canonical_request = "\n".join(
        (
            "POST",
            "/",
            canonical_query,
            canonical_headers,
            _SIGNED_HEADERS,
            payload_hash,
        )
    )
    credential_scope = f"{short_date}/{region}/{VOLCANO_ASSET_SERVICE}/request"
    string_to_sign = "\n".join(
        (
            "HMAC-SHA256",
            x_date,
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        )
    )
    date_key = _hmac_sha256(secret_access_key.encode("utf-8"), short_date)
    region_key = _hmac_sha256(date_key, region)
    service_key = _hmac_sha256(region_key, VOLCANO_ASSET_SERVICE)
    signing_key = _hmac_sha256(service_key, "request")
    signature = hmac.new(
        signing_key,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    authorization = (
        f"HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={_SIGNED_HEADERS}, Signature={signature}"
    )
    return VolcanoSignedRequest(
        url=f"https://{host}/?{canonical_query}",
        body=payload,
        headers={
            "authorization": authorization,
            "content-type": "application/json",
            "host": host,
            "x-content-sha256": payload_hash,
            "x-date": x_date,
        },
        canonical_request=canonical_request,
        string_to_sign=string_to_sign,
    )


def normalize_volcano_result(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    if "Result" in payload:
        return payload["Result"]
    return {
        key: value
        for key, value in payload.items()
        if key not in {"ResponseMetadata", "response_metadata"}
    }


def _mapping_value(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


def _text(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def _optional_text(value: Any) -> str | None:
    result = _text(value).strip()
    return result or None


def _int_value(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _unwrap_mapping(raw: Any, *keys: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    nested = _mapping_value(raw, *keys)
    return nested if isinstance(nested, dict) else raw


def normalize_asset_group(
    raw: Any,
    *,
    project_name: str,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = _unwrap_mapping(raw, "AssetGroup", "AssetGroupInfo", "Group")
    fallback = fallback or {}
    group_id = _optional_text(
        _mapping_value(item, "Id", "ID", "GroupId", "id", "group_id")
    ) or _text(fallback.get("id"))
    name = _optional_text(_mapping_value(item, "Name", "name")) or _text(
        fallback.get("name")
    )
    title = _optional_text(_mapping_value(item, "Title", "title")) or name
    description = _text(
        _mapping_value(item, "Description", "description"),
        _text(fallback.get("description")),
    )
    return {
        "id": group_id,
        "name": name,
        "title": title,
        "description": description,
        "group_type": _text(
            _mapping_value(item, "GroupType", "group_type"),
            _text(fallback.get("group_type")),
        ),
        "project_name": _text(
            _mapping_value(item, "ProjectName", "project_name"),
            _text(fallback.get("project_name"), project_name),
        ),
        "create_time": _optional_text(
            _mapping_value(
                item,
                "CreateTime",
                "CreatedAt",
                "create_time",
                "created_at",
            )
        )
        or _optional_text(fallback.get("create_time")),
        "update_time": _optional_text(
            _mapping_value(
                item,
                "UpdateTime",
                "UpdatedAt",
                "update_time",
                "updated_at",
            )
        )
        or _optional_text(fallback.get("update_time")),
    }


def normalize_asset_group_list(
    raw: Any,
    *,
    project_name: str,
    page_number: int,
    page_size: int,
) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    raw_items = (
        raw
        if isinstance(raw, list)
        else _mapping_value(
            payload,
            "AssetGroups",
            "AssetGroupInfos",
            "Groups",
            "Items",
            "items",
        )
    )
    items = raw_items if isinstance(raw_items, list) else []
    return {
        "items": [
            normalize_asset_group(item, project_name=project_name)
            for item in items
            if isinstance(item, dict)
        ],
        "total_count": _int_value(
            _mapping_value(
                payload,
                "TotalCount",
                "Total",
                "total_count",
                "total",
            ),
            len(items),
        ),
        "page_number": _int_value(
            _mapping_value(payload, "PageNumber", "page_number"),
            page_number,
        ),
        "page_size": _int_value(
            _mapping_value(payload, "PageSize", "page_size"),
            page_size,
        ),
    }


def _asset_error(item: dict[str, Any]) -> tuple[str | None, str | None]:
    raw_error = _mapping_value(item, "Error", "error")
    if isinstance(raw_error, dict):
        return (
            _optional_text(_mapping_value(raw_error, "Code", "code")),
            _optional_text(_mapping_value(raw_error, "Message", "message")),
        )
    value = _mapping_value(
        item,
        "ErrorMessage",
        "FailedReason",
        "FailReason",
        "error_message",
    )
    if isinstance(value, dict):
        return (
            _optional_text(_mapping_value(value, "Code", "code")),
            _optional_text(_mapping_value(value, "Message", "message")),
        )
    return None, _optional_text(value)


def _sanitize_asset_url(raw: Any) -> str | None:
    value = _optional_text(raw)
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    path = parsed.path.lower()
    if any(path.startswith(prefix) for prefix in _INTERNAL_REFERENCE_PATH_PREFIXES):
        return None
    sanitized_query = urlencode(
        [
            (key, query_value)
            for key, query_value in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if not (
                re.sub(r"[^a-z0-9]", "", key.lower()) == "sig"
                or any(
                    marker in re.sub(r"[^a-z0-9]", "", key.lower())
                    for marker in _SENSITIVE_ASSET_URL_QUERY_MARKERS
                )
            )
        ],
        doseq=True,
    )
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            sanitized_query,
            "",
        )
    )


def _asset_preview_url(raw: Any, *, asset_type: str) -> str | None:
    value = _optional_text(raw)
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    match = _INTERNAL_REFERENCE_PATH_RE.fullmatch(parsed.path)
    if match is None:
        return _sanitize_asset_url(value)
    expected_collection = (
        "images"
        if asset_type.strip().lower() == "image"
        else "videos"
        if asset_type.strip().lower() == "video"
        else None
    )
    if match.group("collection") != expected_collection:
        return None
    resource_id = unquote(match.group("resource_id"))
    if not _SAFE_ASSET_ID_RE.fullmatch(resource_id):
        return None
    return f"/api/{expected_collection}/{quote(resource_id, safe='')}/binary"


def normalize_asset(
    raw: Any,
    *,
    project_name: str,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = _unwrap_mapping(raw, "Asset", "AssetInfo")
    fallback = fallback or {}
    error_code, error_message = _asset_error(item)
    asset_type = _optional_text(
        _mapping_value(item, "AssetType", "asset_type")
    ) or _text(fallback.get("asset_type"))
    raw_url = _mapping_value(item, "URL", "Url", "url")
    return {
        "id": _optional_text(
            _mapping_value(item, "Id", "ID", "AssetId", "id", "asset_id")
        )
        or _text(fallback.get("id")),
        "group_id": _optional_text(_mapping_value(item, "GroupId", "group_id"))
        or _text(fallback.get("group_id")),
        "name": _optional_text(_mapping_value(item, "Name", "name"))
        or _text(fallback.get("name")),
        "asset_type": asset_type,
        "status": _optional_text(_mapping_value(item, "Status", "status"))
        or _text(fallback.get("status")),
        "url": _sanitize_asset_url(raw_url),
        "preview_url": _asset_preview_url(raw_url, asset_type=asset_type),
        "project_name": _text(
            _mapping_value(item, "ProjectName", "project_name"),
            _text(fallback.get("project_name"), project_name),
        ),
        "create_time": _optional_text(
            _mapping_value(
                item,
                "CreateTime",
                "CreatedAt",
                "create_time",
                "created_at",
            )
        )
        or _optional_text(fallback.get("create_time")),
        "update_time": _optional_text(
            _mapping_value(
                item,
                "UpdateTime",
                "UpdatedAt",
                "update_time",
                "updated_at",
            )
        )
        or _optional_text(fallback.get("update_time")),
        "error_code": error_code or _optional_text(fallback.get("error_code")),
        "error_message": error_message or _optional_text(fallback.get("error_message")),
    }


def normalize_asset_list(
    raw: Any,
    *,
    project_name: str,
    page_number: int,
    page_size: int,
) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    raw_items = (
        raw
        if isinstance(raw, list)
        else _mapping_value(
            payload,
            "Assets",
            "AssetInfos",
            "Items",
            "items",
        )
    )
    items = raw_items if isinstance(raw_items, list) else []
    return {
        "items": [
            normalize_asset(item, project_name=project_name)
            for item in items
            if isinstance(item, dict)
        ],
        "total_count": _int_value(
            _mapping_value(
                payload,
                "TotalCount",
                "Total",
                "total_count",
                "total",
            ),
            len(items),
        ),
        "page_number": _int_value(
            _mapping_value(payload, "PageNumber", "page_number"),
            page_number,
        ),
        "page_size": _int_value(
            _mapping_value(payload, "PageSize", "page_size"),
            page_size,
        ),
    }


def _response_metadata(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    metadata = _mapping_value(payload, "ResponseMetadata", "response_metadata")
    return metadata if isinstance(metadata, dict) else {}


def _upstream_error(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    metadata = _response_metadata(payload)
    raw_error = _mapping_value(metadata, "Error", "error")
    if raw_error is None:
        raw_error = _mapping_value(payload, "Error", "error")
    code: str | None = None
    if isinstance(raw_error, dict):
        code = _optional_text(_mapping_value(raw_error, "Code", "code"))
    elif raw_error is not None:
        code = _optional_text(raw_error)
    if code is None:
        code = _optional_text(
            _mapping_value(metadata, "Code", "code")
        ) or _optional_text(_mapping_value(payload, "Code", "code"))
    request_id = _optional_text(
        _mapping_value(metadata, "RequestId", "RequestID", "request_id")
    ) or _optional_text(_mapping_value(payload, "RequestId", "RequestID", "request_id"))
    return code, request_id


def _retry_after_ms(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return max(0, min(int(seconds * 1000), 3_600_000))


def _mapped_upstream_error(
    *,
    action: str,
    status_code: int,
    upstream_code: str | None,
    request_id: str | None,
    retry_after_ms: int | None,
) -> VolcanoAssetServiceError:
    normalized = (upstream_code or "").lower()
    details = {
        key: value
        for key, value in {
            "action": action,
            "upstream_status": status_code or None,
            "upstream_code": upstream_code,
            "request_id": request_id,
        }.items()
        if value is not None
    }
    if status_code == 429 or any(
        marker in normalized
        for marker in ("throttl", "ratelimit", "limitexceeded", "toomany")
    ):
        headers = (
            {"Retry-After": str(max(1, retry_after_ms // 1000))}
            if retry_after_ms
            else None
        )
        return _http(
            "volcano_asset_rate_limited",
            "Volcano asset service is rate limited",
            429,
            details=details,
            retry_after_ms=retry_after_ms,
            headers=headers,
        )
    if status_code >= 500 or any(
        marker in normalized for marker in ("internal", "unavailable", "servicebusy")
    ):
        return _http(
            "volcano_asset_unavailable",
            "Volcano asset service is temporarily unavailable",
            503,
            details=details,
        )
    if status_code in {401, 403} or any(
        marker in normalized
        for marker in (
            "accessdenied",
            "invalidaccesskey",
            "signature",
            "unauthor",
            "forbidden",
        )
    ):
        return _http(
            "volcano_asset_credentials_invalid",
            "Volcano asset credentials or permissions are invalid",
            503,
            details=details,
        )
    if status_code in {404, 410} or any(
        marker in normalized
        for marker in ("notfound", "alreadydeleted", "hasbeendeleted")
    ):
        return _http(
            "volcano_asset_not_found",
            "Volcano asset resource was not found",
            404,
            details=details,
        )
    if status_code == 409 or any(
        marker in normalized for marker in ("conflict", "alreadyexist")
    ):
        return _http(
            "volcano_asset_conflict",
            "Volcano asset request conflicts with the current resource state",
            409,
            details=details,
        )
    if status_code in {400, 422} or any(
        marker in normalized
        for marker in ("invalidparameter", "missingparameter", "badrequest")
    ):
        return _http(
            "volcano_asset_invalid_request",
            "Volcano asset service rejected the request",
            422,
            details=details,
        )
    return _http(
        "volcano_asset_upstream_error",
        "Volcano asset service rejected the request",
        502,
        details=details,
    )


class VolcanoAssetClient:
    def __init__(
        self,
        provider: VideoProviderDefinition,
        *,
        proxy_resolver: Callable[
            [Any],
            Awaitable[str | None],
        ],
    ) -> None:
        if provider.kind != "volcano" or not provider.asset_management_ready:
            raise ValueError("official Volcano asset credentials are required")
        self.provider = provider
        self._proxy_resolver = proxy_resolver

    async def request(self, action: str, body: dict[str, Any]) -> Any:
        signed = build_signed_asset_request(
            action=action,
            body=body,
            access_key_id=self.provider.access_key_id,
            secret_access_key=self.provider.secret_access_key,
            region=self.provider.region,
        )
        try:
            proxy_url = await self._proxy_resolver(self.provider.proxy)
        except Exception as exc:  # noqa: BLE001
            raise _http(
                "volcano_asset_proxy_unavailable",
                "configured Volcano asset proxy is unavailable",
                503,
            ) from exc

        try:
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT,
                proxy=proxy_url,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    signed.url,
                    content=signed.body,
                    headers=signed.headers,
                )
        except httpx.TimeoutException as exc:
            raise _http(
                "volcano_asset_timeout",
                "Volcano asset service timed out",
                504,
                details={"action": action},
            ) from exc
        except httpx.RequestError as exc:
            raise _http(
                "volcano_asset_connection_failed",
                "Volcano asset service connection failed",
                502,
                details={"action": action},
            ) from exc

        is_success_status = 200 <= response.status_code < 300
        try:
            payload = response.json()
        except ValueError as exc:
            if not is_success_status:
                raise _mapped_upstream_error(
                    action=action,
                    status_code=response.status_code,
                    upstream_code=None,
                    request_id=None,
                    retry_after_ms=_retry_after_ms(response),
                ) from exc
            raise _http(
                "volcano_asset_invalid_response",
                "Volcano asset service returned an invalid response",
                502,
                details={
                    "action": action,
                    "upstream_status": response.status_code,
                },
            ) from exc

        upstream_code, request_id = _upstream_error(payload)
        if not is_success_status or upstream_code:
            raise _mapped_upstream_error(
                action=action,
                status_code=response.status_code,
                upstream_code=upstream_code,
                request_id=request_id,
                retry_after_ms=_retry_after_ms(response),
            )
        if not isinstance(payload, dict):
            raise _http(
                "volcano_asset_invalid_response",
                "Volcano asset service returned an invalid response",
                502,
                details={
                    "action": action,
                    "upstream_status": response.status_code,
                },
            )
        return normalize_volcano_result(payload)


__all__ = [
    "VOLCANO_ASSET_ACTIONS",
    "VOLCANO_ASSET_CREATE_DEFAULT_COOLDOWN_MS",
    "VOLCANO_ASSET_CREATE_INTERVAL_MS",
    "VOLCANO_ASSET_CREATE_QPM",
    "VOLCANO_ASSET_CREATE_SCHEDULE_TTL_SECONDS",
    "VOLCANO_ASSET_CREATE_WINDOW_SECONDS",
    "VOLCANO_ASSET_MAX_ASSETS",
    "VOLCANO_ASSET_MAX_GROUPS",
    "VOLCANO_ASSET_OPERATION_TTL_SECONDS",
    "VOLCANO_ASSET_RESERVATION_TTL_SECONDS",
    "VOLCANO_ASSET_SERVICE",
    "VOLCANO_ASSET_VERSION",
    "VolcanoAssetClient",
    "VolcanoAssetCreateRateLimited",
    "VolcanoAssetOperationOwnershipError",
    "VolcanoAssetQuotaExceeded",
    "VolcanoAssetQuotaKey",
    "VolcanoAssetRedisUnavailable",
    "VolcanoAssetServiceError",
    "VolcanoSignedRequest",
    "acquire_volcano_create_rate_limit",
    "build_signed_asset_request",
    "compare_and_set_volcano_asset_operation",
    "defer_volcano_create_rate_limit",
    "normalize_asset",
    "normalize_asset_group",
    "normalize_asset_group_list",
    "normalize_asset_list",
    "normalize_volcano_asset_name",
    "normalize_volcano_result",
    "release_volcano_asset_quota",
    "release_volcano_create_rate_limit",
    "reserve_volcano_asset_quota",
    "volcano_asset_operation_key",
    "volcano_asset_host",
    "volcano_asset_quota_key",
    "volcano_asset_quota_scope",
    "volcano_asset_rate_limit_cooldown_key",
    "volcano_asset_rate_limit_key",
    "volcano_asset_reference_url",
    "volcano_asset_reservation_key",
    "volcano_asset_safe_filename",
]
