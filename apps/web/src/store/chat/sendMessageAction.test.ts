import assert from "node:assert/strict";
import * as nodeModule from "node:module";
import test from "node:test";
import type {
  ChatState,
  ChatStateGetter,
  ChatStateSetter,
} from "./types";
import "./moduleResolution.test-helper.mjs";

// logWarn only reaches console outside production; pin the mode so the warning
// assertions do not depend on the ambient NODE_ENV of the test runner. Each
// test file runs in its own process, so this cannot leak.
(process.env as Record<string, string | undefined>).NODE_ENV = "development";

type ResolveResult = { url: string; shortCircuit?: boolean };
type ResolveHook = (
  specifier: string,
  context: unknown,
  nextResolve: (specifier: string, context: unknown) => ResolveResult,
) => ResolveResult;

// `registerHooks` predates the installed @types/node, so reach it through a
// narrow local signature rather than widening the whole module to any.
const { registerHooks } = nodeModule as unknown as {
  registerHooks: (hooks: { resolve: ResolveHook }) => void;
};

// The action posts through `@/lib/api/conversations`, which would drag in the
// real transport (CSRF handshake, retries, fetch). Redirect that one specifier
// to a stub driven from the test. Hooks run in reverse registration order, so
// registering after the shared `@/` resolver puts this one first.
const stubSource = `
export function createConversation(...args) {
  return globalThis.__conversationsStub.createConversation(...args);
}
export function postMessage(...args) {
  return globalThis.__conversationsStub.postMessage(...args);
}
export function listMessages(...args) {
  return globalThis.__conversationsStub.listMessages?.(...args)
    ?? Promise.resolve({ items: [], next_cursor: null });
}
`;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/lib/api/conversations") {
      return {
        url: `data:text/javascript,${encodeURIComponent(stubSource)}`,
        shortCircuit: true,
      };
    }
    return nextResolve(specifier, context);
  },
});

type PostMessageStub = (
  convId: string,
  body: unknown,
  opts: { signal?: AbortSignal },
) => Promise<unknown>;
type CreateConversationStub = (
  body: unknown,
  opts: { signal?: AbortSignal },
) => Promise<{ id: string }>;

const stubHost = globalThis as typeof globalThis & {
  __conversationsStub?: {
    createConversation?: CreateConversationStub;
    postMessage: PostMessageStub;
    listMessages?: () => Promise<unknown>;
  };
};

const [
  { createSendMessageAction },
  runtime,
  { createComposerState },
  { applySseEventPayload },
  { semanticPostIdempotency },
] =
  await Promise.all([
    import(new URL("./sendMessageAction.ts", import.meta.url).href),
    import(new URL("./runtime.ts", import.meta.url).href),
    import(new URL("./composerSlice.ts", import.meta.url).href),
    import(new URL("./sseEventActions.ts", import.meta.url).href),
    import(
      new URL("../../lib/api/semanticIdempotency.ts", import.meta.url).href
    ),
  ]);

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

async function waitFor(
  predicate: () => boolean,
  message: string,
): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.fail(message);
}

function createHarness(overrides: Partial<ChatState> = {}) {
  let state = {
    currentUserId: "user-1",
    currentConvId: "conv-1",
    messages: [],
    messagesLoading: false,
    messagesError: null,
    generations: {},
    imagesById: {},
    composerError: null,
    composer: { ...createComposerState(null), text: "画一只猫" },
    ...overrides,
  } as unknown as ChatState;
  const set: ChatStateSetter = (partial) => {
    const next = typeof partial === "function" ? partial(state) : partial;
    if (next === state) return;
    state = { ...state, ...next };
  };
  const get: ChatStateGetter = () => state;
  const sendMessage = createSendMessageAction(set, get, {
    createInitialComposer: () => createComposerState(null),
  });
  return { get, set, sendMessage };
}

function backendUserMessage() {
  return {
    id: "real-user-1",
    conversation_id: "conv-1",
    role: "user",
    content: { text: "画一只猫" },
    created_at: "2026-07-26T00:00:00Z",
  };
}

function backendAssistantMessage() {
  return {
    id: "real-asst-1",
    conversation_id: "conv-1",
    role: "assistant",
    content: {},
    status: "pending",
    created_at: "2026-07-26T00:00:00Z",
  };
}

