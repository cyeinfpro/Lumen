import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { Component } from "react";
import ts from "typescript";

const jsx = (type, props) => ({ type, props: props ?? {} });
const runtime = { jsx, jsxs: jsx, Fragment: "Fragment" };
function load(path, overrides = {}) {
  const source = readFileSync(new URL(`../src/components/${path}`, import.meta.url), "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    fileName: path,
  }).outputText;
  const compiled = { exports: {} };
  const defaults = {
    "react/jsx-runtime": runtime,
    "@/lib/utils": { cn: (...values) => values.filter(Boolean).join(" ") },
    "lucide-react": new Proxy({}, { get: (_, key) => key }),
    "./Button": { Button: "Button" },
    "@/components/ui/primitives": { ErrorState: "ErrorState", Button: "Button", Dialog: "Dialog", IconButton: "IconButton", Kbd: "Kbd", Badge: "Badge" },
    "next/link": { default: "Link" },
  };
  new Function("require", "module", "exports", output)((id) => {
    if (id in overrides) return overrides[id];
    if (id in defaults) return defaults[id];
    throw new Error(`Unmocked module: ${id}`);
  }, compiled, compiled.exports);
  return compiled.exports;
}
function find(node, predicate) {
  if (Array.isArray(node)) return node.map((child) => find(child, predicate)).find(Boolean) ?? null;
  if (!node || typeof node !== "object") return null;
  return predicate(node) ? node : find(node.props?.children, predicate);
}
function hooks() {
  const slots = [];
  let cursor = 0;
  let dirty = false;
  let effects = [];
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial;
      return [slots[index], (next) => {
        slots[index] = typeof next === "function" ? next(slots[index]) : next;
        dirty = true;
      }];
    },
    useRef(initial) {
      const index = cursor++;
      return slots[index] ??= { current: initial };
    },
    useId() { return `id-${cursor++}`; },
    useCallback(callback) { return callback; },
    useMemo(callback) { return callback(); },
    useSyncExternalStore(_subscribe, snapshot) { return snapshot(); },
    useEffect(callback, deps) {
      const index = cursor++;
      const previous = slots[index];
      if (!previous || deps.some((value, i) => !Object.is(previous.deps[i], value))) {
        effects.push(() => {
          previous?.cleanup?.();
          slots[index] = { deps, cleanup: callback() };
        });
      }
    },
  };
  return {
    react,
    render(component, props, attach = () => {}) {
      let tree;
      let attempts = 0;
      do {
        dirty = false;
        cursor = 0;
        effects = [];
        tree = component(props);
        assert.ok(++attempts < 10, "render must settle");
      } while (dirty);
      attach(tree);
      const scheduled = effects;
      effects = [];
      scheduled.forEach((effect) => effect());
      return tree;
    },
  };
}

test("ErrorBoundary resets locally and on meaningful keys without looping on persistent errors", () => {
  const { ErrorBoundary } = load("ErrorBoundary.tsx", { react: { Component } });
  let resets = 0;
  let retry;
  const props = { children: "healthy sibling content", resetKeys: ["/agent", 1], fallback: (reset) => {
    retry = reset;
    return "local recovery";
  } };
  const boundary = new ErrorBoundary(props);
  boundary.setState = (state) => { resets++; boundary.state = { ...boundary.state, ...state }; };
  boundary.state = ErrorBoundary.getDerivedStateFromError(new Error("render failure"));
  assert.equal(boundary.render(), "local recovery");
  retry();
  assert.equal(boundary.render(), "healthy sibling content");
  boundary.state = ErrorBoundary.getDerivedStateFromError(new Error("persistent chunk failure"));
  boundary.componentDidUpdate({ ...props, resetKeys: ["/agent", 1] });
  assert.equal(resets, 1);
  boundary.props = { ...props, resetKeys: ["/projects", 1] };
  boundary.componentDidUpdate(props);
  assert.equal(resets, 2);
  boundary.state = ErrorBoundary.getDerivedStateFromError(new Error("persistent chunk failure"));
  boundary.componentDidUpdate(boundary.props);
  assert.equal(resets, 2, "same reset keys must not retry failed chunks indefinitely");
});

