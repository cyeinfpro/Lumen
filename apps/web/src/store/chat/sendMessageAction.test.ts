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

const stubHost = globalThis as typeof globalThis & {
  __conversationsStub?: { postMessage: PostMessageStub };
};

const [{ createSendMessageAction }, runtime, { createComposerState }] =
  await Promise.all([
    import(new URL("./sendMessageAction.ts", import.meta.url).href),
    import(new URL("./runtime.ts", import.meta.url).href),
    import(new URL("./composerSlice.ts", import.meta.url).href),
  ]);

function createHarness() {
  let state = {
    currentUserId: "user-1",
    currentConvId: "conv-1",
    messages: [],
    generations: {},
    imagesById: {},
    composerError: null,
    composer: { ...createComposerState(null), text: "画一只猫" },
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
  return { get, sendMessage };
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
