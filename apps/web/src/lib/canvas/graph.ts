import {
  CANVAS_NODE_SPECS,
  canvasUuid,
  createCanvasNode,
} from "#canvas-registry";
import type {
  CanvasDataType,
  CanvasEdgeDefinition,
  CanvasGraph,
  CanvasNodeDefinition,
  CanvasNodeType,
} from "#canvas-types";

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
  if (template === "image_to_video") {
    const graph = createDefaultCanvasGraph();
    const video = createCanvasNode("video_generate", { x: 790, y: 120 }, {
      id: "video-generate-1",
      config: {
        mode: "i2v",
        model: null,
        duration_s: 5,
        resolution: "720p",
        aspect_ratio: "16:9",
        generate_audio: true,
        seed: null,
        watermark: false,
      },
    });
    const delivery = createCanvasNode("delivery", { x: 1160, y: 140 }, {
      id: "delivery-1",
    });
    graph.nodes.push(video, delivery);
    graph.edges.push(
      {
        id: "edge-prompt-video",
        source_node_id: "prompt-1",
        source_handle: "text",
        target_node_id: video.id,
        target_handle: "prompt",
        data_type: "text",
        binding_mode: "follow_active",
        order: 0,
      },
      {
        id: "edge-image-video",
        source_node_id: "image-generate-1",
        source_handle: "image",
        target_node_id: video.id,
        target_handle: "first_frame",
        data_type: "image",
        binding_mode: "follow_active",
        order: 0,
      },
      {
        id: "edge-video-delivery",
        source_node_id: video.id,
        source_handle: "video",
        target_node_id: delivery.id,
        target_handle: "videos",
        data_type: "video",
        binding_mode: "follow_active",
        order: 0,
      },
    );
    return graph;
  }

  if (template === "multi_ratio" || template === "product_directions") {
    const graph = createDefaultCanvasGraph();
    const ratios =
      template === "multi_ratio" ? ["4:5", "9:16", "16:9"] : ["1:1", "4:5"];
    ratios.forEach((ratio, index) => {
      const node = createCanvasNode(
        "image_generate",
        { x: 430, y: 390 + index * 240 },
        {
          id: `image-generate-${index + 2}`,
          title:
            template === "multi_ratio"
              ? `${ratio} 图片生成`
              : `视觉方向 ${index + 2}`,
          config: {
            ...CANVAS_NODE_SPECS.image_generate.defaultConfig,
            aspect_ratio: ratio,
          },
        },
      );
      graph.nodes.push(node);
      graph.edges.push({
        id: `edge-prompt-image-${index + 2}`,
        source_node_id: "prompt-1",
        source_handle: "text",
        target_node_id: node.id,
        target_handle: "prompt",
        data_type: "text",
        binding_mode: "follow_active",
        order: 0,
      });
    });
    return graph;
  }

  if (template === "storyboard_video") {
    const graph = createCanvasTemplateGraph("image_to_video");
    graph.nodes.unshift(
      createCanvasNode("frame", { x: 30, y: 60 }, {
        id: "frame-1",
        title: "关键帧到视频",
        size: { width: 1420, height: 520 },
      }),
    );
    return graph;
  }

  return createDefaultCanvasGraph();
}

export function canvasGraphReadyToSave(graph: CanvasGraph): boolean {
  return graph.nodes.length <= 1_000 && graph.edges.length <= 3_000;
}

export function normalizeCanvasGraph(value: unknown): CanvasGraph {
  if (!value || typeof value !== "object") return createDefaultCanvasGraph();
  const raw = value as Partial<CanvasGraph>;
  const nodes = Array.isArray(raw.nodes)
    ? raw.nodes.filter(isCanvasNodeDefinition)
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
  return (
    typeof node.id === "string" &&
    typeof node.type === "string" &&
    node.type in CANVAS_NODE_SPECS &&
    typeof node.title === "string" &&
    Boolean(node.position) &&
    typeof node.position?.x === "number" &&
    typeof node.position?.y === "number" &&
    Boolean(node.config) &&
    typeof node.config === "object"
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
  if (targetInputIsFull(graph, target.id, targetPort.id, targetPort.multiple, ignoreEdgeId)) {
    return { valid: false, reason: `${targetPort.label} 只允许一个输入` };
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

export function validateCanvasNodeExecution(
  graph: CanvasGraph,
  nodeId: string,
): { valid: true } | { valid: false; reason: string } {
  const node = graph.nodes.find((candidate) => candidate.id === nodeId);
  if (!node) return { valid: false, reason: "节点不存在" };
  if (node.type !== "image_generate" && node.type !== "video_generate") {
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
  const modeError = canvasVideoExecutionError(graph, node);
  return modeError
    ? { valid: false, reason: modeError }
    : { valid: true };
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
    for (const edge of edges) {
      const source = graph.nodes.find(
        (candidate) => candidate.id === edge.source_node_id,
      );
      if (source?.type === "prompt" && !String(source.config.text ?? "").trim()) {
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
  if (node.type !== "video_generate") return null;
  const mode = String(node.config.mode ?? "t2v");
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

function targetInputIsFull(
  graph: CanvasGraph,
  targetNodeId: string,
  targetHandle: string,
  multiple: boolean | undefined,
  ignoreEdgeId?: string,
): boolean {
  if (multiple) return false;
  return graph.edges.some(
    (edge) =>
      edge.id !== ignoreEdgeId &&
      edge.target_node_id === targetNodeId &&
      edge.target_handle === targetHandle,
  );
}

function videoModeConnectionError(
  target: CanvasNodeDefinition,
  targetHandle: string,
): string | null {
  if (target.type !== "video_generate") return null;
  const mode = String(target.config.mode ?? "t2v");
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
  return {
    id: canvasUuid("edge"),
    source_node_id: input.sourceNodeId,
    source_handle: input.sourceHandle,
    target_node_id: input.targetNodeId,
    target_handle: input.targetHandle,
    data_type: validation.dataType,
    binding_mode: "follow_active",
    order: targetOrder,
  };
}

export function addCanvasNode(
  graph: CanvasGraph,
  type: CanvasNodeType,
  position: { x: number; y: number },
): { graph: CanvasGraph; node: CanvasNodeDefinition } {
  const node = createCanvasNode(type, position);
  return { graph: { ...graph, nodes: [...graph.nodes, node] }, node };
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
