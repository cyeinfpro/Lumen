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

function reactMock() {
  return {
    useCallback(callback) {
      return callback;
    },
    useEffect() {},
    useMemo(factory) {
      return factory();
    },
    useRef(value) {
      return { current: value };
    },
    useState(initial) {
      return [typeof initial === "function" ? initial() : initial, () => {}];
    },
  };
}

function findElements(node, predicate, matches = []) {
  if (node == null || typeof node === "boolean") return matches;
  if (Array.isArray(node)) {
    for (const child of node) findElements(child, predicate, matches);
    return matches;
  }
  if (typeof node !== "object") return matches;
  if (predicate(node)) matches.push(node);
  if (typeof node.type === "function") {
    findElements(node.type(node.props ?? {}), predicate, matches);
  }
  findElements(node.props?.children, predicate, matches);
  return matches;
}

function createHarness() {
  const state = {
    currentConvId: "conv-a",
    messages: [],
    messagesLoading: false,
    messagesError: "history unavailable",
    generations: {},
    loadHistoricalMessages: async () => {},
    sendMessage: async () => {},
    retryAssistant() {},
    retryGeneration() {},
    regenerateAssistant() {},
    promoteImageToReference() {},
    setCurrentConv() {},
    setText() {},
    setMode() {},
  };
  const useChatStore = Object.assign(
    (selector) => selector(state),
    { getState: () => state },
  );
  const mobileModule = loadModule(
    "src/components/ui/shell/MobileStudio.tsx",
    {
      react: reactMock(),
      "next/navigation": {
        useSearchParams: () => ({ get: () => null }),
      },
      "@/components/ui/shell/LandscapeBanner": {
        LandscapeBanner: "LandscapeBanner",
      },
      "./LandscapeBanner": { LandscapeBanner: "LandscapeBanner" },
      "./MobileStudioTopBar": { MobileStudioTopBar: "MobileStudioTopBar" },
      "./MobileTabBar": { MobileTabBar: "MobileTabBar" },
      "@/components/ui/chat/mobile/MobileConversationCanvas": {
        MobileConversationCanvas: "MobileConversationCanvas",
      },
      "@/components/ui/composer/mobile/MobileComposerPill": {
        MobileComposerPill: "MobileComposerPill",
      },
      "@/components/ui/chat/mobile/MobileEmptyStudio": {
        MobileEmptyStudio: "MobileEmptyStudio",
      },
      "@/components/ui/primitives": {
        ErrorState: "ErrorState",
        Spinner: "Spinner",
      },
      "@/components/ui/tray/TaskIsland": { TaskIsland: "TaskIsland" },
      "@/store/useChatStore": { useChatStore },
      "@/lib/queries": {
        useListConversationsInfiniteQuery: () => ({
          data: { pages: [{ items: [] }] },
          hasNextPage: false,
          isFetchingNextPage: false,
          fetchNextPage() {},
        }),
      },
      "@/lib/logger": { logWarn() {} },
      "@/lib/utils": {
        cn: (...values) => values.filter(Boolean).join(" "),
      },
      "@/hooks/useElementBlockSize": {
        useElementBlockSize: () => [() => {}, 0],
      },
      "./useDefaultConversationSelection": {
        useDefaultConversationSelection() {},
      },
      "./useConversationRouteSync": {
        useConversationRouteSync: () => null,
      },
    },
  );

  return {
    state,
    render() {
      return mobileModule.MobileStudio();
    },
  };
}

test("mobile initial history failure renders retry state without composer", () => {
  const harness = createHarness();
  const failedTree = harness.render();

  assert.equal(
    findElements(failedTree, (node) => node.type === "ErrorState").length,
    1,
  );
  assert.equal(
    findElements(
      failedTree,
      (node) => node.type === "MobileComposerPill",
    ).length,
    0,
  );
  assert.equal(
    findElements(failedTree, (node) => node.type === "MobileEmptyStudio")
      .length,
    0,
  );

  harness.state.currentConvId = "conv-new";
  harness.state.messagesError = null;
  const newConversationTree = harness.render();

  assert.equal(
    findElements(
      newConversationTree,
      (node) => node.type === "MobileComposerPill",
    ).length,
    1,
  );
  assert.equal(
    findElements(
      newConversationTree,
      (node) => node.type === "MobileEmptyStudio",
    ).length,
    1,
  );
});
