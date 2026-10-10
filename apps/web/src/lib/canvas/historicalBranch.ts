import { CANVAS_NODE_SPECS, createCanvasNode } from "#canvas-registry";
import { validateCanvasConnection, validateCanvasNodeExecution } from "#canvas-graph";
import { completeExecutionProvenance, historyRecord } from "./executionHistory";
import type { CanvasEdgeDefinition, CanvasGraph, CanvasHistoricalExecution, CanvasNodeDefinition } from "./types";

export type HistoricalBranchResult =
  | { ok: true; graph: CanvasGraph; nodeId: string; nodes: CanvasNodeDefinition[]; edges: CanvasEdgeDefinition[] }
  | { ok: false; reason: string };
type Binding = Record<string, unknown>;
type Context = { graph: CanvasGraph; target: CanvasNodeDefinition; nodes: CanvasNodeDefinition[]; edges: CanvasEdgeDefinition[]; prompts: string[] };
const text = (value: unknown): value is string => typeof value === "string" && value.length > 0;
function fail(reason: string): never { throw new Error(reason); }

function savedBindings(execution: CanvasHistoricalExecution): Binding[] {
  if (!completeExecutionProvenance(execution)) fail("历史快照不完整，无法安全创建分支");
  const bindings = execution.input_snapshot!.bindings as unknown[];
  if (bindings.length === 0 || bindings.length > 100) fail("历史输入来源缺失或超过支持范围");
  const seen = new Set<string>();
  return bindings.map((raw) => {
    const binding = historyRecord(raw);
    if (!binding || !text(binding.edge_id) || seen.has(binding.edge_id)) fail("历史输入绑定缺失或重复");
    if (!text(binding.source_node_id) || !text(binding.target_handle)) fail("历史输入来源或端口缺失");
    if (!Number.isSafeInteger(binding.order) || (binding.order as number) < 0) fail("历史输入顺序缺失");
    if (!["follow_active", "pinned"].includes(String(binding.binding_mode))) fail("历史输入绑定方式缺失");
    seen.add(binding.edge_id);
    return binding;
  });
}
function savedTarget(graph: CanvasGraph, execution: CanvasHistoricalExecution): CanvasNodeDefinition {
  const original = graph.nodes.find((node) => node.id === execution.node_id);
  if (!original || original.type !== execution.node_type || original.schema_version !== 1 ||
    !CANVAS_NODE_SPECS[original.type]?.executable) fail("原节点缺失或节点类型已改变");
  const target = createCanvasNode(original.type, { x: original.position.x + 380, y: original.position.y + 80 }, {
    title: original.title + " · 历史分支",
  });
  target.config = structuredClone(execution.config_snapshot!);
  return target;
}
function savedAsset(binding: Binding): Binding {
  const asset = historyRecord(binding.asset);
  if (!asset) fail("历史素材快照缺失");
  const isVideo = text(asset.video_id), isImage = text(asset.image_id);
  if (isVideo === isImage || !text(asset.sha256) || !/^[a-f0-9]{64}$/.test(asset.sha256)) fail("历史素材身份或内容校验值缺失");
  return asset;
}
function checkGeneratedBinding(binding: Binding, asset: Binding) {
  if (!text(binding.source_execution_id) || !Number.isSafeInteger(binding.output_index) || (binding.output_index as number) < 0 ||
    asset.source_execution_id !== binding.source_execution_id || asset.output_index !== binding.output_index) fail("缺少固定执行或精确输出索引");
}
function literalTextSource(context: Context, source: CanvasNodeDefinition, binding: Binding, index: number): CanvasNodeDefinition {
  if (typeof binding.text !== "string" || binding.source_execution_id != null || binding.asset != null) fail("历史文本缺失或来源类型已改变");
  context.prompts.push(binding.text);
  const position = { x: context.target.position.x - 330, y: context.target.position.y + index * 210 };
  const literal = createCanvasNode("prompt", position, { title: "历史提示词", config: { text: binding.text, locked: true } });
  if (binding.text === binding.text.trim()) return literal;
  // A direct prompt is trimmed by backend input resolution. Reconstruct the
  // saved merge value through an explicit no-trim merge to preserve whitespace.
  if (source.type !== "prompt_merge") fail("历史直接提示词包含不一致的首尾空白");
  const merge = createCanvasNode("prompt_merge", { x: position.x, y: position.y + 210 }, {
    title: "历史文本 · 保留空白", config: { separator: "", prefix: "", suffix: "", trim: false, dedupe: false },
  });
  context.nodes.push(literal);
  context.edges.push({ id: merge.id + "-literal", source_node_id: literal.id, source_handle: "text",
    target_node_id: merge.id, target_handle: "texts", data_type: "text", binding_mode: "follow_active", order: 0 });
  return merge;
}

