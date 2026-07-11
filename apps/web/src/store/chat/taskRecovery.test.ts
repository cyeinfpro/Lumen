import assert from "node:assert/strict";
import test from "node:test";
import type {
  BackendCompletion,
  BackendGeneration,
} from "../../lib/apiClient";
import type {
  AssistantMessage,
  Generation,
} from "../../lib/types";
import type {
  ChatState,
  ChatStateGetter,
  ChatStateSetter,
} from "./types";
import "./moduleResolution.test-helper.mjs";

const [{ createTaskRecoveryActions }, { createRequestFence }] =
  await Promise.all([
    import(new URL("./taskRecovery.ts", import.meta.url).href),
    import(new URL("./requestGuards.ts", import.meta.url).href),
  ]);

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function activeBackendGeneration(
  id: string,
  messageId: string,
): BackendGeneration {
  return {
    id,
    message_id: messageId,
    action: "generate",
    prompt: "recover",
    size_requested: "auto",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status: "queued",
    progress_stage: "queued",
    attempt: 0,
    error_code: null,
    error_message: null,
    started_at: null,
    finished_at: null,
  };
}

test("task recovery resolves loadHistoricalMessages dynamically from get()", async () => {
  const generation: Generation = {
    id: "gen-1",
    message_id: "assistant-1",
    action: "generate",
    prompt: "test",
    size_requested: "auto",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status: "queued",
    stage: "queued",
    attempt: 0,
    started_at: 0,
  };
  const assistant: AssistantMessage = {
    id: "assistant-1",
    role: "assistant",
    parent_user_message_id: "user-message-1",
    intent_resolved: "text_to_image",
    text: "",
    status: "pending",
    created_at: 1,
    generation_id: generation.id,
  };
  const calls: string[] = [];
  const firstLoad = async (convId: string) => {
    calls.push(`first:${convId}`);
  };
  let state = {
    currentUserId: "user-1",
    currentConvId: "conv-1",
    messages: [assistant],
    generations: { [generation.id]: generation },
    loadHistoricalMessages: firstLoad,
  } as unknown as ChatState;
  const get: ChatStateGetter = () => state;
  const set: ChatStateSetter = (partial) => {
    const next = typeof partial === "function" ? partial(state) : partial;
    if (next === state) return;
    state = { ...state, ...next };
  };
  const fresh: BackendGeneration = {
    id: generation.id,
    message_id: assistant.id,
    action: "generate",
    prompt: generation.prompt,
    size_requested: generation.size_requested,
    aspect_ratio: generation.aspect_ratio,
    input_image_ids: [],
    primary_input_image_id: null,
    status: "succeeded",
    progress_stage: "finalizing",
    attempt: 1,
    error_code: null,
    error_message: null,
    started_at: null,
    finished_at: "2026-07-11T00:00:00Z",
  };
  const actions = createTaskRecoveryActions(set, get, {
    flushCompletionStreamPatches: () => {},
    userSessionFence: createRequestFence(),
    isAbortRequest: () => false,
    errorToMessage: (error: unknown) =>
      error instanceof Error ? error.message : String(error),
    getGenerationTask: async () => fresh,
    getCompletionTask: async () => {
      throw new Error("completion lookup should not run");
    },
    listActiveTasks: async () => ({
      generations: [],
      completions: [],
    }),
  });

  state = {
    ...state,
    loadHistoricalMessages: async (convId: string) => {
      calls.push(`second:${convId}`);
    },
  };

  await actions.pollInflightTasks();

  assert.deepEqual(calls, ["second:conv-1"]);
});

