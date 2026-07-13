import assert from "node:assert/strict";
import test from "node:test";

const { createDefaultCanvasGraph } = await import("#canvas-graph");
const { createCanvasEditorStore, operationsBetween } = await import(
  "#canvas-store"
);
const { takeAutosaveOperations } = await import("#canvas-autosave");
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
  store.getState().markSaving();
  store.getState().addNode("delivery", { x: 400, y: 0 });
  store.getState().acknowledgeOperations(sentCount, 6);
  assert.equal(store.getState().revision, 6);
  assert.equal(store.getState().pendingOperations.length, 1);
  assert.equal(store.getState().saveState, "dirty");
});

test("501 pending operations save as a 500 item prefix and one item tail", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 5);
  const operations = Array.from({ length: 501 }, (_, index) => ({
    op: "update_node_meta" as const,
    operation_schema_version: 1 as const,
    node_id: "prompt-1",
    title: `标题 ${index}`,
  }));
  store.setState({
    pendingOperations: operations,
    saveState: "dirty",
  });

  const firstBatch = takeAutosaveOperations(
    store.getState().pendingOperations,
  );
  assert.equal(firstBatch.length, 500);
  store.getState().markSaving(firstBatch.length);
  assert.equal(store.getState().acknowledgeOperations(firstBatch.length, 6), true);
  assert.equal(store.getState().pendingOperations.length, 1);
  assert.equal(store.getState().saveState, "dirty");

  const tail = takeAutosaveOperations(store.getState().pendingOperations);
  store.getState().markSaving(tail.length);
  assert.equal(store.getState().acknowledgeOperations(tail.length, 7), true);
  assert.equal(store.getState().pendingOperations.length, 0);
  assert.equal(store.getState().saveState, "saved");
});

test("in-flight config edits keep the sent prefix and latest unsent value", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 5);
  store.getState().updateNodeConfig("prompt-1", {
    text: "first",
    locked: false,
  });
  const sentCount = store.getState().pendingOperations.length;
  store.getState().markSaving();
  store.getState().updateNodeConfig("prompt-1", {
    text: "second",
    locked: false,
  });
  store.getState().updateNodeConfig("prompt-1", {
    text: "latest",
    locked: false,
  });

  assert.equal(store.getState().inFlightOperationCount, 1);
  assert.deepEqual(
    store.getState().pendingOperations.map((operation) =>
      operation.op === "update_node_config" ? operation.config.text : null,
    ),
    ["first", "latest"],
  );

  store.getState().acknowledgeOperations(sentCount, 6);
  assert.equal(store.getState().revision, 6);
  assert.equal(store.getState().inFlightOperationCount, 0);
  assert.equal(store.getState().pendingOperations.length, 1);
  const remainingOperation = store.getState().pendingOperations[0];
  assert.equal(
    remainingOperation?.op === "update_node_config"
      ? remainingOperation.config.text
      : null,
    "latest",
  );
  assert.equal(
    store.getState().graph.nodes.find((node) => node.id === "prompt-1")
      ?.config.text,
    "latest",
  );
  assert.equal(store.getState().saveState, "dirty");
});

test("moveNodes records one operation and one history step", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);
  const originalPositions = store
    .getState()
    .graph.nodes.map((node) => [node.id, { ...node.position }] as const);

  store.getState().moveNodes([
    {
      nodeId: "prompt-1",
      position: { x: 180, y: 260 },
    },
    {
      nodeId: "image-generate-1",
      position: { x: 620, y: 210 },
    },
  ]);

  assert.equal(store.getState().history.length, 1);
  assert.deepEqual(store.getState().pendingOperations, [
    {
      op: "move_nodes",
      operation_schema_version: 1,
      items: [
        { node_id: "prompt-1", x: 180, y: 260 },
        { node_id: "image-generate-1", x: 620, y: 210 },
      ],
    },
  ]);
  assert.deepEqual(
    store.getState().graph.nodes.map((node) => [node.id, node.position]),
    [
      ["prompt-1", { x: 180, y: 260 }],
      ["image-generate-1", { x: 620, y: 210 }],
    ],
  );

  store.getState().undo();
  assert.deepEqual(
    store.getState().graph.nodes.map((node) => [node.id, node.position]),
    originalPositions,
  );
});

