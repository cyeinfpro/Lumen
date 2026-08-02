import assert from "node:assert/strict";
import test from "node:test";
import type {
  AssistantMessage,
  Generation,
  GeneratedImage,
} from "../../lib/types";
import type {
  ChatState,
  ChatStateGetter,
  ChatStateSetter,
} from "./types";
import "./moduleResolution.test-helper.mjs";

const { applySseEventPayload } = await import(
  new URL("./sseEventActions.ts", import.meta.url).href
);
const { clearUserScopedRuntime } = await import(
  new URL("./runtime.ts", import.meta.url).href
);
const {
  bufferPendingCompletionImage,
  getPendingCompletionImage,
  pendingCompletionImageCount,
  PENDING_COMPLETION_IMAGE_TTL_MS,
} = await import(new URL("./completionImageBuffer.ts", import.meta.url).href);

type StateHarness = {
  get: ChatStateGetter;
  set: ChatStateSetter;
  state(): ChatState;
};

function createState(
  overrides: Partial<ChatState> = {},
): ChatState {
  return {
    currentUserId: "user-b",
    currentConvId: "conv-b",
    messages: [],
    generations: {},
    imagesById: {},
    ...overrides,
  } as ChatState;
}

function createHarness(initial: ChatState): StateHarness {
  let current = initial;
  return {
    get: () => current,
    set: (partial) => {
      const patch =
        typeof partial === "function" ? partial(current) : partial;
      if (patch === current) return;
      current = { ...current, ...patch };
    },
    state: () => current,
  };
}

function assistant(
  id: string,
  completionId: string,
): AssistantMessage {
  return {
    id,
    role: "assistant",
    parent_user_message_id: `user-message-${id}`,
    intent_resolved: "chat",
    status: "streaming",
    completion_id: completionId,
    created_at: 1,
  };
}

function completionGeneration(
  completionId: string,
  messageId: string,
): Generation {
  return {
    id: `completion-tool-${completionId}`,
    message_id: messageId,
    action: "generate",
    prompt: "",
    size_requested: "auto",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status: "running",
    stage: "rendering",
    attempt: 0,
    started_at: 1,
  };
}

function completionImagePayload(
  completionId: string,
  imageId: string,
  messageId?: string,
): Record<string, unknown> {
  return {
    completion_id: completionId,
    ...(messageId ? { assistant_message_id: messageId } : {}),
    images: [
      {
        image_id: imageId,
        actual_size: "1024x1024",
      },
    ],
  };
}

function bufferedImage(id: string): GeneratedImage {
  return {
    id,
    data_url: `/images/${id}`,
    width: 1024,
    height: 1024,
    parent_image_id: null,
    from_generation_id: `completion-tool-${id}`,
    size_requested: "auto",
    size_actual: "1024x1024",
  };
}

function emitCompletionImage(
  harness: StateHarness,
  payload: Record<string, unknown>,
  now = 100,
): void {
  applySseEventPayload(
    harness.set,
    harness.get,
    "completion.image",
    payload,
    now,
  );
}

test.beforeEach(() => {
  clearUserScopedRuntime();
});

test.after(() => {
  clearUserScopedRuntime();
});

test("completion.image from user A cannot create or bootstrap ownership in user B state", () => {
  const harness = createHarness(createState());
  const payload = completionImagePayload(
    "completion-a",
    "image-a",
    "assistant-a",
  );

  emitCompletionImage(harness, payload);
  emitCompletionImage(harness, payload, 101);

  assert.deepEqual(harness.state().generations, {});
  assert.deepEqual(harness.state().imagesById, {});
  assert.deepEqual(harness.state().messages, []);
});

