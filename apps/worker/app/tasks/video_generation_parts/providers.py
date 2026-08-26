"""Provider binding and video request media preparation."""

from __future__ import annotations

from .runtime import video_ports
import hashlib
from typing import Any

from sqlalchemy import select

from lumen_core.models import Image, VideoGeneration
from lumen_core.video_providers import (
    parse_video_provider_config_json,
    select_video_provider,
)

from ...video_upstream_content import SEEDANCE_INLINE_REFERENCE_RAW_MAX_BYTES
from ...video_upstream_service import (
    SEEDANCE_INLINE_IMAGE_MAX_BYTES,
    VideoReferenceMedia,
    VideoUpstreamError,
)


async def provider_config():
    raw_video = await video_ports().provider.runtime_settings.resolve("video.providers")
    raw_shared = await video_ports().provider.runtime_settings.resolve("providers")
    providers, _proxies, errors = parse_video_provider_config_json(
        raw_video,
        shared_provider_raw=raw_shared,
    )
    if errors:
        raise RuntimeError("; ".join(errors))
    return providers


def provider_binding_error(
    generation: VideoGeneration,
    message: str,
    *,
    current_provider_name: str | None = None,
) -> VideoUpstreamError:
    return VideoUpstreamError(
        message,
        error_code="provider_snapshot_unavailable",
        status_code=422,
        raw={
            "provider_name": generation.provider_name,
            "provider_kind": generation.provider_kind,
            "provider_task_id": generation.provider_task_id,
            "current_provider_name": current_provider_name,
        },
    )


def provider_snapshot(generation: VideoGeneration) -> dict[str, Any]:
    raw_request = getattr(generation, "upstream_request", None)
    request = raw_request if isinstance(raw_request, dict) else {}
    raw_snapshot = request.get("provider_snapshot")
    snapshot = dict(raw_snapshot) if isinstance(raw_snapshot, dict) else {}
    for key in ("provider_name", "provider_kind", "upstream_model"):
        value = snapshot.get(key)
        if isinstance(value, str) and value.strip():
            snapshot[key] = value.strip()
            continue
        fallback = request.get(key)
        if isinstance(fallback, str) and fallback.strip():
            snapshot[key] = fallback.strip()
        else:
            snapshot.pop(key, None)
    base_url = snapshot.get("base_url")
    if isinstance(base_url, str) and base_url.strip():
        snapshot["base_url"] = base_url.strip().rstrip("/")
    else:
        snapshot.pop("base_url", None)
    return snapshot


def provider_binding_fingerprint(provider: Any) -> str:
    parts = (
        str(provider.kind),
        str(provider.base_url).rstrip("/"),
        str(provider.api_key),
        str(provider.proxy_name or ""),
    )
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def persist_provider_snapshot(
    generation: VideoGeneration,
    provider: Any,
    *,
    upstream_model: str,
) -> None:
    raw_request = getattr(generation, "upstream_request", None)
    request = dict(raw_request) if isinstance(raw_request, dict) else {}
    request["provider_name"] = provider.name
    request["provider_kind"] = provider.kind
    request["upstream_model"] = upstream_model
    request["provider_snapshot"] = {
        "provider_name": provider.name,
        "provider_kind": provider.kind,
        "base_url": provider.base_url.rstrip("/"),
        "proxy_name": provider.proxy_name,
        "upstream_model": upstream_model,
        "binding_fingerprint": video_ports().provider._provider_binding_fingerprint(
            provider
        ),
        "captured_at": video_ports().operations._now().isoformat(),
    }
    generation.upstream_request = request


def _validate_provider_identity(
    generation: VideoGeneration,
    provider: Any,
) -> None:
    if generation.provider_kind and provider.kind != generation.provider_kind:
        raise video_ports().provider._provider_binding_error(
            generation,
            "persisted video provider kind no longer matches configuration",
            current_provider_name=provider.name,
        )
    snapshot = video_ports().provider._provider_snapshot(generation)
    if snapshot.get("provider_name") not in {None, provider.name}:
        raise video_ports().provider._provider_binding_error(
            generation,
            "video provider snapshot name does not match persisted provider",
            current_provider_name=provider.name,
        )
    if snapshot.get("provider_kind") not in {None, provider.kind}:
        raise video_ports().provider._provider_binding_error(
            generation,
            "video provider snapshot kind no longer matches configuration",
            current_provider_name=provider.name,
        )


