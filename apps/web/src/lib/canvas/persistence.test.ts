import assert from "node:assert/strict";
import test from "node:test";

const {
  canvasSaveBatchMatchesPending,
  isSuspiciousEmptyCanvasDraft,
  SerialCanvasDraftWriter,
} = await import("#canvas-persistence");
const { createDefaultCanvasGraph } = await import("#canvas-graph");

test("empty local canvas drafts cannot replace a non-empty server graph", () => {
  const serverGraph = createDefaultCanvasGraph();
  const emptyDraft = {
    ...serverGraph,
    nodes: [],
    edges: [],
  };

  assert.equal(
    isSuspiciousEmptyCanvasDraft(emptyDraft, serverGraph, []),
    true,
  );
  assert.equal(
    isSuspiciousEmptyCanvasDraft(emptyDraft, serverGraph, [
      {
        op: "remove_nodes",
        operation_schema_version: 1,
        node_ids: serverGraph.nodes.map((node) => node.id),
        edge_ids: serverGraph.edges.map((edge) => edge.id),
      },
    ]),
    false,
  );
  assert.equal(
    isSuspiciousEmptyCanvasDraft(emptyDraft, emptyDraft),
    false,
  );
  assert.equal(
    isSuspiciousEmptyCanvasDraft(serverGraph, serverGraph),
    false,
  );
});

test("draft writes stay serial and rerun with the latest snapshot", async () => {
  let releaseFirst: (() => void) | undefined;
  let snapshot = "first";
  const writes: string[] = [];
  const writer = new SerialCanvasDraftWriter(async () => {
    writes.push(snapshot);
    if (writes.length === 1) {
      await new Promise<void>((resolve) => {
        releaseFirst = resolve;
      });
    }
  });

  const first = writer.request();
  await new Promise<void>((resolve) => setImmediate(resolve));
  snapshot = "latest";
  const second = writer.request();
  assert.equal(first, second);
  releaseFirst?.();
  await second;

  assert.deepEqual(writes, ["first", "latest"]);
});

test("persisted save batches only replay against the exact pending prefix", () => {
  const operation = {
    op: "update_node_meta" as const,
    operation_schema_version: 1 as const,
    node_id: "prompt-1",
    title: "新标题",
  };
  const batch = {
    base_revision: 4,
    operations: [operation],
  };

  assert.equal(
    canvasSaveBatchMatchesPending(batch, 4, [
      structuredClone(operation),
      {
        op: "remove_edges",
        operation_schema_version: 1,
        edge_ids: ["edge-1"],
      },
    ]),
    true,
  );
  assert.equal(canvasSaveBatchMatchesPending(batch, 5, [operation]), false);
  assert.equal(
    canvasSaveBatchMatchesPending(batch, 4, [
      { ...operation, title: "另一个标题" },
    ]),
    false,
  );
});