/** An assistant payload whose `content` throws during response validation. */
function explodingAssistantMessage(onRead: () => void) {
  return {
    id: "real-asst-1",
    conversation_id: "conv-1",
    role: "assistant",
    created_at: "2026-07-26T00:00:00Z",
    get content(): Record<string, unknown> {
      onRead();
      throw new TypeError("malformed assistant_message");
    },
  };
}

function resetAliases() {
  runtime._generationIdAliases.clear();
  runtime._completionMessageAliases.clear();
  // A concurrent send's aliases: a rollback must be surgical, not a clear().
  runtime.rememberGenerationAlias("other-real-gen", "opt-gen-other");
  runtime.rememberCompletionAlias("other-real-comp", "opt-asst-other");
}

test("malformed send validation never registers generation aliases", async () => {
  resetAliases();
  const harness = createHarness();
  let optimisticGenerationIds: string[] = [];
  let aliasesAtFailure: [string, string][] = [];
  stubHost.__conversationsStub = {
    postMessage: async () => {
      optimisticGenerationIds = Object.keys(harness.get().generations);
      return {
        user_message: backendUserMessage(),
        generation_ids: ["real-gen-1"],
        assistant_message: explodingAssistantMessage(() => {
          aliasesAtFailure = [...runtime._generationIdAliases].map(
            ([realId, alias]: [string, { optimisticId: string }]) => [
              realId,
              alias.optimisticId,
            ],
          );
        }),
      };
    },
  };

  await harness.sendMessage({ intentOverride: "text_to_image" });

  assert.equal(optimisticGenerationIds.length, 1);
  assert.deepEqual(
    aliasesAtFailure.filter(([realId]) => realId === "real-gen-1"),
    [],
  );
  assert.deepEqual(harness.get().messages, []);
  assert.deepEqual(harness.get().generations, {});
  assert.equal(runtime._generationIdAliases.has("real-gen-1"), false);
  assert.equal(runtime._generationIdAliases.has("other-real-gen"), true);
  assert.match(harness.get().composerError ?? "", /发送失败/);
});

test("malformed send validation never registers completion aliases", async () => {
  resetAliases();
  const harness = createHarness();
  let optimisticAssistantId = "";
  let aliasesAtFailure: [string, string][] = [];
  stubHost.__conversationsStub = {
    postMessage: async () => {
      const messages = harness.get().messages;
      optimisticAssistantId = messages[messages.length - 1]?.id ?? "";
      return {
        user_message: backendUserMessage(),
        completion_id: "comp-1",
        assistant_message: explodingAssistantMessage(() => {
          aliasesAtFailure = [...runtime._completionMessageAliases].map(
            ([realId, alias]: [string, { optimisticMessageId: string }]) => [
              realId,
              alias.optimisticMessageId,
            ],
          );
        }),
      };
    },
  };

  await harness.sendMessage({ intentOverride: "chat" });

  assert.ok(optimisticAssistantId.startsWith("opt-asst-"));
  assert.deepEqual(
    aliasesAtFailure.filter(([realId]) => realId === "comp-1"),
    [],
  );
  assert.deepEqual(harness.get().messages, []);
  assert.equal(runtime._completionMessageAliases.has("comp-1"), false);
  assert.equal(runtime._completionMessageAliases.has("other-real-comp"), true);
});

test("store blocks sends while existing conversation history is unavailable", async () => {
  let posts = 0;
  stubHost.__conversationsStub = {
    postMessage: async () => {
      posts += 1;
      throw new Error("must not post");
    },
  };

  for (const historyState of [
    { messagesLoading: true, messagesError: null },
    { messagesLoading: false, messagesError: "history unavailable" },
  ]) {
    const harness = createHarness(historyState);
    await harness.sendMessage({ intentOverride: "chat" });

    assert.equal(posts, 0);
    assert.deepEqual(harness.get().messages, []);
    assert.equal(harness.get().composer.text, "画一只猫");
    assert.match(harness.get().composerError ?? "", /历史消息/);
  }
});

