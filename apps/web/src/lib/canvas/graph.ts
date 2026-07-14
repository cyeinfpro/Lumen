import {
  CANVAS_NODE_SPECS,
  canvasDefaultRoleForNode,
  canvasNodeConfigIsValid,
  canvasNodeUiIsValid,
  canvasVideoModeForNode,
  canvasUuid,
  createCanvasNode,
  isCanvasExecutableNodeType,
  isCanvasNodeType,
  isCanvasVideoNodeType,
  normalizeCanvasNodeUi,
  type CanvasNodeCreateOverrides,
} from "#canvas-registry";
import type {
  CanvasDataType,
  CanvasEdgeDefinition,
  CanvasGraph,
  CanvasNodeDefinition,
  CanvasNodeType,
} from "#canvas-types";
import { MAX_PROMPT_CHARS } from "../promptLimits";
import type { VideoOptionsOut } from "../types";

export const MAX_CANVAS_NODES = 1_000;
export const MAX_CANVAS_EDGES = 3_000;
export const MAX_CANVAS_GRAPH_BYTES = 5 * 1024 * 1024;
export const MAX_CANVAS_NODE_CONFIG_BYTES = 64 * 1024;
export const MAX_CANVAS_COORDINATE = 10_000_000;
export const MAX_CANVAS_FRAMES = 1_000;

const IMAGE_ASPECT_RATIOS = new Set([
  "1:1",
  "16:9",
  "9:16",
  "21:9",
  "9:21",
  "10:7",
  "7:10",
  "4:5",
  "3:4",
  "4:3",
  "3:2",
  "2:3",
]);
const VIDEO_RESOLUTIONS = new Set(["480p", "720p", "1080p", "4k"]);
const VIDEO_ASPECT_RATIOS = new Set([
  "adaptive",
  "16:9",
  "4:3",
  "1:1",
  "3:4",
  "9:16",
  "21:9",
]);
const FIXED_SIZE_ALIGNMENT = 16;
const FIXED_SIZE_MAX_SIDE = 3_840;
const FIXED_SIZE_MIN_PIXELS = 655_360;
const FIXED_SIZE_MAX_PIXELS = 8_294_400;
const FIXED_SIZE_MAX_ASPECT = 21 / 9;

export interface CanvasConnectionInput {
  sourceNodeId: string;
  sourceHandle: string;
  targetNodeId: string;
  targetHandle: string;
}

export type ConnectionValidation =
  | {
      valid: true;
      dataType: CanvasDataType;
      sourceType: CanvasDataType;
      targetType: CanvasDataType;
    }
  | { valid: false; reason: string };

export type BulkConnectionValidation =
  | { valid: true }
  | { valid: false; edgeId: string; reason: string };

export interface CanvasConnectionValidationOptions {
  allowLegacyCardinality?: boolean;
}

export function createEmptyCanvasGraph(): CanvasGraph {
  return {
    schema_version: 1,
    nodes: [],
    edges: [],
    frames: [],
    settings: { snap_to_grid: false, grid_size: 16 },
  };
}

export function createDefaultCanvasGraph(): CanvasGraph {
  const prompt = createCanvasNode("prompt", { x: 80, y: 160 }, {
    id: "prompt-1",
    title: "创作提示词",
    config: { text: "", locked: false },
  });
  const image = createCanvasNode("image_generate", { x: 430, y: 130 }, {
    id: "image-generate-1",
  });
  return {
    ...createEmptyCanvasGraph(),
    nodes: [prompt, image],
    edges: [
      {
        id: "edge-prompt-image",
        source_node_id: prompt.id,
        source_handle: "text",
        target_node_id: image.id,
        target_handle: "prompt",
        data_type: "text",
        binding_mode: "follow_active",
        order: 0,
      },
    ],
  };
}

export function createCanvasTemplateGraph(template: string): CanvasGraph {
  switch (template) {
    case "image_to_video":
      return createImageToVideoTemplate();
    case "product_directions":
      return createBranchingImageTemplate(true);
    case "multi_ratio":
      return createBranchingImageTemplate(false);
    case "storyboard_video":
      return createStoryboardVideoTemplate();
    case "image_editing":
      return createImageEditingTemplate();
    case "inpaint":
      return createInpaintTemplate();
    case "reference_video":
      return createReferenceVideoTemplate();
    case "creative_campaign":
      return createCreativeCampaignTemplate();
    default:
      return createDefaultCanvasGraph();
  }
}

function createImageToVideoTemplate(): CanvasGraph {
  const graph = createDefaultCanvasGraph();
  const video = createCanvasNode(
    "video_image_generate",
    { x: 790, y: 120 },
    { id: "video-generate-1", title: "首帧视频" },
  );
  const delivery = createCanvasNode("delivery", { x: 1160, y: 140 }, {
    id: "delivery-1",
  });
  graph.nodes.push(video, delivery);
  graph.edges.push(
    templateEdge(
      "edge-prompt-video",
      "prompt-1",
      "text",
      video.id,
      "prompt",
      "text",
    ),
    templateEdge(
      "edge-image-video",
      "image-generate-1",
      "image",
      video.id,
      "first_frame",
      "image",
    ),
    templateEdge(
      "edge-video-delivery",
      video.id,
      "video",
      delivery.id,
      "videos",
      "video",
    ),
  );
  return graph;
}

