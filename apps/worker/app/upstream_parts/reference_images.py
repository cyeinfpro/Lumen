"""Reference image normalization, caching, and sidecar upload helpers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from typing import Any

import httpx
from lumen_core.svg_output import looks_like_svg

from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    UpstreamServices,
    resolve_image_upstream_services,
)
from ..upstream_clients.image_job_auth import image_job_service_headers
from ..upstream_clients.url_validation import validate_image_job_control_url
from .image_execution import ImageRequestContext, ensure_image_request_context

_DEFAULT_REFERENCE_FRESH_WINDOW_S = 60.0
_DEFAULT_REFERENCE_EXPIRY_MARGIN_S = 120.0
_DEFAULT_REFERENCE_UPLOAD_CONCURRENCY = 4


def _sniff_image_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _runtime_services(runtime: ImageUpstreamRuntime | None) -> UpstreamServices:
    return resolve_image_upstream_services(runtime)


def _load_pyvips() -> Any | None:
    try:
        import pyvips
    except (ImportError, OSError):
        return None
    return pyvips


def _is_animated_webp(raw: bytes) -> bool:
    return (
        len(raw) >= 30
        and raw[:4] == b"RIFF"
        and raw[8:12] == b"WEBP"
        and b"ANIM" in raw[:64]
    )


def _raise_pixel_limit_error(services: UpstreamServices, actual_pixels: int) -> None:
    raise services.infrastructure.UpstreamError(
        "reference image exceeds pixel limit",
        error_code=services.infrastructure.EC.REFERENCE_IMAGE_TOO_LARGE.value,
        status_code=413,
        payload={
            "max_pixels": services.core.MAX_REFERENCE_IMAGE_PIXELS,
            "actual_pixels": actual_pixels,
        },
    )


def _validate_normalized_bytes(
    normalized: bytes,
    *,
    services: UpstreamServices,
) -> tuple[bytes, str]:
    if len(normalized) > services.core.MAX_NORMALIZED_IMAGE_BYTES:
        raise services.infrastructure.UpstreamError(
            "normalized reference image exceeds size limit",
            error_code=services.infrastructure.EC.REFERENCE_IMAGE_TOO_LARGE.value,
            status_code=413,
            payload={
                "max_bytes": services.core.MAX_NORMALIZED_IMAGE_BYTES,
                "actual_bytes": len(normalized),
            },
        )
    return normalized, "image/webp"


def _encode_vips_webp(image: Any) -> bytes:
    if getattr(type(image), "webp_save_buffer", None) is not None or (
        "webp_save_buffer" in getattr(image, "__dict__", {})
    ):
        return bytes(image.webp_save_buffer(Q=90, effort=3, strip=True))
    return bytes(image.webpsave_buffer(Q=90, effort=3, strip=True))


def _prepare_vips_srgb(image: Any) -> Any:
    bands = int(getattr(image, "bands", 3) or 3)
    interpretation = str(getattr(image, "interpretation", "srgb") or "srgb")
    if (interpretation != "srgb" or bands < 3) and hasattr(image, "colourspace"):
        image = image.colourspace("srgb")
    if str(getattr(image, "format", "uchar") or "uchar") != "uchar" and hasattr(
        image, "cast"
    ):
        image = image.cast("uchar")
    bands = int(getattr(image, "bands", 3) or 3)
    if bands > 4 and hasattr(image, "extract_band"):
        keep = 4 if hasattr(image, "hasalpha") and bool(image.hasalpha()) else 3
        image = image.extract_band(0, n=keep)
    return image


def _normalize_reference_image_vips(
    raw: bytes,
    *,
    pyvips: Any,
    services: UpstreamServices,
) -> tuple[bytes, str] | None:
    if _is_animated_webp(raw):
        return None
    image = pyvips.Image.new_from_buffer(raw, "", access="sequential")
    get_typeof = getattr(image, "get_typeof", None)
    if callable(get_typeof) and get_typeof("n-pages") != 0:
        if int(image.get("n-pages") or 1) > 1:
            return None
    width = int(getattr(image, "width", 0) or 0)
    height = int(getattr(image, "height", 0) or 0)
    actual_pixels = max(width, 0) * max(height, 0)
    if (
        width <= 0
        or height <= 0
        or actual_pixels > services.core.MAX_REFERENCE_IMAGE_PIXELS
    ):
        _raise_pixel_limit_error(services, actual_pixels)
    normalized = _encode_vips_webp(_prepare_vips_srgb(image))
    return _validate_normalized_bytes(normalized, services=services)


def _normalize_reference_image_pil(
    raw: bytes,
    *,
    services: UpstreamServices,
) -> tuple[bytes, str]:
    image_api = services.infrastructure.PILImage
    try:
        with image_api.open(io.BytesIO(raw)) as image:
            width, height = image.size
            actual_pixels = max(width, 0) * max(height, 0)
            if (
                width <= 0
                or height <= 0
                or actual_pixels > services.core.MAX_REFERENCE_IMAGE_PIXELS
            ):
                _raise_pixel_limit_error(services, actual_pixels)
            image.load()
            out = io.BytesIO()
            if image.mode in ("RGB", "RGBA"):
                image.save(out, format="WEBP", quality=90, method=4)
            else:
                target_mode = "RGBA" if "A" in image.getbands() else "RGB"
                with image.convert(target_mode) as normalized_image:
                    normalized_image.save(out, format="WEBP", quality=90, method=4)
        return _validate_normalized_bytes(out.getvalue(), services=services)
    except services.infrastructure.UpstreamError:
        raise
    except image_api.DecompressionBombError as exc:
        raise services.infrastructure.UpstreamError(
            f"reference image decompression bomb: {exc}",
            error_code=services.infrastructure.EC.REFERENCE_IMAGE_TOO_LARGE.value,
            status_code=413,
            payload={"max_pixels": services.core.MAX_REFERENCE_IMAGE_PIXELS},
        ) from exc
    except (
        services.infrastructure.UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise services.infrastructure.UpstreamError(
            f"reference image not decodable: {exc}",
            error_code=services.infrastructure.EC.BAD_REFERENCE_IMAGE.value,
            status_code=400,
        ) from exc


def _normalize_reference_image(
    raw: bytes,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[bytes, str]:
    """Re-encode a bounded reference image as metadata-free RGB/RGBA WebP."""
    services = _runtime_services(runtime)
    if len(raw) > services.core.MAX_REFERENCE_IMAGE_BYTES:
        raise services.infrastructure.UpstreamError(
            "reference image exceeds size limit",
            error_code=services.infrastructure.EC.REFERENCE_IMAGE_TOO_LARGE.value,
            status_code=413,
            payload={
                "max_bytes": services.core.MAX_REFERENCE_IMAGE_BYTES,
                "actual_bytes": len(raw),
            },
        )

    if looks_like_svg(raw) or raw.startswith((b"GIF87a", b"GIF89a")):
        raise services.infrastructure.UpstreamError(
            "SVG/GIF originals cannot be editing inputs; use a static PNG, JPEG or WebP",
            error_code=services.infrastructure.EC.BAD_REFERENCE_IMAGE.value,
            status_code=400,
        )

    pyvips = _load_pyvips()
    if pyvips is not None:
        try:
            vips_result = _normalize_reference_image_vips(
                raw,
                pyvips=pyvips,
                services=services,
            )
            if vips_result is not None:
                return vips_result
        except services.infrastructure.UpstreamError:
            raise
        except Exception as exc:  # noqa: BLE001
            services.infrastructure.logger.debug(
                "pyvips reference normalization fallback to PIL err=%r",
                exc,
            )

    return _normalize_reference_image_pil(raw, services=services)


def _reference_cache_keys(
    user_id: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[str, str]:
    services = _runtime_services(runtime)
    cache_key = f"{services.core.REFERENCE_CACHE_KEY_PREFIX}{user_id}"
    return (
        cache_key,
        f"{cache_key}{services.core.REFERENCE_CACHE_LRU_SUFFIX}",
    )


def _redis_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return str(value)


def _is_cache_entry_fresh(
    *,
    now: float,
    expires_at: float,
    verified_at: float,
    services: UpstreamServices,
) -> bool:
    fresh_window_s = float(
        getattr(
            services.core,
            "REFERENCE_CACHE_FRESH_WINDOW_S",
            _DEFAULT_REFERENCE_FRESH_WINDOW_S,
        )
    )
    ttl_s = float(services.core.REFERENCE_CACHE_TTL_S)
    configured_margin_s = float(
        getattr(
            services.core,
            "REFERENCE_CACHE_EXPIRY_MARGIN_S",
            _DEFAULT_REFERENCE_EXPIRY_MARGIN_S,
        )
    )
    expiry_margin_s = min(configured_margin_s, max(0.0, ttl_s * 0.5))
    age_s = now - verified_at
    return (
        verified_at > 0.0
        and 0.0 <= age_s < fresh_window_s
        and (expires_at - now) >= expiry_margin_s
    )


async def _reference_cache_lookup(
    redis: Any,
    *,
    user_id: str,
    digest: str,
    services: UpstreamServices,
) -> tuple[str | None, bool, dict[str, Any] | None]:
    cache_key, lru_key = services.references.reference_cache_keys(user_id)
    try:
        raw = await redis.hget(cache_key, digest)
        text = services.references.redis_text(raw)
        if not text:
            return None, False, None
        item = json.loads(text)
        if not isinstance(item, dict):
            return None, False, None
        expires_at = float(item.get("expires_at") or 0.0)
        url = item.get("upload_url")
        if not isinstance(url, str) or not url:
            return None, False, None
        now = float(services.infrastructure.time.time())
        if expires_at <= now:
            await services.references.reference_cache_delete(
                redis,
                user_id=user_id,
                digest=digest,
            )
            return None, False, None
        verified_at = float(item.get("verified_at") or 0.0)
        is_fresh = _is_cache_entry_fresh(
            now=now,
            expires_at=expires_at,
            verified_at=verified_at,
            services=services,
        )
        await redis.zadd(lru_key, {digest: now})
        await redis.expire(cache_key, services.core.REFERENCE_CACHE_TTL_S)
        await redis.expire(lru_key, services.core.REFERENCE_CACHE_TTL_S)
        return url, is_fresh, item
    except Exception as exc:  # noqa: BLE001
        services.infrastructure.logger.debug(
            "reference cache get skipped digest=%s err=%r",
            digest[:12],
            exc,
        )
        return None, False, None


async def _reference_cache_touch_verified(
    redis: Any,
    *,
    user_id: str,
    digest: str,
    item: dict[str, Any],
    services: UpstreamServices,
) -> None:
    cache_key, _lru_key = services.references.reference_cache_keys(user_id)
    updated = dict(item)
    updated["verified_at"] = float(services.infrastructure.time.time())
    try:
        await redis.hset(
            cache_key,
            digest,
            json.dumps(updated, separators=(",", ":")),
        )
    except Exception as exc:  # noqa: BLE001
        services.infrastructure.logger.debug(
            "reference cache verified_at update skipped digest=%s err=%r",
            digest[:12],
            exc,
        )


async def _reference_cache_get(
    redis: Any,
    *,
    user_id: str,
    digest: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> str | None:
    services = _runtime_services(runtime)
    url, _is_fresh, _item = await _reference_cache_lookup(
        redis,
        user_id=user_id,
        digest=digest,
        services=services,
    )
    return url


async def _reference_cache_store(
    redis: Any,
    *,
    user_id: str,
    digest: str,
    url: str,
    size: int,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = _runtime_services(runtime)
    cache_key, lru_key = services.references.reference_cache_keys(user_id)
    now = services.infrastructure.time.time()
    item = {
        "upload_url": url,
        "expires_at": now + services.core.REFERENCE_CACHE_TTL_S,
        "verified_at": now,
        "size": size,
    }
    try:
        await redis.hset(
            cache_key,
            digest,
            json.dumps(item, separators=(",", ":")),
        )
        await redis.zadd(lru_key, {digest: now})
        await redis.expire(cache_key, services.core.REFERENCE_CACHE_TTL_S)
        await redis.expire(lru_key, services.core.REFERENCE_CACHE_TTL_S)
        await services.references.reference_cache_trim(redis, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        services.infrastructure.logger.debug(
            "reference cache store skipped digest=%s err=%r",
            digest[:12],
            exc,
        )


async def _reference_cache_delete(
    redis: Any,
    *,
    user_id: str,
    digest: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = _runtime_services(runtime)
    cache_key, lru_key = services.references.reference_cache_keys(user_id)
    try:
        await redis.hdel(cache_key, digest)
        await redis.zrem(lru_key, digest)
    except Exception as exc:  # noqa: BLE001
        services.infrastructure.logger.debug(
            "reference cache delete skipped digest=%s err=%r",
            digest[:12],
            exc,
        )


async def _reference_cache_trim(
    redis: Any,
    *,
    user_id: str,
    runtime: ImageUpstreamRuntime | None = None,
) -> None:
    services = _runtime_services(runtime)
    cache_key, lru_key = services.references.reference_cache_keys(user_id)
    try:
        total = await redis.zcard(lru_key)
        overflow = int(total) - services.core.REFERENCE_CACHE_MAX_ENTRIES
        if overflow <= 0:
            return
        stale = await redis.zrange(lru_key, 0, overflow - 1)
        digests = [services.references.redis_text(item) for item in stale or []]
        digests = [item for item in digests if item]
        if not digests:
            return
        await redis.hdel(cache_key, *digests)
        await redis.zrem(lru_key, *digests)
    except Exception as exc:  # noqa: BLE001
        services.infrastructure.logger.debug(
            "reference cache trim skipped err=%r", exc
        )


async def _reference_url_is_live(
    url: str,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> bool:
    services = _runtime_services(runtime)
    assert runtime is not None
    candidate = url.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return False
    try:
        target = await services.infrastructure.resolve_public_http_target(
            candidate, allow_http=True
        )
        timeout = services.infrastructure.httpx.Timeout(
            services.core.REFERENCE_CACHE_HEAD_TIMEOUT_S
        )
        client = await runtime.reference_http_pool.get_probe_client(
            client_cls=services.infrastructure.httpx.AsyncClient,
            timeout=timeout,
            target=target,
            pinned_transport_factory=services.infrastructure.pinned_async_http_transport,
        )
        response = await client.head(target.url)
        return 200 <= response.status_code < 300
    except (ValueError, httpx.HTTPError, OSError) as exc:
        services.infrastructure.logger.debug(
            "reference cache HEAD failed url=%s err=%r",
            url,
            exc,
        )
        return False


async def _resolve_cached_reference_url(
    redis: Any,
    *,
    user_id: str,
    digest: str,
    services: UpstreamServices,
) -> str | None:
    cache_get_fn = services.references.reference_cache_get
    if getattr(cache_get_fn, "func", None) is _reference_cache_get:
        cached, is_fresh, item = await _reference_cache_lookup(
            redis,
            user_id=user_id,
            digest=digest,
            services=services,
        )
    else:
        cached = await cache_get_fn(redis, user_id=user_id, digest=digest)
        is_fresh = False
        item = None
    if not cached:
        return None
    if is_fresh:
        return cached
    if await services.references.reference_url_is_live(cached):
        if item is not None:
            await _reference_cache_touch_verified(
                redis,
                user_id=user_id,
                digest=digest,
                item=item,
                services=services,
            )
        return cached
    await services.references.reference_cache_delete(
        redis,
        user_id=user_id,
        digest=digest,
    )
    return None


async def _get_or_upload_reference(
    ref_bytes: bytes,
    mime: str,
    *,
    base_url: str,
    api_key: str,
    user_id: str | None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> str | None:
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)
    redis: Any | None = None
    digest = hashlib.sha256(ref_bytes).hexdigest()
    if user_id:
        try:
            pool = await services.infrastructure.provider_pool.get_pool()
            redis = services.providers.provider_pool_redis(pool)
        except Exception as exc:  # noqa: BLE001
            services.infrastructure.logger.debug(
                "reference cache redis unavailable err=%r", exc
            )

    if redis is not None and user_id:
        cached = await _resolve_cached_reference_url(
            redis,
            user_id=user_id,
            digest=digest,
            services=services,
        )
        if cached:
            return cached

    upload_kwargs: dict[str, Any] = {
        "base_url": base_url,
        "api_key": api_key,
    }
    if request_context is not None:
        upload_kwargs["request_context"] = request_context
    uploaded = await services.references.push_reference_to_image_job(
        ref_bytes,
        mime,
        **upload_kwargs,
    )
    if uploaded and redis is not None and user_id:
        await services.references.reference_cache_store(
            redis,
            user_id=user_id,
            digest=digest,
            url=uploaded,
            size=len(ref_bytes),
        )
    return uploaded


async def _push_reference_to_image_job(
    raw: bytes,
    mime: str,
    *,
    base_url: str,
    api_key: str,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> str | None:
    """Upload normalized bytes to the configured image-job reference endpoint."""
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)
    assert runtime is not None
    if not base_url or not api_key:
        return None
    builder = services.image_jobs.build_image_job_client
    builder_func = getattr(builder, "func", None)
    if getattr(builder_func, "__name__", "") != "_build_image_job_client":
        injected_client = builder(
            base_url,
            service_token=api_key,
            read_timeout_s=services.core.REFERENCE_PUSH_TIMEOUT_S,
        )
        try:
            uploaded = await injected_client.upload_reference(
                raw,
                mime,
                trace_id=context.trace_id,
            )
            return uploaded.url if uploaded is not None else None
        except (httpx.HTTPError, OSError, ValueError) as exc:
            services.infrastructure.logger.warning(
                "reference push to image-job error: %r", exc
            )
            return None
        finally:
            await injected_client.close()
    try:
        validated_base = validate_image_job_control_url(
            services.requests.validate_image_job_base_url(base_url),
            allow_private_http=True,
        )
        settings = services.infrastructure.settings
        timeout = services.infrastructure.httpx.Timeout(
            connect=settings.upstream_connect_timeout_s,
            read=services.core.REFERENCE_PUSH_TIMEOUT_S,
            write=settings.upstream_write_timeout_s,
            pool=settings.upstream_connect_timeout_s,
        )
        client = await runtime.reference_http_pool.get_upload_client(
            client_cls=services.infrastructure.httpx.AsyncClient,
            timeout=timeout,
        )
        response = await client.post(
            f"{validated_base}/v1/refs",
            content=raw,
            headers=image_job_service_headers(
                service_token=api_key,
                trace_id=context.trace_id,
                content_type=mime,
            ),
        )
        if response.status_code != 200:
            return None
        data = response.json()
        url = data.get("url") if isinstance(data, dict) else None
        return url if isinstance(url, str) and url else None
    except (httpx.HTTPError, OSError, ValueError) as exc:
        services.infrastructure.logger.warning(
            "reference push to image-job error: %r", exc
        )
        return None


def _encode_inline_data_url(ref_bytes: bytes, mime: str) -> str:
    encoded = base64.b64encode(ref_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"


async def _resolve_reference_image_urls(
    images: list[bytes] | None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    user_id: str | None = None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> list[str]:
    context = ensure_image_request_context(request_context)
    runtime = runtime or context.upstream_runtime
    services = _runtime_services(runtime)
    if not images:
        return []
    concurrency = max(
        1,
        int(
            getattr(
                services.core,
                "REFERENCE_UPLOAD_CONCURRENCY",
                _DEFAULT_REFERENCE_UPLOAD_CONCURRENCY,
            )
        ),
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def _resolve_single(raw: bytes) -> str:
        async with semaphore:
            ref_bytes, mime = await asyncio.to_thread(
                services.references.normalize_reference_image,
                raw,
            )
            ref_url: str | None = None
            if base_url and api_key:
                upload_kwargs: dict[str, Any] = {
                    "base_url": base_url,
                    "api_key": api_key,
                    "user_id": user_id,
                }
                if request_context is not None:
                    upload_kwargs["request_context"] = request_context
                ref_url = await services.references.get_or_upload_reference(
                    ref_bytes,
                    mime,
                    **upload_kwargs,
                )
            if ref_url:
                return ref_url
            return await asyncio.to_thread(_encode_inline_data_url, ref_bytes, mime)

    return list(await asyncio.gather(*(_resolve_single(raw) for raw in images)))


__all__ = [
    "_get_or_upload_reference",
    "_normalize_reference_image",
    "_redis_text",
    "_reference_cache_delete",
    "_reference_cache_get",
    "_reference_cache_keys",
    "_reference_cache_store",
    "_reference_cache_trim",
    "_reference_url_is_live",
    "_push_reference_to_image_job",
    "_resolve_reference_image_urls",
    "_sniff_image_mime",
]