test("load-more errors do not block sending with loaded messages", async () => {
  let posts = 0;
  const existingMessage = {
    id: "existing-user",
    role: "user",
    text: "Earlier message",
    created_at: 1,
  } as ChatState["messages"][number];
  const harness = createHarness({
    messages: [existingMessage],
    messagesError: "older messages unavailable",
  });
  stubHost.__conversationsStub = {
    postMessage: async () => {
      posts += 1;
      return {
        user_message: backendUserMessage(),
        assistant_message: backendAssistantMessage(),
        completion_id: "comp-1",
      };
    },
  };

  await harness.sendMessage({ intentOverride: "chat" });

  assert.equal(posts, 1);
  assert.equal(harness.get().messages.length, 3);
});

test("response loss keeps the semantic send key for the user's second attempt", async () => {
  await semanticPostIdempotency.clear();
  const accepted = {
    user_message: backendUserMessage(),
    assistant_message: backendAssistantMessage(),
    completion_id: "comp-replayed",
  };
  const acceptedByKey = new Map<string, typeof accepted>();
  const bodies: Array<{ idempotency_key: string; trace_id?: string }> = [];
  const harness = createHarness();
  stubHost.__conversationsStub = {
    postMessage: async (_convId, body) => {
      const request = body as {
        idempotency_key: string;
        trace_id?: string;
      };
      bodies.push(request);
      const prior = acceptedByKey.get(request.idempotency_key);
      if (prior) return prior;
      acceptedByKey.set(request.idempotency_key, accepted);
      throw new TypeError("response lost after backend accepted request");
    },
  };

  await harness.sendMessage({ intentOverride: "chat" });
  assert.match(harness.get().composerError ?? "", /发送失败/);
  assert.equal(harness.get().composer.text, "画一只猫");

  await harness.sendMessage({ intentOverride: "chat" });

  assert.equal(bodies.length, 2);
  assert.equal(bodies[1]?.idempotency_key, bodies[0]?.idempotency_key);
  assert.equal(bodies[0]?.trace_id, bodies[0]?.idempotency_key);
  assert.equal(bodies[1]?.trace_id, bodies[1]?.idempotency_key);
  assert.equal(acceptedByKey.size, 1);
  assert.equal(harness.get().messages.length, 2);
  assert.equal(harness.get().messages[1]?.id, "real-asst-1");
  assert.equal(harness.get().composerError, null);
  await semanticPostIdempotency.clear();
});

test("malformed 2xx reconciliation keeps the semantic send key", async () => {
  await semanticPostIdempotency.clear();
  const keys: string[] = [];
  let calls = 0;
  const harness = createHarness();
  stubHost.__conversationsStub = {
    postMessage: async (_convId, body) => {
      calls += 1;
      keys.push((body as { idempotency_key: string }).idempotency_key);
      if (calls === 1) {
        return {
          user_message: backendUserMessage(),
          completion_id: "comp-malformed",
          assistant_message: explodingAssistantMessage(() => undefined),
        };
      }
      return {
        user_message: backendUserMessage(),
        assistant_message: backendAssistantMessage(),
        completion_id: "comp-reconciled",
      };
    },
  };

  await harness.sendMessage({ intentOverride: "chat" });
  assert.deepEqual(harness.get().messages, []);
  assert.match(harness.get().composerError ?? "", /发送失败/);

  await harness.sendMessage({ intentOverride: "chat" });

  assert.equal(keys.length, 2);
  assert.equal(keys[1], keys[0]);
  assert.equal(harness.get().messages.length, 2);
  assert.equal(harness.get().composerError, null);
  await semanticPostIdempotency.clear();
});

test("text send without completion id retains the semantic key", async () => {
  await semanticPostIdempotency.clear();
  const keys: string[] = [];
  let calls = 0;
  const harness = createHarness();
  stubHost.__conversationsStub = {
    postMessage: async (_convId, body) => {
      calls += 1;
      keys.push((body as { idempotency_key: string }).idempotency_key);
      return {
        user_message: backendUserMessage(),
        assistant_message: backendAssistantMessage(),
        completion_id: calls === 1 ? "   " : "comp-reconciled",
      };
    },
  };

  await harness.sendMessage({ intentOverride: "chat" });
  assert.deepEqual(harness.get().messages, []);
  assert.match(harness.get().composerError ?? "", /发送失败/);

  await harness.sendMessage({ intentOverride: "chat" });

  assert.deepEqual(keys, [keys[0], keys[0]]);
  assert.equal(harness.get().messages.length, 2);
  assert.equal(harness.get().composerError, null);
  await semanticPostIdempotency.clear();
});

