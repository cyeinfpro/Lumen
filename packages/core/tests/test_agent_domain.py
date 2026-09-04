from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

from lumen_core.agent_capability import (
    AgentCapabilityClaims,
    AgentCapabilityError,
    issue_agent_capability,
    verify_agent_capability,
)
from lumen_core.agent_dispatch import (
    mark_provider_dispatch_authorized,
    provider_dispatch_authorized_count,
    provider_dispatch_evidence_count,
)
from lumen_core.agent_events import (
    AGENT_FIRST_PARTY_TOOLS,
    AGENT_TOOL_CREATE_IMAGE,
    AgentRunStatus,
    agent_channel,
    agent_event_id,
    require_agent_run_transition,
)
from lumen_core.agent_image_tokens import estimate_agent_image_tokens
from lumen_core.agent_history import plan_agent_runtime_context
from lumen_core.message_content import public_message_content
from lumen_core.model_entities import (
    AgentRun,
    AgentCapabilityGrant,
    AgentRunReference,
    AgentSession,
    AgentToolCall,
)
from lumen_core.runtime_settings import get_spec, parse_value
from lumen_core.providers import parse_provider_json
from lumen_core.schema_models import (
    AgentCreateImageArgumentsIn,
    AgentImageDefaultsIn,
    AgentMessageCreateIn,
    AgentToolCreateImageIn,
    agent_message_request_fingerprint,
    agent_tool_semantic_key,
    normalize_create_image_arguments,
    stable_reference_label,
)
from lumen_core.sizing import quality_to_fixed_size


def _claims(**updates: object) -> AgentCapabilityClaims:
    values: dict[str, object] = {
        "capability_id": "capability-123456",
        "nonce": "nonce-1234567890abcdef",
        "run_id": "run-1",
        "user_id": "user-1",
        "agent_session_id": "session-1",
        "execution_epoch": 3,
        "allowed_tools": [AGENT_TOOL_CREATE_IMAGE],
        "allowed_reference_labels": ["ref_1", "ref_2"],
        "issued_at": 1_000,
        "expires_at": 1_120,
    }
    values.update(updates)
    return AgentCapabilityClaims.model_validate(values)


def test_agent_input_contracts_are_strict_and_bounded() -> None:
    body = AgentMessageCreateIn(
        idempotency_key="idem-1",
        text="create a poster",
        attachments=[
            {"image_id": "image-1", "role": "product", "label": "  Product  "}
        ],
    )
    assert body.attachments[0].label == "Product"
    assert body.reasoning_effort is None

    with pytest.raises(ValidationError):
        AgentMessageCreateIn.model_validate(
            {"idempotency_key": "idem-1", "text": "hello", "user_id": "forged"}
        )
    with pytest.raises(ValidationError):
        AgentMessageCreateIn(idempotency_key="idem-1", text="")
    with pytest.raises(ValidationError):
        AgentMessageCreateIn(
            idempotency_key="idem-1",
            text="duplicate",
            attachments=[
                {"image_id": "image-1"},
                {"image_id": "image-1"},
            ],
        )
    sixteen = AgentMessageCreateIn(
        idempotency_key="sixteen-references",
        text="use every reference",
        attachments=[{"image_id": f"image-{index}"} for index in range(16)],
    )
    assert len(sixteen.attachments) == 16
    with pytest.raises(ValidationError):
        AgentMessageCreateIn(
            idempotency_key="seventeen-references",
            text="too many references",
            attachments=[{"image_id": f"image-{index}"} for index in range(17)],
        )
    assert (
        len(
            AgentCreateImageArgumentsIn(
                prompt="use every reference",
                reference_labels=[f"ref_{index}" for index in range(1, 17)],
            ).reference_labels
        )
        == 16
    )

    with pytest.raises(ValidationError):
        AgentToolCreateImageIn.model_validate(
            {
                "pi_tool_call_id": "tool-1",
                "ordinal": 0,
                "execution_epoch": 0,
                "arguments": {"prompt": "image", "callback_url": "https://bad"},
            }
        )


