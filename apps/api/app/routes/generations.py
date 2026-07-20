"""Generations feed 路由（Phase 4：灵感流 Tab 后端）。

DESIGN §6.7：
    GET /api/generations/feed
    Query:
        cursor: str | None     # base64("created_at_iso|id")
        limit: int = 30        # 1..100
        ratio: str | None      # "1:1" | "16:9" | "9:16" | "4:5" | "3:4" | "21:9"
        has_ref: bool = False
        fast: bool = False
        q: str | None          # 简易 prompt LIKE 匹配（本轮不做全文索引）

返回当前登录用户、status=succeeded、所属会话未删除且未归档的 generation，
配带首张 image + thumb/display variant URL，按 (created_at DESC, id DESC) tuple 翻页。

与 `0006_gen_feed_idx` migration 配套：`(user_id, created_at DESC)` 复合索引。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.byok_retention import (
    applies_to_user as byok_retention_applies_to_user,
    cutoffs as byok_retention_cutoffs,
)
from lumen_core.constants import GenerationStatus
from lumen_core.models import Conversation, Generation, Image, ImageVariant, Message
from lumen_core.providers import parse_provider_bool

from ..db import get_db
from ..deps import CurrentUser
from ..byok_service import read_byok_settings_cached, retention_policy_from_settings


router = APIRouter()


CURSOR_VERSION = "v3"
COUNT_CAP = 10_000


# ---------- 支持的 aspect ratio 白名单 ----------

_ALLOWED_RATIOS = {
    "1:1",
    "16:9",
    "9:16",
    "4:5",
    "3:4",
    "21:9",
    "10:7",
    "7:10",
}


def _bool_option(value: object, default: bool = False) -> bool:
    try:
        return parse_provider_bool(value, default=default)
    except ValueError:
        return default


# ---------- Schemas ----------


class GenerationImageOut(BaseModel):
    id: str
    url: str
    mime: str
    display_url: str
    preview_url: str | None = None
    thumb_url: str
    width: int
    height: int


class GenerationFeedItem(BaseModel):
    id: str
    created_at: datetime
    prompt: str
    aspect_ratio: str
    has_ref: bool
    fast: bool
    quality: str | None = None
    output_format: str | None = None
    size_actual: str
    image: GenerationImageOut
    message_id: str
    conversation_id: str


class GenerationFeedOut(BaseModel):
    items: list[GenerationFeedItem]
    next_cursor: str | None = None
    total: int


# ---------- helpers ----------


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


def _feed_filter_signature(
    *,
    user_id: str,
    ratio: str | None,
    has_ref: bool,
    fast: bool,
    q: str | None,
    visible_after: datetime | None,
) -> str:
    payload = {
        "user_id": user_id,
        # Keep the retired runtime field stable so existing v3 cursors remain valid.
        "runtime": "docker",
        "ratio": ratio or "",
        "has_ref": bool(has_ref),
        "fast": bool(fast),
        "q": (q or "").strip(),
        "visible_after": (
            visible_after.astimezone(timezone.utc).isoformat()
            if visible_after is not None
            else ""
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _encode_cursor(
    created_at: datetime,
    gen_id: str,
    total: int | None = None,
    filter_sig: str | None = None,
) -> str:
    # 统一走 UTC ISO，解析端直接 fromisoformat。版本前缀用于未来演进。
    iso = created_at.astimezone(timezone.utc).isoformat()
    total_part = "" if total is None else str(max(0, total))
    filter_part = filter_sig or ""
    raw = f"{CURSOR_VERSION}|{iso}|{gen_id}|{total_part}|{filter_part}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor_total(raw_total: str) -> int | None:
    if not raw_total:
        return None
    try:
        return max(0, int(raw_total))
    except ValueError as exc:
        raise _http("invalid_cursor", "invalid cursor total", 400) from exc


def _decoded_cursor_fields(
    parts: list[str],
) -> tuple[str, str, int | None, str | None]:
    if len(parts) == 5:
        version, iso, gen_id, raw_total, filter_sig = parts
        if version != CURSOR_VERSION:
            raise _http("invalid_cursor", "unsupported cursor version", 400)
        if not filter_sig:
            raise _http("invalid_cursor", "missing cursor filter", 400)
        return iso, gen_id, _cursor_total(raw_total), filter_sig
    if len(parts) == 4:
        version, iso, gen_id, raw_total = parts
        if version not in {"v2", CURSOR_VERSION}:
            raise _http("invalid_cursor", "unsupported cursor version", 400)
        return iso, gen_id, _cursor_total(raw_total), None
    if len(parts) == 3:
        version, iso, gen_id = parts
        if version not in {"v1", CURSOR_VERSION}:
            raise _http("invalid_cursor", "unsupported cursor version", 400)
        return iso, gen_id, None, None
    if len(parts) == 2:
        iso, gen_id = parts
        return iso, gen_id, None, None
    raise _http("invalid_cursor", "invalid cursor", 400)


def _decode_cursor(cursor: str) -> tuple[datetime, str, int | None, str | None]:
    # base64 补齐 padding
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise _http("invalid_cursor", "invalid cursor", 400) from exc
    iso, gen_id, cursor_total, cursor_filter_sig = _decoded_cursor_fields(
        raw.split("|")
    )
    if not gen_id:
        raise _http("invalid_cursor", "invalid cursor id", 400)
    try:
        # 接受 Z 后缀 / +00:00；fromisoformat 在 3.11+ 已支持。
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _http("invalid_cursor", "invalid cursor timestamp", 400) from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts, gen_id, cursor_total, cursor_filter_sig


def _validated_cursor_total(
    *,
    cursor_total: int | None,
    cursor_filter_sig: str | None,
    current_filter_sig: str,
) -> int | None:
    if cursor_filter_sig is None:
        return None
    if cursor_filter_sig != current_filter_sig:
        raise _http("invalid_cursor", "cursor does not match current filters", 400)
    return cursor_total


def _escape_like_pattern(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _apply_filters(
    stmt: Select,  # type: ignore[type-arg]
    *,
    user_id: str,
    ratio: str | None,
    has_ref: bool,
    fast: bool,
    q: str | None,
    visible_after: datetime | None = None,
) -> Select:  # type: ignore[type-arg]
    stmt = stmt.join(Message, Message.id == Generation.message_id).join(
        Conversation, Conversation.id == Message.conversation_id
    )
    stmt = stmt.where(
        Generation.user_id == user_id,
        Generation.status == GenerationStatus.SUCCEEDED.value,
        Message.deleted_at.is_(None),
        Conversation.user_id == user_id,
        Conversation.deleted_at.is_(None),
        Conversation.archived.is_(False),
        select(Image.id)
        .where(
            Image.owner_generation_id == Generation.id,
            Image.user_id == user_id,
            Image.deleted_at.is_(None),
        )
        .exists(),
    )
    if visible_after is not None:
        stmt = stmt.where(Generation.created_at >= visible_after)
    stmt = stmt.where(Generation.upstream_request["workflow_run_id"].astext.is_(None))

    if ratio:
        stmt = stmt.where(Generation.aspect_ratio == ratio)

    if has_ref:
        # 有参考图：primary_input_image_id 非空 或 input_image_ids 非空数组
        stmt = stmt.where(
            or_(
                Generation.primary_input_image_id.is_not(None),
                func.cardinality(Generation.input_image_ids) > 0,
            )
        )

    if fast:
        stmt = stmt.where(
            func.lower(Generation.upstream_request["fast"].astext).in_(("true", "1"))
        )

    if q:
        # 简易 prompt LIKE 匹配；大小写不敏感。
        q_escaped = _escape_like_pattern(q.strip())
        pattern = f"%{q_escaped}%"
        if pattern != "%%":
            stmt = stmt.where(Generation.prompt.ilike(pattern, escape="\\"))

    return stmt


async def _feed_visible_after(
    user: CurrentUser,
    db: AsyncSession,
) -> datetime | None:
    if not byok_retention_applies_to_user(user):
        return None
    policy = retention_policy_from_settings(await read_byok_settings_cached(db))
    if not policy.hide_enabled:
        return None
    return byok_retention_cutoffs(policy=policy).visible_after


async def _generation_feed_total(
    db: AsyncSession,
    *,
    trusted_cursor_total: int | None,
    user_id: str,
    ratio: str | None,
    has_ref: bool,
    fast: bool,
    q: str | None,
    visible_after: datetime | None,
) -> int:
    if trusted_cursor_total is not None:
        return trusted_cursor_total
    count_stmt: Select = select(Generation.id)  # type: ignore[assignment]
    count_stmt = _apply_filters(
        count_stmt,
        user_id=user_id,
        ratio=ratio,
        has_ref=has_ref,
        fast=fast,
        q=q,
        visible_after=visible_after,
    )
    limited_count = select(func.count()).select_from(
        count_stmt.limit(COUNT_CAP + 1).subquery()
    )
    return int((await db.execute(limited_count)).scalar() or 0)


async def _generation_feed_page(
    db: AsyncSession,
    *,
    user_id: str,
    ratio: str | None,
    has_ref: bool,
    fast: bool,
    q: str | None,
    visible_after: datetime | None,
    cursor_ts: datetime | None,
    cursor_id: str | None,
    limit: int,
) -> tuple[list[Generation], bool]:
    stmt: Select = select(Generation)  # type: ignore[assignment]
    stmt = _apply_filters(
        stmt,
        user_id=user_id,
        ratio=ratio,
        has_ref=has_ref,
        fast=fast,
        q=q,
        visible_after=visible_after,
    )
    if cursor_ts is not None and cursor_id is not None:
        stmt = stmt.where(
            or_(
                Generation.created_at < cursor_ts,
                and_(Generation.created_at == cursor_ts, Generation.id < cursor_id),
            )
        )
    stmt = stmt.order_by(Generation.created_at.desc(), Generation.id.desc()).limit(
        limit + 1
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return rows[:limit], len(rows) > limit


async def _feed_images(
    db: AsyncSession,
    generations: list[Generation],
    *,
    visible_after: datetime | None,
) -> dict[str, Image]:
    generation_ids = [generation.id for generation in generations]
    ranked_images = (
        select(
            Image.id.label("image_id"),
            Image.owner_generation_id.label("owner_generation_id"),
            func.row_number()
            .over(
                partition_by=Image.owner_generation_id,
                order_by=(Image.created_at.asc(), Image.id.asc()),
            )
            .label("rn"),
        )
        .where(
            Image.owner_generation_id.in_(generation_ids),
            Image.deleted_at.is_(None),
        )
        .subquery()
    )
    image_filters = (
        [Image.created_at >= visible_after] if visible_after is not None else []
    )
    rows = (
        (
            await db.execute(
                select(Image)
                .join(ranked_images, Image.id == ranked_images.c.image_id)
                .where(ranked_images.c.rn == 1)
                .where(*image_filters)
                .order_by(ranked_images.c.owner_generation_id.asc())
            )
        )
        .scalars()
        .all()
    )
    image_by_generation: dict[str, Image] = {}
    for image in rows:
        generation_id = image.owner_generation_id
        if generation_id and generation_id not in image_by_generation:
            image_by_generation[generation_id] = image
    return image_by_generation


async def _feed_variant_kinds(
    db: AsyncSession,
    image_by_generation: dict[str, Image],
) -> dict[str, set[str]]:
    if not image_by_generation:
        return {}
    image_ids = [image.id for image in image_by_generation.values()]
    rows = (
        await db.execute(
            select(ImageVariant.image_id, ImageVariant.kind).where(
                ImageVariant.image_id.in_(image_ids)
            )
        )
    ).all()
    kinds_by_image: dict[str, set[str]] = {}
    for image_id, kind in rows:
        kinds_by_image.setdefault(image_id, set()).add(kind)
    return kinds_by_image


async def _feed_conversation_ids(
    db: AsyncSession,
    generations: list[Generation],
) -> dict[str, str]:
    rows = (
        await db.execute(
            select(Message.id, Message.conversation_id).where(
                Message.id.in_([generation.message_id for generation in generations])
            )
        )
    ).all()
    return {message_id: conversation_id for message_id, conversation_id in rows}


def _feed_item(
    generation: Generation,
    *,
    image: Image,
    variant_kinds: set[str],
    conversation_id: str,
) -> GenerationFeedItem:
    preview_url = (
        f"/api/images/{image.id}/variants/preview1024"
        if "preview1024" in variant_kinds
        else None
    )
    thumb_url = (
        f"/api/images/{image.id}/variants/thumb256"
        if "thumb256" in variant_kinds
        else preview_url or f"/api/images/{image.id}/binary"
    )
    upstream_request = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    quality = upstream_request.get("render_quality")
    output_format = upstream_request.get("output_format")
    return GenerationFeedItem(
        id=generation.id,
        created_at=generation.created_at,
        prompt=generation.prompt,
        aspect_ratio=generation.aspect_ratio,
        has_ref=bool(
            generation.primary_input_image_id
            or (generation.input_image_ids and len(generation.input_image_ids) > 0)
        ),
        fast=_bool_option(upstream_request.get("fast"), False),
        quality=quality if isinstance(quality, str) else None,
        output_format=output_format if isinstance(output_format, str) else None,
        size_actual=f"{image.width}x{image.height}",
        image=GenerationImageOut(
            id=image.id,
            url=f"/api/images/{image.id}/binary",
            mime=image.mime,
            display_url=f"/api/images/{image.id}/variants/display2048",
            preview_url=preview_url,
            thumb_url=thumb_url,
            width=image.width,
            height=image.height,
        ),
        message_id=generation.message_id,
        conversation_id=conversation_id,
    )


def _feed_items(
    generations: list[Generation],
    *,
    images: dict[str, Image],
    variants: dict[str, set[str]],
    conversations: dict[str, str],
) -> list[GenerationFeedItem]:
    items: list[GenerationFeedItem] = []
    for generation in generations:
        image = images.get(generation.id)
        if image is None:
            continue
        items.append(
            _feed_item(
                generation,
                image=image,
                variant_kinds=variants.get(image.id, set()),
                conversation_id=conversations.get(generation.message_id, ""),
            )
        )
    return items


# ---------- routes ----------


@router.get("/feed", response_model=GenerationFeedOut)
async def list_generation_feed(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    ratio: str | None = None,
    has_ref: bool = False,
    fast: bool = False,
    q: str | None = None,
) -> GenerationFeedOut:
    if ratio is not None and ratio not in _ALLOWED_RATIOS:
        raise _http("invalid_ratio", f"unsupported ratio: {ratio}", 400)

    cursor_total: int | None = None
    cursor_filter_sig: str | None = None
    cur_ts: datetime | None = None
    cur_id: str | None = None
    if cursor:
        cur_ts, cur_id, cursor_total, cursor_filter_sig = _decode_cursor(cursor)

    visible_after = await _feed_visible_after(user, db)

    current_filter_sig = _feed_filter_signature(
        user_id=user.id,
        ratio=ratio,
        has_ref=has_ref,
        fast=fast,
        q=q,
        visible_after=visible_after,
    )

    # ---- total（不包括 cursor 分页条件；包括所有过滤）----
    # New cursors carry the first-page total so infinite-scroll page requests
    # don't pay a full COUNT(*) on every fetch.
    trusted_cursor_total = _validated_cursor_total(
        cursor_total=cursor_total,
        cursor_filter_sig=cursor_filter_sig,
        current_filter_sig=current_filter_sig,
    )
    total = await _generation_feed_total(
        db,
        trusted_cursor_total=trusted_cursor_total,
        user_id=user.id,
        ratio=ratio,
        has_ref=has_ref,
        fast=fast,
        q=q,
        visible_after=visible_after,
    )

    # ---- page ----
    gens, has_more = await _generation_feed_page(
        db,
        user_id=user.id,
        ratio=ratio,
        has_ref=has_ref,
        fast=fast,
        q=q,
        visible_after=visible_after,
        cursor_ts=cur_ts,
        cursor_id=cur_id,
        limit=limit,
    )

    if not gens:
        return GenerationFeedOut(items=[], next_cursor=None, total=total)

    # ---- 聚合 image + message 信息 ----
    image_by_gen = await _feed_images(db, gens, visible_after=visible_after)
    kinds_by_image = await _feed_variant_kinds(db, image_by_gen)
    conv_by_msg = await _feed_conversation_ids(db, gens)
    items = _feed_items(
        gens,
        images=image_by_gen,
        variants=kinds_by_image,
        conversations=conv_by_msg,
    )

    next_cursor: str | None = None
    if has_more:
        last = gens[-1]
        next_cursor = _encode_cursor(
            last.created_at,
            last.id,
            total=total,
            filter_sig=current_filter_sig,
        )

    return GenerationFeedOut(items=items, next_cursor=next_cursor, total=total)
