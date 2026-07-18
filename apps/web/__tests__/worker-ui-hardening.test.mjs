import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import ts from "typescript";
import { fileURLToPath } from "node:url";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(testDir, "..");

function source(relativePath) {
  return fs.readFileSync(path.join(webRoot, relativePath), "utf8");
}

const jsxRuntime = {
  Fragment: Symbol.for("react.fragment"),
  jsx(type, props, key) {
    return { type, key, props: props ?? {} };
  },
  jsxs(type, props, key) {
    return { type, key, props: props ?? {} };
  },
};

function looseMock() {
  return new Proxy(
    { __esModule: true },
    {
      get(target, key) {
        if (key === "__esModule") return true;
        if (!(key in target)) {
          target[key] = () => undefined;
        }
        return target[key];
      },
    },
  );
}

function createReactHarness({ captureEffects = false } = {}) {
  const memoCalls = [];
  const refs = [];
  const cleanups = [];
  const stateValues = [];
  let stateCursor = 0;
  let onStateChange = null;

  const react = {
    memo(component, compare) {
      memoCalls.push({ component, compare });
      return component;
    },
    useCallback(callback) {
      return callback;
    },
    useMemo(factory) {
      return factory();
    },
    useRef(value) {
      const ref = { current: value };
      refs.push(ref);
      return ref;
    },
    useState(initial) {
      const index = stateCursor++;
      if (!(index in stateValues)) {
        stateValues[index] =
          typeof initial === "function" ? initial() : initial;
      }
      const setState = (next) => {
        stateValues[index] =
          typeof next === "function" ? next(stateValues[index]) : next;
        onStateChange?.(stateValues[index]);
      };
      return [stateValues[index], setState];
    },
    useEffect(effect) {
      if (!captureEffects) return;
      const cleanup = effect();
      if (typeof cleanup === "function") cleanups.push(cleanup);
    },
  };

  return {
    react,
    memoCalls,
    refs,
    cleanups,
    startRender() {
      stateCursor = 0;
    },
    setStateObserver(observer) {
      onStateChange = observer;
    },
  };
}

function resolveRelativeModule(relativePath, id) {
  if (!id.startsWith(".")) return null;
  const basePath = path.resolve(webRoot, path.dirname(relativePath), id);
  const candidates = [
    basePath,
    `${basePath}.ts`,
    `${basePath}.tsx`,
    `${basePath}.js`,
    `${basePath}.mjs`,
    path.join(basePath, "index.ts"),
    path.join(basePath, "index.tsx"),
    path.join(basePath, "index.js"),
    path.join(basePath, "index.mjs"),
  ];
  const match = candidates.find((candidate) => fs.existsSync(candidate));
  return match
    ? path.relative(webRoot, match).split(path.sep).join("/")
    : null;
}

function loadModule(
  relativePath,
  overrides = {},
  resolveRelative = false,
  cache = new Map(),
) {
  if (resolveRelative && cache.has(relativePath)) {
    return cache.get(relativePath);
  }
  const output = ts.transpileModule(source(relativePath), {
    compilerOptions: {
      isolatedModules: true,
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: relativePath,
  }).outputText;
  const compiledModule = { exports: {} };
  if (resolveRelative) cache.set(relativePath, compiledModule.exports);
  const requireModule = (id) => {
    if (id in overrides) return overrides[id];
    if (resolveRelative) {
      const localModule = resolveRelativeModule(relativePath, id);
      if (localModule) {
        return loadModule(localModule, overrides, true, cache);
      }
    }
    if (id === "react/jsx-runtime") return jsxRuntime;
    return looseMock();
  };
  new Function("require", "module", "exports", output)(
    requireModule,
    compiledModule,
    compiledModule.exports,
  );
  return compiledModule.exports;
}

function textContent(node) {
  if (node === null || node === undefined || node === false) return "";
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) return node.map(textContent).join("");
  if (typeof node === "object" && node.props) {
    return textContent(node.props.children);
  }
  return "";
}

function findElement(node, predicate) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElement(child, predicate);
      if (found) return found;
    }
    return null;
  }
  if (!node || typeof node !== "object") return null;
  if (predicate(node)) return node;
  return findElement(node.props?.children, predicate);
}

