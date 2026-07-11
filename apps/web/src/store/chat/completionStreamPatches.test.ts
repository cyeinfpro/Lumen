import assert from "node:assert/strict";
import test from "node:test";
import type { AssistantMessage } from "../../lib/types";

const {
  applyCompletionStreamPatches,
  completionStreamPatchKey,
  createCompletionStreamPatch,
  mergeCompletionStreamPatch,
} = await import(
  new URL("./completionStreamPatches.ts", import.meta.url).href
);

function assistant(
  overrides: Partial<AssistantMessage> = {},
): AssistantMessage {
  return {
    id: "assistant-1",
    role: "assistant",
    parent_user_message_id: "user-1",
    intent_resolved: "chat",
    status: "pending",
    completion_id: "completion-1",
    created_at: 1,
    ...overrides,
  };
}

test("completion stream patch keys prefer completion identity", () => {
  assert.equal(
    completionStreamPatchKey("assistant-1", "completion-1"),
    "comp:completion-1",
  );
  assert.equal(completionStreamPatchKey("assistant-1", undefined), "msg:assistant-1");
  assert.equal(completionStreamPatchKey(undefined, undefined), null);
});

test("completion stream patches merge deltas without losing identity", () => {
  const target = createCompletionStreamPatch(
    undefined,
    "completion-1",
    10,
  );
  target.text = "hello";
  const source = createCompletionStreamPatch(
    "assistant-1",
    "completion-1",
    20,
  );
  source.text = " world";
  source.thinking = "thinking";

  mergeCompletionStreamPatch(target, source);

  assert.equal(target.msgId, "assistant-1");
  assert.equal(target.text, "hello world");
  assert.equal(target.thinking, "thinking");
  assert.equal(target.updatedAt, 20);
});

test("completion stream patches update active messages and consume pending deltas", () => {
  const direct = createCompletionStreamPatch(
    "assistant-1",
    "completion-1",
    10,
  );
  direct.text = "hello";
  const pending = createCompletionStreamPatch(
    undefined,
    "completion-1",
    11,
  );
  pending.text = " world";

  const result = applyCompletionStreamPatches(
    [assistant()],
    [["comp:completion-1", direct]],
    new Map([["completion-1", pending]]),
    20,
  );
  const message = result.messages[0] as AssistantMessage;

  assert.equal(message.text, "hello world");
  assert.equal(message.status, "streaming");
  assert.equal(message.stream_started_at, 20);
  assert.deepEqual([...result.appliedPatchKeys], ["comp:completion-1"]);
  assert.deepEqual([...result.appliedPendingCompletionIds], ["completion-1"]);
});

test("terminal messages do not append an already-applied suffix", () => {
  const patch = createCompletionStreamPatch(
    "assistant-1",
    "completion-1",
    10,
  );
  patch.text = "done";

  const result = applyCompletionStreamPatches(
    [assistant({ status: "succeeded", text: "done" })],
    [["comp:completion-1", patch]],
    new Map(),
    20,
  );
  const message = result.messages[0] as AssistantMessage;

  assert.equal(message.text, "done");
  assert.equal(message.status, "succeeded");
});
