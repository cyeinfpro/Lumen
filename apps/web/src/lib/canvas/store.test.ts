import assert from "node:assert/strict";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";

const {
  createDefaultCanvasGraph,
  MAX_CANVAS_GRAPH_BYTES,
  MAX_CANVAS_NODES,
} = await import("#canvas-graph");
const {
  CANVAS_HISTORY_GRAPH_BYTE_BUDGET,
  createCanvasEditorStore,
  operationsBetween,
} = await import("#canvas-store");
const {
  CANVAS_AUTOSAVE_OPERATION_LIMIT,
  takeAutosaveOperations,
} = await import("#canvas-autosave");
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
  assert.equal(store.getState().inFlightOperationCount, 0);
  assert.equal(store.getState().retryPrefixOperationCount, 1);

  store.getState().updateNodeConfig("prompt-1", {
    text: "newer",
    locked: false,
  });
  store.getState().updateNodeConfig("prompt-1", {
    text: "latest",
    locked: false,
  });
  assert.equal(store.getState().pendingOperations.length, 2);
  assert.deepEqual(
    store.getState().pendingOperations.map((operation) =>
      operation.op === "update_node_config" ? operation.config.text : null,
    ),
    ["sent", "latest"],
  );

  store.getState().markConflict("remote update");
  assert.equal(store.getState().acknowledgeOperations(1, 6), false);
  assert.equal(store.getState().revision, 5);
  assert.equal(store.getState().pendingOperations.length, 2);
  assert.equal(store.getState().retryPrefixOperationCount, 0);
});

test("non-retryable save errors release the protected prefix until a new edit", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 5);
  store.getState().updateNodeConfig("prompt-1", {
    text: "invalid batch",
    locked: false,
  });
  store.getState().markSaving(1);
  store.getState().markSaveError("invalid request", false);

  assert.equal(store.getState().saveState, "error");
  assert.equal(store.getState().inFlightOperationCount, 0);
  assert.equal(store.getState().retryPrefixOperationCount, 0);

  store.getState().updateNodeConfig("prompt-1", {
    text: "corrected",
    locked: false,
  });
  assert.equal(store.getState().saveState, "dirty");
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

test("canvas store records V1 appearance, resize, edge, and settings operations", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);

  store.getState().updateNodeAppearance("prompt-1", {
    title: "  新标题  ",
    ui: { collapsed: true, color_tag: "blue" },
  });
  store.getState().resizeNode("prompt-1", { width: 320, height: 220 });
  store.getState().updateEdgeDetails("edge-prompt-image", {
    order: 3,
  });
  store.getState().updateDocumentSettings({
    snap_to_grid: true,
    grid_size: 24,
  });

  assert.deepEqual(store.getState().pendingOperations, [
    {
      op: "update_node_meta",
      operation_schema_version: 1,
      node_id: "prompt-1",
      title: "新标题",
      ui: { collapsed: true, color_tag: "blue" },
    },
    {
      op: "resize_node",
      operation_schema_version: 1,
      node_id: "prompt-1",
      size: { width: 320, height: 220 },
    },
    {
      op: "update_edge",
      operation_schema_version: 1,
      edge_id: "edge-prompt-image",
      binding_mode: "follow_active",
      pinned_execution_id: null,
      pinned_output_index: null,
      order: 3,
    },
    {
      op: "update_document_settings",
      operation_schema_version: 1,
      settings: { snap_to_grid: true, grid_size: 24 },
    },
  ]);
});