test("completion recovery streams, then refreshes terminal rich history once", async () => {
  const assistant: AssistantMessage = {
    id: "assistant-1",
    role: "assistant",
    parent_user_message_id: "user-message-1",
    intent_resolved: "chat",
    text: "",
    status: "pending",
    created_at: 1,
    completion_id: "completion-1",
  };
  const historyCalls: string[] = [];
  let completionChecks = 0;
  let state = {
    currentUserId: "user-1",
    currentConvId: "conv-1",
    messages: [assistant],
    generations: {},
    loadHistoricalMessages: async (convId: string) => {
      historyCalls.push(convId);
    },
  } as unknown as ChatState;
  const get: ChatStateGetter = () => state;
  const set: ChatStateSetter = (partial) => {
    const next = typeof partial === "function" ? partial(state) : partial;
    if (next === state) return;
    state = { ...state, ...next };
  };
  const completion = (
    status: BackendCompletion["status"],
    text = "",
  ): BackendCompletion =>
    ({
      id: "completion-1",
      message_id: "assistant-1",
      model: "test",
      input_image_ids: [],
      text,
      tokens_in: 0,
      tokens_out: 0,
      status,
      progress_stage: status,
      attempt: 1,
      error_code: null,
      error_message: null,
      started_at: null,
      finished_at: status === "succeeded" ? "2026-07-11T00:00:00Z" : null,
    }) as BackendCompletion;
  const actions = createTaskRecoveryActions(set, get, {
    flushCompletionStreamPatches: () => {},
    userSessionFence: createRequestFence(),
    isAbortRequest: () => false,
    errorToMessage: (error: unknown) =>
      error instanceof Error ? error.message : String(error),
    getGenerationTask: async () => {
      throw new Error("generation lookup should not run");
    },
    getCompletionTask: async () => {
      completionChecks += 1;
      return completionChecks === 1
        ? completion("streaming")
        : completion("succeeded", "final response");
    },
    listActiveTasks: async () => ({
      generations: [],
      completions: [],
    }),
  });

  await actions.pollInflightTasks();
  assert.equal((state.messages[0] as AssistantMessage).status, "streaming");
  assert.deepEqual(historyCalls, []);

  await actions.pollInflightTasks();
  assert.equal((state.messages[0] as AssistantMessage).status, "succeeded");
  assert.equal((state.messages[0] as AssistantMessage).text, "final response");
  assert.deepEqual(historyCalls, ["conv-1"]);

  await actions.pollInflightTasks();
  assert.equal(completionChecks, 2);
  assert.deepEqual(historyCalls, ["conv-1"]);
});

test("hydrate defers until auth resolves and coalesces one user request", async () => {
  const requests: Array<
    ReturnType<typeof deferred<{
      generations: BackendGeneration[];
      completions: BackendCompletion[];
    }>>
  > = [];
  let state = {
    currentUserId: null,
    currentConvId: null,
    messages: [],
    generations: {},
    loadHistoricalMessages: async () => {},
  } as unknown as ChatState;
  const get: ChatStateGetter = () => state;
  const set: ChatStateSetter = (partial) => {
    const next = typeof partial === "function" ? partial(state) : partial;
    if (next === state) return;
    state = { ...state, ...next };
  };
  const userSessionFence = createRequestFence();
  const actions = createTaskRecoveryActions(set, get, {
    flushCompletionStreamPatches: () => {},
    userSessionFence,
    isAbortRequest: () => false,
    errorToMessage: (error: unknown) =>
      error instanceof Error ? error.message : String(error),
    getGenerationTask: async () => {
      throw new Error("generation lookup should not run");
    },
    getCompletionTask: async () => {
      throw new Error("completion lookup should not run");
    },
    listActiveTasks: async () => {
      const request = deferred<{
        generations: BackendGeneration[];
        completions: BackendCompletion[];
      }>();
      requests.push(request);
      return request.promise;
    },
  });

  const anonymousHydrate = actions.hydrateActiveTasks();
  await Promise.resolve();
  assert.equal(requests.length, 0);
  await anonymousHydrate;

  state = { ...state, currentUserId: "user-1" };
  const authenticatedHydrate = actions.hydrateActiveTasks();
  const duplicateAuthenticatedHydrate = actions.hydrateActiveTasks();
  await Promise.resolve();
  assert.equal(requests.length, 1);

  requests[0].resolve({
    generations: [activeBackendGeneration("user-generation", "user-message")],
    completions: [],
  });
  await Promise.all([
    authenticatedHydrate,
    duplicateAuthenticatedHydrate,
  ]);

  assert.deepEqual(Object.keys(state.generations), ["user-generation"]);
  assert.equal(requests.length, 1);
});

