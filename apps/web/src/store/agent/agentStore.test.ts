import assert from "node:assert/strict";
import test from "node:test";
import type {
  AgentDraftAttachment,
  AgentMessage,
} from "../../features/agent/model/contracts";
import "../chat/moduleResolution.test-helper.mjs";

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalLocalStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
const storage = new Map<string, string>();
Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: new EventTarget(),
});
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  },
});

const { useAgentStore } = await import(
  new URL("./useAgentStore.ts", import.meta.url).href
);
const { serializeAgentDrafts, deserializeAgentDrafts, removeAgentDrafts } = await import(
  new URL("./draftPersistence.ts", import.meta.url).href
);

test.after(() => {
  if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
  else Reflect.deleteProperty(globalThis, "window");
  if (originalLocalStorage) {
    Object.defineProperty(globalThis, "localStorage", originalLocalStorage);
  } else {
    Reflect.deleteProperty(globalThis, "localStorage");
  }
});

test("draft persistence strips URLs and rejects another owner", () => {
  const raw = serializeAgentDrafts("user-a", {
    session: {
      text: "private text",
      attachments: [
        {
          imageId: "image-a",
          role: "product",
          label: "产品",
          name: "secret filename.png",
          previewUrl: "data:image/png;base64,secret",
          mime: "image/png",
        },
      ],
      allowImage: true,
      imageDefaults: {
        count: 2,
        aspect_ratio: "3:4",
        quality: "2k",
        render_quality: "high",
        background: "auto",
        output_format: "webp",
      },
    },
  });
  assert.doesNotMatch(raw, /data:image|secret filename|previewUrl|mime/);
  assert.deepEqual(deserializeAgentDrafts(raw, "user-b"), {});
  const restored = deserializeAgentDrafts(raw, "user-a");
  assert.equal(restored.session.attachments[0].role, "product");
  assert.equal(restored.session.attachments[0].previewUrl, "/api/images/image-a/variants/thumb256");
});

test("Agent attachments preserve controlled role and order", () => {
  useAgentStore.getState().resetForIdentity({ userId: "user-a", epoch: 1 });
  const first = {
    imageId: "one",
    role: "reference" as const,
    label: null,
    name: "一",
    previewUrl: "/one",
  };
  const second = { ...first, imageId: "two", name: "二" };
  assert.equal(useAgentStore.getState().addDraftAttachment("session", first), true);
  assert.equal(useAgentStore.getState().addDraftAttachment("session", second), true);
  useAgentStore.getState().setDraftAttachmentRole("session", "two", "style");
  useAgentStore.getState().moveDraftAttachment("session", "two", -1);
  const attachments = useAgentStore.getState().draftsBySession.session.attachments;
  assert.deepEqual(attachments.map((item: AgentDraftAttachment) => [item.imageId, item.role]), [
    ["two", "style"],
    ["one", "reference"],
  ]);
});

test("successful content clearing preserves sticky image defaults", () => {
  useAgentStore.getState().resetForIdentity({ userId: "user-a", epoch: 3 });
  useAgentStore.getState().setDraft("session", {
    text: "send me",
    attachments: [
      {
        imageId: "one",
        role: "reference",
        label: null,
        name: "one",
        previewUrl: "/one",
      },
    ],
    allowImage: false,
    imageDefaults: { count: 3, aspect_ratio: "3:4", quality: "4k" },
  });
  useAgentStore.getState().clearDraftContent("session");
  const draft = useAgentStore.getState().draftsBySession.session;
  assert.equal(draft.text, "");
  assert.deepEqual(draft.attachments, []);
  assert.equal(draft.allowImage, false);
  assert.equal(draft.imageDefaults.count, 3);
  assert.equal(draft.imageDefaults.aspect_ratio, "3:4");
  assert.equal(draft.imageDefaults.quality, "4k");
});

test("account deletion removes only the owning persisted Agent drafts", () => {
  storage.set("lumen.agent.drafts.v1:user-a", "private-a");
  storage.set("lumen.agent.drafts.v1:user-b", "private-b");
  removeAgentDrafts("user-a");
  assert.equal(storage.has("lumen.agent.drafts.v1:user-a"), false);
  assert.equal(storage.get("lumen.agent.drafts.v1:user-b"), "private-b");
});

test("identity reset clears user A messages and loads only matching drafts", () => {
  useAgentStore.setState({
    messagesBySession: {
      private: [
        { id: "a", role: "user", text: "secret", attachments: [], createdAt: "2026-01-01" },
      ],
    },
  });
  useAgentStore.getState().resetForIdentity({ userId: "user-b", epoch: 2 });
  assert.equal(useAgentStore.getState().ownerUserId, "user-b");
  assert.deepEqual(useAgentStore.getState().messagesBySession, {});
  assert.deepEqual(useAgentStore.getState().draftsBySession, {});
});

