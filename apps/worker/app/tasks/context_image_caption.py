"""Image caption helpers for compressed conversation context.

The context summary pipeline can keep old image references useful by caching a
short visual caption on the image row. Failures are deliberately soft: a missing
file or upstream issue should degrade to the old ``[user_image image_id=...]``
placeholder instead of blocking summarization.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx
from sqlalchemy import update

from lumen_core.constants import GenerationErrorCode as EC
from lumen_core.models import Image
from lumen_core.providers import ProviderProxyDefinition, resolve_provider_proxy_url

from ..storage import storage
from ..upstream import UpstreamError, _auth_headers

logger = logging.getLogger(__name__)


_CAPTION_INSTRUCTIONS = (
    "用不超过 100 字描述这张图的视觉内容，包含主体、场景、风格、颜色、"
    "以及图中文字（如有）。直接输出描述，不要解释。"
)
_CAPTION_HTTP_TIMEOUT_S = 15.0
_PER_PROVIDER_RETRY_ATTEMPTS = 2
_PER_PROVIDER_RETRY_BACKOFF_S = 1.0
_MAX_CAPTION_CHARS = 200


def _cached_caption(image_record: Any) -> str | None:
    caption_jsonb = getattr(image_record, "caption_jsonb", None)
    if isinstance(caption_jsonb, dict):
        caption = caption_jsonb.get("caption") or caption_jsonb.get("text")
        if isinstance(caption, str) and caption.strip():
            return caption.strip()
    elif isinstance(caption_jsonb, str) and caption_jsonb.strip():
        return caption_jsonb.strip()

    metadata = getattr(image_record, "metadata_jsonb", None)
    if isinstance(metadata, dict):
        caption = metadata.get("caption")
        if isinstance(caption, str) and caption.strip():
            return caption.strip()
    return None


def _sanitize_caption(raw: str) -> str | None:
    caption = " ".join((raw or "").strip().split())
    for prefix in ("Caption:", "caption:", "描述：", "描述:", "图片描述：", "图片描述:"):
        if caption.startswith(prefix):
            caption = caption[len(prefix) :].strip()
    if not caption:
        return None
    return caption[:_MAX_CAPTION_CHARS]


def _extract_response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text_part = part.get("text") or part.get("output_text")
                if isinstance(text_part, str) and text_part:
                    chunks.append(text_part)
    return "".join(chunks).strip()


def _responses_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return f"{base}/responses"


async def _image_data_url(image_record: Any) -> str | None:
    storage_key = getattr(image_record, "storage_key", None)
    if not isinstance(storage_key, str) or not storage_key:
        return None
    try:
        raw = await storage.aget_bytes(storage_key)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "context image caption skipped: cannot read image id=%s key=%s err=%s",
            getattr(image_record, "id", None),
            storage_key,
            exc,
        )
        return None
    if not raw:
        return None
    mime = getattr(image_record, "mime", None)
    if not isinstance(mime, str) or not mime.startswith("image/"):
        mime = "image/png"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def _call_upstream_one(
    image_record: Any,
    image_url: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    proxy: ProviderProxyDefinition | None = None,
) -> str:
    body = {
        "model": model,
        "instructions": _CAPTION_INSTRUCTIONS,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _CAPTION_INSTRUCTIONS},
                    {"type": "input_image", "image_url": image_url},
                ],
            }
        ],
        "metadata": {"image_id": str(getattr(image_record, "id", ""))},
        "stream": False,
        "store": False,
        "max_output_tokens": 300,
    }
    try:
        proxy_url = await resolve_provider_proxy_url(proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=_CAPTION_HTTP_TIMEOUT_S,
                write=_CAPTION_HTTP_TIMEOUT_S,
                pool=10.0,
            ),
            proxy=proxy_url,
        ) as client:
            resp = await client.post(
                _responses_url(base_url),
                json=body,
                headers={**_auth_headers(api_key), "content-type": "application/json"},
            )
    except httpx.TimeoutException as exc:
        raise UpstreamError(
            "context image caption upstream timeout",
            error_code=EC.UPSTREAM_TIMEOUT.value,
            status_code=None,
        ) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(
            f"context image caption upstream network error: {exc}",
            error_code=EC.UPSTREAM_ERROR.value,
            status_code=None,
        ) from exc

    if resp.status_code >= 400:
        raise UpstreamError(
            f"context image caption upstream http {resp.status_code}",
            error_code=EC.UPSTREAM_ERROR.value,
            status_code=resp.status_code,
        )

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise UpstreamError(
            "context image caption upstream returned invalid json",
            error_code=EC.BAD_RESPONSE.value,
            status_code=resp.status_code,
        ) from exc
    return _extract_response_text(payload)


async def _call_upstream(image_record: Any, image_url: str, *, model: str) -> str | None:
    from ..provider_pool import get_pool
    from ..retry import is_retriable as classify_retriable

    pool = await get_pool()
    providers = await pool.select(route="text")
    last_exc: BaseException | None = None
    attempted_providers: list[str] = []

    for provider in providers:
        attempted_providers.append(provider.name)
        for attempt in range(_PER_PROVIDER_RETRY_ATTEMPTS):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "base_url": provider.base_url,
                    "api_key": provider.api_key,
                }
                proxy = getattr(provider, "proxy", None)
                if proxy is not None:
                    kwargs["proxy"] = proxy
                return await _call_upstream_one(image_record, image_url, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                if not decision.retriable:
                    break
                if attempt + 1 < _PER_PROVIDER_RETRY_ATTEMPTS:
                    await asyncio.sleep(_PER_PROVIDER_RETRY_BACKOFF_S * (2**attempt))

    if last_exc is not None:
        logger.info(
            "context image caption upstream failed image_id=%s providers=%s err=%.300s",
            getattr(image_record, "id", None),
            ",".join(attempted_providers) or "<none>",
            str(last_exc),
        )
    return None


async def _write_caption_cache(session: Any, image_record: Any, caption: str) -> None:
    image_id = getattr(image_record, "id", None)
    if not image_id:
        return
    try:
        target = image_record
        session_get = getattr(session, "get", None)
        if callable(session_get):
            loaded = await session_get(Image, image_id)
            if loaded is not None:
                target = loaded

        metadata = getattr(target, "metadata_jsonb", None)
        next_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        next_metadata["caption"] = caption
        setattr(target, "metadata_jsonb", next_metadata)
        if target is not image_record:
            try:
                setattr(image_record, "metadata_jsonb", next_metadata)
            except Exception:  # noqa: BLE001
                pass

        # Real AsyncSession objects use the managed Image row above, which keeps
        # the identity map coherent. This fallback is for lightweight test/session
        # doubles that expose execute() but not get().
        if not callable(session_get):
            execute = getattr(session, "execute", None)
            if callable(execute):
                await execute(
                    update(Image)
                    .where(Image.id == image_id)
                    .values(metadata_jsonb=next_metadata),
                    {"caption": caption, "image_id": image_id},
                )

        flush = getattr(session, "flush", None)
        if flush is not None:
            await flush()
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "context image caption cache write skipped image_id=%s err=%s",
            image_id,
            exc,
        )


async def ensure_caption_for_image(
    session: Any, image_record: Any, *, model: str
) -> str | None:
    """Return a cached or newly generated short caption for one image.

    This function never raises for expected captioning failures. It returns
    ``None`` when the image cannot be read, upstream fails, or the model returns
    no usable text.
    """
    cached = _cached_caption(image_record)
    if cached:
        return cached

    image_url = await _image_data_url(image_record)
    if image_url is None:
        return None

    try:
        async with asyncio.timeout(_CAPTION_HTTP_TIMEOUT_S):
            caption = _sanitize_caption(
                await _call_upstream(image_record, image_url, model=model) or ""
            )
    except (TimeoutError, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "context image caption failed image_id=%s err=%s",
            getattr(image_record, "id", None),
            exc,
        )
        return None

    if caption is None:
        return None
    await _write_caption_cache(session, image_record, caption)
    return caption


async def batch_caption_images(
    session: Any,
    image_records: list[Any],
    *,
    model: str,
    max_concurrency: int = 4,
    total_timeout: float = 45,
) -> dict[str, str]:
    """Caption many images with a hard total deadline.

    Returns only successful captions keyed by image id. Timed-out pending tasks
    are cancelled, while completed successes are preserved.
    """
    if not image_records:
        return {}

    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    results: dict[str, str] = {}

    async def _one(record: Any) -> tuple[str | None, str | None, Any | None, bool]:
        async with semaphore:
            cached = _cached_caption(record)
            if cached:
                image_id = getattr(record, "id", None)
                return (str(image_id) if image_id else None), cached, None, False

            image_url = await _image_data_url(record)
            if image_url is None:
                image_id = getattr(record, "id", None)
                return (str(image_id) if image_id else None), None, record, False
            try:
                async with asyncio.timeout(_CAPTION_HTTP_TIMEOUT_S):
                    caption = _sanitize_caption(
                        await _call_upstream(record, image_url, model=model) or ""
                    )
            except (TimeoutError, asyncio.CancelledError):
                raise
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "context image caption failed image_id=%s err=%s",
                    getattr(record, "id", None),
                    exc,
                )
                caption = None
            image_id = getattr(record, "id", None)
            return (str(image_id) if image_id else None), caption, record, bool(caption)

    tasks = [asyncio.create_task(_one(record)) for record in image_records]
    done: set[asyncio.Task[tuple[str | None, str | None, Any | None, bool]]] = set()
    pending: set[asyncio.Task[tuple[str | None, str | None, Any | None, bool]]] = set(tasks)
    cache_writes = 0
    try:
        done, pending = await asyncio.wait(tasks, timeout=total_timeout)
        for task in done:
            try:
                image_id, caption, record, should_cache = task.result()
            except Exception as exc:  # noqa: BLE001
                logger.info("context image batch caption item failed err=%s", exc)
                continue
            if image_id and caption:
                results[image_id] = caption
                if should_cache and record is not None:
                    await _write_caption_cache(session, record, caption)
                    cache_writes += 1
        if cache_writes:
            commit = getattr(session, "commit", None)
            if callable(commit):
                await commit()
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return results


__all__ = ["batch_caption_images", "ensure_caption_for_image"]
