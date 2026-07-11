import assert from "node:assert/strict";
import test from "node:test";
import type { BackendCompletion } from "../../lib/apiClient";
import type {
  AssistantMessage,
  Message,
} from "../../lib/types";

const {
  applyCompletionSnapshot,
  mergeMessagesById,
  preferredMessageSnapshot,
  shouldAcceptTaskSnapshot,
} = await import(
  new URL("./messageReconciliation.ts", import.meta.url).href
);

function assistant(
  overrides: Partial<AssistantMessage> = {},
): AssistantMessage {
  return {
    id: "assistant-1",
    role: "assistant",
    parent_user_message_id: "user-1",
    intent_resolved: "chat",
    status: "streaming",
    created_at: 1,
    ...overrides,
  };
}

test("terminal message snapshots do not regress or lose longer text", () => {
  const current = assistant({
    status: "succeeded",
    text: "complete response",
    last_delta_at: 10,
  });

  assert.equal(shouldAcceptTaskSnapshot("succeeded", "running"), false);
  assert.equal(
    preferredMessageSnapshot(
      current,
      assistant({ status: "streaming", text: "partial" }),
    ),
    current,
  );
  assert.deepEqual(
    preferredMessageSnapshot(
      current,
      assistant({ status: "succeeded", text: "short" }),
    ),
    current,
  );
});

test("message merge deduplicates and keeps chronological order", () => {
  const current = assistant({ status: "failed", text: "final error" });
  const earlier = assistant({ id: "assistant-0", created_at: 0 });
  const incoming = assistant({ status: "streaming", text: "retrying" });

  assert.deepEqual(
    mergeMessagesById([current], [incoming, earlier]).map((message: Message) => [
      message.id,
      message.role === "assistant" ? message.status : undefined,
    ]),
    [
      ["assistant-0", "streaming"],
      ["assistant-1", "failed"],
    ],
  );
});

test("completion snapshots preserve terminal state against stale polling", () => {
  const current = assistant({
    completion_id: "completion-1",
    status: "succeeded",
    text: "complete response",
  });
  const stale = {
    status: "running",
    text: "partial",
  } as unknown as BackendCompletion;

  const result = applyCompletionSnapshot(
    [current],
    "completion-1",
    stale,
    20,
  );
  assert.equal(result[0], current);
});

test("completion snapshots promote streaming exactly once", () => {
  const current = assistant({
    completion_id: "completion-1",
    status: "pending",
  });
  const fresh = {
    status: "streaming",
  } as unknown as BackendCompletion;

  const result = applyCompletionSnapshot(
    [current],
    "completion-1",
    fresh,
    20,
  );
  const message = result[0] as AssistantMessage;

  assert.equal(message.status, "streaming");
  assert.equal(message.stream_started_at, 20);
  assert.equal(
    applyCompletionSnapshot(result, "completion-1", fresh, 30),
    result,
  );
});

test("completion snapshots apply terminal status and text together", () => {
  const current = assistant({
    completion_id: "completion-1",
    status: "streaming",
    text: "partial",
  });
  const fresh = {
    status: "succeeded",
    text: "complete response",
  } as unknown as BackendCompletion;

  const result = applyCompletionSnapshot(
    [current],
    "completion-1",
    fresh,
    20,
  );
  const message = result[0] as AssistantMessage;

  assert.equal(message.status, "succeeded");
  assert.equal(message.text, "complete response");
  assert.equal(message.last_delta_at, 20);
});
