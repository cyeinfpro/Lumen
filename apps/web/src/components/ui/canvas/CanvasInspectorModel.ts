import type { fetchVideoOptions } from "@/lib/video/requestLifecycle";
import {
  canvasVideoCapabilityError,
  validateCanvasNodeExecution,
} from "@/lib/canvas/graph";
import {
  CANVAS_NODE_SPECS,
  findMatchingCanvasNodeCatalogItem,
  isCanvasExecutableNodeType,
} from "@/lib/canvas/registry";
import type {
  CanvasDocument,
  CanvasNodeDefinition,
} from "@/lib/canvas/types";

export function inspectorRunDisabledReason(
  graph: CanvasDocument["graph"],
  node: CanvasNodeDefinition,
): string | null {
  if (!isCanvasExecutableNodeType(node.type)) return null;
  const validation = validateCanvasNodeExecution(graph, node.id);
  return validation.valid ? null : validation.reason;
}

export function inspectorVideoRunDisabledReason(
  graph: CanvasDocument["graph"],
  node: CanvasNodeDefinition,
  options: Awaited<ReturnType<typeof fetchVideoOptions>> | undefined,
  loading: boolean,
  error: string | null,
): string | null {
  if (CANVAS_NODE_SPECS[node.type].family !== "video") return null;
  if (loading) return "视频能力加载中";
  if (error) return error;
  if (!options) return "视频能力未加载";
  return canvasVideoCapabilityError(node, options, graph);
}

export function queryErrorMessage(
  isError: boolean,
  error: unknown,
  fallback: string,
): string | null {
  if (!isError) return null;
  return error instanceof Error ? error.message : fallback;
}

export function canvasNodePreset(node: CanvasNodeDefinition) {
  return findMatchingCanvasNodeCatalogItem(node);
}

export function incompatibleVideoConnectionCount(
  graph: CanvasDocument["graph"],
  node: CanvasNodeDefinition,
  nextConfig: Record<string, unknown>,
): number {
  if (node.type !== "video_generate") return 0;
  const currentMode = String(node.config.mode ?? "t2v");
  const nextMode = String(nextConfig.mode ?? "t2v");
  if (currentMode === nextMode) return 0;
  const blocked =
    nextMode === "t2v"
      ? new Set(["first_frame", "reference_images", "reference_videos"])
      : nextMode === "i2v"
        ? new Set(["reference_images", "reference_videos"])
        : new Set(["first_frame"]);
  return graph.edges.filter(
    (edge) =>
      edge.target_node_id === node.id && blocked.has(edge.target_handle),
  ).length;
}
