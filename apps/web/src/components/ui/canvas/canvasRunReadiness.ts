import {
  canvasVideoCapabilityError,
  validateCanvasNodeExecution,
} from "@/lib/canvas/graph";
import { isCanvasVideoNodeType } from "@/lib/canvas/registry";
import { latestExecutionsByNode } from "@/lib/canvas/runtime";
import { canvasTaskSubmissionIsUnknown } from "@/lib/canvas/generationDetails";
import type { CanvasDocument, CanvasGraph, CanvasSaveState } from "@/lib/canvas/types";
import type { VideoOptionsOut } from "@/lib/billingVideoTypes";

const ACTIVE_EXECUTION_STATUSES = new Set([
  "pending", "ready", "queued", "running", "reconciling", "canceling",
]);
const ACTIVE_RUN_STATUSES = new Set([
  "planning", "queued", "running", "reconciling", "canceling",
]);

export function canvasUncertainNodeIds(
  document: Pick<CanvasDocument, "recent_executions">,
): Set<string> {
  return new Set([...latestExecutionsByNode(document.recent_executions).values()]
    .filter((execution) => execution.tasks?.some(canvasTaskSubmissionIsUnknown))
    .map((execution) => execution.node_id));
}

export function canvasActiveNodeIds(
  document: Pick<CanvasDocument, "recent_executions" | "active_runs">,
): Set<string> {
  const uncertain = canvasUncertainNodeIds(document);
  const nodeIds = new Set(
    [...latestExecutionsByNode(document.recent_executions).values()]
      .filter((execution) => ACTIVE_EXECUTION_STATUSES.has(execution.status) && !uncertain.has(execution.node_id))
      .map((execution) => execution.node_id),
  );
  for (const run of document.active_runs) {
    if (!ACTIVE_RUN_STATUSES.has(run.status)) continue;
    for (const nodeId of run.target_node_ids ?? []) {
      if (!uncertain.has(nodeId)) nodeIds.add(nodeId);
    }
  }
  return nodeIds;
}

export function canvasRunBusyReason(
  nodeId: string, active: ReadonlySet<string>, submitting: ReadonlySet<string>, uncertain: ReadonlySet<string>,
): string | null {
  if (uncertain.has(nodeId)) return "上次提交状态待确认，请先查询原任务";
  return active.has(nodeId) || submitting.has(nodeId) ? "节点运行中，等待当前任务完成" : null;
}

export function assertCanvasRunIntentMatches(graph: CanvasGraph, checkedGraphKey: string) {
  if (canvasRunGraphKey(graph) !== checkedGraphKey) {
    throw new Error("节点配置已变化，请重新运行");
  }
}

export interface CanvasRunScope {
  workflowId: string;
  revision: number;
  graphKey: string;
}

export interface CanvasRunSnapshot extends CanvasRunScope {
  graph: CanvasGraph;
  structuralReasons: ReadonlyMap<string, string | null>;
  videoNodeIds: ReadonlySet<string>;
}

export interface CanvasRunDiagnostics extends CanvasRunScope {
  reasons: ReadonlyMap<string, string | null>;
  videoOptions?: VideoOptionsOut;
}

/** Ignore layout-only edits, but fence unsaved input changes at the same revision. */
export function canvasRunGraphKey(graph: CanvasGraph): string {
  return JSON.stringify({
    nodes: graph.nodes.map(({ id, type, config }) => ({ id, type, config })),
    edges: graph.edges,
  });
}

export function createCanvasRunSnapshot(
  workflowId: string,
  revision: number,
  graph: CanvasGraph,
): CanvasRunSnapshot {
  const structuralReasons = new Map<string, string | null>();
  const videoNodeIds = new Set<string>();
  for (const node of graph.nodes) {
    const result = validateCanvasNodeExecution(graph, node.id);
    structuralReasons.set(node.id, result.valid ? null : result.reason);
    if (isCanvasVideoNodeType(node.type)) videoNodeIds.add(node.id);
  }
  return {
    workflowId,
    revision,
    graphKey: canvasRunGraphKey(graph),
    graph,
    structuralReasons,
    videoNodeIds,
  };
}

export function canvasRunScopeMatches(
  current: CanvasRunScope,
  result: CanvasRunScope | undefined,
): boolean {
  return Boolean(
    result &&
      result.workflowId === current.workflowId &&
      result.revision === current.revision &&
      result.graphKey === current.graphKey,
  );
}

/** One read-only capability lookup for the entire graph. Never execute a node here. */
export async function loadCanvasRunDiagnostics(
  snapshot: CanvasRunSnapshot,
  loadVideoOptions: () => Promise<VideoOptionsOut>,
  signal: AbortSignal,
): Promise<CanvasRunDiagnostics> {
  const videoOptions = snapshot.videoNodeIds.size > 0
    ? await loadVideoOptions()
    : undefined;
  signal.throwIfAborted();
  const reasons = new Map(snapshot.structuralReasons);
  if (videoOptions) {
    for (const node of snapshot.graph.nodes) {
      if (!snapshot.videoNodeIds.has(node.id) || reasons.get(node.id)) continue;
      reasons.set(
        node.id,
        canvasVideoCapabilityError(node, videoOptions, snapshot.graph),
      );
    }
  }
  return {
    workflowId: snapshot.workflowId,
    revision: snapshot.revision,
    graphKey: snapshot.graphKey,
    reasons,
    videoOptions,
  };
}

export function projectCanvasRunDisabledReasons({
  snapshot,
  diagnostics,
  failed,
  runningNodeIds,
  uncertainNodeIds,
  saveState,
}: {
  snapshot: CanvasRunSnapshot;
  diagnostics?: CanvasRunDiagnostics;
  failed: boolean;
  runningNodeIds: ReadonlySet<string>;
  uncertainNodeIds?: ReadonlySet<string>;
  saveState: CanvasSaveState;
}): ReadonlyMap<string, string | null> {
  const current = canvasRunScopeMatches(snapshot, diagnostics);
  const reasons = new Map(snapshot.structuralReasons);
  for (const [nodeId, structuralReason] of reasons) {
    if (uncertainNodeIds?.has(nodeId)) {
      reasons.set(nodeId, "上次提交状态待确认，请先查询原任务");
    } else if (runningNodeIds.has(nodeId)) {
      reasons.set(nodeId, "节点运行中，等待当前任务完成");
    } else if (saveState === "conflict") {
      reasons.set(nodeId, "画布存在保存冲突，解决后再运行");
    } else if (!structuralReason && snapshot.videoNodeIds.has(nodeId)) {
      // A transient projection failure must not permanently disable Start:
      // the authoritative run path still reloads capabilities before submission.
      reasons.set(
        nodeId,
        failed ? null : current
          ? diagnostics!.reasons.get(nodeId) ?? null
          : "视频能力检查中",
      );
    }
  }
  return reasons;
}
