import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test, { before, after, beforeEach, afterEach, type TestContext } from "node:test";
import ts from "typescript";
import "../../../store/chat/moduleResolution.test-helper.mjs";
import type { AgentMessageList } from "../model/contracts";
import type { useAgentSnapshotPolling as PollingHook } from "./useAgentSnapshotPolling";

const { refreshAgentSnapshot } = await import(new URL("./agentSnapshot.ts", import.meta.url).href);
const { useAgentStore } = await import(new URL("../../../store/agent/useAgentStore.ts", import.meta.url).href);
const { transitionPrivateIdentity, getPrivateIdentitySnapshot } = await import(new URL("../../../lib/auth/privateIdentityEpoch.ts", import.meta.url).href);

// The production store deliberately has no shared SSR singleton. These tests
// exercise browser recovery, so establish a browser before selecting a session.
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
before(() => Object.defineProperty(globalThis, "window", { configurable: true, value: {} }));
after(() => {
  if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
  else Reflect.deleteProperty(globalThis, "window");
});

beforeEach(() => {
  const identity = transitionPrivateIdentity("nullable-test-user");
  useAgentStore.getState().resetForIdentity(identity);
  useAgentStore.getState().setCurrentSession("session-2");
});
afterEach(() => {
  transitionPrivateIdentity(null);
  useAgentStore.getState().resetForIdentity(getPrivateIdentitySnapshot());
});

function snapshot(id = "message-recovered"): AgentMessageList {
  return {
    items: [{
      id, conversation_id: "conversation-2", role: "user",
      content: { source: "agent", text: "Recovered idle session message" },
      intent: "agent", status: null, parent_message_id: null,
      created_at: "2026-09-13T00:00:00Z",
    }],
    runs: [], next_cursor: null, generations: [], completions: [], images: [],
  };
}

function json(value: unknown) {
  return new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });
}

function stubSnapshots(t: TestContext) {
  return t.mock.method(globalThis, "fetch", async (url: string) =>
    json(url.includes("/active-run") ? null : snapshot()),
  );
}

const settle = () => new Promise<void>((resolve) => setImmediate(resolve));

test("actual snapshot recovery applies messages when active-run is null", async (t) => {
  const fetchMock = stubSnapshots(t);
  await refreshAgentSnapshot();
  const state = useAgentStore.getState();
  assert.equal(state.messagesBySession["session-2"][0].id, "message-recovered");
  assert.deepEqual(state.runsById, {});
  assert.equal(fetchMock.mock.callCount(), 2);
});

test("nullable recovery keeps the session-switch fence", async (t) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  t.mock.method(globalThis, "fetch", async (url: string) => {
    await gate;
    return json(url.includes("/active-run") ? null : snapshot());
  });
  const pending = refreshAgentSnapshot();
  useAgentStore.getState().setCurrentSession("session-3");
  release();
  await pending;
  assert.deepEqual(useAgentStore.getState().messagesBySession, {});
});

test("nullable recovery does not apply old-account responses", async (t) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  t.mock.method(globalThis, "fetch", async (url: string) => {
    await gate;
    return json(url.includes("/active-run") ? null : snapshot());
  });
  const pending = refreshAgentSnapshot();
  transitionPrivateIdentity("different-user");
  release();
  await assert.rejects(pending, (error: unknown) =>
    error instanceof Error && "code" in error && error.code === "identity_changed",
  );
  assert.deepEqual(useAgentStore.getState().messagesBySession, {});
});

test("no selected session makes no recovery requests", async (t) => {
  const fetchMock = stubSnapshots(t);
  useAgentStore.getState().setCurrentSession(null);
  await refreshAgentSnapshot();
  assert.equal(fetchMock.mock.callCount(), 0);
});

function mountPolling(t: TestContext, refresh: (signal: AbortSignal) => Promise<void>) {
  const descriptors = ["window", "document"].map((name) => Object.getOwnPropertyDescriptor(globalThis, name));
  const timers = new Map<number, TimerHandler>();
  let nextTimer = 0;
  const fakeWindow = Object.assign(new EventTarget(), {
    setTimeout(fn: TimerHandler) { timers.set(++nextTimer, fn); return nextTimer; },
    clearTimeout(id: number) { timers.delete(id); },
  });
  const fakeDocument = Object.assign(new EventTarget(), { visibilityState: "visible" });
  Object.defineProperty(globalThis, "window", { configurable: true, value: fakeWindow });
  Object.defineProperty(globalThis, "document", { configurable: true, value: fakeDocument });
  let cleanup: (() => void) | undefined;
  t.after(() => {
    cleanup?.();
    ["window", "document"].forEach((name, index) => {
      const descriptor = descriptors[index];
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else Reflect.deleteProperty(globalThis, name);
    });
  });
  // Run the production hook effect, mocking only React's mounting lifecycle.
  const source = readFileSync(new URL("./useAgentSnapshotPolling.ts", import.meta.url), "utf8");
  const output = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const compiled = { exports: {} as { useAgentSnapshotPolling: typeof PollingHook } };
  new Function("require", "module", "exports", output)((id: string) => {
    assert.equal(id, "react");
    return { useEffect(effect: () => (() => void) | undefined) { cleanup = effect(); } };
  }, compiled, compiled.exports);
  const statuses: string[] = [];
  compiled.exports.useAgentSnapshotPolling({
    sessionId: "session-2", intervalMs: 30_000, refresh,
    setStatus(status) { statuses.push(status); },
  });
  return { fakeWindow, fakeDocument, statuses, timers };
}

test("production polling, focus and visibility recovery accept idle null without error", async (t) => {
  const fetchMock = stubSnapshots(t);
  const mounted = mountPolling(t, refreshAgentSnapshot);
  await settle();
  assert.equal(useAgentStore.getState().messagesBySession["session-2"][0].id, "message-recovered");
  assert.deepEqual(mounted.statuses, []);
  assert.equal(mounted.timers.size, 1);
  mounted.fakeWindow.dispatchEvent(new Event("focus"));
  await settle();
  mounted.fakeDocument.dispatchEvent(new Event("visibilitychange"));
  await settle();
  assert.equal(fetchMock.mock.callCount(), 6);
  assert.deepEqual(mounted.statuses, []);
});

test("production polling still reports genuine snapshot errors", async (t) => {
  t.mock.method(globalThis, "fetch", async () => json({ invalid: true }));
  const mounted = mountPolling(t, refreshAgentSnapshot);
  await settle();
  assert.deepEqual(mounted.statuses, ["error"]);
  assert.deepEqual(useAgentStore.getState().messagesBySession, {});
});

test("production polling ignores caller cancellation", async (t) => {
  const mounted = mountPolling(t, async () => { throw new DOMException("cancelled", "AbortError"); });
  await settle();
  assert.deepEqual(mounted.statuses, []);
});