test("aborted hydrate cannot swallow its replacement or merge stale data", async () => {
  const requests: Array<
    ReturnType<typeof deferred<{
      generations: BackendGeneration[];
      completions: BackendCompletion[];
    }>>
  > = [];
  let state = {
    currentUserId: "user-1",
    currentConvId: null,
    messages: [],
    generations: {},
    loadHistoricalMessages: async () => {},
  } as unknown as ChatState;
  const get: ChatStateGetter = () => state;
  const set: ChatStateSetter = (partial) => {
    const next = typeof partial === "function" ? partial(state) : partial;
    if (next === state) return;
    state = { ...state, ...next };
  };
  const actions = createTaskRecoveryActions(set, get, {
    flushCompletionStreamPatches: () => {},
    userSessionFence: createRequestFence(),
    isAbortRequest: () => false,
    errorToMessage: (error: unknown) =>
      error instanceof Error ? error.message : String(error),
    getGenerationTask: async () => {
      throw new Error("generation lookup should not run");
    },
    getCompletionTask: async () => {
      throw new Error("completion lookup should not run");
    },
    listActiveTasks: async () => {
      const request = deferred<{
        generations: BackendGeneration[];
        completions: BackendCompletion[];
      }>();
      requests.push(request);
      return request.promise;
    },
  });
  const abortedController = new AbortController();

  const abortedHydrate = actions.hydrateActiveTasks({
    signal: abortedController.signal,
  });
  await Promise.resolve();
  assert.equal(requests.length, 1);

  abortedController.abort();
  const replacementHydrate = actions.hydrateActiveTasks();
  const duplicateReplacementHydrate = actions.hydrateActiveTasks();
  await Promise.resolve();
  assert.equal(requests.length, 2);

  requests[0].resolve({
    generations: [activeBackendGeneration("stale-generation", "stale-message")],
    completions: [],
  });
  await abortedHydrate;
  assert.equal(state.generations["stale-generation"], undefined);

  requests[1].resolve({
    generations: [activeBackendGeneration("fresh-generation", "fresh-message")],
    completions: [],
  });
  await Promise.all([replacementHydrate, duplicateReplacementHydrate]);

  assert.deepEqual(Object.keys(state.generations), ["fresh-generation"]);
  assert.equal(requests.length, 2);
});

test("hydrate drops a prior user's response after the identity fence advances", async () => {
  const requests: Array<
    ReturnType<typeof deferred<{
      generations: BackendGeneration[];
      completions: BackendCompletion[];
    }>>
  > = [];
  let state = {
    currentUserId: "user-a",
    currentConvId: null,
    messages: [],
    generations: {},
    loadHistoricalMessages: async () => {},
  } as unknown as ChatState;
  const get: ChatStateGetter = () => state;
  const set: ChatStateSetter = (partial) => {
    const next = typeof partial === "function" ? partial(state) : partial;
    if (next === state) return;
    state = { ...state, ...next };
  };
  const userSessionFence = createRequestFence();
  const actions = createTaskRecoveryActions(set, get, {
    flushCompletionStreamPatches: () => {},
    userSessionFence,
    isAbortRequest: () => false,
    errorToMessage: (error: unknown) =>
      error instanceof Error ? error.message : String(error),
    getGenerationTask: async () => {
      throw new Error("generation lookup should not run");
    },
    getCompletionTask: async () => {
      throw new Error("completion lookup should not run");
    },
    listActiveTasks: async () => {
      const request = deferred<{
        generations: BackendGeneration[];
        completions: BackendCompletion[];
      }>();
      requests.push(request);
      return request.promise;
    },
  });

  const firstUserHydrate = actions.hydrateActiveTasks();
  await Promise.resolve();
  assert.equal(requests.length, 1);

  userSessionFence.advance();
  state = { ...state, currentUserId: "user-b" };
  const secondUserHydrate = actions.hydrateActiveTasks();
  await Promise.resolve();
  assert.equal(requests.length, 2);

  requests[0].resolve({
    generations: [activeBackendGeneration("user-a-generation", "message-a")],
    completions: [],
  });
  await firstUserHydrate;
  assert.equal(state.generations["user-a-generation"], undefined);

  requests[1].resolve({
    generations: [activeBackendGeneration("user-b-generation", "message-b")],
    completions: [],
  });
  await secondUserHydrate;

  assert.deepEqual(Object.keys(state.generations), ["user-b-generation"]);
});
