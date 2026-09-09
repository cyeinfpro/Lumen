import assert from "node:assert/strict";
import test from "node:test";

const {
  DEFAULT_PARAMS,
  clampImageCount,
  normalizeImageParams,
  normalizeRenderQuality,
} = await import(
  new URL("./imageParams.ts", import.meta.url).href
);

test("image count normalization clamps and truncates invalid values", () => {
  assert.equal(clampImageCount(undefined), 1);
  assert.equal(clampImageCount(Number.NaN), 1);
  assert.equal(clampImageCount(-3), 1);
  assert.equal(clampImageCount(4.9), 4);
  assert.equal(clampImageCount(99), 10);
});

test("image params clamp output compression without mutating defaults", () => {
  assert.deepEqual(
    normalizeImageParams({
      ...DEFAULT_PARAMS,
      count: 12,
      output_compression: 104.8,
    }),
    {
      ...DEFAULT_PARAMS,
      count: 10,
      output_compression: 100,
    },
  );
  assert.equal(DEFAULT_PARAMS.output_compression, undefined);
  assert.equal(DEFAULT_PARAMS.background, "opaque");
});

test("render quality normalization uses the high-quality fallback", () => {
  assert.equal(normalizeRenderQuality("low"), "low");
  assert.equal(normalizeRenderQuality("medium"), "medium");
  assert.equal(normalizeRenderQuality("high"), "high");
  assert.equal(normalizeRenderQuality(undefined), "high");
  assert.equal(normalizeRenderQuality("invalid"), "high");
});


test("new model quality survives persistence and switching back clamps unsupported quality", () => {
  for (const model of ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst"] as const) {
    for (const render_quality of ["low", "medium", "high", "xhigh", "max"] as const) {
      const params = normalizeImageParams({ ...DEFAULT_PARAMS, model, render_quality });
      assert.equal(params.model, model);
      assert.equal(params.render_quality, render_quality);
      const original = normalizeImageParams({ ...params, model: "gpt-image-2" });
      assert.equal(original.render_quality, ["xhigh", "max"].includes(render_quality) ? "high" : render_quality);
    }
  }
});
