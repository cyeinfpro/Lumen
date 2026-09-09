import assert from "node:assert/strict";
import test from "node:test";
import { imageParamsForReroll } from "./imageModels.ts";

test("reroll preserves the original model and quality from API diagnostics", () => {
  assert.deepEqual(imageParamsForReroll({ requested_params: { model: "gpt-image-2.5-sunburst", render_quality: "max" } }), { model: "gpt-image-2.5-sunburst", render_quality: "max" });
  assert.deepEqual(imageParamsForReroll({ effective_params: { image_model: "gpt-image-2.5-flare", render_quality: "xhigh" } }), { model: "gpt-image-2.5-flare", render_quality: "xhigh" });
  assert.deepEqual(imageParamsForReroll({}), { model: "gpt-image-2", render_quality: "high" });
});
