import { deepEqual, equal } from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

type Conversation = { id: string; archived: boolean };
type SelectionOptions = {
  currentConvId: string | null;
  urlConversationId: string | null;
  conversations: readonly Conversation[];
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => unknown;
  loadHistoricalMessages: (id: string) => Promise<void>;
  setCurrentConv: (id: string | null) => void;
};

const source = readFileSync(new URL("./useDefaultConversationSelection.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;

function createHarness() {
  const effects: (() => void)[] = [];
  const selected: (string | null)[] = [];
  const history: string[] = [];
  let fetchedPages = 0;
  const state = {
    currentConvId: null as string | null,
    composerExpanded: false,
    composer: { text: "", attachments: [] as unknown[], mask: null as object | null },
  };
  const moduleRecord = {
    exports: {} as { useDefaultConversationSelection: (options: SelectionOptions) => void },
  };
  runInNewContext(compiled, {
    module: moduleRecord,
    exports: moduleRecord.exports,
    require(id: string) {
      if (id === "react") return { useEffect: (effect: () => void) => effects.push(effect) };
      if (id === "@/store/useChatStore") return { useChatStore: { getState: () => state } };
      if (id === "./conversationSelection") return {
        firstActiveConversation: (items: readonly Conversation[]) => items.find((item) => !item.archived) ?? null,
      };
      throw new Error(`Unexpected dependency: ${id}`);
    },
  });
  return {
    state,
    selected,
    history,
    fetchedPages: () => fetchedPages,
    render(overrides: Partial<SelectionOptions> = {}) {
      moduleRecord.exports.useDefaultConversationSelection({
        currentConvId: null,
        urlConversationId: null,
        conversations: [{ id: "latest", archived: false }],
        hasNextPage: false,
        isFetchingNextPage: false,
        fetchNextPage: () => { fetchedPages += 1; },
        loadHistoricalMessages: async (id) => { history.push(id); },
        setCurrentConv: (id) => { selected.push(id); state.currentConvId = id; },
        ...overrides,
      });
    },
    flushEffects() {
      for (const effect of effects.splice(0)) effect();
    },
  };
}

test("an untouched studio still opens the latest active conversation", () => {
  const harness = createHarness();
  harness.render();
  harness.flushEffects();
  deepEqual(harness.selected, ["latest"]);
  deepEqual(harness.history, ["latest"]);
  equal(harness.fetchedPages(), 0);
});

for (const text of ["雨夜东京街角，35mm 胶片质感", " "]) {
  test(`a draft entered after render prevents late default selection: ${JSON.stringify(text)}`, () => {
    const harness = createHarness();
    harness.render();
    // The response render is already queued when the user starts composing.
    harness.state.composer.text = text;
    harness.flushEffects();
    deepEqual(harness.selected, []);
    deepEqual(harness.history, []);
    equal(harness.state.composer.text, text);
  });
}

for (const content of ["attachment", "mask"] as const) {
  test(`an unsent ${content} prevents automatic conversation replacement`, () => {
    const harness = createHarness();
    harness.render();
    if (content === "attachment") harness.state.composer.attachments.push({ id: "reference" });
    else harness.state.composer.mask = { imageId: "reference" };
    harness.flushEffects();
    deepEqual(harness.selected, []);
    deepEqual(harness.history, []);
  });
}

test("a conversation selected after render is not replaced by stale effect props", () => {
  const harness = createHarness();
  harness.render();
  harness.state.currentConvId = "chosen-by-user";
  harness.flushEffects();
  deepEqual(harness.selected, []);
  deepEqual(harness.history, []);
  equal(harness.state.currentConvId, "chosen-by-user");
});

test("explicitly opening an empty composer prevents late default selection", () => {
  const harness = createHarness();
  harness.render();
  harness.state.composerExpanded = true;
  harness.flushEffects();
  deepEqual(harness.selected, []);
  deepEqual(harness.history, []);
});

test("explicit route selection remains authoritative", () => {
  const harness = createHarness();
  harness.render({ urlConversationId: "from-link" });
  harness.flushEffects();
  deepEqual(harness.selected, []);
  deepEqual(harness.history, []);
});

test("archive-only pagination continues only while the studio is untouched", () => {
  for (const draftStarted of [false, true]) {
    const harness = createHarness();
    harness.render({ conversations: [{ id: "archived", archived: true }], hasNextPage: true });
    if (draftStarted) harness.state.composer.text = "Keep this draft";
    harness.flushEffects();
    deepEqual(harness.selected, []);
    equal(harness.fetchedPages(), draftStarted ? 0 : 1);
  }
});
