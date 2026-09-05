import assert from "node:assert/strict";
import test from "node:test";
import type {
  AgentMessage,
  AgentMessageAttachment,
  AgentRun,
} from "../model/contracts";
import "../../../store/chat/moduleResolution.test-helper.mjs";

const {
  acquireAgentSubmissionFence,
  agentMessagePayload,
  agentSubmissionDeliveryIsUncertain,
  createSessionForSubmission,
  reconcileFailedAgentSubmission,
  releaseAgentSubmissionFence,
  stageOptimisticSubmission,
} = await import(new URL("./agentSubmission.ts", import.meta.url).href);

const { ApiError } = await import("../../../lib/api/errors.ts");
const { transitionPrivateIdentity } = await import("../../../lib/auth/privateIdentityEpoch.ts");

test("first-send fence is acquired synchronously before session creation", () => {
  const fence = { current: false };
  assert.equal(acquireAgentSubmissionFence(fence), true);
  assert.equal(acquireAgentSubmissionFence(fence), false);
  releaseAgentSubmissionFence(fence);
  assert.equal(acquireAgentSubmissionFence(fence), true);
});

test("known preflight failures discard the optimistic pair", () => {
  const discarded: Array<{ sessionId: string; runId: string }> = [];
  reconcileFailedAgentSubmission({
    sessionId: "session-1",
    optimistic: { runId: "optimistic:run-1", assistantMessageId: "assistant-1" },
    error: new ApiError({ code: "validation_error", status: 422, message: "known rejection" }),
    discard: (input: { sessionId: string; runId: string }) => discarded.push(input),
    fail: () => assert.fail("known failures must not leave a failed optimistic turn"),
  });
  assert.deepEqual(discarded, [
    { sessionId: "session-1", runId: "optimistic:run-1" },
  ]);
});

test("optimistic attachments wait for server labels and Auto is omitted", () => {
  const draft = {
    text: "use image",
    attachments: [
      {
        imageId: "image-1",
        role: "reference" as const,
        label: "Reference",
        name: "Reference",
        previewUrl: "/image-1",
      },
    ],
    files: [],
    allowImage: true,
    allowWebSearch: false,
    allowFileTools: true,
    reasoningEffort: "auto" as const,
    imageDefaults: {
      count: 1,
      aspect_ratio: "1:1" as const,
      quality: "2k" as const,
      render_quality: "high" as const,
      background: "auto" as const,
      output_format: "webp" as const,
    },
  };
  type OptimisticInput = {
    sessionId: string;
    userMessage: AgentMessage;
    assistantMessage: AgentMessage;
    run: AgentRun;
  };
  let optimisticAttachment: AgentMessageAttachment | undefined;
  stageOptimisticSubmission({
    sessionId: "session-1",
    draft,
    append: (input: OptimisticInput) => {
      if (input.userMessage.role === "user") {
        optimisticAttachment = input.userMessage.attachments[0];
      }
      return { userMessageId: input.userMessage.id, assistantMessageId: input.assistantMessage.id, runId: input.run.id };
    },
    idempotencyKey: "message-key",
  });

  assert.equal(
    "reference_label" in (optimisticAttachment ?? {}),
    false,
  );
  const body = agentMessagePayload(draft, true);
  assert.equal("idempotency_key" in body, false);
  assert.equal("reasoning_effort" in body, false);
  assert.deepEqual(body.files, []);
  assert.equal(body.allow_web_search, false);
  assert.equal(body.allow_file_tools, true);
});

test("uncertain delivery retains the exact attempt with an explicit local marker", () => {
  for (const error of [new Error("lost reply"), ...[0, 200, 408, 425, 429, 504].map((status) =>
    new ApiError({ code: "transport_error", status, message: "lost reply" }))]) {
    assert.equal(agentSubmissionDeliveryIsUncertain(error), true);
    let failed: Record<string, unknown> | undefined;
    reconcileFailedAgentSubmission({
      sessionId: "session-1",
      optimistic: { runId: "optimistic:exact", assistantMessageId: "exact" },
      error,
      discard: () => assert.fail("delivery is not a definitive rejection"),
      fail: (input: Record<string, unknown>) => { failed = input; },
    });
    assert.equal(failed?.runId, "optimistic:exact");
    assert.equal(failed?.errorCode, "agent_submission_uncertain");
  }
});

test("late session creation cannot migrate or clear a new identity's draft", async () => {
  const identity = transitionPrivateIdentity("owner-a");
  const untouched = () => assert.fail("stale result must not mutate client state");
  await assert.rejects(createSessionForSubmission({
    identity,
    draft: { imageDefaults: {}, allowImage: false },
    toolGatewayConfigured: true,
    create: async () => {
      transitionPrivateIdentity("owner-b");
      return { id: "stale-session" };
    },
    upsert: untouched, migrateDraft: untouched, select: untouched, navigate: untouched,
  }), { code: "identity_changed" });
});
