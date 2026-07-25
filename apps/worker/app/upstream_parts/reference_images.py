"""Reference image normalization, caching, and sidecar upload helpers."""

from __future__ import annotations

from ..provider_runtime.upstream_services import upstream_services

import base64
import hashlib
import io
import json
from typing import Any

import httpx


def _sniff_image_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize_reference_image(raw: bytes) -> tuple[bytes, str]:
    """Re-encode a bounded reference image as metadata-free RGB/RGBA WebP."""
    if len(raw) > upstream_services().core.MAX_REFERENCE_IMAGE_BYTES:
        raise upstream_services().infrastructure.UpstreamError(
            "reference image exceeds size limit",
            error_code=upstream_services().infrastructure.EC.REFERENCE_IMAGE_TOO_LARGE.value,
            status_code=413,
            payload={
                "max_bytes": upstream_services().core.MAX_REFERENCE_IMAGE_BYTES,
                "actual_bytes": len(raw),
            },
        )

    image_api = upstream_services().infrastructure.PILImage
    try:
        with image_api.open(io.BytesIO(raw)) as image:
            width, height = image.size
            actual_pixels = max(width, 0) * max(height, 0)
            if (
                width <= 0
                or height <= 0
                or actual_pixels > upstream_services().core.MAX_REFERENCE_IMAGE_PIXELS
            ):
                raise upstream_services().infrastructure.UpstreamError(
                    "reference image exceeds pixel limit",
                    error_code=upstream_services().infrastructure.EC.REFERENCE_IMAGE_TOO_LARGE.value,
                    status_code=413,
                    payload={
                        "max_pixels": upstream_services().core.MAX_REFERENCE_IMAGE_PIXELS,
                        "actual_pixels": actual_pixels,
                    },
                )
            image.load()
            out = io.BytesIO()
            if image.mode in ("RGB", "RGBA"):
                image.save(out, format="WEBP", quality=90, method=4)
            else:
                target_mode = "RGBA" if "A" in image.getbands() else "RGB"
                with image.convert(target_mode) as normalized_image:
                    normalized_image.save(out, format="WEBP", quality=90, method=4)

        normalized = out.getvalue()
        if len(normalized) > upstream_services().core.MAX_NORMALIZED_IMAGE_BYTES:
            raise upstream_services().infrastructure.UpstreamError(
                "normalized reference image exceeds size limit",
                error_code=upstream_services().infrastructure.EC.REFERENCE_IMAGE_TOO_LARGE.value,
                status_code=413,
                payload={
                    "max_bytes": upstream_services().core.MAX_NORMALIZED_IMAGE_BYTES,
                    "actual_bytes": len(normalized),
                },
            )
        return normalized, "image/webp"
    except upstream_services().infrastructure.UpstreamError:
        raise
    except image_api.DecompressionBombError as exc:
        raise upstream_services().infrastructure.UpstreamError(
            f"reference image decompression bomb: {exc}",
            error_code=upstream_services().infrastructure.EC.REFERENCE_IMAGE_TOO_LARGE.value,
            status_code=413,
            payload={"max_pixels": upstream_services().core.MAX_REFERENCE_IMAGE_PIXELS},
        ) from exc
    except (
        upstream_services().infrastructure.UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise upstream_services().infrastructure.UpstreamError(
            f"reference image not decodable: {exc}",
            error_code=upstream_services().infrastructure.EC.BAD_REFERENCE_IMAGE.value,
            status_code=400,
        ) from exc


def _reference_cache_keys(user_id: str) -> tuple[str, str]:
    cache_key = f"{upstream_services().core.REFERENCE_CACHE_KEY_PREFIX}{user_id}"
    return (
        cache_key,
        f"{cache_key}{upstream_services().core.REFERENCE_CACHE_LRU_SUFFIX}",
    )


def _redis_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return str(value)


async def _reference_cache_get(
    redis: Any,
    *,
    user_id: str,
    digest: str,
) -> str | None:
    cache_key, lru_key = upstream_services().references.reference_cache_keys(user_id)
    try:
        raw = await redis.hget(cache_key, digest)
        text = upstream_services().references.redis_text(raw)
        if not text:
            return None
        item = json.loads(text)
        if not isinstance(item, dict):
            return None
        expires_at = float(item.get("expires_at") or 0.0)
        url = item.get("upload_url")
        if not isinstance(url, str) or not url:
            return None
        if expires_at <= upstream_services().infrastructure.time.time():
            await upstream_services().references.reference_cache_delete(
                redis,
                user_id=user_id,
                digest=digest,
            )
            return None
        await redis.zadd(
            lru_key, {digest: upstream_services().infrastructure.time.time()}
        )
        await redis.expire(cache_key, upstream_services().core.REFERENCE_CACHE_TTL_S)
        await redis.expire(lru_key, upstream_services().core.REFERENCE_CACHE_TTL_S)
        return url
    except Exception as exc:  # noqa: BLE001
        upstream_services().infrastructure.logger.debug(
            "reference cache get skipped digest=%s err=%r",
            digest[:12],
            exc,
        )
        return None


async def _reference_cache_store(
    redis: Any,
    *,
    user_id: str,
    digest: str,
    url: str,
    size: int,
) -> None:
    cache_key, lru_key = upstream_services().references.reference_cache_keys(user_id)
    now = upstream_services().infrastructure.time.time()
    item = {
        "upload_url": url,
        "expires_at": now + upstream_services().core.REFERENCE_CACHE_TTL_S,
        "size": size,
    }
    try:
        await redis.hset(
            cache_key,
            digest,
            json.dumps(item, separators=(",", ":")),
        )
        await redis.zadd(lru_key, {digest: now})
        await redis.expire(cache_key, upstream_services().core.REFERENCE_CACHE_TTL_S)
        await redis.expire(lru_key, upstream_services().core.REFERENCE_CACHE_TTL_S)
        await upstream_services().references.reference_cache_trim(
            redis, user_id=user_id
        )
    except Exception as exc:  # noqa: BLE001
        upstream_services().infrastructure.logger.debug(
            "reference cache store skipped digest=%s err=%r",
            digest[:12],
            exc,
        )


async def _reference_cache_delete(
    redis: Any,
    *,
    user_id: str,
    digest: str,
) -> None:
    cache_key, lru_key = upstream_services().references.reference_cache_keys(user_id)
    try:
        await redis.hdel(cache_key, digest)
        await redis.zrem(lru_key, digest)
    except Exception as exc:  # noqa: BLE001
        upstream_services().infrastructure.logger.debug(
            "reference cache delete skipped digest=%s err=%r",
            digest[:12],
            exc,
        )


async def _reference_cache_trim(redis: Any, *, user_id: str) -> None:
    cache_key, lru_key = upstream_services().references.reference_cache_keys(user_id)
    try:
        total = await redis.zcard(lru_key)
        overflow = int(total) - upstream_services().core.REFERENCE_CACHE_MAX_ENTRIES
        if overflow <= 0:
            return
        stale = await redis.zrange(lru_key, 0, overflow - 1)
        digests = [
            upstream_services().references.redis_text(item) for item in stale or []
        ]
        digests = [item for item in digests if item]
        if not digests:
            return
        await redis.hdel(cache_key, *digests)
        await redis.zrem(lru_key, *digests)
    except Exception as exc:  # noqa: BLE001
        upstream_services().infrastructure.logger.debug(
            "reference cache trim skipped err=%r", exc
        )


async def _reference_url_is_live(url: str) -> bool:
    candidate = url.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return False
    try:
        target = await upstream_services().infrastructure.resolve_public_http_target(
            candidate, allow_http=True
        )
        transport = (
            upstream_services().infrastructure.pinned_async_http_transport(target)
            if getattr(target, "resolved_ips", ())
            else None
        )
        client_kwargs: dict[str, Any] = {
            "follow_redirects": False,
            "trust_env": False,
            "timeout": upstream_services().infrastructure.httpx.Timeout(
                upstream_services().core.REFERENCE_CACHE_HEAD_TIMEOUT_S
            ),
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        async with upstream_services().infrastructure.httpx.AsyncClient(
            **client_kwargs
        ) as client:
            response = await client.head(target.url)
        return 200 <= response.status_code < 300
    except (ValueError, httpx.HTTPError, OSError) as exc:
        upstream_services().infrastructure.logger.debug(
            "reference cache HEAD failed url=%s err=%r",
            url,
            exc,
        )
        return False


async def _get_or_upload_reference(
    ref_bytes: bytes,
    mime: str,
    *,
    base_url: str,
    api_key: str,
    user_id: str | None,
) -> str | None:
    redis: Any | None = None
    digest = hashlib.sha256(ref_bytes).hexdigest()
    if user_id:
        try:
            pool = await upstream_services().infrastructure.provider_pool.get_pool()
            redis = upstream_services().providers.provider_pool_redis(pool)
        except Exception as exc:  # noqa: BLE001
            upstream_services().infrastructure.logger.debug(
                "reference cache redis unavailable err=%r", exc
            )

    if redis is not None and user_id:
        cached = await upstream_services().references.reference_cache_get(
            redis,
            user_id=user_id,
            digest=digest,
        )
        if cached:
            if await upstream_services().references.reference_url_is_live(cached):
                return cached
            await upstream_services().references.reference_cache_delete(
                redis,
                user_id=user_id,
                digest=digest,
            )

    uploaded = await upstream_services().references.push_reference_to_image_job(
        ref_bytes,
        mime,
        base_url=base_url,
        api_key=api_key,
    )
    if uploaded and redis is not None and user_id:
        await upstream_services().references.reference_cache_store(
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
) -> str | None:
    """Upload normalized bytes to the configured image-job reference endpoint."""
    if not base_url or not api_key:
        return None
    client = upstream_services().image_jobs.build_image_job_client(
        base_url,
        service_token=api_key,
        read_timeout_s=upstream_services().core.REFERENCE_PUSH_TIMEOUT_S,
    )
    try:
        uploaded = await client.upload_reference(
            raw,
            mime,
            trace_id=upstream_services().core.generate_trace_id(),
        )
        return uploaded.url if uploaded is not None else None
    except (httpx.HTTPError, OSError, ValueError) as exc:
        upstream_services().infrastructure.logger.warning(
            "reference push to image-job error: %r", exc
        )
        return None
    finally:
        await client.close()


async def _resolve_reference_image_urls(
    images: list[bytes] | None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    user_id: str | None = None,
) -> list[str]:
    if not images:
        return []
    output: list[str] = []
    for raw in images:
        ref_bytes, mime = upstream_services().references.normalize_reference_image(raw)
        ref_url: str | None = None
        if base_url and api_key:
            ref_url = await upstream_services().references.get_or_upload_reference(
                ref_bytes,
                mime,
                base_url=base_url,
                api_key=api_key,
                user_id=user_id,
            )
        if ref_url:
            output.append(ref_url)
        else:
            encoded = base64.b64encode(ref_bytes).decode("ascii")
            output.append(f"data:{mime};base64,{encoded}")
    return output


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
