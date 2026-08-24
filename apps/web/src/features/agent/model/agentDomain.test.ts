import assert from "node:assert/strict";
import test from "node:test";
import "../../../store/chat/moduleResolution.test-helper.mjs";

const {
  applyAgentEvent,
} = await import(new URL("./events.ts", import.meta.url).href);
const {
  mergeAgentMessage,
  mergeAgentRun,
  projectAgentGenerations,
  reconcileAgentSnapshot,
} = await import(new URL("./reconciliation.ts", import.meta.url).href);
const { agentRunErrorPresentation } = await import(
  new URL("./errors.ts", import.meta.url).href
);

function run(overrides: Record<string, unknown> = {}) {
  return {
    id: "run-1",
    agent_session_id: "session-1",
    user_message_id: "user-1",
    assistant_message_id: "assistant-1",
    status: "running" as const,
    execution_epoch: 2,
    last_event_seq: 4,
    idempotency_key: "message-key-1",
    model: "model",
    reasoning_effort: null,
    turn_count: 1,
    tool_call_count: 0,
    usage: {},
    error_code: null,
    error_message: null,
    started_at: "2026-01-01T00:00:00Z",
    finished_at: null,
    cancel_requested_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:01Z",
    references: [],
    tool_calls: [],
    ...overrides,
  };
}

function assistant(overrides: Record<string, unknown> = {}) {
  return {
    id: "assistant-1",
    role: "assistant" as const,
    text: "已保留的部分文本",
    status: "running" as const,
    agentRunId: "run-1",
    parentUserMessageId: "user-1",
    generationIds: [],
    toolCalls: [],
    createdAt: "2026-01-01T00:00:00Z",
    partial: false,
    ...overrides,
  };
}

test("Agent event ordering rejects stale epochs and duplicate sequences", () => {
  assert.deepEqual(
    applyAgentEvent(run(), assistant(), {
      agent_session_id: "session-1",
      agent_run_id: "run-1",
      assistant_message_id: "assistant-1",
      execution_epoch: 1,
      event_seq: 99,
      event_name: "agent.output.delta",
      text_delta: "旧",
    }),
    { accepted: false, reason: "stale_epoch" },
  );
  assert.deepEqual(
    applyAgentEvent(run(), assistant(), {
      agent_session_id: "session-1",
      agent_run_id: "run-1",
      assistant_message_id: "assistant-1",
      execution_epoch: 2,
      event_seq: 4,
      event_name: "agent.output.delta",
      text_delta: "重复",
    }),
    { accepted: false, reason: "stale_sequence" },
  );
});

test("accepted deltas append once and terminal Agent runs remain monotonic", () => {
  const accepted = applyAgentEvent(run(), assistant(), {
    agent_session_id: "session-1",
    agent_run_id: "run-1",
    assistant_message_id: "assistant-1",
    execution_epoch: 2,
    event_seq: 5,
    event_name: "agent.output.delta",
    text_delta: " + 新增",
  });
  assert.equal(accepted.accepted, true);
  if (accepted.accepted) {
    assert.equal(accepted.nextMessage.text, "已保留的部分文本 + 新增");
    assert.equal(accepted.nextRun.last_event_seq, 5);
  }
  const terminal = run({ status: "partial", last_event_seq: 8 });
  assert.equal(
    mergeAgentRun(terminal, run({ status: "running", last_event_seq: 9 })).status,
    "partial",
  );
  assert.deepEqual(
    applyAgentEvent(terminal, assistant({ status: "partial", partial: true }), {
      agent_session_id: "session-1",
      agent_run_id: "run-1",
      assistant_message_id: "assistant-1",
      execution_epoch: 2,
      event_seq: 9,
      event_name: "agent.run.failed",
    }),
    { accepted: false, reason: "terminal" },
  );
});