test("removeElements deletes nodes and extra edges in one undo step", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes.push({
    ...structuredClone(graph.nodes[0]),
    id: "prompt-2",
    title: "补充提示词",
    position: { x: 80, y: 420 },
  });
  graph.edges.push({
    ...structuredClone(graph.edges[0]),
    id: "edge-prompt-2-image",
    source_node_id: "prompt-2",
    order: 1,
  });
  const store = createCanvasEditorStore(graph, 1);

  store
    .getState()
    .removeElements(["prompt-1"], ["edge-prompt-2-image"]);

  assert.equal(store.getState().history.length, 1);
  assert.deepEqual(store.getState().pendingOperations, [
    {
      op: "remove_nodes",
      operation_schema_version: 1,
      node_ids: ["prompt-1"],
      edge_ids: ["edge-prompt-image"],
    },
    {
      op: "remove_edges",
      operation_schema_version: 1,
      edge_ids: ["edge-prompt-2-image"],
    },
  ]);
  assert.equal(
    store.getState().graph.nodes.some((node) => node.id === "prompt-1"),
    false,
  );
  assert.equal(store.getState().graph.edges.length, 0);

  store.getState().undo();
  assert.deepEqual(
    store.getState().graph.nodes.map((node) => node.id),
    ["prompt-1", "image-generate-1", "prompt-2"],
  );
  assert.deepEqual(
    store.getState().graph.edges.map((edge) => edge.id),
    ["edge-prompt-image", "edge-prompt-2-image"],
  );
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

test("separate text edit sessions create separate undo boundaries", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);
  store.getState().beginNodeConfigEdit("prompt-1");
  store.getState().updateNodeConfig("prompt-1", {
    text: "第一轮",
    locked: false,
  });
  store.getState().endNodeConfigEdit("prompt-1");
  const sentCount = store.getState().pendingOperations.length;
  store.getState().markSaving(sentCount);
  assert.equal(store.getState().acknowledgeOperations(sentCount, 2), true);

  store.getState().beginNodeConfigEdit("prompt-1");
  store.getState().updateNodeConfig("prompt-1", {
    text: "第二轮",
    locked: false,
  });
  store.getState().endNodeConfigEdit("prompt-1");

  assert.equal(store.getState().history.length, 2);
  store.getState().undo();
  assert.equal(
    store.getState().graph.nodes.find((node) => node.id === "prompt-1")
      ?.config.text,
    "第一轮",
  );
});

test("save errors preserve the protected prefix and conflicts reject stale acknowledgements", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 5);
  store.getState().updateNodeConfig("prompt-1", {
    text: "sent",
    locked: false,
  });
  store.getState().markSaving(1);
  store.getState().markSaveError("network");
  assert.equal(store.getState().inFlightOperationCount, 1);

  store.getState().updateNodeConfig("prompt-1", {
    text: "newer",
    locked: false,
  });
  assert.equal(store.getState().pendingOperations.length, 2);

  store.getState().markConflict("remote update");
  assert.equal(store.getState().acknowledgeOperations(1, 6), false);
  assert.equal(store.getState().revision, 5);
  assert.equal(store.getState().pendingOperations.length, 2);
});

test("conflicts remain sticky while local edits and history actions continue", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 5);
  store.getState().updateNodeConfig("prompt-1", {
    text: "before conflict",
    locked: false,
  });
  store.getState().markConflict("remote update");
  store.getState().updateNodeConfig("prompt-1", {
    text: "after conflict",
    locked: false,
  });

  assert.equal(store.getState().saveState, "conflict");
  assert.equal(store.getState().saveMessage, "remote update");
  store.getState().undo();
  assert.equal(store.getState().saveState, "conflict");
  store.getState().redo();
  assert.equal(store.getState().saveState, "conflict");
});

test("continuous config history only merges edits for the same node", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes = graph.nodes.map((node) => ({
    ...node,
    title: "同名节点",
  }));
  const originalImageConfig = structuredClone(graph.nodes[1].config);
  const store = createCanvasEditorStore(graph, 1);

  store.getState().updateNodeConfig("prompt-1", {
    text: "保留第一次编辑",
    locked: false,
  });
  store.getState().updateNodeConfig("image-generate-1", {
    ...originalImageConfig,
    seed: 42,
  });

  assert.equal(store.getState().history.length, 2);
  store.getState().undo();
  assert.equal(
    store.getState().graph.nodes.find((node) => node.id === "prompt-1")
      ?.config.text,
    "保留第一次编辑",
  );
  assert.deepEqual(
    store
      .getState()
      .graph.nodes.find((node) => node.id === "image-generate-1")?.config,
    originalImageConfig,
  );
});