def test_agent_text_files_and_first_party_tool_scope_are_bounded() -> None:
    body = AgentMessageCreateIn(
        idempotency_key="files",
        files=[
            {
                "name": "brief.md",
                "mime_type": "text/markdown",
                "size": 1,
                "content": "# Brief",
            }
        ],
        allow_file_tools=True,
    )
    assert body.files[0].name == "brief.md"
    assert body.files[0].size == 7
    assert set(
        AgentCapabilityClaims.model_validate(
            {
                **_claims().model_dump(),
                "allowed_tools": sorted(AGENT_FIRST_PARTY_TOOLS),
            }
        ).allowed_tools
    ) == set(AGENT_FIRST_PARTY_TOOLS)
    with pytest.raises(ValidationError):
        AgentMessageCreateIn(
            idempotency_key="path",
            files=[
                {
                    "name": "../secret.txt",
                    "mime_type": "text/plain",
                    "size": 1,
                    "content": "x",
                }
            ],
        )
    with pytest.raises(ValidationError):
        AgentMessageCreateIn(
            idempotency_key="disabled",
            files=[
                {
                    "name": "brief.txt",
                    "mime_type": "text/plain",
                    "size": 1,
                    "content": "x",
                }
            ],
            allow_file_tools=False,
        )
    with pytest.raises(ValidationError):
        _claims(allowed_tools=["bash"])


def test_agent_image_token_estimate_is_dimension_and_provider_aware() -> None:
    openai = estimate_agent_image_tokens("openai-responses", 1024, 512)
    anthropic = estimate_agent_image_tokens("anthropic-messages", 1024, 512)
    unknown = estimate_agent_image_tokens("custom", 1024, 512)

    assert openai.policy_version == anthropic.policy_version
    assert openai.upper != anthropic.upper
    assert unknown.upper >= max(openai.upper, anthropic.upper)


def test_agent_context_planner_distinguishes_direct_compaction_and_impossible() -> None:
    direct = plan_agent_runtime_context(
        context_window=128_000,
        max_output_tokens=16_384,
        fixed_input_tokens=10_000,
        history_tokens=50_000,
    )
    compact = plan_agent_runtime_context(
        context_window=128_000,
        max_output_tokens=16_384,
        fixed_input_tokens=15_000,
        history_tokens=100_000,
        largest_history_entry_tokens=40_000,
    )
    impossible = plan_agent_runtime_context(
        context_window=128_000,
        max_output_tokens=16_384,
        fixed_input_tokens=1_000,
        history_tokens=112_000,
    )

    assert direct.mode == "direct"
    assert compact.mode == "compact_before_prompt"
    assert compact.estimated_post_compaction_tokens <= compact.direct_input_limit
    assert impossible.mode == "impossible"
    assert impossible.estimated_input_tokens < compact.estimated_input_tokens


def test_provider_dispatch_evidence_is_monotonic_and_conservative() -> None:
    dispatch = {"provider_dispatch_count": 1}

    mark_provider_dispatch_authorized(dispatch, 2)

    assert provider_dispatch_evidence_count(dispatch) == 2
    assert provider_dispatch_authorized_count(dispatch) == 2
    mark_provider_dispatch_authorized(dispatch, 1)
    assert provider_dispatch_evidence_count(dispatch) == 2
    with pytest.raises(ValueError):
        mark_provider_dispatch_authorized(dispatch, 0)


def test_agent_reference_labels_and_tool_hashes_are_stable() -> None:
    assert [stable_reference_label(index) for index in range(4)] == [
        "ref_1",
        "ref_2",
        "ref_3",
        "ref_4",
    ]
    assert stable_reference_label(15) == "ref_16"
    assert stable_reference_label(63) == "ref_64"
    with pytest.raises(ValueError):
        stable_reference_label(64)

    defaults = AgentImageDefaultsIn(
        count=2,
        aspect_ratio="3:4",
        quality="2k",
        output_format="jpeg",
    )
    normalized = normalize_create_image_arguments(
        AgentCreateImageArgumentsIn(prompt="  Clean product poster  "),
        defaults,
    )
    assert normalized.prompt == "Clean product poster"
    assert normalized.image_params().fixed_size == "1248x1664"
    request_hash, semantic_key = agent_tool_semantic_key("run-1", 0, normalized)
    assert len(request_hash) == len(semantic_key) == 64
    assert agent_tool_semantic_key("run-1", 0, normalized) == (
        request_hash,
        semantic_key,
    )
    changed = normalized.model_copy(update={"count": 3})
    assert agent_tool_semantic_key("run-1", 0, changed) != (
        request_hash,
        semantic_key,
    )

    first = AgentMessageCreateIn(idempotency_key="idem", text="hello")
    second = first.model_copy(update={"text": "different"})
    assert agent_message_request_fingerprint(
        first
    ) != agent_message_request_fingerprint(second)


