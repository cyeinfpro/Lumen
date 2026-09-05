import assert from "node:assert/strict";
import test from "node:test";
import { agentContentScrollAction, observeAgentContentResize, preferredAgentScrollBehavior } from "./agentScrollBehavior.ts";

test("Agent scroll policy gives prepend and reading history priority over following output", () => {
  const base = { hasContent: true, hasPrependAnchor: false, pinned: false, newLocalSubmission: false };
  assert.equal(agentContentScrollAction(base), "notify");
  assert.equal(agentContentScrollAction({ ...base, pinned: true }), "latest");
  assert.equal(agentContentScrollAction({ ...base, newLocalSubmission: true }), "latest");
  assert.equal(agentContentScrollAction({ ...base, hasPrependAnchor: true, newLocalSubmission: true }), "prepend");
  assert.equal(agentContentScrollAction({ ...base, hasContent: false }), "none");
});

test("JS scroll behavior respects current reduced-motion preference and SSR", () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
  let reduce = false;
  try {
    Object.defineProperty(globalThis, "window", { configurable: true, value: {
      matchMedia: (query: string) => {
        assert.equal(query, "(prefers-reduced-motion: reduce)");
        return { matches: reduce };
      },
    } });
    assert.equal(preferredAgentScrollBehavior(true), "smooth");
    assert.equal(preferredAgentScrollBehavior(false), "auto");
    reduce = true;
    assert.equal(preferredAgentScrollBehavior(true), "auto");
    Reflect.deleteProperty(globalThis, "window");
    assert.equal(preferredAgentScrollBehavior(false), "auto");
  } finally {
    if (descriptor) Object.defineProperty(globalThis, "window", descriptor);
    else Reflect.deleteProperty(globalThis, "window");
  }
});

test("content observer follows disclosure/image and viewport resizing only when pinned without a prepend", () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "ResizeObserver");
  const observed: Element[] = [];
  let resize = () => {};
  let disconnected = false;
  let pinned = true;
  let prepending = false;
  const scrolls: ScrollToOptions[] = [];
  const root = { scrollHeight: 1000, scrollTo: (options: ScrollToOptions) => { scrolls.push(options); } } as unknown as HTMLElement;
  const content = {} as HTMLElement;
  class Observer {
    constructor(callback: () => void) { resize = callback; }
    observe(element: Element) { observed.push(element); }
    disconnect() { disconnected = true; }
  }
  try {
    Object.defineProperty(globalThis, "ResizeObserver", { configurable: true, value: Observer });
    const cleanup = observeAgentContentResize({ root, content, canFollow: () => pinned && !prepending });
    assert.deepEqual(observed, [content, root]);
    resize();
    assert.deepEqual(scrolls, [{ top: 1000, behavior: "auto" }]);
    pinned = false;
    resize();
    assert.equal(scrolls.length, 1);
    pinned = true;
    prepending = true;
    resize();
    assert.equal(scrolls.length, 1);
    prepending = false;
    resize();
    assert.equal(scrolls.length, 2);
    cleanup();
    assert.equal(disconnected, true);
    Reflect.deleteProperty(globalThis, "ResizeObserver");
    observeAgentContentResize({ root, content, canFollow: () => true })();
  } finally {
    if (descriptor) Object.defineProperty(globalThis, "ResizeObserver", descriptor);
    else Reflect.deleteProperty(globalThis, "ResizeObserver");
  }
});
