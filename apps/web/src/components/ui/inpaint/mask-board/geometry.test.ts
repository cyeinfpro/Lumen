import assert from "node:assert/strict";
import test from "node:test";

const geometry = await import(
  new URL("./geometry.ts", import.meta.url).href
) as typeof import("./geometry");

test("brush and view geometry stay inside supported bounds", () => {
  assert.equal(geometry.clampBrush(2), geometry.MIN_BRUSH);
  assert.equal(geometry.clampBrush(120), geometry.MAX_BRUSH);
  assert.equal(geometry.clampBrush(35.6), 36);
  assert.deepEqual(
    geometry.clampViewTransform({ x: -200, y: 20, scale: 2 }, 100, 80),
    { x: -100, y: 0, scale: 2 },
  );
  assert.deepEqual(
    geometry.clampViewTransform({ x: -20, y: -20, scale: 0.5 }, 100, 80),
    { x: 0, y: 0, scale: 1 },
  );
});

test("stage and image coordinates preserve the active view transform", () => {
  const view = { x: -40, y: -20, scale: 2 };
  const dimensions = { width: 300, height: 200, scale: 0.5 };
  const imagePoint = geometry.imagePointFromStagePoint(
    { x: 160, y: 100 },
    view,
    dimensions,
  );
  assert.deepEqual(imagePoint, { x: 100, y: 60 });
  assert.deepEqual(
    geometry.stagePointFromImagePoint(imagePoint, view),
    { x: 160, y: 100 },
  );
});

test("display dimensions fit both the measured container and max edge", () => {
  assert.deepEqual(
    geometry.displayDimensions(
      { naturalWidth: 1600, naturalHeight: 800 },
      { w: 600, h: 400 },
    ),
    { width: 600, height: 300, scale: 0.375 },
  );
  assert.deepEqual(
    geometry.displayDimensions(
      { naturalWidth: 2000, naturalHeight: 1000 },
      null,
    ),
    { width: 768, height: 384, scale: 0.384 },
  );
});

test("pen pressure and image luminance keep existing mask feedback behavior", () => {
  assert.equal(
    geometry.effectiveBrushRadius(
      { pointerType: "pen", pressure: 0.5 } as PointerEvent,
      40,
    ),
    28,
  );
  assert.equal(
    geometry.effectiveBrushRadius(
      { pointerType: "mouse", pressure: 0.5 } as PointerEvent,
      40,
    ),
    40,
  );
  assert.equal(geometry.maskColorsForLuminance(0.2).isDarkBg, true);
  assert.equal(geometry.maskColorsForLuminance(0.8).isDarkBg, false);
});