function createBranchingImageTemplate(productDirections: boolean): CanvasGraph {
  const graph = createDefaultCanvasGraph();
  const ratios = productDirections
    ? ["4:5", "16:9"]
    : ["4:5", "9:16", "16:9"];
  const firstImage = graph.nodes.find(
    (node) => node.id === "image-generate-1",
  );
  if (firstImage && productDirections) firstImage.title = "视觉方向 1";
  ratios.forEach((ratio, index) => {
    const node = createCanvasNode(
      "image_generate",
      { x: 430, y: 370 + index * 240 },
      {
        id: `image-generate-${index + 2}`,
        title: productDirections
          ? `视觉方向 ${index + 2}`
          : `${ratio} 图片生成`,
        config: {
          ...CANVAS_NODE_SPECS.image_generate.defaultConfig,
          aspect_ratio: ratio,
        },
      },
    );
    graph.nodes.push(node);
    graph.edges.push(
      templateEdge(
        `edge-prompt-image-${index + 2}`,
        "prompt-1",
        "text",
        node.id,
        "prompt",
        "text",
      ),
    );
  });
  if (!productDirections) return graph;

  const product = createCanvasNode("image_asset", { x: 80, y: 430 }, {
    id: "product-reference-1",
    title: "商品参考",
    config: { display_name: "商品参考" },
    ui: { preset_id: "product_reference" },
  });
  graph.nodes.push(product);
  for (const image of graph.nodes.filter(
    (node) => node.type === "image_generate",
  )) {
    graph.edges.push(
      templateEdge(
        `edge-product-${image.id}`,
        product.id,
        "image",
        image.id,
        "references",
        "image",
        "product",
      ),
    );
  }
  return graph;
}

function createStoryboardVideoTemplate(): CanvasGraph {
  const graph = createImageToVideoTemplate();
  graph.nodes.unshift(
    createCanvasNode(
      "frame",
      { x: 30, y: 60 },
      {
        id: "frame-1",
        title: "关键帧到视频",
        size: { width: 1420, height: 520 },
      },
    ),
  );
  return graph;
}

function createImageEditingTemplate(): CanvasGraph {
  const prompt = createCanvasNode("prompt", { x: 80, y: 110 }, {
    id: "edit-prompt-1",
    title: "编辑指令",
  });
  const source = createCanvasNode("image_asset", { x: 80, y: 390 }, {
    id: "edit-source-1",
    title: "待编辑原图",
    config: { display_name: "待编辑原图" },
  });
  const edit = createCanvasNode("image_edit", { x: 450, y: 170 }, {
    id: "image-edit-1",
  });
  const delivery = createCanvasNode("delivery", { x: 830, y: 190 }, {
    id: "delivery-1",
  });
  return {
    ...createEmptyCanvasGraph(),
    nodes: [prompt, source, edit, delivery],
    edges: [
      templateEdge(
        "edge-edit-prompt",
        prompt.id,
        "text",
        edit.id,
        "prompt",
        "text",
      ),
      templateEdge(
        "edge-edit-source",
        source.id,
        "image",
        edit.id,
        "source",
        "image",
        "edit_target",
      ),
      templateEdge(
        "edge-edit-delivery",
        edit.id,
        "image",
        delivery.id,
        "images",
        "image",
      ),
    ],
  };
}

function createInpaintTemplate(): CanvasGraph {
  const prompt = createCanvasNode("prompt", { x: 80, y: 70 }, {
    id: "inpaint-prompt-1",
    title: "重绘指令",
  });
  const source = createCanvasNode("image_asset", { x: 80, y: 320 }, {
    id: "inpaint-source-1",
    title: "待重绘原图",
    config: { display_name: "待重绘原图" },
  });
  const mask = createCanvasNode("mask_asset", { x: 80, y: 570 }, {
    id: "inpaint-mask-1",
    title: "重绘遮罩",
    config: { display_name: "重绘遮罩" },
  });
  const inpaint = createCanvasNode("image_inpaint", { x: 460, y: 220 }, {
    id: "image-inpaint-1",
  });
  const delivery = createCanvasNode("delivery", { x: 840, y: 250 }, {
    id: "delivery-1",
  });
  return {
    ...createEmptyCanvasGraph(),
    nodes: [prompt, source, mask, inpaint, delivery],
    edges: [
      templateEdge(
        "edge-inpaint-prompt",
        prompt.id,
        "text",
        inpaint.id,
        "prompt",
        "text",
      ),
      templateEdge(
        "edge-inpaint-source",
        source.id,
        "image",
        inpaint.id,
        "source",
        "image",
        "edit_target",
      ),
      templateEdge(
        "edge-inpaint-mask",
        mask.id,
        "mask",
        inpaint.id,
        "mask",
        "mask",
      ),
      templateEdge(
        "edge-inpaint-delivery",
        inpaint.id,
        "image",
        delivery.id,
        "images",
        "image",
      ),
    ],
  };
}

