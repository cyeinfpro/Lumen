import assert from "node:assert/strict";
import test from "node:test";
const { nextCanvasAutoFitRequest: next, canApplyCanvasAutoFit: canApply } = await import("./autoFit.ts");
const trigger = { canvasId: "canvas-a", viewportToken: {}, nodeCount: 100, fullscreen: false, compact: false };

test("first ready viewport receives one initial fit", () => {
  const request = next(null, trigger);
  assert.equal(canApply(request, 0), true);
  request.completed = true;
  assert.equal(canApply(next(request, { ...trigger }), 0), false);
});
test("ending repeated gestures does not refit a completed layout", () => {
  const request = next(null, trigger);
  request.completed = true;
  for (const active of [1, 0, 2, 1, 0]) {
    const current = next(request, { ...trigger });
    assert.equal(current, request);
    assert.equal(canApply(current, active), false);
  }
});
test("a real layout change waits for an active gesture to finish", () => {
  const request = next(null, trigger);
  request.completed = true;
  const pending = next(request, { ...trigger, nodeCount: 101 });
  assert.equal(canApply(pending, 1), false);
  assert.equal(next(pending, { ...pending.trigger }), pending);
  assert.equal(canApply(pending, 0), true);
});
test("fullscreen changes create a fresh fit request", () => {
  const request = next(null, trigger); request.completed = true;
  assert.equal(canApply(next(request, { ...trigger, fullscreen: true }), 0), true);
});
test("responsive layout changes create a fresh fit request", () => {
  const request = next(null, trigger); request.completed = true;
  assert.equal(canApply(next(request, { ...trigger, compact: true }), 0), true);
});
test("new viewport and canvas identities cannot reuse an old completion", () => {
  const request = next(null, trigger); request.completed = true;
  assert.equal(canApply(next(request, { ...trigger, viewportToken: {} }), 0), true);
  assert.equal(canApply(next(request, { ...trigger, canvasId: "canvas-b" }), 0), true);
});
test("unavailable viewport waits rather than consuming initial fit", () => {
  const request = next(null, { ...trigger, viewportToken: null });
  assert.equal(canApply(request, 0), false);
  assert.equal(canApply(next(request, trigger), 0), true);
});
test("large canvases retain the existing automatic-fit size boundary", () => {
  assert.equal(canApply(next(null, { ...trigger, nodeCount: 200 }), 0), true);
  assert.equal(canApply(next(null, { ...trigger, nodeCount: 201 }), 0), false);
});
test("returning below the large-canvas boundary is a new layout request", () => {
  const initial = next(null, trigger); initial.completed = true;
  const large = next(initial, { ...trigger, nodeCount: 500 });
  assert.equal(canApply(large, 0), false);
  const returned = next(large, trigger);
  assert.notEqual(returned, initial);
  assert.equal(canApply(returned, 0), true);
});
test("newest layout identity replaces a pending callback identity", () => {
  const pending = next(null, trigger);
  const newer = next(pending, { ...trigger, nodeCount: 101 });
  assert.notEqual(newer, pending);
  assert.equal(newer.completed, false);
});
