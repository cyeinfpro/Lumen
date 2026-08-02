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

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
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
  const conversation = {
    id: "conv-a",
    title: "Conversation A",
    archived: false,
    last_activity_at: "2026-07-30T10:00:00Z",
  };
  const state = {
    currentConvId: null,
    messages: [],
    messagesLoading: false,
    messagesError: null,
    generations: {},
    imagesById: {},
    composer: { fast: false },
    loadHistoricalMessages: null,
    sendMessage() {},
    retryAssistant() {},
    retryGeneration() {},
    regenerateAssistant() {},
    rerollImage() {},
    promoteImageToReference() {},
    setText() {},
    setMode() {},
    setFast() {},
  };
  const setCurrentCalls = [];
  const loadCalls = [];
  const loadGates = [];
  const navigateCalls = [];
  const deleteRequests = [];

  state.setCurrentConv = (conversationId) => {
    setCurrentCalls.push(conversationId);
    state.currentConvId = conversationId;
    state.messages = [];
    state.messagesLoading = false;
    state.messagesError = null;
  };
  state.loadHistoricalMessages = async (conversationId) => {
    loadCalls.push(conversationId);
    state.messagesLoading = true;
    state.messagesError = null;
    const gate = loadGates.shift();
    assert.ok(gate, "missing deferred history response");
    try {
      const messages = await gate.promise;
      state.messages = messages;
      state.messagesLoading = false;
    } catch (error) {
      state.messages = [];
      state.messagesLoading = false;
      state.messagesError =
        error instanceof Error ? error.message : String(error);
      throw error;
    }
  };

  const useChatStore = Object.assign(
    (selector) => selector(state),
    { getState: () => state },
  );
  const listQuery = {
    data: { pages: [{ items: [conversation] }] },
    hasNextPage: false,
    isFetchingNextPage: false,
    isLoading: false,
    isError: false,
    isFetchNextPageError: false,
    fetchNextPage() {},
    refetch() {},
  };
  const deleteMutation = {
    isPending: false,
    variables: undefined,
    mutate(id, options) {
      const gate = deferred();
      const completion = gate.promise.then(() => options.onSuccess?.());
      deleteRequests.push({ id, gate, completion });
    },
  };
  const queryMocks = {
    useListConversationsInfiniteQuery: () => listQuery,
    useCreateConversationMutation: () => ({
      isPending: false,
      isError: false,
      error: null,
      mutate() {},
    }),
    useDeleteConversationMutation: () => deleteMutation,
    usePatchConversationMutation: () => ({
      isPending: false,
      variables: undefined,
      mutate() {},
    }),
    useConversationContextQuery: () => ({
      data: null,
      refetch: async () => {},
    }),
  };
  const commonMocks = {
    react: reactMock(),
    "@/store/useChatStore": { useChatStore },
    "@/store/useUiStore": {
      useUiStore: (selector) => {
        const uiState = {
          sidebarOpen: true,
          toggleSidebar() {},
          setSidebarOpen() {},
          studioView: "chat",
          setStudioView() {},
        };
        return selector ? selector(uiState) : uiState;
      },
    },
    "@/lib/queries": queryMocks,
    "@/lib/logger": { logWarn() {} },
    "@/lib/utils": {
      cn: (...values) => values.filter(Boolean).join(" "),
    },
  };

  const sidebarModule = loadModule(
    "src/components/ui/sidebar/useSidebarController.ts",
    {
      ...commonMocks,
      "@/hooks/useBodyScrollLock": {
        acquireBodyScrollLock: () => () => {},
      },
      "./ConversationItem": {
        titleOf: (item) => item.title || "New conversation",
      },
    },
  );
  const desktopModule = loadModule(
    "src/components/ui/shell/DesktopStudio.tsx",
    {
      ...commonMocks,
      "framer-motion": {
        AnimatePresence: "AnimatePresence",
        motion: new Proxy(
          {},
          { get: (_target, key) => `motion.${String(key)}` },
        ),
      },
      "lucide-react": {
        PanelLeftOpen: "PanelLeftOpen",
        Plus: "Plus",
        X: "X",
      },
      "@/components/ui/shell/DesktopTopNav": {
        DesktopTopNav: "DesktopTopNav",
      },
      "@/components/ui/Sidebar": { Sidebar: "Sidebar" },
      "@/components/Onboarding": { Onboarding: "Onboarding" },
      "@/components/ui/composer/desktop": {
        DesktopComposerPill: "DesktopComposerPill",
      },
      "@/components/ui/primitives": {
        ErrorState: "ErrorState",
        IconButton: "IconButton",
        Spinner: "Spinner",
      },
      "@/components/ui/chat/desktop": {
        ConversationImageGallery: "ConversationImageGallery",
        DesktopConversationCanvas: "DesktopConversationCanvas",
      },
      "@/lib/motion": {
        DURATION: { instant: 0 },
        EASE: { shutter: "linear" },
        SPRING: { drawer: {} },
      },
      "@/hooks/useMediaQuery": { useMediaQuery: () => false },
      "./StudioContextBar": { StudioContextBar: "StudioContextBar" },
      "./useDefaultConversationSelection": {
        useDefaultConversationSelection() {},
      },
      "./useConversationRouteSync": {
        useConversationRouteSync: () => null,
      },
    },
  );

  return {
    conversation,
    state,
    setCurrentCalls,
    loadCalls,
    loadGates,
    navigateCalls,
    deleteRequests,
    renderSidebar() {
      return sidebarModule.useSidebarController({
        embedded: true,
        onNavigate: () => navigateCalls.push("navigate"),
      });
    },
    renderDesktop() {
      return desktopModule.DesktopStudio();
    },
  };
}

