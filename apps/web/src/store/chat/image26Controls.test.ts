import assert from "node:assert/strict";
import test from "node:test";
import "./moduleResolution.test-helper.mjs";

const { createComposerActions, createComposerState, cloneComposerState } = await import(
  new URL("./composerSlice.ts", import.meta.url).href
);
const { normalizeImageParams, DEFAULT_PARAMS } = await import(
  new URL("./imageParams.ts", import.meta.url).href
);

test("complexity is independent, explicit, and absent in defaults", () => {
  let state = { composer: createComposerState(null), imagesById: {} };
  const actions = createComposerActions((partial: unknown) => {
    state = { ...state, ...(typeof partial === "function" ? partial(state) : partial as object) };
  }, () => state, { createInitialComposer: () => createComposerState(null), markFastTouched() {} });
  assert.equal(state.composer.params.complexity, undefined);
  const reasoning = state.composer.reasoningEffort;
  actions.setImageComplexity("low");
  assert.equal(state.composer.params.complexity, "low");
  assert.equal(state.composer.reasoningEffort, reasoning);
  actions.setImageComplexity(undefined);
  assert.equal(state.composer.params.complexity, undefined);
});

test("markup follows only first reference and never masquerades as a mask", () => {
  let state = { composer: createComposerState(null), imagesById: {} };
  const actions = createComposerActions((partial: unknown) => {
    state = { ...state, ...(typeof partial === "function" ? partial(state) : partial as object) };
  }, () => state, { createInitialComposer: () => createComposerState(null), markFastTouched() {} });
  const reference = (id: string) => ({ id, kind: "uploaded", data_url: "data:image/png;base64,aA==", mime: "image/png", width: 2, height: 2 });
  actions.addAttachment(reference("first"));
  actions.setMask({ image_id: "old-mask", preview_data_url: "", target_attachment_id: "first" });
  actions.setMarkup({ image_id: "annotation", preview_data_url: "", target_attachment_id: "first" });
  assert.equal(state.composer.mask, null);
  actions.addAttachment(reference("second"));
  assert.equal(state.composer.markup.image_id, "annotation");
  assert.equal(cloneComposerState(state.composer).markup.image_id, "annotation");
  actions.moveAttachment("second", "first");
  assert.equal(state.composer.markup, null);
  actions.setMarkup({ image_id: "stale", preview_data_url: "", target_attachment_id: "first" });
  assert.equal(state.composer.markup, null);
  actions.setMarkup({ image_id: "new", preview_data_url: "", target_attachment_id: "second" });
  actions.removeAttachment("second");
  assert.equal(state.composer.markup, null);
  actions.clearComposer();
  assert.equal(state.composer.markup ?? null, null);
});

test("GIF parameters normalize while raster clears animation controls", () => {
  const gif = normalizeImageParams({ ...DEFAULT_PARAMS, output_format: "gif", count: 9, output_compression: 80, frame_count: 99, frame_duration_ms: 123 });
  assert.equal(gif.count, 1);
  assert.equal(gif.output_compression, undefined);
  assert.equal(gif.frame_count, 16);
  assert.equal(gif.frame_duration_ms, 120);
  const raster = normalizeImageParams({ ...gif, output_format: "png" });
  assert.equal(raster.frame_count, undefined);
  assert.equal(raster.frame_duration_ms, undefined);
  assert.equal(normalizeImageParams({ ...DEFAULT_PARAMS, complexity: "auto" }).complexity, undefined);
});
