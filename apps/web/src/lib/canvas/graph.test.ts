import assert from "node:assert/strict";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";

const {
  MAX_CANVAS_FRAMES,
  MAX_CANVAS_GRAPH_BYTES,
  MAX_CANVAS_NODE_CONFIG_BYTES,
  canvasGraphReadyToSave,
  canvasVideoCapabilityError,
  createCanvasEdge,
  createCanvasTemplateGraph,
  createDefaultCanvasGraph,
  resolveCanvasTextOutput,
  validateCanvasConnections,
  validateCanvasNodeExecution,
  validateCanvasConnection,
} = await import("#canvas-graph");
const {
  createCanvasNode,
  createCanvasNodeFromCatalog,
  findMatchingCanvasNodeCatalogItem,
  isCanvasNodeType,
} = await import("#canvas-registry");

test("default canvas is prompt connected to image generation", () => {
  const graph = createDefaultCanvasGraph();
  assert.deepEqual(
    graph.nodes.map((node) => node.type),
    ["prompt", "image_generate"],
  );
  assert.equal(graph.edges.length, 1);
  assert.equal(graph.edges[0]?.data_type, "text");
});

test("node type guards do not inherit Object prototype keys", () => {
  assert.equal(isCanvasNodeType("constructor"), false);
  assert.equal(isCanvasNodeType("toString"), false);
  assert.equal(isCanvasNodeType("prompt"), true);
});

