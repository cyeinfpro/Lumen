import assert from "node:assert/strict";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";

const { serializeCanvasRecoveryCopy } = await import("./recoveryCopy.ts");
const { createDefaultCanvasGraph } = await import("#canvas-graph");

test("exported current copy preserves the full graph, baseline, and pending atomic edits without storage", () => {
  const copy = {
    canvas_id: "canvas-1", client_id: "client-1", title: "Local title", description: "Local copy",
    base_revision: 4, updated_at: 123,
    graph: createDefaultCanvasGraph(),
    operations: [{
      op: "update_document_settings" as const,
      operation_schema_version: 1 as const,
      settings: { snap_to_grid: true, grid_size: 32 },
    }],
    operation_group_sizes: [1],
  };
  copy.graph.settings = copy.operations[0]!.settings;
  const serialized = serializeCanvasRecoveryCopy(copy);
  assert.deepEqual(JSON.parse(serialized), copy);
  assert.equal(copy.base_revision, 4);
  assert.equal(copy.operations.length, 1);
});
