import assert from "node:assert/strict";
import test from "node:test";
import type { BackendMessage } from "../../lib/apiClient";
import type {
  AttachmentImage,
  CompletionToolCall,
  ImageParams,
} from "../../lib/types";

const {
  adaptBackendAssistantMessage,
  adaptBackendUserMessage,
  coerceAssistantIntent,
  coerceAssistantStatus,
  coerceCompletionToolCalls,
  coerceMemoryWrites,
  coerceUsedMemorySummary,
  mergeCompletionToolCall,
  normalizeCompletionToolStatus,
  optionalAssistantIntent,
} = await import(
  new URL("./messageAdapters.ts", import.meta.url).href
);

function backendMessage(
  overrides: Partial<BackendMessage> = {},
): BackendMessage {
  return {
    id: "message-1",
    conversation_id: "conversation-1",
    role: "assistant",
    content: {},
    created_at: "2026-07-11T00:00:00Z",
    ...overrides,
  };
}

test("assistant intent and status coercion reject unsupported values", () => {
  assert.equal(coerceAssistantIntent("vision_qa", "chat"), "vision_qa");
  assert.equal(coerceAssistantIntent("auto", "text_to_image"), "text_to_image");
  assert.equal(coerceAssistantIntent("CHAT", "image_to_image"), "image_to_image");
  assert.equal(optionalAssistantIntent("text_to_image"), "text_to_image");
  assert.equal(optionalAssistantIntent("auto"), undefined);
  assert.equal(optionalAssistantIntent(null), undefined);

  assert.equal(coerceAssistantStatus("streaming"), "streaming");
  assert.equal(coerceAssistantStatus("canceled"), "canceled");
  assert.equal(coerceAssistantStatus("running"), "pending");
  assert.equal(coerceAssistantStatus("cancelled"), "pending");

  const generationIds = ["generation-1", "generation-2"];
  const adapted = adaptBackendAssistantMessage(
    backendMessage({
      intent: "auto",
      status: "running",
      parent_message_id: null,
      content: {
        text: "answer",
        thinking: "reasoning",
        used_memory_ids: ["memory-1", 2],
        confirmation_candidate_id: "",
      },
    }),
    "user-fallback",
    "vision_qa",
    generationIds,
    "completion-1",
  );

  assert.equal(adapted.parent_user_message_id, "user-fallback");
  assert.equal(adapted.intent_resolved, "vision_qa");
  assert.equal(adapted.status, "pending");
  assert.equal(adapted.generation_ids, generationIds);
  assert.equal(adapted.generation_id, "generation-1");
  assert.equal(adapted.completion_id, "completion-1");
  assert.deepEqual(adapted.used_memory_ids, ["memory-1"]);
  assert.equal(adapted.confirmation_candidate_id, "");
  assert.equal(adapted.created_at, Date.parse("2026-07-11T00:00:00Z"));
});

test("tool status aliases normalize to the established UI states", () => {
  const aliases = new Map<unknown, string>([
    ["queued", "queued"],
    ["pending", "queued"],
    ["created", "queued"],
    ["running", "running"],
    ["in_progress", "running"],
    ["searching", "running"],
    ["interpreting", "running"],
    ["generating", "running"],
    ["completed", "succeeded"],
    ["complete", "succeeded"],
    ["succeeded", "succeeded"],
    ["success", "succeeded"],
    ["failed", "failed"],
    ["error", "failed"],
    ["incomplete", "failed"],
    ["cancelled", "cancelled"],
    ["canceled", "cancelled"],
    ["timed_out", "timed_out"],
    ["timeout", "timed_out"],
    ["  COMPLETE  ", "succeeded"],
    ["unsupported", "unknown"],
    [null, "unknown"],
  ]);

  for (const [input, expected] of aliases) {
    assert.equal(normalizeCompletionToolStatus(input), expected);
  }
});

