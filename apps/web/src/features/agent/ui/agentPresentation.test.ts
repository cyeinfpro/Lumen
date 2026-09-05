import assert from "node:assert/strict";
import test from "node:test";
import type { AgentDraft, AgentMessage, AgentRun } from "../model/contracts.ts";
import { agentDraftSummary, agentEstimateLabel, agentRunPresentation, hasAgentSubmissionUncertain } from "./agentPresentation.ts";

function run(patch: Partial<AgentRun> = {}): AgentRun {
  return {
    id: "run-1", agent_session_id: "session-1", user_message_id: "user-1",
    assistant_message_id: "assistant-1", status: "running", execution_epoch: 1,
    last_event_seq: 1, idempotency_key: "request-1", model: null, reasoning_effort: null,
    turn_count: 1, tool_call_count: 0, usage: {}, error_code: null, error_message: null,
    started_at: null, finished_at: null, cancel_requested_at: null, created_at: "", updated_at: "",
    references: [], tool_calls: [], ...patch,
  };
}

test("Agent run presentation distinguishes submission, accepted queue, execution and requested stop", () => {
  assert.equal(agentRunPresentation(run({ id: "optimistic:assistant-1", status: "queued" })).kind, "submitting");
  assert.equal(agentRunPresentation(run({ status: "queued" })).kind, "queued");
  assert.equal(agentRunPresentation(run()).kind, "running");
  for (const status of ["queued", "running"] as const) {
    assert.equal(agentRunPresentation(run({ status, cancel_requested_at: "2026-09-05" })).kind, "stopping");
  }
});

test("a cancel request does not overwrite the server's final result", () => {
  for (const status of ["succeeded", "partial", "failed", "cancelled"] as const) {
    assert.equal(agentRunPresentation(run({ status, cancel_requested_at: "2026-09-05" })).kind, status);
  }
});

test("unknown submission uses only the explicit operation code, never free-text errors", () => {
  assert.deepEqual(agentRunPresentation(run({ status: "failed", error_code: "agent_submission_uncertain" })), {
    kind: "uncertain", label: "提交待确认",
  });
  assert.equal(agentRunPresentation(run({ status: "failed", error_message: "timeout; agent_submission_uncertain" })).kind, "failed");
  assert.equal(agentRunPresentation(run({ status: "failed", error_code: "request_timeout" })).kind, "failed");
});

test("the current conversation, not an unrelated run, owns its uncertainty banner", () => {
  const message = { id: "assistant-1", role: "assistant", agentRunId: "run-1" } as AgentMessage;
  const runs = { "run-1": run({ status: "failed", error_code: "agent_submission_uncertain" }) };
  assert.equal(hasAgentSubmissionUncertain([message], runs), true);
  assert.equal(hasAgentSubmissionUncertain([], runs), false);
  assert.equal(hasAgentSubmissionUncertain([message], {}), false);
  assert.equal(hasAgentSubmissionUncertain([message], { "run-1": run({ status: "succeeded" }) }), false);
});

test("parameter summary derives directly from the draft and exposes unavailable capability without mutating it", () => {
  const draft: AgentDraft = {
    text: "draft", attachments: [], files: [], allowWebSearch: true, allowFileTools: false,
    allowImage: true, model: null, reasoningEffort: "auto",
    imageDefaults: { count: 3, aspect_ratio: "4:5", quality: "4k", render_quality: "medium", background: "transparent", output_format: "webp" },
  };
  const original = structuredClone(draft);
  assert.equal(agentDraftSummary(draft, true), "联网 · 3 张 · 4:5 · 4K");
  assert.equal(agentDraftSummary(draft, false), "联网 · 仅文本 · 生图不可用");
  assert.deepEqual(draft, original);
});

test("estimate wording never claims reservation/settlement or converts billing units", () => {
  assert.equal(agentEstimateLabel("预计扣 ¥1.20"), "预计 ¥1.20");
  assert.equal(agentEstimateLabel("按实际 token 计费"), "按实际 token 计费");
  assert.equal(agentEstimateLabel("费用暂不可用"), "费用暂不可用");
  assert.equal(agentEstimateLabel(null), null);
});
