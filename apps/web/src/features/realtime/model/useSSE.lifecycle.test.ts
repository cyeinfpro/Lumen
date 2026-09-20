import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test, { type TestContext } from "node:test";
import ts from "typescript";
import type { useSSE as UseSSE } from "./useSSE";

type Slot = {
  value?: unknown;
  deps?: readonly unknown[];
  cleanup?: () => void;
  callback?: (...args: unknown[]) => unknown;
};
type Subscriber = {
  subscribedScope: string;
  emit: (scope: string, invocation: { kind: "open" }) => void;
};

function mount(t: TestContext) {
  const descriptors = ["window", "document"].map((key) => Object.getOwnPropertyDescriptor(globalThis, key));
  Object.defineProperty(globalThis, "window", { configurable: true, value: new EventTarget() });
  Object.defineProperty(globalThis, "document", {
    configurable: true, value: Object.assign(new EventTarget(), { visibilityState: "visible" }),
  });
  const slots: Slot[] = [];
  let cursor = 0;
  let subscriptions = 0;
  let releases = 0;
  let reconnects = 0;
  let subscriber: Subscriber | undefined;
  let delivered: { onProtocolIssue?: unknown; onOpen?: unknown } | undefined;
  const next = () => slots[cursor++] ?? (slots[cursor - 1] = {});
  const same = (previous: readonly unknown[] | undefined, current: readonly unknown[]) =>
    previous?.length === current.length && current.every((value, index) => Object.is(value, previous[index]));
  function memo<T>(factory: () => T, deps: readonly unknown[]): T {
    const slot = next();
    if (!same(slot.deps, deps)) { slot.value = factory(); slot.deps = deps; }
    return slot.value as T;
  }
  const runtime = {
    subscribe(value: Subscriber) { subscriber = value; subscriptions += 1; return () => undefined; },
    visibility() {}, online() {}, reconnect() { reconnects += 1; },
  };
  const source = readFileSync(new URL("./useSSE.ts", import.meta.url), "utf8");
  const output = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const compiled = { exports: {} as { useSSE: typeof UseSSE } };
  new Function("require", "module", "exports", output)((id: string) => {
    if (id === "react") return {
      useMemo: memo,
      useCallback: (callback: unknown, deps: readonly unknown[]) => memo(() => callback, deps),
      useRef: (value: unknown) => memo(() => ({ current: value }), []),
      useState(initial: unknown) {
        const slot = next();
        if (!slot.deps) {
          slot.value = typeof initial === "function" ? initial() : initial;
          slot.deps = [];
        }
        return [slot.value, (value: unknown) => { slot.value = value; }];
      },
      useEffectEvent(callback: (...args: unknown[]) => unknown) {
        const slot = next(); slot.callback = callback;
        return (...args: unknown[]) => slot.callback?.(...args);
      },
      useEffect(effect: () => (() => void), deps: readonly unknown[]) {
        const slot = next();
        if (same(slot.deps, deps)) return;
        slot.cleanup?.(); slot.deps = deps; slot.cleanup = effect();
      },
    };
    if (id === "./sseSubscription") return {
      createSSESubscriber: (value: Subscriber) => value,
      dispatchSSECallbackForScope: (_old: string, _current: string, _guard: unknown, callbacks: typeof delivered) => {
        delivered = callbacks;
      },
      recoverSSESnapshotForScope: () => Promise.resolve(),
    };
    if (id === "@/shared/realtime/runtimeRegistry") return {
      acquireRealtimeRuntime: () => ({ runtime }),
      releaseRealtimeRuntime: () => { releases += 1; },
    };
    throw new Error(`Unexpected dependency: ${id}`);
  }, compiled, compiled.exports);
  t.after(() => {
    for (const slot of slots) slot.cleanup?.();
    ["window", "document"].forEach((key, index) => {
      const descriptor = descriptors[index];
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else Reflect.deleteProperty(globalThis, key);
    });
  });
  return {
    render(options: Parameters<typeof UseSSE>[2] = {}, channels = ["user:owner"]) {
      cursor = 0;
      return compiled.exports.useSSE(channels, { custom: () => undefined }, options);
    },
    emit() { subscriber?.emit(subscriber.subscribedScope, { kind: "open" }); return delivered; },
    counts: () => ({ subscriptions, releases, reconnects }),
  };
}

test("inline protocol callbacks and ordinary renders do not reacquire the runtime lease", (t) => {
  const mounted = mount(t);
  mounted.render({ onProtocolIssue: () => undefined });
  const latestProtocol = () => undefined;
  const latestOpen = () => undefined;
  for (let index = 0; index < 5; index += 1) {
    mounted.render({ onProtocolIssue: () => undefined, onOpen: () => undefined });
  }
  const view = mounted.render({ onProtocolIssue: latestProtocol, onOpen: latestOpen });
  assert.deepEqual(mounted.counts(), { subscriptions: 1, releases: 0, reconnects: 0 });
  assert.equal(mounted.emit()?.onProtocolIssue, latestProtocol);
  assert.equal(mounted.emit()?.onOpen, latestOpen);
  view.reconnect();
  assert.equal(mounted.counts().reconnects, 1);
});

test("scope and channel changes still clean up the previous subscription", (t) => {
  const mounted = mount(t);
  mounted.render({ scopeIdentity: "owner:1" });
  mounted.render({ scopeIdentity: "owner:2" });
  mounted.render({ scopeIdentity: "owner:2" }, ["user:other"]);
  assert.deepEqual(mounted.counts(), { subscriptions: 3, releases: 2, reconnects: 0 });
});

test("transport retry and visibility policies remain reactive subscription inputs", (t) => {
  const mounted = mount(t);
  mounted.render({ maxRetryCount: 3, hiddenCloseDelayMs: 100 });
  mounted.render({ maxRetryCount: 4, hiddenCloseDelayMs: 100 });
  mounted.render({ maxRetryCount: 4, hiddenCloseDelayMs: 200 });
  assert.deepEqual(mounted.counts(), { subscriptions: 3, releases: 2, reconnects: 0 });
});
