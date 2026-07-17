import assert from "node:assert/strict";
import { test } from "node:test";

import { projectMomentum, rubberBandDistance } from "./motion.ts";

test("projectMomentum preserves direction and uses the configured decay curve", () => {
  assert.equal(Math.round(projectMomentum(1_000)), 99);
  assert.equal(Math.round(projectMomentum(-1_000)), -99);
  assert.equal(projectMomentum(Number.NaN), 0);
  assert.equal(Math.round(projectMomentum(1_000, Number.NaN)), 99);
});

test("rubberBandDistance is continuous, directional, and progressively resistant", () => {
  assert.equal(rubberBandDistance(0, 120), 0);
  assert.ok(rubberBandDistance(80, 120) > 0);
  assert.ok(rubberBandDistance(-80, 120) < 0);
  assert.ok(Math.abs(rubberBandDistance(160, 120)) < 160);
  assert.ok(
    Math.abs(rubberBandDistance(160, 120)) >
      Math.abs(rubberBandDistance(80, 120)),
  );
  assert.ok(Number.isFinite(rubberBandDistance(80, Number.NaN)));
  assert.ok(Number.isFinite(rubberBandDistance(80, 120, Number.NaN)));
});