test("image send without generation ids retains the semantic key", async () => {
  await semanticPostIdempotency.clear();
  const keys: string[] = [];
  let calls = 0;
  const harness = createHarness();
  stubHost.__conversationsStub = {
    postMessage: async (_convId, body) => {
      calls += 1;
      keys.push((body as { idempotency_key: string }).idempotency_key);
      return {
        user_message: backendUserMessage(),
        assistant_message: backendAssistantMessage(),
        generation_ids: calls === 1 ? ["   "] : ["gen-reconciled"],
      };
    },
  };

  await harness.sendMessage({ intentOverride: "text_to_image" });
  assert.deepEqual(harness.get().messages, []);
  assert.deepEqual(harness.get().generations, {});

  await harness.sendMessage({ intentOverride: "text_to_image" });

  assert.deepEqual(keys, [keys[0], keys[0]]);
  assert.equal(harness.get().messages.length, 2);
  assert.ok(harness.get().generations["gen-reconciled"]);
  await semanticPostIdempotency.clear();
});

test("conversation switch during blocked acquire preserves the new draft", async () => {
  await semanticPostIdempotency.clear();
  const acquireEntered = deferred<void>();
  const releaseAcquire = deferred<void>();
  const idempotency = semanticPostIdempotency as {
    acquire: typeof semanticPostIdempotency.acquire;
  };
  const originalAcquire = idempotency.acquire;
  let posts = 0;
  const harness = createHarness();
  stubHost.__conversationsStub = {
    postMessage: async () => {
      posts += 1;
      throw new Error("stale send must not post");
    },
  };
  idempotency.acquire = async (scope: unknown, payload: unknown) => {
    acquireEntered.resolve();
    await releaseAcquire.promise;
    return originalAcquire.call(semanticPostIdempotency, scope, payload);
  };

  try {
    const pending = harness.sendMessage({ intentOverride: "chat" });
    await acquireEntered.promise;
    runtime._conversationMutationFence.advance();
    runtime.abortAllSendRequests();
    harness.set({
      currentConvId: "conv-2",
      messages: [],
      composer: {
        ...createComposerState(null),
        text: "新会话中未发送的草稿",
      },
    });
    releaseAcquire.resolve();
    await pending;

    assert.equal(posts, 0);
    assert.equal(harness.get().currentConvId, "conv-2");
    assert.equal(harness.get().composer.text, "新会话中未发送的草稿");
    assert.deepEqual(harness.get().messages, []);
    assert.deepEqual(harness.get().generations, {});
  } finally {
    releaseAcquire.resolve();
    idempotency.acquire = originalAcquire;
    await semanticPostIdempotency.clear();
  }
});

test("conversation switch suppresses stale send UI after confirming its valid response", async () => {
  await semanticPostIdempotency.clear();
  const firstResponse = deferred<unknown>();
  const keys: string[] = [];
  let calls = 0;
  const harness = createHarness();
  stubHost.__conversationsStub = {
    postMessage: async (_convId, body) => {
      calls += 1;
      keys.push((body as { idempotency_key: string }).idempotency_key);
      if (calls === 1) return firstResponse.promise;
      return {
        user_message: backendUserMessage(),
        assistant_message: backendAssistantMessage(),
        completion_id: "comp-second",
      };
    },
  };

  const first = harness.sendMessage({ intentOverride: "chat" });
  await waitFor(() => calls === 1, "first send was not dispatched");
  harness.set({
    currentConvId: "conv-2",
    messages: [],
    composer: { ...createComposerState(null), text: "other conversation" },
  });
  firstResponse.resolve({
    user_message: backendUserMessage(),
    assistant_message: backendAssistantMessage(),
    completion_id: "comp-first",
  });
  await first;

  assert.equal(harness.get().currentConvId, "conv-2");
  assert.deepEqual(harness.get().messages, []);

  harness.set({
    currentConvId: "conv-1",
    messages: [],
    composer: { ...createComposerState(null), text: "画一只猫" },
  });
  await harness.sendMessage({ intentOverride: "chat" });

  assert.equal(keys.length, 2);
  assert.notEqual(keys[1], keys[0]);
  assert.equal(harness.get().messages.length, 2);
  await semanticPostIdempotency.clear();
});

