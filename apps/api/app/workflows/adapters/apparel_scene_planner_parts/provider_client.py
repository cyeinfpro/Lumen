"""Provider resolution and Responses API transport for scene planning."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    resolve_provider_proxy_url,
)
from lumen_core.vision_tagging import extract_response_text, responses_url

from ...domain.apparel_scene_fallbacks import clean_text
from .contracts import SceneProviderSelection

DIRECTOR_MODEL = "gpt-5.5"
FALLBACK_MODEL = "gpt-5.4"
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
PROVIDER_LIMIT_ENV = "LUMEN_SHOWCASE_GPT_PROVIDER_LIMIT"
CALL_TIMEOUT_ENV = "LUMEN_SHOWCASE_GPT_CALL_TIMEOUT_SEC"
DEFAULT_PROVIDER_LIMIT = 2
DIRECTOR_TIMEOUT_SEC = 150.0
COMPOSER_TIMEOUT_SEC = 75.0
REVIEW_TIMEOUT_SEC = 45.0
DEFAULT_TIMEOUT_SEC = 75.0
ATTEMPT_TIMEOUT_SEC = 70.0
REFERENCE_IMAGE_RETRY_STATUS = frozenset({400, 413, 415, 422})
REFERENCE_IMAGE_RETRY_TOKENS = (
    "input_image",
    "image_url",
    "data url",
    "data_url",
    "base64",
    "image too large",
    "too large",
    "invalid image",
    "invalid_image",
    "unsupported image",
    "unsupported_file",
    "content part",
    "invalid request",
)


class Gpt55CallTimeout(TimeoutError):
    pass


class UpstreamHTTPError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"http {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ProviderResolutionDependencies:
    get_spec: Callable[[str], Any]
    get_setting: Callable[..., Awaitable[Any]]
    build_effective_provider_config: Callable[..., tuple[Any, Any, list[str]]]
    endpoint_kind_allowed: Callable[[ProviderDefinition, str], bool]
    weighted_priority_order: Callable[
        [list[ProviderDefinition], dict[int, int]],
        list[ProviderDefinition],
    ]
    logger: logging.Logger


@dataclass(frozen=True, slots=True)
class ProviderCallDependencies:
    resolve_providers: Callable[
        [AsyncSession, SceneProviderSelection | None],
        Awaitable[list[ProviderDefinition]],
    ]
    call_responses_text: Callable[..., Awaitable[str]]
    extract_json_object: Callable[[str], dict[str, Any]]
    call_timeout_seconds: Callable[[str], float]
    should_retry_without_reference_images: Callable[[Exception], bool]
    should_try_next_attempt: Callable[[Exception], bool]
    logger: logging.Logger


async def resolve_scene_provider_order(
    db: AsyncSession,
    provider_runtime: Any,
    *,
    dependencies: ProviderResolutionDependencies,
) -> list[ProviderDefinition]:
    spec_providers = dependencies.get_spec("providers")
    raw_providers = (
        await dependencies.get_setting(db, spec_providers) if spec_providers else None
    )
    providers, _proxies, errors = dependencies.build_effective_provider_config(
        raw_providers=raw_providers,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    for error in errors:
        dependencies.logger.warning("%s", error)
    providers = [
        provider
        for provider in providers
        if dependencies.endpoint_kind_allowed(provider, "responses")
    ]
    async with provider_runtime.lock:
        return dependencies.weighted_priority_order(
            providers,
            provider_runtime.counters,
        )


async def call_gpt55_json(
    db: AsyncSession,
    *,
    purpose: str,
    instructions: str,
    payload: dict[str, Any],
    max_output_tokens: int,
    provider_selection: SceneProviderSelection | None,
    reference_images: list[dict[str, str]] | None,
    dependencies: ProviderCallDependencies,
) -> dict[str, Any]:
    providers = await dependencies.resolve_providers(db, provider_selection)
    primary_effort = "medium" if purpose == "apparel_scene_director" else "low"
    attempts = (
        {
            "name": "gpt55-priority",
            "model": DIRECTOR_MODEL,
            "reasoning": {"effort": primary_effort},
            "service_tier": "priority",
        },
        {
            "name": "gpt55-standard",
            "model": DIRECTOR_MODEL,
            "reasoning": {"effort": "low"},
            "service_tier": None,
        },
        {
            "name": "gpt54-standard-fallback",
            "model": FALLBACK_MODEL,
            "reasoning": {"effort": "low"},
            "service_tier": None,
        },
    )
    last_error = "unknown"
    call_timeout = dependencies.call_timeout_seconds(purpose)
    deadline = asyncio.get_running_loop().time() + call_timeout
    for provider in providers:
        provider_fatal = False
        reference_image_fallback_reason: str | None = None
        for attempt in attempts:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError(
                    f"{purpose} exceeded {call_timeout:g}s GPT JSON budget; "
                    f"last_error={last_error}"
                )
            attempt_timeout = min(ATTEMPT_TIMEOUT_SEC, remaining)
            attempt_reference_images = (
                None if reference_image_fallback_reason else reference_images
            )
            try:
                text = await call_responses_text_with_timeout(
                    provider=provider,
                    attempt=attempt,
                    purpose=purpose,
                    instructions=instructions,
                    payload=payload,
                    max_output_tokens=max_output_tokens,
                    reference_images=attempt_reference_images,
                    timeout_seconds=attempt_timeout,
                    call_responses_text=dependencies.call_responses_text,
                )
                data = dependencies.extract_json_object(text)
                if isinstance(data, dict):
                    if reference_image_fallback_reason:
                        data.setdefault(
                            "reference_image_fallback_reason",
                            reference_image_fallback_reason[:300],
                        )
                    return data
                raise ValueError("json root is not object")
            except Exception as exc:  # noqa: BLE001
                last_error = f"{provider.name}/{attempt['name']}: {exc}"
                dependencies.logger.info("gpt55 json attempt failed: %s", last_error)
                decision_exc = exc
                if (
                    attempt_reference_images
                    and dependencies.should_retry_without_reference_images(exc)
                ):
                    reference_image_fallback_reason = last_error[:300]
                    try:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise Gpt55CallTimeout(
                                f"timed out after {call_timeout:g}s total budget"
                            )
                        dependencies.logger.info(
                            "gpt55 json retrying without reference images: %s",
                            last_error,
                        )
                        text = await call_responses_text_with_timeout(
                            provider=provider,
                            attempt=attempt,
                            purpose=purpose,
                            instructions=instructions,
                            payload=payload,
                            max_output_tokens=max_output_tokens,
                            reference_images=None,
                            timeout_seconds=min(
                                ATTEMPT_TIMEOUT_SEC,
                                remaining,
                            ),
                            call_responses_text=dependencies.call_responses_text,
                        )
                        data = dependencies.extract_json_object(text)
                        if isinstance(data, dict):
                            data.setdefault(
                                "reference_image_fallback_reason",
                                reference_image_fallback_reason,
                            )
                            return data
                        raise ValueError("json root is not object")
                    except Exception as text_exc:  # noqa: BLE001
                        last_error = (
                            f"{provider.name}/{attempt['name']} text-only: {text_exc}"
                        )
                        dependencies.logger.info(
                            "gpt55 text-only retry failed: %s",
                            last_error,
                        )
                        decision_exc = text_exc
                if not dependencies.should_try_next_attempt(decision_exc):
                    provider_fatal = True
                    break
        if provider_fatal:
            continue
    raise RuntimeError(last_error)


async def resolve_gpt55_providers(
    db: AsyncSession,
    selection: SceneProviderSelection | None,
    *,
    resolve_provider_order: Callable[..., Awaitable[list[ProviderDefinition]]],
    limit_providers: Callable[
        [list[ProviderDefinition]],
        list[ProviderDefinition],
    ],
) -> list[ProviderDefinition]:
    if selection is not None and selection.order is not None:
        providers = list(selection.order)
    elif selection is not None and selection.runtime is not None:
        providers = await resolve_provider_order(db, selection.runtime)
    else:
        raise RuntimeError("scene provider runtime is not configured")
    providers = limit_providers(providers)
    if not providers:
        raise RuntimeError("no responses provider available")
    return providers


def gpt55_provider_limit(logger: logging.Logger) -> int:
    raw_limit = os.environ.get(PROVIDER_LIMIT_ENV)
    if raw_limit:
        try:
            return max(1, min(16, int(raw_limit)))
        except (TypeError, ValueError):
            logger.warning(
                "invalid %s=%r; using default",
                PROVIDER_LIMIT_ENV,
                raw_limit,
            )
    return DEFAULT_PROVIDER_LIMIT


def limit_gpt55_providers(
    providers: list[ProviderDefinition],
    *,
    provider_limit: Callable[[], int],
) -> list[ProviderDefinition]:
    if not providers:
        return providers
    return providers[: min(len(providers), provider_limit())]


def gpt55_call_timeout_seconds(
    purpose: str,
    *,
    logger: logging.Logger,
) -> float:
    raw_timeout = os.environ.get(CALL_TIMEOUT_ENV)
    if raw_timeout:
        try:
            return max(1.0, float(raw_timeout))
        except (TypeError, ValueError):
            logger.warning(
                "invalid %s=%r; using purpose default",
                CALL_TIMEOUT_ENV,
                raw_timeout,
            )
    if purpose == "apparel_scene_director":
        return DIRECTOR_TIMEOUT_SEC
    if purpose == "apparel_prompt_composer":
        return COMPOSER_TIMEOUT_SEC
    if purpose == "apparel_prompt_risk_review":
        return REVIEW_TIMEOUT_SEC
    logger.warning(
        "unknown GPT-5.5 call purpose=%r; using default timeout %gs",
        purpose,
        DEFAULT_TIMEOUT_SEC,
    )
    return DEFAULT_TIMEOUT_SEC


async def call_responses_text_with_timeout(
    *,
    provider: ProviderDefinition,
    attempt: dict[str, Any],
    purpose: str,
    instructions: str,
    payload: dict[str, Any],
    max_output_tokens: int,
    reference_images: list[dict[str, str]] | None,
    timeout_seconds: float,
    call_responses_text: Callable[..., Awaitable[str]],
) -> str:
    try:
        return await asyncio.wait_for(
            call_responses_text(
                provider=provider,
                attempt=attempt,
                purpose=purpose,
                instructions=instructions,
                payload=payload,
                max_output_tokens=max_output_tokens,
                reference_images=reference_images,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise Gpt55CallTimeout(f"timed out after {timeout_seconds:g}s") from exc


async def call_responses_text(
    *,
    provider: ProviderDefinition,
    attempt: dict[str, Any],
    purpose: str,
    instructions: str,
    payload: dict[str, Any],
    max_output_tokens: int,
    reference_images: list[dict[str, str]] | None,
) -> str:
    content: list[dict[str, Any]] = []
    for index, reference in enumerate(reference_images or [], start=1):
        image_url = str(
            reference.get("image_url") or reference.get("url") or ""
        ).strip()
        if not image_url:
            continue
        label = clean_text(reference.get("label"), max_len=80) or f"参考图 {index}"
        content.extend(
            [
                {
                    "type": "input_text",
                    "text": (
                        f"参考图 {index}：{label}。请只用于观察搭配、模特气质、"
                        "比例、姿态和摄影适配，不要在输出中复述图片细节。"
                    ),
                },
                {"type": "input_image", "image_url": image_url},
            ]
        )
    content.append(
        {
            "type": "input_text",
            "text": json.dumps(payload, ensure_ascii=False),
        }
    )
    body: dict[str, Any] = {
        "model": attempt["model"],
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "stream": False,
        "store": False,
        "max_output_tokens": max_output_tokens,
        "metadata": {"purpose": purpose},
    }
    if attempt.get("reasoning"):
        body["reasoning"] = attempt["reasoning"]
    if attempt.get("service_tier"):
        body["service_tier"] = attempt["service_tier"]

    proxy_url = await resolve_provider_proxy_url(provider.proxy)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=8.0, read=70.0, write=30.0, pool=8.0),
        proxy=proxy_url,
    ) as client:
        response = await client.post(
            responses_url(provider.base_url),
            json=body,
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            },
        )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise UpstreamHTTPError(response.status_code, detail)
    try:
        response_payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("upstream returned invalid JSON") from exc
    text = extract_response_text(response_payload)
    if not text:
        raise ValueError("upstream returned empty text")
    return text


def should_retry_without_reference_images(exc: Exception) -> bool:
    if not isinstance(exc, UpstreamHTTPError):
        return False
    if exc.status_code not in REFERENCE_IMAGE_RETRY_STATUS:
        return False
    detail = str(getattr(exc, "detail", "") or exc).lower()
    return any(token in detail for token in REFERENCE_IMAGE_RETRY_TOKENS)


def should_try_next_attempt(exc: Exception) -> bool:
    if isinstance(exc, UpstreamHTTPError):
        if exc.status_code in RETRYABLE_STATUS:
            return True
        if 400 <= exc.status_code < 500:
            return False
        return True
    return True
