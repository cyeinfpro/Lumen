import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = (file) => fs.readFileSync(path.join(root, file), "utf8");
const jsx = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
const loose = new Proxy({}, { get: () => () => undefined });
function load(file, mocks = {}, globals = {}, extra = "") {
  const output = ts.transpileModule(source(file) + extra, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
    fileName: file,
  }).outputText;
  const cjsModule = { exports: {} };
  const require = (name) => mocks[name] ?? (name === "react/jsx-runtime" ? jsx : loose);
  new Function("require", "module", "exports", ...Object.keys(globals), output)(require, cjsModule, cjsModule.exports, ...Object.values(globals));
  return cjsModule.exports;
}
function hooks() {
  const slots = [];
  const effects = [];
  let cursor = 0;
  let queued = [];
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial;
      return [slots[index], (next) => { slots[index] = typeof next === "function" ? next(slots[index]) : next; }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = { current: initial };
      return slots[index];
    },
    useEffect(callback, deps) {
      const index = cursor++;
      const previous = effects[index];
      if (!previous || deps.some((value, i) => !Object.is(value, previous.deps[i]))) {
        queued.push(() => { previous?.cleanup?.(); effects[index] = { deps, cleanup: callback() }; });
      }
    },
    useMemo: (callback) => callback(),
    useCallback: (callback) => callback,
    useId: () => "test-id",
  };
  return {
    react, slots,
    render(callback) { cursor = 0; queued = []; const result = callback(); queued.forEach((run) => run()); return result; },
    unmount() { effects.forEach((effect) => effect?.cleanup?.()); },
  };
}
function timers() {
  let id = 0;
  const pending = new Map();
  return {
    pending,
    setTimeout: (callback, delay) => { pending.set(++id, { callback, delay }); return id; },
    clearTimeout: (timer) => pending.delete(timer),
  };
}
function clipboardDom({ modern, copied = true, modal = false } = {}) {
  const appended = [];
  let document;
  class Element {
    isConnected = true;
    style = {};
    selectionStart = 2;
    selectionEnd = 5;
    selectionDirection = "backward";
    setAttribute() {}
    focus() { document.activeElement = this; }
    select() { this.selectionStart = 0; this.selectionEnd = this.value.length; }
    setSelectionRange(start, end, direction) { Object.assign(this, { selectionStart: start, selectionEnd: end, selectionDirection: direction }); }
    closest() { return modal ? modalRoot : null; }
    remove() { this.isConnected = false; appended.splice(appended.indexOf(this), 1); }
  }
  const body = { appendChild: (node) => { node.owner = "body"; appended.push(node); } };
  const modalRoot = { appendChild: (node) => { node.owner = "modal"; appended.push(node); } };
  const active = new Element();
  let copiedOwner;
  document = {
    activeElement: active, body,
    getSelection: () => null,
    createElement: () => new Element(),
    execCommand: () => { copiedOwner = appended[0]?.owner; if (copied instanceof Error) throw copied; return copied; },
  };
  return {
    active, appended, owner: () => copiedOwner,
    globals: { navigator: { clipboard: modern ? { writeText: modern } : undefined }, document, HTMLElement: Element, HTMLInputElement: Element, HTMLTextAreaElement: Element },
  };
}

test("clipboard fallback restores input focus, caret direction and removes its temporary field", async () => {
  const dom = clipboardDom();
  const api = load("src/lib/clipboard.ts", {}, dom.globals);
  await api.copyTextToClipboard("selected text");
  assert.equal(dom.globals.document.activeElement, dom.active);
  assert.deepEqual([dom.active.selectionStart, dom.active.selectionEnd, dom.active.selectionDirection], [2, 5, "backward"]);
  assert.equal(dom.appended.length, 0);
});

test("clipboard API rejection tries the fallback inside the active modal", async () => {
  const dom = clipboardDom({ modern: async () => { throw new Error("permission denied"); }, modal: true });
  const api = load("src/lib/clipboard.ts", {}, dom.globals);
  assert.equal(await api.tryCopyTextToClipboard("text"), true);
  assert.equal(dom.owner(), "modal");
  assert.equal(dom.appended.length, 0);
});

for (const result of [false, new Error("copy failed")]) {
  test(`clipboard failure never reports success and still restores focus (${String(result)})`, async () => {
    const dom = clipboardDom({ copied: result });
    const api = load("src/lib/clipboard.ts", {}, dom.globals);
    assert.equal(await api.tryCopyTextToClipboard("text"), false);
    assert.equal(dom.globals.document.activeElement, dom.active);
    assert.equal(dom.appended.length, 0);
  });
}

