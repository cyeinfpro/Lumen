import assert from "node:assert/strict";
import test from "node:test";

const rendering = await import(
  new URL("./canvasRendering.ts", import.meta.url).href
) as typeof import("./canvasRendering");

test("mask alpha threshold is binary and reports transparent coverage", () => {
  const data = new Uint8ClampedArray([
    255, 255, 255, 0,
    255, 255, 255, 127,
    255, 255, 255, 128,
    255, 255, 255, 255,
  ]);

  assert.equal(rendering.thresholdMaskAlpha(data), 0.5);
  assert.deepEqual(
    [data[3], data[7], data[11], data[15]],
    [0, 0, 255, 255],
  );
});

test("mask preview keeps small canvases and limits large canvases to 512px", () => {
  assert.deepEqual(rendering.previewDimensions(320, 240), {
    width: 320,
    height: 240,
  });
  assert.deepEqual(rendering.previewDimensions(2000, 1000), {
    width: 512,
    height: 256,
  });
  assert.deepEqual(rendering.previewDimensions(1, 4000), {
    width: 1,
    height: 512,
  });
});