@pytest.mark.parametrize(
    ("quality", "aspect", "expected"),
    [
        ("1k", "16:9", "1536x864"),
        ("2k", "3:4", "1248x1664"),
        ("4k", "1:1", "2880x2880"),
    ],
)
def test_agent_quality_uses_canonical_core_dimensions(
    quality: str,
    aspect: str,
    expected: str,
) -> None:
    assert quality_to_fixed_size(quality, aspect) == expected  # type: ignore[arg-type]


def test_agent_capability_signing_detects_tamper_expiry_and_future_tokens() -> None:
    secret = "s" * 48
    token = issue_agent_capability(secret, _claims())
    verified = verify_agent_capability(secret, token, now=1_060)
    assert verified.run_id == "run-1"
    assert verified.allowed_reference_labels == ["ref_1", "ref_2"]
    expanded = _claims(
        allowed_reference_labels=[f"ref_{index}" for index in range(1, 65)]
    )
    assert expanded.allowed_reference_labels[-1] == "ref_64"
    with pytest.raises(ValueError):
        _claims(allowed_reference_labels=["ref_65"])

    prefix, payload, signature = token.split(".")
    replacement = "A" if payload[-1] != "A" else "B"
    with pytest.raises(AgentCapabilityError) as tampered:
        verify_agent_capability(
            secret,
            f"{prefix}.{payload[:-1]}{replacement}.{signature}",
            now=1_060,
        )
    assert tampered.value.code == "agent_capability_invalid"

    with pytest.raises(AgentCapabilityError) as expired:
        verify_agent_capability(secret, token, now=1_120)
    assert expired.value.code == "agent_capability_expired"

    future = issue_agent_capability(
        secret,
        _claims(issued_at=2_000, expires_at=2_120),
    )
    with pytest.raises(AgentCapabilityError) as not_yet_valid:
        verify_agent_capability(secret, future, now=1_900)
    assert not_yet_valid.value.code == "agent_capability_not_yet_valid"

    with pytest.raises(AgentCapabilityError) as unconfigured:
        issue_agent_capability("short", _claims())
    assert unconfigured.value.code == "agent_capability_unconfigured"

    with pytest.raises(ValidationError, match="lifetime"):
        _claims(expires_at=87_401)


def test_agent_state_and_event_contracts_fence_terminal_transitions() -> None:
    require_agent_run_transition("queued", "running")
    require_agent_run_transition("running", "partial")
    with pytest.raises(ValueError):
        require_agent_run_transition("succeeded", "running")
    with pytest.raises(ValueError):
        require_agent_run_transition("queued", "succeeded")
    assert AgentRunStatus.CANCELLED.value == "cancelled"
    assert agent_channel("session-1") == "agent:session-1"
    assert agent_event_id("run-1", 3, 7) == "agent:run-1:3:7"
    with pytest.raises(ValueError):
        agent_event_id("run-1", 3, 0)


def test_agent_public_message_projection_is_allowlist_based() -> None:
    content = {
        "text": "submitted",
        "source": "agent",
        "agent_run_id": "run-1",
        "provider_name": "private-provider",
        "capability_token": "private-token",
        "raw_tool_arguments": {"prompt": "private"},
        "tool_calls": [
            {
                "id": "tool-1",
                "name": AGENT_TOOL_CREATE_IMAGE,
                "status": "failed",
                "generation_ids": [],
                "error_code": "agent_provider_unavailable",
                "error_message": "internal host failed",
                "arguments": {"prompt": "private"},
            }
        ],
        "images": [
            {
                "image_id": "image-1",
                "generation_id": "generation-1",
                "storage_key": "private/key.png",
                "signed_url": "https://private.example/token",
            }
        ],
        "generation_ids": ["generation-1", None, 7],
        "blocks": [
            {
                "kind": "tool",
                "turn": 1,
                "tool_call_id": "tool-1",
                "name": AGENT_TOOL_CREATE_IMAGE,
                "status": "succeeded",
                "result_text": '{"api_key":"must-not-be-public"}',
            }
        ],
    }
    projected = public_message_content(content)
    assert projected == {
        "text": "submitted",
        "source": "agent",
        "agent_run_id": "run-1",
        "tool_calls": [
            {
                "id": "tool-1",
                "name": AGENT_TOOL_CREATE_IMAGE,
                "status": "failed",
                "generation_ids": [],
                "error_code": "agent_provider_unavailable",
            }
        ],
        "images": [{"image_id": "image-1", "generation_id": "generation-1"}],
        "generation_ids": ["generation-1"],
        "blocks": [
            {
                "kind": "tool",
                "turn": 1,
                "tool_call_id": "tool-1",
                "name": AGENT_TOOL_CREATE_IMAGE,
                "status": "succeeded",
            }
        ],
    }


