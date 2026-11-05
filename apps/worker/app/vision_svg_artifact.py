"""Non-tool, private SVG artifacts from durable Completion receipts. No model or billing calls."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import io
import uuid
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from typing import Any
from PIL import Image as PILImage
from lumen_core.models import Image, ImageVariant, Message
from lumen_core.svg_output import validate_static_svg
from . import image_artifacts
from .storage import storage

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_PIXELS = 16_000_000
MAX_SVG_BYTES = 1024 * 1024
MAX_SVG_ELEMENTS = 10_000
VALIDATOR_VERSION = "lumen-static-svg-vision-v1"
SVG_INSTRUCTIONS = (
    "Reconstruct the supplied image as one self-contained static SVG. Follow the user's "
    "requested changes. Return only UTF-8 SVG XML without Markdown or commentary, with "
    "numeric width/height and viewBox. Use vectors only: no scripts, events, CSS, embedded "
    "images, external resources, animation, DTDs, entities, or foreignObject."
)

def safe_endpoint(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    # Provenance stores only endpoint path, never host credentials/query/fragment.
    path = parsed.path
    return path[:256] if path else None

def is_vision_svg(completion: Any) -> bool:
    return (getattr(completion, "upstream_request", None) or {}).get("artifact_format") == "svg"

def artifact_identity(completion_id: str, response_id: str) -> str:
    if not response_id:
        raise ValueError("SVG requires a durable upstream response identity")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lumen:vision-svg:{completion_id}:{response_id}"))

def validate_svg_text(text: str) -> bytes:
    raw = text.encode("utf-8")
    if not raw or len(raw) > MAX_SVG_BYTES:
        raise ValueError("SVG output exceeds the 1 MiB limit or is empty")
    width, height = validate_static_svg(raw)
    if width * height > 16_000_000 or max(width, height) > 8192:
        raise ValueError("SVG output exceeds the render pixel limit")
    if sum(1 for _ in ET.fromstring(raw).iter()) > MAX_SVG_ELEMENTS:
        raise ValueError("SVG output exceeds the element limit")
    return raw

def source_data_url(raw: bytes, expected_sha: str | None = None) -> str:
    if not raw or len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("SVG source exceeds the 20 MiB limit")
    if expected_sha and hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("SVG source changed after submission")
    with PILImage.open(io.BytesIO(raw)) as image:
        if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
            raise ValueError("SVG source must be one static PNG, JPEG or WebP")
        if image.width < 1 or image.height < 1 or image.width * image.height > MAX_SOURCE_PIXELS:
            raise ValueError("SVG source exceeds the 16 megapixel limit")
        mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image.format]
        image.verify()
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

async def load_vision_svg_context(state: Any, completion: Any, session: Any) -> None:
    request = dict(completion.upstream_request or {})
    source_id = request.get("source_image_id")
    image = await session.get(Image, source_id) if source_id else None
    if (image is None or image.user_id != completion.user_id or image.deleted_at is not None
            or getattr(image, "artifact_status", "ready") != "ready"
            or list(completion.input_image_ids or []) != [source_id]):
        raise ValueError("SVG source is missing, deleted, or not owned by this user")
    target = await session.get(Message, completion.message_id)
    parent = await session.get(Message, target.parent_message_id) if target else None
    if parent is None:
        raise ValueError("SVG source message is unavailable")
    image_artifacts.require_svg_renderer()
    async with asyncio.timeout(30):
        raw = await asyncio.to_thread(storage.get_bytes, image.storage_key)
    url = source_data_url(raw, request.get("source_image_sha256"))
    state.preparation.artifact_format = "svg"
    state.preparation.artifact_max_output_tokens = min(16000, max(1, int(request.get("max_output_tokens") or 2048)))
    state.streaming.input_list = [{"role": "user", "content": [
        {"type": "input_text", "text": str((parent.content or {}).get("text") or "")},
        {"type": "input_image", "image_url": url}]}]
    state.streaming.instructions = SVG_INSTRUCTIONS
    state.streaming.chat_tools = []
    state.preparation.fast_mode = False

def artifact_content(completion: Any) -> dict[str, Any]:
    request = dict(completion.upstream_request or {})
    status = request.get("artifact_status", "pending")
    return {
        "artifact_format": "svg", "artifact_status": status,
        "artifact_image_id": request.get("artifact_image_id"), "artifact_error": request.get("artifact_error"),
        "artifact_source_text": str(completion.text or "")[:MAX_SVG_BYTES],
        "text": ("已重建 SVG。按视觉模型实际文本用量计费。" if status == "ready" else
                 "SVG 未交付；已完成的视觉模型文本用量仍计费。" if status in {"invalid", "processing_failed"} else "正在处理 SVG。"),
        **({"images": [request["artifact_image"]]} if status == "ready" and request.get("artifact_image") else {}),
    }

async def stage_vision_svg_artifact(session: Any, completion: Any) -> dict[str, Any]:
    """Caller holds row lock/fence through commit. No tool usage or token mutation.
    Files are immutable and deterministic. Never delete after an uncertain commit;
    retry adopts the same keys rather than repeating the model request.
    """
    request = dict(completion.upstream_request or {})
    if request.get("artifact_status") == "invalid":
        return artifact_content(completion)
    try:
        if request.get("artifact_output_oversized"):
            raise ValueError("SVG output exceeds the 1 MiB limit")
        raw = validate_svg_text(str(completion.text or ""))
    except (ValueError, UnicodeError) as exc:
        request.update(artifact_status="invalid", artifact_error=str(exc)[:240], artifact_image_id=None)
        completion.upstream_request = request
        return artifact_content(completion)
    try:
        response_id = str(request.get("completion_checkpoint_response_id") or "")
        image_id = artifact_identity(str(completion.id), response_id)
        sha = hashlib.sha256(raw).hexdigest()
        existing = await session.get(Image, image_id)
        if existing is not None:
            if (existing.user_id != completion.user_id or existing.sha256 != sha or existing.deleted_at is not None
                    or (existing.metadata_jsonb or {}).get("completion_id") != str(completion.id)):
                raise ValueError("SVG artifact identity conflict")
            width, height = existing.width, existing.height
        else:
            async with asyncio.timeout(60):
                bundle = await asyncio.to_thread(image_artifacts._make_image_variants_sync, raw)
            width, height = bundle.width, bundle.height
            prefix = f"u/{completion.user_id}/vision-svg/{completion.id}/{image_id}"
            keys = {"original": f"{prefix}/orig.svg", "display2048": f"{prefix}/display2048.webp",
                    "preview1024": f"{prefix}/preview1024.webp", "thumb256": f"{prefix}/thumb256.jpg"}
            for key, data in [(keys["original"], raw), (keys["display2048"], bundle.display.bytes),
                              (keys["preview1024"], bundle.preview.bytes), (keys["thumb256"], bundle.thumb.bytes)]:
                written = await asyncio.to_thread(storage.put_bytes_result, key, data)
                if getattr(written, "size", None) != len(data):
                    raise OSError("SVG storage write was not confirmed")
            session.add(Image(
                id=image_id, user_id=completion.user_id, owner_generation_id=None, source="generated",
                parent_image_id=request.get("source_image_id"), storage_key=keys["original"],
                mime="image/svg+xml", width=width, height=height, size_bytes=len(raw), sha256=sha, visibility="private",
                metadata_jsonb={
                    "source": "vision_svg", "completion_id": str(completion.id),
                    "completion_execution_epoch": request.get("completion_checkpoint_execution_epoch"),
                    "completion_attempt_epoch": request.get("completion_checkpoint_attempt_epoch"),
                    "source_image_id": request.get("source_image_id"), "source_image_sha256": request.get("source_image_sha256"),
                    "svg_sha256": sha, "validator_version": VALIDATOR_VERSION,
                    "model": request.get("vision_svg_actual_model") or completion.model,
                    "provider": request.get("actual_provider"), "endpoint": safe_endpoint(request.get("actual_endpoint")),
                    "upstream_response_id": response_id, "upstream_request_id": request.get("vision_svg_request_id"),
                }))
            for kind, variant in (("display2048", bundle.display), ("preview1024", bundle.preview), ("thumb256", bundle.thumb)):
                session.add(ImageVariant(image_id=image_id, kind=kind, storage_key=keys[kind],
                                         width=variant.size[0], height=variant.size[1]))
        payload = {
            "image_id": image_id, "from_completion_id": str(completion.id), "source": "vision_svg",
            "mime": "image/svg+xml", "width": width, "height": height, "url": f"/api/images/{image_id}/binary",
            "display_url": f"/api/images/{image_id}/variants/display2048",
            "preview_url": f"/api/images/{image_id}/variants/preview1024", "thumb_url": f"/api/images/{image_id}/variants/thumb256",
        }
        request.update(artifact_status="ready", artifact_error=None, artifact_image_id=image_id, artifact_image=payload)
    except Exception:
        request.update(artifact_status="processing_failed", artifact_error="SVG 本地处理失败，可重试本地处理，不再调用模型。",
                       artifact_image_id=None)
    completion.upstream_request = request
    return artifact_content(completion)


async def repair_terminal_svg(state: Any, session: Any, completion: Any) -> bool:
    """Terminal repair is strictly local. The claim already locked and fenced this row."""
    from lumen_core.constants import CompletionStatus, EV_COMP_SUCCEEDED
    from .completion_checkpoint import completion_has_completed_checkpoint
    if not is_vision_svg(completion) or completion.status != CompletionStatus.SUCCEEDED.value:
        return False
    request = dict(completion.upstream_request or {})
    if (completion.cancel_requested_at is not None or request.get("artifact_status") not in {"pending", "processing_failed"}
            or not completion_has_completed_checkpoint(completion)):
        return False
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost before local SVG repair")
    await state.ports.retry._raise_if_completion_cancelled(state.request.redis, str(completion.id), "cancelled before SVG repair")
    content = await stage_vision_svg_artifact(session, completion)
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost during local SVG repair")
    await state.ports.retry._raise_if_completion_cancelled(state.request.redis, str(completion.id), "cancelled during SVG repair")
    message = await session.get(Message, completion.message_id)
    if message is not None:
        message.content = {**dict(message.content or {}), **content}
    delivery = state.ports.events._stage_completion_event(
        session, completion.user_id, state.request.channel, EV_COMP_SUCCEEDED,
        {"completion_id": str(completion.id), "message_id": completion.message_id,
         "attempt": int(completion.attempt or 0), "attempt_epoch": int(completion.attempt or 0),
         "execution_epoch": int(completion.execution_epoch or 0), "local_artifact_repair": True, **content})
    await session.commit()
    await state.ports.events._deliver_completion_event(state.request.redis, delivery)
    state.settlement.task_outcome = "succeeded"
    return True