test("LumenAppShell isolates each optional feature with contextual recovery and route/object reset keys", () => {
  const state = hooks();
  let pathname = "/agent";
  const ui = {
    lightbox: { open: false, imageId: "image-a", identityEpoch: 1 }, taskTray: { minimized: true },
    closeLightbox() { ui.lightbox.open = false; },
  };
  const inpaint = {
    open: false, source: { imageId: "image-a" }, identityEpoch: 1, submitting: false,
    drafts: { "image-a": "preserved edit" },
    close() { if (!inpaint.submitting) inpaint.open = false; },
  };
  const modules = {
    react: state.react,
    "react-dom": { createPortal: (child) => child },
    "next/navigation": { usePathname: () => pathname },
    "next/dynamic": { default: () => "DynamicIsland" },
    "@/store/useUiStore": { useUiStore: (select) => select(ui) },
    "@/store/useInpaintStore": { useInpaintStore: (select) => select(inpaint) },
    "@/components/ErrorBoundary": { ErrorBoundary: "ErrorBoundary" },
    "@/components/ui/shell/PageTransitions": { PageTransitions: "PageTransitions" },
  };
  for (const name of ["IdleRouteWarmup", "OfflineBanner", "QueryProvider", "RuntimeDefaultsBootstrap", "ServiceWorkerRegister", "SSEProvider", "SystemUpgradeBanner"]) {
    modules[`@/components/${name}`] = { [name]: name };
  }
  const { LumenAppShell } = load("LumenAppShell.tsx", modules);
  const render = () => state.render(LumenAppShell, { children: "workspace", initialRuntimeDefaults: {} });
  const island = (tree, title) => find(tree, (node) => node.props.title === title);
  let tree = render();
  for (const title of ["图片预览", "局部编辑", "任务列表", "命令面板"]) {
    const child = island(tree, title);
    const boundary = child.type(child.props);
    assert.equal(boundary.type, "ErrorBoundary");
    let retries = 0;
    const recovery = boundary.props.fallback(() => retries++);
    assert.equal(recovery.props.title, `${title}不可用`);
    assert.equal(recovery.props.retryLabel, `重试${title}`);
    recovery.props.onRetry();
    assert.equal(retries, 1);
    assert.ok(find(recovery.props.secondaryAction, (node) => node.props.children === "刷新页面").props.onClick,
      "cached chunk failures have explicit refresh");
  }
  const preview = island(tree, "图片预览");
  ui.lightbox.open = true;
  preview.props.onDismiss();
  assert.equal(ui.lightbox.open, false, "recovery can restore mobile navigation without a reload");
  inpaint.submitting = true;
  tree = render();
  assert.equal(island(tree, "局部编辑").props.dismissDisabled, true);
  assert.deepEqual(inpaint.drafts, { "image-a": "preserved edit" });
  const before = island(tree, "局部编辑").props.resetKeys;
  inpaint.source.imageId = "image-b";
  pathname = "/projects";
  tree = render();
  const after = island(tree, "局部编辑").props.resetKeys;
  assert.notDeepEqual(before, after);
  assert.deepEqual(after, ["/projects", false, "image-b", 1]);
  assert.ok(find(tree, (node) => node.props.href === "#lumen-workspace"));
  assert.ok(find(tree, (node) => node.props["data-lumen-app-shell"] !== undefined));
});

test("shared empty and error states retain distinct semantics and a local retry", () => {
  const { EmptyState } = load("ui/primitives/EmptyState.tsx");
  const { ErrorState } = load("ui/primitives/ErrorState.tsx");
  let retries = 0;
  assert.equal(EmptyState({ title: "No results" }).props.role, undefined);
  const error = ErrorState({ title: "Load failed", onRetry: () => retries++ });
  assert.equal(error.props.role, "alert");
  find(error, (node) => node.type === "Button").props.onClick();
  assert.equal(retries, 1);
});

function confirmHarness() {
  const state = hooks();
  const { ConfirmDialog } = load("ui/primitives/ConfirmDialog.tsx", {
    react: state.react,
    "./Dialog": { Dialog: Object.assign(() => {}, { Header: "Header", Body: "Body", Footer: "Footer" }) },
  });
  const props = { open: true, title: "Delete resource A?", resetKey: "a", onOpenChange() {}, onConfirm() {} };
  return { props, render: () => state.render(ConfirmDialog, props) };
}
const confirmButton = (tree) => find(tree, (node) => node.type === "Button" && node.props.loading !== undefined);

test("ConfirmDialog renders internal pending, blocks duplicate/close, catches rejection and permits retry after cooldown", async (t) => {
  let now = 10000;
  t.mock.method(Date, "now", () => now);
  const harness = confirmHarness();
  let reject;
  let confirms = 0;
  let closes = 0;
  harness.props.onOpenChange = () => closes++;
  harness.props.onConfirm = () => { confirms++; return new Promise((_, fail) => { reject = fail; }); };
  const initial = harness.render();
  const pending = confirmButton(initial).props.onClick();
  await confirmButton(initial).props.onClick();
  const busy = harness.render();
  assert.equal(busy.props["aria-busy"], true);
  assert.equal(confirmButton(busy).props.loading, true);
  assert.equal(find(busy, (node) => node.type === "Button" && node.props.disabled).props.disabled, true);
  busy.props.onClose();
  assert.equal(closes, 0);
  assert.equal(confirms, 1);
  reject(new Error("secret server detail"));
  await pending;
  const failed = harness.render();
  assert.equal(failed.props["aria-busy"], false);
  const alert = find(failed, (node) => node.props.role === "alert");
  assert.ok(alert);
  assert.ok(failed.props["aria-describedby"].includes(alert.props.id));
  assert.doesNotMatch(alert.props.children, /secret server detail/);
  await confirmButton(failed).props.onClick();
  assert.equal(confirms, 1, "original cooldown still applies after rejection");
  now += 1201;
  harness.props.onConfirm = () => { confirms++; };
  await confirmButton(harness.render()).props.onClick();
  assert.equal(confirms, 2);
  assert.equal(find(harness.render(), (node) => node.props.role === "alert"), null);
});

