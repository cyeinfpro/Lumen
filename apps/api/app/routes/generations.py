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
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import GenerationStatus
from lumen_core.models import Conversation, Generation, Image, ImageVariant, Message

from ..db import get_db
from ..deps import CurrentUser


router = APIRouter()


CURSOR_VERSION = "v2"


# ---------- 支持的 aspect ratio 白名单 ----------

_ALLOWED_RATIOS = {"1:1", "16:9", "9:16", "4:5", "3:4", "21:9"}


# ---------- Schemas ----------


class GenerationImageOut(BaseModel):
    id: str
    url: str
    mime: str
    display_url: str
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


def _encode_cursor(created_at: datetime, gen_id: str, total: int | None = None) -> str:
    # 统一走 UTC ISO，解析端直接 fromisoformat。版本前缀用于未来演进。
    iso = created_at.astimezone(timezone.utc).isoformat()
    total_part = "" if total is None else str(max(0, total))
    raw = f"{CURSOR_VERSION}|{iso}|{gen_id}|{total_part}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str, int | None]:
    # base64 补齐 padding
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise _http("invalid_cursor", "invalid cursor", 400) from exc
    parts = raw.split("|")
    cursor_total: int | None = None
    if len(parts) == 4:
        version, iso, gen_id, raw_total = parts
        if version != CURSOR_VERSION:
            raise _http("invalid_cursor", "unsupported cursor version", 400)
        if raw_total:
            try:
                cursor_total = max(0, int(raw_total))
            except ValueError as exc:
                raise _http("invalid_cursor", "invalid cursor total", 400) from exc
    elif len(parts) == 3:
        version, iso, gen_id = parts
        if version not in {"v1", CURSOR_VERSION}:
            raise _http("invalid_cursor", "unsupported cursor version", 400)
    elif len(parts) == 2:
        # legacy 兼容：旧客户端的 cursor 不带版本前缀
        iso, gen_id = parts
    else:
        raise _http("invalid_cursor", "invalid cursor", 400)
    try:
        # 接受 Z 后缀 / +00:00；fromisoformat 在 3.11+ 已支持。
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _http("invalid_cursor", "invalid cursor timestamp", 400) from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts, gen_id, cursor_total


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
    )

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
            Generation.upstream_request["fast"].as_boolean().is_(True)
        )

    if q:
        # 简易 prompt LIKE 匹配；大小写不敏感。
        q_escaped = _escape_like_pattern(q.strip())
        pattern = f"%{q_escaped}%"
        if pattern != "%%":
            stmt = stmt.where(Generation.prompt.ilike(pattern, escape="\\"))

    return stmt


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
    cur_ts: datetime | None = None
    cur_id: str | None = None
    if cursor:
        cur_ts, cur_id, cursor_total = _decode_cursor(cursor)

    # ---- total（不包括 cursor 分页条件；包括所有过滤）----
    # New cursors carry the first-page total so infinite-scroll page requests
    # don't pay a full COUNT(*) on every fetch.
    if cursor_total is not None:
        total = cursor_total
    else:
        count_stmt: Select = select(func.count(Generation.id))  # type: ignore[assignment]
        count_stmt = _apply_filters(
            count_stmt,
            user_id=user.id,
            ratio=ratio,
            has_ref=has_ref,
            fast=fast,
            q=q,
        )
        total = int((await db.execute(count_stmt)).scalar() or 0)

    # ---- page ----
    stmt: Select = select(Generation)  # type: ignore[assignment]
    stmt = _apply_filters(
        stmt,
        user_id=user.id,
        ratio=ratio,
        has_ref=has_ref,
        fast=fast,
        q=q,
    )

    if cur_ts is not None and cur_id is not None:
        # (created_at, id) < (cur_ts, cur_id) tuple 比较
        stmt = stmt.where(
            or_(
                Generation.created_at < cur_ts,
                and_(Generation.created_at == cur_ts, Generation.id < cur_id),
            )
        )

    stmt = stmt.order_by(Generation.created_at.desc(), Generation.id.desc()).limit(
        limit + 1
    )

    rows = (await db.execute(stmt)).scalars().all()
    gens = list(rows[:limit])
    has_more = len(rows) > limit

    if not gens:
        return GenerationFeedOut(items=[], next_cursor=None, total=total)

    # ---- 聚合 image + message 信息 ----
    gen_ids = [g.id for g in gens]

    # 每个 generation 拿"最早一张 owner image"（多图场景），没有则跳过。
    # 本轮选择最旧一张（created_at, id ASC），便于 UI 稳定。
    img_rows = (
        await db.execute(
            select(Image)
            .where(
                Image.owner_generation_id.in_(gen_ids),
                Image.deleted_at.is_(None),
            )
            .order_by(Image.created_at.asc(), Image.id.asc())
        )
    ).scalars().all()

    image_by_gen: dict[str, Image] = {}
    for img in img_rows:
        gid = img.owner_generation_id
        if gid and gid not in image_by_gen:
            image_by_gen[gid] = img

    # 查 variant 种类（只看这些 image）
    kinds_by_image: dict[str, set[str]] = {}
    if image_by_gen:
        image_ids = [i.id for i in image_by_gen.values()]
        variant_rows = (
            await db.execute(
                select(ImageVariant.image_id, ImageVariant.kind).where(
                    ImageVariant.image_id.in_(image_ids)
                )
            )
        ).all()
        for image_id, kind in variant_rows:
            kinds_by_image.setdefault(image_id, set()).add(kind)

    # message_id → conversation_id：Message.conversation_id
    # 通过一次 select 拿到所有 message 的 conv_id
    conv_rows = (
        await db.execute(
            select(Message.id, Message.conversation_id).where(
                Message.id.in_([g.message_id for g in gens])
            )
        )
    ).all()
    conv_by_msg: dict[str, str] = {mid: cid for (mid, cid) in conv_rows}

    # ---- 组装 items ----
    items: list[GenerationFeedItem] = []
    for g in gens:
        img = image_by_gen.get(g.id)
        if not img:
            # 数据不一致：succeeded 但无 image。跳过，保证前端 item.image 必存在。
            continue

        variant_kinds = kinds_by_image.get(img.id, set())
        # 优先 preview1024 作为 thumb；否则 thumb256；否则退回 binary。
        if "preview1024" in variant_kinds:
            thumb_url = f"/api/images/{img.id}/variants/preview1024"
        elif "thumb256" in variant_kinds:
            thumb_url = f"/api/images/{img.id}/variants/thumb256"
        else:
            thumb_url = f"/api/images/{img.id}/binary"

        upstream_request = g.upstream_request if isinstance(g.upstream_request, dict) else {}
        items.append(
            GenerationFeedItem(
                id=g.id,
                created_at=g.created_at,
                prompt=g.prompt,
                aspect_ratio=g.aspect_ratio,
                has_ref=bool(
                    g.primary_input_image_id
                    or (g.input_image_ids and len(g.input_image_ids) > 0)
                ),
                fast=bool(upstream_request.get("fast")),
                quality=(
                    upstream_request.get("render_quality")
                    if isinstance(upstream_request.get("render_quality"), str)
                    else None
                ),
                output_format=(
                    upstream_request.get("output_format")
                    if isinstance(upstream_request.get("output_format"), str)
                    else None
                ),
                size_actual=f"{img.width}x{img.height}",
                image=GenerationImageOut(
                    id=img.id,
                    url=f"/api/images/{img.id}/binary",
                    mime=img.mime,
                    display_url=f"/api/images/{img.id}/variants/display2048",
                    thumb_url=thumb_url,
                    width=img.width,
                    height=img.height,
                ),
                message_id=g.message_id,
                conversation_id=conv_by_msg.get(g.message_id, ""),
            )
        )

    next_cursor: str | None = None
    if has_more and items:
        last = gens[-1]
        next_cursor = _encode_cursor(last.created_at, last.id, total=total)

    return GenerationFeedOut(items=items, next_cursor=next_cursor, total=total)
