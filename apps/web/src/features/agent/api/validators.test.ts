import assert from "node:assert/strict";
import test from "node:test";
import "../../../store/chat/moduleResolution.test-helper.mjs";

const {
  validateAgentMessages,
  validateAgentRun,
  validateAgentSessionList,
  validateAgentStatus,
} = await import(new URL("./validators.ts", import.meta.url).href);

function validRun() {
  return {
    id: "run-1",
    agent_session_id: "session-1",
    user_message_id: "user-1",
    assistant_message_id: "assistant-1",
    status: "running",
    execution_epoch: 1,
    last_event_seq: 3,
    idempotency_key: "message-key-1",
    model: null,
    reasoning_effort: null,
    turn_count: 1,
    tool_call_count: 0,
    usage: {},
    error_code: null,
    error_message: null,
    started_at: null,
    finished_at: null,
    cancel_requested_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:01Z",
    references: [],
    tool_calls: [],
  };
}

function validTool() {
  return {
    id: "tool-1",
    agent_run_id: "run-1",
    ordinal: 0,
    name: "lumen_web_search",
    mode: "web_search",
    status: "succeeded",
    generation_ids: [],
    generation_count: 0,
    details: {
      kind: "web_search",
      query: "current design trends",
      result_snippets: ["A bounded public summary"],
    },
    duration_ms: 950,
    error_code: null,
    started_at: "2026-01-01T00:00:00Z",
    finished_at: "2026-01-01T00:00:00.950Z",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00.950Z",
  };
}

function validSession() {
  return {
    id: "session-1",
    conversation_id: "conversation-1",
    title: "Agent",
    pinned: false,
    archived: false,
    memory_disabled: false,
    active_scope_id: null,
    default_system: null,
    default_system_prompt_id: null,
    image_defaults: {
      count: 1,
      aspect_ratio: "1:1",
      quality: "2k",
      render_quality: "high",
      background: "auto",
      output_format: "webp",
    },
    allow_image: true,
    allow_web_search: false,
    allow_file_tools: true,
    runtime_version: "0.84.4",
    last_activity_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    active_run: validRun(),
  };
}

test("strict Agent validators preserve complete server payloads", () => {
  const sessions = { items: [validSession()], next_cursor: null };
  const messages = {
    items: [
      {
        id: "assistant-1",
        conversation_id: "conversation-1",
        role: "assistant",
        content: { source: "agent", text: "hello" },
        intent: "agent",
        status: "streaming",
        parent_message_id: "user-1",
        created_at: "2026-01-01T00:00:00Z",
      },
    ],
    runs: [validRun()],
    next_cursor: null,
    generations: [],
    completions: [],
    images: [],
  };
  assert.equal(validateAgentSessionList(sessions), sessions);
  assert.equal(validateAgentMessages(messages), messages);
  assert.deepEqual(validateAgentStatus({ enabled: true, tool_gateway_configured: true }), {
    enabled: true,
    tool_gateway_configured: true,
    default_model: null,
    models: [],
  });
});

test("strict Agent validators reject stale or malformed successful responses", () => {
  const missingSequence = validRun() as Record<string, unknown>;
  delete missingSequence.last_event_seq;
  assert.throws(
    () => validateAgentRun(missingSequence),
    (error: unknown) =>
      error instanceof Error &&
      "code" in error &&
      (error as { code: string }).code === "response_schema_error",
  );
  assert.throws(() =>
    validateAgentSessionList({
      items: [{ ...validSession(), image_defaults: { count: 9 } }],
      next_cursor: null,
    }),
  );
  assert.throws(() =>
    validateAgentStatus({ enabled: "yes", tool_gateway_configured: true }),
  );
});

test("strict Agent validators accept only typed public tool details", () => {
  const withTool = { ...validRun(), tool_call_count: 1, tool_calls: [validTool()] };
  assert.equal(validateAgentRun(withTool), withTool);

  const legacyTool = { ...validTool() } as Record<string, unknown>;
  delete legacyTool.details;
  delete legacyTool.duration_ms;
  const legacyRun = validateAgentRun({
    ...validRun(),
    tool_call_count: 1,
    tool_calls: [legacyTool],
  });
  assert.equal(legacyRun.tool_calls[0].details, null);
  assert.equal(legacyRun.tool_calls[0].duration_ms, null);
  assert.throws(() =>
    validateAgentRun({
      ...validRun(),
      tool_call_count: 1,
      tool_calls: [
        {
          ...validTool(),
          details: {
            ...validTool().details,
            provider_response: { api_key: "must-not-cross-boundary" },
          },
        },
      ],
    }),
  );

  for (const injected of [
    { arguments_jsonb: { authorization: "Bearer private" } },
    { result_jsonb: { provider_response: { api_key: "private" } } },
    { provider_response: { api_key: "private" } },
    { arbitrary_private_field: "private" },
  ]) {
    assert.throws(() =>
      validateAgentRun({
        ...validRun(),
        tool_call_count: 1,
        tool_calls: [{ ...validTool(), ...injected }],
      }),
    );
  }
});
