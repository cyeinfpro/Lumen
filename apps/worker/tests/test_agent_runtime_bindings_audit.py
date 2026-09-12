"""Keep Worker acceptance aligned with the Node Runtime capability boundary."""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.agent_runtime_client import AgentRuntimeRequest, runtime_request_body


def _payload() -> dict[str, Any]:
    return {
        "version": 5,
        "run_id": "binding-audit",
        "agent_session_id": "session-test",
        "user_id": "user-test",
        "execution_epoch": 1,
        "user_message_id": "message-user",
        "assistant_message_id": "message-assistant",
        "trace_id": "0123456789abcdef0123456789abcdef",
        "provider": {
            "provider_id": "provider-test",
            "api": "openai-responses",
            "base_url": "http://provider.test:8080/v1",
            "api_key": "not-a-real-secret",
            "model": "configured-model",
            "context_window": 128000,
            "max_output_tokens": 4096,
            "reasoning_supported": False,
            "vision_supported": False,
        },
        "system_prompt": "test",
        "history": [],
        "current_prompt": "hello",
        "references": [],
        "allowed_tools": [],
        "image_defaults": {
            "count": 1, "aspect_ratio": "1:1", "quality": "2k",
            "render_quality": "high", "background": "auto", "output_format": "webp",
        },
        "tool_policy": {"max_image_tool_calls": 1, "max_images_per_run": 4},
    }


@pytest.mark.parametrize("url,capability,budget", [
    (True, False, False), (False, True, False), (True, True, False),
    (False, False, True), (True, False, True), (False, True, True),
])
def test_dispatch_binding_is_all_or_none(url: bool, capability: bool, budget: bool) -> None:
    payload = _payload()
    if url:
        payload["provider_dispatch_url"] = "http://api:8000/internal/permit"
    if capability:
        payload["provider_dispatch_capability"] = "capability-test-token-longer-than-32-characters"
    if budget:
        payload["safety_budget"] = {"max_provider_dispatches": 8}
    with pytest.raises(ValidationError, match="dispatch bindings"):
        AgentRuntimeRequest.model_validate(payload)


@pytest.mark.parametrize("field,value", [
    ("tool_gateway_url", "http://api:8000/internal/tool"),
    ("tool_capability", "capability-test-token-longer-than-32-characters"),
])
def test_inactive_image_tool_cannot_keep_half_a_binding(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="image gateway bindings"):
        AgentRuntimeRequest.model_validate({**_payload(), field: value})


@pytest.mark.parametrize("field", [
    "tool_gateway_url", "tool_capability", "provider_dispatch_url", "provider_dispatch_capability",
])
def test_empty_binding_does_not_masquerade_as_absent(field: str) -> None:
    with pytest.raises(ValidationError):
        AgentRuntimeRequest.model_validate({**_payload(), field: ""})


def test_unbound_legacy_and_complete_http_bindings_remain_valid() -> None:
    payload = _payload()
    for version in (2, 3, 4, 5):
        request = AgentRuntimeRequest.model_validate({**payload, "version": version})
        assert runtime_request_body(request)
    request = AgentRuntimeRequest.model_validate({
        **payload,
        "allowed_tools": ["lumen_create_image"],
        "tool_gateway_url": "http://api:8000/internal/tool",
        "tool_capability": "capability-test-token-longer-than-32-characters",
        "provider_dispatch_url": "http://api:8000/internal/permit",
        "provider_dispatch_capability": "capability-test-token-longer-than-32-characters",
        "safety_budget": {"max_provider_dispatches": 8},
    })
    assert b"http://api:8000" in runtime_request_body(request)
