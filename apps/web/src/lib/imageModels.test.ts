import assert from "node:assert/strict";
import test from "node:test";
import {
  DEFAULT_IMAGE_MODEL,
  IMAGE_MODEL_OPTIONS,
  imageParamsForReroll,
  imageQualityOptions,
  normalizeImageModel,
} from "./imageModels.ts";

test("only 2.6 models are selectable, both support max quality", () => {
  assert.deepEqual(IMAGE_MODEL_OPTIONS.map((option) => option.value), [
    "gpt-image-2.6-flare", "gpt-image-2.6-sunburst",
  ]);
  for (const { value } of IMAGE_MODEL_OPTIONS) {
    assert.ok(imageQualityOptions(value).some((option) => option.value === "max"));
    assert.ok(imageQualityOptions(value).some((option) => option.value === "xhigh"));
    assert.equal(normalizeImageModel(value), value);
  }
});

test("reroll preserves active models and upgrades retired models without changing history", () => {
  for (const [oldModel, model] of [
    ["gpt-image-2", "gpt-image-2.6-flare"],
    ["gpt-image-2.5-flare", "gpt-image-2.6-flare"],
    ["gpt-image-2.5-sunburst", "gpt-image-2.6-sunburst"],
    ["gpt-image-2.6-flare", "gpt-image-2.6-flare"],
    ["gpt-image-2.6-sunburst", "gpt-image-2.6-sunburst"],
  ]) {
    const history = { requested_params: { model: oldModel, render_quality: "max" } };
    assert.deepEqual(imageParamsForReroll(history), { model, render_quality: "max" });
    assert.equal(history.requested_params.model, oldModel);
    assert.deepEqual(imageParamsForReroll({
      effective_params: { image_model: oldModel, render_quality: "xhigh" },
    }), { model, render_quality: "xhigh" });
  }
  assert.deepEqual(imageParamsForReroll({}), { model: DEFAULT_IMAGE_MODEL, render_quality: "high" });
  assert.equal(normalizeImageModel("unknown"), DEFAULT_IMAGE_MODEL);
});

test("Canvas accepts both added quality levels", async () => {
  const { validateCanvasNodeConfig } = await import("./canvas/registry-validation.ts");
  for (const render_quality of ["xhigh", "max"]) {
    assert.ok(validateCanvasNodeConfig("image_generate", {
      model: "gpt-image-2.6-flare", render_quality,
    }));
  }
});