test("successful modern clipboard copy never creates a legacy selection", async () => {
  const writes = [];
  const dom = clipboardDom({ modern: async (text) => writes.push(text) });
  await load("src/lib/clipboard.ts", {}, dom.globals).copyTextToClipboard("text");
  assert.deepEqual(writes, ["text"]);
  assert.equal(dom.owner(), undefined);
});

test("copy feedback waits for completion, prevents duplicate requests, and preserves failure truth", async () => {
  const h = hooks(); const clock = timers(); let finish; let calls = 0;
  const api = load("src/hooks/useClipboardFeedback.ts", {
    react: h.react,
    "@/lib/clipboard": { tryCopyTextToClipboard: () => { calls += 1; return new Promise((resolve) => { finish = resolve; }); } },
  }, { window: clock });
  const render = () => h.render(api.useClipboardFeedback);
  const pending = render().copy("image:prompt", "text");
  assert.equal(render().copiedKey, null);
  assert.equal(render().pendingKey, "image:prompt");
  await render().copy("image:prompt", "text");
  assert.equal(calls, 1);
  finish(false); await pending;
  assert.equal(render().copiedKey, null);
  assert.equal(render().errorKey, "image:prompt");
  assert.equal(clock.pending.size, 0);
});

test("copy feedback clears older success timers and makes no result claim for legacy void callbacks", async () => {
  const h = hooks(); const clock = timers();
  const api = load("src/hooks/useClipboardFeedback.ts", { react: h.react, "@/lib/clipboard": { tryCopyTextToClipboard: async () => true } }, { window: clock });
  const render = () => h.render(api.useClipboardFeedback);
  await render().copy("a", "first");
  assert.equal(render().copiedKey, "a");
  await render().copy("b", "second");
  assert.equal(clock.pending.size, 1);
  assert.equal(render().copiedKey, "b");
  await render().copy("c", "third", () => {});
  assert.equal(render().copiedKey, null);
  assert.equal(render().errorKey, null);
  h.unmount(); assert.equal(clock.pending.size, 0);
});

function downloadDom(response) {
  const clock = timers(); const clicks = []; const anchors = []; const revoked = [];
  const globals = {
    window: clock,
    fetch: typeof response === "function" ? response : async () => response,
    document: {
      body: { appendChild: (anchor) => anchors.push(anchor) },
      createElement: () => ({ click() { clicks.push({ href: this.href, filename: this.download }); }, remove() { anchors.splice(anchors.indexOf(this), 1); } }),
    },
    URL: { createObjectURL: () => "blob:test", revokeObjectURL: (url) => revoked.push(url) },
  };
  return { clock, clicks, anchors, revoked, api: load("src/lib/imageDownload.ts", {}, globals) };
}

test("image downloads use real MIME extension, sanitize the name and clean anchors/object URLs", async () => {
  const dom = downloadDom(new Response(new Blob(["image bytes"], { type: "image/webp" })));
  await dom.api.downloadImageFile({ url: "/image", filename: "poster_9:16.png" }, new AbortController().signal);
  assert.deepEqual(dom.clicks, [{ href: "blob:test", filename: "poster_9-16.webp" }]);
  assert.equal(dom.anchors.length, 0);
  assert.equal(dom.clock.pending.size, 1);
  [...dom.clock.pending.values()][0].callback();
  assert.deepEqual(dom.revoked, ["blob:test"]);
});

for (const [name, response] of [["HTTP error", new Response("expired", { status: 403 })], ["HTML error page", new Response("login", { headers: { "content-type": "text/html" } })], ["empty image", new Response(new Blob([], { type: "image/png" }))]]) {
  test(`image downloads reject ${name} without clicking a fake download`, async () => {
    const dom = downloadDom(response);
    await assert.rejects(dom.api.downloadImageFile({ url: "/image", filename: "image.png" }, new AbortController().signal));
    assert.equal(dom.clicks.length, 0);
    assert.equal(dom.clock.pending.size, 0);
  });
}