test("ConfirmDialog resets errors on resource/reopen and ignores an old object's late rejection", async () => {
  const harness = confirmHarness();
  harness.props.onConfirm = async () => { throw new Error("failed"); };
  await confirmButton(harness.render()).props.onClick();
  assert.ok(find(harness.render(), (node) => node.props.role === "alert"));
  harness.props.resetKey = "b";
  harness.props.title = "Delete resource B?";
  assert.equal(find(harness.render(), (node) => node.props.role === "alert"), null);
  harness.props.open = false;
  harness.render();
  harness.props.open = true;
  let reject;
  harness.props.onConfirm = () => new Promise((_, fail) => { reject = fail; });
  const pending = confirmButton(harness.render()).props.onClick();
  harness.props.resetKey = "c";
  harness.render();
  reject(new Error("old resource failed"));
  await pending;
  assert.equal(find(harness.render(), (node) => node.props.role === "alert"), null);
});

test("ConfirmDialog keeps errors across fresh description nodes but clears them on reopening", async () => {
  const harness = confirmHarness();
  harness.props.onConfirm = async () => { throw new Error("failed"); };
  await confirmButton(harness.render()).props.onClick();
  harness.props.description = jsx("p", { children: "Same object impact" });
  assert.ok(find(harness.render(), (node) => node.props.role === "alert"));
  harness.props.open = false;
  harness.render();
  harness.props.open = true;
  assert.equal(find(harness.render(), (node) => node.props.role === "alert"), null);
});

test("modal focus candidates exclude every negative tabindex, disabled, inherited hidden and unrendered elements", (t) => {
  const document = { activeElement: null };
  t.mock.method(globalThis, "setTimeout", (callback) => { callback(); return 0; });
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  globalThis.window = { getComputedStyle: (element) => ({ visibility: element.visibility ?? "visible" }) };
  globalThis.document = document;
  t.after(() => { globalThis.window = previousWindow; globalThis.document = previousDocument; });
  const candidate = (options = {}) => ({
    tabIndex: 0,
    closest: () => null,
    getAttribute: () => null,
    hasAttribute: () => true,
    getClientRects: () => [1],
    matches: () => false,
    focus() { document.activeElement = this; },
    ...options,
  });
  const first = candidate();
  const last = candidate();
  const rejected = [
    candidate({ tabIndex: -1 }), candidate({ tabIndex: -2 }),
    candidate({ matches: () => true }),
    candidate({ closest: () => ({ inert: true }) }),
    candidate({ closest: () => ({ hidden: true }) }),
    candidate({ visibility: "hidden" }), candidate({ visibility: "collapse" }),
    candidate({ getClientRects: () => [] }),
  ];
  const root = { querySelectorAll: () => [first, ...rejected, last], contains: () => true, focus() { document.activeElement = root; } };
  const { trapModalFocus } = load("ui/primitives/mobile/useModalLayer.ts", { react: {} });
  let prevented = false;
  first.focus();
  trapModalFocus({ key: "Tab", shiftKey: true, preventDefault() { prevented = true; } }, root);
  assert.equal(document.activeElement, last);
  assert.equal(prevented, true);
  trapModalFocus({ key: "Tab", shiftKey: false, preventDefault() {} }, root);
  assert.equal(document.activeElement, first);
  root.querySelectorAll = () => rejected;
  trapModalFocus({ key: "Tab", shiftKey: false, preventDefault() {} }, root);
  assert.equal(document.activeElement, root);
});