function baseChatMocks(harness, extra = {}) {
  return {
    react: harness.react,
    "react/jsx-runtime": jsxRuntime,
    "lucide-react": looseMock(),
    "@/components/ui/primitives": {
      Button: "Button",
      IconButton: "IconButton",
      toast: {
        success() {},
        error() {},
      },
    },
    "@/components/ui/primitives/mobile": {
      pushMobileToast() {},
    },
    "@/components/ui/Markdown": { Markdown: "Markdown" },
    "@/components/ui/ViewportImage": { ViewportImage: "ViewportImage" },
    "@/components/ui/chat/CompletionStatusLine": {
      CompletionStatusLine: "CompletionStatusLine",
    },
    "@/components/ui/chat/generationRenderSignature": {
      generationRenderSignature: (generation) =>
        generation ? `${generation.id}:${generation.status}` : "",
    },
    "@/hooks/useHistoryPaging": {
      useHistoryPaging: () => ({
        topSentinelRef: { current: null },
        hasMore: false,
        loading: false,
        error: null,
        loadMore() {},
        retry() {},
      }),
    },
    "@/lib/apiClient": {
      cancelTask: async () => {},
      imageVariantUrl: (id) => `/images/${id}`,
    },
    "@/lib/imagePreload": { prewarmImage() {} },
    "@/lib/imageResultLightbox": {
      imageResultToLightboxItem: () => ({ id: "image" }),
    },
    "@/lib/sizing": { aspectRatioToCss: () => "1 / 1" },
    "@/lib/utils": { cn: (...values) => values.filter(Boolean).join(" ") },
    "./DevelopingCard": { DevelopingCard: "DevelopingCard" },
    "./SceneDivider": { SceneDivider: "SceneDivider" },
    "@tanstack/react-virtual": {
      useVirtualizer: () => ({
        getTotalSize: () => 0,
        getVirtualItems: () => [],
        measureElement() {},
        scrollToIndex() {},
      }),
    },
    ...extra,
  };
}