def _validate_submitted_provider_binding(
    generation: VideoGeneration,
    provider: Any,
) -> None:
    if not generation.provider_task_id:
        return
    snapshot = video_ports().provider._provider_snapshot(generation)
    snapshot_base_url = snapshot.get("base_url")
    if isinstance(snapshot_base_url, str) and snapshot_base_url.rstrip(
        "/"
    ) != provider.base_url.rstrip("/"):
        raise video_ports().provider._provider_binding_error(
            generation,
            "video provider endpoint changed after task submission",
            current_provider_name=provider.name,
        )
    snapshot_binding = snapshot.get("binding_fingerprint")
    if isinstance(
        snapshot_binding, str
    ) and snapshot_binding != video_ports().provider._provider_binding_fingerprint(
        provider
    ):
        raise video_ports().provider._provider_binding_error(
            generation,
            "video provider credentials or route changed after task submission",
            current_provider_name=provider.name,
        )


def _validate_provider_support(
    generation: VideoGeneration,
    provider: Any,
) -> None:
    if generation.provider_task_id:
        return
    if provider.supports(generation.model, generation.action):
        return
    raise video_ports().provider._provider_binding_error(
        generation,
        "persisted video provider is no longer enabled for this request",
        current_provider_name=provider.name,
    )


def _configured_provider(
    generation: VideoGeneration,
    providers: list[Any],
) -> Any:
    provider_name = (generation.provider_name or "").strip()
    for provider in providers:
        if provider.name != provider_name:
            continue
        _validate_provider_identity(generation, provider)
        _validate_submitted_provider_binding(generation, provider)
        _validate_provider_support(generation, provider)
        return provider
    raise video_ports().provider._provider_binding_error(
        generation,
        "persisted video provider is no longer configured; refusing provider switch",
    )


async def provider_for_generation(generation: VideoGeneration):
    providers = await video_ports().provider._provider_config()
    provider_name = (generation.provider_name or "").strip()
    if generation.provider_task_id and not provider_name:
        raise video_ports().provider._provider_binding_error(
            generation,
            "submitted video task has no persisted provider identity",
        )
    if provider_name:
        return _configured_provider(generation, providers)
    provider = select_video_provider(
        providers,
        model=generation.model,
        action=generation.action,
    )
    if provider is None:
        raise RuntimeError("no enabled video provider supports this model/action")
    return provider


async def input_image_bytes(
    session: Any,
    generation: VideoGeneration,
) -> tuple[bytes | None, str | None]:
    if generation.action != "i2v":
        return None, None
    key = generation.input_image_storage_key
    mime: str | None = None
    if generation.input_image_id:
        image = (
            await session.execute(
                select(Image).where(Image.id == generation.input_image_id)
            )
        ).scalar_one_or_none()
        if image is not None:
            mime = image.mime
            key = key or image.storage_key
        commit = getattr(session, "commit", None)
        if callable(commit):
            await commit()
    if not key:
        raise RuntimeError("i2v input image storage key missing")
    return await video_ports().store.storage.aget_bytes(key), mime


def input_image_url(generation: VideoGeneration) -> str | None:
    request = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    raw = request.get("input_image_url")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _clean_optional_text(value: Any, *, lowercase: bool = False) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    return cleaned.lower() if lowercase else cleaned


def _reference_source(
    item: dict[str, Any],
    *,
    clean_url: str | None,
) -> tuple[str | None, str | None]:
    upstream_storage_key = _clean_optional_text(
        item.get("upstream_reference_storage_key")
    )
    upstream_variant = _clean_optional_text(item.get("upstream_reference_variant"))
    lazy_variant = bool(clean_url and upstream_variant and not upstream_storage_key)
    if upstream_storage_key or lazy_variant:
        return (
            upstream_storage_key,
            _clean_optional_text(item.get("upstream_reference_mime")),
        )
    return (
        _clean_optional_text(item.get("storage_key")),
        _clean_optional_text(item.get("mime")),
    )


def _reference_storage_key(item: dict[str, Any]) -> str | None:
    return _reference_source(
        item,
        clean_url=_clean_optional_text(item.get("url")),
    )[0]


def _reference_mime(item: dict[str, Any]) -> str | None:
    return _reference_source(
        item,
        clean_url=_clean_optional_text(item.get("url")),
    )[1]