function createReferenceVideoTemplate(): CanvasGraph {
  const prompt = createCanvasNode("prompt", { x: 80, y: 70 }, {
    id: "reference-video-prompt-1",
    title: "视频提示词",
  });
  const image = createCanvasNode("image_asset", { x: 80, y: 320 }, {
    id: "reference-image-1",
    title: "人物或风格参考",
  });
  const video = createCanvasNode("video_asset", { x: 80, y: 570 }, {
    id: "reference-video-1",
    title: "动作参考",
  });
  const generate = createCanvasNode(
    "video_reference_generate",
    { x: 470, y: 220 },
    { id: "reference-video-generate-1" },
  );
  const delivery = createCanvasNode("delivery", { x: 860, y: 250 }, {
    id: "delivery-1",
  });
  return {
    ...createEmptyCanvasGraph(),
    nodes: [prompt, image, video, generate, delivery],
    edges: [
      templateEdge(
        "edge-reference-prompt",
        prompt.id,
        "text",
        generate.id,
        "prompt",
        "text",
      ),
      templateEdge(
        "edge-reference-image",
        image.id,
        "image",
        generate.id,
        "reference_images",
        "image",
        "subject",
      ),
      templateEdge(
        "edge-reference-video",
        video.id,
        "video",
        generate.id,
        "reference_videos",
        "video",
      ),
      templateEdge(
        "edge-reference-delivery",
        generate.id,
        "video",
        delivery.id,
        "videos",
        "video",
      ),
    ],
  };
}

function createCreativeCampaignTemplate(): CanvasGraph {
  const frame = createCanvasNode("frame", { x: 30, y: 30 }, {
    id: "campaign-frame-1",
    title: "营销创意流水线",
    size: { width: 1500, height: 780 },
  });
  const brand = createCanvasNode("prompt", { x: 80, y: 90 }, {
    id: "campaign-brand-1",
    title: "品牌与商品信息",
  });
  const scene = createCanvasNode("prompt", { x: 80, y: 330 }, {
    id: "campaign-scene-1",
    title: "场景与视觉风格",
  });
  const constraints = createCanvasNode("prompt", { x: 80, y: 570 }, {
    id: "campaign-constraints-1",
    title: "文案与限制条件",
  });
  const merge = createCanvasNode("prompt_merge", { x: 400, y: 250 }, {
    id: "campaign-prompt-merge-1",
    title: "完整创意提示词",
    config: { separator: "\n\n", trim: true, dedupe: true },
  });
  const product = createCanvasNode("image_asset", { x: 410, y: 570 }, {
    id: "campaign-product-1",
    title: "商品参考",
    config: { display_name: "商品参考" },
    ui: { preset_id: "product_reference" },
  });
  const image = createCanvasNode("image_generate", { x: 760, y: 150 }, {
    id: "campaign-image-1",
    title: "商品主视觉",
    config: { aspect_ratio: "4:5", quality: "4k", size: "4K", fast: false },
    ui: { preset_id: "product_key_visual" },
  });
  const video = createCanvasNode(
    "video_image_generate",
    { x: 760, y: 500 },
    {
      id: "campaign-video-1",
      title: "竖屏动态短片",
      config: { aspect_ratio: "9:16", duration_s: 5 },
    },
  );
  const delivery = createCanvasNode("delivery", { x: 1150, y: 300 }, {
    id: "campaign-delivery-1",
  });
  return {
    ...createEmptyCanvasGraph(),
    nodes: [
      frame,
      brand,
      scene,
      constraints,
      merge,
      product,
      image,
      video,
      delivery,
    ],
    edges: [
      templateEdge(
        "edge-campaign-brand",
        brand.id,
        "text",
        merge.id,
        "texts",
        "text",
        null,
        0,
      ),
      templateEdge(
        "edge-campaign-scene",
        scene.id,
        "text",
        merge.id,
        "texts",
        "text",
        null,
        1,
      ),
      templateEdge(
        "edge-campaign-constraints",
        constraints.id,
        "text",
        merge.id,
        "texts",
        "text",
        null,
        2,
      ),
      templateEdge(
        "edge-campaign-image-prompt",
        merge.id,
        "text",
        image.id,
        "prompt",
        "text",
      ),
      templateEdge(
        "edge-campaign-product",
        product.id,
        "image",
        image.id,
        "references",
        "image",
        "product",
      ),
      templateEdge(
        "edge-campaign-video-prompt",
        merge.id,
        "text",
        video.id,
        "prompt",
        "text",
      ),
      templateEdge(
        "edge-campaign-first-frame",
        image.id,
        "image",
        video.id,
        "first_frame",
        "image",
      ),
      templateEdge(
        "edge-campaign-image-delivery",
        image.id,
        "image",
        delivery.id,
        "images",
        "image",
      ),
      templateEdge(
        "edge-campaign-video-delivery",
        video.id,
        "video",
        delivery.id,
        "videos",
        "video",
      ),
    ],
  };
}

function templateEdge(
  id: string,
  sourceNodeId: string,
  sourceHandle: string,
  targetNodeId: string,
  targetHandle: string,
  dataType: CanvasDataType,
  role: CanvasEdgeDefinition["role"] = null,
  order = 0,
): CanvasEdgeDefinition {
  return {
    id,
    source_node_id: sourceNodeId,
    source_handle: sourceHandle,
    target_node_id: targetNodeId,
    target_handle: targetHandle,
    data_type: dataType,
    binding_mode: "follow_active",
    role,
    order,
  };
}

export function canvasGraphReadyToSave(graph: CanvasGraph): boolean {
  if (
    graph.nodes.length > MAX_CANVAS_NODES ||
    graph.edges.length > MAX_CANVAS_EDGES ||
    graph.frames.length > MAX_CANVAS_FRAMES
  ) {
    return false;
  }
  for (const node of graph.nodes) {
    const configBytes = canvasJsonByteLength(node.config);
    if (
      configBytes === null ||
      configBytes > MAX_CANVAS_NODE_CONFIG_BYTES
    ) {
      return false;
    }
  }
  const graphBytes = canvasJsonByteLength(graph);
  return graphBytes !== null && graphBytes <= MAX_CANVAS_GRAPH_BYTES;
}