test("multi-selection survives changing the inspector primary node", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);
  store.getState().selectNodes(["prompt-1", "image-generate-1", "prompt-1"]);
  assert.deepEqual(store.getState().selectedNodeIds, [
    "prompt-1",
    "image-generate-1",
  ]);
  assert.equal(store.getState().selectedNodeId, "prompt-1");

  store.getState().selectNode("image-generate-1");
  assert.equal(store.getState().selectedNodeId, "image-generate-1");
  assert.deepEqual(store.getState().selectedNodeIds, [
    "prompt-1",
    "image-generate-1",
  ]);

  store
    .getState()
    .moveNode("image-generate-1", { x: 700, y: 300 });
  assert.deepEqual(store.getState().selectedNodeIds, [
    "prompt-1",
    "image-generate-1",
  ]);
  store.getState().undo();
  assert.equal(store.getState().selectedNodeId, null);
  assert.deepEqual(store.getState().selectedNodeIds, []);

  store.getState().selectNodes(["prompt-1", "image-generate-1"]);
  store.getState().selectEdge("edge-prompt-image");
  assert.equal(store.getState().selectedNodeId, null);
  assert.deepEqual(store.getState().selectedNodeIds, []);
});

test("repeated controlled selection reports are idempotent", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);
  let notifications = 0;
  const unsubscribe = store.subscribe(() => {
    notifications += 1;
  });

  store.getState().selectNodes(["prompt-1", "image-generate-1"]);
  assert.equal(notifications, 1);
  store.getState().selectNodes(["prompt-1", "image-generate-1"]);
  assert.equal(notifications, 1);

  store.getState().selectEdge("edge-prompt-image");
  assert.equal(notifications, 2);
  store.getState().selectEdge("edge-prompt-image");
  assert.equal(notifications, 2);

  store.getState().selectNode(null);
  assert.equal(notifications, 3);
  store.getState().selectNode(null);
  assert.equal(notifications, 3);
  unsubscribe();
});

test("adding an edge clears the node selection set", () => {
  const graph = createDefaultCanvasGraph();
  graph.edges = [];
  const store = createCanvasEditorStore(graph, 1);
  store.getState().selectNodes(["prompt-1", "image-generate-1"]);

  assert.deepEqual(
    store.getState().addEdge({
      sourceNodeId: "prompt-1",
      sourceHandle: "text",
      targetNodeId: "image-generate-1",
      targetHandle: "prompt",
    }),
    { ok: true },
  );
  assert.equal(store.getState().selectedNodeId, null);
  assert.deepEqual(store.getState().selectedNodeIds, []);
  assert.notEqual(store.getState().selectedEdgeId, null);
});

test("editing state is explicit and clears on mode changes and remote resets", () => {
  const graph = createDefaultCanvasGraph();
  const store = createCanvasEditorStore(graph, 1);
  store.getState().beginNodeEdit("prompt-1");
  assert.equal(store.getState().editingNodeId, "prompt-1");

  store.getState().setToolMode("hand");
  assert.equal(store.getState().editingNodeId, null);
  store.getState().beginNodeEdit("missing");
  assert.equal(store.getState().editingNodeId, null);

  store.getState().setToolMode("select");
  store.getState().beginNodeEdit("prompt-1");
  store.getState().replaceFromRemote(graph, 2);
  assert.equal(store.getState().editingNodeId, null);
});

test("hydrate and remote replacement reset transient interaction state", () => {
  const graph = createDefaultCanvasGraph();
  const store = createCanvasEditorStore(graph, 1);
  const draft = {
    sourceNodeId: "prompt-1",
    sourceHandle: "text",
    dataType: "text" as const,
  };

  store.getState().beginInteraction();
  store.getState().beginInteraction();
  store.getState().endInteraction();
  store.getState().setConnectionDraft(draft);
  assert.equal(store.getState().activeInteractionCount, 1);
  store.getState().hydrate(graph, 2);
  assert.equal(store.getState().activeInteractionCount, 0);
  assert.equal(store.getState().connectionDraft, null);

  store.getState().beginInteraction();
  store.getState().setConnectionDraft(draft);
  store.getState().selectNodes(["prompt-1", "image-generate-1"]);
  store.getState().replaceFromRemote(graph, 3);
  assert.equal(store.getState().activeInteractionCount, 0);
  assert.equal(store.getState().connectionDraft, null);
  assert.equal(store.getState().selectedNodeId, null);
  assert.deepEqual(store.getState().selectedNodeIds, []);
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

test("operationsBetween treats cloned nested config values as unchanged", () => {
  const current = createDefaultCanvasGraph();
  current.nodes[0] = {
    ...current.nodes[0],
    config: {
      ...current.nodes[0].config,
      nested: { tags: ["a", "b"], enabled: true },
    },
  };

  assert.deepEqual(operationsBetween(current, structuredClone(current)), []);
});
