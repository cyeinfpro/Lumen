import assert from "node:assert/strict";
import * as nodeModule from "node:module";
import test from "node:test";
import type {
  AssistantMessage,
  Generation,
  UserMessage,
} from "../../lib/types";
import type { ChatState, ChatStateGetter, ChatStateSetter } from "./types";
import "./moduleResolution.test-helper.mjs";

// logWarn only reaches console outside production; pin the mode so the warning
// assertions do not depend on the ambient NODE_ENV of the test runner.
(process.env as Record<string, string | undefined>).NODE_ENV = "development";

type ResolveResult = { url: string; shortCircuit?: boolean };
type ResolveHook = (
  specifier: string,
  context: unknown,
  nextResolve: (specifier: string, context: unknown) => ResolveResult,
) => ResolveResult;

const { registerHooks } = nodeModule as unknown as {
  registerHooks: (hooks: { resolve: ResolveHook }) => void;
};

// 生成动作通过 @/lib/apiClient 发请求，直接换成一个计数桩，避免引入真实传输层
// （CSRF 握手、重试、fetch）。其余模块（runtime/history 等）仍走真实实现。
const stubSource = `
export class ApiError extends Error {
  constructor(info) {
    super(info?.message ?? "api error");
    this.code = info?.code;
    this.status = info?.status;
  }
}
export function apiFetch(...args) {
  return globalThis.__apiClientStub.apiFetch(...args);
}
export function createSilentGeneration(...args) {
  return globalThis.__apiClientStub.createSilentGeneration(...args);
}
export function retryTask(...args) {
  return globalThis.__apiClientStub.retryTask(...args);
}
`;

// Hooks run in reverse registration order, so registering after the shared
// `@/` resolver puts this one first.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/lib/apiClient") {
      return {
        url: `data:text/javascript,${encodeURIComponent(stubSource)}`,
        shortCircuit: true,
      };
    }
    return nextResolve(specifier, context);
  },
});

const stubHost = globalThis as typeof globalThis & {
  __apiClientStub?: {
    apiFetch?: (path: string, opts?: unknown) => Promise<unknown>;
    createSilentGeneration?: (
      convId: string,
      body: unknown,
    ) => Promise<unknown>;
    retryTask?: (kind: string, id: string) => Promise<unknown>;
  };
};

const [
  { createGenerationActions },
  runtime,
  { makeConversationHistoryCacheEntry },
] = await Promise.all([
  import(new URL("./generationActions.ts", import.meta.url).href),
  import(new URL("./runtime.ts", import.meta.url).href),
  import(new URL("./history.ts", import.meta.url).href),
]);

function deferred<T = unknown>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

const userMessage: UserMessage = {
  id: "user-1",
  role: "user",
  text: "画一只猫",
  attachments: [],
  intent: "text_to_image",
  image_params: {
    aspect_ratio: "1:1",
    size_mode: "auto",
    count: 1,
    fast: false,
    quality: "1k",
  },
  created_at: 1,
};

function generatedImage(id: string, fromGenerationId: string) {
  return {
    id,
    data_url: `http://img.local/${id}.png`,
    width: 1024,
    height: 1024,
    parent_image_id: null,
    from_generation_id: fromGenerationId,
    size_requested: "auto",
    size_actual: "1024x1024",
  };
}

const oldAssistant: AssistantMessage = {
  id: "asst-old",
  role: "assistant",
  parent_user_message_id: "user-1",
  intent_resolved: "text_to_image",
  generation_id: "gen-old",
  generation_ids: ["gen-old"],
  status: "succeeded",
  created_at: 1,
};

const oldGeneration: Generation = {
  id: "gen-old",
  message_id: "asst-old",
  action: "generate",
  prompt: "画一只猫",
  size_requested: "auto",
  aspect_ratio: "1:1",
  input_image_ids: [],
  primary_input_image_id: null,
  status: "succeeded",
  stage: "finalizing",
  attempt: 0,
  started_at: 1000,
};

function makeHarness() {
  const state = {
    currentUserId: "user-1",
    currentConvId: "conv-1",
    messages: [userMessage, oldAssistant],
    generations: { "gen-old": oldGeneration },
    imagesById: {},
    messagesCursor: null,
    messagesHasMore: false,
    messagesLoading: false,
    messagesError: null,
    composerError: null,
    composer: {},
  } as unknown as ChatState;
  const get: ChatStateGetter = () => state;
  const set: ChatStateSetter = (partial) => {
    if (typeof partial === "function") {
      Object.assign(state, partial(state));
    } else {
      Object.assign(state, partial);
    }
  };
  const actions = createGenerationActions(set, get, {
    runtimeFastDefault: () => false,
  });
  return { state, get, set, actions };
}

function silentGenerationOut(assistantId: string, generationId: string) {
  return {
    assistant_message: {
      id: assistantId,
      role: "assistant",
      status: "queued",
      intent: "image_to_image",
      content: {},
      created_at: new Date().toISOString(),
    },
    generation_ids: [generationId],
  };
}

