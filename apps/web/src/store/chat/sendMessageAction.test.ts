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
] =
  await Promise.all([
    import(new URL("./sendMessageAction.ts", import.meta.url).href),
    import(new URL("./runtime.ts", import.meta.url).href),
    import(new URL("./composerSlice.ts", import.meta.url).href),
    import(new URL("./sseEventActions.ts", import.meta.url).href),
  ]);

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
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

/**
 * An assistant payload whose `content` throws on first read — the shape a
 * malformed/partial backend response has once `adaptBackendAssistantMessage`
 * touches it. `onRead` runs after `registerResponseAliases`, so it can prove
 * the aliases really existed at the moment reconciliation blew up.
 */
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

test("rollback drops the generation aliases the failed send registered", async () => {
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

  // Non-vacuity: the alias existed and pointed at the optimistic row.
  assert.equal(optimisticGenerationIds.length, 1);
  assert.deepEqual(
    aliasesAtFailure.filter(([realId]) => realId === "real-gen-1"),
    [["real-gen-1", optimisticGenerationIds[0]]],
  );
  // The optimistic rows are gone, so an alias onto them would strand every
  // later SSE generation event on a message that no longer exists.
  assert.deepEqual(harness.get().messages, []);
  assert.deepEqual(harness.get().generations, {});
  assert.equal(runtime._generationIdAliases.has("real-gen-1"), false);
  assert.equal(runtime._generationIdAliases.has("other-real-gen"), true);
  assert.match(harness.get().composerError ?? "", /发送失败/);
});

test("rollback drops the completion alias the failed send registered", async () => {
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
    [["comp-1", optimisticAssistantId]],
  );
  assert.deepEqual(harness.get().messages, []);
  assert.equal(runtime._completionMessageAliases.has("comp-1"), false);
  assert.equal(runtime._completionMessageAliases.has("other-real-comp"), true);
});

async function sendAndCaptureWarnings(
  completionId: string | null,
): Promise<string[]> {
  const harness = createHarness();
  stubHost.__conversationsStub = {
    postMessage: async () => ({
      user_message: backendUserMessage(),
      assistant_message: backendAssistantMessage(),
      completion_id: completionId,
    }),
  };
  const warnings: string[] = [];
  const originalWarn = console.warn;
  console.warn = (...args: unknown[]) => {
    warnings.push(args.map((arg) => JSON.stringify(arg) ?? "").join(" "));
  };
  try {
    await harness.sendMessage({ intentOverride: "chat" });
  } finally {
    console.warn = originalWarn;
  }
  assert.equal(harness.get().messages.length, 2);
  return warnings;
}

test("a chat send without a completion id is reported, not silently accepted", async () => {
  const missing = await sendAndCaptureWarnings(null);
  const reported = missing.filter((line) =>
    /chat send returned no completion id/.test(line),
  );
  // Without an id `rememberCompletionMessage` no-ops: streaming deltas can
  // never resolve a message, so the bubble stays blank while the send looks
  // successful. The break has to surface somewhere.
  assert.equal(reported.length, 1);
  assert.match(reported[0] ?? "", /real-asst-1/);

  const healthy = await sendAndCaptureWarnings("comp-1");
  assert.deepEqual(
    healthy.filter((line) =>
      /chat send returned no completion id/.test(line),
    ),
    [],
  );
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
  await Promise.resolve();
  await Promise.resolve();
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
  // sendMessage 尚未执行到 POST（首个 await 在 ensureConversation 处），
  // 此时 abort 无副作用：后端未收到请求，也不会计费。
  runtime.abortAllSendRequests();
  await pendingSend;

  assert.equal(posts, 0);
  assert.deepEqual(harness.get().messages, []);
  assert.equal(harness.get().composer.text, "画一只猫");
  assert.equal(harness.get().composerError, null);
});
