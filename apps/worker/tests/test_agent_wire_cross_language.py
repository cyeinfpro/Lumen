from __future__ import annotations

import json
from pathlib import Path
import subprocess

from app.agent_runtime_client import (
    AgentRuntimeEvent,
    AgentRuntimeImageDefaults,
    AgentRuntimeProviderEnvelope,
    AgentRuntimeRequest,
    AgentRuntimeToolPolicy,
    _RuntimeEventDecoder,
    runtime_request_body,
)


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "apps/agent-runtime/scripts/agent-wire-contract.ts"
TSX = ROOT / "apps/agent-runtime/node_modules/.bin/tsx"


def _request(version: int) -> AgentRuntimeRequest:
    return AgentRuntimeRequest(
        version=version,  # type: ignore[arg-type]
        run_id=f"wire-run-v{version}",
        agent_session_id="wire-session",
        user_id="wire-user",
        execution_epoch=1,
        user_message_id="wire-user-message",
        assistant_message_id="wire-assistant-message",
        trace_id="0123456789abcdef0123456789abcdef",
        provider=AgentRuntimeProviderEnvelope(
            provider_id="wire-provider",
            api="openai-responses",
            base_url="https://provider.example/v1",
            api_key="secret",
            headers={},
            proxy_url=None,
            resolved_ips=[],
            model="configured-model",
            context_window=128_000,
            max_output_tokens=4_096,
            reasoning_supported=True,
            vision_supported=False,
            thinking_level_map={"off": "none", "high": "high"},
        ),
        system_prompt="Exact Lumen prompt",
        history=[],
        compaction=None,
        current_prompt="hello",
        references=[],
        allowed_tools=[],
        image_defaults=AgentRuntimeImageDefaults(
            count=1,
            aspect_ratio="1:1",
            quality="2k",
            render_quality="high",
            background="auto",
            output_format="webp",
        ),
        tool_gateway_url=None,
        tool_capability=None,
        reasoning_effort=None,
        tool_policy=AgentRuntimeToolPolicy(
            max_image_tool_calls=0,
            max_images_per_run=4,
        ),
        operation="prompt" if version >= 3 else None,
    )


def test_python_runtime_requests_parse_in_typescript_receiver() -> None:
    requests = [
        json.loads(runtime_request_body(_request(version))) for version in (2, 3, 4)
    ]
    completed = subprocess.run(
        [str(TSX), str(SCRIPT)],
        cwd=ROOT,
        input=json.dumps(requests),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert json.loads(completed.stdout) == [2, 3, 4]


def test_typescript_large_text_chunks_fit_python_receiver_contract() -> None:
    completed = subprocess.run(
        [str(TSX), str(SCRIPT), "--emit-large-text"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    request = _request(4).model_copy(update={"run_id": "wire-run"})
    decoder = _RuntimeEventDecoder(request=request, max_line_bytes=64 * 1024)
    raw = b"".join(
        json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for event in payload["events"]
    )
    events = decoder.feed(raw)
    assert "".join(event.delta or "" for event in events) == payload["text"]
    assert all(len(list(event.delta or "")) <= 8_192 for event in events)


def test_typescript_runtime_events_validate_in_python_receiver() -> None:
    completed = subprocess.run(
        [str(TSX), str(SCRIPT), "--emit-events"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    raw_events = json.loads(completed.stdout)
    events = [AgentRuntimeEvent.model_validate(value) for value in raw_events]
    assert {event.type for event in events} == {
        "run.started",
        "run.heartbeat",
        "provider.dispatched",
        "provider.response",
        "text.delta",
        "text.reset",
        "turn.completed",
        "compaction.completed",
        "tool.started",
        "tool.succeeded",
        "tool.failed",
        "limit.reached",
        "run.completed",
        "run.failed",
        "run.cancelled",
    }
