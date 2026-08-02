import assert from "node:assert/strict";
import * as nodeModule from "node:module";
import test from "node:test";
import type {
  ChatState,
  ChatStateGetter,
  ChatStateSetter,
} from "./types";
import type { GeneratedImage } from "../../lib/types";
import "./moduleResolution.test-helper.mjs";

type ResolveResult = { url: string; shortCircuit?: boolean };
type ResolveHook = (
  specifier: string,
  context: unknown,
  nextResolve: (specifier: string, context: unknown) => ResolveResult,
) => ResolveResult;

const { registerHooks } = nodeModule as unknown as {
  registerHooks: (hooks: { resolve: ResolveHook }) => void;
};

const conversationsStubSource = `
export function listMessages(...args) {
  return globalThis.__conversationHistoryStub.listMessages(...args);
}
`;
const imagesStubSource = `
export function imageBinaryUrl(id) {
  return "/api/images/" + id + "/binary";
}
export function uploadImage(...args) {
  return globalThis.__conversationHistoryStub.uploadImage(...args);
}
`;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/lib/api/conversations") {
      return {
        url: `data:text/javascript,${encodeURIComponent(conversationsStubSource)}`,
        shortCircuit: true,
      };
    }
    if (specifier === "@/lib/api/images") {
      return {
        url: `data:text/javascript,${encodeURIComponent(imagesStubSource)}`,
        shortCircuit: true,
      };
    }
    return nextResolve(specifier, context);
  },
});

type ListMessagesStub = (
  convId: string,
  options: {
    cursor?: string;
    since?: string;
    include?: string[];
    signal?: AbortSignal;
  },
) => Promise<unknown>;

const stubHost = globalThis as typeof globalThis & {
  __conversationHistoryStub?: {
    listMessages: ListMessagesStub;
    uploadImage: () => Promise<never>;
  };
};

const { createConversationActions } = await import(
  new URL("./conversationActions.ts", import.meta.url).href
);
const { bufferPendingCompletionImage } = await import(
  new URL("./completionImageBuffer.ts", import.meta.url).href
);
const { clearUserScopedRuntime } = await import(
  new URL("./runtime.ts", import.meta.url).href
);

function createHarness(overrides: Partial<ChatState> = {}) {
  let state = {
    currentUserId: "user-1",
    currentConvId: "conv-1",
    messages: [],
    generations: {},
    imagesById: {},
    messagesCursor: null,
    messagesHasMore: false,
    messagesLoading: false,
    messagesError: null,
    ...overrides,
  } as unknown as ChatState;
  const set: ChatStateSetter = (partial) => {
    const next = typeof partial === "function" ? partial(state) : partial;
    if (next === state) return;
    state = { ...state, ...next };
  };
  const get: ChatStateGetter = () => state;
  return {
    get,
    set,
    loadHistoricalMessages: createConversationActions(
      set,
      get,
    ).loadHistoricalMessages,
  };
}

function pendingImage(): GeneratedImage {
  return {
    id: "image-buffered",
    data_url: "/images/image-buffered",
    width: 1024,
    height: 1024,
    parent_image_id: null,
    from_generation_id: "completion-tool-completion-buffered",
    size_requested: "auto",
    size_actual: "1024x1024",
  };
}

test.beforeEach(() => {
  clearUserScopedRuntime();
});

test.after(() => {
  clearUserScopedRuntime();
});

function rejectHistory(message: string) {
  stubHost.__conversationHistoryStub = {
    listMessages: async () => {
      throw new Error(message);
    },
    uploadImage: async () => {
      throw new Error("unused");
    },
  };
}

test("initial history failure with no messages becomes shell-fatal", async () => {
  rejectHistory("history unavailable");
  const harness = createHarness();

  await assert.rejects(
    harness.loadHistoricalMessages("conv-1"),
    /history unavailable/,
  );

  assert.equal(harness.get().messagesLoading, false);
  assert.equal(harness.get().messagesError, "history unavailable");
  assert.deepEqual(harness.get().messages, []);
});

test("load-more failure preserves loaded history and stays page-local", async () => {
  rejectHistory("older messages unavailable");
  const loadedMessages = [
    {
      id: "message-1",
      role: "user",
      text: "Already loaded",
      created_at: 1,
    },
  ] as ChatState["messages"];
  const harness = createHarness({
    messages: loadedMessages,
    messagesCursor: "cursor-1",
    messagesHasMore: true,
  });

  await assert.rejects(
    harness.loadHistoricalMessages("conv-1", true),
    /older messages unavailable/,
  );

  assert.equal(harness.get().messagesLoading, false);
  assert.equal(harness.get().messagesError, null);
  assert.equal(harness.get().messages, loadedMessages);
  assert.equal(harness.get().messagesCursor, "cursor-1");
  assert.equal(harness.get().messagesHasMore, true);
});

