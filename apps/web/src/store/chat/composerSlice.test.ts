import assert from "node:assert/strict";
import test from "node:test";
import type {
  ChatState,
  ChatStateGetter,
  ChatStateSetter,
} from "./types";
import "./moduleResolution.test-helper.mjs";

const { createComposerActions, createComposerState } = await import(
  new URL("./composerSlice.ts", import.meta.url).href
);

test("extracted composer actions preserve preferences while clearing drafts", () => {
  const initialComposer = {
    ...createComposerState(null),
    text: "draft",
    mode: "image" as const,
    params: {
      ...createComposerState(null).params,
      aspect_ratio: "16:9" as const,
    },
    reasoningEffort: "medium" as const,
    fast: false,
    webSearch: false,
    fileSearch: true,
    codeInterpreter: true,
    imageGeneration: true,
  };
  let state = {
    composer: initialComposer,
    composerError: "old error",
    imagesById: {},
  } as unknown as ChatState;
  const set: ChatStateSetter = (partial) => {
    const next = typeof partial === "function" ? partial(state) : partial;
    if (next === state) return;
    state = { ...state, ...next };
  };
  const get: ChatStateGetter = () => state;
  let fastTouched = 0;
  const actions = createComposerActions(set, get, {
    createInitialComposer: () => createComposerState(null),
    markFastTouched: () => {
      fastTouched += 1;
    },
  });

  actions.setFast(true);
  assert.equal(fastTouched, 1);
  actions.clearComposer();

  assert.equal(state.composer.text, "");
  assert.deepEqual(state.composer.attachments, []);
  assert.equal(state.composer.mode, "image");
  assert.equal(state.composer.params, initialComposer.params);
  assert.equal(state.composer.reasoningEffort, "medium");
  assert.equal(state.composer.fast, true);
  assert.equal(state.composer.webSearch, false);
  assert.equal(state.composer.fileSearch, true);
  assert.equal(state.composer.codeInterpreter, true);
  assert.equal(state.composer.imageGeneration, true);
});