test("image download cancellation and timeout abort the request rather than leave a stuck button", async () => {
  const fetch = (_url, { signal }) => new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true }));
  const dom = downloadDom(fetch); const controller = new AbortController();
  const request = dom.api.downloadImageFile({ url: "/image", filename: "image.png" }, controller.signal);
  controller.abort(); await assert.rejects(request, { name: "AbortError" });
  assert.equal(dom.clicks.length, 0);
  const timeout = dom.api.downloadImageFile({ url: "/image", filename: "image.png" }, new AbortController().signal);
  [...dom.clock.pending.values()].find((entry) => entry.delay === 30_000).callback();
  await assert.rejects(timeout, /超时/);
  assert.equal(dom.clock.pending.size, 0);
});

test("batch download locks synchronously, tracks partial failure, and retries only failed files", async () => {
  const h = hooks(); const calls = []; let finish; let fail = true;
  const api = load("src/components/ui/projects/components/ProjectImageDownloads.tsx", {
    react: h.react,
    "@/components/QueryProvider": { useUserQueryScope: () => ({ userId: "user" }) },
    "@/lib/imageDownload": { downloadImageFile: async (file) => {
      calls.push(file.url);
      if (calls.length === 1) await new Promise((resolve) => { finish = resolve; });
      if (file.url === "b" && fail) throw new Error("offline");
    } },
  });
  const render = () => h.render(() => api.useProjectImageDownloads("workflow"));
  const files = ["a", "b", "c"].map((url) => ({ url, filename: `${url}.png` }));
  const running = render().start(files);
  await render().start(files);
  assert.deepEqual(calls, ["a"]);
  finish(); await running;
  assert.equal(render().state.dispatched, 2);
  assert.equal(render().busy, false);
  assert.deepEqual(render().state.remaining.map((file) => file.url), ["b"]);
  fail = false; await render().start(render().state.remaining);
  assert.deepEqual(calls, ["a", "b", "c", "b"]);
  assert.equal(render().state.remaining.length, 0);
});

test("batch download cancellation preserves the remaining files for retry", async () => {
  const h = hooks(); const calls = [];
  const api = load("src/components/ui/projects/components/ProjectImageDownloads.tsx", {
    react: h.react,
    "@/components/QueryProvider": { useUserQueryScope: () => ({ userId: "user" }) },
    "@/lib/imageDownload": { downloadImageFile: (file, signal) => new Promise((_resolve, reject) => {
      calls.push(file.url); signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    }) },
  });
  const render = () => h.render(() => api.useProjectImageDownloads("workflow"));
  const files = ["a", "b"].map((url) => ({ url, filename: url }));
  const running = render().start(files); render().cancel(); await running;
  assert.deepEqual(calls, ["a"]);
  assert.equal(render().state.stopped, true);
  assert.deepEqual(render().state.remaining, files);
  assert.equal(render().state.error, null);
});

test("form feedback focuses the invalid field after render and associates the error without losing its hint", () => {
  const h = hooks(); const actions = [];
  const api = load("src/hooks/useFormFeedback.ts", { react: h.react }, { document: { getElementById: (id) => ({ matches: () => false, focus: () => actions.push(id), scrollIntoView() {} }) } });
  const render = () => h.render(() => api.useFormFeedback("form-error"));
  render().report("Invalid email", "email");
  assert.deepEqual(render().fieldProps("email", "email-hint"), { "aria-invalid": true, "aria-describedby": "email-hint form-error" });
  assert.deepEqual(actions, ["email"]);
  render().clear();
  assert.equal(render().fieldProps("email")["aria-invalid"], undefined);
});

test("candidate images always preview and do not silently choose a one-image candidate", () => {
  const calls = [];
  const api = load("src/components/ui/projects/components/CandidateCard.tsx", {}, {}, "\nexport { CandidateGallery };\n");
  const image = { id: "image" };
  const gallery = api.CandidateGallery({ images: [image], candidate: { candidate_index: 1 }, generating: false, onPreview: (...args) => calls.push(args), onChoose: () => assert.fail("unexpected choose") });
  gallery[0].props.onClick();
  assert.equal(calls[0][0], image);
  assert.match(gallery[0].props["aria-label"], /预览/);
});

