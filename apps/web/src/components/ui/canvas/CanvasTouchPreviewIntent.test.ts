import assert from "node:assert/strict";
import test from "node:test";
import { CanvasTouchPreviewIntent } from "./CanvasTouchPreviewIntent.ts";

const down = { pointerId: 1, pointerType: "touch", isPrimary: true, nodeId: "n-55", x: 193, y: 431, now: 100 };
const up = { pointerId: 1, x: 193, y: 431, now: 120 };
const click = { x: 193, y: 431, now: 138, detail: 1, preview: true };
function tapped() { const g = new CanvasTouchPreviewIntent(); g.down(down); g.up(up); return g; }

test("stationary header touch associates one nearby preview compatibility click", () => {
  const g = tapped();
  assert.equal(g.consume(click), "n-55");
  assert.equal(g.consume(click), null);
});
test("retarget click may be nearby rather than exactly at the original SVG pixel", () => {
  assert.equal(tapped().consume({ ...click, x: click.x + 16 }), "n-55");
  assert.equal(tapped().consume({ ...click, x: click.x + 25 }), null);
});
test("actual preview touch has no eligible header origin and remains unchanged", () => {
  const g = new CanvasTouchPreviewIntent(); g.down({ ...down, nodeId: null }); g.up(up);
  assert.equal(g.consume(click), null);
});
test("keyboard detail-zero and non-preview activation never get intercepted", () => {
  assert.equal(tapped().consume({ ...click, detail: 0 }), null);
  assert.equal(tapped().consume({ ...click, preview: false }), null);
});
test("mouse or pen pointerdown clears a previous touch association", () => {
  for (const pointerType of ["mouse", "pen"]) {
    const g = tapped(); g.down({ ...down, pointerType, now: 130 });
    assert.equal(g.consume(click), null);
  }
});
test("no pointerup, expired compatibility click, and backwards timestamps are refused", () => {
  const g = new CanvasTouchPreviewIntent(); g.down(down);
  assert.equal(g.consume(click), null);
  assert.equal(tapped().consume({ ...click, now: 621 }), null);
  assert.equal(tapped().consume({ ...click, now: 119 }), null);
});
test("movement invalidates intent even when the finger returns to its starting point", () => {
  const g = new CanvasTouchPreviewIntent(); g.down(down);
  g.move({ ...up, x: up.x + 9, now: 110 }); g.up(up);
  assert.equal(g.consume(click), null);
});
test("pointerup displacement and long presses are not taps", () => {
  for (const end of [{ ...up, y: up.y + 9 }, { ...up, now: 801 }]) {
    const g = new CanvasTouchPreviewIntent(); g.down(down); g.up(end);
    assert.equal(g.consume({ ...click, now: 820 }), null);
  }
});
test("second touch invalidates the gesture until every pointer has ended", () => {
  const g = new CanvasTouchPreviewIntent(); g.down(down);
  g.down({ ...down, pointerId: 2, isPrimary: false, now: 105 }); g.up({ ...up, pointerId: 2 }); g.up(up);
  assert.equal(g.consume(click), null);
  g.down({ ...down, now: 150 }); g.up({ ...up, now: 170 });
  assert.equal(g.consume({ ...click, now: 190 }), "n-55");
});
test("non-primary and cancelled touches never select", () => {
  const g = new CanvasTouchPreviewIntent(); g.down({ ...down, isPrimary: false }); g.up(up);
  assert.equal(g.consume(click), null);
  const cancelled = tapped(); cancelled.clear(); assert.equal(cancelled.consume(click), null);
});
test("a newer direct media touch replaces the previous header gesture", () => {
  const g = tapped(); g.down({ ...down, nodeId: null, now: 150 }); g.up({ ...up, now: 170 });
  assert.equal(g.consume({ ...click, now: 190 }), null);
});
test("external pointer end or blur clears stranded active pointers before the next touch", () => {
  const g = new CanvasTouchPreviewIntent(); g.down(down);
  g.down({ ...down, pointerId: 2, isPrimary: false });
  g.clear();
  g.down({ ...down, pointerId: 3, now: 200 }); g.up({ ...up, pointerId: 3, now: 220 });
  assert.equal(g.consume({ ...click, now: 240 }), "n-55");
});
test("a second finger outside the surface invalidates the first gesture", () => {
  const g = new CanvasTouchPreviewIntent(); g.down(down);
  g.clear(); // Window capture observes the outside pointerdown.
  g.up(up);
  assert.equal(g.consume(click), null);
  g.down({ ...down, pointerId: 3, now: 200 }); g.up({ ...up, pointerId: 3, now: 220 });
  assert.equal(g.consume({ ...click, now: 240 }), "n-55");
});
test("blur after pointerup discards its pending compatibility click", () => {
  const g = tapped(); g.clear(); assert.equal(g.consume(click), null);
  g.down({ ...down, now: 200 }); g.up({ ...up, now: 220 });
  assert.equal(g.consume({ ...click, now: 240 }), "n-55");
});
test("nonfinite coordinates and time cannot create or consume an intent", () => {
  for (const key of ["x", "y", "now"] as const) {
    const g = new CanvasTouchPreviewIntent(); g.down({ ...down, [key]: NaN }); g.up(up);
    assert.equal(g.consume(click), null);
    assert.equal(tapped().consume({ ...click, [key]: Infinity }), null);
  }
});