test("desktop history failure blocks composing and the active row retries", async () => {
  const harness = createHarness();
  const failedLoad = deferred();
  harness.loadGates.push(failedLoad);

  const controller = harness.renderSidebar();
  const selection = controller.selectConversation(harness.conversation);
  assert.equal(harness.state.currentConvId, harness.conversation.id);
  assert.equal(harness.state.messagesLoading, true);

  failedLoad.reject(new Error("history unavailable"));
  await selection;

  assert.equal(harness.state.messagesError, "history unavailable");
  assert.deepEqual(harness.navigateCalls, []);

  const failedTree = harness.renderDesktop();
  assert.equal(
    findElements(failedTree, (node) => node.type === "ErrorState").length,
    1,
  );
  assert.equal(
    findElements(
      failedTree,
      (node) => node.type === "DesktopComposerPill",
    ).length,
    0,
  );
  assert.equal(
    findElements(failedTree, (node) => node.type === "Onboarding").length,
    0,
  );

  const retryLoad = deferred();
  harness.loadGates.push(retryLoad);
  const retryController = harness.renderSidebar();
  const retry = retryController.selectConversation(harness.conversation);

  assert.deepEqual(harness.loadCalls, ["conv-a", "conv-a"]);
  assert.equal(harness.state.messagesLoading, true);
  assert.equal(harness.state.messagesError, null);

  const loadingTree = harness.renderDesktop();
  assert.equal(
    findElements(loadingTree, (node) => node.type === "Spinner").length,
    1,
  );
  assert.equal(
    findElements(
      loadingTree,
      (node) => node.type === "DesktopComposerPill",
    ).length,
    0,
  );

  retryLoad.resolve([
    { id: "message-1", role: "user", text: "Recovered history" },
  ]);
  await retry;

  const recoveredTree = harness.renderDesktop();
  assert.equal(
    findElements(
      recoveredTree,
      (node) => node.type === "DesktopConversationCanvas",
    ).length,
    1,
  );
  assert.equal(
    findElements(
      recoveredTree,
      (node) => node.type === "DesktopComposerPill",
    ).length,
    1,
  );
  assert.deepEqual(harness.navigateCalls, ["navigate"]);
});

test("desktop load-more failure keeps the canvas and composer available", () => {
  const harness = createHarness();
  harness.state.currentConvId = "conv-a";
  harness.state.messages = [
    { id: "message-1", role: "user", text: "Loaded history" },
  ];
  harness.state.messagesError = "older messages unavailable";

  const tree = harness.renderDesktop();

  assert.equal(
    findElements(tree, (node) => node.type === "ErrorState").length,
    0,
  );
  assert.equal(
    findElements(
      tree,
      (node) => node.type === "DesktopConversationCanvas",
    ).length,
    1,
  );
  assert.equal(
    findElements(
      tree,
      (node) => node.type === "DesktopComposerPill",
    ).length,
    1,
  );
});

test("a deferred delete cannot clear a newer live conversation", async () => {
  const harness = createHarness();
  harness.state.setCurrentConv("conv-a");
  harness.setCurrentCalls.length = 0;

  const controller = harness.renderSidebar();
  controller.deleteConversation(harness.conversation);
  assert.equal(harness.deleteRequests.length, 1);

  harness.state.setCurrentConv("conv-b");
  harness.setCurrentCalls.length = 0;
  const pendingDelete = harness.deleteRequests[0];
  pendingDelete.gate.resolve();
  await pendingDelete.completion;

  assert.equal(harness.state.currentConvId, "conv-b");
  assert.deepEqual(harness.setCurrentCalls, []);
});