test("shared dialog, toast and delivery retain their explicit interaction contracts", () => {
  const dialog = source("src/components/ui/primitives/Dialog.tsx");
  assert.match(dialog, /useReducedMotion/);
  assert.match(dialog, /reduceMotion \? \{ duration: 0 \}/);
  const toast = source("src/components/ui/primitives/Toast.tsx");
  assert.match(toast, /const paused = hovered \|\| focused/);
  assert.match(toast, /onMouseLeave=\{\(\) => setHovered\(false\)\}/);
  const delivery = source("src/components/ui/projects/stages/DeliveryStage.tsx");
  assert.match(delivery, /await reopen\.mutateAsync\(\);\s*setConfirmReopen\(false\)/);
  assert.doesNotMatch(delivery, /href=\{canDownload\(image\) \|\| "#"\}/);
  assert.doesNotMatch(delivery, /一键打包下载/);
});

function jsxNodes(node) {
  if (Array.isArray(node)) return node.flatMap(jsxNodes);
  if (!node || typeof node !== "object" || !node.props) return [];
  return [node, ...jsxNodes(node.props.children)];
}

for (const flags of [{ isComposing: true }, { keyCode: 229 }, { repeat: true }]) {
  test(`rename ignores IME confirmation and repeated keys (${JSON.stringify(flags)})`, () => {
    const h = hooks();
    const renamed = [];
    const keys = load("src/lib/interactionKeys.ts");
    const api = load("src/components/ui/me/ConversationRowMobile.tsx", {
      react: h.react,
      "@/lib/interactionKeys": keys,
      "@/lib/copy": { copy: { action: { cancel: "取消", confirm: "确认" } } },
      "@/components/ui/primitives/mobile": { SwipeRow: "SwipeRow", BottomSheet: "BottomSheet", ActionSheet: "ActionSheet" },
    });
    const props = { conv: { id: "fixture", title: "原名称", created_at: "" }, active: false, onSelect() {}, onArchive() {}, onDelete() {}, onRename: (value) => renamed.push(value) };
    const render = () => jsxNodes(h.render(() => api.ConversationRowMobile(props)));
    render().find((node) => node.type === "SwipeRow").props.actions[0].onAction();
    render().find((node) => node.type === "input").props.onChange({ target: { value: "   " } });
    render().find((node) => node.type === "input").props.onKeyDown({ key: "Enter", nativeEvent: {}, preventDefault() {} });
    assert.equal(render().find((node) => node.type === "BottomSheet").props.open, true);
    assert.deepEqual(renamed, []);
    render().find((node) => node.type === "input").props.onChange({ target: { value: " 中文标题 " } });
    const input = render().find((node) => node.type === "input");
    assert.equal(input.props.maxLength, 120);
    for (const key of ["Enter", "Escape"]) {
      input.props.onKeyDown({ key, nativeEvent: flags, preventDefault() {} });
      assert.equal(render().find((node) => node.type === "BottomSheet").props.open, true);
      assert.deepEqual(renamed, []);
    }
    render().find((node) => node.type === "input").props.onKeyDown({ key: "Enter", nativeEvent: {}, preventDefault() {} });
    assert.deepEqual(renamed, ["中文标题"]);
    assert.equal(render().find((node) => node.type === "BottomSheet").props.open, false);
  });
}

test("keyboard action guard accepts ordinary keys, rejects composition and repeat", () => {
  const { isImeOrRepeatedKey } = load("src/lib/interactionKeys.ts");
  assert.equal(isImeOrRepeatedKey({}), false);
  assert.equal(isImeOrRepeatedKey({ keyCode: 13, repeat: false, isComposing: false }), false);
  for (const flags of [{ isComposing: true }, { keyCode: 229 }, { repeat: true }]) assert.equal(isImeOrRepeatedKey(flags), true);
});

test("reset confirmation exposes recovery, field associations and post-success focus", () => {
  const reset = source("src/app/reset-password/[token]/page.tsx");
  assert.match(reset, /useFormFeedback\("reset-confirm-error"\)/);
  assert.match(reset, /href="\/reset-password"/);
  assert.match(reset, /重新获取重置链接/);
  assert.doesNotMatch(reset, /--link-fg/);
  assert.match(source("src/app/video/video-prompt-editor.tsx"), /aria-label="镜头描述"/);
  assert.match(reset, /id="reset-confirm-mismatch"/);
  assert.match(reset, /id="reset-password-hint"/);
  assert.match(reset, /getElementById\("reset-confirm-success"\)\?\.focus\(\)/);
  const desktop = source("src/components/ui/sidebar/ConversationItem.tsx");
  assert.match(desktop, /aria-label="重命名会话"/);
  assert.match(desktop, /aria-label="删除会话"/);
  assert.match(desktop, /disabled=\{busy \|\| !renameValue.trim\(\)\}/);
});
