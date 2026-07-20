"""Provider binding and video request media preparation."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select

from lumen_core.models import Image, VideoGeneration
from lumen_core.video_providers import (
    parse_video_provider_config_json,
    select_video_provider,
)

from ...video_upstream import VideoReferenceMedia, VideoUpstreamError
from ._facade import _g


async def provider_config():
    raw_video = await _g.runtime_settings.resolve("video.providers")
    raw_shared = await _g.runtime_settings.resolve("providers")
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
        "binding_fingerprint": _g._provider_binding_fingerprint(provider),
        "captured_at": _g._now().isoformat(),
    }
    generation.upstream_request = request


def _validate_provider_identity(
    generation: VideoGeneration,
    provider: Any,
) -> None:
    if generation.provider_kind and provider.kind != generation.provider_kind:
        raise _g._provider_binding_error(
            generation,
            "persisted video provider kind no longer matches configuration",
            current_provider_name=provider.name,
        )
    snapshot = _g._provider_snapshot(generation)
    if snapshot.get("provider_name") not in {None, provider.name}:
        raise _g._provider_binding_error(
            generation,
            "video provider snapshot name does not match persisted provider",
            current_provider_name=provider.name,
        )
    if snapshot.get("provider_kind") not in {None, provider.kind}:
        raise _g._provider_binding_error(
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
    snapshot = _g._provider_snapshot(generation)
    snapshot_base_url = snapshot.get("base_url")
    if isinstance(snapshot_base_url, str) and snapshot_base_url.rstrip(
        "/"
    ) != provider.base_url.rstrip("/"):
        raise _g._provider_binding_error(
            generation,
            "video provider endpoint changed after task submission",
            current_provider_name=provider.name,
        )
    snapshot_binding = snapshot.get("binding_fingerprint")
    if isinstance(
        snapshot_binding, str
    ) and snapshot_binding != _g._provider_binding_fingerprint(provider):
        raise _g._provider_binding_error(
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
    raise _g._provider_binding_error(
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
    raise _g._provider_binding_error(
        generation,
        "persisted video provider is no longer configured; refusing provider switch",
    )


async def provider_for_generation(generation: VideoGeneration):
    providers = await _g._provider_config()
    provider_name = (generation.provider_name or "").strip()
    if generation.provider_task_id and not provider_name:
        raise _g._provider_binding_error(
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
    if not key:
        raise RuntimeError("i2v input image storage key missing")
    return await _g.storage.aget_bytes(key), mime


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


def _reference_storage_key(item: dict[str, Any]) -> str | None:
    return _clean_optional_text(
        item.get("upstream_reference_storage_key") or item.get("storage_key")
    )


def _reference_mime(item: dict[str, Any]) -> str | None:
    upstream_mime = _clean_optional_text(item.get("upstream_reference_mime"))
    return upstream_mime or _clean_optional_text(item.get("mime"))


async def _reference_image_bytes(
    *,
    clean_url: str | None,
    storage_key: str | None,
) -> bytes | None:
    if not storage_key:
        return None
    if not clean_url:
        return await _g.storage.aget_bytes(storage_key)
    try:
        return await _g.storage.aget_bytes(storage_key)
    except Exception:
        _g.logger.warning(
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
    storage_key = _reference_storage_key(item)
    _validate_reference_location(
        kind=kind,
        clean_url=clean_url,
        storage_key=storage_key,
    )
    mime = _reference_mime(item)
    data = (
        await _reference_image_bytes(
            clean_url=clean_url,
            storage_key=storage_key,
        )
        if kind == "image"
        else None
    )
    if kind == "image" and data is None and not clean_url and storage_key:
        data = await _g.storage.aget_bytes(storage_key)
    return VideoReferenceMedia(  # type: ignore[arg-type]
        kind=kind,
        data=data,
        mime=mime,
        url=clean_url,
        label=_clean_optional_text(item.get("label")),
        ref_id=_clean_optional_text(item.get("ref_id"), lowercase=True),
    )


async def reference_media_bytes(
    generation: VideoGeneration,
) -> list[VideoReferenceMedia]:
    raw = (generation.upstream_request or {}).get("reference_media")
    if generation.action != "reference":
        return []
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("reference media snapshot missing")
    result: list[VideoReferenceMedia] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        reference = await _reference_media_from_item(item)
        if reference is not None:
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
