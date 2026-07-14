import assert from "node:assert/strict";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";

import type { CanvasGraph, CanvasNodeDefinition } from "./types";

const { alignNodes, autoLayoutDag, distributeNodes } = await import(
  new URL("./layout.ts", import.meta.url).href
) as typeof import("./layout");

function node(
  id: string,
  x: number,
  y: number,
  width: number,
  height: number,
): CanvasNodeDefinition {
  return {
    id,
    type: "note",
    schema_version: 1,
    title: id,
    position: { x, y },
    size: { width, height },
    parent_group_id: null,
    config: { text: "", tags: [] },
    ui: { collapsed: false, color_tag: null },
  };
}

test("alignNodes accounts for node dimensions", () => {
  const nodes = [
    node("a", 0, 0, 100, 50),
    node("b", 200, 100, 50, 100),
  ];

  assert.deepEqual(alignNodes(nodes, "right"), [
    { nodeId: "a", position: { x: 150, y: 0 } },
    { nodeId: "b", position: { x: 200, y: 100 } },
  ]);
  assert.deepEqual(alignNodes(nodes, "vertical-center"), [
    { nodeId: "a", position: { x: 0, y: 75 } },
    { nodeId: "b", position: { x: 200, y: 50 } },
  ]);
});

test("alignNodes uses registry dimensions for legacy nodes without size", () => {
  const prompt: CanvasNodeDefinition = {
    ...node("prompt", 0, 0, 260, 180),
    type: "prompt",
    size: undefined,
  };
  const delivery: CanvasNodeDefinition = {
    ...node("delivery", 300, 100, 320, 180),
    type: "delivery",
    size: undefined,
  };

  assert.deepEqual(alignNodes([prompt, delivery], "right"), [
    { nodeId: "prompt", position: { x: 360, y: 0 } },
    { nodeId: "delivery", position: { x: 300, y: 100 } },
  ]);
});

test("distributeNodes keeps outer nodes fixed and creates equal gaps", () => {
  const nodes = [
    node("a", 0, 0, 100, 50),
    node("b", 120, 20, 50, 50),
    node("c", 300, 40, 100, 50),
  ];

  assert.deepEqual(distributeNodes(nodes, "horizontal"), [
    { nodeId: "a", position: { x: 0, y: 0 } },
    { nodeId: "b", position: { x: 175, y: 20 } },
    { nodeId: "c", position: { x: 300, y: 40 } },
  ]);
});

test("autoLayoutDag is deterministic across input order and rejects cycles", () => {
  const nodes = [
    node("source-b", 0, 200, 100, 80),
    node("source-a", 0, 0, 100, 80),
    node("target", 300, 100, 120, 80),
  ];
  const graph: CanvasGraph = {
    schema_version: 1,
    nodes,
    edges: [
      {
        id: "edge-b",
        source_node_id: "source-b",
        source_handle: "text",
        target_node_id: "target",
        target_handle: "prompt",
        data_type: "text",
        binding_mode: "follow_active",
      },
      {
        id: "edge-a",
        source_node_id: "source-a",
        source_handle: "text",
        target_node_id: "target",
        target_handle: "prompt",
        data_type: "text",
        binding_mode: "follow_active",
      },
    ],
    frames: [],
    settings: { snap_to_grid: false, grid_size: 16 },
  };
  const reversed = {
    ...structuredClone(graph),
    nodes: [...graph.nodes].reverse(),
    edges: [...graph.edges].reverse(),
  };

  const first = autoLayoutDag(graph, { rankGap: 100, nodeGap: 20 });
  assert.deepEqual(first, autoLayoutDag(reversed, { rankGap: 100, nodeGap: 20 }));
  const positions = new Map<string, { x: number; y: number }>(
    first.map((move) => [move.nodeId, move.position] as const),
  );
  assert.ok((positions.get("target")?.x ?? 0) > (positions.get("source-a")?.x ?? 0));

  const cyclic = structuredClone(graph);
  cyclic.edges.push({
    ...cyclic.edges[0],
    id: "edge-cycle",
    source_node_id: "target",
    target_node_id: "source-a",
  });
  assert.throws(() => autoLayoutDag(cyclic), /acyclic graph/);
});

test("layout rejects duplicate IDs and positions outside backend bounds", () => {
  const duplicate = [
    node("duplicate", 0, 0, 100, 80),
    node("duplicate", 200, 0, 100, 80),
  ];
  assert.throws(
    () => alignNodes(duplicate, "left"),
    /duplicate node ID/,
  );

  assert.throws(
    () =>
      distributeNodes(
        [
          node("a", -10_000_001, 0, 100, 80),
          node("b", 0, 0, 100, 80),
        ],
        "horizontal",
      ),
    /outside canvas bounds/,
  );
});

test("auto layout rejects generated coordinates beyond backend bounds", () => {
  const graph: CanvasGraph = {
    schema_version: 1,
    nodes: [
      node("source", 0, 0, 100, 80),
      node("target", 200, 0, 100, 80),
    ],
    edges: [
      {
        id: "edge",
        source_node_id: "source",
        source_handle: "text",
        target_node_id: "target",
        target_handle: "prompt",
        data_type: "text",
        binding_mode: "follow_active",
      },
    ],
    frames: [],
    settings: { snap_to_grid: false, grid_size: 16 },
  };

  assert.throws(
    () =>
      autoLayoutDag(graph, {
        origin: { x: 10_000_000, y: 0 },
        rankGap: 120,
      }),
    /outside canvas bounds/,
  );
});
