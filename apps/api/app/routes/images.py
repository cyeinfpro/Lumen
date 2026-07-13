"""Images 路由（DESIGN §5.6 简化版：仅上传、查看、反代、软删）。

V1 不实现：variations、share、shares/*（V1.1+）。

本地文件系统存储：`settings.storage_root + /u/{uid}/uploads/{image_id}.{ext}`。
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import io
import logging
import os
import secrets
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Awaitable, BinaryIO, Iterator, TypeVar

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from PIL import Image as PILImage, ImageOps, UnidentifiedImageError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lumen_core.byok_retention import (
    applies_to_user as byok_retention_applies_to_user,
    cutoffs as byok_retention_cutoffs,
    is_user_visible as byok_retention_is_user_visible,
)
from lumen_core.constants import ImageSource, ImageVisibility
from lumen_core.image_signing import (
    ALLOWED_VARIANTS as SIGNED_ALLOWED_VARIANTS,
    verify_image_sig,
)
from lumen_core.image_reference import (
    DEFAULT_REFERENCE_MAX_SIDE,
    ImageReferenceError,
    MaskPreflightError,
    analyze_mask_image,
    make_reference_variant,
    validate_mask_preflight,
)
from lumen_core.models import (
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    Share,
    User,
)
from lumen_core.model_image_metadata import (
    build_model_image_metadata,
    model_image_filename,
    parse_model_image_filename,
    read_model_image_metadata,
)
from lumen_core.schemas import ImageOut

from ..audit import hash_email, request_ip_hash, write_audit
from ..byok_service import read_byok_settings_cached, retention_policy_from_settings
from ..canvas_services import asset_ref_service
from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..ratelimit import (
    PUBLIC_IMAGE_LIMITER,
    UPLOADS_LIMITER,
    client_ip,
    require_client_ip,
)
from ..redis_client import get_redis
from ..services import storage_files
from ..video_reference_images import (
    VIDEO_REFERENCE_IMAGE_KIND,
    VIDEO_REFERENCE_IMAGE_MIME,
    VideoReferenceImageError,
    ensure_video_reference_image_variant,
)


router = APIRouter()
logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_VIDEO_REFERENCE_ACCESS_TOKEN_TTL = timedelta(hours=24)

_LINK_UNSUPPORTED_ERRNOS = {
    errno.EPERM,
    errno.EACCES,
    errno.EXDEV,
    getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    errno.EOPNOTSUPP,
}


MAX_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_LONG_SIDE = 4096
MAX_IMAGE_PIXELS = 64_000_000
PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}
EXT_BY_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
NORMALIZABLE_UPLOAD_MIME = {"image/mpo", "image/x-mpo"}
MIN_STORAGE_FREE_BYTES = 512 * 1024 * 1024

DISPLAY_VARIANT = "display2048"
PREVIEW_VARIANT = "preview1024"
THUMB_VARIANT = "thumb256"
ALLOWED_VARIANTS = {DISPLAY_VARIANT, PREVIEW_VARIANT, THUMB_VARIANT}
VARIANT_MEDIA_TYPE = {
    DISPLAY_VARIANT: "image/webp",
    PREVIEW_VARIANT: "image/webp",
    THUMB_VARIANT: "image/jpeg",
}
VARIANT_LOCK_TTL_SECONDS = 60
VARIANT_LOCK_WAIT_SECONDS = 5.0


async def _resolve_redis_result(value: Awaitable[_T] | _T) -> _T:
    if inspect.isawaitable(value):
        return await value
    return value


def _image_mime_type(image: PILImage.Image) -> str:
    custom_mimetype = getattr(image, "custom_mimetype", None)
    if isinstance(custom_mimetype, str) and custom_mimetype:
        return custom_mimetype.lower()
    image_format = image.format
    if not isinstance(image_format, str):
        return ""
    mime = PILImage.MIME.get(image_format.upper())
    return mime.lower() if isinstance(mime, str) else ""


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


def _content_references_image(value: Any, image_id: str) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                key in {"image_id", "source_image_id", "mask_image_id"}
                and item == image_id
            ):
                return True
            if _content_references_image(item, image_id):
                return True
        return False
    if isinstance(value, list):
        return any(_content_references_image(item, image_id) for item in value)
    return False


async def _image_referenced_by_visible_user_history(
    db: AsyncSession,
    img: Image,
    user: Any,
    policy: Any,
) -> bool:
    visible_after = byok_retention_cutoffs(policy=policy).visible_after
    gen_row = (
        await db.execute(
            select(Generation.id)
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Generation.user_id == user.id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
                Message.deleted_at.is_(None),
                Message.created_at >= visible_after,
                or_(
                    Generation.id == getattr(img, "owner_generation_id", None),
                    Generation.primary_input_image_id == img.id,
                    Generation.mask_image_id == img.id,
                ),
            )
            .limit(1)
        )
    ).first()
    if gen_row is not None:
        return True

    gen_inputs = (
        (
            await db.execute(
                select(Generation.input_image_ids)
                .join(Message, Message.id == Generation.message_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Generation.user_id == user.id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                    Message.deleted_at.is_(None),
                    Message.created_at >= visible_after,
                )
            )
        )
        .scalars()
        .all()
    )
    if any(img.id in (input_ids or []) for input_ids in gen_inputs):
        return True

    contents = (
        (
            await db.execute(
                select(Message.content)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                    Message.deleted_at.is_(None),
                    Message.created_at >= visible_after,
                )
            )
        )
        .scalars()
        .all()
    )
    return any(_content_references_image(content, img.id) for content in contents)


async def _ensure_image_visible_to_user(
    db: AsyncSession,
    img: Image,
    user: Any,
) -> None:
    if not byok_retention_applies_to_user(user):
        return
    created_at = getattr(img, "created_at", None)
    if created_at is None:
        return
    policy = retention_policy_from_settings(await read_byok_settings_cached(db))
    if not byok_retention_is_user_visible(
        account_mode=getattr(user, "account_mode", None),
        created_at=created_at,
        policy=policy,
    ):
        if await _image_referenced_by_visible_user_history(db, img, user, policy):
            return
        raise _http("not_found", "image not found", 404)


async def _ensure_public_image_visible(db: AsyncSession, img: Image) -> None:
    user_id = getattr(img, "user_id", None)
    created_at = getattr(img, "created_at", None)
    if not user_id or created_at is None:
        return
    account_mode = (
        await db.execute(select(User.account_mode).where(User.id == user_id))
    ).scalar_one_or_none()
    if account_mode != "byok":
        return
    policy = retention_policy_from_settings(await read_byok_settings_cached(db))
    if not byok_retention_is_user_visible(
        account_mode=account_mode,
        created_at=created_at,
        policy=policy,
    ):
        raise _http("not_found", "image not found", 404)


def _too_many_pixels() -> HTTPException:
    return _http(
        "too_many_pixels",
        f"image exceeds safe pixel limit ({MAX_IMAGE_PIXELS} pixels)",
        413,
    )


def _enforce_pixel_limit(size: tuple[int, int]) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise _http("invalid_image", "invalid image size", 400)
    if width * height > MAX_IMAGE_PIXELS:
        raise _too_many_pixels()
    if max(width, height) > MAX_LONG_SIDE:
        raise _http("too_large", f"image long side exceeds {MAX_LONG_SIDE}px", 413)


def _open_image_bytes(data: bytes, *, verify: bool = False) -> PILImage.Image:
    try:
        im = PILImage.open(io.BytesIO(data))
        if verify:
            im.verify()
        return im
    except PILImage.DecompressionBombError as exc:
        raise _too_many_pixels() from exc
    except UnidentifiedImageError as exc:
        raise _http("invalid_image", "unreadable image", 400) from exc
    except Exception as exc:
        raise _http("invalid_image", "unreadable image", 400) from exc


def _prepare_upload_image(
    data: bytes,
    filename: str | None,
    *,
    mask_requested: bool = False,
    reference_size: tuple[int, int] | None = None,
) -> tuple[
    bytes,
    str,
    int,
    int,
    dict[str, Any],
    bytes,
    dict[str, Any],
]:
    _open_image_bytes(data, verify=True)

    with _open_image_bytes(data) as im:
        mime = _image_mime_type(im)
        source_mime = mime
        uploaded_model_metadata = _model_metadata_json_from_upload(im, filename)
        if mime in ALLOWED_MIME:
            width, height = im.size
            _enforce_pixel_limit((width, height))
        elif mime in NORMALIZABLE_UPLOAD_MIME:
            data, width, height = _normalize_upload_image_to_jpeg(im)
            mime = "image/jpeg"
            uploaded_model_metadata["upload_normalized"] = {
                "source_mime": source_mime,
                "target_mime": mime,
                "reason": "unsupported_upload_mime",
            }
        else:
            raise _http("unsupported_mime", f"mime not allowed: {mime}", 400)
    try:
        normalized_ref = make_reference_variant(
            data,
            max_side=DEFAULT_REFERENCE_MAX_SIDE,
        )
        mask_preflight = analyze_mask_image(data, reference_size=reference_size)
        if mask_requested:
            validate_mask_preflight(mask_preflight)
    except MaskPreflightError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    except ImageReferenceError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc

    metadata = {
        **uploaded_model_metadata,
        "mask_preflight": mask_preflight.to_metadata(),
    }
    normalized_ref_meta = {
        "mime": normalized_ref.mime,
        "width": normalized_ref.width,
        "height": normalized_ref.height,
        "sha256": normalized_ref.sha256,
        "bytes": normalized_ref.bytes,
        "max_side": normalized_ref.max_side,
    }
    return (
        data,
        mime,
        width,
        height,
        metadata,
        normalized_ref.data,
        normalized_ref_meta,
    )


def _normalize_upload_image_to_jpeg(im: PILImage.Image) -> tuple[bytes, int, int]:
    try:
        normalized = ImageOps.exif_transpose(im)
        width, height = normalized.size
        _enforce_pixel_limit((width, height))
        if "A" in normalized.getbands() or "transparency" in getattr(
            normalized, "info", {}
        ):
            rgba = normalized.convert("RGBA")
            flattened = PILImage.new("RGB", rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.getchannel("A"))
            normalized = flattened
        elif normalized.mode != "RGB":
            normalized = normalized.convert("RGB")
        out = io.BytesIO()
        normalized.save(out, format="JPEG", quality=95)
        data = out.getvalue()
    except PILImage.DecompressionBombError as exc:
        raise _too_many_pixels() from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise _http("invalid_image", "unreadable image", 400) from exc
    if len(data) > MAX_BYTES:
        raise _http("too_large", f"file exceeds {MAX_BYTES // (1024 * 1024)}MB", 413)
    return data, width, height


def _model_metadata_json_from_upload(
    im: PILImage.Image,
    filename: str | None,
) -> dict[str, Any]:
    parsed = read_model_image_metadata(im)
    metadata_source = "embedded"
    if parsed is None and filename:
        parsed = parse_model_image_filename(filename)
        metadata_source = "filename"
    if parsed is None:
        return {}
    payload = build_model_image_metadata(
        age_segment=parsed.age_segment,
        gender=parsed.gender,
        appearance_direction=parsed.appearance_direction,
        style_tags=list(parsed.style_tags or []),
        source=parsed.source or metadata_source,
        prompt_hint=parsed.prompt_hint,
    )
    if not payload:
        return {}
    return {
        "model_library": payload,
        "model_library_metadata_source": metadata_source,
    }


def _upload_requests_mask_preflight(purpose: str | None, filename: str | None) -> bool:
    purpose_norm = (purpose or "").strip().lower()
    if purpose_norm in {"mask", "inpaint_mask", "inpaint-mask"}:
        return True
    name = Path(filename or "").name.lower()
    stem = Path(name).stem
    return stem == "mask" or stem.startswith("mask_")


def _key_for_upload(user_id: str, image_id: str, ext: str) -> str:
    return f"u/{user_id}/uploads/{image_id}.{ext}"


def _key_for_normalized_ref(user_id: str, image_id: str) -> str:
    return f"u/{user_id}/uploads/{image_id}.ref.webp"


def _fs_path(storage_key: str) -> Path:
    return storage_files.resolve_storage_path(
        settings.storage_root,
        storage_key,
        error_factory=_http,
    )


def _storage_usage_path(root: Path) -> Path:
    current = root
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _minimum_storage_free_bytes() -> int:
    raw = os.environ.get("LUMEN_MIN_STORAGE_FREE_BYTES", "").strip()
    if not raw:
        return MIN_STORAGE_FREE_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("invalid LUMEN_MIN_STORAGE_FREE_BYTES=%r; using default", raw)
        return MIN_STORAGE_FREE_BYTES


def _ensure_storage_free_space(incoming_bytes: int) -> None:
    root = Path(settings.storage_root).resolve()
    usage = shutil.disk_usage(_storage_usage_path(root))
    required = max(0, incoming_bytes) + _minimum_storage_free_bytes()
    if usage.free < required:
        raise _http(
            "storage_insufficient_space",
            "not enough free storage to accept this upload",
            507,
        )


def _variant_lock_key(image_id: str, kind: str) -> str:
    return f"image_variant_lock:{image_id}:{kind}"


async def _acquire_variant_generation_lock(
    image_id: str,
    kind: str,
    *,
    ttl_seconds: int = VARIANT_LOCK_TTL_SECONDS,
) -> str | None:
    token = secrets.token_urlsafe(24)
    try:
        ok = await get_redis().set(
            _variant_lock_key(image_id, kind),
            token,
            nx=True,
            ex=ttl_seconds,
        )
    except Exception:  # noqa: BLE001
        logger.warning("variant generation lock unavailable", exc_info=True)
        return token
    return token if ok else None


async def _release_variant_generation_lock(
    image_id: str,
    kind: str,
    token: str,
) -> None:
    script = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )
    try:
        await _resolve_redis_result(
            get_redis().eval(
                script,
                1,
                _variant_lock_key(image_id, kind),
                token,
            )
        )
    except Exception:  # noqa: BLE001
        logger.warning("variant generation lock release failed", exc_info=True)


async def _wait_for_variant(
    db: AsyncSession,
    image_id: str,
    kind: str,
    *,
    timeout_seconds: float = VARIANT_LOCK_WAIT_SECONDS,
) -> ImageVariant | None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
        variant = (
            await db.execute(
                select(ImageVariant).where(
                    ImageVariant.image_id == image_id,
                    ImageVariant.kind == kind,
                )
            )
        ).scalar_one_or_none()
        if variant is not None:
            return variant
    return None


def _metadata_storage_keys(metadata: Any) -> Iterator[str]:
    if not isinstance(metadata, dict):
        return
    normalized_ref = metadata.get("normalized_ref")
    if isinstance(normalized_ref, dict):
        storage_key = normalized_ref.get("storage_key")
        if isinstance(storage_key, str) and storage_key:
            yield storage_key


def _is_image_file_storage_key(key: str) -> bool:
    parts = PurePosixPath(key).parts
    if len(parts) < 4 or parts[0] != "u" or not parts[1]:
        return False
    if parts[2] == "uploads":
        return len(parts) == 4 and bool(parts[3])
    if parts[2] == "g":
        return len(parts) == 5 and bool(parts[3]) and bool(parts[4])
    return False


async def _known_storage_keys(db: AsyncSession) -> set[str]:
    keys: set[str] = set()
    image_rows = (
        await db.execute(select(Image.storage_key, Image.metadata_jsonb))
    ).all()
    for row in image_rows:
        storage_key = row[0]
        metadata = row[1]
        if isinstance(storage_key, str) and storage_key:
            keys.add(storage_key)
        keys.update(_metadata_storage_keys(metadata))
    variant_keys = (await db.execute(select(ImageVariant.storage_key))).scalars().all()
    keys.update(key for key in variant_keys if isinstance(key, str) and key)
    return keys


async def sweep_orphan_image_files(
    db: AsyncSession,
    *,
    storage_root: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    root = Path(storage_root or settings.storage_root).resolve()
    known_keys = await _known_storage_keys(db)
    scanned = 0
    deleted = 0
    orphan_keys: list[str] = []
    if not root.exists():
        return {
            "dry_run": dry_run,
            "storage_root": str(root),
            "scanned": 0,
            "orphans": [],
            "deleted": 0,
        }
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        scanned += 1
        rel = path.relative_to(root).as_posix()
        if not _is_image_file_storage_key(rel):
            continue
        if rel in known_keys:
            continue
        orphan_keys.append(rel)
        if not dry_run:
            await asyncio.to_thread(path.unlink)
            deleted += 1
    return {
        "dry_run": dry_run,
        "storage_root": str(root),
        "scanned": scanned,
        "orphans": orphan_keys,
        "deleted": deleted,
    }


def _image_url(image_id: str) -> str:
    # 相对路径：前端同源请求，反代 /api/* → 后端 /*。
    # 不拼 public_base_url：避免把 dev/prod host 焊进 API 响应，导致
    # HTTPS 前端拿到 http://IP:8000/... 触发 Mixed Content。
    return f"/api/images/{image_id}/binary"


def _variant_url(image_id: str, kind: str) -> str:
    return f"/api/images/{image_id}/variants/{kind}"


def _variant_key_for_image(img: Image, kind: str) -> str:
    src = Path(img.storage_key)
    return str(src.with_name(f"{img.id}.{kind}.webp"))


def _make_display_variant(
    path: Path, max_side: int = 2048
) -> tuple[bytes, tuple[int, int]]:
    try:
        im_ctx = PILImage.open(path)
    except PILImage.DecompressionBombError as exc:
        raise _too_many_pixels() from exc
    except UnidentifiedImageError as exc:
        raise _http("invalid_image", "unreadable image", 400) from exc

    with im_ctx as im:
        _enforce_pixel_limit(im.size)
        im.load()
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        with im.convert("RGB") as rgb:
            rgb.save(buf, format="WEBP", quality=86, method=4)
        return buf.getvalue(), im.size


async def _ensure_display_variant(
    db: AsyncSession,
    img: Image,
) -> ImageVariant:
    locked_img = (
        await db.execute(
            select(Image)
            .where(
                Image.id == img.id,
                Image.user_id == img.user_id,
                Image.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked_img is None:
        raise _http("not_found", "image not found", 404)
    img = locked_img

    existing = (
        await db.execute(
            select(ImageVariant).where(
                ImageVariant.image_id == img.id,
                ImageVariant.kind == DISPLAY_VARIANT,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    src_path = _fs_path(img.storage_key)
    if not src_path.is_file():
        raise _http("not_found", "binary missing", 404)

    data, size = await asyncio.to_thread(_make_display_variant, src_path)
    key = _variant_key_for_image(img, DISPLAY_VARIANT)

    # Why: write file FIRST, then commit DB row. Reverse order from previous
    # implementation. Trade-off:
    #   - old order (DB commit -> file write): file failure required deleting
    #     the DB row, and if that delete-commit also failed we left an orphan
    #     row pointing at a missing file (404 forever for that variant).
    #   - new order (file write -> DB commit): if the DB commit fails, an
    #     orphan FILE is left on disk with no DB pointer. The next call to
    #     _ensure_display_variant for the same image_id derives the same key
    #     (deterministic from image_id + kind), _write_new_file_atomic raises
    #     FileExistsError (we pass), then commits the row. Self-healing.
    # Orphan files are harmless (small WEBP, can be reclaimed by an offline
    # `find storage_root -name '*.display2048.webp' | ...` if ever needed)
    # whereas orphan rows poison the variant lookup forever.
    dst_path = _fs_path(key)
    try:
        await asyncio.to_thread(_write_new_file_atomic, dst_path, data)
    except FileExistsError:
        # Concurrent writer (or self-heal from a previous failed commit) won
        # the disk write race. Continue to commit our row — IntegrityError
        # below handles the case where they also won the DB race.
        pass

    variant = ImageVariant(
        image_id=img.id,
        kind=DISPLAY_VARIANT,
        storage_key=key,
        width=size[0],
        height=size[1],
    )
    db.add(variant)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        winner = (
            await db.execute(
                select(ImageVariant).where(
                    ImageVariant.image_id == img.id,
                    ImageVariant.kind == DISPLAY_VARIANT,
                )
            )
        ).scalar_one_or_none()
        if winner:
            return winner
        raise
    except Exception:
        # DB flush failed after file landed on disk. The file is now an
        # orphan; the next call self-heals via FileExistsError + new row.
        # Log loudly so ops can spot persistent flush failures (e.g. PG
        # outage) and reclaim disk via a cron sweeper if needed.
        await db.rollback()
        logger.error(
            "display variant DB flush failed (orphan file left at %s) "
            "image_id=%s key=%s",
            dst_path,
            img.id,
            key,
        )
        raise
    return variant


async def _image_out(db: AsyncSession, img: Image) -> ImageOut:
    variants = (
        (await db.execute(select(ImageVariant).where(ImageVariant.image_id == img.id)))
        .scalars()
        .all()
    )
    kinds = {v.kind for v in variants}
    return ImageOut(
        id=img.id,
        source=img.source,
        parent_image_id=img.parent_image_id,
        owner_generation_id=img.owner_generation_id,
        width=img.width,
        height=img.height,
        mime=img.mime,
        blurhash=img.blurhash,
        url=_image_url(img.id),
        display_url=_variant_url(img.id, DISPLAY_VARIANT),
        preview_url=(
            _variant_url(img.id, PREVIEW_VARIANT) if PREVIEW_VARIANT in kinds else None
        ),
        thumb_url=_variant_url(img.id, THUMB_VARIANT)
        if THUMB_VARIANT in kinds
        else None,
        metadata_jsonb=img.metadata_jsonb or {},
    )


async def _check_upload_rate_limit(user_id: str) -> None:
    redis = get_redis()
    await UPLOADS_LIMITER.check(redis, f"rl:upload:{user_id}")


async def _check_public_image_lookup_rate_limit(request: Request) -> None:
    redis = get_redis()
    await PUBLIC_IMAGE_LIMITER.check(redis, f"rl:image-key:{client_ip(request)}")


async def _check_signed_image_rate_limit(request: Request) -> None:
    """Public unauthenticated signed-image route: reject when client IP is
    unknown so anonymous clients can't share one bucket and DoS the rest."""
    redis = get_redis()
    await PUBLIC_IMAGE_LIMITER.check(
        redis, f"rl:image-sig:{require_client_ip(request)}"
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new_file_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
            _fsync_directory(path.parent)
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                raise
            if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
                raise
            _write_new_file_exclusive(path, data)
    finally:
        tmp.unlink(missing_ok=True)


def _write_new_file_exclusive(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _fsync_directory(path.parent)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _unlink_file_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    except OSError:
        logger.warning(
            "failed to remove orphan upload file path=%s", path, exc_info=True
        )


def _open_regular_file_no_symlink(path: Path) -> tuple[BinaryIO, int]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _http("not_found", "binary missing", 404) from exc
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            raise _http("not_found", "binary missing", 404) from exc
        if exc.errno == errno.ELOOP:
            raise _http(
                "invalid_path", "symlink storage paths are not allowed", 400
            ) from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _http("not_found", "binary missing", 404)
        return os.fdopen(fd, "rb"), int(st.st_size)
    except Exception:
        os.close(fd)
        raise


def _iter_open_file_and_close(f: BinaryIO) -> Iterator[bytes]:
    yield from storage_files.iter_open_file_and_close(f)


_INTERNAL_REDIRECT_PREFIX = "/_internal_storage/"


def _internal_redirect_enabled() -> bool:
    """Whether to emit X-Accel-Redirect for nginx sendfile hand-off.

    Default off: lumen-api ships with the streaming fallback that works
    behind any reverse proxy. Operators opt in by:

      1. Adding an ``internal`` location in nginx that aliases to
         storage_root (see deploy/nginx.conf.example).
      2. Setting ``LUMEN_INTERNAL_REDIRECT_ENABLED=1`` in shared/.env.

    With both in place, lumen-api stops opening files itself —
    nginx native sendfile + open_file_cache + direct CIFS read takes over.
    Without either, the streaming path keeps working unchanged.
    """
    return os.environ.get("LUMEN_INTERNAL_REDIRECT_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _etag_matches_if_none_match(etag: str, header_value: str) -> bool:
    """RFC 7232 §3.2: If-None-Match accepts ``*`` or a comma list of opaque tags.

    Both sides may carry the weak prefix ``W/``; for our use case (file-level
    immutable variants) treating weak and strong as equivalent is fine — the
    tag identity is what matters.
    """
    header_value = header_value.strip()
    if header_value == "*":
        return True
    canonical = etag.removeprefix("W/").strip()
    for raw in header_value.split(","):
        candidate = raw.strip().removeprefix("W/").strip()
        if candidate and candidate == canonical:
            return True
    return False


def _storage_streaming_response(
    path: Path,
    *,
    media_type: str,
    etag: str,
    cache_control: str,
    storage_key: str | None = None,
    request: Request | None = None,
) -> Response:
    """Serve a file from storage_root with three escalating fast paths.

    1. ``304 Not Modified`` short-circuit when the client's If-None-Match
       matches our ETag — never opens the file. Always on, no opt-in needed.
    2. ``X-Accel-Redirect`` hand-off to nginx when
       ``LUMEN_INTERNAL_REDIRECT_ENABLED=1`` and a ``storage_key`` is supplied.
       lumen-api emits headers only; nginx native sendfile streams the bytes.
    3. Fallback: open the file in the Python process and stream chunks via
       StreamingResponse — works without any nginx cooperation.
    """
    headers = {
        "Cache-Control": cache_control,
        "ETag": etag,
    }

    if request is not None:
        inm = request.headers.get("if-none-match")
        if inm and _etag_matches_if_none_match(etag, inm):
            return Response(status_code=304, headers=headers)

    if _internal_redirect_enabled() and storage_key:
        # Defense in depth: re-validate the key resolves under storage_root.
        # Callers normally already ran _fs_path; if the key somehow contains
        # path traversal we fall through to the streaming path which uses
        # _open_regular_file_no_symlink for the same protection.
        try:
            _fs_path(storage_key)
        except HTTPException:
            pass
        else:
            return Response(
                status_code=200,
                media_type=media_type,
                headers={
                    **headers,
                    "X-Accel-Redirect": _INTERNAL_REDIRECT_PREFIX
                    + storage_key.lstrip("/"),
                },
            )

    f, size = _open_regular_file_no_symlink(path)
    return StreamingResponse(
        _iter_open_file_and_close(f),
        media_type=media_type,
        headers={**headers, "Content-Length": str(size)},
    )


@router.post("/upload", response_model=ImageOut, dependencies=[Depends(verify_csrf)])
async def upload_image(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
    purpose: str | None = Form(default=None),
    reference_width: int | None = Form(default=None),
    reference_height: int | None = Form(default=None),
) -> ImageOut:
    await _check_upload_rate_limit(user.id)

    # Read all bytes with size cap.
    buf = bytearray()
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_BYTES:
            raise _http(
                "too_large", f"file exceeds {MAX_BYTES // (1024 * 1024)}MB", 413
            )

    if not buf:
        raise _http("empty_file", "empty file", 400)

    data = bytes(buf)
    _ensure_storage_free_space(len(data))
    reference_size = (
        (reference_width, reference_height)
        if reference_width is not None and reference_height is not None
        else None
    )
    (
        data,
        mime,
        width,
        height,
        upload_metadata,
        normalized_ref,
        normalized_ref_meta,
    ) = await asyncio.to_thread(
        _prepare_upload_image,
        data,
        file.filename,
        mask_requested=_upload_requests_mask_preflight(purpose, file.filename),
        reference_size=reference_size,
    )
    _ensure_storage_free_space(len(data) + len(normalized_ref))
    buf = bytearray(data)

    # hash
    sha = hashlib.sha256(bytes(buf)).hexdigest()

    # blurhash (best effort; lib is in worker only — we skip here and leave None)
    blurhash: str | None = None

    # Build image row.
    img = Image(
        user_id=user.id,
        source=ImageSource.UPLOADED.value,
        storage_key="",  # filled after we know image_id
        mime=mime,
        width=width,
        height=height,
        size_bytes=len(buf),
        sha256=sha,
        blurhash=blurhash,
        visibility=ImageVisibility.PRIVATE.value,
    )
    db.add(img)
    await db.flush()

    ext = EXT_BY_MIME[mime]
    model_payload = upload_metadata.get("model_library")
    if isinstance(model_payload, dict):
        upload_metadata["suggested_filename"] = model_image_filename(
            image_id=img.id,
            ext=ext,
            age_segment=model_payload.get("age_segment"),
            gender=model_payload.get("gender"),
            appearance_direction=model_payload.get("appearance_direction"),
            style_tags=model_payload.get("style_tags") or [],
        )
    key = _key_for_upload(user.id, img.id, ext)
    normalized_key = _key_for_normalized_ref(user.id, img.id)
    upload_metadata["normalized_ref"] = {
        **normalized_ref_meta,
        "storage_key": normalized_key,
    }
    img.metadata_jsonb = upload_metadata
    img.storage_key = key

    # Write to disk.
    path = _fs_path(key)
    normalized_path = _fs_path(normalized_key)
    written_paths: list[Path] = []
    try:
        await asyncio.to_thread(_write_new_file_atomic, path, bytes(buf))
        written_paths.append(path)
        await asyncio.to_thread(_write_new_file_atomic, normalized_path, normalized_ref)
        written_paths.append(normalized_path)
        await db.commit()
    except FileExistsError as exc:
        await db.rollback()
        for written_path in reversed(written_paths):
            await asyncio.to_thread(_unlink_file_if_exists, written_path)
        raise _http(
            "storage_conflict", "image storage key already exists", 409
        ) from exc
    except Exception:
        await db.rollback()
        for written_path in reversed(written_paths):
            await asyncio.to_thread(_unlink_file_if_exists, written_path)
        raise
    await db.refresh(img)

    return await _image_out(db, img)


@router.get("/{image_id}", response_model=ImageOut)
async def get_image_meta(
    image_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ImageOut:
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await _ensure_image_visible_to_user(db, img, user)
    return await _image_out(db, img)


@router.get("/{image_id}/binary")
async def get_image_binary(
    image_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await _ensure_image_visible_to_user(db, img, user)

    path = _fs_path(img.storage_key)
    return _storage_streaming_response(
        path,
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=31536000, immutable",
        storage_key=img.storage_key,
        request=request,
    )


@router.get("/{image_id}/variants/{kind}")
async def get_image_variant(
    image_id: str,
    kind: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    if kind not in ALLOWED_VARIANTS:
        raise _http("invalid_variant", "unsupported image variant", 400)
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await _ensure_image_visible_to_user(db, img, user)
    variant = (
        await db.execute(
            select(ImageVariant).where(
                ImageVariant.image_id == img.id,
                ImageVariant.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if not variant:
        if kind != DISPLAY_VARIANT:
            raise _http("not_found", "variant not found", 404)
        lock_token = await _acquire_variant_generation_lock(img.id, kind)
        if lock_token is None:
            variant = await _wait_for_variant(db, img.id, kind)
        try:
            if variant is None:
                variant = await _ensure_display_variant(db, img)
                await db.commit()
        except PILImage.DecompressionBombError as exc:
            raise _too_many_pixels() from exc
        finally:
            if lock_token is not None:
                await _release_variant_generation_lock(img.id, kind, lock_token)
    path = _fs_path(variant.storage_key)

    media_type = VARIANT_MEDIA_TYPE[kind]
    return _storage_streaming_response(
        path,
        media_type=media_type,
        etag=f'"{variant.image_id}-{variant.kind}"',
        cache_control="private, max-age=31536000, immutable",
        storage_key=variant.storage_key,
        request=request,
    )


@router.get("/_/sig/{image_id}/{variant}")
async def get_image_signed(
    image_id: str,
    variant: str,
    exp: int,
    sig: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """无登录、签名授权的图片端点。

    流程：
    1. settings.image_proxy_secret 未配置 → 503（功能未启用）
    2. variant / 签名 / 过期校验失败 → 403
    3. 通过则定位到 Image (variant=orig) 或 ImageVariant
    4. 流式回写 binary，缓存头允许 1h（远低于 sig TTL 默认 24h）

    Owner 检查在这里**不**做——签名本身就是授权凭证。
    """
    secret_str = settings.image_proxy_secret.strip()
    if not secret_str:
        raise _http("signed_proxy_disabled", "image signing not configured", 503)
    if variant not in SIGNED_ALLOWED_VARIANTS:
        raise _http("invalid_variant", "unsupported image variant", 400)
    await _check_signed_image_rate_limit(request)
    if not verify_image_sig(image_id, variant, exp, sig, secret_str.encode("utf-8")):
        # 不区分"签名错"和"过期"——攻击者无需区分，错误码统一收敛
        raise _http("forbidden", "invalid or expired image signature", 403)

    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await _ensure_public_image_visible(db, img)

    # Why: defense-in-depth. The HMAC signature is the primary authorization,
    # but if the signing secret leaks (e.g. compromised worker) an attacker
    # could forge sigs for arbitrary image_ids. Require the image to be
    # exposed via at least one non-revoked, non-expired Share so that
    # private images that were never publicly shared cannot be served by
    # this endpoint even with a valid signature.
    now = datetime.now(timezone.utc)
    share_primary = aliased(Image)
    share_hit = (
        await db.execute(
            select(Share.id)
            .join(share_primary, share_primary.id == Share.image_id)
            .where(
                share_primary.user_id == img.user_id,
                share_primary.deleted_at.is_(None),
                or_(
                    Share.image_id == img.id,
                    Share.image_ids.contains([img.id]),
                ),
                Share.revoked_at.is_(None),
                or_(Share.expires_at.is_(None), Share.expires_at > now),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not share_hit:
        raise _http("not_found", "image not found", 404)

    if variant == "orig":
        path = _fs_path(img.storage_key)
        media_type = img.mime
        etag = f'"{img.sha256}"'
        storage_key = img.storage_key
    else:
        v = (
            await db.execute(
                select(ImageVariant).where(
                    ImageVariant.image_id == img.id,
                    ImageVariant.kind == variant,
                )
            )
        ).scalar_one_or_none()
        if not v:
            raise _http("not_found", "variant not found", 404)
        path = _fs_path(v.storage_key)
        media_type = VARIANT_MEDIA_TYPE.get(variant, "application/octet-stream")
        etag = f'"{v.image_id}-{v.kind}"'
        storage_key = v.storage_key

    return _storage_streaming_response(
        path,
        media_type=media_type,
        etag=etag,
        cache_control="private, max-age=300",
        storage_key=storage_key,
        request=request,
    )


def _parse_video_reference_token_expiry(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _video_reference_token_is_valid(
    metadata: dict[str, Any],
    *,
    token: str,
    updated_at: datetime | None,
) -> bool:
    expected = metadata.get("video_reference_access_token")
    if not isinstance(expected, str) or not secrets.compare_digest(expected, token):
        return False
    expires_at = _parse_video_reference_token_expiry(
        metadata.get("video_reference_access_token_expires_at")
    )
    now = datetime.now(timezone.utc)
    if expires_at is not None:
        return expires_at > now
    if updated_at is None:
        return False
    fallback_updated_at = (
        updated_at.replace(tzinfo=timezone.utc)
        if updated_at.tzinfo is None
        else updated_at.astimezone(timezone.utc)
    )
    return fallback_updated_at + _VIDEO_REFERENCE_ACCESS_TOKEN_TTL > now


@router.get("/reference/{image_id}/binary")
async def reference_image_binary(
    image_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: str = Query(min_length=16, max_length=256),
    variant: str | None = Query(default=None, max_length=32),
) -> Response:
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if img is None:
        raise _http("not_found", "image not found", 404)
    await _ensure_public_image_visible(db, img)
    metadata = img.metadata_jsonb or {}
    if not _video_reference_token_is_valid(
        metadata,
        token=token,
        updated_at=getattr(img, "updated_at", None),
    ):
        raise _http("not_found", "image not found", 404)
    if variant:
        if variant != VIDEO_REFERENCE_IMAGE_KIND:
            raise _http("invalid_variant", "unsupported image reference variant", 400)
        try:
            ref_variant = await ensure_video_reference_image_variant(
                db,
                img,
                storage_root=settings.storage_root,
            )
            await db.commit()
        except VideoReferenceImageError as exc:
            raise _http(exc.code, exc.message, exc.status_code) from exc
        return _storage_streaming_response(
            _fs_path(ref_variant.storage_key),
            media_type=VIDEO_REFERENCE_IMAGE_MIME,
            etag=f'"{ref_variant.image_id}-{ref_variant.kind}"',
            cache_control="private, max-age=3600",
            storage_key=ref_variant.storage_key,
            request=request,
        )
    return _storage_streaming_response(
        _fs_path(img.storage_key),
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=3600",
        storage_key=img.storage_key,
        request=request,
    )


@router.get("/_/by-key/{key:path}")
async def get_image_by_key(
    key: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Proxy lookup by `storage_key`. Used when Worker writes a `public_url` that
    references our key space. Owner check is enforced."""
    await _check_public_image_lookup_rate_limit(request)
    img = (
        await db.execute(
            select(Image).where(
                Image.storage_key == key,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await _ensure_image_visible_to_user(db, img, user)
    path = _fs_path(img.storage_key)
    return _storage_streaming_response(
        path,
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=31536000, immutable",
        storage_key=img.storage_key,
        request=request,
    )


@router.delete("/{image_id}", dependencies=[Depends(verify_csrf)])
async def delete_image(
    image_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    await asset_ref_service.ensure_asset_not_canvas_referenced(db, image_id=img.id)
    img.deleted_at = datetime.now(timezone.utc)
    await write_audit(
        db,
        event_type="image.delete",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "image_id": img.id,
            "source": img.source,
            "owner_generation_id": img.owner_generation_id,
        },
    )
    await db.commit()
    return {"ok": True}