test("inpaint submit acquires a synchronous lock before mask export awaits", async () => {
  const harness = createReactHarness();
  const inpaint = {
    open: true,
    source: { imageId: "image-1", src: "data:image/png;base64,x", width: 10, height: 10 },
    submitting: false,
    drafts: { "image-1": "replace it" },
    maskDrafts: { "image-1": [{ id: "stroke-1" }] },
    setSubmitting: (value) => {
      inpaint.submitting = value;
    },
    close() {},
    setDraft() {},
    clearDraft() {},
    setMaskDraft() {},
    clearMaskDraft() {},
  };
  inpaint.getState = () => inpaint;
  inpaint.setState = (patch) => Object.assign(inpaint, patch);

  let exportCalls = 0;
  let resolveMask;
  const maskPromise = new Promise((resolve) => {
    resolveMask = resolve;
  });
  let submitCalls = 0;
  const chat = {
    submitInpaintTask: async () => {
      submitCalls += 1;
    },
  };
  const mocks = {
    ...baseChatMocks(harness),
    framerMotion: true,
    "framer-motion": {
      AnimatePresence: "AnimatePresence",
      motion: new Proxy(
        {},
        {
          get: (_target, key) => `motion.${String(key)}`,
        },
      ),
    },
    "@/store/useInpaintStore": {
      useInpaintStore: Object.assign(
        (selector) => selector(inpaint),
        {
          getState: () => inpaint,
          setState: (patch) => Object.assign(inpaint, patch),
        },
      ),
    },
    "@/store/useChatStore": {
      useChatStore: (selector) => selector(chat),
    },
    "./MaskBoard": { MaskBoard: "MaskBoard" },
    "@/hooks/useBodyScrollLock": { useBodyScrollLock() {} },
    "@/lib/logger": { logError() {} },
    "@/lib/promptLimits": { MAX_PROMPT_CHARS: 10_000 },
    "@/lib/sizing": { nearestAspectRatio: () => "1:1" },
    "@/lib/utils": { cn: (...values) => values.filter(Boolean).join(" ") },
  };
  const InpaintModal = loadModule(
    "src/components/ui/inpaint/InpaintModal.tsx",
    {
      ...mocks,
      "@/components/ui/primitives": {
        Button: "Button",
        IconButton: "IconButton",
        Textarea: "Textarea",
        Tooltip: "Tooltip",
      },
    },
    true,
  ).InpaintModal;

  harness.startRender();
  const outer = InpaintModal();
  harness.startRender();
  const innerElement = Array.isArray(outer.props.children)
    ? outer.props.children[0]
    : outer.props.children;
  const inner = innerElement.type;
  const view = inner();
  view.props.boardRef.current = {
    exportMask: () => {
      exportCalls += 1;
      return maskPromise;
    },
  };

  view.props.onSubmit();
  view.props.onSubmit();
  assert.equal(exportCalls, 1);
  assert.equal(inpaint.submitting, true);

  resolveMask({
    coverage: 0.1,
    width: 10,
    height: 10,
    blob: new Blob(["mask"], { type: "image/png" }),
    preview_data_url: "data:image/png;base64,mask",
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(submitCalls, 1);
});

test("mobile regenerate preserves the assistant intent and memo callbacks are live", () => {
  const harness = createReactHarness();
  const toasts = [];
  const mocks = baseChatMocks(harness, {
    "@/lib/clipboard": {
      tryCopyTextToClipboard: async () => true,
    },
    "@/components/ui/primitives/mobile": {
      pushMobileToast: (...args) => toasts.push(args),
    },
  });
  const exports = loadModule(
    "src/components/ui/chat/mobile/MobileConversationCanvas.tsx",
    mocks,
  );
  const assistantMemo = harness.memoCalls.find(
    ({ component }) => component.name === "AssistantTurn",
  );
  const userMemo = harness.memoCalls.find(
    ({ component }) => component.name === "UserTurn",
  );
  assert.ok(assistantMemo?.compare);

  const msg = {
    id: "assistant-1",
    role: "assistant",
    parent_user_message_id: "user-1",
    intent_resolved: "image_to_image",
    status: "succeeded",
    generation_ids: ["generation-1"],
    created_at: 1,
  };
  const generation = {
    id: "generation-1",
    status: "succeeded",
    input_image_ids: ["reference-1"],
  };
  const callbacks = {
    onEditImage() {},
    onRetryGen() {},
    onRetryText() {},
    onRegenerate() {},
  };
  const baseProps = {
    msg,
    generations: { "generation-1": generation },
    ...callbacks,
  };
  assert.equal(
    assistantMemo.compare(baseProps, {
      ...baseProps,
      onRegenerate: () => {},
    }),
    false,
  );
  assert.equal(assistantMemo.compare(baseProps, baseProps), true);

  let regenerateArgs;
  harness.startRender();
  const tree = assistantMemo.component({
    ...baseProps,
    onRegenerate: (...args) => {
      regenerateArgs = args;
    },
  });
  const regenerate = findElement(
    tree,
    (node) =>
      node.type === "button" && textContent(node).includes("重新生成"),
  );
  regenerate.props.onClick();
  assert.deepEqual(regenerateArgs, ["assistant-1", "image_to_image"]);

  const userMemoTree = userMemo.component({
    msg: {
      id: "user-1",
      role: "user",
      text: "copy me",
      attachments: [],
    },
  });
  const copyButton = findElement(
    userMemoTree,
    (node) => node.type === "button" && node.props["aria-label"] === "复制",
  );
  assert.equal(typeof exports.MobileConversationCanvas, "function");
  assert.deepEqual(toasts, []);
  copyButton.props.onClick();
});

test("desktop memo comparator rejects stale callbacks and consumes copy rejection", async () => {
  const harness = createReactHarness();
  const errors = [];
  const exports = loadModule(
    "src/components/ui/chat/desktop/DesktopConversationCanvas.tsx",
    baseChatMocks(harness, {
      "@/lib/clipboard": {
        tryCopyTextToClipboard: async () => false,
      },
      "@/components/ui/primitives": {
        Button: "Button",
        IconButton: "IconButton",
        toast: {
          success() {},
          error: (message) => errors.push(message),
        },
      },
      "next/navigation": { useRouter: () => ({ push() {} }) },
      "@/store/useChatStore": (selector) => selector({ currentConvId: null }),
      "@/store/useUiStore": (selector) => selector({}),
      "./DesktopSceneDivider": { DesktopSceneDivider: "DesktopSceneDivider" },
    }),
  );
  const assistantMemo = harness.memoCalls.find(
    ({ component }) => component.name === "AssistantTurn",
  );
  assert.ok(assistantMemo?.compare);
  const msg = {
    id: "assistant-1",
    role: "assistant",
    parent_user_message_id: "user-1",
    intent_resolved: "chat",
    status: "succeeded",
    created_at: 1,
    text: "hello",
  };
  const baseProps = {
    msg,
    generations: {},
    onEditImage() {},
    onRetryGen() {},
    onRetryText() {},
    onRegenerate() {},
    onOpenMenu() {},
  };
  assert.equal(
    assistantMemo.compare(baseProps, {
      ...baseProps,
      onOpenMenu: () => {},
    }),
    false,
  );

  harness.startRender();
  const tree = assistantMemo.component(baseProps);
  const copyComponent = findElement(
    tree,
    (node) =>
      typeof node.type === "function" && node.type.name === "CopyButton",
  );
  harness.startRender();
  const copyTree = copyComponent.type(copyComponent.props);
  findElement(copyTree, (node) => node.type === "button").props.onClick();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(errors, ["复制失败"]);
  assert.equal(typeof exports.DesktopConversationCanvas, "function");
});

test("a stale mask upload cannot clear the current upload state", async () => {
  const harness = createReactHarness();
  const submittingStates = [];
  const uploads = [];
  const composer = {
    mode: "image",
    attachments: [{ id: "attachment-1", data_url: "data:image/png;base64,x" }],
    mask: null,
    setMask() {},
    clearMask() {},
  };
  const mocks = {
    react: harness.react,
    "react/jsx-runtime": jsxRuntime,
    "@/components/ui/primitives/mobile": {
      pushMobileToast() {},
    },
    "@/lib/apiClient": {
      uploadImage: (_file, options) => {
        const deferred = {};
        deferred.promise = new Promise((resolve, reject) => {
          deferred.resolve = resolve;
          deferred.reject = reject;
        });
        deferred.signal = options.signal;
        uploads.push(deferred);
        return deferred.promise;
      },
    },
    "@/lib/logger": { logError() {} },
    "@/store/useChatStore": {
      useChatStore: Object.assign(
        (selector) =>
          selector({
            composer,
            setMask: composer.setMask,
            clearMask: composer.clearMask,
          }),
        {
          getState: () => ({ composer }),
        },
      ),
    },
  };
  const useMaskInpaint = loadModule(
    "src/components/ui/composer/shared/useMaskInpaint.ts",
    mocks,
  ).useMaskInpaint;

  harness.setStateObserver((value) => submittingStates.push(value));
  harness.startRender();
  const result = useMaskInpaint();
  const mask = {
    blob: new Blob(["mask"], { type: "image/png" }),
    preview_data_url: "data:image/png;base64,mask",
  };
  const first = result.handleConfirm(mask);
  const second = result.handleConfirm(mask);
  await Promise.resolve();
  assert.equal(uploads.length, 2);
  assert.deepEqual(submittingStates, [true, true]);

  uploads[0].resolve({ id: "stale-mask" });
  await first;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(submittingStates.at(-1), true);

  uploads[1].resolve({ id: "current-mask" });
  await second;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(submittingStates.at(-1), false);
});

test("lightbox delayed taps are canceled by effect cleanup", () => {
  const harness = createReactHarness({ captureEffects: true });
  const timers = new Map();
  let nextTimerId = 1;
  let tapCount = 0;
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const fakeWindow = {
    innerWidth: 400,
    innerHeight: 800,
    setTimeout(callback) {
      const id = nextTimerId++;
      timers.set(id, callback);
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    requestAnimationFrame(callback) {
      callback();
      return 1;
    },
    cancelAnimationFrame() {},
  };
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: fakeWindow,
  });
  try {
    const listeners = new Map();
    const element = {
      addEventListener(type, callback) {
        listeners.set(type, callback);
      },
      removeEventListener(type) {
        listeners.delete(type);
      },
      setPointerCapture() {},
      releasePointerCapture() {},
    };
    const motionValue = (value) => ({
      get: () => value,
      set(next) {
        value = next;
      },
    });
    const gestureExports = loadModule(
      "src/components/ui/lightbox/LightboxGestures.ts",
      {
        react: harness.react,
        "@/lib/motion": {
          projectMomentum: () => 0,
          rubberBandDistance: (value) => value,
        },
      },
    );
    gestureExports.useLightboxGestures(
      { current: element },
      {
        onSwipeLeft() {},
        onSwipeRight() {},
        onDismiss() {},
        onRevealOpen() {},
        onRevealClose() {},
        onTap() {
          tapCount += 1;
        },
        onDoubleTap() {},
      },
      {
        revealOpen: false,
        isFirst: false,
        isLast: false,
        dragX: motionValue(0),
        dragY: motionValue(0),
        scale: motionValue(1),
        haloOpacity: motionValue(1),
      },
    );
    listeners.get("pointerdown")({
      pointerId: 1,
      pointerType: "touch",
      button: 0,
      clientX: 10,
      clientY: 10,
    });
    listeners.get("pointerup")({
      pointerId: 1,
      pointerType: "touch",
      clientX: 10,
      clientY: 10,
    });
    assert.equal(timers.size, 1);
    harness.cleanups.at(-1)();
    for (const callback of timers.values()) callback();
    assert.equal(tapCount, 0);
  } finally {
    if (previousWindow) {
      Object.defineProperty(globalThis, "window", previousWindow);
    } else {
      delete globalThis.window;
    }
  }
});

test("completion status uses stream start as the missing last-delta fallback", () => {
  const harness = createReactHarness();
  const statusExports = loadModule(
    "src/components/ui/chat/CompletionStatusLine.tsx",
    {
      react: harness.react,
      "react/jsx-runtime": jsxRuntime,
      "@/lib/utils": { cn: (...values) => values.filter(Boolean).join(" ") },
    },
  );
  const status = statusExports.resolveCompletionStatus(
    {
      id: "assistant-1",
      role: "assistant",
      parent_user_message_id: "user-1",
      intent_resolved: "chat",
      status: "streaming",
      text: "partial",
      stream_started_at: 1_000,
      created_at: 1_000,
    },
    15_000,
  );
  assert.equal(status.label, "等待后续输出 14s");
});

test("desktop scene divider is a native keyboard-accessible disclosure control", () => {
  const harness = createReactHarness();
  const dividerExports = loadModule(
    "src/components/ui/chat/desktop/DesktopSceneDivider.tsx",
    {
      react: harness.react,
      "react/jsx-runtime": jsxRuntime,
      "@/lib/utils": { cn: (...values) => values.filter(Boolean).join(" ") },
    },
  );
  let toggled = 0;
  const tree = dividerExports.DesktopSceneDivider({
    index: 3,
    collapsed: true,
    controlsId: "scene-body",
    onToggle: () => {
      toggled += 1;
    },
  });
  assert.equal(tree.type, "button");
  assert.equal(tree.props.type, "button");
  assert.equal(tree.props["aria-expanded"], false);
  assert.equal(tree.props["aria-controls"], "scene-body");
  tree.props.onClick();
  assert.equal(toggled, 1);
});

test("shared clipboard helper consumes rejected writes for feedback callers", async () => {
  const previousNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      clipboard: {
        writeText: async () => {
          throw new Error("permission denied");
        },
      },
    },
  });
  try {
    const { tryCopyTextToClipboard } = await import(
      new URL("../src/lib/clipboard.ts", import.meta.url).href
    );
    assert.equal(await tryCopyTextToClipboard("text"), false);
  } finally {
    if (previousNavigator) {
      Object.defineProperty(globalThis, "navigator", previousNavigator);
    } else {
      delete globalThis.navigator;
    }
  }
});