export function canvasJsonByteLength(value: unknown): number | null {
  try {
    const serialized = JSON.stringify(value);
    return serialized === undefined
      ? null
      : new TextEncoder().encode(serialized).byteLength;
  } catch {
    return null;
  }
}

export function normalizeCanvasGraph(value: unknown): CanvasGraph {
  if (!value || typeof value !== "object") return createDefaultCanvasGraph();
  const raw = value as Partial<CanvasGraph>;
  const nodes = Array.isArray(raw.nodes)
    ? raw.nodes
        .filter(isCanvasNodeDefinition)
        .map((node) => ({ ...node, ui: normalizeCanvasNodeUi(node.ui) }))
    : [];
  const edges = Array.isArray(raw.edges)
    ? raw.edges.filter(isCanvasEdgeDefinition)
    : [];
  return {
    schema_version: 1,
    nodes,
    edges,
    frames: Array.isArray(raw.frames) ? raw.frames : [],
    settings: {
      snap_to_grid: raw.settings?.snap_to_grid === true,
      grid_size:
        typeof raw.settings?.grid_size === "number" && raw.settings.grid_size > 0
          ? raw.settings.grid_size
          : 16,
    },
  };
}

function isCanvasNodeDefinition(value: unknown): value is CanvasNodeDefinition {
  if (!value || typeof value !== "object") return false;
  const node = value as Partial<CanvasNodeDefinition>;
  const type = typeof node.type === "string" && isCanvasNodeType(node.type)
    ? node.type
    : null;
  return (
    typeof node.id === "string" &&
    type !== null &&
    typeof node.title === "string" &&
    Boolean(node.position) &&
    canvasPositionIsValid(node.position) &&
    canvasNodeConfigIsValid(type, node.config) &&
    (node.ui === undefined || canvasNodeUiIsValid(node.ui))
  );
}

function isCanvasEdgeDefinition(value: unknown): value is CanvasEdgeDefinition {
  if (!value || typeof value !== "object") return false;
  const edge = value as Partial<CanvasEdgeDefinition>;
  return (
    typeof edge.id === "string" &&
    typeof edge.source_node_id === "string" &&
    typeof edge.source_handle === "string" &&
    typeof edge.target_node_id === "string" &&
    typeof edge.target_handle === "string"
  );
}

export function validateCanvasConnection(
  graph: CanvasGraph,
  input: CanvasConnectionInput,
  ignoreEdgeId?: string,
  options: CanvasConnectionValidationOptions = {},
): ConnectionValidation {
  if (input.sourceNodeId === input.targetNodeId) {
    return { valid: false, reason: "节点不能连接自身" };
  }
  const endpoints = resolveConnectionEndpoints(graph, input);
  if (!endpoints) return { valid: false, reason: "连接节点或端口不存在" };
  const { source, target, sourcePort, targetPort } = endpoints;

  const accepted = targetPort.accepts ?? [targetPort.dataType];
  if (!accepted.includes(sourcePort.dataType)) {
    return {
      valid: false,
      reason: `${sourcePort.dataType} 不能连接到 ${targetPort.dataType}`,
    };
  }
  const capacityError = targetInputCapacityError(
    graph,
    target,
    targetPort,
    ignoreEdgeId,
    options,
  );
  if (capacityError) {
    return { valid: false, reason: capacityError };
  }
  const modeError = videoModeConnectionError(target, targetPort.id);
  if (modeError) {
    return { valid: false, reason: modeError };
  }
  if (connectionExists(graph, input, ignoreEdgeId)) {
    return { valid: false, reason: "连接已存在" };
  }
  if (wouldCreateCanvasCycle(graph, source.id, target.id, ignoreEdgeId)) {
    return { valid: false, reason: "连接会形成环" };
  }

  return {
    valid: true,
    dataType: targetPort.dataType,
    sourceType: sourcePort.dataType,
    targetType: targetPort.dataType,
  };
}

export function validateCanvasConnections(
  graph: CanvasGraph,
  edges: readonly CanvasEdgeDefinition[],
  options: CanvasConnectionValidationOptions = {},
): BulkConnectionValidation {
  const workingEdges = [...graph.edges];
  const edgeIds = new Set(workingEdges.map((edge) => edge.id));
  for (const edge of edges) {
    if (edgeIds.has(edge.id)) {
      return {
        valid: false,
        edgeId: edge.id,
        reason: "连接 ID 重复",
      };
    }
    const validation = validateCanvasConnection(
      { ...graph, edges: workingEdges },
      {
        sourceNodeId: edge.source_node_id,
        sourceHandle: edge.source_handle,
        targetNodeId: edge.target_node_id,
        targetHandle: edge.target_handle,
      },
      undefined,
      options,
    );
    if (!validation.valid) {
      return { valid: false, edgeId: edge.id, reason: validation.reason };
    }
    if (validation.dataType !== edge.data_type) {
      return {
        valid: false,
        edgeId: edge.id,
        reason: "连接数据类型不匹配",
      };
    }
    edgeIds.add(edge.id);
    workingEdges.push(edge);
  }
  return { valid: true };
}

