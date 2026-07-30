import type { Edge } from "@xyflow/react";
import { useMemo } from "react";

import {
  resolveCanvasTextOutputs,
  validateCanvasNodeExecution,
} from "@/lib/canvas/graph";
import {
  activeOutputsByNode,
  latestExecutionsByNode,
} from "@/lib/canvas/runtime";
import type {
  CanvasDocument,
  CanvasGraph,
  CanvasNodeDefinition,
  CanvasPosition,
} from "@/lib/canvas/types";
import { canvasEdgeAriaLabel } from "./CanvasViewportModel";
import type { CanvasFlowNode } from "./nodes/CanvasNodes";

export interface CanvasNodeProjectionContext {
  execution: ReturnType<typeof latestExecutionsByNode> extends Map<
    string,
    infer T
  >
    ? T | null
    : never;
  activeOutput: ReturnType<typeof activeOutputsByNode> extends Map<
    string,
    infer T
  >
    ? T | null
    : never;
  activeOutputs: ReturnType<typeof activeOutputsByNode>;
  inputCounts: Record<string, number>;
  resolvedText?: string;
  runDisabledReason: string | null;
}

interface UseCanvasViewportProjectionOptions {
  document: CanvasDocument;
  graph: CanvasGraph;
  selectedEdgeId: string | null;
  transientPositions: Record<string, CanvasPosition>;
  projectNode: (
    node: CanvasNodeDefinition,
    context: CanvasNodeProjectionContext,
  ) => CanvasFlowNode;
}

export function useCanvasViewportProjection({
  document,
  graph,
  selectedEdgeId,
  transientPositions,
  projectNode,
}: UseCanvasViewportProjectionOptions) {
  const executions = useMemo(
    () => latestExecutionsByNode(document.recent_executions),
    [document.recent_executions],
  );
  const activeOutputs = useMemo(
    () =>
      activeOutputsByNode({
        graph,
        selections: document.selections,
        recent_executions: document.recent_executions,
      }),
    [document.recent_executions, document.selections, graph],
  );
  const inputCountsByNode = useMemo(() => {
    const counts = new Map<string, Record<string, number>>();
    for (const edge of graph.edges) {
      const nodeCounts = counts.get(edge.target_node_id) ?? {};
      nodeCounts[edge.target_handle] =
        (nodeCounts[edge.target_handle] ?? 0) + 1;
      counts.set(edge.target_node_id, nodeCounts);
    }
    return counts;
  }, [graph.edges]);
  const resolvedTextsByNode = useMemo(() => {
    const values = new Map<string, string>();
    const resolutions = resolveCanvasTextOutputs(graph);
    for (const node of graph.nodes) {
      if (node.type !== "prompt_merge") continue;
      values.set(node.id, resolutions.get(node.id)?.value ?? "");
    }
    return values;
  }, [graph]);

  const projectedNodes = useMemo(
    () =>
      graph.nodes.map((node) =>
        projectNode(node, {
          execution: executions.get(node.id) ?? null,
          activeOutput: activeOutputs.get(node.id) ?? null,
          activeOutputs,
          inputCounts: inputCountsByNode.get(node.id) ?? {},
          resolvedText: resolvedTextsByNode.get(node.id),
          runDisabledReason: canvasRunDisabledReason(graph, node.id),
        }),
      ),
    [
      activeOutputs,
      executions,
      graph,
      inputCountsByNode,
      projectNode,
      resolvedTextsByNode,
    ],
  );
  const flowNodes = useMemo(
    () =>
      projectedNodes.map((node) => {
        const transient = transientPositions[node.id];
        return transient
          ? { ...node, position: transient, dragging: true }
          : node;
      }),
    [projectedNodes, transientPositions],
  );
  const graphNodesById = useMemo(
    () => new Map(graph.nodes.map((node) => [node.id, node])),
    [graph.nodes],
  );
  const flowEdges = useMemo<Edge[]>(
    () =>
      graph.edges.map((edge) => ({
        id: edge.id,
        source: edge.source_node_id,
        sourceHandle: edge.source_handle,
        target: edge.target_node_id,
        targetHandle: edge.target_handle,
        selected: edge.id === selectedEdgeId,
        label: edge.role || undefined,
        ariaLabel: canvasEdgeAriaLabel(graphNodesById, edge),
        type: "smoothstep",
      })),
    [graph.edges, graphNodesById, selectedEdgeId],
  );

  return { activeOutputs, flowEdges, flowNodes };
}

function canvasRunDisabledReason(
  graph: CanvasGraph,
  nodeId: string,
): string | null {
  const validation = validateCanvasNodeExecution(graph, nodeId);
  return validation.valid ? null : validation.reason;
}