test("operationsBetween covers node metadata, size, edge role, and document settings", () => {
  const current = createDefaultCanvasGraph();
  current.nodes.push({
    ...structuredClone(current.nodes[0]),
    id: "frame-1",
    type: "frame",
    title: "画框",
    config: {
      label: "画框",
      collapsed: false,
      hidden_in_run: false,
      runnable_scope: true,
    },
  });
  current.nodes.push({
    id: "image-asset-1",
    type: "image_asset",
    schema_version: 1,
    title: "参考图",
    position: { x: -320, y: 320 },
    size: { width: 292, height: 180 },
    parent_group_id: null,
    config: {
      image_id: "image-1",
      display_name: "参考图",
      crop: null,
    },
    ui: { collapsed: false, color_tag: null },
  });
  current.edges.push({
    id: "edge-image-reference",
    source_node_id: "image-asset-1",
    source_handle: "image",
    target_node_id: "image-generate-1",
    target_handle: "references",
    data_type: "image",
    binding_mode: "follow_active",
    role: null,
    order: 0,
  });
  const next = structuredClone(current);
  next.nodes[0] = {
    ...next.nodes[0],
    parent_group_id: "frame-1",
    size: { width: 340, height: 210 },
    ui: { collapsed: true, color_tag: "green" },
  };
  next.edges[1] = {
    ...next.edges[1],
    role: "style",
    order: 2,
  };
  next.settings = { snap_to_grid: true, grid_size: 32 };

  assert.deepEqual(operationsBetween(current, next), [
    {
      op: "update_node_meta",
      operation_schema_version: 1,
      node_id: "prompt-1",
      parent_group_id: "frame-1",
      ui: { collapsed: true, color_tag: "green" },
    },
    {
      op: "resize_node",
      operation_schema_version: 1,
      node_id: "prompt-1",
      size: { width: 340, height: 210 },
    },
    {
      op: "update_edge",
      operation_schema_version: 1,
      edge_id: "edge-image-reference",
      binding_mode: "follow_active",
      pinned_execution_id: null,
      pinned_output_index: null,
      role: "style",
      order: 2,
    },
    {
      op: "update_document_settings",
      operation_schema_version: 1,
      settings: { snap_to_grid: true, grid_size: 32 },
    },
  ]);
});

test("duplicateNodes remaps the internal edge and uses one undo step", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);
  const insertedIds = store
    .getState()
    .duplicateNodes(["prompt-1", "image-generate-1"], { x: 40, y: 60 });

  assert.equal(insertedIds.length, 2);
  assert.equal(store.getState().history.length, 1);
  assert.equal(store.getState().graph.nodes.length, 4);
  assert.equal(store.getState().graph.edges.length, 2);
  const insertedEdge = store
    .getState()
    .graph.edges.find((edge) => edge.id !== "edge-prompt-image");
  assert.ok(insertedEdge);
  assert.equal(insertedEdge.source_node_id, insertedIds[0]);
  assert.equal(insertedEdge.target_node_id, insertedIds[1]);
  assert.notEqual(insertedEdge.id, "edge-prompt-image");
  assert.deepEqual(store.getState().selectedNodeIds, insertedIds);

  store.getState().undo();
  assert.equal(store.getState().graph.nodes.length, 2);
  assert.equal(store.getState().graph.edges.length, 1);
});

test("legacy nullable sizes canonicalize and resize undo redo round-trips defaults", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes[0] = { ...graph.nodes[0], size: null };
  delete graph.nodes[1].size;
  const store = createCanvasEditorStore(graph, 1);

  assert.deepEqual(
    store.getState().graph.nodes.find((node) => node.id === "prompt-1")?.size,
    { width: 260, height: 180 },
  );
  assert.deepEqual(
    store
      .getState()
      .graph.nodes.find((node) => node.id === "image-generate-1")?.size,
    { width: 292, height: 180 },
  );

  store.getState().resizeNode("prompt-1", { width: 320, height: 240 });
  store.getState().undo();
  assert.deepEqual(
    store.getState().graph.nodes.find((node) => node.id === "prompt-1")?.size,
    { width: 260, height: 180 },
  );
  assert.deepEqual(store.getState().pendingOperations.at(-1), {
    op: "resize_node",
    operation_schema_version: 1,
    node_id: "prompt-1",
    size: { width: 260, height: 180 },
  });

  store.getState().redo();
  assert.deepEqual(
    store.getState().graph.nodes.find((node) => node.id === "prompt-1")?.size,
    { width: 320, height: 240 },
  );
});

test("frame resize commits position and size atomically", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);
  const before = structuredClone(store.getState().graph);

  store
    .getState()
    .resizeNode(
      "prompt-1",
      { width: 340, height: 260 },
      { x: 32, y: 48 },
    );

  const resized = store
    .getState()
    .graph.nodes.find((node) => node.id === "prompt-1");
  assert.deepEqual(resized?.position, { x: 32, y: 48 });
  assert.deepEqual(resized?.size, { width: 340, height: 260 });
  assert.equal(store.getState().history.length, 1);
  assert.deepEqual(
    store.getState().pendingOperations.map((operation) => operation.op),
    ["move_nodes", "resize_node"],
  );

  store.getState().undo();
  assert.deepEqual(store.getState().graph, before);
});

