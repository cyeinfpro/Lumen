import type {
  CanvasDocument,
  CanvasGraph,
  CanvasNodeExecution,
  CanvasOutput,
} from "#canvas-types";

export function latestExecutionsByNode(
  executions: CanvasNodeExecution[],
): Map<string, CanvasNodeExecution> {
  const map = new Map<string, CanvasNodeExecution>();
  for (const execution of executions) {
    if (!map.has(execution.node_id)) map.set(execution.node_id, execution);
  }
  return map;
}

export function activeOutputsByNode(
  document: Pick<CanvasDocument, "graph" | "selections" | "recent_executions">,
): Map<string, CanvasOutput> {
  const map = new Map<string, CanvasOutput>();
  const executions = new Map(
    document.recent_executions.map((execution) => [execution.id, execution]),
  );
  for (const node of document.graph.nodes) {
    if (node.type === "image_asset" || node.type === "mask_asset") {
      const imageId = stringValue(node.config.image_id);
      if (imageId) {
        map.set(node.id, {
          type: "image",
          image_id: imageId,
        });
      }
      continue;
    }
    if (node.type === "video_asset") {
      const videoId = stringValue(node.config.video_id);
      if (videoId) {
        map.set(node.id, {
          type: "video",
          video_id: videoId,
        });
      }
      continue;
    }
    const selection = document.selections.find(
      (candidate) =>
        candidate.node_id === node.id && candidate.execution_id !== null,
    );
    if (!selection || selection.execution_id === null) continue;
    const output = executions.get(selection.execution_id)?.outputs[selection.output_index];
    if (output) map.set(node.id, output);
  }
  return map;
}

export function deliveryOutputsForNode(
  graph: CanvasGraph,
  deliveryNodeId: string,
  outputs: Map<string, CanvasOutput>,
  executions: CanvasNodeExecution[],
): CanvasOutput[] {
  const executionById = new Map(
    executions.map((execution) => [execution.id, execution]),
  );
  return graph.edges
    .filter((edge) => edge.target_node_id === deliveryNodeId)
    .sort((left, right) => (left.order ?? 0) - (right.order ?? 0))
    .map((edge) => {
      if (
        edge.binding_mode === "pinned" &&
        edge.pinned_execution_id &&
        edge.pinned_output_index != null
      ) {
        return executionById.get(edge.pinned_execution_id)?.outputs[
          edge.pinned_output_index
        ];
      }
      return outputs.get(edge.source_node_id);
    })
    .filter((output): output is CanvasOutput => Boolean(output));
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}