function paletteHarness(t) {
  const state = hooks();
  const events = new Map();
  const media = { matches: true, addEventListener(_name, callback) { this.update = callback; }, removeEventListener() {} };
  let scrollTop = 0;
  const viewport = {
    get scrollTop() { return scrollTop; },
    set scrollTop(value) { scrollTop = Math.max(0, value); },
    clientTop: 0, clientHeight: 200, offsetHeight: 200,
    getBoundingClientRect: () => ({ top: 100, bottom: 300, height: 200 }),
    contains: () => true,
  };
  let tree;
  let observer;
  const restore = new Map();
  for (const [name, value] of Object.entries({
    window: {
      matchMedia: () => media,
      addEventListener: (name, callback) => events.set(name, callback),
      removeEventListener: (name) => events.delete(name),
      setTimeout: () => 1, clearTimeout() {},
    },
    document: { activeElement: null, getElementById(id) {
      const list = find(tree, (node) => node.props.role === "listbox");
      const options = list?.props.children;
      const index = Array.isArray(options) ? options.findIndex((option) => option.props.id === id) : -1;
      return index < 0 ? null : { getBoundingClientRect: () => ({
        top: 100 + index * 56 - viewport.scrollTop,
        bottom: 156 + index * 56 - viewport.scrollTop,
      }) };
    } },
    HTMLElement: class {}, HTMLButtonElement: class {},
    ResizeObserver: class { constructor(callback) { observer = callback; } observe() {} disconnect() {} },
  })) {
    restore.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  t.after(() => {
    for (const [name, descriptor] of restore) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  });
  const { CommandPalette } = load("ui/CommandPalette.tsx", {
    react: state.react,
    "next/navigation": { usePathname: () => "/", useRouter: () => ({ push() {} }) },
    "@/store/useUiStore": { useUiStore: (select) => select({ navVisibility: {} }) },
    "@/components/ui/primitives/mobile/BottomSheet": { BottomSheet: "BottomSheet" },
    "@/components/ui/shell/navigation": {
      getAppNavItems: () => ["studio", "agent", "video", "projects", "assets", "me"].map((key) => ({ key, label: key, detail: key, route: `/${key}`, keywords: [] })),
      getActiveNavKey: () => "studio", isSameRoute: () => false,
    },
  });
  const render = () => state.render(CommandPalette, {}, (next) => {
    tree = next;
    find(tree, (node) => node.props.role === "listbox").props.ref.current = viewport;
  });
  const key = (name, flags = {}) => ({ key: name, repeat: false, nativeEvent: { isComposing: false }, preventDefault() { this.defaultPrevented = true; }, ...flags });
  return { render, events, key, viewport, media, resize: () => observer?.() };
}

test("CommandPalette ignores composition and repeat before local Escape and global command K", (t) => {
  const harness = paletteHarness(t);
  let tree = harness.render();
  for (const flags of [{ isComposing: true }, { repeat: true }]) {
    harness.events.get("keydown")(harness.key("k", { ctrlKey: true, ...flags }));
    assert.equal(harness.render().props.open, false);
  }
  harness.events.get("keydown")(harness.key("k", { ctrlKey: true }));
  tree = harness.render();
  assert.equal(tree.props.open, true);
  for (const flags of [{ nativeEvent: { isComposing: true } }, { repeat: true }]) {
    tree.props.onKeyDown(harness.key("Escape", flags));
    tree = harness.render();
    assert.equal(tree.props.open, true);
  }
  tree.props.onKeyDown(harness.key("Escape"));
  assert.equal(harness.render().props.open, false);
});

test("CommandPalette reveals active options for arrows, query replacement, resize and responsive remount", (t) => {
  const harness = paletteHarness(t);
  harness.render();
  harness.events.get("lumen:command-palette-open")();
  let tree = harness.render();
  for (let i = 0; i < 10; i++) {
    tree.props.onKeyDown(harness.key("ArrowDown", { repeat: i > 0 }));
    tree = harness.render();
  }
  assert.ok(harness.viewport.scrollTop > 300);
  let input = find(tree, (node) => node.props.role === "combobox");
  assert.equal(input.props["aria-label"], "搜索命令或页面");
  const active = find(tree, (node) => node.props.id === input.props["aria-activedescendant"]);
  assert.equal(active.props["aria-selected"], true);
  assert.equal(active.props.tabIndex, -1);
  harness.viewport.scrollTop = 0;
  harness.resize();
  assert.ok(harness.viewport.scrollTop > 300);
  harness.viewport.scrollTop = 0;
  harness.media.matches = false;
  harness.media.update();
  tree = harness.render();
  assert.equal(tree.type, "BottomSheet");
  assert.ok(harness.viewport.scrollTop > 300, "responsive remount reveals current selection");
  input = find(tree, (node) => node.props.role === "combobox");
  input.props.onChange({ target: { value: "settings" } });
  tree = harness.render();
  assert.equal(harness.viewport.scrollTop, 0, "first filtered option is revealed");
  input = find(tree, (node) => node.props.role === "combobox");
  input.props.onChange({ target: { value: "no-such-command" } });
  tree = harness.render();
  assert.equal(find(tree, (node) => node.props.role === "combobox").props["aria-activedescendant"], undefined);
});
