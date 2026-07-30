import { CANVAS_NODE_SPECS } from "@/lib/canvas/registry";
import type {
  CanvasDataType,
  CanvasEdgeDefinition,
  CanvasGraph,
  CanvasNodeDefinition,
  ConnectionDraft,
} from "@/lib/canvas/types";
import type {
  ConnectionCompatibility,
} from "./CanvasViewportModel";
import type { CanvasConnectionInput } from "@/lib/canvas/graph";

interface BuildConnectionCompatibilityOptions {
  isValid: (
    input: CanvasConnectionInput,
    targetDataType: CanvasDataType,
  ) => boolean;
  targetPosition: (
    node: CanvasNodeDefinition,
  ) => { x: number; y: number };
}

export function buildCanvasConnectionCompatibility(
  graph: CanvasGraph,
  draft: ConnectionDraft | null,
  options: BuildConnectionCompatibilityOptions,
): ConnectionCompatibility {
  const handlesByNode = new Map<string, string[]>();
  const targets: ConnectionCompatibility["targets"] = [];
  if (!draft) return { handlesByNode, targets };

  for (const node of graph.nodes) {
    const handles: string[] = [];
    for (const port of CANVAS_NODE_SPECS[node.type].inputs) {
      const input = {
        sourceNodeId: draft.sourceNodeId,
        sourceHandle: draft.sourceHandle,
        targetNodeId: node.id,
        targetHandle: port.id,
      };
      if (!options.isValid(input, port.dataType)) continue;
      handles.push(port.id);
      targets.push({
        key: `${node.id}:${port.id}`,
        nodeId: node.id,
        nodeTitle: node.title,
        nodeType: CANVAS_NODE_SPECS[node.type].label,
        handleId: port.id,
        handleLabel: port.label,
        ...options.targetPosition(node),
      });
    }
    if (handles.length > 0) handlesByNode.set(node.id, handles);
  }
  return { handlesByNode, targets };
}

export function createCanvasConnectionCandidate(
  graph: CanvasGraph,
  input: CanvasConnectionInput,
  targetDataType?: CanvasDataType,
): CanvasEdgeDefinition | null {
  const resolvedTargetDataType =
    targetDataType ?? canvasConnectionTargetDataType(graph, input);
  if (!resolvedTargetDataType) return null;
  return {
    id: canvasConnectionCandidateId(graph),
    source_node_id: input.sourceNodeId,
    source_handle: input.sourceHandle,
    target_node_id: input.targetNodeId,
    target_handle: input.targetHandle,
    data_type: resolvedTargetDataType,
    binding_mode: "follow_active",
  };
}

function canvasConnectionTargetDataType(
  graph: CanvasGraph,
  input: CanvasConnectionInput,
): CanvasDataType | null {
  const targetNode = graph.nodes.find((node) => node.id === input.targetNodeId);
  const targetPort = targetNode
    ? CANVAS_NODE_SPECS[targetNode.type].inputs.find(
        (port) => port.id === input.targetHandle,
      )
    : null;
  return targetPort?.dataType ?? null;
}

function canvasConnectionCandidateId(graph: CanvasGraph): string {
  let id = "__canvas_connection_candidate__";
  const existingIds = new Set(graph.edges.map((edge) => edge.id));
  while (existingIds.has(id)) id += "_";
  return id;
}