function literalSource(context: Context, source: CanvasNodeDefinition, binding: Binding, index: number): CanvasNodeDefinition {
  const position = { x: context.target.position.x - 330, y: context.target.position.y + index * 210 };
  if (source.type === "prompt" || source.type === "prompt_merge") {
    return literalTextSource(context, source, binding, index);
  }
  const asset = savedAsset(binding), isVideo = text(asset.video_id);
  if (binding.source_execution_id != null || !["image_asset", "mask_asset", "video_asset"].includes(source.type)) fail("历史素材来源类型已改变");
  if (isVideo !== (source.type === "video_asset")) fail("历史素材来源类型已改变");
  return createCanvasNode(source.type, position, {
    title: "历史素材", config: isVideo ? { video_id: asset.video_id, display_name: "历史素材" }
      : { image_id: asset.image_id, display_name: "历史素材", crop: null },
  });
}
function savedSource(context: Context, binding: Binding, index: number) {
  const source = context.graph.nodes.find((node) => node.id === binding.source_node_id);
  if (!source || !CANVAS_NODE_SPECS[source.type]) fail("历史输入来源节点已删除，无法创建固定分支");
  if (CANVAS_NODE_SPECS[source.type].executable) {
    checkGeneratedBinding(binding, savedAsset(binding));
    return { source, generated: true };
  }
  const literal = literalSource(context, source, binding, index);
  context.nodes.push(literal);
  return { source: literal, generated: false };
}
function savedPorts(context: Context, source: CanvasNodeDefinition, binding: Binding) {
  const targetPort = CANVAS_NODE_SPECS[context.target.type].inputs.find((port) => port.id === binding.target_handle);
  if (!targetPort) fail("历史目标端口已不兼容");
  const accepted = targetPort.accepts ?? [targetPort.dataType];
  const sourcePorts = CANVAS_NODE_SPECS[source.type].outputs.filter((port) => accepted.includes(port.dataType));
  if (sourcePorts.length !== 1) fail("无法唯一确定兼容的历史来源端口");
  const asset = historyRecord(binding.asset), dataType = sourcePorts[0].dataType;
  if (asset?.video_id && dataType !== "video") fail("历史视频与来源端口不兼容");
  if (asset?.image_id && !["image", "mask"].includes(dataType)) fail("历史图片与来源端口不兼容");
  return { sourcePort: sourcePorts[0], targetPort };
}
function appendBinding(context: Context, binding: Binding, index: number) {
  const { source, generated } = savedSource(context, binding, index);
  const { sourcePort, targetPort } = savedPorts(context, source, binding);
  const candidate = { ...context.graph, nodes: [...context.graph.nodes, ...context.nodes], edges: [...context.graph.edges, ...context.edges] };
  const input = { sourceNodeId: source.id, sourceHandle: sourcePort.id, targetNodeId: context.target.id, targetHandle: targetPort.id };
  const validation = validateCanvasConnection(candidate, input);
  if (!validation.valid) fail(validation.reason);
  context.edges.push({
    id: context.target.id + "-history-" + index, source_node_id: source.id, source_handle: sourcePort.id,
    target_node_id: context.target.id, target_handle: targetPort.id, data_type: validation.dataType,
    binding_mode: generated ? "pinned" : "follow_active",
    pinned_execution_id: generated ? binding.source_execution_id as string : null,
    pinned_output_index: generated ? binding.output_index as number : null,
    role: binding.role == null ? null : binding.role as CanvasEdgeDefinition["role"], order: binding.order as number,
  });
}

// Draft-only reconstruction. Never copy live config/inputs or active selections.
// Generated sources pin the exact saved execution/output; literals are independent.
export function buildHistoricalBranch(graph: CanvasGraph, execution: CanvasHistoricalExecution): HistoricalBranchResult {
  try {
    const bindings = savedBindings(execution), target = savedTarget(graph, execution);
    const context: Context = { graph, target, nodes: [target], edges: [], prompts: [] };
    bindings.forEach((binding, index) => appendBinding(context, binding, index));
    if (context.prompts.length !== 1 || context.prompts[0] !== execution.input_snapshot!.prompt) fail("历史提示词与输入绑定不一致");
    const nextGraph = { ...graph, nodes: [...graph.nodes, ...context.nodes], edges: [...graph.edges, ...context.edges] };
    const validation = validateCanvasNodeExecution(nextGraph, target.id);
    if (!validation.valid) fail(validation.reason);
    return { ok: true, graph: nextGraph, nodeId: target.id, nodes: context.nodes, edges: context.edges };
  } catch (error) {
    return { ok: false, reason: error instanceof Error ? error.message : "历史来源验证失败" };
  }
}