def test_agent_orm_declares_all_concurrency_and_state_constraints() -> None:
    assert AgentSession.__table__.c.conversation_id.unique is not True
    run_constraints = {constraint.name for constraint in AgentRun.__table__.constraints}
    tool_constraints = {
        constraint.name for constraint in AgentToolCall.__table__.constraints
    }
    reference_constraints = {
        constraint.name for constraint in AgentRunReference.__table__.constraints
    }
    grant_constraints = {
        constraint.name for constraint in AgentCapabilityGrant.__table__.constraints
    }
    run_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in AgentRun.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    run_indexes = {index.name: index for index in AgentRun.__table__.indexes}

    assert "uq_agent_runs_session_idempotency" in run_constraints
    assert "uq_agent_runs_user_message" in run_constraints
    assert "uq_agent_runs_assistant_message" in run_constraints
    assert "uq_agent_tool_calls_ordinal" in tool_constraints
    assert "uq_agent_tool_calls_semantic" in tool_constraints
    assert "uq_agent_tool_calls_pi_id" in tool_constraints
    assert "uq_agent_run_references_ordinal" in reference_constraints
    assert "uq_agent_run_references_label" in reference_constraints
    assert "uq_agent_capability_grants_nonce" in grant_constraints
    assert "ck_agent_capability_grants_redemptions_bounded" in grant_constraints
    assert (
        "execution_epoch >= 0"
        in run_checks["ck_agent_runs_execution_epoch_nonnegative"]
    )
    active = run_indexes["uq_agent_runs_one_active_session"]
    assert active.unique is True
    assert str(active.dialect_options["postgresql"]["where"]) == (
        "status IN ('queued', 'running')"
    )


def test_agent_runtime_settings_are_closed_and_bounded() -> None:
    expected = {
        "agent.enabled": ("AGENT_ENABLED", "0", "1"),
        "ui.nav.agent_visible": ("UI_NAV_AGENT_VISIBLE", "0", "1"),
        "agent.max_image_tool_calls": ("AGENT_MAX_IMAGE_TOOL_CALLS", "0", "8"),
        "agent.max_images_per_run": ("AGENT_MAX_IMAGES_PER_RUN", "1", "16"),
        "agent.max_web_search_calls": ("AGENT_MAX_WEB_SEARCH_CALLS", "0", "8"),
        "agent.max_file_tool_calls": ("AGENT_MAX_FILE_TOOL_CALLS", "0", "32"),
        "agent.max_tool_calls": ("AGENT_MAX_TOOL_CALLS", "0", "48"),
    }
    for key, (environment, minimum, maximum) in expected.items():
        spec = get_spec(key)
        assert spec is not None
        assert spec.env_fallback == environment
        assert parse_value(spec, minimum) == int(minimum)
        assert parse_value(spec, maximum) == int(maximum)
        with pytest.raises(ValueError):
            parse_value(spec, str(int(maximum) + 1))
    for removed in (
        "agent.max_turns",
        "agent.max_output_tokens",
        "agent.run_timeout_seconds",
        "agent.tool_timeout_seconds",
        "agent.capability_ttl_seconds",
    ):
        assert get_spec(removed) is None


def test_provider_contract_carries_verified_vision_capability() -> None:
    providers, errors = parse_provider_json(
        '[{"name":"vision","base_url":"https://provider.example",'
        '"api_key":"secret","purposes":["chat"],"vision_supported":true,'
        '"agent_api":"anthropic-messages","agent_context_window":200000,'
        '"agent_max_output_tokens":8192,"agent_reasoning_supported":false}]'
    )
    assert errors == []
    assert len(providers) == 1
    assert providers[0].vision_supported is True
    assert providers[0].agent_api == "anthropic-messages"
    assert providers[0].agent_context_window == 200000
    assert providers[0].agent_max_output_tokens == 8192
    assert providers[0].agent_reasoning_supported is False
