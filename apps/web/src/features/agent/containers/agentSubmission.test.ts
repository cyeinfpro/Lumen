import assert from "node:assert/strict";
import test from "node:test";
import "../../../store/chat/moduleResolution.test-helper.mjs";

const {
  acquireAgentSubmissionFence,
  reconcileFailedAgentSubmission,
  releaseAgentSubmissionFence,
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