test("optimistic submission reconciliation removes temporary messages exactly once", () => {
  const state = useAgentStore.getState();
  const optimisticRun = {
    id: "optimistic:assistant-temp",
    agent_session_id: "session-1",
    user_message_id: "user-temp",
    assistant_message_id: "assistant-temp",
    status: "queued" as const,
    execution_epoch: 0,
    last_event_seq: 0,
    idempotency_key: "message-key-1",
    model: null,
    reasoning_effort: null,
    turn_count: 0,
    tool_call_count: 0,
    usage: {},
    error_code: null,
    error_message: null,
    started_at: null,
    finished_at: null,
    cancel_requested_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    references: [],
    tool_calls: [],
  };
  state.appendOptimistic({
    sessionId: "session-1",
    userMessage: {
      id: "user-temp",
      role: "user",
      text: "hello",
      attachments: [],
      createdAt: "2026-01-01T00:00:00Z",
      optimistic: true,
    },
    assistantMessage: {
      id: "assistant-temp",
      role: "assistant",
      text: "",
      status: "queued",
      agentRunId: optimisticRun.id,
      parentUserMessageId: "user-temp",
      generationIds: [],
      toolCalls: [],
      createdAt: "2026-01-01T00:00:00Z",
      partial: false,
      optimistic: true,
    },
    run: optimisticRun,
  });
  const realRun = {
    ...optimisticRun,
    id: "run-real",
    user_message_id: "user-real",
    assistant_message_id: "assistant-real",
    last_event_seq: 1,
  };
  const result = {
    user_message: {
      id: "user-real",
      conversation_id: "conversation-1",
      role: "user" as const,
      content: { source: "agent", text: "hello", attachments: [] },
      intent: "agent",
      status: null,
      parent_message_id: null,
      created_at: "2026-01-01T00:00:00Z",
    },
    assistant_message: {
      id: "assistant-real",
      conversation_id: "conversation-1",
      role: "assistant" as const,
      content: { source: "agent", text: "", agent_run_id: "run-real" },
      intent: "agent",
      status: "pending",
      parent_message_id: "user-real",
      created_at: "2026-01-01T00:00:01Z",
    },
    agent_run: realRun,
  };
  state.reconcileSubmission({
    sessionId: "session-1",
    optimisticUserId: "user-temp",
    optimisticAssistantId: "assistant-temp",
    result,
  });
  state.reconcileSubmission({
    sessionId: "session-1",
    optimisticUserId: "user-temp",
    optimisticAssistantId: "assistant-temp",
    result,
  });
  assert.deepEqual(
    useAgentStore.getState().messagesBySession["session-1"].map((message: AgentMessage) => message.id),
    ["user-real", "assistant-real"],
  );
});

test("snapshot reconciliation removes an uncertain optimistic turn by idempotency key", () => {
  useAgentStore.getState().resetForIdentity({ userId: "user-a", epoch: 4 });
  const optimisticRun = {
    id: "optimistic:assistant-lost",
    agent_session_id: "session-uncertain",
    user_message_id: "user-lost",
    assistant_message_id: "assistant-lost",
    status: "failed" as const,
    execution_epoch: 0,
    last_event_seq: 0,
    idempotency_key: "delivery-key",
    model: null,
    reasoning_effort: null,
    turn_count: 0,
    tool_call_count: 0,
    usage: {},
    error_code: "network_error",
    error_message: null,
    started_at: null,
    finished_at: null,
    cancel_requested_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    references: [],
    tool_calls: [],
  };
  useAgentStore.getState().appendOptimistic({
    sessionId: "session-uncertain",
    userMessage: {
      id: "user-lost",
      role: "user",
      text: "committed",
      attachments: [],
      createdAt: "2026-01-01T00:00:00Z",
      optimistic: true,
    },
    assistantMessage: {
      id: "assistant-lost",
      role: "assistant",
      text: "",
      status: "failed",
      agentRunId: optimisticRun.id,
      parentUserMessageId: "user-lost",
      generationIds: [],
      toolCalls: [],
      createdAt: "2026-01-01T00:00:00Z",
      partial: false,
      optimistic: true,
    },
    run: optimisticRun,
  });
  const authoritative = {
    ...optimisticRun,
    id: "run-authoritative",
    user_message_id: "user-authoritative",
    assistant_message_id: "assistant-authoritative",
    status: "succeeded" as const,
    execution_epoch: 1,
    last_event_seq: 4,
  };
  useAgentStore.getState().applySnapshot("session-uncertain", {
    items: [
      {
        id: "user-authoritative",
        conversation_id: "conversation-1",
        role: "user",
        content: { source: "agent", text: "committed" },
        intent: "agent",
        status: null,
        parent_message_id: null,
        created_at: "2026-01-01T00:00:00Z",
      },
      {
        id: "assistant-authoritative",
        conversation_id: "conversation-1",
        role: "assistant",
        content: { source: "agent", text: "done", agent_run_id: authoritative.id },
        intent: "agent",
        status: "succeeded",
        parent_message_id: "user-authoritative",
        created_at: "2026-01-01T00:00:01Z",
      },
    ],
    runs: [authoritative],
    next_cursor: null,
    generations: [],
    completions: [],
    images: [],
  });
  assert.deepEqual(
    useAgentStore.getState().messagesBySession["session-uncertain"].map(
      (message: AgentMessage) => message.id,
    ),
    ["user-authoritative", "assistant-authoritative"],
  );
  assert.equal(useAgentStore.getState().runsById[optimisticRun.id], undefined);
});
