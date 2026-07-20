"""Redemption and redemption-batch routes."""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import (
    RedemptionBatch,
    RedemptionCode,
    RedemptionCodeUsage,
    User,
    new_uuid7,
)
from lumen_core.schemas import (
    AdminRedemptionBatchRedownloadOut,
    AdminRedemptionCodeCreateIn,
    AdminRedemptionCodeCreateOut,
    AdminRedemptionCodeListOut,
    AdminRedemptionCodeOut,
    AdminRedemptionUsageListOut,
    AdminRedemptionUsageOut,
    RedemptionIn,
    RedemptionOut,
    RedemptionUsageListOut,
    RedemptionUsageOut,
)

from ...audit import hash_email
from ...db import get_db
from ...deps import AdminUser, CurrentUser, verify_csrf
from ...observability import redemption_redeemed_total
from ...ratelimit import client_ip
from .compat import current_runtime


router = APIRouter()


@router.post(
    "/me/redemptions",
    response_model=RedemptionOut,
    dependencies=[Depends(verify_csrf)],
)
async def redeem_code(
    body: RedemptionIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedemptionOut:
    b = current_runtime()
    b._require_wallet_user(user)
    normalized_code = billing_core.normalize_redemption_code(body.code)
    if len(normalized_code) < 4:
        raise b._http("invalid_code", "redemption code is invalid", 422)
    request_hash = b._redemption_request_hash(normalized_code)
    idempotency_key = b._redemption_idempotency_key(
        request,
        user_id=user.id,
        normalized_code=normalized_code,
    )
    cached = await b._cached_redemption_out(user.id, idempotency_key, request_hash)
    if cached is not None:
        return cached
    await b._lock_redemption_idempotency_key(db, user.id, idempotency_key)
    cached = await b._cached_redemption_out(user.id, idempotency_key, request_hash)
    if cached is not None:
        return cached
    usage_id = b._redemption_usage_id(user.id, idempotency_key)
    existing = await b._redemption_out_for_usage(
        db,
        user_id=user.id,
        usage_id=usage_id,
        request_hash=request_hash,
    )
    if existing is not None:
        await b._cache_redemption_out(user.id, idempotency_key, request_hash, existing)
        return existing

    await b._require_redemption_operational(db)
    redis = b.get_redis()
    await b.REDEMPTION_LIMITER.check(redis, f"rl:redemption:user:{user.id}")
    await b.REDEMPTION_LIMITER.check(redis, f"rl:redemption:ip:{client_ip(request)}")
    code_hashes = [
        billing_core.hash_redemption_code(normalized_code, secret)
        for secret in await b._redemption_secrets(db)
    ]
    now = datetime.now(timezone.utc)
    matching_codes = (
        (
            await db.execute(
                select(RedemptionCode)
                .where(RedemptionCode.code_hash.in_(code_hashes))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    codes_by_hash = {item.code_hash: item for item in matching_codes}
    code = next((codes_by_hash.get(code_hash) for code_hash in code_hashes), None)
    if code is None:
        raise b._http("CODE_NOT_FOUND", "redemption code not found", 404)
    if code.revoked_at is not None:
        raise b._http("CODE_REVOKED", "redemption code was revoked", 410)
    expires_at = code.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at <= now:
        raise b._http("CODE_EXPIRED", "redemption code expired", 410)
    if code.redeemed_count >= code.max_redemptions:
        raise b._http("CODE_EXHAUSTED", "redemption code is exhausted", 409)
    try:
        tx = await billing_core.topup_redeem(
            db,
            user.id,
            code.amount_micro,
            usage_id=usage_id,
            code_id=code.id,
            meta={
                "client_idempotency_hash": hashlib.sha256(
                    idempotency_key.encode("utf-8")
                ).hexdigest()[:16],
                "redemption_request_hash": request_hash,
            },
        )
        db.add(
            RedemptionCodeUsage(
                id=usage_id,
                code_id=code.id,
                user_id=user.id,
                amount_micro=code.amount_micro,
                wallet_tx_id=tx.id,
                ip_hash=b.request_ip_hash(request),
            )
        )
        code.redeemed_count += 1
        redemption_redeemed_total.inc()
        await b.write_audit(
            db,
            event_type="wallet.topup.redeem",
            user_id=user.id,
            actor_email_hash=hash_email(user.email),
            actor_ip_hash=b.request_ip_hash(request),
            details={
                "code_id": code.id,
                "usage_id": usage_id,
                "amount_micro": code.amount_micro,
                "balance_after": tx.balance_after,
            },
            autocommit=False,
        )
        await db.commit()
        await b._invalidate_balance_cache(user.id)
    except IntegrityError as exc:
        await db.rollback()
        constraint_name = b._integrity_constraint_name(exc)
        if constraint_name in b._REDEMPTION_REPLAY_CONSTRAINTS:
            existing = await b._redemption_out_for_usage(
                db,
                user_id=user.id,
                usage_id=usage_id,
                request_hash=request_hash,
            )
            if existing is not None:
                await b._cache_redemption_out(
                    user.id, idempotency_key, request_hash, existing
                )
                return existing
        if constraint_name == b._REDEMPTION_ALREADY_USED_CONSTRAINT:
            raise b._http(
                "CODE_ALREADY_USED",
                "this code was already used by this user",
                409,
            ) from exc
        raise
    response = RedemptionOut(
        amount=b._money(code.amount_micro), balance=b._money(tx.balance_after)
    )
    await b._cache_redemption_out(user.id, idempotency_key, request_hash, response)
    return response


@router.get("/me/redemptions", response_model=RedemptionUsageListOut)
async def list_my_redemptions(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> RedemptionUsageListOut:
    b = current_runtime()
    b._require_wallet_user(user)
    stmt = (
        select(RedemptionCodeUsage)
        .where(RedemptionCodeUsage.user_id == user.id)
        .order_by(RedemptionCodeUsage.redeemed_at.desc(), RedemptionCodeUsage.id.desc())
        .limit(limit + 1)
    )
    stmt = b._cursor_filter(stmt, RedemptionCodeUsage, cursor, attr="redeemed_at")
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return RedemptionUsageListOut(
        items=[
            RedemptionUsageOut(
                id=row.id,
                code_id=row.code_id,
                amount=b._money(row.amount_micro),
                redeemed_at=row.redeemed_at,
            )
            for row in rows
        ],
        next_cursor=b._next_cursor(rows, has_more, attr="redeemed_at"),
    )


@router.get("/admin/redemption_codes", response_model=AdminRedemptionCodeListOut)
async def admin_list_redemption_codes(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = "active",
    batch_id: str | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> AdminRedemptionCodeListOut:
    b = current_runtime()
    stmt = select(RedemptionCode)
    now = datetime.now(timezone.utc)
    if batch_id:
        stmt = stmt.where(RedemptionCode.batch_id == batch_id)
    if q:
        needle = q.strip()
        if needle:
            stmt = stmt.where(
                or_(
                    RedemptionCode.code_prefix.ilike(f"{needle[:8]}%"),
                    RedemptionCode.batch_id.ilike(f"%{needle}%"),
                )
            )
    filters = {
        "revoked": RedemptionCode.revoked_at.is_not(None),
        "expired": (
            RedemptionCode.expires_at.is_not(None),
            RedemptionCode.expires_at <= now,
        ),
        "exhausted": RedemptionCode.redeemed_count >= RedemptionCode.max_redemptions,
        "active": (
            RedemptionCode.revoked_at.is_(None),
            or_(
                RedemptionCode.expires_at.is_(None),
                RedemptionCode.expires_at > now,
            ),
            RedemptionCode.redeemed_count < RedemptionCode.max_redemptions,
        ),
    }
    if status in filters:
        selected = filters[status]
        stmt = (
            stmt.where(*selected)
            if isinstance(selected, tuple)
            else stmt.where(selected)
        )
    elif status not in {None, "", "all"}:
        raise b._http("invalid_status", "status is invalid", 422)
    stmt = b._cursor_filter(stmt, RedemptionCode, cursor)
    rows = (
        (
            await db.execute(
                stmt.order_by(
                    RedemptionCode.created_at.desc(), RedemptionCode.id.desc()
                ).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return AdminRedemptionCodeListOut(
        items=[b._redemption_code_out(row, now=now) for row in rows],
        next_cursor=b._next_cursor(rows, has_more),
    )


@router.get(
    "/admin/redemption_codes/{code_id}/usage",
    response_model=AdminRedemptionUsageListOut,
)
async def admin_list_redemption_code_usage(
    code_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> AdminRedemptionUsageListOut:
    b = current_runtime()
    rows = (
        await db.execute(
            select(RedemptionCodeUsage, User.email)
            .join(User, User.id == RedemptionCodeUsage.user_id)
            .where(RedemptionCodeUsage.code_id == code_id)
            .order_by(
                RedemptionCodeUsage.redeemed_at.desc(),
                RedemptionCodeUsage.id.desc(),
            )
            .limit(limit)
        )
    ).all()
    return AdminRedemptionUsageListOut(
        items=[
            AdminRedemptionUsageOut(
                id=usage.id,
                code_id=usage.code_id,
                user_id=usage.user_id,
                user_email=email,
                amount=b._money(usage.amount_micro),
                wallet_tx_id=usage.wallet_tx_id,
                redeemed_at=usage.redeemed_at,
                ip_hash=usage.ip_hash,
            )
            for usage, email in rows
        ]
    )


@router.post(
    "/admin/redemption_codes",
    response_model=AdminRedemptionCodeCreateOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_create_redemption_codes(
    body: AdminRedemptionCodeCreateIn,
    request: Request,
    response: Response,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedemptionCodeCreateOut:
    b = current_runtime()
    amount = b._rmb_to_micro_or_422(body.amount_rmb, field="amount_rmb")
    if amount <= 0:
        raise b._http("invalid_amount", "amount must be positive", 422)
    request_hash = b._redemption_batch_request_hash(body, amount_micro=amount)
    now = datetime.now(timezone.utc)
    idempotency_key = b._redemption_batch_idempotency_key(
        request,
        admin_id=admin.id,
        request_hash=request_hash,
        now=now,
    )
    lock_identity = b._redemption_batch_lock_identity(idempotency_key, request_hash)
    await b._lock_redemption_batch_idempotency_key(db, admin.id, lock_identity)
    existing_batch = await b._redemption_batch_for_idempotency(
        db,
        admin_id=admin.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        created_after=now - timedelta(seconds=b._REDEMPTION_DOWNLOAD_TTL_SECONDS),
    )
    if existing_batch is not None:
        return await b._replay_redemption_batch(
            existing_batch,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            response=response,
        )
    await b._require_bootstrap_completed(db)
    secret = await b._redemption_secret(db)
    batch_id = new_uuid7()
    batch = RedemptionBatch(
        id=batch_id,
        created_by=admin.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        amount_micro=amount,
        code_count=body.count,
        max_redemptions=body.max_redemptions,
        expires_at=body.expires_at,
    )
    db.add(batch)
    try:
        await db.flush([batch])
    except IntegrityError as exc:
        await db.rollback()
        if (
            b._integrity_constraint_name(exc)
            != b._REDEMPTION_BATCH_IDEMPOTENCY_CONSTRAINT
        ):
            raise
        existing_batch = await b._redemption_batch_for_idempotency(
            db,
            admin_id=admin.id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            created_after=now - timedelta(seconds=b._REDEMPTION_DOWNLOAD_TTL_SECONDS),
        )
        if existing_batch is None:
            raise
        return await b._replay_redemption_batch(
            existing_batch,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            response=response,
        )
    plaintext_codes: list[str] = []
    for _ in range(body.count):
        code = billing_core.generate_redemption_code()
        plaintext_codes.append(code)
        db.add(
            RedemptionCode(
                id=new_uuid7(),
                code_hash=billing_core.hash_redemption_code(code, secret),
                code_prefix=billing_core.code_prefix(code),
                amount_micro=amount,
                max_redemptions=body.max_redemptions,
                batch_id=batch_id,
                note=body.note,
                expires_at=body.expires_at,
                created_by=admin.id,
            )
        )
    await b.write_audit(
        db,
        event_type="redemption.create",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=b.request_ip_hash(request),
        details={
            "batch_id": batch_id,
            "count": body.count,
            "amount_micro": amount,
            "idempotency_key_hash": hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest()[:16],
        },
        autocommit=False,
    )
    await db.flush()
    try:
        token = await b._store_redemption_plaintext_batch(
            batch_id=batch_id,
            amount_micro=amount,
            codes=plaintext_codes,
            expires_at=body.expires_at,
        )
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        raise b._http(
            "download_cache_unavailable",
            "redemption code download cache is unavailable; no codes were created",
            503,
        ) from exc
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        try:
            redis = b.get_redis()
            await redis.delete(b._DOWNLOAD_TOKEN_PREFIX + token)
            await redis.delete(b._PLAINTEXT_BATCH_PREFIX + batch_id)
        except Exception:
            b.logger.warning(
                "redemption plaintext cache cleanup failed batch_id=%s token=%s",
                batch_id,
                token,
                exc_info=True,
            )
        raise
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotency-Key"] = idempotency_key
    return AdminRedemptionCodeCreateOut(
        batch_id=batch_id,
        count=body.count,
        amount=b._money(amount),
        download_token=token,
        plaintext_codes=plaintext_codes,
        expires_at=body.expires_at,
    )


@router.get("/admin/redemption_codes/batches/{batch_id}.csv")
async def admin_download_redemption_batch_csv(
    batch_id: str,
    _admin: AdminUser,
    download_token: str = Query(min_length=8),
) -> StreamingResponse:
    b = current_runtime()
    data = await b.get_redis().get(b._DOWNLOAD_TOKEN_PREFIX + download_token)
    if data is None:
        raise b._http("download_token_expired", "download token expired", 410)
    text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    b._require_redemption_download_batch(text, batch_id)
    return StreamingResponse(
        io.BytesIO(text.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="redemption-{batch_id}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/admin/redemption_codes/batches/{batch_id}.txt")
async def admin_download_redemption_batch_txt(
    batch_id: str,
    _admin: AdminUser,
    download_token: str = Query(min_length=8),
) -> StreamingResponse:
    b = current_runtime()
    data = await b.get_redis().get(b._DOWNLOAD_TOKEN_PREFIX + download_token)
    if data is None:
        raise b._http("download_token_expired", "download token expired", 410)
    csv_text = (
        data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    )
    b._require_redemption_download_batch(csv_text, batch_id)
    codes = [
        str(row.get("code") or "")
        for row in csv.DictReader(io.StringIO(csv_text))
        if row.get("code")
    ]
    text = "\n".join(codes) + ("\n" if codes else "")
    return StreamingResponse(
        io.BytesIO(text.encode("utf-8")),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="redemption-{batch_id}.txt"',
            "Cache-Control": "no-store",
        },
    )


@router.post(
    "/admin/redemption_codes/batches/{batch_id}/redownload",
    response_model=AdminRedemptionBatchRedownloadOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_redownload_redemption_batch(
    batch_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedemptionBatchRedownloadOut:
    b = current_runtime()
    payload = await b._load_redemption_plaintext_batch(batch_id)
    codes = [str(code) for code in payload["codes"]]
    amount = b._rmb_to_micro_or_422(
        str(payload.get("amount_rmb") or "0"), field="amount_rmb"
    )
    expires_raw = payload.get("expires_at")
    expires_at = (
        datetime.fromisoformat(expires_raw)
        if isinstance(expires_raw, str) and expires_raw
        else None
    )
    token = await b._store_redemption_plaintext_batch(
        batch_id=batch_id,
        amount_micro=amount,
        codes=codes,
        expires_at=expires_at,
    )
    await b.write_audit(
        db,
        event_type="redemption.batch.redownload",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=b.request_ip_hash(request),
        details={"batch_id": batch_id, "count": len(codes)},
        autocommit=False,
    )
    await db.commit()
    return AdminRedemptionBatchRedownloadOut(
        batch_id=batch_id,
        count=len(codes),
        download_token=token,
        plaintext_codes=codes,
        expires_in_seconds=b._REDEMPTION_DOWNLOAD_TTL_SECONDS,
    )


@router.post(
    "/admin/redemption_codes/{code_id}:revoke",
    response_model=AdminRedemptionCodeOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_revoke_redemption_code(
    code_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedemptionCodeOut:
    b = current_runtime()
    code = await db.get(RedemptionCode, code_id)
    if code is None:
        raise b._http("not_found", "redemption code not found", 404)
    if code.revoked_at is not None:
        raise b._http("ALREADY_REVOKED", "redemption code was already revoked", 409)
    code.revoked_at = datetime.now(timezone.utc)
    await b.write_audit(
        db,
        event_type="redemption.revoke",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=b.request_ip_hash(request),
        details={"code_id": code_id},
        autocommit=False,
    )
    await db.commit()
    await db.refresh(code)
    return b._redemption_code_out(code)


@router.post(
    "/admin/redemption_codes/batches/{batch_id}:revoke",
    response_model=AdminRedemptionCodeListOut,
    dependencies=[Depends(verify_csrf)],
)
async def admin_revoke_redemption_batch(
    batch_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminRedemptionCodeListOut:
    b = current_runtime()
    now = datetime.now(timezone.utc)
    await db.execute(
        update(RedemptionCode)
        .where(RedemptionCode.batch_id == batch_id, RedemptionCode.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await b.write_audit(
        db,
        event_type="redemption.batch.revoke",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=b.request_ip_hash(request),
        details={"batch_id": batch_id},
        autocommit=False,
    )
    await db.commit()
    return await b.admin_list_redemption_codes(
        admin, db, status="all", batch_id=batch_id
    )
