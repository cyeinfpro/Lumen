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
        if (!(key in target)) target[key] = () => undefined;
        return target[key];
      },
    },
  );
}

function loadModule(relativePath, overrides = {}) {
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
  const requireModule = (id) => {
    if (id in overrides) return overrides[id];
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

function motionMock() {
  return new Proxy(
    {},
    {
      get: (_target, key) => `motion.${String(key)}`,
    },
  );
}

test("ConfirmDialog blocks backdrop and modal close while confirming", () => {
  let modalOptions;
  const calls = [];
  const { ConfirmDialog } = loadModule(
    "src/components/ui/primitives/ConfirmDialog.tsx",
    {
      react: {
        useCallback: (callback) => callback,
        useId: () => "dialog-id",
        useRef: (value) => ({ current: value }),
      },
      "framer-motion": {
        AnimatePresence: "AnimatePresence",
        motion: motionMock(),
      },
      "@/hooks/useBodyScrollLock": { useBodyScrollLock() {} },
      "@/lib/utils": { cn: (...values) => values.filter(Boolean).join(" ") },
      "./Button": { Button: "Button" },
      "./mobile/useModalLayer": {
        useModalLayer(options) {
          modalOptions = options;
          return () => {};
        },
      },
    },
  );

  const tree = ConfirmDialog({
    open: true,
    confirming: true,
    title: "Delete",
    onConfirm() {},
    onOpenChange: (open) => calls.push(["open", open]),
    onCancel: () => calls.push(["cancel"]),
  });
  const overlay = findElement(
    tree,
    (node) =>
      node.type === "motion.div" &&
      typeof node.props.onMouseDown === "function",
  );
  const dialog = findElement(tree, (node) => node.props?.role === "dialog");
  const backdrop = {};
  overlay.props.onMouseDown({ target: backdrop, currentTarget: backdrop });
  modalOptions.onClose();

  assert.deepEqual(calls, []);
  assert.equal(dialog.props["aria-busy"], true);
});

test("ConfirmDialog still closes normally when it is idle", () => {
  let modalOptions;
  const calls = [];
  const { ConfirmDialog } = loadModule(
    "src/components/ui/primitives/ConfirmDialog.tsx",
    {
      react: {
        useCallback: (callback) => callback,
        useId: () => "dialog-id",
        useRef: (value) => ({ current: value }),
      },
      "framer-motion": {
        AnimatePresence: "AnimatePresence",
        motion: motionMock(),
      },
      "@/hooks/useBodyScrollLock": { useBodyScrollLock() {} },
      "@/lib/utils": { cn: (...values) => values.filter(Boolean).join(" ") },
      "./Button": { Button: "Button" },
      "./mobile/useModalLayer": {
        useModalLayer(options) {
          modalOptions = options;
          return () => {};
        },
      },
    },
  );

  ConfirmDialog({
    open: true,
    title: "Delete",
    onConfirm() {},
    onOpenChange: (open) => calls.push(["open", open]),
    onCancel: () => calls.push(["cancel"]),
  });
  modalOptions.onClose();

  assert.deepEqual(calls, [["open", false], ["cancel"]]);
});

test("Tooltip links its trigger without discarding existing descriptions", () => {
  const stateValues = [true, false, false];
  let stateIndex = 0;
  const { Tooltip } = loadModule(
    "src/components/ui/primitives/Tooltip.tsx",
    {
      react: {
        cloneElement: (node, props) => ({
          ...node,
          props: { ...node.props, ...props },
        }),
        useEffect() {},
        useId: () => "tooltip-id",
        useRef: (value) => ({ current: value }),
        useState: () => [stateValues[stateIndex++], () => {}],
      },
      "framer-motion": {
        AnimatePresence: "AnimatePresence",
        motion: motionMock(),
        useReducedMotion: () => false,
      },
      "@/lib/utils": { cn: (...values) => values.filter(Boolean).join(" ") },
    },
  );
  const trigger = {
    type: "button",
    props: { "aria-describedby": "existing-description", children: "Info" },
  };

  const tree = Tooltip({ content: "Details", children: trigger });
  const renderedTrigger = findElement(tree, (node) => node.type === "button");
  const tooltip = findElement(tree, (node) => node.props?.role === "tooltip");

  assert.equal(
    renderedTrigger.props["aria-describedby"],
    "existing-description tooltip-id",
  );
  assert.equal(tooltip.props.id, "tooltip-id");
});

test("Chip forwards the original click handler instead of converting it to onPress", () => {
  const onClick = () => {};
  const { Chip } = loadModule(
    "src/components/ui/primitives/mobile/Chip.tsx",
    { "./Pressable": { Pressable: "Pressable" } },
  );

  const tree = Chip({ onClick, children: "All" });

  assert.equal(tree.props.onClick, onClick);
  assert.equal(tree.props.onPress, undefined);
});

test("Pressable leaves caller opacity in control while idle", () => {
  const react = {
    forwardRef: (render) => (props) => render(props, null),
    useCallback: (callback) => callback,
    useEffect() {},
    useRef: (value) => ({ current: value }),
    useState: (initial) => [initial, () => {}],
  };
  const { Pressable } = loadModule(
    "src/components/ui/primitives/mobile/Pressable.tsx",
    {
      react,
      "framer-motion": { useReducedMotion: () => false },
      "@/hooks/useHaptic": { useHaptic: () => ({ haptic() {} }) },
      "@/lib/motion": {
        PRESS_SCALE: { tight: 0.96, soft: 0.98 },
      },
    },
  );

  const classDriven = Pressable({
    className: "opacity-40",
    children: "Open",
  });
  const styleDriven = Pressable({
    style: { opacity: 0.35 },
    children: "Open",
  });
  const disabled = Pressable({ disabled: true, children: "Open" });

  assert.equal(Object.hasOwn(classDriven.props.style, "opacity"), false);
  assert.equal(styleDriven.props.style.opacity, 0.35);
  assert.equal(disabled.props.style.opacity, "var(--op-disabled)");
});

test("SwipeRow exposes its actions through keyboard controls", () => {
  const stateValues = [];
  let stateIndex = 0;
  const motionValue = {
    value: 0,
    set(value) {
      this.value = value;
    },
  };
  const react = {
    useCallback: (callback) => callback,
    useEffect() {},
    useId: () => "swipe-actions",
    useRef: (value) => ({ current: value }),
    useState(initial) {
      const index = stateIndex++;
      if (!(index in stateValues)) stateValues[index] = initial;
      return [
        stateValues[index],
        (next) => {
          stateValues[index] =
            typeof next === "function" ? next(stateValues[index]) : next;
        },
      ];
    },
  };
  const { SwipeRow } = loadModule(
    "src/components/ui/primitives/mobile/SwipeRow.tsx",
    {
      react,
      "framer-motion": {
        animate: () => ({ stop() {}, then() {} }),
        motion: motionMock(),
        useMotionValue: () => motionValue,
        useReducedMotion: () => true,
      },
      "@/lib/motion": {
        GESTURE: {
          dismissVelocity: 800,
          snapVelocity: 300,
        },
        SPRING: { gesture: {} },
        projectMomentum: () => 0,
      },
      "./Pressable": { Pressable: "Pressable" },
    },
  );

  const render = () => {
    stateIndex = 0;
    return SwipeRow({
      actions: [{ key: "delete", label: "Delete", onAction() {} }],
      children: "Row",
    });
  };
  const closed = render();
  let prevented = false;
  const closedTarget = {};
  closed.props.onKeyDown({
    key: "ArrowLeft",
    target: closedTarget,
    currentTarget: closedTarget,
    preventDefault: () => {
      prevented = true;
    },
  });

  assert.equal(closed.props.tabIndex, 0);
  assert.equal(closed.props.role, "group");
  assert.equal(closed.props["aria-controls"], "swipe-actions");
  assert.equal(prevented, true);
  assert.equal(motionValue.value, -80);
  assert.equal(stateValues[1], true);

  const open = render();
  prevented = false;
  open.props.onKeyDown({
    key: "Escape",
    target: {},
    currentTarget: {},
    preventDefault: () => {
      prevented = true;
    },
  });

  assert.equal(prevented, true);
  assert.equal(motionValue.value, 0);
  assert.equal(stateValues[1], false);
});

test("mobile toast preserves ReactNode content and tone mapping", () => {
  const calls = [];
  const toast = {
    success: (message) => calls.push(["success", message]),
    error: (message) => calls.push(["error", message]),
    info: (message) => calls.push(["info", message]),
    warning: (message) => calls.push(["warning", message]),
  };
  const { pushMobileToast } = loadModule(
    "src/components/ui/primitives/mobile/Toast.tsx",
    { "../Toast": { toast } },
  );
  const message = { type: "strong", props: { children: "Saved" } };

  pushMobileToast(message, "success");
  pushMobileToast(message, "danger");

  assert.equal(calls[0][1], message);
  assert.equal(calls[1][1], message);
  assert.deepEqual(
    calls.map(([tone]) => tone),
    ["success", "error"],
  );
});

test("global toast keeps actionable and severe notices readable on mobile", () => {
  const toastSource = source("src/components/ui/primitives/Toast.tsx");

  assert.match(toastSource, /success: 4000/);
  assert.match(toastSource, /info: 5000/);
  assert.match(toastSource, /warning: 8000/);
  assert.match(toastSource, /error: 8000/);
  assert.match(toastSource, /const ACTION_DURATION_MS = 10000/);
  assert.match(toastSource, /defaultDurationMs\(t\.tone, Boolean\(t\.action\)\)/);
  assert.match(toastSource, /max-sm:min-h-11 max-sm:px-2/);
  assert.match(toastSource, /aria-atomic="true"/);
});

test("image lightbox URL selection skips null and invalid candidates", () => {
  const { imageResultToLightboxItem } = loadModule(
    "src/lib/imageResultLightbox.ts",
    {
      "@/lib/apiClient": {
        imageBinaryUrl: (id) => `/api/images/${id}/binary`,
        imageVariantUrl: (id, kind) => `/api/images/${id}/${kind}`,
      },
    },
  );
  const generation = {
    id: "generation-1",
    prompt: "A test image",
    action: "generate",
  };
  const image = {
    id: "image-1",
    data_url: "",
    display_url: null,
    preview_url: "javascript:alert(1)",
    thumb_url: "  /api/images/image-1/thumb-safe  ",
    width: 512,
    height: 512,
    parent_image_id: null,
    from_generation_id: "generation-1",
    size_requested: "512x512",
    size_actual: "512x512",
  };

  const item = imageResultToLightboxItem(generation, image, {
    url: null,
    previewUrl: "asset://missing-preview",
    thumbUrl: /** @type {any} */ ({ invalid: true }),
  });

  assert.equal(item.url, "/api/images/image-1/binary");
  assert.equal(item.previewUrl, "/api/images/image-1/display2048");
  assert.equal(item.thumbUrl, "/api/images/image-1/thumb-safe");
});