test("malformed tool and memory payload entries are dropped or coerced", () => {
  assert.deepEqual(coerceCompletionToolCalls({ id: "call-1" }), []);
  assert.deepEqual(
    coerceCompletionToolCalls([
      null,
      "invalid",
      {},
      { id: "   " },
      {
        id: "  call-1  ",
        type: "",
        status: "mystery",
        label: "",
        name: "   ",
        title: "",
        error: 12,
      },
    ]),
    [
      {
        id: "call-1",
        type: "tool",
        status: "unknown",
        label: "调用工具",
        name: undefined,
        title: "",
        error: undefined,
      },
    ],
  );

  assert.deepEqual(coerceMemoryWrites({ kind: "added" }), []);
  assert.deepEqual(
    coerceMemoryWrites([
      null,
      {},
      { kind: "ignored", content: "drop" },
      {
        kind: "added",
        id: 42,
        type: "unknown",
        content: 42,
        source_excerpt: "source",
        undo_token: false,
        scope_id: "scope-1",
      },
    ]),
    [
      {
        id: null,
        kind: "added",
        type: null,
        content: "",
        source_excerpt: "source",
        undo_token: null,
        scope_id: "scope-1",
        recommended_scope_id: null,
      },
    ],
  );

  assert.deepEqual(
    coerceUsedMemorySummary([
      null,
      { id: "missing-content", type: "profile" },
      { id: 1, type: "profile", content: "drop" },
      { id: "", type: "project", content: "" },
    ]),
    [{ id: "", type: "project", content: "" }],
  );
});

test("tool call merge replaces core fields but preserves absent details", () => {
  const existingCall: CompletionToolCall = {
    id: "call-1",
    type: "web_search",
    status: "running",
    label: "Searching",
    name: "search",
    title: "Existing title",
    error: "Existing error",
  };
  const current = [existingCall];
  const incoming: CompletionToolCall = {
    id: "call-1",
    type: "web_search_result",
    status: "succeeded",
    label: "Done",
    name: undefined,
    title: undefined,
    error: undefined,
  };

  const merged = mergeCompletionToolCall(current, incoming);

  assert.notEqual(merged, current);
  assert.notEqual(merged[0], existingCall);
  assert.deepEqual(merged[0], {
    id: "call-1",
    type: "web_search_result",
    status: "succeeded",
    label: "Done",
    name: "search",
    title: "Existing title",
    error: "Existing error",
  });
  assert.equal(existingCall.status, "running");

  const appendedCall: CompletionToolCall = {
    id: "call-2",
    type: "tool",
    status: "queued",
    label: "Queued",
  };
  const appended = mergeCompletionToolCall(current, appendedCall);
  assert.deepEqual(appended, [existingCall, appendedCall]);
  assert.equal(appended[0], existingCall);
  assert.equal(appended[1], appendedCall);
});

test("backend user adaptation preserves flags, inputs, and timestamps", () => {
  const attachments: AttachmentImage[] = [
    {
      id: "image-1",
      kind: "upload",
      data_url: "data:image/png;base64,AA==",
      mime: "image/png",
    },
  ];
  const params: ImageParams = {
    aspect_ratio: "16:9",
    size_mode: "fixed",
    quality: "2k",
    count: 2,
  };
  const adapted = adaptBackendUserMessage(
    backendMessage({
      role: "user",
      content: {
        text: "prompt",
        web_search: true,
        file_search: 1,
        code_interpreter: true,
        image_generation: "true",
      },
      created_at: "2026-07-11T08:09:10.123Z",
    }),
    attachments,
    params,
    "auto",
  );

  assert.equal(adapted.attachments, attachments);
  assert.equal(adapted.image_params, params);
  assert.equal(adapted.text, "prompt");
  assert.equal(adapted.web_search, true);
  assert.equal(adapted.file_search, false);
  assert.equal(adapted.code_interpreter, true);
  assert.equal(adapted.image_generation, false);
  assert.equal(adapted.created_at, Date.parse("2026-07-11T08:09:10.123Z"));
});
