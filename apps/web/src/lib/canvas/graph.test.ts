import assert from "node:assert/strict";
import test from "node:test";

const {
  canvasGraphReadyToSave,
  createCanvasEdge,
  createDefaultCanvasGraph,
  validateCanvasNodeExecution,
  validateCanvasConnection,
} = await import("#canvas-graph");
const { createCanvasNode } = await import("#canvas-registry");

test("default canvas is prompt connected to image generation", () => {
  const graph = createDefaultCanvasGraph();
  assert.deepEqual(
    graph.nodes.map((node) => node.type),
    ["prompt", "image_generate"],
  );
  assert.equal(graph.edges.length, 1);
  assert.equal(graph.edges[0]?.data_type, "text");
});

test("connection validation rejects type mismatch and input overflow", () => {
  const graph = createDefaultCanvasGraph();
  const video = createCanvasNode("video_asset", { x: 0, y: 0 }, { id: "video-1" });
  graph.nodes.push(video);
  const mismatch = validateCanvasConnection(graph, {
    sourceNodeId: video.id,
    sourceHandle: "video",
    targetNodeId: "image-generate-1",
    targetHandle: "references",
  });
  assert.equal(mismatch.valid, false);

  const duplicatePrompt = createCanvasNode("prompt", { x: 0, y: 0 }, { id: "prompt-2" });
  graph.nodes.push(duplicatePrompt);
  const overflow = validateCanvasConnection(graph, {
    sourceNodeId: duplicatePrompt.id,
    sourceHandle: "text",
    targetNodeId: "image-generate-1",
    targetHandle: "prompt",
  });
  assert.deepEqual(overflow, {
    valid: false,
    reason: "提示词 只允许一个输入",
  });
});

test("connection validation rejects a cycle immediately", () => {
  const graph = createDefaultCanvasGraph();
  const secondImage = createCanvasNode("image_generate", { x: 780, y: 120 }, {
    id: "image-2",
  });
  graph.nodes.push(secondImage);
  const firstToSecond = createCanvasEdge(graph, {
    sourceNodeId: "image-generate-1",
    sourceHandle: "image",
    targetNodeId: secondImage.id,
    targetHandle: "references",
  });
  assert.ok(firstToSecond);
  graph.edges.push(firstToSecond);
  const cycle = validateCanvasConnection(graph, {
    sourceNodeId: secondImage.id,
    sourceHandle: "image",
    targetNodeId: "image-generate-1",
    targetHandle: "references",
  });
  assert.deepEqual(cycle, { valid: false, reason: "连接会形成环" });
});

test("execution validation blocks empty prompts and missing i2v frames", () => {
  const graph = createDefaultCanvasGraph();
  assert.deepEqual(validateCanvasNodeExecution(graph, "image-generate-1"), {
    valid: false,
    reason: "提示词不能为空",
  });

  graph.nodes[0].config = { text: "产品主视觉", locked: false };
  const video = createCanvasNode("video_generate", { x: 720, y: 120 }, {
    id: "video-1",
    config: { mode: "i2v" },
  });
  graph.nodes.push(video);
  graph.edges.push({
    id: "prompt-video",
    source_node_id: "prompt-1",
    source_handle: "text",
    target_node_id: "video-1",
    target_handle: "prompt",
    data_type: "text",
    binding_mode: "follow_active",
    order: 0,
  });

  assert.deepEqual(validateCanvasNodeExecution(graph, "video-1"), {
    valid: false,
    reason: "图生视频需要且只能连接一个首帧",
  });
});

test("execution validation matches mask and reference-mode server guards", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes[0].config = { text: "产品主视觉", locked: false };
  const asset = createCanvasNode("image_asset", { x: 40, y: 360 }, {
    id: "asset-1",
    config: { image_id: "image-1" },
  });
  graph.nodes.push(asset);
  const mask = createCanvasEdge(graph, {
    sourceNodeId: asset.id,
    sourceHandle: "image",
    targetNodeId: "image-generate-1",
    targetHandle: "mask",
  });
  assert.ok(mask);
  graph.edges.push(mask);
  assert.deepEqual(validateCanvasNodeExecution(graph, "image-generate-1"), {
    valid: false,
    reason: "遮罩需要且只能连接一张参考图",
  });

  const reference = createCanvasEdge(graph, {
    sourceNodeId: asset.id,
    sourceHandle: "image",
    targetNodeId: "image-generate-1",
    targetHandle: "references",
  });
  assert.ok(reference);
  graph.edges.push(reference);
  assert.deepEqual(validateCanvasNodeExecution(graph, "image-generate-1"), {
    valid: true,
  });

  const video = createCanvasNode("video_generate", { x: 720, y: 120 }, {
    id: "video-reference",
    config: { mode: "reference" },
  });
  graph.nodes.push(video);
  graph.edges.push({
    id: "prompt-video-reference",
    source_node_id: "prompt-1",
    source_handle: "text",
    target_node_id: video.id,
    target_handle: "prompt",
    data_type: "text",
    binding_mode: "follow_active",
    order: 0,
  });
  assert.deepEqual(validateCanvasNodeExecution(graph, video.id), {
    valid: false,
    reason: "参考视频模式至少需要一个参考素材",
  });
});

test("save readiness matches the server graph limits", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes = Array.from({ length: 1_000 }, (_, index) =>
    createCanvasNode("note", { x: index, y: 0 }, { id: `note-${index}` }),
  );
  assert.equal(canvasGraphReadyToSave(graph), true);
  graph.nodes.push(
    createCanvasNode("note", { x: 1_001, y: 0 }, { id: "overflow" }),
  );
  assert.equal(canvasGraphReadyToSave(graph), false);
});