test("delete undo and redo clear stale connection drafts", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);
  const draft = {
    sourceNodeId: "prompt-1",
    sourceHandle: "text",
    dataType: "text" as const,
  };

  store.getState().setConnectionDraft(draft);
  store.getState().removeEdges(["edge-prompt-image"]);
  assert.equal(store.getState().connectionDraft, null);

  store.getState().setConnectionDraft(draft);
  store.getState().undo();
  assert.equal(store.getState().connectionDraft, null);

  store.getState().setConnectionDraft(draft);
  store.getState().redo();
  assert.equal(store.getState().connectionDraft, null);
});

test("moves reject non-finite and out-of-range coordinates", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);
  const original = structuredClone(store.getState().graph);

  store.getState().moveNode("prompt-1", { x: Number.NaN, y: 0 });
  store.getState().moveNode("prompt-1", { x: Number.POSITIVE_INFINITY, y: 0 });
  store.getState().moveNode("prompt-1", { x: 10_000_001, y: 0 });
  assert.deepEqual(store.getState().graph, original);
  assert.equal(store.getState().history.length, 0);
  assert.equal(store.getState().pendingOperations.length, 0);

  store.getState().moveNodes([
    { nodeId: "prompt-1", position: { x: -10_000_001, y: 0 } },
    {
      nodeId: "image-generate-1",
      position: { x: 700, y: 320 },
    },
  ]);
  assert.deepEqual(
    store.getState().graph.nodes.map((node) => node.position),
    [original.nodes[0].position, { x: 700, y: 320 }],
  );
});

test("edge updates reject invalid role and pinning metadata", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);
  const original = structuredClone(store.getState().graph.edges[0]);

  store
    .getState()
    .updateEdgeDetails("edge-prompt-image", { role: "invalid" } as never);
  store.getState().updateEdgeDetails("edge-prompt-image", {
    binding_mode: "pinned",
    pinned_execution_id: null,
    pinned_output_index: 0,
  });
  store.getState().updateEdgeDetails("edge-prompt-image", {
    binding_mode: "pinned",
    pinned_execution_id: "execution-1",
    pinned_output_index: -1,
  });
  store.getState().updateEdgeDetails("edge-prompt-image", {
    binding_mode: "follow_active",
    pinned_execution_id: "execution-1",
    pinned_output_index: 0,
  });
  store.getState().updateEdgeDetails("edge-prompt-image", {
    role: "subject",
  });
  store.getState().updateEdgeDetails("edge-prompt-image", {
    binding_mode: "pinned",
    pinned_execution_id: "execution-1",
    pinned_output_index: 0,
  });

  assert.deepEqual(store.getState().graph.edges[0], original);
  assert.equal(store.getState().history.length, 0);
  assert.equal(store.getState().pendingOperations.length, 0);
});

test("history retention stays within the graph byte budget", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes[0] = {
    ...graph.nodes[0],
    config: {
      ...graph.nodes[0].config,
      payload: "x".repeat(
        Math.ceil(CANVAS_HISTORY_GRAPH_BYTE_BUDGET / 3),
      ),
    },
  };
  const store = createCanvasEditorStore(graph, 1);

  for (let index = 1; index <= 4; index += 1) {
    store
      .getState()
      .moveNode("prompt-1", { x: 80 + index, y: 160 });
  }

  const retainedBytes = store
    .getState()
    .history.reduce(
      (total, entry) =>
        total +
        new TextEncoder().encode(JSON.stringify(entry.graph)).byteLength,
      0,
    );
  assert.ok(retainedBytes <= CANVAS_HISTORY_GRAPH_BYTE_BUDGET);
  assert.ok(store.getState().history.length < 4);
  assert.ok(store.getState().history.length > 0);
});

test("subgraph commits refuse more than one autosave batch atomically", () => {
  const store = createCanvasEditorStore(createDefaultCanvasGraph(), 1);
  const template = store.getState().graph.nodes[0];
  const subgraph = {
    schema_version: 1 as const,
    nodes: Array.from(
      { length: CANVAS_AUTOSAVE_OPERATION_LIMIT + 1 },
      (_, index) => ({
        ...structuredClone(template),
        id: `bulk-${index}`,
        position: { x: index, y: index },
      }),
    ),
    edges: [],
  };

  assert.deepEqual(store.getState().insertSubgraph(subgraph), []);
  assert.equal(store.getState().graph.nodes.length, 2);
  assert.equal(store.getState().history.length, 0);
  assert.equal(store.getState().pendingOperations.length, 0);

  const boundaryStore = createCanvasEditorStore(
    createDefaultCanvasGraph(),
    1,
  );
  const boundaryTemplate = boundaryStore.getState().graph.nodes[0];
  const boundarySubgraph = {
    schema_version: 1 as const,
    nodes: Array.from(
      { length: CANVAS_AUTOSAVE_OPERATION_LIMIT },
      (_, index) => ({
        ...structuredClone(boundaryTemplate),
        id: `boundary-${index}`,
        position: { x: index, y: index },
      }),
    ),
    edges: [],
  };
  assert.equal(
    boundaryStore.getState().insertSubgraph(boundarySubgraph).length,
    CANVAS_AUTOSAVE_OPERATION_LIMIT,
  );
  assert.equal(
    boundaryStore.getState().pendingOperations.length,
    CANVAS_AUTOSAVE_OPERATION_LIMIT,
  );
});