async def _reference_image_bytes(
    *,
    clean_url: str | None,
    storage_key: str | None,
) -> bytes | None:
    if not storage_key:
        return None
    if not clean_url:
        return await video_ports().store.storage.aget_bytes(storage_key)
    try:
        return await video_ports().store.storage.aget_bytes(storage_key)
    except Exception:
        video_ports().operations.logger.warning(
            "reference image variant bytes unavailable; "
            "falling back to url storage_key=%s",
            storage_key,
            exc_info=True,
        )
        return None


def _validate_reference_location(
    *,
    kind: str,
    clean_url: str | None,
    storage_key: str | None,
) -> None:
    if clean_url:
        return
    if kind == "audio":
        raise RuntimeError("reference audio snapshot missing public URL")
    if kind == "video":
        raise RuntimeError("reference video snapshot missing public URL")
    if not storage_key:
        raise RuntimeError("reference media storage key missing")


async def _reference_media_from_item(
    item: dict[str, Any],
) -> VideoReferenceMedia | None:
    kind = item.get("kind")
    if kind not in {"image", "video", "audio"}:
        return None
    clean_url = _clean_optional_text(item.get("url"))
    storage_key, mime = _reference_source(item, clean_url=clean_url)
    _validate_reference_location(
        kind=kind,
        clean_url=clean_url,
        storage_key=storage_key,
    )
    data = (
        await _reference_image_bytes(
            clean_url=clean_url,
            storage_key=storage_key,
        )
        if kind == "image"
        else None
    )
    if kind == "image" and data is None and not clean_url and storage_key:
        data = await video_ports().store.storage.aget_bytes(storage_key)
    return VideoReferenceMedia(  # type: ignore[arg-type]
        kind=kind,
        data=data,
        mime=mime,
        url=clean_url,
        label=_clean_optional_text(item.get("label")),
        ref_id=_clean_optional_text(item.get("ref_id"), lowercase=True),
    )


def _declared_inline_reference_image_bytes(item: dict[str, Any]) -> int:
    if item.get("kind") != "image" or _clean_optional_text(item.get("url")):
        return 0
    raw = item.get("size_bytes")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def _validate_declared_inline_reference_sizes(
    items: list[dict[str, Any]],
) -> None:
    declared_total = 0
    for item in items:
        declared = _declared_inline_reference_image_bytes(item)
        if declared > SEEDANCE_INLINE_IMAGE_MAX_BYTES:
            raise VideoUpstreamError(
                "reference image is too large for inline video submission",
                error_code="invalid_input",
                status_code=413,
                raw={
                    "actual_bytes": declared,
                    "max_bytes": SEEDANCE_INLINE_IMAGE_MAX_BYTES,
                },
            )
        declared_total += declared
    if declared_total > SEEDANCE_INLINE_REFERENCE_RAW_MAX_BYTES:
        raise VideoUpstreamError(
            "reference images are too large for safe inline video submission",
            error_code="invalid_input",
            status_code=413,
            raw={
                "actual_bytes": declared_total,
                "max_bytes": SEEDANCE_INLINE_REFERENCE_RAW_MAX_BYTES,
            },
        )


async def reference_media_bytes(
    generation: VideoGeneration,
) -> list[VideoReferenceMedia]:
    raw = (generation.upstream_request or {}).get("reference_media")
    if generation.action != "reference":
        return []
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("reference media snapshot missing")
    items = [item for item in raw if isinstance(item, dict)]
    _validate_declared_inline_reference_sizes(items)
    result: list[VideoReferenceMedia] = []
    inline_bytes = 0
    for item in items:
        reference = await _reference_media_from_item(item)
        if reference is not None:
            if reference.kind == "image" and reference.data:
                inline_bytes += len(reference.data)
                if inline_bytes > SEEDANCE_INLINE_REFERENCE_RAW_MAX_BYTES:
                    raise VideoUpstreamError(
                        "reference images are too large for safe inline video submission",
                        error_code="invalid_input",
                        status_code=413,
                        raw={
                            "actual_bytes": inline_bytes,
                            "max_bytes": SEEDANCE_INLINE_REFERENCE_RAW_MAX_BYTES,
                        },
                    )
            result.append(reference)
    if not result:
        raise RuntimeError("reference media snapshot has no usable entries")
    return result


__all__ = [
    "input_image_bytes",
    "input_image_url",
    "persist_provider_snapshot",
    "provider_binding_error",
    "provider_binding_fingerprint",
    "provider_config",
    "provider_for_generation",
    "provider_snapshot",
    "reference_media_bytes",
]
