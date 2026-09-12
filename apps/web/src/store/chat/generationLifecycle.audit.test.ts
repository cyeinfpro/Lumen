import assert from "node:assert/strict";
import * as nodeModule from "node:module";
import test from "node:test";
import type { AssistantMessage, Generation } from "../../lib/types";
import type { ChatState, ChatStateGetter, ChatStateSetter } from "./types";
import "./moduleResolution.test-helper.mjs";

type ResolveResult = { url: string; shortCircuit?: boolean };
type ResolveHook = (specifier: string, context: unknown,
  nextResolve: (specifier: string, context: unknown) => ResolveResult) => ResolveResult;
const { registerHooks } = nodeModule as unknown as {
  registerHooks: (hooks: { resolve: ResolveHook }) => void;
};
const host = globalThis as typeof globalThis & {
  __generationAuditApi?: {
    apiFetch: (...args: unknown[]) => Promise<unknown>;
    createSilentGeneration: (...args: unknown[]) => Promise<unknown>;
    retryTask: (...args: unknown[]) => Promise<unknown>;
  };
};
const stub = `
export class ApiError extends Error {
  constructor(info) { super(info.message); Object.assign(this, info); }
}
export const apiFetch = (...args) => globalThis.__generationAuditApi.apiFetch(...args);
export const createSilentGeneration = (...args) => globalThis.__generationAuditApi.createSilentGeneration(...args);
export const retryTask = (...args) => globalThis.__generationAuditApi.retryTask(...args);
`;
registerHooks({
  resolve(specifier, context, nextResolve) {
    return specifier === "@/lib/apiClient"
      ? { url: `data:text/javascript,${encodeURIComponent(stub)}`, shortCircuit: true }
      : nextResolve(specifier, context);
  },
});
const { createGenerationActions } = await import(new URL("./generationActions.ts", import.meta.url).href);
const runtime = await import(new URL("./runtime.ts", import.meta.url).href);
const { semanticPostIdempotency } = await import(
  new URL("../../lib/api/semanticIdempotency.ts", import.meta.url).href
);

function assistant(id: string): AssistantMessage {
  return { id, role: "assistant", parent_user_message_id: "user-message",
    intent_resolved: "text_to_image", status: "succeeded", generation_id: "old-gen",
    generation_ids: ["old-gen"], created_at: 1 };
}
function generation(id: string, messageId: string): Generation {
  return { id, message_id: messageId, action: "generate", prompt: "cat",
    size_requested: "1024x1024", aspect_ratio: "1:1", input_image_ids: [],
    primary_input_image_id: null, status: "succeeded", stage: "finalizing",
    attempt: 1, started_at: 1, finished_at: 2 };
}
function harness() {
  const state = {
    currentUserId: "owner", currentConvId: "conversation",
    messages: [{ id: "user-message", role: "user", text: "cat", attachments: [],
      intent: "text_to_image", image_params: { aspect_ratio: "1:1", size_mode: "auto" },
      created_at: 1 }, assistant("old-asst")],
    generations: { "old-gen": generation("old-gen", "old-asst") },
    imagesById: { image: { id: "image", data_url: "http://image.test/image.png",
      width: 1024, height: 1024, parent_image_id: null, from_generation_id: "old-gen",
      size_requested: "1024x1024", size_actual: "1024x1024" } },
    composer: {}, composerError: null, messagesLoading: false, messagesError: null,
  } as unknown as ChatState;
  const get: ChatStateGetter = () => state;
  const set: ChatStateSetter = (partial) => {
    Object.assign(state, typeof partial === "function" ? partial(state) : partial);
  };
  const unexpected = async () => { throw new Error("Unexpected transport call"); };
  const api = { apiFetch: unexpected as (...args: unknown[]) => Promise<unknown>,
    createSilentGeneration: unexpected as (...args: unknown[]) => Promise<unknown>,
    retryTask: unexpected as (...args: unknown[]) => Promise<unknown> };
  host.__generationAuditApi = api;
  return { state, api, actions: createGenerationActions(set, get) };
}
function response(assistantId: string, generationId: string) {
  return { assistant_message: { id: assistantId, role: "assistant", status: "queued",
    intent: "image_to_image", content: {}, created_at: new Date().toISOString() },
    generation_ids: [generationId] };
}

for (const action of ["upscaleImage", "rerollImage"] as const) {
  test(`${action}: realtime completion before HTTP ack stays complete and unique`, async () => {
    await semanticPostIdempotency.clear();
    const h = harness();
    const completed = generation("new-gen", "new-asst");
    h.api.createSilentGeneration = async () => {
      h.state.messages.push({ ...assistant("new-asst"), generation_id: "new-gen",
        generation_ids: ["new-gen"], text: "complete" });
      h.state.generations["new-gen"] = completed;
      return response("new-asst", "new-gen");
    };
    await h.actions[action]("image");
    assert.equal(h.state.messages.filter((m) => m.id === "new-asst").length, 1);
    assert.strictEqual(h.state.generations["new-gen"], completed);
    const message = h.state.messages.find((m) => m.id === "new-asst") as AssistantMessage;
    assert.equal(message.status, "succeeded");
    await semanticPostIdempotency.clear();
  });
}

test("retry acknowledgement cannot reset the already completed newer attempt", async () => {
  const h = harness();
  h.state.generations["old-gen"] = { ...h.state.generations["old-gen"], status: "failed" };
  const completed = { ...generation("old-gen", "old-asst"), execution_epoch: 2, attempt: 2 };
  h.api.retryTask = async () => {
    h.state.generations["old-gen"] = completed;
    return { status: "queued" };
  };
  await h.actions.retryGeneration("old-gen");
  assert.strictEqual(h.state.generations["old-gen"], completed);
});