test("undo and redo refuse deltas larger than one autosave batch", () => {
  const graph = createDefaultCanvasGraph();
  const expanded = graphWithExtraNodes(
    graph,
    CANVAS_AUTOSAVE_OPERATION_LIMIT + 1,
  );
  const undoStore = createCanvasEditorStore(graph, 1);
  undoStore.setState({
    history: [{ graph: expanded, label: "oversized undo" }],
  });
  const beforeUndo = undoStore.getState();
  undoStore.getState().undo();
  assert.equal(undoStore.getState().graph.nodes.length, beforeUndo.graph.nodes.length);
  assert.equal(undoStore.getState().history.length, 1);
  assert.equal(undoStore.getState().pendingOperations.length, 0);

  const redoStore = createCanvasEditorStore(graph, 1);
  redoStore.setState({
    future: [{ graph: expanded, label: "oversized redo" }],
  });
  redoStore.getState().redo();
  assert.equal(redoStore.getState().graph.nodes.length, graph.nodes.length);
  assert.equal(redoStore.getState().future.length, 1);
  assert.equal(redoStore.getState().pendingOperations.length, 0);

  const boundaryRedoStore = createCanvasEditorStore(graph, 1);
  boundaryRedoStore.setState({
    future: [
      {
        graph: graphWithExtraNodes(
          graph,
          CANVAS_AUTOSAVE_OPERATION_LIMIT,
        ),
        label: "boundary redo",
      },
    ],
  });
  boundaryRedoStore.getState().redo();
  assert.equal(
    boundaryRedoStore.getState().pendingOperations.length,
    CANVAS_AUTOSAVE_OPERATION_LIMIT,
  );
});

test("direct node and edge additions refuse graph capacity overflow", () => {
  const graph = createDefaultCanvasGraph();
  const template = graph.nodes[0];
  graph.nodes = Array.from({ length: MAX_CANVAS_NODES }, (_, index) => ({
    ...structuredClone(template),
    id: `node-${index}`,
  }));
  const nodeStore = createCanvasEditorStore(graph, 1);
  assert.equal(nodeStore.getState().addNode("note", { x: 0, y: 0 }), "");
  assert.equal(nodeStore.getState().graph.nodes.length, MAX_CANVAS_NODES);
  assert.equal(nodeStore.getState().pendingOperations.length, 0);

  const oversizedGraph = createDefaultCanvasGraph();
  oversizedGraph.edges = [];
  oversizedGraph.frames = [{ payload: "x".repeat(MAX_CANVAS_GRAPH_BYTES) }];
  const edgeStore = createCanvasEditorStore(oversizedGraph, 1);
  assert.equal(edgeStore.getState().addNode("note", { x: 0, y: 0 }), "");
  assert.deepEqual(
    edgeStore.getState().addEdge({
      sourceNodeId: "prompt-1",
      sourceHandle: "text",
      targetNodeId: "image-generate-1",
      targetHandle: "prompt",
    }),
    { ok: false, reason: "画布已达到容量上限" },
  );
  assert.equal(edgeStore.getState().graph.edges.length, 0);
  assert.equal(edgeStore.getState().pendingOperations.length, 0);
});

function graphWithExtraNodes(
  graph: ReturnType<typeof createDefaultCanvasGraph>,
  count: number,
) {
  const template = graph.nodes[0];
  return {
    ...structuredClone(graph),
    nodes: [
      ...structuredClone(graph.nodes),
      ...Array.from({ length: count }, (_, index) => ({
        ...structuredClone(template),
        id: `extra-${index}`,
        position: { x: index, y: index },
      })),
    ],
  };
}