test("completion.image succeeds when the current user already owns the completion message", () => {
  const message = assistant("assistant-b", "completion-b");
  const harness = createHarness(
    createState({
      messages: [message],
    }),
  );

  emitCompletionImage(
    harness,
    completionImagePayload("completion-b", "image-b"),
  );

  const generationId = "completion-tool-completion-b";
  const state = harness.state();
  assert.equal(state.generations[generationId]?.message_id, "assistant-b");
  assert.equal(state.generations[generationId]?.image?.id, "image-b");
  assert.equal(state.imagesById["image-b"]?.from_generation_id, generationId);
  assert.deepEqual(
    (state.messages[0] as AssistantMessage).generation_ids,
    [generationId],
  );
});

test("completion.image preserves legal out-of-order delivery when an owned generation already exists", () => {
  const generation = completionGeneration(
    "completion-late",
    "assistant-late",
  );
  const harness = createHarness(
    createState({
      generations: { [generation.id]: generation },
    }),
  );

  emitCompletionImage(
    harness,
    completionImagePayload("completion-late", "image-late"),
  );

  const state = harness.state();
  assert.equal(state.messages.length, 0);
  assert.equal(state.generations[generation.id]?.message_id, "assistant-late");
  assert.equal(state.generations[generation.id]?.image?.id, "image-late");
  assert.equal(
    state.imagesById["image-late"]?.from_generation_id,
    generation.id,
  );
});

test("completion.image buffers until the current user message materializes", () => {
  const completionId = "completion-buffered";
  const messageId = "assistant-buffered";
  const harness = createHarness(createState());

  emitCompletionImage(
    harness,
    completionImagePayload(completionId, "image-buffered", messageId),
  );
  assert.deepEqual(harness.state().generations, {});
  assert.deepEqual(harness.state().imagesById, {});

  harness.set({
    messages: [assistant(messageId, completionId)],
  });
  applySseEventPayload(
    harness.set,
    harness.get,
    "completion.started",
    {
      completion_id: completionId,
      message_id: messageId,
    },
    101,
  );

  const generationId = `completion-tool-${completionId}`;
  const state = harness.state();
  assert.equal(state.generations[generationId]?.image?.id, "image-buffered");
  assert.deepEqual(
    (state.messages[0] as AssistantMessage).generation_ids,
    [generationId],
  );
});

test("buffered completion.image still rejects a conflicting raw owner", () => {
  const completionId = "completion-conflict";
  const harness = createHarness(createState());

  emitCompletionImage(
    harness,
    completionImagePayload(completionId, "image-a", "assistant-a"),
  );
  harness.set({
    messages: [assistant("assistant-b", completionId)],
  });
  applySseEventPayload(
    harness.set,
    harness.get,
    "completion.started",
    {
      completion_id: completionId,
      message_id: "assistant-b",
    },
    101,
  );

  assert.deepEqual(harness.state().generations, {});
  assert.deepEqual(harness.state().imagesById, {});
  assert.equal(pendingCompletionImageCount(), 0);
});

test("pending completion images are isolated by user scope and expire", () => {
  bufferPendingCompletionImage(
    {
      userScope: "user:user-a",
      completionId: "completion-shared",
      image: bufferedImage("image-a"),
      eventNow: 1,
    },
    100,
  );
  bufferPendingCompletionImage(
    {
      userScope: "user:user-b",
      completionId: "completion-shared",
      image: bufferedImage("image-b"),
      eventNow: 2,
    },
    200,
  );

  assert.equal(
    getPendingCompletionImage(
      "user:user-a",
      "completion-shared",
      100,
    )?.image.id,
    "image-a",
  );
  assert.equal(
    getPendingCompletionImage(
      "user:user-b",
      "completion-shared",
      200,
    )?.image.id,
    "image-b",
  );
  assert.equal(
    getPendingCompletionImage(
      "user:user-a",
      "completion-shared",
      100 + PENDING_COMPLETION_IMAGE_TTL_MS + 1,
    ),
    undefined,
  );
  assert.equal(
    getPendingCompletionImage(
      "user:user-b",
      "completion-shared",
      100 + PENDING_COMPLETION_IMAGE_TTL_MS + 1,
    )?.image.id,
    "image-b",
  );
});
