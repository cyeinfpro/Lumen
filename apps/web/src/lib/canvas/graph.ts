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
import {
  videoReferenceLimitError,
  videoUnavailableReasonMessage,
  type VideoReferenceCounts,
} from "../video/optionsModel";
import { createDefaultCanvasGraph } from "./graphTemplates";

export {
  createCanvasTemplateGraph,
  createDefaultCanvasGraph,
  createEmptyCanvasGraph,
} from "./graphTemplates";

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
  graph?: CanvasGraph,
): string | null {
  if (!isCanvasVideoNodeType(node.type)) return null;
  if (!options.enabled) {
    return videoUnavailableReasonMessage(options.unavailable_reason);
  }
  const action = canvasVideoModeForNode(node);
  if (!action) return "视频生成模式无效";
  return canvasVideoModelCapabilityError(node, options, action, graph);
}

export function canvasVideoReferenceCounts(
  graph: CanvasGraph,
  nodeId: string,
): VideoReferenceCounts {
  const counts: VideoReferenceCounts = { image: 0, video: 0, audio: 0 };
  for (const edge of graph.edges) {
    if (edge.target_node_id !== nodeId) continue;
    if (edge.target_handle === "reference_images") counts.image += 1;
    if (edge.target_handle === "reference_videos") counts.video += 1;
  }
  return counts;
}

function canvasReferenceCompatibleModels(
  models: VideoOptionsOut["models"],
  {
    action,
    configuredModel,
    graph,
    nodeId,
  }: {
    action: NonNullable<ReturnType<typeof canvasVideoModeForNode>>;
    configuredModel: string;
    graph?: CanvasGraph;
    nodeId: string;
  },
): { models: VideoOptionsOut["models"]; error: string | null } {
  if (!graph || action !== "reference") return { models, error: null };
  const counts = canvasVideoReferenceCounts(graph, nodeId);
  const compatible = models.filter(
    (model) => videoReferenceLimitError(model, counts) === null,
  );
  if (compatible.length > 0) return { models: compatible, error: null };
  return {
    models: compatible,
    error: configuredModel
      ? videoReferenceLimitError(models[0], counts)
      : "没有视频模型支持当前连接的参考素材",
  };
}

function canvasVideoModelCapabilityError(
  node: CanvasNodeDefinition,
  options: VideoOptionsOut,
  action: NonNullable<ReturnType<typeof canvasVideoModeForNode>>,
  graph?: CanvasGraph,
): string | null {
  const resolution = String(node.config.resolution ?? "720p");
  const configuredModel = String(node.config.model ?? "");
  const actionModels = options.models.filter((model) =>
    model.actions.includes(action),
  );
  if (actionModels.length === 0) return "当前模式没有可用的视频模型";
  const configuredModels = configuredModel
    ? actionModels.filter((model) => model.model === configuredModel)
    : actionModels;
  if (configuredModels.length === 0) return "当前视频模型不可用，请重新选择";
  const referenceSelection = canvasReferenceCompatibleModels(configuredModels, {
    action,
    configuredModel,
    graph,
    nodeId: node.id,
  });
  if (referenceSelection.error) return referenceSelection.error;
  const selectedModels = referenceSelection.models;
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
