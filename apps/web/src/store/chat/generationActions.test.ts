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
  { semanticPostIdempotency },
] = await Promise.all([
  import(new URL("./generationActions.ts", import.meta.url).href),
  import(new URL("./runtime.ts", import.meta.url).href),
  import(new URL("./history.ts", import.meta.url).href),
  import(
    new URL("../../lib/api/semanticIdempotency.ts", import.meta.url).href
  ),
]);

function deferred<T = unknown>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
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

function holdNextSemanticAcquire() {
  const store = semanticPostIdempotency as {
    acquire: typeof semanticPostIdempotency.acquire;
  };
  const originalAcquire = store.acquire;
  const entered = deferred<void>();
  const release = deferred<void>();
  let held = false;
  store.acquire = async (scope: unknown, payload: unknown) => {
    const lease = await originalAcquire.call(
      semanticPostIdempotency,
      scope,
      payload,
    );
    if (!held) {
      held = true;
      entered.resolve(undefined);
      await release.promise;
    }
    return lease;
  };
  return {
    entered: entered.promise,
    release: () => release.resolve(undefined),
    restore: () => {
      store.acquire = originalAcquire;
    },
  };
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
  await waitFor(() => calls === 1, "regenerate request was not dispatched");
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
  await waitFor(() => calls === 1, "upscale request was not dispatched");
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
  await waitFor(() => calls === 1, "reroll request was not dispatched");
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

test("regenerate response loss reuses the same semantic key and matching header", async () => {
  await semanticPostIdempotency.clear();
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  const accepted = {
    assistant_message_id: "asst-replayed",
    completion_id: null,
    generation_ids: ["gen-replayed"],
  };
  const acceptedByKey = new Map<string, typeof accepted>();
  const requests: Array<{ bodyKey: string; headerKey: string | null }> = [];
  stub.apiFetch = async (_path, opts) => {
    const request = opts as RequestInit;
    const body = JSON.parse(String(request.body)) as {
      idempotency_key: string;
    };
    const headerKey = new Headers(request.headers).get("Idempotency-Key");
    requests.push({ bodyKey: body.idempotency_key, headerKey });
    const prior = acceptedByKey.get(body.idempotency_key);
    if (prior) return prior;
    acceptedByKey.set(body.idempotency_key, accepted);
    throw new TypeError("response lost after backend accepted regenerate");
  };

  const { state, actions } = makeHarness();
  await assert.rejects(
    actions.regenerateAssistant("asst-old", "text_to_image"),
    /response lost/,
  );
  assert.ok(state.messages.some((message) => message.id === "asst-old"));

  await actions.regenerateAssistant("asst-old", "text_to_image");

  assert.equal(requests.length, 2);
  assert.equal(requests[1]?.bodyKey, requests[0]?.bodyKey);
  assert.equal(requests[0]?.headerKey, requests[0]?.bodyKey);
  assert.equal(requests[1]?.headerKey, requests[1]?.bodyKey);
  assert.equal(acceptedByKey.size, 1);
  assert.ok(state.messages.some((message) => message.id === "asst-replayed"));
  await semanticPostIdempotency.clear();
});

test("image regenerate without generation ids reuses the same semantic key", async () => {
  await semanticPostIdempotency.clear();
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  const keys: string[] = [];
  let calls = 0;
  stub.apiFetch = async (_path, opts) => {
    calls += 1;
    const body = JSON.parse(String((opts as RequestInit).body)) as {
      idempotency_key: string;
    };
    keys.push(body.idempotency_key);
    if (calls === 1) {
      return {
        assistant_message_id: "asst-malformed",
        completion_id: null,
        generation_ids: ["   "],
      };
    }
    return {
      assistant_message_id: "asst-reconciled",
      completion_id: null,
      generation_ids: ["gen-reconciled"],
    };
  };

  const { state, actions } = makeHarness();
  await assert.rejects(
    actions.regenerateAssistant("asst-old", "text_to_image"),
    /malformed regenerate response/,
  );
  assert.ok(state.messages.some((message) => message.id === "asst-old"));

  await actions.regenerateAssistant("asst-old", "text_to_image");

  assert.deepEqual(keys, [keys[0], keys[0]]);
  assert.ok(state.messages.some((message) => message.id === "asst-reconciled"));
  await semanticPostIdempotency.clear();
});

test("text regenerate without completion id reuses the same semantic key", async () => {
  await semanticPostIdempotency.clear();
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  const keys: string[] = [];
  let calls = 0;
  stub.apiFetch = async (_path, opts) => {
    calls += 1;
    const body = JSON.parse(String((opts as RequestInit).body)) as {
      idempotency_key: string;
    };
    keys.push(body.idempotency_key);
    return {
      assistant_message_id:
        calls === 1 ? "asst-malformed-text" : "asst-reconciled-text",
      completion_id: calls === 1 ? "   " : "comp-reconciled",
      generation_ids: [],
    };
  };

  const { state, actions } = makeHarness();
  await assert.rejects(
    actions.regenerateAssistant("asst-old", "chat"),
    /malformed regenerate response/,
  );
  assert.ok(state.messages.some((message) => message.id === "asst-old"));

  await actions.regenerateAssistant("asst-old", "chat");

  assert.deepEqual(keys, [keys[0], keys[0]]);
  assert.ok(
    state.messages.some((message) => message.id === "asst-reconciled-text"),
  );
  await semanticPostIdempotency.clear();
});

test("upscale response loss reuses the same semantic key on manual retry", async () => {
  await semanticPostIdempotency.clear();
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  const accepted = silentGenerationOut("asst-up-replayed", "gen-up-replayed");
  const acceptedByKey = new Map<string, typeof accepted>();
  const keys: string[] = [];
  stub.createSilentGeneration = async (_convId, body) => {
    const request = body as { idempotency_key: string };
    keys.push(request.idempotency_key);
    const prior = acceptedByKey.get(request.idempotency_key);
    if (prior) return prior;
    acceptedByKey.set(request.idempotency_key, accepted);
    throw new TypeError("response lost after backend accepted upscale");
  };

  const { state, actions } = makeHarness();
  state.imagesById = {
    "img-1": generatedImage("img-1", "gen-old"),
  };

  await assert.rejects(actions.upscaleImage("img-1"), /response lost/);
  await actions.upscaleImage("img-1");

  assert.deepEqual(keys, [keys[0], keys[0]]);
  assert.equal(acceptedByKey.size, 1);
  assert.ok(state.generations["gen-up-replayed"]);
  await semanticPostIdempotency.clear();
});

test("upscale malformed 2xx reuses the same semantic key", async () => {
  await semanticPostIdempotency.clear();
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  const keys: string[] = [];
  let calls = 0;
  stub.createSilentGeneration = async (_convId, body) => {
    calls += 1;
    keys.push((body as { idempotency_key: string }).idempotency_key);
    if (calls === 1) {
      return { assistant_message: {}, generation_ids: [] };
    }
    return silentGenerationOut("asst-up-valid", "gen-up-valid");
  };

  const { state, actions } = makeHarness();
  state.imagesById = {
    "img-1": generatedImage("img-1", "gen-old"),
  };

  await assert.rejects(
    actions.upscaleImage("img-1"),
    /malformed silent generation response/,
  );
  await actions.upscaleImage("img-1");

  assert.deepEqual(keys, [keys[0], keys[0]]);
  assert.ok(state.generations["gen-up-valid"]);
  await semanticPostIdempotency.clear();
});

test("reroll response loss reuses the same semantic key on manual retry", async () => {
  await semanticPostIdempotency.clear();
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  const accepted = silentGenerationOut(
    "asst-roll-replayed",
    "gen-roll-replayed",
  );
  const acceptedByKey = new Map<string, typeof accepted>();
  const keys: string[] = [];
  stub.createSilentGeneration = async (_convId, body) => {
    const request = body as { idempotency_key: string };
    keys.push(request.idempotency_key);
    const prior = acceptedByKey.get(request.idempotency_key);
    if (prior) return prior;
    acceptedByKey.set(request.idempotency_key, accepted);
    throw new TypeError("response lost after backend accepted reroll");
  };

  const { state, actions } = makeHarness();
  state.imagesById = {
    "img-1": generatedImage("img-1", "gen-old"),
  };

  await assert.rejects(actions.rerollImage("img-1"), /response lost/);
  await actions.rerollImage("img-1");

  assert.deepEqual(keys, [keys[0], keys[0]]);
  assert.equal(acceptedByKey.size, 1);
  assert.ok(state.generations["gen-roll-replayed"]);
  await semanticPostIdempotency.clear();
});

test("conversation switch during regenerate acquire prevents POST and mutation", async () => {
  await semanticPostIdempotency.clear();
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  let calls = 0;
  stub.apiFetch = async () => {
    calls += 1;
    return {
      assistant_message_id: "asst-should-not-post",
      completion_id: null,
      generation_ids: ["gen-should-not-post"],
    };
  };
  const heldAcquire = holdNextSemanticAcquire();
  const { state, actions } = makeHarness();
  const pending = actions.regenerateAssistant(
    "asst-old",
    "text_to_image",
  );

  try {
    await heldAcquire.entered;
    const nextMessages: ChatState["messages"] = [];
    const nextGenerations: ChatState["generations"] = {};
    state.currentConvId = "conv-2";
    state.messages = nextMessages;
    state.generations = nextGenerations;
    runtime._conversationMutationFence.advance();
    runtime.abortAllSendRequests();
    heldAcquire.release();
    await pending;

    assert.equal(calls, 0);
    assert.equal(state.currentConvId, "conv-2");
    assert.strictEqual(state.messages, nextMessages);
    assert.strictEqual(state.generations, nextGenerations);
  } finally {
    heldAcquire.release();
    heldAcquire.restore();
    await semanticPostIdempotency.clear();
  }
});

for (const actionName of ["upscaleImage", "rerollImage"] as const) {
  test(`identity switch during ${actionName} acquire prevents POST and mutation`, async () => {
    await semanticPostIdempotency.clear();
    const stub = stubHost.__apiClientStub ?? {};
    stubHost.__apiClientStub = stub;
    let calls = 0;
    stub.createSilentGeneration = async () => {
      calls += 1;
      return silentGenerationOut("asst-should-not-post", "gen-should-not-post");
    };
    const heldAcquire = holdNextSemanticAcquire();
    const { state, actions } = makeHarness();
    state.imagesById = {
      "img-1": generatedImage("img-1", "gen-old"),
    };
    const pending = actions[actionName]("img-1");

    try {
      await heldAcquire.entered;
      const nextMessages: ChatState["messages"] = [];
      const nextGenerations: ChatState["generations"] = {};
      const nextImages: ChatState["imagesById"] = {};
      state.currentUserId = "user-2";
      state.currentConvId = "conv-2";
      state.messages = nextMessages;
      state.generations = nextGenerations;
      state.imagesById = nextImages;
      runtime._userSessionFence.advance();
      runtime._conversationMutationFence.advance();
      runtime.abortAllSendRequests();
      heldAcquire.release();
      await pending;

      assert.equal(calls, 0);
      assert.equal(state.currentUserId, "user-2");
      assert.equal(state.currentConvId, "conv-2");
      assert.strictEqual(state.messages, nextMessages);
      assert.strictEqual(state.generations, nextGenerations);
      assert.strictEqual(state.imagesById, nextImages);
    } finally {
      heldAcquire.release();
      heldAcquire.restore();
      await semanticPostIdempotency.clear();
    }
  });
}

test("controller cancellation during regenerate acquire prevents POST", async () => {
  await semanticPostIdempotency.clear();
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  let calls = 0;
  stub.apiFetch = async () => {
    calls += 1;
    throw new Error("canceled generation must not post");
  };
  const heldAcquire = holdNextSemanticAcquire();
  const { state, actions } = makeHarness();
  const initialMessages = state.messages;
  const initialGenerations = state.generations;
  const pending = actions.regenerateAssistant(
    "asst-old",
    "text_to_image",
  );

  try {
    await heldAcquire.entered;
    runtime.abortAllSendRequests();
    heldAcquire.release();
    await pending;

    assert.equal(calls, 0);
    assert.strictEqual(state.messages, initialMessages);
    assert.strictEqual(state.generations, initialGenerations);
  } finally {
    heldAcquire.release();
    heldAcquire.restore();
    await semanticPostIdempotency.clear();
  }
});

test("conversation switch suppresses stale regenerate UI after confirming its response", async () => {
  await semanticPostIdempotency.clear();
  const firstResponse = deferred<unknown>();
  const keys: string[] = [];
  let calls = 0;
  const stub = stubHost.__apiClientStub ?? {};
  stubHost.__apiClientStub = stub;
  stub.apiFetch = async (_path, opts) => {
    calls += 1;
    const body = JSON.parse(String((opts as RequestInit).body)) as {
      idempotency_key: string;
    };
    keys.push(body.idempotency_key);
    if (calls === 1) return firstResponse.promise;
    return {
      assistant_message_id: "asst-second",
      completion_id: null,
      generation_ids: ["gen-second"],
    };
  };

  const { state, actions } = makeHarness();
  const first = actions.regenerateAssistant("asst-old", "text_to_image");
  await waitFor(() => calls === 1, "first regenerate was not dispatched");
  state.currentConvId = "conv-2";
  runtime._conversationMutationFence.advance();
  firstResponse.resolve({
    assistant_message_id: "asst-first",
    completion_id: null,
    generation_ids: ["gen-first"],
  });
  await first;

  state.currentConvId = "conv-1";
  state.messages = [userMessage, oldAssistant];
  state.generations = { "gen-old": oldGeneration };
  await actions.regenerateAssistant("asst-old", "text_to_image");

  assert.equal(keys.length, 2);
  assert.notEqual(keys[1], keys[0]);
  assert.ok(state.messages.some((message) => message.id === "asst-second"));
  await semanticPostIdempotency.clear();
});

for (const actionName of ["upscaleImage", "rerollImage"] as const) {
  test(`identity switch suppresses stale ${actionName} UI after confirming its response`, async () => {
    await semanticPostIdempotency.clear();
    const firstResponse = deferred<unknown>();
    const keys: string[] = [];
    let calls = 0;
    const stub = stubHost.__apiClientStub ?? {};
    stubHost.__apiClientStub = stub;
    stub.createSilentGeneration = async (_convId, body) => {
      calls += 1;
      keys.push((body as { idempotency_key: string }).idempotency_key);
      if (calls === 1) return firstResponse.promise;
      return silentGenerationOut(`asst-${actionName}-second`, "gen-second");
    };

    const { state, actions } = makeHarness();
    state.imagesById = {
      "img-1": generatedImage("img-1", "gen-old"),
    };
    const first = actions[actionName]("img-1");
    await waitFor(() => calls === 1, `first ${actionName} was not dispatched`);
    state.currentUserId = "user-2";
    runtime._conversationMutationFence.advance();
    firstResponse.resolve(
      silentGenerationOut(`asst-${actionName}-first`, "gen-first"),
    );
    await first;

    state.currentUserId = "user-1";
    await actions[actionName]("img-1");

    assert.equal(keys.length, 2);
    assert.notEqual(keys[1], keys[0]);
    assert.ok(state.generations["gen-second"]);
    await semanticPostIdempotency.clear();
  });
}

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
