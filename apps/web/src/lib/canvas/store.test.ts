import assert from "node:assert/strict";
import test from "node:test";

const { createDefaultCanvasGraph } = await import("#canvas-graph");
const { createCanvasEditorStore, operationsBetween } = await import("#canvas-store");
const { canvasDraftKey } = await import("#canvas-persistence");

test("canvas store records commands and supports undo redo", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 3);
  const nodeId = store.getState().addNode("note", { x: 100, y: 200 });
  assert.equal(store.getState().graph.nodes.length, 3);
  assert.equal(store.getState().pendingOperations.at(-1)?.op, "add_node");

  store.getState().updateNodeConfig(nodeId, { text: "review" });
  assert.equal(store.getState().graph.nodes.find((node) => node.id === nodeId)?.config.text, "review");
  store.getState().undo();
  assert.equal(store.getState().graph.nodes.find((node) => node.id === nodeId)?.config.text, "");
  store.getState().redo();
  assert.equal(store.getState().graph.nodes.find((node) => node.id === nodeId)?.config.text, "review");
});

test("canvas drafts are scoped to both canvas and browser client", () => {
  assert.equal(canvasDraftKey("canvas-1", "tab-1"), "canvas-1:tab-1");
  assert.notEqual(
    canvasDraftKey("canvas-1", "tab-1"),
    canvasDraftKey("canvas-1", "tab-2"),
  );
});

test("acknowledging a save preserves operations created while request was in flight", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 5);
  store.getState().addNode("note", { x: 0, y: 0 });
  const sentCount = store.getState().pendingOperations.length;
  store.getState().addNode("delivery", { x: 400, y: 0 });
  store.getState().acknowledgeOperations(sentCount, 6);
  assert.equal(store.getState().revision, 6);
  assert.equal(store.getState().pendingOperations.length, 1);
  assert.equal(store.getState().saveState, "dirty");
});

test("continuous text edits share one undo step and pending operation", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);
  store.getState().updateNodeConfig("prompt-1", {
    text: "产",
    locked: false,
  });
  store.getState().updateNodeConfig("prompt-1", {
    text: "产品",
    locked: false,
  });
  store.getState().updateNodeConfig("prompt-1", {
    text: "产品主视觉",
    locked: false,
  });

  assert.equal(store.getState().history.length, 1);
  assert.equal(store.getState().pendingOperations.length, 1);
  store.getState().undo();
  assert.equal(
    store.getState().graph.nodes.find((node) => node.id === "prompt-1")?.config
      .text,
    "",
  );
});

test("operationsBetween creates a typed update_edge operation when order changes", () => {
  const current = createDefaultCanvasGraph();
  const next = structuredClone(current);
  next.edges[0] = {
    ...next.edges[0],
    binding_mode: "pinned",
    pinned_execution_id: "execution-1",
    pinned_output_index: 2,
    order: 3,
  };

  assert.deepEqual(operationsBetween(current, next), [
    {
      op: "update_edge",
      operation_schema_version: 1,
      edge_id: next.edges[0].id,
      binding_mode: "pinned",
      pinned_execution_id: "execution-1",
      pinned_output_index: 2,
      order: 3,
    },
  ]);
});
