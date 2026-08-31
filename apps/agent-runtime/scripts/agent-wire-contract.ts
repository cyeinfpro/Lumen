import { parseRuntimeRequest } from "../src/contracts.js";
import { splitRuntimeTextDelta } from "../src/runtime.js";

const usage = {
  input_tokens: 1,
  output_tokens: 1,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  cache_write_1h_tokens: 0,
  reasoning_tokens: 0,
  total_tokens: 2,
};

function event(type: string, seq: number, extra: Record<string, unknown> = {}) {
  return {
    version: 1,
    type,
    seq,
    run_id: "wire-run",
    execution_epoch: 1,
    ...extra,
  };
}

function runtimeEvents(): unknown[] {
  return [
    event("run.started", 1, {
      tools: ["lumen_create_image"],
      runtime_version: "pi-0.84.4",
      reasoning_effort: null,
    }),
    event("run.heartbeat", 2),
    event("provider.dispatched", 3, { turn: 1, dispatch_ordinal: 1 }),
    event("provider.response", 4, {
      turn: 1,
      dispatch_ordinal: 1,
      status: 200,
    }),
    event("text.delta", 5, { turn: 1, delta: "wire" }),
    event("text.reset", 6, { turn: 1 }),
    event("turn.completed", 7, {
      turn: 1,
      dispatch_ordinal: 1,
      usage,
      usage_evidence: "exact",
      stop_reason: "stop",
    }),
    event("compaction.completed", 8, {
      checkpoint_version: 2,
      pi_runtime_version: "pi-0.84.4",
      summary: "summary",
      first_kept_message_id: "message-1",
      next_message_id: "message-2",
      phase: "pre_prompt",
      tokens_before: 10,
      provider_call_count: 1,
      usage,
    }),
    event("tool.started", 9, {
      turn: 1,
      tool_call_id: "tool-1",
      ordinal: 0,
      name: "lumen_create_image",
      arguments: { prompt: "wire image" },
    }),
    event("tool.succeeded", 10, {
      turn: 1,
      tool_call_id: "tool-1",
      ordinal: 0,
      name: "lumen_create_image",
      mode: "text_to_image",
      generation_ids: ["generation-1"],
      replayed: false,
      result_text: '{"status":"accepted"}',
    }),
    event("tool.failed", 11, {
      turn: 1,
      tool_call_id: "tool-2",
      ordinal: 1,
      name: "lumen_create_image",
      error_code: "agent_tool_failed",
      result_unknown: false,
    }),
    event("limit.reached", 12, { reason: "tool_calls" }),
    event("run.completed", 13, {
      status: "succeeded",
      error_code: null,
      usage,
      usage_evidence: "exact",
      turn_count: 1,
      tool_call_count: 1,
      provider_dispatch_count: 1,
      provider_completed_count: 1,
    }),
    event("run.failed", 14, {
      status: "failed",
      error_code: "agent_provider_error",
      usage,
      usage_evidence: "unknown",
      turn_count: 1,
      tool_call_count: 1,
      provider_dispatch_count: 2,
      provider_completed_count: 1,
    }),
    event("run.cancelled", 15, {
      status: "cancelled",
      error_code: "agent_cancelled",
      usage,
      usage_evidence: "unknown",
      turn_count: 1,
      tool_call_count: 1,
      provider_dispatch_count: 2,
      provider_completed_count: 1,
    }),
  ];
}

async function main(): Promise<void> {
  if (process.argv[2] === "--emit-large-text") {
    const text = `${"a".repeat(9_000)}${'"\\😀中'.repeat(3_000)}`;
    const chunks = splitRuntimeTextDelta(text, {
      maxLineBytes: 64 * 1024,
      firstSequence: 1,
      runId: "wire-run",
      executionEpoch: 1,
      turn: 1,
    });
    process.stdout.write(`${JSON.stringify({
      text,
      events: chunks.map((delta, index) =>
        event("text.delta", index + 1, { delta, turn: 1 })
      ),
    })}\n`);
    return;
  }
  if (process.argv[2] === "--emit-events") {
    process.stdout.write(`${JSON.stringify(runtimeEvents())}\n`);
    return;
  }
  let raw = "";
  for await (const chunk of process.stdin) raw += String(chunk);
  const values = JSON.parse(raw) as unknown;
  if (!Array.isArray(values)) throw new Error("expected request array");
  const parsed = values.map((value) => parseRuntimeRequest(value).version);
  process.stdout.write(`${JSON.stringify(parsed)}\n`);
}

await main();
