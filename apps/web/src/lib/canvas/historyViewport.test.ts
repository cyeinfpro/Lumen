import assert from "node:assert/strict";
import test from "node:test";
const { historyViewportRecoveryTargets: targets } = await import("./historyViewport.ts");
const view = { x: 0, y: 0, width: 800, height: 600 };
const a = { id: "a", x: 10, y: 20, width: 200, height: 180 };
const far = { ...a, x: 5000 };

test("layout undo recovers an existing node that leaves the entire viewport", () => {
  assert.deepEqual(targets([a], [far], view), ["a"]);
});
test("history does not fit while any result remains visible", () => {
  assert.deepEqual(targets([a, { ...a, id: "b" }], [far, { ...a, id: "b" }], view), []);
});
test("history preserves deliberately empty pre-action view", () => {
  assert.deepEqual(targets([far], [{ ...far, x: 6000 }], view), []);
});
test("config-only history and no-op history preserve camera", () => {
  assert.deepEqual(targets([a], [{ ...a }], view), []);
});
test("branch insertion and removal do not replace current viewport", () => {
  assert.deepEqual(targets([a], [{ ...far, id: "branch" }], view), []);
  assert.deepEqual(targets([a, { ...far, id: "branch" }], [a], view), []);
});
test("partly visible node still preserves camera", () => {
  assert.deepEqual(targets([a], [{ ...a, x: 790 }], view), []);
});
test("invalid or unavailable geometry fails closed", () => {
  assert.deepEqual(targets([a], [far], { ...view, width: 0 }), []);
  assert.deepEqual(targets([a], [{ ...far, x: Number.NaN }], view), []);
});
test("inspector closing may reveal the destination without a camera jump", () => {
  const destination = { ...a, x: 900 };
  assert.deepEqual(targets([a], [destination], view), ["a"]);
  assert.deepEqual(targets([a], [destination], { ...view, width: 1152 }), []);
});
test("inspector closing still recovers geometry outside the wider viewport", () => {
  assert.deepEqual(targets([a], [far], { ...view, width: 1152 }), ["a"]);
});
test("only moved existing nodes are reveal targets", () => {
  assert.deepEqual(targets([a], [far, { ...far, id: "new" }], view), ["a"]);
});
