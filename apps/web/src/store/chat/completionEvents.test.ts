import assert from "node:assert/strict";
import test from "node:test";
import type {
  AssistantMessage,
  CompletionToolCall,
  Message,
  UserMessage,
} from "../../lib/types";

const {
  applyCompletionEventToMessage,
  applyCompletionLifecycleEvent,
  applyCompletionProgressEvent,
  applyCompletionSucceededEvent,
  completionMessageMatches,
} = await import(
  new URL("./completionEvents.ts", import.meta.url).href
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

function user(): UserMessage {
  return {
    id: "user-1",
    role: "user",
    text: "prompt",
    attachments: [],
    intent: "chat",
    image_params: {
      aspect_ratio: "7:10",
      size_mode: "fixed",
    },
    created_at: 1,
  };
}

function noIds(): undefined {
  return undefined;
}

test("completion matching accepts either message or completion identity", () => {
  const message = assistant();

  assert.equal(
    completionMessageMatches(message, "assistant-1", undefined),
    true,
  );
  assert.equal(
    completionMessageMatches(message, undefined, "completion-1"),
    true,
  );
  assert.equal(
    completionMessageMatches(message, "other", "completion-1"),
    true,
  );
  assert.equal(
    completionMessageMatches(message, "other", "other-completion"),
    false,
  );
  assert.equal(completionMessageMatches(message, undefined, undefined), false);
});

test("completion lifecycle transitions clone messages and retain timestamps", () => {
  const toolCalls: CompletionToolCall[] = [
    {
      id: "call-1",
      type: "tool",
      status: "queued",
      label: "Queued",
    },
  ];
  const original = assistant({ tool_calls: toolCalls });
  const started = applyCompletionLifecycleEvent(
    original,
    "completion.started",
    {},
    noIds,
    10,
  );

  assert.notEqual(started, original);
  assert.equal(started.tool_calls, toolCalls);
  assert.equal(started.status, "streaming");
  assert.equal(started.stream_started_at, 10);
  assert.equal(started.last_delta_at, 10);
  assert.equal(original.status, "pending");
  assert.equal(original.stream_started_at, undefined);

  const startedAgain = applyCompletionLifecycleEvent(
    started,
    "completion.started",
    {},
    noIds,
    20,
  );
  assert.equal(startedAgain.stream_started_at, 10);
  assert.equal(startedAgain.last_delta_at, 10);

  const queued = applyCompletionLifecycleEvent(
    startedAgain,
    "completion.queued",
    {},
    noIds,
    30,
  );
  assert.equal(queued.status, "pending");
  assert.equal(queued.stream_started_at, undefined);
  assert.equal(queued.last_delta_at, undefined);
});

test("completion progress mutates its input and merges singular tool updates", () => {
  const message = assistant({
    stream_started_at: 4,
    tool_calls: [
      {
        id: "call-1",
        type: "web_search",
        status: "running",
        label: "Searching",
        name: "search",
        title: "Existing title",
      },
    ],
  });

  const result = applyCompletionProgressEvent(
    message,
    {
      tool_call: {
        id: "call-1",
        type: "web_search",
        status: "complete",
        label: "Done",
      },
      tool_calls: [
        {
          id: "ignored-because-singular-is-valid",
          status: "running",
        },
      ],
    },
    12,
  );

  assert.equal(result, undefined);
  assert.equal(message.status, "streaming");
  assert.equal(message.stream_started_at, 4);
  assert.equal(message.last_delta_at, 12);
  assert.deepEqual(message.tool_calls, [
    {
      id: "call-1",
      type: "web_search",
      status: "succeeded",
      label: "Done",
      name: "search",
      title: "Existing title",
      error: undefined,
    },
  ]);

  applyCompletionProgressEvent(
    message,
    {
      tool_call: { id: " " },
      tool_calls: [
        {
          id: "call-2",
          type: "",
          status: "timeout",
          label: "",
        },
      ],
    },
    13,
  );
  assert.deepEqual(message.tool_calls, [
    {
      id: "call-2",
      type: "tool",
      status: "timed_out",
      label: "调用工具",
      name: undefined,
      title: undefined,
      error: undefined,
    },
  ]);
});

test("completion success preserves or replaces memory fields by ID presence", () => {
  const preserved = assistant({
    text: "partial",
    used_memory_ids: ["memory-old"],
    used_memory_summary: [
      { id: "memory-old", type: "profile", content: "old" },
    ],
    confirmation_candidate_id: "candidate-old",
  });
  applyCompletionSucceededEvent(
    preserved,
    {
      text: 42,
      used_memory_ids: [],
      used_memory_summary: [
        { id: "memory-new", type: "profile", content: "new" },
      ],
      confirmation_candidate_id: 42,
    },
    20,
  );

  assert.equal(preserved.status, "succeeded");
  assert.equal(preserved.text, "partial");
  assert.deepEqual(preserved.used_memory_ids, ["memory-old"]);
  assert.deepEqual(preserved.used_memory_summary, [
    { id: "memory-old", type: "profile", content: "old" },
  ]);
  assert.equal(preserved.confirmation_candidate_id, "candidate-old");
  assert.equal(preserved.last_delta_at, 20);

  const replaced = assistant({
    used_memory_ids: ["memory-old"],
    used_memory_summary: [
      { id: "memory-old", type: "profile", content: "old" },
    ],
  });
  applyCompletionSucceededEvent(
    replaced,
    {
      text: "complete",
      used_memory_ids: ["memory-new", 1],
      used_memory_summary: [
        { id: "malformed", type: "profile" },
      ],
      confirmation_candidate_id: "",
    },
    21,
  );

  assert.equal(replaced.text, "complete");
  assert.deepEqual(replaced.used_memory_ids, ["memory-new"]);
  assert.deepEqual(replaced.used_memory_summary, []);
  assert.equal(replaced.confirmation_candidate_id, "");
});

test("completion restart clears stream state but preserves memory metadata", () => {
  const original = assistant({
    status: "streaming",
    text: "partial",
    thinking: "thinking",
    tool_calls: [
      {
        id: "call-1",
        type: "tool",
        status: "running",
        label: "Running",
      },
    ],
    used_memory_ids: ["memory-1"],
    used_memory_summary: [
      { id: "memory-1", type: "project", content: "context" },
    ],
    confirmation_candidate_id: "candidate-1",
    stream_started_at: 5,
    last_delta_at: 6,
  });
  const restarted = applyCompletionLifecycleEvent(
    original,
    "completion.restarted",
    {},
    noIds,
    30,
  );

  assert.notEqual(restarted, original);
  assert.equal(restarted.status, "pending");
  assert.equal(restarted.text, "");
  assert.equal(restarted.thinking, "");
  assert.equal(restarted.tool_calls, undefined);
  assert.equal(restarted.stream_started_at, undefined);
  assert.equal(restarted.last_delta_at, undefined);
  assert.equal(restarted.used_memory_ids, original.used_memory_ids);
  assert.equal(restarted.used_memory_summary, original.used_memory_summary);
  assert.equal(restarted.confirmation_candidate_id, "candidate-1");
  assert.equal(original.text, "partial");
});

test("completion failures keep exact custom and fallback strings", () => {
  const custom = applyCompletionLifecycleEvent(
    assistant(),
    "completion.failed",
    {},
    (key: string) =>
      key === "code"
        ? "provider_failed"
        : key === "message"
          ? "上游失败"
          : undefined,
    40,
  );
  assert.equal(custom.status, "failed");
  assert.equal(custom.text, "⚠️ 上游失败（provider_failed）");
  assert.equal(custom.last_delta_at, 40);

  const fallback = applyCompletionLifecycleEvent(
    assistant(),
    "completion.failed",
    {},
    noIds,
    41,
  );
  assert.equal(fallback.text, "⚠️ 文本生成失败（completion_failed）");
});

test("unmatched completion events preserve reference identity", () => {
  const assistantMessage = assistant();
  const userMessage = user();
  const input = {
    messageId: "other-assistant",
    completionId: "other-completion",
    eventName: "completion.started",
    payload: {},
    getId: noIds,
    eventNow: 50,
  };

  assert.equal(
    applyCompletionEventToMessage(assistantMessage, input),
    assistantMessage,
  );
  assert.equal(applyCompletionEventToMessage(userMessage, input), userMessage);

  const matched = applyCompletionEventToMessage(assistantMessage, {
    ...input,
    messageId: undefined,
    completionId: "completion-1",
  }) as AssistantMessage;
  assert.notEqual(matched, assistantMessage);
  assert.equal(matched.status, "streaming");
  assert.equal(assistantMessage.status, "pending");

  const unknownMatched = applyCompletionEventToMessage(assistantMessage, {
    ...input,
    messageId: "assistant-1",
    completionId: undefined,
    eventName: "completion.unknown",
  }) as Message;
  assert.notEqual(unknownMatched, assistantMessage);
  assert.deepEqual(unknownMatched, assistantMessage);
});
