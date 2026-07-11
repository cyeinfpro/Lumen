import assert from "node:assert/strict";
import test from "node:test";
import type {
  BackendGeneration,
  MessageListResponse,
} from "../../lib/apiClient";
import type {
  AssistantMessage,
  Generation,
  GeneratedImage,
  UserMessage,
} from "../../lib/types";
import "./moduleResolution.test-helper.mjs";

const {
  buildMessageListState,
  cloneConversationHistoryCacheEntry,
  makeConversationHistoryCacheEntry,
} = await import(new URL("./history.ts", import.meta.url).href);

function generation(
  overrides: Partial<Generation> = {},
): Generation {
  return {
    id: "generation-1",
    message_id: "assistant-1",
    action: "generate",
    prompt: "draw",
    size_requested: "auto",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status: "succeeded",
    stage: "finalizing",
    attempt: 1,
    started_at: 10,
    finished_at: 20,
    ...overrides,
  };
}

function image(
  overrides: Partial<GeneratedImage> = {},
): GeneratedImage {
  return {
    id: "image-1",
    data_url: "/images/image-1",
    width: 1024,
    height: 1024,
    parent_image_id: null,
    from_generation_id: "generation-1",
    size_requested: "1024x1024",
    size_actual: "1024x1024",
    ...overrides,
  };
}

function userMessage(
  overrides: Partial<UserMessage> = {},
): UserMessage {
  return {
    id: "user-1",
    role: "user",
    text: "draw",
    attachments: [],
    intent: "auto",
    image_params: {
      aspect_ratio: "1:1",
      size_mode: "fixed",
      quality: "1k",
      render_quality: "high",
      count: 1,
    },
    created_at: 1,
    ...overrides,
  };
}

function assistantMessage(
  overrides: Partial<AssistantMessage> = {},
): AssistantMessage {
  return {
    id: "assistant-1",
    role: "assistant",
    parent_user_message_id: "user-1",
    intent_resolved: "text_to_image",
    status: "succeeded",
    generation_ids: ["generation-1"],
    generation_id: "generation-1",
    created_at: 2,
    ...overrides,
  };
}

function backendGeneration(
  overrides: Partial<BackendGeneration> = {},
): BackendGeneration {
  return {
    id: "generation-1",
    message_id: "assistant-1",
    action: "generate",
    prompt: "draw",
    size_requested: "auto",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status: "running",
    progress_stage: "rendering",
    attempt: 1,
    error_code: null,
    error_message: null,
    started_at: "2026-01-01T00:00:00.000Z",
    finished_at: null,
    ...overrides,
  };
}

test("history cache clones isolate nested messages, generations, and images", () => {
  const cachedImage = image({ metadata_jsonb: { trace_id: "trace-1" } });
  const cachedGeneration = generation({ image: cachedImage });
  const entry = makeConversationHistoryCacheEntry(
    [userMessage(), assistantMessage()],
    { "generation-1": cachedGeneration },
    { "image-1": cachedImage },
    "cursor-1",
    true,
    123,
  );
  const cloned = cloneConversationHistoryCacheEntry(entry);

  assert.notEqual(cloned.messages, entry.messages);
  assert.notEqual(cloned.generations, entry.generations);
  assert.notEqual(cloned.imagesById, entry.imagesById);
  assert.equal(cloned.updatedAt, 123);

  (cloned.messages[0] as UserMessage).text = "changed";
  cloned.imagesById["image-1"].metadata_jsonb!.trace_id = "changed";
  assert.equal((entry.messages[0] as UserMessage).text, "draw");
  assert.equal(
    entry.imagesById["image-1"].metadata_jsonb?.trace_id,
    "trace-1",
  );
});

