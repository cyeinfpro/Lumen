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


test("SVG author stays independent, survives clones, and is omitted when disabled or non-SVG", () => {
  let state = { composer: createComposerState(null), imagesById: {} };
  const actions = createComposerActions((partial: unknown) => {
    state = { ...state, ...(typeof partial === "function" ? partial(state) : partial as object) };
  }, () => state, { createInitialComposer: () => createComposerState(null), markFastTouched() {} });
  actions.setImageOutputFormat("svg");
  actions.setImageSvgAuthor({ model: "custom-author", reasoning: { effort: "max" } });
  actions.setImageComplexity("low");
  const copy = cloneComposerState(state.composer);
  assert.deepEqual(copy.params.svg, { model: "custom-author", reasoning: { effort: "max" } });
  assert.notEqual(copy.params.svg, state.composer.params.svg);
  assert.equal(state.composer.reasoningEffort, "high");
  assert.equal(normalizeImageParams(copy.params).svg.model, "custom-author");
  for (const output_format of ["png", "jpeg", "webp", "gif"]) {
    assert.equal("svg" in normalizeImageParams({ ...copy.params, output_format }), false);
  }
  actions.setImageOutputFormat("png");
  actions.setImageOutputFormat("svg");
  assert.equal(state.composer.params.svg.model, "custom-author");
  for (const svg of [undefined, null, { model: "" }, { model: "  ", reasoning: { effort: "max" } }]) {
    assert.equal("svg" in normalizeImageParams({ ...copy.params, svg }), false);
  }
  for (const effort of ["none", "minimal", "low", "medium", "high", "xhigh", "max"]) {
    assert.deepEqual(normalizeImageParams({ ...copy.params, svg: { model: " user-id ", reasoning: { effort } } }).svg,
      { model: "user-id", reasoning: { effort } });
  }
});

test("reroll preserves SVG author config and canvas validates it strictly", async () => {
  const { rerollImageOptions } = await import(new URL("./rerollImageOptions.ts", import.meta.url).href);
  const { validateCanvasNodeConfig } = await import(new URL("../../lib/canvas/registry-validation.ts", import.meta.url).href);
  const svg = { model: "author-id", reasoning: { effort: "none" } };
  assert.deepEqual(rerollImageOptions({ requested_params: { output_format: "svg", svg } }).svg, svg);
  assert.equal(rerollImageOptions({ requested_params: { output_format: "png", svg } }).svg, undefined);
  assert.equal(validateCanvasNodeConfig("image_generate", { output_format: "svg", svg }), true);
  assert.equal(validateCanvasNodeConfig("image_generate", { output_format: "png", svg }), false);
  assert.equal(validateCanvasNodeConfig("image_generate", { output_format: "svg", svg: { model: " " } }), false);
  assert.equal(validateCanvasNodeConfig("image_generate", { output_format: "svg", svg: { model: "id", reasoning: { effort: "auto" } } }), false);
});


test("GIF custom and auto sizing survive format switches without static resolution overrides", async () => {
  const { gifSizeError, resolveGifSize } = await import(new URL("../../lib/gifSize.ts", import.meta.url).href);
  let state = { composer: createComposerState(null), imagesById: {} };
  const actions = createComposerActions((partial: unknown) => {
    state = { ...state, ...(typeof partial === "function" ? partial(state) : partial as object) };
  }, () => state, { createInitialComposer: () => createComposerState(null), markFastTouched() {} });
  actions.setImageOutputFormat("gif");
  actions.setFixedSize("768x432");
  actions.setImageOutputFormat("png");
  actions.setImageOutputFormat("gif");
  assert.equal(state.composer.params.fixed_size, "768x432");
  assert.equal(gifSizeError(state.composer.params), null);
  assert.deepEqual(resolveGifSize(state.composer.params), { size_mode: "fixed", fixed_size: "768x432" });
  actions.setFixedSize("767x432");
  assert.match(gifSizeError(state.composer.params), /16/);
  actions.setSizeMode("auto");
  actions.setImageOutputFormat("png");
  actions.setImageOutputFormat("gif");
  assert.equal(gifSizeError(state.composer.params), null);
  assert.deepEqual(resolveGifSize(state.composer.params), { size_mode: "auto", fixed_size: undefined });
});

test("GIF presets retain explicit pairs without inventing a grid budget", async () => {
  const { GIF_SIZE_PRESETS, gifPresetId, gifSizeError } = await import(new URL("../../lib/gifSize.ts", import.meta.url).href);
  assert.deepEqual(GIF_SIZE_PRESETS.map((p: { size: string; frames: number }) => [p.size, p.frames]),
    [["512x512", 2], ["512x512", 4], ["512x512", 9], ["512x512", 16], ["960x960", 9], ["768x432", 16]]);
  for (const preset of GIF_SIZE_PRESETS) {
    const params = { ...DEFAULT_PARAMS, output_format: "gif", fixed_size: preset.size, frame_count: preset.frames };
    assert.equal(gifPresetId(params), preset.id);
    assert.equal(gifSizeError(params), null);
    assert.equal(gifPresetId({ ...params, size_mode: "auto" }), "");
    assert.equal(gifPresetId({ ...params, frame_count: 3 }), "");
  }
  // Base validation is not acceptance: provider rejected these aligned combinations.
  assert.equal(gifSizeError({ ...DEFAULT_PARAMS, output_format: "gif", fixed_size: "736x736", frame_count: 16 }), null);
});

test("GIF timing previews exact requested encoding and rejects off-grid or out-of-product values", async () => {
  const { gifTimingError, gifTimingPreview } = await import(new URL("../../lib/gifSize.ts", import.meta.url).href);
  const params = { ...DEFAULT_PARAMS, output_format: "gif", frame_count: 16, frame_duration_ms: 20 };
  assert.deepEqual(gifTimingPreview(params), { fps: 50, loopSeconds: 0.32 });
  assert.deepEqual(gifTimingPreview({ ...params, frame_count: 9, frame_duration_ms: 200 }), { fps: 5, loopSeconds: 1.8 });
  assert.deepEqual(gifTimingPreview({ ...params, frame_count: 2, frame_duration_ms: 2000 }), { fps: 0.5, loopSeconds: 4 });
  for (const invalid of [{ frame_count: 1 }, { frame_count: 17 }, { frame_count: NaN }, { frame_duration_ms: 33 }, { frame_duration_ms: 2100 }]) {
    assert.ok(gifTimingError({ ...params, ...invalid }));
    assert.equal(gifTimingPreview({ ...params, ...invalid }), null);
  }
  assert.equal(gifTimingError({ ...params, output_format: "png", frame_count: 100 }), null);
});