test("mature canvas templates contain valid end-to-end graphs", () => {
  const templates = [
    "image_to_video",
    "product_directions",
    "multi_ratio",
    "storyboard_video",
    "image_editing",
    "inpaint",
    "reference_video",
    "creative_campaign",
  ];
  for (const template of templates) {
    const graph = createCanvasTemplateGraph(template);
    assert.ok(graph.nodes.length >= 3, `${template} should contain nodes`);
    assert.ok(graph.edges.length >= 2, `${template} should contain edges`);
    assert.deepEqual(
      validateCanvasConnections({ ...graph, edges: [] }, graph.edges),
      { valid: true },
      `${template} should contain only valid connections`,
    );
  }
  assert.deepEqual(
    createCanvasTemplateGraph("inpaint").nodes.map((node) => node.type),
    ["prompt", "image_asset", "mask_asset", "image_inpaint", "delivery"],
  );
  assert.ok(
    createCanvasTemplateGraph("creative_campaign").nodes.some(
      (node) => node.type === "prompt_merge",
    ),
  );
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

test("reference video ports enforce image and video media limits", () => {
  const graph = createDefaultCanvasGraph();
  const target = createCanvasNode(
    "video_reference_generate",
    { x: 760, y: 120 },
    { id: "reference-video" },
  );
  graph.nodes.push(target);

  for (let index = 0; index < 10; index += 1) {
    const source = createCanvasNode("image_asset", { x: 0, y: index * 40 }, {
      id: `reference-image-${index}`,
    });
    graph.nodes.push(source);
    const validation = validateCanvasConnection(graph, {
      sourceNodeId: source.id,
      sourceHandle: "image",
      targetNodeId: target.id,
      targetHandle: "reference_images",
    });
    if (index < 9) {
      assert.equal(validation.valid, true);
      const edge = createCanvasEdge(graph, {
        sourceNodeId: source.id,
        sourceHandle: "image",
        targetNodeId: target.id,
        targetHandle: "reference_images",
      });
      assert.ok(edge);
      graph.edges.push(edge);
    } else {
      assert.deepEqual(validation, {
        valid: false,
        reason: "参考图 最多允许 9 个输入",
      });
    }
  }

  for (let index = 0; index < 4; index += 1) {
    const source = createCanvasNode("video_asset", { x: 240, y: index * 80 }, {
      id: `reference-clip-${index}`,
    });
    graph.nodes.push(source);
    const validation = validateCanvasConnection(graph, {
      sourceNodeId: source.id,
      sourceHandle: "video",
      targetNodeId: target.id,
      targetHandle: "reference_videos",
    });
    if (index < 3) {
      assert.equal(validation.valid, true);
      const edge = createCanvasEdge(graph, {
        sourceNodeId: source.id,
        sourceHandle: "video",
        targetNodeId: target.id,
        targetHandle: "reference_videos",
      });
      assert.ok(edge);
      graph.edges.push(edge);
    } else {
      assert.deepEqual(validation, {
        valid: false,
        reason: "参考视频 最多允许 3 个输入",
      });
    }
  }
});

test("catalog presets assign roles only while their identifying config still matches", () => {
  const graph = createDefaultCanvasGraph();
  const product = createCanvasNodeFromCatalog(
    "product_reference",
    { x: 0, y: 320 },
    { id: "product-reference" },
  );
  graph.nodes.push(product);
  const matchingEdge = createCanvasEdge(graph, {
    sourceNodeId: product.id,
    sourceHandle: "image",
    targetNodeId: "image-generate-1",
    targetHandle: "references",
  });
  assert.equal(findMatchingCanvasNodeCatalogItem(product)?.id, "product_reference");
  assert.equal(matchingEdge?.role, "product");

  product.config.display_name = "已改名的参考图";
  assert.equal(findMatchingCanvasNodeCatalogItem(product), undefined);
  const driftedEdge = createCanvasEdge(graph, {
    sourceNodeId: product.id,
    sourceHandle: "image",
    targetNodeId: "image-generate-1",
    targetHandle: "references",
  });
  assert.equal(driftedEdge?.role, null);
});

test("specialized image presets remain distinct from base defaults", () => {
  const base = createCanvasNodeFromCatalog("image_upscale", { x: 0, y: 0 });
  const redraw = createCanvasNodeFromCatalog("image_4k_redraw", { x: 0, y: 0 });
  const transparent = createCanvasNodeFromCatalog(
    "transparent_background",
    { x: 0, y: 0 },
  );

  assert.deepEqual(
    [base.config.size, base.config.quality, base.config.fast],
    ["2K", "2k", true],
  );
  assert.deepEqual(
    [redraw.config.size, redraw.config.quality, redraw.config.fast],
    ["4K", "4k", false],
  );
  assert.deepEqual(
    [
      transparent.config.background,
      transparent.config.output_format,
      transparent.config.output_compression,
    ],
    ["transparent", "png", null],
  );
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

test("legacy execution config loads but is blocked until corrected", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes[0].config = { text: "产品主视觉", locked: false };
  graph.nodes[1].config = {
    ...graph.nodes[1].config,
    aspect_ratio: "legacy-ratio",
  };
  assert.deepEqual(validateCanvasNodeExecution(graph, "image-generate-1"), {
    valid: false,
    reason: "图片比例不受支持，请重新选择",
  });

  graph.nodes[1].config = {
    ...graph.nodes[1].config,
    aspect_ratio: "1:1",
    size_mode: "fixed",
    fixed_size: "100x100",
  };
  assert.deepEqual(validateCanvasNodeExecution(graph, "image-generate-1"), {
    valid: false,
    reason: "宽高必须是 16 的倍数",
  });
});

test("video capability validation blocks disabled or incompatible options", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes[0].config = { text: "产品主视觉", locked: false };
  const video = createCanvasNode("video_text_generate", { x: 720, y: 120 }, {
    id: "video-capability",
  });
  graph.nodes.push(video);
  const disabled: Parameters<typeof canvasVideoCapabilityError>[1] = {
    enabled: false,
    unavailable_reason: "管理员已停用",
    models: [],
    durations_s: [],
    resolutions: [],
    aspect_ratios: [],
    generate_audio: false,
    pricing: [],
    hold_estimates: {},
  };
  assert.equal(
    canvasVideoCapabilityError(video, disabled),
    "管理员已停用",
  );

  assert.equal(
    canvasVideoCapabilityError(video, {
      ...disabled,
      enabled: true,
      unavailable_reason: null,
    }),
    "当前模式没有可用的视频模型",
  );
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

test("mask assets expose only mask output while image assets may feed masks", () => {
  const graph = createDefaultCanvasGraph();
  const mask = createCanvasNode("mask_asset", { x: 0, y: 320 }, {
    id: "mask-asset",
    config: { image_id: "mask-image" },
  });
  const image = createCanvasNode("image_asset", { x: 0, y: 520 }, {
    id: "image-asset",
    config: { image_id: "image-mask" },
  });
  const inpaint = createCanvasNode("image_inpaint", { x: 680, y: 320 }, {
    id: "inpaint-1",
  });
  const edit = createCanvasNode("image_edit", { x: 680, y: 560 }, {
    id: "edit-1",
  });
  graph.nodes.push(mask, image, inpaint, edit);

  assert.deepEqual(
    validateCanvasConnection(graph, {
      sourceNodeId: mask.id,
      sourceHandle: "mask",
      targetNodeId: inpaint.id,
      targetHandle: "mask",
    }),
    {
      valid: true,
      dataType: "mask",
      sourceType: "mask",
      targetType: "mask",
    },
  );
  assert.deepEqual(
    validateCanvasConnection(graph, {
      sourceNodeId: image.id,
      sourceHandle: "image",
      targetNodeId: inpaint.id,
      targetHandle: "mask",
    }),
    {
      valid: true,
      dataType: "mask",
      sourceType: "image",
      targetType: "mask",
    },
  );
  assert.deepEqual(
    validateCanvasConnection(graph, {
      sourceNodeId: mask.id,
      sourceHandle: "mask",
      targetNodeId: edit.id,
      targetHandle: "source",
    }),
    {
      valid: false,
      reason: "mask 不能连接到 image",
    },
  );
  assert.deepEqual(
    validateCanvasConnection(graph, {
      sourceNodeId: mask.id,
      sourceHandle: "image",
      targetNodeId: inpaint.id,
      targetHandle: "mask",
    }),
    {
      valid: false,
      reason: "连接节点或端口不存在",
    },
  );
});

test("prompt merge resolves ordered text with trimming, dedupe, prefix, and suffix", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes[0].config = { text: "  商品主视觉  ", locked: false };
  const duplicate = createCanvasNode("prompt", { x: 0, y: 320 }, {
    id: "prompt-duplicate",
    config: { text: "商品主视觉", locked: false },
  });
  const detail = createCanvasNode("prompt", { x: 0, y: 520 }, {
    id: "prompt-detail",
    config: { text: "棚拍光线", locked: false },
  });
  const merge = createCanvasNode("prompt_merge", { x: 340, y: 280 }, {
    id: "merge-1",
    config: {
      separator: " | ",
      prefix: "开始：",
      suffix: "：结束",
      trim: true,
      dedupe: true,
    },
  });
  graph.nodes.push(duplicate, detail, merge);
  graph.edges.push(
    {
      id: "merge-source-1",
      source_node_id: "prompt-1",
      source_handle: "text",
      target_node_id: merge.id,
      target_handle: "texts",
      data_type: "text",
      binding_mode: "follow_active",
      order: 0,
    },
    {
      id: "merge-source-2",
      source_node_id: duplicate.id,
      source_handle: "text",
      target_node_id: merge.id,
      target_handle: "texts",
      data_type: "text",
      binding_mode: "follow_active",
      order: 1,
    },
    {
      id: "merge-source-3",
      source_node_id: detail.id,
      source_handle: "text",
      target_node_id: merge.id,
      target_handle: "texts",
      data_type: "text",
      binding_mode: "follow_active",
      order: 2,
    },
  );
  assert.equal(
    resolveCanvasTextOutput(graph, merge.id),
    "开始：商品主视觉 | 棚拍光线：结束",
  );
});

test("new image and fixed video nodes enforce their required ports", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes[0].config = { text: "产品主视觉", locked: false };
  const source = createCanvasNode("image_asset", { x: 0, y: 320 }, {
    id: "source-1",
    config: { image_id: "image-1" },
  });
  const edit = createCanvasNode("image_edit", { x: 680, y: 120 }, {
    id: "edit-1",
  });
  const imageVideo = createCanvasNode("video_image_generate", { x: 680, y: 420 }, {
    id: "image-video-1",
  });
  graph.nodes.push(source, edit, imageVideo);
  graph.edges.push(
    {
      id: "prompt-edit",
      source_node_id: "prompt-1",
      source_handle: "text",
      target_node_id: edit.id,
      target_handle: "prompt",
      data_type: "text",
      binding_mode: "follow_active",
      order: 0,
    },
    {
      id: "prompt-image-video",
      source_node_id: "prompt-1",
      source_handle: "text",
      target_node_id: imageVideo.id,
      target_handle: "prompt",
      data_type: "text",
      binding_mode: "follow_active",
      order: 0,
    },
  );
  assert.deepEqual(validateCanvasNodeExecution(graph, edit.id), {
    valid: false,
    reason: "缺少原图输入",
  });
  assert.deepEqual(validateCanvasNodeExecution(graph, imageVideo.id), {
    valid: false,
    reason: "缺少首帧输入",
  });

  const editSource = createCanvasEdge(graph, {
    sourceNodeId: source.id,
    sourceHandle: "image",
    targetNodeId: edit.id,
    targetHandle: "source",
  });
  const videoFrame = createCanvasEdge(graph, {
    sourceNodeId: source.id,
    sourceHandle: "image",
    targetNodeId: imageVideo.id,
    targetHandle: "first_frame",
  });
  assert.ok(editSource);
  assert.ok(videoFrame);
  graph.edges.push(editSource, videoFrame);
  assert.deepEqual(validateCanvasNodeExecution(graph, edit.id), { valid: true });
  assert.deepEqual(validateCanvasNodeExecution(graph, imageVideo.id), { valid: true });
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

test("save readiness limits standalone frames", () => {
  const graph = createDefaultCanvasGraph();
  graph.frames = Array.from({ length: MAX_CANVAS_FRAMES }, (_, index) => ({
    id: `frame-${index}`,
  }));
  assert.equal(canvasGraphReadyToSave(graph), true);
  graph.frames.push({ id: "frame-overflow" });
  assert.equal(canvasGraphReadyToSave(graph), false);
});

test("save readiness enforces UTF-8 graph and per-node config byte limits", () => {
  const graph = createDefaultCanvasGraph();
  graph.nodes[0].config = {
    text: "界".repeat(Math.ceil(MAX_CANVAS_NODE_CONFIG_BYTES / 3)),
  };
  assert.equal(canvasGraphReadyToSave(graph), false);

  graph.nodes[0].config = { text: "" };
  graph.frames = [{ payload: "x".repeat(MAX_CANVAS_GRAPH_BYTES) }];
  assert.equal(canvasGraphReadyToSave(graph), false);
});

test("bulk connection validation applies duplicate and cycle checks sequentially", () => {
  const graph = createDefaultCanvasGraph();
  graph.edges = [];
  const secondImage = createCanvasNode("image_generate", { x: 780, y: 120 }, {
    id: "image-2",
  });
  graph.nodes.push(secondImage);

  const valid = validateCanvasConnections(graph, [
    {
      id: "prompt-first",
      source_node_id: "prompt-1",
      source_handle: "text",
      target_node_id: "image-generate-1",
      target_handle: "prompt",
      data_type: "text",
      binding_mode: "follow_active",
    },
    {
      id: "first-second",
      source_node_id: "image-generate-1",
      source_handle: "image",
      target_node_id: "image-2",
      target_handle: "references",
      data_type: "image",
      binding_mode: "follow_active",
      order: 0,
    },
  ]);
  assert.deepEqual(valid, { valid: true });

  const duplicateId = validateCanvasConnections(graph, [
    {
      id: "duplicate-edge",
      source_node_id: "prompt-1",
      source_handle: "text",
      target_node_id: "image-generate-1",
      target_handle: "prompt",
      data_type: "text",
      binding_mode: "follow_active",
    },
    {
      id: "duplicate-edge",
      source_node_id: "image-generate-1",
      source_handle: "image",
      target_node_id: "image-2",
      target_handle: "references",
      data_type: "image",
      binding_mode: "follow_active",
      order: 0,
    },
  ]);
  assert.deepEqual(duplicateId, {
    valid: false,
    edgeId: "duplicate-edge",
    reason: "连接 ID 重复",
  });

  const cyclic = validateCanvasConnections(graph, [
    {
      id: "first-second",
      source_node_id: "image-generate-1",
      source_handle: "image",
      target_node_id: "image-2",
      target_handle: "references",
      data_type: "image",
      binding_mode: "follow_active",
      order: 0,
    },
    {
      id: "second-first",
      source_node_id: "image-2",
      source_handle: "image",
      target_node_id: "image-generate-1",
      target_handle: "references",
      data_type: "image",
      binding_mode: "follow_active",
      order: 0,
    },
  ]);
  assert.deepEqual(cyclic, {
    valid: false,
    edgeId: "second-first",
    reason: "连接会形成环",
  });
});
