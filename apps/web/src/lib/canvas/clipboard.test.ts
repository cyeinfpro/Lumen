import assert from "node:assert/strict";
import test from "node:test";

const {
  MAX_CANVAS_GRAPH_BYTES,
  MAX_CANVAS_NODE_CONFIG_BYTES,
  createDefaultCanvasGraph,
} = await import("#canvas-graph");
const { createCanvasNode } = await import("#canvas-registry");
const {
  CANVAS_CLIPBOARD_PREFIX,
  copySubgraph,
  insertSubgraph,
  parseCanvasSubgraph,
  serializeCanvasSubgraph,
} = await import(
  new URL("./clipboard.ts", import.meta.url).href
) as typeof import("./clipboard");

test("copySubgraph keeps only internal edges and insertion remaps every entity ID", () => {
  const graph = createDefaultCanvasGraph();
  const subgraph = copySubgraph(graph, ["prompt-1", "image-generate-1"]);
  const result = insertSubgraph(graph, subgraph, {
    offset: { x: 40, y: 60 },
    idFactory: (kind: string, sourceId: string, attempt: number) =>
      `${kind}-copy-${attempt}-${sourceId}`,
  });

  assert.equal(subgraph.nodes.length, 2);
  assert.equal(subgraph.edges.length, 1);
  assert.equal(graph.nodes.length, 2);
  assert.equal(graph.edges.length, 1);
  assert.deepEqual(result.nodeIdMap, {
    "prompt-1": "node-copy-0-prompt-1",
    "image-generate-1": "node-copy-0-image-generate-1",
  });
  assert.deepEqual(result.edgeIdMap, {
    "edge-prompt-image": "edge-copy-0-edge-prompt-image",
  });
  assert.deepEqual(result.nodes[0].position, { x: 120, y: 220 });
  assert.equal(result.edges[0].source_node_id, result.nodes[0].id);
  assert.equal(result.edges[0].target_node_id, result.nodes[1].id);
  assert.notEqual(result.edges[0].id, subgraph.edges[0].id);
});

test("copySubgraph excludes edges that leave the selected node set", () => {
  const graph = createDefaultCanvasGraph();
  const subgraph = copySubgraph(graph, ["prompt-1"]);

  assert.deepEqual(subgraph.nodes.map((node) => node.id), ["prompt-1"]);
  assert.deepEqual(subgraph.edges, []);
});

test("canvas clipboard text round trips and rejects malformed graphs", () => {
  const graph = createDefaultCanvasGraph();
  const copied = copySubgraph(graph, graph.nodes.map((node) => node.id));
  const mismatchedEdge = structuredClone(copied);
  mismatchedEdge.edges[0].data_type = "video";

  assert.deepEqual(parseCanvasSubgraph(serializeCanvasSubgraph(copied)), copied);
  assert.equal(
    parseCanvasSubgraph(
      `${CANVAS_CLIPBOARD_PREFIX}${JSON.stringify(mismatchedEdge)}`,
    ),
    null,
  );
  assert.equal(parseCanvasSubgraph("plain text"), null);
  assert.equal(
    parseCanvasSubgraph(
      `${CANVAS_CLIPBOARD_PREFIX}${JSON.stringify({
        schema_version: 1,
        nodes: [],
        edges: [{ id: "edge-orphan" }],
      })}`,
    ),
    null,
  );
});

test("insertSubgraph refuses to exceed the canvas node limit", () => {
  const graph = createDefaultCanvasGraph();
  const source = graph.nodes[0];
  const fullGraph = {
    ...graph,
    nodes: Array.from({ length: 1_000 }, (_, index) => ({
      ...structuredClone(source),
      id: `note-${index}`,
    })),
    edges: [],
  };
  const result = insertSubgraph(
    fullGraph,
    copySubgraph(graph, [graph.nodes[1].id]),
  );

  assert.equal(result.graph, fullGraph);
  assert.deepEqual(result.nodes, []);
  assert.deepEqual(result.edges, []);
});

