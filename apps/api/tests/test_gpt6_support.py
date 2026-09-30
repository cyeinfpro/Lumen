from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.routes import prompts
from app.routes.admin_models import _known_model_family
from app.routes.prompt_parts.content import VideoEnhanceIn
from app.routes.prompt_parts.upstream import ENHANCE_ATTEMPTS, enhance_attempts_for_model
from app.services.agent.status import _wallet_model_options


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"])
def test_explicit_enhancement_model_pins_request_and_billing(model):
    attempts = enhance_attempts_for_model(model)
    assert len(attempts) == 1
    body = prompts._build_enhance_body("Keep the original subject", attempts[0])
    assert body["model"] == model
    assert body["reasoning"] == {"effort": "low"}
    assert "service_tier" not in body
    assert prompts._prompt_billing_runtime(model).attempts == attempts
    assert body["input"][0]["content"][0]["text"] == "Keep the original subject"


def test_legacy_enhancement_policy_and_idempotency_payload_stay_identical():
    assert enhance_attempts_for_model(None) is ENHANCE_ATTEMPTS
    assert prompts._prompt_billing_runtime().attempts == ENHANCE_ATTEMPTS
    assert prompts._enhance_request_payload(prompts.EnhanceIn(text="original")) == {"text": "original"}
    selected = prompts.EnhanceIn(text="original", enhancement_model="gpt-6-astra")
    assert prompts._enhance_request_payload(selected)["enhancement_model"] == "gpt-6-astra"
    with pytest.raises(ValidationError):
        prompts.EnhanceIn(text="original", enhancement_model="gpt-6-astra-pro")


def test_video_generation_and_enhancement_models_are_independent():
    body = VideoEnhanceIn(text="scene", model="video-model", enhancement_model="gpt-6-astra")
    assert body.model == "video-model"
    assert body.enhancement_model == "gpt-6-astra"
    legacy = VideoEnhanceIn(text="scene", model="video-model")
    assert "enhancement_model" not in prompts._enhance_request_payload(legacy)


@pytest.mark.asyncio
async def test_reserved_billing_forwards_selected_model_without_global_mutation(monkeypatch):
    captured = []

    async def prepare(db, user, **kwargs):
        captured.append(kwargs["runtime"].attempts)
        return None

    async def reserved(*args, runtime):
        return await runtime.prepare_billing(None, None), False

    monkeypatch.setattr(prompts._prompt_billing, "prepare_prompt_enhance_billing", prepare)
    monkeypatch.setattr(prompts._prompt_responses, "prepare_reserved_billing", reserved)
    runtime = prompts.PromptRuntime()
    await prompts._prepare_reserved_billing(runtime=runtime, enhancement_model="gpt-6-astra")
    await prompts._prepare_reserved_billing(runtime=runtime)
    assert [attempt.model for attempt in captured[0]] == ["gpt-6-astra"]
    assert captured[1] == ENHANCE_ATTEMPTS


def test_model_discovery_is_conservative_and_agent_catalog_requires_responses():
    profile = _known_model_family("openai/gpt-6-astra")
    assert profile == (True, True, 272_000, 16_384)
    provider = SimpleNamespace(enabled=True, purposes=("chat",), agent_models=("gpt-6-astra", "custom"),
                               agent_api="openai-completions", vision_supported=True,
                               agent_reasoning_supported=True)
    assert [option.model for option in _wallet_model_options([provider], None)] == ["custom"]
    provider.agent_api = "openai-responses"
    assert {option.model for option in _wallet_model_options([provider], None)} == {"custom", "gpt-6-astra"}