test("load-more stall fallback passes newest persisted message id as since", async () => {
  const calls: Array<Record<string, unknown>> = [];
  stubHost.__conversationHistoryStub = {
    listMessages: async (
      convId: string,
      options: { cursor?: string; since?: string; signal?: AbortSignal },
    ) => {
      calls.push({ convId, ...options });
      if (options.cursor) {
        // 停滞：返回全部已加载消息且 next_cursor 不变
        return {
          items: [
            {
              id: "message-1",
              conversation_id: "conv-1",
              role: "user",
              content: { text: "old" },
              intent: "chat",
              status: "succeeded",
              parent_message_id: null,
              created_at: "2026-07-31T00:00:01.000Z",
            },
            {
              id: "message-2",
              conversation_id: "conv-1",
              role: "user",
              content: { text: "newest" },
              intent: "chat",
              status: "succeeded",
              parent_message_id: null,
              created_at: "2026-07-31T00:00:02.000Z",
            },
          ],
          next_cursor: "cursor-1",
        };
      }
      // 回退：since 查询补上新到达的消息
      return {
        items: [
          {
            id: "message-3",
            conversation_id: "conv-1",
            role: "user",
            content: { text: "arrived" },
            intent: "chat",
            status: "succeeded",
            parent_message_id: null,
            created_at: "2026-07-31T00:00:03.000Z",
          },
        ],
        next_cursor: "cursor-3",
      };
    },
    uploadImage: async () => {
      throw new Error("unused");
    },
  };
  const harness = createHarness({
    messages: [
      {
        id: "message-1",
        role: "user",
        text: "old",
        created_at: 1,
        attachments: [],
      },
      {
        id: "message-2",
        role: "user",
        text: "newest",
        created_at: 2,
        attachments: [],
      },
    ] as unknown as ChatState["messages"],
    messagesCursor: "cursor-1",
    messagesHasMore: true,
  });

  await harness.loadHistoricalMessages("conv-1", true);

  assert.equal(calls.length, 2);
  // 回退请求必须携带正确的 since（最新持久消息 id），而不是分页令牌 cursor。
  assert.equal(calls[1].since, "message-2");
  assert.equal(calls[1].cursor, undefined);
  assert.equal(harness.get().messages.length, 3);
  assert.equal(harness.get().messagesCursor, "cursor-3");
  assert.equal(harness.get().messagesHasMore, true);
});

test("load-more stall fallback skips optimistic opt- placeholder ids", async () => {
  stubHost.__conversationHistoryStub = {
    listMessages: async (
      _convId: string,
      options: { cursor?: string; since?: string; signal?: AbortSignal },
    ) => {
      if (options.cursor) {
        // 停滞：返回全部已加载消息且 next_cursor 不变
        return {
          items: [
            {
              id: "message-1",
              conversation_id: "conv-1",
              role: "user",
              content: { text: "old" },
              intent: "chat",
              status: "succeeded",
              parent_message_id: null,
              created_at: "2026-07-31T00:00:01.000Z",
            },
            {
              id: "message-2",
              conversation_id: "conv-1",
              role: "user",
              content: { text: "persisted" },
              intent: "chat",
              status: "succeeded",
              parent_message_id: null,
              created_at: "2026-07-31T00:00:02.000Z",
            },
          ],
          next_cursor: "cursor-1",
        };
      }
      assert.equal(options.since, "message-2");
      return { items: [], next_cursor: null };
    },
    uploadImage: async () => {
      throw new Error("unused");
    },
  };
  const harness = createHarness({
    messages: [
      {
        id: "message-1",
        role: "user",
        text: "old",
        created_at: 1,
        attachments: [],
      },
      {
        id: "message-2",
        role: "user",
        text: "persisted",
        created_at: 2,
        attachments: [],
      },
      {
        id: "opt-3",
        role: "user",
        text: "pending",
        created_at: 3,
        attachments: [],
      },
    ] as unknown as ChatState["messages"],
    messagesCursor: "cursor-1",
    messagesHasMore: true,
  });

  await harness.loadHistoricalMessages("conv-1", true);

  // opt- 占位是本地乐观消息，后端不存在；since 必须回退到前一条持久消息，
  // 否则后端 422 invalid_since。
  assert.equal(harness.get().messages.length, 3);
  assert.equal(harness.get().messagesHasMore, false);
});

test("initial history materialization drains a buffered completion image", async () => {
  bufferPendingCompletionImage({
    userScope: "user:user-1",
    completionId: "completion-buffered",
    rawMessageId: "assistant-buffered",
    image: pendingImage(),
    eventNow: 100,
  });
  stubHost.__conversationHistoryStub = {
    listMessages: async () => ({
      items: [
        {
          id: "assistant-buffered",
          conversation_id: "conv-1",
          role: "assistant",
          content: {},
          intent: "chat",
          status: "streaming",
          parent_message_id: "user-buffered",
          created_at: "2026-07-31T00:00:00.000Z",
        },
      ],
      completions: [
        {
          id: "completion-buffered",
          message_id: "assistant-buffered",
          status: "streaming",
        },
      ],
      generations: [],
      images: [],
      next_cursor: null,
    }),
    uploadImage: async () => {
      throw new Error("unused");
    },
  };
  const harness = createHarness();

  await harness.loadHistoricalMessages("conv-1");

  const generationId = "completion-tool-completion-buffered";
  assert.equal(
    harness.get().generations[generationId]?.image?.id,
    "image-buffered",
  );
  assert.deepEqual(
    (
      harness.get().messages[0] as import("../../lib/types").AssistantMessage
    ).generation_ids,
    [generationId],
  );
});