test("clipboard enforces raw byte, config byte, and value depth limits", () => {
  assert.equal(
    parseCanvasSubgraph(
      `${CANVAS_CLIPBOARD_PREFIX}${" ".repeat(MAX_CANVAS_GRAPH_BYTES + 1)}`,
    ),
    null,
  );

  const graph = createDefaultCanvasGraph();
  const oversized = copySubgraph(graph, ["prompt-1"]);
  oversized.nodes[0].config = {
    text: "界".repeat(Math.ceil(MAX_CANVAS_NODE_CONFIG_BYTES / 3)),
  };
  assert.throws(
    () => serializeCanvasSubgraph(oversized),
    /config is too large/,
  );

  const deeplyNested = copySubgraph(graph, ["prompt-1"]);
  let nested: Record<string, unknown> = {};
  deeplyNested.nodes[0].config = nested;
  for (let depth = 0; depth < 40; depth += 1) {
    const child: Record<string, unknown> = {};
    nested.child = child;
    nested = child;
  }
  assert.throws(
    () => serializeCanvasSubgraph(deeplyNested),
    /subgraph is invalid/,
  );
});

test("clipboard rejects non-finite geometry and duplicate source IDs", () => {
  const graph = createDefaultCanvasGraph();
  const invalidGeometry = copySubgraph(graph, ["prompt-1"]);
  invalidGeometry.nodes[0].position.x = Number.POSITIVE_INFINITY;
  assert.throws(
    () => serializeCanvasSubgraph(invalidGeometry),
    /outside canvas bounds/,
  );

  const duplicateIds = copySubgraph(graph, [
    "prompt-1",
    "image-generate-1",
  ]);
  duplicateIds.nodes[1].id = duplicateIds.nodes[0].id;
  assert.equal(
    parseCanvasSubgraph(
      `${CANVAS_CLIPBOARD_PREFIX}${JSON.stringify(duplicateIds)}`,
    ),
    null,
  );
});

test("clipboard validates frame parents, cycles, and backend group depth", () => {
  const graph = createDefaultCanvasGraph();
  const unknownParent = copySubgraph(graph, ["prompt-1"]);
  unknownParent.nodes[0].parent_group_id = "missing-frame";
  assert.equal(
    parseCanvasSubgraph(
      `${CANVAS_CLIPBOARD_PREFIX}${JSON.stringify(unknownParent)}`,
    ),
    null,
  );

  const nonFrameParent = copySubgraph(graph, [
    "prompt-1",
    "image-generate-1",
  ]);
  nonFrameParent.nodes[1].parent_group_id = "prompt-1";
  assert.equal(
    parseCanvasSubgraph(
      `${CANVAS_CLIPBOARD_PREFIX}${JSON.stringify(nonFrameParent)}`,
    ),
    null,
  );

  const frames = Array.from({ length: 6 }, (_, index) =>
    createCanvasNode("frame", { x: index * 100, y: 0 }, {
      id: `frame-${index}`,
      parent_group_id: index === 0 ? null : `frame-${index - 1}`,
    }),
  );
  const tooDeep = {
    schema_version: 1 as const,
    nodes: frames,
    edges: [],
  };
  assert.equal(
    parseCanvasSubgraph(
      `${CANVAS_CLIPBOARD_PREFIX}${JSON.stringify(tooDeep)}`,
    ),
    null,
  );

  frames[1].parent_group_id = frames[1].id;
  assert.equal(
    parseCanvasSubgraph(
      `${CANVAS_CLIPBOARD_PREFIX}${JSON.stringify({
        ...tooDeep,
        nodes: frames.slice(0, 2),
      })}`,
    ),
    null,
  );
});

test("clipboard ID remapping never falls through Object.prototype", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes.push(
    createCanvasNode("frame", { x: 0, y: 0 }, { id: "constructor" }),
  );
  const child = createCanvasNode("note", { x: 120, y: 120 }, {
    id: "child",
    parent_group_id: "constructor",
  });
  const result = insertSubgraph(
    graph,
    { schema_version: 1, nodes: [child], edges: [] },
    {
      idFactory: () => "child-copy",
    },
  );

  assert.equal(result.nodes[0]?.parent_group_id, "constructor");
  assert.equal(result.nodes[0]?.id, "child-copy");
});
