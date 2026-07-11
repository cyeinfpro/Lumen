"""Reference image normalization, caching, and sidecar upload helpers."""

from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
from typing import Any

import httpx

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    """Resolve compatibility dependencies at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


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
    facade = _facade()
    if len(raw) > facade._MAX_REFERENCE_IMAGE_BYTES:
        raise facade.UpstreamError(
            "reference image exceeds size limit",
            error_code=facade.EC.REFERENCE_IMAGE_TOO_LARGE.value,
            status_code=413,
            payload={
                "max_bytes": facade._MAX_REFERENCE_IMAGE_BYTES,
                "actual_bytes": len(raw),
            },
        )

    image_api = facade.PILImage
    try:
        with image_api.open(io.BytesIO(raw)) as image:
            width, height = image.size
            actual_pixels = max(width, 0) * max(height, 0)
            if (
                width <= 0
                or height <= 0
                or actual_pixels > facade._MAX_REFERENCE_IMAGE_PIXELS
            ):
                raise facade.UpstreamError(
                    "reference image exceeds pixel limit",
                    error_code=facade.EC.REFERENCE_IMAGE_TOO_LARGE.value,
                    status_code=413,
                    payload={
                        "max_pixels": facade._MAX_REFERENCE_IMAGE_PIXELS,
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
        if len(normalized) > facade._MAX_NORMALIZED_IMAGE_BYTES:
            raise facade.UpstreamError(
                "normalized reference image exceeds size limit",
                error_code=facade.EC.REFERENCE_IMAGE_TOO_LARGE.value,
                status_code=413,
                payload={
                    "max_bytes": facade._MAX_NORMALIZED_IMAGE_BYTES,
                    "actual_bytes": len(normalized),
                },
            )
        return normalized, "image/webp"
    except facade.UpstreamError:
        raise
    except image_api.DecompressionBombError as exc:
        raise facade.UpstreamError(
            f"reference image decompression bomb: {exc}",
            error_code=facade.EC.REFERENCE_IMAGE_TOO_LARGE.value,
            status_code=413,
            payload={"max_pixels": facade._MAX_REFERENCE_IMAGE_PIXELS},
        ) from exc
    except (facade.UnidentifiedImageError, OSError, ValueError) as exc:
        raise facade.UpstreamError(
            f"reference image not decodable: {exc}",
            error_code=facade.EC.BAD_REFERENCE_IMAGE.value,
            status_code=400,
        ) from exc


def _reference_cache_keys(user_id: str) -> tuple[str, str]:
    facade = _facade()
    cache_key = f"{facade._REFERENCE_CACHE_KEY_PREFIX}{user_id}"
    return cache_key, f"{cache_key}{facade._REFERENCE_CACHE_LRU_SUFFIX}"


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
    facade = _facade()
    cache_key, lru_key = facade._reference_cache_keys(user_id)
    try:
        raw = await redis.hget(cache_key, digest)
        text = facade._redis_text(raw)
        if not text:
            return None
        item = json.loads(text)
        if not isinstance(item, dict):
            return None
        expires_at = float(item.get("expires_at") or 0.0)
        url = item.get("upload_url")
        if not isinstance(url, str) or not url:
            return None
        if expires_at <= facade.time.time():
            await facade._reference_cache_delete(
                redis,
                user_id=user_id,
                digest=digest,
            )
            return None
        await redis.zadd(lru_key, {digest: facade.time.time()})
        await redis.expire(cache_key, facade._REFERENCE_CACHE_TTL_S)
        await redis.expire(lru_key, facade._REFERENCE_CACHE_TTL_S)
        return url
    except Exception as exc:  # noqa: BLE001
        facade.logger.debug(
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
    facade = _facade()
    cache_key, lru_key = facade._reference_cache_keys(user_id)
    now = facade.time.time()
    item = {
        "upload_url": url,
        "expires_at": now + facade._REFERENCE_CACHE_TTL_S,
        "size": size,
    }
    try:
        await redis.hset(
            cache_key,
            digest,
            json.dumps(item, separators=(",", ":")),
        )
        await redis.zadd(lru_key, {digest: now})
        await redis.expire(cache_key, facade._REFERENCE_CACHE_TTL_S)
        await redis.expire(lru_key, facade._REFERENCE_CACHE_TTL_S)
        await facade._reference_cache_trim(redis, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        facade.logger.debug(
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
    facade = _facade()
    cache_key, lru_key = facade._reference_cache_keys(user_id)
    try:
        await redis.hdel(cache_key, digest)
        await redis.zrem(lru_key, digest)
    except Exception as exc:  # noqa: BLE001
        facade.logger.debug(
            "reference cache delete skipped digest=%s err=%r",
            digest[:12],
            exc,
        )


async def _reference_cache_trim(redis: Any, *, user_id: str) -> None:
    facade = _facade()
    cache_key, lru_key = facade._reference_cache_keys(user_id)
    try:
        total = await redis.zcard(lru_key)
        overflow = int(total) - facade._REFERENCE_CACHE_MAX_ENTRIES
        if overflow <= 0:
            return
        stale = await redis.zrange(lru_key, 0, overflow - 1)
        digests = [facade._redis_text(item) for item in stale or []]
        digests = [item for item in digests if item]
        if not digests:
            return
        await redis.hdel(cache_key, *digests)
        await redis.zrem(lru_key, *digests)
    except Exception as exc:  # noqa: BLE001
        facade.logger.debug("reference cache trim skipped err=%r", exc)


async def _reference_url_is_live(url: str) -> bool:
    facade = _facade()
    candidate = url.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return False
    try:
        target = await facade.resolve_public_http_target(candidate, allow_http=True)
        transport = (
            facade.pinned_async_http_transport(target)
            if getattr(target, "resolved_ips", ())
            else None
        )
        client_kwargs: dict[str, Any] = {
            "follow_redirects": False,
            "trust_env": False,
            "timeout": facade.httpx.Timeout(
                facade._REFERENCE_CACHE_HEAD_TIMEOUT_S
            ),
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        async with facade.httpx.AsyncClient(**client_kwargs) as client:
            response = await client.head(target.url)
        return 200 <= response.status_code < 300
    except (ValueError, httpx.HTTPError, OSError) as exc:
        facade.logger.debug(
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
    facade = _facade()
    redis: Any | None = None
    digest = hashlib.sha256(ref_bytes).hexdigest()
    if user_id:
        try:
            pool = await facade.provider_pool.get_pool()
            redis = facade._provider_pool_redis(pool)
        except Exception as exc:  # noqa: BLE001
            facade.logger.debug("reference cache redis unavailable err=%r", exc)

    if redis is not None and user_id:
        cached = await facade._reference_cache_get(
            redis,
            user_id=user_id,
            digest=digest,
        )
        if cached:
            if await facade._reference_url_is_live(cached):
                return cached
            await facade._reference_cache_delete(
                redis,
                user_id=user_id,
                digest=digest,
            )

    uploaded = await facade._push_reference_to_image_job(
        ref_bytes,
        mime,
        base_url=base_url,
        api_key=api_key,
    )
    if uploaded and redis is not None and user_id:
        await facade._reference_cache_store(
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
    facade = _facade()
    if not base_url or not api_key:
        return None
    url = base_url.rstrip("/") + "/v1/refs"
    headers = {
        "Content-Type": mime,
        "Authorization": f"Bearer {api_key}",
    }
    try:
        async with facade.httpx.AsyncClient(
            timeout=facade.httpx.Timeout(facade._REFERENCE_PUSH_TIMEOUT_S),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(url, content=raw, headers=headers)
        if response.status_code != 200:
            facade.logger.warning(
                "reference push to image-job failed status=%d url=%s body=%s",
                response.status_code,
                url,
                response.text[:200],
            )
            return None
        try:
            data = response.json()
        except ValueError:
            facade.logger.warning(
                "reference push returned non-JSON: %s",
                response.text[:200],
            )
            return None
        public_url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(public_url, str) or not public_url:
            facade.logger.warning("reference push response missing url: %r", data)
            return None
        return public_url
    except (httpx.HTTPError, OSError) as exc:
        facade.logger.warning("reference push to image-job error: %r", exc)
        return None


async def _resolve_reference_image_urls(
    images: list[bytes] | None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    user_id: str | None = None,
) -> list[str]:
    facade = _facade()
    if not images:
        return []
    output: list[str] = []
    for raw in images:
        ref_bytes, mime = facade._normalize_reference_image(raw)
        ref_url: str | None = None
        if base_url and api_key:
            ref_url = await facade._get_or_upload_reference(
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
