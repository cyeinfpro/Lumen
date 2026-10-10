import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import ts from "typescript";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const directory = "src/components/ui/primitives/";
function load(file, mocks = {}) {
  const source = fs.readFileSync(path.join(root, directory, file), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const compiledModule = { exports: {} };
  new Function("require", "module", "exports", compiled)(
    (name) => {
      if (!(name in mocks)) throw new Error(`Unexpected import: ${name}`);
      return mocks[name];
    }, compiledModule, compiledModule.exports,
  );
  return compiledModule.exports;
}
const geometry = load("notificationPlacement.ts");
const rect = (left, top, right, bottom) => ({ left, top, right, bottom, width: right - left, height: bottom - top });
const screen = rect(0, 0, 375, 812);
const place = (overrides = {}) => geometry.calculateNotificationPlacement({
  viewport: screen, obstacles: [], preferredWidth: 375, ...overrides,
});

test("notifications follow actual composer growth rather than a tabbar constant", () => {
  const small = place({ obstacles: [rect(12, 650, 363, 750), rect(0, 756, 375, 812)] });
  const expanded = place({ obstacles: [rect(12, 480, 363, 750), rect(0, 756, 375, 812)] });
  assert.equal(small.bottom, 638);
  assert.equal(expanded.bottom, 468);
  assert.equal(expanded.width, 351);
  assert.equal(expanded.maxHeight, 456);
});

test("desktop notifications ignore non-overlapping side panels and zero-height obstacles", () => {
  const result = place({
    viewport: rect(0, 0, 1440, 900), preferredWidth: 320, gap: 16,
    obstacles: [rect(0, 100, 300, 900), rect(1000, 400, 1400, 400), rect(0, 950, 1440, 1000)],
  });
  assert.deepEqual(result, { left: 1104, width: 320, bottom: 884, maxHeight: 868 });
});

test("modal footer geometry bounds a long stack inside the dialog", () => {
  const result = place({
    viewport: rect(0, 0, 1440, 900), owner: rect(400, 160, 1040, 740),
    preferredWidth: 320, gap: 16, obstacles: [rect(400, 650, 1040, 740)],
  });
  assert.deepEqual(result, { left: 704, width: 320, bottom: 634, maxHeight: 458 });
});

test("keyboard, visual viewport offsets and safe-area bounds are respected", () => {
  const result = place({
    viewport: rect(10, 108, 365, 480), owner: rect(0, 60, 375, 812),
    obstacles: [rect(0, 420, 375, 812)],
  });
  assert.equal(result.left, 22);
  assert.equal(result.width, 331);
  assert.equal(result.bottom, 408);
  assert.equal(result.maxHeight, 288);
});

test("obstacle ordering is irrelevant and a fully occupied viewport never yields negative sizes", () => {
  const obstacles = [rect(0, 700, 375, 812), rect(0, 600, 375, 680), rect(0, 640, 375, 720)];
  assert.deepEqual(place({ obstacles }), place({ obstacles: [...obstacles].reverse() }));
  const result = place({ viewport: rect(0, 0, 20, 20), obstacles: [rect(0, 0, 20, 20)] });
  assert.equal(result.width, 0);
  assert.equal(result.maxHeight, 0);
});

function dom(t) {
  const events = new Map();
  const timers = new Map();
  const frames = new Map();
  let nextId = 1;
  const document = { activeElement: null };
  class Element {
    constructor(tag = "DIV", bounds = rect(0, 0, 375, 812)) {
      this.tagName = tag;
      this.bounds = bounds;
      this.children = [];
      this.parentElement = null;
      this.dataset = {};
      this.attributes = new Map();
      this.inert = false;
      this.tabIndex = 0;
      this.style = { setProperty(key, value) { this[key] = value; } };
    }
    get isConnected() { return this === document.body || Boolean(this.parentElement?.isConnected); }
    get offsetWidth() { return this.getBoundingClientRect().width; }
    get offsetHeight() { return this.getBoundingClientRect().height; }
    append(child) { child.remove(); child.parentElement = this; this.children.push(child); }
    remove() {
      if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
      this.parentElement = null;
    }
    contains(child) { return this === child || this.children.some((node) => node.contains(child)); }
    setAttribute(key, value) { this.attributes.set(key, value); }
    getAttribute(key) { return this.attributes.get(key) ?? null; }
    hasAttribute(key) { return this.attributes.has(key); }
    removeAttribute(key) { this.attributes.delete(key); }
    getBoundingClientRect() {
      if ("lumenToastHost" in this.dataset && this.parentElement) return this.parentElement.getBoundingClientRect();
      return this.bounds;
    }
    getClientRects() { return [this.getBoundingClientRect()]; }
    matches() { return false; }
    closest(selector) {
      if (selector === "[data-lumen-modal-layer]" && this.layer) return this;
      if (selector !== "[data-lumen-modal-layer]" && (this.inert || this.getAttribute("aria-hidden") === "true")) return this;
      return this.parentElement?.closest(selector) ?? null;
    }
    querySelectorAll(selector) {
      return this.children.flatMap((child) => [
        ...((selector.includes("toast-obstacle") ? child.obstacle : child.tagName === "BUTTON") ? [child] : []),
        ...child.querySelectorAll(selector),
      ]);
    }
    focus() { document.activeElement = this; }
  }
  document.body = new Element("BODY");
  document.createElement = (tag) => new Element(tag.toUpperCase());
  document.querySelectorAll = (selector) => document.body.querySelectorAll(selector);
  document.addEventListener = (name, callback) => {
    if (!events.has(name)) events.set(name, new Set());
    events.get(name).add(callback);
  };
  document.removeEventListener = (name, callback) => events.get(name)?.delete(callback);
  const resizeObservers = [];
  const mutations = [];
  const window = {
    innerWidth: 375, innerHeight: 812,
    getComputedStyle: (element) => ({
      display: "block", visibility: "visible", paddingTop: "0", paddingRight: "0",
      paddingBottom: "0", paddingLeft: "0", ...element.style,
    }),
    setTimeout(callback) { const id = nextId++; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    requestAnimationFrame(callback) { const id = nextId++; frames.set(id, callback); return id; },
    cancelAnimationFrame(id) { frames.delete(id); },
    addEventListener: document.addEventListener, removeEventListener: document.removeEventListener,
  };
  const globals = {
    window, document, HTMLElement: Element, Node: Element,
    ResizeObserver: class {
      constructor(callback) { this.callback = callback; this.observed = new Set(); resizeObservers.push(this); }
      observe(element, options) { assert.equal(options?.box, "border-box"); this.observed.add(element); }
      unobserve(element) { this.observed.delete(element); }
      disconnect() { this.observed.clear(); }
    },
    MutationObserver: class {
      constructor(callback) { this.callback = callback; mutations.push(this); }
      observe() {}
      disconnect() { this.disconnected = true; }
    },
  };
  for (const [key, value] of Object.entries(globals)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
    t.after(() => {
      if (previous) Object.defineProperty(globalThis, key, previous);
      else delete globalThis[key];
    });
  }
  return {
    Element, document, window, events, resizeObservers, mutations,
    flushFrames() { for (const [id, callback] of [...frames]) { frames.delete(id); callback(); } },
    flushTimers() { for (const [id, callback] of [...timers]) { timers.delete(id); callback(); } },
  };
}

test("stable toast host moves across nested modal roots, responds to resize and cleans observers", (t) => {
  const d = dom(t);
  const composer = new d.Element("FORM", rect(0, 650, 375, 812));
  composer.obstacle = true;
  d.document.body.append(composer);
  const viewport = new d.Element();
  let owner = null;
  let changed;
  let cleanup;
  const { useToastPortal } = load("useToastPortal.ts", {
    react: { useState: (init) => [init()], useLayoutEffect: (effect) => { cleanup = effect(); } },
    "./mobile/useModalLayer": {
      getActiveModalRoot: () => owner,
      subscribeActiveModalRoot: (callback) => { changed = callback; return () => { changed = null; }; },
    },
    "./notificationPlacement": geometry,
  });
  const host = useToastPortal({ current: viewport }, true);
  host.append(viewport);
  d.flushFrames();
  assert.equal(host.parentElement, d.document.body);
  assert.equal(viewport.style.bottom, "174px");
  const firstObserver = d.resizeObservers[0];
  assert.ok(firstObserver.observed.has(composer));
  composer.bounds = rect(0, 500, 375, 812);
  firstObserver.callback();
  d.flushFrames();
  assert.equal(viewport.style.bottom, "324px");

  const first = new d.Element("SECTION", rect(0, 200, 375, 800));
  const footer = new d.Element("FOOTER", rect(0, 700, 375, 800));
  footer.obstacle = true;
  first.append(footer);
  d.document.body.append(first);
  owner = first;
  changed();
  assert.equal(host.parentElement, first);
  assert.equal(viewport.style.bottom, "112px");
  assert.equal(firstObserver.observed.size, 0);
  const second = new d.Element("SECTION", rect(0, 300, 375, 790));
  d.document.body.append(second);
  owner = second;
  changed();
  assert.equal(host.parentElement, second);
  owner = first;
  changed();
  assert.equal(host.parentElement, first);
  owner = null;
  changed();
  assert.equal(host.parentElement, d.document.body);
  cleanup();
  assert.equal(host.parentElement, null);
  assert.equal(changed, null);
  assert.ok(d.resizeObservers.every((observer) => observer.observed.size === 0));
  assert.ok(d.mutations.every((observer) => observer.disconnected));
});

test("modal ownership moves overlays before isolation and native Tab handles portal actions", (t) => {
  const d = dom(t);
  let cleanups = [];
  const { useModalLayer: renderModalLayer, getActiveModalRoot, subscribeActiveModalRoot } = load("mobile/useModalLayer.ts", {
    react: {
      useCallback: (callback) => callback, useRef: (current) => ({ current }),
      useEffect: (effect) => { const cleanup = effect(); if (cleanup) cleanups.push(cleanup); },
    },
  });
  const background = new d.Element();
  const trigger = new d.Element("BUTTON");
  background.append(trigger);
  d.document.body.append(background);
  trigger.focus();
  const host = new d.Element();
  const close = new d.Element("BUTTON");
  host.append(close);
  d.document.body.append(host);
  const unsubscribe = subscribeActiveModalRoot(() => (getActiveModalRoot() ?? d.document.body).append(host));
  function open() {
    const layer = new d.Element();
    layer.layer = true;
    const root = new d.Element();
    const first = new d.Element("BUTTON");
    root.append(first);
    layer.append(root);
    d.document.body.append(layer);
    cleanups = [];
    const reactKeyDown = renderModalLayer({ open: true, rootRef: { current: root }, onClose() {} });
    return { layer, root, first, reactKeyDown, cleanup: cleanups.at(-1) };
  }
  const first = open();
  assert.equal(host.parentElement, first.root);
  assert.equal(host.inert, false);
  assert.equal(background.inert, true);
  d.flushTimers();
  first.first.focus();
  const second = open();
  assert.equal(host.parentElement, second.root);
  assert.equal(first.layer.inert, true);
  assert.equal(host.closest("[inert]"), null);
  close.focus();
  const event = { key: "Tab", shiftKey: false, currentTarget: second.root, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; } };
  for (const callback of d.events.get("keydown")) callback(event);
  assert.equal(d.document.activeElement, second.first);
  second.reactKeyDown(event);
  assert.equal(d.document.activeElement, second.first);
  second.cleanup();
  second.layer.remove();
  d.flushTimers();
  assert.equal(host.parentElement, first.root);
  assert.equal(d.document.activeElement, first.first);
  assert.equal(first.layer.inert, false);
  first.cleanup();
  first.layer.remove();
  d.flushTimers();
  assert.equal(host.parentElement, d.document.body);
  assert.equal(background.inert, false);
  assert.equal(d.document.activeElement, trigger);
  unsubscribe();
});
