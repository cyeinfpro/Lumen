import assert from "node:assert/strict";
import test from "node:test";

const {
  canvasNodeZIndex,
  centeredCanvasNodePosition,
  splitCanvasNodePositionChanges,
  updateCanvasTransientPositions,
} = await import("#canvas-interaction");

test("canvas position changes separate live drag frames from settled moves", () => {
  assert.deepEqual(
    splitCanvasNodePositionChanges([
      { id: "dragging", position: { x: 10, y: 20 }, dragging: true },
      { id: "keyboard", position: { x: 30, y: 40 } },
      { id: "dropped", position: { x: 50, y: 60 }, dragging: false },
    ]),
    {
      transient: [{ nodeId: "dragging", position: { x: 10, y: 20 } }],
      settled: [
        { nodeId: "keyboard", position: { x: 30, y: 40 } },
        { nodeId: "dropped", position: { x: 50, y: 60 } },
      ],
    },
  );
});

test("transient canvas positions only change state when values change", () => {
  const initial = { dragging: { x: 10, y: 20 } };
  assert.equal(
    updateCanvasTransientPositions(
      initial,
      [{ nodeId: "dragging", position: { x: 10, y: 20 } }],
      ["missing"],
    ),
    initial,
  );

  const moved = updateCanvasTransientPositions(
    initial,
    [{ nodeId: "dragging", position: { x: 15, y: 25 } }],
    [],
  );
  assert.notEqual(moved, initial);
  assert.deepEqual(moved, { dragging: { x: 15, y: 25 } });
  assert.deepEqual(
    updateCanvasTransientPositions(moved, [], ["dragging"]),
    {},
  );
});

test("canvas frames remain below normal nodes and new nodes center in the viewport", () => {
  assert.ok(canvasNodeZIndex("frame") < canvasNodeZIndex("note"));
  assert.deepEqual(
    centeredCanvasNodePosition({
      center: { x: 600, y: 400 },
      width: 300,
      height: 180,
      offset: 24,
    }),
    { x: 474, y: 334 },
  );
});