export function validateCanvasNodeExecution(
  graph: CanvasGraph,
  nodeId: string,
): { valid: true } | { valid: false; reason: string } {
  const node = graph.nodes.find((candidate) => candidate.id === nodeId);
  if (!node) return { valid: false, reason: "节点不存在" };
  if (!isCanvasExecutableNodeType(node.type)) {
    return { valid: false, reason: "该节点无需运行" };
  }
  const requiredInputError = canvasRequiredInputError(graph, node);
  if (requiredInputError) {
    return { valid: false, reason: requiredInputError };
  }
  const imageError = canvasImageExecutionError(graph, node);
  if (imageError) {
    return { valid: false, reason: imageError };
  }
  const configError = canvasExecutionConfigError(node);
  if (configError) {
    return { valid: false, reason: configError };
  }
  const modeError = canvasVideoExecutionError(graph, node);
  return modeError
    ? { valid: false, reason: modeError }
    : { valid: true };
}

export function canvasVideoCapabilityError(
  node: CanvasNodeDefinition,
  options: VideoOptionsOut,
): string | null {
  if (!isCanvasVideoNodeType(node.type)) return null;
  if (!options.enabled) {
    return options.unavailable_reason?.trim() || "视频生成功能当前不可用";
  }
  const action = canvasVideoModeForNode(node);
  if (!action) return "视频生成模式无效";
  return canvasVideoModelCapabilityError(node, options, action);
}

function canvasVideoModelCapabilityError(
  node: CanvasNodeDefinition,
  options: VideoOptionsOut,
  action: NonNullable<ReturnType<typeof canvasVideoModeForNode>>,
): string | null {
  const resolution = String(node.config.resolution ?? "720p");
  const configuredModel = String(node.config.model ?? "");
  const actionModels = options.models.filter((model) =>
    model.actions.includes(action),
  );
  if (actionModels.length === 0) return "当前模式没有可用的视频模型";
  const selectedModels = configuredModel
    ? actionModels.filter((model) => model.model === configuredModel)
    : actionModels;
  if (selectedModels.length === 0) return "当前视频模型不可用，请重新选择";
  const resolutionModels = selectedModels.filter(
    (model) =>
      !model.resolutions?.length ||
      model.resolutions.some((value) => value === resolution),
  );
  if (resolutionModels.length === 0) {
    return "当前视频分辨率不可用，请重新选择";
  }
  const aspectRatio = String(node.config.aspect_ratio ?? "16:9");
  if (
    options.aspect_ratios.length > 0 &&
    !options.aspect_ratios.includes(aspectRatio)
  ) {
    return "当前视频比例不可用，请重新选择";
  }
  return canvasVideoDurationCapabilityError(
    node,
    options,
    resolutionModels,
    action,
    resolution,
  );
}

function canvasVideoDurationCapabilityError(
  node: CanvasNodeDefinition,
  options: VideoOptionsOut,
  models: VideoOptionsOut["models"],
  action: NonNullable<ReturnType<typeof canvasVideoModeForNode>>,
  resolution: string,
): string | null {
  const duration = Number(node.config.duration_s ?? 5);
  const durationSupported = models.some((model) => {
    const values =
      model.durations_by_action_resolution?.[action]?.[resolution] ??
      model.durations_by_action?.[action] ??
      model.durations_s ??
      options.durations_s;
    return values.length === 0 || values.includes(duration);
  });
  return durationSupported ? null : "当前视频时长不可用，请重新选择";
}

function canvasExecutionConfigError(
  node: CanvasNodeDefinition,
): string | null {
  if (CANVAS_NODE_SPECS[node.type].family === "image") {
    return canvasImageExecutionConfigError(node);
  }
  if (CANVAS_NODE_SPECS[node.type].family !== "video") return null;
  return canvasVideoExecutionConfigError(node);
}

function canvasImageExecutionConfigError(
  node: CanvasNodeDefinition,
): string | null {
  const aspectRatio = String(node.config.aspect_ratio ?? "1:1");
  if (!IMAGE_ASPECT_RATIOS.has(aspectRatio)) {
    return "图片比例不受支持，请重新选择";
  }
  return node.config.size_mode === "fixed"
    ? canvasFixedSizeError(String(node.config.fixed_size ?? ""))
    : null;
}

function canvasVideoExecutionConfigError(
  node: CanvasNodeDefinition,
): string | null {
  const resolution = String(node.config.resolution ?? "720p");
  if (!VIDEO_RESOLUTIONS.has(resolution)) {
    return "视频分辨率不受支持，请重新选择";
  }
  const aspectRatio = String(node.config.aspect_ratio ?? "16:9");
  if (!VIDEO_ASPECT_RATIOS.has(aspectRatio)) {
    return "视频比例不受支持，请重新选择";
  }
  const seed = node.config.seed;
  if (
    seed !== null &&
    seed !== undefined &&
    (typeof seed !== "number" ||
      !Number.isSafeInteger(seed) ||
      seed < -1 ||
      seed > 4_294_967_295)
  ) {
    return "视频种子超出支持范围";
  }
  return null;
}