test("regenerateAssistant: 双击在途去重，只发一次 POST /regenerate", async () => {
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  const gate = deferred<unknown>();
  let calls = 0;
  stub.apiFetch = async () => {
    calls += 1;
    await gate.promise;
    return {
      assistant_message_id: "asst-new",
      completion_id: null,
      generation_ids: ["gen-new"],
    };
  };

  const { actions } = makeHarness();
  const first = actions.regenerateAssistant("asst-old", "text_to_image");
  // 第二击同步被在途锁吞掉，不再发 POST
  const second = actions.regenerateAssistant("asst-old", "text_to_image");
  await second;
  assert.equal(calls, 1);

  gate.resolve(null);
  await first;
  assert.equal(calls, 1);

  // 锁已释放：新状态下再次触发可以重新发起（若锁泄漏，该调用会被静默吞掉）
  const { actions: freshActions } = makeHarness();
  await freshActions.regenerateAssistant("asst-old", "text_to_image");
  assert.equal(calls, 2);
});

test("upscaleImage: 双击在途去重，只创建一条放大任务", async () => {
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  const gate = deferred<unknown>();
  let calls = 0;
  stub.createSilentGeneration = async () => {
    calls += 1;
    await gate.promise;
    return silentGenerationOut("asst-up-1", "gen-up-1");
  };

  const { state, actions } = makeHarness();
  state.imagesById = {
    "img-1": generatedImage("img-1", "gen-old"),
  };

  const first = actions.upscaleImage("img-1");
  const second = actions.upscaleImage("img-1");
  await second;
  assert.equal(calls, 1);

  gate.resolve(null);
  await first;
  assert.equal(calls, 1);
  assert.ok(
    state.messages.some((m) => m.id === "asst-up-1"),
    "乐观插入放大 assistant",
  );
  assert.ok(state.generations["gen-up-1"], "乐观插入放大 generation");
});

test("rerollImage: 双击在途去重，只创建一条重roll 任务", async () => {
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  const gate = deferred<unknown>();
  let calls = 0;
  stub.createSilentGeneration = async () => {
    calls += 1;
    await gate.promise;
    return silentGenerationOut("asst-roll-1", "gen-roll-1");
  };

  const { state, actions } = makeHarness();
  state.imagesById = {
    "img-1": generatedImage("img-1", "gen-old"),
  };

  const first = actions.rerollImage("img-1");
  const second = actions.rerollImage("img-1");
  await second;
  assert.equal(calls, 1);

  gate.resolve(null);
  await first;
  assert.equal(calls, 1);
  assert.ok(
    state.messages.some((m) => m.id === "asst-roll-1"),
    "乐观插入重roll assistant",
  );
  assert.ok(state.generations["gen-roll-1"], "乐观插入重roll generation");
});

test("regenerateAssistant 乐观变更后会话历史缓存失效", async () => {
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  stub.apiFetch = async () => ({
    assistant_message_id: "asst-new",
    completion_id: null,
    generation_ids: ["gen-new"],
  });

  runtime.rememberConversationHistoryCache(
    "conv-1",
    makeConversationHistoryCacheEntry(
      [userMessage, oldAssistant],
      { "gen-old": oldGeneration },
      {},
      null,
      false,
    ),
  );
  assert.ok(runtime.readConversationHistoryCache("conv-1"), "seed 缓存");

  const { actions } = makeHarness();
  await actions.regenerateAssistant("asst-old", "text_to_image");

  assert.equal(
    runtime.readConversationHistoryCache("conv-1"),
    null,
    "重生成后缓存被失效，切走切回不会显示旧快照",
  );
});

test("upscaleImage 乐观变更后会话历史缓存失效", async () => {
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  stub.createSilentGeneration = async () =>
    silentGenerationOut("asst-up-2", "gen-up-2");

  runtime.rememberConversationHistoryCache(
    "conv-1",
    makeConversationHistoryCacheEntry(
      [userMessage, oldAssistant],
      { "gen-old": oldGeneration },
      {},
      null,
      false,
    ),
  );
  assert.ok(runtime.readConversationHistoryCache("conv-1"), "seed 缓存");

  const { state, actions } = makeHarness();
  state.imagesById = {
    "img-1": generatedImage("img-1", "gen-old"),
  };
  await actions.upscaleImage("img-1");

  assert.equal(
    runtime.readConversationHistoryCache("conv-1"),
    null,
    "放大后缓存被失效",
  );
});

test("rerollImage 乐观变更后会话历史缓存失效", async () => {
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  stub.createSilentGeneration = async () =>
    silentGenerationOut("asst-roll-2", "gen-roll-2");

  runtime.rememberConversationHistoryCache(
    "conv-1",
    makeConversationHistoryCacheEntry(
      [userMessage, oldAssistant],
      { "gen-old": oldGeneration },
      {},
      null,
      false,
    ),
  );
  assert.ok(runtime.readConversationHistoryCache("conv-1"), "seed 缓存");

  const { state, actions } = makeHarness();
  state.imagesById = {
    "img-1": generatedImage("img-1", "gen-old"),
  };
  await actions.rerollImage("img-1");

  assert.equal(
    runtime.readConversationHistoryCache("conv-1"),
    null,
    "重roll 后缓存被失效",
  );
});