test("a truly new conversation can still be created and sent", async () => {
  let creates = 0;
  let postedConversationId = "";
  const harness = createHarness({ currentConvId: null });
  stubHost.__conversationsStub = {
    createConversation: async () => {
      creates += 1;
      return { id: "conv-new" };
    },
    postMessage: async (convId) => {
      postedConversationId = convId;
      return {
        user_message: backendUserMessage(),
        assistant_message: backendAssistantMessage(),
        completion_id: "comp-1",
      };
    },
  };

  await harness.sendMessage({ intentOverride: "chat" });

  assert.equal(creates, 1);
  assert.equal(postedConversationId, "conv-new");
  assert.equal(harness.get().currentConvId, "conv-new");
  assert.equal(harness.get().messages.length, 2);
});

test("completion image arriving before the POST response drains onto the real assistant", async () => {
  runtime.clearUserScopedRuntime();
  const response = deferred<unknown>();
  const harness = createHarness();
  stubHost.__conversationsStub = {
    postMessage: async () => response.promise,
  };

  const pendingSend = harness.sendMessage({ intentOverride: "chat" });
  await Promise.resolve();
  await Promise.resolve();

  applySseEventPayload(
    harness.set,
    harness.get,
    "completion.queued",
    {
      completion_id: "comp-race",
      message_id: "real-asst-1",
    },
    10,
  );
  applySseEventPayload(
    harness.set,
    harness.get,
    "completion.image",
    {
      completion_id: "comp-race",
      message_id: "real-asst-1",
      images: [{ image_id: "image-race", actual_size: "1024x1024" }],
    },
    11,
  );

  response.resolve({
    user_message: backendUserMessage(),
    assistant_message: backendAssistantMessage(),
    completion_id: "comp-race",
  });
  await pendingSend;

  const assistant = harness
    .get()
    .messages.find(
      (message): message is import("../../lib/types").AssistantMessage =>
        message.role === "assistant",
    );
  const generationId = "completion-tool-comp-race";
  assert.deepEqual(assistant?.generation_ids, [generationId]);
  assert.equal(
    harness.get().generations[generationId]?.image?.id,
    "image-race",
  );
});

test("abortAllSendRequests leaves an already-submitted send in flight", async () => {
  runtime.clearUserScopedRuntime();
  const harness = createHarness();
  const response = deferred<unknown>();
  let postSignal: AbortSignal | undefined;
  stubHost.__conversationsStub = {
    postMessage: async (_convId, _body, opts) => {
      postSignal = opts?.signal;
      return response.promise;
    },
  };

  const pendingSend = harness.sendMessage({ intentOverride: "chat" });
  await waitFor(() => postSignal !== undefined, "postMessage was not dispatched");
  // 请求已交给后端 stub（模拟 setCurrentConv 切换会话时的 abortAllSendRequests）。
  assert.ok(postSignal, "postMessage must have been dispatched");
  runtime.abortAllSendRequests();
  // 已提交(可能已计费)的发送不能被 abort：保留在途状态直至自然完成。
  assert.equal(postSignal.aborted, false);

  response.resolve({
    user_message: backendUserMessage(),
    assistant_message: backendAssistantMessage(),
    completion_id: "comp-1",
  });
  await pendingSend;
  assert.equal(harness.get().messages.length, 2);
  assert.equal(harness.get().messages[1]?.id, "real-asst-1");
  assert.equal(harness.get().composerError, null);
});

test("abortAllSendRequests aborts a send that has not reached the backend", async () => {
  runtime.clearUserScopedRuntime();
  const harness = createHarness();
  let posts = 0;
  stubHost.__conversationsStub = {
    postMessage: async () => {
      posts += 1;
      return {
        user_message: backendUserMessage(),
        assistant_message: backendAssistantMessage(),
        completion_id: "comp-1",
      };
    },
  };

  const pendingSend = harness.sendMessage({ intentOverride: "chat" });
  // sendMessage 未执行到 POST（首个 await 在 ensureConversation 处），
  // 此时 abort 无副作用：后端未收到请求，也不会计费。
  runtime.abortAllSendRequests();
  await pendingSend;

  assert.equal(posts, 0);
  assert.deepEqual(harness.get().messages, []);
  assert.equal(harness.get().composer.text, "画一只猫");
  assert.equal(harness.get().composerError, null);
});