export function canvasFixedSizeError(value: string): string | null {
  const normalized = value.trim().toLowerCase().replace(/\s+/g, "");
  const match = /^([1-9]\d{1,4})x([1-9]\d{1,4})$/.exec(normalized);
  if (!match) return "请输入宽x高，例如 1536x1024";
  const width = Number(match[1]);
  const height = Number(match[2]);
  if (
    width % FIXED_SIZE_ALIGNMENT !== 0 ||
    height % FIXED_SIZE_ALIGNMENT !== 0
  ) {
    return `宽高必须是 ${FIXED_SIZE_ALIGNMENT} 的倍数`;
  }
  if (Math.max(width, height) > FIXED_SIZE_MAX_SIDE) {
    return `最长边不能超过 ${FIXED_SIZE_MAX_SIDE}`;
  }
  const pixels = width * height;
  if (pixels < FIXED_SIZE_MIN_PIXELS || pixels > FIXED_SIZE_MAX_PIXELS) {
    return "总像素需在 655360 至 8294400 之间";
  }
  if (Math.max(width, height) / Math.min(width, height) > FIXED_SIZE_MAX_ASPECT) {
    return "宽高比不能超过 21:9";
  }
  return null;
}

function canvasRequiredInputError(
  graph: CanvasGraph,
  node: CanvasNodeDefinition,
): string | null {
  for (const port of CANVAS_NODE_SPECS[node.type].inputs) {
    if (!port.required) continue;
    const edges = graph.edges.filter(
      (edge) =>
        edge.target_node_id === node.id && edge.target_handle === port.id,
    );
    if (edges.length === 0) {
      return `缺少${port.label}输入`;
    }
    if (port.dataType !== "text") continue;
    for (const edge of edges) {
      const resolution = resolveCanvasTextOutputResult(
        graph,
        edge.source_node_id,
      );
      if (resolution.error === "too_long") {
        return `提示词超过 ${MAX_PROMPT_CHARS.toLocaleString()} 字符`;
      }
      if (resolution.error) {
        return "提示词无法解析";
      }
      if (!resolution.value?.trim()) {
        return "提示词不能为空";
      }
    }
  }
  return null;
}

function canvasImageExecutionError(
  graph: CanvasGraph,
  node: CanvasNodeDefinition,
): string | null {
  if (node.type !== "image_generate") return null;
  const inputs = graph.edges.filter((edge) => edge.target_node_id === node.id);
  const maskCount = inputs.filter((edge) => edge.target_handle === "mask").length;
  const referenceCount = inputs.filter(
    (edge) => edge.target_handle === "references",
  ).length;
  return maskCount > 0 && referenceCount !== 1
    ? "遮罩需要且只能连接一张参考图"
    : null;
}

function canvasVideoExecutionError(
  graph: CanvasGraph,
  node: CanvasNodeDefinition,
): string | null {
  if (!isCanvasVideoNodeType(node.type)) return null;
  const mode = canvasVideoModeForNode(node);
  if (!mode) return null;
  const firstFrameCount = graph.edges.filter(
    (edge) =>
      edge.target_node_id === node.id && edge.target_handle === "first_frame",
  ).length;
  if (mode === "i2v" && firstFrameCount !== 1) {
    return "图生视频需要且只能连接一个首帧";
  }
  const referenceCount = graph.edges.filter(
    (edge) =>
      edge.target_node_id === node.id &&
      (edge.target_handle === "reference_images" ||
        edge.target_handle === "reference_videos"),
  ).length;
  return mode === "reference" && referenceCount === 0
    ? "参考视频模式至少需要一个参考素材"
    : null;
}

function resolveConnectionEndpoints(
  graph: CanvasGraph,
  input: CanvasConnectionInput,
) {
  const source = graph.nodes.find((node) => node.id === input.sourceNodeId);
  const target = graph.nodes.find((node) => node.id === input.targetNodeId);
  if (!source || !target) return null;
  const sourcePort = CANVAS_NODE_SPECS[source.type].outputs.find(
    (port) => port.id === input.sourceHandle,
  );
  const targetPort = CANVAS_NODE_SPECS[target.type].inputs.find(
    (port) => port.id === input.targetHandle,
  );
  return sourcePort && targetPort
    ? { source, target, sourcePort, targetPort }
    : null;
}

function targetInputCapacityError(
  graph: CanvasGraph,
  target: CanvasNodeDefinition,
  targetPort: (typeof CANVAS_NODE_SPECS)[CanvasNodeType]["inputs"][number],
  ignoreEdgeId?: string,
  options: CanvasConnectionValidationOptions = {},
): string | null {
  if (
    options.allowLegacyCardinality &&
    legacyCanvasPortHadUnboundedCardinality(target.type, targetPort.id)
  ) {
    return null;
  }
  const maximum = targetPort.maximum ?? (targetPort.multiple ? null : 1);
  if (maximum === null) return null;
  const count = graph.edges.filter(
    (edge) =>
      edge.id !== ignoreEdgeId &&
      edge.target_node_id === target.id &&
      edge.target_handle === targetPort.id,
  ).length;
  if (count < maximum) return null;
  return maximum === 1
    ? `${targetPort.label} 只允许一个输入`
    : `${targetPort.label} 最多允许 ${maximum} 个输入`;
}

function legacyCanvasPortHadUnboundedCardinality(
  nodeType: CanvasNodeType,
  portId: string,
): boolean {
  return (
    (nodeType === "image_generate" && portId === "references") ||
    (nodeType === "video_generate" &&
      (portId === "reference_images" || portId === "reference_videos"))
  );
}

