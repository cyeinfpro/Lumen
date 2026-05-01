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

from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    build_effective_provider_config,
    endpoint_kind_allowed,
    resolve_provider_proxy_url,
    weighted_priority_order,
)
from lumen_core.runtime_settings import get_spec

from ..db import get_db
from ..deps import CurrentUser, verify_csrf
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


_ENHANCE_ATTEMPTS = (
    _EnhanceAttempt(name="primary", model="gpt-5.5", reasoning_effort="low"),
    _EnhanceAttempt(name="fallback-gpt-5.4-low", model="gpt-5.4", reasoning_effort="low"),
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
    raw_providers = (
        await get_setting(db, spec_providers) if spec_providers else None
    )
    providers, _proxies, errors = build_effective_provider_config(
        raw_providers=raw_providers,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL")
            or DEFAULT_LEGACY_PROVIDER_BASE_URL
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
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": text}]}
        ],
        "stream": True,
    }
    if attempt.reasoning_effort:
        body["reasoning"] = {"effort": attempt.reasoning_effort}
    if attempt.service_tier:
        body["service_tier"] = attempt.service_tier
    return body


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
                data_lines.append(line[len("data:"):].strip())
        if data_lines:
            payloads.append("\n".join(data_lines))
    return payloads, buffer


async def _stream_enhance_one(
    text: str,
    provider: ProviderDefinition,
    attempt: _EnhanceAttempt,
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
                            yield "data: [DONE]\n\n"
                            return
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

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
                            yield "data: [DONE]\n\n"
                            return
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
                            yield "data: [DONE]\n\n"
                            return
                        elif evt_type in {"response.failed", "response.incomplete", "error"}:
                            raise _EnhanceProviderError(
                                _extract_error_message(evt),
                                retryable=not emitted,
                            )

                if emitted:
                    yield "data: [DONE]\n\n"
                    return
                raise _EnhanceProviderError("empty_response", retryable=True)

    except _EnhanceProviderError:
        raise
    except httpx.TimeoutException:
        raise _EnhanceProviderError("timeout", retryable=True) from None
    except httpx.HTTPError as exc:
        raise _EnhanceProviderError(type(exc).__name__, retryable=True) from exc


async def _stream_enhance(
    text: str, providers: list[ProviderDefinition]
) -> AsyncIterator[str]:
    last_error = "upstream_error"
    total_attempts = len(_ENHANCE_ATTEMPTS) * len(providers)
    seen_attempts = 0
    for attempt in _ENHANCE_ATTEMPTS:
        for provider in providers:
            seen_attempts += 1
            emitted = False
            try:
                async for chunk in _stream_enhance_one(text, provider, attempt):
                    emitted = True
                    yield chunk
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
                    yield f"data: {json.dumps({'error': last_error})}\n\n"
                    return
            except Exception:
                logger.exception(
                    "enhance provider exception provider=%s attempt=%s",
                    provider.name,
                    attempt.name,
                )
                last_error = "internal"
                if emitted:
                    yield f"data: {json.dumps({'error': last_error})}\n\n"
                    return
    yield f"data: {json.dumps({'error': last_error})}\n\n"


@router.post("/enhance")
async def enhance_prompt(
    body: EnhanceIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    await PROMPTS_ENHANCE_LIMITER.check(get_redis(), f"rl:prompt_enhance:{user.id}")
    providers = [p for p in await _resolve_provider_order(db) if p.api_key.strip()]
    if not providers:
        raise HTTPException(status_code=503, detail={
            "error": {"code": "not_configured", "message": "upstream API key not set"},
        })

    return StreamingResponse(
        _stream_enhance(body.text, providers),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