test("Pi recovery resets a truncated draft before regenerated deltas", () => {
  const reset = applyAgentEvent(run(), assistant(), {
    agent_session_id: "session-1",
    agent_run_id: "run-1",
    assistant_message_id: "assistant-1",
    execution_epoch: 2,
    event_seq: 5,
    event_name: "agent.output.reset",
  });
  assert.equal(reset.accepted, true);
  if (!reset.accepted) return;
  assert.equal(reset.nextMessage.text, "");

  const regenerated = applyAgentEvent(reset.nextRun, reset.nextMessage, {
    agent_session_id: "session-1",
    agent_run_id: "run-1",
    assistant_message_id: "assistant-1",
    execution_epoch: 2,
    event_seq: 6,
    event_name: "agent.output.delta",
    text_delta: "完整新答案",
  });
  assert.equal(regenerated.accepted, true);
  if (regenerated.accepted) {
    assert.equal(regenerated.nextMessage.text, "完整新答案");
  }
});

test("snapshot reconciliation preserves longer partial text", () => {
  const merged = mergeAgentMessage(
    assistant({ text: "已保留的部分文本和更多内容", status: "partial", partial: true }),
    assistant({ text: "已保留的部分文本", status: "running" }),
  );
  assert.equal(merged.role, "assistant");
  if (merged.role === "assistant") {
    assert.equal(merged.text, "已保留的部分文本和更多内容");
    assert.equal(merged.status, "partial");
    assert.equal(merged.partial, true);
  }
});

test("a current snapshot keeps Pi text reset authoritative", () => {
  const currentRun = run({ last_event_seq: 5 });
  const reconciled = reconcileAgentSnapshot(
    [assistant({ text: "截断旧稿" })],
    { "run-1": currentRun },
    {
      items: [
        {
          id: "assistant-1",
          role: "assistant",
          content: { text: "", agent_run_id: "run-1" },
          parent_message_id: "user-1",
          status: "running",
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
      runs: [run({ last_event_seq: 6 })],
      generations: [],
      images: [],
    },
    "session-1",
  );

  const message = reconciled.messages[0];
  assert.equal(message?.role, "assistant");
  if (message?.role === "assistant") assert.equal(message.text, "");
});

test("Agent generation projection links images and filters foreign sources", () => {
  const base = {
    id: "generation-1",
    message_id: "assistant-1",
    agent_session_id: "session-1",
    agent_run_id: "run-1",
    action: "generate",
    prompt: "产品图",
    size_requested: "2048x2048",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status: "succeeded" as const,
    progress_stage: "finalizing",
    attempt: 1,
    error_code: null,
    error_message: null,
    started_at: "2026-01-01T00:00:00Z",
    finished_at: "2026-01-01T00:00:03Z",
    source: "agent",
  };
  const projection = projectAgentGenerations(
    [
      base,
      { ...base, id: "foreign", agent_session_id: "session-2" },
    ],
    [
      {
        id: "image-1",
        source: "generated",
        parent_image_id: null,
        owner_generation_id: "generation-1",
        width: 16,
        height: 16,
        mime: "image/png",
        blurhash: null,
        url: "/image-1",
      },
    ],
    "session-1",
  );
  assert.deepEqual(projection.orderedIds, ["generation-1"]);
  assert.equal(projection.byId["generation-1"].image?.id, "image-1");
});

test("stable Agent errors expose safe user actions", () => {
  assert.deepEqual(agentRunErrorPresentation("INSUFFICIENT_BALANCE"), {
    title: "余额不足",
    detail: "充值后可继续运行 Agent。",
    recoverable: false,
    href: "/me/wallet",
    actionLabel: "查看钱包",
  });
  assert.deepEqual(agentRunErrorPresentation("agent_run_timeout"), {
    title: "运行达到时间上限",
    detail: "已保留当前结果，可以继续生成。",
    recoverable: true,
  });
  assert.equal(
    agentRunErrorPresentation("internal-secret-error").detail,
    "当前结果已保留，可以重试或新建会话。",
  );
});