function videoModeConnectionError(
  target: CanvasNodeDefinition,
  targetHandle: string,
): string | null {
  if (!isCanvasVideoNodeType(target.type)) return null;
  const mode = canvasVideoModeForNode(target);
  if (!mode) return null;
  const blocked =
    mode === "t2v"
      ? ["first_frame", "reference_images", "reference_videos"]
      : mode === "i2v"
        ? ["reference_images", "reference_videos"]
        : ["first_frame"];
  return blocked.includes(targetHandle) ? `${mode} 模式不接受此输入` : null;
}

function connectionExists(
  graph: CanvasGraph,
  input: CanvasConnectionInput,
  ignoreEdgeId?: string,
): boolean {
  return graph.edges.some(
    (edge) =>
      edge.id !== ignoreEdgeId &&
      edge.source_node_id === input.sourceNodeId &&
      edge.source_handle === input.sourceHandle &&
      edge.target_node_id === input.targetNodeId &&
      edge.target_handle === input.targetHandle,
  );
}

export function wouldCreateCanvasCycle(
  graph: CanvasGraph,
  sourceNodeId: string,
  targetNodeId: string,
  ignoreEdgeId?: string,
): boolean {
  const outgoing = new Map<string, string[]>();
  for (const edge of graph.edges) {
    if (edge.id === ignoreEdgeId) continue;
    const list = outgoing.get(edge.source_node_id) ?? [];
    list.push(edge.target_node_id);
    outgoing.set(edge.source_node_id, list);
  }
  const stack = [targetNodeId];
  const seen = new Set<string>();
  while (stack.length > 0) {
    const current = stack.pop();
    if (!current || seen.has(current)) continue;
    if (current === sourceNodeId) return true;
    seen.add(current);
    stack.push(...(outgoing.get(current) ?? []));
  }
  return false;
}

export function createCanvasEdge(
  graph: CanvasGraph,
  input: CanvasConnectionInput,
): CanvasEdgeDefinition | null {
  const validation = validateCanvasConnection(graph, input);
  if (!validation.valid) return null;
  const targetOrder = graph.edges.filter(
    (edge) =>
      edge.target_node_id === input.targetNodeId &&
      edge.target_handle === input.targetHandle,
  ).length;
  const source = graph.nodes.find((node) => node.id === input.sourceNodeId);
  const role =
    source && (validation.dataType === "image" || validation.dataType === "mask")
      ? canvasDefaultRoleForNode(source)
      : null;
  return {
    id: canvasUuid("edge"),
    source_node_id: input.sourceNodeId,
    source_handle: input.sourceHandle,
    target_node_id: input.targetNodeId,
    target_handle: input.targetHandle,
    data_type: validation.dataType,
    binding_mode: "follow_active",
    role,
    order: targetOrder,
  };
}

export function addCanvasNode(
  graph: CanvasGraph,
  type: CanvasNodeType,
  position: { x: number; y: number },
  overrides?: CanvasNodeCreateOverrides,
): { graph: CanvasGraph; node: CanvasNodeDefinition } {
  const node = createCanvasNode(type, position, overrides);
  return { graph: { ...graph, nodes: [...graph.nodes, node] }, node };
}

export function resolveCanvasTextOutput(
  graph: CanvasGraph,
  nodeId: string,
): string | null {
  return resolveCanvasTextOutputResult(graph, nodeId).value;
}

export type CanvasTextResolutionError =
  | "cycle"
  | "too_long"
  | "unresolved";

export interface CanvasTextResolution {
  value: string | null;
  error: CanvasTextResolutionError | null;
  actualLength?: number;
}

export function resolveCanvasTextOutputResult(
  graph: CanvasGraph,
  nodeId: string,
): CanvasTextResolution {
  return createCanvasTextResolver(graph)(nodeId);
}

export function resolveCanvasTextOutputs(
  graph: CanvasGraph,
): Map<string, CanvasTextResolution> {
  const resolve = createCanvasTextResolver(graph);
  return new Map(
    graph.nodes
      .filter((node) => node.type === "prompt" || node.type === "prompt_merge")
      .map((node) => [node.id, resolve(node.id)]),
  );
}

function createCanvasTextResolver(
  graph: CanvasGraph,
): (nodeId: string) => CanvasTextResolution {
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
  const incoming = canvasTextIncomingEdges(graph.edges);
  const resolved = new Map<string, CanvasTextResolution>();
  return (nodeId) =>
    resolveCanvasTextNode(nodes, incoming, resolved, nodeId);
}

function canvasTextIncomingEdges(
  edges: readonly CanvasEdgeDefinition[],
): Map<string, CanvasEdgeDefinition[]> {
  const incoming = new Map<string, CanvasEdgeDefinition[]>();
  for (const edge of edges) {
    if (edge.target_handle !== "texts") continue;
    incoming.set(edge.target_node_id, [
      ...(incoming.get(edge.target_node_id) ?? []),
      edge,
    ]);
  }
  for (const values of incoming.values()) {
    values.sort(
      (left, right) =>
        (left.order ?? 0) - (right.order ?? 0) ||
        left.id.localeCompare(right.id),
    );
  }
  return incoming;
}

