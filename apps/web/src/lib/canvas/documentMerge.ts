import type {
  CanvasDocument,
  CanvasNodeExecution,
  CanvasNodeSelection,
  CanvasRun,
} from "./types";

export type CanvasRemoteSyncDecision =
  | "ignore"
  | "defer"
  | "replace"
  | "conflict";

export function decideCanvasRemoteSync(
  remoteRevision: number,
  state: {
    revision: number;
    pendingOperationCount: number;
    inFlightOperationCount: number;
    activeInteractionCount: number;
    editingNodeId: string | null;
  },
): CanvasRemoteSyncDecision {
  if (remoteRevision <= state.revision) return "ignore";
  if (state.inFlightOperationCount > 0) return "defer";
  return state.pendingOperationCount === 0 &&
    state.activeInteractionCount === 0 &&
    state.editingNodeId === null
    ? "replace"
    : "conflict";
}

export function mergeCanvasDocumentByRevision(
  current: CanvasDocument | undefined,
  incoming: CanvasDocument,
): CanvasDocument {
  if (!current || incoming.revision > current.revision) return incoming;
  if (incoming.revision < current.revision) return current;
  const preserveMissingCurrent =
    compareDocumentProjection(current, incoming) > 0;
  return {
    ...incoming,
    selections: mergeProjectionItems(
      current.selections,
      incoming.selections,
      (selection) => selection.node_id,
      compareSelectionProjection,
      preserveMissingCurrent,
    ),
    recent_executions: mergeProjectionItems(
      current.recent_executions,
      incoming.recent_executions,
      (execution) => execution.id,
      compareExecutionProjection,
      preserveMissingCurrent,
    ),
    active_runs: mergeProjectionItems(
      current.active_runs,
      incoming.active_runs,
      (run) => run.id,
      compareRunProjection,
      preserveMissingCurrent,
    ),
  };
}

export function mergeCanvasPatchResult(
  current: CanvasDocument | undefined,
  incoming: CanvasDocument,
  input: { title?: string; description?: string },
): CanvasDocument {
  if (!current || incoming.revision >= current.revision) {
    return mergeCanvasDocumentByRevision(current, incoming);
  }
  return {
    ...current,
    ...input,
    updated_at:
      incoming.updated_at > current.updated_at
        ? incoming.updated_at
        : current.updated_at,
  };
}

function mergeProjectionItems<T>(
  current: readonly T[],
  incoming: readonly T[],
  keyOf: (item: T) => string,
  compare: (left: T, right: T) => number,
  preserveMissingCurrent: boolean,
): T[] {
  const currentByKey = new Map(current.map((item) => [keyOf(item), item]));
  const merged = incoming.map((item) => {
    const existing = currentByKey.get(keyOf(item));
    return existing && compare(existing, item) > 0 ? existing : item;
  });
  if (!preserveMissingCurrent) return merged;
  const incomingKeys = new Set(incoming.map(keyOf));
  return [
    ...merged,
    ...current.filter((item) => !incomingKeys.has(keyOf(item))),
  ];
}

function compareDocumentProjection(
  left: CanvasDocument,
  right: CanvasDocument,
): number {
  const leftVersion = documentProjectionVersion(left);
  const rightVersion = documentProjectionVersion(right);
  const timestampDifference =
    leftVersion.timestamp - rightVersion.timestamp;
  return timestampDifference !== 0
    ? timestampDifference
    : leftVersion.sequence - rightVersion.sequence;
}

function documentProjectionVersion(document: CanvasDocument): {
  timestamp: number;
  sequence: number;
} {
  return {
    timestamp: Math.max(
    ...document.recent_executions.map((execution) =>
      projectionTimestamp(
        execution.updated_at,
        execution.finished_at,
        execution.started_at,
        execution.created_at,
      ),
    ),
    ...document.active_runs.map((run) =>
        projectionTimestamp(run.updated_at, run.created_at),
    ),
    Number.NEGATIVE_INFINITY,
    ),
    sequence: Math.max(
      ...document.selections.map((selection) =>
        projectionRevision(selection.revision),
      ),
      ...document.active_runs.map((run) =>
        projectionRevision(run.last_event_seq),
      ),
      Number.NEGATIVE_INFINITY,
    ),
  };
}

function compareSelectionProjection(
  left: CanvasNodeSelection,
  right: CanvasNodeSelection,
): number {
  return projectionRevision(left.revision) - projectionRevision(right.revision);
}

function compareExecutionProjection(
  left: CanvasNodeExecution,
  right: CanvasNodeExecution,
): number {
  return (
    projectionTimestamp(
      left.updated_at,
      left.finished_at,
      left.started_at,
      left.created_at,
    ) -
    projectionTimestamp(
      right.updated_at,
      right.finished_at,
      right.started_at,
      right.created_at,
    )
  );
}

function compareRunProjection(left: CanvasRun, right: CanvasRun): number {
  const sequenceDifference =
    projectionRevision(left.last_event_seq) -
    projectionRevision(right.last_event_seq);
  if (sequenceDifference !== 0) return sequenceDifference;
  return (
    projectionTimestamp(left.updated_at, left.created_at) -
    projectionTimestamp(right.updated_at, right.created_at)
  );
}

function projectionRevision(value: number | undefined): number {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : Number.NEGATIVE_INFINITY;
}

function projectionTimestamp(...values: Array<string | null | undefined>): number {
  let latest = Number.NEGATIVE_INFINITY;
  for (const value of values) {
    if (!value) continue;
    const timestamp = Date.parse(value);
    if (Number.isFinite(timestamp) && timestamp > latest) latest = timestamp;
  }
  return latest;
}
