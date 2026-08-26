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
  agentMessageBody,
  reconcileFailedAgentSubmission,
  releaseAgentSubmissionFence,
  stageOptimisticSubmission,
} = await import(new URL("./agentSubmission.ts", import.meta.url).href);

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
    optimistic: { runId: "optimistic:run-1" },
    error: new Error("known rejection"),
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
    allowImage: true,
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
    },
    idempotencyKey: "message-key",
  });

  assert.equal(
    "reference_label" in (optimisticAttachment ?? {}),
    false,
  );
  assert.equal(
    "reasoning_effort" in agentMessageBody(draft, true, "message-key"),
    false,
  );
});