function resolveCanvasTextNode(
  nodes: ReadonlyMap<string, CanvasNodeDefinition>,
  incoming: ReadonlyMap<string, CanvasEdgeDefinition[]>,
  resolved: Map<string, CanvasTextResolution>,
  rootId: string,
): CanvasTextResolution {
  const visiting = new Set<string>();
  const stack: Array<readonly [string, boolean]> = [[rootId, false]];
  while (stack.length > 0) {
    const current = stack.pop();
    if (!current) continue;
    const [nodeId, expanded] = current;
    if (resolved.has(nodeId)) continue;
    const node = nodes.get(nodeId);
    const leaf = canvasLeafTextResolution(node);
    if (leaf) {
      resolved.set(nodeId, leaf);
      continue;
    }
    const sourceIds = (incoming.get(nodeId) ?? []).map(
      (edge) => edge.source_node_id,
    );
    if (!expanded) {
      if (visiting.has(nodeId)) {
        return markCanvasTextCycle(resolved, visiting, rootId);
      }
      visiting.add(nodeId);
      stack.push([nodeId, true]);
      for (const sourceId of sourceIds.slice().reverse()) {
        if (visiting.has(sourceId)) {
          return markCanvasTextCycle(resolved, visiting, rootId);
        }
        if (!resolved.has(sourceId)) stack.push([sourceId, false]);
      }
      continue;
    }
    visiting.delete(nodeId);
    resolved.set(
      nodeId,
      mergeCanvasTextNode(node, sourceIds, resolved),
    );
  }
  return resolved.get(rootId) ?? unresolvedCanvasText();
}

function canvasLeafTextResolution(
  node: CanvasNodeDefinition | undefined,
): CanvasTextResolution | null {
  if (!node || (node.type !== "prompt" && node.type !== "prompt_merge")) {
    return unresolvedCanvasText();
  }
  if (node.type === "prompt_merge") return null;
  const value = typeof node.config.text === "string" ? node.config.text : "";
  return boundedCanvasText(value);
}

function mergeCanvasTextNode(
  node: CanvasNodeDefinition | undefined,
  sourceIds: string[],
  resolved: ReadonlyMap<string, CanvasTextResolution>,
): CanvasTextResolution {
  if (!node || node.type !== "prompt_merge") return unresolvedCanvasText();
  const childValues: string[] = [];
  for (const sourceId of sourceIds) {
    const child = resolved.get(sourceId) ?? unresolvedCanvasText();
    if (child.error) return child;
    childValues.push(child.value ?? "");
  }
  return mergeCanvasTextValues(node, childValues);
}

function mergeCanvasTextValues(
  node: CanvasNodeDefinition,
  values: string[],
): CanvasTextResolution {
  const separator =
    typeof node.config.separator === "string" ? node.config.separator : "\n\n";
  const prefix = typeof node.config.prefix === "string" ? node.config.prefix : "";
  const suffix = typeof node.config.suffix === "string" ? node.config.suffix : "";
  const trim = node.config.trim !== false;
  const dedupe = node.config.dedupe === true;
  const merged: string[] = [];
  const seen = new Set<string>();
  let length = prefix.length + suffix.length;
  if (length > MAX_PROMPT_CHARS) {
    return { value: null, error: "too_long", actualLength: length };
  }
  for (const rawValue of values) {
    const value = trim ? rawValue.trim() : rawValue;
    if (!value || (dedupe && seen.has(value))) continue;
    length += value.length + (merged.length > 0 ? separator.length : 0);
    if (length > MAX_PROMPT_CHARS) {
      return { value: null, error: "too_long", actualLength: length };
    }
    merged.push(value);
    seen.add(value);
  }
  return { value: `${prefix}${merged.join(separator)}${suffix}`, error: null };
}

function boundedCanvasText(value: string): CanvasTextResolution {
  return value.length > MAX_PROMPT_CHARS
    ? { value: null, error: "too_long", actualLength: value.length }
    : { value, error: null };
}

function unresolvedCanvasText(): CanvasTextResolution {
  return { value: null, error: "unresolved" };
}

function markCanvasTextCycle(
  resolved: Map<string, CanvasTextResolution>,
  visiting: ReadonlySet<string>,
  rootId: string,
): CanvasTextResolution {
  const result = { value: null, error: "cycle" } as const;
  for (const nodeId of visiting) resolved.set(nodeId, result);
  resolved.set(rootId, result);
  return result;
}

export function removeCanvasNodes(
  graph: CanvasGraph,
  nodeIds: string[],
): { graph: CanvasGraph; edgeIds: string[] } {
  const ids = new Set(nodeIds);
  const edgeIds = graph.edges
    .filter((edge) => ids.has(edge.source_node_id) || ids.has(edge.target_node_id))
    .map((edge) => edge.id);
  return {
    graph: {
      ...graph,
      nodes: graph.nodes.filter((node) => !ids.has(node.id)),
      edges: graph.edges.filter((edge) => !edgeIds.includes(edge.id)),
    },
    edgeIds,
  };
}

export function cloneCanvasGraph(graph: CanvasGraph): CanvasGraph {
  return structuredClone(graph);
}

function canvasPositionIsValid(
  position: { x?: unknown; y?: unknown } | null | undefined,
): position is { x: number; y: number } {
  return (
    typeof position?.x === "number" &&
    Number.isFinite(position.x) &&
    Math.abs(position.x) <= MAX_CANVAS_COORDINATE &&
    typeof position.y === "number" &&
    Number.isFinite(position.y) &&
    Math.abs(position.y) <= MAX_CANVAS_COORDINATE
  );
}
