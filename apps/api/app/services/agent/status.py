"""Public Agent capability and model catalog projection."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.providers_parts.config import parse_provider_json
from lumen_core.runtime_settings import get_spec
from lumen_core.schema_models import AgentModelOptionOut, AgentStatusOut

from ...runtime_settings import get_setting
from ..message_submission_prompting import resolve_task_credential_pin
from .common import byok_vision_supported


def _wallet_model_options(
    providers: Iterable[object],
    default_model: str | None,
) -> list[AgentModelOptionOut]:
    capabilities: dict[str, tuple[bool, bool]] = {}
    for provider in providers:
        if (
            not getattr(provider, "enabled", False)
            or "chat" not in set(getattr(provider, "purposes", ()))
            or (
                getattr(provider, "agent_api", "openai-responses") == "openai-responses"
                and getattr(provider, "responses_supported", None) is False
            )
        ):
            continue
        declared = tuple(getattr(provider, "agent_models", ()))
        models = declared or ((default_model,) if default_model else ())
        for model in models:
            if not isinstance(model, str) or not model.strip():
                continue
            normalized = model.strip()[:128]
            previous = capabilities.get(normalized, (False, False))
            capabilities[normalized] = (
                previous[0] or getattr(provider, "vision_supported", None) is True,
                previous[1]
                or bool(getattr(provider, "agent_reasoning_supported", False)),
            )
    ordered = sorted(
        capabilities,
        key=lambda model: (model != default_model, model.casefold()),
    )
    return [
        AgentModelOptionOut(
            model=model,
            vision_supported=capabilities[model][0],
            reasoning_supported=capabilities[model][1],
        )
        for model in ordered
    ]


async def agent_status_out(
    db: AsyncSession,
    *,
    user: object,
    tool_gateway_configured: bool,
) -> AgentStatusOut:
    if getattr(user, "account_mode", "wallet") == "byok":
        try:
            credential = await resolve_task_credential_pin(
                db,
                str(getattr(user, "id")),
                "chat",
                "byok",
            )
        except HTTPException:
            credential = None
        if credential is None:
            return AgentStatusOut(
                tool_gateway_configured=tool_gateway_configured,
            )
        capabilities = credential.capabilities_jsonb
        return AgentStatusOut(
            tool_gateway_configured=tool_gateway_configured,
            default_model=credential.default_chat_model,
            models=[
                AgentModelOptionOut(
                    model=credential.default_chat_model,
                    vision_supported=byok_vision_supported(capabilities),
                    reasoning_supported=(
                        not isinstance(capabilities, dict)
                        or capabilities.get("agent_reasoning_supported") is not False
                    ),
                )
            ],
        )

    providers_spec = get_spec("providers")
    raw_providers = (
        await get_setting(db, providers_spec) if providers_spec is not None else None
    )
    providers, _errors = parse_provider_json(raw_providers)
    model_spec = get_spec("upstream.default_model")
    raw_default = await get_setting(db, model_spec) if model_spec is not None else None
    default_model = (
        raw_default.strip()[:128]
        if isinstance(raw_default, str) and raw_default.strip()
        else None
    )
    return AgentStatusOut(
        tool_gateway_configured=tool_gateway_configured,
        default_model=default_model,
        models=_wallet_model_options(providers, default_model),
    )


__all__ = ["agent_status_out"]