test("regenerate associates all batch IDs without cancelling historical success", async () => {
  await semanticPostIdempotency.clear();
  const h = harness();
  h.api.apiFetch = async () => ({ assistant_message_id: "batch-asst",
    completion_id: null, generation_ids: ["b1", "b2", "b3"] });
  await h.actions.regenerateAssistant("old-asst", "text_to_image");
  const message = h.state.messages.find((m) => m.id === "batch-asst") as AssistantMessage;
  assert.deepEqual(message.generation_ids, ["b1", "b2", "b3"]);
  for (const id of ["b1", "b2", "b3"]) assert.ok(h.state.generations[id]);
  assert.equal(h.state.generations["old-gen"].status, "succeeded");
  await semanticPostIdempotency.clear();
});

test("reroll forwards an existing inpaint mask", async () => {
  await semanticPostIdempotency.clear();
  const h = harness();
  h.state.generations["old-gen"] = { ...h.state.generations["old-gen"], action: "edit",
    input_image_ids: ["reference"], primary_input_image_id: "reference", mask_image_id: "mask" };
  let captured: Record<string, unknown> | undefined;
  h.api.createSilentGeneration = async (_conv, payload) => {
    captured = payload as Record<string, unknown>;
    return response("masked-asst", "masked-gen");
  };
  await h.actions.rerollImage("image");
  assert.equal(captured?.mask_image_id, "mask");
  assert.deepEqual(captured?.attachment_image_ids, ["reference"]);
  assert.equal(h.state.generations["masked-gen"].mask_image_id, "mask");
  await semanticPostIdempotency.clear();
});

for (const action of ["upscaleImage", "rerollImage", "regenerateAssistant"] as const) {
  for (const transition of ["conversation", "session"] as const) {
    test(`${action}: ${transition} change after preflight prevents a stale POST`, async () => {
      await semanticPostIdempotency.clear();
      const h = harness();
      const originalMessages = [...h.state.messages];
      const original = semanticPostIdempotency.markSubmitted;
      let posts = 0;
      h.api.createSilentGeneration = async () => {
        posts += 1;
        return response("stale-asst", "stale-gen");
      };
      h.api.apiFetch = async () => {
        posts += 1;
        return { assistant_message_id: "stale-asst", completion_id: null,
          generation_ids: ["stale-gen"] };
      };
      semanticPostIdempotency.markSubmitted = async (lease: unknown) => {
        await original.call(semanticPostIdempotency, lease);
        // The inner phase resumes first. Change scope before the outer
        // continuation resumes; this is deterministic, without timing sleeps.
        queueMicrotask(() => queueMicrotask(() => {
          if (transition === "conversation") {
            runtime._conversationMutationFence.advance();
            h.state.currentConvId = "new-conversation";
          } else {
            // A refreshed login can have the same user/conversation IDs.
            runtime._userSessionFence.advance();
          }
        }));
      };
      try {
        if (action === "regenerateAssistant") {
          await h.actions.regenerateAssistant("old-asst", "text_to_image");
        } else {
          await h.actions[action]("image");
        }
        assert.equal(posts, 0, "a stale operation must not reach the transport");
        assert.deepEqual(h.state.messages, originalMessages);
        assert.deepEqual(Object.keys(h.state.generations), ["old-gen"]);
      } finally {
        semanticPostIdempotency.markSubmitted = original;
        await semanticPostIdempotency.clear();
      }
    });
  }
}

test("cancelled reroll mask preflight resolves and releases its in-flight guard", async () => {
  await semanticPostIdempotency.clear();
  const h = harness();
  h.state.generations["old-gen"] = { ...h.state.generations["old-gen"], action: "edit",
    input_image_ids: ["reference"], primary_input_image_id: "reference" };
  let posts = 0;
  h.api.createSilentGeneration = async () => {
    posts += 1;
    return response("recovered-asst", "recovered-gen");
  };
  h.api.apiFetch = async (_path, options) => new Promise((_resolve, reject) => {
    const { signal } = options as { signal: AbortSignal };
    signal.addEventListener("abort", () => {
      reject(new DOMException("The operation was aborted", "AbortError"));
    }, { once: true });
  });
  const pending = h.actions.rerollImage("image");
  runtime._conversationMutationFence.advance();
  runtime.abortAllSendRequests();
  await assert.doesNotReject(pending);
  assert.equal(posts, 0);
  h.api.apiFetch = async () => ({ id: "old-gen", mask_image_id: "mask" });
  await h.actions.rerollImage("image");
  assert.equal(posts, 1);
  await semanticPostIdempotency.clear();
});

test("current reroll mask preflight preserves genuine transport failures", async () => {
  const h = harness();
  h.state.generations["old-gen"] = { ...h.state.generations["old-gen"], action: "edit",
    input_image_ids: ["reference"], primary_input_image_id: "reference" };
  const failure = new Error("mask metadata unavailable");
  h.api.apiFetch = async () => { throw failure; };
  await assert.rejects(h.actions.rerollImage("image"), (error) => error === failure);
});

test("reroll resolves missing legacy mask metadata before submitting", async () => {
  await semanticPostIdempotency.clear();
  const h = harness();
  h.state.generations["old-gen"] = { ...h.state.generations["old-gen"], action: "edit",
    input_image_ids: ["reference"], primary_input_image_id: "reference" };
  h.api.apiFetch = async () => ({ id: "old-gen", mask_image_id: "legacy-mask" });
  let captured: Record<string, unknown> | undefined;
  h.api.createSilentGeneration = async (_conv, payload) => {
    captured = payload as Record<string, unknown>;
    return response("legacy-asst", "legacy-gen");
  };
  await h.actions.rerollImage("image");
  assert.equal(captured?.mask_image_id, "legacy-mask");
  await semanticPostIdempotency.clear();
});
