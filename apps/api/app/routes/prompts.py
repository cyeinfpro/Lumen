"""提示词增强（Prompt Enhancement）。

POST /prompts/enhance — 流式返回 AI 优化后的图像生成提示词。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Annotated, Any, AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import new_uuid7
from lumen_core.pricing import UsageTokens, parse_usage
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    build_effective_provider_config,
    endpoint_kind_allowed,
    resolve_provider_proxy_url,
    weighted_priority_order,
)
from lumen_core.runtime_settings import get_spec

from ..billing_cache_state import invalidate_balance_cache
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..audit import hash_email, write_audit
from ..ratelimit import RateLimiter
from ..redis_client import get_redis
from ..runtime_settings import get_setting

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/prompts",
    tags=["prompts"],
    dependencies=[Depends(verify_csrf)],
)

ENHANCE_SYSTEM_PROMPT = """\
You are an expert prompt engineer for AI image generation.
Your task is to enhance the user's image prompt to produce more vivid, detailed results.

Rules:
- Maintain the user's original intent and subject matter exactly
- Add rich details: lighting, atmosphere, composition, texture, color palette, style
- Keep the output concise — one paragraph, under 200 words
- Write in the same language as the input
- Do NOT add negative prompts, technical parameters, or meta-instructions
- Do NOT wrap in quotes or add any prefix/suffix like "Enhanced prompt:"
- Output ONLY the enhanced prompt text, nothing else\
"""

_PROVIDER_RR_COUNTERS: dict[int, int] = {}
_PROVIDER_RR_LOCK = asyncio.Lock()
_RETRYABLE_HTTP_STATUS = {408, 409, 425, 429}
_FALLBACK_400_MARKERS = (
    "model",
    "service_tier",
    "tier",
    "reasoning",
    "unsupported",
    "not_found",
    "not found",
)
PROMPTS_ENHANCE_LIMITER = RateLimiter(capacity=20, refill_per_sec=20 / 60)


@dataclass(frozen=True)
class _EnhanceAttempt:
    name: str
    model: str
    reasoning_effort: str | None = "low"
    service_tier: str | None = "priority"


@dataclass
class _EnhanceBillingContext:
    db: AsyncSession
    user_id: str
    user_email: str | None
    request_id: str
    rate_multiplier_x10000: int
    cache_aware: bool
    allow_negative: bool
    hold_amount_micro: int = 0


@dataclass
class _EnhanceUsageCapture:
    provider_name: str | None = None
    model: str | None = None
    service_tier: str = "standard"
    response_id: str | None = None
    usage: dict[str, Any] | None = None


_ENHANCE_ATTEMPTS = (
    _EnhanceAttempt(name="primary", model="gpt-5.5", reasoning_effort="low"),
    _EnhanceAttempt(
        name="fallback-gpt-5.4-low", model="gpt-5.4", reasoning_effort="low"
    ),
    _EnhanceAttempt(
        name="fallback-gpt-5.4-low-standard",
        model="gpt-5.4",
        reasoning_effort="low",
        service_tier=None,
    ),
)


class EnhanceIn(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


def _responses_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"


async def _resolve_provider_order(db: AsyncSession) -> list[ProviderDefinition]:
    """Read Provider Pool, with legacy UPSTREAM_* env fallback only if absent."""
    spec_providers = get_spec("providers")
    raw_providers = await get_setting(db, spec_providers) if spec_providers else None
    providers, _proxies, errors = build_effective_provider_config(
        raw_providers=raw_providers,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    for err in errors:
        logger.warning("%s", err)
    providers = [p for p in providers if endpoint_kind_allowed(p, "responses")]
    async with _PROVIDER_RR_LOCK:
        return weighted_priority_order(providers, _PROVIDER_RR_COUNTERS)


class _EnhanceProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _build_enhance_body(text: str, attempt: _EnhanceAttempt) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": attempt.model,
        "instructions": ENHANCE_SYSTEM_PROMPT,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
        "stream": True,
    }
    if attempt.reasoning_effort:
        body["reasoning"] = {"effort": attempt.reasoning_effort}
    if attempt.service_tier:
        body["service_tier"] = attempt.service_tier
    return body


async def _setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    try:
        return await get_setting(db, spec)
    except (AssertionError, IndexError):
        if key.startswith("billing."):
            return None
        raise


async def _billing_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.enabled"),
        False,
    )


async def _billing_cache_aware(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.cache_aware"),
        True,
    )


async def _billing_allow_negative(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _setting_raw(db, "billing.allow_negative_balance"),
        False,
    )


def _rate_multiplier_x10000(user: Any) -> int:
    raw = getattr(user, "billing_rate_multiplier", 1)
    try:
        return max(0, int(float(raw if raw is not None else 1) * 10_000))
    except (TypeError, ValueError):
        return 10_000


async def _prepare_prompt_enhance_billing(
    db: AsyncSession,
    user: Any,
) -> _EnhanceBillingContext | None:
    if getattr(user, "account_mode", "wallet") != "wallet":
        return None
    if not await _billing_enabled(db):
        return None

    request_id = new_uuid7()
    rate_multiplier_x10000 = _rate_multiplier_x10000(user)
    cache_aware = await _billing_cache_aware(db)
    allow_negative = await _billing_allow_negative(db)
    preview = await billing_core.estimate_completion_cost(
        db,
        model=_ENHANCE_ATTEMPTS[0].model,
        tokens_in=1,
        tokens_out=1,
        rate_multiplier_x10000=rate_multiplier_x10000,
        service_tier=_ENHANCE_ATTEMPTS[0].service_tier or "standard",
    )
    if preview <= 0:
        await write_audit(
            db,
            event_type="pricing.not_configured",
            user_id=user.id,
            actor_email_hash=hash_email(getattr(user, "email", None)),
            details={
                "scope": "chat_model",
                "model": _ENHANCE_ATTEMPTS[0].model,
                "route": "prompts.enhance.preflight",
            },
            autocommit=False,
        )
    hold_amount = max(10_000, int(preview or 0))
    try:
        await billing_core.hold(
            db,
            user.id,
            hold_amount,
            ref_type="prompt_enhance",
            ref_id=request_id,
            idempotency_key=f"prompt_enhance:hold:{request_id}",
            allow_negative=allow_negative,
            meta={
                "route": "prompts.enhance",
                "model": _ENHANCE_ATTEMPTS[0].model,
                "service_tier": _ENHANCE_ATTEMPTS[0].service_tier or "standard",
                "estimated_cost_micro": preview,
                "preauth_micro": hold_amount,
            },
        )
    except billing_core.BillingError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": {"code": exc.code, "message": exc.message}},
        ) from exc
    await db.commit()
    await invalidate_balance_cache(user.id)
    return _EnhanceBillingContext(
        db=db,
        user_id=user.id,
        user_email=getattr(user, "email", None),
        request_id=request_id,
        rate_multiplier_x10000=rate_multiplier_x10000,
        cache_aware=cache_aware,
        allow_negative=allow_negative,
        hold_amount_micro=hold_amount,
    )


def _capture_enhance_usage(
    capture: _EnhanceUsageCapture | None,
    event: dict[str, Any],
    *,
    provider: ProviderDefinition,
    attempt: _EnhanceAttempt,
) -> None:
    if capture is None:
        return
    response = event.get("response")
    response_obj = response if isinstance(response, dict) else {}
    usage = event.get("usage")
    if not isinstance(usage, dict):
        usage = response_obj.get("usage")
    if not isinstance(usage, dict):
        return

    response_id = response_obj.get("id") or event.get("response_id")
    model = response_obj.get("model") or event.get("model") or attempt.model
    capture.provider_name = provider.name
    capture.model = model if isinstance(model, str) and model.strip() else attempt.model
    capture.service_tier = attempt.service_tier or "standard"
    capture.response_id = (
        response_id if isinstance(response_id, str) and response_id.strip() else None
    )
    capture.usage = usage


def _normalize_usage_for_billing(
    usage: UsageTokens,
    *,
    cache_aware: bool,
) -> UsageTokens:
    if cache_aware:
        return usage.normalized()
    legacy_cache_input_tokens = usage.cache_read_tokens + usage.cache_creation_tokens
    return UsageTokens(
        input_tokens=usage.input_tokens + legacy_cache_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        image_output_tokens=usage.image_output_tokens,
    ).normalized()


async def _charge_prompt_enhance(
    billing: _EnhanceBillingContext,
    capture: _EnhanceUsageCapture,
) -> None:
    if not capture.usage:
        await _release_prompt_enhance_hold(billing, reason="missing_usage")
        return
    model = capture.model or _ENHANCE_ATTEMPTS[0].model
    usage = _normalize_usage_for_billing(
        parse_usage(model, capture.usage),
        cache_aware=billing.cache_aware,
    )
    if (
        usage.input_tokens <= 0
        and usage.output_tokens <= 0
        and usage.cache_read_tokens <= 0
        and usage.cache_creation_tokens <= 0
        and usage.cache_creation_5m_tokens <= 0
        and usage.cache_creation_1h_tokens <= 0
        and usage.reasoning_tokens <= 0
        and usage.image_output_tokens <= 0
    ):
        await _release_prompt_enhance_hold(billing, reason="zero_usage")
        return

    breakdown = await billing_core.estimate_completion_breakdown(
        billing.db,
        model=model,
        tokens=usage,
        rate_multiplier_x10000=billing.rate_multiplier_x10000,
        service_tier=capture.service_tier,
    )
    cost = breakdown.actual_cost_micro
    response_id = capture.response_id or billing.request_id
    ref_id = billing.request_id if billing.hold_amount_micro > 0 else response_id
    if cost <= 0 and (usage.input_tokens > 0 or usage.output_tokens > 0):
        await write_audit(
            billing.db,
            event_type=(
                "billing.pricing.missing"
                if breakdown.pricing_source == "missing"
                else "pricing.not_configured"
            ),
            user_id=billing.user_id,
            actor_email_hash=hash_email(billing.user_email),
            details={
                "scope": "chat_model",
                "model": model,
                "prompt_enhance_id": ref_id,
                "usage": usage.model_dump(),
                "pricing_source": breakdown.pricing_source,
            },
            autocommit=False,
        )
    elif breakdown.pricing_source == "fallback":
        await write_audit(
            billing.db,
            event_type="billing.pricing.fallback_used",
            user_id=billing.user_id,
            actor_email_hash=hash_email(billing.user_email),
            details={
                "model": model,
                "prompt_enhance_id": ref_id,
                "usage": usage.model_dump(),
            },
            autocommit=False,
        )

    tx_meta = {
        "route": "prompts.enhance",
        "model": model,
        "provider": capture.provider_name,
        "response_id": response_id,
        "tokens_in": usage.input_tokens,
        "tokens_out": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_creation_5m_tokens": usage.cache_creation_5m_tokens,
        "cache_creation_1h_tokens": usage.cache_creation_1h_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "image_output_tokens": usage.image_output_tokens,
        "cost_breakdown": breakdown.model_dump(),
        "rate_multiplier_x10000": billing.rate_multiplier_x10000,
        "service_tier": capture.service_tier,
    }
    if billing.hold_amount_micro > 0:
        tx = await billing_core.settle(
            billing.db,
            billing.user_id,
            ref_type="prompt_enhance",
            ref_id=ref_id,
            actual_micro=cost,
            idempotency_key=f"prompt_enhance:settle:{ref_id}",
            allow_negative=billing.allow_negative,
            meta={**tx_meta, "preauth_micro": billing.hold_amount_micro},
        )
    else:
        tx = await billing_core.charge(
            billing.db,
            billing.user_id,
            cost,
            ref_type="prompt_enhance",
            ref_id=ref_id,
            idempotency_key=f"prompt_enhance:{ref_id}",
            allow_negative=billing.allow_negative,
            record_zero=True,
            kind="charge_completion",
            meta=tx_meta,
        )
    if tx is not None:
        await write_audit(
            billing.db,
            event_type="wallet.charge.completion",
            user_id=billing.user_id,
            actor_email_hash=hash_email(billing.user_email),
            details={
                "completion_id": ref_id,
                "prompt_enhance_id": ref_id,
                "response_id": response_id,
                "route": "prompts.enhance",
                "cost_micro": cost,
                "usage": usage.model_dump(),
                "cost_breakdown": breakdown.model_dump(),
                "service_tier": capture.service_tier,
                "amount_micro": tx.amount_micro,
                "balance_after": tx.balance_after,
            },
            autocommit=False,
        )
    await billing.db.commit()
    if tx is not None:
        await invalidate_balance_cache(billing.user_id)


async def _release_prompt_enhance_hold(
    billing: _EnhanceBillingContext | None,
    *,
    reason: str,
) -> None:
    if billing is None or billing.hold_amount_micro <= 0:
        return
    try:
        await billing_core.release(
            billing.db,
            billing.user_id,
            ref_type="prompt_enhance",
            ref_id=billing.request_id,
            idempotency_key=f"prompt_enhance:release:{billing.request_id}:{reason}",
            meta={"route": "prompts.enhance", "reason": reason},
        )
        await billing.db.commit()
        await invalidate_balance_cache(billing.user_id)
    except Exception:
        logger.exception("prompt enhance billing hold release failed")


def _is_retryable_upstream_error(status_code: int, raw: bytes) -> bool:
    if status_code in _RETRYABLE_HTTP_STATUS or status_code >= 500:
        return True
    if status_code not in {400, 404}:
        return False
    text = raw[:2000].decode("utf-8", errors="ignore").lower()
    return any(marker in text for marker in _FALLBACK_400_MARKERS)


def _extract_error_message(evt: dict[str, Any]) -> str:
    err = evt.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or err.get("type")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        return json.dumps(err, ensure_ascii=False)[:500]
    if isinstance(err, str) and err.strip():
        return err.strip()
    msg = evt.get("message")
    return msg.strip() if isinstance(msg, str) and msg.strip() else "response_failed"


def _extract_response_text(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    chunks: list[str] = []
    output = obj.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        chunks.append(text)
    text = obj.get("output_text") or obj.get("text")
    if isinstance(text, str) and text:
        chunks.append(text)
    return "".join(chunks)


def _iter_sse_payloads_from_buffer(buffer: str) -> tuple[list[str], str]:
    buffer = buffer.replace("\r\n", "\n")
    payloads: list[str] = []
    while "\n\n" in buffer:
        raw_event, buffer = buffer.split("\n\n", 1)
        data_lines: list[str] = []
        for line in raw_event.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            payloads.append("\n".join(data_lines))
    return payloads, buffer


async def _stream_enhance_one(
    text: str,
    provider: ProviderDefinition,
    attempt: _EnhanceAttempt,
    capture: _EnhanceUsageCapture | None = None,
) -> AsyncIterator[str]:
    url = _responses_url(provider.base_url)

    body = _build_enhance_body(text, attempt)

    try:
        proxy_url = await resolve_provider_proxy_url(provider.proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            proxy=proxy_url,
        ) as client:
            async with client.stream(
                "POST",
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {provider.api_key}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status_code != 200:
                    raw = await resp.aread()
                    logger.warning(
                        "enhance upstream error provider=%s attempt=%s status=%s: %s",
                        provider.name,
                        attempt.name,
                        resp.status_code,
                        raw[:500],
                    )
                    raise _EnhanceProviderError(
                        f"upstream http {resp.status_code}",
                        retryable=_is_retryable_upstream_error(resp.status_code, raw),
                    )

                buf = ""
                emitted = False
                async for chunk in resp.aiter_text():
                    buf += chunk
                    payloads, buf = _iter_sse_payloads_from_buffer(buf)
                    for payload in payloads:
                        if payload == "[DONE]":
                            return
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        _capture_enhance_usage(
                            capture,
                            evt,
                            provider=provider,
                            attempt=attempt,
                        )
                        evt_type = evt.get("type", "")
                        if evt_type == "response.output_text.delta":
                            delta = evt.get("delta", "")
                            if delta:
                                emitted = True
                                yield f"data: {json.dumps({'text': delta})}\n\n"
                        elif evt_type == "response.output_text.done":
                            text_done = evt.get("text")
                            if not emitted and isinstance(text_done, str) and text_done:
                                emitted = True
                                yield f"data: {json.dumps({'text': text_done})}\n\n"
                            elif not emitted:
                                raise _EnhanceProviderError(
                                    "empty_response",
                                    retryable=True,
                                )
                        elif evt_type == "response.completed":
                            if not emitted:
                                completed_text = _extract_response_text(
                                    evt.get("response") or evt
                                )
                                if completed_text:
                                    emitted = True
                                    yield f"data: {json.dumps({'text': completed_text})}\n\n"
                                else:
                                    raise _EnhanceProviderError(
                                        "empty_response",
                                        retryable=True,
                                    )
                            return
                        elif evt_type in {
                            "response.failed",
                            "response.incomplete",
                            "error",
                        }:
                            raise _EnhanceProviderError(
                                _extract_error_message(evt),
                                retryable=not emitted,
                            )

                if emitted:
                    return
                raise _EnhanceProviderError("empty_response", retryable=True)

    except _EnhanceProviderError:
        raise
    except httpx.TimeoutException:
        raise _EnhanceProviderError("timeout", retryable=True) from None
    except httpx.HTTPError as exc:
        raise _EnhanceProviderError(type(exc).__name__, retryable=True) from exc


async def _stream_enhance(
    text: str,
    providers: list[ProviderDefinition],
    billing: _EnhanceBillingContext | None = None,
) -> AsyncIterator[str]:
    last_error = "upstream_error"
    total_attempts = len(_ENHANCE_ATTEMPTS) * len(providers)
    seen_attempts = 0
    settled = False
    try:
        for attempt in _ENHANCE_ATTEMPTS:
            for provider in providers:
                seen_attempts += 1
                emitted = False
                capture = _EnhanceUsageCapture()
                try:
                    async for chunk in _stream_enhance_one(
                        text,
                        provider,
                        attempt,
                        capture,
                    ):
                        emitted = True
                        yield chunk
                    if billing is not None:
                        try:
                            await _charge_prompt_enhance(billing, capture)
                            settled = True
                        except Exception:
                            logger.exception("prompt enhance billing charge failed")
                            await _release_prompt_enhance_hold(
                                billing,
                                reason="charge_failed",
                            )
                            settled = True
                            yield f"data: {json.dumps({'error': 'billing_failed'})}\n\n"
                            return
                    yield "data: [DONE]\n\n"
                    return
                except _EnhanceProviderError as exc:
                    last_error = "timeout" if str(exc) == "timeout" else "upstream_error"
                    logger.warning(
                        (
                            "enhance provider failed provider=%s attempt=%s "
                            "remaining=%d retryable=%s err=%s"
                        ),
                        provider.name,
                        attempt.name,
                        total_attempts - seen_attempts,
                        exc.retryable,
                        exc,
                    )
                    if emitted or not exc.retryable:
                        await _release_prompt_enhance_hold(
                            billing,
                            reason="provider_error_after_emit"
                            if emitted
                            else "provider_error",
                        )
                        settled = True
                        yield f"data: {json.dumps({'error': last_error})}\n\n"
                        return
                except GeneratorExit:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "enhance provider exception provider=%s attempt=%s",
                        provider.name,
                        attempt.name,
                    )
                    last_error = "internal"
                    if emitted:
                        await _release_prompt_enhance_hold(
                            billing,
                            reason="internal_error_after_emit",
                        )
                        settled = True
                        yield f"data: {json.dumps({'error': last_error})}\n\n"
                        return
        await _release_prompt_enhance_hold(billing, reason="no_success")
        settled = True
        yield f"data: {json.dumps({'error': last_error})}\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        if not settled:
            await _release_prompt_enhance_hold(billing, reason="stream_cancelled")
        raise


@router.post("/enhance")
async def enhance_prompt(
    body: EnhanceIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    await PROMPTS_ENHANCE_LIMITER.check(get_redis(), f"rl:prompt_enhance:{user.id}")
    providers = [p for p in await _resolve_provider_order(db) if p.api_key.strip()]
    if not providers:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "not_configured",
                    "message": "upstream API key not set",
                },
            },
        )
    billing = await _prepare_prompt_enhance_billing(db, user)

    return StreamingResponse(
        _stream_enhance(body.text, providers, billing),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