test("history cache only materializes referenced generations and images", () => {
  const referencedImage = image();
  const unrelatedImage = image({
    id: "image-2",
    from_generation_id: "generation-2",
  });
  const entry = makeConversationHistoryCacheEntry(
    [assistantMessage()],
    {
      "generation-1": generation({ image: referencedImage }),
      "generation-2": generation({
        id: "generation-2",
        image: unrelatedImage,
      }),
    },
    {
      "image-1": referencedImage,
      "image-2": unrelatedImage,
    },
    null,
    false,
    456,
  );

  assert.deepEqual(Object.keys(entry.generations), ["generation-1"]);
  assert.deepEqual(Object.keys(entry.imagesById), ["image-1"]);
});

test("history materialization preserves terminal generation identity against stale snapshots", () => {
  const existingGeneration = generation();
  const response: MessageListResponse = {
    items: [
      {
        id: "assistant-1",
        conversation_id: "conversation-1",
        role: "assistant",
        content: {},
        intent: "text_to_image",
        status: "running",
        parent_message_id: "user-1",
        created_at: "2026-01-01T00:00:00.000Z",
      },
    ],
    generations: [backendGeneration()],
  };
  const built = buildMessageListState(
    response,
    { "generation-1": existingGeneration },
    {},
  );

  assert.equal(built.generations["generation-1"], existingGeneration);
  assert.equal(built.materialization.generations[0], existingGeneration);
  assert.equal((built.messages[0] as AssistantMessage).status, "succeeded");
});

test("history materialization rebuilds completion image generations and indexes", () => {
  const response: MessageListResponse = {
    items: [
      {
        id: "user-1",
        conversation_id: "conversation-1",
        role: "user",
        content: {
          text: "make an image",
          attachments: [{ image_id: "upload-1" }],
        },
        created_at: "2026-01-01T00:00:00.000Z",
      },
      {
        id: "assistant-1",
        conversation_id: "conversation-1",
        role: "assistant",
        content: {
          text: "done",
          images: [{ image_id: "image-1" }],
        },
        intent: "chat",
        status: "succeeded",
        parent_message_id: "user-1",
        created_at: "2026-01-01T00:00:01.000Z",
      },
    ],
    completions: [
      {
        id: "completion-1",
        message_id: "assistant-1",
        model: "test",
        input_image_ids: [],
        text: "done",
        tokens_in: 1,
        tokens_out: 1,
        status: "succeeded",
        progress_stage: "finalizing",
        attempt: 1,
        error_code: null,
        error_message: null,
        started_at: "2026-01-01T00:00:00.000Z",
        finished_at: "2026-01-01T00:00:01.000Z",
      },
    ],
    images: [
      {
        id: "image-1",
        source: "generated",
        parent_image_id: null,
        width: 1024,
        height: 1024,
        mime: "image/png",
        blurhash: null,
        url: "/images/image-1",
        metadata_jsonb: { completion_id: "completion-1" },
      },
    ],
  };
  const built = buildMessageListState(response, {}, {});
  const completionGeneration =
    built.generations["completion-tool-completion-1"];

  assert.equal(completionGeneration.image, built.imagesById["image-1"]);
  assert.equal(
    (built.messages[1] as AssistantMessage).generation_id,
    "completion-tool-completion-1",
  );
  assert.deepEqual(built.materialization.imageIds, ["image-1"]);
  assert.deepEqual(built.materialization.completionMessages, [
    { completionId: "completion-1", messageId: "assistant-1" },
  ]);
  assert.equal(
    (built.messages[0] as UserMessage).attachments[0]?.id,
    "upload-1",
  );
});

test("history image merge preserves long local data URLs", () => {
  const dataUrl = `data:image/png;base64,${"a".repeat(1100)}`;
  const existing = image({ data_url: dataUrl });
  const response: MessageListResponse = {
    items: [],
    images: [
      {
        id: "image-1",
        source: "generated",
        parent_image_id: null,
        width: 1024,
        height: 1024,
        mime: "image/png",
        blurhash: null,
        url: "/images/image-1/new",
      },
    ],
  };

  const built = buildMessageListState(response, {}, { "image-1": existing });
  assert.equal(built.imagesById["image-1"].data_url, dataUrl);
});
